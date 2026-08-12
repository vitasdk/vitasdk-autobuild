"""Turning staged package files into a pacman repository.

The database is not built here: it is built by scripts/create-repository.sh in
the recipe repository, which is the same script the old pipeline used and the
one that knows how to make the output reproducible.
"""

import json
import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from typing import Iterable

from . import config, gh
from .gh import Asset
from .queue import Package, PackageStatus
from .utils import run

# Pinned Arch image carrying repo-add, matching the one vitasdk/packages uses.
ARCHLINUX_IMAGE = ("archlinux@sha256:"
                   "c1829f370be8434135f43fb3acaef1256780804ac3b2d2eec90dfb1232e1ffdf")


def selectable(packages: Iterable[Package], include_blocked: bool) -> list[Package]:
    """Packages whose files may go into a repository.

    Blocked packages are complete builds that are held back because something
    around them is not rebuilt yet. They belong in the staging repository,
    where partial results are expected, and not in a published snapshot.
    """

    wanted = {PackageStatus.FINISHED}
    if include_blocked:
        wanted.add(PackageStatus.FINISHED_BUT_BLOCKED)
    return [p for p in packages if p.status in wanted]


def select_assets(packages: Iterable[Package], assets: Iterable[Asset]) -> list[Asset]:
    import fnmatch

    by_name = {asset.filename: asset for asset in assets}
    selected = []
    for package in packages:
        for pattern in package.build_patterns():
            for name in sorted(fnmatch.filter(by_name, pattern)):
                selected.append(by_name[name])
    return selected


def download(assets: list[Asset], target_dir: str) -> list[str]:
    os.makedirs(target_dir, exist_ok=True)

    def fetch(asset: Asset) -> str:
        path = os.path.join(target_dir, asset.filename)
        if not os.path.exists(path):
            gh.download_asset(asset, path)
        return path

    with ThreadPoolExecutor(8) as executor:
        paths = list(executor.map(fetch, assets))
    print(f"Fetched {len(paths)} packages into {target_dir}", flush=True)
    return paths


def create_database(packages_dir: str, input_dir: str, output_dir: str,
                    source_date_epoch: str) -> None:
    """Runs the recipe repository's repository script in the Arch image."""

    if os.path.exists(output_dir):
        shutil.rmtree(output_dir)
    parent = os.path.dirname(os.path.abspath(output_dir))
    os.makedirs(parent, exist_ok=True)

    run([
        "docker", "run", "--rm", "--platform", "linux/amd64",
        "--mount", f"type=bind,source={os.path.abspath(packages_dir)},target=/workspace,readonly",
        "--mount", f"type=bind,source={os.path.abspath(input_dir)},target=/input,readonly",
        "--mount", f"type=bind,source={parent},target=/output",
        "--env", f"SOURCE_DATE_EPOCH={source_date_epoch}",
        "--env", f"REPOSITORY_NAME={config.REPOSITORY_NAME}",
        ARCHLINUX_IMAGE,
        "bash", "-euc",
        f"/workspace/scripts/create-repository.sh /output/{os.path.basename(output_dir)} /input/*.pkg.tar.*",
    ])


def write_provenance(output_dir: str, packages_revision: str,
                     buildscripts_revision: str = "") -> str:
    """Records exactly what a snapshot was built from.

    The core snapshot is not a guess: the staging area is wiped whenever the
    pin changes, so every package in here was built against this one.
    """

    path = os.path.join(output_dir, "provenance.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({
            "schema_version": 1,
            "core_snapshot": config.CORE_SNAPSHOT,
            "packages_revision": packages_revision,
            "buildscripts_revision": buildscripts_revision,
        }, handle, indent=2)
        handle.write("\n")
    return path
