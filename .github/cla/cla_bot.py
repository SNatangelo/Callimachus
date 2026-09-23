# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Record version-specific CLA acceptances in a private GitHub repository.

Run only from trusted default-branch workflow code. Never check out a PR head.
Uses the Python standard library and GitHub REST/GraphQL APIs; no hosted CLA app.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.request import Request, urlopen

SOURCE = "SNatangelo/Callimachus"
ARCHIVE = "SNatangelo/callimachus-legal"
OWNER_ID = 248719534
VERSION = "1.1"
AGREEMENT_PATH = ".github/cla/agreements/CLA-v1.1.md"
AGREEMENT_SHA256 = "f89491e1cf64c52a5119adabe9928c665af0392c23af76258ce4446f003d9634"
SIGN = "I have read and accept the Callimachus CLA v1.1 for my own contributions."
STATUS = "cla/signatures"
MARKER = f"<!-- callimachus-cla:{VERSION}:{AGREEMENT_SHA256} -->"


class Failure(RuntimeError):
    pass


class ApiError(Failure):
    def __init__(self, status: int):
        self.status = status
        super().__init__(f"GitHub API returned HTTP {status}; check configuration and permissions.")


class API:
    def __init__(self, token: str):
        self.token = token

    def request(self, method: str, path: str, data=None):
        # Callers supply only API-relative paths, never URLs from PRs or comments.
        if not path.startswith("/") or path.startswith("//"):
            raise Failure("Invalid API path")
        body = None if data is None else json.dumps(data).encode()
        headers = {"Authorization": f"Bearer {self.token}",
                   "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28",
                   "User-Agent": "Callimachus-CLA", "Content-Type": "application/json"}
        req = Request("https://api.github.com" + path, data=body, headers=headers, method=method)
        try:
            with urlopen(req, timeout=30) as response:
                raw = response.read()
                return json.loads(raw) if raw else None
        except HTTPError as exc:
            # Never print response bodies, private records, or authentication headers.
            raise ApiError(exc.code) from None

    def pages(self, path: str):
        for page in range(1, 101):
            items = self.request("GET", f"{path}?per_page=100&page={page}")
            if not isinstance(items, list):
                raise Failure("Unexpected paginated response")
            yield from items
            if len(items) < 100:
                return
        raise Failure("Pagination limit exceeded; manual review required")


def read_file(api: API, repo: str, path: str, ref="main"):
    try:
        result = api.request("GET", f"/repos/{repo}/contents/{path}?ref={ref}")
    except ApiError as exc:
        if exc.status == 404:
            return None
        raise
    if not isinstance(result, dict) or result.get("encoding") != "base64":
        raise Failure("Expected a small base64-encoded file")
    return base64.b64decode(result["content"], validate=False)


def put_once(api: API, path: str, content: bytes):
    """Create immutable evidence, refusing to overwrite different content."""
    for attempt in range(4):
        existing = read_file(api, ARCHIVE, path)
        if existing is not None:
            if existing != content:
                raise Failure("Existing archive evidence differs; do not overwrite it")
            return
        try:
            api.request("PUT", f"/repos/{ARCHIVE}/contents/{path}", {
                "message": f"Archive CLA v{VERSION} evidence: {path}",
                "branch": "main", "content": base64.b64encode(content).decode()})
            return
        except ApiError as exc:
            if exc.status not in (409, 422) or attempt == 3:
                raise
            time.sleep(1 + attempt)
    raise Failure("Could not preserve evidence")


def json_bytes(value):
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def comment_is_acceptance(comment, prompted_at: str, contributor_ids: set[int]):
    user = comment.get("user") or {}
    return (user.get("type") == "User" and user.get("id") in contributor_ids
            and (comment.get("body") or "").strip() == SIGN
            and bool(prompted_at)
            and (comment.get("updated_at") or comment["created_at"]) >= prompted_at)


def valid_signature(record, user_id):
    return (isinstance(record, dict) and record.get("schema") == 1
            and record.get("github_user_id") == user_id
            and record.get("agreement_version") == VERSION
            and record.get("agreement_sha256") == AGREEMENT_SHA256
            and record.get("acceptance_method") == "github_comment_own_rights"
            and record.get("comment_body", "").strip() == SIGN
            and isinstance(record.get("comment_id"), int)
            and bool(record.get("accepted_at")))


def contributor_ids(api: API, pr_number: int, opener):
    """Include the submitter and every GitHub-linked author/coauthor, not just committer."""
    people = {opener["id"]: opener["login"]}
    query = """query($number:Int!, $after:String) {
      repository(owner:"SNatangelo", name:"Callimachus") {
        pullRequest(number:$number) { commits(first:100, after:$after) {
          nodes { commit { authors(first:100) {
            nodes { user { databaseId login } } pageInfo { hasNextPage }
          } } } pageInfo { hasNextPage endCursor }
        } }
      }
    }"""
    cursor = None
    for _ in range(100):
        result = api.request("POST", "/graphql", {"query": query, "variables": {
            "number": pr_number, "after": cursor}})
        if result.get("errors"):
            raise Failure("Unable to enumerate all PR authors")
        connection = result["data"]["repository"]["pullRequest"]["commits"]
        for node in connection["nodes"]:
            authors = node["commit"]["authors"]
            if authors["pageInfo"]["hasNextPage"]:
                raise Failure("More than 100 authors on a commit; manual review required")
            for actor in authors["nodes"]:
                user = actor.get("user")
                if not user or not isinstance(user.get("databaseId"), int):
                    raise Failure("An author/coauthor is not linked to GitHub; resolve attribution before merge")
                people[user["databaseId"]] = user["login"]
        if not connection["pageInfo"]["hasNextPage"]:
            # The original maintainer already owns his own contributions. No bot wildcard exemptions.
            people.pop(OWNER_ID, None)
            return people
        cursor = connection["pageInfo"]["endCursor"]
    raise Failure("Author pagination limit exceeded")


def execute(public: API, private: API, event: dict, env: dict):
    if env.get("GITHUB_REPOSITORY", "").lower() != SOURCE.lower():
        raise Failure("This workflow is restricted to SNatangelo/Callimachus")
    number = (event.get("pull_request") or event.get("issue") or {}).get("number")
    if not isinstance(number, int) or ("issue" in event and not event["issue"].get("pull_request")):
        raise Failure("Only pull requests are supported")
    pr = public.request("GET", f"/repos/{SOURCE}/pulls/{number}")
    if pr["state"] != "open":
        return
    sha = pr["head"]["sha"]
    run_url = f"https://github.com/{SOURCE}/actions/runs/{env['GITHUB_RUN_ID']}"
    ref = env.get("CLA_WORKFLOW_SHA", "")
    if not re.fullmatch(r"[0-9a-f]{40}", ref):
        raise Failure("Missing trusted workflow commit")

    def status(state, description):
        public.request("POST", f"/repos/{SOURCE}/statuses/{sha}", {
            "state": state, "context": STATUS, "description": description[:140], "target_url": run_url})

    status("pending", "Checking CLA acceptance and private evidence archive")
    try:
        if not private.token:
            raise Failure("Configure CLA_ARCHIVE_TOKEN before accepting external contributions")
        repo = private.request("GET", f"/repos/{ARCHIVE}")
        if repo.get("private") is not True or repo.get("full_name", "").lower() != ARCHIVE.lower():
            raise Failure("Signature archive must be the designated PRIVATE repository")
        # Fail early for an empty/missing archive branch; create the private repository with a README.
        private.request("GET", f"/repos/{ARCHIVE}/branches/main")
        document = read_file(public, SOURCE, AGREEMENT_PATH, ref)
        root = read_file(public, SOURCE, "CLA.md", ref)
        if document is None or document != root or hashlib.sha256(document).hexdigest() != AGREEMENT_SHA256:
            raise Failure("CLA text/hash mismatch; use a new version instead of changing signed terms")
        archive_path = f"agreements/v{VERSION}/CLA.md"
        put_once(private, archive_path, document)
        # Content-bound manifest stays stable even when unrelated workflow commits change.
        put_once(private, f"agreements/v{VERSION}/manifest.json", json_bytes({
            "schema": 1, "version": VERSION, "sha256": AGREEMENT_SHA256,
            "source_repository": SOURCE, "source_path": AGREEMENT_PATH,
            "archive_path": archive_path, "signature_text": SIGN}))
        people = contributor_ids(public, number, pr["user"])
        comments = list(public.pages(f"/repos/{SOURCE}/issues/{number}/comments"))
        prompts = [c for c in comments if MARKER in (c.get("body") or "")
                   and (c.get("user") or {}).get("login") == "github-actions[bot]"
                   and (c.get("user") or {}).get("type") == "Bot"]
        prompted_at = min((c["created_at"] for c in prompts), default="")
        document_url = f"https://github.com/{SOURCE}/blob/{ref}/{AGREEMENT_PATH}"
        # Scan the whole thread: concurrency queues may replace pending events, not signed comments.
        for comment in comments:
            if not comment_is_acceptance(comment, prompted_at, set(people)):
                continue
            uid = comment["user"]["id"]
            signature_path = f"signatures/v{VERSION}/github-{uid}.json"
            old = read_file(private, ARCHIVE, signature_path)
            if old is not None:
                if not valid_signature(json.loads(old), uid):
                    raise Failure("Invalid existing signature record; manual review required")
                continue
            record = {
                "schema": 1, "agreement_version": VERSION, "agreement_sha256": AGREEMENT_SHA256,
                "agreement_url": document_url, "agreement_source_commit": ref,
                "github_user_id": uid, "github_username": comment["user"]["login"],
                "acceptance_method": "github_comment_own_rights", "comment_id": comment["id"],
                "comment_url": comment["html_url"], "comment_body": comment["body"],
                "comment_created_at": comment["created_at"],
                "accepted_at": comment.get("updated_at") or comment["created_at"],
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "source_repository": SOURCE, "source_repository_id": event["repository"]["id"],
                "pull_request": number, "pull_request_url": pr["html_url"],
                "observed_head_sha": sha, "workflow_run_url": run_url}
            put_once(private, signature_path, json_bytes(record))
        missing = []
        for uid, login in people.items():
            raw = read_file(private, ARCHIVE, f"signatures/v{VERSION}/github-{uid}.json")
            if raw is None or not valid_signature(json.loads(raw), uid):
                missing.append(login)
        # The bot comment discloses no data retrieved from the private archive.
        state_text = ("Awaiting acceptance: " + ", ".join("@" + x for x in sorted(missing))) if missing else "All identified contributors are covered by the configured check."
        if missing:
            body = (f"{MARKER}\n## CLA signature required\n\n"
                    "Thanks for contributing to Callimachus. Before this PR can be merged:\n\n"
                    f"1. **Read the [Callimachus CLA v{VERSION}]({document_url}).**\n"
                    "2. If you agree **and you personally own the rights to your contribution**, copy the exact sentence below.\n"
                    "3. Post it as a new comment in this pull request.\n\n"
                    f"```text\n{SIGN}\n```\n\n"
                    f"**Waiting for:** {', '.join('@' + x for x in sorted(missing))}\n\n"
                    "You keep the copyright in your contribution. The CLA permits the maintainer to distribute it under the public AGPL licence and under alternative licences, including paid proprietary licences, subject to the CLA terms.\n\n"
                    "**If your employer or another organisation owns the rights to this contribution, do not use the sentence above.** Contact hello@callimachus.science so the actual rights holder can provide the required authorisation.\n\n"
                    "The acceptance comment is public; the detailed evidence archive is private. Do not post email addresses, legal names or employer documents here. "
                    "Comment `recheck` if the check needs to be run again.\n")
        else:
            body = (f"{MARKER}\n## CLA verified\n\n"
                    "All identified contributors are covered by the Callimachus CLA check. ✅\n\n"
                    f"[View the exact Callimachus CLA v{VERSION}]({document_url}).\n")
        if prompts:
            if prompts[0]["body"] != body:
                public.request("PATCH", f"/repos/{SOURCE}/issues/comments/{prompts[0]['id']}", {"body": body})
        else:
            public.request("POST", f"/repos/{SOURCE}/issues/{number}/comments", {"body": body})
        if missing:
            status("failure", "CLA acceptance missing; follow the bot comment")
            return
        # This record binds the successful check to the exact PR head and version.
        put_once(private, f"checks/v{VERSION}/pr-{number}/{sha}.json", json_bytes({
            "schema": 1, "agreement_sha256": AGREEMENT_SHA256,
            "repository": SOURCE, "pull_request": number, "head_sha": sha,
            "github_user_ids": sorted(people), "maintainer_exemption_id": OWNER_ID}))
        status("success", "CLA acceptance verified; versioned evidence preserved privately")
    except Exception:
        status("failure", "CLA check/archive unavailable; inspect workflow configuration")
        raise


def main():
    with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as handle:
        event = json.load(handle)
    try:
        execute(API(os.environ["GITHUB_TOKEN"]), API(os.environ.get("CLA_ARCHIVE_TOKEN", "")), event, os.environ)
    except Failure as exc:
        print(f"CLA check failed: {exc}", file=sys.stderr)
        return 1
    except Exception:
        print("CLA check failed unexpectedly; no success status was granted.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
