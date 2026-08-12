import base64
import io
import tarfile
import unittest

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
