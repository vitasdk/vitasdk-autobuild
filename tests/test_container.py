"""The checks that only mean anything against a real SDK.

Every failure of the first real runs was of this kind: pacman rejecting an
option, a directory owned by the wrong user, makepkg refusing to run as root.
None of them could be caught with mocks, because none of them was about the
arguments this code builds — they were about what the tools do with them.

Skipped unless VITASDK points at an installed SDK, so it runs inside the build
image and nowhere else.
"""

import os
import subprocess
import unittest

from vitasdk_autobuild import build, srcinfo
from vitasdk_autobuild.utils import as_build_user, get_build_user, give_to_build_user

SDK = os.environ.get("VITASDK", "")
# Only a missing SDK is a reason to skip. This also required the client to be
# at a path it named itself, which switched these tests off in precisely the
# case they exist to catch: an installed SDK whose client had moved.
ENABLED = bool(SDK)


@unittest.skipUnless(ENABLED, "needs an installed SDK: set VITASDK")
class TestAgainstRealPacman(unittest.TestCase):

    def setUp(self):
        build.prepare_prefix(SDK)

    def test_the_package_client_can_be_reached_at_all(self):
        # The one that would have caught bin/pacman becoming
        # libexec/vdpm/pacman: every dependency install died on the old path
        # for a week while this suite reported success by skipping itself.
        result = build.pacman(SDK, "--version", check=False)
        self.assertEqual(result.returncode, 0,
                         f"vdpm could not run pacman: {result.stderr or result.stdout}")

    def test_the_package_database_can_be_created(self):
        # As the build user, in a tree this process created as root. makepkg
        # needs the same thing to write .BUILDINFO.
        self.assertIsInstance(build.installed_dependencies(SDK), list)

    def test_querying_does_not_use_transaction_options(self):
        # pacman answers 'invalid option' to a query carrying a transaction
        # option. When that error was swallowed, the prefix was never reset
        # and one build kept polluting the next.
        result = build.pacman(SDK, "--query", "--deps", "--quiet", check=False)
        self.assertNotIn("invalid option", (result.stderr or "").lower())

        # An exit code of 1 with no output at all is not an error: it is what
        # pacman says when the local database holds no packages, which is the
        # state of an SDK installed from a tarball. vita-makepkg had to learn
        # the same distinction in run_pacman.
        if result.returncode != 0:
            self.assertEqual((result.stdout, result.stderr), ("", ""),
                             "a failing query must be the empty database case")

    def test_the_installed_list_is_readable_either_way(self):
        self.assertIsInstance(build.installed_dependencies(SDK), list)

    def test_resetting_the_prefix_twice_is_harmless(self):
        build.reset_dependencies(SDK)
        self.assertEqual(build.reset_dependencies(SDK), [])

    def test_the_prefix_belongs_to_whoever_builds(self):
        user = get_build_user()
        if user is None:
            self.skipTest("not running as root, nothing to hand over")
        state = os.path.join(SDK, "var", "lib", "pacman")
        import pwd
        self.assertEqual(os.stat(state).st_uid, pwd.getpwnam(user).pw_uid)


@unittest.skipUnless(ENABLED, "needs an installed SDK: set VITASDK")
class TestAgainstRealMakepkg(unittest.TestCase):

    def test_a_recipe_can_be_read(self):
        # makepkg refuses to run as root, and writes next to the recipe even
        # when only printing metadata.
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            with open(os.path.join(directory, "VITABUILD"), "w", encoding="utf-8") as handle:
                handle.write("pkgname=smoke\npkgver=1\npkgrel=1\n"
                             "source=()\nsha256sums=()\n\npackage() { :; }\n")
            give_to_build_user(directory)
            makepkg = srcinfo.find_vita_makepkg()
            info = srcinfo.read(directory, makepkg)
        self.assertEqual(info["pkgbase"], "smoke")
        self.assertEqual(info["pkgver"], "1")

    def test_the_build_user_cannot_read_this_process_environment(self):
        # The reason the container runs as root: recipes are arbitrary code and
        # this process holds a token that can write packages.
        if get_build_user() is None:
            self.skipTest("not running as root, no boundary to check")
        environ = dict(os.environ, SMOKE_SECRET="must-not-be-visible")
        command = as_build_user(["sh", "-c", f"cat /proc/{os.getpid()}/environ || true"],
                                environ, ["PATH", "HOME"])
        result = subprocess.run(command, env=environ, capture_output=True, text=True)
        self.assertNotIn("must-not-be-visible", result.stdout)


if __name__ == "__main__":
    unittest.main()
