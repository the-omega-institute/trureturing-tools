# Trureturing tools

This repository supplies the StrataLint CLI, Lean inspectors, scripts and tests used by trureturing. Each GitHub Release contains a source archive named `trureturing-tools.tar.gz`. The consumer pins its release tag and SHA-256 in `Meta/tools-release.json` and installs the archive under `tools/`.

## Build and test

Use Git, Python 3.12 or newer, and the .NET SDK selected by `global.json`.

```sh
make -C tools dotnet
dotnet test tools/tests/Trureturing.Truth.Tests --configuration Release --no-build
```

The solution builds independently. Repository integration checks read the consumer's `Meta/`, `Blueprint/`, Git history and Lean inputs. Run them in the trureturing checkout with the candidate release installed.

## Package and release

```sh
python3 package_release.py --revision HEAD
```

The command writes `build/trureturing-tools.tar.gz` and prints its SHA-256. It reads regular files and executable bits from the selected Git tree in sorted path order, with archive timestamps and ownership set to zero. It includes the repository license as `tools/LICENSE`.

Each push to `dev` builds the solution, runs the standalone truth tests and publishes a GitHub prerelease named `v0.1.0-dev.<run_number>` for the checked commit. Re-running the same workflow run uses the same tag and compares the published archive with the verified build. Pull requests run the build and tests.

To publish a stable release, push a version tag such as `v1.0.0` at the commit verified by the consumer. Each release includes the archive and its SHA-256 checksum in the release notes. Update the consumer's tag and digest together to adopt it.
