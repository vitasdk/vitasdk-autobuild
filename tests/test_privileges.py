import unittest
from unittest import mock

from vitasdk_autobuild import utils


class TestBuildUser(unittest.TestCase):
    """makepkg refuses to run as root, even to print metadata."""

    def test_no_user_when_not_root(self):
        with mock.patch("os.geteuid", return_value=1000):
            self.assertIsNone(utils.get_build_user())

    def test_prefers_the_image_user(self):
        with mock.patch("os.geteuid", return_value=0):
            with mock.patch("pwd.getpwnam") as getpwnam:
                self.assertEqual(utils.get_build_user(), "vita")
        getpwnam.assert_called_with("vita")

    def test_falls_back_when_the_user_does_not_exist(self):
        def only_nobody(name):
            if name != "nobody":
                raise KeyError(name)
            return mock.Mock(pw_dir="/nonexistent")

        with mock.patch("os.geteuid", return_value=0):
            with mock.patch("pwd.getpwnam", side_effect=only_nobody):
                self.assertEqual(utils.get_build_user(), "nobody")


class TestAsBuildUser(unittest.TestCase):

    def test_command_is_untouched_when_not_root(self):
        with mock.patch.object(utils, "get_build_user", return_value=None):
            self.assertEqual(utils.as_build_user(["bash", "x"], {}, ["PATH"]), ["bash", "x"])

    def build(self, environ, keep):
        with mock.patch.object(utils, "get_build_user", return_value="vita"):
            with mock.patch("pwd.getpwnam", return_value=mock.Mock(pw_dir="/home/vita")):
                return utils.as_build_user(["bash", "x"], environ, keep)

    def test_drops_to_the_user(self):
        self.assertEqual(self.build({}, [])[:4], ["sudo", "-u", "vita", "--"])

    def test_passes_the_variables_explicitly(self):
        # sudoers can reset PATH, and makepkg is found through it.
        command = self.build({"PATH": "/usr/bin", "VITASDK": "/opt/vitasdk"},
                             ["PATH", "VITASDK"])
        self.assertIn("PATH=/usr/bin", command)
        self.assertIn("VITASDK=/opt/vitasdk", command)

    def test_home_belongs_to_the_build_user(self):
        # Root's home is not writable by it, and the tools cache there.
        command = self.build({"HOME": "/root"}, ["HOME"])
        self.assertIn("HOME=/home/vita", command)
        self.assertNotIn("HOME=/root", command)

    def test_nothing_else_is_carried_over(self):
        command = self.build({"GITHUB_TOKEN": "secret"}, ["PATH"])
        self.assertNotIn("GITHUB_TOKEN=secret", command)
        self.assertTrue(all("secret" not in part for part in command))

class TestGitOwnership(unittest.TestCase):
    """Root reading a checkout that belongs to the build user."""

    def test_nothing_is_configured_when_not_root(self):
        with mock.patch("os.geteuid", return_value=1000):
            with mock.patch("subprocess.run") as run:
                utils.trust_git_checkouts()
        run.assert_not_called()

    def test_root_marks_checkouts_as_safe(self):
        with mock.patch("os.geteuid", return_value=0):
            with mock.patch("subprocess.run") as run:
                utils.trust_git_checkouts()
        arguments = run.call_args[0][0]
        self.assertEqual(arguments[:3], ["git", "config", "--global"])
        self.assertIn("safe.directory", arguments)

    def test_it_replaces_rather_than_appends(self):
        # Called once per worker start; appending would grow the config.
        with mock.patch("os.geteuid", return_value=0):
            with mock.patch("subprocess.run") as run:
                utils.trust_git_checkouts()
        self.assertIn("--replace-all", run.call_args[0][0])


if __name__ == "__main__":
    unittest.main()
