"""The report pipeline (CLAUDE.md section 6). AI understands, code decides.

Steps (rule IDs appear in the decision log):
 1 store raw text   2 SP1 rate limit   3 clean + T1 tone   4 I1 injection strip
 5 AI extraction    6 V1 validity      7 C1 confidential   8 S1/S2 safety
 9 completeness (ASK1/ASK2) + location (L1-L7)            10 problem type
 11 routing R0-R5 (campus.json)   12 grouping G1-G4   13 priority P1-P5   14 respond

A report that is not complete (problem or place missing) is NOT routed, grouped, counted or
shown on any board: the code asks one question at a time (ASK3) until it is complete. The
answers re-run steps 8-14 from the stored extraction, with NO AI call when buttons or the
code resolver can read the answer (ASK7). Safety never waits (ASK9).
"""
import json
from dataclasses import dataclass, field
from datetime import timedelta

from sqlmodel import Session, select

from app.community import grouping, priority, trust, voting
from app.models import AuditEvent, Flag, Issue, Report, TrustEvent, User, utcnow
from app.permissions import UNVERIFIED
from app.triage import clarify, llm, locations, rules, taxonomy
from app.triage.locations import Resolution
from app.triage.schema import TriageExtraction
from app.triage.taxonomy import HAZARD_TO_TYPE, LEVEL_NAMES, PROBLEM_TYPES, SAFETY_TYPES
from app.workflow import RuleError

RATE_LIMIT = 5  # SP1: reports allowed per window
RATE_WINDOW = timedelta(minutes=10)
MAX_CHARS = 1000
NOT_A_REPORT = "This doesn't look like a campus problem report."
CONFIDENTIAL_NOTICE = "Confidential: only the admin can see this. Your identity is hidden."


@dataclass
class Ctx:
    session: Session
    user: User
    report: Report
    log: list[str] = field(default_factory=list)
    inj: rules.InjectionResult | None = None
    injection_flag: Flag | None = None
    engine: str = ""
    latency_ms: int = 0
    state: dict = field(default_factory=dict)

    def add(self, lines) -> None:
        """Append decision-log lines (skipping ones already there: steps 8-14 can re-run)."""
        for line in ([lines] if isinstance(lines, str) else lines):
            if line not in self.log:
                self.log.append(line)


# ----------------------------------------------------------------------
# conversation state (Report.clarification_history_json)
# ----------------------------------------------------------------------
def load_state(report: Report) -> dict:
    st = json.loads(report.clarification_history_json or "{}") or {}
    st.setdefault("turns", [])
    st.setdefault("facts", {"place": None, "ptype": None, "problem_clear": None,
                            "admin_decide": False})
    st.setdefault("attempts", {"place": 0, "problem": 0})
    st.setdefault("extra_stripped", [])
    return st


def _save(ctx: Ctx) -> None:
    ctx.report.decision_log_json = json.dumps(ctx.log)
    ctx.report.clarification_history_json = json.dumps(ctx.state)
    ctx.session.add(ctx.report)
    ctx.session.commit()


def conversation(state: dict) -> list[dict]:
    """The question/answer thread as the student sees it."""
    return [{"question": t["question"],
             "answer": (t["answer"] or {}).get("text") if t.get("answer") else None}
            for t in state["turns"]]


def pending_payload(report: Report) -> dict:
    """What the UI needs to show a waiting report: the question, options and thread."""
    st = load_state(report)
    options = json.loads(report.suggestions_json or "{}")
    return {"report_id": report.id, "text": report.raw_text, "status": report.status,
            "question": report.clarification_question, "options": options,
            "history": conversation(st), "optional": bool(st.get("optional")),
            "hazard": bool(report.issue_id) and report.status != "confidential"}


def _place_label(loc: str | None) -> str:
    return locations.place_name(loc) or "no place"


def _result(ctx: Ctx, message: str, **extra) -> dict:
    r = ctx.report
    out = {
        "report_id": r.id, "status": r.status, "message": message,
        "issue_id": r.issue_id, "engine": ctx.engine or r.engine,
        "rules_mode": (ctx.engine or r.engine) == "rules",
        "latency_ms": ctx.latency_ms or r.latency_ms, "decision_log": list(ctx.log),
        "clarification": None, "merged": False, "already_member": False,
    }
    if r.status == "needs_clarification" or (r.status == "confidential"
                                             and r.clarification_question):
        out["clarification"] = pending_payload(r)
    out.update(extra)
    return out


def _injection_consequences(ctx: Ctx, confidential: bool) -> None:
    """Flag + I2 penalty. For confidential reports nothing may point at the reporter (C2)."""
    inj = ctx.inj
    if not inj or not inj.found:
        return
    detail = "Instruction-like text ignored: " + "; ".join(f'"{p}"' for p in inj.phrases)
    ctx.injection_flag = Flag(
        type="injection", detail=detail if not confidential else detail + " (confidential report)",
        user_ids_json="[]" if confidential else json.dumps([ctx.user.id]),
        report_id=None if confidential else ctx.report.id)
    ctx.session.add(ctx.injection_flag)
    if inj.explicit:
        trust.add_event(ctx.session, ctx.user.id, "INJECTION_ATTEMPT",
                        "Explicit system-manipulation text in a report",
                        report_id=None if confidential else ctx.report.id)
        ctx.add("I2: explicit system manipulation -> trust penalty (-10)")
    else:
        ctx.add("I2: polite instruction ignored, no penalty")


# ----------------------------------------------------------------------
# Step 8: safety (S1 / S2)
# ----------------------------------------------------------------------
def analyze_safety(ext: TriageExtraction, stripped: str) -> tuple[bool, str | None, list[str]]:
    lines: list[str] = []
    kind = None
    safety = False
    hit = rules.find_hazard(stripped)
    if hit:
        kind, word = hit
        safety = True
        lines.append(f'S1: hazard word "{word}" found -> CRITICAL')
    if ext.hazard_present:
        ev = (ext.hazard_evidence or "").strip()
        how = rules.evidence_in_text(ev, stripped) if ev else None
        if how:
            safety = True
            if ext.hazard_kind != "none":
                kind = ext.hazard_kind
            lines.append(f'S2: AI hazard evidence "{ev}" verified in the report ({how}) -> CRITICAL')
        elif ev:
            lines.append(f'S2: AI hazard claim rejected: evidence "{ev}" is not in the report '
                         "text (or lies inside an ignored instruction)")
        else:
            lines.append("S2: AI hazard claim rejected: no evidence given")
    return safety, kind, lines


# ----------------------------------------------------------------------
# Step 10: problem type
# ----------------------------------------------------------------------
def decide_problem_type(ext: TriageExtraction, stripped: str, is_safety: bool,
                        kind: str | None) -> tuple[str, str]:
    ai = ext.problem_type
    if is_safety and kind in HAZARD_TO_TYPE:
        t = HAZARD_TO_TYPE[kind]
        return t, f"Type: safety hazard ({kind}) -> {t}"
    rules_t, _kw = rules.keyword_problem_type(stripped)
    if ai not in PROBLEM_TYPES:
        t = rules_t or "other"
        return t, f"Type: AI type '{ai}' is not valid -> rules engine chose {t}"
    if ai in SAFETY_TYPES and not is_safety:
        t = rules_t or "other"
        return t, (f"Type: AI said '{ai}' but no verified hazard evidence -> {t} "
                   "(a report can't declare its own hazard)")
    return ai, f"Type: {ai}"


# ----------------------------------------------------------------------
# Steps 1-7
# ----------------------------------------------------------------------
def process_report(session: Session, user: User, text: str) -> dict:
    text = (text or "").strip()
    if not text:
        raise RuleError("Please describe the problem first.")
    if len(text) > MAX_CHARS:
        raise RuleError(f"Please keep the report under {MAX_CHARS} characters.")

    # 1. raw text is stored once and never modified
    report = Report(user_id=user.id, raw_text=text, created_at=utcnow())
    session.add(report)
    session.flush()
    ctx = Ctx(session, user, report, state=load_state(report))

    # 2. SP1 rate limit
    cutoff = utcnow() - RATE_WINDOW
    recent = session.exec(select(Report).where(Report.user_id == user.id,
                                               Report.created_at >= cutoff,
                                               Report.id != report.id)).all()
    if len(recent) >= RATE_LIMIT:
        return _rate_limited(ctx, cutoff)

    # 3. clean + T1 tone
    cleaned = rules.clean(text)
    tone = rules.detect_tone(text)
    report.cleaned_text, report.tone_flag = cleaned, bool(tone)
    if tone:
        ctx.add(f"T1: tone ignored ({', '.join(tone)})")

    # 4. I1 injection strip (only the instruction phrase, never the whole sentence)
    inj = rules.strip_injection(cleaned)
    ctx.inj = inj
    report.stripped_text, report.injection_flag = inj.stripped, inj.found
    for p in inj.phrases:
        ctx.add(f'I1: instruction ignored: "{p}"')
    if not inj.stripped:  # nothing left but instructions
        ctx.add("V1: the report contained only instructions, no problem")
        _injection_consequences(ctx, confidential=False)
        return _reject_not_report(ctx)

    # 5. AI extraction (one call, via the chain; falls back to rules)
    res = llm.triage_report(cleaned, inj.stripped)
    ext = res.extraction
    ctx.engine, ctx.latency_ms = res.engine, res.latency_ms
    report.engine, report.latency_ms = res.engine, res.latency_ms
    report.extraction_json = ext.model_dump_json()
    ctx.add(res.notes)
    where = f'place "{ext.location_mention}" -> {ext.location_id}' if ext.location_mention \
        else f"place -> {ext.location_id}"
    if res.engine == "rules":
        ctx.add(f"{ext.reason}; type={ext.problem_type}, {where}")
    else:
        speed = "cached" if res.cached else f"{res.latency_ms} ms"
        ctx.add(f"AI ({res.engine}, {speed}): type={ext.problem_type}, {where}")
    if ext.injection_attempt and not inj.found:
        report.injection_flag = True
        ctx.add("I1 (AI): instruction-like text ignored")

    # 6. V1 validity
    hazard_kw = rules.find_hazard(inj.stripped)
    harass_kw = rules.find_harassment(inj.stripped)
    if not ext.is_campus_report and not hazard_kw and not harass_kw:
        ctx.add("V1: not a campus problem report -> no routing, no penalty")
        _injection_consequences(ctx, confidential=False)
        return _reject_not_report(ctx)

    # 7. C1 confidential (never blocks: ASK10)
    if harass_kw or ext.harassment_or_ragging or ext.problem_type == "harassment_ragging":
        why = f'harassment word "{harass_kw}"' if harass_kw else "AI flagged harassment/ragging"
        ctx.add(f"C1: {why} -> CONFIDENTIAL (admin only, reporter hidden)")
        _injection_consequences(ctx, confidential=True)
        return _confidential(ctx, ext)

    _injection_consequences(ctx, confidential=False)
    return _advance(ctx, ext)


def _rate_limited(ctx: Ctx, cutoff) -> dict:
    s, user, report = ctx.session, ctx.user, ctx.report
    report.status = "rejected_rate_limit"
    ctx.add(f"SP1: {RATE_LIMIT}+ reports in {int(RATE_WINDOW.total_seconds() // 60)} minutes "
            "-> rejected")
    already = s.exec(select(TrustEvent).where(TrustEvent.user_id == user.id,
                                               TrustEvent.code == "SPAM_RATE",
                                               TrustEvent.created_at >= cutoff)).first()
    if not already:  # penalty and flag once per window
        trust.add_event(s, user.id, "SPAM_RATE", "Sent too many reports in a short time",
                        report_id=report.id)
        s.add(Flag(type="spam", user_ids_json=json.dumps([user.id]), report_id=report.id,
                   detail=f"{RATE_LIMIT}+ reports in {int(RATE_WINDOW.total_seconds() // 60)} minutes"))
        ctx.add("SP1: spam flag created, trust -5")
    _save(ctx)
    return _result(ctx, "You are sending reports very quickly. Please wait a few minutes "
                        "before sending another one.")


def _reject_not_report(ctx: Ctx) -> dict:
    ctx.report.status = "rejected_not_report"
    _save(ctx)
    return _result(ctx, NOT_A_REPORT)


# ----------------------------------------------------------------------
# Confidential flow (C1-C3, ASK10): no grouping, never blocked by a missing place
# ----------------------------------------------------------------------
def _confidential(ctx: Ctx, ext: TriageExtraction) -> dict:
    s, report = ctx.session, ctx.report
    res = locations.resolve_location(report.stripped_text, ext.location_id,
                                     ext.location_mention, "harassment_ragging")
    loc = res.place_id if res.status == "resolved" else None
    ctx.add(res.log if loc else "ASK10: a place is optional for confidential reports")
    dept, rule, why = taxonomy.route("harassment_ragging", True, None, "NONE")
    ctx.add(f"{rule}: {why}")
    ctx.add("C3: excluded from the public board, grouping, crowd priority and the AI summary")

    issue = Issue(title=ext.summary_en or "Confidential report", department=dept,
                  location_id=loc, problem_type="harassment_ragging", base_impact=0,
                  is_safety=False, confidential=True, status="open",
                  decision_log_json=json.dumps(ctx.log))
    s.add(issue)
    s.flush()
    grouping.add_member(s, issue, ctx.user, "report")
    s.add(AuditEvent(issue_id=issue.id, actor_id=None, action="reported", to_status="open",
                     note="Confidential report received"))
    priority.recompute_priority(s, issue)
    report.status, report.issue_id, report.location_id = "confidential", issue.id, loc
    if not loc:  # ASK10: one OPTIONAL place question; skipping is fine, never a name
        ctx.state["optional"] = True
        report.clarification_question = clarify.OPTIONAL_PLACE_QUESTION
        report.suggestions_json = json.dumps({
            "kind": "place", "buttons": [], "dropdown": locations.dropdown_groups(),
            "free_text": True, "pick_only": False, "admin_decide": False, "cancel": False,
            "skip": True})
        ctx.state["turns"].append({"kind": "place", "reason": "optional", "mention": None,
                                   "candidates": [], "question": clarify.OPTIONAL_PLACE_QUESTION,
                                   "source": "template", "answer": None})
    _save(ctx)
    return _result(ctx, CONFIDENTIAL_NOTICE, confidential=True)


# ----------------------------------------------------------------------
# Steps 8-14 (and every re-run after an answer)
# ----------------------------------------------------------------------
def _advance(ctx: Ctx, ext: TriageExtraction) -> dict:
    s, report, st = ctx.session, ctx.report, ctx.state
    facts = st["facts"]
    all_text = " ".join([report.stripped_text, *st["extra_stripped"]]).strip()

    # 8. safety (also looks at the student's answers)
    is_safety, kind, lines = analyze_safety(ext, all_text)
    ctx.add(lines)

    # 10 (computed first: the portal/LMS exception in L6 needs the type)
    if facts["ptype"]:
        ptype, type_line = facts["ptype"], f"Type: {facts['ptype']} (from the student's answer)"
    else:
        ptype, type_line = decide_problem_type(ext, all_text, is_safety, kind)
    problem_clear = bool(is_safety or facts["problem_clear"] or ext.problem_clear
                         or ptype != "other")

    # 9. completeness: the place
    res: Resolution | None
    if facts["place"]:
        res = Resolution("resolved", facts["place"], None, log=[])
    elif facts["admin_decide"]:
        res = None
    else:
        res = locations.resolve_location(report.stripped_text, ext.location_id,
                                         ext.location_mention, ptype, ext.other_place_mentions)
        ctx.add(res.log)
    if res and res.place_id == locations.online_place_id() and ptype != "portal_lms_email":
        raise RuleError("The online service is only valid for portal, LMS and email problems.")

    gap = clarify.first_gap(ptype, problem_clear, res)
    if facts["admin_decide"]:
        gap = None
    for kind_name in ("place", "problem"):  # an item that is now known starts fresh
        if gap is None or gap.kind != kind_name:
            st["attempts"][kind_name] = 0

    if gap:
        if is_safety and gap.kind == "place":
            _ensure_emergency(ctx, ext, ptype, gap)  # ASK9: safety never waits
        return _ask(ctx, ext, gap, ptype, res.place_id if res else None, is_safety)

    ctx.add(type_line)
    loc = res.place_id if res else None
    return _finish(ctx, ext, ptype, loc, is_safety)


def _ask(ctx: Ctx, ext: TriageExtraction, gap: clarify.Gap, ptype: str, place_id: str | None,
         is_safety: bool) -> dict:
    """ASK3/ASK5/ASK6/ASK8: one question, with options built from campus.json."""
    report, st = ctx.report, ctx.state
    prev = st["turns"][-1] if st["turns"] else None
    same_item = bool(prev and prev["kind"] == gap.kind and prev["reason"] == gap.reason
                     and prev.get("answer"))
    if same_item:  # the last answer did not resolve this item
        st["attempts"][gap.kind] += 1
    pick_only = st["attempts"][gap.kind] >= 2  # ASK8
    q, source = clarify.build_question(gap, ptype, locations.place_name(place_id), ext,
                                       bool(report.injection_flag), report.engine, pick_only)
    if prev and prev["question"] == q:  # never the identical question twice in a row
        q = "I didn't catch that. / Samajh nahi aaya. " + q
    options = clarify.build_options(gap, ptype, place_id, pick_only, allow_cancel=not is_safety)
    what = f" ('{gap.mention}')" if gap.mention else ""
    ctx.add(f"ASK2: {gap.kind} {gap.reason}{what} -> asked '{q}'"
            + (" [buttons only: ASK8]" if pick_only else ""))
    st["turns"].append({"kind": gap.kind, "reason": gap.reason, "mention": gap.mention,
                        "candidates": gap.candidates, "question": q, "source": source,
                        "answer": None})
    report.status = "needs_clarification"
    report.clarification_question = q
    report.suggestions_json = json.dumps(options)
    _save(ctx)
    msg = q
    if is_safety:
        msg = ("Hazard reported as CRITICAL: the admin has been alerted right away. " + q)
    return _result(ctx, msg, emergency=is_safety)


def _ensure_emergency(ctx: Ctx, ext: TriageExtraction, ptype: str, gap: clarify.Gap) -> None:
    """ASK9: create the CRITICAL issue in UNASSIGNED now, place unverified."""
    s, report = ctx.session, ctx.report
    if report.issue_id:
        return
    mention = (gap.mention or "unknown place").strip()[:40]
    issue = Issue(title=_title(ext, ptype, None), department="UNASSIGNED",
                  location_id=f"{UNVERIFIED}: {mention}", problem_type=ptype,
                  base_impact=taxonomy.default_impact(ptype), is_safety=True, status="open")
    s.add(issue)
    s.flush()
    grouping.add_member(s, issue, ctx.user, "report")
    s.add(AuditEvent(issue_id=issue.id, actor_id=ctx.user.id, action="reported",
                     to_status="open",
                     note="ASK9: hazard reported, place not verified yet. Sent to the admin"))
    priority.recompute_priority(s, issue)
    ctx.add("ASK9: hazard with an unverified place -> CRITICAL issue created now in UNASSIGNED "
            f"(place \"UNVERIFIED: {mention}\"); still asking the student where it is")
    ctx.add(json.loads(issue.priority_reasons_json))
    issue.decision_log_json = json.dumps(ctx.log)
    report.issue_id = issue.id
    s.add(issue)


def _title(ext: TriageExtraction, ptype: str, loc: str | None) -> str:
    title = (ext.summary_en or "").strip() or ptype.replace("_", " ").capitalize()
    name = locations.place_name(loc)
    if name and name.lower() not in title.lower() and locations.get_place(loc).type not in (
            "online", "campus"):
        title = f"{title} ({name})"
    return title


def _finish(ctx: Ctx, ext: TriageExtraction, ptype: str, loc: str | None,
            is_safety: bool) -> dict:
    """Steps 11-14 once the report is complete."""
    s, report, st = ctx.session, ctx.report, ctx.state
    st["optional"] = False
    report.clarification_question = None
    report.suggestions_json = "{}"
    report.location_id = loc

    # 11. routing (decided in code from campus.json; a report never chooses its department)
    if loc is None:  # ASK8: the student asked the admin to decide
        dept, rule, why = "UNASSIGNED", "ASK8", "student asked the admin to decide -> UNASSIGNED"
        ctx.add("ASK8: student asked the admin to decide; code never guesses a department")
    else:
        dept, rule, why = taxonomy.route(ptype, False, loc, ext.suggested_department)
        ctx.add(f"{rule}: {why}")
    sugg = ext.suggested_department
    if sugg == "NONE":
        ctx.add(f"R5: AI had no department suggestion; rule chose {dept}")
    elif sugg != dept:
        ctx.add(f"R5: AI suggested {sugg}, rule chose {dept}")
    else:
        ctx.add(f"R5: AI suggested {sugg}, same as the rule")

    # ASK9: an emergency issue already exists -> move it to the owner department
    if report.issue_id:
        return _rehome(ctx, ext, ptype, loc, dept, is_safety)

    if loc is None:  # nothing to group on: a new UNASSIGNED issue
        return _new_issue(ctx, ext, ptype, dept, None, is_safety)

    # 12. grouping
    issue, gline = grouping.find_matching_issue(s, loc, ptype, ext.summary_en)
    ctx.add(gline)
    if issue:
        return _merge(ctx, issue, ext, is_safety)
    return _new_issue(ctx, ext, ptype, dept, loc, is_safety)


def _rehome(ctx: Ctx, ext, ptype: str, loc: str | None, dept: str, is_safety: bool) -> dict:
    s, report = ctx.session, ctx.report
    issue = s.get(Issue, report.issue_id)
    frm = issue.department
    issue.department = dept
    if loc:
        issue.location_id = loc
    issue.problem_type = ptype
    issue.title = _title(ext, ptype, loc) if loc else issue.title
    s.add(AuditEvent(issue_id=issue.id, actor_id=None, action="place_confirmed",
                     note=f"ASK9: place confirmed ({_place_label(loc)}); moved {frm} -> {dept}"))
    priority.recompute_priority(s, issue)
    ctx.add(f"ASK9: place confirmed ({_place_label(loc)}) -> issue #{issue.id} moved "
            f"from {frm} to {dept}")
    issue.decision_log_json = json.dumps(ctx.log)
    report.status = "routed_new"
    s.add(issue)
    _save(ctx)
    level = LEVEL_NAMES[issue.priority_level]
    return _result(ctx, f"Sent to {dept} · {_place_label(loc)} · Priority {level} · "
                        "the place is confirmed", department=dept, priority=level,
                   is_safety=issue.is_safety, issue_title=issue.title)


def _new_issue(ctx: Ctx, ext, ptype: str, dept: str, loc: str | None, is_safety: bool) -> dict:
    s, report = ctx.session, ctx.report
    base, _ = priority.base_level(ptype, ext.scope, ext.blocks_essential_activity)
    issue = Issue(title=_title(ext, ptype, loc), department=dept, location_id=loc,
                  problem_type=ptype, base_impact=min(base, 4), is_safety=is_safety,
                  status="open", decision_log_json="[]")
    s.add(issue)
    s.flush()
    grouping.add_member(s, issue, ctx.user, "report")
    s.add(AuditEvent(issue_id=issue.id, actor_id=ctx.user.id, action="reported",
                     to_status="open", note=f"Issue created and sent to {dept}"))
    priority.recompute_priority(s, issue)
    ctx.add(json.loads(issue.priority_reasons_json))
    issue.decision_log_json = json.dumps(ctx.log)
    if ctx.injection_flag:
        ctx.injection_flag.issue_id = issue.id
    report.status, report.issue_id = "routed_new", issue.id
    _save(ctx)
    level = LEVEL_NAMES[issue.priority_level]
    where = _place_label(loc) if loc else "no place given"
    msg = f"Sent to {dept} · {where} · Priority {level} · {ext.reason}"
    return _result(ctx, msg, department=dept, priority=level, is_safety=is_safety,
                   issue_title=issue.title)


def _merge(ctx: Ctx, issue: Issue, ext, is_safety: bool) -> dict:
    s, user, report = ctx.session, ctx.user, ctx.report
    report.issue_id, report.status = issue.id, "merged"
    if grouping.get_member(s, issue.id, user.id):  # G3: count does not change
        ctx.add("G3: you already reported or joined this issue, count unchanged")
        _save(ctx)
        return _result(ctx, "You already reported or joined this issue.", merged=True,
                       already_member=True, issue_title=issue.title)

    after_resolve = issue.status == "resolved"
    grouping.add_member(s, issue, user, "report_after_resolve" if after_resolve else "report")
    n = grouping.member_count(s, issue.id)
    extra = ""
    if is_safety and not issue.is_safety:  # is_safety is sticky
        issue.is_safety = True
        ctx.add("S1: this report adds a safety hazard to the issue -> CRITICAL (sticky)")
    base, _ = priority.base_level(issue.problem_type, ext.scope, ext.blocks_essential_activity)
    if base > issue.base_impact and not issue.is_safety:
        issue.base_impact = min(base, 4)
    s.add(AuditEvent(issue_id=issue.id, actor_id=None, action="joined",
                     note=f"Another student reported the same problem ({n} students now)"))
    if after_resolve:  # G4: reporting again after "resolved" means it is still broken
        voting.cast_vote(s, issue, user, "dispute")
        ctx.add("G4: reported after the issue was marked resolved -> counts as a dispute of the fix")
        extra = " Your report counts as a dispute of the fix."
        if issue.status == "reopened":
            extra += " The issue was reopened."
    priority.recompute_priority(s, issue)
    ctx.add(json.loads(issue.priority_reasons_json))
    issue_log = json.loads(issue.decision_log_json)
    issue_log.append(f"G1: student #{n} joined via a new report; priority now "
                     f"{LEVEL_NAMES[issue.priority_level]}")
    issue.decision_log_json = json.dumps(issue_log)
    s.add(issue)
    _save(ctx)
    msg = f"Merged into issue #{issue.id} '{issue.title}', you're student #{n}.{extra}"
    return _result(ctx, msg, merged=True, issue_title=issue.title,
                   department=issue.department, priority=LEVEL_NAMES[issue.priority_level])


# ----------------------------------------------------------------------
# ASK7: processing one answer
# ----------------------------------------------------------------------
def answer_report(session: Session, user: User, report_id: int, payload: dict) -> dict:
    report = session.get(Report, report_id)
    if not report or report.user_id != user.id:
        raise PermissionError("That report does not belong to you.")
    st = load_state(report)
    ext = TriageExtraction.model_validate_json(report.extraction_json)
    ctx = Ctx(session, user, report, log=json.loads(report.decision_log_json), state=st,
              engine=report.engine, latency_ms=report.latency_ms)
    if report.status == "confidential":
        return _answer_optional(ctx, payload)
    if report.status != "needs_clarification":
        raise RuleError("This report is not waiting for an answer.")

    turn = st["turns"][-1] if st["turns"] else {"kind": "place", "reason": "missing",
                                                 "candidates": [], "question": ""}
    gap = clarify.Gap(turn["kind"], turn["reason"], turn.get("mention"),
                      list(turn.get("candidates", [])))
    facts = st["facts"]
    choice = (payload.get("choice") or "").strip()
    text = (payload.get("text") or "").strip()
    if len(text) > MAX_CHARS:
        raise RuleError(f"Please keep the answer under {MAX_CHARS} characters.")

    # ---- buttons / dropdown (no AI call, no guessing) ----
    if choice == "cancel":
        if report.issue_id:
            raise RuleError("Hazard reports can't be cancelled: the admin has already been "
                            "alerted. Please answer the place question, or pick 'let the "
                            "admin decide'.")
        turn["answer"] = {"text": "Cancel report", "how": "button"}
        report.status = "cancelled"
        report.clarification_question = None
        ctx.add("ASK8: student cancelled the report (no trust change)")
        _save(ctx)
        return _result(ctx, "Report cancelled.")
    if choice == "admin_decide":
        turn["answer"] = {"text": "Not sure, let the admin decide", "how": "button"}
        facts["admin_decide"] = True
        ctx.add("ASK8: student asked the admin to decide")
        return _advance(ctx, ext)

    shown, how, outcome = "", "button", []
    if payload.get("location_id"):
        pid = payload["location_id"]
        place = locations.get_place(pid)
        if not place:
            raise RuleError("Please pick a place from the campus list.")
        facts["place"] = pid
        shown, outcome = place.name, [f"{pid}"]
    if payload.get("problem_type"):
        pt = payload["problem_type"]
        if pt not in PROBLEM_TYPES or pt == "harassment_ragging":
            raise RuleError("Please pick a problem type from the list.")
        facts["ptype"], facts["problem_clear"] = pt, True
        shown = shown or taxonomy.label(pt)
        outcome.append(pt)
    if text and not outcome:
        how = "code"
        shown = text
        outcome = _understand_text(ctx, ext, gap, text)
        if outcome and outcome[0] == "ai":
            how, outcome = "ai", outcome[1:]
        elif not outcome:
            how = "none"
    elif not outcome:
        raise RuleError("Please answer the question, pick an option, or choose 'let the admin "
                        "decide'.")
    turn["answer"] = {"text": shown, "how": how}
    ctx.add(f"ASK7: student answered '{shown[:80]}' ({how}) -> "
            + (", ".join(outcome) if outcome else "not understood"))
    return _advance(ctx, ext)


def _understand_text(ctx: Ctx, ext: TriageExtraction, gap: clarify.Gap, text: str) -> list[str]:
    """Free-text answer: untrusted data. Code first; ONE AI call only if the code can't read it."""
    s, report, st = ctx.session, ctx.report, ctx.state
    facts = st["facts"]
    cleaned = rules.clean(text)
    tone = rules.detect_tone(text)
    if tone:
        ctx.add(f"T1: tone ignored in the answer ({', '.join(tone)})")
    inj = rules.strip_injection(cleaned)
    ctx.inj = inj
    for p in inj.phrases:
        ctx.add(f'I1: instruction ignored in the answer: "{p}"')
    if inj.found:
        report.injection_flag = True
        _injection_consequences(ctx, confidential=False)
        if ctx.injection_flag:
            ctx.injection_flag.issue_id = report.issue_id
    st["extra_stripped"].append(inj.stripped)

    u = clarify.understand(inj.stripped, gap)
    out: list[str] = []
    if u["place"]:
        facts["place"] = u["place"]
        out.append(u["place"])
    if u["ptype"]:
        facts["ptype"], facts["problem_clear"] = u["ptype"], True  # also a correction
        out.append(u["ptype"])
    if out or not inj.stripped.strip():
        return out

    # the code could not read it: ONE AI call with everything, inside the untrusted <report>
    turns = " ".join(f"question: {t['question']} answer: {(t.get('answer') or {}).get('text', '')}"
                     for t in st["turns"])
    combined = rules.clean(f"{report.cleaned_text} {turns} answer: {cleaned}")
    res = llm.triage_report(combined, inj.stripped)
    if res.engine == "rules":
        return []
    ctx.engine, ctx.latency_ms = res.engine, res.latency_ms
    ctx.add(res.notes)
    ctx.add(f"AI ({res.engine}, {'cached' if res.cached else str(res.latency_ms) + ' ms'}): "
            "read the answer (the code still validates it)")
    new = res.extraction
    got: list[str] = ["ai"]
    if new.problem_type in PROBLEM_TYPES and new.problem_type not in ("other", "harassment_ragging") \
            and new.problem_clear:
        facts["ptype"], facts["problem_clear"] = new.problem_type, True
        got.append(new.problem_type)
    pr = locations.resolve_location(inj.stripped, new.location_id, new.location_mention,
                                    facts["ptype"] or "other")
    if pr.status == "resolved":  # L2: the AI's pick is checked against the answer text
        facts["place"] = pr.place_id
        got.append(pr.place_id)
    return got if len(got) > 1 else []


def _answer_optional(ctx: Ctx, payload: dict) -> dict:
    """ASK10: the optional place question of a confidential report."""
    s, report, st = ctx.session, ctx.report, ctx.state
    issue = s.get(Issue, report.issue_id)
    choice = (payload.get("choice") or "").strip()
    text = (payload.get("text") or "").strip()
    loc = None
    if payload.get("location_id") and locations.get_place(payload["location_id"]):
        loc = payload["location_id"]
    elif text and choice != "skip":
        res = locations.resolve_location(rules.clean(text), "NONE", None, "harassment_ragging")
        loc = res.place_id if res.status == "resolved" else None
    if st["turns"]:
        st["turns"][-1]["answer"] = {"text": "(skipped)" if not loc else _place_label(loc),
                                     "how": "button"}
    if loc and issue:
        issue.location_id = loc
        report.location_id = loc
        ctx.add(f"ASK10: optional place given -> {loc}")
        s.add(issue)
    else:
        ctx.add("ASK10: place skipped, which is fine for a confidential report")
    st["optional"] = False
    report.clarification_question = None
    report.suggestions_json = "{}"
    _save(ctx)
    return _result(ctx, CONFIDENTIAL_NOTICE, confidential=True)
