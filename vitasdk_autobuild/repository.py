"""Turning staged package files into pacman repositories, one per world.

The database is not built here: it is built by scripts/create-repository.sh in
the recipe repository, which is the same script the old pipeline used and the
one that knows how to make the output reproducible.
"""

import fnmatch
import json
import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from typing import Iterable

from . import config, gh
from .config import World
from .gh import Asset
from .queue import Package, PackageStatus
from .utils import run

# Pinned Arch image carrying repo-add, matching the one vitasdk/packages uses.
ARCHLINUX_IMAGE = ("archlinux@sha256:"
                   "c1829f370be8434135f43fb3acaef1256780804ac3b2d2eec90dfb1232e1ffdf")


def selectable(packages: Iterable[Package], world: World,
               include_blocked: bool) -> list[Package]:
    """Packages whose files may go into a repository for this world.

    Blocked packages are complete builds that are held back because something
    around them is not rebuilt yet. They belong in the staging repository,
    where partial results are expected, and not in a published snapshot.
    """

    wanted = {PackageStatus.FINISHED}
    if include_blocked:
        wanted.add(PackageStatus.FINISHED_BUT_BLOCKED)
    return [p for p in packages
            if p.builds_for(world) and p.get_status(world) in wanted]


def select_assets(packages: Iterable[Package], world: World,
                  assets: Iterable[Asset]) -> list[Asset]:
    by_name = {asset.filename: asset for asset in assets}
    selected = []
    for package in packages:
        for pattern in package.build_patterns(world):
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
                    source_date_epoch: str, repository_name: str,
                    architecture: str) -> None:
    """Runs the recipe repository's repository script in the Arch image.

    The architecture is passed because the script validates every package
    against it, and its own default is the first world that ever existed.
    """

    if os.path.exists(output_dir):
        shutil.rmtree(output_dir)
    parent = os.path.dirname(os.path.abspath(output_dir))
    os.makedirs(parent, exist_ok=True)

    run([
        "docker", "run", "--rm", "--platform", "linux/amd64",
        # The repository script creates its output with mktemp, which is
        # readable only by its owner. Writing as the calling user keeps the
        # result usable by whoever asked for it instead of by root.
        "--user", f"{os.getuid()}:{os.getgid()}",
        "--env", "HOME=/tmp",
        "--mount", f"type=bind,source={os.path.abspath(packages_dir)},target=/workspace,readonly",
        "--mount", f"type=bind,source={os.path.abspath(input_dir)},target=/input,readonly",
        "--mount", f"type=bind,source={parent},target=/output",
        "--env", f"SOURCE_DATE_EPOCH={source_date_epoch}",
        "--env", f"REPOSITORY_NAME={repository_name}",
        "--env", f"EXPECTED_ARCHITECTURE={architecture}",
        ARCHLINUX_IMAGE,
        "bash", "-euc",
        f"/workspace/scripts/create-repository.sh /output/{os.path.basename(output_dir)} /input/*.pkg.tar.*",
    ])


def write_provenance(output_dir: str, packages_revision: str,
                     buildscripts_revision: str = "",
                     worlds: Iterable[World] | None = None) -> str:
    """Records exactly what a snapshot was built from.

    The core is per world and is not a guess: a world's staged packages are
    dropped whenever its pin changes, so every package here was built against
    the core named for its own world.
    """

    configured = list(worlds) if worlds is not None else config.worlds()
    path = os.path.join(output_dir, "provenance.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({
            "schema_version": 2,
            # Kept for the channel manifest generator, which reads a single
            # core. It names the first world's core, which is the only one
            # while there is one world.
            "core_snapshot": configured[0].core if configured else "",
            "worlds": [
                {"arch": world.arch, "repository": world.repository, "core": world.core}
                for world in configured
            ],
            "packages_revision": packages_revision,
            "buildscripts_revision": buildscripts_revision,
        }, handle, indent=2)
        handle.write("\n")
    return path
