"""Assembling the queue from the recipes and from what GitHub holds."""

import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from . import config, gh, queue, repodb
from .queue import Package
from .utils import run


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
    return path


def packages_revision(path: str) -> str:
    result = subprocess.run(["git", "-C", path, "rev-parse", "HEAD"],
                            capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else ""


def staging_release() -> gh.Release:
    return gh.get_release(gh.get_current_repo(), config.STAGING_RELEASE)


def failed_release() -> gh.Release:
    return gh.get_release(gh.get_current_repo(), config.FAILED_RELEASE)


def status_release() -> gh.Release:
    return gh.get_release(gh.get_current_repo(), config.STATUS_RELEASE)


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


def get_queue_with_status(full_details: bool = False) -> Snapshot:
    """The build queue, with every package's state resolved."""

    packages_dir = packages_checkout()
    packages = queue.build_queue(packages_dir)

    staging_assets = gh.get_assets(staging_release())
    failed_assets = gh.get_assets(failed_release())
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


def get_core_marker() -> str:
    """The core snapshot the staged packages were built against."""

    for asset in gh.get_assets(staging_release()):
        if asset.filename == config.CORE_MARKER_ASSET:
            return gh.download_asset_text(asset).strip()
    return ""


def enforce_core_pin(dry_run: bool = False) -> bool:
    """Drops the staging area when the core it was built against changed.

    A snapshot must never mix packages built against different cores, so
    bumping the pin is what triggers a full rebuild. Returns True if anything
    was dropped.
    """

    staged = get_core_marker()
    if staged == config.CORE_SNAPSHOT:
        return False

    print(f"Core pin changed: staged against {staged or '(nothing)'}, "
          f"configured {config.CORE_SNAPSHOT}", flush=True)
    if dry_run:
        return True

    for release in (staging_release(), failed_release()):
        for asset in gh.get_assets(release, include_incomplete=True):
            print(f"Deleting {asset.filename} from {release.tag}", flush=True)
            gh.delete_asset(release.repo, asset)

    gh.upload_asset(staging_release(), config.CORE_MARKER_ASSET,
                    content=(config.CORE_SNAPSHOT + "\n").encode(), replace=True)
    return True
