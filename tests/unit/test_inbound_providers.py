import base64
import json

import pytest

from agentbox.inbound.providers import parse_postmark
from agentbox.mime.parse import parse_mime


def test_postmark_rebuilds_mime_preserving_thread_headers_and_envelope():
    payload = {
        "OriginalRecipient": "bcc@example.com", "MessageID": "delivery-id",
        "FromFull": {"Name": "Поставщик", "Email": "vendor@example.org"},
        "ToFull": [{"Name": "Buyer", "Email": "visible@example.com"}],
        "CcFull": [{"Email": "copy@example.com"}], "ReplyTo": "reply@example.org",
        "HtmlBody": "<p>Hello</p>",
        "Headers": [
            {"Name": "Message-ID", "Value": "<original@example.org>"},
            {"Name": "References", "Value": "<first@example.org>\r\n <second@example.org>"},
            {"Name": "In-Reply-To", "Value": "<second@example.org>"},
            {"Name": "Content-Type", "Value": 'multipart/mixed; boundary="old"'},
            {"Name": "Content-Transfer-Encoding", "Value": "base64"},
            {"Name": "Bcc", "Value": "hidden@example.com"},
        ],
        "Attachments": [{"Name": "data.bin", "Content": base64.b64encode(bytes(range(256))).decode()}],
    }
    first = parse_postmark(json.dumps(payload).encode(), 10000)
    second = parse_postmark(json.dumps(payload, sort_keys=True).encode(), 10000)
    assert first.raw == second.raw
    assert first.recipients == ["bcc@example.com"]
    parsed = parse_mime(first.raw)
    assert parsed.message_id == "<original@example.org>"
    assert parsed.references == ["<first@example.org>", "<second@example.org>"]
    assert parsed.in_reply_to == "<second@example.org>"
    assert parsed.from_[0].name == "Поставщик"
    assert parsed.reply_to[0].email == "reply@example.org"
    assert parsed.to[0].email == "visible@example.com" and parsed.cc[0].email == "copy@example.com"
    assert parsed.text == "Hello" and "Hello" in parsed.html
    assert parsed.attachments[0].content == bytes(range(256))
    assert not any(name.lower() == "bcc" for name, _ in parsed.headers)


def test_postmark_attachment_limit():
    payload = {"OriginalRecipient": "x@y", "MessageID": "id", "Attachments": [
        {"Name": "x.bin", "Content": base64.b64encode(b"x" * 100).decode()},
    ]}
    with pytest.raises(OverflowError):
        parse_postmark(json.dumps(payload).encode(), 99)
