"""Design Studio: a conversational solutions designer that turns a business conversation into a desk blueprint.

Flow
  1. The owner chats with the designer. Every designer reply carries a hidden machine block
     <atlas-design>{...}</atlas-design> with suggestion chips and the current blueprint draft.
  2. The blueprint (agents, hierarchy, tools, workflows, connectors, policy) grows turn by turn and is drawn
     live on the sketch canvas in the portal.
  3. `blueprint_to_desk` converts the approved blueprint into the desk config the engine runs.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from . import templates as T
from . import tools as TL

_BLOCK = re.compile(r"<atlas-design>\s*(\{.*?\})\s*</atlas-design>", re.S)
_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)

SPECIALIST_TOOLS = ["read_file", "list_files", "web_fetch", "run_python", "save_deliverable", "camera_look", "camera_events", "camera_ask"]
ATLAS_TOOLS = ["delegate", "list_agents", "save_deliverable", "read_file", "list_files", "crm_lookup", "crm_update",
                "queue_action", "list_connectors", "http_request", "schedule_task", "mcp", "run_python", "remember", "recall", "generate_media"]
PALETTE = ["#7c3aed", "#db2777", "#1f9d63", "#b45309", "#0e7490", "#6d28d9", "#ea580c", "#15803d", "#a21caf", "#0369a1"]
STATUS_MARK = "\x00"                      # on_token prefix for a status line instead of prose
CONNECTOR_KINDS = ("smtp", "imap", "http", "mcp", "webhook", "hermes_agent", "higgsfield")
TRIGGER_KINDS = ("webhook", "inbox", "schedule", "manual")

DESIGNER_SYSTEM = """You are Atlas, the orchestrator of an AI operations desk, talking to a business owner for the first time.
Your job: understand their business, find the highest-value processes to take off their plate, and assemble your own
team - the specialist agents you will brief, run and review - to do that work. Speak in the first person ("I'll add a
researcher who…"); you are the one who will lead this team.

How to run the conversation
- Professional, plain English, no hype. 40-110 words per turn. One focused question per turn.
- Make concrete suggestions early. After the first answer you already know enough to sketch a first draft of the desk;
  refine it every turn instead of asking ten questions first.
- Prefer ONE function first (e.g. inbound lead handling, proposal writing, inbox triage, order follow-ups), then extras.
- Every turn, state briefly what changed in the blueprint ("Added a research agent that…").
- If the owner asks what you understood, what you are doing, or why - answer that directly in your own words (what
  they told you, what you inferred, what you are unsure of), then ask your one question. Never answer with a stock line.
- Do not invent a team from a greeting or a question. Sketch a blueprint only once the owner has said something about
  their business or the work; until then "blueprint" is null.
- When the design is solid (usually after 3-5 exchanges), set "ready": true and tell the owner to review the sketch
  and press Approve & build.

Design rules
- Atlas (id "atlas") is always the root orchestrator. Every other agent has "reports_to": "atlas", except members of a
  sub-team, whose "reports_to" is their lead's id. Use a sub-team (one lead + 2-4 members, max depth atlas -> lead ->
  member) only when the work naturally splits into parallel strands with their own coordinator (a research pod over
  several markets, one writer per channel). Flat is the default.
- 2-6 specialist agents. Each agent: short id (a-z, _), name, role (3-6 words), goal (1-2 sentences: what it produces
  and the quality bar), tools (subset of: read_file, list_files, web_fetch, run_python, save_deliverable),
  "strong": true only if the role needs top-tier judgement or client-facing writing,
  "instructions": 3-6 short operating rules written for THIS role in THIS business (what to check first, what it
  must never do, the exact shape of what it hands back) - these become the agent's standing orders,
  "engine": "hermes_agent" or "atlas", and "reports_to": "atlas" for a top-level agent or the id of the lead it
  works under. A lead is just an agent whose members name it in "reports_to" - e.g. {"id": "enquiries_lead",
  "reports_to": "atlas"} with {"id": "stock", "reports_to": "enquiries_lead"}. When the owner asks for a pod, a
  sub-team, or "put X under Y", you MUST set reports_to on the members - the structure is drawn from that field. Use "hermes_agent" (the Nous Research Hermes Agent runtime: own browser,
  terminal, file system, skills and per-client long-term memory) for roles that must browse live websites, run code
  or shell commands, work through files over many steps, reconcile data, or remember a client between runs.
  Use "atlas" (the fast built-in loop) for drafting, replying, classifying, summarising and QA.
- Design the team for THIS job. Never reuse a stock line-up: the roles, their instructions and the workflow steps
  must follow from what the owner actually described.
- Workflows: 1-3, each with an id, name, trigger {"kind": webhook|inbox|schedule|manual, "detail": text} and ordered
  "steps": list of agent ids. Atlas reviews and approves outbound messages implicitly; do not list atlas in steps.
- Connectors the desk needs: kinds smtp (send email), imap (watch inbox), http (any REST API), mcp (tool server:
  Slack, Notion, Google Sheets, GitHub...), webhook (web forms, Zapier, Make). Each: {"kind", "name", "purpose", "required": bool}.
- Policy: {"no_money_figures": bool, "max_words": int, "banned_phrases": [..]}. Default no_money_figures true for
  anything customer-facing with quotes/prices.
- Business: {"name","tagline","description","services":[..],"target_clients","tone","sender_name","availability","pricing_notes"}.
  Fill what you know; leave unknown fields out (do not invent a sender name or pricing).

Output format — MANDATORY on every turn
Write your reply to the owner as plain prose first (no markdown headings, no bullet spam). Then, on a new line, append
exactly one machine block and nothing after it:

<atlas-design>{"suggestions": ["3-4 short reply options for the owner, max 8 words each"], "ready": false, "blueprint": { "business": {...}, "agents": [...], "workflows": [...], "connectors": [...], "policy": {...} }}</atlas-design>

The blueprint must be COMPLETE each time (full current state, not a diff). On the very first turn, before the owner
has said anything substantive, "blueprint" may be null."""

GREETING = ("Hi, I'm Atlas. I run a team of AI agents for your business — I brief them, check their work, and nothing goes "
            "out without your approval. Tell me what your business does and which task eats the most time each week: "
            "answering enquiries, writing proposals, chasing invoices, watching an inbox, anything repetitive. I'll "
            "assemble the team in front of you as we talk.")
GREETING_SUGGESTIONS = ["We get enquiries we answer too slowly", "Proposals take us days to write",
                        "Our inbox needs triage every morning", "I want my shop cameras documented"]

CAMERA_GUIDE = """

Cameras (the desk's eyes)
- If the owner mentions cameras, CCTV, footage, a shop floor, a lobby, a kitchen, a till or "watching" anything, add a
  "cameras" list to the blueprint: [{"name": "short-slug", "source": "...", "notes": "where it points",
  "focus": "what to document in detail", "journal": true, "alerts": false, "watch_for": "person"}].
- "source" is what the owner gave you: an RTSP address (rtsp://user:pass@ip:554/...), a snapshot URL, a webcam index
  ("0"), or "sample:<clip>" for sample footage from the library below. If they have cameras but gave no address yet,
  use "" and ask for the stream addresses (the owner can also add them later on the Cameras page).
- "journal": true makes the camera keep a detailed, searchable written record of everything it sees (what people do,
  wear, carry, how long they wait, what changes) - the owner can later ask questions about it. Default true.
- "alerts": true only if the owner wants the agents woken when something specific happens; then say what in "focus".
- Give any agent that reads or reports on the cameras the tools camera_ask and camera_events (camera_look for a fresh
  frame). Agents never narrate feeds themselves - the journal does that.
- If the owner wants to try it without their own cameras, offer the sample footage and use "sample:" sources. When they
  want several cameras on one place, use clips that share a site (the campus-* set is five angles of one campus; the kitchen-* set is five angles of one real kitchen - use it for restaurants, cafes and food businesses).
- One watcher per camera: give that agent "camera": "<camera name>" so it only reads its own feed."""


SWITCH_TO_PAID = "Switch to the paid model"
PAID_VLM = os.environ.get("ATLAS_PAID_VLM", "google/gemini-2.5-flash-lite")   # cheap paid eyes for desks off the free tier


def sample_dir() -> Path:
    return Path(os.environ.get("ATLAS_SAMPLE_VIDEOS") or (Path.home() / "AtlasDemo" / "videos"))


SAMPLE_LABELS = {
    "corner-store_ezymart": "corner shop till, real CCTV (a wallet is taken from a customer's backpack)",
    "retail-store": "retail shop floor and checkout, real CCTV",
    "liquor-store-delivery": "liquor shop counter, real CCTV",
    "hotel-lobby_Browse4": "hotel lobby reception desk, ceiling camera",
    "hotel-lobby_LeftBag": "hotel lobby seating, someone leaves a bag behind",
    "hotel-lobby_Meet_Crowd": "hotel lobby entrance, a group meets and splits",
    "hotel-lobby_Browse_WhileWaiting2": "hotel lobby waiting area, a guest waits",
    "restaurant-sushi-counter": "sushi restaurant, chef at the counter",
    # five synchronised cameras on ONE site (MEVA dataset, CC BY 4.0): use them together for a multi-camera desk
    "campus-lobby": "campus building, lobby and coffee point by the doors, people meeting (same site as the other campus-* clips, same 5 minutes)",
    "campus-entrance": "campus building, main entrance and forecourt from above, people arriving (same site, same 5 minutes)",
    "campus-carpark": "campus car park, cars and people walking through (same site, same 5 minutes)",
    "campus-drive": "campus approach road and paths, vehicles and pedestrians at distance (same site, same 5 minutes)",
    "campus-gym": "campus sports hall, court and bleachers, mostly empty (same site, same 5 minutes)",
    # five synchronised cameras in ONE real kitchen (EPFL-Smart-Kitchen-30, faces blurred, one cook): a restaurant back-of-house desk
    "kitchen-overview": "kitchen, whole room from the doorway corner, cook moving between stations (same kitchen as the other kitchen-* clips, same 5 minutes)",
    "kitchen-wide": "kitchen, long view down the counters from the far end (same kitchen, same 5 minutes)",
    "kitchen-stove": "kitchen, hob and pans from above (same kitchen, same 5 minutes)",
    "kitchen-prep": "kitchen, chopping board and sink, close on the cook's hands (same kitchen, same 5 minutes)",
    "kitchen-sink": "kitchen, sink and back counter, cook washing and plating (same kitchen, same 5 minutes)",
}


SAMPLE_CAMERA_NAMES = {"corner-store_ezymart": "till", "retail-store": "shop-floor", "liquor-store-delivery": "liquor-counter",
                       "hotel-lobby_Browse4": "reception", "hotel-lobby_LeftBag": "lobby-seating",
                       "hotel-lobby_Meet_Crowd": "lobby-entrance", "hotel-lobby_Browse_WhileWaiting2": "lobby-waiting",
                       "restaurant-sushi-counter": "sushi-counter", "campus-lobby": "lobby", "campus-entrance": "entrance",
                       "campus-carpark": "car-park", "campus-drive": "drive", "campus-gym": "gym",
                       "kitchen-overview": "overview", "kitchen-wide": "counters", "kitchen-stove": "stove", "kitchen-prep": "prep",
                       "kitchen-sink": "sink"}


def sample_clips() -> dict[str, str]:
    """Sample footage the owner can try the desk on: {clip name: path}. Any .mp4 in the sample folder counts."""
    d = sample_dir()
    return {p.stem: str(p) for p in sorted(d.glob("*.mp4"))} if d.is_dir() else {}


def resolve_camera_source(src: str) -> str:
    src = (src or "").strip()
    if src.lower().startswith("sample:"):
        return sample_clips().get(src.split(":", 1)[1].strip(), "")
    return src


def camera_guide() -> str:
    clips = sample_clips()
    if not clips:
        return CAMERA_GUIDE + "\n- No sample footage is installed on this desk."
    return CAMERA_GUIDE + "\n- Sample footage library (use as \"sample:<name>\"):\n" + "\n".join(
        f"  {n}: {SAMPLE_LABELS.get(n, n.replace('_', ' ').replace('-', ' '))}" for n in clips)


# ---------------------------------------------------------------------------- sessions
class DesignSession:
    def __init__(self, mode: str, tier: str = "free"):
        self.id = uuid.uuid4().hex[:12]
        self.mode = mode
        self.tier = tier
        self.created = time.time()
        self.messages: list[dict[str, Any]] = []         # provider-neutral {"role","content"} history
        self.transcript: list[dict[str, Any]] = [{"role": "assistant", "text": GREETING}]
        self.blueprint: dict[str, Any] | None = None
        self.suggestions: list[str] = list(GREETING_SUGGESTIONS) + (["Try it on sample hotel footage"] if sample_clips() else [])
        self.ready = False
        self.desk_id: int | None = None
        self.turn = 0
        self.links: list[str] = []
        self.profile: dict[str, Any] | None = None       # company profile from the pre-study of their links
        self.lock = threading.Lock()

    def public(self) -> dict[str, Any]:
        return {"sid": self.id, "mode": self.mode, "tier": self.tier, "transcript": self.transcript,
                "blueprint": self.blueprint, "suggestions": self.suggestions, "ready": self.ready,
                "desk_id": self.desk_id, "turn": self.turn, "links": self.links, "profile": self.profile}

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "mode": self.mode, "tier": self.tier, "created": self.created, "messages": self.messages,
                "transcript": self.transcript, "blueprint": self.blueprint, "suggestions": self.suggestions,
                "ready": self.ready, "desk_id": self.desk_id, "turn": self.turn, "links": self.links, "profile": self.profile}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "DesignSession":
        s = cls(d.get("mode", "demo"), d.get("tier", "free"))
        for k in ("id", "created", "messages", "transcript", "blueprint", "suggestions", "ready", "desk_id", "turn", "links", "profile"):
            if k in d:
                setattr(s, k, d[k])
        return s

    def apply_profile(self, profile: dict[str, Any]) -> None:
        """Seed the conversation from a study of the owner's links: opening message, chips, first-draft blueprint."""
        self.profile = profile
        opening = profile.get("opening_message") or GREETING
        self.transcript = [{"role": "assistant", "text": opening}]
        self.messages = [{"role": "assistant", "content": opening}]
        self.suggestions = [str(x)[:60] for x in (profile.get("suggestions") or GREETING_SUGGESTIONS)][:4]
        bp = normalise(profile.get("blueprint"), None) if isinstance(profile.get("blueprint"), dict) else None
        if bp and not bp["business"].get("name") and profile.get("name"):
            bp["business"]["name"] = profile["name"]
        if bp and bp.get("agents"):
            self.blueprint = bp


SESSIONS: dict[str, DesignSession] = {}
_SESSIONS_MAX = 200


def new_session(mode: str, tier: str = "free") -> DesignSession:
    s = DesignSession(mode, tier)
    if len(SESSIONS) >= _SESSIONS_MAX:
        for k in sorted(SESSIONS, key=lambda k: SESSIONS[k].created)[: _SESSIONS_MAX // 4]:
            SESSIONS.pop(k, None)
    SESSIONS[s.id] = s
    return s


# ---------------------------------------------------------------------------- parsing + normalising
def split_reply(raw: str) -> tuple[str, dict[str, Any] | None]:
    """Return (prose, machine dict|None). Tolerates missing tags / fenced JSON / trailing junk."""
    raw = raw or ""
    m = _BLOCK.search(raw)
    data: dict[str, Any] | None = None
    prose = raw
    if m:
        prose = raw[: m.start()].strip()
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            data = _loose_json(m.group(1))
    else:
        f = list(_FENCE.finditer(raw))
        if f:
            last = f[-1]
            try:
                cand = json.loads(last.group(1))
                if isinstance(cand, dict) and ("blueprint" in cand or "suggestions" in cand):
                    data = cand
                    prose = (raw[: last.start()] + raw[last.end():]).strip()
            except json.JSONDecodeError:
                pass
        if data is None and "<atlas-design>" in raw:          # opened but never closed (cut off)
            prose = raw.split("<atlas-design>", 1)[0].strip()
    prose = re.sub(r"</?atlas-design>", "", prose).strip()
    return prose, data if isinstance(data, dict) else None


def _loose_json(s: str) -> dict[str, Any] | None:
    s = s.strip()
    for cut in range(len(s), max(0, len(s) - 400), -1):        # walk back to the last parseable prefix + closers
        chunk = s[:cut]
        for tail in ("", "}", "}}", "]}", "]}}", "\"}", "\"]}", "\"]}}"):
            try:
                v = json.loads(chunk + tail)
                return v if isinstance(v, dict) else None
            except json.JSONDecodeError:
                continue
    return None


def _slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", str(s or "").lower()).strip("_")
    return s[:24] or "agent"


def normalise(bp: dict[str, Any] | None, prev: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Coerce a model-produced blueprint into the canonical shape; fall back to `prev` for missing sections."""
    if not isinstance(bp, dict):
        return prev
    prev = prev or {}
    out: dict[str, Any] = {}
    biz = bp.get("business") if isinstance(bp.get("business"), dict) else prev.get("business") or {}
    b: dict[str, Any] = {}
    for k in ("name", "tagline", "description", "target_clients", "tone", "sender_name", "availability", "pricing_notes"):
        v = biz.get(k)
        if isinstance(v, str) and v.strip():
            b[k] = v.strip()
    sv = biz.get("services")
    if isinstance(sv, str):
        sv = [x.strip() for x in sv.split(",")]
    if isinstance(sv, list):
        b["services"] = [str(x).strip() for x in sv if str(x).strip()][:10]
    out["business"] = b

    agents_in = bp.get("agents") if isinstance(bp.get("agents"), list) else prev.get("agents") or []
    agents: list[dict[str, Any]] = []
    seen: set[str] = set()
    prev_ids = {a["id"] for a in prev.get("agents") or []}
    prev_by_name = {str(a.get("name") or "").strip().lower(): a["id"] for a in prev.get("agents") or []}
    renamed: dict[str, str] = {}                       # id the model used this turn -> id the agent already has
    for i, a in enumerate(agents_in):
        if not isinstance(a, dict):
            continue
        aid = _slug(a.get("id") or a.get("name") or f"agent_{i}")
        keep = prev_by_name.get(str(a.get("name") or "").strip().lower())
        if keep and aid not in prev_ids and keep not in seen:      # same agent, new slug: keep the id the canvas knows
            renamed[aid] = aid = keep
        if aid in seen:
            continue
        seen.add(aid)
        tools = a.get("tools") if isinstance(a.get("tools"), list) else []
        tools = [t for t in tools if t in SPECIALIST_TOOLS] or ["read_file", "list_files"]
        instr = a.get("instructions")
        if isinstance(instr, str):
            instr = [x.strip(" -•\t") for x in instr.splitlines()]
        instr = [str(x).strip()[:220] for x in (instr if isinstance(instr, list) else []) if str(x).strip()][:8]
        agents.append({
            "id": aid,
            "name": str(a.get("name") or aid.replace("_", " ").title())[:40],
            "role": str(a.get("role") or "Specialist")[:60],
            "goal": str(a.get("goal") or a.get("description") or "")[:600],
            "tools": tools if aid != "atlas" else list(ATLAS_TOOLS),
            "reports_to": (_slug(a.get("reports_to") or "atlas") or "atlas") if aid != "atlas" else "",
            "camera": str(a.get("camera") or "")[:32],
            "strong": bool(a.get("strong")),
            "engine": "hermes_agent" if str(a.get("engine") or "").lower() in ("hermes_agent", "hermes") else "atlas",
            "instructions": instr,
        })
    agents = [a for a in agents if a["id"] != "atlas"][:8]
    for i, a in enumerate(agents):
        a["color"] = PALETTE[i % len(PALETTE)]
        a["reports_to"] = renamed.get(a["reports_to"], a["reports_to"])
    # hierarchy contract shared with atlas/team.py: unknown leads, cycles and chains deeper than 2 collapse to atlas
    from . import team as TM
    shape, _errs = TM.validate_team({"agents": [{**a, "instructions": a.get("instructions") or ["as briefed", "as briefed"]} for a in agents]},
                                    allowed_tools=SPECIALIST_TOOLS)
    struct = {s["id"]: s for s in shape["agents"]}
    for a in agents:
        s = struct.get(a["id"], {})
        a["reports_to"] = s.get("reports_to", "atlas")
        a["members"] = list(s.get("members") or [])
    out["agents"] = agents
    ids = {a["id"] for a in agents}

    wfs_in = bp.get("workflows") if isinstance(bp.get("workflows"), list) else prev.get("workflows") or []
    wfs: list[dict[str, Any]] = []
    for i, w in enumerate(wfs_in):
        if not isinstance(w, dict):
            continue
        trig = w.get("trigger") if isinstance(w.get("trigger"), dict) else {"kind": str(w.get("trigger") or "manual")}
        kind = str(trig.get("kind") or "manual").lower()
        kind = {"email": "inbox", "form": "webhook", "cron": "schedule", "timer": "schedule", "api": "webhook"}.get(kind, kind)
        if kind not in TRIGGER_KINDS:
            kind = "manual"
        steps = w.get("steps") if isinstance(w.get("steps"), list) else []
        steps = [_slug(s if isinstance(s, str) else (s or {}).get("agent")) for s in steps]
        steps = [renamed.get(s, s) for s in steps]
        steps = [s for s in steps if s in ids]
        wfs.append({"id": _slug(w.get("id") or w.get("name") or f"workflow_{i}"),
                    "name": str(w.get("name") or f"Workflow {i + 1}")[:60],
                    "trigger": {"kind": kind, "detail": str(trig.get("detail") or "")[:200]},
                    "steps": steps})
    out["workflows"] = wfs[:4]

    cons_in = bp.get("connectors") if isinstance(bp.get("connectors"), list) else prev.get("connectors") or []
    cons: list[dict[str, Any]] = []
    for c in cons_in:
        if isinstance(c, str):
            c = {"kind": c}
        if not isinstance(c, dict):
            continue
        kind = str(c.get("kind") or "").lower()
        kind = {"email": "smtp", "gmail": "imap", "inbox": "imap", "api": "http", "rest": "http", "form": "webhook", "zapier": "webhook"}.get(kind, kind)
        if kind not in CONNECTOR_KINDS:
            continue
        cons.append({"kind": kind, "name": str(c.get("name") or T_KIND_LABEL.get(kind, kind))[:40],
                     "purpose": str(c.get("purpose") or "")[:160], "required": bool(c.get("required", True))})
    out["connectors"] = cons[:8]

    cams_in = bp.get("cameras") if isinstance(bp.get("cameras"), list) else prev.get("cameras") or []
    cams: list[dict[str, Any]] = []
    seen_c: set[str] = set()
    for i, c in enumerate(cams_in):
        if isinstance(c, str):
            c = {"name": c}
        if not isinstance(c, dict):
            continue
        name = re.sub(r"[^a-z0-9-]+", "-", str(c.get("name") or f"camera-{i + 1}").lower()).strip("-")[:32] or f"camera-{i + 1}"
        if name in seen_c:
            continue
        seen_c.add(name)
        cams.append({"name": name, "source": str(c.get("source") or "").strip()[:400],
                     "notes": str(c.get("notes") or "")[:200], "focus": str(c.get("focus") or "")[:300],
                     "journal": c.get("journal", True) not in (False, "0", "false", "off"),
                     "alerts": c.get("alerts", False) in (True, "1", "true", "on"),
                     "watch_for": str(c.get("watch_for") or "person")[:60]})
    cams = cams[:12]
    _wire_cameras(agents, cams)
    out["cameras"] = cams

    pol_in = bp.get("policy") if isinstance(bp.get("policy"), dict) else prev.get("policy") or {}
    banned = pol_in.get("banned_phrases")
    if isinstance(banned, str):
        banned = [x.strip() for x in banned.split(",")]
    out["policy"] = {"no_money_figures": bool(pol_in.get("no_money_figures", True)),
                     "max_words": int(pol_in.get("max_words") or 220),
                     "banned_phrases": [str(x).strip() for x in (banned or []) if str(x).strip()][:20]}
    return out


_CAM_TOOLS = ("camera_ask", "camera_events", "camera_look")
_CAM_READER = re.compile(r"summar|digest|report|document|log|record|overview|incident|safety|security|cross.?camera", re.I)


def _wire_cameras(agents: list[dict[str, Any]], cams: list[dict[str, Any]]) -> None:
    """Make a camera team hang together (in place). Models routinely sketch "five watchers and a summarizer" with no
    cameras for them to watch and a summarizer that cannot read a single event:
    - every watcher gets a camera: missing ones are added as placeholders the owner fills in (source "");
    - watchers are bound one camera each, in order, and told which one is theirs;
    - an agent that summarises / documents / reports, in a team that has cameras, gets the read tools."""
    watchers = [a for a in agents if any(t in a["tools"] for t in _CAM_TOOLS)]
    if not watchers and not cams:
        return
    solo = [a for a in watchers if not _CAM_READER.search(f"{a['name']} {a['role']}") or re.search(r"watch|camera \d|cam \d", f"{a['name']} {a['role']}", re.I)]
    if len(solo) > 1:                                  # several watchers = one camera each
        names = {c["name"] for c in cams}
        n = 0
        while len(cams) < min(len(solo), 12):
            n += 1
            if f"camera-{n}" not in names:
                names.add(f"camera-{n}")
                cams.append({"name": f"camera-{n}", "source": "", "notes": "", "focus": "", "journal": True, "alerts": False, "watch_for": "person"})
        taken = {a["camera"] for a in solo if a.get("camera") in names}
        free = [c["name"] for c in cams if c["name"] not in taken]
        for a in solo:
            if a.get("camera") not in names:
                a["camera"] = free.pop(0) if free else ""
    if len(solo) > 1:
        agents.sort(key=lambda a: a not in solo)       # stable: watchers first, whoever reads across them after
    for a in agents:
        cam = a.get("camera") or ""
        a["instructions"] = [x for x in a["instructions"] if not x.startswith("Your camera is ")]
        if cam and a in solo and len(solo) > 1:
            a["instructions"] = ([f"Your camera is '{cam}': pass camera=\"{cam}\" to camera_ask, camera_events and camera_look."] + a["instructions"])[:8]
        elif a not in solo or len(solo) <= 1:
            a["camera"] = ""
        if a not in watchers and _CAM_READER.search(f"{a['name']} {a['role']} {a['goal']}"):
            a["tools"] = ["camera_events", "camera_ask"] + [t for t in a["tools"] if t not in _CAM_TOOLS]


T_KIND_LABEL = {"smtp": "Email sending", "imap": "Inbox", "http": "API", "mcp": "Tool server", "webhook": "Web form", "hermes_agent": "Hermes Agent", "higgsfield": "Higgsfield video"}


# ---------------------------------------------------------------------------- the turn
def _provider_for(mode: str, providers_cfg: dict[str, Any] | None):
    from .providers import ProviderPool
    if mode == "demo" or not providers_cfg:
        return None, ""
    pool = ProviderPool(providers_cfg)
    prov_name = providers_cfg.get("default_provider") or pool.default_name
    prov = pool.get(prov_name)
    model = T.STRONG_MODEL if prov_name == "openrouter" and False else ""      # tier-gated later; default model for now
    return prov, model


def reply(session: DesignSession, user_text: str, on_token: Callable[[str], None] | None = None,
          providers_cfg: dict[str, Any] | None = None, designer_model: str = "") -> dict[str, Any]:
    """Run one designer turn. Streams prose tokens through on_token (machine block is withheld)."""
    user_text = (user_text or "").strip()
    with session.lock:
        session.turn += 1
        session.transcript.append({"role": "user", "text": user_text})
        session.messages.append({"role": "user", "content": user_text})
        if session.mode == "demo":
            raw = _demo_turn(session, user_text, on_token)
        else:
            raw = _live_turn(session, providers_cfg, designer_model, on_token)
        prose, data = split_reply(raw)
        prose = re.sub(r"\*\*|__|^#+\s*", "", prose, flags=re.M).strip()      # no markdown in the chat column
        if not prose:
            prose = "Noted. I have updated the sketch — tell me more, or press Approve & build when it looks right."
        suggestions = []
        if data:
            sg = data.get("suggestions")
            if isinstance(sg, list):
                suggestions = [str(x).strip()[:60] for x in sg if str(x).strip()][:4]
            bp = normalise(data.get("blueprint"), session.blueprint)
            if bp and (bp.get("agents") or bp.get("business")):
                session.blueprint = bp
            if isinstance(data.get("ready"), bool):
                session.ready = data["ready"] and bool(session.blueprint and session.blueprint.get("agents"))
        session.suggestions = suggestions or session.suggestions
        session.transcript.append({"role": "assistant", "text": prose})
        session.messages.append({"role": "assistant", "content": raw})
        if len(session.messages) > 24:                       # keep the context bounded; blueprint state is in the last reply
            session.messages = session.messages[-16:]
        return {"text": prose, "suggestions": session.suggestions, "blueprint": session.blueprint, "ready": session.ready,
                "turn": session.turn}


def _live_turn(session: DesignSession, providers_cfg: dict[str, Any] | None, model: str,
               on_token: Callable[[str], None] | None) -> str:
    from .providers import ProviderPool
    pool = ProviderPool(providers_cfg or {})
    prov = pool.get()
    buf: list[str] = []
    gate = {"open": True}

    def tok(text: str, thinking: bool = False):
        if thinking or not on_token:
            return
        buf.append(text)
        if not gate["open"]:
            return
        joined = "".join(buf)
        cut = joined.find("<atlas")
        if cut >= 0:                                   # machine block starts: stop streaming prose
            gate["open"] = False
            on_token(STATUS_MARK + "Sketching the blueprint")
            return
        if "<" in text:                                # hold back a tag fragment so "<her" + "mes-design>" never leaks
            head = text.split("<", 1)[0]
            if head:
                on_token(head)
            gate["held"] = text[len(head):]
            return
        held = gate.pop("held", "")
        on_token(held + text)

    system = DESIGNER_SYSTEM + camera_guide()
    if session.profile:
        prof = {k: session.profile.get(k) for k in ("name", "summary", "sector", "services", "locations", "customers", "team_hint",
                                                   "channels", "tech", "tone", "opportunities") if session.profile.get(k)}
        system += ("\n\nYou already studied the owner's public links (" + ", ".join(session.links[:3]) + "). Company profile - treat as known facts, "
                   "do not ask for them again, reference them naturally:\n" + json.dumps(prof, ensure_ascii=False)[:3500])
    if session.blueprint:
        system += "\n\nCurrent blueprint (update it, keep what still holds):\n" + json.dumps(session.blueprint, ensure_ascii=False)
    msgs = [prov.user_message(m["content"]) if m["role"] == "user" else {"role": "assistant", "content": m["content"]}
            for m in session.messages]
    raw = ""
    err = ""
    for attempt in range(2):
        try:
            r = prov.chat(system, msgs, [], model or "", on_token=tok)
            raw = r.text or ""
            break
        except Exception as exc:
            err = f"{type(exc).__name__}: {str(exc)[:160]}"
            time.sleep(2.0)
    if not raw and err:
        if "free-models-per-day" in err or ("429" in err and ":free" in (model or "")):
            return ("Today's free model allowance on this account is used up (it resets at midnight UTC). You can switch "
                    "this conversation to the paid model, about 3p a message, or come back after the reset. The sketch so far is kept."
                    + chr(10) + '<atlas-design>{"suggestions": ["' + SWITCH_TO_PAID + '", "I will come back later"], "ready": false, "blueprint": null}</atlas-design>')
        return (f"The model did not answer ({err}). Say that again in a moment — the sketch so far is kept."
                + chr(10) + '<atlas-design>{"suggestions": ["Continue"], "ready": false, "blueprint": null}</atlas-design>')
    if not raw.strip():                                    # some free models return content="" - try the fallback model once
        fallback = (getattr(prov, "cfg", {}) or {}).get("fallback_model") or ""
        if fallback and fallback != model:
            try:
                r = prov.chat(system, msgs, [], fallback, on_token=tok)
                raw = r.text or ""
            except Exception as exc:
                err = f"{type(exc).__name__}: {str(exc)[:160]}"
    if not raw.strip():
        return ("I did not get a reply from the model that time - nothing was lost. Say that again, or switch to the paid model."
                + chr(10) + '<atlas-design>{"suggestions": ["Say it again", "' + SWITCH_TO_PAID + '"], "ready": false, "blueprint": null}</atlas-design>')
    # repair pass: the prose came back without a usable machine block -> ask for the block alone (not streamed).
    # Only once the owner has actually said something about the business (a greeting or "what did you understand"
    # must not conjure a team).
    _, data = split_reply(raw)
    said = " ".join(m["content"] for m in session.messages if m["role"] == "user" and isinstance(m["content"], str))
    substantive = bool(session.blueprint) or bool(session.profile) or len(said.split()) >= 12
    if substantive and not (data and isinstance(data.get("blueprint"), dict)):
        try:
            fix = prov.chat(system, msgs + [{"role": "assistant", "content": raw},
                                           prov.user_message("Output ONLY the <atlas-design>{...}</atlas-design> block, nothing else. "
                                                             "Include your best FIRST-DRAFT blueprint for what has been said so far (make reasonable "
                                                             "assumptions; the owner can adjust it on the canvas). Keep the same suggestions.")],
                            [], model or "")
            _, data2 = split_reply(fix.text or "")
            if data2:
                raw = split_reply(raw)[0] + chr(10) + "<atlas-design>" + json.dumps(data2, ensure_ascii=False) + "</atlas-design>"
        except Exception:
            pass
    return raw


# ---------------------------------------------------------------------------- demo designer (no model)
_DEMO_KINDS = [
    ("sales", ("enquir", "lead", "quote", "book", "customer", "sales", "slow", "respond", "reply"), "sales_desk"),
    ("proposal", ("proposal", "pitch", "tender", "consult", "advis", "scope", "brief"), "consultancy"),
    ("inbox", ("inbox", "email", "triage", "mail", "support", "ticket"), "sales_desk"),
    ("orders", ("order", "shop", "store", "ship", "deliver", "ecom", "product"), "ecommerce"),
]


def _demo_blueprint(kind: str, biz_hint: str, turn: int) -> dict[str, Any]:
    base = {
        "sales": {
            "agents": [
                {"id": "research", "name": "Researcher", "role": "Lead & company research", "goal": "Find who the enquirer is, their company, size and public signals. Cite sources. At most 2 pages fetched.", "tools": ["web_fetch", "read_file", "list_files"]},
                {"id": "writer", "name": "Reply writer", "role": "First-response drafting", "goal": "Draft a short, specific first reply in the business tone with one clear next step. No prices, no placeholders.", "tools": ["read_file", "list_files"], "strong": True},
                {"id": "crm", "name": "CRM keeper", "role": "Pipeline hygiene", "goal": "Set the stage, log a one-line summary and a dated next action for every lead.", "tools": ["read_file", "list_files"]},
                {"id": "qa", "name": "Quality reviewer", "role": "Outbound quality gate", "goal": "Check every outbound draft against policy and tone; return fixes, not opinions.", "tools": ["read_file"], "strong": True},
            ],
            "workflows": [{"id": "inbound_lead", "name": "Inbound lead → researched reply", "trigger": {"kind": "webhook", "detail": "Website form / Zapier POST"}, "steps": ["research", "writer", "qa", "crm"]},
                          {"id": "followups", "name": "3-day follow-up chaser", "trigger": {"kind": "schedule", "detail": "Daily 08:30"}, "steps": ["writer", "qa"]}],
            "connectors": [{"kind": "webhook", "name": "Website form", "purpose": "New enquiries in", "required": True},
                           {"kind": "smtp", "name": "Email sending", "purpose": "Approved replies out", "required": True},
                           {"kind": "imap", "name": "Inbox", "purpose": "Replies and new email enquiries", "required": False}],
            "policy": {"no_money_figures": True, "max_words": 180, "banned_phrases": ["guaranteed", "best price"]},
        },
        "proposal": {
            "agents": [
                {"id": "research", "name": "Researcher", "role": "Client & market brief", "goal": "Build a one-page brief on the client, sector and the problem stated.", "tools": ["web_fetch", "read_file", "list_files"]},
                {"id": "strategy", "name": "Strategist", "role": "Approach & roadmap", "goal": "Recommend an approach with phases, deliverables and risks.", "tools": ["read_file"], "strong": True},
                {"id": "finance", "name": "Pricing analyst", "role": "Effort & pricing", "goal": "Estimate effort and produce fixed-fee and day-rate options.", "tools": ["run_python", "read_file"]},
                {"id": "proposal", "name": "Proposal writer", "role": "Client-facing proposal", "goal": "Write the complete proposal in the house tone, ready to send.", "tools": ["save_deliverable", "read_file"], "strong": True},
                {"id": "qa", "name": "Reviewer", "role": "Proposal review", "goal": "Review for clarity, promises and pricing consistency; return concrete fixes.", "tools": ["read_file"], "strong": True},
            ],
            "workflows": [{"id": "new_proposal", "name": "Brief → proposal", "trigger": {"kind": "manual", "detail": "Owner pastes the brief"}, "steps": ["research", "strategy", "finance", "proposal", "qa"]}],
            "connectors": [{"kind": "smtp", "name": "Email sending", "purpose": "Send approved proposals", "required": True},
                           {"kind": "mcp", "name": "Google Drive", "purpose": "Past proposals as reference", "required": False}],
            "policy": {"no_money_figures": False, "max_words": 900, "banned_phrases": ["guarantee"]},
        },
        "inbox": {
            "agents": [
                {"id": "triage", "name": "Triage agent", "role": "Classify & prioritise email", "goal": "Label each email (lead, support, invoice, spam), set urgency, extract the ask.", "tools": ["read_file"]},
                {"id": "writer", "name": "Reply drafter", "role": "Draft replies", "goal": "Draft replies for anything routine in the house tone; escalate the rest.", "tools": ["read_file"], "strong": True},
                {"id": "crm", "name": "CRM keeper", "role": "Log & next action", "goal": "Log every thread against the contact with a next action.", "tools": ["read_file"]},
            ],
            "workflows": [{"id": "inbox_triage", "name": "Inbox triage", "trigger": {"kind": "inbox", "detail": "Every 2 minutes, unread mail"}, "steps": ["triage", "writer", "crm"]}],
            "connectors": [{"kind": "imap", "name": "Inbox", "purpose": "Read unread mail", "required": True},
                           {"kind": "smtp", "name": "Email sending", "purpose": "Send approved replies", "required": True}],
            "policy": {"no_money_figures": True, "max_words": 160, "banned_phrases": []},
        },
        "orders": {
            "agents": [
                {"id": "orders", "name": "Order agent", "role": "Order status lookups", "goal": "Look up orders and shipments via the store API and summarise status.", "tools": ["read_file", "run_python"]},
                {"id": "writer", "name": "Customer writer", "role": "Customer updates", "goal": "Write short, warm status updates and delay apologies with a clear next step.", "tools": ["read_file"], "strong": True},
                {"id": "ops", "name": "Ops checker", "role": "Exceptions & escalation", "goal": "Flag stuck orders, refunds and anything needing a human.", "tools": ["read_file", "run_python"]},
            ],
            "workflows": [{"id": "order_update", "name": "Customer asks about an order", "trigger": {"kind": "inbox", "detail": "support@ inbox"}, "steps": ["orders", "writer"]},
                          {"id": "stuck_orders", "name": "Daily stuck-order sweep", "trigger": {"kind": "schedule", "detail": "Daily 07:00"}, "steps": ["orders", "ops", "writer"]}],
            "connectors": [{"kind": "http", "name": "Store API (Shopify)", "purpose": "Orders, shipments, refunds", "required": True},
                           {"kind": "imap", "name": "Support inbox", "purpose": "Customer questions in", "required": True},
                           {"kind": "smtp", "name": "Email sending", "purpose": "Updates out", "required": True}],
            "policy": {"no_money_figures": False, "max_words": 150, "banned_phrases": ["guaranteed"]},
        },
    }[kind]
    bp = json.loads(json.dumps(base))
    # grow with the conversation so the canvas animates in stages
    if turn == 1:
        bp["agents"] = bp["agents"][:2]
        bp["workflows"] = bp["workflows"][:1]
        bp["connectors"] = bp["connectors"][:1]
    elif turn == 2:
        bp["agents"] = bp["agents"][:3]
        bp["connectors"] = bp["connectors"][:2]
    bp["business"] = {"name": biz_hint} if biz_hint else {}
    return bp


def _demo_turn(session: DesignSession, text: str, on_token: Callable[[str], None] | None) -> str:
    low = text.lower()
    kind = getattr(session, "_demo_kind", None)
    if not kind:
        for k, words, _ in _DEMO_KINDS:
            if any(w in low for w in words):
                kind = k
                break
        kind = kind or "sales"
        session._demo_kind = kind  # type: ignore[attr-defined]
    name = getattr(session, "_demo_name", "")
    m = re.search(r"\b(?i:we are|we're|i run|i own|called|at)\s+([A-Z][\w&'\-]*(?: [A-Z][\w&'\-]*){0,3})", text)
    if m and not name:
        name = m.group(1).strip()
        session._demo_name = name  # type: ignore[attr-defined]
    turn = session.turn
    label = {"sales": "inbound enquiry desk", "proposal": "proposal desk", "inbox": "inbox triage desk", "orders": "order-care desk"}[kind]
    scripts = {
        1: (f"Understood — that points to an {label}. I have sketched the core: Atlas orchestrating, a researcher and a writer. "
            f"Every outbound message will wait for your approval, and the policy layer blocks prices and placeholders. "
            f"Who are your customers, and how do enquiries usually arrive — web form, email, phone?",
            ["Mostly through our website form", "By email to one inbox", "Phone and WhatsApp mostly", "A mix of all of these"]),
        2: ("Good. I have added a CRM keeper so every contact gets a stage and a dated next action, and wired the intake as the "
            "first trigger. What tone should the replies have, and is there anything the agents must never say or promise?",
            ["Warm and local, never pushy", "Formal and precise", "Never quote prices in writing", "No guarantees or timelines"]),
        3: ("Noted, added to policy. The team is complete: a quality reviewer now checks every draft before it reaches your "
            "approval queue, and a follow-up chaser runs on a schedule. Review the sketch — click any agent to adjust its role or "
            "tools — then press Approve & build. Next we connect your data live.",
            ["Approve & build", "Add a second workflow", "Rename an agent", "Explain the approval flow"]),
    }
    prose, sugg = scripts.get(turn, ("[Scripted preview] This deployment has no model key, so I can't actually respond to what you typed. "
                                     "The sketch shows a sample desk. For the real designer, run the desk with a live model "
                                     "(locally: AtlasDesk launcher; hosted: set OPENROUTER_API_KEY).",
                                     ["Approve & build the sample desk", "How do I go live?"]))
    bp = _demo_blueprint(kind, name, turn)
    cams = getattr(session, "_demo_cams", None)
    if cams is None and re.search(r"camera|cctv|footage|lobby|hotel|till|restaurant|kitchen", low):
        cams = _demo_cameras(low)
        session._demo_cams = cams  # type: ignore[attr-defined]
    if cams:
        bp["cameras"] = cams
        bp["agents"].append({"id": "watch_reporter", "name": "Camera reporter", "role": "Reads the camera journal",
                             "goal": "Answer questions and write the daily report from the camera journal, citing times.",
                             "tools": ["camera_ask", "camera_events", "save_deliverable"]})
        prose_note = f" I have added {len(cams)} camera{'s' if len(cams) != 1 else ''} with a detailed journal, and a reporter who reads it."
    else:
        prose_note = ""
    if turn >= 3 and "never" in low:
        bp["policy"]["banned_phrases"] = list(dict.fromkeys(bp["policy"]["banned_phrases"] + ["guarantee", "promise"]))
    prose += prose_note
    if on_token:
        for w in prose.split(" "):
            on_token(w + " ")
            time.sleep(0.018)
    return prose + "\n<atlas-design>" + json.dumps({"suggestions": sugg, "ready": turn >= 3, "blueprint": bp}) + "</atlas-design>"


def _demo_cameras(low: str) -> list[dict[str, Any]]:
    clips = sample_clips()
    if "hotel" in low or "lobby" in low:
        want = [n for n in clips if n.startswith("hotel-lobby")]
    elif "restaurant" in low or "kitchen" in low or "sushi" in low:
        want = [n for n in clips if n.startswith("restaurant")]
    else:
        want = [n for n in clips if n.startswith(("corner-store", "retail-store", "liquor"))]
    want = want or list(clips)[:2]
    out = []
    for n in want[:4]:
        out.append({"name": SAMPLE_CAMERA_NAMES.get(n, n[:32]),
                    "source": f"sample:{n}", "notes": SAMPLE_LABELS.get(n, n), "focus": "", "journal": True, "alerts": False,
                    "watch_for": "person"})
    return out if clips else [{"name": "front-door", "source": "", "notes": "", "focus": "", "journal": True,
                               "alerts": False, "watch_for": "person"}]


# ---------------------------------------------------------------------------- blueprint -> desk config
def _agent_prompt(a: dict[str, Any], biz: dict[str, Any]) -> str:
    lines = [f"You are {a['name']}, {a['role']} for {biz.get('name') or 'the business'}.",
             f"Your job: {a.get('goal') or a['role']}."]
    if biz.get("tone"):
        lines.append(f"House tone: {biz['tone']}")
    if biz.get("description"):
        lines.append(f"About the business: {biz['description']}")
    if a.get("instructions"):
        lines.append("Standing orders for this role:")
        lines.extend(f"- {x}" for x in a["instructions"])
    lines.append("Be specific and concise. Never invent facts about the client; say what you assumed. "
                 "Do not include prices, fees or placeholders like [name] in anything customer-facing unless the task supplies them.")
    return "\n".join(lines)


def blueprint_to_desk(bp: dict[str, Any], tier: str = "free") -> dict[str, Any]:
    """Turn an approved blueprint into {business, agents, workflows} for store.add_desk."""
    bp = normalise(bp) or {}
    biz_in = bp.get("business") or {}
    base = json.loads(json.dumps(T.CONSULTANCY["business"]))
    b = {**base, **biz_in}
    b["model"] = "custom"
    b["currency"] = b.get("currency") or "GBP"
    pol = bp.get("policy") or {}
    b["policy"] = {"no_money_figures": bool(pol.get("no_money_figures", True)), "max_words": int(pol.get("max_words") or 220),
                   "banned_phrases": list(pol.get("banned_phrases") or [])}
    roster = "\n".join(f"- {a['id']}: {a['name']} — {a['role']}" + (f" (leads: {', '.join(a['members'])})" if a.get("members") else "")
                       for a in bp.get("agents") or [] if (a.get("reports_to") or "atlas") == "atlas")
    atlas_extra = ("Specialists on this desk:\n" + roster + "\n\nYou lead this team: brief the specialists with delegate (leads run "
                   "their own members), run independent strands in parallel, review what comes back, then merge. Never do a "
                   "specialist's job yourself. Every customer-facing message goes through queue_action for owner "
                    "approval. Keep CRM up to date with crm_update.") if roster else ""
    agents = [T._atlas(atlas_extra)]
    from . import team as TM
    by_id = {a["id"]: a for a in bp.get("agents") or []}
    for a in bp.get("agents") or []:
        tools = list(a["tools"])
        if a.get("members"):                                   # a sub-team lead: needs delegate for its members
            tools = list(dict.fromkeys(TM.LEAD_TOOLS + tools))
        agents.append(T._agent(a["id"], a["name"], a["role"], TM.agent_prompt(a, b, by_id), tools=tools, color=a["color"]))
        agents[-1]["strong"] = bool(a.get("strong"))
        agents[-1]["engine"] = "atlas" if a.get("members") else (a.get("engine") or "atlas")
        agents[-1]["reports_to"] = a.get("reports_to") or "atlas"
        agents[-1]["members"] = list(a.get("members") or [])
        agents[-1]["instructions"] = list(a.get("instructions") or [])
        agents[-1]["goal"] = a.get("goal", "")
    strong_ids = {a["id"] for a in bp.get("agents") or [] if a.get("strong")}
    T.apply_tier(agents, tier)
    if tier == "best":
        for ag in agents:
            if ag["id"] in strong_ids:
                ag["model"] = T.STRONG_MODEL
    workflows = []
    for w in bp.get("workflows") or []:
        steps = []
        for i, sid in enumerate(w["steps"]):
            tmpl = "{task}" if i == 0 else "Task: {task}\n\nWork so far:\n{all}"
            steps.append({"agent": sid, "task": tmpl})
        if steps:
            workflows.append({"id": w["id"], "name": w["name"], "description": " -> ".join(w["steps"]),
                              "synthesize": True, "steps": steps, "trigger": w["trigger"]})
    return {"business": b, "agents": agents, "workflows": workflows, "blueprint": bp}
