# -*- coding: utf-8 -*-
"""
Safety phrases, translated once and shipped.

## Why this is a table and not a translation call

Everything `/incident` can say is drawn from a fixed set: 12 step
strings, 6 contraindications, 5 protective-equipment labels and one
spoken-alert template. Eighteen sentences and a frame. That is small
enough to translate once, review once, and ship.

Machine-translating them at request time would be worse in four
separate ways, and the fourth is the one that matters:

  1. It needs a provider key, so a deployment without one silently
     falls back to English -- which is exactly the state the console
     was in when the language picker appeared to do nothing.
  2. It costs a network round trip on the path that is supposed to
     answer in under a second.
  3. It is automated LLM traffic, which is the thing the Featherless
     Chat tier excludes and the 429s were traced to.
  4. **A mistranslated safety instruction is a hazard.** "Do not add
     water to the acid" rendered wrongly, at speed, unreviewed, by a
     model nobody checked, in a language the operator cannot verify --
     that is the failure this project exists to prevent, arriving
     through the translation layer instead of the reasoning layer.

A table can be read by a native speaker before it ships. A runtime
translation cannot.

## The honest caveat, and it is a real one

**These translations need review by a native speaker before this runs
anywhere real.** They are careful and they are idiomatic for industrial
safety usage, but "careful" is not "verified", and none of the three
languages here has been checked by someone who speaks it.

`REVIEWED` records that. It is False, the API reports it, and the
console shows it. A translation presented as verified when it is not is
the same class of error as an untranslated string presented as a
translation -- which `alert_language.py` already refuses to do.

Fill in a reviewer and flip the flag when somebody has actually read
these.

    python phrases.py --selftest
    python phrases.py --show hi
"""

import argparse
import sys

# Languages this table covers. Anything else falls through to English,
# and the caller is told which language the text is actually in.
LANGUAGES = ("hi", "te", "bn")

# Set to a name and flip REVIEWED once a native speaker has read the
# table for that language. Per-language, because review arrives one
# language at a time and a blanket flag would overclaim.
REVIEWED = {"hi": False, "te": False, "bn": False}
REVIEWERS = {"hi": None, "te": None, "bn": None}


# ============================================================
# THE SPOKEN ALERT FRAME
# ============================================================
#
# Positional, not named, and deliberately: the clause order that is
# natural in English is not natural in Hindi, Telugu or Bengali, and a
# format string with named holes invites translating the frame word by
# word and leaving the English word order in place.

SPOKEN = {
    "en": "Hazard in %(bay)s. %(item)s missing%(near)s. "
          "Clear the bay and wait for the safety officer.",

    "hi": "%(bay)s में खतरा।%(near)s %(item)s नहीं है। "
          "क्षेत्र खाली करें और सुरक्षा अधिकारी की प्रतीक्षा करें।",

    "te": "%(bay)sలో ప్రమాదం.%(near)s %(item)s లేదు. "
          "ప్రాంతాన్ని ఖాళీ చేసి భద్రతా అధికారి కోసం వేచి ఉండండి.",

    "bn": "%(bay)s-এ বিপদ।%(near)s %(item)s নেই। "
          "এলাকা খালি করুন এবং নিরাপত্তা কর্মকর্তার জন্য অপেক্ষা করুন।",
}

# The " near <substance>" fragment, kept separate because it is often
# absent and because gluing it into the frame above would force every
# translation to carry an empty clause.
NEAR = {
    "en": " near %s",
    "hi": " %s के पास।",
    "te": " %s దగ్గర.",
    "bn": " %s-এর কাছে।",
}


# ============================================================
# PROTECTIVE EQUIPMENT
# ============================================================

ITEMS = {
    "head protection": {
        "hi": "हेलमेट (सिर की सुरक्षा)",
        "te": "హెల్మెట్ (తల రక్షణ)",
        "bn": "হেলমেট (মাথার সুরক্ষা)",
    },
    "respiratory protection": {
        "hi": "मास्क (श्वास सुरक्षा)",
        "te": "మాస్క్ (శ్వాస రక్షణ)",
        "bn": "মাস্ক (শ্বাস সুরক্ষা)",
    },
    "high-visibility clothing": {
        "hi": "सुरक्षा जैकेट",
        "te": "భద్రతా జాకెట్",
        "bn": "নিরাপত্তা জ্যাকেট",
    },
    "hand protection": {
        "hi": "दस्ताने (हाथ की सुरक्षा)",
        "te": "చేతి తొడుగులు",
        "bn": "দস্তানা (হাতের সুরক্ষা)",
    },
    "required protective equipment": {
        "hi": "आवश्यक सुरक्षा उपकरण",
        "te": "అవసరమైన భద్రతా పరికరాలు",
        "bn": "প্রয়োজনীয় সুরক্ষা সরঞ্জাম",
    },
    "eye protection": {
        "hi": "चश्मा (आँखों की सुरक्षा)",
        "te": "కళ్లద్దాలు (కంటి రక్షణ)",
        "bn": "চশমা (চোখের সুরক্ষা)",
    },
}


# ============================================================
# RESPONSE STEPS
# ============================================================
#
# Keys are the exact English strings from incident_api.VIOLATIONS,
# including the {bay} placeholder. The selftest asserts that every one
# of them is present here, so adding a step to the rules table without
# translating it fails the build rather than silently shipping English.

STEPS = {
    "Stop work in {bay}.": {
        "hi": "{bay} में काम रोकें।",
        "te": "{bay}లో పని ఆపండి.",
        "bn": "{bay}-এ কাজ বন্ধ করুন।",
    },
    "Stop work in {bay} and hold the area.": {
        "hi": "{bay} में काम रोकें और क्षेत्र को सुरक्षित रखें।",
        "te": "{bay}లో పని ఆపి, ప్రాంతాన్ని అదుపులో ఉంచండి.",
        "bn": "{bay}-এ কাজ বন্ধ করুন এবং এলাকা নিয়ন্ত্রণে রাখুন।",
    },
    "Stop handling operations in {bay}.": {
        "hi": "{bay} में सामग्री संभालने का काम रोकें।",
        "te": "{bay}లో పదార్థాలు నిర్వహించే పనులు ఆపండి.",
        "bn": "{bay}-এ পদার্থ পরিচালনার কাজ বন্ধ করুন।",
    },
    "Stop vehicle and plant movement through {bay}.": {
        "hi": "{bay} से वाहनों और मशीनों की आवाजाही रोकें।",
        "te": "{bay} గుండా వాహనాలు, యంత్రాల రాకపోకలు ఆపండి.",
        "bn": "{bay} দিয়ে যানবাহন ও যন্ত্রপাতির চলাচল বন্ধ করুন।",
    },
    "Clear {bay} to fresh air and hold the area.": {
        "hi": "{bay} को खाली कर सभी को खुली हवा में ले जाएँ और क्षेत्र सुरक्षित रखें।",
        "te": "{bay} ఖాళీ చేసి అందరినీ స్వచ్ఛమైన గాలిలోకి తరలించి, ప్రాంతాన్ని అదుపులో ఉంచండి.",
        "bn": "{bay} খালি করে সবাইকে খোলা বাতাসে নিয়ে যান এবং এলাকা নিয়ন্ত্রণে রাখুন।",
    },
    "Issue a hardhat before anyone re-enters {bay}.": {
        "hi": "{bay} में दोबारा प्रवेश से पहले सभी को हेलमेट दें।",
        "te": "{bay}లోకి తిరిగి ప్రవేశించే ముందు అందరికీ హెల్మెట్ ఇవ్వండి.",
        "bn": "{bay}-এ পুনরায় প্রবেশের আগে সবাইকে হেলমেট দিন।",
    },
    "Issue the correct respirator for the substance before re-entry.": {
        "hi": "दोबारा प्रवेश से पहले पदार्थ के अनुसार सही मास्क दें।",
        "te": "తిరిగి ప్రవేశించే ముందు పదార్థానికి తగిన సరైన మాస్క్ ఇవ్వండి.",
        "bn": "পুনরায় প্রবেশের আগে পদার্থ অনুযায়ী সঠিক মাস্ক দিন।",
    },
    "Issue high-visibility clothing before work resumes.": {
        "hi": "काम दोबारा शुरू करने से पहले सुरक्षा जैकेट दें।",
        "te": "పని తిరిగి ప్రారంభించే ముందు భద్రతా జాకెట్ ఇవ్వండి.",
        "bn": "কাজ পুনরায় শুরুর আগে নিরাপত্তা জ্যাকেট দিন।",
    },
    "Issue gloves rated for the substance in use.": {
        "hi": "उपयोग हो रहे पदार्थ के लिए उपयुक्त दस्ताने दें।",
        "te": "వాడుతున్న పదార్థానికి తగిన చేతి తొడుగులు ఇవ్వండి.",
        "bn": "ব্যবহৃত পদার্থের জন্য উপযুক্ত দস্তানা দিন।",
    },
    "Issue eye protection; check the nearest eyewash station is clear.": {
        "hi": "आँखों की सुरक्षा दें; निकटतम आईवॉश स्टेशन चालू और खुला है यह जाँचें।",
        "te": "కంటి రక్షణ ఇవ్వండి; సమీప ఐవాష్ స్టేషన్ పనిచేస్తుందో సరిచూడండి.",
        "bn": "চোখের সুরক্ষা দিন; নিকটতম আইওয়াশ স্টেশন সচল আছে কি না দেখুন।",
    },
    "Check for overhead work or suspended loads above the bay.": {
        "hi": "जाँचें कि ऊपर कोई काम चल रहा है या कोई भार लटका हुआ है।",
        "te": "పైన ఏదైనా పని జరుగుతుందా, బరువులు వేలాడుతున్నాయా అని తనిఖీ చేయండి.",
        "bn": "উপরে কোনো কাজ চলছে বা কোনো ভার ঝুলছে কি না পরীক্ষা করুন।",
    },
    "Contain the spill with a dry absorbent; do not hose it down.": {
        "hi": "रिसाव को सूखे अवशोषक से रोकें; पानी की धार न डालें।",
        "te": "చిందిన పదార్థాన్ని పొడి శోషకంతో నిలువరించండి; నీటితో కడగవద్దు.",
        "bn": "ছড়িয়ে পড়া পদার্থ শুকনো শোষক দিয়ে আটকান; জল দিয়ে ধোবেন না।",
    },
    "Contain with dry soda ash or a spill pillow.": {
        "hi": "सूखी सोडा ऐश या स्पिल पिलो से रोकें।",
        "te": "పొడి సోడా యాష్ లేదా స్పిల్ పిల్లోతో నిలువరించండి.",
        "bn": "শুকনো সোডা অ্যাশ বা স্পিল পিলো দিয়ে আটকান।",
    },
    "Evacuate upwind and ventilate before anyone re-enters.": {
        "hi": "हवा की दिशा के विपरीत ओर हटें और दोबारा प्रवेश से पहले हवादार करें।",
        "te": "గాలి వీచే దిక్కుకు ఎదురుగా ఖాళీ చేయండి; తిరిగి ప్రవేశించే ముందు గాలి ఆడనివ్వండి.",
        "bn": "বাতাসের উজানে সরে যান এবং পুনরায় প্রবেশের আগে বাতাস চলাচল করান।",
    },
    "Evacuate upwind; ammonia vapour is lighter than air.": {
        "hi": "हवा की दिशा के विपरीत ओर हटें; अमोनिया वाष्प हवा से हल्की है।",
        "te": "గాలి వీచే దిక్కుకు ఎదురుగా ఖాళీ చేయండి; అమ్మోనియా ఆవిరి గాలి కంటే తేలికైనది.",
        "bn": "বাতাসের উজানে সরে যান; অ্যামোনিয়ার বাষ্প বাতাসের চেয়ে হালকা।",
    },
    "Remove ignition sources and ventilate at floor level.": {
        "hi": "आग लगने के सभी स्रोत हटाएँ और फर्श के स्तर पर हवादार करें।",
        "te": "మంట రాజేసే వనరులన్నీ తొలగించి, నేల స్థాయిలో గాలి ఆడనివ్వండి.",
        "bn": "আগুনের উৎস সরান এবং মেঝের স্তরে বাতাস চলাচল করান।",
    },
    "Isolate the supply valve before anything else.": {
        "hi": "सबसे पहले आपूर्ति वाल्व बंद करें।",
        "te": "అన్నిటికంటే ముందు సరఫరా వాల్వ్ మూసివేయండి.",
        "bn": "সবার আগে সরবরাহ ভালভ বন্ধ করুন।",
    },
    "Have a competent person assess the hazard before work resumes.": {
        "hi": "काम दोबारा शुरू करने से पहले किसी सक्षम व्यक्ति से खतरे का आकलन कराएँ।",
        "te": "పని తిరిగి ప్రారంభించే ముందు సమర్థుడైన వ్యక్తితో ప్రమాదాన్ని అంచనా వేయించండి.",
        "bn": "কাজ পুনরায় শুরুর আগে একজন যোগ্য ব্যক্তিকে দিয়ে বিপদ মূল্যায়ন করান।",
    },
    "Log the incident and notify the shift safety officer.": {
        "hi": "घटना दर्ज करें और शिफ्ट सुरक्षा अधिकारी को सूचित करें।",
        "te": "సంఘటనను నమోదు చేసి, షిఫ్ట్ భద్రతా అధికారికి తెలియజేయండి.",
        "bn": "ঘটনাটি নথিভুক্ত করুন এবং শিফট নিরাপত্তা কর্মকর্তাকে জানান।",
    },
    "Check the local exhaust ventilation is running.": {
        "hi": "जाँचें कि स्थानीय निकास वेंटिलेशन चालू है।",
        "te": "స్థానిక ఎగ్జాస్ట్ వెంటిలేషన్ పనిచేస్తుందో తనిఖీ చేయండి.",
        "bn": "স্থানীয় নিষ্কাশন ভেন্টিলেশন চালু আছে কি না পরীক্ষা করুন।",
    },
}


# ============================================================
# CONTRAINDICATIONS
# ============================================================
#
# The strings where a translation error is not an inconvenience. Each
# one is a negative instruction, and the negation is carried explicitly
# in every language rather than by a suffix or a particle that a
# distracted reader could skip.

CONTRAINDICATIONS = {
    "Do not flush with a pressurised water jet -- sodium hydroxide "
    "reacts exothermically with water and will spatter caustic solution.": {
        "hi": "तेज़ पानी की धार से न बहाएँ — सोडियम हाइड्रॉक्साइड पानी के साथ ऊष्मा छोड़ता है और कास्टिक घोल छिटकेगा।",
        "te": "అధిక పీడన నీటి ధారతో కడగవద్దు — సోడియం హైడ్రాక్సైడ్ నీటితో వేడిని విడుదల చేసి కాస్టిక్ ద్రావణాన్ని చిమ్ముతుంది.",
        "bn": "উচ্চচাপের জলের ধারা দিয়ে ধুবেন না — সোডিয়াম হাইড্রক্সাইড জলের সঙ্গে তাপ উৎপন্ন করে কস্টিক দ্রবণ ছিটিয়ে দেবে।",
    },
    "Do not add water to the acid -- the reaction is violently "
    "exothermic. Absorb, then neutralise with soda ash.": {
        "hi": "अम्ल में पानी न डालें — प्रतिक्रिया अत्यंत ऊष्माजनक होती है। पहले सोखें, फिर सोडा ऐश से उदासीन करें।",
        "te": "ఆమ్లంలో నీరు కలపవద్దు — చర్య తీవ్రంగా వేడిని విడుదల చేస్తుంది. ముందు పీల్చుకోండి, తర్వాత సోడా యాష్‌తో తటస్థీకరించండి.",
        "bn": "অ্যাসিডে জল দেবেন না — বিক্রিয়া প্রচণ্ড তাপ উৎপাদক। আগে শোষণ করুন, পরে সোডা অ্যাশ দিয়ে নিষ্ক্রিয় করুন।",
    },
    "Do not mix with ammonia or acids -- releases chlorine gas. Do not "
    "enter without respiratory protection.": {
        "hi": "अमोनिया या अम्ल के साथ न मिलाएँ — क्लोरीन गैस निकलती है। श्वास सुरक्षा के बिना प्रवेश न करें।",
        "te": "అమ్మోనియా లేదా ఆమ్లాలతో కలపవద్దు — క్లోరిన్ వాయువు విడుదలవుతుంది. శ్వాస రక్షణ లేకుండా ప్రవేశించవద్దు.",
        "bn": "অ্যামোনিয়া বা অ্যাসিডের সঙ্গে মেশাবেন না — ক্লোরিন গ্যাস নির্গত হয়। শ্বাস সুরক্ষা ছাড়া প্রবেশ করবেন না।",
    },
    "Do not mix with chlorine or bleach. Do not use water spray "
    "directly on a liquid ammonia pool -- it accelerates vaporisation.": {
        "hi": "क्लोरीन या ब्लीच के साथ न मिलाएँ। तरल अमोनिया के जमाव पर सीधे पानी का छिड़काव न करें — इससे वाष्पीकरण तेज़ होता है।",
        "te": "క్లోరిన్ లేదా బ్లీచ్‌తో కలపవద్దు. ద్రవ అమ్మోనియా నిల్వపై నేరుగా నీటిని చిమ్మవద్దు — ఆవిరి వేగంగా పెరుగుతుంది.",
        "bn": "ক্লোরিন বা ব্লিচের সঙ্গে মেশাবেন না। তরল অ্যামোনিয়ার জমাটে সরাসরি জল ছিটাবেন না — বাষ্পীভবন দ্রুত হয়।",
    },
    "Do not use a water jet on an acetone fire -- it spreads the pool. "
    "Do not create sparks; vapour is heavier than air and travels.": {
        "hi": "एसीटोन की आग पर पानी की धार न डालें — इससे जलता हुआ द्रव फैलता है। चिंगारी न बनाएँ; वाष्प हवा से भारी है और दूर तक जाती है।",
        "te": "అసిటోన్ మంటపై నీటి ధార వాడవద్దు — మంట వ్యాపిస్తుంది. నిప్పురవ్వలు రానీయవద్దు; ఆవిరి గాలి కంటే బరువైనది, దూరం ప్రయాణిస్తుంది.",
        "bn": "অ্যাসিটোনের আগুনে জলের ধারা দেবেন না — জ্বলন্ত তরল ছড়িয়ে পড়ে। স্ফুলিঙ্গ তৈরি করবেন না; বাষ্প বাতাসের চেয়ে ভারী এবং দূরে ছড়ায়।",
    },
    "Do not extinguish a burning gas leak until the supply is isolated "
    "-- unburnt gas accumulating is worse than a controlled flame.": {
        "hi": "आपूर्ति बंद होने तक जलती हुई गैस लीक को न बुझाएँ — बिना जली गैस का जमा होना नियंत्रित लौ से अधिक खतरनाक है।",
        "te": "సరఫరా ఆపే వరకు మండుతున్న గ్యాస్ లీక్‌ను ఆర్పవద్దు — కాలని గ్యాస్ పేరుకుపోవడం అదుపులో ఉన్న మంట కంటే ప్రమాదకరం.",
        "bn": "সরবরাহ বন্ধ না হওয়া পর্যন্ত জ্বলন্ত গ্যাস লিক নেভাবেন না — অদগ্ধ গ্যাস জমা হওয়া নিয়ন্ত্রিত শিখার চেয়ে বিপজ্জনক।",
    },
}


# ============================================================
# LOOKUP
# ============================================================

def _lookup(table, english, language):
    entry = table.get(english)

    if not entry:
        return None

    return entry.get(language)


def step(english, language):
    """One response step, or None if there is no translation."""

    return _lookup(STEPS, english, language)


def contraindication(english, language):
    return _lookup(CONTRAINDICATIONS, english, language)


def item(english, language):
    return _lookup(ITEMS, english, language)


def spoken(language, bay, item_label, near=None):
    """
    Build the spoken alert in `language`.

    Returns None rather than a half-translated sentence if any part is
    missing -- a Hindi frame around an English equipment name is not a
    Hindi alert, it is two languages in one sentence read by one voice.
    """

    frame = SPOKEN.get(language)

    if not frame:
        return None

    localized_item = item(item_label, language) if language != "en" else item_label

    if not localized_item:
        return None

    if near:
        near_frame = NEAR.get(language)

        if not near_frame:
            return None

        near_text = near_frame % near
    else:
        near_text = ""

    return frame % {"bay": bay, "item": localized_item, "near": near_text}


def covers(language):
    """Is there a table for this language at all?"""

    return language in LANGUAGES


def review_state(language):
    """
    What to tell a caller about trustworthiness.

    Never claims verification that has not happened.
    """

    return {
        "reviewed": bool(REVIEWED.get(language)),
        "reviewer": REVIEWERS.get(language),
        "note": None if REVIEWED.get(language) else
                "machine-assisted translation, not yet reviewed by a "
                "native speaker -- verify before operational use",
    }


# ============================================================
# SELF TEST
# ============================================================

def selftest():
    """
    Completeness, not quality. No test here can tell you whether the
    Telugu is good -- only that nothing is missing and nothing is
    accidentally still English.
    """

    checks = []

    def check(name, condition, detail=""):
        checks.append(bool(condition))
        print("  %s  %-52s %s" % ("PASS" if condition else "FAIL", name, detail))

    # -- every rules-table string is covered -------------------------
    #
    # This is the check that matters over time: adding a step to
    # incident_api without translating it fails here rather than
    # silently shipping English inside a Hindi response.

    try:
        import incident_api

        english_steps = set()

        for violation in incident_api.VIOLATIONS.values():
            english_steps.update(violation["steps"])

        # Steps do not all come from VIOLATIONS. assess() inserts a
        # substance-specific first_step and appends a logging line, and
        # the first version of this check looked only at VIOLATIONS --
        # so seven strings were missing and every translated response
        # fell back to English with no test failing.
        for substance in incident_api.SUBSTANCES.values():
            if substance.get("first_step"):
                english_steps.add(substance["first_step"])

        # The literal appended by assess(), which belongs to neither
        # table. Named here rather than derived from assess() output,
        # because that output is already formatted -- "{bay}" has become
        # "BAY-1" and would never match a template key.
        english_steps.add("Log the incident and notify the shift safety officer.")

        # The unknown-hazard branch. Every incident type the console
        # offers -- Spill, Gas Leak, Skin Contact -- is unmapped against
        # the PPE table, so this is the branch they ALL take, and it was
        # the one branch the coverage check ignored.
        generic = incident_api.assess("Spill", "{bay}", None)
        english_steps.update(generic["_step_templates"])
        english_items = set()
        english_items.add(generic["_item"])

        missing = sorted(english_steps - set(STEPS))
        check("every rules-table step has an entry", not missing,
              "missing: %s" % (missing[0][:40] if missing else "none"))

        english_contras = {
            s["contraindication"] for s in incident_api.SUBSTANCES.values()
            if s.get("contraindication")
        }
        missing = sorted(english_contras - set(CONTRAINDICATIONS))
        check("every contraindication has an entry", not missing,
              "missing: %s" % (missing[0][:40] if missing else "none"))

        english_items |= {v["label"] for v in incident_api.VIOLATIONS.values()}
        missing = sorted(english_items - set(ITEMS))
        check("every equipment label has an entry", not missing,
              "missing: %s" % (missing[0][:40] if missing else "none"))

    except ImportError:
        check("incident_api importable for coverage check", False, "skipped")

    # -- every entry covers every language ---------------------------

    gaps = []

    for table_name, table in (("steps", STEPS),
                              ("contraindications", CONTRAINDICATIONS),
                              ("items", ITEMS)):
        for english, translations in table.items():
            for lang in LANGUAGES:
                if not (translations.get(lang) or "").strip():
                    gaps.append("%s/%s/%s" % (table_name, lang, english[:24]))

    check("no empty translation in any language", not gaps,
          "%d gap(s)" % len(gaps) if gaps else "%d strings x %d languages"
          % (len(STEPS) + len(CONTRAINDICATIONS) + len(ITEMS), len(LANGUAGES)))

    # -- nothing is silently still English ---------------------------
    #
    # A copy-paste that left the English in place would pass every
    # check above. All three languages use non-Latin scripts, so a
    # translation with no character outside ASCII is one that never
    # happened.

    latin = []

    for table_name, table in (("steps", STEPS),
                              ("contraindications", CONTRAINDICATIONS),
                              ("items", ITEMS)):
        for english, translations in table.items():
            for lang in LANGUAGES:
                text = translations.get(lang, "")
                stripped = "".join(c for c in text if not c.isascii())

                if not stripped:
                    latin.append("%s/%s/%s" % (table_name, lang, english[:24]))

    check("no entry is still in Latin script", not latin,
          "%d untranslated" % len(latin) if latin else
          "all three scripts present")

    # -- placeholders survive translation ----------------------------
    #
    # A dropped {bay} renders "Stop work in ." and a doubled one
    # crashes .format(). Both have happened to other people.

    bad = []

    for english, translations in STEPS.items():
        want = english.count("{bay}")

        for lang in LANGUAGES:
            if translations.get(lang, "").count("{bay}") != want:
                bad.append("%s/%s" % (lang, english[:26]))

    check("{bay} placeholder preserved exactly", not bad,
          "%d mismatch(es)" % len(bad) if bad else "counts match everywhere")

    # -- the spoken frame --------------------------------------------

    for lang in ("en",) + LANGUAGES:
        frame = SPOKEN.get(lang, "")
        ok = all(k in frame for k in ("%(bay)s", "%(item)s", "%(near)s"))
        check("spoken frame [%s] has all three slots" % lang, ok, "")

    line = spoken("hi", "BAY-3", "head protection")
    check("spoken() builds a Hindi alert",
          line and "BAY-3" in line and not line.isascii(), (line or "")[:44])

    line = spoken("hi", "BAY-3", "head protection", near="Sodium hydroxide")
    check("spoken() includes the substance when given",
          line and "Sodium hydroxide" in line, "substance carried through")

    check("spoken() refuses a half-translated sentence",
          spoken("hi", "BAY-3", "no such equipment") is None,
          "None rather than mixed languages")

    check("an uncovered language returns None, not English",
          spoken("fr", "BAY-3", "head protection") is None, "fr -> None")

    # -- honesty about review ----------------------------------------

    state = review_state("hi")
    check("review state does not claim verification",
          state["reviewed"] is False and state["note"],
          "says it needs a native speaker")

    passed = sum(checks)
    print("\n  %d/%d phrase checks passed" % (passed, len(checks)))
    return passed == len(checks)


def show(language):
    """Print the table for one language, for a reviewer to read."""

    if not covers(language):
        print("no table for %r -- have %s" % (language, ", ".join(LANGUAGES)))
        return 1

    state = review_state(language)
    print("\n  %s -- %s\n" % (
        language, "reviewed by %s" % state["reviewer"] if state["reviewed"]
        else "NOT YET REVIEWED"))

    for title, table in (("EQUIPMENT", ITEMS), ("STEPS", STEPS),
                         ("CONTRAINDICATIONS", CONTRAINDICATIONS)):
        print("  " + title)

        for english, translations in table.items():
            print("    en: %s" % english)
            print("    %s: %s\n" % (language, translations[language]))

    return 0


def _utf8_stdout():
    """
    A Windows console is cp1252 by default, and printing Devanagari to
    it raises UnicodeEncodeError -- so the selftest crashed halfway,
    and --show, whose entire purpose is letting a reviewer read the
    table, could not print the thing being reviewed.

    Done here rather than at import, because reconfiguring stdout as a
    side effect of importing a module is a rude surprise for anything
    that imports this to look up one string.
    """

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main():
    _utf8_stdout()
    parser = argparse.ArgumentParser(description=__doc__.strip().split("\n")[0])
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--show", metavar="LANG",
                        help="print one language's table for review")
    args = parser.parse_args()

    if args.show:
        return show(args.show)

    if args.selftest:
        return 0 if selftest() else 1

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
