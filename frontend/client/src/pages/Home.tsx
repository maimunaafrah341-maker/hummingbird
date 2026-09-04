import { useEffect, useMemo, useRef, useState, type RefObject } from "react";
import {
  Activity,
  AlertTriangle,
  Ban,
  Beaker,
  Building2,
  CheckCircle2,
  ChevronDown,
  CircleHelp,
  Cloud,
  Clock3,
  Droplets,
  Factory,
  Flame,
  HardHat,
  Languages,
  LockKeyhole,
  Mic,
  MicOff,
  MapPin,
  Radio,
  RotateCcw,
  ShieldAlert,
  Siren,
  Sparkles,
  Thermometer,
  Video,
  VideoOff,
  Volume2,
  Waves,
  Wind,
} from "lucide-react";

import { apiUrl } from "@/api";

type IncidentType = "Spill" | "Vapor Release" | "Skin Contact" | "Fire Flare" | "Gas Leak" | "Thermal Runaway" | "Unknown Chemical" | "Structural Failure";
type Severity = "HIGH" | "CRITICAL" | "MEDIUM";

type IncidentResponse = {
  severity: Severity;
  steps: string[];
  spoken_alert: string;
  // The backend sends these and the console used to drop them on the
  // floor. contraindication is the single most important string in the
  // whole response -- "do not add water to the acid" is the difference
  // between a spill and a hospital -- and it was being fetched, parsed,
  // and thrown away.
  contraindication?: string;
  regulatory_citation?: string;
  tier?: string;
  latency_ms?: number;
  substance_class?: string;
  // The retrieval-grounded backend answers with these instead of
  // `tier`. Both shapes are in play across the team, and a console that
  // only understands one of them does not fail loudly -- it falls
  // through to the other one's default, which is how "deterministic"
  // ends up printed above a sentence a model wrote.
  generation_provider?: string;
  grounded?: boolean;
  retrieval_mode?: string;
  retrieved_sources?: string[];
  spoken_alert_translated?: boolean;
  localization?: {
    requested: string;
    language: string;
    translated: boolean;
    reason: string | null;
    spoken_alert: string;
    steps: string[];
  };
};

const locations = ["Bay-1", "Bay-2", "Bay-3, Reactor B", "Centrifuge C-201", "Distillation Column DC-4", "Mixing Vessel MV-12", "Compressor K-07", "Pump P-204", "Scrubber S-03", "Storage Tank TK-18", "Loading Dock 2", "Utilities · Boiler House"];
const substances = ["Hydrochloric Acid", "Acetone", "Caustic Soda", "Sulfuric Acid", "Chlorine", "Ammonia Solution", "Methanol", "Hydrogen Peroxide", "Sodium Hypochlorite", "Ethylene Oxide", "Unknown Substance"];
const languages = ["Telugu", "Hindi", "Bengali", "English"];

// The picker shows names; the API takes codes. This console was sending
// "Hindi" into a field documented as a language code, so the backend
// could not have honoured it even once it started trying to.
const languageCodes: Record<string, string> = {
  English: "en", Hindi: "hi", Telugu: "te", Bengali: "bn",
};
const incidentTypes: { label: IncidentType; icon: typeof Waves; note: string }[] = [
  { label: "Spill", icon: Waves, note: "Liquid release" },
  { label: "Vapor Release", icon: Wind, note: "Airborne exposure" },
  { label: "Skin Contact", icon: ShieldAlert, note: "Personnel exposure" },
  { label: "Fire Flare", icon: Flame, note: "Ignition / thermal event" },
  { label: "Gas Leak", icon: Cloud, note: "Toxic or flammable gas" },
  { label: "Thermal Runaway", icon: Thermometer, note: "Heat / pressure rise" },
  { label: "Unknown Chemical", icon: CircleHelp, note: "Unidentified material" },
  { label: "Structural Failure", icon: Droplets, note: "Equipment or building" },
];

const defaultResponse: IncidentResponse = {
  severity: "HIGH",
  steps: ["Evacuate", "Do not use water", "Move crosswind"],
  spoken_alert: "Evacuate now",
};
const evacuationBroadcasts: Record<string, { lang: string; lead: string; instructions: string[] }> = {
  English: { lang: "en-IN", lead: "EVACUATE NOW! IT'S NOT A DRILL!!", instructions: ["DO NOT USE WATER.", "MOVE CROSSWIND.", "REPORT TO THE ASSEMBLY POINT.", "DO NOT RE-ENTER.", "WAIT FOR THE ALL CLEAR."] },
  Hindi: { lang: "hi-IN", lead: "अभी खाली करें! यह अभ्यास नहीं है!!", instructions: ["पानी का उपयोग न करें।", "हवा की दिशा के पार जाएँ।", "सभा स्थल पर रिपोर्ट करें।", "दोबारा प्रवेश न करें।", "सभी सुरक्षित होने की घोषणा की प्रतीक्षा करें।"] },
  Telugu: { lang: "te-IN", lead: "ఇప్పుడే ఖాళీ చేయండి! ఇది మాక్ డ్రిల్ కాదు!!", instructions: ["నీటిని ఉపయోగించవద్దు.", "గాలి దిశకు అడ్డంగా కదలండి.", "సమావేశ స్థలానికి వెళ్లండి.", "తిరిగి ప్రవేశించవద్దు.", "అందరూ సురక్షితమని ప్రకటించే వరకు వేచి ఉండండి."] },
  Bengali: { lang: "bn-IN", lead: "এখনই সরে যান! এটি কোনো মহড়া নয়!!", instructions: ["জল ব্যবহার করবেন না।", "বাতাসের আড়াআড়ি দিকে যান।", "সমাবেশস্থলে রিপোর্ট করুন।", "পুনরায় প্রবেশ করবেন না।", "নিরাপদ ঘোষণার জন্য অপেক্ষা করুন।"] },
};

function SelectField({
  label,
  value,
  options,
  onChange,
  icon: Icon,
}: {
  label: string;
  value: string;
  options: string[];
  onChange: (value: string) => void;
  icon: typeof Factory;
}) {
  return (
    <label className="field-shell">
      <span className="field-label">{label}</span>
      <span className="field-control">
        <Icon size={17} strokeWidth={1.8} aria-hidden="true" />
        <select value={value} onChange={(event) => onChange(event.target.value)} aria-label={label}>
          {options.map((option) => (
            <option key={option} value={option}>
              {option}
            </option>
          ))}
        </select>
        <ChevronDown className="field-chevron" size={16} aria-hidden="true" />
      </span>
    </label>
  );
}

function MediaDock({ cameraOn, micOn, unknownMaterial, fieldLevel, voiceLevel, mediaError, videoRef, onToggleCamera, onToggleMic }: {
  cameraOn: boolean;
  micOn: boolean;
  unknownMaterial: boolean;
  fieldLevel: number;
  voiceLevel: number;
  mediaError: string | null;
  videoRef: RefObject<HTMLVideoElement | null>;
  onToggleCamera: () => void;
  onToggleMic: () => void;
}) {
  return (
    <section className="media-dock" aria-label="Incident media controls">
      <div className="media-dock-copy">
        <div className="media-title-row"><span className="media-live-dot" /><span className="eyebrow">Field media link</span><span className="media-optional">Optional / operator controlled</span></div>
        <h2>Visual &amp; audio confirmation</h2>
        <p>{unknownMaterial ? "Unknown material selected. Use the visual and audio link to help responders confirm the release before entry." : "Keep a live view available for the response team. Camera and microphone access can be opened or closed at any time."}</p>
        {/* Said plainly, because the panel invites the opposite reading.
            This is a human confirmation feed: a responder looking at a
            bay before they walk into it. PPE detection is a different
            camera doing a different job -- the fixed bay cameras run
            the YOLO trigger, which opens incidents on its own without
            anybody at this console. Leaving that unstated makes an
            operator feed look like a detector that is failing. */}
        <p className="media-scope">This feed is for human confirmation. It does not run detection &mdash; PPE monitoring happens on the fixed bay cameras, which open incidents automatically.</p>
        {mediaError && <span className="media-error" role="status">{mediaError}</span>}
        {unknownMaterial && <div className="unknown-actions"><span>Recommended for unknown substance</span><button onClick={onToggleCamera}>{cameraOn ? "Camera active" : "View from camera"}</button><button onClick={onToggleMic}>{micOn ? "Mic active" : "Listen to field"}</button></div>}
      </div>
      <div className={`media-preview ${cameraOn ? "media-preview-live" : ""}`}>
        {cameraOn ? <video ref={videoRef} autoPlay muted playsInline aria-label="Live camera preview" /> : <><VideoOff size={22} /><span>Camera closed</span></>}
        {cameraOn && <span className="preview-live-label"><span className="media-live-dot" /> LIVE</span>}
      </div>
      <div className="media-actions">
        <button className={`media-control ${cameraOn ? "media-control-active" : ""}`} onClick={onToggleCamera} aria-pressed={cameraOn}>
          {cameraOn ? <Video size={17} /> : <VideoOff size={17} />}<span>{cameraOn ? "Close camera" : "Open camera"}</span><kbd>{cameraOn ? "ON" : "OFF"}</kbd>
        </button>
        <button className={`media-control ${micOn ? "media-control-active" : ""}`} onClick={onToggleMic} aria-pressed={micOn}>
          {micOn ? <Mic size={17} /> : <MicOff size={17} />}<span>{micOn ? "Close microphone" : "Open microphone"}</span><kbd>{micOn ? "ON" : "OFF"}</kbd>
        </button>
        <div className={`audio-status ${micOn ? "audio-status-live" : ""}`}><span className="audio-bars" aria-hidden="true">{[fieldLevel, fieldLevel * .8, fieldLevel * 1.1, fieldLevel * .7, fieldLevel * .9].map((level, index) => <i key={index} style={{ height: `${Math.max(4, Math.min(14, 4 + level * 10))}px` }} />)}</span><span>{micOn ? "Field sound · live" : "Field sound · closed"}</span></div>
        <div className={`audio-status ${micOn ? "audio-status-live" : ""}`}><span className="audio-bars voice-bars" aria-hidden="true">{[voiceLevel * .8, voiceLevel, voiceLevel * 1.15, voiceLevel * .65, voiceLevel * .9].map((level, index) => <i key={index} style={{ height: `${Math.max(4, Math.min(14, 4 + level * 10))}px` }} />)}</span><span>{micOn ? "Human voice · live" : "Human voice · closed"}</span></div>
      </div>
    </section>
  );
}

function SystemStatus() {
  return (
    <section className="status-card" aria-label="Operational status">
      <div className="status-card-heading">
        <div>
          <p className="eyebrow">System status</p>
          <h2>Operational</h2>
        </div>
        <span className="status-dot" aria-label="All systems operational" />
      </div>
      <div className="status-grid">
        <div className="status-metric">
          <span className="status-metric-icon"><Activity size={15} /></span>
          <div><strong>Online</strong><span>Sensor mesh</span></div>
        </div>
        <div className="status-metric">
          <span className="status-metric-icon"><Radio size={15} /></span>
          <div><strong>Ready</strong><span>Alert relay</span></div>
        </div>
        <div className="status-metric">
          <span className="status-metric-icon"><Clock3 size={15} /></span>
          <div><strong>&lt; 1 sec</strong><span>Response target</span></div>
        </div>
        <div className="status-metric">
          <span className="status-metric-icon"><CheckCircle2 size={15} /></span>
          <div><strong>Verified</strong><span>Protocol library</span></div>
        </div>
      </div>
    </section>
  );
}

function LockoutView({ response, context, cameraOn, micOn, fieldLevel, voiceLevel, videoRef, onReset, fromFallback = false }: { response: IncidentResponse; context: { location: string; substance: string; incidentType: IncidentType; language: string }; cameraOn: boolean; micOn: boolean; fieldLevel: number; voiceLevel: number; videoRef: RefObject<HTMLVideoElement | null>; onReset: () => void; fromFallback?: boolean }) {
  const isCritical = response.severity === "CRITICAL";
  const [evacuationConfirmed, setEvacuationConfirmed] = useState(false);
  const [autoStopped, setAutoStopped] = useState(false);
  const [elapsedSeconds, setElapsedSeconds] = useState(0);
  const [messageIndex, setMessageIndex] = useState(0);
  const speechTimerRef = useRef<number | null>(null);
  const broadcast = evacuationBroadcasts[context.language] ?? evacuationBroadcasts.English;
  const sequence = broadcast.instructions.flatMap((instruction) => [broadcast.lead, instruction]);
  const currentMessage = sequence[messageIndex];

  // The numbered steps are the incident service's answer, not the
  // evacuation table. Those five broadcast lines are the same for a
  // caustic spill and a chlorine leak -- they are the loudspeaker loop,
  // and they were standing in for the substance-specific response the
  // backend actually returns. That made the whole point of the backend
  // invisible on the one screen anybody looks at.
  const loc = response.localization;
  const translated = Boolean(loc?.translated && loc.steps.length);
  const responseSteps = translated
    ? loc!.steps
    : response.steps?.length
      ? response.steps
      : broadcast.instructions;

  useEffect(() => {
    const timer = window.setInterval(() => setElapsedSeconds((seconds) => seconds + 1), 1000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    if (elapsedSeconds >= 300 && !evacuationConfirmed) setAutoStopped(true);
  }, [elapsedSeconds, evacuationConfirmed]);

  useEffect(() => {
    if (typeof window === "undefined" || !("speechSynthesis" in window) || evacuationConfirmed || autoStopped) {
      if (typeof window !== "undefined" && "speechSynthesis" in window) window.speechSynthesis.cancel();
      return;
    }
    const speakAlert = () => {
      window.speechSynthesis.cancel();
      const utterance = new SpeechSynthesisUtterance(currentMessage);
      utterance.lang = broadcast.lang;
      utterance.rate = 0.86;
      utterance.pitch = 0.92;
      utterance.volume = 1;
      let advanced = false;
      const advance = () => {
        if (advanced || evacuationConfirmed || autoStopped) return;
        advanced = true;
        if (speechTimerRef.current) window.clearTimeout(speechTimerRef.current);
        setMessageIndex((index) => (index + 1) % sequence.length);
      };
      utterance.onend = advance;
      speechTimerRef.current = window.setTimeout(advance, Math.max(5200, currentMessage.length * 85));
      window.speechSynthesis.speak(utterance);
    };
    speakAlert();
    return () => {
      window.speechSynthesis.cancel();
      if (speechTimerRef.current) window.clearTimeout(speechTimerRef.current);
      speechTimerRef.current = null;
    };
  }, [broadcast.lang, broadcast.lead, broadcast.instructions, currentMessage, sequence.length, autoStopped, evacuationConfirmed]);

  function confirmEvacuation() {
    setEvacuationConfirmed(true);
    setAutoStopped(false);
    window.speechSynthesis?.cancel();
    if (speechTimerRef.current) window.clearTimeout(speechTimerRef.current);
    speechTimerRef.current = null;
  }

  const minutes = String(Math.floor(elapsedSeconds / 60)).padStart(2, "0");
  const seconds = String(elapsedSeconds % 60).padStart(2, "0");

  return (
    <main className={`lockout-screen ${isCritical ? "lockout-critical" : "lockout-high"}`}>
      <div className="lockout-noise" />
      <header className="lockout-header">
        <div className="brand lockout-brand">
          <span className="brand-mark"><Siren size={22} strokeWidth={2.4} /></span>
          <span><strong>HAZARD WATCH</strong><small>Operational safety network</small></span>
        </div>
        <div className="lockout-badge"><LockKeyhole size={15} /> Protocol locked</div>
        {fromFallback && (
          <div className="lockout-badge" role="alert" style={{ background: "#7f1d1d", color: "#fff", borderColor: "#fca5a5" }}>
            <AlertTriangle size={15} /> DEMO FALLBACK &middot; backend unreachable
          </div>
        )}
        <div className="lockout-time"><Clock3 size={15} /> {new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</div>
      </header>

      <div className="lockout-center">
        <div className="severity-kicker"><span className="pulse-dot" /> Active incident / {response.severity}</div>
        <h1>BREACH PROTOCOL<br /><em>ACTIVE</em></h1>
        <p className="lockout-context"><span>{context.location}</span><span className="context-divider">/</span><span>{context.substance}</span><span className="context-divider">/</span><span>{context.incidentType}</span></p>

        <div className="pictogram-row" aria-label="Emergency pictograms">
          <div className="pictogram"><Siren size={42} strokeWidth={1.5} /><span>ALERT</span></div>
          <div className="pictogram"><HardHat size={42} strokeWidth={1.5} /><span>PROTECT</span></div>
          <div className="pictogram"><Waves size={42} strokeWidth={1.5} /><span>ISOLATE</span></div>
        </div>

        <div className="lockout-media-status">
          <div className="lockout-camera"><video ref={videoRef} autoPlay muted playsInline aria-label="Live incident camera feed" />{!cameraOn && <span>Camera feed unavailable</span>}</div>
          <div className="lockout-audio"><strong>Field audio link</strong><label>Field sound <span className="live-meter">{[fieldLevel, fieldLevel * .8, fieldLevel * 1.1, fieldLevel * .7, fieldLevel * .9].map((level, index) => <i key={index} style={{ height: `${Math.max(5, 5 + level * 16)}px` }} />)}</span></label><label>Human voice <span className="live-meter voice-meter">{[voiceLevel * .8, voiceLevel, voiceLevel * 1.15, voiceLevel * .65, voiceLevel * .9].map((level, index) => <i key={index} style={{ height: `${Math.max(5, 5 + level * 16)}px` }} />)}</span></label><small>{micOn ? "Input live · operator confirmation required" : "Microphone unavailable"}</small></div>
        </div>
        <div className="spoken-alert">
          <Volume2 size={23} className="spoken-icon" />
          <span>{currentMessage}</span>
        </div>
        <div className={`evacuation-control ${evacuationConfirmed ? "evacuation-confirmed" : autoStopped ? "evacuation-auto-stopped" : ""}`}>
          <div className="evacuation-control-copy"><strong>{evacuationConfirmed ? "Evacuation confirmed" : autoStopped ? "Audio safeguard stopped" : `Broadcast ${messageIndex + 1} of ${sequence.length}`}</strong><span>{evacuationConfirmed ? "Confirmed by operator after camera/audio review · accountability required" : autoStopped ? "Five-minute limit reached. Confirm status before restarting." : `Repeating in ${context.language} · elapsed ${minutes}:${seconds}`}</span></div>
          {!evacuationConfirmed && <button className="confirm-evacuation" onClick={confirmEvacuation}>{autoStopped ? "Confirm evacuation" : "Confirm evacuation"}</button>}
          {evacuationConfirmed && <CheckCircle2 size={22} />}
        </div>

        {!loc && response.spoken_alert_translated && (
          <div className="translation-state translation-ok">
            <Languages size={14} />
            <span>Spoken alert translated by the incident service</span>
          </div>
        )}

        {loc && loc.requested !== "en" && (
          // Never present English as though it were the requested
          // language. loc.language says what the text IS.
          <div className={`translation-state ${translated ? "translation-ok" : "translation-off"}`}>
            <Languages size={14} />
            <span>{translated
              ? `Response translated by the incident service · ${loc.language}`
              : `Instructions shown in English — ${loc.reason ?? "translation unavailable"}`}</span>
          </div>
        )}

        {response.contraindication && (
          // Above the steps, not below. Someone reading top-down under
          // stress must hit the thing not to do before the list of
          // things to do -- the dossier PDF orders it the same way.
          <div className="contraindication" role="alert">
            <Ban size={20} strokeWidth={2.2} />
            <div>
              <strong>DO NOT</strong>
              <span>{response.contraindication}</span>
            </div>
          </div>
        )}

        <ol className="steps-list">
          {responseSteps.map((step, index) => (
            <li key={`${step}-${index}`}>
              <span className="step-number">0{index + 1}</span>
              <strong>{step}</strong>
            </li>
          ))}
        </ol>
      </div>

      <footer className="lockout-footer">
        <span><span className={`live-indicator ${evacuationConfirmed || autoStopped ? "live-indicator-muted" : ""}`} /> {evacuationConfirmed ? "Broadcast acknowledged" : autoStopped ? "Broadcast stopped at five minutes" : `Broadcast active · ${context.language} channel`}</span>
        <button className="reset-button" onClick={onReset}><RotateCcw size={15} /> Reset console</button>
      </footer>
    </main>
  );
}

function ReviewView({ response, context, onConfirm, onCancel, fromFallback }: {
  response: IncidentResponse;
  context: { location: string; substance: string; incidentType: IncidentType; language: string };
  onConfirm: () => void;
  onCancel: () => void;
  fromFallback: boolean;
}) {
  // Press and hold, not a typed phrase.
  //
  // The typed confirmation satisfied the review -- a broadcast must not
  // be one reflexive click -- but it was the wrong interaction for the
  // person doing it. An operator standing in a bay with a chlorine leak
  // is wearing gloves. They cannot type DECLARE BAY-1, and asking them
  // to is asking them to take the gloves off during a gas release.
  //
  // Holding is deliberate in the same way and impossible to do by
  // accident or muscle memory, which is the property that mattered. It
  // is also what real plant HMIs use, for this exact reason.
  const HOLD_MS = 1200;
  const [held, setHeld] = useState(0);
  const holdFrame = useRef<number | null>(null);
  const holdStart = useRef(0);
  const fired = useRef(false);

  function stopHold() {
    if (holdFrame.current !== null) cancelAnimationFrame(holdFrame.current);
    holdFrame.current = null;
    setHeld(0);
  }

  function startHold() {
    if (fired.current) return;
    holdStart.current = performance.now();

    const tick = () => {
      const progress = Math.min(1, (performance.now() - holdStart.current) / HOLD_MS);
      setHeld(progress);

      if (progress >= 1) {
        // Guarded: rAF can deliver one more frame after the state
        // update, and broadcasting twice is not a harmless duplicate.
        fired.current = true;
        stopHold();
        onConfirm();
        return;
      }

      holdFrame.current = requestAnimationFrame(tick);
    };

    holdFrame.current = requestAnimationFrame(tick);
  }

  useEffect(() => stopHold, []);

  // "Bay-3, Reactor B" -> "DECLARE BAY-3". The asset, not the prose.
  const asset = context.location.split(",")[0].trim().toUpperCase();
  const phrase = `DECLARE ${asset}`;

  // Rules tier or model tier -- the operator is entitled to know which
  // one wrote what they are about to broadcast to a plant.
  // Only the rules tier may claim to be deterministic. A response
  // carrying generation_provider was written by a model, and defaulting
  // that to "deterministic" would be the console asserting something
  // false about the most safety-critical text on the screen.
  const generated = Boolean(response.generation_provider);
  const fromRules = !generated && (!response.tier || response.tier.startsWith("rules"));

  return (
    <main className="console-shell review-shell">
      <div className="console-grid" />
      <section className="review-panel" aria-labelledby="review-heading">
        <div className="review-head">
          <span className="eyebrow">Step 2 of 3 &middot; Supervisor review</span>
          <h1 id="review-heading">Review before broadcast</h1>
          <p>Nothing has been broadcast yet. This is the draft protocol for {context.location} &mdash; {context.substance}, {context.incidentType.toLowerCase()}.</p>
        </div>

        {fromFallback && (
          <div className="review-fallback" role="alert">
            <AlertTriangle size={17} />
            <span><strong>DEMO FALLBACK</strong> &mdash; the incident gateway did not answer. These steps are local placeholders and did <strong>not</strong> come from the protocol library. Do not broadcast.</span>
          </div>
        )}

        <div className="review-severity">
          {/* Severity is stated in words and marked with a glyph, not
              carried by colour alone -- a red block means nothing to a
              screen reader or to eight percent of men. */}
          <span className={`sev-chip sev-${response.severity.toLowerCase()}`}>
            <span aria-hidden="true">{response.severity === "CRITICAL" ? "◆◆◆" : response.severity === "HIGH" ? "◆◆" : "◆"}</span>
            <span>Severity: {response.severity}</span>
          </span>
        </div>

        {response.contraindication && (
          <div className="contraindication" role="alert">
            <Ban size={20} strokeWidth={2.2} />
            <div>
              <strong>DO NOT</strong>
              <span>{response.contraindication}</span>
            </div>
          </div>
        )}

        <h2 className="review-sub">Response steps</h2>
        <ol className="review-steps">
          {response.steps.map((step, i) => <li key={i}>{step}</li>)}
        </ol>

        <h2 className="review-sub">Spoken alert</h2>
        <p className="review-spoken">&ldquo;{response.spoken_alert}&rdquo;</p>

        <div className="provenance">
          <div className="provenance-row">
            <span>Source</span>
            <strong>{fromRules
              ? "Deterministic protocol library — rules table"
              : `Model-generated (${response.generation_provider ?? response.tier ?? "unknown provider"})`}</strong>
          </div>
          {response.regulatory_citation && (
            <div className="provenance-row"><span>Citation</span><strong>{response.regulatory_citation}</strong></div>
          )}
          {response.retrieval_mode && (
            <div className="provenance-row"><span>Retrieval</span><strong>{response.retrieval_mode}{response.grounded === false ? " (ungrounded)" : ""}</strong></div>
          )}
          {response.retrieved_sources && response.retrieved_sources.length > 0 && (
            <div className="provenance-row"><span>Sources</span><strong>{response.retrieved_sources.join(", ")}</strong></div>
          )}
          {response.substance_class && (
            <div className="provenance-row"><span>Substance class</span><strong>{response.substance_class}</strong></div>
          )}
          <div className="provenance-row">
            <span>Generated</span>
            <strong>{new Date().toLocaleTimeString()}{typeof response.latency_ms === "number" ? ` · ${response.latency_ms} ms` : ""}</strong>
          </div>
          <p className="provenance-note">
            {fromRules
              ? "Severity, steps and the contraindication are produced by a deterministic rules table, not generated by a model. Verify with the EHS lead before broadcast."
              : "AI-assisted recommendation — verify with the EHS lead before broadcast."}
          </p>
        </div>

        <div className="review-confirm">
          <p className="hold-label">
            Press and hold to broadcast <strong>{phrase}</strong>
          </p>
          <div className="review-actions">
            <button type="button" className="btn-cancel" onClick={onCancel}>Cancel</button>
            <button
              type="button"
              className={held > 0 ? "btn-hold btn-hold-active" : "btn-hold"}
              aria-describedby="hold-help"
              onPointerDown={startHold}
              onPointerUp={stopHold}
              onPointerLeave={stopHold}
              onPointerCancel={stopHold}
              // Keyboard parity: a hold has to be reachable without a
              // pointer, or the one control that matters is the one
              // control some operators cannot use.
              onKeyDown={(e) => { if ((e.key === " " || e.key === "Enter") && !e.repeat) { e.preventDefault(); startHold(); } }}
              onKeyUp={(e) => { if (e.key === " " || e.key === "Enter") stopHold(); }}
              onBlur={stopHold}
            >
              <span className="btn-hold-fill" style={{ width: `${held * 100}%` }} />
              <span className="btn-hold-text">
                {held > 0 ? "Keep holding…" : "Hold to broadcast"}
              </span>
            </button>
          </div>
          <p id="hold-help" className="declare-help">
            Broadcasting locks the console and alerts the response network. It cannot be undone from this screen. Release early to cancel.
          </p>
        </div>
      </section>
    </main>
  );
}

type CopilotEntry = {
  id: number;
  question: string;
  answer: string;
  provider: string;
  shared: Record<string, unknown>;
  at: string;
  saved: boolean;
};

const COPILOT_PROMPTS = [
  "What must I verify in the next 60 seconds?",
  "What information is missing?",
  "Explain the chemical hazard simply",
  "What questions should I ask the field team?",
  "Draft a handover for the EHS lead",
  "Compare this event against the approved SOP",
];

function CopilotPanel({ open, onClose, context, onSaveNote }: {
  open: boolean;
  onClose: () => void;
  context: { location: string; substance: string; incidentType: string; language: string };
  onSaveNote: (text: string) => void;
}) {
  const [question, setQuestion] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [trace, setTrace] = useState<CopilotEntry[]>([]);
  const [drawerOpen, setDrawerOpen] = useState(false);

  // Which facts leave this machine. Ticked by the operator, and the
  // drawer below prints exactly this object -- not a description of it.
  // A transparency panel that lists something other than what was sent
  // is worse than no panel: it is a false assurance.
  const [share, setShare] = useState({
    location: true, substance: true, incidentType: true, language: false,
  });

  const shared = useMemo(() => {
    const out: Record<string, string> = {};
    if (share.location) out.bay = context.location;
    if (share.substance) out.substance = context.substance;
    if (share.incidentType) out.incident_type = context.incidentType;
    if (share.language) out.language = context.language;
    return out;
  }, [share, context]);

  async function ask(text: string) {
    const q = text.trim();
    if (!q || busy) return;
    setBusy(true);
    setError(null);

    // A person is standing at this screen waiting, so a long hang is a
    // worse failure than a refusal. Same reasoning as the incident call.
    const abort = new AbortController();
    const giveUp = window.setTimeout(() => abort.abort(), 45000);

    try {
      const res = await fetch(apiUrl("/incident/copilot"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question: q, shared_context: shared }),
        signal: abort.signal,
      });

      if (!res.ok) {
        const body = await res.json().catch(() => null);
        throw new Error(body?.detail ?? "gateway returned " + res.status);
      }

      const data = await res.json();
      setTrace((prev) => [{
        id: Date.now(),
        question: q,
        answer: data.answer ?? "",
        provider: data.provider ?? "unknown",
        shared: data.shared?.operator_shared_context ?? shared,
        at: data.timestamp ?? new Date().toISOString(),
        saved: false,
      }, ...prev]);
      setQuestion("");
    } catch (err) {
      const e = err as Error;
      setError(e?.name === "AbortError"
        ? "The copilot did not answer within 45 seconds. The incident response is unaffected."
        : e?.message ?? "The copilot is unavailable.");
    } finally {
      window.clearTimeout(giveUp);
      setBusy(false);
    }
  }

  if (!open) return null;

  const sharedCount = Object.keys(shared).length;

  return (
    <>
      <div className="copilot-scrim" onClick={onClose} aria-hidden="true" />
      <aside className="copilot" role="dialog" aria-labelledby="copilot-title">
        <header className="copilot-head">
          <div>
            <h2 id="copilot-title"><Sparkles size={16} /> Featherless EHS Copilot</h2>
            <p>Open-model intelligence &middot; Advisory only</p>
          </div>
          <div className="copilot-head-right">
            <span className="human-mode">Human mode</span>
            <button type="button" className="copilot-close" onClick={onClose} aria-label="Close copilot">&times;</button>
          </div>
        </header>

        {/* Permanent, not conditional. The moment a safety disclaimer is
            shown only sometimes, its absence starts meaning the opposite. */}
        <div className="advisory-pill">Advisory only &middot; human in control</div>

        <section className="copilot-section">
          <h3>Context shared manually</h3>
          <div className="chips">
            {([
              ["location", context.location],
              ["substance", context.substance],
              ["incidentType", context.incidentType],
              ["language", context.language],
            ] as const).map(([key, value]) => (
              <button
                key={key}
                type="button"
                className={share[key] ? "chip chip-on" : "chip"}
                aria-pressed={share[key]}
                onClick={() => setShare((s) => ({ ...s, [key]: !s[key] }))}
              >
                <span aria-hidden="true">{share[key] ? "✓" : "×"}</span> {value}
              </button>
            ))}
          </div>
          <button type="button" className="drawer-toggle" onClick={() => setDrawerOpen((d) => !d)}>
            {drawerOpen ? "Hide" : "Show"} exactly what is sent ({sharedCount} field{sharedCount === 1 ? "" : "s"})
          </button>
          {drawerOpen && <pre className="shared-drawer">{JSON.stringify(shared, null, 2)}</pre>}
        </section>

        <section className="copilot-section">
          <h3>Ask the incident copilot</h3>
          <textarea
            className="copilot-input"
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            placeholder="What should I verify before escalating this spill?"
            rows={3}
            maxLength={2000}
          />
          <button type="button" className="ask-btn" onClick={() => ask(question)} disabled={busy || !question.trim()}>
            {busy ? <><Sparkles size={14} className="spark-spin" /> Analysing&hellip;</> : "Ask Copilot"}
          </button>

          <div className="prompt-chips">
            {COPILOT_PROMPTS.map((p) => (
              <button key={p} type="button" className="prompt-chip" disabled={busy} onClick={() => ask(p)}>{p}</button>
            ))}
          </div>
        </section>

        {error && <div className="copilot-error" role="alert">{error}</div>}

        {trace.map((entry) => (
          <article key={entry.id} className="answer-card">
            <div className="answer-head">
              <span className="answer-tag">AI advisory &middot; review before use</span>
              <span className="answer-meta">{entry.provider}</span>
            </div>
            <p className="answer-q">{entry.question}</p>
            <div className="answer-body">{entry.answer}</div>
            <div className="answer-actions">
              <button type="button" onClick={() => navigator.clipboard?.writeText(entry.answer)}>Copy</button>
              <button
                type="button"
                onClick={() => {
                  onSaveNote(entry.answer);
                  setTrace((prev) => prev.map((t) => (t.id === entry.id ? { ...t, saved: true } : t)));
                }}
              >
                {entry.saved ? "Added to incident notes" : "Add note to incident"}
              </button>
              <button type="button" onClick={() => setQuestion("Follow-up: ")}>Ask follow-up</button>
            </div>
            {entry.saved && <div className="ai-assisted-badge">AI-assisted, operator reviewed</div>}
            <div className="answer-shared">
              Sent: {Object.keys(entry.shared).join(", ") || "nothing"} &middot; {new Date(entry.at).toLocaleTimeString()}
            </div>
          </article>
        ))}

        <footer className="copilot-foot">
          <strong>Human decision required</strong>
          <span>Featherless can analyse context. Only an authorised operator can issue instructions. This panel cannot dispatch, broadcast, or control equipment.</span>
        </footer>
      </aside>
    </>
  );
}

export default function Home() {

  const [location, setLocation] = useState(locations[0]);
  const [substance, setSubstance] = useState(substances[0]);
  const [incidentType, setIncidentType] = useState<IncidentType>("Spill");
  const [language, setLanguage] = useState("English");
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [queuedBehind, setQueuedBehind] = useState(0);
  // One /incident in flight, ever. `disabled={isSubmitting}` is not
  // enough on its own: React state lands a render late, so a fast
  // double-click, a keyboard repeat, or a second tab can still put two
  // requests on the wire. Measured against the deployed service, six
  // back-to-back requests had five rejected with 429 -- an abandoned
  // request keeps consuming the provider's rate limit server-side, so a
  // race costs the quota even when we stop waiting for it.
  //
  // A promise chain rather than a boolean flag: each call waits on the
  // previous one to settle, whatever it settled as.
  const inFlight = useRef<Promise<void>>(Promise.resolve());
  const outstanding = useRef(0);
  const [lockout, setLockout] = useState<IncidentResponse | null>(null);
  const [lockoutFromFallback, setLockoutFromFallback] = useState(false);
  // The draft, waiting on a human. Generating a protocol and
  // broadcasting it to a plant were one button; they are now two steps
  // with a person in between, which is the rule this whole system is
  // built around -- nothing that reaches a worker is issued without an
  // authorised human approving it.
  const [review, setReview] = useState<IncidentResponse | null>(null);
  const [copilotOpen, setCopilotOpen] = useState(false);
  // Notes the operator chose to keep from the copilot. Kept in the
  // console rather than posted anywhere: the copilot advises, the
  // person decides what becomes part of the incident.
  const [operatorNotes, setOperatorNotes] = useState<string[]>([]);

  const [cameraOn, setCameraOn] = useState(false);
  const [micOn, setMicOn] = useState(false);
  const [fieldLevel, setFieldLevel] = useState(0);
  const [voiceLevel, setVoiceLevel] = useState(0);
  const [mediaError, setMediaError] = useState<string | null>(null);
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const lockoutVideoRef = useRef<HTMLVideoElement | null>(null);
  const cameraStreamRef = useRef<MediaStream | null>(null);
  const micStreamRef = useRef<MediaStream | null>(null);
  const audioContextRef = useRef<AudioContext | null>(null);
  const audioFrameRef = useRef<number | null>(null);

  const selectedIncident = useMemo(() => incidentTypes.find((item) => item.label === incidentType) ?? incidentTypes[0], [incidentType]);

  useEffect(() => () => {
    cameraStreamRef.current?.getTracks().forEach((track) => track.stop());
    micStreamRef.current?.getTracks().forEach((track) => track.stop());
  }, []);

  useEffect(() => {
    if (cameraOn && videoRef.current && cameraStreamRef.current) {
      videoRef.current.srcObject = cameraStreamRef.current;
    }
  }, [cameraOn]);

  useEffect(() => {
    if (lockout && cameraOn && lockoutVideoRef.current && cameraStreamRef.current) {
      lockoutVideoRef.current.srcObject = cameraStreamRef.current;
    }
  }, [cameraOn, lockout]);

  useEffect(() => {
    if (!micOn || !micStreamRef.current) {
      if (audioFrameRef.current) cancelAnimationFrame(audioFrameRef.current);
      audioContextRef.current?.close();
      audioContextRef.current = null;
      setFieldLevel(0);
      setVoiceLevel(0);
      return;
    }
    const context = new AudioContext();
    const source = context.createMediaStreamSource(micStreamRef.current);
    const fieldAnalyser = context.createAnalyser();
    const voiceAnalyser = context.createAnalyser();
    const voiceFilter = context.createBiquadFilter();
    fieldAnalyser.fftSize = 64;
    voiceAnalyser.fftSize = 64;
    voiceFilter.type = "bandpass";
    voiceFilter.frequency.value = 1450;
    voiceFilter.Q.value = 0.7;
    source.connect(fieldAnalyser);
    source.connect(voiceFilter);
    voiceFilter.connect(voiceAnalyser);
    audioContextRef.current = context;
    const fieldData = new Uint8Array(fieldAnalyser.frequencyBinCount);
    const voiceData = new Uint8Array(voiceAnalyser.frequencyBinCount);
    let frameCounter = 0;
    const readLevels = () => {
      fieldAnalyser.getByteFrequencyData(fieldData);
      voiceAnalyser.getByteFrequencyData(voiceData);
      frameCounter += 1;
      if (frameCounter % 6 === 0) {
        setFieldLevel(fieldData.reduce((sum, value) => sum + value, 0) / fieldData.length / 255);
        setVoiceLevel(voiceData.reduce((sum, value) => sum + value, 0) / voiceData.length / 255);
      }
      audioFrameRef.current = requestAnimationFrame(readLevels);
    };
    readLevels();
    return () => {
      if (audioFrameRef.current) cancelAnimationFrame(audioFrameRef.current);
      context.close();
      audioContextRef.current = null;
    };
  }, [micOn]);

  async function toggleCamera() {
    setMediaError(null);
    if (cameraOn) {
      cameraStreamRef.current?.getTracks().forEach((track) => track.stop());
      cameraStreamRef.current = null;
      if (videoRef.current) videoRef.current.srcObject = null;
      setCameraOn(false);
      return;
    }
    if (!navigator.mediaDevices?.getUserMedia) {
      setMediaError("Camera access is unavailable in this browser.");
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment" }, audio: false });
      cameraStreamRef.current = stream;
      if (videoRef.current) videoRef.current.srcObject = stream;
      setCameraOn(true);
    } catch (err) {
      // Naming the DOMException matters: NotAllowedError is a blocked
      // permission the operator can re-grant, NotFoundError is no
      // camera attached, and NotReadableError is another app holding
      // the device. "Permission was not granted" was wrong for two of
      // those three, and sent people to the wrong setting.
      const name = (err as Error)?.name ?? "";
      setMediaError(
        name === "NotFoundError" || name === "OverconstrainedError"
          ? "No camera found on this device. The console remains fully operational."
          : name === "NotReadableError"
            ? "The camera is already in use by another application. Close it and retry."
            : name === "NotAllowedError"
              ? "Camera permission was blocked. Allow it in the browser's address-bar icon, then retry."
              : `Camera unavailable (${name || "unknown error"}). The console remains fully operational.`);
    }
  }

  async function toggleMic() {
    setMediaError(null);
    if (micOn) {
      micStreamRef.current?.getTracks().forEach((track) => track.stop());
      micStreamRef.current = null;
      setMicOn(false);
      return;
    }
    if (!navigator.mediaDevices?.getUserMedia) {
      setMediaError("Microphone access is unavailable in this browser.");
      return;
    }
    try {
      micStreamRef.current = await navigator.mediaDevices.getUserMedia({ video: false, audio: true });
      setMicOn(true);
    } catch (err) {
      const name = (err as Error)?.name ?? "";
      setMediaError(
        name === "NotFoundError"
          ? "No microphone found on this device. The console remains fully operational."
          : name === "NotReadableError"
            ? "The microphone is already in use by another application. Close it and retry."
            : name === "NotAllowedError"
              ? "Microphone permission was blocked. Allow it in the browser's address-bar icon, then retry."
              : `Microphone unavailable (${name || "unknown error"}). The console remains fully operational.`);
    }
  }

  function triggerProtocol() {
    // Queue behind whatever is running, and tell the operator we did
    // rather than dropping the press or racing it. outstanding counts
    // the in-flight call too, so what is displayed is one less.
    outstanding.current += 1;
    setQueuedBehind(outstanding.current - 1);
    const mine = inFlight.current.then(sendIncident, sendIncident);
    inFlight.current = mine;
    return mine;
  }

  async function sendIncident() {
    setIsSubmitting(true);
    if (!cameraOn) await toggleCamera();
    if (!micOn) await toggleMic();
    const payload = { location, substance, incident_type: incidentType, language: languageCodes[language] ?? "en", media: { camera: cameraOn || Boolean(cameraStreamRef.current), microphone: micOn || Boolean(micStreamRef.current) } };
    let response = defaultResponse;
    let live = false;

    // A backend that is down must not look like a console that is
    // broken. Without this, fetch waits on the browser's own timeout --
    // minutes -- and the button sits on "Contacting incident gateway..."
    // forever, which is exactly what a dead Render instance produced.
    // Twelve seconds, then fall back and say so.
    const abort = new AbortController();
    const giveUp = window.setTimeout(() => abort.abort(), 12000);

    try {
      const result = await fetch(apiUrl("/incident"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
        signal: abort.signal,
      });
      if (result.ok) {
        const data = await result.json();
        if (data?.severity && Array.isArray(data?.steps) && data?.spoken_alert) {
          // Normalised once, here, at the only place a backend response
          // enters this console.
          //
          // Every backend in this project returns severity lower case
          // ("critical"), and the UI compared it against "CRITICAL".
          // The comparison silently failed, so a CRITICAL chlorine
          // incident rendered with the styling of a routine one -- the
          // single case where the screen most needs to look different,
          // looking the same. Nothing threw and nothing logged.
          response = {
            ...data,
            severity: String(data.severity).toUpperCase() as Severity,
          } as IncidentResponse;
          live = true;
        } else {
          console.error("[incident] backend answered %s but the body did not match the expected shape (severity, steps[], spoken_alert):", result.status, data);
        }
      } else {
        console.error("[incident] backend returned %s %s", result.status, result.statusText);
      }
    } catch (error) {
      if ((error as Error)?.name === "AbortError") {
        console.error("[incident] %s did not answer within 12s -- it is probably asleep or not deployed.", apiUrl("/incident"));
      } else {
        console.error("[incident] request to %s failed:", apiUrl("/incident"), error);
      }
    } finally {
      window.clearTimeout(giveUp);
    }

    // Falling back keeps the console usable, but it must never be
    // mistaken for a real answer -- the locked screen says so, and the
    // console above says why. Silent fallback is how invented safety
    // steps get demoed as if a backend produced them.
    if (!live) {
      console.error("[incident] SHOWING LOCAL DEMO FALLBACK -- these steps did not come from the backend. Set VITE_API_BASE_URL and rebuild.");
    }

    outstanding.current = Math.max(0, outstanding.current - 1);
    setQueuedBehind(Math.max(0, outstanding.current - 1));
    setIsSubmitting(false);
    setLockoutFromFallback(!live);
    setReview(response);          // draft, not broadcast
  }

  function closeMedia() {
    cameraStreamRef.current?.getTracks().forEach((track) => track.stop());
    micStreamRef.current?.getTracks().forEach((track) => track.stop());
    cameraStreamRef.current = null;
    micStreamRef.current = null;
    if (videoRef.current) videoRef.current.srcObject = null;
    if (lockoutVideoRef.current) lockoutVideoRef.current.srcObject = null;
    setCameraOn(false);
    setMicOn(false);
  }

  function resetConsole() {
    window.speechSynthesis?.cancel();
    closeMedia();
    setLockout(null);
    setReview(null);
  }

  if (review && !lockout) {
    return (
      <ReviewView
        response={review}
        context={{ location, substance, incidentType, language }}
        fromFallback={lockoutFromFallback}
        onCancel={() => setReview(null)}
        onConfirm={() => setLockout(review)}
      />
    );
  }

  if (lockout) {
    return <LockoutView response={lockout} context={{ location, substance, incidentType, language }} cameraOn={cameraOn} micOn={micOn} fieldLevel={fieldLevel} voiceLevel={voiceLevel} videoRef={lockoutVideoRef} onReset={resetConsole} fromFallback={lockoutFromFallback} />;
  }

  return (
    <main className="console-shell">
      <div className="console-grid" />
      <div className="console-topline" />
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark"><Siren size={21} strokeWidth={2.4} /></span>
          <span><strong>HAZARD WATCH</strong><small>Operational safety network</small></span>
        </div>
        <div className="topbar-center"><span className="live-indicator" /> Live console <span className="topbar-slash">/</span> Incident gateway <span className="gateway-status">CONNECTED</span></div>
        <div className="topbar-right"><MapPin size={15} /> <span>Plant 04 · South Zone</span><span className="topbar-divider" /><span className="utc-time">UTC+05:30</span></div>
      </header>

      <div className="page-frame">
        <section className="hero-copy">
          <div className="hero-kicker"><span>01</span> Incident command console</div>
          <h1>Make the right call<br /><em>before it spreads.</em></h1>
          <p>Declare a site hazard to activate the appropriate emergency protocol, broadcast the alert, and lock the console for response coordination.</p>
          <div className="hero-meta"><span><span className="meta-dot" /> Response target: under 1 second</span><span><ShieldAlert size={14} /> No incident declared</span></div>
        </section>

        <section className="incident-panel" aria-label="Incident declaration form">
          <div className="panel-header">
            <div><span className="panel-index">Incident gateway</span><h2>Declare an incident</h2></div>
            <span className="panel-lock"><LockKeyhole size={14} /> Secured console</span>
          </div>
          <div className="form-fields">
            <SelectField label="Bay / machine ID" value={location} options={locations} onChange={setLocation} icon={Factory} />
            <SelectField label="Substance involved" value={substance} options={substances} onChange={setSubstance} icon={Beaker} />
            <SelectField label="Alert language" value={language} options={languages} onChange={setLanguage} icon={Languages} />
          </div>
          <div className="field-shell incident-selector">
            <span className="field-label">Incident type</span>
            <div className="incident-options" role="radiogroup" aria-label="Incident type">
              {incidentTypes.map(({ label, icon: Icon, note }) => (
                <button key={label} className={`incident-option ${incidentType === label ? "selected" : ""}`} onClick={() => setIncidentType(label)} role="radio" aria-checked={incidentType === label}>
                  <span className="incident-icon"><Icon size={19} strokeWidth={1.8} /></span>
                  <span><strong>{label}</strong><small>{note}</small></span>
                  <span className="radio-dot" />
                </button>
              ))}
            </div>
          </div>
          <div className="selection-summary"><span className="selection-icon"><selectedIncident.icon size={16} /></span><span>Ready to declare <strong>{incidentType.toLowerCase()}</strong> at <strong>{location}</strong></span><span className="summary-line" /></div>
          <button className="trigger-button" onClick={triggerProtocol} disabled={isSubmitting || queuedBehind > 0}>
            <span className="trigger-icon" aria-hidden="true">
              <svg className="hazard-symbol" viewBox="0 0 32 28" role="img">
                <path d="M14.06 2.42a2.25 2.25 0 0 1 3.88 0l12.1 20.93A2.25 2.25 0 0 1 28.1 26.7H3.9a2.25 2.25 0 0 1-1.94-3.35l12.1-20.93Z" fill="currentColor" />
                <path d="M16 8.2v8.7" stroke="#f05a47" strokeWidth="3.2" strokeLinecap="round" />
                <circle cx="16" cy="21.2" r="1.8" fill="#f05a47" />
              </svg>
            </span>
            <span>{queuedBehind > 0
              ? `Processing previous alert… (${queuedBehind} queued)`
              : isSubmitting
                ? "Contacting incident gateway…"
                : "Generate breach protocol"}</span>
            <span className="trigger-arrow">↗</span>
          </button>
          <p className="panel-footnote">Use only for confirmed or imminent hazards. This generates a draft protocol for review — nothing is broadcast until a supervisor confirms it.</p>
        </section>

        <aside className="side-rail">
          <SystemStatus />
          <section className="briefing-card">
            <div className="briefing-heading"><span className="eyebrow">Response brief</span><Building2 size={17} /></div>
            <div className="briefing-row"><span>Current site</span><strong>South manufacturing zone</strong></div>
            <div className="briefing-row"><span>On-call lead</span><strong>Shift B · EHS desk</strong></div>
            <div className="briefing-row"><span>Protocol library</span><strong>Synced 2 min ago</strong></div>
          </section>
          <button type="button" className="copilot-open" onClick={() => setCopilotOpen(true)}>
            <span className="copilot-open-title"><Sparkles size={15} /> Open Featherless EHS Copilot</span>
            <span className="copilot-open-sub">Human-guided analysis &middot; no autonomous actions</span>
          </button>
          {operatorNotes.length > 0 && (
            <section className="notes-card">
              <div className="briefing-heading"><span className="eyebrow">Incident notes</span><span className="notes-count">{operatorNotes.length}</span></div>
              {operatorNotes.map((note, i) => (
                <div key={i} className="note-row">
                  <span className="note-badge">AI-assisted, operator reviewed</span>
                  <p>{note.length > 220 ? note.slice(0, 220) + String.fromCharCode(8230) : note}</p>
                </div>
              ))}
            </section>
          )}
          <div className="rail-notice"><span><AlertTriangle size={14} /> Decision support</span><p>Protocol output is generated from the selected chemical and event type. The copilot is advisory and never issues instructions.</p></div>
        </aside>
        <CopilotPanel
          open={copilotOpen}
          onClose={() => setCopilotOpen(false)}
          context={{ location, substance, incidentType, language }}
          onSaveNote={(text) => setOperatorNotes((n) => [text, ...n])}
        />
        <MediaDock cameraOn={cameraOn} micOn={micOn} unknownMaterial={substance === "Unknown Substance"} fieldLevel={fieldLevel} voiceLevel={voiceLevel} mediaError={mediaError} videoRef={videoRef} onToggleCamera={toggleCamera} onToggleMic={toggleMic} />
      </div>

      <footer className="console-footer"><span>HW-OS / CONSOLE 04</span><span>Classification: internal operational use</span><span>v2.4.1 · All systems nominal</span></footer>
    </main>
  );
}
