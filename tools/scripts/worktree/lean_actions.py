"""Transport the independent Lean dependency and project cache layers."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import shutil
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request

from cache_material import sha
from lean_artifact_inventory import retired_report_path
from lean_cache import binary_platform, partition_path, resolved_mathlib

LAYERS = ("dependency", "project")


class CachePathRegistrationError(ValueError):
    pass


def native_archive_paths(root: pathlib.Path, layers):
    selected = set(layers) & set(LAYERS)
    if not selected:
        return {}
    manifest = "Meta/ci-cache-paths.json"

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate field: " + key)
            result[key] = value
        return result

    try:
        import tomllib
        filemap = tomllib.loads((root / "Meta/FILEMAP.toml").read_text())
        rows = [row for row in filemap.get("files", []) if row.get("pattern") == manifest]
        if len(rows) != 1:
            raise ValueError("manifest must have one literal FILEMAP entry")
        registration = json.loads((root / manifest).read_text(), object_pairs_hook=unique_object)
        if (not isinstance(registration, dict) or set(registration) != {"schema_version", "layers"}
                or registration["schema_version"] != 1 or not isinstance(registration["layers"], dict)
                or not set(registration["layers"]).issubset(LAYERS)):
            raise ValueError("invalid schema or layer")
        roots = {"dependency": ".lake/packages", "project": ".lake/build"}
        result = {}
        for layer in sorted(selected):
            paths = registration["layers"].get(layer)
            if not isinstance(paths, list) or not paths:
                raise ValueError("missing paths for " + layer)
            for path in paths:
                if (not isinstance(path, str) or not re.fullmatch(r"[A-Za-z0-9_./*\-]+", path)
                        or any(part in ("", ".", "..") for part in path.split("/"))
                        or not (path == roots[layer] or path.startswith(roots[layer] + "/"))):
                    raise ValueError("invalid path for " + layer + ": " + repr(path))
            if len(set(paths)) != len(paths):
                raise ValueError("duplicate paths for " + layer)
            result[layer] = sorted(paths)
        return result
    except (OSError, ValueError, TypeError, AttributeError) as error:
        raise CachePathRegistrationError(f"cache path registration {manifest}: {error}") from error


def output_lines(key, lines):
    value = "\n".join(lines)
    delimiter = "STRATALINT_" + hashlib.sha256(value.encode()).hexdigest()
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
            stream.write(f"{key}<<{delimiter}\n{value}\n{delimiter}\n")
    print(key + "=" + json.dumps(lines, separators=(",", ":")))


def elan_key(root):
    system, arch = binary_platform()
    return f"elan-pinned-{system}-{arch}-{hashlib.sha256((root / 'lean-toolchain').read_bytes()).hexdigest()}"


def is_pr():
    return os.environ.get("GITHUB_EVENT_NAME") in {"pull_request", "pull_request_target"}


def native_namespaces():
    if is_pr():
        ref = "refs/heads/" + os.environ.get("GITHUB_BASE_REF", "")
    else:
        ref = os.environ.get("GITHUB_REF", "")
    namespaces = ["dev-"]
    if ref.startswith("refs/heads/integration-"):
        namespaces.insert(0, "integration-" + hashlib.sha256(ref.encode()).hexdigest() + "-")
    # PRs can see caches on their source branch and their historical merge ref.
    if not is_pr():
        namespaces.append("")
    return namespaces


def manual_source_is_valid() -> bool:
    """Accept cache writes from a workflow dispatch only after its source pin was checked.

    The workflow performs the protected-branch/API check before setting the marker.  This
    second, local check binds the writer to the exact workflow ref and checked-out SHA and
    keeps an unvalidated dispatch from silently becoming a branch writer.
    """
    if os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch":
        return False
    if os.environ.get("STRATALINT_CACHE_SOURCE_VALIDATED") != "true":
        return False
    source_ref = os.environ.get("STRATALINT_CACHE_SOURCE_REF", "")
    source_commit = os.environ.get("STRATALINT_CACHE_SOURCE_COMMIT", "")
    if not re.fullmatch(r"refs/heads/(?:dev|integration-[A-Za-z0-9._/-]+)", source_ref):
        return False
    if source_ref != os.environ.get("GITHUB_REF", ""):
        return False
    if not re.fullmatch(r"[0-9a-f]{40}", source_commit) or source_commit == "0" * 40:
        return False
    return source_commit == os.environ.get("STRATALINT_CACHE_CHECKED_OUT_SHA", "")


def preparation_source_is_valid() -> bool:
    """Identify validated PR preparations when reading candidate releases."""
    source_ref = os.environ.get("STRATALINT_CACHE_SOURCE_REF", "")
    target_ref = os.environ.get("STRATALINT_CACHE_TARGET_REF", "")
    source_commit = os.environ.get("STRATALINT_CACHE_SOURCE_COMMIT", "")
    return (os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
            and os.environ.get("GITHUB_REF_PROTECTED") == "true"
            and os.environ.get("STRATALINT_CACHE_PR_SOURCE_VALIDATED") == "true"
            and re.fullmatch(r"refs/pull/[1-9][0-9]*/merge", source_ref) is not None
            and re.fullmatch(r"refs/heads/(?:dev|integration-[A-Za-z0-9._/-]+)", target_ref) is not None
            and target_ref == os.environ.get("GITHUB_REF")
            and re.fullmatch(r"[0-9a-f]{40}", source_commit) is not None
            and source_commit != "0" * 40
            and source_commit == os.environ.get("STRATALINT_CACHE_CHECKED_OUT_SHA"))


def actions_keys(root: pathlib.Path) -> dict:
    revision = resolved_mathlib(root)
    system, machine = binary_platform()
    run = os.environ.get("GITHUB_RUN_ID", "")
    attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "")
    if not re.fullmatch(r"[0-9]+", run) or not re.fullmatch(r"[0-9]+", attempt):
        raise ValueError("snapshot keys require GITHUB_RUN_ID and GITHUB_RUN_ATTEMPT")
    event, ref = os.environ.get("GITHUB_EVENT_NAME"), os.environ.get("GITHUB_REF", "")
    push_writer = (event == "push" and os.environ.get("GITHUB_REF_PROTECTED") == "true"
                   and re.fullmatch(r"refs/heads/(?:dev|integration-[A-Za-z0-9._/-]+)", ref) is not None)
    writer_allowed = (push_writer or manual_source_is_valid()) and os.environ.get("STRATALINT_CACHE_WRITES") == "true"
    result = {
        "mathlib_revision": revision, "os": system, "arch": machine,
        "partition": partition_path(root),
        "save_allowed": writer_allowed and os.environ.get("STRATALINT_CHECK_SUCCEEDED") == "true",
        "producer_save_allowed": writer_allowed and os.environ.get("GITHUB_REF_PROTECTED") == "true",
        "project_save_allowed": writer_allowed and os.environ.get("STRATALINT_REPORT_SUCCEEDED") == "true",
        "release_prefix": f"lean-cache-v2-{revision}-{system}-{machine}-",
    }
    namespaces = native_namespaces()
    for layer in LAYERS:
        generation = "project-push" if layer == "project" else "dependency-v4"
        legacy_prefix = f"lean-{generation}-{revision}-{system}-{machine}-"
        prefixes = [namespace + legacy_prefix for namespace in namespaces]
        prefix = prefixes[0]
        result[layer] = {
            "restore_prefix": prefix, "key": f"{prefix}{run}-{attempt}",
            "restore_keys": prefixes,
            "path": ".lake/packages" if layer == "dependency" else ".lake/build",
        }
    return result


def output(values, destination="GITHUB_OUTPUT"):
    text = "".join(f"{key}={str(value).lower() if isinstance(value, bool) else value}\n" for key, value in values.items())
    if os.environ.get(destination):
        with open(os.environ[destination], "a", encoding="utf-8") as stream:
            stream.write(text)
    print(text, end="")


def receipt(layer, status, **fields):
    print("LEAN_ACTIONS_CACHE " + json.dumps({"layer": layer, "status": status, **fields}, sort_keys=True), flush=True)


class SeedLookupUnavailable(ValueError):
    pass


def wait_for_seed_lookup(completed, seconds):
    return completed.wait(seconds)


def guarded_seed_lookup(lookup):
    completed = threading.Event()
    result = []

    def run():
        try:
            result.append((lookup(), None))
        except Exception as error:
            result.append((None, error))
        finally:
            completed.set()

    threading.Thread(target=run, daemon=True).start()
    # One transport hang guard covers DNS, headers, bodies and every page.
    if not wait_for_seed_lookup(completed, 10):
        raise SeedLookupUnavailable("lookup-transport-hang-guard")
    key, error = result[0]
    if error is not None:
        raise error
    return key


def lookup_project_seed(spec, ref, token, repository, api):
    endpoint = api + "/repos/" + repository + "/actions/caches"
    query = dict(key=spec["restore_prefix"], ref=ref, per_page="100", sort="created_at", direction="desc")
    url = endpoint + "?" + urllib.parse.urlencode(query)
    total, seen, page, best = None, set(), 1, None
    pattern = re.compile(re.escape(spec["restore_prefix"]) + r"([1-9][0-9]*)-([1-9][0-9]*)")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise SeedLookupUnavailable("lookup-malformed-listing")
            result[key] = value
        return result

    while True:
        request = urllib.request.Request(url, headers={
            "Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"})
        with urllib.request.urlopen(request, timeout=10) as response:
            if response.status != 200:
                raise SeedLookupUnavailable("lookup-http-error")
            try:
                value = json.loads(response.read(), object_pairs_hook=unique)
            except (ValueError, UnicodeError):
                raise SeedLookupUnavailable("lookup-malformed-listing")
            link = response.headers.get("Link", "")
        if (not isinstance(value, dict) or type(value.get("total_count")) is not int
                or value["total_count"] < 0 or not isinstance(value.get("actions_caches"), list)):
            raise SeedLookupUnavailable("lookup-malformed-listing")
        if total is None:
            total = value["total_count"]
        elif total != value["total_count"]:
            raise SeedLookupUnavailable("lookup-incomplete-listing")
        rows = value["actions_caches"]
        for row in rows:
            if (not isinstance(row, dict) or not isinstance(row.get("key"), str)
                    or not isinstance(row.get("ref"), str)):
                raise SeedLookupUnavailable("lookup-malformed-listing")
            cache_id = row.get("id")
            if type(cache_id) is not int or cache_id <= 0 or cache_id in seen:
                raise SeedLookupUnavailable("lookup-incomplete-listing")
            seen.add(cache_id)
            match = pattern.fullmatch(row["key"])
            if row["ref"] == ref and match:
                candidate = (int(match[1]), int(match[2]), row["key"])
                if best is None or candidate[:2] > best[:2]:
                    best = candidate
        if len(seen) > total or not isinstance(link, str):
            raise SeedLookupUnavailable("lookup-incomplete-listing")
        links = {}
        if link:
            for entry in link.split(","):
                match = re.fullmatch(r'\s*<([^<>]+)>;\s*rel="([^"]+)"\s*', entry)
                if not match or match[2] in links:
                    raise SeedLookupUnavailable("lookup-incomplete-listing")
                links[match[2]] = match[1]
        next_url = links.get("next")
        if next_url is None:
            if len(seen) != total:
                raise SeedLookupUnavailable("lookup-incomplete-listing")
            break
        next_parts = urllib.parse.urlsplit(next_url)
        if (not rows or len(seen) >= total or next_parts._replace(query="").geturl() != endpoint
                or urllib.parse.parse_qs(next_parts.query) != {
                    **{key: [item] for key, item in query.items()}, "page": [str(page + 1)]}):
            raise SeedLookupUnavailable("lookup-incomplete-listing")
        url, page = next_url, page + 1
    if best is None:
        raise SeedLookupUnavailable("lookup-no-eligible-seed")
    return best[2]


def project_restore_key(keys, layers=LAYERS):
    spec = keys["project"]
    fallback = spec["key"]
    token = os.environ.get("GH_TOKEN", "")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    event = os.environ.get("GITHUB_EVENT_NAME", "")
    base = os.environ.get("GITHUB_BASE_REF", "")
    ref = os.environ.get("GITHUB_REF", "")
    selected_ref = ""
    if is_pr() and base:
        selected_ref = "refs/heads/" + base
    elif event == "push":
        selected_ref = ref

    def preferred_seed():
        try:
            key = lookup_project_seed(spec, selected_ref, token, repository, api)
            return selected_ref, key, {}
        except SeedLookupUnavailable as error:
            if str(error) != "lookup-no-eligible-seed":
                raise
        head = os.environ.get("GITHUB_HEAD_REF", "")
        if not (is_pr() and head and not head.startswith("integration-")
                and (base == "dev" or base.startswith("integration-"))):
            raise SeedLookupUnavailable("lookup-no-eligible-seed")

        # Old native keys end in the unique producer run and attempt. Resolve
        # them on a permitted ref after a scoped miss, within the same deadline.
        legacy = {}
        for layer in layers:
            prefix = keys[layer]["restore_keys"][-1].removeprefix("dev-")
            for owner in dict.fromkeys([selected_ref, "refs/heads/dev"]):
                try:
                    key = lookup_project_seed({"restore_prefix": prefix}, owner, token, repository, api)
                    legacy[layer] = (owner, key)
                    break
                except SeedLookupUnavailable as error:
                    if str(error) != "lookup-no-eligible-seed":
                        raise
        owner, preferred = legacy.get("project", ("", fallback))
        return owner, preferred, legacy

    reason = "lookup-not-configured"
    try:
        if not token or not repository or not selected_ref:
            raise SeedLookupUnavailable(reason)
        api = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
        owner, preferred, legacy = guarded_seed_lookup(preferred_seed)
        for layer, (_, exact) in legacy.items():
            keys[layer]["restore_keys"].append(exact)
            # The validator runs in a separate step, after Actions extraction.
            output({"STRATALINT_LEGACY_" + layer.upper(): keys[layer]["key"] + " " + exact}, "GITHUB_ENV")
        if not owner:
            raise SeedLookupUnavailable("lookup-no-eligible-seed")
        receipt("project", "restore-preference", base_ref=owner, requested_key=preferred)
        return preferred
    except SeedLookupUnavailable as error:
        reason = str(error)
    except urllib.error.HTTPError:
        reason = "lookup-http-error"
    except Exception:
        reason = "lookup-request-failed"
    receipt("project", "restore-preference-fallback", base_ref=selected_ref, requested_key=fallback, reason=reason)
    return fallback


def dependency_input_materials(root: pathlib.Path):
    inputs = {}
    for relative in ("lake-manifest.json", "lean-toolchain", "lakefile.toml"):
        path = root / relative
        if path.is_symlink() or not path.is_file():
            raise ValueError("dependency save input is unavailable: " + relative)
        inputs[relative] = sha(path)
    optional = root / "lakefile.lean"
    if optional.exists():
        if optional.is_symlink() or not optional.is_file():
            raise ValueError("dependency save input is unavailable: lakefile.lean")
        inputs["lakefile.lean"] = sha(optional)
    declaration = json.loads((root / "lean-report-inputs.json").read_text())
    names = declaration["report_execution"]["environment"]
    if (not isinstance(names, list) or any(not isinstance(name, str) or not name for name in names)
            or len(set(names)) != len(names)):
        raise ValueError("dependency save environment registration is invalid")
    return {"files": inputs, "environment": {name: os.environ.get(name) for name in names}}


def dependency_inputs(root: pathlib.Path):
    value = dependency_input_materials(root)
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def dependency_restored_record(root):
    return root / "build/lean-cache/dependency-restored.json"


def write_small_record(path: pathlib.Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".actions-inputs-", dir=path.parent)
    temporary = pathlib.Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream)
            stream.write("\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def stamp_restored_dependency(root, keys):
    write_small_record(root / ".lake/.stratalint-lean-cache-stamp.json", {
        "schema": "stratalint-lean-cache-v2", "mathlib_revision": keys["mathlib_revision"],
        "os": keys["os"], "arch": keys["arch"]})


def discard_retired_reports(build):
    if build.parent.is_symlink() or build.is_symlink():
        return
    for name in ("stratalint", "lean-inspector"):
        directory = build / name
        if directory.is_symlink() or not directory.is_dir():
            continue
        for path in directory.iterdir():
            if retired_report_path(path.relative_to(build)) and (path.is_symlink() or path.is_file()):
                path.unlink()


def restore_native(root, keys, layer, key, outcome):
    spec = keys[layer]
    requested = {"requested_key": spec.get("restore_key", spec["key"])} if layer == "project" else {}
    transport = {"key": key, **requested} if layer == "project" else {}
    record = dependency_restored_record(root)
    if layer == "dependency":
        record.unlink(missing_ok=True)
    if outcome in ("", "skipped"):
        receipt(layer, "miss", reason="Actions supplied no cache", **transport)
        return False
    target = root / spec["path"]
    try:
        if outcome != "success" or not key:
            raise ValueError("Actions restore did not succeed or supplied no matched cache")
        exact_legacy = (is_pr() and os.environ.get("GITHUB_HEAD_REF")
                        and not os.environ["GITHUB_HEAD_REF"].startswith("integration-")
                        and os.environ.get("STRATALINT_LEGACY_" + layer.upper()) == spec["key"] + " " + key)
        if not exact_legacy and not any(re.fullmatch(re.escape(prefix) + r"[0-9]+-[0-9]+", key)
                                        for prefix in spec["restore_keys"] if prefix.endswith("-")):
            raise ValueError("Actions seed is outside the selected partition")
        if target.is_symlink() or not target.is_dir():
            raise ValueError("native Actions cache directory is unavailable")
        if layer == "dependency":
            stamp_restored_dependency(root, keys)
            try:
                previous = json.loads((target / ".stratalint-actions-inputs.json").read_text())
                digest = previous["inputs_sha256"]
                if isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest):
                    write_small_record(record, {"snapshot_key": spec["key"], "matched_key": key, "inputs_sha256": digest})
            except (OSError, ValueError, TypeError, KeyError):
                pass
        else:
            discard_retired_reports(target)
        receipt(layer, "restored", key=key, partition=keys["partition"], transport="actions-native", **requested)
        return True
    except (OSError, ValueError, TypeError) as error:
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.exists():
            shutil.rmtree(target)
        receipt(layer, "miss", reason=str(error), **transport)
        return False


def adoption_record(root):
    """Invocation-local acceptance, outside every transported native path."""
    path = root / "build/lean-cache/pr-adopted.json"
    try:
        if path.is_symlink():
            return None
        value = json.loads(path.read_text())
        checkout = os.environ.get("STRATALINT_CACHE_CHECKED_OUT_SHA", os.environ.get("GITHUB_SHA"))
        if (value["run"] != os.environ.get("GITHUB_RUN_ID")
                or value["attempt"] != os.environ.get("GITHUB_RUN_ATTEMPT")
                or value["repository"] != str(root.resolve()) or value["checkout"] != checkout
                or value["ref"] != os.environ.get("GITHUB_REF")
                or type(value["artifact"]) is not int or value["artifact"] <= 0
                or type(value["merged"]) is not bool):
            return None
        return value
    except (OSError, ValueError, TypeError, KeyError):
        return None


def restore(root, keys, matched, layers=LAYERS, *, outcomes=None):
    if adoption_record(root) is not None:
        receipt("project", "preparation-retained", reason="validated PR artifact already adopted")
        return
    seeded = False
    for layer in layers:
        accepted = restore_native(root, keys, layer, matched.get(layer, ""), (outcomes or {}).get(layer, "success"))
        seeded |= layer == "project" and accepted
    if "project" in layers:
        output({"STRATALINT_ACTIONS_CACHE_SEEDED": "1" if seeded else "0"}, "GITHUB_ENV")


def resume_marker(root):
    return root / ".lake/build/lean-inspector/producer-resume.json"


def resume_record(root):
    # This invocation fact is deliberately outside every transported cache path.
    return root / "build/lean-cache/producer-resume-restored.json"


def read_resume_record(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate resume field")
            result[key] = value
        return result
    if path.is_symlink():
        raise ValueError("resume record is a symlink")
    value = json.loads(path.read_text(), object_pairs_hook=unique)
    if not isinstance(value, dict):
        raise ValueError("resume record is not an object")
    return value


def resume_spec(root):
    keys = actions_keys(root)
    if not keys["producer_save_allowed"]:
        raise ValueError("producer resume requires an authorized protected branch")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("producer repository is unavailable")
    binding = {
        "repository": repository, "ref": os.environ["GITHUB_REF"],
        "inputs": dependency_inputs(root), "mathlib": keys["mathlib_revision"],
        "os": keys["os"], "arch": keys["arch"],
    }
    release_tag = os.environ.get("STRATALINT_RELEASE_SEED_TAG", "")
    release_producer = os.environ.get("STRATALINT_RELEASE_SEED_PRODUCER", "")
    if release_tag or release_producer:
        if (not manual_source_is_valid() or not re.fullmatch(r"[A-Za-z0-9._-]+", release_tag)
                or not re.fullmatch(r"[0-9a-f]{40}", release_producer) or release_producer == "0" * 40):
            raise ValueError("Release continuation requires a protected dispatch and both Release pins")
        binding["release_seed"] = {"tag": release_tag, "producer": release_producer,
                                   "target": os.environ["STRATALINT_CACHE_SOURCE_COMMIT"],
                                   "run": os.environ["GITHUB_RUN_ID"]}
    identity = hashlib.sha256(json.dumps(binding, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    prefix = f"lean-producer-resume-v1-{keys['mathlib_revision']}-{keys['os']}-{keys['arch']}-{identity}-"
    paths = native_archive_paths(root, LAYERS)
    return {"identity": identity, "restore_prefix": prefix,
            "key": prefix + os.environ["GITHUB_RUN_ID"] + "-" + os.environ["GITHUB_RUN_ATTEMPT"],
            "paths": sorted(set(path for group in paths.values() for path in group))}


def resume_key_matches(spec, key):
    return re.fullmatch(re.escape(spec["restore_prefix"]) + r"[1-9][0-9]*-[1-9][0-9]*", key) is not None


def resume_snapshot(root, spec):
    for layer in LAYERS:
        target = root / (".lake/packages" if layer == "dependency" else ".lake/build")
        if target.is_symlink() or not target.is_dir():
            receipt("resume", "save-disabled", reason="native-material-unavailable")
            output({"resume_ready": False})
            return
    write_small_record(resume_marker(root), {
        "schema_version": 1, "identity": spec["identity"], "key": spec["key"]})
    receipt("resume", "snapshot", key=spec["key"], complete=False)
    output({"resume_ready": True})


def resume_restore(root, spec, key, outcome):
    resume_record(root).unlink(missing_ok=True)
    # A lookup miss skips extraction; every attempted restore must supply its key.
    if outcome in ("", "skipped"):
        receipt("resume", "miss", reason="Actions supplied no continuation")
        return
    try:
        if outcome != "success" or not resume_key_matches(spec, key):
            raise ValueError("resume transport failed or identity is incompatible")
        marker = read_resume_record(resume_marker(root))
        if (set(marker) != {"schema_version", "identity", "key"}
                or type(marker["schema_version"]) is not int or marker["schema_version"] != 1
                or marker["identity"] != spec["identity"] or marker["key"] != key):
            raise ValueError("resume metadata does not match the selected source")
        for path in (root / ".lake/packages", root / ".lake/build"):
            if path.is_symlink() or not path.is_dir():
                raise ValueError("resume native material is unavailable")
        discard_retired_reports(root / ".lake/build")
        stamp_restored_dependency(root, actions_keys(root))
        write_small_record(resume_record(root), {
            "schema_version": 1, "identity": spec["identity"],
            "snapshot_key": spec["key"], "matched_key": key})
        receipt("resume", "restored", key=key, complete=False)
    except (OSError, ValueError, TypeError, KeyError) as error:
        # Actions may already have extracted part of an incompatible archive.
        for path in (root / ".lake/packages", root / ".lake/build"):
            if path.is_symlink() or path.is_file():
                path.unlink()
            elif path.exists():
                shutil.rmtree(path)
        receipt("resume", "miss", reason=str(error))


def resume_verified(root):
    try:
        spec = resume_spec(root)
        value = read_resume_record(resume_record(root))
        return (set(value) == {"schema_version", "identity", "snapshot_key", "matched_key"}
                and type(value["schema_version"]) is int and value["schema_version"] == 1
                and value["identity"] == spec["identity"] and value["snapshot_key"] == spec["key"]
                and resume_key_matches(spec, value["matched_key"]))
    except (OSError, ValueError, TypeError, KeyError):
        return False


def project_work(root):
    path = pathlib.Path(os.environ.get("STRATALINT_LEAN_BUILD_WORK_FILE", str(root / "build/lean-cache/build-work.json")))
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate work field")
            result[key] = value
        return result
    try:
        if (not path.is_absolute() or path.is_symlink()
                or path.resolve().is_relative_to(root.resolve() / ".lake")):
            raise ValueError("work fact must be outside the restored tree")
        value = json.loads(path.read_text(), object_pairs_hook=unique)
        if (not isinstance(value, dict) or set(value) != {"schema_version", "run_id", "run_attempt", "repository", "report", "programs"}
                or type(value["schema_version"]) is not int or value["schema_version"] != 1
                or value["run_id"] != os.environ.get("GITHUB_RUN_ID")
                or value["run_attempt"] != os.environ.get("GITHUB_RUN_ATTEMPT")
                or value["repository"] != str(root.resolve())
                or any(type(value[name]) is not int or value[name] < 0 for name in ("report", "programs"))):
            raise ValueError("invalid or stale invocation work fact")
        return value["report"] + value["programs"]
    except (OSError, UnicodeError, ValueError, TypeError, KeyError):
        return None


def project_publication(root):
    if os.environ.get("GITHUB_EVENT_NAME") == "pull_request":
        return False, "pull-requests-do-not-publish-project-seeds", None
    work = project_work(root)
    if work is None:
        return False, "build-work-unknown", work
    adopted = adoption_record(root)
    if adopted is not None and adopted["merged"]:
        return True, "adopted-merged-preparation", work
    if work == 0:
        if os.environ.get("STRATALINT_RELEASE_CACHE_SEEDED") == "1" and manual_source_is_valid():
            return True, "accepted-explicit-release-seed", work
        if os.environ.get("STRATALINT_CACHE_PRODUCER_COMPLETE") == "true":
            if os.environ.get("GITHUB_EVENT_NAME") == "push" and os.environ.get("STRATALINT_REPORT_SUCCEEDED") == "true":
                return True, "validated-push", work
            if resume_verified(root):
                return True, "validated-producer-resume", work
        return False, "no-build-work", work
    return True, "built-project-artifacts", work


def snapshot(root, keys, layers=LAYERS):
    for layer in layers:
        if layer not in LAYERS:
            continue
        ready = False
        try:
            if layer == "project":
                allowed, reason, work = project_publication(root)
                if not allowed:
                    receipt(layer, "save-disabled", reason=reason)
                    output({layer + "_ready": False, "project_skip_reason": reason})
                    continue
            if not keys["project_save_allowed" if layer == "project" else "save_allowed"]:
                receipt(layer, "save-disabled")
                output({layer + "_ready": False})
                continue
            target = root / keys[layer]["path"]
            if target.is_symlink() or not target.is_dir():
                raise ValueError("native cache directory is unavailable: " + keys[layer]["path"])
            if layer == "dependency":
                fingerprint = dependency_inputs(root)
                try:
                    restored = json.loads(dependency_restored_record(root).read_text())
                    spec = keys[layer]
                    if (restored.get("snapshot_key") == spec["key"]
                            and re.fullmatch(re.escape(spec["restore_prefix"]) + r"[0-9]+-[0-9]+", restored.get("matched_key", ""))
                            and restored.get("inputs_sha256") == fingerprint):
                        receipt(layer, "save-disabled", reason="retained-restored-dependency")
                        output({layer + "_ready": False})
                        continue
                except (OSError, ValueError, TypeError, AttributeError):
                    pass
                write_small_record(target / ".stratalint-actions-inputs.json", {"inputs_sha256": fingerprint})
            ready = True
            receipt(layer, "snapshot", key=keys[layer]["key"], **({"build_work": work} if layer == "project" else {}))
        except (OSError, ValueError, TypeError) as error:
            receipt(layer, "save-failed", reason=str(error))
        output({layer + "_ready": ready})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("keys", "restore", "snapshot", "resume-keys", "resume-restore", "resume-snapshot"))
    parser.add_argument("--repository", required=True, type=pathlib.Path)
    parser.add_argument("--layers", choices=LAYERS, nargs="+")
    parser.add_argument("--project-restore-key", default="")
    parser.add_argument("--resume-key", default="")
    parser.add_argument("--resume-outcome", default="", choices=("", "success", "failure", "cancelled", "skipped"))
    for layer in LAYERS:
        parser.add_argument("--" + layer + "-key", default="")
        parser.add_argument("--" + layer + "-outcome", default="", choices=("", "success", "failure", "cancelled", "skipped"))
    args = parser.parse_args()
    args.layers = list(LAYERS) if args.layers is None else args.layers
    try:
        root = args.repository.resolve()
        keys = actions_keys(root)
        if args.command.startswith("resume-"):
            spec = resume_spec(root)
            if args.command == "resume-keys":
                output({"resume_key": spec["key"], "resume_restore_prefix": spec["restore_prefix"]})
                output_lines("resume_archive_path", spec["paths"])
            elif args.command == "resume-restore":
                resume_restore(root, spec, args.resume_key, args.resume_outcome)
            else:
                resume_snapshot(root, spec)
        elif args.command == "keys":
            archive_paths = native_archive_paths(root, args.layers)
            if "project" in args.layers:
                keys["project"]["restore_key"] = project_restore_key(keys, args.layers)
            values = {key: keys[key] for key in ("mathlib_revision", "os", "arch", "partition", "save_allowed", "release_prefix")}
            toolchain = elan_key(root)
            namespaces = native_namespaces()
            values["elan_key"] = namespaces[0] + toolchain
            fallbacks = [namespace + toolchain for namespace in namespaces[1:]]
            if len(fallbacks) == 1:
                values["elan_restore_key"] = fallbacks[0]
            for layer in args.layers:
                values.update({layer + "_" + key: value for key, value in keys[layer].items() if key != "restore_keys"})
            output(values)
            if len(fallbacks) > 1:
                output_lines("elan_restore_key", fallbacks)
            for layer, paths in archive_paths.items():
                output_lines(layer + "_archive_path", paths)
                output_lines(layer + "_restore_keys", keys[layer]["restore_keys"])
        elif args.command == "restore":
            keys["project"]["restore_key"] = args.project_restore_key or keys["project"]["key"]
            restore(root, keys, {layer: getattr(args, layer + "_key") for layer in args.layers}, args.layers,
                    outcomes={layer: getattr(args, layer + "_outcome") for layer in args.layers})
        else:
            snapshot(root, keys, args.layers)
        return 0
    except (OSError, ValueError, TypeError, KeyError, CachePathRegistrationError) as error:
        print("CI_INPUT_FAILED " + str(error), file=os.sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
