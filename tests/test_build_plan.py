import unittest

from vitasdk_autobuild import build_plan, config, queue
from vitasdk_autobuild.queue import PackageStatus

from .test_queue import WORLD, make_package, queue_of


def apply_status(packages, done, failed):
    queue.apply_status(packages, done, failed, None, [WORLD])


def plan(packages, tag="tag"):
    return build_plan.create_build_plan(packages, {WORLD.arch: tag}, [WORLD])


class TestJobCount(unittest.TestCase):

    def test_empty_queue_dispatches_nothing(self):
        self.assertEqual(build_plan.job_count(0), 0)

    def test_one_package_gets_one_worker(self):
        self.assertEqual(build_plan.job_count(1), 1)

    def test_scales_with_the_queue(self):
        self.assertEqual(build_plan.job_count(build_plan.PACKAGES_PER_JOB), 1)
        self.assertEqual(build_plan.job_count(build_plan.PACKAGES_PER_JOB + 1), 2)

    def test_never_exceeds_the_maximum(self):
        self.assertEqual(build_plan.job_count(10000), config.MAXIMUM_JOB_COUNT)

    def test_a_full_catalogue_saturates_the_workers(self):
        # 132 recipes today: a cold start should use every worker allowed.
        self.assertEqual(build_plan.job_count(132), config.MAXIMUM_JOB_COUNT)


class TestBuildPlan(unittest.TestCase):

    def test_plan_is_empty_when_nothing_waits(self):
        package = make_package("zlib")
        apply_status([package], ["zlib-1.0-1-vita.pkg.tar.xz"], [])
        self.assertEqual(plan([package]), [])

    def test_blocked_packages_do_not_ask_for_workers(self):
        zlib = make_package("zlib")
        png = make_package("libpng", depends=["zlib"])
        packages = queue_of(zlib, png)
        apply_status(packages, ["zlib-1.0-1-vita.pkg.tar.xz"], [])
        png.set_status(WORLD, PackageStatus.WAITING_FOR_DEPENDENCIES)
        self.assertEqual(plan(packages), [])

    def test_job_names_are_unique(self):
        packages = [make_package(f"p{i}") for i in range(60)]
        apply_status(packages, [], [])
        jobs = plan(packages)
        names = [job["name"] for job in jobs]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(names[0], WORLD.arch)

    def test_workers_start_from_different_ends(self):
        packages = [make_package(f"p{i}") for i in range(60)]
        apply_status(packages, [], [])
        jobs = plan(packages)
        positions = [job["build-args"] for job in jobs[:3]]
        self.assertEqual(positions, [
            "--world vita --build-from start",
            "--world vita --build-from end",
            "--world vita --build-from middle"])

    def test_image_tag_is_passed_to_every_worker(self):
        packages = [make_package("zlib")]
        apply_status(packages, [], [])
        jobs = plan(packages, "sdk-snapshot-20260812.565.1")
        self.assertTrue(all(job["image-tag"] == "sdk-snapshot-20260812.565.1"
                            for job in jobs))


class TestStalePackages(unittest.TestCase):

    def test_dependent_built_before_its_dependency_is_stale(self):
        zlib = make_package("zlib")
        png = make_package("libpng", depends=["zlib"])
        packages = queue_of(zlib, png)
        built_at = {
            "libpng-1.0-1-vita.pkg.tar.xz": 100.0,
            "zlib-1.0-1-vita.pkg.tar.xz": 200.0,
        }
        apply_status(packages, list(built_at), [])
        self.assertEqual(queue.find_stale_packages(packages, built_at, WORLD), [png])

    def test_dependent_built_after_its_dependency_is_current(self):
        zlib = make_package("zlib")
        png = make_package("libpng", depends=["zlib"])
        packages = queue_of(zlib, png)
        built_at = {
            "zlib-1.0-1-vita.pkg.tar.xz": 100.0,
            "libpng-1.0-1-vita.pkg.tar.xz": 200.0,
        }
        apply_status(packages, list(built_at), [])
        self.assertEqual(queue.find_stale_packages(packages, built_at, WORLD), [])

    def test_rebuilding_clears_the_staleness(self):
        # The point of comparing times: after the rebuild the same input says
        # "current", so the round does not fire again.
        zlib = make_package("zlib")
        png = make_package("libpng", depends=["zlib"])
        packages = queue_of(zlib, png)
        built_at = {
            "libpng-1.0-1-vita.pkg.tar.xz": 300.0,
            "zlib-1.0-1-vita.pkg.tar.xz": 200.0,
        }
        apply_status(packages, list(built_at), [])
        self.assertEqual(queue.find_stale_packages(packages, built_at, WORLD), [])

    def test_unbuilt_packages_are_not_stale(self):
        zlib = make_package("zlib")
        png = make_package("libpng", depends=["zlib"])
        packages = queue_of(zlib, png)
        apply_status(packages, [], [])
        self.assertEqual(queue.find_stale_packages(packages, {}, WORLD), [])

    def test_staleness_reaches_the_whole_chain_one_step_at_a_time(self):
        a = make_package("a")
        b = make_package("b", depends=["a"])
        c = make_package("c", depends=["b"])
        packages = queue_of(a, b, c)
        built_at = {
            "c-1.0-1-vita.pkg.tar.xz": 100.0,
            "b-1.0-1-vita.pkg.tar.xz": 100.0,
            "a-1.0-1-vita.pkg.tar.xz": 200.0,
        }
        apply_status(packages, list(built_at), [])
        self.assertEqual(queue.find_stale_packages(packages, built_at, WORLD), [b])


if __name__ == "__main__":
    unittest.main()
