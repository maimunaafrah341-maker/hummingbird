"""
Turn one incident into a PDF somebody can sign, file, or hand to an
inspector.

The report is generated from two things: the hazard event the trigger
built, and the response the incident service returned. Everything on
the page is one of those two, or is labelled as coming from somewhere
else -- which matters most for the regulatory citation.

**About the citation.** The incident response shape does not carry one,
so if the service supplies `regulatory_citation` this uses it, and
otherwise it matches the hazard type against the table below and says
so on the page. The table holds real 29 CFR standards. It is not
exhaustive and it is not legal advice; it is a starting reference so
the report points a human at the right part of the rulebook instead of
inventing a citation, which is the one failure mode a compliance
document must not have. The page always states which of the two
sources it used.

Run it:

    python dossier.py --demo            # a sample report, then open it
    python dossier.py --selftest        # generate + verify, no viewer
"""

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (KeepTogether, Paragraph, SimpleDocTemplate,
                                Spacer, Table, TableStyle)


# ============================================================
# CONFIGURATION
# ============================================================

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "outputs")

# Real 29 CFR standards, matched by violation label. Used only when the
# incident service does not supply a citation of its own. Keys are
# matched case-insensitively as substrings of the hazard type, longest
# first, so "NO-Hardhat" finds "hardhat" and an unknown hazard falls
# through to the general PPE requirement rather than to nothing.
CITATIONS = {
    "hardhat": ("29 CFR 1910.135", "Head protection"),
    "helmet": ("29 CFR 1910.135", "Head protection"),
    "mask": ("29 CFR 1910.134", "Respiratory protection"),
    "respirator": ("29 CFR 1910.134", "Respiratory protection"),
    "goggles": ("29 CFR 1910.133", "Eye and face protection"),
    "glasses": ("29 CFR 1910.133", "Eye and face protection"),
    "gloves": ("29 CFR 1910.138", "Hand protection"),
    "vest": ("29 CFR 1910.132", "PPE, general requirements"),
    "boots": ("29 CFR 1910.136", "Foot protection"),
    "spill": ("29 CFR 1910.120", "Hazardous waste operations and emergency response"),
    "chemical": ("29 CFR 1910.1200", "Hazard communication"),
}

FALLBACK_CITATION = ("29 CFR 1910.132", "PPE, general requirements")

# Severity -> colour for the badge. Unknown severities render grey and
# are printed verbatim, so a value nobody anticipated still shows up
# rather than being silently mapped to "low".
SEVERITY_COLOURS = {
    "critical": colors.HexColor("#B3261E"),
    "high": colors.HexColor("#C2410C"),
    "medium": colors.HexColor("#A16207"),
    "moderate": colors.HexColor("#A16207"),
    "low": colors.HexColor("#15803D"),
    "info": colors.HexColor("#3F51B5"),
}

UNKNOWN_SEVERITY_COLOUR = colors.HexColor("#52525B")


# Fonts that can actually draw Devanagari, Bengali, Telugu and Arabic.
# reportlab's built-in Helvetica cannot: it is WinAnsi-encoded, so a
# Hindi step silently renders as nothing at all. On a report whose whole
# purpose is telling someone what to do about a hazard, dropping the
# instructions and still producing a valid-looking PDF is the worst
# available outcome, so this is checked and reported rather than hoped.
#
# (name, path, subfont index for .ttc collections)
UNICODE_FONT_CANDIDATES = [
    ("NirmalaUI", r"C:\Windows\Fonts\Nirmala.ttc", 0),      # Indic scripts
    ("ArialUnicode", r"C:\Windows\Fonts\ARIALUNI.TTF", None),
    ("SegoeUI", r"C:\Windows\Fonts\segoeui.ttf", None),     # Arabic/Urdu
    ("NotoSans", "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf", None),
    ("DejaVuSans", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", None),
]

_unicode_font = "unchecked"   # cached: font name, or None if none worked


def unrenderable(text):
    """Characters the built-in Helvetica cannot draw. Empty means fine."""

    bad = set()

    for char in str(text or ""):
        try:
            char.encode("cp1252")       # what WinAnsi/Helvetica covers
        except UnicodeEncodeError:
            bad.add(char)

    return bad


def unicode_font():
    """
    Register and return a font that can draw non-Latin scripts, or None.

    Cached: registration is idempotent but the file probing is not free,
    and this is called per report.
    """

    global _unicode_font

    if _unicode_font != "unchecked":
        return _unicode_font

    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    _unicode_font = None

    for name, path, subfont in UNICODE_FONT_CANDIDATES:
        if not os.path.exists(path):
            continue

        try:
            if subfont is None:
                pdfmetrics.registerFont(TTFont(name, path))
            else:
                pdfmetrics.registerFont(TTFont(name, path, subfontIndex=subfont))

            _unicode_font = name
            break

        except Exception:
            continue      # try the next candidate; absence is handled by the caller

    return _unicode_font


def resolve_citation(event, response):
    """
    Return (citation, title, source).

    `source` is "incident service" or "local reference table" and is
    printed on the page. A reader must always be able to tell whether a
    human system asserted this citation or whether this script matched
    it from a keyword.
    """

    supplied = (response.get("regulatory_citation")
                or response.get("citation")
                or response.get("regulation"))

    if supplied:
        if isinstance(supplied, dict):
            return (supplied.get("code", ""), supplied.get("title", ""),
                    "incident service")

        return (str(supplied), "", "incident service")

    hazard = "%s %s" % (event.get("hazard_type") or "", event.get("substance") or "")
    hazard = hazard.lower()

    for keyword in sorted(CITATIONS, key=len, reverse=True):
        if keyword in hazard:
            code, title = CITATIONS[keyword]
            return code, title, "local reference table"

    code, title = FALLBACK_CITATION
    return code, title, "local reference table (no keyword matched)"


def incident_id(event):
    """Stable, readable id: HW-YYYYMMDD-XXXXXX."""

    stamp = (event.get("timestamp") or "")[:10].replace("-", "") or \
        datetime.now(timezone.utc).strftime("%Y%m%d")

    seed = json.dumps(event, sort_keys=True, default=str).encode("utf-8")
    return "HW-%s-%s" % (stamp, hashlib.sha256(seed).hexdigest()[:6].upper())


def _as_steps(steps):
    """Normalise whatever `steps` came back as into a list of strings."""

    if steps is None:
        return []

    if isinstance(steps, str):
        steps = steps.splitlines()

    # Drop any numbering or bullet the service already applied. The page
    # numbers the steps itself, so leaving "1." on the text renders as
    # "1. 1. Evacuate".
    cleaned = []

    for step in steps:
        step = re.sub(r"^\s*(?:\d+[.)]|[-*•])\s*", "", str(step)).strip()

        if step:
            cleaned.append(step)

    return cleaned


# ============================================================
# THE REPORT
# ============================================================

def build_dossier(event, response, out_dir=None, filename=None, open_after=False):
    """
    Write the incident PDF and return its path.

    `event` is what yolo_trigger built; `response` is what the incident
    service returned. Missing fields render as "not recorded" rather
    than raising -- a report with a gap in it is still evidence, a
    traceback is not.
    """

    out_dir = out_dir or OUTPUT_DIR
    os.makedirs(out_dir, exist_ok=True)

    event = dict(event or {})
    response = dict(response or {})

    ident = incident_id(event)
    path = os.path.join(out_dir, filename or ("%s.pdf" % ident))

    severity = str(response.get("severity") or "unrecorded")
    colour = SEVERITY_COLOURS.get(severity.lower(), UNKNOWN_SEVERITY_COLOUR)
    code, title, source = resolve_citation(event, response)

    # Does anything on this page need a script Helvetica cannot draw?
    page_text = " ".join(str(v) for v in list(event.values()) + list(response.values()))
    exotic = unrenderable(page_text)
    body_font = None
    font_warning = None

    if exotic:
        body_font = unicode_font()

        if body_font is None:
            # Say so on the page. A report that silently omits the
            # instructions is worse than one that admits it could not
            # print them.
            font_warning = (
                "This report contains text in a script this machine has no "
                "font for (%s). Those passages are missing from the page "
                "below. Install a Unicode font, or read this incident in "
                "English." % ", ".join(sorted(exotic))[:80])

    base = getSampleStyleSheet()

    title_style = ParagraphStyle(
        "title", parent=base["Title"], fontSize=19, leading=23,
        fontName=body_font or base["Title"].fontName,
        alignment=TA_LEFT, spaceAfter=2, textColor=colors.HexColor("#18181B"))

    subtitle_style = ParagraphStyle(
        "subtitle", parent=base["Normal"], fontSize=9.5, leading=13,
        textColor=colors.HexColor("#71717A"))

    heading_style = ParagraphStyle(
        "heading", parent=base["Normal"], fontName=body_font or "Helvetica-Bold",
        fontSize=10, leading=14, spaceBefore=13, spaceAfter=5,
        textColor=colors.HexColor("#3F3F46"))

    body_style = ParagraphStyle(
        "body", parent=base["Normal"], fontSize=10, leading=14.5,
        fontName=body_font or base["Normal"].fontName)

    step_style = ParagraphStyle(
        "step", parent=body_style, leftIndent=15, spaceAfter=5)

    warn_style = ParagraphStyle(
        "warn", parent=body_style,
        fontName=body_font or "Helvetica-Bold",
        textColor=colors.HexColor("#7F1D1D"), fontSize=10.5, leading=15)

    footer_style = ParagraphStyle(
        "footer", parent=base["Normal"], fontSize=7.8, leading=11,
        textColor=colors.HexColor("#71717A"))

    story = []

    # -- header ------------------------------------------------------
    story.append(Paragraph("Hazard Incident Report", title_style))
    story.append(Paragraph(
        "HazardWatch OS &nbsp;&middot;&nbsp; %s &nbsp;&middot;&nbsp; "
        "generated %s" % (ident, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        subtitle_style))
    story.append(Spacer(1, 9))

    severity_table = Table(
        [[Paragraph('<font color="white"><b>SEVERITY: %s</b></font>'
                    % severity.upper(), body_style)]],
        colWidths=[165 * mm])
    severity_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colour),
        ("LEFTPADDING", (0, 0), (-1, -1), 9),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(severity_table)

    if font_warning:
        missing = Table([[Paragraph(font_warning, body_style)]], colWidths=[165 * mm])
        missing.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#FEF9C3")),
            ("BOX", (0, 0), (-1, -1), 0.9, colors.HexColor("#A16207")),
            ("LEFTPADDING", (0, 0), (-1, -1), 9),
            ("RIGHTPADDING", (0, 0), (-1, -1), 9),
            ("TOPPADDING", (0, 0), (-1, -1), 7),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ]))
        story.append(Spacer(1, 10))
        story.append(missing)

    # -- the facts ---------------------------------------------------
    def value(*keys, **kwargs):
        for key in keys:
            if event.get(key) not in (None, ""):
                return str(event[key])

        return kwargs.get("default", "not recorded")

    confidence = event.get("confidence")

    facts = [
        ("Bay / zone", value("bay", "zone")),
        ("Hazard", value("hazard_type", "violation")),
        ("Substance", value("substance")),
        ("Detected at", value("timestamp")),
        ("Triggered by", "%s%s" % (
            value("source"),
            "" if confidence is None else "  (confidence %.0f%%)" % (float(confidence) * 100))),
        ("Camera", value("camera_id", default="n/a (kiosk trigger)")),
        ("Alert language", value("language", default="en")),
    ]

    story.append(Paragraph("Incident", heading_style))

    facts_table = Table([[Paragraph("<b>%s</b>" % k, body_style),
                          Paragraph(v, body_style)] for k, v in facts],
                        colWidths=[42 * mm, 123 * mm])
    facts_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 3.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
        ("LINEBELOW", (0, 0), (-1, -2), 0.4, colors.HexColor("#E4E4E7")),
    ]))
    story.append(facts_table)

    # -- contraindication, before the steps --------------------------
    # Deliberately above the instructions: "do not use water" has to be
    # read before step 1, not discovered after it.
    contraindication = response.get("contraindication")

    if contraindication:
        warning = Table(
            [[Paragraph("DO NOT: %s" % contraindication, warn_style)]],
            colWidths=[165 * mm])
        warning.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#FEF2F2")),
            ("BOX", (0, 0), (-1, -1), 1.1, colors.HexColor("#B3261E")),
            ("LEFTPADDING", (0, 0), (-1, -1), 9),
            ("RIGHTPADDING", (0, 0), (-1, -1), 9),
            ("TOPPADDING", (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ]))
        story.append(Spacer(1, 13))
        story.append(KeepTogether(warning))

    # -- response steps ----------------------------------------------
    steps = _as_steps(response.get("steps"))

    story.append(Paragraph("Response steps", heading_style))

    if steps:
        for index, step in enumerate(steps, 1):
            story.append(Paragraph("<b>%d.</b>&nbsp; %s" % (index, step), step_style))
    else:
        story.append(Paragraph("No steps were returned by the incident service.",
                               body_style))

    # -- spoken alert, transcribed -----------------------------------
    if response.get("spoken_alert"):
        story.append(Paragraph("Spoken alert (as broadcast)", heading_style))
        story.append(Paragraph("&ldquo;%s&rdquo;" % response["spoken_alert"], body_style))

    # -- citation ----------------------------------------------------
    story.append(Paragraph("Regulatory reference", heading_style))
    story.append(Paragraph(
        "<b>%s</b>%s" % (code, " &mdash; %s" % title if title else ""), body_style))
    story.append(Paragraph("Source: %s." % source, footer_style))

    story.append(Spacer(1, 16))
    story.append(Paragraph(
        "Generated automatically by HazardWatch OS from a %s trigger. "
        "The regulatory reference above is a pointer to the applicable "
        "standard, not a compliance determination or legal advice; a "
        "qualified person must review this report before it is filed or "
        "acted on as a finding." % value("source", default="system"),
        footer_style))

    SimpleDocTemplate(
        path, pagesize=A4,
        leftMargin=22 * mm, rightMargin=23 * mm,
        topMargin=20 * mm, bottomMargin=18 * mm,
        title="Hazard Incident Report %s" % ident,
        author="HazardWatch OS",
    ).build(story)

    if open_after:
        open_pdf(path)

    return path


def open_pdf(path):
    """Open the PDF in whatever the OS uses. Never raises."""

    try:
        if sys.platform == "win32":
            os.startfile(path)  # noqa: S606 -- intended: hand it to the shell
        elif sys.platform == "darwin":
            import subprocess
            subprocess.run(["open", path], check=False)
        else:
            import subprocess
            subprocess.run(["xdg-open", path], check=False)

        return True

    except Exception as e:
        print("  could not open %s: %s" % (path, e), file=sys.stderr)
        return False


# ============================================================
# SAMPLE + SELF TEST
# ============================================================

SAMPLE_EVENT = {
    "zone": "BAY-3", "bay": "BAY-3",
    "hazard_type": "NO-Hardhat", "violation": "NO-Hardhat",
    "substance": "Sodium hydroxide (50% solution)",
    "source": "camera", "confidence": 0.91, "camera_id": "0",
    "timestamp": "2026-09-04T11:42:07+00:00", "language": "en",
}

SAMPLE_RESPONSE = {
    "severity": "high",
    "steps": [
        "Stop work in BAY-3 and clear personnel to the upwind muster point.",
        "Isolate the sodium hydroxide line at the bay shutoff valve.",
        "Issue a hardhat and face shield before anyone re-enters the bay.",
        "Log the exposure window and notify the shift safety officer.",
    ],
    "contraindication": "Do not flush the spill with water under pressure -- "
                        "it will generate heat and spatter caustic solution.",
    "spoken_alert": "Hazard in bay 3. Caustic spill. Clear the bay upwind and "
                    "wait for the safety officer.",
}


def selftest():
    """Generate a report and verify what came out. Opens nothing."""

    checks = []

    def check(name, condition, detail=""):
        checks.append(bool(condition))
        print("  %s  %-36s %s" % ("PASS" if condition else "FAIL", name, detail))

    path = build_dossier(SAMPLE_EVENT, SAMPLE_RESPONSE)
    size = os.path.getsize(path) if os.path.exists(path) else 0

    check("pdf written", size > 1200, "%s (%d bytes)" % (os.path.basename(path), size))

    with open(path, "rb") as handle:
        head = handle.read(5)

    check("is a real pdf", head == b"%PDF-", repr(head))

    ident = incident_id(SAMPLE_EVENT)
    check("id is stable", ident == incident_id(SAMPLE_EVENT), ident)

    moved = dict(SAMPLE_EVENT, zone="BAY-9", bay="BAY-9")
    check("id varies with the event", incident_id(moved) != ident, incident_id(moved))

    code, title, source = resolve_citation(SAMPLE_EVENT, SAMPLE_RESPONSE)
    check("citation matched from hazard", code == "29 CFR 1910.135",
          "%s -- %s (%s)" % (code, title, source))

    code, _, source = resolve_citation(
        SAMPLE_EVENT, dict(SAMPLE_RESPONSE, regulatory_citation="29 CFR 1910.1200"))
    check("service citation wins", code == "29 CFR 1910.1200" and source == "incident service",
          source)

    code, _, source = resolve_citation({"hazard_type": "SOMETHING-NEW"}, {})
    check("unknown hazard falls back", code == "29 CFR 1910.132",
          "%s (%s)" % (code, source))

    # The empty case matters: a service that returns nothing useful must
    # still produce a filable page, not a traceback.
    bare = build_dossier({"zone": "BAY-1"}, {}, filename="selftest_bare.pdf")
    check("survives an empty response", os.path.getsize(bare) > 1200,
          os.path.basename(bare))

    # The service may number its own steps and the page numbers them
    # again, so the numbering has to come off here or it renders
    # "1. 1. Evacuate".
    steps = _as_steps("1. Evacuate\n2) Isolate the valve\n\n- Call it in")
    check("strips service numbering",
          steps == ["Evacuate", "Isolate the valve", "Call it in"], str(steps))

    check("accepts a list unchanged",
          _as_steps(["Evacuate", "  Isolate  ", ""]) == ["Evacuate", "Isolate"],
          str(_as_steps(["Evacuate", "  Isolate  ", ""])))

    # -- non-Latin scripts -------------------------------------------
    # This project's alerts are hi/bn/te/ur. reportlab's built-in
    # Helvetica is WinAnsi and silently draws nothing for those, so a
    # Hindi report used to come out as a valid PDF with the safety steps
    # simply absent. Checked here because "the PDF generated" and "the
    # instructions are on it" were, for a while, different claims.
    hindi_steps = ["बे 3 में काम बंद करें।"]
    hindi = dict(SAMPLE_RESPONSE, steps=hindi_steps,
                 contraindication="दबाव वाले पानी से न धोएं।")

    check("detects unrenderable text",
          len(unrenderable(hindi_steps[0])) > 0 and not unrenderable("Bay 3 clear"),
          "%d chars need a real font" % len(unrenderable(hindi_steps[0])))

    hindi_path = build_dossier(dict(SAMPLE_EVENT, language="hi"), hindi,
                               filename="selftest_hindi.pdf")
    embedded = open(hindi_path, "rb").read().decode("latin-1", "ignore")
    font = unicode_font()

    if font:
        check("hindi report embeds a usable font", font in embedded,
              "%s, %.1f KB" % (font, os.path.getsize(hindi_path) / 1024))
    else:
        # No Unicode font on this machine. The requirement is then that
        # the page SAYS the text is missing, not that it renders.
        check("missing font is declared on the page",
              "no font for" in embedded or "script this machine" in embedded,
              "no Unicode font installed; page must admit it")

    print("\n%d/%d dossier checks passed" % (sum(checks), len(checks)))
    print("  wrote: %s" % path)
    return 0 if all(checks) else 1


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Generate a PDF incident report into ./outputs.")
    parser.add_argument("--demo", action="store_true",
                        help="build a sample report and open it")
    parser.add_argument("--selftest", action="store_true",
                        help="build and verify, open nothing")
    parser.add_argument("--event", help="path to a JSON file holding the hazard event")
    parser.add_argument("--response", help="path to a JSON file holding the /incident response")
    parser.add_argument("--out", default=None, help="output directory (default ./outputs)")

    args = parser.parse_args(argv)

    if args.selftest:
        return selftest()

    if args.event or args.response:
        def load(p):
            if not p:
                return {}
            with open(p, encoding="utf-8") as handle:
                return json.load(handle)

        path = build_dossier(load(args.event), load(args.response),
                             out_dir=args.out, open_after=True)
    else:
        path = build_dossier(SAMPLE_EVENT, SAMPLE_RESPONSE,
                             out_dir=args.out, open_after=args.demo)

    print("  wrote %s" % path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
