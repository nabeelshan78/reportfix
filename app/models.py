"""SQLModel tables (CLAUDE.md section 6.1). All timestamps are timezone-aware UTC."""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    role: str  # student / staff / admin
    department: Optional[str] = None  # staff only
    created_at: datetime = Field(default_factory=utcnow)
    is_seed: bool = False


class Issue(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    title: str
    department: str  # EE/CSE/IS/Library/Cafeteria/Labs/CONFIDENTIAL/UNASSIGNED
    location_id: Optional[str] = None
    problem_type: str
    base_impact: int = 1
    is_safety: bool = False
    confidential: bool = False
    # open / in_progress / resolved / reopened / closed_verified / closed_unverified / closed_fake
    status: str = "open"
    priority_level: int = 1  # 1 LOW .. 4 CRITICAL, 0 = n/a (confidential)
    priority_score: int = 0
    priority_reasons_json: str = "[]"
    decision_log_json: str = "[]"
    round: int = 0
    resolved_at: Optional[datetime] = None
    reopen_count: int = 0
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class Report(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id")
    raw_text: str  # never modified
    cleaned_text: str = ""
    stripped_text: str = ""
    extraction_json: str = "{}"
    engine: str = ""
    latency_ms: int = 0
    # routed_new / merged / needs_clarification / rejected_not_report /
    # rejected_rate_limit / confidential / cancelled
    status: str = "routed_new"
    issue_id: Optional[int] = Field(default=None, foreign_key="issue.id")
    location_id: Optional[str] = None
    injection_flag: bool = False
    tone_flag: bool = False
    clarification_question: Optional[str] = None
    suggestions_json: str = "{}"  # the current question's options (ASK6)
    clarification_history_json: str = "{}"  # ASK7: turns, facts, attempts
    decision_log_json: str = "[]"
    created_at: datetime = Field(default_factory=utcnow)


class IssueMember(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("issue_id", "user_id"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    issue_id: int = Field(foreign_key="issue.id")
    user_id: int = Field(foreign_key="user.id")
    via: str = "report"  # report / me_too / report_after_resolve
    joined_at: datetime = Field(default_factory=utcnow)


class Vote(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("issue_id", "user_id", "round"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    issue_id: int = Field(foreign_key="issue.id")
    user_id: int = Field(foreign_key="user.id")
    round: int = 1
    value: str  # confirm / dispute
    weight_at_vote: float = 1.0
    discounted: bool = False
    created_at: datetime = Field(default_factory=utcnow)


class TrustEvent(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id")
    code: str
    delta: int
    reason_text: str = ""
    issue_id: Optional[int] = Field(default=None, foreign_key="issue.id")
    report_id: Optional[int] = Field(default=None, foreign_key="report.id")
    created_at: datetime = Field(default_factory=utcnow)


class AuditEvent(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    issue_id: int = Field(foreign_key="issue.id")
    actor_id: Optional[int] = Field(default=None, foreign_key="user.id")
    action: str
    from_status: Optional[str] = None
    to_status: Optional[str] = None
    note: str = ""
    created_at: datetime = Field(default_factory=utcnow)


class Flag(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    type: str  # injection / spam / brigading / disputed
    user_ids_json: str = "[]"
    issue_id: Optional[int] = Field(default=None, foreign_key="issue.id")
    report_id: Optional[int] = Field(default=None, foreign_key="report.id")
    detail: str = ""
    cleared: bool = False
    created_at: datetime = Field(default_factory=utcnow)
