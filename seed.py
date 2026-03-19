"""
Seed demo data into a running server (Iteration 2).

    python seed.py

Server must be running first:
    uvicorn main:app --reload
"""

import os
import time
import uuid
import requests

BASE = os.environ.get("BASE_URL", "http://localhost:8000")
EMAIL = os.environ.get("DEMO_EMAIL", "demo@agentcommerce.local")
PASSWORD = os.environ.get("DEMO_PASSWORD", "demo-password")


def auth_headers() -> dict:
    r = requests.post(f"{BASE}/auth/login", json={"email": EMAIL, "password": PASSWORD})
    r.raise_for_status()
    token = r.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def main() -> None:
    print("── Seeding Agent Commerce (Iteration 2) ─────────\n")
    h = auth_headers()

    r = requests.post(f"{BASE}/demo/reset", headers=h)
    r.raise_for_status()
    demo = r.json()

    wallet_keys = demo["wallet_keys"]
    products = demo["products"]
    prod_box = next(p for p in products if p["vendor_name"] == "packright.com")

    print("  created demo org:", demo["org_id"])
    print("  procurement wallet:", demo["wallets"]["procurement"]["id"])
    print("  research wallet:", demo["wallets"]["research"]["id"])
    print()

    # Agent purchase (auto-approve) from procurement wallet
    idem = str(uuid.uuid4())
    r = requests.post(
        f"{BASE}/purchase",
        headers={"X-Wallet-Key": wallet_keys["procurement"]},
        json={"product_id": prod_box["id"], "quantity": 2, "intent": "order packaging supplies", "idempotency_key": idem},
    )
    r.raise_for_status()
    print("  purchase:", r.json()["status"], r.json()["amount"])

    # Approve the seeded pending approval
    pending = requests.get(f"{BASE}/approvals?status=pending", headers=h).json()
    if pending:
        aid = pending[0]["id"]
        rr = requests.post(f"{BASE}/approvals/{aid}/approve", headers=h, json={})
        rr.raise_for_status()
        print("  approved:", aid)

    # Tick settlement a few times
    for _ in range(5):
        requests.post(f"{BASE}/simulate/tick", headers=h)
        time.sleep(0.3)

    print(f"\n── Done. Visit {BASE} ───────────────────────────\n")


if __name__ == "__main__":
    main()

