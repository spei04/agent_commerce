# Plan
Stage 1 (now): Sell to developers as infrastructure
Target: engineers at companies already deploying AI agents — logistics, procurement, ops automation, fintech. The pitch is simple: "wrap your agent's spending in three lines of code, get policy controls and an audit trail." No platform commitment required. This gets you live customers, transaction data, and a wedge into the companies that matter.

Stage 2: Expand upward into the org
Once your SDK is in their stack, sell the dashboard to the finance and ops teams above the engineers. Now you're not just a developer tool — you're where the CFO reviews what agents spent this week. This is where SaaS revenue compounds. The dashboard you built is already this.

Stage 3: Become the agent operating system
With enough wallets running through your platform, you see demand signals no one else sees: which vendors agents hit most, which intents go unmatched, which approvals get rejected repeatedly. You use that to build the orchestration layer — policy templates, agent identity, multi-agent workflows, vendor integrations. At this point you're not infrastructure anymore; you're the control plane for enterprise AI.


# Agent Commerce

Spending governance infrastructure for autonomous AI agents. Agents submit natural-language intents, get back ranked product matches, and transact directly — with programmable spending policies, rolling budget windows, velocity controls, and a human approval queue enforced at every purchase.

---

## Installation

**Requires Python 3.10+**

```bash
# 1. Clone / navigate to the project
cd agent_commerce

# 2. Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate        # macOS / Linux
# .venv\Scripts\activate         # Windows

# 3. Install dependencies
pip install -r requirements.txt
```

---

## Start

```bash
source .venv/bin/activate
uvicorn main:app --reload
```

The API is running at `http://localhost:8000`.

| Page | URL |
|------|-----|
| Dashboard (audit & monitoring) | `http://localhost:8000` |
| Interactive demo | `http://localhost:8000/demo` |
| API reference | `http://localhost:8000/docs-ref` |
| FastAPI auto-docs | `http://localhost:8000/docs` |

---

## Load demo data

In a second terminal (venv active, server running):

```bash
python seed.py
```

Registers 3 agents, 11 products across 7 vendors, and runs a batch of simulated transactions — including approvals, blocks, and a pending approval queue item.

---

## Interactive demo

`http://localhost:8000/demo` runs a live six-scenario walkthrough against the real API:

| Scenario | Agent | What it shows |
|----------|-------|---------------|
| Auto-Approve | procurement | $8 purchase clears instantly — under auto-approve threshold |
| Approval Queue | research | $45 purchase routed to human review queue |
| Hard Block | procurement | $320 purchase rejected — over hard ceiling |
| Vendor Block | research | Purchase from unauthorized vendor rejected at allowlist check |
| Daily Limit | procurement | Third purchase fails because budget window is exhausted |
| Velocity Breach | procurement | Rapid-fire 4th purchase auto-suspends the wallet |

Each scenario shows the animated step-by-step policy evaluation trace.

---

## SDK usage

```python
from sdk import AgentClient, SpendBlocked

client = AgentClient(wallet_id="<wallet-id>", wallet_key="<wallet-key>", base_url="http://localhost:8000")

# Find products matching a natural-language intent
matches = client.find("cheapest cloud storage for dataset exports", budget=1.00)

# Buy the top match
try:
    txn = client.buy(matches[0]["product_id"], quantity=10, intent="store embeddings")
    print(txn["status"], txn["amount"])
    print(txn["policy_trace"])   # full audit trace of every policy check
except SpendBlocked as e:
    print("blocked:", e.reason)

# Resolve and buy in one call
txn = client.find_and_buy("eco-friendly packaging for 50 orders", quantity=2)

# Check balance
print(client.balance())

# Transaction history
for t in client.history():
    print(t["status"], t["vendor_name"], t["amount"])
```

---

## Auth (MVP)

- Admin endpoints require `X-Admin-Key` (defaults to `demo-admin`).
- Agent endpoints require `X-Wallet-Key` (returned once on wallet creation).
- Set `AGENT_COMMERCE_ADMIN_KEY` to change the admin key.

## Payments (Simulated)

Purchases create a simulated `PaymentIntent` and settle asynchronously (like a real processor).

- Auto-approved purchases reserve funds immediately (`reserved_balance`) and settle to `payment_status: succeeded|failed`.
- Approval-required purchases reserve funds at request-time so an approval is guaranteed to have funds later.
- The demo UI advances settlement by calling `POST /simulate/tick` periodically (admin-only).

## Spending policy

Every `POST /purchase` runs through an ordered evaluation chain. The first failing check determines the outcome.

| Check | Behaviour |
|-------|-----------|
| `wallet.status` | Blocked immediately if wallet is suspended |
| `allowed_vendors` | Blocked if vendor not in allowlist (empty = all allowed) |
| `velocity_max_txn` | Blocked if approved transactions in the last 60s ≥ limit; wallet auto-suspended |
| `balance` | Blocked if wallet balance < transaction amount |
| `daily_limit` | Blocked if today's approved spend + amount would exceed the daily window |
| `weekly_limit` | Blocked if this week's approved spend + amount would exceed the weekly window |
| `spend_limit_max` | Blocked if amount > hard ceiling |
| `auto_approve_limit` | Routed to human approval queue if amount > threshold |
| — | Auto-approved if all checks pass |

Every transaction (approved, blocked, or pending) stores the full policy trace for audit.

### Approval queue

Purchases that fall between `auto_approve_limit` and `spend_limit_max` create an `ApprovalRequest` — no funds are moved until a human approves via the dashboard or API. On rejection, a blocked transaction is logged for the audit trail.

---

## Wallet configuration

```python
POST /wallets
# headers: { "X-Admin-Key": "demo-admin" }
{
  "agent_id": "procurement-agent",
  "name": "Procurement Agent",
  "balance": 500.0,
  "auto_approve_limit": 50.0,    # below → instant approval
  "spend_limit_max": 300.0,      # above → hard block; middle → approval queue
  "daily_limit": 200.0,          # rolling 24h spend cap (0 = no limit)
  "weekly_limit": 0.0,           # rolling 7-day spend cap (0 = no limit)
  "velocity_max_txn": 5,         # max approved txn per 60s (0 = no limit)
  "allowed_vendors": [],         # vendor allowlist (empty = all allowed)
  "webhook_url": "https://..."   # POSTed when an approval is needed
}
```

---

## API

### Wallets
| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/wallets` | Create an agent wallet |
| `GET`  | `/wallets` | List all wallets |
| `GET`  | `/wallets/{id}` | Get wallet details and balance |
| `PATCH`| `/wallets/{id}` | Update limits, vendors, or status |
| `POST` | `/wallets/{id}/topup` | Add funds |
| `POST` | `/wallets/{id}/rotate_key` | Rotate wallet key (returns new key) |
| `POST` | `/wallets/{id}/unsuspend` | Restore a suspended wallet |

### Products
| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/products` | Register a product |
| `GET`  | `/products` | List all products |

### Transactions
| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/resolve` | Resolve intent → ranked product matches |
| `POST` | `/purchase` | Buy a product (runs full policy evaluation) |
| `GET`  | `/transactions` | Transaction log (filter by wallet, status) |
| `GET`  | `/intents` | Intent resolution log |

### Approvals
| Method | Path | Description |
|--------|------|-------------|
| `GET`  | `/approvals` | List approval requests (filter by status) |
| `POST` | `/approvals/{id}/approve` | Approve — executes transaction, decrements balance |
| `POST` | `/approvals/{id}/reject` | Reject — logs blocked transaction for audit |

### Catalog & Analytics
| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/catalog/sync` | Bulk upsert products from a PIM/ERP |
| `GET`  | `/catalog/syncs` | List sync job history |
| `GET`  | `/analytics/sku` | Per-SKU AI visibility scores |
| `GET`  | `/analytics/intents` | Keyword trends and zero-match demand gaps |
| `GET`  | `/analytics/optimize/{id}` | Tag gap analysis for a product |
| `POST` | `/analytics/optimize/{id}/apply` | Auto-apply top suggested tags |

### Dashboard & Demo
| Method | Path | Description |
|--------|------|-------------|
| `GET`  | `/dashboard/summary` | Aggregated metrics |
| `GET`  | `/` | Audit dashboard |
| `GET`  | `/demo` | Interactive policy demo |
| `GET`  | `/docs-ref` | API reference |
| `POST` | `/demo/reset` | Reset demo wallets and products to initial state |

---

## Project structure

```
agent_commerce/
├── main.py          # FastAPI app and all routes
├── models.py        # SQLAlchemy ORM models
├── database.py      # SQLite engine and session
├── policy.py        # Policy engine (evaluate_full + trace)
├── resolver.py      # Intent → product matching + SKU instrumentation
├── sdk.py           # Agent-facing Python client
├── dashboard.html   # Audit and monitoring dashboard
├── demo.html        # Interactive six-scenario policy demo
├── docs.html        # API reference documentation
├── seed.py          # Demo data loader
└── requirements.txt
```
