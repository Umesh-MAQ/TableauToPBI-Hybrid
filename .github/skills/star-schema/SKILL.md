# Star Schema Design Skill

## Purpose

Transform Tableau's flat/denormalized datasources into a proper star schema for Power BI semantic models with fact tables, dimension tables, and relationships.

## When to Use

- Decomposing denormalized Tableau data into star schema
- Designing Power BI relationships from Tableau joins
- Creating fact/dimension table split for semantic models

## Reference

- https://learn.microsoft.com/en-us/power-bi/guidance/star-schema

## Star Schema Principles

### Fact Tables
- Store observations/events/transactions
- Contain dimension key columns (FKs) + numeric measure columns
- Grain = one row per event (e.g., one loan, one sale, one transaction)
- Large row counts, growing over time
- Enable summarization (SUM, COUNT, AVG on measures)

### Dimension Tables
- Store business entities (who, what, where, when)
- Contain a unique key column + descriptive attributes
- Small row counts relative to facts
- Enable filtering and grouping
- Examples: Date, Customer, Product, Geography, Status

### Relationships
- Always one-to-many: dimension (one) → fact (many)
- Single cross-filter direction: dimension filters fact
- One active relationship per table pair
- Use USERELATIONSHIP() for inactive relationships
- Many-to-many: use bridging/factless fact tables

### Special Dimension Types
| Type | Use Case |
|------|----------|
| Role-playing | Same dim used multiple ways (OrderDate, ShipDate) — create copies |
| Junk | Consolidate low-cardinality flags into one table |
| Degenerate | Single attribute stays in fact table (e.g., order number) |
| Slowly Changing | Track historical changes with start/end dates |
| Snowflake | Avoid — prefer denormalized single dimension tables |

### Date Dimension
- ALWAYS required if any date/time fields exist
- Contains: DateKey, Date, Year, Quarter, Month, MonthName, Day, DayOfWeek, WeekNumber
- Mark as Date table in Power BI
- Use continuous date range (no gaps)

## Decomposition Strategy

Given a Tableau flat datasource:
1. Identify grain (what does one row represent?)
2. Separate numeric aggregatable columns → fact table measures
3. Separate descriptive/categorical columns → dimension tables
4. Determine key strategy (see below)
5. Handle repeated column groups as separate dimensions
6. Create Date dimension from date fields

### Key Strategy: Natural Keys vs Surrogate Keys

**CRITICAL: If all tables derive from a SINGLE source file/table, use NATURAL KEYS:**

- The dimension key = the distinct text values from the source column (e.g., `TypeName`, `RatingName`, `CountryName`)
- The fact table keeps the original columns as-is — they ARE the foreign keys
- Relationships: `FactTable.[original_column]` → `DimTable.[NaturalKey]`
- Each table loads the source independently (no cross-table M query references)
- This avoids circular dependencies that prevent data loading in Power BI TMSL

**Only use surrogate integer keys when:**
- Multiple independent source tables already exist (no need to split)
- The natural key is too long or complex (composite keys)
- Source data has quality issues requiring key mapping

**For many-to-many (comma-separated values):**
- Create a Bridge table that splits values independently from the same source
- Bridge has: `fact_key` (natural) + `dim_natural_key` (text)
- NO cross-table M references needed

## Row-Level Security (RLS) Mapping Table

If the Tableau analysis reports RLS `Detected: Yes` with a **Dynamic** (mapping-table) pattern, the user-mapping table must be part of the star schema:

- Include the user-mapping/entitlement table (e.g. `User_Access` with `Username`, `Country`) as a **real model table** with its own partition.
- Define a security relationship `User_Access.[entitlement]` → `{SecuredTable}.[entitlement]` (e.g. `User_Access.[Country]` → `FactSales.[Country]`, or to a shared dimension that reaches the fact).
- Set `crossFilteringBehavior: bothDirections` on that relationship when the user filter must propagate from the mapping table through to the fact rows.
- The mapping table is NOT a normal analytical dimension — it exists only to drive the role filter (`'User_Access'[Username] = USERPRINCIPALNAME()`). Keep it out of report slicers.
- For **Static** per-value RLS (no mapping table), no schema change is needed — one role per value filters the secured table directly.

Record any RLS mapping table, its user/entitlement columns, and the security relationship in the star-schema output so the PBIP stage can wire roles and relationships.

## Output

Save to `.specify/memory/star-schema-output.md` with fact tables, dimension tables, relationships, and decomposition notes.
