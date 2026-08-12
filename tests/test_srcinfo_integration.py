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
        for package in self.packages:
            with self.subTest(package=package.name):
                for pattern in package.build_patterns():
                    self.assertTrue(pattern.endswith("-vita.pkg.tar.*"), pattern)

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
