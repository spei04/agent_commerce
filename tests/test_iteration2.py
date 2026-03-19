import uuid


def _login(client, email="demo@agentcommerce.local", password="demo-password", org_id=None):
    body = {"email": email, "password": password}
    if org_id:
        body["org_id"] = org_id
    r = client.post("/auth/login", json=body)
    assert r.status_code == 200, r.text
    data = r.json()
    return data["access_token"], data["org_id"], data["roles"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _mk_agent_wallet_key_product(client, token, *, external_agent_id, purpose, balance_cents, auto_approve_limit_cents, product_price_cents):
    # Agent
    r = client.post(
        "/agents",
        headers={**_auth(token), "Content-Type": "application/json"},
        json={"external_agent_id": external_agent_id, "name": external_agent_id, "metadata": {}},
    )
    assert r.status_code == 200, r.text
    agent_id = r.json()["id"]

    # Wallet
    r = client.post(
        "/wallets",
        headers={**_auth(token), "Content-Type": "application/json"},
        json={
            "agent_id": agent_id,
            "purpose": purpose,
            "name": f"{external_agent_id}:{purpose}",
            "balance_cents": balance_cents,
            "auto_approve_limit_cents": auto_approve_limit_cents,
            "spend_limit_max_cents": balance_cents,
        },
    )
    assert r.status_code == 200, r.text
    wallet_id = r.json()["id"]

    # Wallet key
    r = client.post(
        f"/wallets/{wallet_id}/keys",
        headers={**_auth(token), "Content-Type": "application/json"},
        json={"name": "k1"},
    )
    assert r.status_code == 200, r.text
    wallet_key = r.json()["wallet_key"]

    # Product
    r = client.post(
        "/products",
        headers={**_auth(token), "Content-Type": "application/json"},
        json={"vendor_name": "acme.test", "name": "Widget", "price_cents": product_price_cents, "tags": ["widget"]},
    )
    assert r.status_code == 200, r.text
    product_id = r.json()["id"]

    return wallet_id, wallet_key, product_id


def test_org_isolation(client):
    token_a, org_a, _ = _login(client)

    # Create a second org and scope a new token to it.
    r = client.post("/orgs", headers={**_auth(token_a), "Content-Type": "application/json"}, json={"name": "Org B"})
    assert r.status_code == 200, r.text
    org_b = r.json()["id"]

    token_b, _, _ = _login(client, org_id=org_b)

    wallet_id_b, wallet_key_b, product_id_b = _mk_agent_wallet_key_product(
        client,
        token_b,
        external_agent_id="agent-b",
        purpose="procurement",
        balance_cents=50_000,
        auto_approve_limit_cents=0,  # forces approval
        product_price_cents=1000,
    )

    r = client.post(
        "/purchase",
        headers={"X-Wallet-Key": wallet_key_b, "Content-Type": "application/json"},
        json={"product_id": product_id_b, "quantity": 1, "idempotency_key": str(uuid.uuid4()), "intent": "buy widget"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "pending_approval"

    approvals_b = client.get("/approvals?status=pending", headers=_auth(token_b)).json()
    assert any(a["wallet_id"] == wallet_id_b for a in approvals_b)

    approvals_a = client.get("/approvals?status=pending", headers=_auth(token_a)).json()
    assert all(a["wallet_id"] != wallet_id_b for a in approvals_a)


def test_rbac_auditor_cannot_approve(client):
    token, org_id, _ = _login(client)

    # Seed a pending approval in the demo org.
    r = client.post("/demo/reset", headers=_auth(token))
    assert r.status_code == 200, r.text
    approval_id = r.json()["pending_approval_id"]

    # Create an auditor user directly in DB (no public user admin API in MVP).
    import database
    from models import User, Membership
    from passlib.context import CryptContext
    import uuid as uuidlib

    pwd = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")
    auditor_email = "auditor@acme.test"
    auditor_pw = "auditor-password"

    db = database.SessionLocal()
    try:
        u = User(id=str(uuidlib.uuid4()), email=auditor_email, password_hash=pwd.hash(auditor_pw))
        db.add(u)
        db.add(Membership(id=str(uuidlib.uuid4()), org_id=org_id, user_id=u.id, role="auditor"))
        db.commit()
    finally:
        db.close()

    auditor_token, _, roles = _login(client, email=auditor_email, password=auditor_pw, org_id=org_id)
    assert "auditor" in roles

    r = client.post(
        f"/approvals/{approval_id}/approve",
        headers={**_auth(auditor_token), "Content-Type": "application/json"},
        json={},
    )
    assert r.status_code == 403


def test_key_revocation_blocks_agent_calls(client):
    token, _, _ = _login(client)
    r = client.post("/demo/reset", headers=_auth(token))
    assert r.status_code == 200, r.text
    data = r.json()

    wallet_id = data["wallets"]["procurement"]["id"]
    product_id = data["products"][0]["id"]

    # Revoke the only key.
    keys = client.get(f"/wallets/{wallet_id}/keys", headers=_auth(token)).json()
    assert len(keys) >= 1
    key_id = keys[0]["id"]
    r = client.post(f"/wallets/{wallet_id}/keys/{key_id}/revoke", headers=_auth(token))
    assert r.status_code == 200, r.text

    # The UI key should now be invalid.
    wallet_key = data["wallet_keys"]["procurement"]
    r = client.post(
        "/purchase",
        headers={"X-Wallet-Key": wallet_key, "Content-Type": "application/json"},
        json={"product_id": product_id, "quantity": 1, "idempotency_key": str(uuid.uuid4()), "intent": "buy"},
    )
    assert r.status_code == 401


def test_purchase_idempotency(client):
    token, _, _ = _login(client)
    wallet_id, wallet_key, product_id = _mk_agent_wallet_key_product(
        client,
        token,
        external_agent_id="idem-agent",
        purpose="procurement",
        balance_cents=10_000,
        auto_approve_limit_cents=10_000,  # auto-approve
        product_price_cents=500,
    )

    idem = "idem-" + str(uuid.uuid4())
    r1 = client.post(
        "/purchase",
        headers={"X-Wallet-Key": wallet_key, "Content-Type": "application/json"},
        json={"product_id": product_id, "quantity": 1, "idempotency_key": idem, "intent": "buy widget"},
    )
    assert r1.status_code == 200, r1.text
    txn1 = r1.json()

    r2 = client.post(
        "/purchase",
        headers={"X-Wallet-Key": wallet_key, "Content-Type": "application/json"},
        json={"product_id": product_id, "quantity": 1, "idempotency_key": idem, "intent": "buy widget"},
    )
    assert r2.status_code == 200, r2.text
    txn2 = r2.json()

    assert txn1["id"] == txn2["id"]
    # Sanity: wallet_id in returned txn should match.
    assert txn1["wallet_id"] == wallet_id


def test_reject_releases_reserved_hold(client):
    token, _, _ = _login(client)
    wallet_id, wallet_key, product_id = _mk_agent_wallet_key_product(
        client,
        token,
        external_agent_id="reject-agent",
        purpose="procurement",
        balance_cents=2_000,
        auto_approve_limit_cents=0,  # forces approval
        product_price_cents=400,
    )

    w0 = client.get(f"/wallets/{wallet_id}", headers=_auth(token)).json()
    assert w0["reserved_cents"] == 0

    r = client.post(
        "/purchase",
        headers={"X-Wallet-Key": wallet_key, "Content-Type": "application/json"},
        json={"product_id": product_id, "quantity": 1, "idempotency_key": str(uuid.uuid4()), "intent": "buy widget"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "pending_approval"
    approval_id = r.json()["approval_request_id"]

    w1 = client.get(f"/wallets/{wallet_id}", headers=_auth(token)).json()
    assert w1["reserved_cents"] == 400

    r = client.post(
        f"/approvals/{approval_id}/reject",
        headers={**_auth(token), "Content-Type": "application/json"},
        json={"note": "no"},
    )
    assert r.status_code == 200, r.text

    w2 = client.get(f"/wallets/{wallet_id}", headers=_auth(token)).json()
    assert w2["reserved_cents"] == 0
    assert w2["balance_cents"] == 2_000
