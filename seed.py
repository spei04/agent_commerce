"""
Seed demo data into a running server.

    python seed.py

Server must be running first:
    uvicorn main:app --reload
"""

import time
import requests
import os

BASE = "http://localhost:8000"
ADMIN_KEY = os.environ.get("AGENT_COMMERCE_ADMIN_KEY", "demo-admin")

AGENTS = [
    {"agent_id": "research-agent-01", "name": "Research Agent",   "balance": 500,  "auto_approve_limit": 30, "spend_limit_max": 200},
    {"agent_id": "procure-bot",       "name": "Procurement Bot",  "balance": 1000, "auto_approve_limit": 80, "spend_limit_max": 400},
    {"agent_id": "data-fetcher",      "name": "Data Fetcher",     "balance": 150,  "auto_approve_limit": 12, "spend_limit_max": 50},
]

PRODUCTS = [
    # AI / Inference
    {"vendor_name": "openai.com",    "name": "GPT-4 Inference",           "description": "GPT-4 API call, per 1k tokens",        "price": 0.02,   "tags": ["ai", "llm", "inference", "gpt", "language"],   "lead_time_days": 0},
    {"vendor_name": "openai.com",    "name": "Embeddings API",            "description": "Text embedding generation",             "price": 0.0001, "tags": ["ai", "embeddings", "search", "vectors"],        "lead_time_days": 0},
    {"vendor_name": "anthropic.com", "name": "Claude API Call",           "description": "Claude Sonnet inference",               "price": 0.015,  "tags": ["ai", "llm", "inference", "claude", "language"], "lead_time_days": 0},

    # Cloud / Compute
    {"vendor_name": "aws.com",       "name": "S3 Storage (1GB)",          "description": "Amazon S3 object storage, per GB",      "price": 0.023,  "tags": ["storage", "cloud", "aws", "data", "files"],     "lead_time_days": 0},
    {"vendor_name": "aws.com",       "name": "EC2 t3.medium (hourly)",    "description": "Cloud compute instance",                "price": 0.0416, "tags": ["compute", "cloud", "server", "aws"],            "lead_time_days": 0},

    # Data
    {"vendor_name": "serpapi.com",   "name": "Search API Query",          "description": "Google search results via API",         "price": 0.005,  "tags": ["search", "web", "data", "research", "query"],   "lead_time_days": 0},
    {"vendor_name": "clearbit.com",  "name": "Company Enrichment",        "description": "B2B company data lookup",               "price": 0.10,   "tags": ["data", "enrichment", "b2b", "research", "crm"], "lead_time_days": 0},
    {"vendor_name": "bloomberg.com", "name": "Market Data Feed",          "description": "Real-time financial market data",       "price": 450.00, "tags": ["finance", "market", "data", "trading"],         "lead_time_days": 1},

    # Payments
    {"vendor_name": "stripe.com",    "name": "Payment Processing",        "description": "Credit card processing, per txn",       "price": 0.30,   "tags": ["payments", "checkout", "billing"],               "lead_time_days": 0},

    # Physical / Packaging
    {"vendor_name": "packright.com", "name": "Recycled Kraft Boxes",      "description": "50-pack eco corrugated boxes",          "price": 24.99,  "tags": ["packaging", "boxes", "shipping", "eco"],        "lead_time_days": 3},
    {"vendor_name": "packright.com", "name": "Bubble Mailers 100-pack",   "description": "Padded poly mailers",                   "price": 18.50,  "tags": ["packaging", "mailers", "shipping", "envelopes"], "lead_time_days": 2},
]

SCENARIOS = [
    # (agent_id, intent, vendor, quantity)
    ("research-agent-01", "summarize documents using language model inference",        "openai.com",    50),
    ("research-agent-01", "search the web for competitor pricing data",                "serpapi.com",   20),
    ("research-agent-01", "enrich company contacts from CRM for outreach",             "clearbit.com",  10),
    ("research-agent-01", "generate embeddings for semantic search index",             "openai.com",   200),
    ("procure-bot",        "order eco-friendly shipping boxes for 50 orders",          "packright.com",   2),
    ("procure-bot",        "process payment transactions for subscription renewals",   "stripe.com",     15),
    ("procure-bot",        "store monthly dataset export files in cloud storage",      "aws.com",        40),
    ("procure-bot",        "order padded mailers for small product shipments",         "packright.com",   3),
    ("data-fetcher",       "run inference to classify incoming support tickets",       "anthropic.com",  10),
    ("data-fetcher",       "pull live market data for portfolio rebalancing",          "bloomberg.com",   1),  # blocked: exceeds limit
    ("data-fetcher",       "query web search for latest documentation updates",       "serpapi.com",     8),
    ("data-fetcher",       "store raw crawl results in object storage",               "aws.com",         5),
]


def post(path, body):
    r = requests.post(
        f"{BASE}{path}",
        json=body,
        headers={"X-Admin-Key": ADMIN_KEY} if path in ("/wallets", "/products") else None,
    )
    r.raise_for_status()
    return r.json()


def get(path):
    r = requests.get(f"{BASE}{path}", headers={"X-Admin-Key": ADMIN_KEY})
    r.raise_for_status()
    return r.json()


def main():
    print("── Seeding Agent Commerce ──────────────────────\n")

    # Wallets
    wallets = {}
    wallet_keys = {}
    for a in AGENTS:
        try:
            w = post("/wallets", {**a, "allowed_vendors": []})
            wallets[a["agent_id"]] = w["id"]
            wallet_keys[a["agent_id"]] = w["wallet_key"]
            print(f"  wallet  {a['agent_id']:<30} balance=${a['balance']}")
        except requests.HTTPError as e:
            if e.response.status_code == 400:
                # Already exists — look up by listing
                all_w = get("/wallets")
                for w in all_w:
                    if w["agent_id"] == a["agent_id"]:
                        wallets[a["agent_id"]] = w["id"]
                        # Rotate key so we can seed purchases idempotently.
                        rk = requests.post(f"{BASE}/wallets/{w['id']}/rotate_key", headers={"X-Admin-Key": ADMIN_KEY})
                        rk.raise_for_status()
                        wallet_keys[a["agent_id"]] = rk.json()["wallet_key"]
                        print(f"  wallet  {a['agent_id']:<30} (existing)")
            else:
                raise

    # Products
    products = {}
    for p in PRODUCTS:
        prod = post("/products", p)
        products[p["vendor_name"]] = products.get(p["vendor_name"], [])
        products[p["vendor_name"]].append(prod)
    total_prods = sum(len(v) for v in products.values())
    print(f"\n  registered {total_prods} products across {len(products)} vendors\n")

    # Transactions
    print("── Simulating transactions ──────────────────────\n")
    for agent_id, intent, vendor, qty in SCENARIOS:
        wallet_id = wallets.get(agent_id)
        if not wallet_id:
            continue
        vendor_products = products.get(vendor, [])
        if not vendor_products:
            continue

        product = vendor_products[0]
        try:
            r = requests.post(f"{BASE}/purchase", json={
                "wallet_id": wallet_id,
                "product_id": product["id"],
                "quantity": qty,
                "intent": intent,
                "idempotency_key": f"seed-{agent_id}-{vendor}-{qty}-{hash(intent)}",
            }, headers={"X-Wallet-Key": wallet_keys.get(agent_id, "")})
            r.raise_for_status()
            txn = r.json()
            if "status" not in txn:
                raise Exception(str(txn))
            status = "✓" if txn["status"] == "approved" else "✗"
            note = f"  ({txn['reason']})" if txn["status"] == "blocked" else ""
            print(f"  {status}  {agent_id:<25} → {vendor:<20} {money(txn['amount'])}{note}")
        except Exception as e:
            print(f"  !  {agent_id} → {vendor}: {e}")

        time.sleep(0.05)

    print(f"\n── Done. Visit http://localhost:8000 ────────────\n")


def money(n):
    v = float(n)
    if v < 0.01 and v > 0:
        return f"${v:.6f}"
    return f"${v:,.2f}"


if __name__ == "__main__":
    main()
