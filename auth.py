import os
from typing import Optional

from fastapi import Depends, Header, HTTPException
from sqlalchemy.orm import Session

from database import get_db
from models import Wallet


ADMIN_KEY_ENV = "AGENT_COMMERCE_ADMIN_KEY"
DEFAULT_ADMIN_KEY = "demo-admin"


def admin_key_value() -> str:
    # MVP default: keep the demo usable without extra setup.
    return os.environ.get(ADMIN_KEY_ENV, DEFAULT_ADMIN_KEY)


def require_admin(x_admin_key: Optional[str] = Header(None, alias="X-Admin-Key")) -> None:
    if x_admin_key != admin_key_value():
        raise HTTPException(status_code=401, detail="missing/invalid admin key")


def is_admin(x_admin_key: Optional[str]) -> bool:
    return (x_admin_key or "") == admin_key_value()


def require_wallet_key(wallet_id: str, wallet_key: Optional[str], db: Session) -> Wallet:
    wallet = db.query(Wallet).filter(Wallet.id == wallet_id).first()
    if not wallet:
        raise HTTPException(status_code=404, detail="wallet not found")
    if not wallet.api_key or wallet.api_key != (wallet_key or ""):
        raise HTTPException(status_code=401, detail="missing/invalid wallet key")
    return wallet


def authed_wallet_from_path(
    wallet_id: str,
    x_wallet_key: Optional[str] = Header(None, alias="X-Wallet-Key"),
    db: Session = Depends(get_db),
) -> Wallet:
    return require_wallet_key(wallet_id=wallet_id, wallet_key=x_wallet_key, db=db)
