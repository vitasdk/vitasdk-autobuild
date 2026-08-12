# vitasdk-autobuild

Builds the VitaSDK package catalogue from the recipes in
[vitasdk/packages](https://github.com/vitasdk/packages).

This repository is the scheduler. It **reads** the recipes and never contains
them, so adding a library does not require touching the scheduler and changing
the scheduler does not need library-maintainer review. It is an adaptation of
[msys2-autobuild](https://github.com/msys2/msys2-autobuild) (MIT), whose queue
model, blocking rules and supervisor/worker split it follows.

## How it decides what to build

A package is built when **the file it would produce is not in the staging
release**. The version is part of the file name, so presence is the whole
answer and no database has to be compared:

```
zlib-1.3.2-2-vita.pkg.tar.xz   present  -> nothing to do
zlib-1.3.3-1-vita.pkg.tar.xz   absent   -> build it
```

Metadata comes from `vita-makepkg --printsrcinfo`, run over the real recipes.
Nothing here parses a `VITABUILD`.

Each package ends up in one of these states:

| state | meaning |
|---|---|
| `finished` | built and publishable |
| `finished-but-blocked` | built, but something around it is not, so publishing it alone would leave the repository inconsistent |
| `waiting-for-build` | ready to be picked up by a worker |
| `waiting-for-dependencies` | what it links against is still being built |
| `failed-to-build` | a marker in `staging-failed` says so, and it is not retried until cleared |
| `manual-build-required` | listed in `MANUAL_BUILD` |

Dependents are **rebuilt**, not merely blocked: a package whose file is older
than a file it links against is dropped from staging so a worker picks it up.
Comparing upload times makes that idempotent, since a rebuilt package is no
longer the older one.

## Supervisor and workers

Workers are generic. None of them owns a package: each pulls from the same
queue until it is empty, so the number of jobs follows the depth of the queue
and not the size of the catalogue.

```
build.yml (cron)
  -> image.yml          build image = packages/Dockerfile + pinned core SDK
  -> supervise          plan, dispatch, follow, publish status.json
       -> build-jobs.yml (matrix of N generic workers, on build-branch)
            -> pacman -U the dependency files, vita-makepkg, upload
```

The worker container runs as root **so that the build itself can be dropped to
an unprivileged user**. Recipes are arbitrary code and the job holds a token
that can write to the package store; running the build as another user puts a
kernel boundary between the two.

## Worlds

A world is an architecture, a libc and a toolchain taken together, named by
the target triple. pacman already carries it in a package's `arch` field, so
nothing new was invented: `zlib-1.3.2-2-vita.pkg.tar.xz` belongs to the `vita`
world and could not be confused with another one.

```python
WORLDS = [
    World(arch="vita", core="sdk-snapshot-...", repository="vita",
          triple="arm-vita-eabi"),
]
```

One world is configured. A second toolchain or libc is a second entry, and
everything follows from it: the state of every package is per world, and so
are the file names, the failure markers, the dependency graph, the repository
cut from a snapshot and the status file the catalogue reads.

**The core is per world**, because the core *is* that world's toolchain and
sysroot: autobuilds delivers one complete SDK per world. A worker's world is
therefore decided by the image it runs in, not by a switch inside a shared
SDK, and nothing needs to verify that — a build producing another
architecture would not match the expected file name and fails instead of
uploading something mislabelled.

Bumping one world's core empties only that world's staged packages, so a
snapshot never mixes cores, `provenance.json` is exact, and the other world
stays publishable. It only changes by commit, which is what removes the
nightly full rebuild.

A recipe declares which worlds it supports with the standard `arch` array;
declaring none means all of them. A world whose dependencies do not support it
is pruned, and the pruning propagates, so a package is never built without a
dependency it asked for.

Two worlds are never installed against each other: their files live under
different triples, and on a client each has its own pacman database, which is
what lets both exist in one SDK without pacman treating one as an upgrade of
the other.

## Commands

```sh
python3 -m vitasdk_autobuild show                    # the queue, as tables
python3 -m vitasdk_autobuild supervise --target-branch build-branch
python3 -m vitasdk_autobuild build --build-from start
python3 -m vitasdk_autobuild snapshot                # cut a release
python3 -m vitasdk_autobuild snapshot --staging      # refresh the staging index
python3 -m vitasdk_autobuild update-recipes          # report unpinned git sources
python3 -m vitasdk_autobuild clean-assets
python3 -m vitasdk_autobuild clear-failed --pattern 'zlib-*'
```

Every setting can be overridden without editing the file:

```sh
python3 -m vitasdk_autobuild -o MAXIMUM_JOB_COUNT=2 supervise --target-branch build-branch
```

There are no third-party dependencies: it is Python 3.11 and the standard
library, because the worker runs inside the SDK image and has no pip
environment.

## Releases used as storage

| release | holds |
|---|---|
| `staging` | built packages, the pacman index for them, and the core marker |
| `staging-failed` | one JSON marker per failed build, with links to the log |
| `status` | `status.json`, the only thing the website reads |
| `packages-snapshot-*` | immutable published repositories with `provenance.json` |

`staging` is a usable pacman repository. It can hold partial results of a
rebuild, which is exactly why it is not the published one:

```ini
[vita-staging]
SigLevel = Never
Server = https://github.com/vitasdk/vitasdk-autobuild/releases/download/staging
```

## What happens after a snapshot

A snapshot is a repository, not something a client is pointed at yet. What
points clients at it is a signed channel manifest, and the signing key lives
in `vitasdk/autobuilds` and nowhere else. So cutting a snapshot ends by asking
that repository for a manifest naming it:

```
core-update / build / snapshot        →  packages-snapshot-*   (here, immutable)
                                      →  ask autobuilds for a manifest
                                      →  channels/nightly.json (signed, on Pages)
```

The request carries the snapshot tag, the core it was built against, and this
repository's name, so the manifest records where the packages actually live
rather than assuming it. If the token is missing or the request fails, the
snapshot is published all the same and the manifest can be produced by hand
afterwards; nothing is rebuilt.

## Setting it up

1. A branch named `build-branch` tracking `main`. The supervisor dispatches
   workers onto it so a push to `main` cannot change a build already running.
2. `RECIPES_TOKEN`, only for `update-recipes.yml`, which opens a pull request
   against the recipe repository.
3. Two tokens for talking to other repositories, because a job's own
   `GITHUB_TOKEN` cannot reach outside this one: `WEBSITE_TOKEN` to tell the
   website there is new status, and `CHANNEL_TOKEN` to ask for a channel
   manifest. Both are optional by design — without them the builds and the
   snapshots are unaffected, only the notifications are.
4. Nothing else. There is no service to host, and no signing key here.

## Tests

```sh
python3 -m unittest discover -t . -s tests
VITASDK_AUTOBUILD_SRCINFO_TEST=1 python3 -m unittest tests.test_srcinfo_integration
```

The second one clones the real recipe repository and reads all of them with
the real `vita-makepkg`, which is what proves the queue can be computed on a
runner with no SDK installed.

## Licence

MIT, see [LICENSE](LICENSE). The queue model it adapts comes from
`msys2-autobuild`, also MIT; [NOTICE](NOTICE) records what was taken.
