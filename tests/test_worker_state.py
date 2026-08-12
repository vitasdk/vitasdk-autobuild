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


if __name__ == "__main__":
    unittest.main()
