"""
Speak the alert out loud, from Python, with nobody touching anything.

Explicitly not the Web Speech API. A browser will not speak until the
tab has been interacted with, it picks its own voice per machine, and
it can silently do nothing behind an autoplay policy -- three ways for
the loudest moment of a live demo to produce silence. This runs in the
process that detected the hazard and drives the machine's own audio.

Two backends, chosen by what the language actually needs:

  pyttsx3   Offline, via the OS speech engine. Zero network, starts
            instantly. Limited to voices the machine has installed --
            on a stock Windows box that is English and nothing else.
  gTTS      Google Translate's TTS. Covers hi/bn/te/ur properly, but
            needs the network at the moment you speak.

The interesting case is the third one: a demo in a room with bad wifi,
in Hindi. So every synthesis is cached to disk by (text, language), and
a cache hit never touches the network. Warm the cache while you still
have signal:

    python tts_alert.py --prefetch alerts.txt --lang hi

and the live run plays a local file. That is the difference between a
demo that degrades and a demo that stops.

Run it:

    python tts_alert.py "Bay 3 evacuated. Do not use water."
    python tts_alert.py "बे 3 खाली करें" --lang hi
    python tts_alert.py --list-voices
    python tts_alert.py --selftest         # synthesis + cache, silent
"""

import argparse
import hashlib
import inspect
import os
import re
import sys


# ============================================================
# CONFIGURATION
# ============================================================

# Cached audio lives here. Kept out of git (see .gitignore) -- it is
# regenerable, and mp3s do not belong in a repo.
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "outputs", "tts_cache")

# Languages the offline engine is allowed to claim. Anything else goes
# to gTTS even if some voice on the machine nominally reports it,
# because a wrong-language voice reading Hindi phonetically as English
# is worse than a network wait.
OFFLINE_OK_PREFIXES = ("en",)

# Speaking rate for the offline engine. The default is a shade fast for
# an emergency instruction someone has to act on.
OFFLINE_RATE = int(os.getenv("HAZARDWATCH_TTS_RATE", "165"))

# Set HAZARDWATCH_TTS=pyttsx3|gtts to force one backend. Unset picks by
# language, which is what you want.
FORCED_BACKEND = os.getenv("HAZARDWATCH_TTS") or None

# Cached only for *probing* which voices exist -- never for speaking.
# speak_offline() builds its own engine per utterance; see the note
# there about what sharing one across calls silently does.
_engine = None


def _cache_path(text, language):
    """Stable filename for a (text, language) pair."""

    digest = hashlib.sha256(("%s|%s" % (language, text)).encode("utf-8")).hexdigest()[:16]
    return os.path.join(CACHE_DIR, "%s_%s.mp3" % (language, digest))


# ============================================================
# BACKENDS
# ============================================================

def _offline_voice_for(language):
    """
    An installed voice matching `language`, or None.

    Matching is token-exact, never substring. A substring test looks
    reasonable and is quietly wrong: the Windows voice id
    "HKEY_LOCAL_MACHINE\\...\\TTS_MS_EN-US_DAVID_11.0" contains "hi"
    inside "MACHINE", so `"hi" in voice_id` hands you an American
    English voice and it reads the Hindi alert phonetically. A wrong
    language spoken confidently is worse than no audio.
    """

    global _engine

    _init_com()

    import pyttsx3

    if _engine is None:
        _engine = pyttsx3.init()

    wanted = language.lower().split("-")[0]

    for voice in _engine.getProperty("voices"):
        tags = []

        for tag in (getattr(voice, "languages", None) or []):
            # Windows reports these as str, some platforms as bytes.
            tags.append(tag.decode("utf-8", "ignore") if isinstance(tag, bytes) else str(tag))

        # Voice metadata is unreliable across platforms, so fall back to
        # the id and name -- but split them into tokens first, so "EN"
        # in TTS_MS_EN-US matches and "hi" in MACHINE does not.
        tags += re.split(r"[^A-Za-z]+", "%s %s" % (voice.id or "", voice.name or ""))

        for tag in tags:
            if tag.lower().split("-")[0] == wanted:
                return voice

    return None


def _init_com():
    """
    Initialise COM for this thread, on Windows.

    pyttsx3's Windows driver is SAPI5 over COM, and COM is per-thread.
    A worker thread that never calls this gets an engine that accepts
    say() and runAndWait() and returns immediately having produced no
    sound -- success by every check the caller can make, and silence in
    the room. Harmless everywhere else.
    """

    if sys.platform != "win32":
        return

    try:
        import pythoncom
        pythoncom.CoInitialize()
    except Exception:
        pass      # already initialised, or pywin32 absent; speak() will find out


def speak_offline(text, language="en"):
    """Speak via the OS engine. Returns True if it spoke."""

    voice = _offline_voice_for(language)

    if voice is None:
        return False

    # pyttsx3.Engine(), never pyttsx3.init().
    #
    # init() returns a process-wide SINGLETON, and that engine speaks
    # exactly once. Measured: first utterance 4.2s and audible, second
    # 0.3s and silent, third 0.2s and silent -- on the main thread, with
    # no threads involved at all. Every call returns success. So a demo
    # announces its first violation and then goes quiet for the rest of
    # the session while every check still reports ok.
    #
    # Engine() bypasses that cache: 3.9s, 3.8s, 3.8s, all audible. The
    # ~0.3s of extra construction is worth it to not lose every alert
    # after the first.
    _init_com()

    import pyttsx3

    engine = pyttsx3.Engine(driverName=None, debug=False)

    try:
        engine.setProperty("voice", voice.id)
        engine.setProperty("rate", OFFLINE_RATE)
        engine.say(text)
        engine.runAndWait()
        return True

    finally:
        try:
            engine.stop()
        except Exception:
            pass


def synthesize(text, language="en", refresh=False):
    """
    Render `text` to an mp3 on disk and return its path.

    Cached by (text, language): a second call for the same alert is a
    file check, not a network round trip. This is the function to call
    ahead of a demo -- see --prefetch.
    """

    from gtts import gTTS

    os.makedirs(CACHE_DIR, exist_ok=True)
    path = _cache_path(text, language)

    if os.path.exists(path) and os.path.getsize(path) > 0 and not refresh:
        return path

    gTTS(text=text, lang=language).save(path)
    return path


def play_file(path, block=True):
    """Play an audio file through the machine's speakers."""

    from playsound3 import playsound

    playsound(path, block=block)
    return True


# ============================================================
# THE ONE ENTRY POINT
# ============================================================

def speak(text, language="en", backend=None, block=True, allow_network=True):
    """
    Say `text` out loud in `language`. Never raises.

    Returns a dict describing what actually happened, because "the
    alert was spoken" and "the alert function returned" are different
    claims and only the first one matters in a demo:

        {"ok": bool, "backend": str, "path": str|None, "reason": str|None}

    Order of preference:
      1. A cached mp3 for this exact alert -- offline, instant.
      2. The OS engine, if it has a voice for this language.
      3. gTTS, if the network is up.
    """

    text = (text or "").strip()

    if not text:
        return {"ok": False, "backend": None, "path": None, "reason": "empty text"}

    backend = backend or FORCED_BACKEND
    language = (language or "en").lower()
    attempts = []

    # 1. A cache hit is both the fastest path and the only one that
    #    works with the wifi off, so it is checked before anything else.
    if backend != "pyttsx3":
        cached = _cache_path(text, language)

        if os.path.exists(cached) and os.path.getsize(cached) > 0:
            try:
                play_file(cached, block=block)
                return {"ok": True, "backend": "cache", "path": cached, "reason": None}
            except Exception as e:
                attempts.append("cache: %s: %s" % (type(e).__name__, e))

    # 2. Offline engine, but only for languages it can genuinely speak.
    wanted = language.split("-")[0]

    if backend in (None, "pyttsx3") and (
            backend == "pyttsx3" or wanted in OFFLINE_OK_PREFIXES):
        try:
            if speak_offline(text, language):
                return {"ok": True, "backend": "pyttsx3", "path": None, "reason": None}

            attempts.append("pyttsx3: no installed voice for %r" % language)

        except Exception as e:
            attempts.append("pyttsx3: %s: %s" % (type(e).__name__, e))

    # 3. Network synthesis.
    if backend in (None, "gtts") and allow_network:
        try:
            path = synthesize(text, language)
            play_file(path, block=block)
            return {"ok": True, "backend": "gtts", "path": path, "reason": None}

        except Exception as e:
            attempts.append("gtts: %s: %s" % (type(e).__name__, e))

    # Everything failed. Say so plainly rather than returning a silent
    # success -- the caller may want to fall back to an on-screen alert.
    reason = "; ".join(attempts) or "no backend available"
    print("  TTS FAILED (%s): %s" % (language, reason), file=sys.stderr)
    return {"ok": False, "backend": None, "path": None, "reason": reason}


def prefetch(texts, language="en", refresh=False):
    """
    Warm the cache for a list of alerts. Run this while the network is
    up; afterwards speak() plays from disk with the wifi off.
    """

    done = []

    for text in texts:
        text = text.strip()

        if not text:
            continue

        try:
            path = synthesize(text, language, refresh=refresh)
            done.append((text, path, None))
        except Exception as e:
            done.append((text, None, "%s: %s" % (type(e).__name__, e)))

    return done


# ============================================================
# SELF TEST -- cache behaviour, without making noise
# ============================================================

def selftest():
    """Synthesis and cache keying. Needs the network; plays nothing."""

    checks = []

    def check(name, condition, detail=""):
        checks.append(bool(condition))
        print("  %s  %-36s %s" % ("PASS" if condition else "FAIL", name, detail))

    a = _cache_path("Bay 3 evacuated", "en")
    b = _cache_path("Bay 3 evacuated", "hi")
    c = _cache_path("Bay 4 evacuated", "en")

    check("cache key varies by language", a != b, os.path.basename(a))
    check("cache key varies by text", a != c, os.path.basename(c))
    check("cache key is stable", a == _cache_path("Bay 3 evacuated", "en"), "")

    try:
        path = synthesize("Bay 3 evacuated. Do not use water.", "en")
        size = os.path.getsize(path)
        check("synthesizes to disk", size > 500, "%d bytes" % size)

        stamp = os.path.getmtime(path)
        again = synthesize("Bay 3 evacuated. Do not use water.", "en")
        check("second call is a cache hit",
              again == path and os.path.getmtime(again) == stamp,
              "not re-downloaded")

    except Exception as e:
        check("synthesizes to disk", False, "%s: %s (network?)" % (type(e).__name__, e))

    voice = None

    try:
        voice = _offline_voice_for("en")
    except Exception as e:
        print("  note: offline engine unavailable: %s" % e)

    print("  note: offline voice for 'en': %s"
          % (voice.name if voice else "none installed"))
    print("  note: offline voice for 'hi': %s"
          % (getattr(_offline_voice_for("hi"), "name", None) or "none -- will use gTTS"))

    # The singleton trap, guarded without making a sound.
    #
    # pyttsx3.init() hands back a process-wide engine that speaks once
    # and is silently mute afterwards -- every later call still returns
    # success. This asserts the property that made it dangerous, so if
    # speak_offline ever drifts back to init() the reason is written
    # down right here.
    try:
        import pyttsx3

        check("pyttsx3.init() is a singleton (the trap)",
              pyttsx3.init() is pyttsx3.init(),
              "one engine per process; it speaks once, then stays silent")

        first = pyttsx3.Engine(driverName=None, debug=False)
        second = pyttsx3.Engine(driverName=None, debug=False)

        check("Engine() gives a distinct engine (the fix)",
              first is not second, "which is what speak_offline uses")

        # Comments stripped first: the explanation above deliberately
        # mentions init(), and a guard that trips on its own
        # documentation is a guard nobody keeps.
        code = "\n".join(
            line.split("#", 1)[0]
            for line in inspect.getsource(speak_offline).splitlines())

        check("speak_offline does not call init()",
              "pyttsx3.init(" not in code and "pyttsx3.Engine(" in code,
              "regression guard")

    except ImportError:
        print("  note: pyttsx3 not installed -- engine checks skipped")

    print("\n%d/%d TTS checks passed" % (sum(checks), len(checks)))
    return 0 if all(checks) else 1


# ============================================================
# CLI
# ============================================================

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Speak a hazard alert out loud from Python (no browser).")
    parser.add_argument("text", nargs="?", help="the spoken_alert text")
    parser.add_argument("--lang", default="en", help="language code: en, hi, bn, te, ur")
    parser.add_argument("--backend", choices=["pyttsx3", "gtts"], default=None,
                        help="force a backend instead of picking by language")
    parser.add_argument("--offline", action="store_true",
                        help="refuse the network: cache and OS voices only")
    parser.add_argument("--prefetch", metavar="FILE",
                        help="synthesize every line of FILE into the cache and exit")
    parser.add_argument("--refresh", action="store_true",
                        help="with --prefetch, re-synthesize even if cached")
    parser.add_argument("--list-voices", action="store_true",
                        help="show the OS voices installed on this machine")
    parser.add_argument("--selftest", action="store_true",
                        help="check synthesis and caching, play nothing")

    args = parser.parse_args(argv)

    if args.selftest:
        return selftest()

    if args.list_voices:
        import pyttsx3
        for voice in pyttsx3.init().getProperty("voices"):
            print("  %-46s %s" % (voice.name, getattr(voice, "languages", None)))
        return 0

    if args.prefetch:
        with open(args.prefetch, encoding="utf-8") as handle:
            lines = handle.readlines()

        failed = 0

        for text, path, error in prefetch(lines, args.lang, refresh=args.refresh):
            if error:
                failed += 1
                print("  FAIL  %-40s %s" % (text[:40], error))
            else:
                print("  ok    %-40s %s" % (text[:40], os.path.basename(path)))

        print("\ncache: %s" % CACHE_DIR)
        return 1 if failed else 0

    if not args.text:
        parser.error("give me some text to speak, or use --prefetch/--list-voices")

    result = speak(args.text, args.lang, backend=args.backend,
                   allow_network=not args.offline)

    print("  spoken via %s%s" % (result["backend"],
                                 "" if result["ok"] else " -- FAILED"))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
