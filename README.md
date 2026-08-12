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

## The core SDK is pinned

`CORE_SNAPSHOT` in `config.py` names the core every staged package is built
against. Changing it wipes the staging area, so a published snapshot is never
a mixture of cores and `provenance.json` is exact. It only changes by commit,
which is what removes the nightly full rebuild.

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

## Setting it up

1. A branch named `build-branch` tracking `main`. The supervisor dispatches
   workers onto it so a push to `main` cannot change a build already running.
2. `RECIPES_TOKEN`, only for `update-recipes.yml`, which opens a pull request
   against the recipe repository. Everything else uses `GITHUB_TOKEN`.
3. Nothing else. There is no service to host and no key to rotate.

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
