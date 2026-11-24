"""Problem types, default impact and routing (CLAUDE.md section 5).

Problem types and default impacts are code. Departments, places, owners and routing
exceptions are NOT: they come from app/data/campus.json (see app/campus.py).
"""

SPECIAL_QUEUES = ["CONFIDENTIAL", "UNASSIGNED"]  # admin only; not departments

LEVEL_NAMES = {0: "N/A", 1: "LOW", 2: "MEDIUM", 3: "HIGH", 4: "CRITICAL"}
LEVELS = {"LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}

# problem_type -> (category, default impact level; 0 = n/a)
PROBLEM_TYPES: dict[str, tuple[str, int]] = {
    "wifi_internet": ("network", 2),
    "portal_lms_email": ("network", 2),
    "power_outage": ("electrical", 3),
    "sparks_or_exposed_wiring": ("electrical+safety", 4),
    "lights_not_working": ("electrical", 2),
    "fan_not_working": ("electrical", 1),
    "ac_not_working": ("electrical", 2),
    "socket_not_working": ("electrical", 1),
    "fire_or_smoke": ("safety", 4),
    "gas_smell": ("safety", 4),
    "medical_emergency": ("safety", 4),
    "structural_hazard": ("safety", 4),
    "computer_not_working": ("equipment", 2),
    "projector_not_working": ("equipment", 2),
    "lab_equipment_broken": ("equipment", 2),
    "printer_not_working": ("equipment", 1),
    "water_leak": ("facilities", 2),
    "drinking_water": ("facilities", 2),
    "washroom_cleanliness": ("facilities", 2),
    "cleanliness": ("facilities", 1),
    "furniture_broken": ("facilities", 1),
    "door_or_lock": ("facilities", 1),
    "noise": ("facilities", 1),
    "books_or_resources": ("library", 1),
    "food_quality": ("food", 1),
    "food_hygiene": ("food", 2),
    "food_service_price": ("food", 1),
    "harassment_ragging": ("CONFIDENTIAL", 0),
    "other": ("other", 1),
}
PROBLEM_TYPE_NAMES = list(PROBLEM_TYPES)

SAFETY_TYPES = {"sparks_or_exposed_wiring", "fire_or_smoke", "gas_smell",
                "medical_emergency", "structural_hazard"}

# S1/S2: hazard kind -> safety problem type (step 10)
HAZARD_TO_TYPE = {
    "fire_smoke": "fire_or_smoke",
    "sparks_electrical": "sparks_or_exposed_wiring",
    "gas": "gas_smell",
    "medical": "medical_emergency",
    "structural": "structural_hazard",
}


def default_impact(problem_type: str) -> int:
    return PROBLEM_TYPES.get(problem_type, PROBLEM_TYPES["other"])[1]


def label(problem_type: str) -> str:
    return problem_type.replace("_", " ")


def department_codes() -> list[str]:
    """Valid department codes, straight from campus.json."""
    from app import campus
    return campus.get().dept_codes()


def route(problem_type: str, confidential: bool, place_id: str | None,
          suggested: str = "NONE") -> tuple[str, str, str]:
    """R0-R4, decided in code from campus.json. Returns (department, rule id, explanation).
    A report can never choose its own department (the AI's suggestion is only logged, R5)."""
    from app import campus
    c = campus.get()
    if confidential or problem_type == "harassment_ragging":
        return "CONFIDENTIAL", "R0", "confidential report -> CONFIDENTIAL (admin only)"
    for sr in c.data["special_routes"]:  # R1/R2: wherever the problem is
        if problem_type in sr["problem_types"]:
            dept = sr["department"]
            return dept, sr["rule"], (f"{label(problem_type)} is a special route in "
                                      f"campus.json -> {dept}")
    place = c.places.get(place_id) if place_id else None
    if place and place["owner"]:  # R3: the owner of the place where the problem is
        owner = place["owner"]
        return owner, "R3", f"{place['name']} belongs to {owner} (campus.json) -> {owner}"
    name = place["name"] if place else "this place"
    hint = f"; AI suggested {suggested}" if suggested in c.departments else ""
    return "UNASSIGNED", "R4", f"{name} has no owner (campus.json) -> UNASSIGNED{hint}"
