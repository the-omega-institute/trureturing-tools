"""GitHub Releases transport for directly addressed, immutable physical volumes."""

import hashlib
import io
import shutil
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


def stage_publication(host, root, stage, partition, cache_key, identity, verification, deadline):
    test_id = os.environ.get("LEAN_CACHE_VOLUME_TEST_ID") or None
    if os.environ.get("LEAN_CACHE_VOLUME_RETIRE_TAG") and not test_id:
        raise ValueError("retiring an integration snapshot requires a test volume scope")
    scope = namespace(partition, cache_key, verification, identity["publication_id"], host,
                      test_id=test_id, source_ref=identity.get("source_ref"))
    import lean_release_sparse as sparse

    plan = sparse.read_plan(root)
    if plan is not None:
        prior_stage = stage / "sparse-base"
        prior_stage.mkdir()
        previous = sparse.overlay_base(host, root, prior_stage, partition, cache_key, verification, scope, plan, deadline)
    else:
        previous = published_base(host, scope, partition, cache_key, verification, deadline)
    binding = None
    if old_tag := os.environ.get("LEAN_CACHE_VOLUME_RETIRE_TAG"):
        binding = predecessor_binding(host, partition, cache_key, scope, test_id, identity, old_tag,
                                      previous[0] if previous else None, deadline)
    uploads = []
    confirmed = set()
    pool = volume_prefix(scope)
    serialized = os.environ.get("GITHUB_ACTIONS") == "true" and identity["publication_id"].startswith("ci-")

    def save_part(part, path):
        digest = part["sha256"]
        if ensure_asset(host, pool + digest, digest, part["bytes"], path, deadline, serialized=serialized):
            uploads.append(digest)
        confirmed.add(digest)

    prior_requests = prior_bytes = 0
    if plan is None:
        contents, tree, written, compact, peak = volumes.pack_volumes(
            root, stage, previous, host.CHUNK_BYTES, save_part, deadline)
    else:
        from lean_release_sparse_pack import pack_overlay
        prior_assets = {}
        def read_prior(rows, output):
            nonlocal prior_requests, prior_bytes
            requests, size = download(host, previous[0], previous[1], rows, output, deadline, prior_assets)
            prior_requests += requests
            prior_bytes += size
        contents, tree, written, compact, peak = pack_overlay(
            root, stage, previous, plan, host.CHUNK_BYTES, save_part, read_prior, deadline)
    for digest, size in tree["volumes"].items():
        if digest not in confirmed:
            ensure_asset(host, pool + digest, digest, size, None, deadline, serialized=serialized)
    volumes.write_tree(stage / volumes.INDEX, tree)
    host.receipt("volume-upload", "saved", new_volumes=uploads,
                 payload_bytes=sum(tree["volumes"][name] for name in uploads),
                 reused_bytes=contents["archive_bytes"] - written, compacted=compact, peak_staged_payload_bytes=peak,
                 predecessor_requests=prior_requests, predecessor_bytes_read=prior_bytes)
    return {**contents,
            **({"volume_test_id": test_id} if test_id else {}),
            **({"volume_predecessor": binding} if binding else {}), "volume_scope": scope, "index": {"name": volumes.INDEX, "bytes": (stage / volumes.INDEX).stat().st_size,
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


def asset_ranges(host, manifest, tree, rows, deadline, assets):
    for digest, start, size in ranges(tree, rows):
        if digest not in assets:
            tag = volume_prefix(manifest["volume_scope"]) + digest
            assets[digest] = asset_for(host, tag, digest, tree["volumes"][digest], deadline)
            if assets[digest] is None:
                raise ValueError("missing physical volume: " + tag)
        yield assets[digest], start, size


def download(host, manifest, tree, rows, output, deadline, assets):
    calls = total = 0
    for asset, start, size in asset_ranges(host, manifest, tree, rows, deadline, assets):
        host.download_range(asset, start, size, output, deadline)
        calls += 1
        total += size
    return calls, total


def single_volume_restore(stage, root, manifest, tree):
    # v6 records compressed lengths only. On the extraction filesystem, keep
    # one physical volume beside the build as its uncompressed files grow.
    if stage.stat().st_dev == root.stat().st_dev:
        return True
    staging_bytes = manifest["archive_bytes"] + max(tree["volumes"].values())
    return shutil.disk_usage(stage).free < staging_bytes


class LogicalVolumeInput:
    """Read authenticated extents in logical order using one spool and bounded read-ahead."""

    WINDOW = 8 * 1024 * 1024
    WINDOW_BUDGET = 32 * 1024 * 1024

    def __init__(self, host, stage, manifest, tree, deadline):
        self.host, self.stage, self.deadline = host, stage, deadline
        self.prefix = volume_prefix(manifest["volume_scope"])
        self.sizes = tree["volumes"]
        self.window_size = min(self.WINDOW, self.WINDOW_BUDGET // len(self.sizes))
        # A logical interleave can still consume each individual asset forward.
        # Prefetch only bytes that will be consumed before a seek on that asset.
        spans = list(ranges(tree, tree["members"]))
        following = {}
        for span in reversed(spans):
            digest, start, count = span
            end = start + count
            next_start, run_end = following.get(digest, (end, end))
            if next_start != end:
                run_end = end
            span.append(run_end)
            following[digest] = start, run_end
        self.spans = iter(spans)
        self.left = 0
        self.assets = {}
        self.spool = None
        self.windows = {}
        self.requests = self.transferred = self.range_requests = 0

    def window(self, digest, start):
        window = self.windows.get(digest)
        if window is not None and window[0] <= start < window[0] + len(window[1]):
            return memoryview(window[1])[start - window[0]:]
        size = self.sizes[digest]
        if digest not in self.assets:
            if self.spool is not None:
                self.spool.unlink()
            tag = self.prefix + digest
            asset = asset_for(self.host, tag, digest, size, self.deadline)
            if asset is None:
                raise ValueError("missing physical volume: " + tag)
            with self.deadline.pause():
                self.host.gh(None, "release", "download", tag, "--repo", self.host.REPO,
                             "--dir", str(self.stage), "--pattern", digest)
            self.spool = self.stage / digest
            if self.spool.stat().st_size != size or archive_sha(self.spool, self.deadline) != digest:
                raise ValueError("physical volume checksum or size mismatch")
            self.assets[digest] = asset
            self.requests += 1
            self.transferred += size

        count = min(self.run_end - start, self.window_size)
        if self.spool.name == digest:
            with self.spool.open("rb") as source:
                source.seek(start)
                data = source.read(count)
        else:
            # Revisit the same immutable asset ID. The frame and whole-archive
            # digests independently cover the bytes actually passed to extraction.
            output = io.BytesIO()
            with self.deadline.pause():
                self.host.download_range(self.assets[digest], start, count, output, self.host.operation_deadline())
            data = output.getvalue()
            self.requests += 1
            self.range_requests += 1
            self.transferred += len(data)
        if len(data) != count:
            raise ValueError("truncated physical volume window")
        self.windows[digest] = (start, data)
        return memoryview(data)

    def read(self, size):
        pieces = []
        while size:
            if not self.left:
                span = next(self.spans, None)
                if span is None:
                    break
                self.digest, self.offset, self.left, self.run_end = span
            block = self.window(self.digest, self.offset)[:min(self.left, size)]
            pieces.append(block)
            self.offset += len(block)
            self.left -= len(block)
            size -= len(block)
        return b"".join(pieces)


def restore_one_volume(host, stage, manifest, tree, destination, deadline):
    with tempfile.TemporaryDirectory(dir=stage) as temporary:
        reader = LogicalVolumeInput(host, Path(temporary), manifest, tree, deadline)
        index.restore_stream(reader, manifest, tree["members"], destination, deadline)
    host.receipt("volume-download", "complete", payload_requests=reader.requests, payload_bytes=reader.transferred,
                 range_requests=reader.range_requests, logical_bytes=manifest["archive_bytes"],
                 staged_payload_bound=max(tree["volumes"].values()))


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


def check_workflow(host, manifest, ref, deadline):
    run = host.publisher_run(manifest["workflow_run_id"], manifest["workflow_run_attempt"],
                             ".github/workflows/ci-publication-verify.yml",
                             lambda path: json.loads(host.gh(deadline, "api", path)))
    if (run["head_sha"] != manifest["producer_commit_sha"]
            or "refs/heads/" + run["head_branch"] != ref):
        raise ValueError("snapshot origin differs from its protected verification run")


def valid_binding(bound, tag):
    if (not isinstance(bound, dict) or set(bound) != {"tag", "manifest_sha256", "archive_bytes", "volumes"}
            or bound["tag"] != tag or not index.valid_digest(bound["manifest_sha256"])
            or type(bound["archive_bytes"]) is not int or not 0 < bound["archive_bytes"] <= volumes.VOLUME_BYTES * volumes.MAX_VOLUMES):
        return False
    sizes = bound["volumes"]
    return (isinstance(sizes, dict) and 0 < len(sizes) <= volumes.MAX_VOLUMES
            and all(index.valid_digest(d) and type(n) is int and 0 < n <= volumes.VOLUME_BYTES
                    for d, n in sizes.items()) and sum(sizes.values()) <= 2 * bound["archive_bytes"])


def predecessor_binding(host, partition, key, scope, test_id, identity, old_tag, witness, deadline):
    prefix = host.prefix(partition, key, True, indexed="volumes")
    ref = identity["source_ref"]
    if (os.environ.get("GITHUB_ACTIONS") != "true" or not test_id or not old_tag.startswith(prefix + "ci-")
            or not host.valid_publication_id(old_tag[len(prefix):])):
        raise ValueError("invalid verification volume retirement target")
    run, attempt = map(int, old_tag[len(prefix + "ci-"):].split("-"))
    if (run, attempt) >= (int(identity["workflow_run_id"]), int(identity["workflow_run_attempt"])):
        raise ValueError("retirement requires an earlier verification publication")
    metadata = host.existing_release(old_tag, deadline)
    if metadata is None:
        bound = witness.get("volume_predecessor") if witness else None
        if (not valid_binding(bound, old_tag)
                or witness.get("volume_scope") != scope or witness.get("volume_test_id") != test_id
                or witness.get("source_ref") != ref):
            raise ValueError("missing predecessor has no published binding for this verification test")
        check_workflow(host, witness, ref, deadline)
        return bound
    with tempfile.TemporaryDirectory(prefix="lean-volume-retire-") as temp:
        stage = Path(temp)
        _, prior, _ = host.snapshot_manifest(partition, old_tag, stage, deadline, verification=True,
                                             cache_key=key, metadata=metadata)
        if prior["volume_scope"] != scope or prior.get("volume_test_id") != test_id or prior.get("source_ref") != ref:
            raise ValueError("predecessor does not belong to the same verification test and source ref")
        check_workflow(host, prior, ref, deadline)
        tree = load_tree(host, old_tag, stage, prior, deadline)
        return {"tag": old_tag, "manifest_sha256": archive_sha(stage / host.MANIFEST, deadline),
                "archive_bytes": prior["archive_bytes"], "volumes": tree["volumes"]}


def retire_verification(host, partition, key, current_tag, current, old_tag, deadline):
    """Retire a bound predecessor and recheck the surviving roots after interruptions."""
    prefix = host.prefix(partition, key, True, indexed="volumes")
    ref = current["source_ref"]
    scope = current["volume_scope"]
    bound = current.get("volume_predecessor")
    if not valid_binding(bound, old_tag):
        raise ValueError("predecessor is not bound to the same verification test")
    if current_tag == old_tag or current_tag != prefix + current["publication_id"]:
        raise ValueError("invalid current verification tag")
    checked = predecessor_binding(host, partition, key, scope, current["volume_test_id"],
                                  current, old_tag, current, deadline)
    if checked != bound:
        raise ValueError("retirement predecessor differs from the published binding")
    pool = volume_prefix(scope)
    live = {}
    current_seen = False
    preserve_orphans = False
    for entry in host.release_tags(deadline):
        name = entry["tagName"]
        if name == old_tag or not name.startswith(prefix + "ci-") or not host.valid_publication_id(name[len(prefix):]):
            continue
        if entry["isDraft"]:
            preserve_orphans = True
            continue
        with tempfile.TemporaryDirectory(prefix="lean-volume-mark-") as temp:
            stage = Path(temp)
            _, manifest, _ = host.snapshot_manifest(partition, name, stage, deadline,
                                                    verification=True, cache_key=key)
            if manifest["volume_scope"] == scope:
                live.update(load_tree(host, name, stage, manifest, deadline)["volumes"])
                if name == current_tag:
                    if manifest != current:
                        raise ValueError("published verification snapshot changed before retirement")
                    check_workflow(host, manifest, ref, deadline)
                    current_seen = True
    if not current_seen:
        raise ValueError("current verification snapshot must be visible before retirement")
    for digest, size in live.items():
        if asset_for(host, pool + digest, digest, size, deadline) is None:
            raise ValueError("missing physical volume in retained verification snapshot")
    existed = host.existing_release(old_tag, deadline) is not None
    if existed:
        host.gh(deadline, "release", "delete", old_tag, "--repo", host.REPO, "--yes", "--cleanup-tag")
    if host.existing_release(old_tag, deadline) is not None:
        raise ValueError("retired verification snapshot is still visible")
    reclaimed = 0
    for digest, size in bound["volumes"].items():
        if not preserve_orphans and digest not in live and asset_for(host, pool + digest, digest, size, deadline) is not None:
            host.gh(deadline, "release", "delete", pool + digest, "--repo", host.REPO, "--yes", "--cleanup-tag")
            reclaimed += size
    host.receipt("volume-retire", "retired" if existed else "already-retired", tag=old_tag, retained_tag=current_tag,
                 source_ref=ref, test_id=current["volume_test_id"], retired_snapshot_payload_bytes=bound["archive_bytes"],
                 reclaimed_payload_bytes=reclaimed)
