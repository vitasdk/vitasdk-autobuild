import os
import subprocess
import tempfile
import unittest

from vitasdk_autobuild import state


def git(*arguments, cwd):
    subprocess.run(["git", *arguments], cwd=cwd, check=True,
                   capture_output=True, text=True)


class PackagesCheckoutTest(unittest.TestCase):
    """The recipes have to come from the branch that was asked for.

    vitasdk/packages carries a tag named master, written in 2021 and never
    removed. Git resolves a bare name to it before the branch, so the chain
    spent a night building recipes five years old and reporting them as recipe
    errors.
    """

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.origin = os.path.join(self.directory.name, "origin")
        os.makedirs(self.origin)

        environment = {
            "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.com",
        }
        os.environ.update(environment)
        git("init", "--quiet", "--initial-branch", "master", cwd=self.origin)

        with open(os.path.join(self.origin, "recipe"), "w") as handle:
            handle.write("old\n")
        git("add", "recipe", cwd=self.origin)
        git("commit", "--quiet", "-m", "old", cwd=self.origin)
        git("tag", "master", cwd=self.origin)

        with open(os.path.join(self.origin, "recipe"), "w") as handle:
            handle.write("current\n")
        git("add", "recipe", cwd=self.origin)
        git("commit", "--quiet", "-m", "current", cwd=self.origin)

        cache = os.path.join(self.directory.name, "cache")
        os.makedirs(cache)
        for name, value in (("VITASDK_AUTOBUILD_CACHE", cache),
                            ("PACKAGES_URL", self.origin),
                            ("PACKAGES_BRANCH", "master")):
            self.addCleanup(os.environ.pop, name, None)
            os.environ[name] = value
        os.environ.pop("PACKAGES_DIR", None)

    def read_recipe(self, path):
        with open(os.path.join(path, "recipe")) as handle:
            return handle.read().strip()

    def test_a_tag_of_the_same_name_does_not_win(self):
        self.assertEqual(self.read_recipe(state.packages_checkout()), "current")

    def test_the_second_run_updates_instead_of_keeping_what_it_had(self):
        state.packages_checkout()
        with open(os.path.join(self.origin, "recipe"), "w") as handle:
            handle.write("newer\n")
        git("add", "recipe", cwd=self.origin)
        git("commit", "--quiet", "-m", "newer", cwd=self.origin)
        self.assertEqual(self.read_recipe(state.packages_checkout()), "newer")


if __name__ == "__main__":
    unittest.main()
