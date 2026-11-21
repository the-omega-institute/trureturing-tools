"""GitHub Releases transport for directly addressed, immutable physical volumes."""

import gzip
import hashlib
import json
import os
import re
from pathlib import Path
import tempfile
import subprocess

import lean_release_index as index
from lean_cache_release_io import archive_sha, remaining
import lean_release_volumes as volumes

SCHEMA = "lean-release-seed-v6"


def namespace(partition, key, verification, identity, host, *, test_id=None, source_ref=None):
    address = host.prefix(partition, key, verification, indexed="volumes")
    # The protected production and verification workflows have separate writer groups.
    # A local publisher cannot participate in either hosted lock.
    address += "ci-" if identity.startswith("ci-") else "local-"
    if test_id is not None:
        if (not verification or not identity.startswith("ci-") or not isinstance(test_id, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", test_id)
                or not isinstance(source_ref, str) or not re.fullmatch(r"refs/heads/integration-[A-Za-z0-9._/-]+", source_ref)):
            raise ValueError("test volume scope requires a named protected integration publication")
        address += "test-" + hashlib.sha256(volumes.json_bytes([source_ref, test_id])).hexdigest()
    return address


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


def ensure_asset(host, tag, digest, size, source, deadline, *, serialized=False):
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
    assets = metadata.get("assets", []) if metadata is not None else []
    if serialized and len(assets) == 1 and assets[0].get("state") == "starter":
        starter = assets[0]
        if (starter.get("name") != digest or type(starter.get("size")) is not int or starter["size"] != 0
                or starter.get("digest") not in (None, "", "sha256:" + hashlib.sha256(b"").hexdigest())
                or type(starter.get("id")) is not int or starter["id"] <= 0
                or not re.fullmatch(r"lean-volume-v1-[0-9a-f]{64}-" + re.escape(digest), tag)
                or source is None or source.stat().st_size != size or archive_sha(source, deadline) != digest):
            raise ValueError("incomplete physical volume does not match the staged upload")
        host.gh(deadline, "api", "--method", "DELETE", f"repos/{host.REPO}/releases/assets/{starter['id']}")
        assets = []
    if assets:
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
    family = prefix + ("ci-" if scope.startswith(prefix + "ci-") else "local-")
    for record in host.release_tags(deadline):
        tag = record["tagName"]
        if (not record["isDraft"] and tag.startswith(family)
                and host.valid_publication_id(tag[len(prefix):])):
            with tempfile.TemporaryDirectory(prefix="lean-volume-base-") as temp:
                stage = Path(temp)
                _, manifest, _ = host.snapshot_manifest(partition, tag, stage, deadline,
                    verification=verification, cache_key=key)
                if manifest["volume_scope"] == scope:
                    return manifest, load_tree(host, tag, stage, manifest, deadline)
    return None


def stage_publication(host, stage, contents, partition, cache_key, identity, verification, deadline):
    test_id = os.environ.get("LEAN_CACHE_VOLUME_TEST_ID") or None
    if os.environ.get("LEAN_CACHE_VOLUME_RETIRE_TAG") and not test_id:
        raise ValueError("retiring an integration snapshot requires a test volume scope")
    scope = namespace(partition, cache_key, verification, identity["publication_id"], host,
                      test_id=test_id, source_ref=identity.get("source_ref"))
    previous = published_base(host, scope, partition, cache_key, verification, deadline)
    rows = index.read_index(stage / volumes.INDEX, contents["archive_bytes"])
    delta = stage / "delta"
    delta.mkdir()
    tree, packed, parts, written, compact, peak = volumes.materialize_delta(stage, contents, rows, previous,
                                                                      host.CHUNK_BYTES, delta, deadline)
    new = {part["sha256"]: part for part in parts}
    uploads = []
    for digest, size in tree["volumes"].items():
        physical = volume_prefix(scope) + digest
        source = packed / new[digest]["name"] if digest in new else None
        if ensure_asset(host, physical, digest, size, source, deadline,
                        serialized=(os.environ.get("GITHUB_ACTIONS") == "true" and identity["publication_id"].startswith("ci-"))):
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
                 reused_bytes=contents["archive_bytes"] - written, compacted=compact, peak_staged_payload_bytes=peak)
    return {**{k: contents[k] for k in ("lake_inputs_sha256", "archive_bytes", "archive_sha256")},
            **({"volume_test_id": test_id} if test_id else {}), "volume_scope": scope, "index": {"name": volumes.INDEX, "bytes": (stage / volumes.INDEX).stat().st_size,
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
    """Read each physical volume once and scatter its live ranges to the logical archive."""
    by_volume = {digest: [] for digest in tree["volumes"]}
    for row, places in zip(tree["members"], tree["locations"], strict=True):
        dest = row["offset"]
        for digest, start, size in places:
            by_volume[digest].append((start, dest, size))
            dest += size
    logical = stage / index.ASSET
    transferred = 0
    with logical.open("xb") as output:
        output.truncate(manifest["archive_bytes"])
        for digest, spans in by_volume.items():
            size = tree["volumes"][digest]
            tag = volume_prefix(manifest["volume_scope"]) + digest
            if asset_for(host, tag, digest, size, deadline) is None:
                raise ValueError("missing physical volume: " + tag)
            with deadline.pause():
                host.gh(None, "release", "download", tag, "--repo", host.REPO, "--dir", str(stage),
                        "--pattern", digest)
            path = stage / digest
            if path.stat().st_size != size or archive_sha(path, deadline) != digest:
                raise ValueError("physical volume checksum or size mismatch")
            with path.open("rb") as payload:
                for start, dest, count in sorted(spans):
                    payload.seek(start)
                    output.seek(dest)
                    while count:
                        remaining(deadline)
                        block = payload.read(min(count, 1024 * 1024))
                        if not block:
                            raise ValueError("truncated physical volume")
                        output.write(block)
                        count -= len(block)
            path.unlink()
            transferred += size
    if archive_sha(logical, deadline) != manifest["archive_sha256"]:
        raise ValueError("reconstructed physical volumes disagree with the logical snapshot")
    host.receipt("volume-download", "complete", payload_requests=len(by_volume), payload_bytes=transferred,
                 logical_bytes=manifest["archive_bytes"],
                 staged_payload_bound=manifest["archive_bytes"] + max(tree["volumes"].values()))
    return [{"name": logical.name, "bytes": manifest["archive_bytes"], "sha256": manifest["archive_sha256"]}]


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


def retire_verification(host, partition, key, current_tag, current, old_tag, deadline):
    """Retire solely an explicit predecessor in this integration test's isolated pool."""
    prefix = host.prefix(partition, key, True, indexed="volumes")
    if (os.environ.get("GITHUB_ACTIONS") != "true" or not current.get("volume_test_id")
            or current_tag == old_tag or not old_tag.startswith(prefix + "ci-")
            or not host.valid_publication_id(old_tag[len(prefix):])):
        raise ValueError("invalid verification volume retirement target")
    ref = current["source_ref"]
    scope = current["volume_scope"]
    pool = volume_prefix(scope)
    # Validate the predecessor before any mutation; all other namespaces are read-only.
    with tempfile.TemporaryDirectory(prefix="lean-volume-retire-") as temp:
        stage = Path(temp)
        _, previous, _ = host.snapshot_manifest(partition, old_tag, stage, deadline, verification=True, cache_key=key)
        if (previous["volume_scope"] != scope or previous.get("volume_test_id") != current["volume_test_id"]
                or previous.get("source_ref") != ref or int(previous["workflow_run_id"]) > int(current["workflow_run_id"])
                or (previous["workflow_run_id"] == current["workflow_run_id"] and
                    int(previous["workflow_run_attempt"]) >= int(current["workflow_run_attempt"]))):
            raise ValueError("predecessor does not belong to the same verification test and source ref")
        historical = host.publisher_run(previous["workflow_run_id"], previous["workflow_run_attempt"],
                                       ".github/workflows/ci-publication-verify.yml",
                                       lambda path: json.loads(host.gh(deadline, "api", path)))
        if (historical["head_sha"] != previous["producer_commit_sha"]
                or "refs/heads/" + historical["head_branch"] != ref):
            raise ValueError("retired tag's origin differs from its protected verification run")
        old = load_tree(host, old_tag, stage, previous, deadline)
    live = set()
    seen = set()
    for entry in host.release_tags(deadline):
        name = entry["tagName"]
        if not name.startswith(prefix + "ci-") or not host.valid_publication_id(name[len(prefix):]):
            continue
        if name == old_tag:
            seen.add(name)
            continue
        if entry["isDraft"]:
            # An incomplete draft cannot reliably expose its test scope. Retain its
            # possible physical volumes, while still retiring only the explicit predecessor.
            live.update(old["volumes"])
            continue
        with tempfile.TemporaryDirectory(prefix="lean-volume-mark-") as temp:
            stage = Path(temp)
            _, manifest, _ = host.snapshot_manifest(partition, name, stage, deadline,
                                                    verification=True, cache_key=key)
            if manifest["volume_scope"] == scope:
                live.update(load_tree(host, name, stage, manifest, deadline)["volumes"])
                seen.add(name)
    if not {current_tag, old_tag}.issubset(seen):
        raise ValueError("both verification tags must be visible before retirement")
    host.gh(deadline, "release", "delete", old_tag, "--repo", host.REPO, "--yes", "--cleanup-tag")
    if host.existing_release(old_tag, deadline) is not None:
        raise ValueError("retired verification snapshot is still visible")
    reclaimed = 0
    for digest, size in old["volumes"].items():
        if digest not in live:
            if asset_for(host, pool + digest, digest, size, deadline) is None:
                raise ValueError("retired predecessor volume is unavailable")
            host.gh(deadline, "release", "delete", pool + digest, "--repo", host.REPO, "--yes", "--cleanup-tag")
            reclaimed += size
    host.receipt("volume-retire", "retired", tag=old_tag, retained_tag=current_tag, source_ref=ref,
                 test_id=current["volume_test_id"], retired_snapshot_payload_bytes=previous["archive_bytes"],
                 reclaimed_payload_bytes=reclaimed)
