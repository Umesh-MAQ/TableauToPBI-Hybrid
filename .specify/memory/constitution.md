# Tableau → Power BI Migration Constitution

## Core Principles

### I. Fidelity to Source (NON-NEGOTIABLE)
Every artifact generated MUST trace to a concrete element extracted from the source
Tableau workbook. Never invent tables, columns, measures, relationships, or visuals.
When the source is ambiguous, mark the decision `UNVERIFIED` rather than fabricating.

### II. Star Schema First
The semantic model MUST follow Power BI star-schema guidance: a fact table surrounded
by conformed dimension tables, single-direction relationships from dimension → fact,
and a dedicated Date dimension marked with `dataCategory: Time`. Avoid snowflaking
unless the source explicitly requires it.

### III. DAX Correctness
Tableau calculated fields translate to DAX measures. Simple row/aggregate calcs are
mapped deterministically; table calculations (LOOKUP, WINDOW_*, RANK, INDEX) and
LOD expressions require explicit DAX intent recorded in `decisions.json`. CALCULATE
boolean filters use the VAR pattern; never reference a measure directly inside a
boolean filter argument.

### IV. Format Preservation
Number, currency, percentage, and date formats from the source are preserved on
columns and measures. Percentages such as Tableau `p0.00%` map to DAX format string
`0.00%`.

### V. Validation Before Delivery
No generated PBIP project is considered complete until it passes the TMDL structural
linter, the PBIP cross-cutting validator, the binding validator, and the semantic
validator. Errors must be fixed before the output is presented.

## Technology Standards

- Output format: PBIP (Power BI Project) — `.pbip` + `.SemanticModel/` (TMDL) + `.Report/` (PBIR JSON)
- Storage mode: Import for CSV/Excel sources
- M queries: `File.Contents()` with absolute paths for local file sources
- Naming: PascalCase model/table names, descriptive measure names matching Tableau captions

## Governance
This constitution supersedes ad-hoc choices during migration. Every stage of the
pipeline (analysis → spec → DAX/schema → plan → tasks → model → report → validation)
must comply. Complexity must be justified and recorded in `decisions.json`.

**Version**: 1.0.0 | **Ratified**: 2026-06-22 | **Last Amended**: 2026-06-22
