import base64
import http.client
import io
import json
import tarfile
import unittest
from unittest import mock

from vitasdk_autobuild import config, gh, repodb


class TestAssetNames(unittest.TestCase):

    def test_package_names_stay_readable(self):
        name, label = gh.asset_upload_name("zlib-1.3.2-2-vita.pkg.tar.xz")
        self.assertEqual(name, "zlib-1.3.2-2-vita.pkg.tar.xz")
        self.assertEqual(label, "zlib-1.3.2-2-vita.pkg.tar.xz")

    def test_readable_names_keep_the_release_usable_as_a_pacman_repository(self):
        # A pacman database points at file names; if GitHub rewrote them the
        # repository would 404 on every package.
        for name in ("libc++-1.0-1-vita.pkg.tar.xz", "vita.db", "SHA256SUMS",
                     "zlib-1.3.2-2.failed"):
            self.assertEqual(gh.asset_upload_name(name)[0], name)

    def test_names_github_would_rewrite_are_encoded(self):
        original = "zlib-1:1.3.2-2-vita.pkg.tar.xz"
        name, label = gh.asset_upload_name(original)
        self.assertNotEqual(name, original)
        self.assertTrue(name.endswith(".bin"))
        self.assertEqual(label, original)
        padded = name[:-4] + "=" * (-len(name[:-4]) % 4)
        self.assertEqual(base64.urlsafe_b64decode(padded).decode(), original)

    def test_empty_name_is_rejected(self):
        with self.assertRaises(ValueError):
            gh.asset_upload_name("")

    def test_filename_prefers_the_label(self):
        asset = gh.Asset(id=1, name="encoded.bin", label="real-name.pkg.tar.xz", size=1,
                         state="uploaded", uploader="github-actions[bot]",
                         uploader_type="Bot", url="", download_url="")
        self.assertEqual(asset.filename, "real-name.pkg.tar.xz")

    def test_filename_falls_back_to_the_name(self):
        asset = gh.Asset(id=1, name="thing.txt", label="", size=1, state="uploaded",
                         uploader="github-actions[bot]", uploader_type="Bot",
                         url="", download_url="")
        self.assertEqual(asset.filename, "thing.txt")


class TestAssetState(unittest.TestCase):

    def test_only_uploaded_assets_count(self):
        def asset(state):
            return gh.Asset(id=1, name="a", label="a", size=1, state=state,
                            uploader="github-actions[bot]", uploader_type="Bot",
                            url="", download_url="")
        self.assertTrue(asset("uploaded").complete)
        self.assertFalse(asset("starter").complete)
        self.assertFalse(asset("open").complete)


class TestTrustedUploader(unittest.TestCase):

    def make(self, login, type_):
        return gh.Asset(id=1, name="a", label="a", size=1, state="uploaded",
                        uploader=login, uploader_type=type_, url="", download_url="")

    def test_actions_bot_is_trusted(self):
        self.assertTrue(gh._is_trusted_uploader(self.make("github-actions[bot]", "Bot")))

    def test_a_random_user_is_not(self):
        self.assertFalse(gh._is_trusted_uploader(self.make("someone", "User")))

    def test_configured_users_are_trusted(self):
        original = config.ALLOWED_UPLOADERS
        config.ALLOWED_UPLOADERS = ["frangarcj"]
        try:
            self.assertTrue(gh._is_trusted_uploader(self.make("frangarcj", "User")))
        finally:
            config.ALLOWED_UPLOADERS = original


class TestPagination(unittest.TestCase):

    def test_finds_the_next_page(self):
        headers = {"Link": '<https://api/x?page=2>; rel="next", <https://api/x?page=9>; rel="last"'}
        self.assertEqual(gh._next_link(headers), "https://api/x?page=2")

    def test_last_page_has_no_next(self):
        self.assertEqual(gh._next_link({"Link": '<https://api/x?page=1>; rel="prev"'}), "")

    def test_missing_header(self):
        self.assertEqual(gh._next_link({}), "")


class TestTimestamps(unittest.TestCase):

    def test_parses_utc_regardless_of_local_time(self):
        self.assertEqual(gh.parse_timestamp("1970-01-01T00:00:10Z"), 10.0)

    def test_empty_timestamp(self):
        self.assertEqual(gh.parse_timestamp(""), 0.0)

    def test_ordering_is_preserved(self):
        self.assertLess(gh.parse_timestamp("2026-08-12T10:00:00Z"),
                        gh.parse_timestamp("2026-08-12T10:00:01Z"))


class TestRepositoryDatabase(unittest.TestCase):

    def make_database(self, entries):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            for name, version in entries:
                desc = (f"%FILENAME%\n{name}-{version}-vita.pkg.tar.xz\n\n"
                        f"%NAME%\n{name}\n\n%VERSION%\n{version}\n\n").encode()
                info = tarfile.TarInfo(f"{name}-{version}/desc")
                info.size = len(desc)
                archive.addfile(info, io.BytesIO(desc))
        return buffer.getvalue()

    def test_reads_names_and_versions(self):
        data = self.make_database([("zlib", "1.3.2-2"), ("libpng", "1.6.40-1")])
        self.assertEqual(repodb.parse_database(data),
                         {"zlib": "1.3.2-2", "libpng": "1.6.40-1"})

    def test_empty_database(self):
        self.assertEqual(repodb.parse_database(self.make_database([])), {})

    def test_ignores_entries_without_a_version(self):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            desc = b"%NAME%\nbroken\n"
            info = tarfile.TarInfo("broken-1/desc")
            info.size = len(desc)
            archive.addfile(info, io.BytesIO(desc))
        self.assertEqual(repodb.parse_database(buffer.getvalue()), {})


if __name__ == "__main__":
    unittest.main()



class _Response:

    def __init__(self, body=b"{}", error=None):
        self.status = 200
        self.headers = {}
        self._body = body
        self._error = error

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        if self._error:
            raise self._error
        return self._body


class TestCutResponses(unittest.TestCase):
    """A body cut short is a network failure, and a GET is retried."""

    def test_a_cut_body_is_retried(self):
        cut = _Response(error=http.client.IncompleteRead(b"", 20119254))
        with mock.patch.object(gh, "get_token", return_value=""), \
                mock.patch.object(gh.time, "sleep"), \
                mock.patch.object(gh, "_opener") as opener:
            opener.open.side_effect = [cut, _Response(b'{"ok": true}')]
            _, _, body = gh._request("GET", "https://x")
        self.assertEqual(json.loads(body), {"ok": True})
        self.assertEqual(opener.open.call_count, 2)

    def test_a_body_that_never_arrives_is_a_github_error(self):
        cut = _Response(error=http.client.IncompleteRead(b"", 1))
        with mock.patch.object(gh, "get_token", return_value=""), \
                mock.patch.object(gh.time, "sleep"), \
                mock.patch.object(gh, "_opener") as opener:
            opener.open.return_value = cut
            with self.assertRaises(gh.GitHubError):
                gh._request("GET", "https://x", retries=2)


def _page(nodes, cursor=None):
    return {"data": {"repository": {"releases": {
        "nodes": nodes,
        "pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor},
    }}}}


def _node(tag, draft=False, published="2026-11-22T19:34:31Z"):
    return {"tagName": tag, "isDraft": draft, "publishedAt": published,
            "createdAt": "2026-11-22T19:00:00Z"}


class TestReleaseListing(unittest.TestCase):
    """Tags come from a listing that does not drag every asset along."""

    def test_asks_graphql_when_there_is_a_token(self):
        pages = [_page([_node("packages-snapshot-2"), _node("other-1")], cursor="c1"),
                 _page([_node("packages-snapshot-1"), _node("packages-snapshot-3", draft=True)])]
        with mock.patch.object(gh, "get_token", return_value="t"), \
                mock.patch.object(gh, "api", side_effect=pages) as api, \
                mock.patch.object(gh, "api_list") as rest:
            tags = gh.find_releases("vitasdk/x", "packages-snapshot-")
        self.assertEqual(tags, ["packages-snapshot-2", "packages-snapshot-1"])
        rest.assert_not_called()
        self.assertEqual(api.call_count, 2)
        first, second = (call.args[2]["variables"] for call in api.call_args_list)
        self.assertEqual((first["owner"], first["name"], first["cursor"]), ("vitasdk", "x", None))
        self.assertEqual(second["cursor"], "c1")
        self.assertTrue(api.call_args_list[0].args[1].endswith("/graphql"))

    def test_dates_come_with_the_same_listing(self):
        pages = [_page([_node("s-1", published="2026-11-01T00:00:00Z"),
                        _node("s-2", published=None)])]
        with mock.patch.object(gh, "get_token", return_value="t"), \
                mock.patch.object(gh, "api", side_effect=pages):
            found = gh.find_releases_with_dates("vitasdk/x", "s-")
        self.assertEqual(found, [("s-2", "2026-11-22T19:00:00Z"), ("s-1", "2026-11-01T00:00:00Z")])

    def test_falls_back_to_rest_without_a_token(self):
        listing = [{"tag_name": "s-1", "draft": False}, {"tag_name": "s-2", "draft": True}]
        with mock.patch.object(gh, "get_token", return_value=""), \
                mock.patch.object(gh, "api") as api, \
                mock.patch.object(gh, "api_list", return_value=listing) as rest:
            tags = gh.find_releases("vitasdk/x", "s-")
        self.assertEqual(tags, ["s-1"])
        api.assert_not_called()
        rest.assert_called_once_with("/repos/vitasdk/x/releases")

    def test_a_graphql_error_is_not_an_empty_listing(self):
        # An empty answer read as "nothing published" would rebuild the world.
        answer = {"data": {"repository": None}, "errors": [{"message": "Could not resolve"}]}
        with mock.patch.object(gh, "get_token", return_value="t"), \
                mock.patch.object(gh, "api", return_value=answer):
            with self.assertRaises(gh.GitHubError) as caught:
                gh.find_releases("vitasdk/x", "s-")
        self.assertIn("Could not resolve", caught.exception.message)
