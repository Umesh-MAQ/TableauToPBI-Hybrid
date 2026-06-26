"""Post-generate validation subsystem.

Compares the source Tableau workbook (IR) against the generated Power BI
project and produces a SINGLE Excel validation workbook that accumulates every
iteration of the migration lifecycle, with side-by-side Tableau vs Power BI
screenshot evidence embedded directly in the sheets.

Entry point: ``scripts/validate/validate_migration.py`` (CLI), also reachable
through ``python scripts/migrate.py validate <folder>``.
"""
