"""Gmail read/send transport. No message mutation or deletion endpoints are used."""

import base64
from email import policy
from email.parser import BytesParser

import httpx

from agentbox.api.errors import APIError
from agentbox.mime.build import format_address
from agentbox.providers.base import PermanentError, SendResult, TemporaryError

BASE_URL = "https://gmail.googleapis.com/gmail/v1/users/me"


class GmailAPI:
    def __init__(self, http, cfg):
        self.http = http
        self.cfg = cfg

    async def request(self, method, path, *, allow_missing=False, **kwargs):
        try:
            response = await self.http.request(method, BASE_URL + path,
                headers={"Authorization": "Bearer " + self.cfg["access_token"]}, timeout=30, **kwargs)
        except httpx.RequestError as exc:
            raise APIError(502, "gmail_unavailable", "Gmail is unavailable. Try again later.") from exc
        if response.status_code == 404 and allow_missing:
            return None
        if response.status_code != 200:
            # Provider response bodies may include private mail or credentials.
            code = "gmail_access_denied" if response.status_code in (401, 403) else "gmail_unavailable"
            message = "Gmail access failed. Check permissions or reconnect." if code == "gmail_access_denied" else "Gmail is unavailable. Try again later."
            raise APIError(502, code, message, {"provider_status": response.status_code})
        return response.json()

    async def initialize(self, address, *, import_history=False):
        profile = await self.request("GET", "/profile")
        if profile["emailAddress"].lower() != address.lower():
            raise APIError(403, "oauth_identity_invalid", "Gmail mailbox does not match the authorized Google account.")
        self.cfg["gmail_history_id"] = str(profile["historyId"])
        self.cfg["gmail_import_history"] = import_history

    async def fetch_new(self):
        """Fetch one complete page; persist its cursor only with the queued ingests.

        The initial history ID is captured before an optional full import. During
        incremental pagination it stays fixed until the last page is processed.
        """
        importing = self.cfg.get("gmail_import_history", False)
        params = {"maxResults": 50}
        if self.cfg.get("gmail_page_token"):
            params["pageToken"] = self.cfg["gmail_page_token"]
        if importing:
            params["labelIds"] = "INBOX"
            result = await self.request("GET", "/messages", params=params)
            ids = [m["id"] for m in result.get("messages", [])]
        else:
            params.update(startHistoryId=self.cfg["gmail_history_id"], historyTypes="messageAdded", labelId="INBOX")
            result = await self.request("GET", "/history", params=params, allow_missing=True)
            if result is None:
                raise APIError(409, "gmail_history_expired", "Gmail sync history expired. Disconnect and reconnect to resume receiving new mail.")
            # History messages often contain only id/threadId. Filter by label
            # in the request instead of dropping entries without labelIds.
            ids = [m["message"]["id"] for h in result.get("history", []) for m in h.get("messagesAdded", [])]
        fetched = []
        for message_id in dict.fromkeys(ids):
            result_message = await self.request("GET", "/messages/" + message_id, params={"format": "raw"}, allow_missing=True)
            if result_message is not None:
                raw = result_message["raw"]
                fetched.append(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        if result.get("nextPageToken"):
            self.cfg["gmail_page_token"] = result["nextPageToken"]
        else:
            self.cfg.pop("gmail_page_token", None)
            if importing:
                self.cfg["gmail_import_history"] = False
            else:
                self.cfg["gmail_history_id"] = str(result["historyId"])
        return fetched


class GmailProvider:
    name = "gmail_api"

    def __init__(self, http, cfg):
        self.api = GmailAPI(http, cfg)

    async def send(self, envelope, message, raw):
        # Gmail derives recipients from MIME headers, whereas SMTP has an envelope.
        # Add Bcc only to the submitted copy; the stored RFC822 stays Bcc-free.
        if message.bcc:
            submitted = BytesParser(policy=policy.SMTP).parsebytes(raw)
            submitted["Bcc"] = ", ".join(format_address(a) for a in message.bcc)
            raw = submitted.as_bytes()
        try:
            result = await self.api.request("POST", "/messages/send", json={"raw": base64.urlsafe_b64encode(raw).decode()})
        except APIError as exc:
            status = exc.details.get("provider_status", 503)
            failure = TemporaryError if status == 429 or status >= 500 else PermanentError
            # Gmail also reports quota limits as HTTP 403. Treat those conservatively
            # as retryable, without exposing the provider body.
            if status == 403:
                failure = TemporaryError
            raise failure(exc.message) from exc
        return SendResult(accepted=True, provider_message_id=result["id"])
