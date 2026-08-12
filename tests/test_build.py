import unittest

from vitasdk_autobuild import build, config, gh, queue

from .test_queue import WORLD, make_package, queue_of


def make_asset(filename, created_at=0.0):
    return gh.Asset(
        id=abs(hash(filename)) % 10000, name=filename, label=filename, size=1,
        state="uploaded", uploader="github-actions[bot]", uploader_type="Bot",
        url=f"https://api/{filename}", download_url=f"https://dl/{filename}",
        created_at=created_at)


class TestSelectDependencyAssets(unittest.TestCase):

    def test_picks_the_files_of_every_transitive_dependency(self):
        a = make_package("a")
        b = make_package("b", depends=["a"])
        c = make_package("c", depends=["b"])
        queue_of(a, b, c)
        assets = [make_asset("a-1.0-1-vita.pkg.tar.xz"),
                  make_asset("b-1.0-1-vita.pkg.tar.xz"),
                  make_asset("unrelated-1.0-1-vita.pkg.tar.xz")]
        selected = build.select_dependency_assets(c, WORLD, assets)
        self.assertEqual([a.filename for a in selected],
                         ["a-1.0-1-vita.pkg.tar.xz", "b-1.0-1-vita.pkg.tar.xz"])

    def test_dependencies_come_before_dependents(self):
        a = make_package("a")
        b = make_package("b", depends=["a"])
        c = make_package("c", depends=["b"])
        queue_of(a, b, c)
        assets = [make_asset("b-1.0-1-vita.pkg.tar.xz"),
                  make_asset("a-1.0-1-vita.pkg.tar.xz")]
        selected = build.select_dependency_assets(c, WORLD, assets)
        self.assertEqual([a.filename for a in selected],
                         ["a-1.0-1-vita.pkg.tar.xz", "b-1.0-1-vita.pkg.tar.xz"])

    def test_a_package_without_dependencies_needs_nothing(self):
        a = make_package("a")
        queue_of(a)
        self.assertEqual(build.select_dependency_assets(a, WORLD, []), [])

    def test_missing_dependency_file_is_an_error(self):
        a = make_package("a")
        b = make_package("b", depends=["a"])
        queue_of(a, b)
        with self.assertRaises(build.BuildError) as caught:
            build.select_dependency_assets(b, WORLD, [])
        self.assertIn("a-1.0-1-vita.pkg.tar.*", str(caught.exception))

    def test_wrong_version_does_not_satisfy_a_dependency(self):
        a = make_package("a", "2.0-1")
        b = make_package("b", depends=["a"])
        queue_of(a, b)
        with self.assertRaises(build.BuildError):
            build.select_dependency_assets(b, WORLD, [make_asset("a-1.0-1-vita.pkg.tar.xz")])

    def test_conflicting_dependencies_are_not_installed_together(self):
        original = config.CONFLICTING_DEPS
        config.CONFLICTING_DEPS = [["openssl", "openssl-1.1.1"]]
        try:
            new = make_package("openssl")
            old = make_package("openssl-1.1.1")
            curl = make_package("curl", depends=["openssl", "openssl-1.1.1"])
            queue_of(new, old, curl)
            assets = [make_asset("openssl-1.0-1-vita.pkg.tar.xz"),
                      make_asset("openssl-1.1.1-1.0-1-vita.pkg.tar.xz")]
            selected = build.select_dependency_assets(curl, WORLD, assets)
            self.assertEqual([a.filename for a in selected],
                             ["openssl-1.1.1-1.0-1-vita.pkg.tar.xz"])
        finally:
            config.CONFLICTING_DEPS = original


class TestExpectedOutputs(unittest.TestCase):

    def test_accepts_the_expected_file(self):
        package = make_package("zlib", "1.3.2-2")
        self.assertEqual(
            build.expected_outputs(package, WORLD, ["zlib-1.3.2-2-vita.pkg.tar.xz"]),
            ["zlib-1.3.2-2-vita.pkg.tar.xz"])

    def test_rejects_a_different_version(self):
        package = make_package("zlib", "1.3.2-2")
        with self.assertRaises(build.BuildError) as caught:
            build.expected_outputs(package, WORLD, ["zlib-1.3.2-1-vita.pkg.tar.xz"])
        self.assertIn("different version", str(caught.exception))

    def test_rejects_an_empty_build(self):
        package = make_package("zlib", "1.3.2-2")
        with self.assertRaises(build.BuildError) as caught:
            build.expected_outputs(package, WORLD, [])
        self.assertIn("nothing", str(caught.exception))

    def test_split_package_needs_every_file(self):
        package = make_package("example", binaries=["example", "example-tools"])
        with self.assertRaises(build.BuildError):
            build.expected_outputs(package, WORLD, ["example-1.0-1-vita.pkg.tar.xz"])


class TestPackager(unittest.TestCase):

    def test_packager_points_at_the_run(self):
        import os
        original = dict(os.environ)
        os.environ.update({
            "GITHUB_SERVER_URL": "https://github.com",
            "GITHUB_REPOSITORY": "vitasdk/vitasdk-autobuild",
            "GITHUB_RUN_ID": "42",
        })
        try:
            self.assertEqual(
                build.get_packager(),
                "CI (https://github.com/vitasdk/vitasdk-autobuild/actions/runs/42)")
        finally:
            os.environ.clear()
            os.environ.update(original)


class TestEnvironmentIsolation(unittest.TestCase):

    def test_recipe_environment_has_no_credentials(self):
        from vitasdk_autobuild.utils import clean_environ
        cleaned = clean_environ({
            "GITHUB_TOKEN": "secret", "GH_TOKEN": "secret",
            "ACTIONS_RUNTIME_TOKEN": "secret", "RUNNER_TEMP": "/tmp",
            "VITASDK": "/usr/local/vitasdk", "PATH": "/usr/bin",
        })
        self.assertEqual(sorted(cleaned), ["PATH", "VITASDK"])


if __name__ == "__main__":
    unittest.main()
