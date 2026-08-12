import unittest

from vitasdk_autobuild import config, queue, srcinfo
from vitasdk_autobuild.queue import Package, PackageStatus


WORLD = config.default_world()
OTHER = config.World(arch="vita-musl", core="musl-core", repository="vita-musl")


def make_package(name, version="1.0-1", depends=(), binaries=None, provides=(),
                 makedepends=(), arch=(), worlds=None):
    """Builds a Package the way srcinfo.parse() would hand it over."""

    pkgver, pkgrel = version.split("-")
    info = {
        "pkgbase": name,
        "pkgver": pkgver,
        "pkgrel": pkgrel,
        "makedepends": list(makedepends),
        "arch": list(arch),
        "packages": {},
    }
    for binary in (binaries or [name]):
        info["packages"][binary] = {
            "depends": list(depends),
            "provides": list(provides),
        }
    return Package(info, worlds if worlds is not None else [WORLD])


def queue_of(*packages):
    packages = list(packages)
    queue.link_dependencies(packages)
    return packages


def apply(packages, done, failed, urls=None, worlds=None):
    queue.apply_status(packages, done, failed, urls, worlds or [WORLD])


def install_order_in(package, world=None):
    return queue.install_order(package, world or WORLD)


class TestPackage(unittest.TestCase):

    def test_version_and_patterns(self):
        package = make_package("zlib", "1.3.2-2")
        self.assertEqual(package.version, "1.3.2-2")
        self.assertEqual(package.build_patterns(WORLD), ["zlib-1.3.2-2-vita.pkg.tar.*"])

    def test_pattern_matches_any_compression(self):
        import fnmatch
        pattern = make_package("zlib", "1.3.2-2").build_patterns(WORLD)[0]
        self.assertTrue(fnmatch.fnmatch("zlib-1.3.2-2-vita.pkg.tar.xz", pattern))
        self.assertTrue(fnmatch.fnmatch("zlib-1.3.2-2-vita.pkg.tar.zst", pattern))
        self.assertFalse(fnmatch.fnmatch("zlib-1.3.2-1-vita.pkg.tar.xz", pattern))

    def test_epoch_is_part_of_the_version(self):
        package = Package({
            "pkgbase": "x", "pkgver": "2.0", "pkgrel": "3", "epoch": "1",
            "packages": {"x": {}},
        }, [WORLD])
        self.assertEqual(package.version, "1:2.0-3")

    def test_failed_marker_name(self):
        # The marker is per world: a package can fail in one and build in
        # another.
        self.assertEqual(make_package("zlib", "1.3.2-2").failed_name(WORLD),
                         "zlib-1.3.2-2-vita.failed")

    def test_split_package_has_one_pattern_per_binary(self):
        package = make_package("example", binaries=["example", "example-tools"])
        self.assertEqual(sorted(package.build_patterns(WORLD)), [
            "example-1.0-1-vita.pkg.tar.*",
            "example-tools-1.0-1-vita.pkg.tar.*",
        ])

    def test_dependencies_within_the_same_recipe_are_dropped(self):
        package = make_package("example", binaries=["example", "example-tools"],
                               depends=["example", "zlib"])
        self.assertEqual(package.depends, ["zlib"])

    def test_makedepends_count_as_dependencies(self):
        package = make_package("thing", depends=["zlib"], makedepends=["cmake-helper"])
        self.assertEqual(package.depends, ["cmake-helper", "zlib"])

    def test_version_constraints_are_stripped(self):
        self.assertEqual(make_package("thing", depends=["zlib>=1.2"]).depends, ["zlib"])


class TestLinking(unittest.TestCase):

    def test_links_both_directions(self):
        zlib = make_package("zlib")
        png = make_package("libpng", depends=["zlib"])
        queue_of(zlib, png)
        self.assertEqual(png.ext_depends, {zlib})
        self.assertEqual(zlib.ext_rdepends, {png})

    def test_resolves_through_provides(self):
        provider = make_package("openssl-1.1.1", provides=["openssl"])
        user = make_package("curl", depends=["openssl"])
        queue_of(provider, user)
        self.assertEqual(user.ext_depends, {provider})

    def test_real_package_wins_over_provides(self):
        real = make_package("openssl")
        alternative = make_package("openssl-1.1.1", provides=["openssl"])
        user = make_package("curl", depends=["openssl"])
        queue_of(real, alternative, user)
        self.assertEqual(user.ext_depends, {real})

    def test_unknown_dependency_is_fatal(self):
        with self.assertRaises(SystemExit) as caught:
            queue_of(make_package("curl", depends=["nonexistent"]))
        self.assertIn("curl -> nonexistent", str(caught.exception))


class TestStatus(unittest.TestCase):

    def test_present_asset_means_finished(self):
        zlib = make_package("zlib", "1.3.2-2")
        apply([zlib], ["zlib-1.3.2-2-vita.pkg.tar.xz"], [])
        self.assertEqual(zlib.get_status(WORLD), PackageStatus.FINISHED)

    def test_older_asset_does_not_count(self):
        zlib = make_package("zlib", "1.3.2-2")
        apply([zlib], ["zlib-1.3.2-1-vita.pkg.tar.xz"], [])
        self.assertEqual(zlib.get_status(WORLD), PackageStatus.WAITING_FOR_BUILD)

    def test_split_package_needs_every_binary(self):
        package = make_package("example", binaries=["example", "example-tools"])
        apply([package], ["example-1.0-1-vita.pkg.tar.xz"], [])
        self.assertEqual(package.get_status(WORLD), PackageStatus.WAITING_FOR_BUILD)

    def test_failure_marker(self):
        zlib = make_package("zlib", "1.3.2-2")
        apply([zlib], [], ["zlib-1.3.2-2-vita.failed"],
              {"zlib-1.3.2-2-vita.failed": {"build": "https://example/1"}})
        self.assertEqual(zlib.get_status(WORLD), PackageStatus.FAILED_TO_BUILD)
        self.assertEqual(zlib.get_details(WORLD)["urls"], {"build": "https://example/1"})

    def test_built_package_beats_a_stale_failure_marker(self):
        zlib = make_package("zlib", "1.3.2-2")
        apply([zlib], ["zlib-1.3.2-2-vita.pkg.tar.xz"], ["zlib-1.3.2-2-vita.failed"])
        self.assertEqual(zlib.get_status(WORLD), PackageStatus.FINISHED)

    def test_waits_for_dependencies(self):
        zlib = make_package("zlib")
        png = make_package("libpng", depends=["zlib"])
        packages = queue_of(zlib, png)
        apply(packages, [], [])
        self.assertEqual(zlib.get_status(WORLD), PackageStatus.WAITING_FOR_BUILD)
        self.assertEqual(png.get_status(WORLD), PackageStatus.WAITING_FOR_DEPENDENCIES)
        self.assertIn("zlib", png.get_details(WORLD)["desc"])

    def test_ready_once_the_dependency_is_built(self):
        zlib = make_package("zlib")
        png = make_package("libpng", depends=["zlib"])
        packages = queue_of(zlib, png)
        apply(packages, ["zlib-1.0-1-vita.pkg.tar.xz"], [])
        self.assertEqual(png.get_status(WORLD), PackageStatus.WAITING_FOR_BUILD)

    def test_finished_package_is_blocked_by_an_unbuilt_dependency(self):
        zlib = make_package("zlib")
        png = make_package("libpng", depends=["zlib"])
        packages = queue_of(zlib, png)
        apply(packages, ["libpng-1.0-1-vita.pkg.tar.xz"], [])
        self.assertEqual(png.get_status(WORLD), PackageStatus.FINISHED_BUT_BLOCKED)

    def test_finished_package_is_blocked_by_a_published_dependent(self):
        # zlib is rebuilt, libpng is in the repository and not rebuilt yet:
        # publishing zlib alone would leave libpng linked against the old one.
        zlib = make_package("zlib")
        png = make_package("libpng", depends=["zlib"])
        packages = queue_of(zlib, png)
        png.repo_versions[WORLD.arch] = "0.9-1"
        apply(packages, ["zlib-1.0-1-vita.pkg.tar.xz"], [])
        self.assertEqual(zlib.get_status(WORLD), PackageStatus.FINISHED_BUT_BLOCKED)
        self.assertIn("libpng", zlib.get_details(WORLD)["desc"])

    def test_a_dependent_that_was_never_published_does_not_block(self):
        zlib = make_package("zlib")
        png = make_package("libpng", depends=["zlib"])
        packages = queue_of(zlib, png)
        apply(packages, ["zlib-1.0-1-vita.pkg.tar.xz"], [])
        self.assertEqual(zlib.get_status(WORLD), PackageStatus.FINISHED)

    def test_blocking_propagates_along_the_chain(self):
        a = make_package("a")
        b = make_package("b", depends=["a"])
        c = make_package("c", depends=["b"])
        packages = queue_of(a, b, c)
        apply(packages, [
            "b-1.0-1-vita.pkg.tar.xz", "c-1.0-1-vita.pkg.tar.xz"], [])
        self.assertEqual(a.get_status(WORLD), PackageStatus.WAITING_FOR_BUILD)
        self.assertEqual(b.get_status(WORLD), PackageStatus.FINISHED_BUT_BLOCKED)
        self.assertEqual(c.get_status(WORLD), PackageStatus.FINISHED_BUT_BLOCKED)
        self.assertIn("a", c.get_details(WORLD)["desc"])

    def test_everything_built_is_simply_finished(self):
        a = make_package("a")
        b = make_package("b", depends=["a"])
        packages = queue_of(a, b)
        apply(packages, [
            "a-1.0-1-vita.pkg.tar.xz", "b-1.0-1-vita.pkg.tar.xz"], [])
        self.assertEqual([p.get_status(WORLD) for p in packages],
                         [PackageStatus.FINISHED, PackageStatus.FINISHED])

    def test_manual_build_required(self):
        original = config.MANUAL_BUILD
        config.MANUAL_BUILD = ["cpython*"]
        try:
            package = make_package("cpython2")
            apply([package], [], [])
            self.assertEqual(package.get_status(WORLD), PackageStatus.MANUAL_BUILD_REQUIRED)
        finally:
            config.MANUAL_BUILD = original


class TestCycles(unittest.TestCase):

    def test_detects_a_two_package_cycle(self):
        a = make_package("a", depends=["b"])
        b = make_package("b", depends=["a"])
        packages = queue_of(a, b)
        apply(packages, [], [])
        self.assertEqual(queue.get_cycles(packages, WORLD), [("a", "b")])

    def test_no_cycle_in_a_chain(self):
        a = make_package("a")
        b = make_package("b", depends=["a"])
        packages = queue_of(a, b)
        apply(packages, [], [])
        self.assertEqual(queue.get_cycles(packages, WORLD), [])

    def test_a_built_cycle_is_not_reported(self):
        a = make_package("a", depends=["b"])
        b = make_package("b", depends=["a"])
        packages = queue_of(a, b)
        apply(packages, [
            "a-1.0-1-vita.pkg.tar.xz", "b-1.0-1-vita.pkg.tar.xz"], [])
        self.assertEqual(queue.get_cycles(packages, WORLD), [])


class TestOptionalDeps(unittest.TestCase):

    def setUp(self):
        self.original = config.OPTIONAL_DEPS
        config.OPTIONAL_DEPS = {"a": ["b"]}
        self.addCleanup(setattr, config, "OPTIONAL_DEPS", self.original)

    def test_optional_dependency_does_not_block_when_published(self):
        a = make_package("a", depends=["b"])
        b = make_package("b", depends=["a"])
        packages = queue_of(a, b)
        b.repo_versions[WORLD.arch] = "0.9-1"
        apply(packages, [], [])
        self.assertEqual(a.get_status(WORLD), PackageStatus.WAITING_FOR_BUILD)

    def test_optional_dependency_still_blocks_when_never_published(self):
        a = make_package("a", depends=["b"])
        b = make_package("b", depends=["a"])
        packages = queue_of(a, b)
        apply(packages, [], [])
        self.assertEqual(a.get_status(WORLD), PackageStatus.WAITING_FOR_DEPENDENCIES)


class TestInstallOrder(unittest.TestCase):

    def test_dependencies_come_first(self):
        a = make_package("a")
        b = make_package("b", depends=["a"])
        c = make_package("c", depends=["b"])
        queue_of(a, b, c)
        self.assertEqual([p.name for p in install_order_in(c)], ["a", "b"])

    def test_excludes_the_package_itself(self):
        a = make_package("a")
        b = make_package("b", depends=["a"])
        queue_of(a, b)
        self.assertNotIn(b, install_order_in(b))

    def test_survives_a_cycle(self):
        a = make_package("a", depends=["b"])
        b = make_package("b", depends=["a"])
        queue_of(a, b)
        self.assertEqual([p.name for p in install_order_in(a)], ["b"])

    def test_conflicting_dependencies_keep_the_last_of_the_group(self):
        original = config.CONFLICTING_DEPS
        config.CONFLICTING_DEPS = [["openssl", "openssl-1.1.1"]]
        try:
            openssl = make_package("openssl")
            old = make_package("openssl-1.1.1")
            curl = make_package("curl", depends=["openssl", "openssl-1.1.1"])
            queue_of(openssl, old, curl)
            names = [p.name for p in install_order_in(curl)]
            self.assertEqual(names, ["openssl-1.1.1"])
        finally:
            config.CONFLICTING_DEPS = original


class TestBuildQueueFromSrcinfo(unittest.TestCase):

    def test_package_built_from_real_srcinfo(self):
        info = srcinfo.parse(
            "pkgbase = libvita2d\n\tpkgver = 9999\n\tpkgrel = 1\n"
            "\tdepends = zlib\n\npkgname = libvita2d\n")
        package = Package(info)
        self.assertEqual(package.version, "9999-1")
        self.assertEqual(package.depends, ["zlib"])
        self.assertEqual(package.build_patterns(WORLD), ["libvita2d-9999-1-vita.pkg.tar.*"])


if __name__ == "__main__":
    unittest.main()
