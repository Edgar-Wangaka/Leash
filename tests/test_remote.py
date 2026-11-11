"""Leash on a remote host: public host, env tokens, health check, remote MCP."""

from __future__ import annotations

import json
import threading
from http.server import ThreadingHTTPServer

import httpx
import pytest
from starlette.testclient import TestClient

from leash import dashboard, mcp_server, remote
from leash.dashboard import TOKEN_HEADER, make_handler
from leash.gate import PaymentGate
from leash.policy import PaymentRequest


TOKEN = "dashboard-token-0123456789"
AGENT_TOKEN = "agent-token-abcdefghijklmn"
PUBLIC = "leash.example.com"
SELLER = "sb-fwgx550980251@personal.example.com"


class FakePayout:
    def __init__(self):
        self.calls = []

    def send_payout(self, *args):
        self.calls.append(args)
        return {"status": "success", "paypal_batch_id": "batch-1"}

    def close(self):
        pass


def make_gate(tmp_path, payout):
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({
        "per_transaction_cap_usd": 25,
        "daily_budget_usd": 60,
        "merchant_allowlist": [SELLER],
        "approval_threshold_usd": 10,
        "max_requests_per_minute": 100,
        "approved_task_ids": ["demo-legitimate-task"],
        "human_approval_expiry_hours": 24,
    }))
    gate = PaymentGate(
        db_path=str(tmp_path / "leash.db"),
        policy_path=policy,
        payout_client=payout,
        risk_signal=lambda request, task: ("none", "none"),
    )
    gate.process(PaymentRequest("held-1", "agent-1", SELLER, "15", "gift", "demo-legitimate-task"))
    return gate


@pytest.fixture
def public_server(tmp_path):
    payout = FakePayout()
    gate = make_gate(tmp_path, payout)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(gate, TOKEN, [PUBLIC]))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", payout, gate
    httpd.shutdown()
    httpd.server_close()


def approve(base, headers):
    return httpx.post(
        f"{base}/api/approve",
        content=json.dumps({"request_id": "held-1"}),
        headers={"Content-Type": "application/json", **headers},
    )


# --- dashboard on a public host ------------------------------------------------

def test_public_host_with_https_origin_and_token_can_approve(public_server):
    base, payout, _ = public_server
    response = approve(base, {
        TOKEN_HEADER: TOKEN, "Host": PUBLIC, "Origin": f"https://{PUBLIC}",
    })

    assert response.status_code == 200
    assert response.json()["status"] == "paid"
    assert len(payout.calls) == 1


def test_public_host_without_token_is_refused(public_server):
    base, payout, _ = public_server
    response = approve(base, {"Host": PUBLIC, "Origin": f"https://{PUBLIC}"})

    assert response.status_code == 401
    assert payout.calls == []


def test_plain_http_origin_for_the_public_host_is_refused(public_server):
    base, payout, _ = public_server
    response = approve(base, {TOKEN_HEADER: TOKEN, "Host": PUBLIC, "Origin": f"http://{PUBLIC}"})

    assert response.status_code == 403
    assert payout.calls == []


def test_other_hosts_are_still_refused(public_server):
    base, payout, _ = public_server
    for host in ("evil.example", f"{PUBLIC}.evil.example", f"evil.{PUBLIC}"):
        response = approve(base, {TOKEN_HEADER: TOKEN, "Host": host, "Origin": f"https://{host}"})
        assert response.status_code == 403
    assert payout.calls == []


def test_healthz_answers_any_host_and_reveals_nothing(public_server):
    base, _, _ = public_server
    response = httpx.get(f"{base}/healthz", headers={"Host": "10.0.0.7:10000"})

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_token_comes_from_env_when_set(monkeypatch):
    monkeypatch.setenv("LEASH_DASHBOARD_TOKEN", TOKEN)
    assert dashboard.dashboard_token() == (TOKEN, True)


def test_short_env_token_is_refused(monkeypatch):
    monkeypatch.setenv("LEASH_DASHBOARD_TOKEN", "short")
    with pytest.raises(SystemExit):
        dashboard.dashboard_token()


def test_random_token_when_env_is_unset(monkeypatch):
    monkeypatch.delenv("LEASH_DASHBOARD_TOKEN", raising=False)
    token, from_env = dashboard.dashboard_token()
    assert len(token) >= 32 and from_env is False


def test_listening_on_all_interfaces_needs_a_public_host(tmp_path, monkeypatch):
    monkeypatch.delenv("LEASH_PUBLIC_HOST", raising=False)
    gate = make_gate(tmp_path, FakePayout())
    with pytest.raises(SystemExit):
        dashboard.build_server(gate, host="0.0.0.0", port=0, token=TOKEN)


def test_build_server_reads_port_and_public_host(tmp_path, monkeypatch):
    monkeypatch.setenv("LEASH_PUBLIC_HOST", f"{PUBLIC}, other.example.com")
    gate = make_gate(tmp_path, FakePayout())
    httpd, token, _ = dashboard.build_server(gate, host="127.0.0.1", port=0, token=TOKEN)
    try:
        assert token == TOKEN
        assert dashboard.public_hosts_from_env() == [PUBLIC, "other.example.com"]
    finally:
        httpd.server_close()


# --- MCP transport switch ------------------------------------------------------

def test_transport_defaults_to_stdio(monkeypatch):
    monkeypatch.delenv("LEASH_MCP_TRANSPORT", raising=False)
    calls = []
    monkeypatch.setattr(mcp_server.server, "run", lambda transport: calls.append(transport))
    monkeypatch.setattr(mcp_server.config, "configure", lambda: None)
    mcp_server.main()
    assert calls == ["stdio"]


def test_streamable_http_transport_runs_uvicorn_behind_the_token(monkeypatch):
    import uvicorn

    monkeypatch.setenv("LEASH_MCP_TRANSPORT", "streamable-http")
    monkeypatch.setenv("LEASH_MCP_TOKEN", AGENT_TOKEN)
    monkeypatch.setenv("LEASH_DASHBOARD_TOKEN", TOKEN)
    monkeypatch.setattr(mcp_server.config, "configure", lambda: None)
    served = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: served.update(app=app, **kw))
    mcp_server.main()

    assert served["host"] == "127.0.0.1"
    client = TestClient(served["app"])
    assert client.post("/mcp", json={}).status_code == 401


def test_unknown_transport_is_refused(monkeypatch):
    monkeypatch.setenv("LEASH_MCP_TRANSPORT", "carrier-pigeon")
    with pytest.raises(SystemExit):
        mcp_server.transport_from_env()


def test_agent_token_must_differ_from_dashboard_token(monkeypatch):
    monkeypatch.setenv("LEASH_MCP_TOKEN", TOKEN)
    with pytest.raises(SystemExit):
        remote.mcp_token_from_env(TOKEN)


def test_agent_token_is_required(monkeypatch):
    monkeypatch.delenv("LEASH_MCP_TOKEN", raising=False)
    with pytest.raises(SystemExit):
        remote.mcp_token_from_env(TOKEN)


# --- remote MCP over HTTP ------------------------------------------------------

INITIALIZE = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


@pytest.fixture
def mcp_client():
    app = remote.build_mcp_app(AGENT_TOKEN, ["testserver"])
    with TestClient(app) as client:
        yield client


def test_mcp_without_bearer_is_refused(mcp_client):
    response = mcp_client.post("/mcp", json=INITIALIZE, headers=MCP_HEADERS)
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Bearer")


def test_mcp_with_the_dashboard_token_is_refused(mcp_client):
    response = mcp_client.post(
        "/mcp", json=INITIALIZE, headers={**MCP_HEADERS, "Authorization": f"Bearer {TOKEN}"}
    )
    assert response.status_code == 401


def test_mcp_with_the_agent_token_lists_only_the_three_tools(mcp_client):
    auth = {**MCP_HEADERS, "Authorization": f"Bearer {AGENT_TOKEN}"}
    response = mcp_client.post("/mcp", json=INITIALIZE, headers=auth)
    assert response.status_code == 200, response.text

    listed = mcp_client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        headers={**auth, "mcp-protocol-version": "2025-06-18"},
    )
    assert listed.status_code == 200, listed.text
    names = {tool["name"] for tool in listed.json()["result"]["tools"]}
    assert names == {"search_products", "request_payment", "get_payment_status"}


# --- the combined front --------------------------------------------------------

@pytest.fixture
def front_client(public_server):
    base, payout, _ = public_server
    app = remote.front(
        remote.build_mcp_app(AGENT_TOKEN, [PUBLIC]), remote.proxy_to(base)
    )
    with TestClient(app, base_url=f"https://{PUBLIC}") as client:
        yield client, payout


def test_front_health_check(front_client):
    client, _ = front_client
    assert client.get("/healthz").json() == {"ok": True}


def test_front_serves_the_dashboard_page(front_client):
    client, _ = front_client
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_front_forwards_approve_with_the_human_token(front_client):
    client, payout = front_client
    response = client.post(
        "/api/approve", json={"request_id": "held-1"},
        headers={TOKEN_HEADER: TOKEN, "Origin": f"https://{PUBLIC}"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "paid"
    assert len(payout.calls) == 1


def test_agent_token_cannot_approve_through_the_front(front_client):
    client, payout = front_client
    for headers in (
        {TOKEN_HEADER: AGENT_TOKEN},
        {"Authorization": f"Bearer {AGENT_TOKEN}"},
    ):
        response = client.post(
            "/api/approve", json={"request_id": "held-1"},
            headers={**headers, "Origin": f"https://{PUBLIC}"},
        )
        assert response.status_code == 401
    assert payout.calls == []


def test_front_routes_mcp_to_the_bearer_guard(front_client):
    client, _ = front_client
    assert client.post("/mcp", json=INITIALIZE, headers=MCP_HEADERS).status_code == 401


# --- config is found when installed into site-packages -------------------------

def test_project_root_falls_back_to_cwd_when_installed(tmp_path, monkeypatch):
    from leash import config

    installed = tmp_path / "venv/lib/python3.11/site-packages/leash/config.py"
    installed.parent.mkdir(parents=True)
    installed.write_text("")
    (tmp_path / "repo/config").mkdir(parents=True)
    (tmp_path / "repo/config/policy.json").write_text("{}")
    monkeypatch.delenv("LEASH_PROJECT_ROOT", raising=False)
    monkeypatch.setattr(config, "__file__", str(installed))
    monkeypatch.chdir(tmp_path / "repo")

    assert config._find_project_root() == (tmp_path / "repo").resolve()


def test_project_root_can_be_set_explicitly(tmp_path, monkeypatch):
    from leash import config

    monkeypatch.setenv("LEASH_PROJECT_ROOT", str(tmp_path))
    assert config._find_project_root() == tmp_path.resolve()
