"""Deciding how many workers to dispatch.

Workers are generic: none of them owns a package. They pull from the same
queue until it is empty, so the job count follows the depth of the queue and
not the size of the catalogue. Adding a library never changes the workflow.
"""

import shlex
from typing import Any, Iterable

from . import config
from .queue import Package, PackageStatus

# Each worker walks the queue from a different end so that two of them are
# unlikely to pick the same package at the same moment.
START_POSITIONS = ["start", "end", "middle"]

# Packages per worker. Low enough that a handful of queued packages still get
# some parallelism, high enough that a full rebuild does not ask for more
# runners than the queue can keep busy.
PACKAGES_PER_JOB = 14


def job_count(queued: int) -> int:
    if queued <= 0:
        return 0
    needed = (queued + PACKAGES_PER_JOB - 1) // PACKAGES_PER_JOB
    return max(1, min(config.MAXIMUM_JOB_COUNT, needed))


def create_build_plan(packages: Iterable[Package], image_tag: str) -> list[dict[str, Any]]:
    """The matrix handed to the worker workflow."""

    queued = sum(1 for package in packages
                 if package.status == PackageStatus.WAITING_FOR_BUILD)
    jobs = []
    for index in range(job_count(queued)):
        name = "build" if index == 0 else f"build-{index + 1}"
        position = START_POSITIONS[index % len(START_POSITIONS)]
        jobs.append({
            "name": name,
            "runner": config.RUNNER_LABELS,
            "image-tag": image_tag,
            "build-args": shlex.join(["--build-from", position]),
        })
    return jobs
