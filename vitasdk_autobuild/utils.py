"""Small helpers shared by the commands."""

import os
import re
import subprocess
import sys
from collections.abc import Generator, Iterable, Sequence
from contextlib import contextmanager


def is_running_in_gha() -> bool:
    return os.environ.get("GITHUB_ACTIONS") == "true"


@contextmanager
def group(title: str) -> Generator[None, None, None]:
    """Collapsible log section when running under GitHub Actions."""

    if is_running_in_gha():
        print(f"::group::{title}", flush=True)
    else:
        print(title, flush=True)
    try:
        yield
    finally:
        if is_running_in_gha():
            print("::endgroup::", flush=True)


def notice(message: str) -> None:
    if is_running_in_gha():
        print(f"::notice::{message}", flush=True)
    else:
        print(message, flush=True)


def error(message: str) -> None:
    if is_running_in_gha():
        print(f"::error::{message}", flush=True)
    else:
        print(f"ERROR: {message}", file=sys.stderr, flush=True)


def table(headers: Sequence[str], rows: Iterable[Sequence[object]]) -> str:
    """Render a plain text table, so the tool needs no third party code."""

    text_rows = [[str(cell) for cell in row] for row in rows]
    if not text_rows:
        return "(none)"
    widths = [len(h) for h in headers]
    for row in text_rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    lines = [
        "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)).rstrip(),
        "  ".join("-" * w for w in widths),
    ]
    for row in text_rows:
        lines.append("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
    return "\n".join(lines)


def run(args: Sequence[str], **kwargs: object) -> subprocess.CompletedProcess:
    """subprocess.run() that fails loudly and echoes what it runs.

    The echo goes to stderr: commands whose output a caller reads must leave
    stdout carrying nothing but the answer.
    """

    printable = " ".join(str(a) for a in args)
    print(f"$ {printable}", file=sys.stderr, flush=True)
    return subprocess.run([str(a) for a in args], check=True, **kwargs)  # type: ignore[arg-type]


def clean_environ(environ: dict[str, str]) -> dict[str, str]:
    """Strip CI variables so package recipes cannot read our credentials.

    Recipes are arbitrary code. Even though they come from a repository we
    trust, there is no reason for them to see the token the worker uses to
    upload the results.
    """

    cleaned = dict(environ)
    for key in list(cleaned):
        if key.startswith(("GITHUB_", "RUNNER_", "ACTIONS_")) or key in ("GH_TOKEN", "CI"):
            del cleaned[key]
    return cleaned


def sanitize_tag(name: str) -> str:
    """Turn a git ref or release tag into something usable as a docker tag."""

    return re.sub(r"[^a-zA-Z0-9._-]", "-", name)[:128]
