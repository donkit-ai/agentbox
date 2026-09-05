import asyncio


async def test_replay_same_key_same_body(client, org):
    h = {**org.headers, "Idempotency-Key": "k1"}
    r1 = await client.post("/v1/inboxes", headers=h, json={"username": "idem"})
    r2 = await client.post("/v1/inboxes", headers=h, json={"username": "idem"})
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json() == r2.json()
    assert r2.headers.get("Idempotent-Replayed") == "true"


async def test_conflict_on_different_body(client, org):
    h = {**org.headers, "Idempotency-Key": "k2"}
    assert (await client.post("/v1/inboxes", headers=h, json={"username": "one"})).status_code == 201
    r = await client.post("/v1/inboxes", headers=h, json={"username": "two"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "idempotency_conflict"


async def test_failed_request_does_not_poison_key(client, org):
    h = {**org.headers, "Idempotency-Key": "k3"}
    r = await client.post("/v1/inboxes", headers=h, json={"username": "postmaster"})
    assert r.status_code == 422
    r = await client.post("/v1/inboxes", headers=h, json={"username": "postmaster"})
    assert r.status_code == 422  # re-executed, not replayed
    assert r.headers.get("Idempotent-Replayed") is None


async def test_concurrent_duplicates_serialize(client, org):
    h = {**org.headers, "Idempotency-Key": "k4"}
    calls = [client.post("/v1/inboxes", headers=h, json={"username": "race"}) for _ in range(5)]
    results = await asyncio.gather(*calls)
    codes = sorted(r.status_code for r in results)
    assert codes == [201] * 5
    assert len({r.json()["id"] for r in results}) == 1


async def test_abandoned_reservation_recovers_after_process_exit(client, org, runtime):
    from agentbox.api.idempotency import _begin, request_hash

    # Simulate an abruptly terminated process: it reserved a key but committed neither resource nor receipt.
    await _begin(runtime, org.id, "crashed", "POST /v1/inboxes", request_hash(b'{"username":"recover"}'))
    headers = {**org.headers, "Idempotency-Key": "crashed"}
    r = await client.post("/v1/inboxes", headers=headers, json={"username": "recover"})
    assert r.status_code == 201, r.text
    assert (await client.post("/v1/inboxes", headers=headers, json={"username": "recover"})).json() == r.json()
