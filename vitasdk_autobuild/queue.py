"""The build queue: what exists, what it needs, and what state it is in.

The queue is derived from three inputs and nothing else: the recipes, the file
names present in the staging release, and the failure markers. A package is
built when its file is not there, which means the version is part of the
answer and no database has to be diffed.

Everything is keyed by *(package, world)*. A world is an architecture, a libc
and a toolchain taken together, named by the target triple; a package can be
finished in one world, failing in another and not exist in a third.
"""

import fnmatch
from enum import Enum
from typing import Any, Iterable

from . import config, srcinfo
from .config import World

ANY_ARCH = "any"


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


class Package:
    """One recipe, with the binary packages it produces, per world."""

    def __init__(self, info: dict[str, Any], worlds: Iterable[World] | None = None) -> None:
        self.name: str = info["pkgbase"]
        self.repo_path: str = info.get("repo_path", self.name)
        self.description: str = info.get("pkgdesc", "")
        self.url: str = info.get("url", "")
        self.licenses: list[str] = list(info.get("license", []))
        # Declared by the recipe, not derived: a package nobody should start
        # something new on top of, and why.
        self.deprecated: str = info.get("deprecated", "")
        epoch = info.get("epoch", "")
        self.version: str = f"{info['pkgver']}-{info['pkgrel']}"
        if epoch:
            self.version = f"{epoch}:{self.version}"

        configured = list(worlds) if worlds is not None else config.worlds()
        declared = [a for a in info.get("arch", []) if a]

        # A recipe builds for every world unless that world says otherwise.
        # It used to be able to restrict itself by naming architectures, but
        # arch is also the first field anyone fills in when writing ordinary
        # pacman metadata, and doing so silently removed the package and
        # everything depending on it from every other world. Which packages a
        # world cannot build is now the world's own business, in config.
        #
        # arch=('any') still means what it says: one file, no architecture in
        # its name, serving every world at once.
        self.any_arch: bool = ANY_ARCH in declared
        self.worlds: list[World] = [w for w in configured if w.builds(self.name)]
        self.declared_arch: list[str] = declared

        self.binaries: dict[str, dict[str, Any]] = {}
        depends: list[str] = []
        provides: list[str] = []
        for name, binary in info["packages"].items():
            self.binaries[name] = {
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
        self.repo_versions: dict[str, str] = {}
        self.builds: dict[str, dict[str, Any]] = {
            world.arch: {"status": PackageStatus.UNKNOWN, "details": {}}
            for world in self.worlds
        }

    def __repr__(self) -> str:
        return f"Package({self.name!r})"

    def __hash__(self) -> int:
        return hash(self.name)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Package) and other.name == self.name

    @property
    def repo_version(self) -> str:
        """What the published repository holds, for display."""

        return next((v for v in self.repo_versions.values() if v), "")

    def is_new_in(self, world: World) -> bool:
        """True when this package is not in the published repository of a world.

        Publishing a package cannot break a dependent that was never published,
        which is what keeps a first publication from blocking on itself.
        """

        return not self.repo_versions.get(world.arch)

    def builds_for(self, world: World) -> bool:
        return world.arch in self.builds

    def file_arch(self, world: World) -> str:
        """The architecture that ends up in the file name.

        An arch-independent package produces one file that serves every world,
        so it is built once and counted everywhere.
        """

        return ANY_ARCH if self.any_arch else world.arch

    def build_world(self) -> World:
        """The world an arch-independent package is actually built in."""

        return self.worlds[0]

    def build_patterns(self, world: World) -> list[str]:
        arch = self.file_arch(world)
        return [f"{name}-{self.version}-{arch}.pkg.tar.*" for name in self.binaries]

    def failed_name(self, world: World) -> str:
        return f"{self.name}-{self.version}-{self.file_arch(world)}.failed"

    def get_status(self, world: World) -> PackageStatus:
        return self.builds.get(world.arch, {}).get("status", PackageStatus.UNKNOWN)

    def get_details(self, world: World) -> dict[str, Any]:
        return self.builds.get(world.arch, {}).get("details", {})

    def set_status(self, world: World, status: PackageStatus, description: str = "",
                   urls: dict[str, str] | None = None) -> None:
        build = self.builds.setdefault(world.arch, {})
        build["status"] = status
        build["details"] = {"desc": description, "urls": urls or {}}

    def set_blocked(self, world: World, status: PackageStatus, blocker: "Package") -> None:
        details = self.get_details(world)
        blocked = set(details.get("blocked", ()))
        inherited = blocker.get_details(world).get("blocked")
        blocked = set(inherited) if inherited else blocked | {blocker.name}
        self.set_status(world, status, "Blocked by: " + ", ".join(sorted(blocked)))
        self.builds[world.arch]["details"]["blocked"] = sorted(blocked)


def build_queue(packages_dir: str, worlds: Iterable[World] | None = None) -> list[Package]:
    """Reads every recipe and links the dependency graph both ways."""

    configured = list(worlds) if worlds is not None else config.worlds()
    packages = [Package(info, configured) for info in srcinfo.collect(packages_dir)]
    link_dependencies(packages)
    prune_impossible_worlds(packages)
    return packages


def prune_impossible_worlds(packages: Iterable[Package]) -> dict[str, list[str]]:
    """Drops worlds a package cannot be built for after all.

    A recipe can declare a world its dependencies do not support, and worlds
    never link against each other, so that package simply cannot exist there.
    Pruning propagates: dropping a library drops whatever needed it.

    Returns what was dropped, so the caller can say so out loud instead of
    quietly building something without a dependency it asked for.
    """

    packages = list(packages)
    dropped: dict[str, list[str]] = {}
    changed = True
    while changed:
        changed = False
        for package in packages:
            for arch in list(package.builds):
                world = next((w for w in package.worlds if w.arch == arch), None)
                if world is None:
                    continue
                unmet = [d.name for d in package.ext_depends if not d.builds_for(world)]
                if not unmet:
                    continue
                del package.builds[arch]
                package.worlds = [w for w in package.worlds if w.arch != arch]
                dropped.setdefault(package.name, []).append(arch)
                changed = True
    return dropped


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


def dependencies_in(package: Package, world: World) -> list[Package]:
    """Dependencies of a package within one world.

    A dependency that does not build for this world cannot satisfy anything in
    it: worlds never link against each other.
    """

    return sorted((d for d in package.ext_depends if d.builds_for(world)),
                  key=lambda p: p.name)


def dependents_in(package: Package, world: World) -> list[Package]:
    return sorted((d for d in package.ext_rdepends if d.builds_for(world)),
                  key=lambda p: p.name)


def missing_dependencies(package: Package, world: World) -> list[str]:
    """Dependencies this package needs that nothing provides in this world."""

    available = {d.name for d in package.ext_depends if d.builds_for(world)}
    return sorted(d.name for d in package.ext_depends if d.name not in available)


def is_optional_dep(package: Package, dependency: Package) -> bool:
    """Dependencies manually marked optional, to break a cycle.

    Only honoured once the dependency is in the published repository: until
    then there is nothing to build against and the cycle is real.
    """

    return (dependency.name in config.OPTIONAL_DEPS.get(package.name, [])
            and dependency.repo_version != "")


def is_manual(package: Package) -> bool:
    return any(fnmatch.fnmatchcase(package.name, pattern)
               for pattern in config.MANUAL_BUILD)


def apply_status(packages: Iterable[Package], done_names: Iterable[str],
                 failed_names: Iterable[str],
                 failed_urls: dict[str, dict[str, str]] | None = None,
                 worlds: Iterable[World] | None = None) -> None:
    """Computes the state of every package, in every world it builds for."""

    packages = list(packages)
    configured = list(worlds) if worlds is not None else config.worlds()
    done = list(done_names)
    failed = set(failed_names)
    failed_urls = failed_urls or {}

    for package in packages:
        for world in configured:
            if not package.builds_for(world):
                continue
            if all(fnmatch.filter(done, pattern)
                   for pattern in package.build_patterns(world)):
                package.set_status(world, PackageStatus.FINISHED)
            elif package.failed_name(world) in failed:
                package.set_status(world, PackageStatus.FAILED_TO_BUILD,
                                   urls=failed_urls.get(package.failed_name(world)))
            elif is_manual(package):
                package.set_status(world, PackageStatus.MANUAL_BUILD_REQUIRED)
            else:
                package.set_status(world, PackageStatus.WAITING_FOR_BUILD)

    # A package is only worth starting once everything it links against exists
    # in the same world.
    for package in packages:
        for world in configured:
            if package.get_status(world) != PackageStatus.WAITING_FOR_BUILD:
                continue
            for dependency in dependencies_in(package, world):
                if dependency.get_status(world) == PackageStatus.FINISHED:
                    continue
                if is_optional_dep(package, dependency):
                    continue
                package.set_blocked(world, PackageStatus.WAITING_FOR_DEPENDENCIES, dependency)

    # Being blocked is contagious: a package whose dependency is only
    # finished-but-blocked is not publishable either, so the pass runs until
    # nothing moves. It only ever turns FINISHED into FINISHED_BUT_BLOCKED,
    # which is what makes it terminate.
    changed = True
    while changed:
        changed = False
        for package in packages:
            for world in configured:
                if package.get_status(world) != PackageStatus.FINISHED:
                    continue
                for dependency in dependencies_in(package, world):
                    if dependency.get_status(world) != PackageStatus.FINISHED:
                        package.set_blocked(world, PackageStatus.FINISHED_BUT_BLOCKED, dependency)
                        changed = True
                for dependent in dependents_in(package, world):
                    if dependent.name in config.IGNORE_RDEP_PACKAGES:
                        continue
                    # A dependent that is not in the repository yet cannot be
                    # broken by publishing this one.
                    if (dependent.get_status(world) != PackageStatus.FINISHED
                            and not dependent.is_new_in(world)):
                        package.set_blocked(world, PackageStatus.FINISHED_BUT_BLOCKED, dependent)
                        changed = True


def find_stale_packages(packages: Iterable[Package], built_at: dict[str, float],
                        world: World) -> list[Package]:
    """Finished packages built before one of their dependencies, in one world.

    Blocking a dependent is not enough when the decision is to rebuild it: a
    package that links against a dependency built after it carries the old
    code. Comparing build times says exactly that, and says it idempotently,
    because a rebuilt package is no longer older than what it links against.
    """

    finished = (PackageStatus.FINISHED, PackageStatus.FINISHED_BUT_BLOCKED)

    def times(package: Package) -> list[float]:
        return [built_at[name] for name in asset_names(package, world, built_at)]

    stale = []
    for package in packages:
        if not package.builds_for(world) or package.get_status(world) not in finished:
            continue
        own = times(package)
        if not own:
            continue
        for dependency in dependencies_in(package, world):
            if dependency.get_status(world) not in finished:
                continue
            newer = times(dependency)
            if newer and max(newer) > min(own):
                stale.append(package)
                break
    return stale


def asset_names(package: Package, world: World, available: Iterable[str]) -> list[str]:
    """Files of this package present among the given names, in one world."""

    names = list(available)
    found = []
    for pattern in package.build_patterns(world):
        found.extend(fnmatch.filter(names, pattern))
    return found


def get_cycles(packages: Iterable[Package], world: World | None = None) -> list[tuple[str, str]]:
    """Pairs of packages that transitively depend on each other.

    Branches whose root is already finished are cut: a cycle that is fully
    built is not a problem that needs solving.
    """

    packages = list(packages)
    world = world or config.default_world()
    finished = (PackageStatus.FINISHED, PackageStatus.FINISHED_BUT_BLOCKED)

    def transitive(start: Package) -> set[Package]:
        todo = [start]
        seen: set[Package] = set()
        result: set[Package] = set()
        while todo:
            package = todo.pop()
            if package in seen:
                continue
            seen.add(package)
            if package is not start and package.get_status(world) in finished:
                continue
            if package is not start:
                result.add(package)
            todo.extend(dependencies_in(package, world))
        return result

    cycles: set[tuple[str, str]] = set()
    for package in packages:
        if not package.builds_for(world):
            continue
        for dependency in transitive(package):
            if is_optional_dep(package, dependency) or is_optional_dep(dependency, package):
                continue
            if package in transitive(dependency):
                cycles.add(tuple(sorted([package.name, dependency.name])))  # type: ignore[arg-type]
    return sorted(cycles)


def install_order(package: Package, world: World) -> list[Package]:
    """Transitive dependencies of a package in one world, dependencies first.

    Packages that cannot coexist are resolved by dropping every alternative
    but the one that is actually reachable last.
    """

    order: list[Package] = []
    seen: set[Package] = set()

    def visit(current: Package) -> None:
        if current in seen:
            return
        seen.add(current)
        for dependency in dependencies_in(current, world):
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
