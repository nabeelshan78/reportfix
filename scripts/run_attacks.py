r"""Run every attack case through the REAL pipeline (live AI) against a temporary database.

  .venv\Scripts\python.exe scripts\run_attacks.py                 # all cases, live AI
  .venv\Scripts\python.exe scripts\run_attacks.py --only 5,9,15   # selected cases
  .venv\Scripts\python.exe scripts\run_attacks.py --limit 10      # the first N cases
  .venv\Scripts\python.exe scripts\run_attacks.py --rules-only    # no AI at all (0 API calls)
  .venv\Scripts\python.exe scripts\run_attacks.py --pace 10       # seconds to wait after each AI call

We are on the FREE Gemini tier, so the runner is gentle: after any case that made a live AI
call it waits --pace seconds (default 7). Answers already in llm_cache.db cost nothing, so
re-running unchanged cases is free. It prints how many AI calls were made and warns when a
model went into cooldown, so a failure caused by quota is not mistaken for a logic bug.
Writes attack_report.md. Never prints API keys.
"""
import argparse
import json
import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# a temporary database: the real reportfix.db is never touched
os.environ["REPORTFIX_DB"] = str(Path(tempfile.mkdtemp()) / "attack_run.db")

CASES_PATH = ROOT / "tests" / "attack_cases.json"
REPORT_PATH = ROOT / "attack_report.md"


def total_calls(chain) -> int:
    return sum(st.calls_today for st in chain.state.values())


def check(expect: dict, res: dict, issue: dict | None, trust_delta: int | None) -> list[str]:
    """Compare one response (plus its issue, as the admin sees it) with the expectations."""
    bad: list[str] = []
    log = "\n".join(res.get("decision_log", []))
    status = expect.get("status")
    if status == "issue":
        if res["status"] not in ("routed_new", "merged"):
            bad.append(f"status {res['status']!r}, wanted an issue (routed_new or merged)")
    elif status and res["status"] != status:
        bad.append(f"status {res['status']!r}, wanted {status!r}")

    def cmp(key, actual):
        if key in expect and actual != expect[key]:
            bad.append(f"{key} is {actual!r}, wanted {expect[key]!r}")

    if issue:
        cmp("department", issue["department"])
        cmp("location_id", issue["location_id"])
        cmp("problem_type", issue["problem_type"])
        cmp("priority", issue["priority_name"])
        cmp("is_safety", issue["is_safety"])
        cmp("confidential", issue["confidential"])
        if "priority_not" in expect and issue["priority_name"] == expect["priority_not"]:
            bad.append(f"priority is {issue['priority_name']}, must NOT be {expect['priority_not']}")
        if "not_department" in expect and issue["department"] == expect["not_department"]:
            bad.append(f"department is {issue['department']}, must NOT be that")
    elif any(k in expect for k in ("department", "location_id", "problem_type", "priority",
                                   "is_safety", "confidential")):
        bad.append("no issue was created, but the case expects one")
    if "injection" in expect and ("I1" in log) != expect["injection"]:
        bad.append(f"injection logged={('I1' in log)}, wanted {expect['injection']}")
    if expect.get("tone_logged") and "T1:" not in log:
        bad.append("tone (T1) was not logged")
    if expect.get("penalized") and trust_delta != -10:
        bad.append(f"trust change is {trust_delta}, wanted -10 (I2)")
    if "already_member" in expect and res.get("already_member") != expect["already_member"]:
        bad.append(f"already_member is {res.get('already_member')}, wanted {expect['already_member']}")
    clar = res.get("clarification") or {}
    opts = clar.get("options") or {}
    if "question_kind" in expect and opts.get("kind") != expect["question_kind"]:
        bad.append(f"question is about {opts.get('kind')!r}, wanted {expect['question_kind']!r}")
    for lid in expect.get("buttons_include", []):
        if lid not in [b.get("location_id") for b in opts.get("buttons", [])]:
            bad.append(f"button for {lid} is missing")
    if "emergency" in expect and bool(res.get("emergency")) != expect["emergency"]:
        bad.append(f"emergency={bool(res.get('emergency'))}, wanted {expect['emergency']}")
    if expect.get("optional_question") and not clar.get("optional"):
        bad.append("no optional place question (ASK10)")
    for needle in expect.get("log_contains", []):
        if needle not in log:
            bad.append(f"log lacks {needle!r}")
    return bad


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pace", type=float, default=7.0, help="seconds to wait after a case that used the AI")
    ap.add_argument("--only", default="", help="comma-separated case ids, e.g. 5,9,15")
    ap.add_argument("--limit", type=int, default=0, help="run only the first N cases")
    ap.add_argument("--rules-only", action="store_true", help="no AI at all (zero API calls)")
    ap.add_argument("--cases", default=str(CASES_PATH))
    args = ap.parse_args()

    cases = json.loads(Path(args.cases).read_text(encoding="utf-8"))["cases"]
    if args.only:
        wanted = {int(x) for x in args.only.split(",") if x.strip()}
        cases = [c for c in cases if c["id"] in wanted]
    if args.limit:
        cases = cases[:args.limit]

    from fastapi.testclient import TestClient

    from app.main import app
    from app.triage import llm

    if args.rules_only:
        # a throwaway cache, so --rules-only never reads answers the AI gave earlier
        llm.set_chain(llm.Chain([], cache=llm.Cache(str(Path(tempfile.mkdtemp()) / 'rules_cache.db'))))
    chain = llm.get_chain()
    mode = "RULES ONLY (no AI)" if args.rules_only else "LIVE AI"
    print(f"Running {len(cases)} cases, mode: {mode}, pace {args.pace}s\n")

    rows, issue_of, students, warned = [], {}, {}, set()
    calls_start = total_calls(chain)
    with TestClient(app) as client:
        client.post("/api/reset")
        users = {u["name"]: u["id"] for u in client.get("/api/users").json()}
        admin = {"X-User-Id": str(users["Registrar Office"])}

        def new_student() -> int:
            return client.post("/api/users/new_student").json()["id"]

        def trust(uid: int) -> int:
            return client.get("/api/me", headers={"X-User-Id": str(uid)}).json()["trust"]

        for case in cases:
            t0 = time.time()
            calls_before = total_calls(chain)
            key = case.get("student") or f"case{case['id']}"
            uid = students.setdefault(key, new_student())
            hdr = {"X-User-Id": str(uid)}
            trust_before = trust(uid)
            failures: list[str] = []
            engine, quota = "?", False
            try:
                r = client.post("/api/reports", json={"text": case["text"]}, headers=hdr).json()
                engine = r.get("engine", "?")
                if "first" in case:
                    first_issue = None
                    if r.get("issue_id"):
                        first_issue = client.get(f"/api/issues/{r['issue_id']}", headers=admin).json()
                    fx = dict(case["first"])
                    f_bad = check(fx, r, first_issue, None)
                    if "issue_priority" in fx and (not first_issue or first_issue["priority_name"] != fx["issue_priority"]):
                        f_bad.append(f"first response: issue priority wanted {fx['issue_priority']}")
                    if "issue_department" in fx and (not first_issue or first_issue["department"] != fx["issue_department"]):
                        f_bad.append(f"first response: issue department wanted {fx['issue_department']}")
                    failures += [f"[first] {m}" for m in f_bad]
                for ans in case.get("answers", []):
                    if r.get("status") not in ("needs_clarification", "confidential"):
                        failures.append("[answers] the report was not waiting for an answer")
                        break
                    out = client.post(f"/api/reports/{r['report_id']}/answer", json=ans, headers=hdr)
                    if out.status_code != 200:
                        failures.append(f"[answer {ans}] HTTP {out.status_code}: {out.json().get('detail')}")
                        break
                    r = out.json()
                    engine = r.get("engine", engine)
                issue = None
                if r.get("issue_id"):
                    issue = client.get(f"/api/issues/{r['issue_id']}", headers=admin).json()
                    issue_of[case["id"]] = r["issue_id"]
                failures += check(case["expect"], r, issue, trust(uid) - trust_before)
                ref = case.get("same_issue_as")
                if ref and ref in issue_of and issue_of[ref] != r.get("issue_id"):
                    failures.append(f"should be the same issue as case {ref} (#{issue_of[ref]}), got #{r.get('issue_id')}")
                ref = case.get("different_issue_from")
                if ref and ref in issue_of and issue_of[ref] == r.get("issue_id"):
                    failures.append(f"should be a DIFFERENT issue from case {ref}")
                log = "\n".join(r.get("decision_log", []))
                quota = any(tag in log for tag in ("Q1:", "Q2:", "API key rejected")) or "failed (" in log
            except Exception as exc:  # a crash is a failure, never a stop
                failures.append(f"crashed: {type(exc).__name__}: {exc}")

            made = total_calls(chain) - calls_before
            ms = int((time.time() - t0) * 1000)
            ok = not failures
            rows.append({"id": case["id"], "group": case.get("group", ""), "ok": ok, "text": case["text"],
                         "failures": failures, "engine": engine, "calls": made, "quota": quota, "ms": ms})
            tag = "PASS" if ok else "FAIL"
            note = (" [quota/AI trouble, fell back]" if quota and not ok else "")
            print(f"{tag}  #{case['id']:<3} {case.get('group', ''):<14} {engine:<22} {ms:>6} ms  {case['text'][:60]}{note}")
            for f in failures:
                print(f"        - {f}")

            for p in chain.status()["providers"]:
                state = p["state"]
                if (state.startswith("cooling") or state == "rate-guarded") and (p["id"], state) not in warned:
                    warned.add((p["id"], state))
                    print(f"  !! WARNING: {p['label']} ({p['model']}) is {state}. Failures from here may be quota, not logic.")
            if made > 0 and not args.rules_only and case is not cases[-1]:
                time.sleep(args.pace)  # stay under the free-tier per-minute limit

    calls = total_calls(chain) - calls_start
    passed = sum(1 for r in rows if r["ok"])
    rate = 100 * passed / max(len(rows), 1)
    engines = {}
    for r in rows:
        engines[r["engine"]] = engines.get(r["engine"], 0) + 1
    print(f"\nPass rate: {passed}/{len(rows)} = {rate:.1f}%   |   live AI calls made: {calls}   |   engines: {engines}")
    by_group: dict[str, list[bool]] = {}
    for r in rows:
        by_group.setdefault(r["group"], []).append(r["ok"])
    print("By group: " + ", ".join(f"{g} {sum(v)}/{len(v)}" for g, v in by_group.items()))
    if warned:
        print("Quota/cooldown was hit: " + ", ".join(f"{a} {b}" for a, b in sorted(warned)))

    lines = [f"# Attack report", "",
             f"- Run: {datetime.now().strftime('%Y-%m-%d %H:%M')}  |  mode: **{mode}**",
             f"- Pass rate: **{passed}/{len(rows)} = {rate:.1f}%**  |  live AI calls: {calls}  |  engines: {engines}",
             "- By group: " + ", ".join(f"{g} {sum(v)}/{len(v)}" for g, v in by_group.items()), ""]
    if warned:
        lines += ["> Quota/cooldown was hit: " + ", ".join(f"{a} {b}" for a, b in sorted(warned)), ""]
    lines += ["| # | group | result | engine | text |", "|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['id']} | {r['group']} | {'PASS' if r['ok'] else 'FAIL'} | {r['engine']} | {r['text'][:70].replace('|', '/')} |")
    fails = [r for r in rows if not r["ok"]]
    if fails:
        lines += ["", "## Failures", ""]
        for r in fails:
            lines.append(f"**#{r['id']}** `{r['text']}`" + ("  (quota/AI trouble)" if r["quota"] else ""))
            lines += [f"- {m}" for m in r["failures"]] + [""]
    REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nWrote {REPORT_PATH.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
