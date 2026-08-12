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
class TestReusableWorkflowCalls(unittest.TestCase):
    """A calling job must hand over every permission the called one asks for.

    With `permissions: {}` at the top of a file, a job that calls a reusable
    workflow grants nothing, and the whole run then fails to start rather than
    failing a step, which is hard to read from the outside.
    """

    def called_workflows(self):
        for path in workflow_files():
            document = load(path)
            for job_name, job in document["jobs"].items():
                uses = str(job.get("uses", ""))
                if uses.startswith("./.github/workflows/"):
                    yield path, job_name, job, os.path.join(
                        WORKFLOW_DIR, os.path.basename(uses))

    def test_there_is_at_least_one_call(self):
        self.assertTrue(list(self.called_workflows()))

    def test_callers_grant_what_the_called_workflow_needs(self):
        # A called workflow can hold no more than its caller, and the strictest
        # job in it decides what the caller has to hand over.
        levels = {None: 0, "none": 0, "read": 1, "write": 2}
        for path, job_name, job, called_path in self.called_workflows():
            granted = job.get("permissions") or {}
            for called_job, called in load(called_path)["jobs"].items():
                for scope, level in (called.get("permissions") or {}).items():
                    with self.subTest(caller=os.path.basename(path), job=job_name,
                                      called=called_job, scope=scope):
                        self.assertGreaterEqual(levels[granted.get(scope)], levels[level])


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

    def test_it_can_be_started_by_hand(self):
        # The nightly schedule is commented out until a supervised run has
        # been verified once; only the manual trigger is live.
        self.assertIn("workflow_dispatch", triggers(self.document))
        self.assertNotIn("schedule", triggers(self.document))

@unittest.skipIf(yaml is None, "PyYAML is not installed")
class TestRecipeUpdateWorkflow(unittest.TestCase):
    """Editing someone else's recipes has to be inspectable before it happens."""

    def setUp(self):
        self.document = load(os.path.join(WORKFLOW_DIR, "update-recipes.yml"))
        self.steps = self.document["jobs"]["update"]["steps"]

    def step(self, fragment):
        for step in self.steps:
            if fragment in step.get("name", ""):
                return step
        self.fail(f"no step named like {fragment!r}")

    def test_a_dry_run_can_be_asked_for(self):
        inputs = triggers(self.document)["workflow_dispatch"]["inputs"]
        self.assertEqual(inputs["dry_run"]["type"], "boolean")
        self.assertIs(inputs["dry_run"]["default"], False)

    def test_a_dry_run_neither_edits_nor_opens_a_pull_request(self):
        plan = self.step("could change")
        self.assertIn("if [[ $DRY_RUN != 'true' ]]; then", plan["run"])
        self.assertIn("arguments+=(--write)", plan["run"])
        self.assertIn("dry_run", self.step("pull request")["if"])

    def test_every_offered_proposal_is_one_the_command_knows(self):
        from vitasdk_autobuild import main
        parser = main.build_parser()
        known = set()
        for action in parser._subparsers._group_actions[0].choices["update-recipes"]._actions:
            if action.dest == "propose":
                known = set(action.choices)
        offered = triggers(self.document)["workflow_dispatch"]["inputs"]["propose"]["options"]
        for option in offered:
            for kind in option.split(","):
                with self.subTest(kind=kind):
                    self.assertIn(kind, known)

    def test_each_package_gets_its_own_pull_request(self):
        # A wrong proposal for one package must not hold back the right ones,
        # and a package is the unit a maintainer decides about.
        body = self.step("pull request")["run"]
        self.assertIn('git checkout --quiet -B "follow/$package"', body)
        self.assertIn('git apply --include="$package/*"', body)

    def test_a_proposal_that_moved_updates_its_own_pull_request(self):
        body = self.step("pull request")["run"]
        self.assertIn("--force", body)
        self.assertIn("gh pr list", body)

    def test_it_never_pushes_to_the_recipes_directly(self):
        # Noticing that upstream moved is this job's business; taking it is
        # the maintainer's, so the only way out is a pull request.
        body = self.step("pull request")["run"]
        self.assertIn("gh pr create", body)
        for branch in ("master", "next"):
            self.assertNotIn(f'"$remote" "{branch}"', body)

    def test_the_job_cannot_write_to_its_own_repository(self):
        self.assertEqual(self.document["jobs"]["update"]["permissions"]["contents"], "read")


@unittest.skipIf(yaml is None, "PyYAML is not installed")
class TestMaintenanceWorkflow(unittest.TestCase):
    """Unsticking a package must not require a local token."""

    def setUp(self):
        self.document = load(os.path.join(WORKFLOW_DIR, "maintenance.yml"))

    def actions(self):
        return triggers(self.document)["workflow_dispatch"]["inputs"]["action"]["options"]

    def test_every_offered_action_is_a_real_command(self):
        from vitasdk_autobuild import main
        parser = main.build_parser()
        known = {"staging-index": "snapshot"}
        for action in self.actions():
            with self.subTest(action=action):
                command = known.get(action, action)
                self.assertTrue(callable(parser.parse_args([command]).func))

    def test_clearing_failures_is_offered(self):
        # The one operation that a broken recipe cannot fix by itself.
        self.assertIn("clear-failed", self.actions())

    def test_it_can_write(self):
        self.assertEqual(self.document["jobs"]["run"]["permissions"]["contents"], "write")

    def test_it_is_only_manual(self):
        self.assertEqual(list(triggers(self.document)), ["workflow_dispatch"])


if __name__ == "__main__":
    unittest.main()
