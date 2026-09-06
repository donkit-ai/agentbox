import re
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import select

from agentbox.api.errors import APIError
from agentbox.connectors import oauth
from agentbox.db.models import ConnectionLink, Domain, Inbox, MailboxConnection, ProviderAccount
from agentbox.security.crypto import decrypt_json, encrypt_json


async def open_link(client, org):
    link = (await client.post("/v1/connection-links", headers=org.headers, json={})).json()
    parsed = urlsplit(link["url"])
    assert (await client.post(parsed.path + "/session", json={"token": parsed.fragment})).status_code == 204
    response = await client.get(parsed.path)
    csrf = re.search(r'name="csrf" value="([^"]+)"', response.text)[1]
    return link, parsed.path, csrf, response


async def test_unconfigured_choices_are_visible_without_password_fallback(client, org):
    link, path, csrf, response = await open_link(client, org)
    assert response.status_code == 200
    assert "Создать почту агенту" in response.text and "Подключить мой Gmail" in response.text
    assert "настройте получение и отправку" in response.text and "Вы управляете этой установкой" in response.text
    assert 'type="password"' not in response.text and 'name="address"' not in response.text
    assert "весь ящик" in response.text and "копии писем" in response.text
    assert response.headers["cache-control"] == "no-store"
    assert "style-src 'self'" in response.headers["content-security-policy"]
    for action in ("managed", "oauth"):
        result = await client.post(path + "/" + action, data={"csrf": csrf})
        assert result.status_code == 409 and "text/html" in result.headers["content-type"]
        assert "Вернуться к выбору" in result.text
    password = await client.post(path + "/password", data={"csrf": csrf, "address": "me@gmail.com", "password": "private-password"})
    assert password.status_code == 403 and "private-password" not in password.text
    assert (await client.get(f"/v1/connection-links/{link['id']}", headers=org.headers)).json()["status"] == "pending"


async def test_passwords_stay_disabled_in_cloud_even_if_flag_is_set(client, org, runtime, monkeypatch):
    monkeypatch.setattr(runtime.settings, "allow_app_password_connections", True)
    monkeypatch.setattr(runtime.settings, "edition", "cloud")
    _, path, csrf, response = await open_link(client, org)
    assert 'type="password"' not in response.text
    result = await client.post(path + "/password", data={"csrf": csrf, "address": "me@gmail.com", "password": "secret"})
    assert result.status_code == 403
    result = await client.post("/v1/connections", headers=org.headers,
                               json={"provider": "gmail", "address": "me@gmail.com", "password": "secret"})
    assert result.status_code == 403


async def test_verified_managed_delivery_creates_single_use_inbox(client, org, runtime, monkeypatch):
    monkeypatch.setattr(runtime.settings, "managed_delivery_enabled", True)
    # Merely enabling a flag must not turn the seeded Mailpit demo into live mail.
    _, path, csrf, _ = await open_link(client, org)
    assert (await client.post(path + "/managed", data={"csrf": csrf})).status_code == 409
    monkeypatch.setattr(runtime.settings, "managed_domain", "agents.company.org")
    async with runtime.db.session() as session:
        domain = await session.scalar(select(Domain).where(Domain.domain == "agentbox.local"))
        domain.domain = "agents.company.org"
        account = await session.scalar(select(ProviderAccount))
        account.config_encrypted = encrypt_json(runtime.settings.app_secret_key, {"host": "smtp.company.org", "port": 587})
        await session.commit()
    link, path, csrf, response = await open_link(client, org)
    assert 'name="username"' in response.text
    assert (await client.post(path + "/managed", data={"csrf": "bad"})).status_code == 403
    result = await client.post(path + "/managed", data={"csrf": csrf, "username": "research-agent"})
    assert result.status_code == 200 and "research-agent@agents.company.org" in result.text
    assert "Почта агента создана" in result.text
    status = (await client.get(f"/v1/connection-links/{link['id']}", headers=org.headers)).json()
    assert status["status"] == "connected" and status["inbox_id"]
    async with runtime.db.session() as session:
        inbox = await session.get(Inbox, status["inbox_id"])
        assert inbox.provider_mode == "managed" and inbox.organization_id == org.id
        assert await session.scalar(select(MailboxConnection)) is None
        row = await session.get(ConnectionLink, link["id"])
        assert decrypt_json(runtime.settings.app_secret_key, row.state_encrypted) == {}
    assert (await client.post(path + "/managed", data={"csrf": csrf})).status_code == 410


async def test_cancelled_google_consent_is_retryable_and_state_is_consumed(client, org, runtime, monkeypatch):
    monkeypatch.setattr(runtime.settings, "google_client_id", "google-client")
    monkeypatch.setattr(runtime.settings, "google_client_secret", "google-secret")
    link, path, csrf, _ = await open_link(client, org)
    start = await client.post(path + "/oauth", data={"csrf": csrf})
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    result = await client.get("/connect/oauth/callback", params={"state": state, "error": "access_denied"})
    assert result.status_code == 200 and "Подключение отменено" in result.text and path in result.text
    assert (await client.get("/connect/oauth/callback", params={"state": state, "code": "old-code"})).status_code == 403
    assert (await client.get(f"/v1/connection-links/{link['id']}", headers=org.headers)).json()["status"] == "pending"
    retry = await client.post(path + "/oauth", data={"csrf": csrf})
    assert retry.status_code == 303 and state not in retry.headers["location"]


async def test_google_failure_is_styled_and_link_can_retry(client, org, runtime, monkeypatch):
    monkeypatch.setattr(runtime.settings, "google_client_id", "google-client")
    monkeypatch.setattr(runtime.settings, "google_client_secret", "google-secret")

    async def fail(*args):
        raise APIError(502, "oauth_error", "private-provider-details")

    monkeypatch.setattr(oauth, "exchange_code", fail)
    _, path, csrf, _ = await open_link(client, org)
    start = await client.post(path + "/oauth", data={"csrf": csrf})
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    result = await client.get("/connect/oauth/callback", params={"state": state, "code": "secret-code"})
    assert result.status_code == 502 and "text/html" in result.headers["content-type"]
    assert "private-provider-details" not in result.text and "secret-code" not in result.text
    assert (await client.post(path + "/oauth", data={"csrf": csrf})).status_code == 303


async def test_organization_name_is_escaped(client, make_org):
    org = await make_org('<img src=x onerror="alert(1)">')
    _, _, _, response = await open_link(client, org)
    assert '<img src=x' not in response.text and '&lt;img src=x' in response.text


async def test_legacy_gmail_tokens_require_reconnection(runtime, org):
    from agentbox.connectors.service import _fresh_config

    cfg = {"auth": "oauth", "oauth_provider": "gmail", "access_token": "legacy-secret", "expires_at": 9999999999}
    conn = MailboxConnection(provider="gmail", config_encrypted=encrypt_json(runtime.settings.app_secret_key, cfg))
    async with runtime.db.session() as session:
        with pytest.raises(APIError) as exc:
            await _fresh_config(session, runtime.settings, conn, runtime.http)
        assert exc.value.code == "oauth_reconnect_required"
