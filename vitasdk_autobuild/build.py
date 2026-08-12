"""Building one package, inside the SDK image.

The worker owns no package. It takes whatever the queue says is ready, builds
it with the recipe repository's own build script, and uploads the result.
"""

import fnmatch
import json
import os
import shlex
import shutil
import subprocess
import tempfile
from typing import Iterable

from . import config, gh, queue
from .config import World
from .gh import Asset
from .queue import Package
from .utils import as_build_user, clean_environ, give_to_build_user, group


class BuildError(Exception):
    pass


def get_packager() -> str:
    """Identifies the run that produced a package, recorded in .PKGINFO."""

    repo = os.environ.get("GITHUB_REPOSITORY", config.PACKAGES_REPO)
    urls = gh.get_current_run_urls()
    reference = urls.get("job") or urls.get("build") or f"https://github.com/{repo}"
    return f"CI ({reference})"


def select_dependency_assets(package: Package, world: World,
                             assets: Iterable[Asset]) -> list[Asset]:
    """The exact package files a build needs, dependencies first.

    Only files of the same world: a package never links against another
    world's build of the same library.
    """

    by_name = {asset.filename: asset for asset in assets}
    selected: list[Asset] = []
    missing: list[str] = []
    for dependency in queue.install_order(package, world):
        for pattern in dependency.build_patterns(world):
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


def expected_outputs(package: Package, world: World,
                     produced: Iterable[str]) -> list[str]:
    """Checks that the build produced exactly the versions the queue expects."""

    names = list(produced)
    found = []
    for pattern in package.build_patterns(world):
        matches = fnmatch.filter(names, pattern)
        if not matches:
            raise BuildError(
                f"{pattern} not found, likely a different version was built "
                f"(produced: {', '.join(sorted(names)) or 'nothing'})")
        found.extend(matches)
    return found


# Operations that change the installed set. pacman rejects transaction
# options on a query with "invalid option", which is the same defect
# vita-makepkg already had to fix in run_pacman.
TRANSACTIONS = ("--upgrade", "--remove", "--sync", "-U", "-R", "-S")

# Options only a transaction accepts. Not just --noscriptlet: --noconfirm and
# --noprogressbar are refused by a query too.
TRANSACTION_OPTIONS = ("--noscriptlet", "--noconfirm", "--noprogressbar")


def pacman(sdk: str, *arguments: str, check: bool = True) -> subprocess.CompletedProcess:
    """Runs the SDK's own pacman against the SDK prefix.

    As the build user, not as root: the prefix belongs to that user and the
    package client is rootless by design. Running it as root would leave
    root-owned directories inside the prefix, and a recipe that installs into
    the SDK rather than into $pkgdir would then fail on a permission its
    predecessor did not need.
    """

    command = as_build_user([
        os.path.join(sdk, "bin", "pacman"),
        "--config", os.path.join(sdk, "etc", "pacman.conf"),
        "--root", sdk,
        "--dbpath", os.path.join(sdk, "var", "lib", "pacman"),
        "--cachedir", os.path.join(sdk, "var", "cache", "pacman", "pkg"),
        "--logfile", os.path.join(sdk, "var", "log", "pacman.log"),
        *(TRANSACTION_OPTIONS if any(a in TRANSACTIONS for a in arguments) else ()),
        *arguments,
    ], os.environ, ["PATH", "HOME", "VITASDK"])
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        # Without this the client's own explanation is swallowed and the
        # failure reads as a bare exit code.
        print((result.stderr or result.stdout).strip(), flush=True)
        if check:
            raise subprocess.CalledProcessError(result.returncode, command,
                                                result.stdout, result.stderr)
    return result


def installed_dependencies(sdk: str) -> list[str]:
    """Packages present only because an earlier build needed them."""

    if not os.path.exists(os.path.join(sdk, "var", "lib", "pacman")):
        return []
    result = pacman(sdk, "--query", "--deps", "--quiet", check=False)
    return sorted(line.strip() for line in result.stdout.splitlines() if line.strip())


def reset_dependencies(sdk: str) -> list[str]:
    """Returns the SDK to the state it was shipped in.

    A worker builds many packages in a row into the same prefix, so what an
    earlier build installed is still there. That is how openssl-1.1.1 ends up
    refusing to install next to the openssl a previous package pulled in, and
    every other way one build can quietly change the next one.
    """

    installed = installed_dependencies(sdk)
    if installed:
        print(f"Removing {len(installed)} package(s) from the previous build", flush=True)
        # -dd: the removal order does not matter, they all go.
        pacman(sdk, "--remove", "--nodeps", "--nodeps", *installed)
    return installed


def prepare_prefix(sdk: str) -> None:
    """Creates the state directories pacman needs, owned by the build user.

    The bootstrap archive ships no var/ tree: vdpm and vita-makepkg create it
    on demand. Creating it here as root would leave the package database
    unwritable by the build user, and makepkg needs to read and create it to
    generate .BUILDINFO.
    """

    for relative in ("var/lib/pacman", "var/cache/pacman/pkg", "var/log"):
        os.makedirs(os.path.join(sdk, relative), exist_ok=True)
    give_to_build_user(os.path.join(sdk, "var"))


def install_dependencies(assets: list[Asset], sdk: str) -> None:
    prepare_prefix(sdk)
    reset_dependencies(sdk)
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
        # Downloaded as root into a private directory, but read by pacman as
        # the build user.
        give_to_build_user(directory)

        pacman(sdk, "--upgrade", "--asdeps", "--needed", *paths)
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def world_environment(world: World, sdk: str, environ: dict[str, str]) -> dict[str, str]:
    """Points the build at one world.

    A world is chosen by the image the worker runs in: each carries a complete
    SDK for one EABI, so the SDK's own makepkg configuration already targets
    it and nothing has to be forced here. The optional per-world configuration
    exists only for an SDK that ever ships more than one.

    Nothing verifies the image here, and nothing needs to: a build that
    produced another architecture would not match the expected file name and
    fails instead of uploading something mislabelled.
    """

    configuration = os.path.join(sdk, "etc", f"makepkg-{world.arch}.conf")
    if os.path.exists(configuration):
        environ["MAKEPKG_CONF"] = configuration
    return environ


def run_build(package: Package, world: World, packages_dir: str, output_dir: str,
              source_date_epoch: str, sdk: str) -> list[str]:
    """Runs the recipe repository's build script and returns what it produced."""

    script = os.path.join(packages_dir, "build.sh")
    if not os.path.exists(script):
        raise BuildError(f"{script} not found: the recipe repository decides "
                         f"how a package is built, and it has no build script")

    environ = clean_environ(dict(os.environ))
    environ["SOURCE_DATE_EPOCH"] = source_date_epoch
    environ["PACKAGER"] = get_packager()
    world_environment(world, sdk, environ)

    for path in (output_dir, os.path.join(packages_dir, package.repo_path)):
        give_to_build_user(path)
    command = as_build_user(
        ["bash", script, package.repo_path, output_dir], environ,
        ["SOURCE_DATE_EPOCH", "PACKAGER", "VITASDK", "PATH", "HOME", "MAKEPKG_CONF"])

    print("$ " + shlex.join(command), flush=True)
    result = subprocess.run(command, cwd=packages_dir, env=environ)
    if result.returncode != 0:
        raise BuildError(f"{package.name}: build failed with exit code {result.returncode}")

    return sorted(name for name in os.listdir(output_dir) if ".pkg.tar." in name)


def build_package(package: Package, world: World, packages_dir: str, sdk: str,
                  source_date_epoch: str, staging: gh.Release,
                  assets: list[Asset]) -> list[str]:
    """Installs dependencies, builds, and uploads. Returns uploaded names."""

    output_dir = tempfile.mkdtemp(prefix="vitasdk-out-")
    try:
        install_dependencies(select_dependency_assets(package, world, assets), sdk)
        produced = run_build(package, world, packages_dir, output_dir,
                             source_date_epoch, sdk)
        uploaded = expected_outputs(package, world, produced)
        for name in uploaded:
            gh.upload_asset(staging, name, path=os.path.join(output_dir, name))
        return uploaded
    finally:
        shutil.rmtree(output_dir, ignore_errors=True)


def report_failure(package: Package, world: World, failed: gh.Release) -> None:
    """Records a failure so the queue stops retrying it every round."""

    content = json.dumps({"urls": gh.get_current_run_urls()}, indent=2).encode()
    gh.upload_asset(failed, package.failed_name(world), content=content, replace=True)


def build_one(package: Package, world: World, packages_dir: str, sdk: str,
              source_date_epoch: str, staging: gh.Release, failed: gh.Release,
              assets: list[Asset]) -> bool:
    with group(f"[{world.arch}] {package.name} {package.version}"):
        try:
            build_package(package, world, packages_dir, sdk, source_date_epoch,
                          staging, assets)
        except (BuildError, subprocess.CalledProcessError) as e:
            print(f"FAILED: {e}", flush=True)
            report_failure(package, world, failed)
            return False
    return True
