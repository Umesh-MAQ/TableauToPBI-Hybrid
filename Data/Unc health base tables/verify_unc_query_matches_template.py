"""
Independent verification that executing the UNC Health Hospitals custom SQL
against the base-table CSVs reproduces the main tables (the *_DummyData.csv).

Runs the ACTUAL SQL from the two .sql files through DuckDB against the base
tables, then compares the result to the corresponding DummyData template.
"""

import os
import re
import duckdb
import pandas as pd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BT = os.path.join(BASE_DIR, "Base tables")

CASES = [
    {
        "name": "Census",
        "sql": os.path.join(BASE_DIR, "UNC_Census_CustomSQL_Fabric.sql"),
        "template": os.path.join(BASE_DIR, "UNC_Health_Hospitals_Census_DummyData.csv"),
        "table": "real_time_department_occupancy_statistics_master",
        "csv": os.path.join(BT, "real_time_department_occupancy_statistics_master.csv"),
        "sort": ["department_location_key", "valid_from_instant"],
    },
    {
        "name": "ED Census",
        "sql": os.path.join(BASE_DIR, "UNC_ED_Census_CustomSQL_Fabric.sql"),
        "template": os.path.join(BASE_DIR, "UNC_Health_Hospitals_ED_Census_DummyData.csv"),
        "table": "ed_real_time_census_master",
        "csv": os.path.join(BT, "ed_real_time_census_master.csv"),
        "sort": ["parent_location_name", "location_name",
                 "current_emergency_department_name",
                 "emergency_department_current_care_area"],
    },
]


def load_sql(path):
    with open(path, "r", encoding="utf-8") as f:
        sql = f.read()
    sql = re.sub(r"/\*.*?\*/", "", sql, count=1, flags=re.DOTALL).strip().rstrip(";")
    sql = re.sub(r"\[([^\]]+)\]", r'"\1"', sql)  # T-SQL [id] -> "id"
    return sql


def norm(df):
    """Normalize for robust comparison: lower-case col names, stringify, strip."""
    d = df.copy()
    d.columns = [c.lower() for c in d.columns]
    for c in d.columns:
        d[c] = d[c].astype(str).str.strip()
        # normalize datetime-looking values to a canonical form
    return d


def compare(case):
    con = duckdb.connect()
    con.execute("CREATE SCHEMA REPORTING_EHR;")
    path = case["csv"].replace("\\", "/")
    con.execute(
        f"CREATE VIEW REPORTING_EHR.{case['table']} AS "
        f"SELECT * FROM read_csv_auto('{path}', header=true);"
    )
    sql = load_sql(case["sql"])
    res = con.execute(sql).fetchdf()
    tpl = pd.read_csv(case["template"])

    res_n, tpl_n = norm(res), norm(tpl)

    # align columns by name (template defines the expected output)
    cols = [c.lower() for c in tpl.columns]
    res_n = res_n[cols]
    tpl_n = tpl_n[cols]

    # normalize numeric-ish and datetime columns via pandas to avoid format noise
    for c in cols:
        # try datetime
        rd = pd.to_datetime(res_n[c], errors="coerce")
        td = pd.to_datetime(tpl_n[c], errors="coerce")
        if rd.notna().all() and td.notna().all() and rd.notna().any():
            res_n[c] = rd.dt.strftime("%Y-%m-%d %H:%M:%S")
            tpl_n[c] = td.dt.strftime("%Y-%m-%d %H:%M:%S")
            continue
        # try numeric
        rn = pd.to_numeric(res_n[c], errors="coerce")
        tn = pd.to_numeric(tpl_n[c], errors="coerce")
        if rn.notna().all() and tn.notna().all():
            res_n[c] = rn
            tpl_n[c] = tn

    sort = [s.lower() for s in case["sort"]]
    res_s = res_n.sort_values(sort).reset_index(drop=True)
    tpl_s = tpl_n.sort_values(sort).reset_index(drop=True)

    print(f"\n=== {case['name']} ===")
    print(f"SQL result rows : {len(res_s)}")
    print(f"Template rows   : {len(tpl_s)}")
    print(f"Columns match   : {list(res_s.columns) == list(tpl_s.columns)}")

    if res_s.equals(tpl_s):
        print("RESULT: MATCH - SQL output over base tables exactly equals the main table.")
        return True

    print("RESULT: MISMATCH")
    if len(res_s) != len(tpl_s):
        print(f"  Row count differs: SQL={len(res_s)} vs Template={len(tpl_s)}")
    # show first few differing rows
    ne = (res_s != tpl_s)
    bad_rows = ne.any(axis=1)
    idx = list(ne.index[bad_rows])[:5]
    for i in idx:
        diff_cols = [c for c in res_s.columns if res_s.at[i, c] != tpl_s.at[i, c]]
        print(f"  Row {i} differs in {diff_cols}:")
        for c in diff_cols:
            print(f"    {c}: SQL={res_s.at[i, c]!r} vs TPL={tpl_s.at[i, c]!r}")
    return False


if __name__ == "__main__":
    ok = all(compare(c) for c in CASES)
    print("\nOVERALL:", "ALL MATCH" if ok else "DIFFERENCES FOUND")
