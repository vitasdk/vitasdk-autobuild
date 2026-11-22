import json
import os
import tempfile
import types
import unittest
from unittest import mock

from vitasdk_autobuild import commands, config, gh, queue, repodb, repository
from vitasdk_autobuild.queue import PackageStatus

from .test_build import make_asset
from .test_queue import OTHER, WORLD, make_package, queue_of


def published(package, version, world=WORLD):
    package.repo_versions[world.arch] = version
    return package


def november():
    """zlib moved, its dependents were rebuilt, and one of them failed."""

    zlib = published(make_package("zlib", "1.4-1"), "1.3.2-2")
    png = published(make_package("libpng", "1.6.59-1", depends=["zlib"]), "1.6.58-1")
    cpython = published(make_package("cpython", "2.7-2", depends=["zlib"]), "2.7-2")
    mini18n = published(make_package("mini18n", "0.2.1-1"), "0.2.1-1")
    packages = queue_of(zlib, png, cpython, mini18n)
    queue.apply_status(packages,
                       ["zlib-1.4-1-vita.pkg.tar.xz", "libpng-1.6.59-1-vita.pkg.tar.xz",
                        "mini18n-0.2.1-1-vita.pkg.tar.xz"],
                       ["cpython-2.7-2-vita.failed"], None, [WORLD])
    return packages


class TestKeptFromPublished(unittest.TestCase):
    """A group that is held back stays at what was published, not at nothing."""

    def test_the_snapshot_alone_would_lose_the_group(self):
        packages = november()
        self.assertEqual([p.name for p in repository.selectable(packages, WORLD, False)],
                         ["mini18n"])

    def test_the_held_group_is_kept(self):
        packages = november()
        kept = repository.kept_from_published(packages, WORLD, WORLD.core)
        self.assertEqual([p.name for p in kept], ["zlib", "libpng", "cpython"])

    def test_nothing_is_kept_across_a_core_change(self):
        self.assertEqual(repository.kept_from_published(november(), WORLD, "another-core"), [])

    def test_nothing_is_kept_when_the_published_core_is_unknown(self):
        self.assertEqual(repository.kept_from_published(november(), WORLD, ""), [])

    def test_a_package_never_published_has_nothing_to_keep(self):
        packages = november()
        fresh = make_package("fresh", depends=["zlib"])
        packages = queue_of(*packages, fresh)
        queue.apply_status(packages,
                           ["zlib-1.4-1-vita.pkg.tar.xz", "libpng-1.6.59-1-vita.pkg.tar.xz",
                            "mini18n-0.2.1-1-vita.pkg.tar.xz"],
                           ["cpython-2.7-2-vita.failed"], None, [WORLD])
        kept = repository.kept_from_published(packages, WORLD, WORLD.core)
        self.assertEqual([p.name for p in kept], ["zlib", "libpng", "cpython"])

    def test_an_old_build_is_not_kept_beside_a_dependency_that_moved_on(self):
        lib = published(make_package("lib", "2.0-1"), "1.0-1")
        app = published(make_package("app", "1.0-2", depends=["lib"]), "1.0-1")
        top = published(make_package("top", "1.0-2", depends=["app"]), "1.0-1")
        packages = queue_of(lib, app, top)
        lib.set_status(WORLD, PackageStatus.FINISHED)
        app.set_status(WORLD, PackageStatus.FAILED_TO_BUILD)
        top.set_status(WORLD, PackageStatus.WAITING_FOR_DEPENDENCIES)
        # top goes with app: what it would sit beside is not published either.
        self.assertEqual(repository.kept_from_published(packages, WORLD, WORLD.core), [])

    def test_it_is_kept_beside_a_dependency_that_did_not_move(self):
        lib = published(make_package("lib", "1.0-1"), "1.0-1")
        app = published(make_package("app", "1.0-2", depends=["lib"]), "1.0-1")
        packages = queue_of(lib, app)
        lib.set_status(WORLD, PackageStatus.FINISHED)
        app.set_status(WORLD, PackageStatus.FAILED_TO_BUILD)
        self.assertEqual(repository.kept_from_published(packages, WORLD, WORLD.core), [app])

    def test_each_world_keeps_its_own(self):
        zlib = make_package("zlib", "1.4-1", worlds=[WORLD, OTHER])
        published(zlib, "1.3.2-2", OTHER)
        zlib.set_status(WORLD, PackageStatus.FINISHED)
        zlib.set_status(OTHER, PackageStatus.FAILED_TO_BUILD)
        self.assertEqual(repository.kept_from_published([zlib], WORLD, WORLD.core), [])
        self.assertEqual(repository.kept_from_published([zlib], OTHER, OTHER.core), [zlib])


class TestSelectPublishedAssets(unittest.TestCase):

    def test_takes_the_published_version_not_the_staged_one(self):
        zlib = published(make_package("zlib", "1.4-1"), "1.3.2-2")
        assets = [make_asset("zlib-1.3.2-2-vita.pkg.tar.xz"),
                  make_asset("zlib-1.4-1-vita.pkg.tar.xz"),
                  make_asset("zlib-1.3.2-2-vita_softfp.pkg.tar.xz"),
                  make_asset("vita.db")]
        selected = repository.select_published_assets([zlib], WORLD, assets)
        self.assertEqual([a.filename for a in selected], ["zlib-1.3.2-2-vita.pkg.tar.xz"])

    def test_takes_every_binary_of_a_split_package(self):
        sdl = published(make_package("sdl", "2.0-2", binaries=["sdl", "sdl-docs"]), "2.0-1")
        assets = [make_asset("sdl-2.0-1-vita.pkg.tar.xz"),
                  make_asset("sdl-docs-2.0-1-vita.pkg.tar.xz"),
                  make_asset("sdl-2.0-2-vita.pkg.tar.xz")]
        selected = repository.select_published_assets([sdl], WORLD, assets)
        self.assertEqual(sorted(a.filename for a in selected),
                         ["sdl-2.0-1-vita.pkg.tar.xz", "sdl-docs-2.0-1-vita.pkg.tar.xz"])


class TestPublicationIsBehind(unittest.TestCase):
    """What is finished and what is published can drift apart without a build."""

    def test_a_finished_package_the_snapshot_lacks_is_behind(self):
        zlib = make_package("zlib", "1.4-1")
        zlib.set_status(WORLD, PackageStatus.FINISHED)
        self.assertEqual(repository.publication_is_behind([zlib], [WORLD]), ["zlib (vita)"])

    def test_a_finished_package_published_at_an_older_version_is_behind(self):
        zlib = published(make_package("zlib", "1.4-1"), "1.3.2-2")
        zlib.set_status(WORLD, PackageStatus.FINISHED)
        self.assertEqual(repository.publication_is_behind([zlib], [WORLD]), ["zlib (vita)"])

    def test_what_is_published_as_it_is_built_is_not_behind(self):
        zlib = published(make_package("zlib", "1.4-1"), "1.4-1")
        zlib.set_status(WORLD, PackageStatus.FINISHED)
        self.assertEqual(repository.publication_is_behind([zlib], [WORLD]), [])

    def test_a_held_back_package_is_not_behind(self):
        # Asking again would cut the same snapshot every three hours.
        for status in (PackageStatus.FINISHED_BUT_BLOCKED, PackageStatus.FAILED_TO_BUILD,
                       PackageStatus.WAITING_FOR_DEPENDENCIES):
            zlib = published(make_package("zlib", "1.4-1"), "1.3.2-2")
            zlib.set_status(WORLD, status)
            self.assertEqual(repository.publication_is_behind([zlib], [WORLD]), [])


class TestPublishedCores(unittest.TestCase):

    def cores_of(self, provenance):
        with tempfile.TemporaryDirectory() as cache:
            def download(asset, path):
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(provenance)
            assets = [] if provenance is None else [make_asset("provenance.json")]
            with mock.patch.object(repodb, "_cache_dir", return_value=cache), \
                    mock.patch.object(gh, "get_snapshot_repo", return_value="vitasdk/x"), \
                    mock.patch.object(gh, "get_release"), \
                    mock.patch.object(gh, "get_assets", return_value=assets), \
                    mock.patch.object(gh, "download_asset", side_effect=download):
                return repodb.get_published_cores("packages-snapshot-1")

    def test_reads_the_core_of_every_world(self):
        provenance = json.dumps({"schema_version": 2, "core_snapshot": "a", "worlds": [
            {"arch": "vita", "core": "a"}, {"arch": "vita_softfp", "core": "b"}]})
        self.assertEqual(self.cores_of(provenance), {"vita": "a", "vita_softfp": "b"})

    def test_a_snapshot_from_before_worlds_names_the_default_one(self):
        self.assertEqual(self.cores_of(json.dumps({"core_snapshot": "a"})),
                         {config.default_world().arch: "a"})

    def test_no_provenance_is_no_answer(self):
        self.assertEqual(self.cores_of(None), {})
        self.assertEqual(self.cores_of("not json"), {})

    def test_no_snapshot_is_no_answer(self):
        self.assertEqual(repodb.get_published_cores(""), {})


class TestProvenanceRecordsWhatWasKept(unittest.TestCase):

    def test_kept_packages_are_named_per_world(self):
        with tempfile.TemporaryDirectory() as directory:
            path = repository.write_provenance(directory, "sha", kept={"vita": ["zlib"]})
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        self.assertEqual(data["kept_from_previous"], {"vita": ["zlib"]})

    def test_nothing_kept_is_recorded_as_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = repository.write_provenance(directory, "sha")
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        self.assertEqual(data["kept_from_previous"], {})


class TestSuperviseAfterARecovery(unittest.TestCase):
    """A round with nothing to build still publishes what an earlier one finished."""

    def supervise(self, packages, dry_run=False):
        snapshot = types.SimpleNamespace(packages=packages, packages_dir="/nowhere",
                                         published_tag="packages-snapshot-1")
        args = types.SimpleNamespace(dry_run=dry_run, target_branch="main", poll_interval=0)
        with mock.patch.object(gh, "get_current_repo", return_value="vitasdk/x"), \
                mock.patch.object(commands.state, "enforce_core_pin", return_value=[]), \
                mock.patch.object(commands.state, "get_queue_with_status", return_value=snapshot), \
                mock.patch.object(commands, "drop_stale_dependents", return_value=False), \
                mock.patch.object(commands.report, "show_queue"), \
                mock.patch.object(commands, "update_status"), \
                mock.patch.object(commands, "image_tags", return_value={}), \
                mock.patch.object(commands.build_plan, "create_build_plan", return_value=[]), \
                mock.patch.object(commands.config, "worlds", return_value=[WORLD]), \
                mock.patch.object(gh, "dispatch_workflow") as dispatch:
            commands.cmd_supervise(args)
        return dispatch

    def finished(self, version, published_version):
        zlib = make_package("zlib", version)
        if published_version:
            published(zlib, published_version)
        zlib.set_status(WORLD, PackageStatus.FINISHED)
        return [zlib]

    def test_asks_for_a_snapshot_when_the_published_one_is_behind(self):
        dispatch = self.supervise(self.finished("1.4-1", "1.3.2-2"))
        dispatch.assert_called_once()
        self.assertEqual(dispatch.call_args.args[1], commands.SNAPSHOT_WORKFLOW)

    def test_asks_for_nothing_when_everything_finished_is_published(self):
        self.supervise(self.finished("1.4-1", "1.4-1")).assert_not_called()

    def test_a_dry_run_asks_for_nothing(self):
        self.supervise(self.finished("1.4-1", "1.3.2-2"), dry_run=True).assert_not_called()

    def test_a_snapshot_asked_for_here_still_says_what_is_short(self):
        zlib = self.finished("1.4-1", "1.3.2-2")[0]
        cpython = published(make_package("cpython", "2.7-2"), "2.7-2")
        cpython.set_status(WORLD, PackageStatus.FAILED_TO_BUILD)
        with self.assertRaises(SystemExit):
            self.supervise([zlib, cpython])


class TestSnapshotKeepsTheHeldGroup(unittest.TestCase):
    """What cmd_snapshot hands to the repository script, end to end."""

    STAGED = ["zlib-1.4-1-vita.pkg.tar.xz", "libpng-1.6.59-1-vita.pkg.tar.xz",
              "mini18n-0.2.1-1-vita.pkg.tar.xz"]
    PREVIOUS = ["zlib-1.3.2-2-vita.pkg.tar.xz", "libpng-1.6.58-1-vita.pkg.tar.xz",
                "cpython-2.7-2-vita.pkg.tar.xz", "mini18n-0.2.1-1-vita.pkg.tar.xz",
                "vita.db", "provenance.json"]

    def snapshot(self, staging=False, cores=None):
        state = types.SimpleNamespace(
            packages=november(), packages_dir="/nowhere", packages_revision="sha",
            staging_assets=[make_asset(name) for name in self.STAGED],
            published_tag="packages-snapshot-1")
        fetched = {}

        def download(assets, target):
            fetched[os.path.basename(os.path.dirname(target))] = sorted(a.filename for a in assets)

        def create_database(packages_dir, input_dir, output_dir, *rest):
            os.makedirs(output_dir)

        with tempfile.TemporaryDirectory() as work:
            args = types.SimpleNamespace(staging=staging, include_blocked=False, work_dir=work,
                                         buildscripts_revision="", no_publish=False)
            with mock.patch.object(commands.state, "get_queue_with_status", return_value=state), \
                    mock.patch.object(commands.state, "staging_release"), \
                    mock.patch.object(commands.config, "worlds", return_value=[WORLD]), \
                    mock.patch.object(repodb, "get_published_cores",
                                      return_value={WORLD.arch: WORLD.core} if cores is None else cores), \
                    mock.patch.object(gh, "get_snapshot_repo", return_value="vitasdk/x"), \
                    mock.patch.object(gh, "get_release"), \
                    mock.patch.object(gh, "get_assets",
                                      return_value=[make_asset(name) for name in self.PREVIOUS]), \
                    mock.patch.object(repository, "download", side_effect=download), \
                    mock.patch.object(repository, "create_database", side_effect=create_database), \
                    mock.patch.object(commands, "source_date_epoch", return_value="0"), \
                    mock.patch.object(commands, "publish_snapshot") as publish:
                commands.cmd_snapshot(args)
                provenance = None
                path = os.path.join(work, "release", "provenance.json")
                if os.path.exists(path):
                    with open(path, encoding="utf-8") as handle:
                        provenance = json.load(handle)
        return fetched.get(WORLD.arch), publish, provenance

    def test_the_held_group_goes_in_at_its_published_versions(self):
        fetched, publish, provenance = self.snapshot()
        self.assertEqual(fetched, ["cpython-2.7-2-vita.pkg.tar.xz",
                                   "libpng-1.6.58-1-vita.pkg.tar.xz",
                                   "mini18n-0.2.1-1-vita.pkg.tar.xz",
                                   "zlib-1.3.2-2-vita.pkg.tar.xz"])
        self.assertEqual(publish.call_args.args[1], 4)
        self.assertEqual(provenance["kept_from_previous"],
                         {"vita": ["zlib", "libpng", "cpython"]})

    def test_after_a_core_change_only_what_is_finished_goes_in(self):
        fetched, publish, provenance = self.snapshot(cores={WORLD.arch: "another-core"})
        self.assertEqual(fetched, ["mini18n-0.2.1-1-vita.pkg.tar.xz"])
        self.assertEqual(publish.call_args.args[1], 1)
        self.assertEqual(provenance["kept_from_previous"], {})

    def test_staging_shows_what_is_built_and_keeps_nothing(self):
        fetched, publish, _ = self.snapshot(staging=True)
        self.assertEqual(fetched, sorted(self.STAGED))
        publish.assert_not_called()
