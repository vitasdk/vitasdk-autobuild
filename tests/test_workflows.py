import os
import re
import unittest

try:
    import yaml
except ImportError:  # pragma: no cover - only when PyYAML is absent
    yaml = None

from vitasdk_autobuild import commands

WORKFLOW_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".github", "workflows")

EXPRESSION = re.compile(r"\$\{\{(.*?)\}\}", re.DOTALL)


def workflow_files():
    return sorted(os.path.join(WORKFLOW_DIR, name)
                  for name in os.listdir(WORKFLOW_DIR) if name.endswith(".yml"))


class StrictLoader(getattr(yaml, "SafeLoader", object)):
    """A loader that refuses duplicate keys.

    YAML takes the last of two identical keys without complaining, so a step
    with two `env:` blocks parses cleanly here and is rejected by GitHub.
    """

    def construct_mapping(self, node, deep=False):  # type: ignore[no-untyped-def]
        seen = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in seen:
                raise yaml.constructor.ConstructorError(
                    None, None, f"duplicate key {key!r}", key_node.start_mark)
            seen.add(key)
        return super().construct_mapping(node, deep)


def load(path):
    with open(path, encoding="utf-8") as handle:
        return yaml.load(handle, Loader=StrictLoader)


def triggers(document):
    # PyYAML reads the bare key `on` as the boolean True.
    return document.get("on", document.get(True))


@unittest.skipIf(yaml is None, "PyYAML is not installed")
class TestWorkflowsParse(unittest.TestCase):

    def test_there_are_workflows(self):
        self.assertTrue(workflow_files())

    def test_every_workflow_parses(self):
        for path in workflow_files():
            with self.subTest(workflow=os.path.basename(path)):
                self.assertIsInstance(load(path), dict)

    def test_every_workflow_has_a_trigger_and_jobs(self):
        for path in workflow_files():
            with self.subTest(workflow=os.path.basename(path)):
                document = load(path)
                self.assertIsNotNone(triggers(document))
                self.assertTrue(document.get("jobs"))


class TestExpressionSyntax(unittest.TestCase):

    def test_expressions_never_use_double_quotes(self):
        # GitHub expressions only accept single quoted literals. A double
        # quoted one makes the whole file invalid, and the workflow then never
        # runs at all instead of failing visibly.
        for path in workflow_files():
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
            for expression in EXPRESSION.findall(text):
                with self.subTest(workflow=os.path.basename(path), expression=expression):
                    self.assertNotIn('"', expression)


@unittest.skipIf(yaml is None, "PyYAML is not installed")
class TestPermissions(unittest.TestCase):

    def test_no_workflow_grants_permissions_by_default(self):
        for path in workflow_files():
            with self.subTest(workflow=os.path.basename(path)):
                self.assertEqual(load(path).get("permissions"), {})

    def test_jobs_that_write_say_so(self):
        document = load(os.path.join(WORKFLOW_DIR, "build-jobs.yml"))
        self.assertEqual(document["jobs"]["build"]["permissions"]["contents"], "write")

    def test_nothing_uses_pull_request_target(self):
        for path in workflow_files():
            with self.subTest(workflow=os.path.basename(path)):
                self.assertNotIn("pull_request_target", triggers(load(path)) or {})

    def test_checkouts_do_not_keep_credentials(self):
        # A checkout that persists credentials leaves a usable token in the
        # working tree, which recipes and build scripts can read.
        for path in workflow_files():
            document = load(path)
            for job_name, job in document["jobs"].items():
                for step in job.get("steps", []):
                    if str(step.get("uses", "")).startswith("actions/checkout"):
                        with self.subTest(workflow=os.path.basename(path), job=job_name):
                            self.assertIs(step.get("with", {}).get("persist-credentials"), False)


@unittest.skipIf(yaml is None, "PyYAML is not installed")
class TestWorkerWorkflow(unittest.TestCase):

    def setUp(self):
        self.document = load(os.path.join(WORKFLOW_DIR, "build-jobs.yml"))
        self.job = self.document["jobs"]["build"]

    def test_the_supervisor_dispatches_this_file(self):
        self.assertTrue(os.path.exists(os.path.join(WORKFLOW_DIR, commands.WORKER_WORKFLOW)))

    def test_workers_take_their_matrix_from_the_plan(self):
        self.assertEqual(self.job["strategy"]["matrix"]["include"],
                         "${{ fromJson(inputs.build-plan) }}")

    def test_a_failing_worker_does_not_stop_the_others(self):
        self.assertIs(self.job["strategy"]["fail-fast"], False)

    def test_workers_only_run_on_the_build_branch(self):
        # Dispatching onto its own branch is what keeps a push to the default
        # branch from changing a build that is already running.
        self.assertIn("refs/heads/build-branch", self.job["if"])

    def test_an_empty_plan_builds_nothing(self):
        self.assertIn("inputs.build-plan != '[]'", self.job["if"])

    def test_the_container_runs_as_root_so_the_build_can_drop_privileges(self):
        self.assertEqual(self.job["container"]["options"], "--user 0:0")

    def test_the_image_comes_from_the_plan(self):
        self.assertIn("${{ matrix.image-tag }}", self.job["container"]["image"])


@unittest.skipIf(yaml is None, "PyYAML is not installed")
class TestSupervisorWorkflow(unittest.TestCase):

    def setUp(self):
        self.document = load(os.path.join(WORKFLOW_DIR, "build.yml"))

    def test_the_supervisor_can_dispatch_workflows(self):
        self.assertEqual(self.document["jobs"]["supervise"]["permissions"]["actions"], "write")

    def test_the_image_is_ready_before_the_workers_are_asked_for(self):
        self.assertIn("image", self.document["jobs"]["supervise"]["needs"])

    def test_only_one_supervisor_runs_at_a_time(self):
        self.assertEqual(self.document["concurrency"]["group"], "autobuild-supervisor")

    def test_it_runs_on_a_schedule(self):
        self.assertIn("schedule", triggers(self.document))


if __name__ == "__main__":
    unittest.main()
