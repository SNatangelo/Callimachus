"""Offline regression tests. These tests never access GitHub or collect real signatures."""
import base64
import copy
import importlib.util
import json
from pathlib import Path
import unittest

HERE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("cla_bot", HERE / "cla_bot.py")
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)
DOC = (HERE / "agreements/CLA-v1.1.md").read_bytes()


def encode(raw):
    return {"encoding": "base64", "content": base64.b64encode(raw).decode()}


class FakePublic:
    token = "fake-public-token"
    def __init__(self, authors=None):
        self.people = authors or [{"databaseId": 17, "login": "contributor"}]
        self.comments = []
        self.statuses = []
        self.doc = DOC
        self.pages_called = 0
        self.pr = {"state": "open", "head": {"sha": "a"*40},
                   "user": {"id": 17, "login": "contributor"}, "html_url": "https://github.com/SNatangelo/Callimachus/pull/7"}
    def pages(self, path):
        self.pages_called += 1
        return iter(copy.deepcopy(self.comments))
    def request(self, method, path, data=None):
        if path.endswith("/pulls/7"):
            return copy.deepcopy(self.pr)
        if "/statuses/" in path:
            self.statuses.append(copy.deepcopy(data)); return {}
        if "/contents/" in path:
            return encode(self.doc)
        if path == "/graphql":
            return {"data": {"repository": {"pullRequest": {"commits": {
                "nodes": [{"commit": {"authors": {"nodes": [{"user": p} for p in self.people],
                            "pageInfo": {"hasNextPage": False}}}}],
                "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}}
        if method == "POST" and path.endswith("/comments"):
            self.comments.append({"id": 100, "body": data["body"], "created_at": "2026-09-23T07:00:00Z",
                                  "user": {"type": "Bot", "login": "github-actions[bot]"}})
            return {}
        if method == "PATCH" and "/issues/comments/" in path:
            self.comments[0]["body"] = data["body"]; return {}
        raise AssertionError((method, path))


class FakePrivate:
    token = "fake-private-token"
    def __init__(self):
        self.private = True
        self.files = {}
        self.fail_put = False
    def request(self, method, path, data=None):
        if path == f"/repos/{bot.ARCHIVE}":
            return {"private": self.private, "full_name": bot.ARCHIVE}
        if path.endswith("/branches/main"):
            return {}
        name = path.split("/contents/", 1)[1].split("?", 1)[0]
        if method == "GET":
            if name not in self.files:
                raise bot.ApiError(404)
            return encode(self.files[name])
        if method == "PUT":
            if self.fail_put:
                raise bot.ApiError(403)
            if name in self.files:
                raise AssertionError("unexpected overwrite")
            self.files[name] = base64.b64decode(data["content"])
            return {}
        raise AssertionError((method, path))


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.public, self.private = FakePublic(), FakePrivate()
        self.event = {"pull_request": {"number": 7}, "repository": {"id": 1358076021}}
        self.env = {"GITHUB_REPOSITORY": bot.SOURCE, "GITHUB_RUN_ID": "11", "CLA_WORKFLOW_SHA": "b"*40}
    def run_flow(self):
        bot.execute(self.public, self.private, self.event, self.env)
    def add_signature(self, uid=17, body=None):
        self.public.comments.append({"id": 101+uid, "body": body or bot.SIGN,
            "created_at": "2026-09-23T07:01:00Z", "updated_at": "2026-09-23T07:01:00Z",
            "html_url": "https://github.com/SNatangelo/Callimachus/pull/7#issuecomment-118",
            "user": {"id": uid, "login": f"user{uid}", "type": "User"}})
    def test_unsigned_fails_and_archives_exact_document(self):
        self.run_flow()
        self.assertEqual(self.public.statuses[-1]["state"], "failure")
        self.assertEqual(self.private.files["agreements/v1.1/CLA.md"], DOC)
    def test_signature_succeeds_and_is_version_bound(self):
        self.run_flow(); self.add_signature(); self.run_flow()
        self.assertEqual(self.public.statuses[-1]["state"], "success")
        record = json.loads(self.private.files["signatures/v1.1/github-17.json"])
        self.assertTrue(bot.valid_signature(record, 17))
        self.assertNotIn("email", record)
        self.assertIn(f"checks/v1.1/pr-7/{'a'*40}.json", self.private.files)
    def test_success_repeated_without_overwrite(self):
        self.run_flow(); self.add_signature(); self.run_flow()
        old = copy.deepcopy(self.private.files); self.run_flow()
        self.assertEqual(old, self.private.files)
    def test_public_archive_fails_before_writes(self):
        self.private.private = False
        with self.assertRaises(bot.Failure): self.run_flow()
        self.assertFalse(self.private.files)
        self.assertEqual(self.public.statuses[-1]["state"], "failure")
    def test_missing_token_fails(self):
        self.private.token = ""
        with self.assertRaises(bot.Failure): self.run_flow()
        self.assertEqual(self.public.statuses[-1]["state"], "failure")
    def test_changed_agreement_fails(self):
        self.public.doc = DOC + b"changed"
        with self.assertRaises(bot.Failure): self.run_flow()
        self.assertFalse(self.private.files)
    def test_archive_write_failure_fails(self):
        self.private.fail_put = True
        with self.assertRaises(bot.ApiError): self.run_flow()
        self.assertEqual(self.public.statuses[-1]["state"], "failure")
    def test_unknown_coauthor_fails(self):
        self.public.people.append(None)
        with self.assertRaises(bot.Failure): self.run_flow()
        self.assertEqual(self.public.statuses[-1]["state"], "failure")
    def test_all_coauthors_required(self):
        self.public.people.append({"databaseId": 18, "login": "coauthor"})
        self.run_flow(); self.add_signature(); self.run_flow()
        self.assertEqual(self.public.statuses[-1]["state"], "failure")
        self.add_signature(18); self.run_flow()
        self.assertEqual(self.public.statuses[-1]["state"], "success")
    def test_wrong_version_does_not_sign(self):
        self.run_flow(); self.add_signature(body=bot.SIGN.replace("v1.1", "v1.0")); self.run_flow()
        self.assertEqual(self.public.statuses[-1]["state"], "failure")
    def test_unrelated_comment_author_cannot_sign(self):
        self.run_flow(); self.add_signature(99); self.run_flow()
        self.assertNotIn("signatures/v1.1/github-99.json", self.private.files)
        self.assertEqual(self.public.statuses[-1]["state"], "failure")
    def test_fake_bot_prompt_is_ignored(self):
        self.public.comments.append({"id": 100, "body": bot.MARKER,
            "created_at": "2026-09-23T06:00:00Z", "user": {"type": "User", "login": "attacker"}})
        self.add_signature(); self.run_flow()
        self.assertEqual(self.public.statuses[-1]["state"], "failure")
    def test_deletion_of_comment_does_not_delete_evidence(self):
        self.run_flow(); self.add_signature(); self.run_flow()
        self.public.comments = self.public.comments[:1]; self.run_flow()
        self.assertEqual(self.public.statuses[-1]["state"], "success")
    def test_owner_exemption_does_not_exempt_coauthor(self):
        self.public.pr["user"] = {"id": bot.OWNER_ID, "login": "SNatangelo"}
        self.public.people.append({"databaseId": bot.OWNER_ID, "login": "SNatangelo"})
        self.run_flow()
        self.assertEqual(self.public.statuses[-1]["state"], "failure")
    def test_corrupt_existing_signature_fails(self):
        self.run_flow(); self.add_signature()
        self.private.files["signatures/v1.1/github-17.json"] = b'{}'
        with self.assertRaises(bot.Failure): self.run_flow()
        self.assertEqual(self.public.statuses[-1]["state"], "failure")
    def test_different_repository_is_rejected(self):
        self.env["GITHUB_REPOSITORY"] = "attacker/fork"
        with self.assertRaises(bot.Failure): self.run_flow()
        self.assertFalse(self.private.files)
    def test_put_once_rejects_divergent_content(self):
        self.private.files["test.txt"] = b"original"
        with self.assertRaises(bot.Failure): bot.put_once(self.private, "test.txt", b"changed")
        self.assertEqual(self.private.files["test.txt"], b"original")
    def test_issue_comment_runs_on_pr_head(self):
        self.event = {"issue": {"number": 7, "pull_request": {}}, "repository": {"id": 1358076021}}
        # GitHub sends a nonempty pull_request object on PR issue events.
        self.event["issue"]["pull_request"] = {"url": "https://api.github.com/repos/SNatangelo/Callimachus/pulls/7"}
        self.run_flow(); self.add_signature(); self.run_flow()
        self.assertEqual(self.public.statuses[-1]["context"], "cla/signatures")


if __name__ == "__main__":
    unittest.main()
