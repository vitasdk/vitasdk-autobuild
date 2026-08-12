"""The build queue: what exists, what it needs, and what state it is in.

The queue is derived from three inputs and nothing else: the recipes, the file
names present in the staging release, and the failure markers. A package is
built when its file is not there, which means the version is part of the
answer and no database has to be diffed.
"""

import fnmatch
from enum import Enum
from typing import Any, Iterable

from . import config, srcinfo


class PackageStatus(Enum):
    FINISHED = "finished"
    FINISHED_BUT_BLOCKED = "finished-but-blocked"
    FAILED_TO_BUILD = "failed-to-build"
    WAITING_FOR_BUILD = "waiting-for-build"
    WAITING_FOR_DEPENDENCIES = "waiting-for-dependencies"
    MANUAL_BUILD_REQUIRED = "manual-build-required"
    UNKNOWN = "unknown"

    def __str__(self) -> str:
        return self.value


FINISHED_STATES = (PackageStatus.FINISHED, PackageStatus.FINISHED_BUT_BLOCKED)


class Package:
    """One recipe, with the binary packages it produces."""

    def __init__(self, info: dict[str, Any]) -> None:
        self.name: str = info["pkgbase"]
        self.repo_path: str = info.get("repo_path", self.name)
        self.description: str = info.get("pkgdesc", "")
        self.url: str = info.get("url", "")
        self.licenses: list[str] = list(info.get("license", []))
        epoch = info.get("epoch", "")
        self.version: str = f"{info['pkgver']}-{info['pkgrel']}"
        if epoch:
            self.version = f"{epoch}:{self.version}"

        self.binaries: dict[str, dict[str, Any]] = {}
        depends: list[str] = []
        provides: list[str] = []
        for name, binary in info["packages"].items():
            architectures = binary.get("arch") or [config.ARCH]
            self.binaries[name] = {
                "arch": architectures[0],
                "pkgdesc": binary.get("pkgdesc", self.description),
                "provides": [srcinfo.strip_constraint(p) for p in binary.get("provides", [])],
            }
            depends.extend(binary.get("depends", []))
            provides.extend(self.binaries[name]["provides"])
        for field in ("makedepends", "checkdepends"):
            depends.extend(info.get(field, []))

        self.depends: list[str] = sorted(
            {srcinfo.strip_constraint(d) for d in depends} - set(self.binaries))
        self.provides: list[str] = sorted(set(provides))

        self.ext_depends: set["Package"] = set()
        self.ext_rdepends: set["Package"] = set()
        self.status: PackageStatus = PackageStatus.UNKNOWN
        self.details: dict[str, Any] = {}
        self.repo_version: str = ""

    def __repr__(self) -> str:
        return f"Package({self.name!r})"

    def __hash__(self) -> int:
        return hash(self.name)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Package) and other.name == self.name

    @property
    def is_new(self) -> bool:
        """True when no version of this package is in the published repository."""

        return not self.repo_version

    def build_patterns(self) -> list[str]:
        return [f"{name}-{self.version}-{binary['arch']}.pkg.tar.*"
                for name, binary in self.binaries.items()]

    def failed_name(self) -> str:
        return f"{self.name}-{self.version}.failed"

    def set_status(self, status: PackageStatus, description: str = "",
                   urls: dict[str, str] | None = None) -> None:
        self.status = status
        self.details = {"desc": description, "urls": urls or {}}

    def set_blocked(self, status: PackageStatus, blocker: "Package") -> None:
        blocked = set(self.details.get("blocked", ()))
        inherited = blocker.details.get("blocked")
        blocked = set(inherited) if inherited else blocked | {blocker.name}
        self.set_status(status, "Blocked by: " + ", ".join(sorted(blocked)))
        self.details["blocked"] = sorted(blocked)


def build_queue(packages_dir: str) -> list[Package]:
    """Reads every recipe and links the dependency graph both ways."""

    packages = [Package(info) for info in srcinfo.collect(packages_dir)]
    link_dependencies(packages)
    return packages


def link_dependencies(packages: list[Package]) -> None:
    provider: dict[str, Package] = {}
    for package in packages:
        for name in package.binaries:
            provider[name] = package
    for package in packages:
        for name in package.provides:
            provider.setdefault(name, package)

    unknown: list[str] = []
    for package in packages:
        for dependency in package.depends:
            target = provider.get(dependency)
            if target is None:
                unknown.append(f"{package.name} -> {dependency}")
            elif target is not package:
                package.ext_depends.add(target)
    if unknown:
        raise SystemExit(
            "ERROR: recipes depend on packages that do not exist:\n  "
            + "\n  ".join(sorted(unknown)))

    for package in packages:
        for dependency in package.ext_depends:
            dependency.ext_rdepends.add(package)


def is_optional_dep(package: Package, dependency: Package) -> bool:
    """Dependencies manually marked optional, to break a cycle.

    Only honoured once the dependency is in the published repository: until
    then there is nothing to build against and the cycle is real.
    """

    return (dependency.name in config.OPTIONAL_DEPS.get(package.name, [])
            and not dependency.is_new)


def is_manual(package: Package) -> bool:
    return any(fnmatch.fnmatchcase(package.name, pattern)
               for pattern in config.MANUAL_BUILD)


def apply_status(packages: Iterable[Package], done_names: Iterable[str],
                 failed_names: Iterable[str],
                 failed_urls: dict[str, dict[str, str]] | None = None) -> None:
    """Computes the state of every package from the file names that exist."""

    done = list(done_names)
    failed = set(failed_names)
    failed_urls = failed_urls or {}

    for package in packages:
        if all(fnmatch.filter(done, pattern) for pattern in package.build_patterns()):
            package.set_status(PackageStatus.FINISHED)
        elif package.failed_name() in failed:
            package.set_status(PackageStatus.FAILED_TO_BUILD,
                               urls=failed_urls.get(package.failed_name()))
        elif is_manual(package):
            package.set_status(PackageStatus.MANUAL_BUILD_REQUIRED)
        else:
            package.set_status(PackageStatus.WAITING_FOR_BUILD)

    # A package is only worth starting once everything it links against exists.
    for package in packages:
        if package.status != PackageStatus.WAITING_FOR_BUILD:
            continue
        for dependency in sorted(package.ext_depends, key=lambda p: p.name):
            if dependency.status == PackageStatus.FINISHED:
                continue
            if is_optional_dep(package, dependency):
                continue
            package.set_blocked(PackageStatus.WAITING_FOR_DEPENDENCIES, dependency)

    # Hold back finished packages whose dependencies or dependents are not
    # finished. Publishing one alone would put a package in the repository
    # built against something nobody else has, which is the whole reason
    # dependents get rebuilt in the first place.
    # Being blocked is contagious: a package whose dependency is only
    # finished-but-blocked is not publishable either, so the pass runs until
    # nothing moves. It only ever turns FINISHED into FINISHED_BUT_BLOCKED,
    # which is what makes it terminate.
    changed = True
    while changed:
        changed = False
        for package in packages:
            if package.status != PackageStatus.FINISHED:
                continue
            for dependency in sorted(package.ext_depends, key=lambda p: p.name):
                if dependency.status != PackageStatus.FINISHED:
                    package.set_blocked(PackageStatus.FINISHED_BUT_BLOCKED, dependency)
                    changed = True
            for dependent in sorted(package.ext_rdepends, key=lambda p: p.name):
                if dependent.name in config.IGNORE_RDEP_PACKAGES:
                    continue
                # A dependent that is not in the repository yet cannot be
                # broken by publishing this one.
                if dependent.status != PackageStatus.FINISHED and not dependent.is_new:
                    package.set_blocked(PackageStatus.FINISHED_BUT_BLOCKED, dependent)
                    changed = True


def find_stale_packages(packages: Iterable[Package],
                        built_at: dict[str, float]) -> list[Package]:
    """Finished packages built before one of their dependencies.

    Blocking a dependent is not enough when the decision is to rebuild it: a
    package that links against a dependency built after it carries the old
    code. Comparing build times says exactly that, and says it idempotently,
    because a rebuilt package is no longer older than what it links against.
    """

    def newest(package: Package) -> float:
        times = [built_at.get(name, 0.0) for name in _asset_names(package, built_at)]
        return max(times) if times else 0.0

    def oldest(package: Package) -> float:
        times = [built_at.get(name, 0.0) for name in _asset_names(package, built_at)]
        return min(times) if times else 0.0

    stale = []
    for package in packages:
        if package.status not in FINISHED_STATES:
            continue
        own = oldest(package)
        if not own:
            continue
        for dependency in package.ext_depends:
            if dependency.status not in FINISHED_STATES:
                continue
            if newest(dependency) > own:
                stale.append(package)
                break
    return stale


def _asset_names(package: Package, available: dict[str, float]) -> list[str]:
    names = []
    for pattern in package.build_patterns():
        names.extend(fnmatch.filter(available, pattern))
    return names


def get_cycles(packages: Iterable[Package]) -> list[tuple[str, str]]:
    """Pairs of packages that transitively depend on each other.

    Branches whose root is already finished are cut: a cycle that is fully
    built is not a problem that needs solving.
    """

    def transitive(start: Package) -> set[Package]:
        todo = [start]
        seen: set[Package] = set()
        result: set[Package] = set()
        while todo:
            package = todo.pop()
            if package in seen:
                continue
            seen.add(package)
            if package is not start and package.status in FINISHED_STATES:
                continue
            if package is not start:
                result.add(package)
            todo.extend(package.ext_depends)
        return result

    cycles: set[tuple[str, str]] = set()
    for package in packages:
        for dependency in transitive(package):
            if is_optional_dep(package, dependency) or is_optional_dep(dependency, package):
                continue
            if package in transitive(dependency):
                cycles.add(tuple(sorted([package.name, dependency.name])))  # type: ignore[arg-type]
    return sorted(cycles)


def install_order(package: Package) -> list[Package]:
    """Transitive dependencies of a package, dependencies first.

    Packages that cannot coexist are resolved by dropping every alternative
    but the one that is actually reachable first.
    """

    order: list[Package] = []
    seen: set[Package] = set()

    def visit(current: Package) -> None:
        if current in seen:
            return
        seen.add(current)
        for dependency in sorted(current.ext_depends, key=lambda p: p.name):
            visit(dependency)
        if current is not package:
            order.append(current)

    visit(package)

    names = [p.name for p in order]
    for group in config.CONFLICTING_DEPS:
        present = [name for name in group if name in names]
        if len(present) > 1:
            keep = present[-1]
            order = [p for p in order if p.name not in present or p.name == keep]
    return order
