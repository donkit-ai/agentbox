"""Agent capabilities. Inbox filtering applies to every ORM read in an authenticated session."""
from sqlalchemy import event, or_, select
from sqlalchemy.orm import Session, with_loader_criteria

from agentbox.db.models import Attachment, Event, Inbox, MailboxConnection, Message, Thread

READ_SCOPES = ("inboxes:read", "messages:read", "attachments:read", "events:read")
PROFILES = {
    "readonly": READ_SCOPES,
    "review": (*READ_SCOPES, "messages:send", "attachments:write", "events:consume"),
    "autonomous": (*READ_SCOPES, "messages:send", "attachments:write", "events:consume"),
}


@event.listens_for(Session, "do_orm_execute")
def restrict_inbox_reads(state) -> None:
    principal = state.session.info.get("principal")
    if not state.is_select or principal is None or principal.inbox_ids is None:
        return
    ids = list(principal.inbox_ids)
    key_id = principal.api_key_id
    message_ids = select(Message.id).where(Message.inbox_id.in_(ids))
    criteria = [
        (Inbox, Inbox.id.in_(ids)),
        (Message, Message.inbox_id.in_(ids)),
        (Thread, Thread.inbox_id.in_(ids)),
        (MailboxConnection, MailboxConnection.inbox_id.in_(ids)),
        (Event, Event.payload["inbox_id"].astext.in_(ids)),
        (Attachment, or_(Attachment.message_id.in_(message_ids),
                         (Attachment.message_id.is_(None)) & (Attachment.uploaded_by_key_id == key_id))),
    ]
    for model, condition in criteria:
        state.statement = state.statement.options(with_loader_criteria(model, condition, include_aliases=True))
