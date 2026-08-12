"""The subcommands. One entry point per thing a human or a workflow asks for."""

import fnmatch
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from typing import Any

from . import (build, build_plan, config, gh, queue, recipes, report, repository,
               srcinfo, state)
from .queue import Package, PackageStatus
from .utils import group, notice, sanitize_tag, trust_git_checkouts

WORKER_WORKFLOW = "build-jobs.yml"


def image_tag(packages_dir: str | None = None) -> str:
    """Identifies the build image by everything that can change what it holds.

    That is the core SDK it installs and the recipe repository's Dockerfile,
    which decides the host tools. Keying on both means the tag is immutable:
    an existing tag never has to be rebuilt, and a changed input never reuses
    a stale image.
    """

    tag = sanitize_tag(config.CORE_SNAPSHOT)
    if packages_dir:
        with open(os.path.join(packages_dir, "Dockerfile"), "rb") as handle:
            tag += "-" + hashlib.sha256(handle.read()).hexdigest()[:8]
    return tag


def cmd_image_tag(args: Any) -> None:
    print(image_tag(state.packages_checkout()))


def source_date_epoch(packages_dir: str) -> str:
    """Commit time of the recipes, so a rebuild produces the same bytes."""

    override = os.environ.get("SOURCE_DATE_EPOCH")
    if override:
        return override
    result = subprocess.run(["git", "-C", packages_dir, "show", "-s", "--format=%ct", "HEAD"],
                            capture_output=True, text=True)
    if result.returncode != 0 or not result.stdout.strip():
        raise SystemExit("ERROR: cannot read the recipe commit time for SOURCE_DATE_EPOCH")
    return result.stdout.strip()


# ---------------------------------------------------------------- show

def cmd_show(args: Any) -> None:
    snapshot = state.get_queue_with_status(full_details=True, create_releases=False)
    report.show_queue(snapshot.packages)
    notice(report.summary_line(snapshot.packages))


# ---------------------------------------------------------------- status

def collect_jobs() -> list[dict[str, str]]:
    """Workers currently building, for the status file."""

    repo = gh.get_current_repo()
    try:
        runs = gh.api("GET", f"/repos/{repo}/actions/workflows/{WORKER_WORKFLOW}/runs"
                             f"?status=in_progress&per_page=10")
    except gh.GitHubError:
        return []
    jobs: list[dict[str, Any]] = []
    for run in runs.get("workflow_runs", []):
        jobs.extend(gh.get_run_jobs(repo, run["id"]))
    return report.running_jobs(jobs)


def update_status(snapshot: state.Snapshot) -> None:
    status = report.build_status(snapshot.packages, collect_jobs(),
                                 config.CORE_SNAPSHOT, snapshot.packages_revision)
    content = json.dumps(status, indent=2).encode() + b"\n"
    gh.upload_asset(state.status_release(), "status.json", content=content, replace=True)


def cmd_update_status(args: Any) -> None:
    update_status(state.get_queue_with_status(full_details=True))


# ---------------------------------------------------------------- build

def pick_package(packages: list[Package], build_from: str,
                 skip: set[tuple[str, str]]) -> Package | None:
    """Next package to build, approached from one end of the queue.

    Workers do not coordinate. Starting from different ends is what keeps two
    of them from picking the same package at the same moment; picking the same
    one anyway is wasteful but harmless.
    """

    ready = [p for p in packages
             if p.status == PackageStatus.WAITING_FOR_BUILD
             and (p.name, p.version) not in skip]
    if not ready:
        return None
    if build_from == "end":
        return ready[-1]
    if build_from == "middle":
        return ready[len(ready) // 2]
    return ready[0]


def cmd_build(args: Any) -> None:
    trust_git_checkouts()
    sdk = os.environ.get("VITASDK")
    if not sdk or not os.path.isdir(sdk):
        raise SystemExit("ERROR: VITASDK must point at an installed SDK")

    started = time.monotonic()
    skip: set[tuple[str, str]] = set()
    built = 0
    while True:
        if time.monotonic() - started >= config.SOFT_JOB_TIMEOUT:
            print("Soft timeout reached, not starting another package", flush=True)
            break

        snapshot = state.get_queue_with_status()
        package = pick_package(snapshot.packages, args.build_from, skip)
        if package is None:
            print("Nothing left to build", flush=True)
            break

        skip.add((package.name, package.version))
        if build.build_one(package, snapshot.packages_dir, sdk,
                           source_date_epoch(snapshot.packages_dir),
                           state.staging_release(), state.failed_release(),
                           snapshot.staging_assets):
            built += 1

    notice(f"Worker finished after building {built} package(s)")


# ---------------------------------------------------------------- supervise

def drop_stale_dependents(snapshot: state.Snapshot, dry_run: bool) -> int:
    """Removes packages that were built before something they link against."""

    if not config.REBUILD_DEPENDENTS:
        return 0
    stale = queue.find_stale_packages(snapshot.packages, snapshot.built_at)
    if not stale:
        return 0

    by_name = {asset.filename: asset for asset in snapshot.staging_assets}
    staging = state.staging_release()
    dropped = 0
    with group(f"Rebuilding {len(stale)} package(s) built before their dependencies"):
        for package in sorted(stale, key=lambda p: p.name):
            for pattern in package.build_patterns():
                for name in sorted(fnmatch.filter(by_name, pattern)):
                    print(f"{package.name}: dropping {name}", flush=True)
                    if not dry_run:
                        gh.delete_asset(staging.repo, by_name[name])
                    dropped += 1
    return dropped


def cmd_supervise(args: Any) -> None:
    repo = gh.get_current_repo()

    if state.enforce_core_pin(dry_run=args.dry_run):
        notice(f"Staging area reset for core {config.CORE_SNAPSHOT}")

    snapshot = state.get_queue_with_status(full_details=True,
                                           create_releases=not args.dry_run)
    if drop_stale_dependents(snapshot, args.dry_run) and not args.dry_run:
        snapshot = state.get_queue_with_status(full_details=True)

    report.show_queue(snapshot.packages)
    if not args.dry_run:
        update_status(snapshot)

    plan = build_plan.create_build_plan(snapshot.packages, image_tag(snapshot.packages_dir))
    if not plan:
        notice("Nothing to build")
        return
    notice(f"Dispatching {len(plan)} worker(s): {report.summary_line(snapshot.packages)}")
    if args.dry_run:
        print(json.dumps(plan, indent=2))
        return

    dispatched_at = time.time()
    gh.dispatch_workflow(repo, WORKER_WORKFLOW, args.target_branch,
                         {"build-plan": json.dumps(plan)})

    run_id = None
    for _ in range(10):
        run_id = gh.find_dispatched_run(repo, WORKER_WORKFLOW, args.target_branch, dispatched_at)
        if run_id:
            break
        time.sleep(10)
    if run_id is None:
        raise SystemExit("ERROR: the worker workflow was dispatched but no run appeared")
    notice(f"Workers running: {os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}"
           f"/{repo}/actions/runs/{run_id}")

    # Refreshing the status while the workers run is what makes the website
    # show progress; nothing else depends on this loop.
    while True:
        time.sleep(args.poll_interval)
        run = gh.get_run(repo, run_id)
        update_status(state.get_queue_with_status(full_details=True))
        if run.get("status") == "completed":
            notice(f"Workers finished: {run.get('conclusion')}")
            break


# ---------------------------------------------------------------- repository

def cmd_snapshot(args: Any) -> None:
    snapshot = state.get_queue_with_status()
    include_blocked = args.staging or args.include_blocked
    packages = repository.selectable(snapshot.packages, include_blocked)
    if not packages:
        raise SystemExit("ERROR: no finished packages to publish")

    assets = repository.select_assets(packages, snapshot.staging_assets)
    name = config.STAGING_REPOSITORY_NAME if args.staging else config.REPOSITORY_NAME
    original_name = config.REPOSITORY_NAME
    config.REPOSITORY_NAME = name
    try:
        work_dir = args.work_dir or tempfile.mkdtemp(prefix="vitasdk-repo-")
        packages_dir = os.path.join(work_dir, "packages")
        output_dir = os.path.join(work_dir, "repository")
        repository.download(assets, packages_dir)
        repository.create_database(snapshot.packages_dir, packages_dir, output_dir,
                                   source_date_epoch(snapshot.packages_dir))
    finally:
        config.REPOSITORY_NAME = original_name

    notice(f"Repository with {len(packages)} package(s) built in {output_dir}")

    if args.staging:
        staging = state.staging_release()
        for entry in sorted(os.listdir(output_dir)):
            if entry.endswith((".db", ".files", ".db.tar.gz", ".files.tar.gz")):
                gh.upload_asset(staging, entry,
                                path=os.path.join(output_dir, entry), replace=True)
        notice("Staging repository index updated")
        return

    repository.write_provenance(output_dir, snapshot.packages_revision,
                                args.buildscripts_revision)
    if args.no_publish:
        return
    publish_snapshot(output_dir, len(packages))


def publish_snapshot(output_dir: str, package_count: int) -> str:
    repo = gh.get_current_repo()
    tag = (config.SNAPSHOT_PREFIX + time.strftime("%Y%m%d", time.gmtime())
           + f".{os.environ.get('GITHUB_RUN_NUMBER', '0')}"
           + f".{os.environ.get('GITHUB_RUN_ATTEMPT', '1')}")

    release = gh.get_release(repo, tag)
    for entry in sorted(os.listdir(output_dir)):
        path = os.path.join(output_dir, entry)
        if os.path.isfile(path):
            gh.upload_asset(release, entry, path=path, replace=True)
    notice(f"Published {tag} with {package_count} package(s)")
    return tag


# ---------------------------------------------------------------- cleanup

def cmd_clean_assets(args: Any) -> None:
    snapshot = state.get_queue_with_status()
    keep = {config.CORE_MARKER_ASSET}
    patterns = []
    for package in snapshot.packages:
        patterns.extend(package.build_patterns())
        keep.add(package.failed_name())
    for name in (config.STAGING_REPOSITORY_NAME, config.REPOSITORY_NAME):
        keep.update({f"{name}.db", f"{name}.files",
                     f"{name}.db.tar.gz", f"{name}.files.tar.gz", "SHA256SUMS"})
    matcher = re.compile("|".join(fnmatch.translate(p) for p in patterns)) if patterns else None

    for release, assets in ((state.staging_release(), snapshot.staging_assets),
                            (state.failed_release(), snapshot.failed_assets)):
        for asset in assets:
            if asset.filename in keep:
                continue
            if matcher is not None and matcher.match(asset.filename):
                continue
            print(f"Deleting {asset.filename} from {release.tag}", flush=True)
            if not args.dry_run:
                gh.delete_asset(release.repo, asset)


def cmd_clear_failed(args: Any) -> None:
    failed = state.failed_release()
    for asset in gh.get_assets(failed, include_incomplete=True):
        if args.pattern and not fnmatch.fnmatch(asset.filename, args.pattern):
            continue
        print(f"Clearing {asset.filename}", flush=True)
        if not args.dry_run:
            gh.delete_asset(failed.repo, asset)


# ---------------------------------------------------------------- recipes

def cmd_update_recipes(args: Any) -> None:
    """Pins recipes that follow a branch to the commit being served today."""

    if not recipes.have_git():
        raise SystemExit("ERROR: git is required to resolve upstream repositories")

    packages_dir = state.packages_checkout()
    cache = recipes.recipe_cache_dir(state.cache_root())
    makepkg = srcinfo.find_vita_makepkg()

    updates: list[recipes.Update] = []
    problems: list[str] = []
    for info in srcinfo.collect(packages_dir):
        name = info["pkgbase"]
        if args.only and not fnmatch.fnmatch(name, args.only):
            continue
        package_dir = os.path.join(packages_dir, info["repo_path"])
        try:
            update = recipes.plan_update(package_dir, info, cache)
        except (ValueError, subprocess.CalledProcessError) as e:
            problems.append(str(e))
            continue
        if update is None or not update.changed:
            continue
        updates.append(update)
        if args.write:
            try:
                recipes.apply_update(package_dir, update, makepkg)
            except ValueError as e:
                problems.append(str(e))

    with group(f"Recipes following a branch ({len(updates)})"):
        print(report.table_of_updates(updates))
    for problem in problems:
        print(f"PROBLEM: {problem}", flush=True)

    if not args.write:
        notice(f"{len(updates)} recipe(s) would be pinned; pass --write to apply")
    else:
        notice(f"{len(updates)} recipe(s) pinned in {packages_dir}")
    if problems:
        raise SystemExit(f"{len(problems)} recipe(s) could not be updated")
