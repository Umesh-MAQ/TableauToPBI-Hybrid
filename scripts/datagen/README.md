# datagen — dummy datasource & schema for a Tableau report

Some Tableau workbooks ship **without their source data** (e.g.
`Data/Netflix/Netfix Workbook.twb` references `netflix_titles.csv`, but the CSV is
not in the folder). This package reconstructs a **dummy datasource + schema** for
such a report so the migration pipeline has something to bind against.

It follows the repo's hybrid pattern: deterministic code does the structural work,
and the AI only invents realistic *values*.

## The three stages (one folder, one job each)

| Stage | File | Kind | Input → Output |
|-------|------|------|----------------|
| 1. Extraction | [extract.py](extract.py) | **deterministic** | `.twb` → `extracted.json` (physical columns, types, tables, domains) |
| 2. Schema | [schema.py](schema.py) | **deterministic** | `extracted.json` → `schema.json` (canonical types + inferred semantics) |
| 3. Dummy data | [dummy_data.py](dummy_data.py) | **agentic** | `schema.json` → `dummy-todo.json` (agent task) → `dummy-fragment.json` (AI) → `*.csv` |

### How the agentic stage works

1. `dummy_data.py plan` writes **`dummy-todo.json`** — a compact task envelope. For
   every column that needs human-like values (categories, ratings, names, …) it
   asks the agent for a realistic value pool or a numeric/date range.
2. The **AI agent** writes exactly one file, **`dummy-fragment.json`**:
   ```json
   { "netflix_titles.csv": {
       "type":   { "values": ["Movie", "TV Show"] },
       "rating": { "values": ["TV-MA", "PG-13", "R", "TV-14"] },
       "release_year": { "min": 1990, "max": 2024 }
   } }
   ```
3. `dummy_data.py build` merges the fragment with the schema and writes **seeded,
   type-aware CSVs**. If no fragment exists it still produces a valid CSV using
   built-in fallback generators — the AI only improves realism, it is never required.

## Quick start

```powershell
# Stage 1 + 2 (deterministic)
python scripts/datagen/extract.py "Data/Netflix"
python scripts/datagen/schema.py  "Output/datagen/NetfixWorkbook" --rows 150

# Stage 3 (agentic): emit the task, let the agent fill dummy-fragment.json, then build
python scripts/datagen/dummy_data.py plan  "Output/datagen/NetfixWorkbook"
#   -> agent writes Output/datagen/NetfixWorkbook/dummy-fragment.json
python scripts/datagen/dummy_data.py build "Output/datagen/NetfixWorkbook" --write-beside-source
```

Or run all deterministic stages at once:

```powershell
python scripts/datagen/run_all.py "Data/Netflix" --rows 150
```

## Outputs (under `Output/datagen/<PascalName>/`)

- `extracted.json` — raw structural extraction
- `schema.json` — generator-ready schema
- `dummy-todo.json` — AI task envelope
- `dummy-fragment.json` — **AI-authored** value pools (the only AI artifact)
- `data/<file>.csv` — the generated dummy datasource (also copied beside the
  `.twb` when `--write-beside-source` is used)

## Design rules

- Stages 1 & 2 never guess values — they only read structure. Fully reproducible.
- Stage 3 is seeded (`--seed`, default 42) so the same schema + fragment always
  yields the same CSV.
- The AI authors **one** file (`dummy-fragment.json`); everything else is code.
