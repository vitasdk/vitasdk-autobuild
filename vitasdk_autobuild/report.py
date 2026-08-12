"""What the queue looks like, for humans and for the website."""

from typing import Any, Iterable

from . import config
from .queue import Package, PackageStatus, get_cycles
from .utils import group, table

TODO_STATES = (PackageStatus.WAITING_FOR_BUILD,)
WAITING_STATES = (PackageStatus.WAITING_FOR_DEPENDENCIES,
                  PackageStatus.MANUAL_BUILD_REQUIRED)
DONE_STATES = (PackageStatus.FINISHED, PackageStatus.FINISHED_BUT_BLOCKED)


def show_queue(packages: Iterable[Package]) -> None:
    packages = list(packages)
    buckets: dict[str, list[Package]] = {"TODO": [], "WAITING": [], "FAILED": [], "DONE": []}
    for package in packages:
        if package.status in TODO_STATES:
            buckets["TODO"].append(package)
        elif package.status in WAITING_STATES:
            buckets["WAITING"].append(package)
        elif package.status in DONE_STATES:
            buckets["DONE"].append(package)
        else:
            buckets["FAILED"].append(package)

    cycles = get_cycles(packages)
    if cycles:
        with group(f"Dependency cycles ({len(cycles)})"):
            print(table(["Package", "", "Package"],
                        [(a, "<-->", b) for a, b in cycles]))

    for name, bucket in buckets.items():
        with group(f"{name} ({len(bucket)})"):
            print(table(
                ["Package", "Version", "In repository", "Status", "Details"],
                [(p.name, p.version, p.repo_version or "-", str(p.status),
                  p.details.get("desc") or "") for p in sorted(bucket, key=lambda p: p.name)]))


def summary_line(packages: Iterable[Package]) -> str:
    counts: dict[str, int] = {}
    for package in packages:
        counts[str(package.status)] = counts.get(str(package.status), 0) + 1
    return ", ".join(f"{count} {name}" for name, count in sorted(counts.items()))


def build_status(packages: Iterable[Package], jobs: list[dict[str, str]],
                 core_snapshot: str, packages_revision: str) -> dict[str, Any]:
    """The machine readable state, uploaded as status.json.

    This is the only thing the website reads, so it has to carry everything a
    catalogue page needs without a second request.
    """

    packages = sorted(packages, key=lambda p: p.name)
    entries = []
    for package in packages:
        details = dict(package.details)
        details.pop("blocked", None)
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
            "status": str(package.status),
            "details": details,
        })

    return {
        "schema_version": 1,
        "core_snapshot": core_snapshot,
        "packages_repo": config.PACKAGES_REPO,
        "packages_revision": packages_revision,
        "jobs": jobs,
        "packages": entries,
        "cycles": [list(pair) for pair in get_cycles(packages)],
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


def table_of_updates(updates: Iterable[Any]) -> str:
    """What a recipe following a branch would be pinned to."""

    return table(["Package", "Now", "Would become"],
                 [(u.name, u.old_version, u.new_version)
                  for u in sorted(updates, key=lambda u: u.name)])
