"""Grouping and "me too" (CLAUDE.md section 9).

G1 key = (location_id, problem_type) among non-confidential ACTIVE issues.
G2 problem_type "other": fuzzy summary compare, AI yes/no only for the 60-79 grey zone.
G3 already a member -> count does not change.
G4 a matching report on a resolved issue joins as report_after_resolve (+ a dispute vote).
"""
from rapidfuzz import fuzz
from sqlmodel import Session, select

from app.models import Issue, IssueMember, User, utcnow
from app.permissions import ACTIVE_STATUSES
from app.triage import llm


def active_candidates(session: Session, location_id: str | None, problem_type: str) -> list[Issue]:
    q = select(Issue).where(Issue.confidential == False,  # noqa: E712
                            Issue.location_id == location_id,
                            Issue.problem_type == problem_type,
                            Issue.status.in_(ACTIVE_STATUSES)).order_by(Issue.id)
    return list(session.exec(q))


def _same_other_issue(summary_a: str, summary_b: str) -> tuple[bool, str]:
    """G2: token_set_ratio first; only the 60-79 grey zone costs an AI call."""
    score = fuzz.token_set_ratio(summary_a.lower(), summary_b.lower())
    if score >= 80:
        return True, f"G2: summaries match ({score:.0f} >= 80)"
    if score < 60:
        return False, f"G2: summaries differ ({score:.0f} < 60)"
    prompt = ("Do these two campus problem reports describe the SAME problem? "
              f"Answer only YES or NO.\nA: {summary_a}\nB: {summary_b}")
    answer = llm.ask_llm_text(prompt)
    if answer is None:
        same = score >= 70
        return same, f"G2: summaries {score:.0f}; AI unavailable, fallback threshold 70 -> " \
                     f"{'same' if same else 'different'}"
    same = answer.strip().upper().startswith("YES")
    return same, f"G2: summaries {score:.0f}; AI tie-break says {'same' if same else 'different'}"


def find_matching_issue(session: Session, location_id: str | None, problem_type: str,
                        summary_en: str) -> tuple[Issue | None, str]:
    """Returns (issue or None, decision-log line)."""
    cands = active_candidates(session, location_id, problem_type)
    if problem_type != "other":
        if cands:
            c = cands[0]
            return c, f"G1: same place + same problem type ({location_id}, {problem_type}) -> issue #{c.id}"
        return None, f"G1: no active issue for ({location_id}, {problem_type}) -> new issue"
    for c in cands:
        same, line = _same_other_issue(summary_en, c.title)
        if same:
            return c, f"{line} -> issue #{c.id}"
    return None, "G2: no similar active 'other' issue at this place -> new issue"


def get_member(session: Session, issue_id: int, user_id: int) -> IssueMember | None:
    return session.exec(select(IssueMember).where(
        IssueMember.issue_id == issue_id, IssueMember.user_id == user_id)).first()


def member_count(session: Session, issue_id: int) -> int:
    return len(session.exec(select(IssueMember).where(IssueMember.issue_id == issue_id)).all())


def add_member(session: Session, issue: Issue, user: User, via: str) -> IssueMember:
    m = IssueMember(issue_id=issue.id, user_id=user.id, via=via, joined_at=utcnow())
    session.add(m)
    session.flush()
    return m
