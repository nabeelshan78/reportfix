"""Priority (CLAUDE.md section 8). Priority comes ONLY from facts: safety, problem type,
scope + blocked activity, and the weighted crowd. Tone, caps, "urgent" and injection have
no path into this module (P5): it never even sees the report text.
"""
from datetime import timedelta

from sqlmodel import Session, select

from app.community import trust
from app.models import Issue, IssueMember, User, utcnow
from app.triage.taxonomy import LEVEL_NAMES, default_impact

MAX_NON_SAFETY = 3  # P4: CRITICAL is reserved for safety


def crowd_boost(weight: float) -> int:
    """P3: W >= 3 -> +1 level, W >= 6 -> +2 levels."""
    return 2 if weight >= 6 else 1 if weight >= 3 else 0


def base_level(problem_type: str, scope: str, blocks: bool) -> tuple[int, list[str]]:
    """P2 + P2a. Scope boost only when the AI says building_or_many AND blocks essential activity."""
    base = default_impact(problem_type)
    reasons = [f"P2: base impact of {problem_type.replace('_', ' ')} = {LEVEL_NAMES[base]}"]
    if scope == "building_or_many" and blocks:
        base += 1
        reasons.append("P2a: affects many people AND blocks an essential activity -> +1 level")
    return base, reasons


def member_weights(session: Session, issue: Issue) -> list[tuple[User, float]]:
    """Distinct members with their weights. Discounted (brigading) members are excluded."""
    from app.community import brigading
    out = []
    members = session.exec(select(IssueMember).where(IssueMember.issue_id == issue.id)).all()
    discounted = brigading.discounted_user_ids(session, issue)
    for m in members:
        if m.user_id in discounted:
            continue
        user = session.get(User, m.user_id)
        if user and user.role == "student":
            out.append((user, trust.user_weight(session, user)))
    return out


def compute_priority(level_base: int, is_safety: bool, weight: float, n_students: int,
                     minutes_waiting: float, base_reasons: list[str] | None = None
                     ) -> tuple[int, int, list[str]]:
    """Pure function: (level, sort score, human-readable reasons)."""
    reasons: list[str] = list(base_reasons or [])
    if is_safety:
        reasons.insert(0, "P1: safety hazard -> CRITICAL (only an admin can lower it, A1)")
        level = 4
    else:
        level = level_base
        boost = crowd_boost(weight)
        who = f"{n_students} student{'s' if n_students != 1 else ''}"
        if boost:
            level += boost
            reasons.append(f"P3: {who}, crowd weight {weight:.1f} -> +{boost} level"
                           f"{'s' if boost > 1 else ''}")
        else:
            reasons.append(f"P3: {who}, crowd weight {weight:.1f} < 3 -> no boost "
                           "(one voice can't push an issue up)")
        if level > MAX_NON_SAFETY:
            level = MAX_NON_SAFETY
            reasons.append("P4: non-safety issue capped at HIGH (CRITICAL is for safety only)")
    score = level * 1000 + round(weight * 10) + min(int(minutes_waiting), 60)
    return level, score, reasons


def recompute_priority(session: Session, issue: Issue) -> Issue:
    """Recompute for the whole issue. Call after every membership/vote/status/flag change."""
    import json
    if issue.confidential:
        issue.priority_level, issue.priority_score = 0, 0
        issue.priority_reasons_json = json.dumps(
            ["C3: confidential reports are excluded from crowd priority"])
        return issue
    weights = member_weights(session, issue)
    total = sum(w for _, w in weights)
    created = issue.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=utcnow().tzinfo)
    waiting = (utcnow() - created) / timedelta(minutes=1)
    base_reasons = [f"P2: base impact = {LEVEL_NAMES.get(issue.base_impact, '?')} "
                    f"({issue.problem_type.replace('_', ' ')}"
                    f"{', incl. P2a scope boost' if issue.base_impact > default_impact(issue.problem_type) else ''})"]
    level, score, reasons = compute_priority(
        issue.base_impact, issue.is_safety, total, len(weights), waiting, base_reasons)
    issue.priority_level, issue.priority_score = level, score
    issue.priority_reasons_json = json.dumps(reasons)
    issue.updated_at = utcnow()
    session.add(issue)
    return issue
