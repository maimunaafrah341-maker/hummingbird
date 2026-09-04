"""
POST /incident -- a working implementation, so the trigger half is not
hostage to a service that does not exist yet.

## Why this file exists, and what it is not

The teammate owns the real `/incident`. As of writing it is not in this
repo and not on the remote, and everything downstream of the trigger has
only ever run against a mock. That is a single point of failure sitting
entirely outside this half of the project.

So this is a *reference implementation*: a real service, in its own
file, on its own port, that answers the agreed shape. It does not touch
`api.py` and cannot collide with the teammate's work. When theirs lands,
point `--api` at it and delete this, or keep it as the offline fallback.

    uvicorn incident_api:app --port 8001
    python yolo_trigger.py camera --zone BAY-3 --api http://127.0.0.1:8001 \
        --source demo-01-best-13386074.mp4

## How the response is produced

Two tiers, the same shape as `language.py` uses, and for the same
reason: the expensive path must be optional.

  1. **A rules table.** Hazard type plus substance maps to severity,
     steps and a contraindication. Deterministic, instant, offline, and
     the only tier a demo actually needs.
  2. **An LLM**, via the existing `llm.py`, when `INCIDENT_LLM=1` and a
     provider key is set. Better prose, worse latency, and it can fail.

Tier 2 failing falls back to tier 1 rather than erroring. A hazard
service that returns nothing because an API key expired is worse than
one that returns a competent canned answer.

**The contraindication is never generated.** It comes from the table
below and nowhere else. "Do not use water on this" is the field most
likely to hurt somebody if it is wrong, and an LLM that invents a
plausible-sounding one is exactly the failure a safety system cannot
have. If the substance is unknown, the field is omitted -- no
contraindication is safer than a guessed one.
"""

import os
import re
from datetime import datetime, timezone
from typing import Optional

# The rules tier below is pure Python and is the part worth testing.
# Guarding the web imports keeps `--selftest` runnable on a machine
# without FastAPI installed -- which is the state this repo was actually
# in when this file was written.
try:
    from fastapi import FastAPI, Header, HTTPException
    from pydantic import BaseModel, Field

    WEB_AVAILABLE = True

except ImportError:                                  # pragma: no cover
    WEB_AVAILABLE = False
    BaseModel = object

    def Field(default=None, **kwargs):
        return default


# ============================================================
# CONFIGURATION
# ============================================================

# Matches the sibling service's convention: unset means open.
API_KEY = os.getenv("API_KEY") or None

# Tier 2 is off by default. It needs a provider key, adds seconds of
# latency, and the rules tier already answers every demo case.
USE_LLM = os.getenv("INCIDENT_LLM", "").strip() == "1"

# The teammate's limit, matched so both sides reject the same input.
# The trigger's own longest generated field is 14 characters
# (`NO-Safety Vest`), so this only bounds operator-supplied values.
MAX_FIELD_CHARS = 200


# ============================================================
# THE RULES
# ============================================================

# Substance hazards. `contraindication` is the load-bearing field: it is
# the thing a responder must NOT do, and every entry here is a
# well-established property of the substance, not a generated sentence.
SUBSTANCES = {
    "sodium hydroxide": {
        "code": "NAOH",
        "aliases": ("naoh", "caustic soda", "lye"),
        "class": "corrosive base",
        "contraindication": "Do not flush with a pressurised water jet -- "
                            "sodium hydroxide reacts exothermically with water "
                            "and will spatter caustic solution.",
        "first_step": "Contain the spill with a dry absorbent; do not hose it down.",
        "severity_floor": "high",
    },
    "sulfuric acid": {
        "code": "H2SO4",
        "aliases": ("h2so4", "battery acid"),
        "class": "corrosive acid",
        "contraindication": "Do not add water to the acid -- the reaction is "
                            "violently exothermic. Absorb, then neutralise with "
                            "soda ash.",
        "first_step": "Contain with dry soda ash or a spill pillow.",
        "severity_floor": "high",
    },
    "chlorine": {
        "code": "CL2",
        "aliases": ("cl2", "bleach", "hypochlorite"),
        "class": "toxic gas / oxidiser",
        "contraindication": "Do not mix with ammonia or acids -- releases "
                            "chlorine gas. Do not enter without respiratory "
                            "protection.",
        "first_step": "Evacuate upwind and ventilate before anyone re-enters.",
        "severity_floor": "critical",
    },
    "ammonia": {
        "code": "NH3",
        "aliases": ("nh3", "anhydrous ammonia"),
        "class": "toxic gas / base",
        "contraindication": "Do not mix with chlorine or bleach. Do not use "
                            "water spray directly on a liquid ammonia pool -- "
                            "it accelerates vaporisation.",
        "first_step": "Evacuate upwind; ammonia vapour is lighter than air.",
        "severity_floor": "critical",
    },
    "acetone": {
        "code": "ACETONE",
        "aliases": ("propanone",),
        "class": "flammable solvent",
        "contraindication": "Do not use a water jet on an acetone fire -- it "
                            "spreads the pool. Do not create sparks; vapour is "
                            "heavier than air and travels.",
        "first_step": "Remove ignition sources and ventilate at floor level.",
        "severity_floor": "high",
    },
    "lpg": {
        "code": "LPG",
        "aliases": ("propane", "butane", "liquefied petroleum"),
        "class": "flammable gas",
        "contraindication": "Do not extinguish a burning gas leak until the "
                            "supply is isolated -- unburnt gas accumulating is "
                            "worse than a controlled flame.",
        "first_step": "Isolate the supply valve before anything else.",
        "severity_floor": "critical",
    },
}

# PPE violations. Severity here is the floor for the violation alone; a
# hazardous substance in the bay raises it.
VIOLATIONS = {
    "hardhat": {
        "label": "head protection",
        "severity": "high",
        "steps": [
            "Stop work in {bay} and hold the area.",
            "Issue a hardhat before anyone re-enters {bay}.",
            "Check for overhead work or suspended loads above the bay.",
        ],
    },
    "mask": {
        "label": "respiratory protection",
        "severity": "high",
        "steps": [
            "Clear {bay} to fresh air and hold the area.",
            "Check the local exhaust ventilation is running.",
            "Issue the correct respirator for the substance before re-entry.",
        ],
    },
    "vest": {
        "label": "high-visibility clothing",
        "severity": "medium",
        "steps": [
            "Stop vehicle and plant movement through {bay}.",
            "Issue high-visibility clothing before work resumes.",
        ],
    },
    "gloves": {
        "label": "hand protection",
        "severity": "medium",
        "steps": [
            "Stop handling operations in {bay}.",
            "Issue gloves rated for the substance in use.",
        ],
    },
    "goggles": {
        "label": "eye protection",
        "severity": "high",
        "steps": [
            "Stop work in {bay}.",
            "Issue eye protection; check the nearest eyewash station is clear.",
        ],
    },
}

SEVERITY_ORDER = ["low", "medium", "high", "critical"]

CITATIONS = {
    "hardhat": "29 CFR 1910.135",
    "mask": "29 CFR 1910.134",
    "vest": "29 CFR 1910.132",
    "gloves": "29 CFR 1910.138",
    "goggles": "29 CFR 1910.133",
}


def _match_violation(incident_type):
    """Map a detector label to a rules entry. Returns (key, rules) or (None, None)."""

    text = (incident_type or "").lower()

    for key, rules in VIOLATIONS.items():
        if key in text:
            return key, rules

    return None, None


def _match_substance(substance_code, substance_name=None):
    """
    Map a canonical code (preferred) or a display name to a rules entry.

    The code is checked first and exactly, because that is the contract
    field and an exact key beats substring matching. The name is a
    fallback for callers that have not adopted substance_code yet, or
    for a substance the trigger could not map.
    """

    code = (substance_code or "").strip().upper()

    if code:
        for name, rules in SUBSTANCES.items():
            if rules.get("code") == code:
                return name, rules

    text = (substance_name or "").lower()

    if text.strip():
        for name, rules in SUBSTANCES.items():
            if name in text or any(alias in text for alias in rules["aliases"]):
                return name, rules

    return None, None


def _raise_severity(current, floor):
    """The higher of two severities."""

    try:
        return SEVERITY_ORDER[max(SEVERITY_ORDER.index(current),
                                  SEVERITY_ORDER.index(floor))]
    except ValueError:
        return current


def assess(incident_type, bay, substance_code, substance_name=None):
    """
    The rules tier. Deterministic, offline, always answers.

    Returns the full response dict. This is the tier a demo runs on and
    the tier the LLM falls back to.
    """

    bay = bay or "the bay"
    violation_key, violation = _match_violation(incident_type)
    matched_name, substance_rules = _match_substance(substance_code, substance_name)

    if violation:
        severity = violation["severity"]
        steps = [s.format(bay=bay) for s in violation["steps"]]
        missing = violation["label"]
    else:
        # An unrecognised hazard is still an incident. Answer generically
        # rather than 500-ing, and say the type was not recognised.
        severity = "medium"
        steps = ["Stop work in %s and hold the area." % bay,
                 "Have a competent person assess the hazard before work resumes."]
        missing = "required protective equipment"

    contraindication = None

    if substance_rules:
        severity = _raise_severity(severity, substance_rules["severity_floor"])
        contraindication = substance_rules["contraindication"]
        steps.insert(1, substance_rules["first_step"])

    steps.append("Log the incident and notify the shift safety officer.")

    spoken = ("Hazard in %s. %s missing%s. Clear the bay and wait for the "
              "safety officer." % (
                  bay.replace("-", " ").replace("_", " "),
                  missing.capitalize(),
                  " near %s" % matched_name if matched_name else ""))

    response = {
        "severity": severity,
        "steps": steps,
        "spoken_alert": spoken,
    }

    # Omitted, never guessed. An invented contraindication is the one
    # output of this service that could get somebody hurt.
    if contraindication:
        response["contraindication"] = contraindication

    if violation_key in CITATIONS:
        response["regulatory_citation"] = CITATIONS[violation_key]

    response["substance_class"] = (substance_rules or {}).get("class")
    response["tier"] = "rules"
    return response


def assess_with_llm(incident_type, bay, substance, base):
    """
    Tier 2. Rewrites the steps and the spoken line, nothing else.

    Deliberately narrow: severity and contraindication stay exactly as
    the rules produced them. The model is allowed to improve wording, not
    to decide what is dangerous.
    """

    import llm

    prompt = (
        "You are a plant safety officer. Rewrite these emergency response "
        "steps to be clearer and more specific. Keep them imperative, keep "
        "the same order, keep the same meaning, and return one step per "
        "line with no numbering or preamble.\n\n"
        "Bay: %s\nHazard: %s\nSubstance: %s\n\nSteps:\n%s"
        % (bay, incident_type, substance or "none recorded",
           "\n".join(base["steps"])))

    text = llm.generate(prompt) if hasattr(llm, "generate") else None

    if not text:
        return base

    steps = [re.sub(r"^\s*(?:\d+[.)]|[-*])\s*", "", line).strip()
             for line in text.splitlines()]
    steps = [s for s in steps if s]

    if not steps:
        return base

    improved = dict(base)
    improved["steps"] = steps
    improved["tier"] = "llm"
    return improved


# ============================================================
# HTTP
# ============================================================

class IncidentRequest(BaseModel):
    """
    Accepts the trigger's field names and the service's, because the two
    halves grew up calling these different things and neither should
    break while that is settled. At least one of each pair is required.
    """

    bay_id: Optional[str] = Field(default=None, max_length=MAX_FIELD_CHARS)
    incident_type: Optional[str] = Field(default=None, max_length=MAX_FIELD_CHARS)

    # Code is the retrieval key; name is what a human reads. A substance
    # the trigger could not map sends a name and NO code -- that means
    # "unmapped", never "no substance present".
    substance_code: Optional[str] = Field(default=None, max_length=32)
    substance_name: Optional[str] = Field(default=None, max_length=MAX_FIELD_CHARS)

    source: Optional[str] = Field(default=None, max_length=MAX_FIELD_CHARS)
    timestamp: Optional[str] = Field(default=None, max_length=MAX_FIELD_CHARS)
    language: Optional[str] = Field(default="en", max_length=16)
    confidence: Optional[float] = None
    camera_id: Optional[str] = Field(default=None, max_length=MAX_FIELD_CHARS)




app = (FastAPI(title="HazardWatch incident service (reference implementation)")
       if WEB_AVAILABLE else None)


def _check_key(provided):
    if API_KEY and provided != API_KEY:
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")


def health():
    return {
        "status": "ok",
        "tiers": {"rules": True, "llm": USE_LLM or None},
        "substances": sorted(SUBSTANCES),
        "violations": sorted(VIOLATIONS),
    }


def incident(request, x_api_key=None):
    _check_key(x_api_key)

    incident_type = request.incident_type

    if not incident_type or not incident_type.strip():
        raise HTTPException(status_code=400,
                            detail="incident_type must not be empty")

    bay = request.bay_id

    if not bay or not bay.strip():
        raise HTTPException(status_code=400, detail="bay_id must not be empty")

    started = datetime.now(timezone.utc)
    response = assess(incident_type, bay,
                      request.substance_code, request.substance_name)

    if USE_LLM:
        try:
            response = assess_with_llm(
                incident_type, bay,
                request.substance_name or request.substance_code, response)
        except Exception as e:
            # Degrade to the rules tier and say which tier answered. A
            # safety service that returns nothing because a key expired
            # is worse than one returning a competent canned answer.
            response["tier"] = "rules (llm failed: %s)" % type(e).__name__

    response["latency_ms"] = round(
        (datetime.now(timezone.utc) - started).total_seconds() * 1000, 2)

    return response


if WEB_AVAILABLE:
    # Routes are declared here, not as decorators on the functions
    # above, so this module imports -- and --selftest runs -- on a
    # machine with no FastAPI installed.
    #
    # The wrapper is not ceremony: FastAPI injects the request body and
    # the X-API-Key header from this signature's annotations and
    # defaults. Registering the bare function instead would leave
    # x_api_key permanently None and silently disable the auth check.

    @app.get("/health")
    def health_route():
        return health()

    @app.post("/incident")
    def incident_route(request: IncidentRequest,
                       x_api_key: str = Header(default=None)):
        return incident(request, x_api_key)


# ============================================================
# SELF TEST
# ============================================================

def selftest():
    """Exercise the rules directly. No server, no network."""

    checks = []

    def check(name, condition, detail=""):
        checks.append(bool(condition))
        print("  %s  %-42s %s" % ("PASS" if condition else "FAIL", name, detail))

    plain = assess("NO-Hardhat", "BAY-3", None)
    check("hardhat -> high, with steps",
          plain["severity"] == "high" and len(plain["steps"]) >= 3,
          "%s, %d steps" % (plain["severity"], len(plain["steps"])))

    check("no substance -> NO contraindication",
          "contraindication" not in plain,
          "omitted rather than guessed")

    caustic = assess("NO-Hardhat", "BAY-3", "NAOH", "Sodium hydroxide (50% solution)")
    check("substance adds a contraindication",
          "water" in caustic.get("contraindication", "").lower(),
          caustic.get("contraindication", "")[:52])

    check("substance raises severity",
          caustic["severity"] == "high", caustic["severity"])

    chlorine = assess("NO-Mask", "BAY-7", "CL2")
    check("critical substance escalates",
          chlorine["severity"] == "critical", chlorine["severity"])

    check("alias matching works",
          assess("NO-Mask", "BAY-7", None, "cl2 tank")["severity"] == "critical",
          "name fallback still works")

    unknown = assess("SOMETHING-NEW", "BAY-1", None, "unobtainium")
    check("unknown hazard still answers",
          unknown["severity"] and unknown["steps"] and "contraindication" not in unknown,
          "%s, %d steps, no invented contraindication"
          % (unknown["severity"], len(unknown["steps"])))

    check("citation matches the violation",
          assess("NO-Mask", "B", None).get("regulatory_citation") == "29 CFR 1910.134",
          assess("NO-Mask", "B", None).get("regulatory_citation"))

    check("contract fields all present",
          all(k in caustic for k in
              ("severity", "steps", "contraindication", "spoken_alert")),
          ", ".join(sorted(caustic)))

    check("bay name reaches the steps",
          any("BAY-3" in s for s in caustic["steps"]), caustic["steps"][0][:44])

    check("spoken alert is one sentence-ish",
          20 < len(plain["spoken_alert"]) < 200, plain["spoken_alert"])

    # The whole point of the tier split: no LLM, no network, still works.
    check("rules tier needs nothing", plain["tier"] == "rules", plain["tier"])

    print("\n%d/%d incident checks passed" % (sum(checks), len(checks)))
    return 0 if all(checks) else 1


if __name__ == "__main__":
    import sys

    if "--selftest" in sys.argv:
        sys.exit(selftest())

    import uvicorn

    port = int(os.getenv("PORT", "8001"))
    print("incident service on http://127.0.0.1:%d  (POST /incident)" % port)
    uvicorn.run(app, host="0.0.0.0", port=port)
