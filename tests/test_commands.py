import json
import unittest

from vitasdk_autobuild import commands, config, main, queue, report, repository
from vitasdk_autobuild.queue import PackageStatus

from .test_build import make_asset


def apply_status(packages, done, failed, urls=None):
    queue.apply_status(packages, done, failed, urls, [WORLD])
from .test_queue import WORLD, make_package, queue_of


class TestPickPackage(unittest.TestCase):

    def setUp(self):
        self.packages = [make_package(name) for name in ("a", "b", "c", "d", "e")]
        apply_status(self.packages, [], [])

    def test_a_lone_worker_takes_the_first(self):
        self.assertEqual(commands.pick_package(self.packages, WORLD, 0, 1, set()).name, "a")

    def test_the_last_worker_starts_near_the_end(self):
        self.assertEqual(commands.pick_package(self.packages, WORLD, 4, 5, set()).name, "e")

    def test_every_worker_enters_the_queue_somewhere_else(self):
        # The bug this replaces: six workers shared three entry points, so
        # three pairs walked the same queue in the same order and every loser
        # built a package that was already uploaded by the time it finished.
        packages = [make_package(f"p{i}") for i in range(6)]
        apply_status(packages, [], [])
        picked = [commands.pick_package(packages, WORLD, worker, 6, set()).name
                  for worker in range(6)]
        self.assertEqual(len(set(picked)), 6)

    def test_skips_what_this_worker_already_tried(self):
        skip = {("a", "1.0-1")}
        self.assertEqual(commands.pick_package(self.packages, WORLD, 0, 1, skip).name, "b")

    def test_returns_nothing_when_the_queue_is_empty(self):
        for package in self.packages:
            package.set_status(WORLD, PackageStatus.FINISHED)
        self.assertIsNone(commands.pick_package(self.packages, WORLD, 0, 1, set()))

    def test_only_picks_packages_that_are_ready(self):
        zlib = make_package("zlib")
        png = make_package("libpng", depends=["zlib"])
        packages = queue_of(zlib, png)
        apply_status(packages, [], [])
        for _ in range(3):
            self.assertEqual(commands.pick_package(packages, WORLD, 1, 2, set()).name, "zlib")


class TestQueueIsDrained(unittest.TestCase):
    """What decides whether a round of builds ends in a published snapshot."""

    def test_a_queue_with_something_left_is_not_drained(self):
        packages = [make_package("a"), make_package("b")]
        apply_status(packages, [], [])
        self.assertFalse(commands.queue_is_drained(packages))

    def test_everything_built_drains_the_queue(self):
        packages = [make_package("a"), make_package("b")]
        apply_status(packages, ["a-1.0-1-vita.pkg.tar.xz",
                                "b-1.0-1-vita.pkg.tar.xz"], [])
        self.assertTrue(commands.queue_is_drained(packages))

    def test_a_failure_still_drains_the_queue(self):
        # Its dependents can never be built, so waiting for them would mean
        # never publishing again after the first failure.
        zlib = make_package("zlib")
        png = make_package("libpng", depends=["zlib"])
        packages = queue_of(zlib, png)
        apply_status(packages, [], ["zlib-1.0-1-vita.failed"])
        self.assertTrue(commands.queue_is_drained(packages))


class TestSnapshotDispatchInputs(unittest.TestCase):
    """What a supervise round asks snapshot.yml to cut, once the queue drains."""

    def test_the_driving_series_is_forwarded(self):
        inputs = commands.snapshot_dispatch_inputs("2026.08")
        self.assertEqual(inputs["series"], "2026.08")

    def test_the_unnamed_series_is_forwarded_as_itself_not_dropped(self):
        inputs = commands.snapshot_dispatch_inputs("")
        self.assertEqual(inputs["series"], "")

    def test_no_buildscripts_revision_is_invented(self):
        inputs = commands.snapshot_dispatch_inputs("2026.08")
        self.assertEqual(inputs["buildscripts_revision"], "")


class TestImageTag(unittest.TestCase):

    def test_tag_is_derived_from_the_world_core(self):
        self.assertEqual(commands.image_tag(WORLD), WORLD.core)

    def test_tag_is_safe_for_docker(self):
        world = config.World(arch="x", core="refs/heads/weird tag", repository="x")
        self.assertEqual(commands.image_tag(world), "refs-heads-weird-tag")

    def test_every_world_gets_its_own_image(self):
        # A world is chosen by the image a worker runs in, so two worlds must
        # never share one.
        other = config.World(arch="vita-musl", core="musl-core", repository="vita-musl")
        original = config.WORLDS
        config.WORLDS = [WORLD, other]
        try:
            tags = commands.image_tags()
        finally:
            config.WORLDS = original
        self.assertEqual(len(set(tags.values())), 2)


class TestStatusFile(unittest.TestCase):

    def test_status_carries_what_a_catalogue_page_needs(self):
        zlib = make_package("zlib", "1.3.2-2")
        png = make_package("libpng", depends=["zlib"])
        packages = queue_of(zlib, png)
        apply_status(packages, ["zlib-1.3.2-2-vita.pkg.tar.xz"], [])

        status = report.build_status(packages, [], "abc123", [WORLD])
        self.assertEqual(status["worlds"][0]["core"], WORLD.core)
        self.assertEqual(status["packages_revision"], "abc123")
        entry = {p["name"]: p for p in status["packages"]}
        self.assertEqual(entry["zlib"]["builds"][WORLD.arch]["status"], "finished")
        self.assertEqual(entry["zlib"]["rdepends"], ["libpng"])
        self.assertEqual(entry["libpng"]["depends"], ["zlib"])
        self.assertEqual(entry["libpng"]["builds"][WORLD.arch]["status"], "waiting-for-build")

    def test_the_repository_versions_are_attributed_to_a_snapshot(self):
        # "in the repository" names a version; without the tag it never says
        # which repository, and the published snapshots are the only history
        # there is.
        status = report.build_status(
            queue_of(), [], "rev", [WORLD],
            published_tag="packages-snapshot-20260812.1.1",
            snapshot_repo="vitasdk/vitasdk-autobuild",
            published_snapshots=[{"tag": "packages-snapshot-20260812.1.1",
                                  "published_at": "2026-08-12T18:47:27Z",
                                  "core_snapshot": "sdk-snapshot-20260812.565.1"}])
        self.assertEqual(status["published_tag"], "packages-snapshot-20260812.1.1")
        self.assertEqual(status["published_snapshots"][0]["core_snapshot"],
                         "sdk-snapshot-20260812.565.1")
        self.assertEqual(status["snapshot_repo"], "vitasdk/vitasdk-autobuild")

    def test_nothing_published_yet_is_not_an_error(self):
        status = report.build_status(queue_of(), [], "rev", [WORLD])
        self.assertEqual(status["published_tag"], "")
        self.assertEqual(status["published_snapshots"], [])

    def test_a_deprecated_package_says_so(self):
        # Deprecating is not removing: everything that depends on it keeps
        # working, and the point is to say so before somebody starts
        # something new on top of it.
        old = make_package("cpython")
        old.deprecated = "Python 2 is unsupported; use cpython3"
        packages = queue_of(old)
        apply_status(packages, [], [])
        status = report.build_status(packages, [], "rev", [WORLD])
        entry = {p["name"]: p for p in status["packages"]}
        self.assertEqual(entry["cpython"]["deprecated"],
                         "Python 2 is unsupported; use cpython3")
        self.assertEqual(commands.deprecations(packages),
                         {"cpython": "Python 2 is unsupported; use cpython3"})

    def test_a_normal_package_carries_no_notice(self):
        packages = queue_of(make_package("zlib"))
        apply_status(packages, [], [])
        status = report.build_status(packages, [], "rev", [WORLD])
        self.assertEqual(status["packages"][0]["deprecated"], "")
        self.assertEqual(commands.deprecations(packages), {})

    def test_status_is_json_serialisable(self):
        packages = queue_of(make_package("zlib"))
        apply_status(packages, [], [])
        status = report.build_status(packages, [], "rev", [WORLD])
        self.assertIn("zlib", json.dumps(status))

    def test_internal_blocking_details_are_not_published(self):
        zlib = make_package("zlib")
        png = make_package("libpng", depends=["zlib"])
        packages = queue_of(zlib, png)
        apply_status(packages, [], [])
        status = report.build_status(packages, [], "rev", [WORLD])
        for entry in status["packages"]:
            for build in entry["builds"].values():
                self.assertNotIn("blocked", build["details"])

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
        apply_status(packages, ["libpng-1.0-1-vita.pkg.tar.xz"], [])
        self.assertEqual(png.get_status(WORLD), PackageStatus.FINISHED_BUT_BLOCKED)
        self.assertEqual(repository.selectable(packages, WORLD, include_blocked=False), [])

    def test_staging_takes_blocked_packages_too(self):
        zlib = make_package("zlib")
        png = make_package("libpng", depends=["zlib"])
        packages = queue_of(zlib, png)
        apply_status(packages, ["libpng-1.0-1-vita.pkg.tar.xz"], [])
        self.assertEqual(repository.selectable(packages, WORLD, include_blocked=True), [png])

    def test_selects_the_matching_files(self):
        zlib = make_package("zlib", "1.3.2-2")
        apply_status([zlib], ["zlib-1.3.2-2-vita.pkg.tar.xz"], [])
        assets = [make_asset("zlib-1.3.2-2-vita.pkg.tar.xz"),
                  make_asset("zlib-1.3.2-1-vita.pkg.tar.xz"),
                  make_asset("core-snapshot.txt")]
        selected = repository.select_assets([zlib], WORLD, assets)
        self.assertEqual([a.filename for a in selected], ["zlib-1.3.2-2-vita.pkg.tar.xz"])


class TestProvenance(unittest.TestCase):

    def test_provenance_records_the_pinned_core(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            path = repository.write_provenance(directory, "packages-sha", "buildscripts-sha")
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        self.assertEqual(data["core_snapshot"], WORLD.core)
        self.assertEqual(data["worlds"][0]["arch"], WORLD.arch)
        self.assertEqual(data["packages_revision"], "packages-sha")
        self.assertEqual(data["buildscripts_revision"], "buildscripts-sha")
        self.assertEqual(data["schema_version"], 2)


class TestConfigOverrides(unittest.TestCase):

    def test_string_override(self):
        key, value = main.parse_override("PACKAGES_BRANCH=next")
        self.assertEqual((key, value), ("PACKAGES_BRANCH", "next"))

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
        for command in ("show", "build", "update-status", "clean-assets", "clear-failed",
                        "image-tag", "update-recipes"):
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

class TestReadOnlyPaths(unittest.TestCase):
    """A report and a dry run must leave the repository exactly as found."""

    def test_a_missing_release_reads_as_empty(self):
        from vitasdk_autobuild import gh, state

        def missing(create):
            self.assertFalse(create, "a read-only path must not ask for creation")
            raise gh.GitHubError(404, "Not Found")

        self.assertEqual(state.assets_of(missing, create=False), [])

    def test_a_missing_release_is_still_an_error_when_writing(self):
        from vitasdk_autobuild import gh, state

        def missing(create):
            raise gh.GitHubError(404, "Not Found")

        with self.assertRaises(gh.GitHubError):
            state.assets_of(missing, create=True)

    def test_other_errors_are_never_swallowed(self):
        from vitasdk_autobuild import gh, state

        def broken(create):
            raise gh.GitHubError(500, "Boom")

        with self.assertRaises(gh.GitHubError):
            state.assets_of(broken, create=False)

class TestMachineReadableOutput(unittest.TestCase):
    """Commands a workflow reads must print the answer and nothing else."""

    def test_progress_goes_to_stderr(self):
        import contextlib
        import io
        from vitasdk_autobuild.utils import run

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            run(["true"])
        self.assertEqual(out.getvalue(), "")
        self.assertIn("true", err.getvalue())

    def test_image_tag_prints_one_line_per_world(self):
        # Each line is read into a shell variable and written to
        # $GITHUB_OUTPUT, where a malformed line fails the whole job.
        import contextlib
        import io
        import os
        import tempfile
        from unittest import mock
        from vitasdk_autobuild import state

        with tempfile.TemporaryDirectory() as directory:
            with open(os.path.join(directory, "Dockerfile"), "w", encoding="utf-8") as handle:
                handle.write("FROM ubuntu:24.04\n")
            out = io.StringIO()
            with mock.patch.object(state, "packages_checkout", return_value=directory):
                with contextlib.redirect_stdout(out):
                    commands.cmd_image_tag(None)

        lines = out.getvalue().strip().splitlines()
        # Every configured world, not just the running series': a series
        # cannot build until an image holding its own core exists.
        self.assertEqual(len(lines), len(config.WORLDS))
        name, tag = lines[0].split()
        self.assertEqual(name, WORLD.name)
        self.assertTrue(tag.startswith(WORLD.core))

class TestRepositoryGeneration(unittest.TestCase):

    def test_the_container_writes_as_the_calling_user(self):
        # The repository script uses mktemp, whose output only its owner can
        # read. As root that leaves a directory the runner cannot even list.
        import os
        from unittest import mock
        from vitasdk_autobuild import repository

        with mock.patch.object(repository, "run") as run:
            with mock.patch("os.path.exists", return_value=False):
                with mock.patch("os.makedirs"):
                    repository.create_database("/pkgs", "/in", "/tmp/out/repository",
                                               "1", "vita")
        arguments = run.call_args[0][0]
        self.assertIn("--user", arguments)
        self.assertEqual(arguments[arguments.index("--user") + 1],
                         f"{os.getuid()}:{os.getgid()}")

    def test_the_repository_name_reaches_the_script(self):
        from unittest import mock
        from vitasdk_autobuild import config, repository

        with mock.patch.object(repository, "run") as run:
            with mock.patch("os.path.exists", return_value=False):
                with mock.patch("os.makedirs"):
                    repository.create_database("/pkgs", "/in", "/tmp/out/repository",
                                               "1", "vita")
        arguments = run.call_args[0][0]
        self.assertIn("REPOSITORY_NAME=vita", arguments)

class TestWebsiteNotification(unittest.TestCase):
    """Telling the catalogue is an optimisation, never a requirement."""

    def test_without_a_token_nothing_is_sent(self):
        import os
        from unittest import mock
        environ = {k: v for k, v in os.environ.items() if k != "WEBSITE_TOKEN"}
        with mock.patch.dict(os.environ, environ, clear=True):
            with mock.patch.object(commands.gh, "dispatch_repository") as dispatch:
                commands.notify_website()
        dispatch.assert_not_called()

    def test_with_a_token_the_catalogue_is_told(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"WEBSITE_TOKEN": "secret"}):
            with mock.patch.object(commands.gh, "dispatch_repository") as dispatch:
                commands.notify_website()
        repo, event, token = dispatch.call_args[0]
        self.assertEqual(repo, config.WEBSITE_REPO)
        self.assertEqual(event, config.WEBSITE_EVENT)
        self.assertEqual(token, "secret")

    def test_a_failure_to_notify_does_not_fail_the_build(self):
        import os
        from unittest import mock
        from vitasdk_autobuild import gh
        with mock.patch.dict(os.environ, {"WEBSITE_TOKEN": "secret"}):
            with mock.patch.object(commands.gh, "dispatch_repository",
                                   side_effect=gh.GitHubError(404, "Not Found")):
                commands.notify_website()  # must not raise

class TestChannelRequest(unittest.TestCase):
    """A published snapshot asks for a manifest; it never signs one."""

    def test_without_a_token_the_snapshot_is_still_published(self):
        import os
        from unittest import mock
        environ = {k: v for k, v in os.environ.items() if k != "CHANNEL_TOKEN"}
        with mock.patch.dict(os.environ, environ, clear=True):
            with mock.patch.object(commands.gh, "dispatch_repository") as dispatch:
                commands.request_channel_manifest("packages-snapshot-1", "2026.08",
                                                  "vita", "buildscripts-sha")
        dispatch.assert_not_called()

    def test_the_request_names_the_snapshot_and_where_it_lives(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"CHANNEL_TOKEN": "secret",
                                          "GITHUB_REPOSITORY": "vitasdk/vitasdk-autobuild"}):
            with mock.patch.object(commands.gh, "dispatch_repository") as dispatch:
                commands.request_channel_manifest("packages-snapshot-1", "2026.08",
                                                  "vita", "buildscripts-sha")
        repo, event, token = dispatch.call_args[0][:3]
        payload = dispatch.call_args[0][3]
        self.assertEqual(repo, config.CHANNEL_REPO)
        self.assertEqual(event, config.CHANNEL_EVENT)
        self.assertEqual(payload["packages_snapshot"], "packages-snapshot-1")
        self.assertEqual(payload["packages_repository"], "vitasdk/vitasdk-autobuild")
        self.assertEqual(payload["core_snapshot"], config.default_world().core)
        self.assertEqual(payload["channel"], "2026.08")
        self.assertEqual(payload["world"], "vita")
        self.assertEqual(payload["buildscripts_sha"], "buildscripts-sha")

    def test_the_unnamed_series_asks_for_the_nightly_channel(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"CHANNEL_TOKEN": "secret"}):
            with mock.patch.object(commands.gh, "dispatch_repository") as dispatch:
                commands.request_channel_manifest("packages-snapshot-1", "", "vita", "")
        payload = dispatch.call_args[0][3]
        self.assertEqual(payload["channel"], "nightly")

    def test_a_failed_request_does_not_undo_a_published_snapshot(self):
        import os
        from unittest import mock
        from vitasdk_autobuild import gh
        with mock.patch.dict(os.environ, {"CHANNEL_TOKEN": "secret"}):
            with mock.patch.object(commands.gh, "dispatch_repository",
                                   side_effect=gh.GitHubError(404, "Not Found")):
                commands.request_channel_manifest("packages-snapshot-1", "2026.08",
                                                  "vita", "buildscripts-sha")

    def test_a_failed_request_says_how_to_recover_by_hand(self):
        import io
        import contextlib
        import os
        from unittest import mock
        from vitasdk_autobuild import gh
        with mock.patch.dict(os.environ, {"CHANNEL_TOKEN": "secret"}):
            with mock.patch.object(commands.gh, "dispatch_repository",
                                   side_effect=gh.GitHubError(404, "Not Found")):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    commands.request_channel_manifest("packages-snapshot-1", "2026.08",
                                                      "vita", "buildscripts-sha")
        message = out.getvalue()
        self.assertIn("::warning::", message)
        self.assertIn("workflow_dispatch", message)
        self.assertIn("packages-snapshot-1", message)
        self.assertIn("2026.08", message)


if __name__ == "__main__":
    unittest.main()
