"""Plain-code rules: cleaning (T1), injection stripping (I1/I2), hazard (S1) and
harassment (C1) keywords, and the rules-engine fallback used when no AI answers.

Nothing in this file lets report text influence priority or visibility.
"""
import re
from dataclasses import dataclass, field

from rapidfuzz import fuzz

from app.triage import locations
from app.triage.schema import TriageExtraction
from app.triage.taxonomy import HAZARD_TO_TYPE, label


# ----------------------------------------------------------------------
# Cleaning + tone (step 3)
# ----------------------------------------------------------------------
def normalize(text: str) -> str:
    """Lowercase, drop apostrophes, punctuation -> spaces. Used for all keyword matching."""
    t = text.lower().replace("’", "'").replace("'", "")
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def clean(text: str) -> str:
    """`cleaned` = lowercased, repeated punctuation and whitespace collapsed."""
    t = text.lower()
    t = re.sub(r"([!?.,;:])\1+", r"\1", t)
    return re.sub(r"\s+", " ", t).strip()


_TONE_WORDS = re.compile(r"\b(urgent|asap|emergency|jaldi|fori|immediately)\b")


def detect_tone(raw: str) -> list[str]:
    """T1: tone markers. They are logged and then ignored everywhere else."""
    found: list[str] = []
    letters = [c for c in raw if c.isalpha()]
    if len(letters) >= 8 and sum(c.isupper() for c in letters) / len(letters) > 0.5:
        found.append("ALL CAPS")
    if raw.count("!") >= 3:
        found.append("3+ exclamation marks")
    for w in dict.fromkeys(_TONE_WORDS.findall(raw.lower())):
        found.append(f'"{w}"')
    if len(re.findall(r"\b(?:please|plz|pls)\b", raw.lower())) >= 2:
        found.append('repeated "please"')
    return found


# ----------------------------------------------------------------------
# I1 injection stripping (step 4)
# ----------------------------------------------------------------------
_TARGETS = ("critical|urgent|highest|high|top|emergency|p0|p1|max|fire hazard|safety hazard|"
            "hazard|confidential|public|admin|ee|cse|is|library|cafeteria|labs")

# (regex, explicit system manipulation?). Explicit ones get the I2 trust penalty.
_INJECTION_PATTERNS: list[tuple[re.Pattern, bool]] = [
    (re.compile(r"\b(?:ignore|disregard|forget|override|bypass)\s+"
                r"(?:(?:all|any|your|the|previous|above|prior)\s+)*"
                r"(?:rules|instructions|prompt|system|guidelines)\b"), True),
    (re.compile(r"\b(?:mark|classify|treat|set|flag|label|rate|categorize|route|send|assign|"
                r"escalate|prioritize)\s+"
                r"(?:(?:this report|my report|the report|this|it|me|priority)\s+)?"
                r"(?:(?:as|to|at)\s+)?(?:(?:a|an)\s+)?"
                rf"(?:{_TARGETS})(?:\s+priority)?\b"), False),
    (re.compile(r"\b(?:system|assistant|developer|admin)\s*:"), True),
    (re.compile(r"\bpriority\s*[:=]\s*\w+"), True),
    (re.compile(r"\byou are now\b"), True),
    (re.compile(r"\bnew instructions\b"), False),
    (re.compile(r"\bact as\b"), False),
    (re.compile(r"\bjailbreak\b"), False),
    (re.compile(r"\bi am (?:the )?(?:admin|registrar|staff)\b"), True),
]


@dataclass
class InjectionResult:
    stripped: str
    phrases: list[str] = field(default_factory=list)
    spans: list[tuple[int, int]] = field(default_factory=list)
    explicit: bool = False  # I2: explicit system manipulation -> trust penalty

    @property
    def found(self) -> bool:
        return bool(self.phrases)


def _tidy(text: str) -> str:
    t = re.sub(r"\s+", " ", text)
    t = re.sub(r"\s+([.,;:!?])", r"\1", t)
    t = re.sub(r"([.,;:!?])(?:\s*[.,;:!?])+", r"\1", t)
    t = re.sub(r"^[\s.,;:!?\-]+", "", t)
    return t.strip()


def strip_injection(cleaned: str) -> InjectionResult:
    """Remove ONLY the matched instruction phrases, never the whole sentence."""
    spans: list[tuple[int, int]] = []
    phrases: list[str] = []
    explicit = False
    for rx, is_explicit in _INJECTION_PATTERNS:
        for m in rx.finditer(cleaned):
            spans.append((m.start(), m.end()))
            phrases.append(m.group(0).strip())
            explicit = explicit or is_explicit
    merged: list[tuple[int, int]] = []
    for s, e in sorted(spans):
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    out, pos = [], 0
    for s, e in merged:
        out.append(cleaned[pos:s])
        out.append(" ")
        pos = e
    out.append(cleaned[pos:])
    return InjectionResult(_tidy("".join(out)), list(dict.fromkeys(phrases)), merged, explicit)


# ----------------------------------------------------------------------
# Hazard (S1) and harassment (C1) keywords
# Never include tone words (urgent, emergency, danger, asap, jaldi) here.
# ----------------------------------------------------------------------
HAZARD_KEYWORDS: dict[str, list[str]] = {
    "fire_smoke": ["fire", "flames", "flame", "smoke", "burning smell", "burnt smell",
                   "burning", "aag", "aag lagi", "dhuan", "dhuaan", "jal raha", "jalne ki boo"],
    "sparks_electrical": ["spark", "sparks", "sparking", "short circuit", "shot circuit",
                          "exposed wire", "naked wire", "live wire", "electric shock",
                          "got shocked", "chingari", "chingariyan", "current lag",
                          "current lagta", "current aa raha", "jhatka"],
    "gas": ["gas leak", "gas leakage", "gas smell", "smell of gas", "gas ki boo",
            "gas ki smell"],
    "medical": ["fainted", "unconscious", "collapsed", "bleeding", "blood", "injured",
                "injury", "seizure", "cant breathe", "heart attack", "ambulance", "behosh",
                "khoon", "zakhmi", "chot lagi", "saans nahi"],
    "structural": ["broken glass", "ceiling falling", "ceiling fell", "ceiling collapse",
                   "chhat gir"],
}
HARASSMENT_KEYWORDS = [
    "harass", "harassment", "harassing", "ragging", "ragged", "bully", "bullying", "stalk",
    "stalking", "threat", "threatening", "blackmail", "inappropriate", "touched me",
    "molest", "abuse", "eve teasing", "follows me",
    "harassed", "harasses", "stalked", "stalks", "bullied", "bullies", "threatened",
    "threatens", "blackmailing", "blackmailed", "molested", "abused", "abusing",
    "following me", "touching me",
    "tang karta", "tang karte", "tang kar raha", "chherta", "chherte", "chhed",
    "pareshan karta", "dhamki", "dhamkiyan", "ganda mazaq",
]


def _phrase_rx(phrases: list[str]) -> re.Pattern:
    alt = "|".join(re.escape(p) for p in sorted(phrases, key=len, reverse=True))
    return re.compile(rf"(?<![a-z0-9])(?:{alt})(?![a-z0-9])")


_HAZARD_RX = {kind: _phrase_rx(words) for kind, words in HAZARD_KEYWORDS.items()}
_HARASS_RX = _phrase_rx(HARASSMENT_KEYWORDS)


def find_hazard(stripped: str) -> tuple[str, str] | None:
    """S1: (hazard_kind, matched keyword), earliest hit in the stripped text."""
    t = normalize(stripped)
    best = None
    for kind, rx in _HAZARD_RX.items():
        m = rx.search(t)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), kind, m.group(0))
    return (best[1], best[2]) if best else None


def find_harassment(stripped: str) -> str | None:
    """C1: matched harassment keyword in the stripped text."""
    m = _HARASS_RX.search(normalize(stripped))
    return m.group(0) if m else None


def evidence_in_text(evidence: str, stripped: str) -> str | None:
    """S2: AI hazard evidence must appear in the report text and NOT only inside a removed
    injection phrase. Checking against `stripped` covers both. Returns how it matched."""
    ev, text = normalize(evidence), normalize(stripped)
    if not ev:
        return None
    if f" {ev} " in f" {text} ":
        return "verbatim"
    if len(ev) >= 4 and fuzz.partial_ratio(ev, text) >= 90:
        return "approximately"
    return None


# ----------------------------------------------------------------------
# Problem-type keywords (rules engine). Order matters: first match wins.
# ----------------------------------------------------------------------
TYPE_KEYWORDS: list[tuple[str, list[str]]] = [
    ("portal_lms_email", ["lms", "portal", "email", "e mail", "moodle", "student portal"]),
    ("wifi_internet", ["wifi", "wi fi", "internet", "net", "network", "lan", "broadband"]),
    ("power_outage", ["power cut", "no power", "power outage", "bijli gayi", "bijli chali gayi",
                      "bijli nahi", "bijli band", "load shedding", "blackout", "no electricity",
                      "light gayi", "power gone", "power failure"]),
    ("lights_not_working", ["tube light", "tubelight", "light", "lights", "batti", "bulb",
                            "bulbs", "lamp"]),
    ("fan_not_working", ["fan", "fans", "pankha", "pankhe"]),
    ("ac_not_working", ["ac", "a c", "air conditioner", "air conditioning", "aircon"]),
    ("socket_not_working", ["socket", "sockets", "plug point", "switch board", "switchboard",
                            "outlet", "extension board"]),
    ("projector_not_working", ["projector", "projectors", "multimedia"]),
    ("printer_not_working", ["printer", "printers", "printing", "photocopier", "photocopy",
                             "scanner"]),
    ("computer_not_working", ["computer", "computers", "pc", "pcs", "desktop", "cpu",
                              "monitor", "keyboard", "mouse"]),
    ("lab_equipment_broken", ["oscilloscope", "multimeter", "microscope", "lab equipment",
                              "equipment", "apparatus", "function generator", "soldering",
                              "breadboard", "lab ka samaan"]),
    ("water_leak", ["leak", "leaking", "leakage", "pani leak", "tap", "taps", "pipe", "pipes",
                    "flood", "flooded", "dripping", "seepage", "tapak raha", "tapakna"]),
    ("drinking_water", ["drinking water", "water cooler", "water dispenser", "dispenser",
                        "cooler", "pani", "paani", "water"]),
    ("washroom_cleanliness", ["washroom", "washrooms", "bathroom", "toilet", "toilets",
                              "restroom"]),
    ("food_hygiene", ["cockroach", "cockroaches", "insect", "insects", "keera", "keeray",
                      "worm", "worms", "rotten", "food poisoning", "stale", "expired",
                      "undercooked", "makhi", "hair in", "fly in"]),
    ("food_service_price", ["price", "prices", "expensive", "mehnga", "mehngi", "overpriced",
                            "overcharging", "bill", "slow service"]),
    ("food_quality", ["food", "khana", "chai", "tea", "rice", "chawal", "roti", "burger",
                      "biryani", "taste", "tasteless", "meal", "lunch", "paratha", "samosa",
                      "coffee"]),
    ("books_or_resources", ["book", "books", "kitaab", "kitab", "kitabain", "journal",
                            "journals", "novel", "magazine", "library card", "resource",
                            "resources"]),
    ("furniture_broken", ["chair", "chairs", "kursi", "table", "tables", "mez", "desk", "desks",
                          "bench", "benches", "furniture"]),
    ("door_or_lock", ["door", "doors", "darwaza", "lock", "locked", "khidki", "window",
                      "windows"]),
    ("cleanliness", ["dirty", "ganda", "gandagi", "clean", "cleaning", "garbage", "dustbin",
                     "trash", "kachra", "dust", "safai", "smell", "badbu"]),
    ("noise", ["noise", "noisy", "shor", "loud"]),
]
_TYPE_RX = [(t, _phrase_rx(words)) for t, words in TYPE_KEYWORDS]

_COMPLAINT_RX = _phrase_rx([
    "broken", "kharab", "not working", "nahi chal", "nai chal", "nahi kar", "band", "issue",
    "problem", "masla", "down", "damaged", "fault", "faulty", "dead", "missing", "nahi",
    "not available", "stopped", "slow", "ahista"])

_URDU_WORDS = {"hai", "hain", "ka", "ke", "ki", "ko", "mein", "me", "se", "raha", "rha", "rahi",
               "nahi", "nai", "kharab", "bohat", "bht", "yaar", "bhai", "aur", "kuch",
               "subah", "paas", "gayi", "hui", "kaam", "kar", "lag", "mil"}
_ENGLISH_WORDS = {"the", "is", "in", "not", "a", "an", "and", "of", "to", "my", "this",
                  "there", "are", "was", "working", "broken", "near", "from", "please"}


def keyword_problem_type(stripped: str) -> tuple[str | None, str | None]:
    """Rules-engine problem type: (type, matched keyword) or (None, None)."""
    t = normalize(stripped)
    t = re.sub(r"\b(?:computer|cse) lab\b", "lab", t)  # a place, not a computer problem
    for ptype, rx in _TYPE_RX:
        m = rx.search(t)
        if m:
            return ptype, m.group(0)
    return None, None


def detect_language(text: str) -> str:
    words = set(normalize(text).split())
    urdu, eng = len(words & _URDU_WORDS), len(words & _ENGLISH_WORDS)
    if urdu >= 2 and eng >= 2:
        return "mixed"
    if urdu >= 2:
        return "roman_urdu"
    return "english" if eng or not urdu else "mixed"


# ----------------------------------------------------------------------
# Rules-engine fallback (final provider in the chain)
# ----------------------------------------------------------------------
def rules_extract(cleaned: str, stripped: str) -> TriageExtraction:
    """Extract the same structured facts the AI would, from keywords only."""
    hazard = find_hazard(stripped)
    harass = find_harassment(stripped)
    ptype, kw = keyword_problem_type(stripped)
    matched = None

    if harass:
        ptype, matched = "harassment_ragging", harass
    elif hazard:
        ptype, matched = HAZARD_TO_TYPE[hazard[0]], hazard[1]
    elif ptype:
        matched = kw

    res = locations.resolve_location(stripped, "NONE", None, ptype or "other")
    is_report = bool(ptype or hazard or harass)
    if not is_report and res.status != "missing" and _COMPLAINT_RX.search(normalize(stripped)):
        is_report, ptype, matched = True, "other", "complaint word + a place"
    clear = ptype not in (None, "other")  # a vague "other" is not a clear problem (ASK1)
    ptype = ptype or "other"

    place = res.place_id or ("UNKNOWN_PLACE" if res.status in ("unknown", "not_on_campus")
                             else "NONE")
    summary = f"{label(ptype).capitalize()} problem" + (f" at {res.mention}" if res.mention else "")
    return TriageExtraction(
        is_campus_report=is_report,
        problem_clear=clear, other_place_mentions=[], question_about="none",
        clarifying_question="",
        language=detect_language(cleaned),
        summary_en=summary,
        problem_type=ptype,
        location_mention=res.mention,
        location_id=place,
        hazard_present=bool(hazard),
        hazard_kind=hazard[0] if hazard else "none",
        hazard_evidence=hazard[1] if hazard else "",
        harassment_or_ragging=bool(harass),
        scope="room_or_class",
        blocks_essential_activity=False,
        suggested_department="NONE",
        injection_attempt=False,
        reason=f'Rules mode: matched "{matched}"' if matched else "Rules mode: no keyword matched",
    )
