"""Manage Lean dependency cache transfers and producer resume identities."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import shutil
import tempfile

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


def output_archive_paths(layer, paths):
    key = layer + "_archive_path"
    value = "\n".join(paths)
    delimiter = "STRATALINT_" + hashlib.sha256(value.encode()).hexdigest()
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
            stream.write(f"{key}<<{delimiter}\n{value}\n{delimiter}\n")
    print(key + "=" + json.dumps(paths, separators=(",", ":")))


def elan_key(root):
    system, arch = binary_platform()
    return f"elan-pinned-{system}-{arch}-{hashlib.sha256((root / 'lean-toolchain').read_bytes()).hexdigest()}"


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
    prefix = f"lean-dependency-v4-{revision}-{system}-{machine}-"
    result["dependency"] = {
        "restore_prefix": prefix, "key": f"{prefix}{run}-{attempt}", "path": ".lake/packages",
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


def restore_dependency(root, keys, key, outcome):
    spec = keys["dependency"]
    record = dependency_restored_record(root)
    record.unlink(missing_ok=True)
    if outcome in ("", "skipped"):
        receipt("dependency", "miss", reason="Actions supplied no cache")
        return False
    target = root / spec["path"]
    try:
        if outcome != "success" or not key:
            raise ValueError("Actions restore did not succeed or supplied no matched cache")
        if not re.fullmatch(re.escape(spec["restore_prefix"]) + r"[0-9]+-[0-9]+", key):
            raise ValueError("Actions seed is outside the selected partition")
        if target.is_symlink() or not target.is_dir():
            raise ValueError("native Actions cache directory is unavailable")
        stamp_restored_dependency(root, keys)
        try:
            previous = json.loads((target / ".stratalint-actions-inputs.json").read_text())
            digest = previous["inputs_sha256"]
            if isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest):
                write_small_record(record, {"snapshot_key": spec["key"], "matched_key": key, "inputs_sha256": digest})
        except (OSError, ValueError, TypeError, KeyError):
            pass
        receipt("dependency", "restored", key=key, partition=keys["partition"], transport="actions-native")
        return True
    except (OSError, ValueError, TypeError) as error:
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.exists():
            shutil.rmtree(target)
        receipt("dependency", "miss", reason=str(error))
        return False


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


def snapshot_dependency(root, keys):
    ready = False
    try:
        if not keys["save_allowed"]:
            receipt("dependency", "save-disabled")
        else:
            spec = keys["dependency"]
            target = root / spec["path"]
            if target.is_symlink() or not target.is_dir():
                raise ValueError("native cache directory is unavailable: " + spec["path"])
            fingerprint = dependency_inputs(root)
            try:
                restored = json.loads(dependency_restored_record(root).read_text())
                if (restored.get("snapshot_key") == spec["key"]
                        and re.fullmatch(re.escape(spec["restore_prefix"]) + r"[0-9]+-[0-9]+", restored.get("matched_key", ""))
                        and restored.get("inputs_sha256") == fingerprint):
                    receipt("dependency", "save-disabled", reason="retained-restored-dependency")
                    output({"dependency_ready": False})
                    return
            except (OSError, ValueError, TypeError, AttributeError):
                pass
            write_small_record(target / ".stratalint-actions-inputs.json", {"inputs_sha256": fingerprint})
            receipt("dependency", "snapshot", key=spec["key"])
            ready = True
    except (OSError, ValueError, TypeError) as error:
        receipt("dependency", "save-failed", reason=str(error))
    output({"dependency_ready": ready})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("keys", "restore", "snapshot", "resume-keys", "resume-restore", "resume-snapshot"))
    parser.add_argument("--repository", required=True, type=pathlib.Path)
    parser.add_argument("--layers", choices=("dependency",), nargs="+")
    parser.add_argument("--resume-key", default="")
    parser.add_argument("--resume-outcome", default="", choices=("", "success", "failure", "cancelled", "skipped"))
    parser.add_argument("--dependency-key", default="")
    parser.add_argument("--dependency-outcome", default="", choices=("", "success", "failure", "cancelled", "skipped"))
    args = parser.parse_args()
    try:
        root = args.repository.resolve()
        keys = actions_keys(root)
        if args.command.startswith("resume-"):
            spec = resume_spec(root)
            if args.command == "resume-keys":
                output({"resume_key": spec["key"], "resume_restore_prefix": spec["restore_prefix"]})
                output_archive_paths("resume", spec["paths"])
            elif args.command == "resume-restore":
                resume_restore(root, spec, args.resume_key, args.resume_outcome)
            else:
                resume_snapshot(root, spec)
        elif args.command == "keys":
            paths = native_archive_paths(root, ["dependency"])["dependency"]
            values = {key: keys[key] for key in ("mathlib_revision", "os", "arch", "partition", "save_allowed", "release_prefix")}
            values["elan_key"] = elan_key(root)
            values.update({"dependency_" + key: value for key, value in keys["dependency"].items()})
            output(values)
            output_archive_paths("dependency", paths)
        elif args.command == "restore":
            restore_dependency(root, keys, args.dependency_key, args.dependency_outcome)
        else:
            snapshot_dependency(root, keys)
        return 0
    except (OSError, ValueError, TypeError, KeyError, CachePathRegistrationError) as error:
        print("CI_INPUT_FAILED " + str(error), file=os.sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
