"""JSON response types. Values remain plain dictionaries; no runtime model dependency."""
from typing import Generic, TypeVar

from typing_extensions import TypedDict as _TypedDict

T = TypeVar("T")


class Page(_TypedDict, Generic[T]):
    data: list[T]
    next_cursor: str | None


class Inbox(_TypedDict, total=False):
    id: str
    email: str
    username: str
    display_name: str | None
    status: str
    provider_mode: str
    metadata: dict
    created_at: str
    updated_at: str
    expires_at: str | None


class SendResult(_TypedDict):
    id: str
    thread_id: str
    status: str
    created_at: str


class Message(_TypedDict, total=False):
    id: str
    inbox_id: str
    thread_id: str
    direction: str
    status: str
    subject: str
    internet_message_id: str
    in_reply_to: str | None
    references: list[str]
    text: str | None
    html: str | None
    attachments: list[dict]
    created_at: str
    received_at: str | None
    sent_at: str | None


class ReplyWait(_TypedDict):
    message_id: str
    status: str
    reply_message_id: str | None
    expires_at: str
