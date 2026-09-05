from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from agentbox_sdk.types import Inbox, Message, Page, ReplyWait, SendResult

RETRY_STATUSES = {429, 502, 503, 504}


class AgentBoxError(Exception):
    def __init__(self, status: int, code: str, message: str, request_id: str | None = None,
                 details: dict | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.status, self.code, self.message = status, code, message
        self.request_id, self.details = request_id, details or {}


def _addresses(items) -> list:
    if items is None:
        return []
    if isinstance(items, (str, dict)):
        items = [items]
    return [{"email": a} if isinstance(a, str) else a for a in items]


def _clean(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None}


class AgentBox:
    """Synchronous AgentBox client. Every method returns plain dicts (JSON as returned by the API)."""

    def __init__(self, api_key: str, base_url: str = "http://localhost:8000", timeout: float = 30.0,
                 max_retries: int = 3, transport: httpx.BaseTransport | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        headers = {"Authorization": f"Bearer {api_key}", "User-Agent": "agentbox-sdk-python/0.2"}
        self._http = httpx.Client(base_url=self.base_url, timeout=timeout, transport=transport, headers=headers)
        self.inboxes = Inboxes(self)
        self.connections = Connections(self)
        self.messages = Messages(self)
        self.threads = Threads(self)
        self.attachments = Attachments(self)
        self.webhooks = Webhooks(self)
        self.events = Events(self)
        self.domains = Domains(self)
        self.policy = PolicyResource(self)
        self.suppressions = Suppressions(self)
        self.provider_accounts = ProviderAccounts(self)
        self.routing_rules = RoutingRules(self)
        self.analytics = Analytics(self)
        self.usage = Usage(self)
        self.api_keys = ApiKeys(self)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> AgentBox:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def me(self) -> dict:
        return self.request("GET", "/v1/me")

    def request(self, method: str, path: str, *, params: dict | None = None, json: Any = None,
                idempotency_key: str | None = None, timeout: float | None = None) -> Any:
        headers = {}
        method = method.upper()
        # Generate once per invocation, not per attempt. Pass your own key to resume after a process restart.
        if method == "POST" and (path == "/v1/inboxes" or path == "/v1/connections"
                                  or path.endswith(("/messages", "/reply", "/forward"))):
            idempotency_key = idempotency_key or str(uuid.uuid4())
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        retryable = method in ("GET", "HEAD", "OPTIONS") or idempotency_key is not None
        attempt = 0
        while True:
            attempt += 1
            try:
                resp = self._http.request(method, path, params=_clean(params or {}), json=json, headers=headers,
                                          **({"timeout": timeout} if timeout is not None else {}))
            except httpx.TransportError:
                if not retryable or attempt > self.max_retries:
                    raise
                time.sleep(0.5 * attempt)
                continue
            if resp.status_code in RETRY_STATUSES and retryable and attempt <= self.max_retries:
                wait = float(resp.headers.get("Retry-After", 0) or 0) or 0.5 * attempt
                time.sleep(min(wait, 10))
                continue
            if resp.status_code >= 400:
                try:
                    err = resp.json()["error"]
                except (ValueError, KeyError):
                    raise AgentBoxError(resp.status_code, "http_error", resp.text[:500]) from None
                raise AgentBoxError(resp.status_code, err.get("code", "error"), err.get("message", ""),
                                    err.get("request_id"), err.get("details"))
            if resp.status_code == 204 or not resp.content:
                return None
            return resp.json()


class _Resource:
    def __init__(self, client: AgentBox) -> None:
        self._c = client

    def iter(self, *args, **kwargs):
        """Iterate every item, fetching subsequent pages lazily."""
        while True:
            page = self.list(*args, **kwargs)
            if isinstance(page, list):
                yield from page
                return
            yield from page["data"]
            if not page.get("next_cursor"):
                return
            kwargs["cursor"] = page["next_cursor"]


class Inboxes(_Resource):
    def create(self, username: str | None = None, *, domain: str | None = None, display_name: str | None = None,
               metadata: dict | None = None, ttl: str | None = None, idempotency_key: str | None = None) -> Inbox:
        body = _clean({"username": username, "domain": domain, "display_name": display_name,
                       "metadata": metadata, "ttl": ttl})
        return self._c.request("POST", "/v1/inboxes", json=body, idempotency_key=idempotency_key)

    def list(self, *, status: str | None = None, domain: str | None = None, metadata: dict | None = None,
             limit: int = 20, cursor: str | None = None) -> Page[Inbox]:
        params = {"status": status, "domain": domain, "limit": limit, "cursor": cursor}
        for k, v in (metadata or {}).items():
            params[f"metadata.{k}"] = v
        return self._c.request("GET", "/v1/inboxes", params=params)

    def get(self, inbox_id: str) -> Inbox:
        return self._c.request("GET", f"/v1/inboxes/{inbox_id}")

    def disable(self, inbox_id: str) -> dict:
        return self._c.request("POST", f"/v1/inboxes/{inbox_id}/disable")

    def enable(self, inbox_id: str) -> dict:
        return self._c.request("POST", f"/v1/inboxes/{inbox_id}/enable")

    def delete(self, inbox_id: str) -> None:
        self._c.request("DELETE", f"/v1/inboxes/{inbox_id}")

    def get_policy(self, inbox_id: str) -> dict:
        return self._c.request("GET", f"/v1/inboxes/{inbox_id}/policy")

    def set_policy(self, inbox_id: str, config: dict) -> dict:
        return self._c.request("PUT", f"/v1/inboxes/{inbox_id}/policy", json=config)

    def delete_policy(self, inbox_id: str) -> None:
        self._c.request("DELETE", f"/v1/inboxes/{inbox_id}/policy")


class Connections(_Resource):
    """Existing mailboxes (Gmail, Yandex 360, Microsoft 365, any IMAP) exposed as inboxes."""

    def presets(self) -> list[dict]:
        return self._c.request("GET", "/v1/connections/presets")["data"]

    def create_link(self, provider: str = "gmail") -> dict:
        """Give the URL to the human. Never ask them to paste credentials into agent chat."""
        return self._c.request("POST", "/v1/connection-links", json={"provider": provider})

    def link_status(self, link_id: str) -> dict:
        return self._c.request("GET", f"/v1/connection-links/{link_id}")

    def create(self, provider: str, address: str, password: str, *, username: str | None = None,
               imap_host: str | None = None, imap_port: int | None = None, smtp_host: str | None = None,
               smtp_port: int | None = None, smtp_ssl: bool | None = None, display_name: str | None = None,
               metadata: dict | None = None, idempotency_key: str | None = None) -> dict:
        body = _clean({"provider": provider, "address": address, "password": password, "username": username,
                       "imap_host": imap_host, "imap_port": imap_port, "smtp_host": smtp_host, "smtp_port": smtp_port,
                       "smtp_ssl": smtp_ssl, "display_name": display_name, "metadata": metadata or {}})
        return self._c.request("POST", "/v1/connections", json=body, idempotency_key=idempotency_key)

    def list(self) -> list[dict]:
        return self._c.request("GET", "/v1/connections")["data"]

    def get(self, connection_id: str) -> dict:
        return self._c.request("GET", f"/v1/connections/{connection_id}")

    def sync(self, connection_id: str) -> dict:
        return self._c.request("POST", f"/v1/connections/{connection_id}/sync")

    def pause(self, connection_id: str) -> dict:
        return self._c.request("POST", f"/v1/connections/{connection_id}/pause")

    def resume(self, connection_id: str) -> dict:
        return self._c.request("POST", f"/v1/connections/{connection_id}/resume")

    def delete(self, connection_id: str) -> None:
        self._c.request("DELETE", f"/v1/connections/{connection_id}")


class Messages(_Resource):
    def start_reply_wait(self, message_id: str, *, expires_in: int = 86400) -> ReplyWait:
        """Persist a wait for a reply referencing this sent message. Repeated calls do not reset its deadline."""
        return self._c.request("POST", f"/v1/messages/{message_id}/reply-wait", json={"expires_in": expires_in})

    def reply_wait(self, message_id: str) -> ReplyWait:
        return self._c.request("GET", f"/v1/messages/{message_id}/reply-wait")

    def send(self, inbox_id: str, *, to, subject: str = "", text: str | None = None, html: str | None = None,
             cc=None, bcc=None, reply_to=None, attachment_ids: list[str] | None = None,
             headers: dict | None = None, metadata: dict | None = None,
             idempotency_key: str | None = None) -> SendResult:
        body = _clean({"to": _addresses(to), "cc": _addresses(cc), "bcc": _addresses(bcc),
                       "reply_to": _addresses(reply_to), "subject": subject, "text": text, "html": html,
                       "attachment_ids": attachment_ids, "headers": headers, "metadata": metadata})
        return self._c.request("POST", f"/v1/inboxes/{inbox_id}/messages", json=body,
                               idempotency_key=idempotency_key)

    def list(self, inbox_id: str, *, direction: str | None = None, status: str | None = None,
             thread_id: str | None = None, from_: str | None = None, to: str | None = None,
             since: str | None = None, until: str | None = None, wait: int = 0, limit: int = 20,
             cursor: str | None = None) -> Page[Message]:
        params = {"direction": direction, "status": status, "thread_id": thread_id, "from": from_, "to": to,
                  "since": since, "until": until, "wait": wait or None, "limit": limit, "cursor": cursor}
        return self._c.request("GET", f"/v1/inboxes/{inbox_id}/messages", params=params,
                               timeout=max(30.0, wait + 10.0))

    def wait_for(self, inbox_id: str, *, direction: str = "inbound", since: str | None = None,
                 thread_id: str | None = None, timeout: float = 300.0) -> Message | None:
        """Wait for NEW mail by default. Pass since explicitly to include existing mail."""
        since = since or datetime.now(timezone.utc).isoformat()
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            page = self.list(inbox_id, direction=direction, since=since, thread_id=thread_id,
                             wait=int(min(60, max(1, remaining))), limit=1)
            if page["data"]:
                return page["data"][0]

    def get(self, message_id: str, *, include_headers: bool = False) -> Message:
        return self._c.request("GET", f"/v1/messages/{message_id}",
                               params={"include": "headers" if include_headers else None})

    def reply(self, message_id: str, *, text: str | None = None, html: str | None = None, reply_all: bool = False,
              to=None, cc=None, bcc=None, attachment_ids: list[str] | None = None,
              idempotency_key: str | None = None) -> dict:
        body = _clean({"text": text, "html": html, "reply_all": reply_all, "to": _addresses(to),
                       "cc": _addresses(cc), "bcc": _addresses(bcc), "attachment_ids": attachment_ids})
        return self._c.request("POST", f"/v1/messages/{message_id}/reply", json=body,
                               idempotency_key=idempotency_key)

    def forward(self, message_id: str, *, to, text: str | None = None, html: str | None = None, cc=None, bcc=None,
                include_attachments: bool = True, idempotency_key: str | None = None) -> dict:
        body = _clean({"to": _addresses(to), "cc": _addresses(cc), "bcc": _addresses(bcc), "text": text,
                       "html": html, "include_attachments": include_attachments})
        return self._c.request("POST", f"/v1/messages/{message_id}/forward", json=body,
                               idempotency_key=idempotency_key)

    def pending_approvals(self, *, limit: int = 20, cursor: str | None = None) -> dict:
        return self._c.request("GET", "/v1/approvals", params={"limit": limit, "cursor": cursor})

    def approve(self, message_id: str) -> dict:
        return self._c.request("POST", f"/v1/messages/{message_id}/approve")

    def reject(self, message_id: str, reason: str | None = None) -> dict:
        return self._c.request("POST", f"/v1/messages/{message_id}/reject", json=_clean({"reason": reason}))


class Threads(_Resource):
    def list(self, inbox_id: str, *, limit: int = 20, cursor: str | None = None) -> dict:
        return self._c.request("GET", f"/v1/inboxes/{inbox_id}/threads", params={"limit": limit, "cursor": cursor})

    def get(self, thread_id: str) -> dict:
        return self._c.request("GET", f"/v1/threads/{thread_id}")


class Attachments(_Resource):
    def upload_file(self, path: str | Path, content_type: str | None = None) -> str:
        import mimetypes

        file = Path(path)
        return self.upload(file.name, file.read_bytes(), content_type or mimetypes.guess_type(file.name)[0]
                           or "application/octet-stream")

    def upload(self, filename: str, content: bytes, content_type: str = "application/octet-stream") -> str:
        """Create a pre-signed upload, PUT the bytes, return the attachment id to use in send/reply."""
        up = self._c.request("POST", "/v1/attachments/uploads",
                             json={"filename": filename, "content_type": content_type, "size_bytes": len(content)})
        with httpx.Client(timeout=120.0) as ext:
            r = ext.put(up["upload_url"], content=content, headers=up.get("headers") or {"Content-Type": content_type})
            r.raise_for_status()
        return up["attachment_id"]

    def get(self, attachment_id: str) -> dict:
        return self._c.request("GET", f"/v1/attachments/{attachment_id}")

    def list(self, message_id: str) -> dict:
        return self._c.request("GET", f"/v1/messages/{message_id}/attachments")

    def download_url(self, attachment_id: str) -> dict:
        return self._c.request("GET", f"/v1/attachments/{attachment_id}/download")

    def download(self, attachment_id: str) -> bytes:
        url = self.download_url(attachment_id)["url"]
        with httpx.Client(timeout=120.0) as ext:
            r = ext.get(url)
            r.raise_for_status()
            return r.content


class Webhooks(_Resource):
    def create(self, url: str, *, event_types: list[str] | None = None, inbox_id: str | None = None,
               description: str | None = None) -> dict:
        return self._c.request("POST", "/v1/webhooks", json=_clean({"url": url, "event_types": event_types,
                                                                     "inbox_id": inbox_id, "description": description}))

    def list(self, *, limit: int = 20, cursor: str | None = None) -> dict:
        return self._c.request("GET", "/v1/webhooks", params={"limit": limit, "cursor": cursor})

    def get(self, webhook_id: str) -> dict:
        return self._c.request("GET", f"/v1/webhooks/{webhook_id}")

    def update(self, webhook_id: str, **fields) -> dict:
        return self._c.request("PATCH", f"/v1/webhooks/{webhook_id}", json=_clean(fields))

    def delete(self, webhook_id: str) -> None:
        self._c.request("DELETE", f"/v1/webhooks/{webhook_id}")

    def deliveries(self, webhook_id: str, *, limit: int = 20, cursor: str | None = None) -> dict:
        return self._c.request("GET", f"/v1/webhooks/{webhook_id}/deliveries",
                               params={"limit": limit, "cursor": cursor})

    def retry(self, webhook_id: str, delivery_id: str) -> dict:
        return self._c.request("POST", f"/v1/webhooks/{webhook_id}/deliveries/{delivery_id}/retry")


class Events(_Resource):
    def claim(self, consumer: str, *, types: list[str] | None = None, limit: int = 10,
              lease_seconds: int = 120, since: str | None = None) -> dict:
        return self._c.request("POST", f"/v1/consumers/{consumer}/claim", json=_clean({
            "types": types, "limit": limit, "lease_seconds": lease_seconds, "since": since}))

    def ack(self, consumer: str, event_id: str, lease_token: str) -> dict:
        return self._c.request("POST", f"/v1/consumers/{consumer}/events/{event_id}/ack",
                               json={"lease_token": lease_token})

    def list(self, *, type: str | None = None, resource_id: str | None = None, since: str | None = None,
             until: str | None = None, limit: int = 20, cursor: str | None = None) -> dict:
        return self._c.request("GET", "/v1/events", params={"type": type, "resource_id": resource_id, "since": since,
                                                             "until": until, "limit": limit, "cursor": cursor})


class Domains(_Resource):
    def create(self, domain: str) -> dict:
        return self._c.request("POST", "/v1/domains", json={"domain": domain})

    def list(self, *, limit: int = 20, cursor: str | None = None) -> dict:
        return self._c.request("GET", "/v1/domains", params={"limit": limit, "cursor": cursor})

    def get(self, domain_id: str) -> dict:
        return self._c.request("GET", f"/v1/domains/{domain_id}")

    def verify(self, domain_id: str) -> dict:
        return self._c.request("POST", f"/v1/domains/{domain_id}/verify")

    def delete(self, domain_id: str) -> None:
        self._c.request("DELETE", f"/v1/domains/{domain_id}")


class PolicyResource(_Resource):
    def get(self) -> dict:
        return self._c.request("GET", "/v1/policy")

    def set(self, config: dict) -> dict:
        return self._c.request("PUT", "/v1/policy", json=config)

    def delete(self) -> None:
        self._c.request("DELETE", "/v1/policy")


class Suppressions(_Resource):
    def list(self, *, email: str | None = None, reason: str | None = None, limit: int = 20,
             cursor: str | None = None) -> dict:
        return self._c.request("GET", "/v1/suppressions", params={"email": email, "reason": reason, "limit": limit,
                                                                   "cursor": cursor})

    def create(self, email: str, *, reason: str = "manual", note: str | None = None,
               expires_at: str | None = None) -> dict:
        return self._c.request("POST", "/v1/suppressions", json=_clean({"email": email, "reason": reason,
                                                                         "note": note, "expires_at": expires_at}))

    def delete(self, suppression_id: str) -> None:
        self._c.request("DELETE", f"/v1/suppressions/{suppression_id}")


class ProviderAccounts(_Resource):
    def list(self) -> dict:
        return self._c.request("GET", "/v1/provider-accounts")

    def create(self, provider: str, name: str, config: dict) -> dict:
        return self._c.request("POST", "/v1/provider-accounts", json={"provider": provider, "name": name,
                                                                       "config": config})

    def test(self, account_id: str) -> dict:
        return self._c.request("POST", f"/v1/provider-accounts/{account_id}/test")

    def delete(self, account_id: str) -> None:
        self._c.request("DELETE", f"/v1/provider-accounts/{account_id}")


class RoutingRules(_Resource):
    def list(self) -> dict:
        return self._c.request("GET", "/v1/routing-rules")

    def create(self, provider_account_id: str, *, priority: int = 100, match: dict | None = None,
               description: str | None = None) -> dict:
        return self._c.request("POST", "/v1/routing-rules", json=_clean({
            "provider_account_id": provider_account_id, "priority": priority, "match": match or {},
            "description": description}))

    def delete(self, rule_id: str) -> None:
        self._c.request("DELETE", f"/v1/routing-rules/{rule_id}")


class Analytics(_Resource):
    def delivery(self, *, since: str | None = None, until: str | None = None, group_by: str = "provider") -> dict:
        return self._c.request("GET", "/v1/analytics/delivery", params={"since": since, "until": until,
                                                                         "group_by": group_by})


class Usage(_Resource):
    def get(self, *, since: str | None = None, until: str | None = None) -> dict:
        return self._c.request("GET", "/v1/usage", params={"since": since, "until": until})


class ApiKeys(_Resource):
    def list(self) -> dict:
        return self._c.request("GET", "/v1/api-keys")

    def create(self, name: str, *, scopes: list[str] | None = None, environment: str = "live",
               permission_profile: str | None = None, inbox_ids: list[str] | None = None,
               allowed_recipients: list[str] | None = None) -> dict:
        return self._c.request("POST", "/v1/api-keys", json=_clean({"name": name, "scopes": scopes,
            "environment": environment, "permission_profile": permission_profile,
            "inbox_ids": inbox_ids, "allowed_recipients": allowed_recipients}))

    def revoke(self, key_id: str) -> None:
        self._c.request("DELETE", f"/v1/api-keys/{key_id}")


def new_idempotency_key() -> str:
    return uuid.uuid4().hex
