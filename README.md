# Trureturing tools

This repository supplies the StrataLint CLI, Lean inspectors, scripts and tests used by trureturing. Each GitHub Release contains a source archive named `trureturing-tools.tar.gz`. The consumer pins its release tag and SHA-256 in `Meta/tools-release.json` and installs the archive under `tools/`.

## Build and test

Use Git, Python 3.12 or newer, and the .NET SDK selected by `global.json`.

```sh
make -C tools dotnet
python3 .github/scripts/test-suites.py standalone
```

The solution builds independently. Repository integration checks read the consumer's `Meta/`, `Blueprint/`, Git history and Lean inputs. Run them in the trureturing checkout with the candidate release installed.

CI runs the standalone suites on the tool checkout. For suites that read the
consumer's Makefile, FILEMAP, CI contract or Lean inputs, CI packages the
exact candidate and pins its SHA-256 in a fixed consumer checkout. The real
consumer installer verifies and installs the archive before CI runs the
registered units and additional candidate test projects.

The consuming repository checks its own current source, FILEMAP and scoped
Lean evidence against the actual PR or push.

## Package and release

```sh
python3 package_release.py --revision HEAD
```

The command writes `build/trureturing-tools.tar.gz` and prints its SHA-256. It reads regular files and executable bits from the selected Git tree in sorted path order, with archive timestamps and ownership set to zero. It includes the repository license as `tools/LICENSE`.

Every branch push and pull request tests its head commit and packages it after all suites pass. Actions retains the archive for 14 days. To publish a preview, dispatch the release workflow from `dev` and select `dev`, a feature branch, or a pull request. The workflow rebuilds the archive from the selected Git objects using the packager on `dev` and compares it byte for byte with the successful CI artifact. Run these commands in the tools checkout.

```sh
gh workflow run release.yml --ref dev -f source=dev
gh workflow run release.yml --ref dev -f source=my-feature
gh workflow run release.yml --ref dev -f source=pr/123
```

To refresh an expired artifact, rerun the pull request CI or use `gh workflow run ci.yml --ref my-feature` for a branch.

The preview tag contains the channel, run number and source commit. Set the repository variable `TOOLS_RELEASE_SERIES` to choose the version under development (default `0.1.0`). In a downstream trial checkout, run `make install-tools`, then `python3 tools/consumer/pin.py <preview-tag>`. Commit the updated `Meta/tools-release.json` and workflow callers together. The pin command checks the public archive's SHA-256 and matches its reusable workflows to the release commit. Use the same command with a stable tag after promotion.

After downstream CI accepts the candidate, promote it to `main` and run the stable workflow on that branch. Set `preview` to the tested preview tag and `version` to the stable tag.

```sh
gh workflow run stable.yml --ref main -f preview="$preview" -f version="$version"
```

Promotion verifies the preview asset digest and compares its bytes with the package from the selected `main` commit. It publishes the tested asset under the stable tag.
