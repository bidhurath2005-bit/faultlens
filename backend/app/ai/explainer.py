"""AI explanation layer.

Source of truth: the persisted, deterministic investigation. This layer only *phrases* it.

  * TemplateNarrator (default, no API key): deterministic natural-language rendering of findings.
  * Optional LLM providers (Anthropic Messages API, or any OpenAI-compatible endpoint such as a
    local Ollama) receive ONLY the structured facts and must answer from them. Their output is
    validated: every service name and every number it states must exist in the facts, and the
    named root cause must match the top-ranked finding. Any violation => the LLM answer is
    discarded and the deterministic narrative is returned instead (flagged in the response).
  * The layer has no tools and cannot execute actions.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import DEMO_SERVICES, Settings
from app.engines.rca.hypotheses import CAUSE_TITLES

log = logging.getLogger("faultlens.ai")


@dataclass
class Explanation:
    text: str
    provider: str
    validated: bool
    notes: list[str]

    def to_dict(self) -> dict:
        return {"text": self.text, "provider": self.provider, "validated": self.validated, "notes": self.notes}


def facts_from_investigation(inv: dict[str, Any], incident: dict[str, Any], history: list[dict] | None = None) -> dict:
    findings = inv.get("findings", [])
    return {
        "incident": {k: incident.get(k) for k in ("id", "title", "status", "severity", "opened_at", "primary_service")},
        "investigation": {"status": inv.get("status"), "window_start": inv.get("window_start"),
                          "window_end": inv.get("window_end")},
        "findings": [
            {"rank": f["rank"], "service": f.get("service"), "cause_type": f["cause_type"], "title": f["title"],
             "score": f["score"], "confidence": f["confidence"],
             "top_signals": [{"description": s["description"], "value": s["value"], "strength": s["strength"]}
                             for s in sorted(f.get("signals", []), key=lambda s: -s["contribution"])[:4]],
             "recommendations": [r["action"] for r in f.get("recommendations", [])]}
            for f in findings[:3]
        ],
        "log_highlights": inv.get("log_highlights", [])[:5],
        "history": history or [],
    }


class TemplateNarrator:
    name = "deterministic-template"

    def summary(self, facts: dict) -> str:
        inc, fs = facts["incident"], facts["findings"]
        if not fs or facts["investigation"]["status"] == "inconclusive":
            lead = (f"Incident #{inc['id']} ({inc['severity']}) is under investigation, but the available evidence is "
                    f"insufficient to name a root cause with confidence.")
            if fs:
                lead += f" The strongest (weak) candidate is {fs[0]['title'].lower()} (score {fs[0]['score']:.2f})."
            return lead + " Collect more telemetry or widen the window before acting."
        top = fs[0]
        sig = "; ".join(f"{s['description'].lower()}: {s['value']}" for s in top["top_signals"] if s["strength"] > 0.2)
        text = (f"Incident #{inc['id']} ({inc['severity']}): the most likely root cause is {top['title'].lower()} "
                f"(score {top['score']:.2f}, {top['confidence']} confidence). Supporting evidence: {sig}.")
        if len(fs) > 1:
            alt = ", ".join(f"{f['title'].lower()} ({f['score']:.2f})" for f in fs[1:3])
            text += f" Alternatives considered and ranked lower: {alt}."
        if top["recommendations"]:
            text += f" Suggested first step: {top['recommendations'][0]}"
        return text

    def answer(self, facts: dict, question: str) -> str:
        q = question.lower()
        fs = facts["findings"]
        if any(w in q for w in ("log", "error message", "exception")):
            hl = facts.get("log_highlights") or []
            if not hl:
                return "No warning/error log patterns were captured for this investigation window."
            return "Most relevant log patterns in the window: " + "; ".join(
                f"[{h['service']}] {h['template']} (x{h['count']})" for h in hl)
        if any(w in q for w in ("fix", "remediat", "what should", "recommend", "mitigat")):
            if not fs:
                return "No remediation can be recommended without a supported finding."
            return "Recommended actions (advisory, not executed automatically): " + " ".join(
                f"{i}. {r}" for i, r in enumerate(fs[0]["recommendations"], 1))
        if any(w in q for w in ("before", "history", "similar", "previous", "again")):
            h = facts.get("history") or []
            if not h:
                return "No earlier resolved incidents with the same suspected cause and service were found."
            return "Similar past incidents: " + "; ".join(
                f"#{x['id']} {x['title']} (resolved {x.get('resolved_at') or 'n/a'})" for x in h)
        if any(w in q for w in ("why", "confiden", "score", "evidence")) and fs:
            top = fs[0]
            return (f"{top['title']} scored {top['score']:.2f}. The score is a weighted sum of evidence signals: "
                    + "; ".join(f"{s['description']} = {s['value']} (strength {s['strength']:.2f})" for s in top["top_signals"]))
        return self.summary(facts)


_NUM = re.compile(r"(?<![\w.])\d+(?:\.\d+)?")


def validate_against_facts(text: str, facts: dict) -> list[str]:
    """Returns a list of violations (empty == valid)."""
    problems: list[str] = []
    blob = json.dumps(facts, default=str).lower()
    for svc in DEMO_SERVICES:
        if svc in text.lower() and svc not in blob:
            problems.append(f"mentions service not in evidence: {svc}")
    # Compare numerically (0.6 == 0.60) and allow trivially small integers used in prose (ranks, counts <= 10).
    allowed = {round(float(n), 4) for n in _NUM.findall(blob)}
    for n in _NUM.findall(text):
        if round(float(n), 4) not in allowed and not (n.isdigit() and int(n) <= 10):
            problems.append(f"unsupported number: {n}")
    if facts["findings"]:
        top = facts["findings"][0]
        other_titles = [t.lower() for c, t in CAUSE_TITLES.items() if c != top["cause_type"]]
        named_other = [t for t in other_titles if t in text.lower()]
        if named_other and top["title"].lower().split(" in ")[0] not in text.lower():
            problems.append("names a root cause that is not the top-ranked finding")
    return problems[:10]


SYSTEM_PROMPT = (
    "You are FaultLens' incident explanation assistant. Answer ONLY from the JSON facts provided. "
    "Do not invent services, numbers, causes or actions. If the facts do not answer the question, say so. "
    "Never instruct anyone to run destructive commands. Keep answers under 150 words."
)


class LLMNarrator:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self.name = f"llm:{settings.llm_provider}:{settings.llm_model}"

    async def complete(self, facts: dict, question: str) -> str:
        user = f"FACTS:\n{json.dumps(facts, default=str)}\n\nQUESTION: {question}"
        async with httpx.AsyncClient(timeout=30) as c:
            if self.s.llm_provider == "anthropic":
                r = await c.post((self.s.llm_base_url or "https://api.anthropic.com") + "/v1/messages",
                                 headers={"x-api-key": self.s.llm_api_key, "anthropic-version": "2023-06-01",
                                          "content-type": "application/json"},
                                 json={"model": self.s.llm_model or "claude-haiku-5-5", "max_tokens": 400,
                                       "system": SYSTEM_PROMPT, "messages": [{"role": "user", "content": user}]})
                r.raise_for_status()
                return "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
            r = await c.post(self.s.llm_base_url.rstrip("/") + "/chat/completions",
                             headers={"Authorization": f"Bearer {self.s.llm_api_key}"} if self.s.llm_api_key else {},
                             json={"model": self.s.llm_model, "temperature": 0,
                                   "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                                                {"role": "user", "content": user}]})
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]


class Explainer:
    def __init__(self, settings: Settings) -> None:
        self.template = TemplateNarrator()
        configured = settings.llm_provider in {"anthropic", "openai_compatible"} and (
            settings.llm_api_key or settings.llm_provider == "openai_compatible") and (
            settings.llm_provider == "anthropic" or settings.llm_base_url)
        self.llm = LLMNarrator(settings) if configured else None

    @property
    def provider(self) -> str:
        return self.llm.name if self.llm else self.template.name

    async def explain(self, facts: dict, question: str | None = None) -> Explanation:
        fallback = self.template.answer(facts, question) if question else self.template.summary(facts)
        if self.llm is None:
            return Explanation(fallback, self.template.name, True, ["deterministic narrative generated from findings"])
        try:
            text = await self.llm.complete(facts, question or "Summarise this incident and its most likely root cause.")
        except Exception as exc:  # noqa: BLE001
            log.warning("llm provider failed; using deterministic narrative", extra={"error": type(exc).__name__})
            return Explanation(fallback, self.template.name, True, [f"LLM unavailable ({type(exc).__name__}); used template"])
        problems = validate_against_facts(text, facts)
        if problems:
            return Explanation(fallback, self.template.name, True, ["LLM answer rejected by evidence validator"] + problems)
        return Explanation(text.strip(), self.llm.name, True, ["LLM answer passed evidence validation"])
