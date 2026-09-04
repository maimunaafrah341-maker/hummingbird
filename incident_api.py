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

import json
import os
import re
import sys
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


# ============================================================
# HAZARD TYPES
# ============================================================
#
# What the console actually asks about.
#
# VIOLATIONS above is keyed on missing PPE, because the camera trigger
# detects missing PPE. The operator console asks a different question --
# a spill, a gas leak, someone splashed -- and none of its eight types
# matched a PPE key, so every console incident fell through to the
# unmapped branch and came back with "Stop work and hold the area. Have
# a competent person assess the hazard." Correct, generic, and plainly
# not about a chlorine spill.
#
# Two taxonomies, one service, because the two front doors genuinely ask
# different things. A camera cannot see a spill and an operator does not
# report a missing hardhat.
#
# `label` is what the spoken alert names. `severity` is the floor before
# any substance raises it.

HAZARDS = {
    "spill": {
        "label": "chemical spill",
        "severity": "high",
        "steps": [
            "Stop handling operations in {bay}.",
            "Keep personnel upwind and out of the spread path.",
            "Do not walk through the spill or track it out of {bay}.",
        ],
    },
    "vapor release": {
        "label": "vapour release",
        "severity": "high",
        "steps": [
            "Clear {bay} to fresh air and hold the area.",
            "Check the local exhaust ventilation is running.",
            "Issue the correct respirator for the substance before re-entry.",
        ],
    },
    "skin contact": {
        "label": "skin or eye contact",
        "severity": "critical",
        "steps": [
            "Move the affected person to the nearest safety shower.",
            "Flush the affected area with running water for at least "
            "fifteen minutes.",
            "Remove contaminated clothing while flushing continues.",
            "Send the person for medical assessment even if they feel well.",
        ],
    },
    "fire flare": {
        "label": "fire or ignition",
        "severity": "critical",
        "steps": [
            "Raise the fire alarm and evacuate {bay}.",
            "Do not approach the seat of the fire.",
            "Account for everyone at the assembly point.",
        ],
    },
    "gas leak": {
        "label": "gas leak",
        "severity": "critical",
        "steps": [
            "Isolate the supply valve before anything else.",
            "Evacuate upwind and ventilate before anyone re-enters.",
            "Remove ignition sources and ventilate at floor level.",
        ],
    },
    "thermal runaway": {
        "label": "thermal runaway",
        "severity": "critical",
        "steps": [
            "Withdraw to the exclusion distance and do not approach the "
            "vessel.",
            "Do not attempt to cool the vessel by hand.",
            "Notify the process engineer and the fire team.",
        ],
    },
    "unknown chemical": {
        "label": "unidentified material",
        "severity": "high",
        "steps": [
            "Stop work in {bay} and hold the area.",
            "Treat the material as hazardous until it is identified.",
            "Do not attempt to identify it by smell or touch.",
        ],
    },
    "structural failure": {
        "label": "structural failure",
        "severity": "high",
        "steps": [
            "Evacuate the affected structure and cordon the area.",
            "Check for trapped or injured personnel from a safe distance.",
            "Do not re-enter until a competent person has assessed it.",
        ],
    },
}

# Words an operator or another console might send for the same thing.
HAZARD_ALIASES = {
    "leak": "spill",
    "liquid release": "spill",
    "chemical spill": "spill",
    "vapour release": "vapor release",
    "vapor": "vapor release",
    "vapour": "vapour release",
    "airborne exposure": "vapor release",
    "splash": "skin contact",
    "personnel exposure": "skin contact",
    "exposure": "skin contact",
    "fire": "fire flare",
    "ignition": "fire flare",
    "thermal event": "fire flare",
    "gas release": "gas leak",
    "toxic gas": "gas leak",
    "overpressure": "thermal runaway",
    "runaway": "thermal runaway",
    "unknown": "unknown chemical",
    "unidentified material": "unknown chemical",
    "equipment failure": "structural failure",
    "collapse": "structural failure",
}


def _match_hazard(incident_type):
    """
    Match the console's incident type. Returns (key, rules) or (None, None).

    Exact key first, then aliases, then a containment check -- so
    "Chemical Spill in Bay 3" still resolves to spill rather than
    falling through to the generic branch, which is the whole failure
    this table exists to fix.
    """

    if not incident_type:
        return None, None

    text = incident_type.strip().lower()

    if text in HAZARDS:
        return text, HAZARDS[text]

    if text in HAZARD_ALIASES:
        key = HAZARD_ALIASES[text]
        return key, HAZARDS[key]

    for key in HAZARDS:
        if key in text:
            return key, HAZARDS[key]

    for alias, key in HAZARD_ALIASES.items():
        if alias in text:
            return key, HAZARDS[key]

    return None, None


def assess(incident_type, bay, substance_code, substance_name=None):
    """
    The rules tier. Deterministic, offline, always answers.

    Returns the full response dict. This is the tier a demo runs on and
    the tier the LLM falls back to.
    """

    bay = bay or "the bay"
    violation_key, violation = _match_violation(incident_type)
    matched_name, substance_rules = _match_substance(substance_code, substance_name)

    hazard_key, hazard = _match_hazard(incident_type)

    if violation:
        severity = violation["severity"]
        templates = list(violation["steps"])
        missing = violation["label"]
        kind = "ppe"
    elif hazard:
        # The console asked about a hazard, not missing equipment.
        severity = hazard["severity"]
        templates = list(hazard["steps"])
        missing = hazard["label"]
        kind = "hazard"
    else:
        # An unrecognised hazard is still an incident. Answer generically
        # rather than 500-ing, and say the type was not recognised.
        severity = "medium"
        templates = ["Stop work in {bay} and hold the area.",
                     "Have a competent person assess the hazard before "
                     "work resumes."]
        missing = "required protective equipment"
        kind = "ppe"

    contraindication = None

    if substance_rules:
        severity = _raise_severity(severity, substance_rules["severity_floor"])
        contraindication = substance_rules["contraindication"]
        templates.insert(1, substance_rules["first_step"])

    templates.append("Log the incident and notify the shift safety officer.")

    # Formatted for the caller; the templates stay so the translator can
    # look them up by the key the phrase table actually uses.
    steps = [t.format(bay=bay) for t in templates]

    spoken_frame = ("Hazard in %s. %s missing%s. Clear the bay and wait for "
                    "the safety officer.") if kind == "ppe" else (
                    "Hazard in %s. %s reported%s. Clear the bay and wait for "
                    "the safety officer.")

    spoken = (spoken_frame % (
                  bay.replace("-", " ").replace("_", " "),
                  missing.capitalize(),
                  " near %s" % matched_name if matched_name else ""))

    response = {
        "severity": severity,
        "steps": steps,
        "spoken_alert": spoken,
        # Underscored, and stripped before the response leaves the
        # service: these are the parts the localizer needs to rebuild a
        # sentence in another language rather than trying to translate
        # an already-assembled one.
        "_step_templates": templates,
        "_bay": bay.replace("-", " ").replace("_", " "),
        "_bay_raw": bay,
        "_item": missing,
        "_kind": kind,
        "_near": matched_name,
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

    # The console's names. It calls these location/substance, and it was
    # built before this service existed -- measured against the deployed
    # console, every request it sends returns 400 "bay_id must not be
    # empty" without these. The console then shows DEMO FALLBACK, which
    # looks like a frontend fault and is not one.
    #
    # Aliased here rather than changed there, because the other
    # /incident implementation already accepts this shape: a console
    # that has to know which backend it is talking to is a worse
    # outcome than two services agreeing to answer the same request.
    location: Optional[str] = Field(default=None, max_length=MAX_FIELD_CHARS)
    substance: Optional[str] = Field(default=None, max_length=MAX_FIELD_CHARS)

    # Sent by the console, meaningless here, and rejected by pydantic as
    # an unexpected field if unnamed. Accepted and ignored.
    media: Optional[dict] = None
    target_lang: Optional[str] = Field(default=None, max_length=16)

    def resolved_bay(self):
        return self.bay_id or self.location

    def resolved_substance_name(self):
        return self.substance_name or self.substance

    def resolved_language(self):
        """
        Accepts a code ("te") or a display name ("Telugu").

        The console sends codes now, but it sent names for most of its
        life and the other backend still accepts them. Cheap to allow.
        """

        raw = (self.target_lang or self.language or "en").strip()
        return LANGUAGE_ALIASES.get(raw.lower(), raw.lower())


LANGUAGE_ALIASES = {
    "english": "en", "hindi": "hi", "telugu": "te", "bengali": "bn",
    "bangla": "bn", "urdu": "ur",
}


class CopilotRequest(BaseModel):
    """
    One operator question, plus exactly the context they ticked.

    shared_context is a free-shaped object on purpose: the console
    decides which fields an operator may share, and pinning the
    schema here would mean a UI change could not add a field without
    a backend deploy. It is echoed back verbatim, so whatever is
    listed in the console's drawer is what was actually sent.
    """

    question: str = Field(max_length=2000)
    shared_context: Optional[dict] = None
    approved_docs: Optional[list] = None
    confidence: Optional[float] = None
    camera_id: Optional[str] = Field(default=None, max_length=MAX_FIELD_CHARS)




app = (FastAPI(title="HazardWatch incident service (reference implementation)")
       if WEB_AVAILABLE else None)

# Browser clients on another origin -- a Vercel/Netlify frontend calling
# a Render backend -- are blocked by the browser unless this service
# says otherwise. No frontend change can work around it; the header has
# to come from here.
#
# CORS_ORIGINS is a comma-separated list. Unset allows any origin, which
# is right for a hackathon demo where the frontend URL is not known
# until it deploys, and wrong for anything real -- so set it once the
# frontend has a stable URL.
# What the twin counts down on a tile. Display only -- the authoritative
# window is escalation_watcher's, in the trigger process. They share a
# default and the same env var so the tile is not showing one number
# while the timer runs another.
TWIN_ACK_WINDOW = float(os.getenv("HAZARDWATCH_ACK_WINDOW", "45"))

# Translate the spoken alert and the steps into the requested language.
#
# Opt-in, and deliberately so. Translation goes through llm.py, which
# tries Featherless first -- and translating on every incident is
# automated traffic on a plan that excludes it, which is the 429 we
# already traced once. Turn this on only with a Groq/Gemini/OpenRouter
# key set, or on a Featherless Developer plan.
#
# Off, the response still carries a `localization` block saying exactly
# why the text is in English. Silence is what made the language picker
# look broken; an honest reason is not the same thing as a failure.
TRANSLATE = os.getenv("INCIDENT_TRANSLATE", "").strip() == "1"

CORS_ORIGINS = [
    origin.strip()
    for origin in os.getenv("CORS_ORIGINS", "*").split(",")
    if origin.strip()
]

if WEB_AVAILABLE:
    from fastapi.middleware.cors import CORSMiddleware

    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        # The frontend posts Content-Type: application/json, which makes
        # the browser send an OPTIONS preflight first. Without OPTIONS
        # here the preflight fails and the POST never happens.
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "X-API-Key"],
    )

    # Bay Twin. Optional on purpose: if bay_twin.py is missing or its
    # import fails, the incident service still answers exactly as before
    # and simply is not being watched. A dashboard is never allowed to
    # be a reason /incident stops working.
    try:
        import bay_twin
        app.include_router(bay_twin.build_router())
    except Exception as e:
        bay_twin = None
        print("[twin] not mounted (%s: %s)" % (type(e).__name__, e))
else:
    bay_twin = None


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

    bay = request.resolved_bay()

    if not bay or not bay.strip():
        raise HTTPException(
            status_code=400,
            detail="bay_id (or location) must not be empty")

    started = datetime.now(timezone.utc)
    response = assess(incident_type, bay,
                      request.substance_code, request.resolved_substance_name())

    if USE_LLM:
        try:
            response = assess_with_llm(
                incident_type, bay,
                request.resolved_substance_name() or request.substance_code,
                response)
        except Exception as e:
            # Degrade to the rules tier and say which tier answered. A
            # safety service that returns nothing because a key expired
            # is worse than one returning a competent canned answer.
            response["tier"] = "rules (llm failed: %s)" % type(e).__name__

    response["latency_ms"] = round(
        (datetime.now(timezone.utc) - started).total_seconds() * 1000, 2)

    response["localization"] = localize_response(response, request.resolved_language())

    # The localizer is done with them, and they are not part of the
    # contract. Leaving them in would ship internal structure to every
    # client and invite somebody to depend on it.
    for private in [k for k in response if k.startswith("_")]:
        response.pop(private)

    # Tell the twin. Wrapped because telemetry must never be able to
    # turn a successful assessment into a 500 -- the caller is a trigger
    # waiting to speak an alert.
    if bay_twin is not None:
        try:
            bay_twin.emit_incident(
                {
                    "bay_id": bay,
                    "incident_type": incident_type,
                    "source": getattr(request, "source", None) or "api",
                    "confidence": getattr(request, "confidence", None),
                    "substance_name": request.substance_name,
                    "substance_code": request.substance_code,
                },
                response,
                ack_window=TWIN_ACK_WINDOW,
            )
        except Exception:
            pass

    return response


def _spoken_from_table(response, language):
    """
    Rebuild the spoken alert from the phrase table rather than
    translating the finished English sentence.

    Translating the assembled string would mean parsing back out the
    bay and the equipment that were just formatted into it. The parts
    are still available here, so the sentence is built once per
    language from its own frame -- which is also the only way the
    clause order comes out natural rather than English-shaped.
    """

    import phrases

    bay = response.get("_bay")
    item = response.get("_item")

    if not bay or not item:
        return None

    return phrases.spoken(language, bay, item, near=response.get("_near"),
                          kind=response.get("_kind", "ppe"))


def localize_response(response, language):
    """
    Translate the spoken alert and the steps, and say honestly what
    language the result is actually in.

    The rule from API_CONTRACT.md, extended from text to safety
    instructions: **never present an untranslated string as a
    translation.** So `language` here always describes what the text
    below IS, never what was asked for. A client that renders
    localization.language cannot mislabel English as Hindi.

    Never raises. A translation failure degrades to English with a
    stated reason; it does not cost the caller their incident.
    """

    requested = (language or "en").strip().lower()

    block = {
        "requested": requested,
        "language": "en",
        "translated": False,
        "reason": None,
        "spoken_alert": response.get("spoken_alert", ""),
        "steps": list(response.get("steps", [])),
    }

    if requested in ("", "en", "eng", "english"):
        block["reason"] = "English requested -- nothing to translate"
        return block

    # Tier 1: the shipped phrase table. Deterministic, offline, instant,
    # and reviewable by a human who speaks the language -- which is the
    # whole argument for it. Everything this service can say comes from
    # a fixed set of 23 strings, so runtime machine translation was
    # never the right tool: it needs a key, costs a round trip, is
    # automated LLM traffic, and can mistranslate "do not add water to
    # the acid" with nobody checking.
    try:
        import phrases

        if phrases.covers(requested):
            bay_raw = response.get("_bay_raw", "")
            templates = response.get("_step_templates") or []
            translated_steps = [phrases.step(t, requested) for t in templates]
            steps = [t.format(bay=bay_raw) if t else None
                     for t in translated_steps]
            contra = response.get("contraindication")
            contra_translated = (phrases.contraindication(contra, requested)
                                 if contra else None)

            # All or nothing. A Hindi step list with one English line in
            # it is not a translation, it is a bug that looks like one.
            if all(steps) and (contra is None or contra_translated):
                block.update({
                    "language": requested,
                    "translated": True,
                    "source": "phrase table",
                    "steps": steps,
                    "review": phrases.review_state(requested),
                })

                if contra_translated:
                    block["contraindication"] = contra_translated

                spoken = _spoken_from_table(response, requested)

                if spoken:
                    block["spoken_alert"] = spoken

                return block

            block["reason"] = ("phrase table is missing an entry for this "
                               "response -- falling back to English")

    except Exception as e:
        block["reason"] = "phrase table failed (%s)" % type(e).__name__

    # Tier 2: a model. Only if somebody asked for it.
    if not TRANSLATE:
        block.setdefault("reason", None)
        block["reason"] = block["reason"] or (
            "no phrase table for %r, and model translation is off "
            "(set INCIDENT_TRANSLATE=1 and a provider key)" % requested)
        return block

    try:
        import alert_language

        spoken = alert_language.localize(response.get("spoken_alert", ""),
                                         requested)

        if not spoken.get("translated"):
            block["reason"] = spoken.get("reason") or "translation unavailable"
            return block

        steps = []

        for step in response.get("steps", []):
            done = alert_language.localize(step, requested)
            # One failure mid-list would otherwise produce a half-Hindi,
            # half-English instruction set, which is worse than either.
            if not done.get("translated"):
                block["reason"] = done.get("reason") or "step translation failed"
                return block

            steps.append(done["text"])

        block.update({
            "language": spoken.get("language", requested),
            "translated": True,
            "spoken_alert": spoken["text"],
            "steps": steps,
        })

    except Exception as e:
        block["reason"] = "%s: %s" % (type(e).__name__, str(e)[:120])

    return block


# ============================================================
# THE EHS COPILOT
# ============================================================
#
# The sponsor requirement, satisfied the way the licence and the safety
# argument both point: Featherless as a tool an operator opens, asks a
# question, and reads -- not a service the system calls on its own.
#
# Nothing here can act. The endpoint returns text. It has no path to the
# webhook dispatcher, the TTS layer, the escalation watcher or the
# incident record; the operator copies what they want into the incident
# themselves. That is not a limitation to work around later, it is the
# whole design: a model that can draft a briefing is useful, and a model
# that can broadcast one is a hazard.

COPILOT_SYSTEM = """You are the Featherless EHS Copilot for an industrial safety console,
operating in HUMAN-DECISION mode.

You provide analysis, clarification, checklists, translation and draft
notes for a trained operator who is handling a live incident.

You NEVER issue autonomous orders, activate equipment, broadcast alerts,
override a procedure, or state that any action has been carried out. You
do not instruct; you help a human think and communicate.

Use ONLY the operator-shared context and approved documents supplied
below. If a fact is not in them, say plainly that it is missing rather
than supplying it from general knowledge -- an invented detail about a
chemical is worse than an acknowledged gap.

Structure every answer under these headings, omitting any that would be
empty:

  What is known
  What must be verified
  Questions for the operator
  Approved-document references

Be concise. Prefer short lines a person can read while standing up.

End every answer with exactly this line:
Human decision required: verify against site SOP/SDS and EHS direction.
"""

COPILOT_CLOSING = ("Human decision required: verify against site SOP/SDS "
                   "and EHS direction.")

# What the console offers as one-tap questions. Kept server-side so the
# advisory framing cannot drift between the two halves of the project --
# and because every one of them asks the model to help the operator
# investigate rather than to decide anything.
COPILOT_PROMPTS = [
    "What must I verify in the next 60 seconds?",
    "What information is missing?",
    "Explain the chemical hazard simply",
    "What questions should I ask the field team?",
    "Draft a handover for the EHS lead",
    "Translate my operator note to Telugu",
    "Summarise this camera observation",
    "Compare this event against the approved SOP",
]


def copilot(question, shared_context=None, approved_docs=None):
    """
    Answer one operator question. Advisory only.

    Returns the answer plus an exact echo of what was sent. The console
    shows a "shared with AI" drawer, and a drawer that lists anything
    other than what actually left the building is worse than no drawer:
    it is a privacy claim that is not true. So the echo is built from
    the same object that goes into the prompt, not assembled separately.
    """

    question = (question or "").strip()

    if not question:
        raise ValueError("question must not be empty")

    if len(question) > 2000:
        raise ValueError("question is too long (limit 2000 characters)")

    # Only what the operator ticked. Empty values are dropped rather
    # than sent as nulls, so the drawer and the payload agree.
    shared = {
        key: value
        for key, value in (shared_context or {}).items()
        if value not in (None, "", [], {})
    }

    docs = list(approved_docs or [])

    payload = {
        "operator_question": question,
        "operator_shared_context": shared,
        "approved_site_documents": docs,
    }

    import llm

    answer, provider = llm.generate_response(
        json.dumps(payload, ensure_ascii=False, indent=2),
        system=COPILOT_SYSTEM,
        temperature=0.2,
        max_tokens=650,
        want_provider=True,
    )

    answer = answer.strip()

    # The closing line is a promise the console displays. A model that
    # drops it under length pressure would leave the console asserting
    # something the text does not say, so it is enforced here rather
    # than hoped for.
    if COPILOT_CLOSING.lower() not in answer.lower():
        answer = answer + "\n\n" + COPILOT_CLOSING

    return {
        "answer": answer,
        "mode": "human_decision_required",
        "advisory": True,
        "provider": provider,
        # Exactly what left this process, for the drawer.
        "shared": payload,
        "prompts": COPILOT_PROMPTS,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "note": "Advisory only. This response cannot dispatch, broadcast, "
                "translate an alert, or control equipment. An authorised "
                "operator decides what, if anything, happens next.",
    }


def shift_brief(request):
    """
    A plain-language handover note for the shift log, written on demand.

    **This is the only place a language model is called, and a person
    has to press a button to get here.** That is deliberate on two
    counts.

    Licensing: Featherless sells Chat plans for human-driven interactive
    use and Developer plans for API-driven automation. Calling a model on
    every detection is automation; generating a note because an operator
    asked for one is a person using a tool. This endpoint is the second
    thing.

    Safety: the autonomous path -- severity, response steps, the
    contraindication -- stays deterministic and never reaches a model.
    A brief is a summary written after the fact for a human to read. If
    it fails, nothing about the incident response changes; you just do
    not get the note.
    """

    incident_type = request.incident_type or "hazard"
    bay = request.bay_id or "the bay"
    substance = request.substance_name or request.substance_code

    assessment = assess(incident_type, bay, request.substance_code,
                        request.substance_name)

    prompt = (
        "Write a short shift-handover note for a plant safety log about "
        "the incident below. Four sentences at most, plain English, past "
        "tense, factual. State what happened, what was done, and what the "
        "next shift should watch for. Do not invent details that are not "
        "listed. Do not add a heading or any preamble.\n\n"
        "Bay: %s\nHazard: %s\nSubstance: %s\nSeverity: %s\n"
        "Actions taken:\n%s\nHazard to avoid: %s"
        % (bay, incident_type, substance or "none recorded",
           assessment["severity"], "\n".join("- " + s for s in assessment["steps"]),
           assessment.get("contraindication") or "none recorded"))

    import llm

    text = llm.generate_response(prompt)

    return {
        "brief": text.strip(),
        "incident": {
            "bay_id": bay,
            "incident_type": incident_type,
            "severity": assessment["severity"],
        },
        "generated_by": "featherless" if os.getenv("FEATHERLESS_API_KEY") else "fallback provider",
        "note": "Operator-requested summary. The incident response itself "
                "is produced deterministically and was not generated.",
    }


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

    @app.get("/incident/copilot/prompts")
    def copilot_prompts_route():
        """The suggested questions, so the console cannot drift from them."""

        return {"prompts": COPILOT_PROMPTS, "advisory": True}

    @app.post("/incident/copilot")
    def copilot_route(request: CopilotRequest,
                      x_api_key: str = Header(default=None)):
        _check_key(x_api_key)

        try:
            return copilot(request.question, request.shared_context,
                           request.approved_docs)

        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        except Exception as e:
            # 503, like the brief: the copilot being unreachable is an
            # inconvenience to an operator, never a failure of the
            # incident response, which was produced without it.
            raise HTTPException(
                status_code=503,
                detail="copilot unavailable (%s: %s). The incident response "
                       "is unaffected -- it is generated deterministically, "
                       "without a model." % (type(e).__name__, str(e)[:160]))

    @app.post("/incident/brief")
    def brief_route(request: IncidentRequest,
                    x_api_key: str = Header(default=None)):
        _check_key(x_api_key)

        try:
            return shift_brief(request)

        except Exception as e:
            # 503, not 500: the incident response is unaffected and the
            # caller can simply try again or do without. Naming the
            # provider matters here -- a 429 from a Chat-tier key means
            # the plan refuses automated traffic, which is a different
            # fix from being out of credit.
            raise HTTPException(
                status_code=503,
                detail="brief unavailable (%s: %s). The incident response "
                       "is unaffected -- it is generated deterministically."
                       % (type(e).__name__, str(e)[:160]))


# ============================================================
# SELF TEST
# ============================================================

def selftest():
    """Exercise the rules directly. No server, no network."""

    # Prints Devanagari now, and a Windows console is cp1252 by default.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

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
    # -- the language field used to be accepted and ignored ----------

    english = assess("NO-Hardhat", "BAY-3", None)

    as_english = localize_response(english, "en")
    check("English asks for no translation",
          as_english["language"] == "en" and not as_english["translated"],
          as_english["reason"])

    hindi = localize_response(english, "hi")
    check("Hindi is translated from the shipped phrase table",
          hindi["translated"] and hindi["language"] == "hi"
          and hindi.get("source") == "phrase table",
          "no key, no network, no model")

    check("every step comes back in Devanagari",
          len(hindi["steps"]) == len(english["steps"])
          and all(not step.isascii() for step in hindi["steps"]),
          "%d steps, none left in English" % len(hindi["steps"]))

    check("the bay id survives translation",
          all("BAY-3" in step for step in hindi["steps"]
              if "{bay}" in "".join(english["steps"]) or "BAY-3" in step),
          "placeholder formatted after translation, not before")

    check("the spoken alert is rebuilt, not string-translated",
          not hindi["spoken_alert"].isascii()
          and "BAY 3" in hindi["spoken_alert"],
          "built from its own frame")

    check("translation does not claim a review that never happened",
          hindi["review"]["reviewed"] is False and hindi["review"]["note"],
          "says it needs a native speaker")

    caustic = assess("NO-Hardhat", "BAY-3", "NAOH")
    hi_caustic = localize_response(caustic, "hi")
    check("the contraindication is translated too",
          hi_caustic["translated"]
          and not hi_caustic["contraindication"].isascii(),
          "the string where an error is not an inconvenience")

    french = localize_response(english, "fr")
    check("an uncovered language falls back and says why",
          not french["translated"] and french["language"] == "en"
          and french["reason"],
          french["reason"][:44])

    check("untranslated text is never labelled as translated",
          french["language"] == "en",
          "language says what the text IS, not what was asked")

    check("a broken translator cannot cost you the incident",
          localize_response({"spoken_alert": "x", "steps": []},
                            "te")["spoken_alert"] == "x",
          "degrades, never raises")

    # -- the copilot: advisory, and structurally unable to act -------

    refused = False
    try:
        copilot("   ")
    except ValueError:
        refused = True
    check("an empty question is refused", refused, "400, not a wasted call")

    refused = False
    try:
        copilot("x" * 2001)
    except ValueError:
        refused = True
    check("an oversized question is refused", refused, "2000 char limit")

    check("the system prompt forbids acting",
          all(p in COPILOT_SYSTEM for p in
              ("NEVER issue autonomous orders", "activate equipment",
               "broadcast alerts", "carried out")),
          "orders, equipment, broadcast, completion claims")

    check("the system prompt forbids inventing chemistry",
          "than supplying it from general knowledge" in COPILOT_SYSTEM,
          "an acknowledged gap beats an invented detail")

    check("every suggested prompt investigates rather than delegates",
          COPILOT_PROMPTS and not any(
              p.strip().lower() in ("what should i do?", "what do i do?",
                                    "fix this", "resolve this")
              for p in COPILOT_PROMPTS),
          "%d prompts, none of them 'what should I do?'" % len(COPILOT_PROMPTS))

    check("the copilot cannot reach anything that acts",
          not any(name in copilot.__code__.co_names
                  for name in ("dispatch_downstream", "post_incident",
                               "webhook_dispatch", "tts_alert", "speak")),
          "no path to dispatch, speech or escalation")

    # -- the console's field names -----------------------------------
    #
    # Measured against the deployed console: every request it sends
    # returned 400 without these, and the console showed DEMO FALLBACK,
    # which reads as a frontend fault and is not one.

    console_shape = IncidentRequest(
        location="Bay-1", substance="Chlorine",
        incident_type="NO-Mask", language="Telugu")

    check("location is accepted as bay_id",
          console_shape.resolved_bay() == "Bay-1", "location -> bay_id")

    check("substance is accepted as substance_name",
          console_shape.resolved_substance_name() == "Chlorine",
          "substance -> substance_name")

    check("a language display name resolves to a code",
          console_shape.resolved_language() == "te", "Telugu -> te")

    check("a language code still passes through",
          IncidentRequest(bay_id="B", language="hi").resolved_language() == "hi",
          "hi -> hi")

    check("the documented shape still wins when both are sent",
          IncidentRequest(bay_id="BAY-9", location="ignored").resolved_bay() == "BAY-9",
          "bay_id takes precedence")

    check("the console's media object does not 422",
          IncidentRequest(location="Bay-1", incident_type="Spill",
                          media={"camera": True}).resolved_bay() == "Bay-1",
          "accepted and ignored")

    check("target_lang is honoured",
          IncidentRequest(bay_id="B", target_lang="bn").resolved_language() == "bn",
          "documented alias")

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
