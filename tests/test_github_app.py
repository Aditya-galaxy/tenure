"""The GitHub side: webhooks in, evidence, the engine's actions, and the API client."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.testclient import TestClient

from tenure import outcomes
from tenure.app import create_app
from tenure.audit import AuditLog
from tenure.classify import ChangedFile
from tenure.engine import Engine
from tenure.github import GitHubApp, MergeRefused
from tenure.grants import GrantStore
from tenure.halt import HaltSwitch
from tenure.ledger import Ledger, Merge
from tenure.policy import Verdict, Verification
from tenure.webhook import BuildFinished, Deliveries, EvaluatePR, PRMerged, RunCommands, actions, signature_ok

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
REPO = "Aditya-galaxy/hyperion-quant"
DEPENDABOT = {"id": 49699333, "type": "Bot", "login": "dependabot[bot]"}
PERSON = {"id": 150531660, "type": "User", "login": "Aditya-galaxy"}
HEAD, BASE = "a" * 40, "b" * 40
PATCH_BUMP = '@@ -10,1 +10,1 @@\n-serde = "1.0.200"\n+serde = "1.0.210"\n'


# ── a fake GitHub installation that records what Tenure did ──────────────────

class FakeInstallation:
    def __init__(self, *, user=DEPENDABOT, files=None, policy=None, roles=None, refuse_merge=None, head=HEAD):
        self.user, self.policy, self.refuse_merge, self.head = user, policy, refuse_merge, head
        self.files = files if files is not None else [ChangedFile("Cargo.toml", "modified", 1, 1, PATCH_BUMP),
                                                      ChangedFile("Cargo.lock", "modified", 4, 4)]
        self.roles = roles or {"alice": "admin", "bob": "maintain", "carol": "write"}
        self.checks, self.merges, self.comments = [], [], []

    def pull(self, repo, n):
        return {"state": "open", "draft": False, "user": self.user, "title": "Bump serde",
                "head": {"sha": self.head}, "base": {"sha": BASE}}

    def pull_files(self, repo, n):
        return self.files

    def file_text(self, repo, path, ref):
        return self.policy

    def role(self, repo, login):
        return self.roles.get(login, "none")

    def check_run(self, repo, sha, *, conclusion, title, summary):
        self.checks.append({"sha": sha, "conclusion": conclusion, "title": title, "summary": summary})
        return 1

    def merge(self, repo, n, *, sha, title):
        if self.refuse_merge:
            raise MergeRefused(self.refuse_merge)
        self.merges.append({"n": n, "sha": sha})
        return "c" * 40

    def comment(self, repo, n, body):
        self.comments.append({"n": n, "body": body})


class Passes:
    def verify(self, repo, number, head_sha, base_sha, policy):
        return Verification(head_sha, tests_passed=True, tests_before=13, tests_after=13, reversible=True)


@pytest.fixture
def parts(tmp_path):
    audit = AuditLog(tmp_path / "audit.jsonl")
    return {"audit": audit, "store": GrantStore(tmp_path / "grants.json", audit),
            "switch": HaltSwitch(tmp_path / "halts.json", audit), "ledger": Ledger(tmp_path / "merges.jsonl", audit)}


def engine(parts, inst, verifier=None):
    return Engine(lambda _id: inst, store=parts["store"], switch=parts["switch"], ledger=parts["ledger"],
                  audit=parts["audit"], verifier=verifier, clock=lambda: NOW)


def grant(parts, cls="deps_patch", agent="dependabot", owner="alice"):
    from tenure import config
    return parts["store"].grant(repo=REPO, agent=agent, change_class=cls, owner=owner, granted_by="alice",
                                reason="clean since June", days=30, max_days=90,
                                rule_version=config.RepoPolicy().rule_version, now=NOW - timedelta(days=1))


EVAL = EvaluatePR(REPO, 12, 1, "main")


# ── the engine ───────────────────────────────────────────────────────────────

def test_unverified_holds_even_with_a_grant(parts):
    grant(parts)
    inst = FakeInstallation()
    d = engine(parts, inst).evaluate(EVAL)
    assert d.verdict is Verdict.HOLD and inst.merges == []
    assert inst.checks[0]["conclusion"] == "neutral" and "not verified" in inst.checks[0]["summary"]


def test_verified_granted_patch_bump_merges_pinned_to_the_verified_commit(parts):
    grant(parts)
    inst = FakeInstallation()
    d = engine(parts, inst, Passes()).evaluate(EVAL)
    assert d.verdict is Verdict.AUTO_MERGE
    assert inst.merges == [{"n": 12, "sha": HEAD}]
    assert inst.checks[0]["conclusion"] == "success"
    assert parts["ledger"].by_pr(REPO, 12).merge_sha == "c" * 40
    assert parts["store"].get(REPO, "dependabot", "deps_patch").last_fired == NOW
    assert "Audit chain head" in inst.checks[0]["summary"]


def test_a_refused_merge_becomes_a_hold(parts):
    grant(parts)
    inst = FakeInstallation(refuse_merge="Head branch was modified")
    d = engine(parts, inst, Passes()).evaluate(EVAL)
    assert d.verdict is Verdict.HOLD and "Head branch was modified" in d.reasons[0]
    assert parts["ledger"].all() == []


def test_people_get_no_check_run(parts):
    inst = FakeInstallation(user=PERSON)
    assert engine(parts, inst, Passes()).evaluate(EVAL) is None
    assert inst.checks == []


def test_a_workflow_change_is_never(parts):
    grant(parts, cls="code")
    inst = FakeInstallation(files=[ChangedFile(".github/workflows/ci.yml", "modified", 3, 1)])
    d = engine(parts, inst, Passes()).evaluate(EVAL)
    assert d.verdict is Verdict.NEVER and inst.merges == []
    assert inst.checks[0]["title"].startswith("Never merged unattended")


def test_a_broken_policy_file_holds_and_says_so(parts):
    grant(parts)
    inst = FakeInstallation(policy="never-auto: ['src/**']")     # the misspelling must not disable protection
    d = engine(parts, inst, Passes()).evaluate(EVAL)
    assert d.verdict is Verdict.HOLD
    assert any("can't be used" in r for r in d.reasons)


def test_the_policy_file_is_read_from_the_default_branch(parts):
    seen = {}
    inst = FakeInstallation()
    original = inst.file_text
    def spy(repo, path, ref):
        seen["ref"] = ref
        return original(repo, path, ref)
    inst.file_text = spy
    engine(parts, inst).evaluate(EVAL)
    assert seen["ref"] == "main"


def test_a_maintainer_grants_by_comment(parts):
    inst = FakeInstallation()
    replies = engine(parts, inst).commands(
        RunCommands(REPO, 3, 1, "alice", "User", "/tenure grant dependabot deps_patch 30d because clean", "main"))
    assert replies[0].startswith("Granted") and inst.comments[0]["body"].startswith("Granted")
    assert parts["store"].get(REPO, "dependabot", "deps_patch") is not None


def test_a_bot_command_is_refused_audited_and_not_answered(parts):
    inst = FakeInstallation(roles={"dependabot[bot]": "admin"})
    replies = engine(parts, inst).commands(
        RunCommands(REPO, 3, 1, "dependabot[bot]", "Bot", "/tenure grant dependabot code 90d because me", "main"))
    assert replies[0].startswith("Not done") and inst.comments == []
    assert parts["audit"].records()[-1]["kind"] == "refused"
    assert parts["store"].all() == []


def test_a_revert_revokes_the_grant(parts):
    grant(parts)
    inst = FakeInstallation()
    eng = engine(parts, inst, Passes())
    eng.evaluate(EVAL)
    msg = eng.merged(PRMerged(REPO, 40, 1, 'Revert "Bump serde"', f"Reverts {REPO}#12"))
    assert msg.startswith("Revoked dependabot's deps_patch grant: #12 was reverted")
    assert parts["store"].get(REPO, "dependabot", "deps_patch") is None


def test_a_red_main_at_the_merge_commit_suspends_the_grant(parts):
    grant(parts)
    inst = FakeInstallation()
    eng = engine(parts, inst, Passes())
    eng.evaluate(EVAL)
    msg = eng.build(BuildFinished(REPO, 1, "c" * 40, "main", "main", "failure"))
    assert msg.startswith("Suspended")
    assert parts["store"].get(REPO, "dependabot", "deps_patch").suspended
    assert inst.comments[-1]["n"] == 12                     # told on the PR that caused it


# ── evidence ─────────────────────────────────────────────────────────────────

def _ledger_with(parts, pr=12, title="Bump serde"):
    parts["ledger"].record(Merge(REPO, pr, title, HEAD, "c" * 40, "dependabot", "deps_patch",
                                 (REPO, "dependabot", "deps_patch"), NOW.isoformat()))


def test_revert_recognised_from_githubs_wording(parts):
    _ledger_with(parts)
    assert outcomes.reverted(REPO, "anything", f"Reverts {REPO}#12", parts["ledger"]).merge.pr == 12
    assert outcomes.reverted(REPO, 'Revert "Bump serde"', "", parts["ledger"]).merge.pr == 12


def test_reverts_that_are_not_about_tenure_are_ignored(parts):
    _ledger_with(parts)
    assert outcomes.reverted(REPO, "x", "Reverts other/repo#12", parts["ledger"]) is None     # another repo
    assert outcomes.reverted(REPO, "x", f"Reverts {REPO}#99", parts["ledger"]) is None       # not Tenure's merge
    _ledger_with(parts, pr=13, title="Bump serde")                                             # ambiguous title
    assert outcomes.reverted(REPO, 'Revert "Bump serde"', "", parts["ledger"]) is None


@pytest.mark.parametrize("branch, conclusion, sha, hit", [
    ("main", "failure", "c" * 40, True),
    ("main", "success", "c" * 40, False),
    ("feature", "failure", "c" * 40, False),
    ("main", "failure", "d" * 40, False),
])
def test_broke_main_only_at_the_merge_commit_on_the_default_branch(parts, branch, conclusion, sha, hit):
    _ledger_with(parts)
    assert (outcomes.broke_main(REPO, branch, "main", sha, conclusion, parts["ledger"]) is not None) is hit


# ── webhooks ─────────────────────────────────────────────────────────────────

SECRET = b"s3cret"


def signed(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET, body, hashlib.sha256).hexdigest()


def test_signature():
    body = b'{"a":1}'
    assert signature_ok(SECRET, body, signed(body))
    assert not signature_ok(SECRET, body, signed(b'{"a":2}'))
    assert not signature_ok(SECRET, body, None)
    assert not signature_ok(b"", body, signed(body))


def test_deliveries_are_handled_once():
    d = Deliveries(limit=2)
    assert d.first_time("x") and not d.first_time("x")
    d.first_time("y"), d.first_time("z")
    assert d.first_time("x")                                 # bounded: the oldest is forgotten


def _pr_payload(action, **pr):
    return {"action": action, "repository": {"full_name": REPO, "default_branch": "main"},
            "installation": {"id": 7}, "pull_request": {"number": 12, "draft": False, **pr}}


def test_webhook_events_to_actions():
    assert actions("pull_request", _pr_payload("opened")) == [EvaluatePR(REPO, 12, 7, "main")]
    assert actions("pull_request", _pr_payload("synchronize")) == [EvaluatePR(REPO, 12, 7, "main")]
    assert actions("pull_request", _pr_payload("opened", draft=True)) == []
    assert actions("pull_request", _pr_payload("closed", merged=False)) == []
    assert actions("pull_request", _pr_payload("closed", merged=True, title="t", body="b")) == \
        [PRMerged(REPO, 12, 7, "t", "b")]
    comment = {"action": "created", "repository": {"full_name": REPO}, "installation": {"id": 7},
               "issue": {"number": 3}, "comment": {"body": "/tenure halt", "user": PERSON}}
    assert actions("issue_comment", comment)[0].sender == "Aditya-galaxy"
    run = {"action": "completed", "repository": {"full_name": REPO, "default_branch": "main"},
           "installation": {"id": 7}, "workflow_run": {"head_sha": "c" * 40, "head_branch": "main",
                                                        "conclusion": "failure"}}
    assert actions("workflow_run", run) == [BuildFinished(REPO, 7, "c" * 40, "main", "main", "failure")]
    assert actions("pull_request", {"action": "opened"}) == []      # no repo, no installation: nothing


def test_endpoint_rejects_forgeries_and_repeats(parts):
    inst = FakeInstallation()
    app = create_app(engine(parts, inst), SECRET)
    client = TestClient(app)
    body = json.dumps(_pr_payload("opened")).encode()
    headers = {"X-GitHub-Event": "pull_request", "X-GitHub-Delivery": "d-1", "Content-Type": "application/json"}
    forged = client.post("/webhook", content=body, headers={**headers, "X-Hub-Signature-256": "sha256=0"})
    assert forged.status_code == 401 and inst.checks == []
    r = client.post("/webhook", content=body, headers={**headers, "X-Hub-Signature-256": signed(body)})
    assert r.status_code == 202 and len(inst.checks) == 1          # handled
    r = client.post("/webhook", content=body, headers={**headers, "X-Hub-Signature-256": signed(body)})
    assert r.json()["status"] == "duplicate" and len(inst.checks) == 1
    assert client.get("/healthz").json()["ok"] is True


def test_no_secret_no_server(parts):
    with pytest.raises(RuntimeError, match="required"):
        create_app(engine(parts, FakeInstallation()), b"")


# ── the API client, against a fake GitHub ────────────────────────────────────

@pytest.fixture(scope="module")
def keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
    return private, key.public_key()


def fake_github(routes):
    calls = []
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        for (method, path), respond in routes.items():
            if request.method == method and request.url.path == path:
                return respond(request)
        return httpx.Response(404, json={"message": "Not Found"})
    return httpx.Client(transport=httpx.MockTransport(handler)), calls


TOKEN = {("POST", "/app/installations/7/access_tokens"):
         lambda r: httpx.Response(201, json={"token": "ghs_x", "expires_at": "2099-01-01T00:00:00Z"})}


def test_app_jwt_is_rs256_with_githubs_claims(keypair):
    private, public = keypair
    token = GitHubApp("12345", private).app_jwt(now=1_000_000)
    claims = jwt.decode(token, public, algorithms=["RS256"], options={"verify_exp": False, "verify_iat": False})
    assert claims == {"iat": 999_940, "exp": 1_000_540, "iss": "12345"}


def test_installation_token_is_cached(keypair):
    http, calls = fake_github(TOKEN)
    app = GitHubApp("1", keypair[0], http=http)
    assert app.installation_token(7) == app.installation_token(7) == "ghs_x"
    assert sum(c.url.path.endswith("access_tokens") for c in calls) == 1


def test_merge_is_pinned_and_a_refusal_raises(keypair):
    sent = {}
    def ok(r):
        sent.update(json.loads(r.content))
        return httpx.Response(200, json={"merged": True, "sha": "m" * 40})
    http, _ = fake_github({**TOKEN, ("PUT", f"/repos/{REPO}/pulls/12/merge"): ok})
    inst = GitHubApp("1", keypair[0], http=http).installation(7)
    assert inst.merge(REPO, 12, sha=HEAD, title="t") == "m" * 40
    assert sent["sha"] == HEAD
    http, _ = fake_github({**TOKEN, ("PUT", f"/repos/{REPO}/pulls/12/merge"):
                           lambda r: httpx.Response(409, json={"message": "Head branch was modified"})})
    with pytest.raises(MergeRefused, match="Head branch was modified"):
        GitHubApp("1", keypair[0], http=http).installation(7).merge(REPO, 12, sha=HEAD, title="t")


def test_role_reads_role_name_and_defaults_to_none(keypair):
    http, _ = fake_github({**TOKEN,
                           ("GET", f"/repos/{REPO}/collaborators/bob/permission"):
                           lambda r: httpx.Response(200, json={"permission": "write", "role_name": "maintain"}),
                           ("GET", f"/repos/{REPO}/collaborators/eve/permission"):
                           lambda r: httpx.Response(200, json={"role_name": "custom-thing"})})
    inst = GitHubApp("1", keypair[0], http=http).installation(7)
    assert inst.role(REPO, "bob") == "maintain"        # "permission" alone would have said write
    assert inst.role(REPO, "eve") == "none"
    assert inst.role(REPO, "stranger") == "none"       # 404


def test_policy_file_missing_is_none_and_present_is_decoded(keypair):
    content = base64.b64encode(b"never_auto: ['src/oms/**']\n").decode()
    http, _ = fake_github({**TOKEN, ("GET", f"/repos/{REPO}/contents/.tenure.yml"):
                           lambda r: httpx.Response(200, json={"encoding": "base64", "content": content})})
    inst = GitHubApp("1", keypair[0], http=http).installation(7)
    assert inst.file_text(REPO, ".tenure.yml", "main") == "never_auto: ['src/oms/**']\n"
    http, _ = fake_github(TOKEN)
    assert GitHubApp("1", keypair[0], http=http).installation(7).file_text(REPO, ".tenure.yml", "main") is None


def test_pull_files_paginates(keypair):
    def page(r):
        n = int(r.url.params["page"])
        count = 100 if n == 1 else 3
        return httpx.Response(200, json=[{"filename": f"f{n}_{i}.md", "status": "modified"} for i in range(count)])
    http, _ = fake_github({**TOKEN, ("GET", f"/repos/{REPO}/pulls/12/files"): page})
    files = GitHubApp("1", keypair[0], http=http).installation(7).pull_files(REPO, 12)
    assert len(files) == 103 and files[0].path == "f1_0.md"
