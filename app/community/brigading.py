"""Brigading detection (B1, bonus). Built in Phase 5; for now no member is discounted."""
from sqlmodel import Session

from app.models import Issue


def discounted_user_ids(session: Session, issue: Issue) -> set[int]:
    """User ids whose weight on this issue is 0 because of an active brigading flag."""
    return set()
