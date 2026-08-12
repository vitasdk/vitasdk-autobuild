import os
import unittest

from vitasdk_autobuild import srcinfo

# Real output of `vita-makepkg --nodeps --printsrcinfo` on vitasdk/packages.
ZLIB = """\
pkgbase = zlib
\tpkgdesc = A lossless data-compression library for Vita
\tpkgver = 1.3.2
\tpkgrel = 2
\turl = https://www.zlib.net/
\tarch = vita
\tlicense = Zlib
\tsource = https://github.com/madler/zlib/releases/download/v1.3.2/zlib-1.3.2.tar.xz
\tsource = zlib-no-pic.diff
\tsha256sums = d7a0654783a4da529d1bb793b7ad9c3318020af77667bcae35f95d0e42a792f3
\tsha256sums = 5880305adc815c4dd3de7d7c4883f3aca824fa9273cf6fa562e77e3dd5a00cef

pkgname = zlib
"""

LIBVITA2D = """\
pkgbase = libvita2d
\tpkgver = 9999
\tpkgrel = 1
\turl = https://github.com/xerpi/libvita2d
\tdepends = zlib
\tdepends = libpng
\tdepends = libjpeg-turbo
\tdepends = freetype
\tsource = git+https://github.com/xerpi/libvita2d.git
\tsha256sums = SKIP

pkgname = libvita2d
"""

SPLIT = """\
pkgbase = example
\tpkgver = 2.0
\tpkgrel = 3
\tepoch = 1
\tarch = vita
\tmakedepends = cmake-helper
\tdepends = zlib

pkgname = example
\tpkgdesc = the library

pkgname = example-tools
\tpkgdesc = the tools
\tdepends = example
\tprovides = example-utils
"""


class TestParse(unittest.TestCase):

    def test_single_package(self):
        info = srcinfo.parse(ZLIB)
        self.assertEqual(info["pkgbase"], "zlib")
        self.assertEqual(info["pkgver"], "1.3.2")
        self.assertEqual(info["pkgrel"], "2")
        self.assertEqual(info["arch"], ["vita"])
        self.assertEqual(list(info["packages"]), ["zlib"])
        self.assertEqual(len(info["source"]), 2)

    def test_arrays_repeat(self):
        info = srcinfo.parse(LIBVITA2D)
        self.assertEqual(info["depends"], ["zlib", "libpng", "libjpeg-turbo", "freetype"])

    def test_missing_arch_is_absent(self):
        # Recipes that do not declare arch inherit CARCH at build time, and
        # .SRCINFO simply has no arch line.
        self.assertNotIn("arch", srcinfo.parse(LIBVITA2D))

    def test_split_packages_inherit_and_override(self):
        info = srcinfo.parse(SPLIT)
        self.assertEqual(sorted(info["packages"]), ["example", "example-tools"])
        library = info["packages"]["example"]
        tools = info["packages"]["example-tools"]
        self.assertEqual(library["pkgdesc"], "the library")
        self.assertEqual(library["depends"], ["zlib"])
        self.assertEqual(library["arch"], ["vita"])
        # An override replaces the inherited value instead of extending it.
        self.assertEqual(tools["depends"], ["example"])
        self.assertEqual(tools["provides"], ["example-utils"])

    def test_inherited_lists_are_copies(self):
        info = srcinfo.parse(SPLIT)
        info["packages"]["example"]["arch"].append("any")
        self.assertEqual(info["packages"]["example-tools"]["arch"], ["vita"])

    def test_rejects_input_without_pkgbase(self):
        with self.assertRaises(ValueError):
            srcinfo.parse("pkgname = orphan\n")

    def test_ignores_comments_and_blank_lines(self):
        info = srcinfo.parse("# a comment\n\npkgbase = x\n\tpkgver = 1\n\npkgname = x\n")
        self.assertEqual(info["pkgver"], "1")


class TestStripConstraint(unittest.TestCase):

    def test_plain_name(self):
        self.assertEqual(srcinfo.strip_constraint("zlib"), "zlib")

    def test_version_constraints(self):
        self.assertEqual(srcinfo.strip_constraint("zlib>=1.2.3"), "zlib")
        self.assertEqual(srcinfo.strip_constraint("zlib<2"), "zlib")
        self.assertEqual(srcinfo.strip_constraint("zlib=1.2.3"), "zlib")

    def test_optdepends_description(self):
        self.assertEqual(srcinfo.strip_constraint("zlib: for compression"), "zlib")


class TestConfFile(unittest.TestCase):

    def test_conf_is_shipped(self):
        self.assertTrue(os.path.exists(srcinfo.SRCINFO_CONF))

    def test_conf_declares_the_architecture_used_for_file_names(self):
        from vitasdk_autobuild import config
        with open(srcinfo.SRCINFO_CONF, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn(f'CARCH="{config.ARCH}"', text)
        # Source protocols are validated even when nothing is downloaded, so
        # dropping these lists breaks every recipe using git sources.
        self.assertIn("VCSCLIENTS=", text)
        self.assertIn("DLAGENTS=", text)


if __name__ == "__main__":
    unittest.main()
