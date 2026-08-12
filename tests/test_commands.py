import json
import unittest

from vitasdk_autobuild import commands, config, main, queue, report, repository
from vitasdk_autobuild.queue import PackageStatus

from .test_build import make_asset
from .test_queue import make_package, queue_of


class TestPickPackage(unittest.TestCase):

    def setUp(self):
        self.packages = [make_package(name) for name in ("a", "b", "c", "d", "e")]
        queue.apply_status(self.packages, [], [])

    def test_start_takes_the_first(self):
        self.assertEqual(commands.pick_package(self.packages, "start", set()).name, "a")

    def test_end_takes_the_last(self):
        self.assertEqual(commands.pick_package(self.packages, "end", set()).name, "e")

    def test_middle_takes_the_middle(self):
        self.assertEqual(commands.pick_package(self.packages, "middle", set()).name, "c")

    def test_skips_what_this_worker_already_tried(self):
        skip = {("a", "1.0-1")}
        self.assertEqual(commands.pick_package(self.packages, "start", skip).name, "b")

    def test_returns_nothing_when_the_queue_is_empty(self):
        for package in self.packages:
            package.set_status(PackageStatus.FINISHED)
        self.assertIsNone(commands.pick_package(self.packages, "start", set()))

    def test_only_picks_packages_that_are_ready(self):
        zlib = make_package("zlib")
        png = make_package("libpng", depends=["zlib"])
        packages = queue_of(zlib, png)
        queue.apply_status(packages, [], [])
        for _ in range(3):
            self.assertEqual(commands.pick_package(packages, "end", set()).name, "zlib")


class TestImageTag(unittest.TestCase):

    def test_tag_is_derived_from_the_core_snapshot(self):
        self.assertEqual(commands.image_tag(), config.CORE_SNAPSHOT)

    def test_tag_is_safe_for_docker(self):
        original = config.CORE_SNAPSHOT
        config.CORE_SNAPSHOT = "refs/heads/weird tag"
        try:
            self.assertEqual(commands.image_tag(), "refs-heads-weird-tag")
        finally:
            config.CORE_SNAPSHOT = original


class TestStatusFile(unittest.TestCase):

    def test_status_carries_what_a_catalogue_page_needs(self):
        zlib = make_package("zlib", "1.3.2-2")
        png = make_package("libpng", depends=["zlib"])
        packages = queue_of(zlib, png)
        queue.apply_status(packages, ["zlib-1.3.2-2-vita.pkg.tar.xz"], [])

        status = report.build_status(packages, [], "sdk-snapshot-1", "abc123")
        self.assertEqual(status["core_snapshot"], "sdk-snapshot-1")
        self.assertEqual(status["packages_revision"], "abc123")
        entry = {p["name"]: p for p in status["packages"]}
        self.assertEqual(entry["zlib"]["status"], "finished")
        self.assertEqual(entry["zlib"]["rdepends"], ["libpng"])
        self.assertEqual(entry["libpng"]["depends"], ["zlib"])
        self.assertEqual(entry["libpng"]["status"], "waiting-for-build")

    def test_status_is_json_serialisable(self):
        packages = queue_of(make_package("zlib"))
        queue.apply_status(packages, [], [])
        status = report.build_status(packages, [], "core", "rev")
        self.assertIn("zlib", json.dumps(status))

    def test_internal_blocking_details_are_not_published(self):
        zlib = make_package("zlib")
        png = make_package("libpng", depends=["zlib"])
        packages = queue_of(zlib, png)
        queue.apply_status(packages, [], [])
        status = report.build_status(packages, [], "core", "rev")
        for entry in status["packages"]:
            self.assertNotIn("blocked", entry["details"])

    def test_running_jobs_are_filtered_and_sorted(self):
        jobs = report.running_jobs([
            {"status": "completed", "name": "old", "html_url": "u0", "started_at": "0"},
            {"status": "in_progress", "name": "b", "html_url": "u2", "started_at": "2"},
            {"status": "in_progress", "name": "a", "html_url": "u1", "started_at": "1"},
        ])
        self.assertEqual([job["name"] for job in jobs], ["a", "b"])


class TestRepositorySelection(unittest.TestCase):

    def test_snapshot_takes_only_clean_packages(self):
        zlib = make_package("zlib")
        png = make_package("libpng", depends=["zlib"])
        packages = queue_of(zlib, png)
        queue.apply_status(packages, ["libpng-1.0-1-vita.pkg.tar.xz"], [])
        self.assertEqual(png.status, PackageStatus.FINISHED_BUT_BLOCKED)
        self.assertEqual(repository.selectable(packages, include_blocked=False), [])

    def test_staging_takes_blocked_packages_too(self):
        zlib = make_package("zlib")
        png = make_package("libpng", depends=["zlib"])
        packages = queue_of(zlib, png)
        queue.apply_status(packages, ["libpng-1.0-1-vita.pkg.tar.xz"], [])
        self.assertEqual(repository.selectable(packages, include_blocked=True), [png])

    def test_selects_the_matching_files(self):
        zlib = make_package("zlib", "1.3.2-2")
        queue.apply_status([zlib], ["zlib-1.3.2-2-vita.pkg.tar.xz"], [])
        assets = [make_asset("zlib-1.3.2-2-vita.pkg.tar.xz"),
                  make_asset("zlib-1.3.2-1-vita.pkg.tar.xz"),
                  make_asset("core-snapshot.txt")]
        selected = repository.select_assets([zlib], assets)
        self.assertEqual([a.filename for a in selected], ["zlib-1.3.2-2-vita.pkg.tar.xz"])


class TestProvenance(unittest.TestCase):

    def test_provenance_records_the_pinned_core(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            path = repository.write_provenance(directory, "packages-sha", "buildscripts-sha")
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        self.assertEqual(data["core_snapshot"], config.CORE_SNAPSHOT)
        self.assertEqual(data["packages_revision"], "packages-sha")
        self.assertEqual(data["buildscripts_revision"], "buildscripts-sha")
        self.assertEqual(data["schema_version"], 1)


class TestConfigOverrides(unittest.TestCase):

    def test_string_override(self):
        key, value = main.parse_override("CORE_SNAPSHOT=sdk-snapshot-9")
        self.assertEqual((key, value), ("CORE_SNAPSHOT", "sdk-snapshot-9"))

    def test_integer_override(self):
        self.assertEqual(main.parse_override("MAXIMUM_JOB_COUNT=3"), ("MAXIMUM_JOB_COUNT", 3))

    def test_boolean_override(self):
        self.assertEqual(main.parse_override("REBUILD_DEPENDENTS=false"),
                         ("REBUILD_DEPENDENTS", False))

    def test_list_override(self):
        self.assertEqual(main.parse_override("MANUAL_BUILD=a,b"), ("MANUAL_BUILD", ["a", "b"]))

    def test_unknown_key_is_rejected(self):
        with self.assertRaises(SystemExit):
            config.apply_overrides({"NOT_A_SETTING": "x"})

    def test_override_applies(self):
        original = config.MAXIMUM_JOB_COUNT
        try:
            config.apply_overrides({"MAXIMUM_JOB_COUNT": 2})
            self.assertEqual(config.MAXIMUM_JOB_COUNT, 2)
        finally:
            config.MAXIMUM_JOB_COUNT = original


class TestCommandLine(unittest.TestCase):

    def test_every_command_is_reachable(self):
        parser = main.build_parser()
        for command in ("show", "build", "update-status", "clean-assets", "clear-failed"):
            self.assertTrue(callable(parser.parse_args([command]).func))

    def test_supervise_requires_a_branch(self):
        import contextlib
        import io
        parser = main.build_parser()
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(["supervise"])

    def test_snapshot_defaults_to_publishing_a_release(self):
        args = main.build_parser().parse_args(["snapshot"])
        self.assertFalse(args.staging)
        self.assertFalse(args.no_publish)


if __name__ == "__main__":
    unittest.main()
