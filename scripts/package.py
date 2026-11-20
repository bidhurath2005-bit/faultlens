"""Builds FaultLens.zip (top-level folder FaultLens/) and verifies it.

    python scripts/package.py [--out ../FaultLens.zip]

Excludes secrets, virtualenvs, node_modules, caches, build output and generated data; normalises text files
to LF (PowerShell scripts to CRLF); then re-opens the archive, checks required files, scans for secrets from
the local .env, and test-extracts it.
"""
from __future__ import annotations

import argparse
import fnmatch
import re
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXCLUDE_DIRS = {".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", "dist",
                "playwright-report", "test-results", ".git", "htmlcov", ".idea", ".vscode", "build"}
EXCLUDE_FILES = ["*.pyc", "*.pyo", ".env", "*.log", "*.partial.json", ".coverage", "coverage.xml", "FaultLens.zip", "*.egg-info", "Thumbs.db",
                 ".DS_Store", "*.tsbuildinfo"]
TEXT_EXT = {".py", ".ts", ".tsx", ".js", ".mjs", ".json", ".md", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".txt", ".html", ".css",
            ".sh", ".conf", ".mako", ".example", ".gitignore", ".gitattributes", ".dockerignore", ".svg", ""}
REQUIRED = ["README.md", "ARCHITECTURE.md", "DEVELOPMENT_STATUS.md", "TESTING.md", "SECURITY.md", "CONTRIBUTING.md",
            "API_DOCUMENTATION.md", "DATABASE_DESIGN.md", "BENCHMARKS.md", ".env.example", ".gitignore", "LICENSE",
            "docker-compose.yml", "backend/app/main.py", "backend/migrations/versions/0001_initial_schema.py",
            "backend/app/engines/rca/hypotheses.py", "services/order_service/main.py", "frontend/src/App.tsx",
            "frontend/package-lock.json", ".github/workflows/backend.yml", "docs/adr/0006-deterministic-rca.md",
            "e2e/test_e2e_flow.py", "scripts/setup.ps1", "backend/app/ml/artifacts/iforest_synthetic.joblib"]


# Stored byte-for-byte (no line-ending normalisation): rca_evaluation.json records SHA256 hashes of its source files.
VERBATIM_PREFIXES = ("e2e/results/",)


def packed_bytes(p: Path, rel: str) -> bytes:
    """Bytes written to the archive for file p (rel = POSIX path relative to ROOT)."""
    data = p.read_bytes()
    if rel.startswith(VERBATIM_PREFIXES):
        return data
    if (p.suffix in TEXT_EXT or p.name in {"Dockerfile", "LICENSE"}) and b"\0" not in data:
        data = data.replace(b"\r\n", b"\n")
        if p.suffix == ".ps1":
            data = data.replace(b"\n", b"\r\n")
    elif p.suffix == ".ps1":
        data = data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    return data


def included(rel: Path) -> bool:
    if any(part in EXCLUDE_DIRS or part.endswith(".egg-info") for part in rel.parts[:-1]):
        return False
    return not any(fnmatch.fnmatch(rel.name, pat) for pat in EXCLUDE_FILES)


def secrets_from_env() -> list[str]:
    env = ROOT / ".env"
    if not env.exists():
        return []
    vals = []
    for line in env.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            v = line.split("=", 1)[1].strip()
            if len(v) >= 16 and re.fullmatch(r"[0-9a-f]+", v):  # generated secrets are long hex strings
                vals.append(v)
    return vals


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT.parent / "FaultLens.zip"))
    out = Path(ap.parse_args().out)
    files = sorted(p for p in ROOT.rglob("*") if p.is_file() and included(p.relative_to(ROOT)))
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for p in files:
            rel = p.relative_to(ROOT).as_posix()
            data = packed_bytes(p, rel)
            info = zipfile.ZipInfo(f"FaultLens/{rel}", date_time=(2026, 11, 19, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o755 if p.suffix == ".sh" else 0o644) << 16
            z.writestr(info, data)

    # ---- verification
    problems = []
    with zipfile.ZipFile(out) as z:
        names = set(z.namelist())
        bad = z.testzip()
        if bad:
            problems.append(f"corrupt member: {bad}")
        for req in REQUIRED:
            if f"FaultLens/{req}" not in names:
                problems.append(f"missing required file: {req}")
        for n in names:
            parts = n.split("/")
            if any(x in EXCLUDE_DIRS for x in parts) or parts[-1] == ".env":
                problems.append(f"excluded path present: {n}")
        secrets = secrets_from_env()
        for n in names:
            if n.endswith((".png", ".joblib", ".woff", ".woff2")):
                continue
            text = z.read(n)
            for s in secrets:
                if s.encode() in text:
                    problems.append(f"secret value from .env found in {n}")
        with tempfile.TemporaryDirectory() as td:
            z.extractall(td)
            extracted = sum(1 for _ in Path(td, "FaultLens").rglob("*") if _.is_file())
    size_mb = out.stat().st_size / 2**20
    print(f"{out} : {len(names)} files, {size_mb:.2f} MB, extracted {extracted} files OK")
    if problems:
        print("PROBLEMS:\n  " + "\n  ".join(problems))
        return 1
    print("verification passed: required files present, no excluded paths, no .env secrets, extracts cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
