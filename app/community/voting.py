"""Verification rounds (CLAUDE.md section 10, V1-V6).

"Resolved" is not the end: students who were affected confirm or dispute the fix.
Staff and admins never vote. Losing a vote carries no penalty (honest minority voters
are not punished); only matching the final outcome earns VOTE_CORRECT.
"""
import json

from sqlmodel import Session, select

from app.community import priority, trust
from app.models import AuditEvent, Flag, Issue, IssueMember, User, Vote, utcnow

THRESHOLD = 1.5  # V3 / V4: weight needed to reopen or verify


def _issue_ref(issue: Issue) -> int | None:
    """Confidential issues never link to the reporter's trust ledger (C2)."""
    return None if issue.confidential else issue.id


def current_votes(session: Session, issue: Issue) -> list[Vote]:
    return list(session.exec(select(Vote).where(Vote.issue_id == issue.id,
                                                Vote.round == issue.round)))


def my_vote(session: Session, issue: Issue, user: User) -> Vote | None:
    return session.exec(select(Vote).where(Vote.issue_id == issue.id, Vote.user_id == user.id,
                                           Vote.round == issue.round)).first()


def tally(session: Session, issue: Issue) -> tuple[float, float, int, int]:
    """(confirm weight, dispute weight, confirm count, dispute count). Discounted votes excluded."""
    c = d = 0.0
    nc = nd = 0
    for v in current_votes(session, issue):
        if v.discounted:
            continue
        if v.value == "confirm":
            c, nc = c + v.weight_at_vote, nc + 1
        else:
            d, nd = d + v.weight_at_vote, nd + 1
    return round(c, 2), round(d, 2), nc, nd


def _log(issue: Issue, line: str) -> None:
    lines = json.loads(issue.decision_log_json)
    lines.append(line)
    issue.decision_log_json = json.dumps(lines)


def _audit(session: Session, issue: Issue, actor: User | None, action: str, frm: str | None,
           to: str | None, note: str) -> None:
    session.add(AuditEvent(issue_id=issue.id, actor_id=actor.id if actor else None,
                           action=action, from_status=frm, to_status=to, note=note))


def cast_vote(session: Session, issue: Issue, user: User, value: str) -> Vote:
    """V1: only eligible students vote; one vote per round, changeable until it closes."""
    from app.permissions import can_vote
    if value not in ("confirm", "dispute"):
        raise ValueError("Vote must be 'confirm' or 'dispute'.")
    if not can_vote(session, issue, user):
        raise PermissionError("Only students who were affected before the fix can vote, "
                              "and only while the issue is marked resolved.")
    w = trust.user_weight(session, user)
    v = my_vote(session, issue, user)
    if v:
        v.value, v.weight_at_vote, v.created_at = value, w, utcnow()
    else:
        v = Vote(issue_id=issue.id, user_id=user.id, round=issue.round, value=value,
                 weight_at_vote=w)
    session.add(v)
    session.flush()
    evaluate_round(session, issue)
    return v


def _flag_disputed(session: Session, issue: Issue, c: float, d: float) -> None:
    detail = f"Disputes {d:.1f} vs confirms {c:.1f}: below the reopen threshold ({THRESHOLD})"
    flag = session.exec(select(Flag).where(Flag.issue_id == issue.id, Flag.type == "disputed",
                                           Flag.cleared == False)).first()  # noqa: E712
    if flag:
        flag.detail = detail
    else:
        flag = Flag(type="disputed", issue_id=issue.id, user_ids_json="[]", detail=detail)
    session.add(flag)


def _clear_disputed_flags(session: Session, issue: Issue) -> None:
    for f in session.exec(select(Flag).where(Flag.issue_id == issue.id, Flag.type == "disputed",
                                             Flag.cleared == False)):  # noqa: E712
        f.cleared = True
        session.add(f)


def _reward(session: Session, issue: Issue, value: str, outcome: str) -> None:
    for v in current_votes(session, issue):
        if v.value == value and not v.discounted:
            trust.add_event(session, v.user_id, "VOTE_CORRECT",
                            f"Your '{value}' vote matched the final outcome ({outcome})",
                            issue_id=_issue_ref(issue))


def _reopen(session: Session, issue: Issue, c: float, d: float, by: str, actor=None) -> str:
    frm = issue.status
    issue.status = "reopened"
    issue.reopen_count += 1
    issue.updated_at = utcnow()
    note = f"Reopened by {by}: disputes {d:.1f} vs confirms {c:.1f}"
    _audit(session, issue, actor, "reopened", frm, "reopened", note)
    _log(issue, f"V3: {note}; back in the {issue.department} queue")
    _reward(session, issue, "dispute", "reopened")
    _clear_disputed_flags(session, issue)
    priority.recompute_priority(session, issue)
    session.add(issue)
    return "reopened"


def _verify(session: Session, issue: Issue, c: float, d: float, by: str, actor=None) -> str:
    frm = issue.status
    issue.status = "closed_verified"
    issue.updated_at = utcnow()
    note = f"Verified by {by}: confirms {c:.1f} vs disputes {d:.1f}"
    _audit(session, issue, actor, "verified", frm, "closed_verified", note)
    _log(issue, f"V4: {note}")
    _reward(session, issue, "confirm", "verified")
    for m in session.exec(select(IssueMember).where(IssueMember.issue_id == issue.id,
                                                    IssueMember.via == "report")):
        trust.add_event(session, m.user_id, "REPORT_CONFIRMED",
                        "An issue you reported was verified as fixed by the community",
                        issue_id=_issue_ref(issue))
    _clear_disputed_flags(session, issue)
    priority.recompute_priority(session, issue)
    session.add(issue)
    return "verified"


def evaluate_round(session: Session, issue: Issue) -> str | None:
    """V3 / V4 / V5 after every vote. Returns 'reopened', 'verified' or None."""
    if issue.status != "resolved":
        return None
    c, d, _nc, _nd = tally(session, issue)
    if d >= THRESHOLD and d > c:
        return _reopen(session, issue, c, d, "community")
    if c >= THRESHOLD and c > d:
        return _verify(session, issue, c, d, "community")
    if d > 0:  # V5: stays resolved, flagged as disputed
        _flag_disputed(session, issue, c, d)
    return None


def finalize(session: Session, issue: Issue, admin: User) -> str:
    """V6: admin 'Finalize now'. Higher weight wins; no votes -> closed_unverified."""
    if issue.status != "resolved":
        raise ValueError("Only an issue marked resolved can be finalized.")
    c, d, nc, nd = tally(session, issue)
    if nc + nd == 0 or c == d:
        frm = issue.status
        issue.status = "closed_unverified"
        issue.updated_at = utcnow()
        why = "no votes were cast" if nc + nd == 0 else f"tie ({c:.1f} vs {d:.1f})"
        note = f"Finalized by admin without a verdict: {why}"
        _audit(session, issue, admin, "finalized", frm, "closed_unverified", note)
        _log(issue, f"V6: {note}; no trust changes")
        _clear_disputed_flags(session, issue)
        priority.recompute_priority(session, issue)
        session.add(issue)
        return "closed_unverified"
    if d > c:
        return _reopen(session, issue, c, d, "admin finalize", admin)
    return _verify(session, issue, c, d, "admin finalize", admin)


def verification_state(session: Session, issue: Issue, viewer: User) -> dict | None:
    """Counts and weights only: never who voted."""
    if issue.round < 1:
        return None
    c, d, nc, nd = tally(session, issue)
    if issue.status == "resolved":
        label = ("Disputed (below threshold)" if d > 0
                 else "Awaiting confirmation from affected students")
    elif issue.status == "reopened":
        label = "Reopened by the community"
    elif issue.status == "closed_verified":
        label = "Verified as fixed"
    elif issue.status == "closed_unverified":
        label = "Closed without a verdict"
    else:
        label = "Fix in progress"
    mine = my_vote(session, issue, viewer) if viewer.role == "student" else None
    return {"round": issue.round, "label": label, "threshold": THRESHOLD,
            "confirm_weight": c, "dispute_weight": d, "confirms": nc, "disputes": nd,
            "my_vote": mine.value if mine else None}
