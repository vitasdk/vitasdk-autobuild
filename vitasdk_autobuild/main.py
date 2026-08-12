"""Command line entry point."""

import argparse
import sys

from . import commands, config


def parse_override(text: str) -> tuple[str, object]:
    key, separator, value = text.partition("=")
    if not separator:
        raise argparse.ArgumentTypeError(f"expected KEY=VALUE, got {text!r}")
    key = key.strip()
    value = value.strip()
    current = getattr(config, key, None)
    if isinstance(current, bool):
        return key, value.lower() in ("1", "true", "yes")
    if isinstance(current, int) and not isinstance(current, bool):
        return key, int(value)
    if isinstance(current, list):
        return key, [item for item in value.split(",") if item]
    return key, value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vitasdk-autobuild",
        description="Build the VitaSDK package catalogue from vitasdk/packages")
    parser.add_argument(
        "-o", "--set", action="append", default=[], metavar="KEY=VALUE",
        help="override a configuration value, e.g. -o CORE_SNAPSHOT=sdk-snapshot-...")
    parser.set_defaults(func=lambda args: parser.print_help())
    subparsers = parser.add_subparsers(title="commands")

    show = subparsers.add_parser("show", help="print the build queue")
    show.set_defaults(func=commands.cmd_show)

    worker = subparsers.add_parser(
        "build", help="build packages from the queue until it is empty")
    worker.add_argument("--world", help="which world to build for, by architecture")
    worker.add_argument("--build-from", choices=["start", "middle", "end"], default="start",
                        help="which end of the queue this worker starts from")
    worker.set_defaults(func=commands.cmd_build)

    supervise = subparsers.add_parser(
        "supervise", help="plan, dispatch and follow a round of builds")
    supervise.add_argument("--target-branch", required=True,
                           help="branch the worker workflow runs on")
    supervise.add_argument("--poll-interval", type=int, default=120,
                           help="seconds between status refreshes")
    supervise.add_argument("--dry-run", action="store_true",
                           help="show the plan without dispatching anything")
    supervise.set_defaults(func=commands.cmd_supervise)

    tag = subparsers.add_parser(
        "image-tag", help="print the tag of the build image the workers need")
    tag.set_defaults(func=commands.cmd_image_tag)

    status = subparsers.add_parser("update-status", help="refresh status.json")
    status.set_defaults(func=commands.cmd_update_status)

    snapshot = subparsers.add_parser(
        "snapshot", help="build a pacman repository from the staged packages")
    snapshot.add_argument("--staging", action="store_true",
                          help="update the index of the staging repository instead "
                               "of publishing a snapshot release")
    snapshot.add_argument("--include-blocked", action="store_true",
                          help="also include packages held back by their dependents")
    snapshot.add_argument("--no-publish", action="store_true",
                          help="build the repository but do not create a release")
    snapshot.add_argument("--work-dir", help="where to assemble the repository")
    snapshot.add_argument("--buildscripts-revision", default="",
                          help="recorded in provenance.json")
    snapshot.set_defaults(func=commands.cmd_snapshot)

    bump = subparsers.add_parser(
        "bump-core",
        help="point a world at a newer core snapshot, for review")
    bump.add_argument("--core", required=True, help="the core snapshot tag to pin")
    bump.add_argument("--world", help="which world, by architecture")
    bump.set_defaults(func=commands.cmd_bump_core)

    update = subparsers.add_parser(
        "update-recipes",
        help="pin recipes that follow a git branch to the commit served today")
    update.add_argument("--write", action="store_true",
                        help="edit the recipes instead of only reporting")
    update.add_argument("--only", help="restrict to recipes matching this glob")
    update.add_argument("--propose", action="append",
                        choices=["pin", "advance", "release"],
                        help="which proposals to make (default: pin only)")
    update.set_defaults(func=commands.cmd_update_recipes)

    try_build = subparsers.add_parser(
        "try-build", help="build one package from a proposed recipe, uploading nothing")
    try_build.add_argument("--package", required=True, help="which recipe to try")
    try_build.add_argument("--world", default="", help="which world, by architecture")
    try_build.set_defaults(func=commands.cmd_try_build)

    clean = subparsers.add_parser(
        "clean-assets", help="delete staged files no recipe asks for any more")
    clean.add_argument("--dry-run", action="store_true")
    clean.set_defaults(func=commands.cmd_clean_assets)

    clear = subparsers.add_parser("clear-failed", help="forget failure markers")
    clear.add_argument("--pattern", help="only markers matching this glob")
    clear.add_argument("--dry-run", action="store_true")
    clear.set_defaults(func=commands.cmd_clear_failed)

    return parser


def main(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv[1:])
    if args.set:
        config.apply_overrides(dict(parse_override(item) for item in args.set))
    args.func(args)
    return 0


def run() -> int:
    return main(sys.argv)
