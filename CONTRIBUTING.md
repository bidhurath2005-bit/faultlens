# Contributing

## Development setup
See the README's *Installation* section, then:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\pip install -r backend\requirements-dev.txt -r services\requirements.txt .\services\common
cd frontend; npm ci; cd ..
docker compose up -d postgres redis      # needed by backend API/DB tests
```

Run the UI against a running backend with hot reload: `cd frontend; npm run dev` (http://localhost:5173,
which proxies `/api` and the WebSocket to `localhost:8000`).

## Before opening a pull request
These are the same commands CI runs (`.github/workflows/`):

```powershell
cd backend;  ..\.venv\Scripts\ruff check app tests;  ..\.venv\Scripts\pytest -q;  cd ..
cd services; ..\.venv\Scripts\ruff check .;  $env:OTEL_ENABLED="false"; ..\.venv\Scripts\pytest -q; cd ..
cd frontend; npx tsc -b --noEmit; npx eslint . --max-warnings 0; npx vitest run; npm run build; cd ..
docker compose config --quiet
```

With the stack running, also run `e2e/test_integration.py` (and `e2e/test_e2e_flow.py` for changes to
detection, correlation or RCA).

## Conventions
- **Schema changes** go through Alembic only: `alembic revision --autogenerate -m "…"`, review the generated
  file, and make sure `alembic check` passes.
- **New metric names**: add the instrument in `services/common/faultlens_common/telemetry.py` *and* the
  query in `backend/app/telemetry/promql.py`; never hard-code PromQL elsewhere.
- **RCA changes**: every new signal needs a weight (the weights of a hypothesis must sum to 1), a documented
  strength function, a unit test in `tests/test_rca_scoring.py`, and a live re-evaluation
  (`python e2e/rca_eval.py`) with the results recorded in `BENCHMARKS.md`.
- **Detector changes**: re-run `python -m app.ml.evaluate` and update `BENCHMARKS.md`.
- **Never** commit `.env`, tokens or real credentials. Logs must not include secrets (the JSON formatter redacts
  common keys, but do not rely on it).
- Do not report results you did not measure.

## Commit messages
Imperative mood and a short subject (`Add pool-wait signal to RCA`), with the body explaining *why*.

## Architecture decisions
For a material design change, add or supersede an ADR in `docs/adr/` (copy the structure of an existing ADR).
