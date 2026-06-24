# PBIP Star Schema Keys Skill

## Purpose

The natural-key star schema pattern for single-source workbooks and the many-to-many bridge pattern. Single-responsibility companion to the PBIP generation pipeline. Prevents circular M dependencies that block data loading.

## When to Use

- During PBIP generation, when building dimension/fact tables and relationships
- Whenever the workbook uses a single data source or has comma-separated multi-value fields

## Natural Key Star Schema (single source)

When the workbook uses ONE data source (one CSV or one DB table), use natural keys:

1. **NEVER use `Table.NestedJoin` referencing other table names** in M partitions — this creates circular TMSL dependencies that prevent loading.
2. **Each table loads the SAME source independently** — every partition reads the source on its own.
3. **Use absolute file paths** in `File.Contents()`.
4. **Relationships use natural keys (text values)** — NOT surrogate integers. The dimension key = distinct text values from the source column.
5. **Fact table keeps all original columns** — relationships use existing text columns directly.
6. **DimDate** is generated via M (no file dependency) for time intelligence.
7. **All measures go on the fact table.**

### Per-Dimension Pattern
```
Dimension M Query:
  1. Load source file independently
  2. Select the dimension column
  3. Get distinct values
  4. Remove nulls/blanks
  5. Rename to DimName (e.g. "TypeName", "RatingName")
  6. Optionally add enrichment columns (e.g. RatingCategory)

Relationship:
  FactTable.[original_column] → DimTable.[DimKey]   (natural text key)
```

### Why Natural Keys Work
- No cross-table M dependencies (every query is self-contained).
- VertiPaq compresses text keys efficiently.
- Dimension "to" side is always unique (guaranteed by DISTINCT).
- No fragile surrogate-key generation to keep in sync.

## Multi-Source Dimensions (separate file/table per dimension)

When EACH dimension comes from its OWN source (e.g. `Customers.csv`, `Location.csv`, `Products.csv` plus a fact `Orders.csv`), the dimension key is NOT built via `DISTINCT` of a fact column — it is read directly from the dimension export. These exports are often denormalized and **contain duplicate keys**, so each dimension MUST be deduplicated on its key:

1. **Each dimension loads its own source file independently.**
2. **The key column is the relationship "one" side** (e.g. `Customer ID`, `Postal Code`, `Product ID`).
3. **End every dimension M query with `Table.Distinct(PreviousStep, {"KeyColumn"})`** — keeps the first row per distinct key. (M code: see **`pbip-m-queries`** → "Dimension key dedup".)
4. **The fact table (many-side) is NOT deduplicated** — it keeps every row.

> A duplicate key on the one-side throws "Column contains a duplicate value and this is not allowed for columns on the one side of a many-to-one relationship" and cascades to "Load was cancelled by an error in loading a previous table" for all other tables. Validators do NOT detect this; only Power BI Desktop does. ALWAYS dedup dimension keys.

## Many-to-Many (comma-separated values)

```
Bridge M Query:
  1. Load source file independently
  2. Select fact_key + multi-value column
  3. Split multi-value column by delimiter
  4. Trim whitespace
  5. Remove nulls

Relationships:
  BridgeTable.[fact_key]        → FactTable.[fact_key]
  BridgeTable.[dim_natural_key] → DimTable.[dim_natural_key]
```

> The M code for splitting/expanding multi-value columns lives in **`pbip-m-queries`**.

## Key Strategy by Source Count

- **Single source** → natural text keys (above).
- **Multi-source / joins** → keep existing table structure with the existing join keys.

## Relationship Definition

- Define each relationship: from table/column, to table/column, direction (`oneDirection` default), cardinality (inferred — do not specify).
- Relationship "to" columns MUST contain unique values (use keys, not repeated attributes).
- Always create `DimDate` if any date fields exist.

## Anti-Hallucination

- Derive dimensions only from real source columns; never invent dimension tables or enrichment values.
