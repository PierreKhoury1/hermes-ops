"""LLM provider abstraction.

Two backends:
  * AnthropicProvider   — official `anthropic` SDK (Messages API, native tool use, adaptive thinking).
  * OpenAICompatProvider — any /v1/chat/completions server (Ollama, LM Studio, vLLM, OpenRouter, OpenAI).

Both expose the same surface so the orchestrator loop is provider-agnostic:
  chat(system, messages, tools, model) -> LLMResponse
  user_message(text) / tool_results([...]) -> provider-native message dicts
"""
from __future__ import annotations

import json
import time
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from . import config as cfg


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict[str, Any]


@dataclass
class LLMResponse:
    text: str
    tool_calls: list[ToolCall]
    assistant_message: Any            # provider-native message to append to history
    stop_reason: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    refusal: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


# Models where adaptive thinking / effort are valid and temperature is removed.
_ADAPTIVE_RE = re.compile(r"(fable-5|mythos-5|opus-5|sonnet-5|opus-4-[678]|sonnet-4-6)")
_FALLBACK_RE = re.compile(r"(fable-5|mythos-5|opus-5)")


class Provider:
    name: str = "base"

    def __init__(self, pcfg: dict[str, Any]):
        self.cfg = pcfg
        self.default_model = pcfg.get("default_model", "")

    def chat(self, system: str, messages: list[Any], tools: list[dict[str, Any]], model: str = "",
             on_token: Callable[..., None] | None = None) -> LLMResponse:
        """`on_token(text, thinking=False)` is called for every streamed delta when supplied."""
        raise NotImplementedError

    def user_message(self, text: str) -> Any:
        return {"role": "user", "content": text}

    def tool_results(self, results: list[tuple[str, str, str, bool]]) -> list[Any]:
        """results: [(tool_call_id, tool_name, content, is_error)] -> messages to append."""
        raise NotImplementedError

    def test(self, model: str = "") -> str:
        r = self.chat("Reply with the single word OK.", [self.user_message("ping")], [], model or self.default_model)
        return f"OK  model={r.model or model}  in={r.input_tokens} out={r.output_tokens}  reply={r.text.strip()[:40]!r}"


class AnthropicProvider(Provider):
    name = "anthropic"

    def __init__(self, pcfg: dict[str, Any]):
        super().__init__(pcfg)
        try:
            import anthropic  # noqa: F401
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("anthropic SDK missing: pip install anthropic") from exc
        import anthropic
        key = cfg.resolve_api_key(pcfg)
        kwargs: dict[str, Any] = {"timeout": float(pcfg.get("timeout", 600)), "max_retries": 3}
        if key:
            kwargs["api_key"] = key
        ws = (pcfg.get("workspace_id") or "").strip()   # required for identity-linked API keys
        if ws:
            kwargs["default_headers"] = {"anthropic-workspace-id": ws}
        self._client = anthropic.Anthropic(**kwargs)
        self._anthropic = anthropic

    @staticmethod
    def _tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]} for t in tools]

    def chat(self, system, messages, tools, model="", on_token=None):
        model = model or self.default_model or "claude-opus-5"
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": int(self.cfg.get("max_tokens", 16000)),
            "messages": messages,
        }
        if system:
            kwargs["system"] = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        if tools:
            kwargs["tools"] = self._tools(tools)
        adaptive = bool(_ADAPTIVE_RE.search(model))
        if adaptive:
            if self.cfg.get("thinking", "adaptive") == "adaptive":
                kwargs["thinking"] = {"type": "adaptive"}
            effort = self.cfg.get("effort")
            if effort in ("low", "medium", "high", "xhigh", "max"):
                kwargs["output_config"] = {"effort": effort}
        else:
            kwargs["temperature"] = float(self.cfg.get("temperature", 0.2))

        use_fallbacks = bool(self.cfg.get("fallbacks", True)) and bool(_FALLBACK_RE.search(model))
        A = self._anthropic

        def _run(with_fallbacks: bool):
            api = self._client.beta.messages if with_fallbacks else self._client.messages
            extra = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"} if with_fallbacks else {}
            if not on_token:
                return api.create(**extra, **kwargs)
            with api.stream(**extra, **kwargs) as s:
                for ev in s:
                    if getattr(ev, "type", "") == "content_block_delta":
                        d = ev.delta
                        dt = getattr(d, "type", "")
                        if dt == "text_delta" and d.text:
                            on_token(d.text)
                        elif dt == "thinking_delta" and getattr(d, "thinking", ""):
                            on_token(d.thinking, True)
                return s.get_final_message()

        try:
            resp = _run(use_fallbacks)
        except A.BadRequestError as exc:
            # Older SDK/server without fallbacks support -> retry plain.
            if use_fallbacks and "fallback" in str(exc).lower():
                resp = _run(False)
            else:
                raise

        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                inp = block.input if isinstance(block.input, dict) else json.loads(block.input or "{}")
                calls.append(ToolCall(block.id, block.name, inp))
        refusal = None
        if resp.stop_reason == "refusal":
            det = getattr(resp, "stop_details", None)
            refusal = f"refused ({getattr(det, 'category', None) or 'unspecified'}): {getattr(det, 'explanation', '') or ''}".strip()
        usage = resp.usage
        return LLMResponse(
            text="\n".join(text_parts).strip(),
            tool_calls=calls,
            assistant_message={"role": "assistant", "content": resp.model_dump()["content"]},
            stop_reason=resp.stop_reason or "",
            input_tokens=(usage.input_tokens or 0) + (getattr(usage, "cache_read_input_tokens", 0) or 0)
                         + (getattr(usage, "cache_creation_input_tokens", 0) or 0),
            output_tokens=usage.output_tokens or 0,
            model=resp.model,
            refusal=refusal,
        )

    def tool_results(self, results):
        content = []
        for call_id, _name, out, is_err in results:
            blk: dict[str, Any] = {"type": "tool_result", "tool_use_id": call_id, "content": out}
            if is_err:
                blk["is_error"] = True
            content.append(blk)
        return [{"role": "user", "content": content}]


# Per-model request extras for OpenAI-compatible endpoints (prefix match on the model id). nemotron-3-nano-omni:free (and ling before it) is a
# reasoning model: with reasoning on it burns max_tokens in hidden thought and returns content=null, so we switch it off.
MODEL_EXTRAS: dict[str, dict[str, Any]] = {
    "inclusionai/ling-3.0": {"reasoning": {"enabled": False}},
    "nvidia/nemotron-3-nano-omni": {"reasoning": {"enabled": False}},
}


def model_extras(model: str) -> dict[str, Any]:
    for prefix, extra in MODEL_EXTRAS.items():
        if (model or "").startswith(prefix):
            return dict(extra)
    return {}


class OpenAICompatProvider(Provider):
    name = "openai"

    def __init__(self, pcfg: dict[str, Any]):
        super().__init__(pcfg)
        import httpx
        self._httpx = httpx
        self.base_url = (pcfg.get("base_url") or "").rstrip("/")
        self.api_key = cfg.resolve_api_key(pcfg)

    @staticmethod
    def _tools(tools):
        return [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                  "parameters": t["parameters"]}} for t in tools]

    def chat(self, system, messages, tools, model="", on_token=None):
        model = model or self.default_model
        if not self.base_url:
            raise RuntimeError("OpenAI-compatible provider: base_url not set (Settings).")
        if not model:
            raise RuntimeError("OpenAI-compatible provider: model not set (Settings or agent).")
        msgs = ([{"role": "system", "content": system}] if system else []) + list(messages)
        payload: dict[str, Any] = {
            "model": model, "messages": msgs,
            "max_tokens": int(self.cfg.get("max_tokens", 4096)),
            "temperature": float(self.cfg.get("temperature", 0.2)),
        }
        if tools:
            payload["tools"] = self._tools(tools)
        payload.update(model_extras(model))
        payload.update(getattr(self, "_extra_payload", None) or {})
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        headers.update(getattr(self, "_extra_headers", None) or {})
        if on_token:
            payload["stream"] = True
            payload["stream_options"] = {"include_usage": True}

        def _raise(r):
            msg = r.text[:400]
            try:
                err = r.json().get("error")
                msg = err.get("message", msg) if isinstance(err, dict) else (err or msg)
                meta = err.get("metadata") if isinstance(err, dict) else None
                if isinstance(meta, dict) and meta.get("raw"):
                    msg = f"{msg} [{meta.get('provider_name', 'upstream')}: {str(meta['raw'])[:240]}]"
            except Exception:
                pass
            if "free-models-per-day" in str(msg):
                msg = f"{msg} [daily free-model quota exhausted on this OpenRouter account - resets 00:00 UTC; add credits or switch tier]"
            raise RuntimeError(f"HTTP {r.status_code}: {msg}")

        fallback = (self.cfg.get("fallback_model") or "").strip()
        # Wall-clock limits for streamed calls. OpenRouter keeps a queued request alive with ": PROCESSING"
        # comments, so the socket read timeout alone never fires - without these a throttled model hangs a run forever.
        limits = {"first_token_s": float(self.cfg.get("first_token_s", 120)), "call_max_s": float(self.cfg.get("call_max_s", 420))}
        with self._httpx.Client(base_url=self.base_url, headers=headers,
                                timeout=float(self.cfg.get("timeout", 300))) as client:
            try:
                body = self._once(client, payload, on_token, _raise, limits)
            except RuntimeError as exc:
                text = str(exc)
                code = text[:9]
                upstream = code.startswith("HTTP 400") and "Provider returned error" in text     # OpenRouter wrapping a flaky upstream
                retryable = upstream or any(code.startswith(f"HTTP {c}") for c in ("402", "404", "429", "500", "502", "503", "529"))   # 404 = model withdrawn from OpenRouter -> fallback_model
                if not retryable:
                    raise
                if upstream or code.startswith(("HTTP 500", "HTTP 502", "HTTP 503", "HTTP 529")):
                    try:                                             # same model, one more time (upstream blips are usually seconds)
                        time.sleep(2.0)
                        body = self._once(client, payload, on_token, _raise, limits)
                    except RuntimeError:
                        if not (fallback and fallback != model):
                            raise
                        payload["model"] = fallback
                        body = self._once(client, payload, on_token, _raise, limits)
                        body["_fallback_from"] = model
                elif fallback and fallback != model:
                    payload["model"] = fallback
                    body = self._once(client, payload, on_token, _raise, limits)
                    body["_fallback_from"] = model
                else:
                    raise
        if not body.get("choices"):                     # OpenRouter can return 200 with an error body and no choices
            raise RuntimeError(f"HTTP 502: provider returned no choices: {json.dumps(body)[:200]}")
        choice = body["choices"][0]
        message = choice["message"]
        calls = []
        for tc in message.get("tool_calls") or []:
            fn = tc.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
                if not isinstance(args, dict):
                    raise json.JSONDecodeError("not an object", "", 0)
            except json.JSONDecodeError:
                # a truncated / malformed tool call (free models do this). Keep the raw text for the tool error, but
                # rewrite the stored assistant message so the next request validates upstream instead of 400-ing forever.
                args = {"_raw": fn.get("arguments")}
                fn["arguments"] = json.dumps({"_raw": str(fn.get("arguments") or "")[:2000], "_error": "malformed arguments, resend"})
            calls.append(ToolCall(tc.get("id") or f"call_{len(calls)}", fn.get("name", ""), args))
        usage = body.get("usage") or {}
        return LLMResponse(
            text=(message.get("content") or "").strip(),
            tool_calls=calls,
            assistant_message=message,
            stop_reason=choice.get("finish_reason") or "",
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            model=body.get("model") or model,
        )

    def _once(self, client, payload, on_token, _raise, limits=None) -> dict[str, Any]:
        if not on_token:
            r = client.post("/chat/completions", json=payload)
            if r.status_code >= 400:
                _raise(r)
            return r.json()
        return self._stream(client, payload, on_token, _raise, limits or {})

    @staticmethod
    def _stream(client, payload, on_token, _raise, limits=None) -> dict[str, Any]:
        """Consume an OpenAI-style SSE stream; rebuild the same `body` shape the non-stream path returns.
        Enforces wall-clock limits: no first token within `first_token_s`, or `call_max_s` overall -> HTTP 504 (retryable)."""
        import time as _t
        limits = limits or {}
        first_s = float(limits.get("first_token_s") or 120)
        max_s = float(limits.get("call_max_s") or 420)
        t0 = _t.time()
        got_any = False
        text_parts: list[str] = []
        calls: dict[int, dict[str, str]] = {}
        finish, usage, model_used = "", {}, ""
        with client.stream("POST", "/chat/completions", json=payload) as r:
            if r.status_code >= 400:
                r.read()
                _raise(r)
            for line in r.iter_lines():
                waited = _t.time() - t0
                if not got_any and waited > first_s:
                    raise RuntimeError(f"HTTP 504: model stalled - no output after {int(waited)}s (queued or throttled upstream)")
                if waited > max_s:
                    raise RuntimeError(f"HTTP 504: model call exceeded {int(max_s)}s")
                if not line or not line.startswith("data:"):
                    continue
                got_any = True
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if chunk.get("usage"):
                    usage = chunk["usage"]
                model_used = chunk.get("model") or model_used
                for ch in chunk.get("choices") or []:
                    d = ch.get("delta") or {}
                    think = d.get("reasoning") or d.get("reasoning_content")
                    if think:
                        on_token(think, True)
                    if d.get("content"):
                        text_parts.append(d["content"])
                        on_token(d["content"])
                    for tc in d.get("tool_calls") or []:
                        acc = calls.setdefault(int(tc.get("index") or 0), {"id": "", "name": "", "args": ""})
                        if tc.get("id"):
                            acc["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name") and not acc["name"]:
                            acc["name"] = fn["name"]
                        if fn.get("arguments"):
                            acc["args"] += fn["arguments"]
                    if ch.get("finish_reason"):
                        finish = ch["finish_reason"]
        message: dict[str, Any] = {"role": "assistant", "content": "".join(text_parts)}
        if calls:
            message["tool_calls"] = [{"id": a["id"] or f"call_{i}", "type": "function",
                                      "function": {"name": a["name"], "arguments": a["args"] or "{}"}}
                                     for i, a in sorted(calls.items())]
        return {"choices": [{"message": message, "finish_reason": finish}], "usage": usage, "model": model_used}

    def tool_results(self, results):
        return [{"role": "tool", "tool_call_id": call_id, "name": name,
                 "content": (f"ERROR: {out}" if is_err else out)}
                for call_id, name, out, is_err in results]


class DemoProvider(Provider):
    """Scripted, offline stand-in for a model. Lets the whole pipeline (orchestration, delegation,
    approvals, CRM, audit) run end-to-end with no API key. Output is plausible but canned."""
    name = "demo"

    def __init__(self, pcfg: dict[str, Any]):
        super().__init__(pcfg)
        self.default_model = "demo-scripted"
        self.delay = float(pcfg.get("delay", 0.6))

    @staticmethod
    def _roster(system: str) -> list[str]:
        ids = []
        for line in system.splitlines():
            line = line.strip()
            if line.startswith("- ") and ":" in line and "—" in line:
                ids.append(line[2:].split(":", 1)[0].strip())
        return ids

    @staticmethod
    def _role(system: str) -> str:
        s = system.lower()[:400]
        keys = ("research", "writer", "proposal", "communications", "crm", "finance", "pricing",
                "strategy", "review", "creative", "media", "product", "merch", "marketing", "operations")
        found = [(s.find(k), k) for k in keys if k in s]
        return min(found)[1] if found else "specialist"

    @staticmethod
    def _task_of(messages: list[Any]) -> str:
        first = messages[0]["content"] if messages else ""
        return first if isinstance(first, str) else ""

    @staticmethod
    def _tool_outputs(messages: list[Any]) -> list[str]:
        outs = []
        for m in messages:
            c = m.get("content") if isinstance(m, dict) else None
            if isinstance(c, list):
                for blk in c:
                    if isinstance(blk, dict) and blk.get("type") == "tool_result":
                        outs.append(str(blk.get("content", "")))
        return outs

    def chat(self, system, messages, tools, model="", on_token=None):
        import time as _t
        fault = float(self.cfg.get("fault_rate") or 0)          # soak testing: inject provider failures / stalls
        if fault > 0:
            import random
            roll = random.random()
            if roll < fault * 0.5:
                raise RuntimeError("HTTP 429: injected fault - rate limit exceeded (soak)")
            if roll < fault * 0.8:
                raise RuntimeError("HTTP 500: injected fault - upstream error (soak)")
            if roll < fault:
                _t.sleep(float(self.cfg.get("fault_sleep") or 8))
        _t.sleep(self.delay)
        r = self._chat(system, messages, tools)
        if on_token and r.text:   # simulate token streaming so the live view behaves like a real model
            words = r.text.split(" ")
            step = min(0.04, 1.5 / max(1, len(words)))
            for i, w in enumerate(words):
                on_token(w + (" " if i < len(words) - 1 else ""))
                _t.sleep(step)
        return r

    def _chat(self, system, messages, tools):
        names = {t["name"] for t in tools}
        task = self._task_of(messages)
        subject_line = (task.strip().splitlines() or ["the task"])[0][:90]
        if "Enquiry:" in task:  # lead-shaped task: use the enquiry itself as the topic
            enq = task.split("Enquiry:", 1)[1].strip()
            first = (enq.splitlines() or [subject_line])[0]
            subject_line = first.split(". ")[0].strip().rstrip(".")[:140]
        n_user = sum(1 for m in messages if m.get("role") == "user")

        def resp(text, calls=(), stop="end_turn"):
            return LLMResponse(text, list(calls), {"role": "assistant", "content": text or "(tool call)"},
                               stop, 350 + len(task) // 4, 120 + len(text) // 4, self.default_model)

        if "delegate" in names:  # ---- orchestrator script
            roster = [r for r in self._roster(system) if r not in ("atlas",)]
            if n_user == 1:
                targets = [r for r in roster if r != "qa"][:3] or roster[:1]
                calls = [ToolCall(f"d{i}", "delegate", {"agent_id": r,
                         "task": f"{subject_line}", "context": task}) for i, r in enumerate(targets)]
                return resp(f"Plan: this needs {', '.join(targets)}. Delegating in parallel.", calls, "tool_use")
            outs = self._tool_outputs(messages)
            done_kinds = " ".join(outs)
            if "queue_action" in names and "queued for approval" not in done_kinds:
                draft = next((o for o in outs if "Subject:" in o), "")
                subj = draft.split("Subject:", 1)[1].splitlines()[0].strip() if "Subject:" in draft else f"Re: {subject_line}"
                body = draft.split("\n\n", 1)[1] if "\n\n" in draft else draft or f"Hello,\n\nFollowing up on {subject_line}."
                to = "lead"
                for line in task.splitlines():
                    if line.lower().startswith("email:"):
                        to = line.split(":", 1)[1].strip()
                calls = [ToolCall("q1", "queue_action", {"kind": "email", "to": to, "subject": subj, "body": body,
                                  "reason": "Outbound message to a lead — needs owner approval."})]
                if "crm_update" in names:
                    calls.append(ToolCall("q2", "crm_update", {"contact": to, "fields": {"stage": "Qualified",
                                          "notes": "Researched + outreach drafted; awaiting approval."}}))
                return resp("Drafts ready. Queuing the outreach for approval and updating the CRM.", calls, "tool_use")
            if "save_deliverable" in names and "saved " not in done_kinds:
                body = "\n\n".join(f"## {i+1}\n{o}" for i, o in enumerate(outs) if o and "queued" not in o and "updated" not in o)
                return resp("", [ToolCall("s1", "save_deliverable", {"filename": "deliverable.md",
                             "content": f"# {subject_line}\n\n{body}"})], "tool_use")
            summary = (f"Done. Researched the lead, drafted personalised outreach, updated the CRM and queued the email "
                       f"for your approval." if "queue_action" in names else
                       f"Done. Produced a research brief, recommendation and pricing; deliverable saved.")
            return resp("", [ToolCall("f1", "finish", {"summary": summary})], "tool_use")

        # ---- specialist scripts
        role = self._role(system)
        m_sign = re.search(r"Sign outbound messages as: (.+)", system)
        signer = m_sign.group(1).strip() if m_sign else "The team"
        name = next((line for line in task.splitlines() if line.lower().startswith("name:")), "Name: the contact").split(":", 1)[1].strip()
        company = next((line for line in task.splitlines() if line.lower().startswith("company:")), "Company: their business").split(":", 1)[1].strip()
        if company in ("(individual)", "-", ""):
            company = name or "the client"
        if role == "research":
            return resp(f"**Research brief — {company}**\n\n- Contact: {name}. Likely decision-maker for {subject_line.lower()}.\n"
                        f"- Signals: recent activity suggests an active need; no existing supplier relationship found.\n"
                        f"- Comparable clients: two similar businesses engaged us on the same problem in the last year.\n"
                        f"- Risks: budget timing unknown; verify on first call.\n- Open questions: timeline, who else is involved in the decision.\n\n"
                        f"*(demo mode — replace with a live model for real research)*")
        if role in ("writer", "proposal", "communications", "creative"):
            return resp(f"Subject: Quick idea for {company}\n\n"
                        f"Hi {name.split()[0] if name else 'there'},\n\n"
                        f"Thanks for getting in touch — \"{subject_line}\". We've helped {'two clients in a similar position' if company == name else 'two businesses like ' + company} with exactly this in the last year; "
                        f"typically it comes down to fixing the hand-offs rather than adding headcount.\n\n"
                        f"Happy to share what worked on a 20-minute call this week. Would a morning or an afternoon suit you better?\n\n"
                        f"Best,\n{signer}")
        if role == "crm":
            return resp(f"CRM: matched {name} at {company}. Stage moved to Qualified. Next action: follow-up in 3 days if no reply.")
        if role in ("finance", "pricing"):
            return resp(f"**Pricing — {subject_line}**\n\n| Item | Days | Rate | Total |\n|---|---|---|---|\n| Discovery | 3 | 1,100 | 3,300 |\n| Delivery | 12 | 1,100 | 13,200 |\n| Review | 2 | 1,100 | 2,200 |\n| **Fixed fee** | | | **18,700** |\n\nPayment: 40% on signing, 40% mid-point, 20% on completion.")
        if role == "strategy":
            return resp(f"**Recommendation — {subject_line}**\n\n1. Diagnose: map the current process and where value leaks.\n2. Fix the top two bottlenecks first (6 weeks).\n3. Then scale with tooling and a light operating rhythm.\n\nRisks: change fatigue, unclear ownership. Mitigate with a named sponsor and weekly checkpoint.")
        if role == "review":
            return resp("Review: structure sound; tighten the opening line, add one concrete number, confirm the pricing assumptions. Otherwise ready to send.")
        return resp(f"Completed: {subject_line}. (demo output)")

    def tool_results(self, results):
        return [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": c, "content": o} for c, _n, o, _e in results]}]


class HermesAgentProvider(OpenAICompatProvider):
    """A running Hermes Agent (Nous Research) API server used as an *engine*: the agent runs its own tool loop
    (terminal, browser, web search, memory, skills, MCP) server-side and returns the final answer.

    Client-side tool schemas are therefore not sent - Atlas keeps orchestration, CRM, policy and approvals on our side
    and treats the Hermes-backed agent as a very capable specialist. Long-term memory is scoped per desk+agent via
    X-Hermes-Session-Key, so the agent remembers a client's business between runs."""
    name = "hermes_agent"

    def __init__(self, pcfg: dict[str, Any]):
        pcfg = {**pcfg, "type": "openai_compatible", "base_url": str(pcfg.get("base_url") or "").rstrip("/") + "/v1",
                "default_model": pcfg.get("default_model") or "hermes-agent", "max_tokens": int(pcfg.get("max_tokens", 4096)),
                "first_token_s": float(pcfg.get("first_token_s", 240)), "call_max_s": float(pcfg.get("call_max_s", 900)),
                "timeout": float(pcfg.get("timeout", 900))}
        super().__init__(pcfg)
        self.session_key = str(pcfg.get("session_key") or "")

    def chat(self, system, messages, tools, model="", on_token=None):
        # Hermes ignores client tools and runs its own; never advertise ours (they would be unusable server-side).
        self._extra_headers = {"X-Hermes-Session-Key": self.session_key} if self.session_key else {}
        want = model or self.default_model or "hermes-agent"
        # Per-request model choice inside the runtime: Hermes honours "model"+"provider" together (bare ids are ignored).
        self._extra_payload = {"provider": "openrouter"} if want != "hermes-agent" else {}
        return super().chat(system, messages, [], want, on_token=on_token)


def make_provider(pcfg: dict[str, Any]) -> Provider:
    t = (pcfg.get("type") or "anthropic").lower()
    if t == "demo":
        return DemoProvider(pcfg)
    if t == "anthropic":
        return AnthropicProvider(pcfg)
    if t == "hermes_agent":
        return HermesAgentProvider(pcfg)
    return OpenAICompatProvider(pcfg)


class ProviderPool:
    """Lazily builds one provider instance per configured provider name."""

    def __init__(self, providers_cfg: dict[str, Any]):
        self.cfg = providers_cfg
        self._cache: dict[str, Provider] = {}

    @property
    def default_name(self) -> str:
        return self.cfg.get("default_provider") or next(iter(self.cfg.get("providers", {})), "anthropic")

    def get(self, name: str = "") -> Provider:
        name = name or self.default_name
        if name not in self._cache:
            pcfg = self.cfg.get("providers", {}).get(name)
            if pcfg is None:
                raise RuntimeError(f"Unknown provider '{name}'")
            self._cache[name] = make_provider(pcfg)
        return self._cache[name]
