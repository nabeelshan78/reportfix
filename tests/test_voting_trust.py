from tests.conftest import as_user

# CLAUDE.md section 13: trust must come from real ledger events.
EXPECTED = {"Ayesha Khan": 81, "Omar Farooq": 73, "Fatima Noor": 70, "Sara Malik": 66,
            "Hamza Ali": 62, "Zainab Raza": 59, "Usman Tariq": 54, "Ali Hassan": 50,
            "Hira Shah": 45, "Bilal Ahmed": 35}


def test_seed_trust_scores_exact(client):
    scores = {u["name"]: u["trust"] for u in client.get("/api/users").json()}
    for name, trust in EXPECTED.items():
        assert scores[name] == trust, name


def test_ledger_sums_equal_displayed_score(client, ids):
    for name, uid in ids.items():
        if name not in EXPECTED:
            continue
        t = client.get(f"/api/users/{uid}/trust", headers=as_user(uid)).json()
        assert 50 + sum(e["delta"] for e in t["ledger"]) == t["user"]["trust"]


def test_weight_capped_and_explained(client, ids):
    uid = ids["Ayesha Khan"]
    w = client.get("/api/me", headers=as_user(uid)).json()["weight"]
    assert w["weight"] == 1.5 and "capped" in w["explanation"]


def test_new_student_is_half_weight(client):
    u = client.post("/api/users/new_student").json()
    w = client.get("/api/me", headers=as_user(u["id"])).json()["weight"]
    assert u["trust"] == 50 and w["new_account"] and w["weight"] == 0.5


def test_trust_ledger_privacy(client, ids):
    r = client.get(f"/api/users/{ids['Ayesha Khan']}/trust", headers=as_user(ids["Omar Farooq"]))
    assert r.status_code == 403
    r = client.get(f"/api/users/{ids['Ayesha Khan']}/trust", headers=as_user(ids["Registrar Office"]))
    assert r.status_code == 200
