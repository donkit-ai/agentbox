"""Availability of dedicated agent mail in the public connection flow."""

from agentbox.providers.base import TemporaryError
from agentbox.providers.router import select_provider_account
from agentbox.security.crypto import decrypt_json
from agentbox.services.inboxes import get_domain_for_org


async def managed_delivery_ready(session, settings, organization_id):
    # The flag records the operator's delivery check. Seeded 'active' rows alone
    # are insufficient: the default deployment is a local Mailpit demo.
    if not settings.managed_delivery_enabled:
        return False
    name = settings.managed_domain.lower()
    if "." not in name or name.endswith((".local", ".localhost", ".test", ".invalid", ".example")):
        return False
    domain = await get_domain_for_org(session, organization_id, name)
    if domain is None or domain.inbound_status != "active" or domain.outbound_status != "active":
        return False
    try:
        account = await select_provider_account(session, organization_id)
    except TemporaryError:
        return False
    cfg = decrypt_json(settings.app_secret_key, account.config_encrypted)
    if account.provider == "smtp_relay":
        host = (cfg.get("host") or "").lower()
        return bool(host) and host not in {"mailpit", "localhost", "127.0.0.1", "::1"} and cfg.get("port") != 1025
    return account.provider in {"sendgrid", "unisender_go"} and bool(cfg.get("api_key"))
