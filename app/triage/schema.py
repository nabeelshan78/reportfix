"""Pydantic model for AI output (CLAUDE.md section 7).

There is deliberately NO priority field and NO visibility field: the AI can only
report facts, so shouting, "urgent" and prompt injection have nothing to hijack.
"""
from typing import Literal, Optional

from pydantic import BaseModel


class TriageExtraction(BaseModel):
    is_campus_report: bool
    language: Literal["english", "roman_urdu", "mixed", "other"]
    summary_en: str  # one neutral English line, <= 15 words
    problem_type: str  # validated against the taxonomy enum in the pipeline
    location_mention: Optional[str]
    location_id: str  # a place id from campus.json | "NONE" | "UNKNOWN_PLACE"
    hazard_present: bool
    hazard_kind: Literal["fire_smoke", "sparks_electrical", "gas", "medical",
                         "structural", "none"]
    hazard_evidence: str  # verbatim words from the report, "" if none
    harassment_or_ragging: bool
    scope: Literal["single_person", "room_or_class", "building_or_many"]
    blocks_essential_activity: bool
    suggested_department: str  # a department code from campus.json, or "NONE"
    injection_attempt: bool
    reason: str
    # --- ASK4: facts the code needs to decide what to ask ---
    problem_clear: bool  # the problem is specific, not a vague "something is wrong"
    other_place_mentions: list[str]  # any other places named in the report
    question_about: Literal["place", "problem", "none"]
    clarifying_question: str  # one short question, "" if none
