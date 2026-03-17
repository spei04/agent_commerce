import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import List

from sqlalchemy import func


class Decision(str, Enum):
    AUTO_APPROVE      = "auto_approve"
    REQUIRES_APPROVAL = "requires_approval"
    BLOCKED           = "blocked"


@dataclass
class PolicyResult:
    decision: Decision
    reason: str
    trace: List[dict] = field(default_factory=list)

    @property
    def approved(self) -> bool:
        return self.decision == Decision.AUTO_APPROVE


def evaluate(amount: float, vendor_name: str, wallet) -> PolicyResult:
    """
    Three-tier policy engine (backward-compatible):

      amount < wallet.auto_approve_limit              → AUTO_APPROVE
      wallet.auto_approve_limit ≤ amount ≤ spend_limit_max → REQUIRES_APPROVAL
      amount > wallet.spend_limit_max                 → BLOCKED

    Vendor allowlist and balance are checked first regardless of tier.
    """
    allowed = json.loads(wallet.allowed_vendors) if isinstance(wallet.allowed_vendors, str) else wallet.allowed_vendors

    # Vendor allowlist (empty = allow all)
    if allowed and vendor_name not in allowed:
        return PolicyResult(Decision.BLOCKED, f"vendor '{vendor_name}' not in allowlist")

    # Insufficient balance
    if wallet.balance < amount:
        return PolicyResult(Decision.BLOCKED, f"insufficient balance (have ${wallet.balance:.2f}, need ${amount:.2f})")

    # Hard ceiling — always blocked
    if amount > wallet.spend_limit_max:
        return PolicyResult(Decision.BLOCKED, f"${amount:.2f} exceeds hard limit of ${wallet.spend_limit_max:.2f}")

    # Middle tier — requires human approval
    if amount > wallet.auto_approve_limit:
        return PolicyResult(Decision.REQUIRES_APPROVAL, f"${amount:.2f} exceeds auto-approve threshold of ${wallet.auto_approve_limit:.2f} — pending manager review")

    # Below auto-approve threshold
    return PolicyResult(Decision.AUTO_APPROVE, "within policy")


def evaluate_full(amount: float, vendor_name: str, wallet, db) -> PolicyResult:
    """
    Full policy evaluation with trace logging. Runs checks in order,
    short-circuiting on first blocking condition.
    """
    from models import Transaction

    trace = []

    # 1. wallet_status
    if wallet.status == "suspended":
        trace.append({"check": "wallet_status", "passed": False, "detail": f"wallet is suspended"})
        return PolicyResult(Decision.BLOCKED, "wallet is suspended", trace)
    trace.append({"check": "wallet_status", "passed": True, "detail": f"wallet status is '{wallet.status}'"})

    # 2. vendor_allowlist
    allowed = json.loads(wallet.allowed_vendors) if isinstance(wallet.allowed_vendors, str) else wallet.allowed_vendors
    if allowed and vendor_name not in allowed:
        trace.append({"check": "vendor_allowlist", "passed": False, "detail": f"vendor '{vendor_name}' not in allowlist {allowed}"})
        return PolicyResult(Decision.BLOCKED, f"vendor '{vendor_name}' not in allowlist", trace)
    if allowed:
        trace.append({"check": "vendor_allowlist", "passed": True, "detail": f"vendor '{vendor_name}' is in allowlist"})
    else:
        trace.append({"check": "vendor_allowlist", "passed": True, "detail": "no vendor restriction (all vendors allowed)"})

    # 3. velocity
    if wallet.velocity_max_txn and wallet.velocity_max_txn > 0:
        since = datetime.utcnow() - timedelta(seconds=60)
        count = db.query(func.count(Transaction.id)).filter(
            Transaction.wallet_id == wallet.id,
            Transaction.status == "approved",
            Transaction.created_at >= since,
        ).scalar() or 0
        if count >= wallet.velocity_max_txn:
            wallet.status = "suspended"
            trace.append({"check": "velocity", "passed": False, "detail": f"{count} approved transactions in last 60s >= limit {wallet.velocity_max_txn} — wallet auto-suspended"})
            return PolicyResult(Decision.BLOCKED, f"velocity limit exceeded ({count} txns in 60s) — wallet suspended", trace)
        trace.append({"check": "velocity", "passed": True, "detail": f"{count} approved transactions in last 60s (limit {wallet.velocity_max_txn})"})
    else:
        trace.append({"check": "velocity", "passed": True, "detail": "no velocity limit configured"})

    # 4. balance
    reserved = float(getattr(wallet, "reserved_balance", 0.0) or 0.0)
    available = float(wallet.balance or 0.0) - reserved
    if available < amount:
        trace.append({"check": "balance", "passed": False, "detail": f"insufficient available balance — have ${available:.2f} (reserved ${reserved:.2f}), need ${amount:.2f}"})
        return PolicyResult(Decision.BLOCKED, f"insufficient balance (available ${available:.2f}, need ${amount:.2f})", trace)
    trace.append({"check": "balance", "passed": True, "detail": f"available ${available:.2f} (reserved ${reserved:.2f}) >= amount ${amount:.2f}"})

    # 5. daily_limit
    if wallet.daily_limit and wallet.daily_limit > 0:
        since = datetime.utcnow() - timedelta(hours=24)
        spent = db.query(func.coalesce(func.sum(Transaction.amount), 0.0)).filter(
            Transaction.wallet_id == wallet.id,
            Transaction.status == "approved",
            Transaction.created_at >= since,
        ).scalar() or 0.0
        if spent + amount > wallet.daily_limit:
            trace.append({"check": "daily_limit", "passed": False, "detail": f"daily spend ${spent:.2f} + ${amount:.2f} = ${spent+amount:.2f} exceeds daily limit ${wallet.daily_limit:.2f}"})
            return PolicyResult(Decision.BLOCKED, f"daily limit exceeded (spent ${spent:.2f} + ${amount:.2f} > ${wallet.daily_limit:.2f})", trace)
        trace.append({"check": "daily_limit", "passed": True, "detail": f"daily spend ${spent:.2f} + ${amount:.2f} = ${spent+amount:.2f} within ${wallet.daily_limit:.2f} limit"})
    else:
        trace.append({"check": "daily_limit", "passed": True, "detail": "no daily limit configured"})

    # 6. weekly_limit
    if wallet.weekly_limit and wallet.weekly_limit > 0:
        since = datetime.utcnow() - timedelta(days=7)
        spent = db.query(func.coalesce(func.sum(Transaction.amount), 0.0)).filter(
            Transaction.wallet_id == wallet.id,
            Transaction.status == "approved",
            Transaction.created_at >= since,
        ).scalar() or 0.0
        if spent + amount > wallet.weekly_limit:
            trace.append({"check": "weekly_limit", "passed": False, "detail": f"weekly spend ${spent:.2f} + ${amount:.2f} = ${spent+amount:.2f} exceeds weekly limit ${wallet.weekly_limit:.2f}"})
            return PolicyResult(Decision.BLOCKED, f"weekly limit exceeded (spent ${spent:.2f} + ${amount:.2f} > ${wallet.weekly_limit:.2f})", trace)
        trace.append({"check": "weekly_limit", "passed": True, "detail": f"weekly spend ${spent:.2f} + ${amount:.2f} = ${spent+amount:.2f} within ${wallet.weekly_limit:.2f} limit"})
    else:
        trace.append({"check": "weekly_limit", "passed": True, "detail": "no weekly limit configured"})

    # 7. spend_limit_max
    if amount > wallet.spend_limit_max:
        trace.append({"check": "spend_limit_max", "passed": False, "detail": f"${amount:.2f} exceeds hard ceiling ${wallet.spend_limit_max:.2f}"})
        return PolicyResult(Decision.BLOCKED, f"${amount:.2f} exceeds hard limit of ${wallet.spend_limit_max:.2f}", trace)
    trace.append({"check": "spend_limit_max", "passed": True, "detail": f"${amount:.2f} within hard ceiling ${wallet.spend_limit_max:.2f}"})

    # 8. auto_approve_limit
    if amount > wallet.auto_approve_limit:
        trace.append({"check": "auto_approve_limit", "passed": False, "detail": f"${amount:.2f} exceeds auto-approve threshold ${wallet.auto_approve_limit:.2f} — routing to approval queue"})
        return PolicyResult(Decision.REQUIRES_APPROVAL, f"${amount:.2f} exceeds auto-approve threshold of ${wallet.auto_approve_limit:.2f} — pending manager review", trace)
    trace.append({"check": "auto_approve_limit", "passed": True, "detail": f"${amount:.2f} within auto-approve threshold ${wallet.auto_approve_limit:.2f}"})

    # 9. All pass
    return PolicyResult(Decision.AUTO_APPROVE, "within policy", trace)
