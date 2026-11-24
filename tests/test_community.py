"""Phase 3: grouping/me-too, verification rounds V1-V6, trust effects, staff actions, G4."""
import pytest

from tests.conftest import as_user

# seed weights: Fatima 70 -> 1.40, Omar 73 -> 1.46, Sara 66 -> 1.32, Ali 50 -> 1.00, Ayesha 81 -> 1.5


def new_student(client):
    return client.post("/api/users/new_student").json()["id"]


def post(client, uid, text):
    r = client.post("/api/reports", json={"text": text}, headers=as_user(uid))
    assert r.status_code == 200, r.text
    return r.json()


def get_issue(client, ids, iid, who="Registrar Office"):
    return client.get(f"/api/issues/{iid}", headers=as_user(ids[who])).json()


def find(client, ids, fragment, who="Registrar Office"):
    return next(i for i in client.get("/api/issues", headers=as_user(ids[who])).json()
                if fragment in i["title"])


def vote(client, ids, who, iid, value, expect=200):
    r = client.post(f"/api/issues/{iid}/vote", json={"value": value}, headers=as_user(ids[who]))
    assert r.status_code == expect, r.text
    return r.json()


def trust(client, ids, who):
    return client.get("/api/me", headers=as_user(ids[who])).json()["trust"]


def events(client, ids, who):
    r = client.get(f"/api/users/{ids[who]}/trust", headers=as_user(ids["Registrar Office"]))
    return [e["code"] for e in r.json()["ledger"]]


@pytest.fixture
def ac(client, ids):
    """The seeded 'Library AC' issue: resolved 20 minutes ago, round 1, no votes yet."""
    return find(client, ids, "Library AC")["id"]


# =====================================================================
# V1: who may vote
# =====================================================================
def test_v1_only_members_from_before_the_resolve_can_vote(client, ids, ac):
    for who in ["Fatima Noor", "Omar Farooq", "Sara Malik", "Ali Hassan"]:
        assert get_issue(client, ids, ac, who)["can_vote"] is True, who
    assert get_issue(client, ids, ac, "Ayesha Khan")["can_vote"] is False  # not affected
    vote(client, ids, "Ayesha Khan", ac, "dispute", expect=403)


def test_v1_staff_and_admin_never_vote(client, ids, ac):
    vote(client, ids, "Ms. Rabia", ac, "confirm", expect=403)
    vote(client, ids, "Registrar Office", ac, "confirm", expect=403)


def test_v1_joining_after_the_resolve_does_not_earn_a_vote(client, ids, ac):
    r = client.post(f"/api/issues/{ac}/metoo", headers=as_user(ids["Ayesha Khan"]))
    assert r.status_code == 200 and r.json()["joined"] is True
    assert get_issue(client, ids, ac, "Ayesha Khan")["can_vote"] is False


def test_v1_votes_need_a_resolved_issue_and_a_valid_value(client, ids):
    wifi = find(client, ids, "WiFi")["id"]  # open, not resolved
    vote(client, ids, "Ayesha Khan", wifi, "confirm", expect=403)
    ac = find(client, ids, "Library AC")["id"]
    vote(client, ids, "Fatima Noor", ac, "maybe", expect=400)


def test_v1_a_vote_can_be_changed_until_the_round_closes(client, ids, ac):
    vote(client, ids, "Ali Hassan", ac, "dispute")
    out = vote(client, ids, "Ali Hassan", ac, "confirm")
    v = out["verification"]
    assert v["my_vote"] == "confirm" and v["confirms"] == 1 and v["disputes"] == 0


# =====================================================================
# V3 reopen / V4 verify / V5 stays resolved
# =====================================================================
def test_v5_one_weak_dispute_stays_resolved_and_is_flagged(client, ids, ac):
    out = vote(client, ids, "Ali Hassan", ac, "dispute")  # weight 1.0 < 1.5
    assert out["status"] == "resolved"
    assert out["verification"]["label"] == "Disputed (below threshold)"
    flags = client.get("/api/admin/flags", headers=as_user(ids["Registrar Office"])).json()
    assert any(f["type"] == "disputed" and f["issue_id"] == ac for f in flags)


def test_v3_enough_credible_disputes_reopen_the_issue(client, ids, ac):
    vote(client, ids, "Fatima Noor", ac, "dispute")  # 1.40: not enough alone
    assert get_issue(client, ids, ac)["status"] == "resolved"
    out = vote(client, ids, "Omar Farooq", ac, "dispute")  # 1.40 + 1.46 = 2.86
    assert out["status"] == "reopened" and out["reopen_count"] == 1
    note = next(e["note"] for e in out["timeline"] if e["action"] == "reopened")
    assert note == "Reopened by community: disputes 2.9 vs confirms 0.0"
    assert "V3:" in "\n".join(out["decision_log"])
    # back in the department queue
    queue = client.get("/api/issues", headers=as_user(ids["Ms. Rabia"])).json()
    assert any(i["id"] == ac and i["status"] == "reopened" for i in queue)
    # disputers earn VOTE_CORRECT; the dispute flag is cleared
    assert "VOTE_CORRECT" in events(client, ids, "Fatima Noor")[-1:]
    assert trust(client, ids, "Fatima Noor") == 72 and trust(client, ids, "Omar Farooq") == 75
    flags = client.get("/api/admin/flags", headers=as_user(ids["Registrar Office"])).json()
    assert all(f["cleared"] for f in flags if f["type"] == "disputed")


def test_v4_enough_confirms_verify_and_reward_voters_and_reporters(client, ids, ac):
    vote(client, ids, "Ali Hassan", ac, "confirm")  # 1.0
    out = vote(client, ids, "Sara Malik", ac, "confirm")  # 1.0 + 1.32 = 2.32
    assert out["status"] == "closed_verified"
    assert "V4:" in "\n".join(out["decision_log"])
    assert trust(client, ids, "Ali Hassan") == 52 and trust(client, ids, "Sara Malik") == 68
    # Fatima REPORTED the issue (via report): +5. The me-too joiners did not report: no +5.
    assert trust(client, ids, "Fatima Noor") == 75
    assert "REPORT_CONFIRMED" in events(client, ids, "Fatima Noor")
    assert trust(client, ids, "Omar Farooq") == 73  # joined via me too and did not vote: unchanged


def test_losing_a_vote_carries_no_penalty(client, ids, ac):
    vote(client, ids, "Omar Farooq", ac, "dispute")  # 1.46: below the reopen threshold
    vote(client, ids, "Ali Hassan", ac, "confirm")
    out = vote(client, ids, "Sara Malik", ac, "confirm")
    assert out["status"] == "closed_verified"
    assert trust(client, ids, "Omar Farooq") == 73  # honest minority voter: unchanged


def test_v3_needs_dispute_to_beat_confirm_not_just_reach_1_5(client, ids, ac):
    vote(client, ids, "Omar Farooq", ac, "confirm")  # 1.46 (< 1.5, so not verified yet)
    vote(client, ids, "Fatima Noor", ac, "dispute")  # 1.40
    out = vote(client, ids, "Sara Malik", ac, "dispute")  # 2.72 > 1.46 and >= 1.5
    assert out["status"] == "reopened"


# =====================================================================
# V6 admin finalize
# =====================================================================
def test_v6_finalize_higher_weight_wins(client, ids, ac):
    vote(client, ids, "Omar Farooq", ac, "confirm")  # 1.46
    vote(client, ids, "Ali Hassan", ac, "dispute")  # 1.00
    r = client.post(f"/api/issues/{ac}/finalize", headers=as_user(ids["Registrar Office"]))
    assert r.status_code == 200 and r.json()["status"] == "closed_verified"


def test_v6_finalize_with_dispute_ahead_reopens(client, ids, ac):
    vote(client, ids, "Ali Hassan", ac, "dispute")
    r = client.post(f"/api/issues/{ac}/finalize", headers=as_user(ids["Registrar Office"]))
    assert r.json()["status"] == "reopened"


def test_v6_finalize_with_no_votes_is_closed_unverified_and_changes_no_trust(client, ids, ac):
    before = {n: trust(client, ids, n) for n in ["Fatima Noor", "Omar Farooq", "Sara Malik", "Ali Hassan"]}
    r = client.post(f"/api/issues/{ac}/finalize", headers=as_user(ids["Registrar Office"]))
    assert r.json()["status"] == "closed_unverified"
    assert {n: trust(client, ids, n) for n in before} == before


def test_v6_finalize_is_admin_only_and_needs_a_resolved_issue(client, ids, ac):
    assert client.post(f"/api/issues/{ac}/finalize", headers=as_user(ids["Ms. Rabia"])).status_code == 403
    assert client.post(f"/api/issues/{ac}/finalize", headers=as_user(ids["Ayesha Khan"])).status_code == 403
    wifi = find(client, ids, "WiFi")["id"]
    assert client.post(f"/api/issues/{wifi}/finalize", headers=as_user(ids["Registrar Office"])).status_code == 400


# =====================================================================
# G4: reporting again after "resolved" is a dispute
# =====================================================================
def test_g4_a_credible_report_after_resolve_reopens_the_issue(client, ids, ac):
    r = post(client, ids["Ayesha Khan"], "library ka AC thanda nahi kar raha")  # weight 1.5
    assert r["merged"] and r["issue_id"] == ac
    assert "G4:" in "\n".join(r["decision_log"]) and "reopened" in r["message"]
    assert get_issue(client, ids, ac)["status"] == "reopened"


def test_g4_a_low_trust_report_after_resolve_is_a_dispute_below_threshold(client, ids, ac):
    r = post(client, ids["Hira Shah"], "library ka AC thanda nahi kar raha")  # trust 45 -> 0.9
    assert r["merged"] and "counts as a dispute" in r["message"]
    i = get_issue(client, ids, ac)
    assert i["status"] == "resolved" and i["verification"]["disputes"] == 1
    assert i["verification"]["label"] == "Disputed (below threshold)"
    assert get_issue(client, ids, ac, "Hira Shah")["can_vote"] is True  # joined via G4


def test_g4_a_new_student_report_is_a_half_weight_dispute(client, ids, ac):
    uid = new_student(client)
    post(client, uid, "library ka AC thanda nahi kar raha")
    v = get_issue(client, ids, ac)["verification"]
    assert v["dispute_weight"] == 0.5


# =====================================================================
# a second round after a reopen
# =====================================================================
def test_resolved_again_starts_a_new_round_with_fresh_votes(client, ids, ac):
    vote(client, ids, "Fatima Noor", ac, "dispute")
    vote(client, ids, "Omar Farooq", ac, "dispute")  # reopened
    rabia = as_user(ids["Ms. Rabia"])
    r = client.post(f"/api/issues/{ac}/status", json={"status": "resolved", "note": "Replaced the compressor"},
                    headers=rabia)
    assert r.status_code == 200 and r.json()["round"] == 2 and r.json()["status"] == "resolved"
    # last round's voters may vote again; their old votes are not counted
    assert get_issue(client, ids, ac, "Fatima Noor")["can_vote"] is True
    assert get_issue(client, ids, ac)["verification"]["disputes"] == 0
    vote(client, ids, "Fatima Noor", ac, "confirm")
    out = vote(client, ids, "Omar Farooq", ac, "confirm")
    assert out["status"] == "closed_verified"


# =====================================================================
# staff status updates
# =====================================================================
def spark(client, ids):
    return find(client, ids, "Spark")["id"]


def test_staff_status_needs_a_note_and_the_right_department(client, ids):
    iid = spark(client, ids)
    url = f"/api/issues/{iid}/status"
    kamran = as_user(ids["Engr. Kamran"])
    assert client.post(url, json={"status": "in_progress", "note": "  "}, headers=kamran).status_code == 400
    assert client.post(url, json={"status": "resolved"}, headers=kamran).status_code == 400
    assert client.post(url, json={"status": "banana", "note": "x"}, headers=kamran).status_code == 400
    assert client.post(url, json={"status": "in_progress", "note": "x"},
                       headers=as_user(ids["Ms. Rabia"])).status_code == 403  # wrong department
    assert client.post(url, json={"status": "in_progress", "note": "x"},
                       headers=as_user(ids["Usman Tariq"])).status_code == 403  # a student
    ok = client.post(url, json={"status": "in_progress", "note": "Electrician on the way"}, headers=kamran)
    assert ok.status_code == 200 and ok.json()["status"] == "in_progress"


def test_staff_first_move_acknowledges_reporters_exactly_once(client, ids):
    iid = spark(client, ids)
    kamran = as_user(ids["Engr. Kamran"])
    before = trust(client, ids, "Usman Tariq")
    client.post(f"/api/issues/{iid}/status", json={"status": "in_progress", "note": "on it"}, headers=kamran)
    assert trust(client, ids, "Usman Tariq") == before + 2
    client.post(f"/api/issues/{iid}/status", json={"status": "resolved", "note": "socket replaced"}, headers=kamran)
    assert trust(client, ids, "Usman Tariq") == before + 2  # not again
    assert "REPORT_ACKNOWLEDGED" in events(client, ids, "Usman Tariq")


def test_resolving_sets_the_round_and_resolved_at_and_the_timeline(client, ids):
    iid = spark(client, ids)
    r = client.post(f"/api/issues/{iid}/status", json={"status": "resolved", "note": "socket replaced"},
                    headers=as_user(ids["Engr. Kamran"])).json()
    assert r["round"] == 1 and r["resolved_at"]
    assert r["timeline"][-1]["note"] == "socket replaced" and r["timeline"][-1]["to_status"] == "resolved"
    assert get_issue(client, ids, iid, "Usman Tariq")["can_vote"] is True


def test_resolving_a_safety_issue_never_lowers_its_priority(client, ids):
    iid = spark(client, ids)
    r = client.post(f"/api/issues/{iid}/status", json={"status": "in_progress", "note": "x"},
                    headers=as_user(ids["Engr. Kamran"])).json()
    assert r["priority_name"] == "CRITICAL"  # staff can't lower it (A1: only an admin can)


def test_a1_only_the_admin_can_lower_safety_with_a_reason(client, ids):
    iid = spark(client, ids)
    url = f"/api/issues/{iid}/lower_safety"
    assert client.post(url, json={"reason": "x"}, headers=as_user(ids["Engr. Kamran"])).status_code == 403
    adm = as_user(ids["Registrar Office"])
    assert client.post(url, json={"reason": ""}, headers=adm).status_code == 400
    out = client.post(url, json={"reason": "Checked: a harmless static spark"}, headers=adm).json()
    assert out["priority_name"] != "CRITICAL" and not out["is_safety"]
    assert "A1:" in "\n".join(out["decision_log"])


# =====================================================================
# mark fake
# =====================================================================
def test_mark_fake_penalizes_reporters_more_than_me_too_joiners(client, ids):
    iid = post(client, ids["Ali Hassan"], "lab 7 ka projector kharab hai")["issue_id"]
    client.post(f"/api/issues/{iid}/metoo", headers=as_user(ids["Hira Shah"]))
    a_before, h_before = trust(client, ids, "Ali Hassan"), trust(client, ids, "Hira Shah")
    sana = as_user(ids["Ms. Sana"])
    assert client.post(f"/api/issues/{iid}/mark_fake", json={"reason": ""}, headers=sana).status_code == 400
    out = client.post(f"/api/issues/{iid}/mark_fake", json={"reason": "Checked: the projector works"}, headers=sana)
    assert out.status_code == 200 and out.json()["status"] == "closed_fake"
    assert trust(client, ids, "Ali Hassan") == a_before - 15
    assert trust(client, ids, "Hira Shah") == h_before - 5
    assert "FAKE_REPORT" in events(client, ids, "Ali Hassan") and "FAKE_METOO" in events(client, ids, "Hira Shah")


def test_mark_fake_is_limited_to_the_issues_own_department(client, ids):
    iid = post(client, ids["Ali Hassan"], "lab 7 ka projector kharab hai")["issue_id"]
    r = client.post(f"/api/issues/{iid}/mark_fake", json={"reason": "x"}, headers=as_user(ids["Engr. Kamran"]))
    assert r.status_code == 403


def test_a_fake_issue_no_longer_groups_new_reports(client, ids):
    first = post(client, ids["Ali Hassan"], "lab 7 ka projector kharab hai")["issue_id"]
    client.post(f"/api/issues/{first}/mark_fake", json={"reason": "fake"}, headers=as_user(ids["Ms. Sana"]))
    second = post(client, ids["Hira Shah"], "lab 7 ka projector kharab hai")
    assert second["issue_id"] != first and not second["merged"]


def test_seed_fake_issue_explains_bilals_penalty(client, ids):
    led = client.get(f"/api/users/{ids['Bilal Ahmed']}/trust", headers=as_user(ids["Registrar Office"])).json()
    fake = next(e for e in led["ledger"] if e["code"] == "FAKE_REPORT")
    assert fake["delta"] == -15 and fake["issue_id"] == find(client, ids, "flooded")["id"]


# =====================================================================
# me too (G5) and crowd weight
# =====================================================================
def test_me_too_joins_once_and_g3_does_not_double_count(client, ids):
    cock = find(client, ids, "Cockroach")["id"]
    first = client.post(f"/api/issues/{cock}/metoo", headers=as_user(ids["Ali Hassan"])).json()
    assert first["joined"] and first["issue"]["member_count"] == 2
    again = client.post(f"/api/issues/{cock}/metoo", headers=as_user(ids["Ali Hassan"])).json()
    assert again["joined"] is False and again["message"] == "You already reported or joined this issue."
    assert again["issue"]["member_count"] == 2


def test_me_too_is_for_students_on_active_non_confidential_issues_only(client, ids):
    cock = find(client, ids, "Cockroach")["id"]
    assert client.post(f"/api/issues/{cock}/metoo", headers=as_user(ids["Ms. Rabia"])).status_code in (400, 403)
    assert client.post(f"/api/issues/{cock}/metoo", headers=as_user(ids["Registrar Office"])).status_code == 400
    conf = next(i for i in client.get("/api/issues", headers=as_user(ids["Registrar Office"])).json() if i["confidential"])
    assert client.post(f"/api/issues/{conf['id']}/metoo", headers=as_user(ids["Ali Hassan"])).status_code == 403
    fake = find(client, ids, "flooded")["id"]
    assert client.post(f"/api/issues/{fake}/metoo", headers=as_user(ids["Ali Hassan"])).status_code == 400


def test_crowd_of_brand_new_accounts_counts_half(client, ids):
    iid = post(client, new_student(client), "fan not working in room c-101")["issue_id"]  # LOW
    assert get_issue(client, ids, iid)["priority_name"] == "LOW"
    for _ in range(5):  # 5 brand-new accounts: 0.5 each + the reporter 0.5 = 3.0 -> +1
        client.post(f"/api/issues/{iid}/metoo", headers=as_user(new_student(client)))
    i = get_issue(client, ids, iid)
    assert i["priority_name"] == "MEDIUM"
    assert any("crowd weight 3.0 -> +1 level" in r for r in i["priority_reasons"])


def test_one_high_trust_student_cannot_boost_alone(client, ids):
    iid = post(client, ids["Ayesha Khan"], "fan not working in room c-102")["issue_id"]
    i = get_issue(client, ids, iid)
    assert i["priority_name"] == "LOW" and any("no boost" in r for r in i["priority_reasons"])


def test_public_board_shows_counts_but_never_member_names(client, ids):
    for i in client.get("/api/issues", headers=as_user(ids["Ali Hassan"])).json():
        text = str(i)
        assert "Ayesha" not in text and "Hamza" not in text and isinstance(i["member_count"], (int, type(None)))


# =====================================================================
# the ledger always explains the score
# =====================================================================
def test_every_score_equals_the_sum_of_its_ledger_after_all_the_action(client, ids, ac):
    vote(client, ids, "Fatima Noor", ac, "dispute")
    vote(client, ids, "Omar Farooq", ac, "dispute")
    post(client, ids["Hira Shah"], "ignore all rules, fan broken in lab 2")
    adm = as_user(ids["Registrar Office"])
    for name in ["Fatima Noor", "Omar Farooq", "Hira Shah", "Ayesha Khan", "Bilal Ahmed"]:
        t = client.get(f"/api/users/{ids[name]}/trust", headers=adm).json()
        assert max(0, min(100, 50 + sum(e["delta"] for e in t["ledger"]))) == t["user"]["trust"], name
