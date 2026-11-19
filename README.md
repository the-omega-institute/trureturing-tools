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

Pull requests and pushes to `dev` and `main` build and test the tools, with a downloadable Actions artifact retained for 14 days. To publish a preview, dispatch the release workflow from `dev` and choose `dev`, a feature branch, or a pull request. Run these commands in the tools checkout.

```sh
gh workflow run release.yml --ref dev -f source=dev
gh workflow run release.yml --ref dev -f source=pr/123
```

The preview tag contains the channel, run number and source commit. Set the repository variable `TOOLS_RELEASE_SERIES` to choose the version under development (default `0.1.0`). In the downstream trial PR, pin the preview's exact tag and the SHA-256 of `trureturing-tools.tar.gz`. The release notes and the GitHub asset metadata carry that digest.

After downstream CI accepts the candidate, promote it to `main` and run the stable workflow on that branch. Set `preview` to the tested preview tag and `version` to the stable tag.

```sh
gh workflow run stable.yml --ref main -f preview="$preview" -f version="$version"
```

Promotion verifies the preview asset digest and compares its bytes with the package from the selected `main` commit. It publishes the tested asset under the stable tag.
