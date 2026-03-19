# Agent Commerce (Iteration 2)

Agent payment infrastructure demo with “real” semantics (but simulated rails):
- Multi-tenant org model
- Human auth: JWT + RBAC (`admin|approver|auditor`)
- Agent auth: wallet-scoped API keys (`X-Wallet-Key: wk_<key_id>.<secret>`) with rotation/revocation (stored hashed)
- Guaranteed approvals: approval requests reserve funds immediately (`available = balance - reserved`)
- Payment rails abstraction (simulated): card-like `authorize → capture` + ACH-style payouts
- Webhooks: outbox delivery (at-least-once) + replay
- Demo UI: policy trace + approval queue + simulated settlement

---

## High-Level Design

This repo is a small-scale MVP slice of a future large-scale agent payments control plane:

- **Tenancy boundary**: everything is scoped to an `org_id`.
- **Identity boundary**:
  - Humans authenticate with JWTs (`Authorization: Bearer <jwt>`).
  - Agents authenticate with **wallet-scoped API keys** (`X-Wallet-Key`), where each key maps to exactly one wallet (and therefore one org).
- **Money correctness**: all monetary amounts are stored in **integer minor units** (`*_cents`), with a strict invariant:
  - `available_cents = balance_cents - reserved_cents`
- **Guaranteed approvals**: any approval request **creates a reservation immediately** so approval later cannot be “stolen” by concurrent spend.
- **Rail semantics**:
  - Card-like: `authorize → capture` (async simulated)
  - ACH-like: `submitted → paid|failed` payouts (async simulated)
- **Webhooks**: outbox pattern (at-least-once), wallet-scoped signing secret, and replay.

## System Architecture (MVP)

Components:
- **API server** (`main.py`): FastAPI app implementing auth, policy, approvals, rails simulation, outbox, and UI endpoints.
- **DB**: Postgres in dev/prod path (`docker-compose.yml`), Alembic migrations in `alembic/`.
  - Tests run on SQLite for speed (see `tests/`), but the “real” workflow is Postgres + Alembic.
- **Worker** (`worker.py`): background loop that settles simulated payments and drains the webhook outbox.
  - The demo UI calls `POST /simulate/tick` periodically; in production this would be a real worker/queue.
- **UI**: `dashboard.html`, `demo.html`, `docs.html` served as static pages by FastAPI.

## Data Model (Iteration 2)

Core entities:
- `Organization`
- `User`
- `Membership` (`org_id`, `user_id`, `role`)
- `Agent` (`org_id`, `external_agent_id`, `name`, `metadata`)
  - Unique: `(org_id, external_agent_id)`
- `Wallet` (`org_id`, `agent_id`, `purpose`, balances, policy config, webhook config)
  - Multiple wallets per agent (distinguished by `purpose`)
  - Unique: `(org_id, agent_id, purpose)`
- `ApiKey` (multiple keys per wallet; secret stored hashed; revoke/rotate)

Money / execution:
- `ApprovalRequest` (pending → approved|rejected)
- `BalanceHold` (reservation records; `kind=approval|auth`)
- `Transaction` (audit trail for approved/blocked purchases)
- `PaymentAuthorization` / `PaymentCapture` (card-like semantics)
- `AchPayout` (ACH-style payouts)
- `WebhookEvent` (outbox)

## Run (Postgres + Alembic)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

docker compose up -d
alembic upgrade head

uvicorn main:app --reload
```

Pages:
- Dashboard: `http://localhost:8000/`
- Demo walkthrough: `http://localhost:8000/demo`
- Human docs: `http://localhost:8000/docs-ref`
- OpenAPI (FastAPI): `http://localhost:8000/docs`

Demo login:
- email: `demo@agentcommerce.local`
- password: `demo-password`

## Restart The Website

If the server is running, stop it (`Ctrl+C`) and run:

```bash
uvicorn main:app --reload
```

## Core Auth

Human/admin calls:
- `Authorization: Bearer <jwt>`
- Get a JWT via `POST /auth/login`

Agent calls:
- `X-Wallet-Key: wk_<key_id>.<secret>`
- Wallet keys are returned only once at creation (`POST /wallets/{id}/keys`).

---

## Agent Payment Pipeline (Card-Like Semantics)

This is the primary “agent buys now” flow.

1. **Agent calls `POST /purchase`** with `product_id`, `quantity`, `intent`, and **`idempotency_key`**
   - Wallet is inferred from `X-Wallet-Key`.
2. **Policy evaluation** runs (status, allowlist, velocity, balance, windows, ceilings, approval threshold).
3. One of:
   - **Blocked**: create `Transaction(status=blocked)` and return immediately.
   - **Requires approval**:
     - Atomically **reserve funds** (`reserved_cents += amount`)
     - Create `ApprovalRequest(status=pending)` and `BalanceHold(kind=approval)`
     - Emit webhook event `approval_required` (if wallet has a webhook configured)
     - Return `{status: pending_approval, approval_request_id: ...}`
   - **Auto-approved**:
     - Atomically **reserve funds** (`reserved_cents += amount`)
     - Create `Transaction(status=approved, payment_status=processing)`
     - Create `BalanceHold(kind=auth)` and `PaymentAuthorization(status=authorized)`
     - Emit webhook event `payment.authorization.created`
4. **Async settlement** (worker or `POST /simulate/tick`):
   - Capture authorization:
     - Atomically move reserved → spent:
       - `reserved_cents -= amount`
       - `balance_cents -= amount`
     - Create `PaymentCapture(status=captured)`
     - Mark `Transaction.payment_status = succeeded`
     - Emit webhook `payment.captured`

## Approvals Pipeline (Guaranteed)

Approvals are guaranteed because the reservation is created when the approval request is created:
- At approval creation time: `reserved_cents` increases and reduces available funds immediately.
- At approval time: the approver is approving **exactly what was requested** (amount/vendor/product snapshot), and the system converts the reserved funds into an executed payment flow.

Approve:
- `POST /approvals/{id}/approve` (role: `approver`)
- Creates a `Transaction(status=approved, payment_status=processing)` linked to the approval’s hold.
- Executes rail:
  - `rail=card`: creates `PaymentAuthorization` and later capture settles it.
  - `rail=ach`: creates `AchPayout(submitted)` which later settles to `paid|failed`.

Reject:
- `POST /approvals/{id}/reject`
- Releases the reservation and logs a `Transaction(status=blocked)` for audit.

## ACH Payout Pipeline

This models “invoice approval → payout” semantics:
1. Approval is created (reservation is held, same as above).
2. Approver approves with `{"rail":"ach"}` which creates an `AchPayout(status=submitted)`.
3. Worker/tick transitions `AchPayout` to `paid|failed` and debits the wallet at settlement time.
4. Webhooks: `payout.submitted`, then `payout.paid` or `payout.failed`.

## Webhook Delivery Pipeline (Outbox + Replay)

Webhooks are delivered at-least-once:
- Events are written to `webhook_events` (outbox) in the same DB as the transaction state.
- A worker (or tick) sends pending events and retries with exponential backoff.
- Receivers must **dedupe by `event.id`**.

Signing:
- Each wallet has a **webhook signing secret** (`wallet.webhook_signing_secret`).
- Header: `X-Agent-Commerce-Signature: v1=<hex(hmac_sha256(secret, raw_body))>`

Replay:
- `POST /webhooks/replay` re-queues a specific `event_id` for redelivery.

---

## Typical Flow (Copy/Paste)

1) Human/admin logs in:

```bash
curl -s -X POST http://localhost:8000/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"demo@agentcommerce.local","password":"demo-password"}'
```

2) Create an agent, wallet (with purpose), and wallet key:
- `POST /agents`
- `POST /wallets`
- `POST /wallets/{wallet_id}/keys`

3) Register products:
- `POST /products`

4) Agent discovers and purchases:
- `POST /resolve`
- `POST /purchase` (always send `idempotency_key`)

5) If `purchase` returns `pending_approval`, an approver completes it:
- `GET /approvals?status=pending`
- `POST /approvals/{id}/approve` (choose `rail: card|ach`)

6) Settlement + webhooks:
- For the demo UI, the page calls `POST /simulate/tick`
- Or run the worker loop:

```bash
python worker.py
```

## Webhooks

Webhooks are wallet-scoped and delivered at-least-once (duplicates possible). You must dedupe by `event.id`.

Configure a wallet webhook URL + rotate its signing secret:

```bash
curl -s -X POST http://localhost:8000/wallets/<wallet_id>/webhook \
  -H "Authorization: Bearer <jwt>" -H "Content-Type: application/json" \
  -d '{"webhook_url":"https://example.com/webhook","rotate_secret":true}'
```

Verify signature:
- Header: `X-Agent-Commerce-Signature: v1=<hex>`
- `expected = "v1=" + hex(hmac_sha256(webhook_signing_secret, raw_body))`

Replay an event id:
- `POST /webhooks/replay`

## Environment

- `DATABASE_URL` (default: `postgresql+psycopg2://agent:agent@localhost:5432/agent_commerce`)
- `JWT_SECRET` (default dev value; change for anything non-local)
- `API_KEY_PEPPER` (pepper used in hashing wallet API key secrets; change for anything non-local)
- `DEMO_MODE=1` seeds demo org/user on startup

## Tests

```bash
pytest
```
