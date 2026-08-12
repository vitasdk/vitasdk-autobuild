"""Reading the pacman database of the last published snapshot.

Knowing what is already in the repository is what separates "this package was
never published" from "this package is published and about to be replaced",
and only the second one can be broken by publishing a dependency alone.
"""

import io
import os
import tarfile

from . import config, gh


def parse_database(data: bytes) -> dict[str, str]:
    """Maps package name to version from a repo-add database."""

    versions: dict[str, str] = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
        for member in archive:
            if not member.isfile() or os.path.basename(member.name) != "desc":
                continue
            handle = archive.extractfile(member)
            if handle is None:
                continue
            fields: dict[str, str] = {}
            key = ""
            for raw in handle.read().decode("utf-8", "replace").splitlines():
                line = raw.strip()
                if line.startswith("%") and line.endswith("%"):
                    key = line.strip("%")
                elif line and key:
                    fields.setdefault(key, line)
            if "NAME" in fields and "VERSION" in fields:
                versions[fields["NAME"]] = fields["VERSION"]
    return versions


def get_published_versions() -> tuple[str, dict[str, dict[str, str]]]:
    """Versions in the newest published snapshot, per world.

    Each world publishes its own repository into the same release, so what is
    already out there has to be read once per world: a package can be
    published for one and never have existed for another.
    """

    repo = gh.get_snapshot_repo()
    tags = gh.find_releases(repo, config.SNAPSHOT_PREFIX)
    if not tags:
        return "", {}
    tag = tags[0]

    release = gh.get_release(repo, tag, create=False)
    assets = {asset.filename: asset for asset in gh.get_assets(release)}
    published: dict[str, dict[str, str]] = {}
    for world in config.worlds():
        database = f"{world.repository}.db"
        asset = assets.get(database)
        if asset is None:
            published[world.arch] = {}
            continue
        path = os.path.join(_cache_dir(), f"{tag}-{database}")
        if not os.path.exists(path):
            gh.download_asset(asset, path)
        with open(path, "rb") as handle:
            published[world.arch] = parse_database(handle.read())
    return tag, published


def _cache_dir() -> str:
    root = os.environ.get("VITASDK_AUTOBUILD_CACHE") or os.path.join(
        os.path.expanduser("~"), ".cache", "vitasdk-autobuild")
    path = os.path.join(root, "repodb")
    os.makedirs(path, exist_ok=True)
    return path
