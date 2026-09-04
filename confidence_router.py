"""
Two-tier confidence routing: act now, or look again before acting.

The existing gate treats every detection above the floor as equal
evidence. It is not. A NO-Hardhat box at 0.92 and one at 0.47 are
different claims, and the measured data says so -- EVAL-ACCURACY.md puts
violation-class precision at 0.827, meaning roughly one in six
detections is wrong, and the wrong ones cluster near the floor.

So confidence splits into two routes:

  HIGH        >= HIGH_CONFIDENCE. Act immediately. The model is as sure
              as it gets, and the cost of waiting is a hazard nobody was
              told about.
  BORDERLINE  between CONFIDENCE_FLOOR and HIGH_CONFIDENCE. Do not act
              yet. Hold it in a verify state and require VERIFY_HITS
              more sightings within VERIFY_WINDOW seconds. Reconfirmed,
              it escalates to a full response. Not reconfirmed, it is
              logged as a suppressed false positive and nothing fires.
  IGNORED     below the floor. Not a detection.

This sits *in front of* the existing TriggerGate rather than replacing
it. The router asks "is this evidence real?"; the gate asks "have we
seen enough of it, and did we already fire?". Both still apply.

**Every decision is recorded with its reasoning.** A router that
silently swallows detections is worse than no router, because you cannot
tell suppression from blindness. `audit_log` holds the last N decisions
and `summary()` counts them, so a demo can show exactly what was
suppressed and why.

Run it:

    python confidence_router.py --selftest    # no camera, no network
    python confidence_router.py --demo        # a scripted stream, narrated
"""

import argparse
import os
import sys
import time
from collections import deque
from datetime import datetime


# ============================================================
# CONFIGURATION
# ============================================================

# At or above this, the detection is acted on immediately.
#
# 0.75 is chosen from the measured distribution, not by feel: on the
# Roboflow test split the violation classes score 0.72-0.92 when they
# are right, and the false positives that survive the 0.45 floor sit
# mostly below 0.7. Tune with HAZARDWATCH_HIGH_CONF and watch the
# suppressed count in summary() -- if real violations are being held,
# lower it; if false alarms are firing, raise it.
HIGH_CONFIDENCE = float(os.getenv("HAZARDWATCH_HIGH_CONF", "0.75"))

# Below this it is not a detection at all. Mirrors the trigger's floor.
CONFIDENCE_FLOOR = float(os.getenv("HAZARDWATCH_CONF", "0.45"))

# A borderline detection needs this many more sightings, within this
# many seconds, before it is treated as real.
#
# Time-based rather than frame-based on purpose: the frame-based gate
# already exists downstream, and expressing this window in seconds means
# it behaves the same at 5 fps on a laptop and 25 fps on better hardware.
VERIFY_HITS = int(os.getenv("HAZARDWATCH_VERIFY_HITS", "3"))
VERIFY_WINDOW = float(os.getenv("HAZARDWATCH_VERIFY_WINDOW", "3.0"))

# How many decisions to keep for the audit trail.
AUDIT_LIMIT = int(os.getenv("HAZARDWATCH_AUDIT_LIMIT", "500"))

# Decision actions.
FIRE = "fire"              # act now
VERIFYING = "verifying"    # held, waiting for reconfirmation
CONFIRMED = "confirmed"    # borderline that reconfirmed -> act now
SUPPRESSED = "suppressed"  # borderline that never reconfirmed -> no action
IGNORED = "ignored"        # below the floor

ACTS = (FIRE, CONFIRMED)   # the two actions that reach the trigger


class Decision(object):
    """One routing decision, with the reasoning that produced it."""

    __slots__ = ("at", "zone", "violation", "confidence", "tier",
                 "action", "reason")

    def __init__(self, at, zone, violation, confidence, tier, action, reason):
        self.at = at
        self.zone = zone
        self.violation = violation
        self.confidence = confidence
        self.tier = tier
        self.action = action
        self.reason = reason

    @property
    def acts(self):
        """Does this decision reach the trigger?"""

        return self.action in ACTS

    def line(self):
        """One readable audit line."""

        return ("%s  %-9s %-14s %-16s conf=%.2f  %s"
                % (datetime.fromtimestamp(self.at).strftime("%H:%M:%S"),
                   self.tier, self.zone, self.violation, self.confidence,
                   self.reason))

    def as_dict(self):
        return {
            "at": self.at, "zone": self.zone, "violation": self.violation,
            "confidence": round(self.confidence, 3), "tier": self.tier,
            "action": self.action, "reason": self.reason,
        }

    def __repr__(self):
        return "<Decision %s %s %.2f %s>" % (
            self.zone, self.violation, self.confidence, self.action)


class ConfidenceRouter(object):
    """
    Routes detections by confidence. Holds borderline ones until they
    reconfirm or expire.

    Time is injected, like TriggerGate, so the verify window can be
    tested without a test that sleeps.
    """

    def __init__(self, high=HIGH_CONFIDENCE, floor=CONFIDENCE_FLOOR,
                 verify_hits=VERIFY_HITS, verify_window=VERIFY_WINDOW,
                 clock=time.monotonic, on_decision=None):
        self.high = high
        self.floor = floor
        self.verify_hits = max(1, verify_hits)
        self.verify_window = verify_window
        self.clock = clock
        self.on_decision = on_decision

        self._pending = {}   # (zone, violation) -> {"first": t, "hits": n, "best": conf}
        self.audit_log = deque(maxlen=AUDIT_LIMIT)

    # -- internals ---------------------------------------------------

    def _record(self, zone, violation, confidence, tier, action, reason):
        decision = Decision(time.time(), zone, violation, confidence,
                            tier, action, reason)
        self.audit_log.append(decision)

        if self.on_decision:
            self.on_decision(decision)

        return decision

    def _expire(self, zone, now):
        """
        Retire borderline holds whose window has closed without enough
        confirmations. Returns their decisions.

        This is what makes suppression visible: a detection that quietly
        goes away still produces an audit entry saying so.
        """

        expired = []

        for key in list(self._pending):
            if key[0] != zone:
                continue

            state = self._pending[key]

            if (now - state["first"]) <= self.verify_window:
                continue

            del self._pending[key]
            expired.append(self._record(
                zone, key[1], state["best"], "borderline", SUPPRESSED,
                "seen %d time(s) in %.1fs, needed %d -- suppressed as a "
                "likely false positive, no action taken"
                % (state["hits"], self.verify_window, self.verify_hits)))

        return expired

    # -- the entry point ---------------------------------------------

    def route(self, zone, detections):
        """
        Feed one frame's detections in; get back every decision made.

        `detections` maps violation label -> confidence for this frame.
        Callers act on the decisions where `.acts` is True and can show
        the rest to explain what the system chose not to do.
        """

        now = self.clock()
        decisions = list(self._expire(zone, now))

        for violation in sorted(detections or {}):
            confidence = float(detections[violation])
            key = (zone, violation)

            if confidence < self.floor:
                decisions.append(self._record(
                    zone, violation, confidence, "ignored", IGNORED,
                    "below the %.2f floor -- not treated as a detection"
                    % self.floor))
                continue

            if confidence >= self.high:
                # A high-confidence sighting also clears any pending
                # hold: the question it was waiting to answer is settled.
                self._pending.pop(key, None)
                decisions.append(self._record(
                    zone, violation, confidence, "high", FIRE,
                    "at or above the %.2f high-confidence line -- acting "
                    "immediately" % self.high))
                continue

            # Borderline.
            state = self._pending.get(key)

            if state is None or (now - state["first"]) > self.verify_window:
                self._pending[key] = {"first": now, "hits": 1, "best": confidence}
                decisions.append(self._record(
                    zone, violation, confidence, "borderline", VERIFYING,
                    "between %.2f and %.2f -- holding for %d more sighting(s) "
                    "within %.1fs before acting"
                    % (self.floor, self.high, self.verify_hits - 1,
                       self.verify_window)))
                continue

            state["hits"] += 1
            state["best"] = max(state["best"], confidence)

            if state["hits"] < self.verify_hits:
                decisions.append(self._record(
                    zone, violation, confidence, "borderline", VERIFYING,
                    "sighting %d of %d within %.1fs -- still holding"
                    % (state["hits"], self.verify_hits, self.verify_window)))
                continue

            elapsed = now - state["first"]
            del self._pending[key]
            decisions.append(self._record(
                zone, violation, state["best"], "borderline", CONFIRMED,
                "reconfirmed %d times in %.1fs (best conf %.2f) -- escalating "
                "to full response" % (state["hits"], elapsed, state["best"])))

        return decisions

    def acting(self, zone, detections):
        """
        Convenience: the violations that should reach the trigger now,
        as {violation: confidence}. Decisions still land in audit_log.
        """

        return {d.violation: d.confidence
                for d in self.route(zone, detections) if d.acts}

    def pending(self):
        """(zone, violation) pairs currently held for verification."""

        return sorted(self._pending)

    def summary(self):
        """Counts by action, for a demo or an end-of-run report."""

        counts = {}

        for decision in self.audit_log:
            counts[decision.action] = counts.get(decision.action, 0) + 1

        return counts

    def print_audit(self, limit=25):
        """The reasoning trail, most recent last."""

        entries = list(self.audit_log)[-limit:]

        if not entries:
            print("  (no decisions recorded)")
            return

        for decision in entries:
            print("  " + decision.line())

        counts = self.summary()
        print("\n  %s" % ", ".join("%s=%d" % kv for kv in sorted(counts.items())))


# ============================================================
# SELF TEST
# ============================================================

def selftest():
    """Fake clock, no camera, no network."""

    checks = []

    def check(name, condition, detail=""):
        checks.append(bool(condition))
        print("  %s  %-44s %s" % ("PASS" if condition else "FAIL", name, detail))

    now = [1000.0]
    router = ConfidenceRouter(high=0.75, floor=0.45, verify_hits=3,
                              verify_window=3.0, clock=lambda: now[0])

    high = router.route("BAY-3", {"NO-Hardhat": 0.92})
    check("high confidence fires at once",
          len(high) == 1 and high[0].action == FIRE and high[0].acts,
          high[0].reason[:56])

    low = router.route("BAY-3", {"NO-Mask": 0.20})
    check("below the floor is ignored",
          low[0].action == IGNORED and not low[0].acts, low[0].reason[:56])

    # Borderline: one sighting must NOT act.
    first = router.route("BAY-7", {"NO-Mask": 0.55})
    check("borderline does not fire on first sight",
          first[0].action == VERIFYING and not first[0].acts,
          first[0].reason[:60])

    check("borderline is held as pending",
          ("BAY-7", "NO-Mask") in router.pending(), str(router.pending()))

    now[0] += 0.4
    second = router.route("BAY-7", {"NO-Mask": 0.58})
    check("second sighting still holds",
          second[0].action == VERIFYING and not second[0].acts,
          second[0].reason[:44])

    now[0] += 0.4
    third = router.route("BAY-7", {"NO-Mask": 0.61})
    check("third sighting confirms and escalates",
          third[0].action == CONFIRMED and third[0].acts, third[0].reason[:60])

    check("confirmed carries the best confidence seen",
          abs(third[0].confidence - 0.61) < 1e-9, "%.2f" % third[0].confidence)

    check("confirmed clears the hold",
          ("BAY-7", "NO-Mask") not in router.pending(), str(router.pending()))

    # The case the whole module exists for: a borderline blip that never
    # comes back must be suppressed, and must SAY it was suppressed.
    blip = ConfidenceRouter(high=0.75, floor=0.45, verify_hits=3,
                            verify_window=3.0, clock=lambda: now[0])
    blip.route("BAY-9", {"NO-Safety Vest": 0.52})

    now[0] += 5.0
    after = blip.route("BAY-9", {})

    check("unconfirmed borderline is suppressed",
          len(after) == 1 and after[0].action == SUPPRESSED and not after[0].acts,
          after[0].reason[:62])

    check("suppression is auditable, not silent",
          any(d.action == SUPPRESSED for d in blip.audit_log)
          and blip.summary().get(SUPPRESSED) == 1,
          "summary: %s" % blip.summary())

    check("suppressed fires nothing",
          not any(d.acts for d in blip.audit_log),
          "%d decisions, 0 actions" % len(blip.audit_log))

    # A high sighting mid-verification settles the question immediately.
    jump = ConfidenceRouter(high=0.75, floor=0.45, verify_hits=3,
                            verify_window=3.0, clock=lambda: now[0])
    jump.route("BAY-1", {"NO-Hardhat": 0.50})
    escalated = jump.route("BAY-1", {"NO-Hardhat": 0.88})

    check("a high sighting settles a pending hold",
          escalated[0].action == FIRE and not jump.pending(),
          "no longer waiting")

    # acting() is what the camera loop uses.
    ready = router.acting("BAY-2", {"NO-Hardhat": 0.91, "NO-Mask": 0.50})
    check("acting() returns only what should fire",
          ready == {"NO-Hardhat": 0.91},
          "borderline NO-Mask withheld: %s" % sorted(ready))

    check("every decision has a reason",
          all(d.reason and d.tier and d.action for d in router.audit_log),
          "%d decisions, all explained" % len(router.audit_log))

    print("\n%d/%d router checks passed" % (sum(checks), len(checks)))
    return 0 if all(checks) else 1


def demo():
    """A scripted stream, narrated, so the routing is visible."""

    now = [time.time()]
    router = ConfidenceRouter(high=0.75, floor=0.45, verify_hits=3,
                              verify_window=3.0, clock=lambda: now[0])

    script = [
        (0.0, "BAY-3", {"NO-Hardhat": 0.91}, "a clear violation"),
        (0.3, "BAY-3", {}, "gone"),
        (0.6, "BAY-7", {"NO-Mask": 0.52}, "a marginal box appears"),
        (1.0, "BAY-7", {}, "and vanishes -- a flicker"),
        (5.0, "BAY-7", {}, "window closes"),
        (5.4, "BAY-9", {"NO-Safety Vest": 0.58}, "another marginal box"),
        (5.8, "BAY-9", {"NO-Safety Vest": 0.61}, "still there"),
        (6.2, "BAY-9", {"NO-Safety Vest": 0.57}, "and again"),
        (6.6, "BAY-1", {"NO-Hardhat": 0.31}, "noise below the floor"),
    ]

    print("Confidence routing -- high >= %.2f, borderline %.2f-%.2f, "
          "%d hits in %.0fs\n" % (router.high, router.floor, router.high,
                                  router.verify_hits, router.verify_window))

    base = now[0]

    for offset, zone, detections, note in script:
        now[0] = base + offset
        decisions = router.route(zone, detections)

        print("t+%4.1fs  %-10s %-24s %s"
              % (offset, zone,
                 ", ".join("%s %.2f" % kv for kv in sorted(detections.items()))
                 or "(nothing)", note))

        for decision in decisions:
            marker = ">>" if decision.acts else "  "
            print("        %s %-10s %s" % (marker, decision.action, decision.reason))

    print("\nsummary: %s" % ", ".join("%s=%d" % kv
                                      for kv in sorted(router.summary().items())))
    print("\n>> marks the decisions that reached the trigger. Everything else")
    print("   was held or suppressed, and every line above says why.")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Two-tier confidence routing in front of the trigger gate.")
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--demo", action="store_true",
                        help="narrate a scripted detection stream")

    args = parser.parse_args(argv)

    if args.demo:
        return demo()

    return selftest()


if __name__ == "__main__":
    sys.exit(main())
