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

PINNED_BUT_LIVE = """\
pkgname=kubridge
pkgver=9999
pkgrel=1
source=("git+https://github.com/bythos14/kubridge.git#commit=a4ef20fc3ab07b493f9d7d67703272831e445e21")
sha256sums=('SKIP')
"""


class TestPinnedButLive(unittest.TestCase):
    """A pinned recipe can still carry a version that sorts above everything."""

    def test_a_pinned_recipe_with_a_live_version_is_not_left_alone(self):
        import os
        import tempfile
        from unittest import mock

        with tempfile.TemporaryDirectory() as directory:
            with open(os.path.join(directory, "VITABUILD"), "w", encoding="utf-8") as handle:
                handle.write(PINNED_BUT_LIVE)
            info = {"pkgbase": "kubridge", "pkgver": "9999"}
            with mock.patch.object(recipes, "count_commits", return_value=("a4ef20f" + "c" * 33, 91)):
                update = recipes.plan_update(directory, info, directory)

        self.assertIsNotNone(update)
        self.assertEqual(update.new_version, "0.0.0.r91.ga4ef20f")
        # The commit does not move: only the version comes down.
        self.assertEqual(update.pins, {})

    def test_a_pinned_recipe_with_a_real_version_is_left_alone(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            with open(os.path.join(directory, "VITABUILD"), "w", encoding="utf-8") as handle:
                handle.write(PINNED_BUT_LIVE.replace("pkgver=9999", "pkgver=1.0"))
            info = {"pkgbase": "kubridge", "pkgver": "1.0"}
            self.assertIsNone(recipes.plan_update(directory, info, directory))

class TestEvaluatedSources(unittest.TestCase):
    """Sources built from shell variables are read from .SRCINFO, not guessed."""

    def test_pairs_a_variable_pin_with_its_value(self):
        raw = recipes.find_sources('source=("git+https://x/y.git#commit=${gitrev}")')
        info = {"source": ["git+https://x/y.git#commit=" + "a" * 40]}
        pairs = recipes.evaluated_sources(raw, info)
        self.assertEqual(pairs[raw[0]].fragment, "#commit=" + "a" * 40)

    def test_pairs_several_sources_by_position(self):
        raw = recipes.find_sources(
            'source=("git+https://a/1.git" "git+https://b/2.git#branch=dev")')
        info = {"source": ["git+https://a/1.git", "git+https://b/2.git#branch=dev"]}
        pairs = recipes.evaluated_sources(raw, info)
        self.assertEqual(pairs[raw[1]].fragment, "#branch=dev")

    def test_a_mismatch_falls_back_to_the_recipe_text(self):
        raw = recipes.find_sources('source=("git+https://x/y.git")')
        self.assertEqual(recipes.evaluated_sources(raw, {"source": []}), {})

    def test_non_git_sources_are_ignored_on_both_sides(self):
        raw = recipes.find_sources('source=("https://x/y.tar.gz" "git+https://x/y.git")')
        info = {"source": ["https://x/y.tar.gz", "git+https://x/y.git#commit=" + "b" * 40]}
        pairs = recipes.evaluated_sources(raw, info)
        self.assertEqual(len(pairs), 1)
        self.assertEqual(list(pairs.values())[0].fragment, "#commit=" + "b" * 40)

CONFIG = '''WORLDS: list[World] = [
    World(
        arch="vita",
        core="sdk-snapshot-20260812.565.1",
        repository="vita",
        triple="arm-vita-eabi",
    ),
    World(
        arch="vita-musl",
        core="musl-snapshot-1",
        repository="vita-musl",
    ),
]
'''


class TestSetCore(unittest.TestCase):
    """Repointing a world at a newer core, as a reviewable text change."""

    def test_changes_only_the_named_world(self):
        updated = recipes.set_core(CONFIG, "vita", "sdk-snapshot-20260901.9.1")
        self.assertIn('core="sdk-snapshot-20260901.9.1"', updated)
        self.assertIn('core="musl-snapshot-1"', updated)

    def test_leaves_everything_else_alone(self):
        updated = recipes.set_core(CONFIG, "vita", "new")
        self.assertIn('triple="arm-vita-eabi"', updated)
        self.assertEqual(updated.count("World("), 2)

    def test_the_second_world_can_be_bumped_too(self):
        updated = recipes.set_core(CONFIG, "vita-musl", "musl-snapshot-2")
        self.assertIn('core="sdk-snapshot-20260812.565.1"', updated)
        self.assertIn('core="musl-snapshot-2"', updated)

    def test_an_unknown_world_is_an_error(self):
        with self.assertRaises(ValueError):
            recipes.set_core(CONFIG, "vita-llvm", "whatever")

    def test_the_real_configuration_can_be_rewritten(self):
        # The regex has to match the file as it is actually written, not a
        # sample that happens to look like it.
        import pathlib
        from vitasdk_autobuild import config
        text = pathlib.Path(config.__file__).read_text()
        updated = recipes.set_core(text, config.default_world().arch, "sdk-snapshot-test")
        self.assertIn('core="sdk-snapshot-test"', updated)


class TestFollowDeclaration(unittest.TestCase):
    """Pinning destroys the ref a recipe came from, so it has to be written down."""

    def test_a_scalar_applies_to_every_git_source(self):
        text = "source=('git+https://a.git' 'git+https://b.git')\n_follow=master\n"
        sources = [s for s in recipes.find_sources(text) if s.protocol == "git"]
        self.assertEqual(set(recipes.follow_refs(text, sources).values()), {"master"})

    def test_an_array_is_matched_to_the_sources_in_order(self):
        text = "source=('git+https://a.git' 'git+https://b.git')\n_follow=('main' 'v2')\n"
        sources = [s for s in recipes.find_sources(text) if s.protocol == "git"]
        self.assertEqual(list(recipes.follow_refs(text, sources).values()), ["main", "v2"])

    def test_a_mismatched_array_is_refused_rather_than_guessed(self):
        text = "source=('git+https://a.git')\n_follow=('main' 'v2')\n"
        sources = [s for s in recipes.find_sources(text) if s.protocol == "git"]
        with self.assertRaises(ValueError):
            recipes.follow_refs(text, sources)

    def test_a_silent_recipe_follows_nothing(self):
        text = "source=('git+https://a.git')\n"
        sources = [s for s in recipes.find_sources(text) if s.protocol == "git"]
        self.assertEqual(recipes.follow_refs(text, sources), {})


class TestReleaseProposals(unittest.TestCase):
    """A tag is only worth proposing if it is really ahead of where we are."""

    def test_a_letter_with_a_number_is_a_prerelease(self):
        # Python publishes 3.11.0a5 before 3.11.0, and pacman would call it an
        # upgrade over a version carrying rNNNN because a digit beats a letter.
        self.assertTrue(recipes.is_prerelease("3.11.0a5"))
        self.assertTrue(recipes.is_prerelease("1.7.0beta89"))
        self.assertTrue(recipes.is_prerelease("2.0.0-alpha"))

    def test_a_bare_letter_is_a_patch_release(self):
        # OpenSSL's 1.0.2a is a released version, not a preview of 1.0.2.
        self.assertFalse(recipes.is_prerelease("1.0.2a"))
        self.assertFalse(recipes.is_prerelease("1.1.1w"))

    def test_the_newest_tag_is_picked_by_version_not_by_name(self):
        self.assertGreater(recipes.version_key("1.10"), recipes.version_key("1.9"))
        self.assertGreater(recipes.version_key("1.2"),
                           recipes.version_key("0.0.0.r1430.g3dddc43"))

    def test_a_tag_prefix_is_not_part_of_the_version(self):
        self.assertEqual(recipes.tag_version("v1.2.3"), "1.2.3")
        self.assertEqual(recipes.tag_version("release-2.0"), "2.0")
        # A hyphen would be read as the start of the release, so it cannot
        # survive into a version.
        self.assertEqual(recipes.tag_version("vita-fix"), "vita_fix")
        self.assertEqual(recipes.tag_version("1.0-rc2"), "1.0_rc2")

    def test_the_version_line_is_respected(self):
        self.assertEqual(recipes.major_of("2.7.r81103.g4f6d059"), "2")
        self.assertNotEqual(recipes.major_of("3.11.0a5"), recipes.major_of("2.7"))



if __name__ == "__main__":
    unittest.main()
