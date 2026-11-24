"""Clarifying questions (CLAUDE.md section 5A, ASK1-ASK10).

CODE decides WHAT is missing and WHEN to ask. The AI only helps word a question or
understand an answer the code could not. Questions are plain text, never HTML.
"""
import re
from dataclasses import dataclass, field

from app import campus
from app.triage import locations, rules
from app.triage.locations import Resolution
from app.triage.taxonomy import PROBLEM_TYPES, label

PICK_ONLY_QUESTION = "Please pick one from the list. / Barah-e-meharbani list mein se chunein."
OPTIONAL_PLACE_QUESTION = "Where did this happen? (optional, you can skip) / Yeh kahan hua? (zaroori nahi, skip kar sakte hain)"
MAX_AI_QUESTION_CHARS = 200
_PERSONAL = re.compile(r"\b(your (full )?name|full name|phone|cnic|roll n|email address|"
                       r"contact|whatsapp|student id)\b", re.I)


@dataclass
class Gap:
    kind: str  # "problem" | "place"
    reason: str  # unclear | missing | unknown | not_on_campus | ambiguous | multiple
    mention: str | None = None
    candidates: list[str] = field(default_factory=list)
    entry: dict | None = None  # matching ambiguous_phrases entry

    def same_item(self, other: "Gap | None") -> bool:
        return bool(other) and (self.kind, self.reason) == (other.kind, other.reason)


def problem_gap(ptype: str, problem_clear: bool) -> Gap | None:
    """ASK1/ASK2: a vague 'other' is not a clear problem."""
    if ptype == "other" and not problem_clear:
        return Gap("problem", "unclear")
    return None


def place_gap(res: Resolution) -> Gap | None:
    if res.status == "resolved":
        return None
    return Gap("place", res.status, res.mention, list(res.candidates), res.entry)


def first_gap(ptype: str, problem_clear: bool, res: Resolution | None) -> Gap | None:
    """One question at a time: the problem first, then the place (ASK3)."""
    return problem_gap(ptype, problem_clear) or (place_gap(res) if res else None)


# ----------------------------------------------------------------------
# wording (ASK3, ASK5)
# ----------------------------------------------------------------------
def _plain(text: str) -> str:
    """Questions are shown as plain text. Strip anything that looks like markup."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]*>", "", text or "")).strip()


def _template(gap: Gap, ptype: str, place_name: str | None) -> str:
    understood = label(ptype) if ptype != "other" else "a problem"
    m = _plain(gap.mention or "that place")
    if gap.kind == "problem":
        if place_name:
            return (f"Got it: something is wrong in {place_name}. What exactly is the problem? / "
                    f"Samajh gaya: {place_name} mein koi masla hai. Masla kya hai?")
        return "What exactly is the problem? / Masla kya hai?"
    if gap.reason == "unknown":
        return (f"I couldn't find '{m}' on campus. Which place did you mean? / "
                f"'{m}' campus ki list mein nahi mila. Kaunsi jagah?")
    if gap.reason == "not_on_campus":
        return (f"'{m}' is not part of our campus map. Which campus place is this about? / "
                f"'{m}' hamare campus mein nahi hai. Campus ki kaunsi jagah?")
    if gap.reason == "multiple":
        c = campus.get()
        names = " and ".join(c.places[p]["name"] for p in gap.candidates[:3] if p in c.places)
        return (f"I found more than one place ({names}). Which one is the problem at? / "
                f"Kai jagahein mili ({names}). Masla kis jagah hai?")
    return (f"Got it: {understood}. Which room or lab is it in? / "
            f"Samajh gaya: {understood}. Kaunsi room ya lab mein?")


def ai_question_ok(ext, gap: Gap, injection_flag: bool) -> bool:
    """ASK5: use the AI's wording only if it matches what the code says is missing."""
    q = (getattr(ext, "clarifying_question", "") or "").strip()
    return bool(q) and ext.question_about == gap.kind and len(q) <= MAX_AI_QUESTION_CHARS \
        and not injection_flag and not _PERSONAL.search(q)


def build_question(gap: Gap, ptype: str, place_name: str | None, ext, injection_flag: bool,
                   engine: str, pick_only: bool = False) -> tuple[str, str]:
    """Returns (question text, source). Order: AI wording if allowed, the ambiguous_phrases
    entry, then a code template. Rules mode and pick-only mode use templates only."""
    if pick_only:
        return PICK_ONLY_QUESTION, "pick_only"
    if engine != "rules" and ai_question_ok(ext, gap, injection_flag):
        return _plain(ext.clarifying_question), "ai"
    if gap.kind == "place" and gap.reason == "ambiguous" and gap.entry:
        return _plain(gap.entry["question"]), "campus.json"
    return _template(gap, ptype, place_name), "template"


# ----------------------------------------------------------------------
# options (ASK6), built from campus.json
# ----------------------------------------------------------------------
def build_options(gap: Gap, ptype: str, place_id: str | None, pick_only: bool,
                  allow_cancel: bool = True) -> dict:
    c = campus.get()
    if gap.kind == "place":
        buttons = [{"label": c.places[p]["name"], "location_id": p}
                   for p in gap.candidates[:5] if p in c.places]
        dropdown = locations.dropdown_groups(ptype)
    else:
        ptype_of_place = c.places.get(place_id, {}).get("type") if place_id else None
        hints = c.data.get("problem_hints", {})
        types = hints.get(ptype_of_place) or hints["default"]
        buttons = [{"label": label(t), "problem_type": t} for t in types[:6]]
        buttons.append({"label": "Other", "problem_type": "other"})
        dropdown = [{"code": "", "name": "All problem types", "places": [
            {"id": t, "name": label(t)} for t in PROBLEM_TYPES if t != "harassment_ragging"]}]
    return {"kind": gap.kind, "buttons": buttons, "dropdown": dropdown,
            "free_text": not pick_only, "pick_only": pick_only,
            "admin_decide": True, "cancel": allow_cancel}


# ----------------------------------------------------------------------
# understanding an answer WITHOUT the AI (ASK7)
# ----------------------------------------------------------------------
def understand(answer_stripped: str, gap: Gap | None) -> dict:
    """What the code can read from a free-text answer: a place and/or a problem type."""
    out: dict = {"place": None, "ptype": None}
    if not answer_stripped.strip():
        return out
    res = locations.resolve_location(answer_stripped, "NONE", None, "other")
    if res.status == "resolved":
        out["place"] = res.place_id
    elif gap and gap.kind == "place" and gap.candidates:
        out["place"] = locations.match_in_candidates(answer_stripped, gap.candidates)
    # "actually it's the AC, not the fan": a negated word is not the problem
    positive = _NEGATED.sub(" ", answer_stripped)
    ptype, _kw = rules.keyword_problem_type(positive)
    out["ptype"] = ptype
    return out


_NEGATED = re.compile(r"\b(?:not|nahi|nahin|nai)\s+(?:the\s+|a\s+|ka\s+|ki\s+)?\w+", re.I)
