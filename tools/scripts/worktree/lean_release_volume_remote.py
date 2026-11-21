"""GitHub Releases transport for directly addressed, immutable physical volumes."""

import gzip
import hashlib
import json
import os
from pathlib import Path
import tempfile
import subprocess

import lean_release_index as index
from lean_cache_release_io import archive_sha, MultipartArchiveOutput
import lean_release_volumes as volumes

SCHEMA = "lean-release-seed-v6"


def namespace(partition, key, verification, identity, host):
    address = host.prefix(partition, key, verification, indexed="volumes")
    # The protected production and verification workflows have separate writer groups.
    # A local publisher cannot participate in either hosted lock.
    return address + ("ci-" if identity.startswith("ci-") else "local-")


def volume_prefix(namespace):
    return "lean-volume-v1-" + hashlib.sha256(namespace.encode()).hexdigest() + "-"


def checked_asset(metadata, tag, digest, size):
    assets = metadata.get("assets", [])
    if len(assets) != 1:
        raise ValueError("physical volume Release is not complete: " + tag)
    asset = assets[0]
    if (asset.get("name") != digest or asset.get("size") != size or asset.get("digest") != "sha256:" + digest
            or type(asset.get("id")) is not int or asset["id"] <= 0):
        raise ValueError("physical volume identity or size mismatch: " + tag)
    return asset


def asset_for(host, tag, digest, size, deadline):
    metadata = host.existing_release(tag, deadline)
    if metadata is None or metadata["draft"]:
        return None
    return checked_asset(metadata, tag, digest, size)


def ensure_asset(host, tag, digest, size, source, deadline):
    metadata = host.existing_release(tag, deadline)
    if metadata is not None and not metadata["draft"]:
        checked_asset(metadata, tag, digest, size)
        return False
    if metadata is None:
        if source is None:
            raise ValueError("prior physical volume is unavailable: " + digest)
        host.gh(deadline, "release", "create", tag, "--repo", host.REPO, "--draft",
                "--title", tag, "--notes", "Immutable Lean cache volume.")
    uploaded = False
    if metadata is not None and metadata.get("assets"):
        checked_asset(metadata, tag, digest, size)
    else:
        if source is None:
            raise ValueError("missing bytes for incomplete physical volume: " + digest)
        with tempfile.TemporaryDirectory(dir=source.parent) as temp:
            payload = Path(temp) / digest
            os.link(source, payload)
            host.gh(deadline, "release", "upload", tag, str(payload), "--repo", host.REPO)
            uploaded = True
    host.gh(deadline, "release", "edit", tag, "--repo", host.REPO, "--draft=false", "--latest=false")
    if asset_for(host, tag, digest, size, deadline) is None:
        raise ValueError("uploaded physical volume is unavailable: " + digest)
    return uploaded


def load_tree(host, tag, stage, manifest, deadline):
    descriptor = manifest["index"]
    host.gh(deadline, "release", "download", tag, "--repo", host.REPO, "--dir", str(stage),
            "--pattern", descriptor["name"])
    path = stage / descriptor["name"]
    if path.stat().st_size != descriptor["bytes"] or archive_sha(path, deadline) != descriptor["sha256"]:
        raise ValueError("Release volume index checksum or size mismatch")
    return volumes.read_tree(path, manifest)


def published_base(host, scope, partition, key, verification, deadline):
    prefix = host.prefix(partition, key, verification, indexed="volumes")
    for record in host.release_tags(deadline):
        tag = record["tagName"]
        if (not record["isDraft"] and tag.startswith(scope)
                and host.valid_publication_id(tag[len(prefix):])):
            with tempfile.TemporaryDirectory(prefix="lean-volume-base-") as temp:
                stage = Path(temp)
                _, manifest, _ = host.snapshot_manifest(partition, tag, stage, deadline,
                    verification=verification, cache_key=key)
                return manifest, load_tree(host, tag, stage, manifest, deadline)
    return None


def stage_publication(host, stage, contents, partition, cache_key, identity, verification, deadline):
    scope = namespace(partition, cache_key, verification, identity["publication_id"], host)
    previous = published_base(host, scope, partition, cache_key, verification, deadline)
    rows = index.read_index(stage / volumes.INDEX, contents["archive_bytes"])
    delta = stage / "delta"
    delta.mkdir()
    tree, packed, parts, written, compact = volumes.materialize_delta(stage, contents, rows, previous,
                                                                      host.CHUNK_BYTES, delta, deadline)
    new = {part["sha256"]: part for part in parts}
    uploads = []
    for digest, size in tree["volumes"].items():
        physical = volume_prefix(scope) + digest
        source = packed / new[digest]["name"] if digest in new else None
        if ensure_asset(host, physical, digest, size, source, deadline):
            uploads.append(digest)
    # Retire the complete v5 staging copy before verification reads the published object.
    for base, entries in ((stage, contents["parts"]), (packed, parts)):
        for part in entries:
            (base / part["name"]).unlink(missing_ok=True)
    with (stage / volumes.INDEX).open("wb") as target:
        with gzip.GzipFile(filename="", fileobj=target, mode="wb", mtime=0) as encoded:
            encoded.write(volumes.json_bytes(tree))
    host.receipt("volume-upload", "saved", new_volumes=uploads,
                 payload_bytes=sum(tree["volumes"][name] for name in uploads),
                 reused_bytes=contents["archive_bytes"] - written, compacted=compact)
    return {**{k: contents[k] for k in ("lake_inputs_sha256", "archive_bytes", "archive_sha256")},
            "volume_scope": scope, "index": {"name": volumes.INDEX, "bytes": (stage / volumes.INDEX).stat().st_size,
                                             "sha256": archive_sha(stage / volumes.INDEX, deadline)}}


def ranges(tree, rows):
    places = {row["offset"]: loc for row, loc in zip(tree["members"], tree["locations"], strict=True)}
    pending = None
    for row in rows:
        for digest, start, count in places[row["offset"]]:
            if pending and pending[0] == digest and pending[1] + pending[2] == start:
                pending[2] += count
            else:
                if pending:
                    yield pending
                pending = [digest, start, count]
    if pending:
        yield pending


def download(host, manifest, tree, rows, output, deadline, assets):
    calls = total = 0
    for digest, start, size in ranges(tree, rows):
        if digest not in assets:
            tag = volume_prefix(manifest["volume_scope"]) + digest
            assets[digest] = asset_for(host, tag, digest, tree["volumes"][digest], deadline)
            if assets[digest] is None:
                raise ValueError("missing physical volume: " + tag)
        host.download_range(assets[digest], start, size, output, deadline)
        calls += 1
        total += size
    return calls, total


def download_full(host, stage, manifest, tree, deadline):
    with MultipartArchiveOutput(stage, index.ASSET, host.CHUNK_BYTES, deadline) as output:
        download(host, manifest, tree, tree["members"], output, deadline, {})
    actual = output.metadata()
    if any(actual[key] != manifest[key] for key in ("archive_bytes", "archive_sha256")):
        raise ValueError("reconstructed physical volumes disagree with the logical snapshot")
    return actual["parts"]


def prune(host, partition, key, tag, identity, deadline):
    # Local writers have no hosted concurrency guarantee and leave retention to their owner.
    if os.environ.get("GITHUB_ACTIONS") != "true" or not identity.startswith("ci-"):
        return 0, None
    scope = namespace(partition, key, False, identity, host)
    root_prefix = host.prefix(partition, key, indexed="volumes")
    pool = volume_prefix(scope)
    removed = 0
    try:
        snapshots, candidates = [], []
        for item in host.release_tags(deadline):
            name = item["tagName"]
            if name.startswith(scope) and host.valid_publication_id(name[len(root_prefix):]):
                if item["isDraft"]:
                    raise ValueError("unfinished volume snapshot; retention deferred")
                snapshots.append(name)
            elif name.startswith(pool) and index.valid_digest(name[len(pool):]) and not item["isDraft"]:
                candidates.append(name)
        if tag not in snapshots[:5]:
            raise ValueError("new snapshot is not among the retained publications")
        live = set()
        for current in snapshots[:5]:
            with tempfile.TemporaryDirectory(prefix="lean-volume-gc-") as temp:
                stage = Path(temp)
                _, m, _ = host.snapshot_manifest(partition, current, stage, deadline, cache_key=key)
                live.update(load_tree(host, current, stage, m, deadline)["volumes"])
        if not live.issubset({physical[len(pool):] for physical in candidates}):
            raise ValueError("retained snapshot references an unavailable physical volume")
        for old in snapshots[5:]:
            host.gh(deadline, "release", "delete", old, "--repo", host.REPO, "--yes", "--cleanup-tag")
            removed += 1
        for physical in candidates:
            if physical[len(pool):] not in live:
                host.gh(deadline, "release", "delete", physical, "--repo", host.REPO, "--yes", "--cleanup-tag")
        return removed, None
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        return removed, str(error)
