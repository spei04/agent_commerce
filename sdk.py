"""
Agent Commerce SDK
─────────────────
Minimal client for AI agents to discover and purchase from the platform.

Usage:
    from sdk import AgentClient, SpendBlocked

    client = AgentClient(wallet_id="...", wallet_key="...", base_url="http://localhost:8000")

    # Find matches for an intent
    matches = client.find("cheapest cloud storage for datasets", budget=5.00)

    # Buy the top match
    txn = client.buy(matches[0]["product_id"], quantity=10, intent="store embeddings")

    # Or do it in one call
    txn = client.find_and_buy("recycled packaging supplies", quantity=50, budget=30)
"""

import time
import uuid
import requests
from typing import Optional


class SpendBlocked(Exception):
    def __init__(self, reason: str, transaction_id: str):
        self.reason = reason
        self.transaction_id = transaction_id
        super().__init__(f"blocked: {reason}")


class AgentClient:
    def __init__(self, wallet_id: str, wallet_key: str, base_url: str = "http://localhost:8000"):
        self.wallet_id = wallet_id
        self.wallet_key = wallet_key
        self.base_url = base_url.rstrip("/")

    def _headers(self) -> dict:
        return {"X-Wallet-Key": self.wallet_key}

    # ── Discovery ────────────────────────────────────────────────────────────

    def find(
        self,
        intent: str,
        budget: Optional[float] = None,
        constraints: Optional[dict] = None,
        limit: int = 5,
    ) -> list:
        """Resolve a natural-language intent to a ranked list of products."""
        constraints = constraints or {}
        resp = requests.post(f"{self.base_url}/resolve", json={
            "wallet_id": self.wallet_id,
            "intent": intent,
            "budget": budget,
            "constraints": constraints,
        }, headers=self._headers())
        resp.raise_for_status()
        return resp.json()["matches"]

    # ── Purchase ─────────────────────────────────────────────────────────────

    def buy(
        self,
        product_id: str,
        quantity: int = 1,
        intent: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> dict:
        """
        Purchase a product directly by ID.
        Raises SpendBlocked if the policy rejects the transaction.
        """
        if idempotency_key is None:
            # Caller should reuse the same key on retries.
            idempotency_key = str(uuid.uuid4())
        resp = requests.post(f"{self.base_url}/purchase", json={
            "wallet_id": self.wallet_id,
            "product_id": product_id,
            "quantity": quantity,
            "intent": intent,
            "idempotency_key": idempotency_key,
        }, headers=self._headers())
        resp.raise_for_status()
        txn = resp.json()
        if txn["status"] == "blocked":
            raise SpendBlocked(txn["reason"], txn["id"])
        return txn

    def find_and_buy(
        self,
        intent: str,
        quantity: int = 1,
        budget: Optional[float] = None,
        constraints: Optional[dict] = None,
    ) -> dict:
        """
        Resolve an intent and immediately purchase the best match.
        Raises ValueError if no products match.
        """
        matches = self.find(intent, budget=budget, constraints=constraints or {})
        if not matches:
            raise ValueError(f"no products matched intent: {intent!r}")
        return self.buy(matches[0]["product_id"], quantity=quantity, intent=intent)

    # ── Account ──────────────────────────────────────────────────────────────

    def balance(self) -> float:
        resp = requests.get(f"{self.base_url}/wallets/{self.wallet_id}", headers=self._headers())
        resp.raise_for_status()
        return resp.json()["balance"]

    def history(self, limit: int = 20) -> list:
        resp = requests.get(f"{self.base_url}/transactions", params={
            "wallet_id": self.wallet_id,
            "limit": limit,
        }, headers=self._headers())
        resp.raise_for_status()
        return resp.json()
