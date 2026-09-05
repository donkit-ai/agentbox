import re
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

from agentbox.connectors import oauth, service
from agentbox.db.models import ConnectionLink, utcnow


class ValidMailbox:
    def __init__(self, cfg):
        assert cfg["password"] == "private-app-password"

    def latest_uid(self):
        return 42

    def close(self):
        pass


async def test_browser_connection_never_returns_password_and_is_single_use(client, org, monkeypatch):
    monkeypatch.setattr(service, "_client_factory", ValidMailbox)
    created = await client.post("/v1/connection-links", headers=org.headers, json={"provider": "gmail"})
    assert created.status_code == 201, created.text
    link = created.json()
    parsed = urlsplit(link["url"])
    assert parsed.fragment and not parsed.query
    assert "private-app-password" not in str(link)
    initial = await client.get(parsed.path)
    assert "location.hash" in initial.text and "no-referrer" == initial.headers["Referrer-Policy"]
    assert (await client.post(parsed.path + "/session", json={"token": "bad-token-of-adequate-length"})).status_code == 404
    response = await client.post(parsed.path + "/session", json={"token": parsed.fragment})
    assert response.status_code == 204 and "HttpOnly" in response.headers["set-cookie"]
    form = await client.get(parsed.path)
    csrf = re.search(r'name="csrf" value="([^"]+)"', form.text)[1]
    data = {"address": "browser@gmail.com", "password": "private-app-password", "csrf": "bad"}
    assert (await client.post(parsed.path + "/password", data=data)).status_code == 403
    response = await client.post(parsed.path + "/password", data={**data, "csrf": csrf})
    assert response.status_code == 200, response.text
    assert "Connected" in response.text and "private-app-password" not in response.text
    status = (await client.get(f"/v1/connection-links/{link['id']}", headers=org.headers)).json()
    assert status["status"] == "connected" and status["inbox_id"]
    conn = (await client.get("/v1/connections", headers=org.headers)).json()["data"][0]
    assert conn["last_uid"] == 42 and "private-app-password" not in str(conn)
    assert (await client.post(parsed.path + "/password", data={**data, "csrf": csrf})).status_code == 410


async def test_invalid_connection_request_does_not_echo_password(client, org):
    response = await client.post("/v1/connections", headers=org.headers,
                                 json={"provider": "gmail", "password": "never-echo-this-secret"})
    assert response.status_code == 422 and "never-echo-this-secret" not in response.text


async def test_connection_link_is_org_scoped_and_expires(client, org, make_org, runtime):
    other = await make_org()
    created = (await client.post("/v1/connection-links", headers=org.headers, json={"provider": "gmail"})).json()
    path = f"/v1/connection-links/{created['id']}"
    assert (await client.get(path, headers=other.headers)).status_code == 404
    parsed = urlsplit(created["url"])
    async with runtime.db.session() as session:
        row = await session.get(ConnectionLink, created["id"])
        row.expires_at = utcnow() - timedelta(seconds=1)
        await session.commit()
    assert (await client.get(path, headers=org.headers)).json()["status"] == "expired"
    response = await client.post(parsed.path + "/session", json={"token": parsed.fragment})
    assert response.status_code == 410


async def test_oauth_callback_requires_browser_state_and_is_single_use(client, org, runtime, monkeypatch):
    monkeypatch.setattr(runtime.settings, "google_client_id", "test-google-client")
    monkeypatch.setattr(runtime.settings, "google_client_secret", "test-google-secret")

    async def exchange(http, settings, provider, code, redirect_uri, verifier, nonce):
        assert provider == "gmail" and code == "test-code" and verifier and nonce
        return {"auth": "oauth", "oauth_provider": "gmail", "email": "oauth-browser@gmail.com",
                "access_token": "private-access", "refresh_token": "private-refresh", "expires_at": 9999999999}

    class OAuthMailbox:
        def __init__(self, config):
            assert config["auth"] == "oauth" and config["access_token"] == "private-access"

        def latest_uid(self):
            return 73

        def close(self):
            pass

    monkeypatch.setattr(oauth, "exchange_code", exchange)
    monkeypatch.setattr(service, "_client_factory", OAuthMailbox)
    link = (await client.post("/v1/connection-links", headers=org.headers, json={"provider": "gmail"})).json()
    parsed = urlsplit(link["url"])
    await client.post(parsed.path + "/session", json={"token": parsed.fragment})
    form = await client.get(parsed.path)
    csrf = re.search(r'name="csrf" value="([^"]+)"', form.text)[1]
    start = await client.post(parsed.path + "/oauth", data={"csrf": csrf})
    assert start.status_code == 303
    params = parse_qs(urlsplit(start.headers["location"]).query)
    assert params["code_challenge_method"] == ["S256"]
    callback = "/connect/oauth/callback"
    state = params["state"][0]
    client.cookies.clear()
    assert (await client.get(callback, params={"state": state, "code": "test-code"})).status_code == 404
    await client.post(parsed.path + "/session", json={"token": parsed.fragment})
    bad = await client.get(callback, params={"state": link["id"] + ".wrong", "code": "test-code"})
    assert bad.status_code == 403
    connected = await client.get(callback, params={"state": state, "code": "test-code"})
    assert connected.status_code == 200 and "Connected" in connected.text
    assert "private-access" not in connected.text and "private-refresh" not in connected.text
    connections = (await client.get("/v1/connections", headers=org.headers)).json()["data"]
    assert connections[0]["last_uid"] == 73
    assert "private-access" not in str(connections) and "private-refresh" not in str(connections)
    assert (await client.get(callback, params={"state": state, "code": "test-code"})).status_code == 410
