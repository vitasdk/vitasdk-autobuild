"""Static configuration of the VitaSDK package autobuilder.

Everything that decides *what* gets built lives here, so that changing the
plan is a reviewable commit and never a click in a web UI.
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class World:
    """One target the catalogue is built for.

    A world is an architecture, a libc and a toolchain taken together, and it
    is named by the target triple, which pacman carries in the `arch` field of
    a package. Two worlds never mix: their file names differ, their
    repositories differ, and their files land under different triples inside
    the same SDK.
    """

    arch: str
    """CARCH, and therefore the architecture in every file name of this world."""

    core: str
    """The core SDK snapshot this world's packages are built against.

    One complete, working SDK per world, which is what autobuilds already
    produces today for the single world that exists. A second world means a
    second such snapshot, not a second sysroot inside this one: the world a
    worker builds for is decided by the image it runs in.
    """

    repository: str
    """Name of the published pacman repository for this world."""

    triple: str = ""
    """Install prefix inside the SDK. Defaults to the arch when unset."""

    description: str = ""

    @property
    def prefix(self) -> str:
        return self.triple or self.arch

    @property
    def staging_repository(self) -> str:
        return f"{self.repository}-staging"

    @property
    def core_marker(self) -> str:
        """Asset recording which core this world's staged packages come from."""

        return f"core-{self.arch}.txt"

# The recipe repository. It is read, never written: adding a library must not
# require touching the scheduler, and changing the scheduler must not need
# library-maintainer review.
PACKAGES_REPO = "vitasdk/packages"
PACKAGES_BRANCH = "next"

# vita-makepkg is needed outside of the SDK to compute .SRCINFO for the build
# queue. It ships inside the SDK, so this pin exists only for the supervisor,
# which runs on a bare runner. Keep it in sync with VITA_MAKEPKG_TAG in
# vitasdk/buildscripts.
VITA_MAKEPKG_REPO = "vitasdk/vita-makepkg"
VITA_MAKEPKG_REF = "32f863c2a58a801b7d5a0296bdbbb443c9676e08"

# The worlds the catalogue is built for. One entry today; a second toolchain
# or libc is a second entry, and nothing else in this program has to change.
#
# The core is per world because the core *is* that world's toolchain and
# sysroot. Bumping one world's core empties only that world's staged packages,
# so a snapshot never mixes cores and the other world stays publishable.
WORLDS: list[World] = [
    World(
        arch="vita",
        core="sdk-snapshot-20260812.565.1",
        repository="vita",
        triple="arm-vita-eabi",
        description="gcc and newlib",
    ),
]

# Rebuild a package when something it links against was rebuilt after it.
# Vita packages are mostly static libraries, so a dependent built against an
# older dependency carries the old code until it is built again.
REBUILD_DEPENDENTS = True

# Releases used as the artifact store. They live in the autobuild repository,
# so recipe contributors never need write access to them.
STAGING_RELEASE = "staging"
FAILED_RELEASE = "staging-failed"
STATUS_RELEASE = "status"

# Marker written before worlds existed. Read as the first world's marker when
# the per-world one is absent, so introducing worlds does not throw away a
# staging area that is already correct.
LEGACY_CORE_MARKER = "core-snapshot.txt"

# The catalogue, told to refresh as soon as a new status file is published.
# It also polls on its own schedule, so this only decides whether the site is
# a minute behind or half an hour. Needs a token with write access to that
# repository, since a job's own token cannot dispatch into another one.
WEBSITE_REPO = "vitasdk/vitasdk-web"
WEBSITE_EVENT = "status_updated"

# Prefix of the immutable snapshot releases cut from the staging area.
SNAPSHOT_PREFIX = "packages-snapshot-"

# Where published snapshots are looked up, to tell whether a package is
# already in the repository. Empty means "the repository we run in".
SNAPSHOT_REPO = ""

# Runtime after which a worker stops picking up new packages. GitHub kills a
# job at 6h, and the longest package (icu4c, ffmpeg) needs well under one.
SOFT_JOB_TIMEOUT = 60 * 60 * 4

# Upper bound on workers dispatched per supervision round.
MAXIMUM_JOB_COUNT = 10

# Runner the workers ask for.
RUNNER_LABELS = ["ubuntu-24.04"]

# Packages that cannot be built unattended. Matched with fnmatch against the
# package name.
MANUAL_BUILD: list[str] = []

# Packages whose unfinished state does not block their dependencies from being
# published. Only for things nobody is going to fix soon.
IGNORE_RDEP_PACKAGES: list[str] = []

# Dependencies treated as optional to break a cycle. Only takes effect when
# the dependency is already in the published repository, otherwise the cycle
# has to be broken by hand.
OPTIONAL_DEPS: dict[str, list[str]] = {}

# Packages that provide the same file set and must never be installed
# together. Each group is ordered by increasing precedence, so the last entry
# that a build actually pulls in is the one that gets installed.
CONFLICTING_DEPS: list[list[str]] = [
    ["openssl", "openssl-1.1.1"],
]

# Users allowed to upload assets by hand. Anything uploaded by anyone else
# aborts the run rather than being trusted, because an asset in the staging
# release is a package someone will install. Maintainers are listed here so a
# manual repair or a hand-built package is possible without disabling the
# check, which is how msys2 uses the same list.
ALLOWED_UPLOADERS: list[str] = [
    "frangarcj",
]


def apply_overrides(overrides: dict[str, Any]) -> None:
    """Override configuration values, used by --config-override and tests."""

    for key, value in overrides.items():
        if key not in globals() or key.startswith("_") or not key.isupper():
            raise SystemExit(f"ERROR: unknown configuration key: {key}")
        globals()[key] = value


def worlds() -> list[World]:
    return list(WORLDS)


def default_world() -> World:
    if not WORLDS:
        raise SystemExit("ERROR: no worlds configured")
    return WORLDS[0]


def world_by_arch(arch: str) -> World:
    for world in WORLDS:
        if world.arch == arch:
            return world
    known = ", ".join(w.arch for w in WORLDS) or "none"
    raise SystemExit(f"ERROR: unknown world {arch!r}; configured: {known}")
