"""Open a review for pinned tools or inspect open tools PRs against dev."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile

from pin import REPOSITORY, release_pin

CONSUMER = "the-omega-institute/trureturing"
REPO_URL = f"https://github.com/{REPOSITORY}"
PULLS = """
query($endCursor: String) {
  repository(owner: "the-omega-institute", name: "trureturing-tools") {
    pullRequests(states: OPEN, first: 100, after: $endCursor) {
      nodes { number url title isDraft baseRefName baseRefOid headRefOid headRepository { nameWithOwner } }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""


def gh_json(*args):
    return json.loads(subprocess.check_output(["gh", "api", *args], text=True))


def tools_api(endpoint, *args):
    return gh_json(f"repos/{REPOSITORY}/{endpoint}", *args)


def open_pulls():
    pages = gh_json("graphql", "--paginate", "--slurp", "-f", f"query={PULLS}")
    return [pull for page in pages for pull in page["data"]["repository"]["pullRequests"]["nodes"]]


def contains(source, tip):
    return source == tip or tools_api(f"compare/{source}...{tip}?per_page=1")["status"] in {"ahead", "identical"}


def existing_pr(source):
    candidates = [pr for pr in open_pulls() if pr["baseRefName"] == "dev"
                  and (pr["headRepository"] or {}).get("nameWithOwner") == REPOSITORY]
    exact = [pr for pr in candidates if pr["headRefOid"] == source]
    found = exact or [pr for pr in candidates if contains(source, pr["headRefOid"])]
    return "\n".join(
        f"{pr['url']} ({'draft' if pr['isDraft'] else 'open'}, head {pr['headRefOid']}, "
        f"{'exact source' if pr['headRefOid'] == source else 'contains source and additional commits'})"
        for pr in found)


def source_pr(lockfile):
    if lockfile:
        pin = json.loads(lockfile.read_text())
        provenance = "The proposed consumer lock"
    else:
        adoption = gh_json(f"repos/{CONSUMER}/git/ref/heads/dev")["object"]["sha"]
        pin = gh_json(f"repos/{CONSUMER}/contents/Meta/tools-release.json?ref={adoption}",
                      "-H", "Accept: application/vnd.github.raw+json")
        provenance = f"The [consumer dev lock](https://github.com/{CONSUMER}/blob/{adoption}/Meta/tools-release.json)"
    tag = pin["version"]
    if release_pin(tag) != pin:
        raise ValueError(f"Consumer pin differs from the published {tag} archive")
    ref = tools_api(f"git/ref/tags/{tag}")["object"]
    source = ref["sha"]
    if ref["type"] != "commit" or not re.fullmatch("[0-9a-f]{40}", source):
        raise ValueError(f"Expected {tag} to point to its release commit")
    preview_sha = re.search(r"\.g([0-9a-f]{12})$", tag)
    if preview_sha and not source.startswith(preview_sha[1]):
        raise ValueError(f"Tag {tag} points to a different source commit")
    if contains(source, tools_api("git/ref/heads/dev")["object"]["sha"]):
        return f"{tag} ({source}) is already in {REPOSITORY}/dev history"
    existing = existing_pr(source)
    if existing:
        return existing

    branch = f"adopted-tools/{source}"
    try:
        tools_api("git/refs", "-f", f"ref=refs/heads/{branch}", "-f", f"sha={source}")
    except subprocess.CalledProcessError:
        # A concurrent invocation or an interrupted run may own the same branch.
        if tools_api(f"git/ref/heads/{branch}")["object"]["sha"] != source:
            raise ValueError(f"Existing branch {branch} differs from the pinned source")
    body = (f"{provenance} selects [{tag}]({REPO_URL}/releases/tag/{tag}). "
            f"This PR submits source commit `{source}` for review against tools/dev.")
    try:
        pull = tools_api("pulls", "-f", f"title=Review the tools source pinned in {tag}",
                         "-f", f"body={body}", "-f", f"head={branch}", "-f", "base=dev")
    except subprocess.CalledProcessError:
        existing = existing_pr(source)
        if existing:
            return existing
        raise
    if pull["head"]["sha"] != source:
        raise ValueError(f"{pull['html_url']} head changed during creation; check it before review")
    return f"{pull['html_url']} (open, head {source})"


def git(repo, *args):
    return subprocess.check_output(["git", "-C", repo, *args], text=True).strip()


def relationship(repo, dev, base, head):
    history = subprocess.run(["git", "-C", repo, "merge-base", "--is-ancestor", head, dev])
    if history.returncode == 0:
        return "in dev history"
    if history.returncode != 1:
        history.check_returncode()
    dev_tree = git(repo, "rev-parse", dev + "^{tree}")
    if dev_tree == git(repo, "rev-parse", head + "^{tree}"):
        return "same tree as dev"

    fork = git(repo, "merge-base", base, head)
    # Reapply only this PR's net change; its base may itself be an unmerged feature.
    replay = subprocess.run(["git", "-C", repo, "merge-tree", "--write-tree", "--merge-base=" + fork, dev, head],
                            capture_output=True, text=True)
    if replay.returncode == 1:
        return "review (conflict replaying PR delta on dev)"
    replay.check_returncode()
    if replay.stdout.strip() == dev_tree:
        return "patch present in current dev"
    patches = git(repo, "cherry", dev, head, fork).splitlines()
    if patches and all(row.startswith("- ") for row in patches):
        return "patches in dev history; review current difference"
    return "distinct PR delta; assess usefulness"


def inventory():
    pulls = open_pulls()
    dev = tools_api("git/ref/heads/dev")["object"]["sha"]
    print(f"tools/dev {dev}")
    if not pulls:
        print("No open pull requests.")
        return
    # Share existing objects read-only. Any fetched objects and merge trees are temporary.
    objects = git(".", "rev-parse", "--path-format=absolute", "--git-path", "objects")
    with tempfile.TemporaryDirectory(prefix="tools-pr-inventory-") as directory:
        git(directory, "init", "--bare", "--quiet")
        (Path(directory) / "objects/info/alternates").write_text(objects + "\n")
        revisions = {dev, *(pr[key] for pr in pulls for key in ("baseRefOid", "headRefOid"))}
        missing = [sha for sha in sorted(revisions) if subprocess.run(
            ["git", "-C", directory, "cat-file", "-e", sha + "^{commit}"], stderr=subprocess.DEVNULL).returncode]
        if missing:
            git(directory, "fetch", "--quiet", "--no-tags", REPO_URL + ".git", *missing)
        for pr in pulls:
            try:
                state = relationship(directory, dev, pr["baseRefOid"], pr["headRefOid"])
            except subprocess.CalledProcessError as error:
                state = f"review (Git comparison failed: {error})"
            flags = "draft" if pr["isDraft"] else "open"
            print(f"#{pr['number']} [{flags}, base {pr['baseRefName']}, {pr['headRefOid'][:12]}] "
                  f"{state} | {pr['title']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--lock", type=Path, help="use this proposed Meta/tools-release.json")
    group.add_argument("--list", action="store_true", help="classify open PRs without publishing")
    args = parser.parse_args()
    try:
        if args.list:
            inventory()
        else:
            print(source_pr(args.lock))
    except (KeyError, ValueError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"tools source PR: {error}\n")
