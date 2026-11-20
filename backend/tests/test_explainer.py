import httpx
import pytest

from app.ai.explainer import Explainer, TemplateNarrator, validate_against_facts
from app.core.config import Settings

FACTS = {
    "incident": {"id": 7, "title": "Database connection pool exhaustion in payment-service", "status": "open", "severity": "critical",
                 "opened_at": "2026-01-01T00:00:00Z", "primary_service": "payment-service"},
    "investigation": {"status": "completed", "window_start": "x", "window_end": "y"},
    "findings": [
        {"rank": 1, "service": "payment-service", "cause_type": "db_pool_exhaustion", "title": "Database connection pool exhaustion in payment-service",
         "score": 0.95, "confidence": "high",
         "top_signals": [{"description": "Connections in use vs pool size", "value": "100.0%", "strength": 1.0},
                         {"description": "P95 time waiting for a pooled connection", "value": "2001 ms", "strength": 1.0}],
         "recommendations": ["Look for connection leaks in payment-service."]},
        {"rank": 2, "service": "payment-service", "cause_type": "http_500_spike", "title": "Application errors (HTTP 5xx spike) in payment-service",
         "score": 0.6, "confidence": "medium", "top_signals": [], "recommendations": []},
    ],
    "log_highlights": [{"service": "payment-service", "template": "database connection pool exhausted", "count": 42}],
    "history": [],
}


def test_template_summary_is_grounded():
    t = TemplateNarrator().summary(FACTS)
    assert "pool exhaustion" in t and "0.95" in t and "payment-service" in t
    assert validate_against_facts(t, FACTS) == []


@pytest.mark.parametrize("q,needle", [("what do the logs show?", "pool exhausted"), ("how do I fix it?", "connection leaks"),
                                      ("why this cause?", "weighted sum"), ("has this happened before?", "No earlier")])
def test_template_answers_by_intent(q, needle):
    assert needle in TemplateNarrator().answer(FACTS, q)


def test_inconclusive_wording():
    facts = FACTS | {"investigation": {"status": "inconclusive"}}
    assert "insufficient" in TemplateNarrator().summary(facts)


def test_validator_rejects_fabrication():
    assert any("inventory-service" in p for p in validate_against_facts("The inventory-service is down.", FACTS))
    assert any("unsupported number" in p for p in validate_against_facts("Latency rose to 9999 ms.", FACTS))
    assert validate_against_facts("High CPU utilisation caused this.", FACTS)
    assert validate_against_facts("Pool usage hit 100.0% and waits reached 2001 ms in payment-service.", FACTS) == []


async def test_explainer_without_llm_uses_template():
    e = Explainer(Settings(jwt_secret="x" * 40, llm_provider="none"))
    out = await e.explain(FACTS)
    assert out.provider == "deterministic-template" and out.validated


async def test_llm_output_failing_validation_falls_back(monkeypatch):
    e = Explainer(Settings(jwt_secret="x" * 40, llm_provider="openai_compatible", llm_base_url="http://llm.local/v1", llm_model="m"))
    assert e.llm is not None

    async def fake(self, facts, q):  # noqa: ANN001, ANN202
        return "The root cause is high CPU in user-service at 97%."
    monkeypatch.setattr(type(e.llm), "complete", fake)
    out = await e.explain(FACTS)
    assert out.provider == "deterministic-template"
    assert "rejected" in out.notes[0]


async def test_llm_grounded_output_accepted(monkeypatch):
    e = Explainer(Settings(jwt_secret="x" * 40, llm_provider="openai_compatible", llm_base_url="http://llm.local/v1", llm_model="m"))

    async def fake(self, facts, q):  # noqa: ANN001, ANN202
        return "Database connection pool exhaustion in payment-service: pool usage 100.0%, waits of 2001 ms."
    monkeypatch.setattr(type(e.llm), "complete", fake)
    out = await e.explain(FACTS)
    assert out.provider.startswith("llm:") and "passed" in out.notes[0]


async def test_llm_unavailable_falls_back(monkeypatch):
    e = Explainer(Settings(jwt_secret="x" * 40, llm_provider="openai_compatible", llm_base_url="http://llm.local/v1", llm_model="m"))

    async def boom(self, facts, q):  # noqa: ANN001, ANN202
        raise httpx.ConnectError("down")
    monkeypatch.setattr(type(e.llm), "complete", boom)
    out = await e.explain(FACTS, "summary?")
    assert out.provider == "deterministic-template" and "unavailable" in out.notes[0]
