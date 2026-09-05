"""Per-request API-key authentication. No shared server key, local file tools, or claimed OAuth login flow."""
import os
from urllib.parse import urlsplit

import httpx
from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend, RequireAuthMiddleware
from mcp.server.auth.provider import AccessToken
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.authentication import AuthenticationMiddleware

from agentbox_sdk.mcp_server import build_server


class Verifier:
    async def verify_token(self, token: str):
        base = os.environ.get("AGENTBOX_API_URL", "http://localhost:8000").rstrip("/")
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(base + "/v1/me", headers={"Authorization": "Bearer " + token})
            if response.status_code != 200:
                return None
            me = response.json()
            return AccessToken(token=token, client_id=me["organization_id"], scopes=me["scopes"],
                               subject=me["api_key_id"])
        except (httpx.HTTPError, KeyError, ValueError):
            return None


class HttpAuth:
    def __init__(self, app):
        self.app = app
        self.auth = AuthenticationMiddleware(AuthContextMiddleware(RequireAuthMiddleware(app, required_scopes=[])),
                                             backend=BearerAuthBackend(Verifier()))

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        return await self.auth(scope, receive, send)


def create_http_app():
    public = os.environ.get("AGENTBOX_MCP_PUBLIC_URL", "http://localhost:8001/mcp")
    url = urlsplit(public)
    if url.scheme != "https" and url.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("Remote MCP requires an HTTPS public URL (TLS may terminate at a reverse proxy).")
    origins = [f"{url.scheme}://{url.netloc}"]
    hosts = [url.netloc]
    if url.hostname in ("localhost", "127.0.0.1", "::1"):
        hosts.extend(["localhost:*", "127.0.0.1:*", "[::1]:*"])
    server = build_server(remote=True)
    security = TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                          allowed_hosts=hosts, allowed_origins=origins)
    # mcp 2.x keeps transport settings on app creation; 1.x keeps them on FastMCP.settings.
    try:
        app = server.streamable_http_app(stateless_http=True, json_response=True, transport_security=security)
    except TypeError:
        server.settings.stateless_http = True
        server.settings.json_response = True
        server.settings.transport_security = security
        app = server.streamable_http_app()
    app.add_middleware(HttpAuth)
    return app
