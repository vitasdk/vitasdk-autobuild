"""Deciding how many workers to dispatch, and for which world.

Workers are generic within a world: none of them owns a package. They pull
from the same queue until it is empty, so the job count follows the depth of
the queue and not the size of the catalogue. Adding a library never changes
the workflow, and adding a world only adds jobs.
"""

import shlex
from typing import Any, Iterable

from . import config
from .config import World
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


def queued_in(packages: Iterable[Package], world: World) -> int:
    return sum(1 for package in packages
               if package.builds_for(world)
               and package.get_status(world) == PackageStatus.WAITING_FOR_BUILD)


def create_build_plan(packages: Iterable[Package], image_tags: dict[str, str],
                      worlds: Iterable[World] | None = None) -> list[dict[str, Any]]:
    """The matrix handed to the worker workflow, across every world."""

    packages = list(packages)
    configured = list(worlds) if worlds is not None else config.worlds()

    jobs: list[dict[str, Any]] = []
    for world in configured:
        count = job_count(queued_in(packages, world))
        for index in range(count):
            # The world is part of the name so two worlds never share a
            # concurrency group and the logs say which is which.
            suffix = "" if index == 0 else f"-{index + 1}"
            position = START_POSITIONS[index % len(START_POSITIONS)]
            jobs.append({
                "name": f"{world.name}{suffix}",
                "runner": config.RUNNER_LABELS,
                "image-tag": image_tags[world.arch],
                # Ahead of the subcommand, because which series a run drives
                # decides which store it reads before it does anything.
                "series-args": (shlex.join(["--series", world.series])
                                if world.series else ""),
                "build-args": shlex.join(["--world", world.arch,
                                          "--build-from", position]),
            })
    return jobs[:config.MAXIMUM_JOB_COUNT]
