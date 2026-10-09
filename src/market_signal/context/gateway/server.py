"""One public information write capability. No database, CLI, trading or shell imports."""

from __future__ import annotations

import asyncio
import os
import time
from collections import deque
from contextlib import asynccontextmanager, suppress
from typing import Any

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.requests import Request
from starlette.responses import JSONResponse

from market_signal.context.entities import load_entities
from market_signal.context.gateway.auth import SCOPE, Verifier
from market_signal.context.gateway.contract import action_schema, validate
from market_signal.context.gateway.spool import GatewayError, Spool, now
from market_signal.research.lab.common import canonical_json, strict_json

MAX_BODY = 32 * 1024


def publish_live(spool, name, data):
    path = spool.root / name
    tmp = path.with_suffix(".tmp")
    tmp.write_text(canonical_json(data))
    os.replace(tmp, path)  # liveness only; immutable receipts use durable_create


def submit(spool, raw, identity, received):
    try:
        em = load_entities()
        sub, observation = validate(raw, received, em)
        return spool.accept(raw, sub, observation, identity, received, em.version)
    except GatewayError:
        raise
    except ValueError:
        raise GatewayError(422, "schema_rejected") from None
    except OSError:
        spool.last_failure_at = now().isoformat()
        raise GatewayError(503, "spool_failure") from None


class Boundary:
    """TLS, auth-before-body, bounded JSON, safe audit, global abuse limit."""

    def __init__(self, app, spool, verifier, *, dev=False):
        self.app, self.spool, self.verifier, self.dev = app, spool, verifier, dev
        self.requests = deque()

    async def reject(self, scope, receive, send, code, status):
        try:
            with self.spool.lock():
                self.spool.audit(
                    code
                    if code in ("auth_rejected", "schema_rejected", "spool_failure")
                    else "rejected"
                )
        except OSError:
            self.spool.last_failure_at = now().isoformat()
        await JSONResponse({"status": "REJECTED", "error": code}, status_code=status)(
            scope, receive, send
        )

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        received = now()
        path, method = scope["path"], scope["method"]
        t = time.monotonic()
        while self.requests and self.requests[0] < t - 60:
            self.requests.popleft()
        if len(self.requests) >= 120:
            await JSONResponse({"error": "rate_limited"}, status_code=429)(scope, receive, send)
            return
        self.requests.append(t)
        if not self.dev and scope["scheme"] != "https":
            return await self.reject(scope, receive, send, "tls_required", 400)
        allowed = (
            (path == "/healthz" and method == "GET")
            or (path == "/context/v1/events" and method == "POST")
            or (path == "/mcp" and method in ("GET", "POST", "DELETE"))
            or (
                path
                in (
                    "/.well-known/oauth-protected-resource",
                    "/.well-known/oauth-protected-resource/mcp",
                )
                and method == "GET"
            )
        )
        if not allowed:
            await JSONResponse({"error": "not_found"}, status_code=404)(scope, receive, send)
            return
        if path in ("/mcp", "/context/v1/events"):
            headers = dict(scope["headers"])
            token = headers.get(b"authorization", b"").decode("latin-1")
            auth = (
                await self.verifier.verify_token(token[7:]) if token.startswith("Bearer ") else None
            )
            # Service credentials are not accepted as OAuth credentials for Work MCP.
            if not auth or (path == "/mcp" and (auth.claims or {}).get("service")):
                if path == "/mcp":
                    try:
                        with self.spool.lock():
                            self.spool.audit("auth_rejected")
                    except OSError:
                        self.spool.last_failure_at = now().isoformat()
                    await JSONResponse(
                        {"error": "auth_rejected"},
                        status_code=401,
                        headers={
                            "WWW-Authenticate": f'Bearer resource_metadata="{(self.verifier.audience or "https://localhost/mcp").rsplit("/mcp", 1)[0]}/.well-known/oauth-protected-resource/mcp"'
                        },
                    )(scope, receive, send)
                    return
                return await self.reject(scope, receive, send, "auth_rejected", 401)
            scope["gateway_identity"] = auth.claims["integration_id"]
            scope["gateway_received_at"] = received
        if method == "POST":
            body = bytearray()
            try:
                async with asyncio.timeout(5):
                    while True:
                        msg = await receive()
                        if msg["type"] != "http.request":
                            return
                        body.extend(msg.get("body", b""))
                        if len(body) > MAX_BODY:
                            return await self.reject(scope, receive, send, "payload_too_large", 413)
                        if not msg.get("more_body"):
                            break
                raw = strict_json(body.decode("utf-8"))
                if not isinstance(raw, dict):
                    raise ValueError("object required")
            except (ValueError, UnicodeError, TimeoutError, RecursionError):
                return await self.reject(scope, receive, send, "schema_rejected", 400)

            if path == "/mcp" and raw.get("method") == "tools/call":
                params = raw.get("params", {})
                args = params.get("arguments", {}) if isinstance(params, dict) else None
                if (
                    not isinstance(args, dict)
                    or set(args) != {"payload"}
                    or not isinstance(args.get("payload"), dict)
                ):
                    return await self.reject(scope, receive, send, "schema_rejected", 422)

            async def replay():
                return {"type": "http.request", "body": bytes(body), "more_body": False}

            receive = replay
        await self.app(scope, receive, send)


def create_app(spool: Spool, verifier: Verifier, *, dev=False):
    if not verifier.tokens and not verifier.jwks:
        raise ValueError("configure OAuth or service credentials before starting gateway")
    resource = verifier.audience
    auth = (
        AuthSettings(
            issuer_url=verifier.issuer,
            resource_server_url=resource,
            required_scopes=[SCOPE],
            validate_token_resource=True,
        )
        if verifier.jwks
        else None
    )

    @asynccontextmanager
    async def lifespan(server):
        async def beat():
            while True:
                publish_live(
                    spool,
                    "server.json",
                    {
                        "heartbeat_at": now().isoformat(),
                        "auth_state": "OAUTH_CONFIGURED" if verifier.jwks else "SERVICE_ONLY",
                        "last_spool_failure_at": getattr(spool, "last_failure_at", None),
                    },
                )
                await asyncio.sleep(5)

        task = asyncio.create_task(beat())
        try:
            yield {}
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    from urllib.parse import urlsplit

    host = urlsplit(resource).netloc if resource else "localhost"
    mcp = FastMCP(
        "Prism Context Gateway",
        token_verifier=verifier if auth else None,
        auth=auth,
        stateless_http=True,
        json_response=True,
        max_request_body_size=MAX_BODY,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[host, "localhost:*", "127.0.0.1:*", "testserver"],
            allowed_origins=[f"https://{host}"],
        ),
    )

    @mcp.tool(
        name="submit_market_event",
        description="Use when you have discovered a sourced factual market event. Submit one context_import_v1 candidate. No trading instructions or interpretation. Preserve submission_id and sent_at for identical retries.",
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
        ),
        meta={"securitySchemes": [{"type": "oauth2", "scopes": [SCOPE]}]},
    )
    async def submit_market_event(payload: dict, ctx: Context) -> dict[str, Any]:
        access = get_access_token()
        if not access:
            return {"status": "REJECTED", "error": "auth_rejected"}
        try:
            return submit(
                spool,
                payload,
                access.claims["integration_id"],
                ctx.request_context.request.scope["gateway_received_at"],
            )
        except GatewayError as exc:
            with suppress(OSError), spool.lock():
                spool.audit(
                    exc.code if exc.code in ("schema_rejected", "spool_failure") else "rejected"
                )
            return {"status": "REJECTED", "error": exc.code}

    # Advertise the exact bounded contract, while validating raw dicts ourselves so rejection
    # counts and strict unknown-field/trade-field handling apply to the complete payload.
    mcp._tool_manager.get_tool("submit_market_event").parameters = action_schema()

    @mcp.custom_route("/context/v1/events", methods=["POST"])
    async def events(request: Request):
        try:
            ack = submit(
                spool,
                strict_json((await request.body()).decode()),
                request.scope["gateway_identity"],
                request.scope["gateway_received_at"],
            )
            return JSONResponse(ack, status_code=200 if ack["status"] == "DUPLICATE" else 202)
        except GatewayError as exc:
            with suppress(OSError), spool.lock():
                spool.audit(
                    exc.code if exc.code in ("schema_rejected", "spool_failure") else "rejected"
                )
            return JSONResponse({"status": "REJECTED", "error": exc.code}, status_code=exc.status)

    @mcp.custom_route("/healthz", methods=["GET"])
    async def health(request: Request):
        return JSONResponse({"running": True})

    base_app = mcp.streamable_http_app()
    sdk_lifespan = base_app.router.lifespan_context

    @asynccontextmanager
    async def app_lifespan(app):
        async with sdk_lifespan(app), lifespan(app):
            yield

    base_app.router.lifespan_context = app_lifespan
    app = Boundary(base_app, spool, verifier, dev=dev)
    app.mcp = mcp  # local tests/Inspector only; never exposed over HTTP
    return app
