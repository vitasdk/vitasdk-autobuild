import unittest
from unittest import mock

from vitasdk_autobuild import build, gh


class TestDependencyReset(unittest.TestCase):
    """A worker builds many packages into the same prefix, one after another."""

    def test_previous_dependencies_are_removed_first(self):
        # openssl and openssl-1.1.1 cannot coexist: whichever an earlier build
        # pulled in makes the next one fail unless the prefix is reset.
        with mock.patch.object(build, "installed_dependencies",
                               return_value=["openssl", "zlib"]):
            with mock.patch.object(build, "pacman") as pacman:
                removed = build.reset_dependencies("/opt/vitasdk")
        self.assertEqual(removed, ["openssl", "zlib"])
        arguments = pacman.call_args[0]
        self.assertIn("--remove", arguments)
        self.assertIn("openssl", arguments)

    def test_nothing_to_remove_touches_nothing(self):
        with mock.patch.object(build, "installed_dependencies", return_value=[]):
            with mock.patch.object(build, "pacman") as pacman:
                self.assertEqual(build.reset_dependencies("/opt/vitasdk"), [])
        pacman.assert_not_called()

    def test_an_absent_database_is_not_an_error(self):
        # The bootstrap archive ships no var/ tree at all.
        self.assertEqual(build.installed_dependencies("/nonexistent/sdk"), [])


class TestUploadRace(unittest.TestCase):
    """Two workers can pick the same package and race to upload it."""

    def make_release(self):
        return gh.Release(id=1, tag="staging", repo="vitasdk/vitasdk-autobuild")

    def test_losing_the_race_is_not_a_failure(self):
        error = gh.GitHubError(422, "Validation Failed (already_exists)")
        with mock.patch.object(gh, "get_token", return_value="x"):
            with mock.patch.object(gh, "get_assets", return_value=[]):
                with mock.patch.object(gh, "_request", side_effect=error):
                    gh.upload_asset(self.make_release(), "zlib-1.0-1-vita.pkg.tar.xz",
                                    content=b"x")

    def test_other_validation_errors_still_raise(self):
        error = gh.GitHubError(422, "Validation Failed (too_large)")
        with mock.patch.object(gh, "get_token", return_value="x"):
            with mock.patch.object(gh, "get_assets", return_value=[]):
                with mock.patch.object(gh, "_request", side_effect=error):
                    with self.assertRaises(gh.GitHubError):
                        gh.upload_asset(self.make_release(), "zlib-1.0-1-vita.pkg.tar.xz",
                                        content=b"x")

    def test_validation_codes_reach_the_message(self):
        # Without the code, every rejected upload reads 'Validation Failed'.
        import json
        import urllib.error
        import io

        body = json.dumps({"message": "Validation Failed",
                           "errors": [{"code": "already_exists"}]}).encode()
        error = urllib.error.HTTPError("https://x", 422, "Validation Failed", {},
                                       io.BytesIO(body))
        with mock.patch.object(gh, "get_token", return_value=""):
            with mock.patch.object(gh, "_opener") as opener:
                opener.open.side_effect = error
                with self.assertRaises(gh.GitHubError) as caught:
                    gh._request("POST", "https://x")
        self.assertIn("already_exists", caught.exception.message)

class TestRootlessPacman(unittest.TestCase):
    """The prefix belongs to the build user, so the client runs as it."""

    def test_pacman_is_dropped_to_the_build_user(self):
        import subprocess as sp
        ok = sp.CompletedProcess([], 0, "", "")
        with mock.patch.object(build, "as_build_user", side_effect=lambda c, e, k: ["sudo", *c]) as drop:
            with mock.patch("subprocess.run", return_value=ok) as run:
                build.pacman("/opt/vitasdk", "--query")
        self.assertTrue(run.call_args[0][0][0] == "sudo")
        passed = drop.call_args[0][2]
        self.assertIn("VITASDK", passed)

    def test_every_path_stays_inside_the_prefix(self):
        # The self contained prefix contract: nothing is written outside it.
        import subprocess as sp
        ok = sp.CompletedProcess([], 0, "", "")
        with mock.patch.object(build, "as_build_user", side_effect=lambda c, e, k: list(c)):
            with mock.patch("subprocess.run", return_value=ok) as run:
                build.pacman("/opt/vitasdk", "--query")
        arguments = run.call_args[0][0]
        for flag in ("--root", "--dbpath", "--cachedir", "--logfile", "--config"):
            value = arguments[arguments.index(flag) + 1]
            self.assertTrue(value.startswith("/opt/vitasdk"), f"{flag} -> {value}")

class TestPrefixOwnership(unittest.TestCase):
    """makepkg reads and creates the package database to write .BUILDINFO."""

    def test_the_state_tree_is_handed_to_the_build_user(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as sdk:
            with mock.patch.object(build, "give_to_build_user") as handover:
                build.prepare_prefix(sdk)
            self.assertTrue(os.path.isdir(os.path.join(sdk, "var", "lib", "pacman")))
            self.assertTrue(os.path.isdir(os.path.join(sdk, "var", "cache", "pacman", "pkg")))
            handover.assert_called_once_with(os.path.join(sdk, "var"))

    def test_a_package_without_dependencies_still_gets_the_tree(self):
        # It needs the database as much as any other: nothing to install is
        # not the same as nothing to prepare.
        with mock.patch.object(build, "prepare_prefix") as prepare:
            with mock.patch.object(build, "reset_dependencies"):
                build.install_dependencies([], "/opt/vitasdk")
        prepare.assert_called_once()


class TestPacmanDiagnostics(unittest.TestCase):

    def test_a_failure_prints_what_the_client_said(self):
        import contextlib
        import io
        import subprocess as sp

        completed = sp.CompletedProcess([], 1, "", "error: could not create database")
        out = io.StringIO()
        with mock.patch.object(build, "as_build_user", side_effect=lambda c, e, k: list(c)):
            with mock.patch("subprocess.run", return_value=completed):
                with contextlib.redirect_stdout(out):
                    with self.assertRaises(sp.CalledProcessError):
                        build.pacman("/opt/vitasdk", "--query")
        self.assertIn("could not create database", out.getvalue())

    def test_check_false_reports_without_raising(self):
        import contextlib
        import io
        import subprocess as sp

        completed = sp.CompletedProcess([], 1, "", "boom")
        with mock.patch.object(build, "as_build_user", side_effect=lambda c, e, k: list(c)):
            with mock.patch("subprocess.run", return_value=completed):
                with contextlib.redirect_stdout(io.StringIO()):
                    result = build.pacman("/opt/vitasdk", "--query", check=False)
        self.assertEqual(result.returncode, 1)


if __name__ == "__main__":
    unittest.main()
