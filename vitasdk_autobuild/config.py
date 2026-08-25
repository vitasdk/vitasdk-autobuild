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

    series: str = ""
    """The release series these packages belong to, empty for the unnamed one.

    A series is a core that stays put while its packages keep improving, so
    two of them are two package sets that must never mix. They cannot be told
    apart by file name, because a package is named after its architecture and
    that is the same in every series, so the series names the store instead.
    """

    @property
    def prefix(self) -> str:
        return self.triple or self.arch

    @property
    def staging_repository(self) -> str:
        return f"{self.repository}-staging"

    @property
    def name(self) -> str:
        """What identifies this world among all of them."""

        return f"{self.series}/{self.arch}" if self.series else self.arch

    @property
    def core_marker(self) -> str:
        """Asset recording which core this world's staged packages come from."""

        return f"core-{self.arch}.txt"


def series_suffix(series: str) -> str:
    """What a series adds to the name of everything it stores."""

    return f"-{series}" if series else ""

# The recipe repository. It is read, never written: adding a library must not
# require touching the scheduler, and changing the scheduler must not need
# library-maintainer review.
PACKAGES_REPO = "vitasdk/packages"
PACKAGES_BRANCH = "master"

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
        core="sdk-snapshot-20260825.609.1",
        repository="vita",
        triple="arm-vita-eabi",
        description="gcc and newlib",
    ),
    # The master branches as they stood in August 2026: newlib 4.1 and the
    # headers without PR #886, which is what the homebrew out there is built
    # against. Same host tools as the series above, so these two differ in
    # exactly what defines a world.
    World(
        arch="vita",
        core="sdk-snapshot-20260825.611.1",
        repository="vita",
        triple="arm-vita-eabi",
        description="gcc and newlib",
        series="2026.08",
    ),
]

# Which series a run drives. One run builds one series, because a series owns
# its whole store; the arch axis inside it works as it always has. Left as a
# runtime choice, like --world, while the set of series stays a commit.
ACTIVE_SERIES = ""

# Rebuild a package when something it links against was rebuilt after it.
# Vita packages are mostly static libraries, so a dependent built against an
# older dependency carries the old code until it is built again.
REBUILD_DEPENDENTS = True

# Releases used as the artifact store. They live in the autobuild repository,
# so recipe contributors never need write access to them. A named series gets
# a store of its own, because two series produce the same file names.
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

# Where a signed channel manifest is produced. The signing key lives there and
# nowhere else, so a published snapshot asks for a manifest rather than making
# one. Needs a token with write access to that repository.
CHANNEL_REPO = "vitasdk/autobuilds"
CHANNEL_EVENT = "update_channel"

# Prefix of the immutable snapshot releases cut from the staging area.
SNAPSHOT_PREFIX = "packages-snapshot-"

# Where published snapshots are looked up, to tell whether a package is
# already in the repository. Empty means "the repository we run in".
SNAPSHOT_REPO = ""

# Where the staging area lives. Empty means "this repository", which is true
# for every job of the autobuilder itself. A check running somewhere else —
# a pull request against the recipes, say — needs to say where to read from.
AUTOBUILD_REPO = ""

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


def all_series() -> list[str]:
    """Every series with a world configured, in the order they are declared."""

    seen: list[str] = []
    for world in WORLDS:
        if world.series not in seen:
            seen.append(world.series)
    return seen


def worlds() -> list[World]:
    """The worlds of the series this run drives, and only those.

    Everything downstream — the queue, the plan, the staging area it reads —
    belongs to one series, so this is where the other ones stop existing.
    """

    return [world for world in WORLDS if world.series == ACTIVE_SERIES]


def select_series(series: str) -> None:
    """Points the run at a series, refusing one that has no world."""

    if series and series not in all_series():
        known = ", ".join(s or "(default)" for s in all_series()) or "none"
        raise SystemExit(f"ERROR: unknown series {series!r}; configured: {known}")
    apply_overrides({"ACTIVE_SERIES": series})


def staging_release_tag() -> str:
    return STAGING_RELEASE + series_suffix(ACTIVE_SERIES)


def failed_release_tag() -> str:
    return FAILED_RELEASE + series_suffix(ACTIVE_SERIES)


def status_release_tag() -> str:
    return STATUS_RELEASE + series_suffix(ACTIVE_SERIES)


def snapshot_prefix() -> str:
    """Snapshots of two series must not answer to the same tag search."""

    if not ACTIVE_SERIES:
        return SNAPSHOT_PREFIX
    return f"packages-{ACTIVE_SERIES}-snapshot-"


def default_world() -> World:
    configured = worlds()
    if not configured:
        raise SystemExit("ERROR: no worlds configured")
    return configured[0]


def world_by_arch(arch: str) -> World:
    for world in worlds():
        if world.arch == arch:
            return world
    known = ", ".join(w.arch for w in worlds()) or "none"
    raise SystemExit(f"ERROR: unknown world {arch!r}; configured: {known}")
