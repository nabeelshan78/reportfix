import inspect

from app.community import priority
from app.community.priority import base_level, compute_priority, crowd_boost
from tests.conftest import as_user


def level(base, safety, w, n=1, minutes=0):
    return compute_priority(base, safety, w, n, minutes)[0]


# ---- P1 safety ----
def test_p1_safety_is_always_critical():
    for w in (0, 1.5, 9):
        assert level(1, True, w) == 4
    lvl, _score, reasons = compute_priority(1, True, 0, 1, 0)
    assert lvl == 4 and reasons[0].startswith("P1:")


# ---- P2 / P2a ----
def test_p2_base_impact_from_problem_type():
    assert base_level("fan_not_working", "single_person", False)[0] == 1
    assert base_level("wifi_internet", "room_or_class", False)[0] == 2
    assert base_level("power_outage", "room_or_class", False)[0] == 3


def test_p2a_needs_both_wide_scope_and_blocked_activity():
    assert base_level("wifi_internet", "building_or_many", True)[0] == 3
    assert base_level("wifi_internet", "building_or_many", False)[0] == 2
    assert base_level("wifi_internet", "room_or_class", True)[0] == 2


# ---- P3 crowd boost ----
def test_p3_one_high_trust_student_can_never_boost_alone():
    # the max weight of one person is 1.5, and a boost needs W >= 3
    assert crowd_boost(1.5) == 0
    lvl, _s, reasons = compute_priority(1, False, 1.5, 1, 0)
    assert lvl == 1
    assert any("no boost" in r and "one voice" in r for r in reasons)


def test_p3_crowd_weight_boosts():
    assert level(1, False, 3.0, 3) == 2
    assert level(1, False, 5.9, 4) == 2
    assert level(1, False, 6.0, 5) == 3  # +2 levels
    assert crowd_boost(2.99) == 0


def test_p3_reason_text_mentions_students_and_weight():
    _l, _s, reasons = compute_priority(2, False, 5.0, 4, 0)
    assert any("P3: 4 students, crowd weight 5.0 -> +1 level" in r for r in reasons)


# ---- P4 cap ----
def test_p4_non_safety_never_goes_above_high():
    assert level(3, False, 9.0, 8) == 3
    assert level(4, False, 0.0) == 3
    _l, _s, reasons = compute_priority(3, False, 9.0, 8, 0)
    assert any(r.startswith("P4:") for r in reasons)


# ---- P5: tone / caps / urgent / injection have no path into priority ----
def test_p5_priority_function_takes_no_text():
    params = set(inspect.signature(compute_priority).parameters)
    assert not params & {"text", "raw_text", "tone", "stripped", "cleaned", "report"}
    assert not params & set(inspect.signature(base_level).parameters) - {"problem_type", "scope", "blocks"}


def test_p5_priority_module_never_imports_report_text_helpers():
    src = inspect.getsource(priority)
    assert "rules" not in src and "detect_tone" not in src and "raw_text" not in src


# ---- sort score ----
def test_sort_score_formula():
    _l, score, _r = compute_priority(2, False, 1.5, 1, 7)
    assert score == 2 * 1000 + round(1.5 * 10) + 7
    _l, score, _r = compute_priority(2, False, 1.5, 1, 500)
    assert score == 2 * 1000 + 15 + 60  # waiting time is capped at 60 minutes


# ---- on real issues ----
def test_seed_wifi_issue_has_crowd_boost_to_high(client, ids):
    admin = as_user(ids["Registrar Office"])
    wifi = next(i for i in client.get("/api/issues", headers=admin).json() if "WiFi" in i["title"])
    assert wifi["priority_name"] == "HIGH"
    assert any(r.startswith("P3: 4 students") and "+1 level" in r for r in wifi["priority_reasons"])


def test_priority_is_recomputed_when_someone_joins(client, ids):
    cock = next(i for i in client.get("/api/issues", headers=as_user(ids["Omar Farooq"])).json()
                if "Cockroach" in i["title"])
    assert cock["priority_name"] == "MEDIUM"
    # three more credible students join: W = 1.46 + 1.4 + 1.32 + 1.24 ~ 5.4 -> +1
    for name in ["Fatima Noor", "Sara Malik", "Hamza Ali"]:
        r = client.post(f"/api/issues/{cock['id']}/metoo", headers=as_user(ids[name]))
        assert r.status_code == 200
    after = client.get(f"/api/issues/{cock['id']}", headers=as_user(ids["Omar Farooq"])).json()
    assert after["priority_name"] == "HIGH" and after["priority_score"] > cock["priority_score"]
