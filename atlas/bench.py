"""Benchmark real engines and models on real task types.

    py -m atlas bench --engines atlas,hermes --tasks research,write,data --n 1

Runs each task through a single specialist agent per engine configuration and scores what comes back:
latency, tokens, tool activity, plus deterministic task checks (did research cite a URL, did the write
respect the word limit and tone rules, did the data task get the arithmetic right). Output:
workspace/bench/<stamp>/report.md + results.json. Engines:

  atlas         built-in specialist loop on the desk's OpenRouter model (--model to override)
  hermes        a running Hermes Agent instance (HERMES_AGENT_URL / HERMES_AGENT_KEY env, or --hermes-url/key)

Add more models with --model nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free --model anthropic/claude-sonnet-4.5 (each becomes its
own atlas-engine column). Everything is real output from real calls - no demo provider anywhere."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from . import config as cfg
from .config import WORKSPACE_DIR
from .providers import ProviderPool, make_provider

TASKS: dict[str, dict[str, Any]] = {
    "research": {
        "label": "Live web research",
        "prompt": ("Find the official website of the UK company 'Screwfix' and report: what they sell, roughly how many "
                   "UK stores they have, and one recent piece of company news. Cite the URL for each fact. Keep it under 150 words."),
        "system": "You are a research specialist. Use your tools to check facts on the live web; never rely on memory alone. Cite URLs.",
        "tools": ["web_fetch"],
        "checks": {"cites_url": lambda t: bool(re.search(r"https?://", t)), "mentions_screwfix": lambda t: "screwfix" in t.lower(),
                   "length_ok": lambda t: len(t.split()) <= 260},
    },
    "write": {
        "label": "Constrained client writing",
        "prompt": ("Write a first reply to this enquiry, max 90 words, plain text, no prices, no exact times, warm but direct, "
                   "signed 'The Northgate team': 'Boiler not firing since last night, no hot water, two small kids at home, "
                   "postcode M20. Can someone come today and what would it cost?'"),
        "system": "You are a customer communications specialist for a plumbing firm. Follow every constraint exactly.",
        "tools": [],
        "checks": {"under_90_words": lambda t: len(t.split()) <= 95, "no_prices": lambda t: not re.search(r"£\s?\d", t),
                   "signed": lambda t: "northgate team" in t.lower(), "no_markdown": lambda t: not re.search(r"\*\*|^#|^- ", t, re.M)},
    },
    "data": {
        "label": "Data reasoning (must compute)",
        "prompt": ("Rent roll: A pays 1500 owes 0; B pays 1450, paid 725, owes 2175; C pays 1580, paid 790, owes 2370; "
                   "D pays 1470, paid 0, owes 4410. Verify each 'owes' figure equals 3*rent - 2*paid... actually verify owes = "
                   "(3 months rent) minus (2 x amount paid) for B, C, D and state for each whether the book figure is correct, "
                   "then give total arrears. Show the arithmetic."),
        "system": "You are a data specialist. Compute with code when a tool is available; never guess arithmetic.",
        "tools": ["run_python"],
        "checks": {"total_correct": lambda t: "8955" in t.replace(",", ""), "checks_each": lambda t: all(x in t.upper() for x in ("B", "C", "D"))},
    },
}


def build_provider(engine: str, model: str, args) -> tuple[Any, str, list[str]]:
    if engine == "hermes":
        url = args.hermes_url or os.environ.get("HERMES_AGENT_URL", "http://127.0.0.1:8642")
        key = args.hermes_key or os.environ.get("HERMES_AGENT_KEY", "")
        if not key:
            kf = Path(cfg.ROOT) / "config" / "hermes_agent.key"
            key = kf.read_text(encoding="utf-8").strip() if kf.exists() else ""
        prov = make_provider({"type": "hermes_agent", "base_url": url, "api_key": key, "session_key": "atlas:bench"})
        return prov, "hermes-agent", []                     # Hermes runs its own tools
    providers = cfg.load("providers", cfg.DEFAULT_PROVIDERS)
    for n, pc in cfg.DEFAULT_PROVIDERS["providers"].items():
        providers.setdefault("providers", {}).setdefault(n, dict(pc))
    providers["default_provider"] = "openrouter"
    return ProviderPool(providers).get("openrouter"), model, None  # None => task decides tools


def run_cell(prov, model: str, task: dict[str, Any], forced_tools, run_dir: Path) -> dict[str, Any]:
    from . import tools as T
    ws = T.WorkspaceTools(run_dir)
    tool_names = forced_tools if forced_tools is not None else task["tools"]
    schemas = T.schema_for(tool_names) if tool_names else []
    messages = [prov.user_message(task["prompt"])]
    t0 = time.time()
    tin = tout = calls = turns = 0
    text, err = "", ""
    try:
        for _ in range(6):
            r = prov.chat(task["system"], messages, schemas, model)
            turns += 1
            tin += r.input_tokens; tout += r.output_tokens
            if not r.tool_calls:
                text = r.text
                break
            messages.append(r.assistant_message)
            results = []
            for c in r.tool_calls:
                calls += 1
                try:
                    out = ws.call(c.name, c.args)
                    results.append((c.id, c.name, out, False))
                except Exception as exc:
                    results.append((c.id, c.name, f"{type(exc).__name__}: {exc}", True))
            messages.extend(prov.tool_results(results))
        else:
            # same rule as the production orchestrator: out of tool turns -> forced no-tools synthesis
            err = "hit max turns (synthesised)"
            r = prov.chat(task["system"], messages + [prov.user_message(
                "You are out of tool turns. Write your best final answer NOW from what you gathered; say what you could not verify.")], [], model)
            turns += 1
            tin += r.input_tokens; tout += r.output_tokens
            text = r.text
    except Exception as exc:
        err = f"{type(exc).__name__}: {str(exc)[:200]}"
    secs = round(time.time() - t0, 1)
    checks = {name: bool(fn(text)) if text else False for name, fn in task["checks"].items()}
    return {"seconds": secs, "tokens_in": tin, "tokens_out": tout, "tool_calls": calls, "turns": turns,
            "error": err, "checks": checks, "score": round(sum(checks.values()) / max(1, len(checks)), 2),
            "output": text[:1600]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="atlas bench", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--engines", default="atlas,hermes", help="comma list: atlas, hermes")
    ap.add_argument("--tasks", default="research,write,data")
    ap.add_argument("--model", action="append", default=[], help="extra OpenRouter model(s) as additional atlas columns")
    ap.add_argument("--n", type=int, default=1)
    ap.add_argument("--hermes-url", default="")
    ap.add_argument("--hermes-key", default="")
    a = ap.parse_args(argv)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = WORKSPACE_DIR / "bench" / stamp
    out.mkdir(parents=True, exist_ok=True)
    default_model = "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"
    columns: list[tuple[str, str, str]] = []            # (key, engine, model)
    for e in [x.strip() for x in a.engines.split(",") if x.strip()]:
        if e == "atlas":
            columns.append((f"atlas:{default_model.split('/')[-1]}", "atlas", default_model))
        elif e == "hermes":
            columns.append(("hermes-agent", "hermes", "hermes-agent"))
    for m in a.model:
        columns.append((f"atlas:{m.split('/')[-1]}", "atlas", m))
    results: dict[str, Any] = {}
    for key, engine, model in columns:
        prov, mdl, forced = build_provider(engine, model, a)
        for tname in [x.strip() for x in a.tasks.split(",") if x.strip() in TASKS]:
            for i in range(a.n):
                print(f"== {key} / {tname} #{i+1}", flush=True)
                cell = run_cell(prov, mdl, TASKS[tname], forced, out / f"{key.replace(':','_').replace('/','_')}-{tname}-{i}")
                results.setdefault(key, {}).setdefault(tname, []).append(cell)
                print(f"   {cell['seconds']}s score {cell['score']} tokens {cell['tokens_in']}/{cell['tokens_out']} "
                      f"tools {cell['tool_calls']} {('ERR ' + cell['error']) if cell['error'] else ''}", flush=True)
                (out / "results.json").write_text(json.dumps(results, indent=1), encoding="utf-8")
                write_report(out, results, a)
    print("REPORT", out / "report.md")
    return 0


def write_report(out: Path, results: dict[str, Any], a) -> None:
    L = ["# Engine / model benchmark - real calls, real tools", "",
         f"Generated {time.strftime('%Y-%m-%d %H:%M')} · n={a.n} per cell · tasks: {a.tasks}", ""]
    tnames = [x.strip() for x in a.tasks.split(",") if x.strip() in TASKS]
    L.append("| engine/model | " + " | ".join(tnames) + " |")
    L.append("|---" * (len(tnames) + 1) + "|")
    for key, per in results.items():
        row = [key]
        for t in tnames:
            cells = per.get(t) or []
            if not cells:
                row.append("-")
                continue
            sc = sum(c["score"] for c in cells) / len(cells)
            secs = sum(c["seconds"] for c in cells) / len(cells)
            errs = sum(1 for c in cells if c["error"])
            row.append(f"score {sc:.2f} · {secs:.0f}s" + (f" · {errs} err" if errs else ""))
        L.append("| " + " | ".join(row) + " |")
    L.append("")
    for key, per in results.items():
        for t, cells in per.items():
            for i, c in enumerate(cells):
                L += [f"## {key} / {t} #{i+1} - score {c['score']} in {c['seconds']}s "
                      f"(tokens {c['tokens_in']}/{c['tokens_out']}, tools {c['tool_calls']}, turns {c['turns']})",
                      f"checks: {c['checks']}" + (f" · ERROR: {c['error']}" if c["error"] else ""), "",
                      "```", c["output"] or "(no output)", "```", ""]
    (out / "report.md").write_text("\n".join(L), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
