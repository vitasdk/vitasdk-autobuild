"""Which open proposals get built, and once each."""

import unittest

from vitasdk_autobuild import proposals


def pull(number=1, fork=False, draft=False, labels=(), sha="abc"):
    base = {"full_name": "vitasdk/packages"}
    head = {"full_name": "somebody/packages"} if fork else dict(base)
    return {"number": number, "draft": draft,
            "labels": [{"name": name} for name in labels],
            "head": {"repo": head, "sha": sha}, "base": {"repo": base}}


class TestWhichRecipesAProposalTouches(unittest.TestCase):

    def test_a_recipe_is_its_directory(self):
        self.assertEqual(proposals.recipes_touched(["zlib/VITABUILD"]), ["zlib"])

    def test_a_file_beside_the_recipe_counts_as_it(self):
        # A proposal that only changes a patch is still that recipe's.
        self.assertEqual(proposals.recipes_touched(["sdl2_vitagl/backend.patch"]),
                         ["sdl2_vitagl"])

    def test_each_recipe_once(self):
        self.assertEqual(
            proposals.recipes_touched(["zlib/VITABUILD", "zlib/fix.diff"]), ["zlib"])

    def test_the_directories_that_are_not_recipes_are_not_recipes(self):
        self.assertEqual(
            proposals.recipes_touched([".github/workflows/x.yml", "scripts/a.sh",
                                       "tests/t.sh"]), [])

    def test_a_file_at_the_top_belongs_to_no_recipe(self):
        self.assertEqual(proposals.recipes_touched(["Dockerfile", "build.sh"]), [])


class TestWhichProposalsAreBuilt(unittest.TestCase):

    def test_a_branch_in_the_repository_is_built(self):
        self.assertEqual(proposals.reason_to_skip(pull()), "")

    def test_a_fork_needs_a_maintainer_to_ask(self):
        # A recipe is a build script. Running one from a fork on the word of
        # whoever opened the proposal would hand this builder to anybody.
        self.assertIn("fork", proposals.reason_to_skip(pull(fork=True)))

    def test_a_fork_a_maintainer_asked_for_is_built(self):
        self.assertEqual(
            proposals.reason_to_skip(pull(fork=True, labels=[proposals.TRY_LABEL])), "")

    def test_a_draft_is_not_built(self):
        self.assertIn("draft", proposals.reason_to_skip(pull(draft=True)))


class TestNotTryingTheSameThingTwice(unittest.TestCase):

    def test_a_commit_with_an_answer_is_not_tried_again(self):
        statuses = [{"context": proposals.status_context("zlib"), "state": "success"}]
        self.assertTrue(proposals.already_tried(statuses, "zlib"))

    def test_a_failure_is_an_answer_too(self):
        statuses = [{"context": proposals.status_context("zlib"), "state": "failure"}]
        self.assertTrue(proposals.already_tried(statuses, "zlib"))

    def test_another_package_answer_does_not_count(self):
        # A proposal touching two recipes needs an answer for each, which is
        # why the package is in the context and not only in the description.
        statuses = [{"context": proposals.status_context("zlib"), "state": "success"}]
        self.assertFalse(proposals.already_tried(statuses, "libpng"))

    def test_a_commit_with_nothing_on_it_is_tried(self):
        self.assertFalse(proposals.already_tried([], "zlib"))


class TestWhereAProposalIsRead(unittest.TestCase):

    def test_the_pull_ref_is_used_for_every_proposal(self):
        # A fork's branch is not in the recipe repository; its pull ref is,
        # and it reads the same for a branch that is.
        self.assertEqual(proposals.pull_ref(pull(number=42)), "refs/pull/42/head")


if __name__ == "__main__":
    unittest.main()
