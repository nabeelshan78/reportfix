"""ALL visibility / permission checks and serializers live here (CLAUDE.md section 11).

Identity is the X-User-Id header: no passwords, by design (judge requirement).
Every endpoint must check permissions on the server, never only in the UI.
"""
import json
import re

from sqlmodel import Session, select

from app.models import AuditEvent, Issue, IssueMember, User, iso
from app.triage.locations import place_name
from app.triage.taxonomy import LEVEL_NAMES

ACTIVE_STATUSES = ("open", "in_progress", "reopened", "resolved")
UNVERIFIED = "UNVERIFIED"  # location_id prefix of an ASK9 emergency whose place is unconfirmed


def is_unverified(location_id: str | None) -> bool:
    return bool(location_id) and location_id.startswith(UNVERIFIED)


def is_member(session: Session, issue_id: int, user_id: int) -> bool:
    return session.exec(
        select(IssueMember).where(IssueMember.issue_id == issue_id,
                                  IssueMember.user_id == user_id)
    ).first() is not None


def is_reporter(session: Session, issue_id: int, user_id: int) -> bool:
    return session.exec(
        select(IssueMember).where(IssueMember.issue_id == issue_id,
                                  IssueMember.user_id == user_id,
                                  IssueMember.via == "report")
    ).first() is not None


def _sorted(issues: list[Issue]) -> list[Issue]:
    return sorted(issues, key=lambda i: (-i.priority_score, -i.id))


def visible_issues(session: Session, viewer: User,
                   include_own_confidential: bool = False) -> list[Issue]:
    """Issues the viewer may see on a board/queue.

    Student: public (non-confidential) board; own confidential ones only if asked.
    Staff: non-confidential issues of their own department.
    Admin: everything.
    """
    issues = list(session.exec(select(Issue)))
    if viewer.role == "admin":
        return _sorted(issues)
    if viewer.role == "staff":
        return _sorted([i for i in issues
                        if not i.confidential and i.department == viewer.department
                        and not is_unverified(i.location_id)])
    out = [i for i in issues if not i.confidential and not is_unverified(i.location_id)]
    if include_own_confidential:
        out += [i for i in issues
                if i.confidential and is_reporter(session, i.id, viewer.id)]
    return _sorted(out)


def my_issues(session: Session, viewer: User) -> list[Issue]:
    """Issues a student reported or joined (including own confidential ones)."""
    ids = [m.issue_id for m in session.exec(
        select(IssueMember).where(IssueMember.user_id == viewer.id))]
    if not ids:
        return []
    issues = list(session.exec(select(Issue).where(Issue.id.in_(ids))))
    return _sorted(issues)


def can_view_issue(session: Session, issue: Issue, viewer: User) -> bool:
    if viewer.role == "admin":
        return True
    if issue.confidential:  # C1: only admin + own reporter
        return viewer.role == "student" and is_reporter(session, issue.id, viewer.id)
    if is_unverified(issue.location_id):  # ASK9: incomplete -> admin + its reporter only
        return viewer.role == "student" and is_member(session, issue.id, viewer.id)
    if viewer.role == "staff":
        return issue.department == viewer.department
    return True  # students: public board


def can_update_issue(session: Session, issue: Issue, viewer: User) -> bool:
    """Status changes / mark fake: staff of that department, or admin."""
    if viewer.role == "admin":
        return True
    return (viewer.role == "staff" and not issue.confidential
            and issue.department == viewer.department)


def can_vote(session: Session, issue: Issue, viewer: User) -> bool:
    """V1: students only, issue resolved, member who joined before resolved_at
    (or joined via report_after_resolve)."""
    if viewer.role != "student" or issue.status != "resolved" or not issue.resolved_at:
        return False
    m = session.exec(
        select(IssueMember).where(IssueMember.issue_id == issue.id,
                                  IssueMember.user_id == viewer.id)
    ).first()
    if not m:
        return False
    return m.via == "report_after_resolve" or m.joined_at <= issue.resolved_at


def serialize_issue(session: Session, issue: Issue, viewer: User,
                    detail: bool = False) -> dict:
    """Issue payload. NEVER contains reporter/member ids or names (C2)."""
    out = {
        "id": issue.id,
        "title": issue.title,
        "department": issue.department,
        "location_id": issue.location_id,
        "location_name": (f"UNVERIFIED place: {issue.location_id.split(':', 1)[1].strip()}"
                          if is_unverified(issue.location_id) else place_name(issue.location_id)),
        "unverified": is_unverified(issue.location_id),
        "problem_type": issue.problem_type,
        "is_safety": issue.is_safety,
        "confidential": issue.confidential,
        "status": issue.status,
        "priority_level": issue.priority_level,
        "priority_name": LEVEL_NAMES.get(issue.priority_level, "N/A"),
        "priority_score": issue.priority_score,
        "priority_reasons": json.loads(issue.priority_reasons_json),
        "decision_log": json.loads(issue.decision_log_json),
        "round": issue.round,
        "reopen_count": issue.reopen_count,
        "created_at": iso(issue.created_at),
        "updated_at": iso(issue.updated_at),
        "resolved_at": iso(issue.resolved_at),
        "is_mine": is_member(session, issue.id, viewer.id),
        "can_vote": can_vote(session, issue, viewer),
        "can_update": can_update_issue(session, issue, viewer),
    }
    out["ai_suggested_department"] = _ai_suggestion(issue)
    from app.community import voting  # lazy: voting imports this module
    out["verification"] = voting.verification_state(session, issue, viewer)
    if issue.confidential:
        out["member_count"] = None
        if viewer.role == "admin":
            out["reporter"] = "Anonymous reporter"
        else:
            out["notice"] = ("Confidential: only the admin can see this. "
                             "Your identity is hidden.")
    else:
        out["member_count"] = len(list(session.exec(
            select(IssueMember).where(IssueMember.issue_id == issue.id))))
    if detail:
        events = session.exec(
            select(AuditEvent).where(AuditEvent.issue_id == issue.id)
            .order_by(AuditEvent.id))
        out["timeline"] = [
            {"action": e.action, "from_status": e.from_status, "to_status": e.to_status,
             "note": e.note, "at": iso(e.created_at)}
            for e in events
        ]
    return out


def _ai_suggestion(issue: Issue) -> str | None:
    """R4: next to an UNASSIGNED issue, show the department the AI suggested (one-click reassign)."""
    if issue.department != "UNASSIGNED":
        return None
    for line in reversed(json.loads(issue.decision_log_json)):
        m = re.search(r"AI suggested (\w+)", line)
        if m and m.group(1) != "NONE":
            return m.group(1)
    return None


def waiting_reports(session: Session) -> list:
    """Admin only: reports waiting for a student's answer (never confidential ones)."""
    from app.models import Report
    return list(session.exec(select(Report).where(Report.status == "needs_clarification")
                             .order_by(Report.id.desc())))


def emergency_issues(session: Session) -> list[Issue]:
    """Admin only (ASK9): hazard issues whose place is not verified yet."""
    return [i for i in _sorted(list(session.exec(select(Issue))))
            if i.is_safety and is_unverified(i.location_id) and i.status in ACTIVE_STATUSES]
