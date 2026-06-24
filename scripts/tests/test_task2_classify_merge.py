"""Tests for the Task 2 classification + deterministic-schema + merge layer.

Stdlib-only (unittest), matching test_pipeline.py conventions. Covers:
  * star_det   — single-flat detection, conservative gating, star hinting
  * classify   — binary routing mirrors map_dax, table map, agent-todo shape
  * merge      — fragment assembly, source normalization, orphan re-homing,
                 dedup precedence, schema validation, and reconcile integration

Run:
    python -m unittest discover -s scripts/tests -v
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
ROOT = os.path.dirname(SCRIPTS)
for sub in ("twb", "dax", "schema", "classify", "merge", "emit"):
    sys.path.insert(0, os.path.join(SCRIPTS, sub))

import parse_twb as P  # noqa: E402
import star_det as S  # noqa: E402
import classify as C  # noqa: E402
import merge_decisions as MG  # noqa: E402
import pbir_bind as BIND  # noqa: E402
import validate_bindings as VB  # noqa: E402

MERGE_PY = os.path.join(SCRIPTS, "merge", "merge_decisions.py")
# Build the IR from the committed .twb (deterministic) rather than from regenerable
# Output/ artifacts, so the test is robust to an Output cleanup.
SALES_TWB = os.path.join(ROOT, "Data", "Sales and Customer",
                         "Sales & Customer Dashboards.twb")


def _load(path: str) -> dict:
    with open(path, encoding="utf-8-sig") as fh:
        return json.load(fh)


def _single_flat_ir():
    """Minimal IR: one active CSV datasource, one table, no parameters -> single-flat."""
    return {
        "workbook": {"pascalName": "Demo"},
        "dataSources": [{
            "name": "Demo Source", "sourceType": "CSV", "active": True,
            "files": ["demo.csv"], "tables": ["demo#csv"],
        }],
        "columns": [
            {"name": "Amount", "role": "measure"},
            {"name": "Region", "role": "dimension"},
        ],
        "calculatedFields": [
            {"caption": "Total Amount", "fieldName": "[ta]", "formula": "SUM([Amount])",
             "complexity": "trivial", "suggestedDaxKind": "measure"},
            {"caption": "Running Total", "fieldName": "[rt]",
             "formula": "RUNNING_SUM(SUM([Amount]))",
             "complexity": "complex", "suggestedDaxKind": "measure"},
        ],
        "parameters": [],
    }


def _star_ir():
    """IR with multiple real source tables -> star-schema route (agent)."""
    return {
        "workbook": {"pascalName": "Shop"},
        "dataSources": [{
            "name": "Shop Source", "sourceType": "CSV", "active": True,
            "files": ["orders.csv"],
            "tables": ["Orders#csv", "Customers#csv", "Products#csv",
                       "Extract].[junk", "Custom SQL Query"],
        }],
        "columns": [{"name": "Sales", "role": "measure"}],
        "calculatedFields": [],
        "parameters": [],
    }


# --------------------------------------------------------------------------- star_det
class TestStarDetDetect(unittest.TestCase):
    def test_single_flat(self):
        det = S.detect(_single_flat_ir())
        self.assertEqual(det["strategy"], "single-flat")
        self.assertEqual(det["fact"], "demo")
        self.assertEqual(det["dimensions"], [])

    def test_star_strategy_and_fact_guess(self):
        det = S.detect(_star_ir())
        self.assertEqual(det["strategy"], "star-schema")
        self.assertEqual(det["fact"], "Orders")  # matched fact-word heuristic
        self.assertIn("Customers", det["dimensions"])
        self.assertNotIn("Custom SQL Query", det["dimensions"])

    def test_clean_source_tables_drops_junk(self):
        ds = _star_ir()["dataSources"][0]
        self.assertEqual(S.clean_source_tables(ds),
                         ["Orders", "Customers", "Products"])

    def test_multiple_active_datasources_routes_to_agent(self):
        ir = _single_flat_ir()
        ir["dataSources"].append({"name": "Second", "sourceType": "CSV",
                                  "active": True, "files": [], "tables": ["x#csv"]})
        self.assertEqual(S.detect(ir)["strategy"], "star-schema")


class TestStarDetBuild(unittest.TestCase):
    def test_build_single_flat_fragment(self):
        frag = S.build_star(_single_flat_ir())
        self.assertIsNotNone(frag)
        self.assertEqual(frag["tableStrategy"], "single-flat")
        self.assertEqual(len(frag["tables"]), 1)
        t = frag["tables"][0]
        self.assertEqual(t["role"], "fact")
        self.assertEqual(t["sourceType"], "csv")
        self.assertEqual(t["sourceFile"], "demo.csv")
        self.assertEqual(t["sourceDatasource"], "Demo Source")
        self.assertEqual(frag["relationships"], [])

    def test_star_returns_none(self):
        self.assertIsNone(S.build_star(_star_ir()))

    def test_parameters_gate_returns_none(self):
        ir = _single_flat_ir()
        ir["parameters"] = [{"name": "p"}]
        self.assertIsNone(S.build_star(ir))


# --------------------------------------------------------------------------- classify
class TestClassify(unittest.TestCase):
    def test_routing_matches_mapdax(self):
        result = C.classify(_single_flat_ir())
        routes = {m["caption"]: m["route"] for m in result["classification"]["measures"]}
        self.assertEqual(routes["Total Amount"], "deterministic")
        self.assertEqual(routes["Running Total"], "agent")

    def test_counts(self):
        cls = C.classify(_single_flat_ir())["classification"]
        self.assertEqual(cls["counts"]["deterministicMeasures"], 1)
        self.assertEqual(cls["counts"]["agentMeasures"], 1)

    def test_schema_route_single_flat_deterministic(self):
        result = C.classify(_single_flat_ir())
        self.assertEqual(result["classification"]["schema"]["route"], "deterministic")
        self.assertIsNotNone(result["schemaFragment"])

    def test_schema_route_star_is_agent(self):
        result = C.classify(_star_ir())
        self.assertEqual(result["classification"]["schema"]["route"], "agent")
        self.assertIsNone(result["schemaFragment"])

    def test_agent_todo_lists_only_agent_measures(self):
        todo = C.classify(_single_flat_ir())["agentTodo"]
        captions = [m["caption"] for m in todo["measures"]]
        self.assertEqual(captions, ["Running Total"])
        self.assertEqual(todo["measures"][0]["hint"], "table-calc")
        self.assertIn("Amount", todo["context"]["baseColumns"])

    def test_hint_tags(self):
        self.assertEqual(C._hint("{ FIXED [A] : SUM([B]) }"), "lod-fixed")
        self.assertEqual(C._hint("INDEX()"), "table-calc")
        self.assertEqual(C._hint("DATEPARSE('yyyy', [x])"), "date-fn")
        self.assertIsNone(C._hint("SUM([x])"))

    @unittest.skipUnless(os.path.isfile(SALES_TWB), "Sales workbook not present")
    def test_real_sales_routes_schema_to_agent(self):
        ir = P.build_ir(SALES_TWB)
        cls = C.classify(ir)["classification"]
        self.assertEqual(cls["schema"]["route"], "agent")
        self.assertEqual(cls["tableMap"]["strategy"], "star-schema")


# --------------------------------------------------------------------------- merge
class TestMerge(unittest.TestCase):
    def test_assembles_and_normalizes_sources(self):
        ir = _single_flat_ir()
        dax_partial = {"measures": [{
            "table": "demo", "name": "Total Amount", "dax": "SUM(demo[Amount])",
            "formatString": "0", "displayFolder": "Base Measures",
            "description": None, "source": "template"}]}
        schema_easy = S.build_star(ir)
        agent_fragment = {"measures": [{
            "table": "demo", "name": "Running Total",
            "dax": "1", "source": "deterministic"}]}  # wrong source -> normalized
        decisions = MG.merge(ir, dax_partial, schema_easy, agent_fragment)
        self.assertEqual(decisions["modelName"], "Demo")
        self.assertEqual(decisions["tableStrategy"], "single-flat")
        self.assertEqual(len(decisions["tables"]), 1)
        by_name = {m["name"]: m for m in decisions["measures"]}
        self.assertEqual(by_name["Total Amount"]["source"], "template")
        self.assertEqual(by_name["Running Total"]["source"], "llm")
        self.assertEqual(decisions["visualDecisions"], [])
        self.assertEqual(decisions["fieldParameters"], [])

    def test_orphan_measure_rehomed_to_fact(self):
        ir = _single_flat_ir()
        schema_easy = S.build_star(ir)  # fact table = "demo"
        agent_fragment = {"measures": [{
            "table": "NonExistent", "name": "Stray", "dax": "1", "source": "llm"}]}
        decisions = MG.merge(ir, {}, schema_easy, agent_fragment)
        self.assertEqual(decisions["measures"][0]["table"], "demo")

    def test_dedup_prefers_template(self):
        ir = _single_flat_ir()
        dax_partial = {"measures": [{"table": "demo", "name": "Sales",
                                     "dax": "SUM(demo[Amount])", "source": "template"}]}
        agent_fragment = {"measures": [{"table": "demo", "name": "sales",
                                        "dax": "0", "source": "llm"}]}
        decisions = MG.merge(ir, dax_partial, None, agent_fragment)
        self.assertEqual(len(decisions["measures"]), 1)
        self.assertEqual(decisions["measures"][0]["source"], "template")

    def test_agent_schema_used_when_no_schema_easy(self):
        ir = _star_ir()
        agent_fragment = {
            "tableStrategy": "star-schema",
            "tables": [{"name": "Orders", "role": "fact"},
                       {"name": "Customers", "role": "dim"}],
            "relationships": [{"fromColumn": "Orders.CID", "toColumn": "Customers.CID"}],
            "measures": [{"table": "Orders", "name": "Total Sales",
                          "dax": "SUM(Orders[Sales])", "source": "llm"}],
        }
        decisions = MG.merge(ir, {}, None, agent_fragment)
        self.assertEqual(decisions["tableStrategy"], "star-schema")
        self.assertEqual(len(decisions["tables"]), 2)
        self.assertEqual(len(decisions["relationships"]), 1)

    def test_validate_catches_missing_dax(self):
        bad = {"modelName": "X", "tableStrategy": "single-flat",
               "tables": [], "measures": [{"table": "t", "name": "m"}]}
        errors = MG.validate(bad)
        self.assertTrue(errors)

    def test_validate_passes_clean(self):
        ir = _single_flat_ir()
        decisions = MG.merge(ir, {}, S.build_star(ir), {})
        self.assertEqual(MG.validate(decisions), [])


class TestMergeCli(unittest.TestCase):
    def test_cli_writes_and_validates(self):
        ir = _single_flat_ir()
        with tempfile.TemporaryDirectory() as d:
            analysis = os.path.join(d, "analysis.json")
            with open(analysis, "w", encoding="utf-8") as fh:
                json.dump(ir, fh)
            # write fragments alongside analysis.json (the convention merge expects)
            with open(os.path.join(d, "schema-easy.json"), "w", encoding="utf-8") as fh:
                json.dump(S.build_star(ir), fh)
            with open(os.path.join(d, "agent-fragment.json"), "w", encoding="utf-8") as fh:
                json.dump({"measures": [{"table": "demo", "name": "Running Total",
                                         "dax": "1", "source": "llm"}]}, fh)
            rc = subprocess.call([sys.executable, MERGE_PY, analysis, "--skip-reconcile"])
            self.assertEqual(rc, 0)
            decisions = _load(os.path.join(d, "decisions.json"))
            names = {m["name"] for m in decisions["measures"]}
            self.assertIn("Running Total", names)


# --------------------------------------------------------------------- slicer binding
class TestSlicerEntityBinding(unittest.TestCase):
    """A synthetic date/param table (sourceDatasource=None) must NOT claim arbitrary
    fact columns. Regression for the bug where Hospital/Unit/Patient Class slicers
    were bound to DimDate (which has no such columns) instead of the fact table."""

    def _decisions(self):
        return {
            "modelName": "Census",
            "tables": [
                {"name": "Midnight_Census_Template", "role": "fact",
                 "sourceDatasource": "Midnight_Census_Template"},
                {"name": "DimDate", "role": "date", "sourceType": "calendar",
                 "sourceDatasource": None, "keyColumns": ["Date"]},
            ],
            "measures": [],
        }

    def _ir(self):
        return {"columns": [
            {"name": "Hospital", "datasource": "Midnight_Census_Template"},
            {"name": "Unit", "datasource": "Midnight_Census_Template"},
            {"name": "Patient Class", "datasource": "Midnight_Census_Template"},
            {"name": "Census Date", "datasource": "Midnight_Census_Template"},
        ]}

    def test_fact_columns_not_claimed_by_synthetic_date_table(self):
        d, ir = self._decisions(), self._ir()
        for col in ("Hospital", "Unit", "Patient Class", "Census Date"):
            self.assertEqual(BIND._field_entity(col, d, ir),
                             "Midnight_Census_Template",
                             f"{col} mis-bound to a synthetic table")

    def test_synthetic_table_still_owns_its_key_columns(self):
        d, ir = self._decisions(), self._ir()
        ir["columns"].append({"name": "Date", "datasource": None})
        self.assertEqual(BIND._field_entity("Date", d, ir), "DimDate")

    def test_resolve_slicer_binds_filter_field_to_fact(self):
        d, ir = self._decisions(), self._ir()
        zone = {"type": "filter", "worksheet": "Filters", "field": "Hospital"}
        ent, prop, _title, mode = BIND.resolve_slicer(zone, ir, d)
        self.assertEqual(ent, "Midnight_Census_Template")
        self.assertEqual(prop, "Hospital")
        self.assertEqual(mode, "Dropdown")

    def test_real_dim_table_scoped_by_datasource(self):
        """A real (non-synthetic) dim table only claims columns from its own datasource."""
        d = self._decisions()
        d["tables"].append({"name": "DimProduct", "role": "dim",
                            "sourceDatasource": "Products"})
        ir = self._ir()
        ir["columns"].append({"name": "Category", "datasource": "Products"})
        self.assertEqual(BIND._field_entity("Category", d, ir), "DimProduct")
        # a fact column must not leak into the product dim
        self.assertEqual(BIND._field_entity("Hospital", d, ir),
                         "Midnight_Census_Template")


# ----------------------------------------------------------------- binding integrity
class TestValidateBindings(unittest.TestCase):
    """The report binding guard must flag a column bound to a table that lacks it."""

    def _scaffold(self, root, entity, prop):
        """Write a 1-table model + 1-visual report binding entity.prop."""
        name = "M"
        tdir = os.path.join(root, f"{name}.SemanticModel", "definition", "tables")
        os.makedirs(tdir)
        with open(os.path.join(tdir, "Fact.tmdl"), "w", encoding="utf-8") as fh:
            fh.write("table Fact\n\tcolumn Hospital\n\t\tdataType: string\n"
                     "\tmeasure Total = SUM(Fact[X])\n")
        with open(os.path.join(tdir, "DimDate.tmdl"), "w", encoding="utf-8") as fh:
            fh.write("table DimDate\n\tcolumn Date\n\t\tdataType: dateTime\n")
        vdir = os.path.join(root, f"{name}.Report", "definition", "pages", "P", "visuals", "s1")
        os.makedirs(vdir)
        visual = {"visual": {"query": {"queryState": {"Values": {"projections": [
            {"field": {"Column": {"Expression": {"SourceRef": {"Entity": entity}},
                                  "Property": prop}}}]}}}}}
        with open(os.path.join(vdir, "visual.json"), "w", encoding="utf-8") as fh:
            json.dump(visual, fh)

    def test_clean_binding_passes(self):
        with tempfile.TemporaryDirectory() as d:
            root = os.path.join(d, "M")
            self._scaffold(root, "Fact", "Hospital")
            self.assertEqual(VB.validate(root), 0)

    def test_column_on_wrong_table_is_error(self):
        with tempfile.TemporaryDirectory() as d:
            root = os.path.join(d, "M")
            self._scaffold(root, "DimDate", "Hospital")  # Hospital not on DimDate
            self.assertEqual(VB.validate(root), 2)

    def test_unknown_entity_is_error(self):
        with tempfile.TemporaryDirectory() as d:
            root = os.path.join(d, "M")
            self._scaffold(root, "Ghost", "Hospital")
            self.assertEqual(VB.validate(root), 2)

    def test_parse_model_collects_columns_and_measures(self):
        with tempfile.TemporaryDirectory() as d:
            root = os.path.join(d, "M")
            self._scaffold(root, "Fact", "Hospital")
            ents, meas = VB.parse_model(
                os.path.join(root, "M.SemanticModel", "definition"))
            self.assertIn("Hospital", ents["Fact"])
            self.assertIn("Total", meas)
            self.assertIn("Date", ents["DimDate"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
