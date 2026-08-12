"""A small GitHub REST client built on the standard library.

The autobuilder runs inside the package build image, which has no pip
environment and no business growing one. Everything here is urllib.
"""

import base64
import calendar
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, BinaryIO

from . import config

API_ROOT = os.environ.get("GITHUB_API_URL", "https://api.github.com")
UPLOAD_ROOT = os.environ.get("GITHUB_UPLOAD_URL", "https://uploads.github.com")
USER_AGENT = "vitasdk-autobuild"

SAFE_ASSET_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")


class GitHubError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"{status}: {message}")
        self.status = status
        self.message = message


class _NoAuthRedirect(urllib.request.HTTPRedirectHandler):
    """Drops the Authorization header when a redirect leaves the API host.

    Asset downloads redirect to object storage, which rejects a request that
    carries both our token and its own signature.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urllib.parse.urlparse(newurl).netloc != urllib.parse.urlparse(req.full_url).netloc:
            new.headers = {k: v for k, v in new.headers.items() if k.lower() != "authorization"}
        return new


_opener = urllib.request.build_opener(_NoAuthRedirect())


def get_token(write: bool = False) -> str:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    if not token and write:
        raise SystemExit("ERROR: GITHUB_TOKEN is required for write operations")
    return token


def get_current_repo() -> str:
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo:
        raise SystemExit(
            "ERROR: GITHUB_REPOSITORY is not set. Set it to owner/name of the "
            "autobuild repository.")
    return repo


def get_snapshot_repo() -> str:
    return config.SNAPSHOT_REPO or get_current_repo()


def _request(method: str, url: str, *, data: bytes | BinaryIO | None = None,
             headers: dict[str, str] | None = None, accept: str = "application/vnd.github+json",
             length: int | None = None, retries: int = 4) -> tuple[int, dict[str, str], bytes]:
    all_headers = {
        "Accept": accept,
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = get_token()
    if token:
        all_headers["Authorization"] = f"Bearer {token}"
    if headers:
        all_headers.update(headers)
    if length is not None:
        all_headers["Content-Length"] = str(length)

    last_error: Exception | None = None
    for attempt in range(retries):
        if attempt:
            time.sleep(min(2 ** attempt, 30))
            # A retried upload has to start from the beginning of the file,
            # or it sends nothing and the asset is silently truncated.
            if hasattr(data, "seek"):
                data.seek(0)  # type: ignore[union-attr]
        request = urllib.request.Request(url, data=data, method=method, headers=all_headers)
        try:
            with _opener.open(request, timeout=120) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as e:
            body = e.read()
            if e.code in (403, 429) and _wait_for_rate_limit(dict(e.headers)):
                last_error = e
                continue
            if e.code >= 500 or e.code == 408:
                last_error = e
                continue
            try:
                parsed = json.loads(body)
                message = parsed.get("message", "")
                codes = [item.get("code", "") for item in parsed.get("errors", [])]
                if codes:
                    message = f"{message} ({', '.join(code for code in codes if code)})"
            except ValueError:
                message = body.decode("utf-8", "replace")
            raise GitHubError(e.code, f"{method} {url}: {message}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if data is not None and not isinstance(data, bytes):
                raise
            last_error = e
            continue

    raise GitHubError(0, f"{method} {url}: giving up after {retries} attempts: {last_error}")


def _wait_for_rate_limit(headers: dict[str, str]) -> bool:
    """Sleeps until the rate limit resets. Returns False if this was not one."""

    lower = {k.lower(): v for k, v in headers.items()}
    retry_after = lower.get("retry-after")
    if retry_after and retry_after.isdigit():
        wait = int(retry_after)
    elif lower.get("x-ratelimit-remaining") == "0" and lower.get("x-ratelimit-reset", "").isdigit():
        wait = int(lower["x-ratelimit-reset"]) - int(time.time()) + 5
    else:
        return False
    wait = max(0, min(wait, 3600))
    print(f"Rate limited, waiting {wait}s", flush=True)
    time.sleep(wait)
    return True


def api(method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
    url = path if path.startswith("http") else f"{API_ROOT}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"} if data else None
    status, _, body = _request(method, url, data=data, headers=headers)
    if status == 204 or not body:
        return None
    return json.loads(body)


def api_list(path: str) -> list[Any]:
    """Follows pagination and returns every item."""

    url = path if path.startswith("http") else f"{API_ROOT}{path}"
    separator = "&" if "?" in url else "?"
    url = f"{url}{separator}per_page=100"
    items: list[Any] = []
    while url:
        _, headers, body = _request("GET", url)
        page = json.loads(body)
        items.extend(page if isinstance(page, list) else [page])
        url = _next_link(headers)
    return items


def _next_link(headers: dict[str, str]) -> str:
    link = {k.lower(): v for k, v in headers.items()}.get("link", "")
    for part in link.split(","):
        match = re.match(r'\s*<([^>]+)>\s*;\s*rel="next"', part)
        if match:
            return match.group(1)
    return ""


@dataclass(frozen=True)
class Asset:
    id: int
    name: str
    label: str
    size: int
    state: str
    uploader: str
    uploader_type: str
    url: str
    download_url: str
    created_at: float = 0.0

    @property
    def filename(self) -> str:
        """The real file name, which the label carries when it is not the name."""

        return self.label or self.name

    @property
    def complete(self) -> bool:
        return self.state == "uploaded"


@dataclass(frozen=True)
class Release:
    id: int
    tag: str
    repo: str


def asset_upload_name(filename: str) -> tuple[str, str]:
    """Returns the (name, label) to upload a file under.

    GitHub silently rewrites characters it does not like in asset names, which
    would break the round trip back to a pacman file name. Names that survive
    unchanged are kept readable, so the staging release doubles as a pacman
    repository; anything else is encoded and recovered from the label.
    """

    if not filename:
        raise ValueError("asset name cannot be empty")
    if SAFE_ASSET_NAME.match(filename):
        return filename, filename
    encoded = base64.urlsafe_b64encode(filename.encode()).decode().rstrip("=")
    return encoded + ".bin", filename


def parse_timestamp(value: str) -> float:
    """GitHub's ISO 8601 timestamps, as seconds since the epoch."""

    if not value:
        return 0.0
    return calendar.timegm(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ"))


def _parse_asset(data: dict[str, Any]) -> Asset:
    uploader = data.get("uploader") or {}
    return Asset(
        id=data["id"],
        name=data["name"],
        label=data.get("label") or "",
        size=data.get("size", 0),
        state=data.get("state", ""),
        uploader=uploader.get("login", ""),
        uploader_type=uploader.get("type", ""),
        url=data["url"],
        download_url=data["browser_download_url"],
        created_at=parse_timestamp(data.get("created_at", "")),
    )


def get_release(repo: str, tag: str, create: bool = True) -> Release:
    try:
        data = api("GET", f"/repos/{repo}/releases/tags/{urllib.parse.quote(tag)}")
    except GitHubError as e:
        if e.status != 404 or not create:
            raise
        get_token(write=True)
        data = api("POST", f"/repos/{repo}/releases", {
            "tag_name": tag,
            "name": tag,
            "body": f"Managed by vitasdk-autobuild. Do not edit by hand.",
            "prerelease": True,
        })
    return Release(id=data["id"], tag=data["tag_name"], repo=repo)


def find_releases(repo: str, prefix: str) -> list[str]:
    """Tags of every release whose name starts with prefix, newest first."""

    tags = [r["tag_name"] for r in api_list(f"/repos/{repo}/releases")
            if r["tag_name"].startswith(prefix) and not r.get("draft")]
    return sorted(tags, reverse=True)


def get_assets(release: Release, include_incomplete: bool = False) -> list[Asset]:
    assets = []
    for raw in api_list(f"/repos/{release.repo}/releases/{release.id}/assets"):
        asset = _parse_asset(raw)
        if not asset.complete and not include_incomplete:
            continue
        if not _is_trusted_uploader(asset):
            raise SystemExit(
                f"ERROR: asset '{asset.filename}' was uploaded by "
                f"'{asset.uploader}', which is not allowed. Aborting.")
        assets.append(asset)
    return assets


def _is_trusted_uploader(asset: Asset) -> bool:
    if asset.uploader_type == "Bot" and asset.uploader == "github-actions[bot]":
        return True
    return asset.uploader in config.ALLOWED_UPLOADERS


def upload_asset(release: Release, filename: str, *, path: str | None = None,
                 content: bytes | None = None, replace: bool = False) -> None:
    assert (path is None) != (content is None), "pass exactly one of path/content"
    get_token(write=True)
    name, label = asset_upload_name(filename)

    for existing in get_assets(release, include_incomplete=True):
        if existing.name != name:
            continue
        # An incomplete asset is worse than no asset: it makes the package look
        # present without being downloadable.
        if replace or not existing.complete:
            delete_asset(release.repo, existing)
            break
        print(f"Skipping upload of {filename}, already present", flush=True)
        return

    query = urllib.parse.urlencode({"name": name, "label": label})
    url = f"{UPLOAD_ROOT}/repos/{release.repo}/releases/{release.id}/assets?{query}"
    try:
        if content is not None:
            _request("POST", url, data=content, length=len(content),
                     headers={"Content-Type": "application/octet-stream"})
        else:
            assert path is not None
            size = os.path.getsize(path)
            with open(path, "rb") as handle:
                _request("POST", url, data=handle, length=size,
                         headers={"Content-Type": "application/octet-stream"})
    except GitHubError as e:
        # Workers do not coordinate, so two of them can build the same package
        # and race to upload it. Losing that race is not a failure: the file
        # the queue was waiting for is there either way.
        if e.status == 422 and "already_exists" in e.message:
            print(f"{filename} was uploaded by another worker first", flush=True)
            return
        raise
    print(f"Uploaded {filename} to {release.tag}", flush=True)


def delete_asset(repo: str, asset: Asset) -> None:
    get_token(write=True)
    try:
        api("DELETE", f"/repos/{repo}/releases/assets/{asset.id}")
    except GitHubError as e:
        if e.status != 404:
            raise


def download_asset(asset: Asset, target: str) -> None:
    directory = os.path.dirname(target)
    if directory:
        os.makedirs(directory, exist_ok=True)
    _, _, body = _request("GET", asset.url, accept="application/octet-stream")
    if len(body) != asset.size and asset.size:
        raise GitHubError(0, f"short read for {asset.filename}: "
                             f"{len(body)} of {asset.size} bytes")
    temporary = target + ".part"
    with open(temporary, "wb") as handle:
        handle.write(body)
    os.replace(temporary, target)


def download_asset_text(asset: Asset) -> str:
    _, _, body = _request("GET", asset.url, accept="application/octet-stream")
    return body.decode("utf-8", "replace")


def dispatch_workflow(repo: str, workflow: str, ref: str, inputs: dict[str, str]) -> None:
    get_token(write=True)
    api("POST", f"/repos/{repo}/actions/workflows/{workflow}/dispatches",
        {"ref": ref, "inputs": inputs})


def find_dispatched_run(repo: str, workflow: str, branch: str, after: float) -> int | None:
    """Newest run of a workflow created after the given timestamp."""

    runs = api("GET", f"/repos/{repo}/actions/workflows/{workflow}/runs"
                      f"?branch={urllib.parse.quote(branch)}&event=workflow_dispatch&per_page=20")
    for run in runs.get("workflow_runs", []):
        if parse_timestamp(run["created_at"]) >= after - 60:
            return int(run["id"])
    return None


def get_run(repo: str, run_id: int) -> dict[str, Any]:
    return api("GET", f"/repos/{repo}/actions/runs/{run_id}")


def get_run_jobs(repo: str, run_id: int) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    url = f"{API_ROOT}/repos/{repo}/actions/runs/{run_id}/jobs?per_page=100"
    while url:
        _, headers, body = _request("GET", url)
        jobs.extend(json.loads(body).get("jobs", []))
        url = _next_link(headers)
    return jobs


def get_current_run_urls() -> dict[str, str]:
    """Links identifying the job we run in, for failure reports."""

    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    if not repo or not run_id:
        return {}
    urls = {"build": f"{server}/{repo}/actions/runs/{run_id}"}
    job_id = os.environ.get("JOB_CHECK_RUN_ID", "")
    if job_id:
        urls["job"] = f"{urls['build']}/job/{job_id}"
    return urls
