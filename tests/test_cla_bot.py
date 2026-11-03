import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".github" / "cla"))
import cla_bot


BOT_ID = 49699333
BOT_LOGIN = "dependabot[bot]"
HUMAN_ID = 91234567
HUMAN_LOGIN = "alice"


def make_pr(*, user=None, head_repository=cla_bot.SOURCE, head_fork=False,
            base_repository=cla_bot.SOURCE, head_ref="dependabot/update"):
    return {
        "user": user or {"id": BOT_ID, "login": BOT_LOGIN, "type": "Bot"},
        "head": {
            "ref": head_ref,
            "repo": {"full_name": head_repository, "fork": head_fork},
        },
        "base": {"repo": {"full_name": base_repository}},
    }


class GraphQLAPI:
    def __init__(self, authors):
        self.authors = authors

    def request(self, method, path, data):
        assert method == "POST"
        assert path == "/graphql"
        assert data["variables"]["number"] == 18
        return {
            "data": {
                "repository": {
                    "pullRequest": {
                        "commits": {
                            "nodes": [{
                                "commit": {
                                    "authors": {
                                        "nodes": self.authors,
                                        "pageInfo": {"hasNextPage": False},
                                    }
                                }
                            }],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                }
            }
        }


def author(user_id, login):
    return {"user": {"databaseId": user_id, "login": login}}


def test_only_exact_same_repository_dependabot_bot_opener_is_exempt():
    pr = make_pr()
    assert cla_bot.trusted_dependabot_exemption(pr) == {
        "github_user_id": BOT_ID,
        "github_username": BOT_LOGIN,
        "identity_type": "Bot",
        "reason": cla_bot.DEPENDABOT_EXEMPTION_REASON,
        "head_repository": cla_bot.SOURCE,
        "base_repository": cla_bot.SOURCE,
    }


@pytest.mark.parametrize(
    "pr",
    [
        make_pr(head_repository="fork-owner/Callimachus", head_fork=True),
        make_pr(base_repository="somewhere-else/Callimachus"),
        make_pr(user={"id": 999, "login": BOT_LOGIN, "type": "Bot"}),
        make_pr(user={"id": BOT_ID, "login": "someone-else[bot]", "type": "Bot"}),
        make_pr(user={"id": BOT_ID, "login": BOT_LOGIN, "type": "User"}),
        make_pr(user={"id": 999, "login": "someone-else[bot]", "type": "Bot"}),
    ],
)
def test_fork_and_spoofed_bot_identities_are_not_exempt(pr):
    assert cla_bot.trusted_dependabot_exemption(pr) is None
    people = cla_bot.contributor_ids(
        GraphQLAPI([]), 18, pr["user"], pr_context=pr
    )
    assert pr["user"]["id"] in people


def test_mixed_dependabot_pr_still_requires_human_author():
    pr = make_pr(head_ref="dependabot/security-update")
    exemption = cla_bot.trusted_dependabot_exemption(pr)
    assert exemption is not None
    people = cla_bot.contributor_ids(
        GraphQLAPI([
            author(BOT_ID, BOT_LOGIN),
            author(HUMAN_ID, HUMAN_LOGIN),
            author(cla_bot.OWNER_ID, "SNatangelo"),
        ]),
        18,
        pr["user"],
        pr_context=pr,
    )
    assert people == {HUMAN_ID: HUMAN_LOGIN}


def test_forked_dependabot_pr_keeps_bot_and_human_authors_required():
    pr = make_pr(head_repository="fork-owner/Callimachus", head_fork=True,
                 head_ref="dependabot/security-update")
    people = cla_bot.contributor_ids(
        GraphQLAPI([author(BOT_ID, BOT_LOGIN), author(HUMAN_ID, HUMAN_LOGIN)]),
        18,
        pr["user"],
        pr_context=pr,
    )
    assert people == {BOT_ID: BOT_LOGIN, HUMAN_ID: HUMAN_LOGIN}


def test_verified_bot_does_not_bypass_unlinked_human_author():
    pr = make_pr()
    with pytest.raises(cla_bot.Failure, match="not linked to GitHub"):
        cla_bot.contributor_ids(
            GraphQLAPI([author(BOT_ID, BOT_LOGIN), {"user": None}]),
            18,
            pr["user"],
            pr_context=pr,
        )


def test_verified_bot_identity_mismatch_in_commit_authors_fails_closed():
    pr = make_pr()
    with pytest.raises(cla_bot.Failure, match="identity differs"):
        cla_bot.contributor_ids(
            GraphQLAPI([author(BOT_ID, "not-dependabot[bot]")]),
            18,
            pr["user"],
            pr_context=pr,
        )


def test_bot_cannot_supply_human_acceptance():
    prompted_at = "2026-01-01T00:00:00Z"
    bot_comment = {
        "user": {"id": BOT_ID, "login": BOT_LOGIN, "type": "Bot"},
        "body": cla_bot.SIGN,
        "created_at": "2026-01-02T00:00:00Z",
    }
    human_comment = {
        "user": {"id": HUMAN_ID, "login": HUMAN_LOGIN, "type": "User"},
        "body": cla_bot.SIGN,
        "created_at": "2026-01-02T00:00:00Z",
    }
    assert not cla_bot.comment_is_acceptance(bot_comment, prompted_at, {BOT_ID, HUMAN_ID})
    assert cla_bot.comment_is_acceptance(human_comment, prompted_at, {HUMAN_ID})


class WorkflowPublicAPI:
    def __init__(self, pr, authors):
        self.pr = pr
        self.authors = authors

    def request(self, method, path, data=None):
        if method == "GET" and path == f"/repos/{cla_bot.SOURCE}/pulls/18":
            return self.pr
        if method == "POST" and path == "/graphql":
            return {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "commits": {
                                "nodes": [{
                                    "commit": {
                                        "authors": {
                                            "nodes": self.authors,
                                            "pageInfo": {"hasNextPage": False},
                                        }
                                    }
                                }],
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                            }
                        }
                    }
                }
            }
        if method == "POST" and (
            path.startswith(f"/repos/{cla_bot.SOURCE}/statuses/")
            or path == f"/repos/{cla_bot.SOURCE}/issues/18/comments"
        ):
            return {}
        raise AssertionError(f"Unexpected public API request: {method} {path}")

    def pages(self, path):
        assert path == f"/repos/{cla_bot.SOURCE}/issues/18/comments"
        return []


class WorkflowPrivateAPI:
    token = "test-token"

    def request(self, method, path, data=None):
        if method == "GET" and path == f"/repos/{cla_bot.ARCHIVE}":
            return {"private": True, "full_name": cla_bot.ARCHIVE}
        if method == "GET" and path == f"/repos/{cla_bot.ARCHIVE}/branches/main":
            return {}
        raise AssertionError(f"Unexpected private API request: {method} {path}")


def run_successful_check_record(monkeypatch, *, trusted_bot):
    document = b"test-only CLA text"
    agreement_hash = hashlib.sha256(document).hexdigest()
    monkeypatch.setattr(cla_bot, "AGREEMENT_SHA256", agreement_hash)
    head_sha = "a" * 40
    if trusted_bot:
        pr = make_pr()
        authors = [author(BOT_ID, BOT_LOGIN), author(HUMAN_ID, HUMAN_LOGIN)]
    else:
        opener = {"id": HUMAN_ID, "login": HUMAN_LOGIN, "type": "User"}
        pr = make_pr(user=opener)
        authors = [author(HUMAN_ID, HUMAN_LOGIN)]
    pr.update({
        "state": "open",
        "head": {**pr["head"], "sha": head_sha},
        "html_url": f"https://github.com/{cla_bot.SOURCE}/pull/18",
    })

    def fake_read_file(api, repo, path, ref="main"):
        if repo == cla_bot.SOURCE and path in (cla_bot.AGREEMENT_PATH, "CLA.md"):
            return document
        if repo == cla_bot.ARCHIVE and path.startswith(f"signatures/v{cla_bot.VERSION}/github-"):
            record = {
                "schema": 1,
                "github_user_id": HUMAN_ID,
                "agreement_version": cla_bot.VERSION,
                "agreement_sha256": agreement_hash,
                "acceptance_method": "github_comment_own_rights",
                "comment_body": cla_bot.SIGN,
                "comment_id": 1,
                "accepted_at": "2026-01-02T00:00:00Z",
            }
            return cla_bot.json_bytes(record)
        return None

    writes = []
    monkeypatch.setattr(cla_bot, "read_file", fake_read_file)
    monkeypatch.setattr(cla_bot, "put_once", lambda api, path, content: writes.append((path, content)))
    cla_bot.execute(
        WorkflowPublicAPI(pr, authors),
        WorkflowPrivateAPI(),
        {"repository": {"id": 123}, "pull_request": {"number": 18}},
        {
            "GITHUB_REPOSITORY": cla_bot.SOURCE,
            "GITHUB_RUN_ID": "12345",
            "CLA_WORKFLOW_SHA": "b" * 40,
        },
    )
    path = f"checks/v{cla_bot.VERSION}/pr-18/{head_sha}.json"
    return next(content for written_path, content in writes if written_path == path), agreement_hash


def test_nonbot_check_record_keeps_legacy_bytes_and_bot_is_audited_separately(monkeypatch):
    human_bytes, agreement_hash = run_successful_check_record(monkeypatch, trusted_bot=False)
    legacy_record = {
        "schema": 1,
        "agreement_sha256": agreement_hash,
        "repository": cla_bot.SOURCE,
        "pull_request": 18,
        "head_sha": "a" * 40,
        "github_user_ids": [HUMAN_ID],
        "maintainer_exemption_id": cla_bot.OWNER_ID,
    }
    assert human_bytes == cla_bot.json_bytes(legacy_record)
    assert "automation_exemptions" not in json.loads(human_bytes)

    bot_bytes, _ = run_successful_check_record(monkeypatch, trusted_bot=True)
    bot_record = json.loads(bot_bytes)
    exemption = cla_bot.trusted_dependabot_exemption(make_pr())
    assert bot_record["github_user_ids"] == [HUMAN_ID]
    assert bot_record["automation_exemptions"] == [exemption]
