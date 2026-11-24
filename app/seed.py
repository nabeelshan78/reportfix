"""Demo users, trust histories and issues (CLAUDE.md section 13). NO AI calls."""
import json
from datetime import timedelta

from sqlmodel import Session, select

from app.community.priority import recompute_priority
from app.community.trust import add_event
from app.models import (AuditEvent, Issue, IssueMember, Report, User, utcnow)
from app.triage import clarify

# (name, ledger as {code: count}); trust must come from real ledger events.
STUDENTS = [
    ("Ayesha Khan", {"REPORT_CONFIRMED": 5, "VOTE_CORRECT": 3}),      # 81
    ("Omar Farooq", {"REPORT_CONFIRMED": 3, "VOTE_CORRECT": 4}),      # 73
    ("Fatima Noor", {"REPORT_CONFIRMED": 2, "VOTE_CORRECT": 5}),      # 70
    ("Sara Malik", {"REPORT_CONFIRMED": 2, "VOTE_CORRECT": 3}),       # 66
    ("Hamza Ali", {"REPORT_CONFIRMED": 2, "VOTE_CORRECT": 1}),        # 62
    ("Zainab Raza", {"REPORT_CONFIRMED": 1, "VOTE_CORRECT": 2}),      # 59
    ("Usman Tariq", {"REPORT_ACKNOWLEDGED": 1, "VOTE_CORRECT": 1}),   # 54
    ("Ali Hassan", {}),                                               # 50
    ("Hira Shah", {"SPAM_RATE": 1}),                                  # 45
    ("Bilal Ahmed", {}),  # 35: FAKE_REPORT is added with his closed_fake issue
]
STAFF = [
    ("Engr. Kamran", "EE"), ("Ms. Sana", "CSE"), ("Mr. Adeel", "IS"),
    ("Ms. Rabia", "Library"), ("Mr. Javed", "Cafeteria"), ("Mr. Imran", "Labs"),
]



def _extraction(summary, ptype, mention, loc_id, *, lang="mixed", scope="room_or_class",
                blocks=False, hazard="none", evidence="", harass=False, dept="NONE",
                reason="", ask="none") -> str:
    return json.dumps({
        "is_campus_report": True, "language": lang, "summary_en": summary,
        "problem_type": ptype, "location_mention": mention, "location_id": loc_id,
        "hazard_present": hazard != "none", "hazard_kind": hazard,
        "hazard_evidence": evidence, "harassment_or_ragging": harass, "scope": scope,
        "blocks_essential_activity": blocks, "suggested_department": dept,
        "injection_attempt": False, "reason": reason, "problem_clear": True,
        "other_place_mentions": [], "question_about": ask, "clarifying_question": "",
    })


def seed(session: Session) -> None:
    now = utcnow()
    old = now - timedelta(days=30)

    # ---- users ----
    users: dict[str, User] = {}
    for name, events in STUDENTS:
        u = User(name=name, role="student", created_at=old, is_seed=True)
        session.add(u)
        session.flush()
        users[name] = u
        for code, n in events.items():
            for _ in range(n):
                add_event(session, u.id, code, f"Seed history: {code}")
    for name, dept in STAFF:
        u = User(name=name, role="staff", department=dept, created_at=old, is_seed=True)
        session.add(u)
        session.flush()
        users[name] = u
    admin = User(name="Registrar Office", role="admin", created_at=old, is_seed=True)
    session.add(admin)
    session.flush()
    session.commit()  # trust ledger must be readable for weights below

    def make_issue(*, title, dept, loc, ptype, base, status, created_ago,
                   members, reports, extraction, log, safety=False,
                   confidential=False, round_=0, resolved_ago=None, audit=()):
        """members: [(student name, via)]; reports: [(student name, raw text)]."""
        created = now - created_ago
        issue = Issue(
            title=title, department=dept, location_id=loc, problem_type=ptype,
            base_impact=base, is_safety=safety, confidential=confidential, status=status,
            decision_log_json=json.dumps(log), round=round_,
            resolved_at=(now - resolved_ago) if resolved_ago else None,
            created_at=created, updated_at=now - (resolved_ago or created_ago))
        session.add(issue)
        session.flush()
        for i, (n, via) in enumerate(members):
            session.add(IssueMember(issue_id=issue.id, user_id=users[n].id, via=via,
                                    joined_at=created + timedelta(minutes=5 * i)))
        for n, raw in reports:
            session.add(Report(
                user_id=users[n].id, raw_text=raw, cleaned_text=raw.lower(),
                stripped_text=raw.lower(), extraction_json=extraction, engine="seed",
                status="confidential" if confidential else "routed_new",
                issue_id=issue.id, location_id=loc,
                decision_log_json=json.dumps(log), created_at=created))
        session.add(AuditEvent(issue_id=issue.id, actor_id=users[reports[0][0]].id,
                               action="reported", to_status="open",
                               note="Issue created from a student report",
                               created_at=created))
        for actor, action, frm, to, note, ago in audit:
            session.add(AuditEvent(issue_id=issue.id, actor_id=users[actor].id,
                                   action=action, from_status=frm, to_status=to,
                                   note=note, created_at=now - ago))
        session.flush()
        recompute_priority(session, issue)  # the real P1-P5 engine, not a copy
        log.extend(json.loads(issue.priority_reasons_json))
        issue.decision_log_json = json.dumps(log)
        for r in session.exec(select(Report).where(Report.issue_id == issue.id)):
            r.decision_log_json = issue.decision_log_json
        session.flush()
        return issue

    H = timedelta(hours=1)
    M = timedelta(minutes=1)

    # 1. Lab 3 WiFi: routed by WHERE it is (Lab 3 belongs to CSE), crowd boost W ~ 5.0 -> HIGH
    make_issue(
        title="Lab 3 WiFi not working", dept="CSE", loc="LAB-3", ptype="wifi_internet",
        base=2, status="open", created_ago=3 * H,
        members=[("Ayesha Khan", "report"), ("Hamza Ali", "report"),
                 ("Usman Tariq", "report"), ("Zainab Raza", "report")],
        reports=[("Ayesha Khan", "lab 3 ka wifi subah se nai chal rha"),
                 ("Hamza Ali", "Internet is down in Lab-3, can't submit my assignment"),
                 ("Usman Tariq", "lab3 me net bht slow hai yaar"),
                 ("Zainab Raza", "lab 3 mein wifi kaam nahi kar raha")],
        extraction=_extraction("WiFi is not working in Lab 3", "wifi_internet",
                               "lab 3", "LAB-3", dept="CSE", reason="WiFi problem in Lab 3"),
        log=["AI (seed data, 0 ms): type=wifi_internet, place \"lab 3\" -> LAB-3",
             "R3: Lab 3 belongs to CSE (campus.json) -> CSE",
             "R5: AI suggested CSE, same as the rule",
             "G1: 4 reports matched on (LAB-3, wifi_internet) -> one issue"])

    # 2. Library AC (the Library owns its rooms; resolved 20 minutes ago, round 1, no votes yet)
    make_issue(
        title="Library AC not cooling", dept="Library", loc="LIB-MAIN", ptype="ac_not_working",
        base=2, status="resolved", created_ago=5 * H, round_=1, resolved_ago=20 * M,
        members=[("Fatima Noor", "report"), ("Omar Farooq", "me_too"),
                 ("Sara Malik", "me_too"), ("Ali Hassan", "me_too")],
        reports=[("Fatima Noor", "library ka AC thanda nahi kar raha, bohat garmi hai")],
        extraction=_extraction("AC is not cooling in the library", "ac_not_working",
                               "library", "LIB-MAIN", dept="Library", reason="AC problem"),
        log=["AI (seed data, 0 ms): type=ac_not_working, place \"library\" -> LIB-MAIN",
             "R3: Library belongs to Library (campus.json) -> Library",
             "R5: AI suggested Library, same as the rule"],
        audit=[("Ms. Rabia", "status_change", "open", "in_progress",
                "Technician assigned", 3 * H),
               ("Ms. Rabia", "status_change", "in_progress", "resolved",
                "Filter cleaned and gas topped up", 20 * M)])

    # 3. Cockroach in food (special route R1: food goes to the Cafeteria wherever it is)
    make_issue(
        title="Cockroach found in cafeteria food", dept="Cafeteria", loc="CAF-MAIN",
        ptype="food_hygiene", base=2, status="open", created_ago=2 * H,
        members=[("Omar Farooq", "report")],
        reports=[("Omar Farooq", "cafeteria ke khane mein keera nikla, a cockroach in my rice")],
        extraction=_extraction("Cockroach found in cafeteria food", "food_hygiene",
                               "cafeteria", "CAF-MAIN", dept="Cafeteria",
                               reason="food hygiene"),
        log=["AI (seed data, 0 ms): type=food_hygiene, place \"cafeteria\" -> CAF-MAIN",
             "R1: food hygiene is a special route in campus.json -> Cafeteria",
             "R5: AI suggested Cafeteria, same as the rule"])

    # 4. Projector in Lab 5 (Lab 5 belongs to CSE; in progress)
    make_issue(
        title="Projector not working in Lab 5", dept="CSE", loc="LAB-5",
        ptype="projector_not_working", base=2, status="in_progress", created_ago=4 * H,
        members=[("Hamza Ali", "report")],
        reports=[("Hamza Ali", "Lab 5 ka projector kharab hai, class nahi ho pa rahi")],
        extraction=_extraction("Projector is not working in Lab 5", "projector_not_working",
                               "lab 5", "LAB-5", dept="CSE", reason="projector fault"),
        log=["AI (seed data, 0 ms): type=projector_not_working, place \"lab 5\" -> LAB-5",
             "R3: Lab 5 belongs to CSE (campus.json) -> CSE",
             "R5: AI suggested CSE, same as the rule"],
        audit=[("Ms. Sana", "status_change", "open", "in_progress",
                "Bulb replacement ordered", 1 * H)])

    # 5. Calm safety issue (E-203 belongs to EE)
    make_issue(
        title="Spark from socket in E-203", dept="EE", loc="E-203",
        ptype="sparks_or_exposed_wiring", base=4, status="open", created_ago=1 * H,
        safety=True, members=[("Usman Tariq", "report")],
        reports=[("Usman Tariq", "there's a little spark from the socket in E-203 "
                                 "whenever someone plugs in a charger, probably nothing")],
        extraction=_extraction("Sparks from a socket in E-203", "sparks_or_exposed_wiring",
                               "E-203", "E-203", lang="english", hazard="sparks_electrical",
                               evidence="a little spark from the socket", dept="EE",
                               reason="sparking socket is an electrical hazard"),
        log=["AI (seed data, 0 ms): type=sparks_or_exposed_wiring, place \"E-203\" -> E-203",
             "S1: hazard word \"spark\" found -> CRITICAL",
             "T1: calm wording (\"probably nothing\") ignored: hazards count anyway",
             "R3: Room E-203 belongs to EE (campus.json) -> EE"])

    # 6. Confidential ragging case (Sara, shown as anonymous)
    make_issue(
        title="Ragging of first-years near CSE Block", dept="CONFIDENTIAL", loc="CSE-BLOCK",
        ptype="harassment_ragging", base=1, status="open", created_ago=6 * H,
        confidential=True, members=[("Sara Malik", "report")],
        reports=[("Sara Malik", "some seniors keep ragging first-years near the CSE Block "
                                "every evening, please keep my name hidden")],
        extraction=_extraction("Seniors ragging first-years near CSE Block",
                               "harassment_ragging", "CSE Block", "CSE-BLOCK",
                               lang="english", harass=True, reason="ragging report"),
        log=["AI (seed data, 0 ms): type=harassment_ragging, place \"CSE Block\" -> CSE-BLOCK",
             "C1: harassment/ragging -> CONFIDENTIAL (admin only, reporter hidden)",
             "R0: confidential report -> CONFIDENTIAL (admin only)"])

    # 7. Bilal's fake report (explains his FAKE_REPORT -15)
    fake = make_issue(
        title="Library is completely flooded", dept="Library", loc="LIB-MAIN",
        ptype="water_leak", base=2, status="closed_fake", created_ago=8 * H,
        members=[("Bilal Ahmed", "report")],
        reports=[("Bilal Ahmed", "Library is completely flooded")],
        extraction=_extraction("Library is flooded", "water_leak", "library", "LIB-MAIN",
                               lang="english", dept="Library", reason="water problem"),
        log=["AI (seed data, 0 ms): type=water_leak, place \"library\" -> LIB-MAIN",
             "R3: Library belongs to Library (campus.json) -> Library"],
        audit=[("Ms. Rabia", "mark_fake", "open", "closed_fake",
                "Checked on site: no water anywhere in the library", 7 * H)])
    add_event(session, users["Bilal Ahmed"].id, "FAKE_REPORT",
              "Staff marked 'Library is completely flooded' as fake", issue_id=fake.id)

    # 8. An IS issue, so the IS staff queue is not empty (the Cyber Security Lab belongs to IS)
    make_issue(
        title="AC not cooling in the Cyber Security Lab", dept="IS", loc="IS-CYBER",
        ptype="ac_not_working", base=2, status="open", created_ago=90 * M,
        members=[("Zainab Raza", "report"), ("Hira Shah", "me_too")],
        reports=[("Zainab Raza", "cyber lab ka AC thanda nahi kar raha")],
        extraction=_extraction("AC is not cooling in the Cyber Security Lab", "ac_not_working",
                               "cyber lab", "IS-CYBER", dept="IS", reason="AC problem"),
        log=["AI (seed data, 0 ms): type=ac_not_working, place \"cyber lab\" -> IS-CYBER",
             "R3: Cyber Security Lab belongs to IS (campus.json) -> IS",
             "R5: AI suggested IS, same as the rule"])

    # 9. An UNASSIGNED issue (Parking has no owner), so the admin queue has an example
    make_issue(
        title="Lights out in the parking", dept="UNASSIGNED", loc="PARKING",
        ptype="lights_not_working", base=2, status="open", created_ago=45 * M,
        members=[("Fatima Noor", "report")],
        reports=[("Fatima Noor", "parking mein raat ko lights band hain")],
        extraction=_extraction("Lights are out in the parking", "lights_not_working",
                               "parking", "PARKING", dept="EE", reason="lights out outdoors"),
        log=["AI (seed data, 0 ms): type=lights_not_working, place \"parking\" -> PARKING",
             "R4: Parking has no owner (campus.json) -> UNASSIGNED; AI suggested EE",
             "R5: AI suggested EE, rule chose UNASSIGNED"])

    # 10. A report waiting for its place answer (Ali Hassan), to demo the questions (ASK1-ASK3)
    ali = users["Ali Hassan"]
    waiting_ext = _extraction("A projector is broken", "projector_not_working", None, "NONE",
                              lang="roman_urdu", reason="Projector fault, but no place was named",
                              ask="place")
    gap = clarify.Gap("place", "missing")
    question = ("Got it: projector not working. Which room or lab is it in? / "
                "Samajh gaya: projector not working. Kaunsi room ya lab mein?")
    options = clarify.build_options(gap, "projector_not_working", None, False)
    wlog = ["Rules mode: no AI call was made for this seed report",
            "ASK2: no place named -> ask",
            f"ASK2: place missing -> asked '{question}'"]
    state = {"turns": [{"kind": "place", "reason": "missing", "mention": None, "candidates": [],
                        "question": question, "source": "template", "answer": None}],
             "facts": {"place": None, "ptype": None, "problem_clear": None,
                       "admin_decide": False},
             "attempts": {"place": 0, "problem": 0}, "extra_stripped": []}
    session.add(Report(
        user_id=ali.id, raw_text="projector kharab hai", cleaned_text="projector kharab hai",
        stripped_text="projector kharab hai", extraction_json=waiting_ext, engine="seed",
        status="needs_clarification", clarification_question=question,
        suggestions_json=json.dumps(options), clarification_history_json=json.dumps(state),
        decision_log_json=json.dumps(wlog), created_at=now - 10 * M))

    session.commit()
