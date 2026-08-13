"""Keeping recipes that follow a git branch honest.

A recipe whose source is an unpinned branch builds something different every
day while claiming the same version. pacman only ships an upgrade when the
version rises, so such a package reaches nobody, and "already built" cannot
be answered by looking at a file name. Pinning the commit and putting it in
the version fixes both at once.
"""

import functools
import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass, replace

from . import srcinfo
from . import version as version_module

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

FOLLOW_LINE = re.compile(r"^_follow=(.+)$", re.MULTILINE)

# A package that should not be used any more, and why. Deprecation is a
# decision by whoever maintains the recipe, so it is declared there rather
# than inferred from anything.
DEPRECATED_LINE = re.compile(r"^_deprecated=(.+)$", re.MULTILINE)


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


def declared_follow(text: str) -> list[str]:
    """The upstream refs a recipe says it tracks.

    Pinning a source to a commit throws away the branch it came from, and
    without that the only thing a follow job could do is guess the default
    branch, which is wrong exactly where it matters: a recipe on a maintenance
    branch would silently jump to the next major version.

    A single value applies to every git source; an array is matched to them in
    order.
    """

    match = FOLLOW_LINE.search(text)
    if match is None:
        return []
    value = match.group(1).strip()
    if value.startswith("(") and value.endswith(")"):
        return [item.strip("'\"") for item in shlex.split(value[1:-1]) if item.strip("'\"")]
    value = value.strip("'\"")
    return [value] if value else []


def declared_deprecation(text: str) -> str:
    """Why a recipe says its package should no longer be used, if it does.

    Removing a package is not the same as deprecating one: everything that
    already depends on it keeps working, and the point is to say so before
    somebody starts something new on top of it.
    """

    match = DEPRECATED_LINE.search(text)
    if match is None:
        return ""
    return match.group(1).strip().strip("'\"")


def follow_refs(text: str, git_sources: list["Source"]) -> dict["Source", str]:
    """Which ref each git source follows, or nothing if the recipe is silent."""

    declared = declared_follow(text)
    if not declared:
        return {}
    if len(declared) == 1:
        return {source: declared[0] for source in git_sources}
    if len(declared) != len(git_sources):
        raise ValueError(
            f"_follow lists {len(declared)} ref(s) for {len(git_sources)} git source(s)")
    return dict(zip(git_sources, declared))


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


def upstream_tags(url: str, cache_dir: str) -> list[str]:
    """Every tag upstream publishes, newest version first.

    Read from the mirror rather than ls-remote so the peeled entries git adds
    for annotated tags are not mistaken for separate releases.
    """

    path = _mirror(url, cache_dir)
    listed = subprocess.run(["git", "-C", path, "tag", "--list"],
                            check=True, capture_output=True, text=True)
    tags = [line.strip() for line in listed.stdout.splitlines() if line.strip()]
    return sorted(tags, key=lambda tag: version_key(tag_version(tag)), reverse=True)


# Two conventions, and they have to be told apart: a bare letter after a
# number is a patch release for OpenSSL (1.0.2a) while a letter followed by a
# number is a prerelease for Python (3.11.0a5).
PRERELEASE = re.compile(r"(?:^|[._\-])(?:alpha|beta|rc|pre|dev|snapshot)\d*(?:$|[._\-])|"
                        r"\d(?:alpha|beta|rc|pre|a|b)\d+$", re.IGNORECASE)


def is_prerelease(version: str) -> bool:
    """Whether a version names something upstream is still working on.

    Proposing one of these is worse than proposing nothing: pacman sees a
    numeric segment where the current version has a letter and calls it an
    upgrade, so an alpha would be handed to everyone as if it were newer than
    the branch the recipe actually tracks.
    """

    return bool(PRERELEASE.search(version.strip()))


def major_of(version: str) -> str:
    """The leading number, which is the line a recipe lives on."""

    match = re.match(r"\d+", version.strip())
    return match.group(0) if match else ""


def tag_version(tag: str) -> str:
    """The version a tag names, as a version a recipe may legally carry.

    A hyphen cannot appear in a pkgver: pacman reads everything after it as
    the release, so `1.0-rc2` would compare equal to `1.0` and the update
    would never be offered. Replaced rather than rejected, which is the same
    thing every distribution does with these tags.
    """

    stripped = tag.strip()
    for prefix in ("version-", "version_", "release-", "release_", "v", "V"):
        if stripped.startswith(prefix) and stripped[len(prefix):len(prefix) + 1].isdigit():
            stripped = stripped[len(prefix):]
            break
    return stripped.replace("-", "_").replace(":", "_")


def version_key(version: str):
    """Orders versions exactly as pacman does."""

    return functools.cmp_to_key(version_module.vercmp)(version)


def commit_of(url: str, cache_dir: str, ref: str) -> str:
    """The commit a tag or branch points at, following annotated tags."""

    path = _mirror(url, cache_dir)
    resolved = subprocess.run(
        ["git", "-C", path, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
        capture_output=True, text=True)
    if resolved.returncode != 0:
        raise ValueError(f"{url}: cannot resolve {ref}")
    return resolved.stdout.strip()


def is_ahead(url: str, cache_dir: str, older: str, newer_sha: str) -> bool:
    """Whether one commit really contains the other.

    The decisive question about a tag, and the one a version string cannot
    answer. cpython3 builds a commit on the 3.11 branch that is well past the
    3.11.0a5 tag, yet pacman reads 3.11.0a5 as the higher version because a
    digit outranks a letter. Only history knows which is actually newer.
    """

    path = _mirror(url, cache_dir)
    return subprocess.run(
        ["git", "-C", path, "merge-base", "--is-ancestor", older, newer_sha],
        capture_output=True).returncode == 0


def count_commits(url: str, cache_dir: str, sha: str) -> tuple[str, int]:
    """How many commits lead to a commit a recipe is already pinned to."""

    path = _mirror(url, cache_dir)
    resolved = subprocess.run(["git", "-C", path, "rev-list", "--count", sha],
                              capture_output=True, text=True)
    if resolved.returncode != 0:
        raise ValueError(f"{url}: commit {sha[:8]} is not in the repository")
    return sha, int(resolved.stdout.strip())


def rewrite(text: str, pkgver: str, pins: dict[str, str]) -> str:
    """Sets the version, resets the release, and repins every listed source.

    A pin is a whole fragment rather than a bare commit, because moving to a
    release means naming a tag and that is the same edit.
    """

    updated = PKGVER_LINE.sub(f"pkgver={pkgver}", text, count=1)
    updated = PKGREL_LINE.sub("pkgrel=1", updated, count=1)
    for source in find_sources(updated):
        fragment = pins.get(source.url)
        if fragment is None:
            continue
        if not fragment.startswith("#"):
            fragment = f"#commit={fragment}"
        updated = updated.replace(source.text, replace(source, fragment=fragment).text)
    return updated


@dataclass
class Update:
    """One proposed change to one recipe.

    The kind is not decoration: pinning a loose source is housekeeping, moving
    a pin forward is taking today's upstream, and switching to a tag is taking
    a release. They deserve separate decisions, so they travel separately.
    """

    name: str
    old_version: str
    new_version: str
    pins: dict[str, str]
    kind: str = "pin"

    @property
    def changed(self) -> bool:
        return self.old_version != self.new_version


def evaluated_sources(raw: list[Source], info: dict) -> dict[Source, Source]:
    """Pairs each source in the recipe with its evaluated .SRCINFO entry.

    makepkg writes the same array in the same order, with every shell
    variable already resolved, so pairing them by position is exact.
    """

    entries = [s for s in find_sources("\n".join(info.get("source", [])))
               if s.protocol == "git"]
    if len(entries) != len(raw):
        return {}
    return dict(zip(raw, entries))


def plan_update(package_dir: str, info: dict, cache_dir: str) -> Update | None:
    """Works out what a recipe following a branch should say instead."""

    with open(os.path.join(package_dir, "VITABUILD"), encoding="utf-8") as handle:
        text = handle.read()

    name = info["pkgbase"]
    pkgver = info["pkgver"]
    git_sources = [s for s in find_sources(text) if s.protocol == "git"]
    # The recipe text is what has to be edited, but a source can be assembled
    # from shell variables, and only vita-makepkg knows what they hold. The
    # .SRCINFO entries are the same array, already evaluated.
    evaluated = evaluated_sources(git_sources, info)
    unpinned = [s for s in git_sources if not s.pinned]

    # A pinned recipe still claiming a live version is not building something
    # different every day, but 9999 sits above every real release for ever, so
    # the version has to come down even though the commit stays put.
    if not unpinned and not (git_sources and pkgver in LIVE_VERSIONS):
        return None

    pins: dict[str, str] = {}
    newest: tuple[int, str] = (0, "")
    for source in git_sources:
        actual = evaluated.get(source, source)
        url = expand(actual.url, name, pkgver)
        if "$" in url:
            raise ValueError(f"{name}: cannot resolve source URL {source.url!r}")
        if actual.pinned:
            if not actual.fragment.startswith("#commit="):
                raise ValueError(f"{name}: pinned to a tag, set the version by hand")
            sha, count = count_commits(url, cache_dir, actual.fragment.split("=", 1)[1])
        else:
            sha, count = resolve(url, cache_dir, source_ref(actual))
            pins[source.url] = sha
        if count > newest[0]:
            newest = (count, sha)

    count, sha = newest
    return Update(name=name, old_version=pkgver,
                  new_version=make_version(version_base(pkgver), count, sha), pins=pins)


def plan_advance(package_dir: str, info: dict, cache_dir: str) -> Update | None:
    """What a recipe would say if it took what its _follow ref points at today.

    Silent unless the recipe declares what it follows: guessing the default
    branch would move a package onto a line upstream never meant it to be on.
    """

    with open(os.path.join(package_dir, "VITABUILD"), encoding="utf-8") as handle:
        text = handle.read()

    name, pkgver = info["pkgbase"], info["pkgver"]
    git_sources = [s for s in find_sources(text) if s.protocol == "git"]
    refs = follow_refs(text, git_sources)
    if not refs:
        return None

    evaluated = evaluated_sources(git_sources, info)
    pins: dict[str, str] = {}
    newest: tuple[int, str] = (0, "")
    for source in git_sources:
        actual = evaluated.get(source, source)
        url = expand(actual.url, name, pkgver)
        if "$" in url:
            raise ValueError(f"{name}: cannot resolve source URL {source.url!r}")
        sha, count = resolve(url, cache_dir, refs[source])
        if not actual.fragment.startswith(f"#commit={sha}"):
            pins[source.url] = sha
        if count > newest[0]:
            newest = (count, sha)

    if not pins:
        return None
    count, sha = newest
    return Update(name=name, old_version=pkgver, kind="advance",
                  new_version=make_version(version_base(pkgver), count, sha), pins=pins)


def current_pin(source: Source) -> str:
    """The ref a source builds from today, whatever form it was written in."""

    if source.fragment.startswith(("#commit=", "#tag=")):
        return source.fragment.split("=", 1)[1]
    if source.fragment.startswith("#branch="):
        return source.fragment.split("=", 1)[1]
    return "HEAD"


def plan_release(package_dir: str, info: dict, cache_dir: str) -> Update | None:
    """Whether upstream has published a tag worth moving to.

    A tag is a statement that upstream considers something finished, which a
    commit never is. It is proposed and never taken automatically: a release
    can change the build as much as the version.
    """

    with open(os.path.join(package_dir, "VITABUILD"), encoding="utf-8") as handle:
        text = handle.read()

    name, pkgver = info["pkgbase"], info["pkgver"]
    git_sources = [s for s in find_sources(text) if s.protocol == "git"]
    if len(git_sources) != 1:
        # With several sources there is no single upstream release to move to.
        return None
    source = git_sources[0]
    evaluated = evaluated_sources(git_sources, info).get(source, source)
    url = expand(evaluated.url, name, pkgver)
    if "$" in url:
        raise ValueError(f"{name}: cannot resolve source URL {source.url!r}")

    current_is_prerelease = is_prerelease(pkgver)
    try:
        current_sha = commit_of(url, cache_dir, current_pin(evaluated))
    except ValueError:
        return None

    for tag in upstream_tags(url, cache_dir):
        if evaluated.fragment == f"#tag={tag}":
            return None
        version = tag_version(tag)
        if is_prerelease(version) and not current_is_prerelease:
            continue
        if not version_module.newer(version, pkgver):
            # pacman would never hand it over, so proposing it is proposing a
            # package that gets built and never installed.
            continue
        tag_sha = commit_of(url, cache_dir, tag)
        if tag_sha == current_sha or not is_ahead(url, cache_dir, current_sha, tag_sha):
            continue
        break
    else:
        return None
    return Update(name=name, old_version=pkgver, new_version=version, kind="release",
                  pins={source.url: f"#tag={tag}"})


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


CORE_LINE = re.compile(
    r'(?P<head>World\(\s*\n\s*arch="(?P<arch>[^"]+)",\s*\n\s*core=")(?P<core>[^"]*)(?P<tail>")')


def set_core(text: str, arch: str, core: str) -> str:
    """Points one world at a different core snapshot.

    The pin lives in configuration because changing it rebuilds that world's
    whole catalogue. Rewriting it is therefore a commit someone reviews, not
    something a notification does on its own.
    """

    found = False

    def replace(match: "re.Match[str]") -> str:
        nonlocal found
        if match.group("arch") != arch:
            return match.group(0)
        found = True
        return match.group("head") + core + match.group("tail")

    updated = CORE_LINE.sub(replace, text)
    if not found:
        raise ValueError(f"no world with arch {arch!r} in the configuration")
    return updated
