"""Reject cross-origin browser mutations, including login and cookie-authenticated forms."""
from urllib.parse import urlsplit

from starlette.responses import JSONResponse


def _origin(value):
    parsed = urlsplit(value)
    return (parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))


class BrowserOriginMiddleware:
    def __init__(self, app, public_base_url):
        self.app, self.expected = app, _origin(public_base_url)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["method"] not in {"GET", "HEAD", "OPTIONS"}:
            headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
            # API bearer requests and provider webhooks do not use browser cookies.
            browser_path = not scope["path"].startswith("/v1/")
            if browser_path:
                source = headers.get("origin") or headers.get("referer")
                try:
                    denied = (source is not None and _origin(source) != self.expected)
                except ValueError:
                    denied = True
                if denied or headers.get("sec-fetch-site") == "cross-site":
                    response = JSONResponse({"error": {"code": "cross_origin_request",
                                                       "message": "Reload this page in AgentBox."}},
                                            status_code=403)
                    return await response(scope, receive, send)
        return await self.app(scope, receive, send)
