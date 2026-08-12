import unittest

from vitasdk_autobuild import build_plan, config, queue
from vitasdk_autobuild.queue import PackageStatus

from .test_queue import make_package, queue_of


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
        queue.apply_status([package], ["zlib-1.0-1-vita.pkg.tar.xz"], [])
        self.assertEqual(build_plan.create_build_plan([package], "tag"), [])

    def test_blocked_packages_do_not_ask_for_workers(self):
        zlib = make_package("zlib")
        png = make_package("libpng", depends=["zlib"])
        packages = queue_of(zlib, png)
        queue.apply_status(packages, ["zlib-1.0-1-vita.pkg.tar.xz"], [])
        png.set_status(PackageStatus.WAITING_FOR_DEPENDENCIES)
        self.assertEqual(build_plan.create_build_plan(packages, "tag"), [])

    def test_job_names_are_unique(self):
        packages = [make_package(f"p{i}") for i in range(60)]
        queue.apply_status(packages, [], [])
        plan = build_plan.create_build_plan(packages, "tag")
        names = [job["name"] for job in plan]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(names[0], "build")

    def test_workers_start_from_different_ends(self):
        packages = [make_package(f"p{i}") for i in range(60)]
        queue.apply_status(packages, [], [])
        plan = build_plan.create_build_plan(packages, "tag")
        positions = [job["build-args"] for job in plan[:3]]
        self.assertEqual(positions, [
            "--build-from start", "--build-from end", "--build-from middle"])

    def test_image_tag_is_passed_to_every_worker(self):
        packages = [make_package("zlib")]
        queue.apply_status(packages, [], [])
        plan = build_plan.create_build_plan(packages, "sdk-snapshot-20260812.565.1")
        self.assertTrue(all(job["image-tag"] == "sdk-snapshot-20260812.565.1"
                            for job in plan))


class TestStalePackages(unittest.TestCase):

    def test_dependent_built_before_its_dependency_is_stale(self):
        zlib = make_package("zlib")
        png = make_package("libpng", depends=["zlib"])
        packages = queue_of(zlib, png)
        built_at = {
            "libpng-1.0-1-vita.pkg.tar.xz": 100.0,
            "zlib-1.0-1-vita.pkg.tar.xz": 200.0,
        }
        queue.apply_status(packages, list(built_at), [])
        self.assertEqual(queue.find_stale_packages(packages, built_at), [png])

    def test_dependent_built_after_its_dependency_is_current(self):
        zlib = make_package("zlib")
        png = make_package("libpng", depends=["zlib"])
        packages = queue_of(zlib, png)
        built_at = {
            "zlib-1.0-1-vita.pkg.tar.xz": 100.0,
            "libpng-1.0-1-vita.pkg.tar.xz": 200.0,
        }
        queue.apply_status(packages, list(built_at), [])
        self.assertEqual(queue.find_stale_packages(packages, built_at), [])

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
        queue.apply_status(packages, list(built_at), [])
        self.assertEqual(queue.find_stale_packages(packages, built_at), [])

    def test_unbuilt_packages_are_not_stale(self):
        zlib = make_package("zlib")
        png = make_package("libpng", depends=["zlib"])
        packages = queue_of(zlib, png)
        queue.apply_status(packages, [], [])
        self.assertEqual(queue.find_stale_packages(packages, {}), [])

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
        queue.apply_status(packages, list(built_at), [])
        self.assertEqual(queue.find_stale_packages(packages, built_at), [b])


if __name__ == "__main__":
    unittest.main()
