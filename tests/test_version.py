"""pacman decides whether an update is an update, so we have to match it."""

import os
import subprocess
import unittest

from vitasdk_autobuild.version import newer, vercmp

# Answers taken from libalpm's own rpmvercmp, not from reasoning about it.
KNOWN = [
    ("1.0", "1.0", 0),
    ("1.1", "1.0", 1),
    ("1.10", "1.9", 1),
    ("1.0.2a", "1.0.2", -1),      # a leftover alpha never beats an empty segment
    ("1.0.2", "1.0.2a", 1),
    ("3.11.0", "3.11.r115133.gfa69d3f", 1),   # numeric beats alpha
    ("3.11.0a5", "3.11.r115133.gfa69d3f", 1),  # which is why alphas are filtered
    ("3.11.r115133.gfa69d3f", "3.11", 1),      # pinning was an upgrade
    ("2.7.r81103.g4f6d059", "2.7", 1),
    ("0.0.0.r1431.gaaaaaaa", "0.0.0.r1430.g3dddc43", 1),
    ("0.0.0.r1430.g3dddc43", "9999", -1),      # 9999 sat above everything
    ("0001.1", "1.1", 0),                      # leading zeros are not a version
    ("1.2", "1..2", -1),                       # separator length is compared
    # OpenSSL's letter releases go backwards for pacman, exactly like 1.0.2a
    # above. Distributions work around it with an epoch; worth knowing before
    # proposing one of these.
    ("1.1.1w", "1.1.1", -1),
]


class TestVercmp(unittest.TestCase):

    def test_the_known_answers(self):
        for a, b, expected in KNOWN:
            with self.subTest(a=a, b=b):
                self.assertEqual(vercmp(a, b), expected)

    def test_it_is_antisymmetric(self):
        for a, b, expected in KNOWN:
            with self.subTest(a=b, b=a):
                self.assertEqual(vercmp(b, a), -expected)

    def test_newer_reads_the_way_the_question_is_asked(self):
        self.assertTrue(newer("1.1", "1.0"))
        self.assertFalse(newer("1.0", "1.0"))
        self.assertFalse(newer("1.0.2a", "1.0.2"))


@unittest.skipUnless(os.environ.get("VITASDK_AUTOBUILD_VERCMP"),
                     "set VITASDK_AUTOBUILD_VERCMP to a real vercmp binary")
class TestAgainstRealPacman(unittest.TestCase):
    """Compares every pair of a corpus against the binary pacman ships.

    Skipped unless a binary is provided, because the point is to check the
    transcription when one is available, not to require pacman to run tests.
    """

    CORPUS = [v for v, _, _ in KNOWN] + [
        "1.0", "1.0.0", "1.6.58", "2.17.0", "4.4.1", "0.3.1_hotfix", "20260812",
        "1.2.3.dev4", "9f", "0.0.0", "3.11.5", "1.7.0beta89",
    ]

    def test_every_pair_agrees(self):
        binary = os.environ["VITASDK_AUTOBUILD_VERCMP"]
        for a in self.CORPUS:
            for b in self.CORPUS:
                real = int(subprocess.run([binary, a, b], check=True,
                                          capture_output=True, text=True).stdout)
                with self.subTest(a=a, b=b):
                    self.assertEqual(vercmp(a, b), real)


if __name__ == "__main__":
    unittest.main()
