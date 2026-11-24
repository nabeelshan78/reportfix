from tests.conftest import as_user


def _by_title(issues, fragment):
    return next(i for i in issues if fragment in i["title"])


def test_missing_user_header_is_401(client):
    assert client.get("/api/issues").status_code == 401
    assert client.get("/api/issues", headers={"X-User-Id": "99999"}).status_code == 401


def test_reset_restores_seed(client, ids):
    client.post("/api/users/new_student")
    assert len(client.get("/api/users").json()) == 18
    client.post("/api/reset")
    assert len(client.get("/api/users").json()) == 17


def test_ee_staff_sees_only_ee_issues_and_gets_403_on_others(client, ids):
    kamran = as_user(ids["Engr. Kamran"])
    issues = client.get("/api/issues", headers=kamran).json()
    assert issues and all(i["department"] == "EE" for i in issues)
    admin = client.get("/api/issues", headers=as_user(ids["Registrar Office"])).json()
    wifi = _by_title(admin, "WiFi")
    assert wifi["department"] == "CSE"  # Lab 3 belongs to CSE (campus.json)
    assert client.get(f"/api/issues/{wifi['id']}", headers=kamran).status_code == 403


def test_staff_never_sees_confidential(client, ids):
    admin = client.get("/api/issues", headers=as_user(ids["Registrar Office"])).json()
    conf = next(i for i in admin if i["confidential"])
    for name in ["Engr. Kamran", "Ms. Sana", "Mr. Adeel", "Ms. Rabia", "Mr. Javed", "Mr. Imran"]:
        h = as_user(ids[name])
        assert all(not i["confidential"] for i in client.get("/api/issues", headers=h).json())
        assert client.get(f"/api/issues/{conf['id']}", headers=h).status_code == 403


def test_confidential_hidden_from_public_board_and_other_students(client, ids):
    ali = as_user(ids["Ali Hassan"])
    board = client.get("/api/issues", headers=ali).json()
    assert all(not i["confidential"] for i in board)
    admin = client.get("/api/issues", headers=as_user(ids["Registrar Office"])).json()
    conf = next(i for i in admin if i["confidential"])
    assert client.get(f"/api/issues/{conf['id']}", headers=ali).status_code == 403
    assert all(not i["confidential"] for i in client.get("/api/my/issues", headers=ali).json())


def test_reporter_sees_own_confidential_with_notice(client, ids):
    sara = as_user(ids["Sara Malik"])
    mine = client.get("/api/my/issues", headers=sara).json()
    conf = next(i for i in mine if i["confidential"])
    assert "only the admin can see this" in conf["notice"]
    assert client.get(f"/api/issues/{conf['id']}", headers=sara).status_code == 200
    # but it is still not on the public board, even for Sara
    assert all(not i["confidential"] for i in client.get("/api/issues", headers=sara).json())


def test_admin_payload_has_no_reporter_identity(client, ids):
    admin = as_user(ids["Registrar Office"])
    conf = next(i for i in client.get("/api/issues", headers=admin).json() if i["confidential"])
    detail = client.get(f"/api/issues/{conf['id']}", headers=admin).json()
    assert detail["reporter"] == "Anonymous reporter"
    text = str(detail)
    assert "Sara" not in text and "user_id" not in text and "reporter_id" not in text
    assert detail["member_count"] is None


def test_issue_payloads_never_contain_user_ids(client, ids):
    for name in ["Ayesha Khan", "Registrar Office", "Engr. Kamran"]:
        for i in client.get("/api/issues", headers=as_user(ids[name])).json():
            assert not any("user" in k for k in i)


def test_seed_priorities(client, ids):
    issues = client.get("/api/issues", headers=as_user(ids["Registrar Office"])).json()
    assert _by_title(issues, "WiFi")["priority_name"] == "HIGH"
    assert _by_title(issues, "Spark")["priority_name"] == "CRITICAL"
    assert _by_title(issues, "Projector")["priority_name"] == "MEDIUM"
    assert _by_title(issues, "flooded")["status"] == "closed_fake"
    ac = _by_title(issues, "AC")
    assert ac["status"] == "resolved" and ac["round"] == 1


def test_can_vote_only_for_pre_resolve_members(client, ids):
    admin = client.get("/api/issues", headers=as_user(ids["Registrar Office"])).json()
    ac = _by_title(admin, "AC")["id"]
    for name, expected in [("Fatima Noor", True), ("Ali Hassan", True),
                           ("Ayesha Khan", False), ("Engr. Kamran", False),
                           ("Registrar Office", False)]:
        d = client.get(f"/api/issues/{ac}", headers=as_user(ids[name]))
        if d.status_code == 200:
            assert d.json()["can_vote"] is expected, name
