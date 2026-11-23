"""GitHub objects shared by paired tools pinning and delivery."""

import base64
import json
import re
import subprocess


TOOLS = "the-omega-institute/trureturing-tools"
CONSUMER = "the-omega-institute/trureturing"
PIN = "Meta/tools-release.json"
ASSET = "trureturing-tools.tar.gz"
SHA = r"[0-9a-f]{40}"
VERSION = r"v[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?"
PR_PREVIEW = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+-pr\.([1-9][0-9]*)\.[1-9][0-9]*\.g([0-9a-f]{12})")
BASE_TRAILER = "Consumer-Tools-Base"
ACTIONS_BOT = 41898282


def api(repo, endpoint, *args):
    response = subprocess.check_output(["gh", "api", f"repos/{repo}/{endpoint}", *args])
    return json.loads(response) if response.strip() else None


def branch_tip(repo, branch="dev"):
    return api(repo, f"git/ref/heads/{branch}")["object"]["sha"]


def tag_source(version, *, lightweight=False):
    if not re.fullmatch(VERSION, version):
        raise ValueError(f"invalid tools release tag: {version}")
    ref = api(TOOLS, f"git/ref/tags/{version}")["object"]
    if not lightweight and ref["type"] == "tag":
        ref = api(TOOLS, f"git/tags/{ref['sha']}")["object"]
    if ref["type"] != "commit" or not re.fullmatch(SHA, ref["sha"]):
        raise ValueError(f"{version} must point to a tools commit")
    suffix = re.search(r"\.g([0-9a-f]{12}|[0-9a-f]{40})$", version)
    if suffix and not ref["sha"].startswith(suffix[1]):
        raise ValueError(f"{version} points to a different tools commit")
    return ref["sha"]


def base_trailers(source):
    message = api(TOOLS, f"git/commits/{source}")["message"]
    trailers = subprocess.check_output(["git", "interpret-trailers", "--parse"], input=message, text=True)
    return [value.strip() for line in trailers.splitlines()
            for key, _, value in [line.partition(":")] if key.lower() == BASE_TRAILER.lower()]


def read_pin(revision):
    data = api(CONSUMER, f"contents/{PIN}?ref={revision}")
    if data["type"] != "file" or data["encoding"] != "base64" or not re.fullmatch(SHA, data["sha"]):
        raise ValueError(f"expected a consumer pin blob at {revision}")
    return data["sha"], json.loads(base64.b64decode(data["content"]))


def artifact_fields(pin):
    for key in ("previous_pin", "workflow_sha"):
        if key in pin and (not isinstance(pin[key], str) or not re.fullmatch(SHA, pin[key])):
            raise ValueError(f"Invalid {key} in consumer pin")
    return {key: value for key, value in pin.items() if key not in {"previous_pin", "workflow_sha"}}


def contains(repo, source, tip):
    return source == tip or api(repo, f"compare/{source}...{tip}?per_page=1")["merge_base_commit"]["sha"] == source


def tools_dev_pull(pull):
    return (pull["base"]["ref"] == "dev" and pull["base"]["repo"]["full_name"] == TOOLS
            and not pull["draft"])
