"""Plan a rolling Release from an authenticated predecessor and the target import graph."""

import json
import os
import re
import shutil
from pathlib import Path, PurePosixPath
import subprocess
import tempfile

from lean_artifact_inventory import inspector_build_path, import_closure, lake_configuration, project_imports, workspace_modules
import lean_compile_layers as compiler
import lean_release_index as index
from lean_cache_release_io import SPARSE_PLAN, check_native_products


SCHEMA = "lean-release-sparse-plan-v1"


def sparse_plan(root, tag, manifest, digest, tree, target, *, extra_roots=()):
    """Return None when the protected publisher should use ordinary full preparation."""
    if (manifest["schema"] != "lean-release-seed-v6"
            or manifest["lake_inputs_sha256"] != lake_configuration(root)
            or compiler.checkout(root) != target):
        return None
    base = manifest["producer_commit_sha"]
    ancestry = subprocess.run(["git", "merge-base", "--is-ancestor", base, target], cwd=root)
    if ancestry.returncode:
        return None
    # A tool, registration or other build input outside the ordinary Lean content surface
    # may affect layout or compiler behavior without an edge in the project import graph.
    paths = subprocess.check_output(
        ["git", "diff", "--no-renames", "--name-only", "-z", base, target], cwd=root,
    ).decode().split("\0")[:-1]
    modules = workspace_modules(root)
    sources = {mod.source.as_posix() for mod in modules.values()}
    for path in paths:
        if path in sources and path.startswith(("D5/", "Reg/")):
            continue
        if path.startswith("Blueprint/") and path.endswith((".md", ".scribe.cs")):
            continue
        if path.startswith("Golden/Frozen/state/") and path.endswith(".lean.json"):
            continue
        # The consumer's literature entries and problem statements are Markdown
        # content alongside the Lean change. Other files retain full preparation.
        if re.fullmatch(r"(?:Library/[^/]+|Problems)/[^/]+\.md", path):
            continue
        return None
    graph = project_imports(root, modules)
    scope = import_closure(graph, compiler.production_targets(root, modules) | set(extra_roots))
    rows = tree["members"]
    prior_names = {row["module"] for row in rows if row["module"] is not None}
    non_inspector = {name for name in prior_names & modules.keys() if not inspector_build_path(modules[name].build)}
    scope |= import_closure(graph, non_inspector)
    compatible = index.compatible_modules(root, rows, modules, graph, scope)
    changed = scope - compatible
    prerequisites = import_closure(graph, changed | set(extra_roots)) & compatible
    return dict(schema=SCHEMA, base_tag=tag, base_commit=base, base_manifest_sha256=digest,
                target_commit=target, rebuild_modules=sorted(changed),
                removed_modules=sorted(prior_names - scope), restore_modules=sorted(prerequisites),
                inherit_modules=sorted(compatible))

def plan_path(root):
    return root / SPARSE_PLAN


def native_fallback(build, paths, tree, plan):
    """Find a predecessor native product whose inputs the olean pass cannot refresh."""
    changed = set(plan["rebuild_modules"]) | set(plan["removed_modules"])
    if not changed:
        return None
    old = {row["module"]: row for row in tree["members"] if row["module"] in changed}
    removed = set(plan["removed_modules"])
    prior_bitcode = next((path for name, row in old.items() for path in row["paths"]
                          if path.endswith(".bc") and not (name in removed and inspector_build_path(PurePosixPath(path)))),
                         None)
    if prior_bitcode is not None:
        return prior_bitcode
    # Lake stores per-module object variants and their sidecars alongside its C or bitcode.
    ir = {str(PurePosixPath(path).with_suffix("")) for row in old.values()
          for path in row["paths"] if path.endswith((".c", ".bc"))}
    for path in sorted(paths):
        filename = str(path)
        if {".a", ".so", ".dylib", ".dll", ".lib"} & set(path.suffixes):
            # An aggregate may gain a newly introduced library member too.
            return filename
        stem = re.split(r"\.(?:c\.|bc\.)?o(?:\.|$)", filename, maxsplit=1)
        if len(stem) == 2 and stem[0] in ir:
            return filename
        if old and path.parent.name == "bin" and path.suffix in ("", ".exe"):
            response = (build / path).with_suffix(".rsp")
            if not response.is_file():
                return filename
            # Lake response files list the exact object inputs, including old absolute
            # build paths. Keep matching module paths relative to the project build.
            listed = response.read_text(encoding="utf-8").replace("\\", "/").splitlines()
            objects = []
            for line in listed:
                argument = line.strip().strip('"')
                relative = argument.rsplit("/.lake/build/", 1)[-1].removeprefix(".lake/build/")
                match = re.fullmatch(r"(.+?)\.(?:c\.|bc\.)?o(?:\.[^/]*)?", relative)
                if match:
                    if argument.startswith("/") and "/.lake/build/" not in argument:
                        return filename
                    objects.append(match[1])
                elif argument.startswith("@"):
                    return filename
            if not objects or ir.intersection(objects):
                return filename
    return None


def hosted_workspace(root):
    workspace = os.environ.get("GITHUB_WORKSPACE")
    return os.environ.get("GITHUB_ACTIONS") == "true" and workspace and Path(workspace).resolve() == root.resolve()


def install(host, root, stage, tag, manifest, digest, target, source_ref, verification, deadline):
    """Install the rolling producer's changed import inputs into a private empty build."""
    import lean_release_volume_remote as remote

    scope = remote.namespace(manifest["partition"], manifest["cache_key"], verification, "ci-publisher", host,
                             test_id=os.environ.get("LEAN_CACHE_VOLUME_TEST_ID") or None, source_ref=source_ref)
    if manifest["volume_scope"] != scope:
        return False
    lake = root / ".lake"
    build = lake / "build"
    marker = plan_path(root)
    if lake.is_symlink() or build.is_symlink() or marker.is_symlink():
        raise ValueError("sparse Release preparation needs a private workspace")
    installed = read_plan(root) if marker.exists() else None
    if installed and (not hosted_workspace(root) or installed["base_tag"] != tag
                      or installed["base_manifest_sha256"] != digest or installed["target_commit"] != target):
        raise ValueError("previous sparse installation belongs to a different publisher or snapshot")
    if build.exists() and (not build.is_dir() or (not installed and any(build.iterdir()))):
        raise ValueError("sparse Release preparation needs an empty project build")
    with tempfile.TemporaryDirectory(dir=stage) as metadata:
        tree = remote.load_tree(host, tag, Path(metadata), manifest, deadline)
    plan = sparse_plan(root, tag, manifest, digest, tree, target,
                       extra_roots=host.VERIFICATION_TARGETS if verification else ())
    if plan is None:
        return False

    needed = set(plan["restore_modules"])
    selected = [row for row in tree["members"] if row["module"] is None or row["module"] in needed]
    # A residual is an authenticated frame of native binaries, reports or other
    # assets. Reserve every omitted module's paths so it cannot also supply Lean data.
    reserved = {PurePosixPath(path) for row in tree["members"]
                if row["module"] is not None and row["module"] not in needed for path in row["paths"]}
    lake.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".sparse-seed-", dir=lake) as temporary:
        temp = Path(temporary)
        unpacked = temp / "build"
        unpacked.mkdir()
        spans = remote.asset_ranges(host, manifest, tree, selected, deadline, {})
        requests, transferred = index.extract_ranges(host.range_blocks, spans, selected, unpacked, reserved, deadline)
        # Older residual frames may mix reports and native programs with ordinary
        # cache data. Only a selected module frame can supply inspector compiler files.
        compiler_paths = {PurePosixPath(path) for row in selected if row["module"] is not None
                          for path in row["paths"]}
        for path in tuple(reserved):
            if inspector_build_path(path) and path not in compiler_paths:
                (unpacked / path).unlink(missing_ok=True)
                reserved.remove(path)
        invalidated = check_native_products(unpacked, manifest["partition"], staging=True)
        if invalidated:
            host.receipt("sparse-seed", "full-fallback", tag=tag, native_products=invalidated)
            return False
        reason = native_fallback(unpacked, reserved, tree, plan)
        if reason is not None:
            host.receipt("sparse-seed", "full-fallback", tag=tag, native_product=reason)
            return False
        plan["base_partition"] = manifest["partition"]
        plan["base_scope"] = manifest["volume_scope"]
        plan["base_cache_key"] = manifest["cache_key"]
        if installed and any(installed.get(field) != value for field, value in plan.items()):
            raise ValueError("previous sparse installation differs from the authenticated snapshot")
        staged_plan = temp / "plan.json"
        staged_plan.write_text(json.dumps(plan, sort_keys=True) + "\n")
        # The marker authorizes replay after interruption between these renames.
        # Only a pinned, authenticated repeat can move a nonempty build aside.
        backup = temp / "previous"
        if installed and build.exists():
            build.replace(backup)
        try:
            staged_plan.replace(marker)
            unpacked.replace(build)
        except BaseException:
            if backup.exists():
                backup.replace(build)
            if not installed:
                marker.unlink(missing_ok=True)
            raise
    if github_env := os.environ.get("GITHUB_ENV"):
        with open(github_env, "a", encoding="utf-8") as env:
            print(f"LEAN_CACHE_SPARSE_PLAN={marker}", file=env)
    host.receipt("sparse-seed", "installed", producer=plan["base_commit"], tag=tag,
                 rebuild_modules=len(plan["rebuild_modules"]), restored_modules=len(needed),
                 inherited_modules=len(plan["inherit_modules"]), requests=requests, payload_bytes=transferred)
    return True


def read_plan(root):
    marker = plan_path(root)
    configured = os.environ.get("LEAN_CACHE_SPARSE_PLAN")
    if configured and Path(configured) != marker:
        raise ValueError("sparse Release plan does not belong to this workspace")
    if marker.is_symlink():
        raise ValueError("sparse Release plan must be a regular file")
    if not marker.exists() and not configured:
        return None
    plan = json.loads(marker.read_bytes())
    if plan.get("schema") != SCHEMA or compiler.checkout(root) != plan.get("target_commit"):
        raise ValueError("sparse Release plan does not match the target checkout")
    return plan


def bound_snapshot(host, root, stage, partition, key, verification, plan, deadline):
    """Reopen the precise predecessor even if another publication has become newest."""
    tag = plan["base_tag"]
    if (plan["base_partition"] != partition or plan["base_cache_key"] != key
            or host.snapshot_partition(root, partition, tag, key, indexed=True,
                                       verification=verification) != partition):
        raise ValueError("sparse Release predecessor address does not match this publisher")
    snapshot = host.snapshot_manifest(partition, tag, stage, deadline, cache_key=key,
                                      verification=verification)
    manifest = snapshot[1]
    if (host.archive_sha(stage / host.MANIFEST, deadline) != plan["base_manifest_sha256"]
            or manifest["producer_commit_sha"] != plan["base_commit"] or manifest["schema"] != "lean-release-seed-v6"
            or manifest["volume_scope"] != plan["base_scope"]):
        raise ValueError("sparse Release predecessor identity differs from the installed seed")
    return snapshot


def overlay_base(host, root, stage, partition, key, verification, scope, plan, deadline):
    import lean_release_volume_remote as remote

    if scope != plan["base_scope"]:
        raise ValueError("sparse Release cannot inherit volumes from a different storage scope")
    snapshot = bound_snapshot(host, root, stage, partition, key, verification, plan, deadline)
    manifest = snapshot[1]
    tree = remote.load_tree(host, plan["base_tag"], stage, manifest, deadline)
    checked = sparse_plan(root, plan["base_tag"], manifest, plan["base_manifest_sha256"],
                          tree, plan["target_commit"], extra_roots=host.VERIFICATION_TARGETS if verification else ())
    if checked is None or any(plan.get(field) != value for field, value in checked.items()):
        raise ValueError("sparse Release inputs differ from the authenticated predecessor and checkout")
    return manifest, tree


def full_fallback(host, root, partition, key, verification, plan, deadline):
    # Only the authenticated workflow owns this disposable sparse build.
    if (not hosted_workspace(root)
            or (root / ".lake").is_symlink() or (root / ".lake/build").is_symlink()):
        raise ValueError("full retry requires the private hosted publisher workspace")
    with host.cache_guard(root), tempfile.TemporaryDirectory(prefix="lean-rolling-full-") as temporary:
        stage = Path(temporary)
        snapshot = bound_snapshot(host, root, stage, partition, key, verification, plan, deadline)
        if read_plan(root) != plan:
            raise ValueError("sparse Release plan changed before full retry")
        marker = plan_path(root)
        if not plan.get("restoring_full"):
            plan = dict(plan, restoring_full=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=marker.parent,
                                             delete=False) as output:
                json.dump(plan, output, sort_keys=True)
                temporary_plan = Path(output.name)
            try:
                temporary_plan.replace(marker)
            finally:
                temporary_plan.unlink(missing_ok=True)
        build = root / ".lake/build"
        if build.exists():
            shutil.rmtree(build)
        if not host.restore_snapshot(root, partition, plan["base_tag"], stage, deadline,
                                     expected={"producer_commit_sha": plan["base_commit"]}, refresh_stale=True,
                                     verification_tag=verification, cache_key=key,
                                     validated_snapshot=snapshot):
            raise ValueError("full retry did not restore the pinned predecessor")
        marker.unlink()
    os.environ.pop("LEAN_CACHE_SPARSE_PLAN", None)
    if github_env := os.environ.get("GITHUB_ENV"):
        with open(github_env, "a", encoding="utf-8") as env:
            print("LEAN_CACHE_SPARSE_PLAN=", file=env)
    host.receipt("sparse-seed", "full-fallback", tag=plan["base_tag"], target=plan["target_commit"])


def prepare_cache(host, root, command, partition, verification, plan):
    if plan.get("restoring_full"):
        full_fallback(host, root, partition, host.release_key(root), verification, plan, host.operation_deadline())
        return subprocess.run(command, cwd=root, env=dict(os.environ, PARTIAL_INDEXED_PROJECT="true"))
    if plan["rebuild_modules"]:
        compiler.compile_roots(root, workspace_modules(root), plan["rebuild_modules"])
    return subprocess.CompletedProcess(command, 0)
