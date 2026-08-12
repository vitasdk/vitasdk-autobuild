import unittest

from vitasdk_autobuild import recipes

LIVE_RECIPE = """\
pkgname=libvita2d
pkgver=9999
pkgrel=3
url="https://github.com/xerpi/libvita2d"
depends=('zlib')
source=("git+https://github.com/xerpi/libvita2d.git")
sha256sums=('SKIP')

build() {
  cd $pkgname
  make
}
"""

PINNED_RECIPE = """\
pkgname=kubridge
pkgver=1.0
pkgrel=1
source=("git+https://github.com/TheOfficialFloW/kubridge.git#tag=v1.0")
sha256sums=('SKIP')
"""

VARIABLE_RECIPE = """\
pkgname=libtoloader
pkgver=9999
pkgrel=1
source=("git+https://github.com/Rinnegatamante/$pkgname.git")
sha256sums=('SKIP')
"""


class TestFindSources(unittest.TestCase):

    def test_finds_a_git_source(self):
        sources = recipes.find_sources(LIVE_RECIPE)
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0].protocol, "git")
        self.assertEqual(sources[0].url, "https://github.com/xerpi/libvita2d.git")
        self.assertEqual(sources[0].fragment, "")

    def test_reads_a_fragment(self):
        source = recipes.find_sources(PINNED_RECIPE)[0]
        self.assertEqual(source.fragment, "#tag=v1.0")

    def test_a_plain_tarball_is_not_a_vcs_source(self):
        self.assertEqual(recipes.find_sources('source=("https://x/y-1.0.tar.gz")'), [])

    def test_pinned_detection(self):
        self.assertTrue(recipes.find_sources(PINNED_RECIPE)[0].pinned)
        self.assertFalse(recipes.find_sources(LIVE_RECIPE)[0].pinned)

    def test_a_branch_is_not_a_pin(self):
        source = recipes.find_sources('source=("git+https://x/y.git#branch=master")')[0]
        self.assertFalse(source.pinned)


class TestExpand(unittest.TestCase):

    def test_expands_pkgname(self):
        self.assertEqual(
            recipes.expand("https://github.com/R/$pkgname.git", "libtoloader", "9999"),
            "https://github.com/R/libtoloader.git")

    def test_expands_braced_form(self):
        self.assertEqual(recipes.expand("https://x/${pkgname}.git", "thing", "1"),
                         "https://x/thing.git")


class TestVersions(unittest.TestCase):

    def test_a_live_version_has_no_meaningful_base(self):
        # 9999 is a Gentoo convention for opt-in live packages. Carrying it
        # forward would put every package above any future release for ever.
        self.assertEqual(recipes.version_base("9999"), "0.0.0")

    def test_a_real_version_is_kept_as_the_base(self):
        self.assertEqual(recipes.version_base("1.0.2"), "1.0.2")

    def test_a_previously_generated_version_is_stripped_back(self):
        self.assertEqual(recipes.version_base("1.0.2.r346.g3d9a51f"), "1.0.2")

    def test_generated_versions_do_not_accumulate_suffixes(self):
        version = recipes.make_version(
            recipes.version_base("0.0.0.r142.ga8f15ab"), 147, "b31c9de1234")
        self.assertEqual(version, "0.0.0.r147.gb31c9de")

    def test_version_shape(self):
        self.assertEqual(recipes.make_version("2.0", 37, "3434597abcdef"),
                         "2.0.r37.g3434597")

    def test_versions_rise_with_history(self):
        # pacman compares r37 and r40 numerically once the shared prefix
        # matches, which is what makes an update reach an installed system.
        old = recipes.make_version("2.0", 37, "aaaaaaa")
        new = recipes.make_version("2.0", 40, "bbbbbbb")
        self.assertLess(old, new)


class TestRewrite(unittest.TestCase):

    def test_sets_version_and_pins_the_source(self):
        pins = {"https://github.com/xerpi/libvita2d.git": "a8f15ab0" + "c" * 32}
        result = recipes.rewrite(LIVE_RECIPE, "0.0.0.r142.ga8f15ab", pins)
        self.assertIn("pkgver=0.0.0.r142.ga8f15ab", result)
        self.assertIn("#commit=a8f15ab0" + "c" * 32, result)

    def test_resets_the_release(self):
        result = recipes.rewrite(LIVE_RECIPE, "0.0.0.r142.ga8f15ab", {})
        self.assertIn("pkgrel=1", result)
        self.assertNotIn("pkgrel=3", result)

    def test_leaves_the_rest_of_the_recipe_alone(self):
        result = recipes.rewrite(LIVE_RECIPE, "0.0.0.r142.ga8f15ab", {})
        self.assertIn("depends=('zlib')", result)
        self.assertIn("  make", result)
        self.assertIn('url="https://github.com/xerpi/libvita2d"', result)

    def test_does_not_touch_a_url_that_is_not_a_source(self):
        # url= and source= point at the same project; only the source carries
        # the git+ prefix and must be pinned.
        pins = {"https://github.com/xerpi/libvita2d.git": "abc1234" + "d" * 33}
        result = recipes.rewrite(LIVE_RECIPE, "1", pins)
        self.assertIn('url="https://github.com/xerpi/libvita2d"\n', result)

    def test_replaces_an_existing_fragment(self):
        text = 'source=("git+https://x/y.git#branch=master")'
        result = recipes.rewrite(text, "1", {"https://x/y.git": "f" * 40})
        self.assertNotIn("#branch=master", result)
        self.assertIn("#commit=" + "f" * 40, result)

    def test_rewriting_is_idempotent(self):
        pins = {"https://github.com/xerpi/libvita2d.git": "a" * 40}
        once = recipes.rewrite(LIVE_RECIPE, "0.0.0.r1.gaaaaaaa", pins)
        twice = recipes.rewrite(once, "0.0.0.r1.gaaaaaaa", pins)
        self.assertEqual(once, twice)


class TestPlanUpdate(unittest.TestCase):

    def test_a_pinned_recipe_needs_no_update(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            with open(os.path.join(directory, "VITABUILD"), "w", encoding="utf-8") as handle:
                handle.write(PINNED_RECIPE)
            info = {"pkgbase": "kubridge", "pkgver": "1.0"}
            self.assertIsNone(recipes.plan_update(directory, info, directory))

    def test_an_unresolvable_url_is_reported(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            with open(os.path.join(directory, "VITABUILD"), "w", encoding="utf-8") as handle:
                handle.write(VARIABLE_RECIPE.replace("$pkgname", "$unknownvariable"))
            info = {"pkgbase": "libtoloader", "pkgver": "9999"}
            with self.assertRaises(ValueError):
                recipes.plan_update(directory, info, directory)


class TestSourceRef(unittest.TestCase):
    """Which upstream ref a recipe actually builds from."""

    def ref_of(self, text):
        return recipes.source_ref(recipes.find_sources(text)[0])

    def test_no_fragment_means_the_default_branch(self):
        self.assertEqual(self.ref_of('source=("git+https://x/y.git")'), "HEAD")

    def test_a_named_branch_is_used(self):
        # cpython and cpython3 share a repository and differ only by branch:
        # resolving HEAD for both would pin them to the same commit and
        # silently change what cpython3 builds.
        self.assertEqual(self.ref_of('source=("git+https://x/y.git#branch=3.11")'), "3.11")

    def test_a_branch_with_dashes_and_underscores(self):
        self.assertEqual(
            self.ref_of('source=("git+https://x/y.git#branch=OpenSSL_1_1_1-vita")'),
            "OpenSSL_1_1_1-vita")

    def test_a_pinned_source_is_never_resolved(self):
        source = recipes.find_sources('source=("git+https://x/y.git#commit=abc")')[0]
        self.assertTrue(source.pinned)


if __name__ == "__main__":
    unittest.main()
