"""Trust ledger and vote/report weight (CLAUDE.md section 10: TR0, W1, W2)."""
from datetime import timedelta

from sqlmodel import Session, select

from app.models import TrustEvent, User, utcnow

# code -> (delta, meaning)
TRUST_CODES = {
    "REPORT_ACKNOWLEDGED": 2,
    "REPORT_CONFIRMED": 5,
    "VOTE_CORRECT": 2,
    "FAKE_REPORT": -15,
    "FAKE_METOO": -5,
    "INJECTION_ATTEMPT": -10,
    "SPAM_RATE": -5,
    "BRIGADING": -5,
}

NEW_ACCOUNT_MINUTES = 30


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def add_event(session: Session, user_id: int, code: str, reason: str,
              issue_id: int | None = None, report_id: int | None = None) -> TrustEvent:
    ev = TrustEvent(user_id=user_id, code=code, delta=TRUST_CODES[code],
                    reason_text=reason, issue_id=issue_id, report_id=report_id)
    session.add(ev)
    return ev


def ledger(session: Session, user_id: int) -> list[TrustEvent]:
    return list(session.exec(
        select(TrustEvent).where(TrustEvent.user_id == user_id).order_by(TrustEvent.id)
    ))


def trust_score(session: Session, user_id: int) -> int:
    """TR0: always computed from the ledger."""
    total = sum(e.delta for e in ledger(session, user_id))
    return int(clamp(50 + total, 0, 100))


def is_new_account(session: Session, user: User) -> bool:
    """W2: created < 30 min ago AND no positive trust events."""
    if utcnow() - user.created_at >= timedelta(minutes=NEW_ACCOUNT_MINUTES):
        return False
    return not any(e.delta > 0 for e in ledger(session, user.id))


def weight_info(session: Session, user: User) -> dict:
    """W1 + W2, with a human-readable explanation."""
    score = trust_score(session, user.id)
    raw = score / 50
    weight = clamp(raw, 0.2, 1.5)
    note = " (capped)" if raw > 1.5 else " (floored)" if raw < 0.2 else ""
    text = f"W1: trust {score} -> weight {weight:.2f}{note}"
    new = is_new_account(session, user)
    if new:
        weight *= 0.5
        text += f"; W2: brand-new account x0.5 -> {weight:.2f}"
    return {"score": score, "weight": round(weight, 2), "new_account": new,
            "explanation": text}


def user_weight(session: Session, user: User) -> float:
    return weight_info(session, user)["weight"]
