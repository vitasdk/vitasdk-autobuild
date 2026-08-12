"""What the queue looks like, for humans and for the website."""

import time
from typing import Any, Iterable

from . import config, queue
from .config import World
from .queue import Package, PackageStatus, get_cycles
from .utils import group, table

TODO_STATES = (PackageStatus.WAITING_FOR_BUILD,)
WAITING_STATES = (PackageStatus.WAITING_FOR_DEPENDENCIES,
                  PackageStatus.MANUAL_BUILD_REQUIRED)
DONE_STATES = (PackageStatus.FINISHED, PackageStatus.FINISHED_BUT_BLOCKED)


def show_queue(packages: Iterable[Package], worlds: Iterable[World] | None = None) -> None:
    packages = list(packages)
    configured = list(worlds) if worlds is not None else config.worlds()

    for world in configured:
        buckets: dict[str, list[Package]] = {"TODO": [], "WAITING": [], "FAILED": [], "DONE": []}
        for package in packages:
            if not package.builds_for(world):
                continue
            status = package.get_status(world)
            if status in TODO_STATES:
                buckets["TODO"].append(package)
            elif status in WAITING_STATES:
                buckets["WAITING"].append(package)
            elif status in DONE_STATES:
                buckets["DONE"].append(package)
            else:
                buckets["FAILED"].append(package)

        cycles = get_cycles(packages, world)
        if cycles:
            with group(f"[{world.arch}] Dependency cycles ({len(cycles)})"):
                print(table(["Package", "", "Package"],
                            [(a, "<-->", b) for a, b in cycles]))

        for name, bucket in buckets.items():
            with group(f"[{world.arch}] {name} ({len(bucket)})"):
                print(table(
                    ["Package", "Version", "In repository", "Status", "Details"],
                    [(p.name, p.version, p.repo_version or "-", str(p.get_status(world)),
                      p.get_details(world).get("desc") or "")
                     for p in sorted(bucket, key=lambda p: p.name)]))


def summary_line(packages: Iterable[Package], worlds: Iterable[World] | None = None) -> str:
    packages = list(packages)
    configured = list(worlds) if worlds is not None else config.worlds()
    parts = []
    for world in configured:
        counts: dict[str, int] = {}
        for package in packages:
            if not package.builds_for(world):
                continue
            key = str(package.get_status(world))
            counts[key] = counts.get(key, 0) + 1
        summary = ", ".join(f"{count} {name}" for name, count in sorted(counts.items()))
        parts.append(f"{world.arch}: {summary}" if len(configured) > 1 else summary)
    return " | ".join(parts)


def build_status(packages: Iterable[Package], jobs: list[dict[str, str]],
                 packages_revision: str,
                 worlds: Iterable[World] | None = None,
                 built_at: dict[str, float] | None = None,
                 downloads: dict[str, int] | None = None,
                 generated_at: float | None = None,
                 published_tag: str = "",
                 snapshot_repo: str = "",
                 published_snapshots: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """The machine readable state, uploaded as status.json.

    This is the only thing the website reads, so it has to carry everything a
    catalogue page needs without a second request, for every world.
    """

    packages = sorted(packages, key=lambda p: p.name)
    configured = list(worlds) if worlds is not None else config.worlds()
    built_at = built_at or {}
    downloads = downloads or {}

    entries = []
    for package in packages:
        builds = {}
        for world in configured:
            if not package.builds_for(world):
                continue
            details = dict(package.get_details(world))
            details.pop("blocked", None)
            files = queue.asset_names(package, world, built_at)
            build = {
                "status": str(package.get_status(world)),
                "details": details,
            }
            # When a package was built, and how much it is downloaded, are
            # facts only the release knows. Carrying them here is what lets
            # the catalogue answer "what changed last night".
            if files:
                build["built_at"] = max(built_at[name] for name in files)
                build["downloads"] = sum(downloads.get(name, 0) for name in files)
            builds[world.arch] = build
        entries.append({
            "name": package.name,
            "version": package.version,
            "repo_version": package.repo_version,
            "description": package.description,
            "url": package.url,
            "licenses": package.licenses,
            "binaries": sorted(package.binaries),
            "depends": sorted(p.name for p in package.ext_depends),
            "rdepends": sorted(p.name for p in package.ext_rdepends),
            "builds": builds,
        })

    return {
        "schema_version": 3,
        # Which snapshot the repository versions above were read from, and the
        # ones before it. Without the tag, "in the repository" names a version
        # but not the thing it is in.
        "published_tag": published_tag,
        "published_snapshots": published_snapshots or [],
        "generated_at": generated_at if generated_at is not None else time.time(),
        "worlds": [
            {
                "arch": world.arch,
                "core": world.core,
                "repository": world.repository,
                "staging_repository": world.staging_repository,
                "description": world.description,
            }
            for world in configured
        ],
        "packages_repo": config.PACKAGES_REPO,
        # Where the snapshots live, which is not where the recipes live: the
        # catalogue needs it to link a published release.
        "snapshot_repo": snapshot_repo,
        "packages_revision": packages_revision,
        "jobs": jobs,
        "packages": entries,
        "cycles": {world.arch: [list(pair) for pair in get_cycles(packages, world)]
                   for world in configured},
    }


def running_jobs(jobs: Iterable[dict[str, Any]]) -> list[dict[str, str]]:
    """The workers currently building, taken from the workflow run's jobs."""

    running = []
    for job in jobs:
        if job.get("status") != "in_progress":
            continue
        running.append({
            "name": job.get("name", ""),
            "html_url": job.get("html_url", ""),
            "started_at": job.get("started_at", ""),
        })
    return sorted(running, key=lambda j: (j["started_at"], j["html_url"]))


KIND_LABELS = {
    "pin": "pin loose source",
    "advance": "take today's upstream",
    "release": "take upstream release",
}


def table_of_updates(updates: Iterable[Any]) -> str:
    """What each proposal would change, and which kind of decision it is."""

    return table(["Package", "Proposal", "Now", "Would become"],
                 [(u.name, KIND_LABELS.get(u.kind, u.kind), u.old_version, u.new_version)
                  for u in sorted(updates, key=lambda u: (u.kind, u.name))])
