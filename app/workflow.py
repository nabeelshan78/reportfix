"""Staff/admin/student actions on existing issues: status, mark fake, me too, reassign,
lower safety. Permission checks happen in main.py via permissions.py; the rules live here."""
import json

from sqlmodel import Session, select

from app.community import grouping, priority, trust
from app.models import AuditEvent, Issue, IssueMember, TrustEvent, User, utcnow
from app.permissions import ACTIVE_STATUSES, is_unverified
from app.triage.taxonomy import department_codes


class RuleError(Exception):
    """A user-facing rule violation (shown as a clear message, HTTP 400)."""


def _ref(issue: Issue) -> int | None:
    return None if issue.confidential else issue.id


def _audit(session, issue, actor, action, frm, to, note):
    session.add(AuditEvent(issue_id=issue.id, actor_id=actor.id, action=action,
                           from_status=frm, to_status=to, note=note))


def _log(issue: Issue, line: str) -> None:
    lines = json.loads(issue.decision_log_json)
    lines.append(line)
    issue.decision_log_json = json.dumps(lines)


def set_status(session: Session, issue: Issue, actor: User, status: str, note: str) -> Issue:
    """Staff: open/reopened -> in_progress, or -> resolved (V1: round + 1, resolved_at set)."""
    if status not in ("in_progress", "resolved"):
        raise RuleError("Status must be 'in_progress' or 'resolved'.")
    if not note or not note.strip():
        raise RuleError("A note is required when changing the status.")
    if issue.status not in ("open", "in_progress", "reopened"):
        raise RuleError(f"An issue that is '{issue.status}' cannot be changed this way.")
    if status == "in_progress" and issue.status == "in_progress":
        raise RuleError("The issue is already in progress.")
    first_move = session.exec(select(AuditEvent).where(
        AuditEvent.issue_id == issue.id, AuditEvent.action == "status_change")).first() is None
    frm = issue.status
    issue.status = status
    issue.updated_at = utcnow()
    if status == "resolved":
        issue.round += 1  # V1: a new verification round starts
        issue.resolved_at = utcnow()
    _audit(session, issue, actor, "status_change", frm, status, note.strip())
    if first_move:
        _acknowledge_reporters(session, issue)
    priority.recompute_priority(session, issue)
    session.add(issue)
    return issue


def _acknowledge_reporters(session: Session, issue: Issue) -> None:
    """REPORT_ACKNOWLEDGED +2 for reporters, when staff FIRST moves the issue."""
    for m in session.exec(select(IssueMember).where(IssueMember.issue_id == issue.id,
                                                    IssueMember.via == "report")):
        trust.add_event(session, m.user_id, "REPORT_ACKNOWLEDGED",
                        "Staff acknowledged an issue you reported", issue_id=_ref(issue))


def mark_fake(session: Session, issue: Issue, actor: User, reason: str) -> Issue:
    """Staff/admin: closed_fake. Reporters -15, me-too joiners -5."""
    if not reason or not reason.strip():
        raise RuleError("A reason is required to mark an issue as fake.")
    if issue.status in ("closed_fake", "closed_verified", "closed_unverified"):
        raise RuleError(f"An issue that is '{issue.status}' cannot be marked fake.")
    frm = issue.status
    issue.status = "closed_fake"
    issue.updated_at = utcnow()
    _audit(session, issue, actor, "mark_fake", frm, "closed_fake", reason.strip())
    _log(issue, f"Marked fake by {actor.role}: {reason.strip()}")
    for m in session.exec(select(IssueMember).where(IssueMember.issue_id == issue.id)):
        if m.via == "me_too":
            trust.add_event(session, m.user_id, "FAKE_METOO",
                            "You joined an issue that was marked fake", issue_id=_ref(issue))
        else:
            trust.add_event(session, m.user_id, "FAKE_REPORT",
                            "An issue you reported was marked fake", issue_id=_ref(issue))
    priority.recompute_priority(session, issue)
    session.add(issue)
    return issue


def me_too(session: Session, issue: Issue, user: User) -> tuple[bool, str]:
    """G5: students join an active, non-confidential issue. Returns (joined, message)."""
    if user.role != "student":
        raise RuleError("Only students can use 'Me too'.")
    if issue.confidential or is_unverified(issue.location_id):
        raise RuleError("This issue cannot be joined.")
    if issue.status not in ACTIVE_STATUSES:
        raise RuleError("This issue is closed, so it can't be joined any more.")
    if grouping.get_member(session, issue.id, user.id):
        return False, "You already reported or joined this issue."
    grouping.add_member(session, issue, user, "me_too")
    n = grouping.member_count(session, issue.id)
    session.add(AuditEvent(issue_id=issue.id, actor_id=None, action="me_too",
                           note=f"Another student confirmed the problem ({n} students now)"))
    priority.recompute_priority(session, issue)
    session.add(issue)
    return True, f"You joined issue #{issue.id}. {n} students are affected."


def reassign(session: Session, issue: Issue, admin: User, department: str) -> Issue:
    if department not in department_codes():
        raise RuleError(f"Department must be one of: {', '.join(department_codes())}.")
    frm = issue.department
    issue.department = department
    issue.updated_at = utcnow()
    _audit(session, issue, admin, "reassign", issue.status, issue.status,
           f"Department changed {frm} -> {department} by admin")
    _log(issue, f"Reassigned by admin: {frm} -> {department}")
    session.add(issue)
    return issue


def lower_safety(session: Session, issue: Issue, admin: User, reason: str) -> Issue:
    """A1: only an admin can lower a safety priority, with a logged reason."""
    if not reason or not reason.strip():
        raise RuleError("A reason is required to lower a safety priority.")
    if not issue.is_safety:
        raise RuleError("This issue is not flagged as a safety hazard.")
    issue.is_safety = False
    issue.base_impact = min(issue.base_impact, 3)
    issue.updated_at = utcnow()
    _audit(session, issue, admin, "lower_safety", issue.status, issue.status,
           f"A1: safety flag removed by admin. Reason: {reason.strip()}")
    _log(issue, f"A1: safety priority lowered by admin. Reason: {reason.strip()}")
    priority.recompute_priority(session, issue)
    session.add(issue)
    return issue
