"""Trying the recipes an open pull request proposes.

The build image is private to this repository and a pull request's own token
cannot pull it, so the build cannot run where the proposal is. Nothing pushes
from there either: the recipe repository would need a token able to start
workflows here, which is a key kept in a repository that otherwise holds
none. So this side asks instead, on a timer, and writes the answer back as a
commit status -- which doubles as the record of what has already been tried,
so nothing else has to remember.
"""

from typing import Any, Iterable

# A proposal is a recipe unless it is one of the few directories that are not
# one. Naming those is better than looking for VITABUILD in the changed files:
# a proposal that only touches a patch beside it is still that recipe's.
NOT_RECIPES = frozenset({".github", "scripts", "tests"})

# What a maintainer adds to a proposal from outside to have it built. A recipe
# is a build script, so running one from a fork on the word of whoever opened
# the pull request would hand this builder to anybody.
TRY_LABEL = "try-build"

STATUS_CONTEXT = "vitasdk-autobuild / builds"


def recipes_touched(paths: Iterable[str]) -> list[str]:
    """Which recipes a set of changed files proposes."""

    found = set()
    for path in paths:
        directory, separator, _rest = path.partition("/")
        if separator and directory and directory not in NOT_RECIPES:
            found.add(directory)
    return sorted(found)


def is_from_a_fork(pull: dict[str, Any]) -> bool:
    head = (pull.get("head") or {}).get("repo") or {}
    base = (pull.get("base") or {}).get("repo") or {}
    return head.get("full_name") != base.get("full_name")


def reason_to_skip(pull: dict[str, Any]) -> str:
    """Why this proposal is not built, or empty when it is."""

    if pull.get("draft"):
        return "it is a draft"
    labels = {label.get("name") for label in pull.get("labels", [])}
    if is_from_a_fork(pull) and TRY_LABEL not in labels:
        return f"it comes from a fork and carries no {TRY_LABEL!r} label"
    return ""


def status_context(package: str) -> str:
    return f"{STATUS_CONTEXT} {package}"


def already_tried(statuses: Iterable[dict[str, Any]], package: str) -> bool:
    """Whether this exact commit already has an answer for this package.

    The status is the record, so a proposal that is pushed to again is tried
    again and one that is only commented on is not.
    """

    wanted = status_context(package)
    return any(status.get("context") == wanted
               and status.get("state") in ("success", "failure", "pending")
               for status in statuses)


def pull_ref(pull: dict[str, Any]) -> str:
    """The ref this proposal can be fetched from in the recipe repository.

    A fork's branch is not there, but every pull request's head is, so this
    is the one ref that reads the same for both.
    """

    return f"refs/pull/{pull['number']}/head"
