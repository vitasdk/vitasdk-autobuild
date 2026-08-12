"""Keeping recipes that follow a git branch honest.

A recipe whose source is an unpinned branch builds something different every
day while claiming the same version. pacman only ships an upgrade when the
version rises, so such a package reaches nobody, and "already built" cannot
be answered by looking at a file name. Pinning the commit and putting it in
the version fixes both at once.
"""

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, replace

from . import srcinfo

# A source entry such as git+https://github.com/xerpi/libvita2d.git#commit=abc
VCS_SOURCE = re.compile(
    r"(?P<protocol>git|hg|svn|bzr)\+(?P<url>[^\s\"'#)]+)(?P<fragment>#[^\s\"')]*)?")

PKGVER_LINE = re.compile(r"^pkgver=.*$", re.MULTILINE)
PKGREL_LINE = re.compile(r"^pkgrel=.*$", re.MULTILINE)

# A version this tool produced before: <base>.r<count>.g<sha>
GENERATED_SUFFIX = re.compile(r"\.r\d+\.g[0-9a-f]{7,}$")

# The base used for recipes that never had a meaningful version. It sorts
# below any real release, so the day upstream publishes one no epoch is
# needed to move over to it.
UNVERSIONED_BASE = "0.0.0"

LIVE_VERSIONS = ("9999", "99999999")


@dataclass(frozen=True)
class Source:
    protocol: str
    url: str
    fragment: str

    @property
    def pinned(self) -> bool:
        return self.fragment.startswith(("#commit=", "#tag="))

    @property
    def text(self) -> str:
        return f"{self.protocol}+{self.url}{self.fragment}"


def find_sources(text: str) -> list[Source]:
    return [Source(m.group("protocol"), m.group("url"), m.group("fragment") or "")
            for m in VCS_SOURCE.finditer(text)]


def expand(url: str, pkgname: str, pkgver: str) -> str:
    """Resolves the shell variables recipes use inside source URLs."""

    for name, value in (("pkgname", pkgname), ("pkgver", pkgver)):
        url = url.replace(f"${{{name}}}", value).replace(f"${name}", value)
    return url


def version_base(pkgver: str) -> str:
    """The part of a version that upstream owns."""

    if pkgver in LIVE_VERSIONS:
        return UNVERSIONED_BASE
    stripped = GENERATED_SUFFIX.sub("", pkgver)
    return stripped or UNVERSIONED_BASE


def make_version(base: str, count: int, sha: str) -> str:
    """<base>.r<commits>.g<sha>, which only ever rises as history grows."""

    return f"{base}.r{count}.g{sha[:7]}"


def source_ref(source: Source) -> str:
    """Which upstream ref a source actually builds from.

    A recipe naming a branch does not build the default one, and resolving
    HEAD instead would pin it to a different branch without saying so.
    """

    if source.fragment.startswith("#branch="):
        return source.fragment.split("=", 1)[1]
    return "HEAD"


def _mirror(url: str, cache_dir: str) -> str:
    """A local mirror of a repository, holding commits but no file contents."""

    os.makedirs(cache_dir, exist_ok=True)
    name = re.sub(r"[^A-Za-z0-9._-]", "_", url)
    path = os.path.join(cache_dir, name + ".git")
    if not os.path.exists(path):
        subprocess.run(["git", "clone", "--quiet", "--bare", "--filter=blob:none", url, path],
                       check=True)
    else:
        subprocess.run(["git", "-C", path, "fetch", "--quiet", "--force", "--prune",
                        "origin", "+refs/heads/*:refs/heads/*"], check=True)
    return path


def resolve(url: str, cache_dir: str, ref: str = "HEAD") -> tuple[str, int]:
    """Current commit of a git ref and how many commits lead to it."""

    path = _mirror(url, cache_dir)
    target = "HEAD" if ref == "HEAD" else f"refs/heads/{ref}"
    resolved = subprocess.run(["git", "-C", path, "rev-parse", "--verify", "--quiet", target],
                              capture_output=True, text=True)
    if resolved.returncode != 0:
        raise ValueError(f"{url}: no such branch: {ref}")
    sha = resolved.stdout.strip()
    count = subprocess.run(["git", "-C", path, "rev-list", "--count", sha],
                           check=True, capture_output=True, text=True).stdout.strip()
    return sha, int(count)


def count_commits(url: str, cache_dir: str, sha: str) -> tuple[str, int]:
    """How many commits lead to a commit a recipe is already pinned to."""

    path = _mirror(url, cache_dir)
    resolved = subprocess.run(["git", "-C", path, "rev-list", "--count", sha],
                              capture_output=True, text=True)
    if resolved.returncode != 0:
        raise ValueError(f"{url}: commit {sha[:8]} is not in the repository")
    return sha, int(resolved.stdout.strip())


def rewrite(text: str, pkgver: str, pins: dict[str, str]) -> str:
    """Sets the version, resets the release, and pins every listed source."""

    updated = PKGVER_LINE.sub(f"pkgver={pkgver}", text, count=1)
    updated = PKGREL_LINE.sub("pkgrel=1", updated, count=1)
    for source in find_sources(updated):
        sha = pins.get(source.url)
        if sha is None:
            continue
        pinned = replace(source, fragment=f"#commit={sha}")
        updated = updated.replace(source.text, pinned.text)
    return updated


@dataclass
class Update:
    name: str
    old_version: str
    new_version: str
    pins: dict[str, str]

    @property
    def changed(self) -> bool:
        return self.old_version != self.new_version


def plan_update(package_dir: str, info: dict, cache_dir: str) -> Update | None:
    """Works out what a recipe following a branch should say instead."""

    with open(os.path.join(package_dir, "VITABUILD"), encoding="utf-8") as handle:
        text = handle.read()

    name = info["pkgbase"]
    pkgver = info["pkgver"]
    git_sources = [s for s in find_sources(text) if s.protocol == "git"]
    unpinned = [s for s in git_sources if not s.pinned]

    # A pinned recipe still claiming a live version is not building something
    # different every day, but 9999 sits above every real release for ever, so
    # the version has to come down even though the commit stays put.
    if not unpinned and not (git_sources and pkgver in LIVE_VERSIONS):
        return None

    pins: dict[str, str] = {}
    newest: tuple[int, str] = (0, "")
    for source in git_sources:
        url = expand(source.url, name, pkgver)
        if "$" in url:
            raise ValueError(f"{name}: cannot resolve source URL {source.url!r}")
        if source.pinned:
            if not source.fragment.startswith("#commit="):
                raise ValueError(f"{name}: pinned to a tag, set the version by hand")
            sha, count = count_commits(url, cache_dir, source.fragment.split("=", 1)[1])
        else:
            sha, count = resolve(url, cache_dir, source_ref(source))
            pins[source.url] = sha
        if count > newest[0]:
            newest = (count, sha)

    count, sha = newest
    return Update(name=name, old_version=pkgver,
                  new_version=make_version(version_base(pkgver), count, sha), pins=pins)


def apply_update(package_dir: str, update: Update, makepkg: str) -> None:
    """Rewrites a recipe and checks the result still parses as intended.

    Editing shell with regular expressions is only acceptable if the result is
    verified, so the recipe is read back with vita-makepkg and reverted unless
    it says exactly what was intended.
    """

    path = os.path.join(package_dir, "VITABUILD")
    with open(path, encoding="utf-8") as handle:
        original = handle.read()

    updated = rewrite(original, update.new_version, update.pins)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(updated)

    try:
        info = srcinfo.read(package_dir, makepkg)
    except SystemExit as e:
        _restore(path, original)
        raise ValueError(f"{update.name}: recipe no longer parses after the update: {e}") from None

    if info["pkgver"] != update.new_version:
        _restore(path, original)
        raise ValueError(f"{update.name}: expected pkgver {update.new_version}, "
                         f"recipe reads {info['pkgver']}")
    if info.get("pkgrel") != "1":
        _restore(path, original)
        raise ValueError(f"{update.name}: pkgrel was not reset")
    for source in find_sources("\n".join(info.get("source", []))):
        if source.protocol == "git" and not source.pinned:
            _restore(path, original)
            raise ValueError(f"{update.name}: source {source.url} is still not pinned")


def _restore(path: str, original: str) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(original)


def recipe_cache_dir(root: str) -> str:
    path = os.path.join(root, "vcs")
    os.makedirs(path, exist_ok=True)
    return path


def have_git() -> bool:
    return shutil.which("git") is not None
