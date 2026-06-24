"""load_constitution.py — Stage 0 deterministic constitution loader.

Reads the two universal constitution files and writes a compact JSON cache
at Output/{PascalName}/constitution-cache.json BEFORE any parsing begins.

Rules:
- NEVER creates constitution files — if either is missing, exits with code 2.
- NEVER modifies constitution files — read-only.
- Safe to re-run: overwrites the cache each time (idempotent).

Usage (called by pipeline.py prepare):
    python scripts/load_constitution.py <output_dir>

    where <output_dir> is e.g. Output/SalesCustomerDashboards

Exit codes:
    0  — cache written successfully
    2  — one or both constitution files are missing (hard stop)
    3  — bad usage (missing argument)
"""
from __future__ import annotations

import datetime
import json
import os
import sys

SPEC_MEMORY = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", ".specify", "memory"
)

CONSTITUTION_FILES = {
    "model": "constitution.md",
    "report": "report-constitution.md",
}

CACHE_FILENAME = "constitution-cache.json"


def load(output_dir: str) -> int:
    """Read both constitution files and write constitution-cache.json."""
    paths = {
        key: os.path.join(SPEC_MEMORY, filename)
        for key, filename in CONSTITUTION_FILES.items()
    }

    # Hard-stop: both files must already exist — never create them.
    missing = [p for p in paths.values() if not os.path.isfile(p)]
    if missing:
        for p in missing:
            print(f"ERROR: constitution file missing: {os.path.abspath(p)}", file=sys.stderr)
        print(
            "\nConstitution files are a one-time workspace setup artifact.\n"
            "They must exist before running the pipeline.\n"
            "Expected locations:\n"
            "  .specify/memory/constitution.md\n"
            "  .specify/memory/report-constitution.md\n"
            "If this is a fresh workspace, copy them from the templates:\n"
            "  .specify/templates/constitution-template.md\n"
            "  .github/skills/report-visual-generation/report-constitution-template.md",
            file=sys.stderr,
        )
        return 2

    # Read both files as raw text — no LLM interpretation, no transformation.
    cache: dict = {}
    for key, path in paths.items():
        with open(path, encoding="utf-8-sig") as fh:
            cache[key] = fh.read()

    # Embed source paths and a load timestamp for auditability.
    workspace_root = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    cache["_meta"] = {
        "model_path": os.path.relpath(paths["model"], workspace_root),
        "report_path": os.path.relpath(paths["report"], workspace_root),
        "model_size_bytes": os.path.getsize(paths["model"]),
        "report_size_bytes": os.path.getsize(paths["report"]),
        "loaded_at": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    os.makedirs(output_dir, exist_ok=True)
    out_path = os.path.join(output_dir, CACHE_FILENAME)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(cache, fh, indent=2, ensure_ascii=False)

    print(
        f"Constitution cache written: {out_path}\n"
        f"  model  : {cache['_meta']['model_path']} "
        f"({cache['_meta']['model_size_bytes']} bytes)\n"
        f"  report : {cache['_meta']['report_path']} "
        f"({cache['_meta']['report_size_bytes']} bytes)"
    )
    return 0


def main(argv=None) -> int:
    argv = argv or sys.argv[1:]
    if not argv:
        print("Usage: python load_constitution.py <output_dir>", file=sys.stderr)
        return 3
    return load(argv[0])


if __name__ == "__main__":
    raise SystemExit(main())
