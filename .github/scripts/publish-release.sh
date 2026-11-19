#!/usr/bin/env bash
set -euo pipefail
channel=$1
asset=$2
preview=false
flags=()
if [[ "$channel" == preview ]]; then
  preview=true
  flags=(--prerelease --latest=false)
elif [[ "$channel" != stable ]]; then
  exit 2
fi
existing=$(git ls-remote --refs origin "refs/tags/$RELEASE_TAG")
if [[ -z "$existing" ]]; then
  git fetch --no-tags --depth=1 origin "$SOURCE_SHA"
  git push origin "$SOURCE_SHA:refs/tags/$RELEASE_TAG"
fi
git fetch --no-tags --depth=1 origin "refs/tags/$RELEASE_TAG"
[[ "$(git rev-parse 'FETCH_HEAD^{commit}')" == "$SOURCE_SHA" ]]
name=$(basename "$asset")
if metadata=$(gh release view "$RELEASE_TAG" --json isDraft,isPrerelease,assets); then
  [[ "$(jq -r '.isPrerelease' <<< "$metadata")" == "$preview" ]]
  draft=$(jq -r '.isDraft' <<< "$metadata")
  if [[ "$draft" == true ]] && ! jq -e --arg name "$name" 'any(.assets[]; .name == $name)' <<< "$metadata" >/dev/null; then
    gh release upload "$RELEASE_TAG" "$asset"
  fi
  gh release download "$RELEASE_TAG" --pattern "$name" --dir "$RUNNER_TEMP/existing"
  cmp "$asset" "$RUNNER_TEMP/existing/$name"
  if [[ "$draft" == true ]]; then gh release edit "$RELEASE_TAG" --draft=false; fi
else
  (cd "$(dirname "$asset")" && sha256sum "$name") > "$RUNNER_TEMP/checksum"
  gh release create "$RELEASE_TAG" "$asset" "${flags[@]}" --verify-tag --title "$RELEASE_TAG" --notes-file "$RUNNER_TEMP/checksum"
fi
