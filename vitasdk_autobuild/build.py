"""Building one package, inside the SDK image.

The worker owns no package. It takes whatever the queue says is ready, builds
it with the recipe repository's own build script, and uploads the result.
"""

import fnmatch
import json
import os
import pwd
import shlex
import shutil
import subprocess
import tempfile
from typing import Iterable

from . import config, gh, queue
from .gh import Asset
from .queue import Package
from .utils import clean_environ, group


class BuildError(Exception):
    pass


def get_packager() -> str:
    """Identifies the run that produced a package, recorded in .PKGINFO."""

    repo = os.environ.get("GITHUB_REPOSITORY", config.PACKAGES_REPO)
    urls = gh.get_current_run_urls()
    reference = urls.get("job") or urls.get("build") or f"https://github.com/{repo}"
    return f"CI ({reference})"


def get_build_user() -> str | None:
    """The unprivileged user a build runs as, or None if we are not root.

    Recipes are arbitrary code and the worker holds a token that can write to
    the package store. Dropping to another user puts a kernel boundary between
    the two: /proc/<pid>/environ of a different user is not readable.
    """

    if os.geteuid() != 0:
        return None
    for name in ("vita", "builder", "nobody"):
        try:
            pwd.getpwnam(name)
        except KeyError:
            continue
        return name
    return None


def select_dependency_assets(package: Package, assets: Iterable[Asset]) -> list[Asset]:
    """The exact package files a build needs, dependencies first."""

    by_name = {asset.filename: asset for asset in assets}
    selected: list[Asset] = []
    missing: list[str] = []
    for dependency in queue.install_order(package):
        for pattern in dependency.build_patterns():
            matches = sorted(fnmatch.filter(by_name, pattern))
            if matches:
                selected.append(by_name[matches[0]])
            elif queue.is_optional_dep(package, dependency):
                # Part of a broken cycle: build without it and hope the recipe
                # copes, which is the only thing that can be done here.
                pass
            else:
                missing.append(pattern)
    if missing:
        raise BuildError(f"{package.name}: missing dependency packages: "
                         + ", ".join(missing))
    return selected


def expected_outputs(package: Package, produced: Iterable[str]) -> list[str]:
    """Checks that the build produced exactly the versions the queue expects."""

    names = list(produced)
    found = []
    for pattern in package.build_patterns():
        matches = fnmatch.filter(names, pattern)
        if not matches:
            raise BuildError(
                f"{pattern} not found, likely a different version was built "
                f"(produced: {', '.join(sorted(names)) or 'nothing'})")
        found.extend(matches)
    return found


def install_dependencies(assets: list[Asset], sdk: str) -> None:
    if not assets:
        return
    directory = tempfile.mkdtemp(prefix="vitasdk-deps-")
    try:
        paths = []
        for asset in assets:
            path = os.path.join(directory, asset.filename)
            print(f"Fetching {asset.filename}", flush=True)
            gh.download_asset(asset, path)
            paths.append(path)

        # The bootstrap archive ships no var/ tree: vdpm and vita-makepkg
        # create it on demand, and a direct pacman call has to do the same or
        # pacman refuses the paths it is handed.
        for relative in ("var/lib/pacman", "var/cache/pacman/pkg", "var/log"):
            os.makedirs(os.path.join(sdk, relative), exist_ok=True)

        subprocess.run([
            os.path.join(sdk, "bin", "pacman"),
            "--config", os.path.join(sdk, "etc", "pacman.conf"),
            "--root", sdk,
            "--dbpath", os.path.join(sdk, "var", "lib", "pacman"),
            "--cachedir", os.path.join(sdk, "var", "cache", "pacman", "pkg"),
            "--logfile", os.path.join(sdk, "var", "log", "pacman.log"),
            "--noscriptlet", "--noconfirm", "--noprogressbar",
            "--upgrade", "--asdeps", "--needed", *paths,
        ], check=True)
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def run_build(package: Package, packages_dir: str, output_dir: str,
              source_date_epoch: str) -> list[str]:
    """Runs the recipe repository's build script and returns what it produced."""

    script = os.path.join(packages_dir, "build.sh")
    if not os.path.exists(script):
        raise BuildError(f"{script} not found: the recipe repository decides "
                         f"how a package is built, and it has no build script")

    environ = clean_environ(dict(os.environ))
    environ["SOURCE_DATE_EPOCH"] = source_date_epoch
    environ["PACKAGER"] = get_packager()

    command = ["bash", script, package.repo_path, output_dir]
    user = get_build_user()
    if user is not None:
        for path in (output_dir, os.path.join(packages_dir, package.repo_path)):
            subprocess.run(["chown", "-R", user, path], check=True)
        # env(1) rather than sudo's --preserve-env, because sudoers can reset
        # PATH behind our back and vita-makepkg is found through it.
        command = ["sudo", "-u", user, "--", "env",
                   f"SOURCE_DATE_EPOCH={environ['SOURCE_DATE_EPOCH']}",
                   f"PACKAGER={environ['PACKAGER']}",
                   f"VITASDK={environ.get('VITASDK', '')}",
                   f"PATH={environ.get('PATH', '')}",
                   *command]

    print("$ " + shlex.join(command), flush=True)
    result = subprocess.run(command, cwd=packages_dir, env=environ)
    if result.returncode != 0:
        raise BuildError(f"{package.name}: build failed with exit code {result.returncode}")

    return sorted(name for name in os.listdir(output_dir) if ".pkg.tar." in name)


def build_package(package: Package, packages_dir: str, sdk: str,
                  source_date_epoch: str, staging: gh.Release,
                  assets: list[Asset]) -> list[str]:
    """Installs dependencies, builds, and uploads. Returns uploaded names."""

    output_dir = tempfile.mkdtemp(prefix="vitasdk-out-")
    try:
        install_dependencies(select_dependency_assets(package, assets), sdk)
        produced = run_build(package, packages_dir, output_dir, source_date_epoch)
        uploaded = expected_outputs(package, produced)
        for name in uploaded:
            gh.upload_asset(staging, name, path=os.path.join(output_dir, name))
        return uploaded
    finally:
        shutil.rmtree(output_dir, ignore_errors=True)


def report_failure(package: Package, failed: gh.Release) -> None:
    """Records a failure so the queue stops retrying it every round."""

    content = json.dumps({"urls": gh.get_current_run_urls()}, indent=2).encode()
    gh.upload_asset(failed, package.failed_name(), content=content, replace=True)


def build_one(package: Package, packages_dir: str, sdk: str, source_date_epoch: str,
              staging: gh.Release, failed: gh.Release, assets: list[Asset]) -> bool:
    with group(f"{package.name} {package.version}"):
        try:
            build_package(package, packages_dir, sdk, source_date_epoch, staging, assets)
        except (BuildError, subprocess.CalledProcessError) as e:
            print(f"FAILED: {e}", flush=True)
            report_failure(package, failed)
            return False
    return True
