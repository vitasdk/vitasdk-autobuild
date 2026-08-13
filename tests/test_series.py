"""Release series: the axis that lets a release keep receiving packages.

A series is a core that stays put while its packages keep improving. Two of
them build the same recipes against different cores, and a package is named
after its architecture, which is the same in both — so `zlib-1.3.2-2-vita`
means one thing in 2026.08 and another in 2026.09, under one name.

The separation is therefore the store: one run drives one series, and the
arch axis inside it works exactly as it does with a single series.
"""

import unittest
from unittest import mock

from vitasdk_autobuild import config

DEFAULT = config.World(arch="vita", core="sdk-snapshot-1", repository="vita",
                       triple="arm-vita-eabi")
BACKPORT = config.World(arch="vita", core="sdk-core-2026.08.0", repository="vita",
                        triple="arm-vita-eabi", series="2026.08")


def with_series(*worlds, active=""):
    return mock.patch.multiple(config, WORLDS=list(worlds), ACTIVE_SERIES=active)


class TestTheUnnamedSeriesDoesNotMove(unittest.TestCase):
    """Whatever is running today must not notice this axis exists."""

    def test_the_store_keeps_the_names_it_has(self):
        with with_series(DEFAULT):
            self.assertEqual(config.staging_release_tag(), "staging")
            self.assertEqual(config.failed_release_tag(), "staging-failed")
            self.assertEqual(config.status_release_tag(), "status")
            self.assertEqual(config.snapshot_prefix(), "packages-snapshot-")

    def test_a_world_without_a_series_is_named_by_its_arch(self):
        self.assertEqual(DEFAULT.name, "vita")


class TestANamedSeriesOwnsItsStore(unittest.TestCase):

    def test_every_stored_thing_carries_the_series(self):
        with with_series(BACKPORT, active="2026.08"):
            self.assertEqual(config.staging_release_tag(), "staging-2026.08")
            self.assertEqual(config.failed_release_tag(), "staging-failed-2026.08")
            self.assertEqual(config.status_release_tag(), "status-2026.08")
            self.assertEqual(config.snapshot_prefix(), "packages-2026.08-snapshot-")

    def test_a_world_is_named_by_series_and_arch(self):
        self.assertEqual(BACKPORT.name, "2026.08/vita")


class TestTwoSeriesNeverSeeEachOther(unittest.TestCase):
    """The defect this exists to prevent.

    Both series build `zlib-1.3.2-2-vita.pkg.tar.xz`. Sharing a store would
    make either one look finished to the other, and would make the core pin
    of each look wrong to the other on every round.
    """

    def test_the_same_file_name_lands_in_different_stores(self):
        with with_series(DEFAULT, BACKPORT):
            default_staging = config.staging_release_tag()
            config.select_series("2026.08")
            self.assertNotEqual(config.staging_release_tag(), default_staging)

    def test_a_run_only_sees_the_worlds_of_its_series(self):
        with with_series(DEFAULT, BACKPORT):
            self.assertEqual([w.name for w in config.worlds()], ["vita"])
            self.assertEqual(config.default_world().core, "sdk-snapshot-1")
            config.select_series("2026.08")
            self.assertEqual([w.name for w in config.worlds()], ["2026.08/vita"])
            self.assertEqual(config.default_world().core, "sdk-core-2026.08.0")

    def test_the_core_marker_is_per_store_so_it_may_repeat(self):
        # Same file name in both, which is only safe because the releases
        # holding it are different ones.
        self.assertEqual(DEFAULT.core_marker, BACKPORT.core_marker)


class TestSelectingASeries(unittest.TestCase):

    def test_an_unconfigured_series_is_refused(self):
        with with_series(DEFAULT):
            with self.assertRaises(SystemExit) as caught:
                config.select_series("2026.08")
            self.assertIn("2026.08", str(caught.exception))

    def test_the_configured_ones_are_listed_in_order(self):
        with with_series(DEFAULT, BACKPORT):
            self.assertEqual(config.all_series(), ["", "2026.08"])

    def test_selecting_the_unnamed_series_is_allowed(self):
        with with_series(DEFAULT, BACKPORT, active="2026.08"):
            config.select_series("")
            self.assertEqual(config.staging_release_tag(), "staging")
