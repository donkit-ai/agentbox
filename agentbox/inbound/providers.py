"""Provider authentication and conversion to the existing MIME ingestion format."""

import base64
import hashlib
import hmac
import json
import re
import time
from dataclasses import dataclass
from email import policy
from email.message import EmailMessage
from email.utils import formataddr

from pydantic import BaseModel, Field


@dataclass(frozen=True)
class InboundEmail:
    raw: bytes
    mail_from: str
    recipients: list[str]
    delivery_id: str | None = None


def verify_mailgun_signature(key: str, timestamp: str, token: str, signature: str) -> bool:
    # Mailgun retries for up to eight hours. Keep the original timestamp valid
    # throughout that window; the database deduplicates every accepted delivery.
    if not valid_timestamp(timestamp) or not token or len(token) > 256:
        return False
    expected = hmac.new(key.encode(), (timestamp + token).encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode(), signature.encode())


def valid_timestamp(timestamp: str) -> bool:
    if not re.fullmatch(r"[0-9]{1,12}", timestamp):
        return False
    age = time.time() - int(timestamp)
    # SendGrid retries for up to three days; tolerate clock skew into the future.
    return -300 <= age <= 3 * 86400


class PostmarkAddress(BaseModel):
    Email: str
    Name: str = ""


class PostmarkHeader(BaseModel):
    Name: str
    Value: str


class PostmarkAttachment(BaseModel):
    Name: str
    Content: str
    ContentType: str = "application/octet-stream"
    ContentID: str | None = None


class PostmarkEmail(BaseModel):
    From: str = ""
    FromFull: PostmarkAddress | None = None
    To: str = ""
    Cc: str = ""
    ToFull: list[PostmarkAddress] = Field(default_factory=list)
    CcFull: list[PostmarkAddress] = Field(default_factory=list)
    OriginalRecipient: str = Field(min_length=3, max_length=320)
    MessageID: str = Field(min_length=1, max_length=255)
    ReplyTo: str = ""
    Subject: str = ""
    Date: str = ""
    TextBody: str | None = None
    HtmlBody: str | None = None
    Headers: list[PostmarkHeader] = Field(default_factory=list)
    Attachments: list[PostmarkAttachment] = Field(default_factory=list)


def _header(value: str) -> str:
    # Unfold legal RFC headers but never allow a value to inject another header.
    value = re.sub(r"\r?\n[ \t]+", " ", value)
    if "\r" in value or "\n" in value or "\x00" in value:
        raise ValueError("Invalid email header")
    return value


def _addresses(addresses: list[PostmarkAddress], fallback: str) -> str:
    return ", ".join(formataddr((_header(a.Name), _header(a.Email)), charset="utf-8") for a in addresses) or fallback


def parse_postmark(body: bytes, max_bytes: int) -> InboundEmail:
    data = PostmarkEmail.model_validate_json(body)
    message = EmailMessage(policy=policy.SMTP)
    # These describe the provider's original MIME body, which is reconstructed
    # from JSON. Copying them would corrupt the new MIME tree.
    rebuilt = {"content-type", "content-transfer-encoding", "mime-version", "content-disposition", "bcc"}
    for header in data.Headers:
        if header.Name.lower() in rebuilt:
            continue
        if not re.fullmatch(r"[!-9;-~]+", header.Name):
            raise ValueError("Invalid email header name")
        message[header.Name] = _header(header.Value)
    sender = data.FromFull.Email if data.FromFull else data.From
    defaults = {
        "From": _addresses([data.FromFull], data.From) if data.FromFull else data.From,
        "To": _addresses(data.ToFull, data.To),
        "Cc": _addresses(data.CcFull, data.Cc),
        "Reply-To": data.ReplyTo,
        "Subject": data.Subject,
        "Date": data.Date,
        "Message-ID": f"<{data.MessageID}@inbound.postmarkapp.com>",
    }
    for name, value in defaults.items():
        if value and name not in message:
            message[name] = _header(value)
    if data.TextBody is not None:
        message.set_content(data.TextBody)
        if data.HtmlBody is not None:
            message.add_alternative(data.HtmlBody, subtype="html")
    elif data.HtmlBody is not None:
        message.set_content(data.HtmlBody, subtype="html")
    else:
        message.set_content("")
    total = 0
    for attachment in data.Attachments:
        content = base64.b64decode(attachment.Content, validate=True)
        total += len(content)
        if total > max_bytes:
            raise OverflowError("Message too large")
        content_type = attachment.ContentType.split(";", 1)[0].strip()
        if not re.fullmatch(r"[\w.+-]+/[\w.+-]+", content_type):
            content_type = "application/octet-stream"
        maintype, subtype = content_type.split("/", 1)
        cid = _header(attachment.ContentID or "").strip("<>")
        message.add_attachment(
            content, maintype=maintype, subtype=subtype, filename=_header(attachment.Name),
            disposition="inline" if cid else "attachment", cid=f"<{cid}>" if cid else None,
        )
    # Stable boundaries keep retries byte-identical even after JSON reordering.
    digest = hashlib.sha256(json.dumps(data.model_dump(), sort_keys=True).encode()).hexdigest()[:40]
    for index, part in enumerate(message.walk()):
        if part.is_multipart():
            part.set_boundary(f"agentbox-{digest}-{index}")
    raw = message.as_bytes()
    return InboundEmail(raw, sender, [data.OriginalRecipient], data.MessageID)
