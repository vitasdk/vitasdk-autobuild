"""Static configuration of the VitaSDK package autobuilder.

Everything that decides *what* gets built lives here, so that changing the
plan is a reviewable commit and never a click in a web UI.
"""

from typing import Any

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

# The core SDK every package in the staging area is built against. Bumping it
# invalidates the whole staging area, so a snapshot never mixes cores.
CORE_SNAPSHOT = "sdk-snapshot-20260812.565.1"

# Package architecture, matching CARCH in the SDK's makepkg.conf.
ARCH = "vita"

# Name of the pacman repository inside a published snapshot. Matches the
# default of scripts/create-repository.sh in vitasdk/packages.
REPOSITORY_NAME = "vita"

# Name of the repository generated inside the staging release, which anyone
# can add to pacman.conf to get packages before they are published. It carries
# partial results of a rebuild by design, so it is deliberately not "vita".
STAGING_REPOSITORY_NAME = "vita-staging"

# Rebuild a package when something it links against was rebuilt after it.
# Vita packages are mostly static libraries, so a dependent built against an
# older dependency carries the old code until it is built again.
REBUILD_DEPENDENTS = True

# Releases used as the artifact store. They live in the autobuild repository,
# so recipe contributors never need write access to them.
STAGING_RELEASE = "staging"
FAILED_RELEASE = "staging-failed"
STATUS_RELEASE = "status"

# Asset inside STAGING_RELEASE recording which core the staged packages were
# built against.
CORE_MARKER_ASSET = "core-snapshot.txt"

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

# Users allowed to upload assets by hand. Anything uploaded by someone else
# aborts the run instead of being trusted.
ALLOWED_UPLOADERS: list[str] = []


def apply_overrides(overrides: dict[str, Any]) -> None:
    """Override configuration values, used by --config-override and tests."""

    for key, value in overrides.items():
        if key not in globals() or key.startswith("_") or not key.isupper():
            raise SystemExit(f"ERROR: unknown configuration key: {key}")
        globals()[key] = value
