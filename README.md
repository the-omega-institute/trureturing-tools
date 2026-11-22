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

The tools repository owns the tool build, regression tests and release workflows.
The consuming repository owns CI for its Lean and Scribe source, FILEMAP,
frozen statements, cache publication and Scribe resources. Its workflows
invoke the tools selected by its release lock.

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

The preview tag contains the channel, run number and source commit. Set the repository variable `TOOLS_RELEASE_SERIES` to choose the version under development (default `0.1.0`).

1. Trial the published preview in a consumer checkout with `make install-tools` and `python3 tools/consumer/pin.py <preview-tag>`. The pin command verifies the archive SHA-256 and updates the local `Meta/tools-release.json`.
2. Submit the tested source for tools `dev` review. From the tools checkout, use the explicit lock path for a proposed adoption. With no argument the command reads the lock already committed to `trureturing/dev`.

   ```sh
   python3 tools/consumer/source_pr.py --lock /path/to/trureturing/Meta/tools-release.json
   python3 tools/consumer/source_pr.py
   ```

   The command uses the maintainer's GitHub CLI credentials. It verifies the release digest, resolves the tag to its commit and reports when that commit is already in tools `dev` history. Otherwise it lists an existing open or draft tools-to-`dev` PR at that commit, or a same-repository PR containing it. Review the additional commits in a stacked PR. If no PR qualifies, the command opens one from the exact source SHA. Review and merge through the normal tools checks.
3. After the tools `dev` push passes CI, its release workflow verifies the archive bytes against the tested artifact and publishes the resulting dev package. Pin that package with `python3 tools/consumer/pin.py <dev-release-tag>` in the consumer checkout, then submit the lock to `trureturing/dev`.

To inspect open tools PRs, run `python3 tools/consumer/source_pr.py --list` from a tools Git checkout with Git 2.43 or newer. The command compares each PR's own base-to-head change to the current tools `dev` in a temporary Git repository. It reports ancestry, identical trees, an already-applied change, distinct changes, and conflicts. Ancestry and historical patch matches can survive a later revert. Maintainers assess the present usefulness of each change before closing or merging its PR.

For a stable version, promote the tested source to `main` and run the stable workflow on that branch. Set `preview` to the tested preview tag and `version` to the stable tag.

```sh
gh workflow run stable.yml --ref main -f preview="$preview" -f version="$version"
```

Promotion verifies the preview asset digest and compares its bytes with the package from the selected `main` commit. It publishes the tested asset under the stable tag.
