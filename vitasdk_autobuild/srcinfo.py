"""Package metadata, straight from vita-makepkg.

The recipes are the only source of truth, and vita-makepkg already knows how
to read them: `--printsrcinfo` evaluates a VITABUILD the same way a build
does. Nothing here re-implements that parsing.
"""

import hashlib
import os
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from . import config
from .utils import as_build_user, run

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
SRCINFO_CONF = os.path.join(DATA_DIR, "srcinfo.conf")

# Fields that may appear more than once and are collected into a list.
ARRAY_FIELDS = frozenset({
    "arch", "license", "groups", "depends", "makedepends", "checkdepends",
    "optdepends", "provides", "conflicts", "replaces", "source", "backup",
    "options", "validpgpkeys", "noextract",
})


def cache_dir() -> str:
    root = os.environ.get("VITASDK_AUTOBUILD_CACHE") or os.path.join(
        os.path.expanduser("~"), ".cache", "vitasdk-autobuild")
    path = os.path.join(root, "srcinfo")
    os.makedirs(path, exist_ok=True)
    return path


def find_vita_makepkg() -> str:
    """Path to a vita-makepkg. Clones the pinned one when there is none."""

    override = os.environ.get("VITA_MAKEPKG")
    if override:
        return override
    found = shutil.which("vita-makepkg")
    if found:
        return found

    root = os.environ.get("VITASDK_AUTOBUILD_CACHE") or os.path.join(
        os.path.expanduser("~"), ".cache", "vitasdk-autobuild")
    checkout = os.path.join(root, "vita-makepkg")
    script = os.path.join(checkout, "vita-makepkg")
    if not os.path.exists(script):
        os.makedirs(root, exist_ok=True)
        run(["git", "clone", "--quiet", f"https://github.com/{config.VITA_MAKEPKG_REPO}.git",
             checkout])
        run(["git", "-C", checkout, "checkout", "--quiet", config.VITA_MAKEPKG_REF])
    return script


def parse(text: str) -> dict[str, Any]:
    """Turns .SRCINFO text into a pkgbase dict with a 'packages' mapping.

    Values that can repeat become lists, everything else is a plain string,
    and each split package inherits the pkgbase values it does not override.
    """

    base: dict[str, Any] = {"packages": {}}
    current: dict[str, Any] = base
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator:
            continue
        key = key.strip()
        value = value.strip()
        if key == "pkgbase":
            base["pkgbase"] = value
            current = base
        elif key == "pkgname":
            current = {}
            base["packages"][value] = current
        elif key in ARRAY_FIELDS:
            current.setdefault(key, []).append(value)
        else:
            current[key] = value

    if "pkgbase" not in base:
        raise ValueError("no pkgbase in .SRCINFO")

    inherited = {k: v for k, v in base.items() if k != "packages"}
    for name, overrides in base["packages"].items():
        merged = {k: (list(v) if isinstance(v, list) else v) for k, v in inherited.items()}
        merged.update(overrides)
        merged["pkgname"] = name
        base["packages"][name] = merged
    return base


def strip_constraint(dependency: str) -> str:
    """'zlib>=1.2.3' and 'zlib: for foo' become 'zlib'."""

    for separator in (":", "<", ">", "="):
        dependency = dependency.split(separator, 1)[0]
    return dependency.strip()


def read(package_dir: str, makepkg: str) -> dict[str, Any]:
    """Runs vita-makepkg --printsrcinfo for one recipe, with an on-disk cache."""

    recipe = os.path.join(package_dir, "VITABUILD")
    with open(recipe, "rb") as handle:
        digest = hashlib.sha256(handle.read())
    with open(SRCINFO_CONF, "rb") as handle:
        digest.update(handle.read())
    cached = os.path.join(cache_dir(), digest.hexdigest() + ".srcinfo")
    if os.path.exists(cached):
        with open(cached, encoding="utf-8") as handle:
            return parse(handle.read())

    environ = dict(os.environ)
    environ["MAKEPKG_CONF"] = SRCINFO_CONF
    # vita-makepkg refuses to start without an absolute VITASDK, but printing
    # metadata never touches it, so a placeholder is enough on a bare runner.
    environ.setdefault("VITASDK", "/nonexistent/vitasdk")
    command = as_build_user(["bash", makepkg, "--nodeps", "--printsrcinfo"],
                            environ, ["MAKEPKG_CONF", "VITASDK", "PATH", "HOME"])
    result = subprocess.run(
        command, cwd=package_dir, env=environ, capture_output=True, text=True)
    if result.returncode != 0:
        message = (result.stderr or result.stdout).strip().splitlines()
        raise SystemExit(
            f"ERROR: cannot read {recipe}: " + " ".join(message[:2]))

    handle_fd, temporary = tempfile.mkstemp(dir=cache_dir())
    with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
        handle.write(result.stdout)
    os.replace(temporary, cached)
    return parse(result.stdout)


def collect(packages_dir: str) -> list[dict[str, Any]]:
    """Reads every recipe in a packages checkout, in parallel."""

    directories = sorted(
        entry for entry in os.listdir(packages_dir)
        if os.path.isfile(os.path.join(packages_dir, entry, "VITABUILD")))
    if not directories:
        raise SystemExit(f"ERROR: no VITABUILD found under {packages_dir}")

    makepkg = find_vita_makepkg()

    def read_one(name: str) -> dict[str, Any]:
        info = read(os.path.join(packages_dir, name), makepkg)
        info["repo_path"] = name
        return info

    with ThreadPoolExecutor(8) as executor:
        return list(executor.map(read_one, directories))
