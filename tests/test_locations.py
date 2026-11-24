import pytest

from app import campus
from app.triage.locations import (all_places, get_place, list_places, match_in_candidates,
                                  resolve_location)


def resolve(text, ai_id="NONE", mention=None, ptype="other", others=None):
    return resolve_location(text.lower(), ai_id, mention, ptype, others)


# ---- map basics ----
def test_map_size_and_ids():
    assert len(all_places()) == 50
    for pid in ["LAB-1", "LAB-8", "C-101", "E-206", "S-301", "S-304", "IS-CYBER", "IS-FORENSICS",
                "IS-BLOCK", "TUCK-SHOP", "ADMIN-BLOCK", "AUDITORIUM", "PARKING", "CAMPUS-WIDE",
                "ONLINE", "LIB-READING", "CAF-KITCHEN", "SERVER-ROOM"]:
        assert get_place(pid), pid


def test_old_it_helpdesk_is_gone():
    assert get_place("IS-HELPDESK") is None


def test_nonexistent_places_are_not_in_the_map():
    for pid in ["LAB-9", "LAB-99", "C-999", "HOSTEL", "BLOCK-Z"]:
        assert get_place(pid) is None


def test_every_place_has_an_explicit_owner_key():
    for p in campus.get().data["places"]:
        assert "owner" in p


def test_owners():
    assert get_place("LAB-3").owner == "CSE"
    assert get_place("E-203").owner == "EE"
    assert get_place("S-302").owner == "IS"
    assert get_place("IS-CYBER").owner == "IS"
    assert get_place("PHY-LAB").owner == "Labs"
    assert get_place("TUCK-SHOP").owner == "Cafeteria"
    for pid in ["ADMIN-BLOCK", "AUDITORIUM", "PARKING", "CAMPUS-WIDE", "ONLINE"]:
        assert get_place(pid).owner is None


def test_locations_endpoint_groups_places_by_department(client):
    r = client.get("/api/locations").json()
    assert [d["code"] for d in r["departments"]] == ["EE", "CSE", "IS", "Labs", "Library", "Cafeteria"]
    assert sum(len(d["places"]) for d in r["departments"]) + len(r["no_owner"]) == 50
    assert any(p["id"] == "LAB-3" for d in r["departments"] if d["code"] == "CSE" for p in d["places"])
    assert {p["id"] for p in r["no_owner"]} == {"ADMIN-BLOCK", "AUDITORIUM", "PARKING",
                                                "CAMPUS-WIDE", "ONLINE"}
    assert "hostel" in r["not_on_campus"] and r["ambiguous_phrases"]


# ---- L1 normalize ----
@pytest.mark.parametrize("text", ["lab3 wifi", "lab-3 wifi", "lab 3 wifi", "lab no 3 wifi",
                                  "lab number 3 wifi", "lab # 3 wifi", "lab teen wifi",
                                  "Lab-3", "cse lab 3 wifi", "computer lab 3 wifi"])
def test_l1_lab3_variants_resolve_to_lab3(text):
    r = resolve(text)
    assert r.status == "resolved" and r.place_id == "LAB-3"


@pytest.mark.parametrize("word,n", [("ek", 1), ("do", 2), ("teen", 3), ("char", 4),
                                    ("paanch", 5), ("chay", 6), ("saat", 7), ("aath", 8)])
def test_l1_roman_urdu_numbers_after_lab(word, n):
    assert resolve(f"lab {word} ka projector").place_id == f"LAB-{n}"


def test_l1_urdu_number_words_only_count_right_after_lab():
    assert resolve("kal do baje lab ka projector kharab tha").status == "ambiguous"


def test_room_variants_across_departments():
    assert resolve("chair broken in room C-102").place_id == "C-102"
    assert resolve("fan not working c101").place_id == "C-101"
    assert resolve("room 203 ac").place_id == "E-203"
    assert resolve("e-204 light").place_id == "E-204"
    assert resolve("room s-302 projector").place_id == "S-302"
    assert resolve("s302 ka ac").place_id == "S-302"


def test_a_stray_letter_and_number_is_not_a_room():
    assert resolve("it costs a 300 rupees").status == "missing"


# ---- L2: the AI may never turn Lab 99 into Lab 9 ----
def test_l2_lab_99_never_maps_to_any_lab():
    for ai in ["LAB-9", "LAB-1", "LAB-8", "UNKNOWN_PLACE", "NONE"]:
        r = resolve("The projector in Lab 99 is broken", ai, "lab 99")
        assert r.status == "unknown" and r.place_id is None and r.mention == "lab 99", ai


def test_l2_lab_9_is_unknown():
    r = resolve("lab 9 ka ac kharab hai", "LAB-8", "lab 9")
    assert r.status == "unknown" and r.place_id is None


def test_l2_ai_pick_with_wrong_number_loses_to_the_text():
    assert resolve("the printer in lab 4 is jammed", "LAB-5", "lab 4").place_id == "LAB-4"


def test_l2_ai_pick_without_support_in_text_is_rejected():
    assert resolve("the projector is broken", "LAB-3", None).status == "missing"


# ---- L3 alias match, most specific wins (building nesting comes from campus.json) ----
def test_l3_reading_hall_beats_library():
    assert resolve("a student fainted in the library reading hall").place_id == "LIB-READING"
    assert resolve("a student fainted in the reading hall").place_id == "LIB-READING"


def test_l3_kitchen_beats_cafeteria():
    assert resolve("gas smell near the cafeteria kitchen").place_id == "CAF-KITCHEN"


def test_l3_lab_beats_its_building():
    assert resolve("wifi down in lab 3 of the cse block").place_id == "LAB-3"
    assert resolve("ac not working in the cyber lab of is block").place_id == "IS-CYBER"


def test_l3_blocks_and_named_places():
    assert resolve("poore cse block mein bijli gayi hui hai").place_id == "CSE-BLOCK"
    assert resolve("is block ka wifi down hai").place_id == "IS-BLOCK"
    assert resolve("electronics lab ka oscilloscope").place_id == "ELEC-LAB"
    assert resolve("canteen ki chai thandi hai").place_id == "CAF-MAIN"
    assert resolve("kutub khana mein shor hai").place_id == "LIB-MAIN"
    assert resolve("tuck shop pe price zyada hai").place_id == "TUCK-SHOP"
    assert resolve("parking me lights band").place_id == "PARKING"
    assert resolve("forensics lab ka pc kharab").place_id == "IS-FORENSICS"


# ---- "is" is also an English word ----
@pytest.mark.parametrize("text", ["the wifi is down", "the door is blocked",
                                  "this is a problem", "the lab is closed and it is cold"])
def test_english_is_never_matches_an_is_place(text):
    r = resolve(text)
    assert not (r.place_id or "").startswith("IS-"), (text, r)
    assert r.status in ("missing", "ambiguous")


# ---- L4 fuzzy for typos ----
@pytest.mark.parametrize("text,expected", [("libary me books nahi", "LIB-MAIN"),
                                           ("cafetaria me chai", "CAF-MAIN"),
                                           ("electronic lab ka scope", "ELEC-LAB"),
                                           ("librarry ka ac kharab", "LIB-MAIN")])
def test_l4_typos_resolve(text, expected):
    assert resolve(text).place_id == expected


def test_l4_digits_must_match_exactly():
    assert resolve("lab 33 ka wifi").status == "unknown"


def test_l4_never_fuzzy_matches_short_alias_words():
    # "is blocked" must not fuzzy-match the alias "is block"
    assert resolve("the exit is blocked by a chair").place_id is None


# ---- ASK2: what counts as missing ----
@pytest.mark.parametrize("text,status,mention", [
    ("hostel ka pankha kharab hai", "not_on_campus", "hostel"),
    ("fan not working in room 999", "unknown", "room 999"),
    ("ac not working in block z", "unknown", "block z"),
    ("swimming pool ka paani ganda hai", "not_on_campus", "swimming pool"),
    ("lab 99 projector", "unknown", "lab 99"),
    ("room s-999 ac", "unknown", "s-999"),
])
def test_ask2_unknown_or_off_campus_places(text, status, mention):
    r = resolve(text)
    assert (r.status, r.mention) == (status, mention) and r.place_id is None


def test_ask2_unknown_lab_suggests_the_labs_from_campus_json():
    r = resolve("The projector in Lab 99 is broken")
    assert [f"LAB-{n}" for n in range(1, 9)] == [c for c in r.candidates if c.startswith("LAB-")]


def test_ask2_ai_unknown_place_is_respected():
    assert resolve("the gym ac is broken", "UNKNOWN_PLACE", "gym").status == "not_on_campus"
    assert resolve("the tennis academy ac is broken", "UNKNOWN_PLACE", "tennis academy").status == "unknown"


@pytest.mark.parametrize("text,phrase", [
    ("computer lab ka projector kharab", "computer lab"),
    ("cse lab ka ac band hai", "cse lab"),
    ("the lab is too hot", "the lab"),
    ("ee lab ka fan kharab", "ee lab"),
    ("is lab ka wifi down", "is lab"),
    ("corridor ki light band hai", "corridor"),
    ("washroom is dirty", "washroom"),
    ("first floor ka ac kharab", "first floor"),
    ("classroom ka projector kharab", "classroom"),
])
def test_ask2_ambiguous_phrases(text, phrase):
    r = resolve(text)
    assert r.status == "ambiguous" and r.mention == phrase and r.entry and r.candidates


def test_ask2_specific_place_beats_an_ambiguous_phrase():
    assert resolve("washroom dirty in cse block").place_id == "CSE-BLOCK"
    assert resolve("cse lab 3 ka ac").place_id == "LAB-3"


def test_ask2_two_different_places_named():
    r = resolve("wifi in lab 3 and lab 5 is down")
    assert r.status == "multiple" and set(r.candidates) == {"LAB-3", "LAB-5"}
    r = resolve("the library and the cafeteria both smell bad")
    assert r.status == "multiple"


def test_ask2_a_place_and_its_own_building_is_not_two_places():
    assert resolve("lab 3 in the cse block").status == "resolved"


def test_ask2_ai_other_place_mentions_count_when_verifiable():
    r = resolve("wifi in lab 3, same as the library", "LAB-3", "lab 3", "wifi_internet",
                others=["library"])
    assert r.status == "multiple"


def test_ask2_no_place_named():
    assert resolve("projector kharab hai").status == "missing"


# ---- L6 exceptions ----
def test_l6_portal_lms_email_uses_online():
    r = resolve("LMS is down, can't upload assignment", ptype="portal_lms_email")
    assert r.status == "resolved" and r.place_id == "ONLINE"


def test_l6_online_is_not_valid_for_other_problem_types():
    assert resolve("email ka wifi slow hai", ptype="wifi_internet").place_id != "ONLINE"


# ---- answering a question: a button label, a number, an Urdu number ----
def test_match_in_candidates():
    labs = [f"LAB-{n}" for n in range(1, 9)]
    assert match_in_candidates("3", labs) == "LAB-3"
    assert match_in_candidates("teen", labs) == "LAB-3"
    assert match_in_candidates("lab 5", labs) == "LAB-5"
    assert match_in_candidates("9", labs) is None
    assert match_in_candidates("lab", labs) is None  # matches all: not one place


def test_list_places_payload_is_grouped_and_complete():
    m = list_places()
    assert m["hash"] == campus.get().hash and len(m["departments"]) == 6
