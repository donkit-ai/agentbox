import pytest


@pytest.mark.parametrize("profile", ["readonly", "review", "autonomous"])
async def test_agent_key_isolation_and_send_policy(client, org, profile):
    a = (await client.post("/v1/inboxes", headers=org.headers, json={})).json()
    b = (await client.post("/v1/inboxes", headers=org.headers, json={})).json()
    other = (await client.post(f"/v1/inboxes/{b['id']}/messages", headers=org.headers,
                              json={"to": ["supplier@example.com"], "text": "private"})).json()
    r = await client.post("/v1/api-keys", headers=org.headers, json={
        "name": "agent", "permission_profile": profile, "inbox_ids": [a["id"]],
        "allowed_recipients": ["supplier@example.com"],
    })
    assert r.status_code == 201, r.text
    auth = {"Authorization": "Bearer " + r.json()["api_key"]}
    assert "admin" not in r.json()["scopes"]
    assert [i["id"] for i in (await client.get("/v1/inboxes", headers=auth)).json()["data"]] == [a["id"]]
    for url in [f"/v1/inboxes/{b['id']}", f"/v1/messages/{other['id']}", f"/v1/threads/{other['thread_id']}"]:
        assert (await client.get(url, headers=auth)).status_code == 404
    for event in (await client.get("/v1/events", headers=auth)).json()["data"]:
        assert event["data"]["inbox_id"] == a["id"]
    assert (await client.post("/v1/api-keys", headers=auth, json={"name": "escalate"})).status_code == 403
    assert (await client.post(f"/v1/messages/{other['id']}/approve", headers=auth)).status_code == 403
    payload = {"to": ["supplier@example.com"], "text": "quote please"}
    sent = await client.post(f"/v1/inboxes/{a['id']}/messages", headers={**auth, "Idempotency-Key": "task-1"}, json=payload)
    if profile == "readonly":
        assert sent.status_code == 403
        return
    assert sent.status_code == 202, sent.text
    assert sent.json()["status"] == ("pending_approval" if profile == "review" else "queued")
    replay = await client.post(f"/v1/inboxes/{a['id']}/messages", headers={**auth, "Idempotency-Key": "task-1"}, json=payload)
    assert replay.json()["id"] == sent.json()["id"]
    assert replay.headers["Idempotent-Replayed"] == "true"
    blocked = await client.post(f"/v1/inboxes/{a['id']}/messages", headers=auth,
                                json={**payload, "bcc": ["leak@example.com"]})
    assert blocked.status_code == (403 if profile == "autonomous" else 202)
    if profile == "review":
        assert (await client.post(f"/v1/messages/{sent.json()['id']}/approve", headers=org.headers)).json()["status"] == "queued"


async def test_external_domains_are_shared_but_not_managed(client, org, make_org):
    other = await make_org()
    for auth, address in [(org.headers, "one@gmail.com"), (other.headers, "two@gmail.com")]:
        r = await client.post("/v1/connections", headers=auth,
                              json={"provider": "gmail", "address": address, "password": "app-password"})
        assert r.status_code == 201, r.text
        assert (await client.post("/v1/inboxes", headers=auth, json={"domain": "gmail.com"})).status_code == 422
    assert (await client.post("/v1/connections", headers=org.headers,
                             json={"provider": "m365", "address": "a@example.com", "password": "x"})).status_code == 422


async def test_pending_attachments_are_private_to_uploading_key(client, org):
    inbox = (await client.post("/v1/inboxes", headers=org.headers, json={})).json()
    headers = []
    for name in ["a", "b"]:
        key = (await client.post("/v1/api-keys", headers=org.headers, json={
            "name": name, "permission_profile": "review", "inbox_ids": [inbox["id"]],
        })).json()
        headers.append({"Authorization": "Bearer " + key["api_key"]})
    r = await client.post("/v1/attachments/uploads", headers=headers[0],
                          json={"filename": "secret.txt", "content_type": "text/plain", "size_bytes": 1})
    assert r.status_code == 201, r.text
    aid = r.json().get("attachment_id", r.json().get("id"))
    assert (await client.get(f"/v1/attachments/{aid}", headers=headers[0])).status_code == 200
    assert (await client.get(f"/v1/attachments/{aid}", headers=headers[1])).status_code == 404
