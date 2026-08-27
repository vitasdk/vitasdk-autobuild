"""Reads the real recipes with the real vita-makepkg.

Skipped unless asked for, because it clones two repositories and forks a shell
per recipe. It is the test that proves the queue can be computed on a bare
runner with no SDK installed, which is what the supervisor does.
"""

import os
import shutil
import unittest

from vitasdk_autobuild import queue, srcinfo, state

ENABLED = os.environ.get("VITASDK_AUTOBUILD_SRCINFO_TEST") == "1"


@unittest.skipUnless(ENABLED, "set VITASDK_AUTOBUILD_SRCINFO_TEST=1 to run")
@unittest.skipUnless(shutil.which("git"), "git is required")
class TestRealRecipes(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.packages_dir = state.packages_checkout()
        cls.packages = queue.build_queue(cls.packages_dir)

    def test_every_recipe_is_readable(self):
        directories = [name for name in os.listdir(self.packages_dir)
                       if os.path.isfile(os.path.join(self.packages_dir, name, "VITABUILD"))]
        self.assertEqual(len(self.packages), len(directories))

    def test_the_catalogue_is_not_trivially_small(self):
        self.assertGreater(len(self.packages), 100)

    def test_every_dependency_resolves(self):
        # build_queue() raises when a recipe depends on something no recipe
        # provides, so reaching this point is the assertion.
        self.assertTrue(any(package.ext_depends for package in self.packages))

    def test_every_package_has_a_usable_file_name(self):
        # A file name belongs to a world, so it is asked for one world at a
        # time and checked against that world's architecture rather than
        # against the name the only world happened to have.
        for package in self.packages:
            with self.subTest(package=package.name):
                for world in package.worlds:
                    for pattern in package.build_patterns(world):
                        self.assertTrue(
                            pattern.endswith(f"-{world.arch}.pkg.tar.*"), pattern)

    def test_no_recipe_varies_by_architecture(self):
        # build_queue() refuses such a recipe, so getting a queue at all is
        # most of the assertion. Reading the text again names the offender
        # instead of only saying the catalogue is fine.
        from vitasdk_autobuild import recipes
        for package in self.packages:
            path = os.path.join(self.packages_dir, package.repo_path, "VITABUILD")
            with open(path, encoding="utf-8") as handle:
                self.assertEqual(recipes.declared_per_arch(handle.read()), [],
                                 f"{package.name} varies by architecture")

    def test_every_declared_architecture_is_a_configured_world(self):
        # A typo in arch=(...) does not fail: it quietly removes the package
        # from every world, and everything depending on it follows.
        from vitasdk_autobuild import config
        known = {world.arch for world in config.WORLDS} | {queue.ANY_ARCH}
        for package in self.packages:
            with self.subTest(package=package.name):
                self.assertLessEqual(set(package.declared_arch), known)

    def test_a_second_world_keeps_the_catalogue(self):
        """Adding a world is a configuration entry, not 130 recipe edits.

        Recipes that declare no arch inherit a new world for free, which is
        what the whole variant design rests on. One that does declare an arch
        opts out of every other world and takes everything depending on it
        along, so this bounds how far that can spread before the axis stops
        being worth having. It is a bound rather than a count because the
        recipes live in their own repository and move on their own.
        """

        from vitasdk_autobuild import config
        original = list(config.WORLDS)
        try:
            config.apply_overrides({"WORLDS": original + [
                config.World(arch="vita-second", core="core-second",
                             repository="vita-second", triple="arm-vita-eabi")]})
            packages = queue.build_queue(self.packages_dir)
        finally:
            config.apply_overrides({"WORLDS": original})

        lost = sorted(package.name for package in packages
                      if not any(world.arch == "vita-second" for world in package.worlds))
        # Half, not a number closer to the truth: the recipes move on their
        # own and today's exact count would fail the day one of them opts out
        # on purpose. What this has to catch is the axis being gutted, and the
        # message is what tells whoever hits it which packages went.
        self.assertGreater(
            len(packages) - len(lost), len(packages) // 2,
            f"a second world would lose {len(lost)} of {len(packages)} packages: {lost}")

    def test_the_queue_has_no_cycles(self):
        queue.apply_status(self.packages, [], [])
        self.assertEqual(queue.get_cycles(self.packages), [])

    def test_reading_a_recipe_twice_hits_the_cache(self):
        makepkg = srcinfo.find_vita_makepkg()
        first = srcinfo.read(os.path.join(self.packages_dir, "zlib"), makepkg)
        second = srcinfo.read(os.path.join(self.packages_dir, "zlib"), makepkg)
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
