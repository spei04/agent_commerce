import json
from datetime import datetime, timezone
from typing import Optional
from models import Product, SkuInsight


def score(product: Product, intent: str, budget: Optional[float], constraints: dict) -> float:
    """
    Score a product against a natural-language intent + constraints.
    Higher score = better match. Returns 0.0 if hard constraints fail.
    """
    intent_lower = intent.lower()
    tags = json.loads(product.tags) if isinstance(product.tags, str) else product.tags

    # Hard filter: budget
    if budget is not None and product.price > budget:
        return 0.0

    s = 0.0

    # Tag matches (strongest signal)
    for tag in tags:
        if tag.lower() in intent_lower:
            s += 2.0

    # Name keyword matches
    for word in intent_lower.split():
        if len(word) > 3:
            if word in product.name.lower():
                s += 1.5
            if word in (product.description or "").lower():
                s += 0.5

    # Soft penalty for slow delivery
    max_days = constraints.get("delivery_days")
    if max_days is not None and product.lead_time_days > max_days:
        s *= 0.3

    # Prefer lower price among equally-scored products
    if s > 0:
        s -= product.price * 0.001

    return s


def resolve_intent(db, intent: str, budget: Optional[float] = None, constraints: Optional[dict] = None, limit: int = 5) -> list:
    constraints = constraints or {}
    products = db.query(Product).all()

    scored = [
        (score(p, intent, budget, constraints), p)
        for p in products
    ]
    scored = [(s, p) for s, p in scored if s > 0]
    scored.sort(key=lambda x: x[0], reverse=True)

    top = scored[:limit]

    # Instrument SKU impressions for every product that appears in results
    if db is not None:
        now = datetime.now(timezone.utc)
        for rank, (s, p) in enumerate(top, start=1):
            insight = db.query(SkuInsight).filter(SkuInsight.product_id == p.id).first()
            if insight is None:
                insight = SkuInsight(
                    id=__import__("uuid").uuid4().__str__(),
                    product_id=p.id,
                )
                db.add(insight)
            insight.impressions += 1
            insight.rank_sum += rank
            insight.score_sum += s
            insight.last_seen = now
        db.commit()

    return [
        {
            "product_id": p.id,
            "vendor_name": p.vendor_name,
            "name": p.name,
            "description": p.description,
            "price": p.price,
            "min_order": p.min_order,
            "lead_time_days": p.lead_time_days,
            "tags": json.loads(p.tags) if isinstance(p.tags, str) else p.tags,
            "score": round(s, 2),
        }
        for s, p in top
    ]
