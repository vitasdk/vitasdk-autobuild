"""The subcommands. One entry point per thing a human or a workflow asks for."""

import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from typing import Any

from . import (build, build_plan, config, gh, queue, recipes, report, repository,
               srcinfo, state)
from .config import World
from .queue import Package, PackageStatus
from .utils import group, notice, sanitize_tag, trust_git_checkouts

WORKER_WORKFLOW = "build-jobs.yml"
SNAPSHOT_WORKFLOW = "snapshot.yml"


def image_tag(world: World, packages_dir: str | None = None) -> str:
    """Identifies a world's build image by everything that decides what it holds.

    That is the core SDK it installs and the recipe repository's Dockerfile,
    which decides the host tools. Keying on both means the tag is immutable:
    an existing tag never has to be rebuilt, and a changed input never reuses
    a stale image.
    """

    tag = sanitize_tag(world.core)
    if packages_dir:
        with open(os.path.join(packages_dir, "Dockerfile"), "rb") as handle:
            tag += "-" + hashlib.sha256(handle.read()).hexdigest()[:8]
    return tag


def image_tags(packages_dir: str | None = None) -> dict[str, str]:
    return {world.arch: image_tag(world, packages_dir) for world in config.worlds()}


def cmd_image_tag(args: Any) -> None:
    """Every configured world, not just the running series'.

    An image belongs to a world and its core, not to a store, and every series
    needs its own before any worker of that series can start.
    """

    packages_dir = state.packages_checkout()
    for world in config.WORLDS:
        print(f"{world.name} {image_tag(world, packages_dir)}")


def cmd_list_series(args: Any) -> None:
    """The series a scheduled run has to drive, as a matrix."""

    print(json.dumps(config.all_series()))


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
    status = report.build_status(
        snapshot.packages, collect_jobs(), snapshot.packages_revision,
        built_at=snapshot.built_at,
        downloads={a.filename: a.downloads for a in snapshot.staging_assets},
        published_tag=snapshot.published_tag,
        snapshot_repo=gh.get_snapshot_repo(),
        published_snapshots=snapshot.published_snapshots)
    content = json.dumps(status, indent=2).encode() + b"\n"
    gh.upload_asset(state.status_release(), "status.json", content=content, replace=True)
    notify_website()


def notify_website() -> None:
    """Tells the catalogue a new status file is up.

    Optional by design: without a token the site still refreshes on its own
    schedule, so a missing secret makes the site slower, never wrong.
    """

    if not config.WEBSITE_REPO:
        return
    token = os.environ.get("WEBSITE_TOKEN", "")
    if not token:
        print("::notice::No WEBSITE_TOKEN set: the catalogue will pick this up "
              "on its own schedule instead", flush=True)
        return
    try:
        gh.dispatch_repository(config.WEBSITE_REPO, config.WEBSITE_EVENT, token)
        print(f"Asked {config.WEBSITE_REPO} to refresh", flush=True)
    except gh.GitHubError as e:
        # Never fail a build over the website being slow to update.
        print(f"::warning::could not notify {config.WEBSITE_REPO}: {e}", flush=True)


def cmd_update_status(args: Any) -> None:
    update_status(state.get_queue_with_status(full_details=True))


# ---------------------------------------------------------------- build

def pick_package(packages: list[Package], world: World, worker: int, workers: int,
                 skip: set[tuple[str, str]]) -> Package | None:
    """Next package to build, entering the queue where this worker belongs.

    Workers do not coordinate, so what keeps two of them off the same package
    is where each one enters the queue. Dividing it by how many were dispatched
    gives every worker its own entry point; naming a fixed set of them instead
    means the ones past the end of that set duplicate the ones at the start,
    and the loser of the race only finds out when it tries to upload.
    """

    ready = [p for p in packages
             if p.builds_for(world)
             and p.get_status(world) == PackageStatus.WAITING_FOR_BUILD
             and (p.name, p.version) not in skip]
    if not ready:
        return None
    return ready[worker * len(ready) // workers]


def cmd_build(args: Any) -> None:
    trust_git_checkouts()
    world = config.world_by_arch(args.world) if args.world else config.default_world()
    sdk = os.environ.get("VITASDK")
    if not sdk or not os.path.isdir(sdk):
        raise SystemExit("ERROR: VITASDK must point at an installed SDK")
    print(f"Building for {world.arch} against {world.core}", flush=True)

    started = time.monotonic()
    skip: set[tuple[str, str]] = set()
    built = 0
    while True:
        if time.monotonic() - started >= config.SOFT_JOB_TIMEOUT:
            print("Soft timeout reached, not starting another package", flush=True)
            break

        snapshot = state.get_queue_with_status()
        package = pick_package(snapshot.packages, world, args.worker, args.workers, skip)
        if package is None:
            print("Nothing left to build", flush=True)
            break

        skip.add((package.name, package.version))
        try:
            if build.build_one(package, world, snapshot.packages_dir, sdk,
                               source_date_epoch(snapshot.packages_dir),
                               state.staging_release(), state.failed_release(),
                               snapshot.staging_assets):
                built += 1
        except gh.GitHubError as e:
            # Not the package's fault, so it gets no failure marker: move on
            # and let this or another worker pick it up again.
            print(f"::warning::{package.name}: {e}", flush=True)

    notice(f"Worker finished after building {built} package(s)")


def cmd_try_build(args: Any) -> None:
    """Builds one package from a proposed recipe, changing nothing.

    This is what makes a proposal decidable: a recipe change is only worth
    merging if it still builds, and reading a diff does not tell you that.
    """

    trust_git_checkouts()
    world = config.world_by_arch(args.world) if args.world else config.default_world()
    sdk = os.environ.get("VITASDK")
    if not sdk or not os.path.isdir(sdk):
        raise SystemExit("ERROR: VITASDK must point at an installed SDK")

    snapshot = state.get_queue_with_status(create_releases=False)
    wanted = [p for p in snapshot.packages if p.name == args.package]
    if not wanted:
        raise SystemExit(f"ERROR: no recipe named {args.package}")
    package = wanted[0]
    if not package.builds_for(world):
        # Not a failure of the proposal: a recipe is tried everywhere it would
        # be built, and declaring that it is not built somewhere is an answer.
        notice(f"{package.name} is not built for {world.name}, nothing to try")
        return

    print(f"Trying {package.name} {package.version} for {world.arch} "
          f"against {world.core}", flush=True)
    output_dir = args.output_dir or tempfile.mkdtemp(prefix="vitasdk-try-")
    with group(f"[{world.arch}] {package.name} {package.version}"):
        produced = build.try_build(package, world, snapshot.packages_dir, sdk,
                                   source_date_epoch(snapshot.packages_dir),
                                   snapshot.staging_assets, output_dir)
    notice(f"{package.name} {package.version} builds: " + ", ".join(produced))
    report_contents(package, world, produced, output_dir, snapshot.staging_assets)


def report_contents(package: Any, world: Any, produced: list[str], output_dir: str,
                    assets: list[gh.Asset]) -> None:
    """Says what the proposed build would change in the installed files.

    Building is only half the question. A release that quietly stops shipping
    a header still builds, and it is the packages that link against it that
    break, later and somewhere else.
    """

    published = {asset.filename: asset for asset in assets}
    for name in produced:
        after = build.file_list(os.path.join(output_dir, name))
        previous = next((published[f] for pattern in package.build_patterns(world)
                         for f in sorted(fnmatch.filter(published, pattern))
                         if f != name), None)
        if previous is None:
            with group(f"{name} installs {len(after)} path(s)"):
                print("\n".join(after), flush=True)
            continue
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, previous.filename)
            gh.download_asset(previous, path)
            before = build.file_list(path)
        added, removed = build.compare_contents(before, after)
        with group(f"{name} against {previous.filename}: "
                   f"+{len(added)} -{len(removed)}"):
            for path in added:
                print(f"+ {path}", flush=True)
            for path in removed:
                print(f"- {path}", flush=True)
        if removed:
            print(f"::warning::{name} no longer installs {len(removed)} path(s) "
                  f"that the published package does", flush=True)


# ---------------------------------------------------------------- supervise

def drop_stale_dependents(snapshot: state.Snapshot, dry_run: bool) -> int:
    """Removes packages that were built before something they link against."""

    if not config.REBUILD_DEPENDENTS:
        return 0

    by_name = {asset.filename: asset for asset in snapshot.staging_assets}
    staging = state.staging_release()
    dropped = 0
    for world in config.worlds():
        stale = queue.find_stale_packages(snapshot.packages, snapshot.built_at, world)
        if not stale:
            continue
        with group(f"[{world.arch}] rebuilding {len(stale)} package(s) "
                   f"built before their dependencies"):
            for package in sorted(stale, key=lambda p: p.name):
                for name in queue.asset_names(package, world, by_name):
                    print(f"{package.name}: dropping {name}", flush=True)
                    if not dry_run:
                        gh.delete_asset(staging.repo, by_name[name])
                    dropped += 1
    return dropped


def queue_is_drained(packages: list[Package]) -> bool:
    """Whether any world still has something a worker could pick up.

    Packages held back by a dependent, or waiting on one that failed, are not
    queued: nothing a worker does moves them, and waiting for them would mean
    never publishing again after the first failure.
    """

    return not any(build_plan.queued_in(packages, world) for world in config.worlds())


def snapshot_dispatch_inputs(series: str) -> dict[str, str]:
    """Inputs for the snapshot.yml dispatch.

    No buildscripts revision is tracked against a core pin, so that one stays
    empty rather than guessed.
    """

    return {"series": series, "buildscripts_revision": ""}


def cmd_supervise(args: Any) -> None:
    repo = gh.get_current_repo()

    for world in state.enforce_core_pin(dry_run=args.dry_run):
        notice(f"Staged packages for {world.arch} dropped: rebuilding against {world.core}")

    snapshot = state.get_queue_with_status(full_details=True,
                                           create_releases=not args.dry_run)
    if drop_stale_dependents(snapshot, args.dry_run) and not args.dry_run:
        snapshot = state.get_queue_with_status(full_details=True)

    report.show_queue(snapshot.packages)
    if not args.dry_run:
        update_status(snapshot)

    plan = build_plan.create_build_plan(snapshot.packages, image_tags(snapshot.packages_dir))
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
        snapshot = state.get_queue_with_status(full_details=True)
        update_status(snapshot)
        if run.get("status") == "completed":
            notice(f"Workers finished: {run.get('conclusion')}")
            break

    # A round that emptied the queue is a round worth publishing. One that did
    # not will be followed by another, and cutting a snapshot per round would
    # name a catalogue that is still moving.
    if not queue_is_drained(snapshot.packages):
        notice("Packages are still queued: the next round picks them up")
        return
    gh.dispatch_workflow(repo, SNAPSHOT_WORKFLOW, args.target_branch,
                         snapshot_dispatch_inputs(config.ACTIVE_SERIES))
    notice("Queue drained: asked for a snapshot")


# ---------------------------------------------------------------- repository

def cmd_snapshot(args: Any) -> None:
    snapshot = state.get_queue_with_status()
    include_blocked = args.staging or args.include_blocked
    work_dir = args.work_dir or tempfile.mkdtemp(prefix="vitasdk-repo-")

    # One repository per world, side by side. Their file names differ by
    # architecture, so they never collide and a client picks the one it wants.
    outputs: dict[str, str] = {}
    total = 0
    for world in config.worlds():
        packages = repository.selectable(snapshot.packages, world, include_blocked)
        if not packages:
            notice(f"[{world.arch}] nothing publishable, skipped")
            continue
        assets = repository.select_assets(packages, world, snapshot.staging_assets)
        name = world.staging_repository if args.staging else world.repository
        packages_dir = os.path.join(work_dir, world.arch, "packages")
        output_dir = os.path.join(work_dir, world.arch, name)
        repository.download(assets, packages_dir)
        repository.create_database(snapshot.packages_dir, packages_dir, output_dir,
                                   source_date_epoch(snapshot.packages_dir), name)
        outputs[world.arch] = output_dir
        total += len(packages)
        notice(f"[{world.arch}] repository with {len(packages)} package(s)")

    if not outputs:
        raise SystemExit("ERROR: no finished packages to publish")

    if args.staging:
        staging = state.staging_release()
        for output_dir in outputs.values():
            for entry in sorted(os.listdir(output_dir)):
                if entry.endswith((".db", ".files", ".db.tar.gz", ".files.tar.gz")):
                    gh.upload_asset(staging, entry,
                                    path=os.path.join(output_dir, entry), replace=True)
        notice("Staging repository index updated")
        return

    combined = os.path.join(work_dir, "release")
    os.makedirs(combined, exist_ok=True)
    for output_dir in outputs.values():
        for entry in sorted(os.listdir(output_dir)):
            os.replace(os.path.join(output_dir, entry), os.path.join(combined, entry))
    repository.write_provenance(combined, snapshot.packages_revision,
                                args.buildscripts_revision)
    # Travels with the snapshot so the channel manifest can carry it, and the
    # client can say something at the moment somebody installs one of these.
    obsolete = deprecations(snapshot.packages)
    with open(os.path.join(combined, "deprecated.json"), "w", encoding="utf-8") as handle:
        json.dump(obsolete, handle, indent=2, sort_keys=True)
        handle.write("\n")
    if obsolete:
        notice(f"{len(obsolete)} deprecated package(s) recorded in the snapshot")
    if args.no_publish:
        return
    publish_snapshot(combined, total, config.ACTIVE_SERIES,
                     config.default_world().arch, args.buildscripts_revision)


def request_channel_manifest(snapshot_tag: str, series: str, world: str,
                             buildscripts_revision: str) -> None:
    """Asks for a signed channel manifest naming this snapshot.

    The signing key lives in the repository that produces the manifest, so
    this asks rather than signs. Optional in the same way as the website
    notification: without a token the snapshot is published all the same and
    the manifest is generated by hand.
    """

    if not config.CHANNEL_REPO:
        return
    token = os.environ.get("CHANNEL_TOKEN", "")
    if not token:
        print("::notice::No CHANNEL_TOKEN set: publish the channel manifest by hand",
              flush=True)
        return
    # Read directly rather than through the checked accessor: this function
    # runs after a snapshot has been published and must not be able to fail
    # the command that published it.
    here = os.environ.get("GITHUB_REPOSITORY", "")
    core = config.default_world().core
    channel = series or "nightly"
    try:
        gh.dispatch_repository(config.CHANNEL_REPO, config.CHANNEL_EVENT, token, {
            "core_snapshot": core,
            "packages_snapshot": snapshot_tag,
            "packages_repository": here,
            "channel": channel,
            "world": world,
            "buildscripts_sha": buildscripts_revision,
        })
        print(f"Asked {config.CHANNEL_REPO} for a channel manifest naming "
              f"{snapshot_tag} on the {channel} channel", flush=True)
    except gh.GitHubError as e:
        # The snapshot is published and immutable either way; the manifest can
        # be generated later without rebuilding anything.
        print(f"::warning::could not ask for a channel manifest: {e}. Regenerate it "
              f"by hand by running channel.yml's workflow_dispatch in "
              f"{config.CHANNEL_REPO} with core_release={core}, "
              f"packages_release={snapshot_tag}, channel={channel}.", flush=True)


def deprecations(packages: Any) -> dict[str, str]:
    """The packages nobody should start something new on, and why.

    Published with the snapshot so the channel can carry it: warning about
    this at install time is the only moment anybody is listening.
    """

    return {package.name: package.deprecated
            for package in sorted(packages, key=lambda p: p.name)
            if package.deprecated}


def publish_snapshot(output_dir: str, package_count: int, series: str, world: str,
                     buildscripts_revision: str) -> str:
    repo = gh.get_current_repo()
    tag = (config.snapshot_prefix() + time.strftime("%Y%m%d", time.gmtime())
           + f".{os.environ.get('GITHUB_RUN_NUMBER', '0')}"
           + f".{os.environ.get('GITHUB_RUN_ATTEMPT', '1')}")

    release = gh.get_release(repo, tag)
    for entry in sorted(os.listdir(output_dir)):
        path = os.path.join(output_dir, entry)
        if os.path.isfile(path):
            gh.upload_asset(release, entry, path=path, replace=True)
    notice(f"Published {tag} with {package_count} package(s)")
    request_channel_manifest(tag, series, world, buildscripts_revision)
    return tag


# ---------------------------------------------------------------- cleanup

def cmd_clean_assets(args: Any) -> None:
    snapshot = state.get_queue_with_status()
    keep = {config.LEGACY_CORE_MARKER, "SHA256SUMS"}
    patterns = []
    for world in config.worlds():
        keep.add(world.core_marker)
        for name in (world.staging_repository, world.repository):
            keep.update({f"{name}.db", f"{name}.files",
                         f"{name}.db.tar.gz", f"{name}.files.tar.gz"})
        for package in snapshot.packages:
            if not package.builds_for(world):
                continue
            patterns.extend(package.build_patterns(world))
            keep.add(package.failed_name(world))
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


# ---------------------------------------------------------------- core pin

def cmd_bump_core(args: Any) -> None:
    """Points a world at a newer core, for a human to review and merge.

    A new core is published every night. Taking it means rebuilding that
    world's entire catalogue, so this writes the change and stops: what
    decides when that happens is a merge, not a notification.
    """

    world = config.world_by_arch(args.world) if args.world else config.default_world()
    path = os.path.join(os.path.dirname(os.path.abspath(config.__file__)), "config.py")
    with open(path, encoding="utf-8") as handle:
        original = handle.read()

    if world.core == args.core:
        notice(f"{world.name} is already pinned to {args.core}")
        return

    try:
        updated = recipes.set_core(original, world.name, args.core)
    except ValueError as e:
        raise SystemExit(f"ERROR: {e}")

    with open(path, "w", encoding="utf-8") as handle:
        handle.write(updated)

    # Read it back the way every command will: a configuration that no longer
    # imports would be found by the next run instead of by this one.
    #
    # The compiled cache has to go first. Python validates it by modification
    # time and size, and two snapshot tags are usually the same length, so a
    # rewrite within the same second is indistinguishable from no rewrite at
    # all and the child would read the previous value back.
    cache = os.path.join(os.path.dirname(path), "__pycache__")
    shutil.rmtree(cache, ignore_errors=True)
    # Selecting the series first is what makes this read the world that was
    # just written. Worlds are looked up by architecture within a series, and
    # every series has a "vita" one, so a child that never picks a series
    # looks up the unnamed world and reports nightly's core -- which is how a
    # correct write came back as a mismatch the first time a named series was
    # ever bumped.
    result = subprocess.run(
        ["python3", "-B", "-c",
         "from vitasdk_autobuild import config; "
         f"config.select_series({world.series!r}); "
         f"print(config.world_by_arch({world.arch!r}).core)"],
        capture_output=True, text=True, cwd=os.path.dirname(os.path.dirname(path)))
    if result.returncode != 0 or result.stdout.strip() != args.core:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(original)
        raise SystemExit(f"ERROR: the configuration did not take the new core: "
                         f"{(result.stderr or result.stdout).strip()}")

    notice(f"{world.arch}: {world.core} -> {args.core}")


# ---------------------------------------------------------------- recipes

def cmd_update_recipes(args: Any) -> None:
    """Pins recipes that follow a branch to the commit being served today."""

    if not recipes.have_git():
        raise SystemExit("ERROR: git is required to resolve upstream repositories")

    packages_dir = state.packages_checkout()
    cache = recipes.recipe_cache_dir(state.cache_root())
    makepkg = srcinfo.find_vita_makepkg()

    # In order of how much they claim: pinning a loose source states nothing
    # about upstream, taking today's commit takes what happens to be there,
    # and taking a release takes what upstream decided to call finished. The
    # first one that applies to a recipe is the one proposed, so a package
    # cannot be pinned and advanced in the same breath.
    planners = {
        "pin": recipes.plan_update,
        "advance": recipes.plan_advance,
        "release": recipes.plan_release,
    }
    wanted = [kind for kind in ("pin", "advance", "release")
              if kind in (args.propose or ["pin"])]

    updates: list[recipes.Update] = []
    problems: list[str] = []
    for info in srcinfo.collect(packages_dir):
        name = info["pkgbase"]
        if args.only and not fnmatch.fnmatch(name, args.only):
            continue
        package_dir = os.path.join(packages_dir, info["repo_path"])
        update = None
        for kind in wanted:
            try:
                update = planners[kind](package_dir, info, cache)
            except (ValueError, subprocess.CalledProcessError) as e:
                problems.append(str(e))
                break
            if update is not None and update.changed:
                break
            update = None
        if update is None:
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
        notice(f"{len(updates)} recipe(s) would change; pass --write to apply")
    else:
        notice(f"{len(updates)} recipe(s) rewritten in {packages_dir}")
    if problems:
        raise SystemExit(f"{len(problems)} recipe(s) could not be updated")
