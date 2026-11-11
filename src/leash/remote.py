"""Leash on one remote machine: dashboard and MCP endpoint behind one port.

Hosts such as Render give a service one public port and one disk. The
reconcile thread, the dashboard and the MCP server must share leash.db, so
they run in one process:

    PORT ──► this ASGI front
               ├── /mcp...   → MCP streamable HTTP (agent bearer token)
               ├── /healthz  → {"ok": true}
               └── anything else → the dashboard on an internal loopback port

The agent's token (LEASH_MCP_TOKEN) and the human's token
(LEASH_DASHBOARD_TOKEN) must differ. The MCP side has no approve tool, and
the agent token is refused by the dashboard, so an agent can never approve.

Run with:  python -m leash.remote
"""

from __future__ import annotations

import logging
import os
import secrets
import threading
from typing import Any, Awaitable, Callable

import httpx

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

MCP_PATH = "/mcp"
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-length",
}


def mcp_token_from_env(dashboard_token: str | None = None) -> str:
    token = os.getenv("LEASH_MCP_TOKEN", "").strip()
    if len(token) < 16:
        raise SystemExit("LEASH_MCP_TOKEN must be set to at least 16 characters")
    if dashboard_token and secrets.compare_digest(token, dashboard_token):
        raise SystemExit("LEASH_MCP_TOKEN must differ from LEASH_DASHBOARD_TOKEN")
    return token


async def _plain(send: Send, status: int, body: bytes, extra: list | None = None) -> None:
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [(b"content-type", b"application/json"), *(extra or [])],
    })
    await send({"type": "http.response.body", "body": body})


def require_bearer(app: ASGIApp, token: str) -> ASGIApp:
    """Refuse any HTTP request without ``Authorization: Bearer <token>``."""
    if not token:
        raise ValueError("a bearer token is required")
    expected = f"Bearer {token}".encode()

    async def guarded(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await app(scope, receive, send)
            return
        sent = dict(scope.get("headers") or []).get(b"authorization", b"")
        if not secrets.compare_digest(sent, expected):
            await _plain(
                send, 401, b'{"error":"token_required"}',
                [(b"www-authenticate", b'Bearer realm="leash"')],
            )
            return
        await app(scope, receive, send)

    return guarded


def build_mcp_app(token: str, public_hosts: list[str] | None = None) -> ASGIApp:
    """The MCP streamable-HTTP app, behind the agent's bearer token."""
    from mcp.server.transport_security import TransportSecuritySettings

    from .mcp_server import server

    hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*", *(public_hosts or [])]
    origins = ["http://127.0.0.1:*", "http://localhost:*"]
    origins += [f"https://{host}" for host in public_hosts or []]
    app = server.streamable_http_app(
        streamable_http_path=MCP_PATH,
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=hosts,
            allowed_origins=origins,
        ),
    )
    return require_bearer(app, token)


def proxy_to(base_url: str, client: httpx.AsyncClient | None = None) -> ASGIApp:
    """Forward a request to the dashboard unchanged (Host and Origin included).

    The dashboard does its own Host, Origin and token checks, so forwarding
    the original headers keeps those checks meaningful.
    """
    http = client or httpx.AsyncClient(base_url=base_url, timeout=30)

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        body = b""
        more = True
        while more:
            message = await receive()
            body += message.get("body", b"")
            more = message.get("more_body", False)
            if len(body) > 64_000:
                await _plain(send, 413, b'{"error":"too_large"}')
                return
        path = scope.get("raw_path") or scope["path"].encode()
        if scope.get("query_string"):
            path += b"?" + scope["query_string"]
        headers = [
            (name.decode("latin-1"), value.decode("latin-1"))
            for name, value in scope.get("headers") or []
            if name.decode("latin-1").lower() not in HOP_BY_HOP
        ]
        try:
            response = await http.request(
                scope["method"], path.decode("latin-1"), headers=headers, content=body
            )
        except httpx.HTTPError:
            await _plain(send, 502, b'{"error":"dashboard_unavailable"}')
            return
        await send({
            "type": "http.response.start",
            "status": response.status_code,
            "headers": [
                (name.encode("latin-1"), value.encode("latin-1"))
                for name, value in response.headers.items()
                if name.lower() not in HOP_BY_HOP
            ] + [(b"content-length", str(len(response.content)).encode())],
        })
        await send({"type": "http.response.body", "body": response.content})

    return app


def front(mcp_app: ASGIApp, dashboard_app: ASGIApp) -> ASGIApp:
    """Route /mcp to the agent side and everything else to the dashboard."""

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await mcp_app(scope, receive, send)  # MCP needs its session manager
            return
        path = scope.get("path", "")
        if path == "/healthz":
            await _plain(send, 200, b'{"ok":true}')
        elif path == MCP_PATH or path.startswith(MCP_PATH + "/"):
            await mcp_app(scope, receive, send)
        else:
            await dashboard_app(scope, receive, send)

    return app


def main() -> int:
    import uvicorn

    from . import config
    from .dashboard import _reconcile_forever, build_server, make_gate, public_hosts_from_env

    config.configure()
    logging.getLogger("httpx").setLevel(logging.WARNING)  # no line per proxied call
    public = public_hosts_from_env()
    if not public:
        raise SystemExit("LEASH_PUBLIC_HOST must name the public host (e.g. leash.onrender.com)")
    if not os.getenv("LEASH_DASHBOARD_TOKEN"):
        raise SystemExit("LEASH_DASHBOARD_TOKEN must be set on a remote host")

    gate = make_gate()
    stop = threading.Event()
    threading.Thread(target=_reconcile_forever, args=(gate, stop), daemon=True).start()
    # The dashboard listens on loopback only; the front is its sole way in.
    httpd, dashboard_token, _ = build_server(gate, host="127.0.0.1", port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    internal = f"http://127.0.0.1:{httpd.server_address[1]}"

    mcp_app = build_mcp_app(mcp_token_from_env(dashboard_token), public)
    app = front(mcp_app, proxy_to(internal))
    port = int(os.getenv("PORT", "10000"))
    print(f"Leash remote: https://{public[0]}/ (dashboard), https://{public[0]}{MCP_PATH} (agents)", flush=True)
    try:
        uvicorn.run(app, host="0.0.0.0", port=port, proxy_headers=True, log_level="warning")
    finally:
        stop.set()
        httpd.shutdown()
        httpd.server_close()
        gate.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
