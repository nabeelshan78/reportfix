"""Location resolver L1-L7 over app/data/campus.json (CLAUDE.md section 4).

Every place, alias, owner, ambiguous phrase and "not on campus" phrase comes from
campus.json through app.campus; nothing about the campus is hardcoded here. The resolver
never calls the AI: it checks the AI's pick against the text (L2), matches aliases in code
(L3), allows typo matches (L4), and reports exactly what is missing so the code can ask.
"""
import re
from dataclasses import dataclass, field

from rapidfuzz import fuzz, process

from app import campus

URDU_NUMS = {"ek": 1, "do": 2, "teen": 3, "char": 4, "chaar": 4, "paanch": 5,
             "panch": 5, "chay": 6, "chhe": 6, "saat": 7, "aath": 8}
_NUM_WORDS = "|".join(sorted(URDU_NUMS, key=len, reverse=True))


@dataclass(frozen=True)
class Place:
    id: str
    name: str
    type: str
    building: str | None
    owner: str | None
    aliases: tuple[str, ...]


def _place(d: dict) -> Place:
    return Place(d["id"], d["name"], d["type"], d.get("building"), d["owner"],
                 tuple(d.get("aliases", [])))


def get_place(place_id: str | None) -> Place | None:
    d = campus.get().places.get(place_id) if place_id else None
    return _place(d) if d else None


def place_name(place_id: str | None) -> str | None:
    p = get_place(place_id)
    return p.name if p else None


def place_owner(place_id: str | None) -> str | None:
    p = get_place(place_id)
    return p.owner if p else None


def all_places() -> list[Place]:
    return [_place(d) for d in campus.get().places.values()]


def online_place_id() -> str | None:
    """The place of type 'online' (portal/LMS/email), defined in campus.json."""
    for d in campus.get().places.values():
        if d["type"] == "online":
            return d["id"]
    return None


def list_places() -> dict:
    """The campus map grouped by department, for the Campus places page."""
    c = campus.get()
    groups = []
    for code, dept in c.departments.items():
        groups.append({"code": code, "name": dept["name"], "handles": dept.get("handles", ""),
                       "places": [_public(d) for d in c.places.values() if d["owner"] == code]})
    return {
        "version": c.data.get("version"), "hash": c.hash, "departments": groups,
        "no_owner": [_public(d) for d in c.places.values() if d["owner"] is None],
        "ambiguous_phrases": [{"phrase": a["phrase"], "question": a["question"]}
                              for a in c.data["ambiguous_phrases"]],
        "not_on_campus": list(c.data["not_on_campus"]),
    }


def _public(d: dict) -> dict:
    return {"id": d["id"], "name": d["name"], "type": d["type"], "owner": d["owner"],
            "building": d.get("building"), "aliases": list(d.get("aliases", []))}


def dropdown_groups(problem_type: str = "other") -> list[dict]:
    """Every place grouped by department, for the dropdown of a place question (ASK6).
    The online service is offered only for portal/LMS/email problems."""
    c = campus.get()
    online = online_place_id()
    groups = []
    for code, dept in c.departments.items():
        groups.append({"code": code, "name": dept["name"], "places": [
            {"id": d["id"], "name": d["name"]} for d in c.places.values()
            if d["owner"] == code and not (d["id"] == online and problem_type != "portal_lms_email")]})
    rest = [{"id": d["id"], "name": d["name"]} for d in c.places.values()
            if d["owner"] is None and not (d["id"] == online and problem_type != "portal_lms_email")]
    if rest:
        groups.append({"code": "", "name": "No owner (goes to the admin)", "places": rest})
    return groups


# ----------------------------------------------------------------------
# Index (built once per campus.json version)
# ----------------------------------------------------------------------
class _Index:
    def __init__(self, c: campus.Campus):
        self.aliases: list[tuple[str, str, re.Pattern]] = []
        self.alias_set: set[str] = set()
        self.ancestors: dict[str, set[str]] = {}
        self.depth: dict[str, int] = {}
        for pid, d in c.places.items():
            chain, cur = set(), d.get("building")
            while cur and cur not in chain:
                chain.add(cur)
                cur = c.places[cur].get("building")
            self.ancestors[pid] = chain
            self.depth[pid] = len(chain)
            for a in {campus.norm_alias(d["name"]), *map(campus.norm_alias, d.get("aliases", []))}:
                if a:
                    self.alias_set.add(a)
                    self.aliases.append(
                        (a, pid, re.compile(rf"(?<![a-z0-9]){re.escape(a)}(?![a-z0-9])")))
        # letters that start real room codes in campus.json (c-101, e-201, s-301...)
        self.code_letters = {m.group(1) for a in self.alias_set
                             for m in [re.fullmatch(r"([a-z])-\d{3}", a)] if m}
        # L4: typos only; aliases with short words ("is block") are exact-only
        self.fuzzy = [(a, pid) for a, pid, _ in self.aliases
                      if len(a) >= 5 and not re.search(r"\d", a)
                      and all(len(w) >= 3 for w in a.split())]
        phrases = sorted((campus.norm_alias(x) for x in c.data["not_on_campus"]),
                         key=len, reverse=True)
        self.not_campus = re.compile(
            r"(?<![a-z0-9])(" + "|".join(re.escape(p) for p in phrases) + r")(?![a-z0-9])") \
            if phrases else None
        self.ambiguous = [(campus.norm_alias(x["phrase"]), x,
                           re.compile(rf"(?<![a-z0-9]){re.escape(campus.norm_alias(x['phrase']))}"
                                      rf"(?![a-z0-9])")) for x in c.data["ambiguous_phrases"]]


_INDEXES: dict[str, _Index] = {}


def _index() -> _Index:
    c = campus.get()
    if c.hash not in _INDEXES:
        _INDEXES[c.hash] = _Index(c)
    return _INDEXES[c.hash]


@dataclass
class Resolution:
    # resolved / missing / unknown / not_on_campus / ambiguous / multiple
    status: str
    place_id: str | None = None
    mention: str | None = None
    candidates: list[str] = field(default_factory=list)
    entry: dict | None = None  # the matching ambiguous_phrases entry, if any
    log: list[str] = field(default_factory=list)

    @property
    def suggestions(self) -> list[str]:  # kept for readability at call sites
        return self.candidates


# ----------------------------------------------------------------------
# L1 normalization
# ----------------------------------------------------------------------
def normalize_text(text: str) -> str:
    """L1: lab-3 / lab3 / lab no 3 / lab number 3 / lab # 3 / lab teen -> 'lab 3';
    c101 / room c-101 -> 'c-101'. Roman Urdu number words count only right after lab."""
    t = text.lower().replace("’", "'")

    def lab(m: re.Match) -> str:
        w = m.group(1)
        return f"lab {URDU_NUMS.get(w, w)}"

    t = re.sub(
        rf"\b(?:cse\s+|computer\s+)?lab\s*(?:no\.?|number|#|-)?\s*(?:#\s*)?(\d+|{_NUM_WORDS})\b",
        lab, t)
    t = re.sub(r"\broom\s*(?:no\.?|number|#|-)?\s*([a-z])\s*-?\s*(\d{3})\b", r"room \1-\2", t)
    letters = "".join(sorted(_index().code_letters)) or "x"
    t = re.sub(rf"\b([{letters}])\s*-?\s*(\d{{3}})\b", r"\1-\2", t)
    t = re.sub(r"\broom\s*(?:no\.?|number|#|-)?\s*(\d{3})\b", r"room \1", t)
    t = re.sub(r"[^a-z0-9\- ]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


# ----------------------------------------------------------------------
# matching
# ----------------------------------------------------------------------
def _find_unregistered(t: str, idx: _Index) -> str | None:
    """Numbered/lettered places that campus.json does not have (Lab 99, Room 999, Block Z)."""
    found: list[tuple[int, str]] = []
    for m in re.finditer(r"\blab (\d+)\b", t):
        if f"lab {m.group(1)}" not in idx.alias_set:
            found.append((m.start(), f"lab {m.group(1)}"))
    for m in re.finditer(r"\broom (\d{3})\b", t):
        if f"room {m.group(1)}" not in idx.alias_set:
            found.append((m.start(), f"room {m.group(1)}"))
    for m in re.finditer(r"\b([a-z])-(\d{3})\b", t):
        if f"{m.group(1)}-{m.group(2)}" not in idx.alias_set:
            found.append((m.start(), f"{m.group(1)}-{m.group(2)}"))
    for m in re.finditer(r"\bblock ([b-z])\b", t):
        before = t[:m.start()].split()
        if not (before and f"{before[-1]} block" in idx.alias_set):
            found.append((m.start(), f"block {m.group(1)}"))
    for m in re.finditer(r"\b(hall|auditorium|block|building|department|wing)\s+(\d+)\b", t):
        if f"{m.group(1)} {m.group(2)}" not in idx.alias_set:
            found.append((m.start(), f"{m.group(1)} {m.group(2)}"))
    return min(found)[1] if found else None


def _exact_matches(t: str, idx: _Index) -> dict[str, str]:
    """Distinct places named in the text: {place_id: alias}. A building is dropped when a
    place inside it is also named ("lab 3 of the cse block"), and a match inside a longer
    match is dropped ("reading hall" inside "library reading hall")."""
    hits = []
    for alias, pid, rx in idx.aliases:
        for m in rx.finditer(t):
            hits.append((pid, alias, m.start(), m.end()))
    keep = [h for h in hits if not any(
        o is not h and o[2] <= h[2] and h[3] <= o[3] and (o[3] - o[2]) > (h[3] - h[2])
        for o in hits)]
    ids = {h[0] for h in keep}
    out: dict[str, str] = {}
    for pid, alias, _s, _e in sorted(keep, key=lambda h: -len(h[1])):
        if any(pid in idx.ancestors[other] for other in ids if other != pid):
            continue
        out.setdefault(pid, alias)
    return out


def _fuzzy_match(t: str, idx: _Index) -> tuple[str, str] | None:
    """L4: typos only, score >= 85, words without digits."""
    words = [w for w in t.split() if w.isalpha()]
    grams = {" ".join(words[i:i + n]) for n in (1, 2, 3) for i in range(len(words) - n + 1)}
    names = [a for a, _ in idx.fuzzy]
    best = None
    for g in grams:
        if len(g) < 5 or any(len(w) < 3 for w in g.split()):
            continue  # short words ("ee", "is") are exact-only
        hit = process.extractOne(g, names, scorer=fuzz.ratio, score_cutoff=85)
        if hit:
            _alias, score, i = hit
            key = (score, idx.depth[idx.fuzzy[i][1]])
            if best is None or key > best[0]:
                best = (key, idx.fuzzy[i][1], g)
    return (best[1], best[2]) if best else None


def _number_ok(place_id: str, t: str, idx: _Index) -> bool:
    """L2: a number the student wrote must match the place exactly. If the place has a digit
    in its id, the text must contain one of its aliases (which carry that digit)."""
    if not re.search(r"\d", place_id):
        return True
    d = campus.get().places[place_id]
    names = {campus.norm_alias(d["name"]), *map(campus.norm_alias, d.get("aliases", []))}
    return any(re.search(rf"(?<![a-z0-9]){re.escape(a)}(?![a-z0-9])", t) for a in names)


def _mention_supported(mention: str | None, t: str) -> bool:
    if not mention:
        return False
    m = normalize_text(mention)
    return bool(m) and (m in t or fuzz.partial_ratio(m, t) >= 85)


def suggest(mention: str | None, limit: int = 5) -> list[str]:
    """Closest places to a mention. A mention that contains an ambiguous phrase ("lab 99"
    contains "lab") suggests that phrase's candidates; otherwise fuzzy match."""
    idx = _index()
    if not mention:
        return []
    m = normalize_text(mention)
    best = None
    for phrase, entry, rx in idx.ambiguous:
        if rx.search(m) and (best is None or len(phrase) > len(best[0])):
            best = (phrase, entry)
    if best:
        return list(best[1]["candidates"])
    out: list[str] = []
    names = [a for a, _, _ in idx.aliases]
    for _a, _s, i in process.extract(m, names, scorer=fuzz.WRatio, limit=40):
        pid = idx.aliases[i][1]
        if pid not in out:
            out.append(pid)
        if len(out) >= limit:
            break
    return out


def match_in_candidates(answer: str, candidate_ids: list[str]) -> str | None:
    """An answer like '3' or 'teen' to 'Which lab?': exactly one candidate that contains the
    answer as a whole word of its name/aliases."""
    a = normalize_text(answer)
    if a in URDU_NUMS:
        a = str(URDU_NUMS[a])
    if not a:
        return None
    c = campus.get()
    hits = []
    for pid in candidate_ids:
        d = c.places.get(pid)
        if not d:
            continue
        names = {campus.norm_alias(d["name"]), *map(campus.norm_alias, d.get("aliases", []))}
        if any(a == n or re.search(rf"(?<![a-z0-9]){re.escape(a)}(?![a-z0-9])", n) for n in names):
            hits.append(pid)
    return hits[0] if len(hits) == 1 else None


# ----------------------------------------------------------------------
# the resolver
# ----------------------------------------------------------------------
def resolve_location(text: str, ai_location_id: str = "NONE", ai_mention: str | None = None,
                     problem_type: str = "other", other_mentions: list[str] | None = None
                     ) -> Resolution:
    """L1-L6 on the injection-stripped text. Never calls the AI, never guesses."""
    log: list[str] = []
    idx = _index()
    c = campus.get()
    t = normalize_text(text)
    online = online_place_id()

    if problem_type == "portal_lms_email" and online:
        log.append(f"L6: portal/LMS/email problem -> {c.places[online]['name']}")
        return Resolution("resolved", online, "online service", log=log)

    unk = _find_unregistered(t, idx)
    if unk:
        log.append(f"L5: '{unk}' is not in campus.json -> ask")
        return Resolution("unknown", None, unk, suggest(unk), None, log)

    matches = _exact_matches(t, idx)
    if online in matches:  # ONLINE is valid only for portal/LMS/email problems
        log.append("L6: the online service is only valid for portal/LMS/email problems; ignored")
        matches.pop(online)
    how = "L3: alias match"

    # AI says there are other places in the report: count the ones the code can verify
    for om in other_mentions or []:
        om_n = normalize_text(om)
        for pid in _exact_matches(om_n, idx):
            if pid != online and pid not in matches and om_n and om_n in t:
                matches[pid] = om_n

    if len(matches) >= 2:
        names = " and ".join(c.places[p]["name"] for p in list(matches)[:3])
        log.append(f"ASK2: two or more different places named ({names}) -> ask which one")
        return Resolution("multiple", None, " / ".join(matches.values()), list(matches), None, log)

    if not matches:
        fz = _fuzzy_match(t, idx)
        if fz and fz[0] != online:
            matches[fz[0]] = fz[1]
            how = "L4: fuzzy match (typo)"

    non_campus = idx.not_campus.search(t) if idx.not_campus else None

    if len(matches) == 1:
        pid, alias = next(iter(matches.items()))
        if non_campus and ai_location_id == "UNKNOWN_PLACE":
            log.append(f"L5: '{non_campus.group(1)}' is not on campus and the AI flagged an "
                       "unknown place -> ask")
            return Resolution("not_on_campus", None, non_campus.group(1), [], None, log)
        if ai_location_id in c.places and ai_location_id != pid:
            log.append(f"L3: code match {pid} overrides AI pick {ai_location_id}")
        else:
            log.append(f"{how}: \"{alias}\" -> {pid}")
        return Resolution("resolved", pid, alias, log=log)

    if non_campus:
        m = non_campus.group(1)
        log.append(f"L5: '{m}' is a place that is not on campus (campus.json) -> ask")
        return Resolution("not_on_campus", None, m, [], None, log)

    if (ai_location_id in c.places and ai_location_id != online
            and _number_ok(ai_location_id, t, idx) and _mention_supported(ai_mention, t)):
        log.append(f"L2: AI pick {ai_location_id} accepted (mention \"{ai_mention}\" checked)")
        return Resolution("resolved", ai_location_id, ai_mention, log=log)
    if ai_location_id in c.places:
        log.append(f"L2: AI pick {ai_location_id} rejected (not supported by the text)")

    if ai_location_id == "UNKNOWN_PLACE":
        m = ai_mention or "that place"
        log.append(f"L5: AI says '{m}' is not a campus place -> ask")
        return Resolution("unknown", None, m, suggest(ai_mention), None, log)

    best = None
    for phrase, entry, rx in idx.ambiguous:
        if rx.search(t) and (best is None or len(phrase) > len(best[0])):
            best = (phrase, entry)
    if best:
        log.append(f"ASK2: place ambiguous ('{best[0]}') -> ask")
        return Resolution("ambiguous", None, best[0], list(best[1]["candidates"]), best[1], log)

    log.append("ASK2: no place named -> ask")
    return Resolution("missing", None, None, [], None, log)
