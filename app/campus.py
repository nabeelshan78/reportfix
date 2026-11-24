"""Loads and validates app/data/campus.json: OUR hand-written campus map.

campus.json is the ONLY place where departments, places, owners, aliases and routing
exceptions are defined. Nothing else in the code (resolver, prompt, routing, UI) may
hardcode a place name, an alias or a department mapping: it all comes from here.
"""
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from app.triage.taxonomy import PROBLEM_TYPES

CAMPUS_PATH = Path(__file__).resolve().parent / "data" / "campus.json"
MIN_ALIAS_LENGTH = 3
# Plain English words are never allowed as a whole alias ("is" is also a word).
STOP_ALIASES = {"is", "in", "at", "the", "a", "an", "it", "on", "of", "to", "and", "or",
                "my", "no", "do", "me", "we", "he", "she", "be", "by", "up", "so", "if"}


class CampusError(ValueError):
    """campus.json is invalid. The message lists every problem found."""


def norm_alias(text: str) -> str:
    """Aliases/names are compared lowercase with punctuation (except '-') removed."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\- ]", " ", text.lower())).strip()


def validate(data: dict) -> None:
    """Raise CampusError listing ALL problems, or return None when the file is valid."""
    errors: list[str] = []

    def need(cond: bool, msg: str) -> None:
        if not cond:
            errors.append(msg)

    for key in ("departments", "places", "special_routes", "ambiguous_phrases",
                "not_on_campus"):
        need(isinstance(data.get(key), list), f"'{key}' must be a list")
    if errors:
        raise CampusError("campus.json is invalid:\n- " + "\n- ".join(errors))

    dept_codes: set[str] = set()
    for d in data["departments"]:
        code = d.get("code")
        need(bool(code) and bool(d.get("name")), f"department needs a code and a name: {d}")
        need(code not in dept_codes, f"duplicate department code '{code}'")
        dept_codes.add(code)

    ids: set[str] = set()
    alias_owner: dict[str, str] = {}
    for p in data["places"]:
        pid = p.get("id")
        need(bool(pid) and bool(p.get("name")), f"place needs an id and a name: {p}")
        need(pid not in ids, f"duplicate place id '{pid}'")
        ids.add(pid)
        need("owner" in p, f"place '{pid}' has no explicit \"owner\" (use null for no owner)")
        need(bool(p.get("type")), f"place '{pid}' has no \"type\"")
    place_ids = ids

    for p in data["places"]:
        pid = p.get("id")
        owner = p.get("owner")
        need(owner is None or owner in dept_codes,
             f"place '{pid}' has owner '{owner}' which is not a department code")
        b = p.get("building")
        need(b is None or b in place_ids, f"place '{pid}' has unknown building '{b}'")
        need(b != pid, f"place '{pid}' cannot be its own building")
        names = {norm_alias(p.get("name", "")), *[norm_alias(a) for a in p.get("aliases", [])]}
        for a in names:
            if not a:
                continue
            need(len(a) >= MIN_ALIAS_LENGTH,
                 f"alias '{a}' of place '{pid}' is shorter than {MIN_ALIAS_LENGTH} characters")
            need(a not in STOP_ALIASES,
                 f"alias '{a}' of place '{pid}' is a plain English word; use a whole phrase")
            other = alias_owner.get(a)
            need(other in (None, pid),
                 f"alias '{a}' belongs to two places: '{other}' and '{pid}'")
            alias_owner.setdefault(a, pid)

    for r in data["special_routes"]:
        need(bool(r.get("rule")), f"special route needs a 'rule' id: {r}")
        need(r.get("department") in dept_codes,
             f"special route {r.get('rule')} sends to unknown department '{r.get('department')}'")
        for t in r.get("problem_types", []):
            need(t in PROBLEM_TYPES,
                 f"special route {r.get('rule')} has unknown problem type '{t}'")

    phrases: set[str] = set()
    for amb in data["ambiguous_phrases"]:
        ph = norm_alias(amb.get("phrase", ""))
        need(bool(ph), f"ambiguous phrase is empty: {amb}")
        need(ph not in phrases, f"duplicate ambiguous phrase '{ph}'")
        phrases.add(ph)
        need(bool(amb.get("question")), f"ambiguous phrase '{ph}' has no question")
        need(bool(amb.get("candidates")), f"ambiguous phrase '{ph}' has no candidates")
        for c in amb.get("candidates", []):
            need(c in place_ids, f"ambiguous phrase '{ph}' has unknown candidate '{c}'")

    for phrase in data["not_on_campus"]:
        a = norm_alias(phrase)
        need(a not in alias_owner,
             f"not_on_campus phrase '{phrase}' is also an alias of place '{alias_owner.get(a)}'")

    for ptype, hints in data.get("problem_hints", {}).items():
        for t in hints:
            need(t in PROBLEM_TYPES, f"problem_hints['{ptype}'] has unknown problem type '{t}'")
    need("default" in data.get("problem_hints", {}), "problem_hints needs a 'default' list")

    if errors:
        raise CampusError("campus.json is invalid:\n- " + "\n- ".join(errors))


@dataclass(frozen=True)
class Campus:
    data: dict
    hash: str
    departments: dict  # code -> {code, name, handles}
    places: dict  # id -> place dict, in file order

    def dept_codes(self) -> list[str]:
        return list(self.departments)


def from_data(data: dict) -> Campus:
    validate(data)
    raw = json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return Campus(
        data=data,
        hash=hashlib.sha256(raw).hexdigest()[:16],
        departments={d["code"]: d for d in data["departments"]},
        places={p["id"]: p for p in data["places"]},
    )


def load(path: str | Path | None = None) -> Campus:
    path = Path(path) if path else CAMPUS_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise CampusError(f"campus.json not found at {path}") from None
    except json.JSONDecodeError as e:
        raise CampusError(f"campus.json is not valid JSON: {e}") from None
    return from_data(data)


_active: Campus | None = None


def get() -> Campus:
    """The active campus map (loaded and validated on first use, i.e. at startup)."""
    global _active
    if _active is None:
        _active = load()
    return _active


def set_active(campus: Campus | None) -> None:
    """Tests swap in a modified map; None restores the real file on next get()."""
    global _active
    _active = campus
