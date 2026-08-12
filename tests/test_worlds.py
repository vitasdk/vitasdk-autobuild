"""Two worlds at once: the dimension that exists for a second toolchain.

One world is configured today. These tests run the machinery with two, so
that adding the second one is a configuration entry and not a rewrite.
"""

import unittest

from vitasdk_autobuild import build_plan, config, queue, report, repository
from vitasdk_autobuild.queue import PackageStatus

from .test_queue import make_package

NEWLIB = config.World(arch="vita", core="core-newlib", repository="vita",
                      triple="arm-vita-eabi")
MUSL = config.World(arch="vita-musl", core="core-musl", repository="vita-musl",
                    triple="arm-vita-muslabi")
BOTH = [NEWLIB, MUSL]


def apply(packages, done=(), failed=()):
    queue.apply_status(packages, list(done), list(failed), None, BOTH)


class TestWhichWorldsARecipeBuildsFor(unittest.TestCase):

    def test_no_declared_arch_means_every_world(self):
        package = make_package("zlib", worlds=BOTH)
        self.assertEqual([w.arch for w in package.worlds], ["vita", "vita-musl"])

    def test_a_declared_arch_restricts_it(self):
        package = make_package("kubridge", arch=["vita"], worlds=BOTH)
        self.assertEqual([w.arch for w in package.worlds], ["vita"])
        self.assertTrue(package.builds_for(NEWLIB))
        self.assertFalse(package.builds_for(MUSL))

    def test_the_file_name_carries_the_world(self):
        package = make_package("zlib", "1.3.2-2", worlds=BOTH)
        self.assertEqual(package.build_patterns(NEWLIB), ["zlib-1.3.2-2-vita.pkg.tar.*"])
        self.assertEqual(package.build_patterns(MUSL), ["zlib-1.3.2-2-vita-musl.pkg.tar.*"])

    def test_so_do_failure_markers(self):
        package = make_package("zlib", "1.3.2-2", worlds=BOTH)
        self.assertNotEqual(package.failed_name(NEWLIB), package.failed_name(MUSL))


class TestStatusIsPerWorld(unittest.TestCase):

    def test_finished_in_one_world_and_queued_in_the_other(self):
        zlib = make_package("zlib", worlds=BOTH)
        apply([zlib], done=["zlib-1.0-1-vita.pkg.tar.xz"])
        self.assertEqual(zlib.get_status(NEWLIB), PackageStatus.FINISHED)
        self.assertEqual(zlib.get_status(MUSL), PackageStatus.WAITING_FOR_BUILD)

    def test_failing_in_one_world_does_not_fail_the_other(self):
        zlib = make_package("zlib", worlds=BOTH)
        apply([zlib], failed=["zlib-1.0-1-vita-musl.failed"])
        self.assertEqual(zlib.get_status(NEWLIB), PackageStatus.WAITING_FOR_BUILD)
        self.assertEqual(zlib.get_status(MUSL), PackageStatus.FAILED_TO_BUILD)

    def test_a_dependency_of_the_other_world_does_not_satisfy_this_one(self):
        zlib = make_package("zlib", worlds=BOTH)
        png = make_package("libpng", depends=["zlib"], worlds=BOTH)
        packages = [zlib, png]
        queue.link_dependencies(packages)
        apply(packages, done=["zlib-1.0-1-vita.pkg.tar.xz"])
        self.assertEqual(png.get_status(NEWLIB), PackageStatus.WAITING_FOR_BUILD)
        self.assertEqual(png.get_status(MUSL), PackageStatus.WAITING_FOR_DEPENDENCIES)


class TestImpossibleWorlds(unittest.TestCase):
    """A recipe can name a world its dependencies do not support."""

    def test_a_package_loses_a_world_its_dependency_lacks(self):
        vita_only = make_package("taihen", arch=["vita"], worlds=BOTH)
        user = make_package("kubridge", depends=["taihen"], worlds=BOTH)
        packages = [vita_only, user]
        queue.link_dependencies(packages)
        dropped = queue.prune_impossible_worlds(packages)

        self.assertEqual(dropped, {"kubridge": ["vita-musl"]})
        self.assertFalse(user.builds_for(MUSL))
        self.assertTrue(user.builds_for(NEWLIB))

    def test_pruning_propagates_to_dependents(self):
        vita_only = make_package("taihen", arch=["vita"], worlds=BOTH)
        middle = make_package("kubridge", depends=["taihen"], worlds=BOTH)
        top = make_package("uvdb", depends=["kubridge"], worlds=BOTH)
        packages = [vita_only, middle, top]
        queue.link_dependencies(packages)
        queue.prune_impossible_worlds(packages)
        self.assertFalse(top.builds_for(MUSL))

    def test_nothing_is_pruned_when_everything_fits(self):
        zlib = make_package("zlib", worlds=BOTH)
        png = make_package("libpng", depends=["zlib"], worlds=BOTH)
        packages = [zlib, png]
        queue.link_dependencies(packages)
        self.assertEqual(queue.prune_impossible_worlds(packages), {})


class TestPlanningAcrossWorlds(unittest.TestCase):

    def test_each_world_gets_its_own_workers_and_image(self):
        packages = [make_package(f"p{i}", worlds=BOTH) for i in range(20)]
        apply(packages)
        plan = build_plan.create_build_plan(
            packages, {"vita": "image-newlib", "vita-musl": "image-musl"}, BOTH)

        by_world = {}
        for job in plan:
            world = job["build-args"].split()[1]
            by_world.setdefault(world, []).append(job)
        self.assertEqual(sorted(by_world), ["vita", "vita-musl"])
        self.assertEqual(by_world["vita"][0]["image-tag"], "image-newlib")
        self.assertEqual(by_world["vita-musl"][0]["image-tag"], "image-musl")

    def test_a_world_with_nothing_queued_asks_for_no_workers(self):
        zlib = make_package("zlib", arch=["vita"], worlds=BOTH)
        apply([zlib])
        plan = build_plan.create_build_plan(
            [zlib], {"vita": "a", "vita-musl": "b"}, BOTH)
        self.assertEqual([job["name"] for job in plan], ["vita"])

    def test_the_worker_cap_covers_every_world_together(self):
        packages = [make_package(f"p{i}", worlds=BOTH) for i in range(400)]
        apply(packages)
        plan = build_plan.create_build_plan(
            packages, {"vita": "a", "vita-musl": "b"}, BOTH)
        self.assertLessEqual(len(plan), config.MAXIMUM_JOB_COUNT)


class TestPublishingAcrossWorlds(unittest.TestCase):

    def test_each_world_publishes_its_own_repository(self):
        zlib = make_package("zlib", worlds=BOTH)
        apply([zlib], done=["zlib-1.0-1-vita.pkg.tar.xz"])
        self.assertEqual(repository.selectable([zlib], NEWLIB, False), [zlib])
        self.assertEqual(repository.selectable([zlib], MUSL, False), [])

    def test_provenance_names_a_core_per_world(self):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            path = repository.write_provenance(directory, "rev", "", BOTH)
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        self.assertEqual([w["core"] for w in data["worlds"]], ["core-newlib", "core-musl"])

    def test_the_status_file_reports_every_world(self):
        zlib = make_package("zlib", worlds=BOTH)
        apply([zlib], done=["zlib-1.0-1-vita.pkg.tar.xz"])
        status = report.build_status([zlib], [], "rev", BOTH)
        builds = status["packages"][0]["builds"]
        self.assertEqual(builds["vita"]["status"], "finished")
        self.assertEqual(builds["vita-musl"]["status"], "waiting-for-build")
        self.assertEqual([w["arch"] for w in status["worlds"]], ["vita", "vita-musl"])

class TestStatusCarriesFacts(unittest.TestCase):
    """The catalogue cannot ask a second question, so the file answers first."""

    def test_build_time_and_downloads_travel_with_the_package(self):
        zlib = make_package("zlib", "1.3.2-2", worlds=BOTH)
        apply([zlib], done=["zlib-1.3.2-2-vita.pkg.tar.xz"])
        status = report.build_status(
            [zlib], [], "rev", BOTH,
            built_at={"zlib-1.3.2-2-vita.pkg.tar.xz": 1000.0},
            downloads={"zlib-1.3.2-2-vita.pkg.tar.xz": 7})
        build = status["packages"][0]["builds"]["vita"]
        self.assertEqual(build["built_at"], 1000.0)
        self.assertEqual(build["downloads"], 7)

    def test_a_package_that_was_never_built_carries_no_time(self):
        zlib = make_package("zlib", worlds=BOTH)
        apply([zlib])
        status = report.build_status([zlib], [], "rev", BOTH)
        self.assertNotIn("built_at", status["packages"][0]["builds"]["vita"])

    def test_the_file_says_when_it_was_written(self):
        # Without this the catalogue cannot tell fresh data from a stale copy.
        status = report.build_status([], [], "rev", BOTH, generated_at=1234.0)
        self.assertEqual(status["generated_at"], 1234.0)

class TestWhatIsAlreadyPublished(unittest.TestCase):
    """Being in the repository is per world, like everything else."""

    def test_a_dependent_published_in_one_world_only_blocks_there(self):
        # Publishing zlib cannot break a libpng that was never published for
        # that world, so it is held back in one and free in the other.
        zlib = make_package("zlib", worlds=BOTH)
        png = make_package("libpng", depends=["zlib"], worlds=BOTH)
        packages = [zlib, png]
        queue.link_dependencies(packages)
        png.repo_versions["vita"] = "0.9-1"
        apply(packages, done=["zlib-1.0-1-vita.pkg.tar.xz",
                              "zlib-1.0-1-vita-musl.pkg.tar.xz"])
        self.assertEqual(zlib.get_status(NEWLIB), PackageStatus.FINISHED_BUT_BLOCKED)
        self.assertEqual(zlib.get_status(MUSL), PackageStatus.FINISHED)

    def test_a_package_in_no_repository_is_new_everywhere(self):
        zlib = make_package("zlib", worlds=BOTH)
        self.assertTrue(zlib.is_new_in(NEWLIB))
        self.assertTrue(zlib.is_new_in(MUSL))
        self.assertEqual(zlib.repo_version, "")


if __name__ == "__main__":
    unittest.main()
