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

Every branch push and pull request tests its head commit and packages it after all suites pass. Actions retains the archive for 14 days. To publish a preview, dispatch the release workflow from `dev` and select `dev`, a feature branch, or a pull request targeting tools `dev`. The workflow checks the test and package jobs against the `dev` CI contract, including every consumer fixture shard. It rebuilds the archive from the selected Git objects using the packager on `dev` and compares it byte for byte with the archive from that CI run and attempt. Run these commands in the tools checkout.

```sh
gh workflow run release.yml --ref dev -f source=dev
gh workflow run release.yml --ref dev -f source=my-feature
gh workflow run release.yml --ref dev -f source=pr/123
```

To refresh an expired artifact, rerun all jobs in the pull request CI or use `gh workflow run ci.yml --ref my-feature` for a branch.

PR and feature preview tags include the channel, workflow run number, and first 12 digits of the source SHA. A dev preview uses `v<series>-dev.g<full-commit-sha>`. Set the repository variable `TOOLS_RELEASE_SERIES` to choose the version under development (default `0.1.0`).

### Pair a tools PR with a consumer PR

1. On the tools PR branch targeting `dev`, run `python3 tools/consumer/source_pr.py --prepare`. If the current consumer pin's tools source is missing, follow the fetch and merge command it prints, resolve any conflicts, and run `--prepare` again. The head commit records the consumer pin's Git blob ID. Push that head and let tools CI pass before publishing with `gh workflow run release.yml --ref dev -f source=pr/123`.
2. From the tools checkout, run `python3 tools/consumer/pin.py <preview-tag> --consumer /path/to/trureturing`. The command verifies the archive SHA-256 and writes the tag, digest, full source SHA and `previous_pin`. For every changed lock, including legacy, dev and stable releases, `previous_pin` is the 40-character Git blob ID of the current consumer `dev` lock. Repeating the same pin preserves the lock bytes. Open the consumer PR targeting `dev` and merge it after the required checks pass.
3. The scheduled tools workflow picks up the landed pin change, including a change before the final commit of a rebased consumer PR. For immediate delivery or a retry, run `gh workflow run adopt-tools.yml --ref dev -f consumer_pr=456` from the tools checkout. It verifies the final reviewed pin and submits the named tools PR at its exact adopted SHA to GitHub's regular merge API.

If another consumer PR changes the pin first, integrate the new pin's source into the original tools PR and prepare, test, and publish its new head. Resolve the consumer lock conflict using that new preview and rerun the consumer checks. This applies even if the older preview already contained the newly adopted tools source. Unrelated consumer changes leave the preview eligible.

Before the adoption workflow is available on tools `dev`, an authenticated maintainer can deliver an already merged consumer PR from a tools checkout that contains the script. Use the consumer PR's final landed commit to resolve the immutable lock blob. The command uses your regular `gh` authentication and GitHub's normal merge checks.

```sh
consumer_pr=456
consumer_repo=the-omega-institute/trureturing
landed=$(gh api "repos/$consumer_repo/pulls/$consumer_pr" --jq 'select(.merged).merge_commit_sha')
pin_blob=$(gh api "repos/$consumer_repo/contents/Meta/tools-release.json?ref=$landed" --jq .sha)
python3 .github/scripts/adopt-tools.py "$consumer_pr" --pin-blob "$pin_blob"
```

The tools workflow dispatches dev CI after its bot merge. To recover dev publication manually, run `gh workflow run ci.yml --ref dev` and wait for success. Then use `gh workflow run release.yml --ref dev -f source=dev` if the dev release still needs publication.

### Adopt a tools release after its source merges

Use `python3 tools/consumer/source_pr.py --lock /path/to/trureturing/Meta/tools-release.json` to find or submit a tools PR for a legacy preview. With no argument it reads the current consumer `dev` pin. Review and merge the source into tools `dev`, then adopt the resulting dev release using `python3 tools/consumer/pin.py <dev-release-tag>` in the consumer checkout.

To inspect open tools PRs, run `python3 tools/consumer/source_pr.py --list` from a tools Git checkout with Git 2.43 or newer. The command compares each PR's own base-to-head change to the current tools `dev` in a temporary Git repository. It reports ancestry, identical trees, an already-applied change, distinct changes, and conflicts. Ancestry and historical patch matches can survive a later revert. Maintainers assess the present usefulness of each change before closing or merging its PR.

For a stable version, promote the tested source to `main` and run the stable workflow on that branch. Set `preview` to the tested preview tag and `version` to the stable tag.

```sh
gh workflow run stable.yml --ref main -f preview="$preview" -f version="$version"
```

Promotion verifies the preview asset digest and compares its bytes with the package from the selected `main` commit. It publishes the tested asset under the stable tag.
