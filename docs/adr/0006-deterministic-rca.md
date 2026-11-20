# ADR-006: Deterministic, evidence-based RCA as the source of truth

- **Status:** Accepted
- **Date:** 2026-11-19

## Context
Root-cause suggestions must be trustworthy and explainable to an on-call engineer, testable, and must not
require a paid AI API. LLMs can phrase findings well but can also invent causes and numbers.

## Decision
Root causes are produced by a **deterministic scoring engine**:

`score = locality × Σ(weightᵢ × strengthᵢ)`

- Eight failure classes × each service form the hypothesis space.
- Each hypothesis has documented signals from metrics, logs, traces and dependencies; weights sum to 1;
  strengths are piecewise-linear functions of observed values.
- A hypothesis exists only if a *primary* signal reaches 0.3. Missing evidence contributes 0; nothing is guessed.
- Locality (0.4–1.0) down-weights symptom-type causes in services whose dependencies are themselves anomalous,
  unless traces attribute the slow time or errors to the service itself.
- A best score below 0.35 makes the investigation **inconclusive**.
- Every signal, weight, strength and contribution is persisted and shown in the UI.

The AI layer only **phrases** persisted findings. By default that is a deterministic template narrator.
Optional LLM providers receive only the structured facts, and their output is validated (service names,
numbers and the named cause must appear in the facts) or discarded.

## Alternatives considered
- **LLM-first RCA**: flexible, but non-deterministic, hard to evaluate, prone to hallucination, needs a paid key.
- **Supervised classifier over features**: needs labelled real incidents we do not have; less explainable.
- **Pure graph heuristics** ("deepest anomalous node"): misses resource causes that do not propagate (memory) and
  cannot tell pool exhaustion from slow queries.

## Consequences
- Fully reproducible and unit-testable; evaluated live with top-1/top-3 accuracy (BENCHMARKS.md).
- Hand-designed weights encode domain knowledge and will not generalise to failure classes they do not model.
  Such incidents should surface as inconclusive or low-confidence rather than confidently wrong; that is a goal,
  not a guarantee.
- The RCA package is forbidden (by test) from reading fault-injection records.

## Implementation
`backend/app/engines/rca/*`, `backend/app/ai/explainer.py`, `backend/tests/test_rca_scoring.py`.
