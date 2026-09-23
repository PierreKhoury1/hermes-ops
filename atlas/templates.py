"""Business-model templates: business profile + agent roster + workflows.

Any JSON file dropped into templates/ with the same shape is picked up too.
"""
from __future__ import annotations

import os

import json
from typing import Any

from .config import TEMPLATES_DIR

_BASE_ATLAS_PROMPT = """You are Atlas, the orchestrator and engagement lead for this business.

You receive a task from the owner. You decide how to get it done using the specialist agents available to you.
Work like a strong principal: understand what the deliverable actually needs to be, delegate focused sub-tasks
with all the context the specialist needs (they cannot see this conversation), review what comes back, push back
or re-delegate if quality is short, and combine the results. Delegate in parallel when sub-tasks are independent.

Save every client-ready deliverable with save_deliverable (markdown). When the work is complete, call finish
with a concise summary of what was produced and any open questions for the owner."""

_SPECIALIST_SUFFIX = """

You receive a task from Atlas (the orchestrator). Produce your deliverable directly and completely in your reply —
it will be passed back verbatim. State assumptions explicitly. If information is genuinely missing, say what you assumed."""


def _agent(id_: str, name: str, role: str, prompt: str, tools: list[str] | None = None,
           model: str = "", provider: str = "", color: str = "") -> dict[str, Any]:
    return {
        "id": id_, "name": name, "role": role, "enabled": True,
        "provider": provider, "model": model,
        "tools": tools or ["read_file", "list_files"],
        "system_prompt": prompt.strip() + ("" if id_ == "atlas" else _SPECIALIST_SUFFIX),
        "color": color,
    }


def _atlas(extra: str = "") -> dict[str, Any]:
    return _agent("atlas", "Atlas", "Orchestrator / Engagement Lead",
                  _BASE_ATLAS_PROMPT + ("\n\n" + extra if extra else ""),
                  tools=["delegate", "list_agents", "save_deliverable", "read_file", "list_files",
                         "crm_lookup", "crm_update", "queue_action", "list_connectors", "http_request", "schedule_task", "calendar_free_slots", "calendar_book",
                         "mcp", "run_python", "remember", "recall", "generate_media", "camera_look", "camera_events", "camera_ask"],
                  color="#0b5fcb")


CONSULTANCY: dict[str, Any] = {
    "business": {
        "name": "Your Consultancy",
        "model": "consultancy",
        "tagline": "Advisory and delivery for growing businesses",
        "description": "Boutique consultancy delivering strategy, operations and digital advisory to SMEs.",
        "services": ["Strategy & growth advisory", "Operations improvement", "Digital transformation",
                     "Market entry research", "Fractional leadership"],
        "target_clients": "Owner-led SMEs and scale-ups, 10-250 staff",
        "tone": "Confident, plain-spoken, evidence-led. No jargon.",
        "currency": "GBP",
        "pricing_notes": "Day rate 900-1,400; fixed-fee projects; retainers from 3,000/mo.",
        "extra_context": "",
    },
    "agents": [
        _atlas(),
        _agent("research", "Research Analyst", "Market & client research",
               "You are a research analyst at a consultancy. You build fast, structured briefs: market size and trends, competitor landscape, client background, risks and open questions. Cite what you know vs what is inferred.",
               tools=["read_file", "list_files", "web_fetch", "browse", "run_python"], color="#7c3aed"),
        _agent("strategy", "Strategy Consultant", "Strategy & recommendations",
               "You are a senior strategy consultant. You turn research and a client situation into a clear point of view: diagnosis, options, recommendation, roadmap with phases, and the risks. Use frameworks only when they sharpen the answer.",
               color="#db2777"),
        _agent("finance", "Pricing & Finance Analyst", "Pricing, budgets, business cases",
               "You are a pricing and financial analyst at a consultancy. You build project pricing, effort estimates, budgets, ROI / business-case models and payment schedules. Show the arithmetic and assumptions in tables.",
               color="#1f9d63"),
        _agent("proposal", "Proposal Writer", "Proposals, pitches, SOWs",
               "You are a proposal writer at a consultancy. You write winning, client-facing proposals and statements of work: situation, objectives, approach, deliverables, timeline, team, pricing, terms, next steps. Structured markdown, client-ready.",
               color="#dc2626"),
        _agent("comms", "Client Communications", "Emails, follow-ups, updates",
               "You handle client communications for a consultancy: outreach emails, follow-ups, meeting summaries, status updates. Short, warm, specific, with a clear ask.",
               color="#ea580c"),
        _agent("qa", "Quality Reviewer", "Partner-level review",
               "You are the reviewing partner. Critique deliverables hard: logic gaps, unsupported claims, weak structure, missing numbers, tone mismatches. Return a prioritised list of fixes and, where quick, the corrected text.",
               color="#b45309"),
    ],
    "workflows": [
        {
            "id": "new_client_proposal", "name": "New client proposal",
            "description": "Research -> strategy -> pricing -> proposal -> review -> final",
            "synthesize": True,
            "steps": [
                {"agent": "research", "task": "Build a research brief for this engagement:\n{task}"},
                {"agent": "strategy", "task": "Using this research, set out the recommended approach and roadmap.\n\nTask: {task}\n\nResearch:\n{previous}"},
                {"agent": "finance", "task": "Price this engagement (effort, day rates, fixed fee options, payment schedule).\n\nTask: {task}\n\nApproach:\n{previous}"},
                {"agent": "proposal", "task": "Write the full client-facing proposal.\n\nTask: {task}\n\nAll prior work:\n{all}"},
                {"agent": "qa", "task": "Review this proposal and return fixes.\n\n{previous}"},
            ],
        },
        {
            "id": "discovery_brief", "name": "Discovery brief",
            "description": "Research + strategic point of view before a first meeting",
            "synthesize": False,
            "steps": [
                {"agent": "research", "task": "Prepare a pre-meeting research brief:\n{task}"},
                {"agent": "strategy", "task": "Draft our point of view and 5 sharp discovery questions.\n\nTask: {task}\n\nResearch:\n{previous}"},
            ],
        },
        {
            "id": "client_email", "name": "Client email",
            "description": "Draft + review a client email",
            "synthesize": False,
            "steps": [
                {"agent": "comms", "task": "Draft this client email:\n{task}"},
                {"agent": "qa", "task": "Review and return the improved final email.\n\n{previous}"},
            ],
        },
    ],
}


AGENCY: dict[str, Any] = {
    "business": {
        "name": "Your Agency", "model": "agency",
        "tagline": "Brand, content and performance marketing",
        "description": "Marketing agency delivering brand, content, paid media and web for SMEs.",
        "services": ["Brand & positioning", "Content & social", "Paid media", "Web design & build", "SEO"],
        "target_clients": "Consumer and B2B brands, 1-50M revenue",
        "tone": "Energetic, sharp, creative but commercially grounded.",
        "currency": "GBP", "pricing_notes": "Retainers 2,500-15,000/mo; projects fixed-fee.",
        "extra_context": "",
    },
    "agents": [
        _atlas(),
        _agent("research", "Audience & Market Research", "Audience, competitor, channel research",
               "You research audiences, competitors and channels for a marketing agency. Output structured briefs with personas, positioning gaps and channel opportunities.", tools=["read_file", "list_files", "web_fetch", "browse"], color="#7c3aed"),
        _agent("creative", "Creative Director", "Concepts, copy, campaign ideas",
               "You are a creative director. You generate campaign concepts, taglines, hooks and copy across channels, with rationale tied to the audience.", color="#db2777"),
        _agent("media", "Media Planner", "Channel plans and budgets",
               "You are a performance media planner. You build channel mixes, budget splits, KPIs and forecast ranges with assumptions shown.", color="#1f9d63"),
        _agent("account", "Account Manager", "Client-facing docs and comms",
               "You are an account manager. You write client-facing proposals, scopes, status reports and emails. Clear, friendly, commercial.", color="#ea580c"),
        _agent("qa", "Quality Reviewer", "Review", "You are the agency's reviewing partner. Critique for strategy, creative strength, feasibility and client fit. Return prioritised fixes.", color="#b45309"),
    ],
    "workflows": [
        {"id": "campaign_pitch", "name": "Campaign pitch", "description": "Research -> creative -> media plan -> pitch -> review", "synthesize": True,
         "steps": [
             {"agent": "research", "task": "Research the audience, competitors and channels for:\n{task}"},
             {"agent": "creative", "task": "Develop 3 campaign concepts with copy.\n\nBrief: {task}\n\nResearch:\n{previous}"},
             {"agent": "media", "task": "Build the channel plan and budget.\n\nBrief: {task}\n\nConcepts:\n{previous}"},
             {"agent": "account", "task": "Write the client pitch document.\n\nBrief: {task}\n\nAll work:\n{all}"},
             {"agent": "qa", "task": "Review and list fixes.\n\n{previous}"},
         ]},
    ],
}


SAAS: dict[str, Any] = {
    "business": {
        "name": "Your SaaS", "model": "saas",
        "tagline": "B2B software",
        "description": "Early-stage B2B SaaS company.",
        "services": ["Product", "Sales & GTM", "Customer success", "Fundraising"],
        "target_clients": "Mid-market B2B companies",
        "tone": "Direct, data-driven, product-led.",
        "currency": "USD", "pricing_notes": "Tiered subscriptions; annual contracts preferred.",
        "extra_context": "",
    },
    "agents": [
        _atlas(),
        _agent("research", "Market Analyst", "Market, competitor, ICP research",
               "You research markets, competitors and ideal customer profiles for a B2B SaaS. Structured briefs with sources vs inferences flagged.", tools=["read_file", "list_files", "web_fetch", "browse"], color="#7c3aed"),
        _agent("product", "Product Manager", "Specs, roadmaps, PRDs",
               "You are a product manager. You write PRDs, user stories, prioritised roadmaps and success metrics.", color="#db2777"),
        _agent("gtm", "GTM Lead", "Positioning, pricing, sales playbooks",
               "You lead go-to-market. You produce positioning, pricing/packaging, outbound sequences and sales playbooks with clear reasoning.", color="#1f9d63"),
        _agent("finance", "Finance", "Models, forecasts, investor material",
               "You are a startup finance lead. You build revenue models, unit economics, burn/runway and investor-facing numbers with assumptions in tables.", color="#ea580c"),
        _agent("qa", "Reviewer", "Review", "You are a sceptical operator/investor reviewer. Find weak logic, missing numbers and unrealistic assumptions. Prioritised fixes.", color="#b45309"),
    ],
    "workflows": [
        {"id": "gtm_plan", "name": "GTM plan", "description": "Research -> positioning/pricing -> model -> review", "synthesize": True,
         "steps": [
             {"agent": "research", "task": "Research the market and ICP for:\n{task}"},
             {"agent": "gtm", "task": "Produce positioning, pricing and a 90-day GTM plan.\n\nTask: {task}\n\nResearch:\n{previous}"},
             {"agent": "finance", "task": "Model the revenue and unit economics for this plan.\n\nTask: {task}\n\nPlan:\n{previous}"},
             {"agent": "qa", "task": "Review everything and list fixes.\n\n{all}"},
         ]},
    ],
}


ECOMMERCE: dict[str, Any] = {
    "business": {
        "name": "Your Store", "model": "ecommerce",
        "tagline": "Online retail brand",
        "description": "Direct-to-consumer e-commerce brand.",
        "services": ["Product sourcing", "Store & merchandising", "Marketing & CRM", "Operations & fulfilment"],
        "target_clients": "Online consumers, 18-45",
        "tone": "Friendly, punchy, brand-led.",
        "currency": "USD", "pricing_notes": "Target 60%+ gross margin; free shipping over threshold.",
        "extra_context": "",
    },
    "agents": [
        _atlas(),
        _agent("research", "Market & Product Research", "Trends, competitors, sourcing",
               "You research product trends, competitors, pricing and suppliers for an e-commerce brand.", tools=["read_file", "list_files", "web_fetch", "browse"], color="#7c3aed"),
        _agent("merch", "Merchandising & Copy", "Listings, collections, copy",
               "You write product listings, collection copy, and merchandising plans that convert. Include SEO titles and bullet benefits.", color="#db2777"),
        _agent("marketing", "Growth Marketer", "Ads, email, CRM",
               "You plan and write ad creative, email flows and promotions with budgets and KPIs.", color="#1f9d63"),
        _agent("ops", "Operations", "Inventory, fulfilment, margins",
               "You plan inventory, fulfilment, landed cost and margin models with the arithmetic shown.", color="#ea580c"),
        _agent("qa", "Reviewer", "Review", "You review for brand consistency, conversion best practice and margin sanity. Prioritised fixes.", color="#b45309"),
    ],
    "workflows": [
        {"id": "product_launch", "name": "Product launch", "description": "Research -> listing -> marketing -> ops -> review", "synthesize": True,
         "steps": [
             {"agent": "research", "task": "Research the market and competitors for:\n{task}"},
             {"agent": "merch", "task": "Write the product listing and launch collection copy.\n\nTask: {task}\n\nResearch:\n{previous}"},
             {"agent": "marketing", "task": "Build the launch marketing plan (ads, email, promo).\n\nTask: {task}\n\nListing:\n{previous}"},
             {"agent": "ops", "task": "Plan inventory, landed cost and margin.\n\nTask: {task}\n\nContext:\n{all}"},
             {"agent": "qa", "task": "Review all of it and list fixes.\n\n{all}"},
         ]},
    ],
}


SALES_DESK: dict[str, Any] = {
    "business": {
        "name": "Acme Estates", "model": "sales_desk",
        "tagline": "Independent estate & lettings agency — AI Sales Desk",
        "description": "Estate and lettings agency in South London. The AI Sales Desk handles inbound leads: research, personalised outreach, CRM updates and follow-ups. Outbound messages always wait for owner approval.",
        "services": ["Sales valuations", "Lettings & property management", "Landlord onboarding", "Buyer matching"],
        "target_clients": "Landlords with 1–10 properties, first-time sellers, relocating buyers in SE London",
        "tone": "Warm, local, specific. Short emails. Never pushy. Always one clear next step.",
        "currency": "GBP", "pricing_notes": "Sales fee 1.2% + VAT; lettings 8% managed / 5% let-only.",
        "extra_context": "Approval rules: any email, WhatsApp or SMS to a lead must go through queue_action. Never promise a valuation figure in writing before a visit.",
        "sender_name": "Sam Reid, Acme Estates",
        "availability": "Weekdays 9am-6pm and Saturday mornings; offer a morning or afternoon, never a specific time slot (the owner books the exact time).",
    },
    "agents": [
        _agent("atlas", "Atlas", "Desk orchestrator",
               _BASE_ATLAS_PROMPT + "\n\nThis is an AI Sales Desk. For every lead: check the CRM (crm_lookup), get research and a drafted outreach from specialists, update the CRM (crm_update) with notes and next action — keep stage at New (it moves to Contacted automatically when the owner approves a send; only set Qualified/Proposal/Won/Lost when the lead's replies justify it), then queue the outreach with queue_action — never send directly. Finish with a summary for the owner.",
               tools=["delegate", "list_agents", "crm_lookup", "crm_update", "queue_action", "save_deliverable", "read_file", "list_files",
                      "list_connectors", "http_request", "schedule_task", "mcp", "run_python", "remember", "recall", "generate_media"],
               color="#0b5fcb"),
        _agent("research", "Lead Researcher", "Researches the lead, property and context",
               "You are a lead researcher at an estate agency. Given a lead, produce a short brief: who they are, likely intent (sell / let / buy), property context, timing signals, risks, and 3 questions to ask on the first call. Flag what is inferred vs known. Never invent market figures: quote a number only if you fetched it (web_fetch) and name the source; otherwise describe the market qualitatively. Fetch at most 2 pages; if a page fails, move on rather than retrying.",
               tools=["read_file", "list_files", "web_fetch", "browse", "run_python"], color="#7c3aed"),
        _agent("writer", "Outreach Writer", "Drafts personalised first-touch messages",
               "You are an outreach writer for an estate agency. Write a short, warm, specific first email to the lead (under 120 words) with one clear next step (a call or visit). Plain text only — no markdown, no asterisks, no bullet symbols. Sign off with the business's sender name; never leave a placeholder. Offer a morning or afternoon, not an exact time. Start your reply with 'Subject: ...' on the first line, then a blank line, then the body.",
               color="#db2777"),
        _agent("crm", "CRM Assistant", "Keeps contact records clean",
               "You maintain the agency CRM. Given lead details and research, state the correct stage (New until the owner has approved a first send), notes and next action for this contact in one short paragraph.",
               tools=["crm_lookup", "crm_update"], color="#1f9d63"),
        _agent("qa", "Quality Reviewer", "Checks outreach before it is queued",
               "You review outreach drafts for an estate agency: accuracy, tone, no written valuation promises, one clear ask, plain text (no markdown), real sign-off (no placeholders), no invented specific time slots. Return either 'APPROVED' plus tiny fixes, or the corrected text.",
               color="#b45309"),
    ],
    "workflows": [
        {"id": "new_lead", "name": "New lead → outreach", "description": "Research → draft → QA → Atlas queues for approval", "synthesize": True,
         "steps": [
             {"agent": "research", "task": "Research this lead:\n{task}"},
             {"agent": "writer", "task": "Draft the first-touch email.\n\nLead:\n{task}\n\nResearch:\n{previous}"},
             {"agent": "qa", "task": "Review this draft.\n\n{previous}"},
         ]},
    ],
}


SITE_WATCH: dict[str, Any] = {
    "business": {
        "name": "Your Site",
        "model": "site_watch",
        "tagline": "Shop, clinic or yard watched by the desk",
        "description": "A physical site (shop floor, reception, yard, warehouse door) with cameras and sensors connected to the desk. "
                       "The desk watches, logs what happens, alerts the right person and answers questions about what the cameras saw.",
        "services": ["After-hours presence alerts", "Queue and footfall watch", "Delivery and vehicle log",
                     "Shelf / display checks", "Daily site digest", "Answer questions about footage"],
        "target_clients": "Owner and site staff",
        "tone": "Calm, factual, specific. Times, counts, camera names. No drama, no speculation.",
        "currency": "GBP",
        "pricing_notes": "",
        "extra_context": "Staff are usually on site 08:00-19:00. Deliveries come to the rear door. Anything after 20:00 is unusual.",
        "sender_name": "Atlas, Site desk",
        "availability": "",
    },
    "agents": [
        _agent("atlas", "Atlas", "Site desk orchestrator",
               _BASE_ATLAS_PROMPT + "\n\nThis is a Site Watch desk: cameras and sensors feed you events. For every camera alert: look again if useful (camera_look with a precise question), check what happened before (camera_events), decide severity (routine / worth telling staff / urgent for the owner), log a one-line incident note with remember(key='incident YYYY-MM-DD HH:MM', ...), and if someone should be told, queue a short message with queue_action (kind=whatsapp or sms or email) — never send directly. For questions about footage, answer from camera_events with times and counts; say plainly when the log has nothing. Routine events (staff during hours, expected deliveries) get logged only.",
               tools=["delegate", "list_agents", "camera_look", "camera_events", "camera_ask", "queue_action", "crm_lookup", "crm_update", "save_deliverable", "read_file", "list_files",
                      "list_connectors", "http_request", "schedule_task", "mcp", "run_python", "remember", "recall"],
               color="#0b5fcb"),
        _agent("watcher", "Vision Analyst", "Interprets camera frames and event history",
               "You are the vision analyst for a physical site. Given a camera event (counts, motion, time, the analyst's answer), decide what most likely happened and how sure you are. Use camera_look with one precise question when a second look would change the decision; use camera_events to compare with the pattern for that camera and time. Output: what happened (1-2 lines), confidence (low/medium/high), severity (routine / notify staff / urgent), evidence (times, counts). Never identify individuals; describe clothing/vehicle only when it matters operationally.",
               tools=["camera_look", "camera_events", "camera_ask", "recall", "read_file", "list_files"], color="#7c3aed"),
        _agent("comms", "Site Comms", "Drafts alerts and digests for staff and owner",
               "You write short operational messages for site staff and the owner: what was seen, where, when, and the one thing to do (check, ignore, call). Under 60 words for alerts, plain text, no markdown. Daily digests: bullet-free plain lines grouped by camera with counts and notable times. Sign off with the sender name.",
               color="#db2777"),
    ],
    "workflows": [
        {"id": "camera_alert", "name": "Camera alert → assess → notify", "description": "Vision analyst assesses, comms drafts, Atlas queues for approval", "synthesize": True,
         "steps": [
             {"agent": "watcher", "task": "Assess this camera event:\n{task}"},
             {"agent": "comms", "task": "Draft the alert message if severity is notify/urgent, otherwise reply NO MESSAGE.\n\nEvent:\n{task}\n\nAssessment:\n{previous}"},
         ]},
        {"id": "daily_digest", "name": "Daily site digest", "description": "Summarise the last 24h of camera events for the owner", "synthesize": True,
         "steps": [
             {"agent": "watcher", "task": "Summarise the last 24 hours of camera events (camera_events hours=24): busiest periods, after-hours activity, deliveries, anomalies.\n{task}"},
             {"agent": "comms", "task": "Write the owner's daily site digest from this summary.\n\n{previous}"},
         ]},
    ],
}



BLANK: dict[str, Any] = {
    "business": {
        "name": "My business", "model": "blank", "tagline": "", "description": "",
        "services": [], "target_clients": "",
        "tone": "Clear, warm, specific. Short messages with one next step.",
        "currency": "GBP", "pricing_notes": "", "extra_context": "",
    },
    "agents": [
        _atlas("You currently have no specialists. Do the work yourself, or - if the task clearly needs a team - "
               "use assemble_team to create focused specialists for this run. Anything customer-facing goes through "
               "queue_action so the owner can approve it."),
    ],
    "workflows": [],
}

BUILTIN: dict[str, dict[str, Any]] = {
    "blank": BLANK,
    "sales_desk": SALES_DESK,
    "site_watch": SITE_WATCH,
    "consultancy": CONSULTANCY,
    "agency": AGENCY,
    "saas": SAAS,
    "ecommerce": ECOMMERCE,
}


def names() -> list[str]:
    out = list(BUILTIN.keys())
    for p in sorted(TEMPLATES_DIR.glob("*.json")):
        if p.stem not in out:
            out.append(p.stem)
    return out


def get(name: str) -> dict[str, Any]:
    p = TEMPLATES_DIR / f"{name}.json"
    if p.exists():
        with p.open("r", encoding="utf-8") as f:
            return json.load(f)
    return json.loads(json.dumps(BUILTIN.get(name, CONSULTANCY)))


def export_current(name: str, business: dict, agents: list, workflows: list) -> None:
    """Save the current setup as a reusable template."""
    with (TEMPLATES_DIR / f"{name}.json").open("w", encoding="utf-8") as f:
        json.dump({"business": business, "agents": agents, "workflows": workflows}, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------- desk catalogue (onboarding)
DESK_TYPES: list[dict[str, Any]] = [
    {"id": "blank", "label": "Blank desk", "tagline": "Start from scratch: just Atlas. Add agents, cameras and integrations as you go.",
     "does": ["Atlas alone on any task", "Add specialists yourself", "Or ask Atlas to design the team"], "for": "Anyone who wants to build it up piece by piece"},
    {"id": "sales_desk", "label": "Sales desk", "tagline": "Inbound leads answered, researched, drafted, CRM kept clean.",
     "does": ["Research every lead", "Personalised first reply", "CRM stage + next action", "Follow-ups"], "for": "Estate agents, clinics, trades, B2B services"},
    {"id": "site_watch", "label": "Site watch desk", "tagline": "Cameras and sensors watched, incidents logged, the right person told, questions answered over footage.",
     "does": ["After-hours presence alerts", "Queue / footfall / delivery log", "Ask the cameras anything", "Daily site digest"], "for": "Shops, clinics, yards, warehouses, restaurants"},
    {"id": "consultancy", "label": "Consultancy desk", "tagline": "Briefs, strategy, pricing and client-ready proposals.",
     "does": ["Research brief", "Recommendation + roadmap", "Pricing", "Proposal reviewed by QA"], "for": "Consultancies, advisors, freelancers"},
    {"id": "agency", "label": "Agency desk", "tagline": "Campaign pitches: research, creative, media plan, review.",
     "does": ["Audience research", "Creative concepts", "Media plan", "Pitch deck copy"], "for": "Marketing, creative and PR agencies"},
    {"id": "saas", "label": "SaaS desk", "tagline": "Go-to-market plans, positioning, pricing, product notes.",
     "does": ["Market + competitor research", "Positioning + pricing", "Business model", "Review"], "for": "Software companies"},
    {"id": "ecommerce", "label": "E-commerce desk", "tagline": "Product launches: listings, marketing, operations.",
     "does": ["Product research", "Listing copy", "Launch marketing", "Ops checklist"], "for": "Online stores and brands"},
]

SAMPLE_LEADS: dict[str, list[dict[str, str]]] = {
    "sales_desk": [
        {"name": "Priya Raman", "company": "", "email": "priya.raman@example.com", "phone": "+44 7700 900101", "source": "website form",
         "notes": "Landlord with 3 flats in SE17, current agent underperforming. Wants a lettings management quote and a valuation for one flat."},
        {"name": "Tom Okafor", "company": "Okafor Property Ltd", "email": "tom@okaforproperty.example.com", "phone": "+44 7700 900102", "source": "Rightmove enquiry",
         "notes": "Thinking of selling a 2-bed in Walworth in the next 3 months. Asked for a rough price range."},
        {"name": "Hannah Weiss", "company": "", "email": "hannah.weiss@example.com", "phone": "", "source": "referral",
         "notes": "Relocating to London in October, needs a 1-bed rental near Elephant & Castle, budget ~£1,600."},
    ],
    "site_watch": [
        {"name": "Front door camera", "company": "", "email": "site.frontdoor@example.com", "phone": "", "source": "camera",
         "notes": "Camera front-door at 22:14: 2 people at the entrance, motion 0.31, shop closed since 19:00. Analyst: two adults standing close to the door, one holding the handle; shutter appears down."},
        {"name": "Rear yard camera", "company": "", "email": "site.yard@example.com", "phone": "", "source": "camera",
         "notes": "Camera rear-yard at 07:52: 1 truck, 1 person, motion 0.44. Analyst: a white box van reversed to the loading door, driver at the rear doors with a trolley."},
        {"name": "Counter camera", "company": "", "email": "site.counter@example.com", "phone": "", "source": "camera",
         "notes": "Camera counter at 12:40: 6 people, motion 0.12. Analyst: five customers queueing at the till, one staff member serving; second till unattended."},
    ],
    "consultancy": [
        {"name": "Dana Whitfield", "company": "Northgate Logistics", "email": "dana@northgate.example.com", "phone": "+44 7700 900201", "source": "LinkedIn",
         "notes": "Ops director. Warehouse picking errors up 30% since a new WMS went in; wants an outside view and a fixed-fee diagnostic."},
        {"name": "Samir Haddad", "company": "Haddad & Co Accountants", "email": "samir@haddadco.example.com", "phone": "", "source": "website form",
         "notes": "12-person accountancy practice. Asks whether AI can take over client onboarding and document chasing, and what it would cost."},
        {"name": "Lena Fischer", "company": "Fischer Dental Group", "email": "lena@fischerdental.example.com", "phone": "+44 7700 900203", "source": "referral",
         "notes": "Three clinics, wants a growth strategy for a fourth site and help pricing a membership plan."},
    ],
    "agency": [
        {"name": "Marcus Bell", "company": "Bell's Bakery", "email": "marcus@bellsbakery.example.com", "phone": "+44 7700 900301", "source": "Instagram DM",
         "notes": "Opening a second shop in Peckham in November. Wants a launch campaign on a 4k budget."},
        {"name": "Aisha Khan", "company": "Kite Yoga Studios", "email": "aisha@kiteyoga.example.com", "phone": "", "source": "website form",
         "notes": "Membership sign-ups flat for six months. Asks for a pitch: social + email + local partnerships."},
        {"name": "Oliver Grant", "company": "Grant Kitchens", "email": "oliver@grantkitchens.example.com", "phone": "+44 7700 900303", "source": "referral",
         "notes": "Premium fitted kitchens, wants brand refresh + a lead-gen campaign for spring."},
    ],
    "saas": [
        {"name": "Chloe Martin", "company": "Ledgerly", "email": "chloe@ledgerly.example.com", "phone": "", "source": "product signup",
         "notes": "Founder of a bookkeeping SaaS for cafes. Wants a go-to-market plan and pricing tiers before launch."},
        {"name": "Ravi Patel", "company": "ShiftBoard", "email": "ravi@shiftboard.example.com", "phone": "+44 7700 900402", "source": "website form",
         "notes": "Rota software for care homes. Asks for competitor positioning and an outbound plan."},
        {"name": "Emma Lund", "company": "Fieldnote", "email": "emma@fieldnote.example.com", "phone": "", "source": "referral",
         "notes": "Inspection app for surveyors. Needs a pricing model review and a churn-reduction plan."},
    ],
    "ecommerce": [
        {"name": "Jade Owens", "company": "Owens Ceramics", "email": "jade@owensceramics.example.com", "phone": "", "source": "Shopify contact form",
         "notes": "Launching a 12-piece tableware range in October. Wants listings, launch emails and an ads plan."},
        {"name": "Ben Carter", "company": "Trailhead Supply", "email": "ben@trailhead.example.com", "phone": "+44 7700 900502", "source": "website form",
         "notes": "Outdoor gear store; asks how to launch a new sleeping-bag line with a 2k budget."},
        {"name": "Nadia Ali", "company": "Nadia Ali Skincare", "email": "nadia@nadiaali.example.com", "phone": "", "source": "Instagram DM",
         "notes": "Wants product descriptions rewritten for 8 SKUs and a launch plan for a new serum."},
    ],
}

# model tiers: which agents get the strong model. Provider stays whatever the desk runs on (OpenRouter by default).
TIERS: dict[str, dict[str, Any]] = {
    "free":     {"label": "Free",     "strong": [], "hermes_model": os.environ.get("ATLAS_FREE_MODEL", "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"),
                 "note": "£0 per lead. Free MiniMax everywhere - inside the Hermes Agent runtime it still scored 3/3 on research, writing and data. Planning is weaker; drafts run long and get bounced by policy."},
    "frugal":   {"label": "Frugal",   "strong": [], "orchestrator": "anthropic/claude-haiku-4.5", "hermes_model": "anthropic/claude-haiku-4.5",
                 "note": "≈ 5-8p per lead. Claude Haiku orchestrates and powers Hermes Agent: fast, tidy, near-perfect on tool tasks."},
    "balanced": {"label": "Balanced", "strong": ["atlas"], "hermes_model": "anthropic/claude-haiku-4.5",
                 "note": "≈ 15-20p per lead. Claude Sonnet plans and reviews; Hermes Agent specialists on Haiku. The default for client work."},
    "best":     {"label": "Best",     "strong": ["atlas", "qa", "writer", "proposal", "strategy", "review"], "hermes_model": "anthropic/claude-sonnet-4.5",
                 "note": "≈ 35p per lead. Sonnet everywhere, including inside Hermes Agent. Only where the words are the product."},
}
STRONG_MODEL = "anthropic/claude-sonnet-4.5"
# The free-tier model. minimax/minimax-m3:free was withdrawn from OpenRouter on 15 Sep 2026; ling-3.0-flash-vl:free followed 15 Sep, withdrawn 24 Sep; nemotron-3-nano-omni:free takes
# images and tool calls (reasoning is switched off per request in providers.MODEL_EXTRAS). Override with ATLAS_FREE_MODEL.
FREE_MODEL = os.environ.get("ATLAS_FREE_MODEL", "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free")


def apply_tier(agents: list[dict[str, Any]], tier: str) -> None:
    t = TIERS.get(tier, TIERS["free"])
    strong = set(t["strong"])
    for a in agents:
        a["model"] = STRONG_MODEL if a["id"] in strong else FREE_MODEL
        if a["id"] == "atlas" and t.get("orchestrator"):
            a["model"] = t["orchestrator"]


def build_desk(template: str, answers: dict[str, Any]) -> dict[str, Any]:
    """Turn onboarding answers into the desk config stored in the DB (business overrides only)."""
    t = get(template)
    b = dict(t["business"])
    for k in ("name", "tagline", "description", "target_clients", "tone", "currency", "pricing_notes",
              "extra_context", "sender_name", "availability"):
        v = answers.get(k)
        if isinstance(v, str):
            v = v.strip()
        if v:
            b[k] = v
    if answers.get("services"):
        sv = answers["services"]
        b["services"] = [x.strip() for x in (sv.split(",") if isinstance(sv, str) else sv) if x.strip()]
    b["policy"] = {
        "no_money_figures": bool(answers.get("no_money_figures", template == "sales_desk")),
        "max_words": int(answers.get("max_words") or 220),
        "banned_phrases": [x.strip() for x in str(answers.get("banned_phrases") or "").split(",") if x.strip()],
    }
    b["model"] = template
    return {"business": b}
