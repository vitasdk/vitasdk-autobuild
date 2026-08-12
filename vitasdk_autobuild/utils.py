"""Small helpers shared by the commands."""

import os
import pwd
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


def get_build_user() -> str | None:
    """The unprivileged user to run makepkg as, or None if we are not root.

    Two independent reasons to drop privileges: makepkg refuses to run as
    root at all, even to print metadata, and recipes are arbitrary code that
    must not be able to read the token this process holds.
    """

    if os.geteuid() != 0:
        return None
    for name in ("vita", "builder", "nobody"):
        try:
            pwd.getpwnam(name)
        except KeyError:
            continue
        return name
    return None


def as_build_user(command: Sequence[str], environ: dict[str, str],
                  keep: Sequence[str]) -> list[str]:
    """Wraps a command so it runs as the unprivileged user, if we are root.

    env(1) rather than sudo's --preserve-env, because sudoers can reset PATH
    behind our back and makepkg is found through it.
    """

    command = [str(a) for a in command]
    user = get_build_user()
    if user is None:
        return command
    values = dict(environ)
    # Root's home is not writable by the build user, and makepkg and the tools
    # it calls put caches there.
    values["HOME"] = pwd.getpwnam(user).pw_dir
    passed = [f"{name}={values.get(name, '')}" for name in keep]
    return ["sudo", "-u", user, "--", "env", *passed, *command]


def give_to_build_user(path: str) -> None:
    """Makes a directory usable by the unprivileged user, if we are root.

    makepkg writes next to the recipe even when only printing metadata, so a
    checkout owned by root is not enough.
    """

    user = get_build_user()
    if user is not None:
        subprocess.run(["chown", "-R", user, path], check=True)


def trust_git_checkouts() -> None:
    """Lets root use a checkout owned by the build user.

    The recipes have to belong to the unprivileged user, because makepkg
    writes next to them, while the queue is read by this process as root. git
    refuses that combination by default, calling it dubious ownership.
    """

    if os.geteuid() != 0:
        return
    subprocess.run(["git", "config", "--global", "--replace-all",
                    "safe.directory", "*"], check=True)


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
