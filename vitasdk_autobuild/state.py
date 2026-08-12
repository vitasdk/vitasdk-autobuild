"""Assembling the queue from the recipes and from what GitHub holds."""

import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from . import config, gh, queue, repodb
from .config import World
from .queue import Package
from .utils import give_to_build_user, run


def cache_root() -> str:
    root = os.environ.get("VITASDK_AUTOBUILD_CACHE") or os.path.join(
        os.path.expanduser("~"), ".cache", "vitasdk-autobuild")
    os.makedirs(root, exist_ok=True)
    return root


def packages_checkout() -> str:
    """A checkout of the recipe repository, cloned shallow and refreshed.

    The recipes are read from a clone instead of living here, so that adding a
    library never means touching the scheduler.
    """

    override = os.environ.get("PACKAGES_DIR")
    if override:
        return override

    path = os.path.join(cache_root(), "packages")
    url = f"https://github.com/{config.PACKAGES_REPO}.git"
    branch = os.environ.get("PACKAGES_BRANCH") or config.PACKAGES_BRANCH
    if not os.path.exists(os.path.join(path, ".git")):
        run(["git", "clone", "--depth", "1", "--branch", branch, url, path])
    else:
        run(["git", "-C", path, "fetch", "--depth", "1", "origin", branch])
        run(["git", "-C", path, "checkout", "--quiet", "--force", "FETCH_HEAD"])
        run(["git", "-C", path, "clean", "-xfdq"])
    give_to_build_user(path)
    return path


def packages_revision(path: str) -> str:
    result = subprocess.run(["git", "-C", path, "rev-parse", "HEAD"],
                            capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else ""


def staging_release(create: bool = True) -> gh.Release:
    return gh.get_release(gh.get_current_repo(), config.STAGING_RELEASE, create=create)


def failed_release(create: bool = True) -> gh.Release:
    return gh.get_release(gh.get_current_repo(), config.FAILED_RELEASE, create=create)


def status_release(create: bool = True) -> gh.Release:
    return gh.get_release(gh.get_current_repo(), config.STATUS_RELEASE, create=create)


def assets_of(release_getter, create: bool) -> list[gh.Asset]:
    """Assets of a release, treating a missing one as empty.

    Reading must not have side effects: a command that only reports state has
    no business creating the release it was going to read.
    """

    try:
        return gh.get_assets(release_getter(create=create))
    except gh.GitHubError as e:
        if e.status == 404 and not create:
            return []
        raise


@dataclass
class Snapshot:
    """Everything a command needs about the current state, fetched once."""

    packages: list[Package]
    staging_assets: list[gh.Asset]
    failed_assets: list[gh.Asset]
    packages_dir: str
    packages_revision: str
    published_tag: str

    @property
    def built_at(self) -> dict[str, float]:
        return {asset.filename: asset.created_at for asset in self.staging_assets}


def assets_of_world(assets: list[gh.Asset], world: World) -> list[gh.Asset]:
    """Assets belonging to one world, by the architecture in their name."""

    import fnmatch
    patterns = (f"*-{world.arch}.pkg.tar.*", f"*-{world.arch}.failed", world.core_marker,
                f"{world.staging_repository}.*", f"{world.repository}.*")
    return [a for a in assets
            if any(fnmatch.fnmatch(a.filename, p) for p in patterns)]


def get_queue_with_status(full_details: bool = False,
                          create_releases: bool = True) -> Snapshot:
    """The build queue, with every package's state resolved.

    With create_releases off nothing is written at all, which is what lets a
    dry run and a plain report leave the repository exactly as they found it.
    """

    packages_dir = packages_checkout()
    packages = queue.build_queue(packages_dir)

    staging_assets = assets_of(staging_release, create_releases)
    failed_assets = assets_of(failed_release, create_releases)
    done_names = [a.filename for a in staging_assets]
    failed_names = [a.filename for a in failed_assets]

    failed_urls: dict[str, dict[str, str]] = {}
    if full_details:
        # One request per failure marker, so only when the result is shown.
        with ThreadPoolExecutor(8) as executor:
            texts = executor.map(gh.download_asset_text, failed_assets)
            for asset, text in zip(failed_assets, texts):
                try:
                    urls = json.loads(text).get("urls", {})
                except ValueError:
                    urls = {}
                if urls:
                    failed_urls[asset.filename] = urls

    published_tag, repo_versions = repodb.get_published_versions()
    for package in packages:
        for name in package.binaries:
            if name in repo_versions:
                package.repo_version = repo_versions[name]
                break

    queue.apply_status(packages, done_names, failed_names, failed_urls)
    return Snapshot(
        packages=packages,
        staging_assets=staging_assets,
        failed_assets=failed_assets,
        packages_dir=packages_dir,
        packages_revision=packages_revision(packages_dir),
        published_tag=published_tag,
    )


def get_core_marker(world: World, create: bool = True) -> str:
    """The core a world's staged packages were built against."""

    assets = {a.filename: a for a in assets_of(staging_release, create)}
    asset = assets.get(world.core_marker)
    if asset is None and world is config.default_world():
        # Written before worlds existed. Reading it keeps a staging area that
        # is already correct from being thrown away on the first run.
        asset = assets.get(config.LEGACY_CORE_MARKER)
    return gh.download_asset_text(asset).strip() if asset is not None else ""


def enforce_core_pin(dry_run: bool = False) -> list[World]:
    """Drops the staged packages of any world whose core pin changed.

    A snapshot must never mix packages built against different cores, so
    bumping a pin is what triggers that world's full rebuild. Only that
    world's files go: the others stay publishable, which is possible because
    every file name carries its architecture.
    """

    reset: list[World] = []
    for world in config.worlds():
        staged = get_core_marker(world, create=not dry_run)
        if staged == world.core:
            continue

        print(f"[{world.arch}] core pin changed: staged against "
              f"{staged or '(nothing)'}, configured {world.core}", flush=True)
        reset.append(world)
        if dry_run:
            continue

        for release in (staging_release(), failed_release()):
            for asset in assets_of_world(
                    gh.get_assets(release, include_incomplete=True), world):
                print(f"Deleting {asset.filename} from {release.tag}", flush=True)
                gh.delete_asset(release.repo, asset)
        if world is config.default_world():
            for asset in gh.get_assets(staging_release(), include_incomplete=True):
                if asset.filename == config.LEGACY_CORE_MARKER:
                    gh.delete_asset(staging_release().repo, asset)

        gh.upload_asset(staging_release(), world.core_marker,
                        content=(world.core + "\n").encode(), replace=True)
    return reset
