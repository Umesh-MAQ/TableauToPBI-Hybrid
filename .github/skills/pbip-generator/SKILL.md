# PBIP Generator Skill (Router)

## Purpose

Generate a valid Power BI Project (.pbip) semantic model that opens in Power BI Desktop without errors. This is a **thin router** — each rule area lives in its own focused skill so nothing is skipped. Read the focused skill for the step you are on.

## When to Use

- During PBIP semantic model generation (the implementation stage of the migration pipeline)
- When building TMDL/TMSL files, M queries, relationships, and PBIP entry files

## Read Context First

1. `.specify/memory/{WorkbookName}/star-schema-output.md` — table structure + relationships
2. `.specify/memory/{WorkbookName}/dax-measures-output.md` — measures
3. `.specify/memory/{WorkbookName}/tableau-analysis-output.md` — source connection details **and the Row-Level Security (RLS) section** (drives `roles/` generation)
4. `.specify/memory/constitution.md` — naming, M rules, PBIP structure requirements

## What to Read for Each Task (focused skills)

| Task | Read this skill |
|------|-----------------|
| PBIP folder structure + entry files (.pbip / .pbir / .pbism / report.json) | **`.github/skills/pbip-structure/SKILL.md`** |
| TMDL syntax (database/model/expressions/table/relationships, indentation, measure-line rule) | **`.github/skills/pbip-tmdl-syntax/SKILL.md`** |
| Natural-key star schema + many-to-many bridge patterns | **`.github/skills/pbip-star-schema-keys/SKILL.md`** |
| M query templates per source + M safety rules | **`.github/skills/pbip-m-queries/SKILL.md`** |
| Row-Level Security roles (`definition/roles/*.tmdl`) | **this skill — see [Row-Level Security (RLS) Roles](#row-level-security-rls-roles)** + `pbip-tmdl-syntax` |
| Validation rules (schema, model integrity, M, plugin validators) | **`.github/skills/pbip-validation/SKILL.md`** |

## Generation Order

1. Read context (above).
2. Scaffold folders + entry files -> `pbip-structure`.
3. Design keys/relationships -> `pbip-star-schema-keys`.
4. Write table/model TMDL -> `pbip-tmdl-syntax`.
5. Write partition M code -> `pbip-m-queries`.
6. Generate RLS roles (ONLY if analysis detected RLS) -> [Row-Level Security (RLS) Roles](#row-level-security-rls-roles).
7. Validate before handoff -> `pbip-validation`.

## Row-Level Security (RLS) Roles

If the Tableau analysis (`tableau-analysis-output.md` → Row-Level Security section) reports `Detected: Yes`, generate one `.tmdl` file per role under `definition/roles/`. If RLS is `Detected: No` / `None`, do NOT create a `roles/` folder.

### Tableau → Power BI RLS mapping

| Tableau construct | Power BI equivalent |
|-------------------|---------------------|
| `USERNAME()` in a calc | `USERPRINCIPALNAME()` (UPN/email) |
| `FULLNAME()` | `USERNAME()` (DOMAIN\\user) — rarely needed |
| `ISMEMBEROF('Group')` | Assign AD/Entra group to the role in the Power BI Service |
| User mapping CSV (Username→entitlement) joined to data | Dynamic RLS: filter the mapping table by current user |
| Hardcoded `USERNAME() = "x" AND value = "India"` | Static RLS: one role per value |

### Dynamic RLS (preferred — mapping table)

The mapping table (e.g. `User_Access` with `Username`, `Country`) must be a real model table joined to the secured/fact table on the entitlement column. The role filters the mapping table; the filter propagates through the relationship.

`definition/roles/{RoleName}.tmdl`:
```tmdl
role {RoleName}
	modelPermission: read

	tablePermission 'User_Access' = 'User_Access'[Username] = USERPRINCIPALNAME()
```

Required supporting model changes:
1. `tables/User_Access.tmdl` — partition loading the user-access source file (its own self-contained M query).
2. `model.tmdl` — `ref table User_Access`.
3. `relationships.tmdl` — relationship `User_Access.[Country]` → `{SecuredTable}.[Country]`. Set `crossFilteringBehavior: bothDirections` if the entitlement must flow from the mapping table to the fact rows.

### Static per-value RLS

One role per distinct entitlement value (use when there is no mapping table):
```tmdl
role India
	modelPermission: read

	tablePermission Netflix = 'Netflix'[country] = "India"
```

### Rules
- `role` declaration takes the name directly — NO `createRole`/TMSL syntax.
- `modelPermission: read` is REQUIRED.
- DAX filter is a per-row boolean predicate on the named table; it must NOT reference measures.
- Reference columns as `'Table'[Column]`.
- After generating roles, verify every table/column referenced in a `tablePermission` exists in the model and that the relationship path actually carries the filter to the fact.

> **Indentation note**: in `.tmdl` files use TABS (one per nesting level) — `modelPermission`/`tablePermission` are one tab under `role`. The spaces shown above are for readability only.

## Non-Negotiable Rules (summary — full detail in focused skills)

- Output to `Output/{WorkbookName}/`; use absolute data-file paths from `Data/{subfolder}/`.
- TMDL by default; `definition.pbism` version `4.2`; `definition.pbir` version `4.0`; `.pbip` has only a `report` artifact.
- Measure DAX on the SAME line as `measure 'Name' =`.
- Self-contained M partitions (no cross-table references); natural text keys for single-source.
- Both `Report/` and `SemanticModel/` folders required. All files UTF-8 without BOM.
- Generate `definition/roles/*.tmdl` ONLY when the analysis reports RLS `Detected: Yes`; otherwise create no `roles/` folder.

## Reference

- https://learn.microsoft.com/en-us/power-bi/developer/projects/projects-dataset

## Anti-Hallucination

- Use only real source columns/connections from the analysis output — never invent tables, columns, paths, or servers.
