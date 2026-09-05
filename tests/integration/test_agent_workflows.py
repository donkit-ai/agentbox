from datetime import timedelta

from agentbox.db.models import EventReceipt, Message, utcnow
from agentbox.domain.ids import new_id


async def test_reply_wait_survives_requests_and_matches_reference(client, org, runtime):
    inbox = (await client.post("/v1/inboxes", headers=org.headers, json={})).json()
    sent = (await client.post(f"/v1/inboxes/{inbox['id']}/messages", headers=org.headers,
                              json={"to": ["supplier@example.com"], "text": "quote"})).json()
    url = f"/v1/messages/{sent['id']}/reply-wait"
    waiting = await client.post(url, headers=org.headers, json={"expires_in": 3600})
    assert waiting.status_code == 200, waiting.text
    assert waiting.json()["status"] == "pending"
    assert (await client.post(url, headers=org.headers, json={"expires_in": 6000})).json() == waiting.json()
    async with runtime.db.session() as s:
        original = await s.get(Message, sent["id"])
        unrelated = Message(id=new_id("msg"), organization_id=org.id, inbox_id=inbox["id"], thread_id=sent["thread_id"],
                            direction="inbound", status="received", internet_message_id="<unrelated@example.com>",
                            from_address={"email": "supplier@example.com"}, to_addresses=[], references=[])
        s.add(unrelated)
        await s.commit()
        assert (await client.get(url, headers=org.headers)).json()["status"] == "pending"
        reply = Message(id=new_id("msg"), organization_id=org.id, inbox_id=inbox["id"], thread_id=sent["thread_id"],
                        direction="inbound", status="received", internet_message_id="<reply@example.com>",
                        in_reply_to=original.internet_message_id, from_address={"email": "supplier@example.com"},
                        to_addresses=[], references=[])
        s.add(reply)
        await s.commit()
    found = (await client.get(url, headers=org.headers)).json()
    assert found["status"] == "matched" and found["reply_message_id"] == reply.id
    assert (await client.get(url, headers=org.headers)).json() == found


async def test_event_leases_acknowledgement_and_redelivery(client, org, runtime):
    await client.post("/v1/inboxes", headers=org.headers, json={})
    path = "/v1/consumers/support/claim"
    payload = {"types": ["inbox.created"], "limit": 1}
    claimed = await client.post(path, headers=org.headers, json=payload)
    assert claimed.status_code == 200, claimed.text
    first = claimed.json()["data"][0]
    assert (await client.post(path, headers=org.headers, json=payload)).json()["data"] == []
    eid = first["event"]["id"]
    ack = f"/v1/consumers/support/events/{eid}/ack"
    assert (await client.post(ack, headers=org.headers, json={"lease_token": "wrong"})).status_code == 409
    async with runtime.db.session() as s:
        row = await s.get(EventReceipt, (org.id, "support", eid))
        row.leased_until = utcnow() - timedelta(seconds=1)
        await s.commit()
    second = (await client.post(path, headers=org.headers, json=payload)).json()["data"][0]
    assert second["event"]["id"] == eid and second["lease_token"] != first["lease_token"]
    assert (await client.post(ack, headers=org.headers, json={"lease_token": first["lease_token"]})).status_code == 409
    for _ in range(2):
        assert (await client.post(ack, headers=org.headers, json={"lease_token": second["lease_token"]})).status_code == 200
    assert (await client.post(path, headers=org.headers, json=payload)).json()["data"] == []
