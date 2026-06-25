"""Regression tests for the deterministic migration engine.

Stdlib-only (unittest) so they run with `python -m unittest` and no extra deps.
Covers the pure logic-heavy functions plus golden-file regression of the
committed Midnight Census artifacts (the design doc's acceptance gate: the
deterministic path must reproduce the committed Output/ artifacts).

Run:
    python -m unittest discover -s scripts/tests -v
    python scripts/tests/test_pipeline.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
ROOT = os.path.dirname(SCRIPTS)
for sub in ("twb", "dax", "emit", "merge"):
    sys.path.insert(0, os.path.join(SCRIPTS, sub))
sys.path.insert(0, SCRIPTS)

import twb_xml as X  # noqa: E402
import twb_datasources as DS  # noqa: E402
import twb_meta as TM  # noqa: E402
import tmdl_blocks as B  # noqa: E402
import map_dax as M  # noqa: E402
import reconcile as R  # noqa: E402
import emit_tmdl as ET  # noqa: E402
import parse_twb as P  # noqa: E402
import twb_visuals as V  # noqa: E402
import pbir_blocks as PB  # noqa: E402
import emit_pbir as EP  # noqa: E402
import feature_audit as FA  # noqa: E402
import mark_infer as MI  # noqa: E402
import merge_decisions as MD  # noqa: E402
import validate_semantics as VS  # noqa: E402
import pbir_bind as PBB  # noqa: E402
import migrate as MG  # noqa: E402
import screenshot_overlay as SO  # noqa: E402
SALES_TWB = os.path.join(ROOT, "Data", "Sales and Customer", "Sales & Customer Dashboards.twb")

MIDNIGHT_TWB = os.path.join(ROOT, "Data", "Midnight Census", "Midnight Census Dashboard.twb")
MIDNIGHT_OUT = os.path.join(ROOT, "Output", "MidnightCensusDashboard")


def _load(path: str) -> dict:
    with open(path, encoding="utf-8-sig") as fh:
        return json.load(fh)


class TestPascalCase(unittest.TestCase):
    def test_spaces_and_punctuation(self):
        self.assertEqual(X.to_pascal_case("Midnight Census Dashboard"), "MidnightCensusDashboard")
        self.assertEqual(X.to_pascal_case("Sales & Customer Dashboards"), "SalesCustomerDashboards")
        self.assertEqual(X.to_pascal_case("(Active) 2021 Q3 Dealer"), "Active2021Q3Dealer")

    def test_empty_falls_back(self):
        self.assertEqual(X.to_pascal_case(""), "Model")
        self.assertEqual(X.to_pascal_case("___"), "Model")


class TestDecodeEntities(unittest.TestCase):
    def test_newline_sequences(self):
        self.assertEqual(X.decode_entities("a&#13;&#10;b"), "a\nb")
        self.assertEqual(X.decode_entities("a&#10;b"), "a\nb")

    def test_residual_xml_entities(self):
        self.assertEqual(X.decode_entities("&lt;x&gt; &amp; &quot;y&quot;"), '<x> & "y"')

    def test_none_is_empty(self):
        self.assertEqual(X.decode_entities(None), "")


class TestDaxTranslate(unittest.TestCase):
    def test_single_aggregations(self):
        self.assertEqual(M.translate("SUM([Sales])", "T"), ("SUM ( T[Sales] )", "#,0"))
        self.assertEqual(M.translate("AVG([Sales])", "T"), ("AVERAGE ( T[Sales] )", "#,0.00"))
        dax, fmt = M.translate("COUNTD([Id])", "T")
        self.assertEqual(dax, "DISTINCTCOUNT ( T[Id] )")
        self.assertEqual(fmt, "#,0")

    def test_population_stats(self):
        # longer keywords must win over their shorter prefixes
        self.assertEqual(M.translate("STDEVP([Sales])", "T"), ("STDEV.P ( T[Sales] )", None))
        self.assertEqual(M.translate("VARP([Sales])", "T"), ("VAR.P ( T[Sales] )", None))
        self.assertEqual(M.translate("STDEV([Sales])", "T"), ("STDEV.S ( T[Sales] )", None))
        self.assertEqual(M.translate("VAR([Sales])", "T"), ("VAR.S ( T[Sales] )", None))

    def test_attr_selectedvalue(self):
        cols = {"Region"}
        self.assertEqual(
            M.translate("ATTR([Region])", "T", cols),
            ("SELECTEDVALUE ( T[Region] )", None),
        )
        # references a non-base-column token -> bail to the agent
        self.assertIsNone(M.translate("ATTR([Calc_1])", "T", cols))

    def test_ratio(self):
        dax, fmt = M.translate("SUM([a]) / SUM([b])", "T")
        self.assertEqual(dax, "DIVIDE ( SUM ( T[a] ), SUM ( T[b] ) )")
        self.assertEqual(fmt, "#,0.00")

    def test_passthrough(self):
        self.assertEqual(M.translate("[Region]", "T"), ("T[Region]", None))

    def test_parameter_ref_becomes_selectedvalue(self):
        # A Tableau parameter-alias calc ([Select Year]) must become a scalar
        # SELECTEDVALUE measure, NOT a bare-column reference (invalid measure DAX).
        params = {"Select Year": {"name": "Select Year", "dataType": "integer",
                                  "default": "2023"}}
        self.assertEqual(
            M.translate("[Select Year]", "Orders", set(), {}, params),
            ("SELECTEDVALUE ( 'Select Year'[Select Year], 2023 )", "0"),
        )
        # trailing scalar arithmetic ([Select Year]-1 -> Previous Year)
        self.assertEqual(
            M.translate("[Select Year]-1", "Orders", set(), {}, params),
            ("SELECTEDVALUE ( 'Select Year'[Select Year], 2023 ) - 1", "0"),
        )

    def test_parameter_ref_string_default_quoted(self):
        params = {"Region Param": {"name": "Region Param", "dataType": "string",
                                   "default": "East"}}
        self.assertEqual(
            M.translate("[Region Param]", "T", set(), {}, params),
            ("SELECTEDVALUE ( 'Region Param'[Region Param], \"East\" )", None),
        )

    def test_param_token_not_bare_column_measure(self):
        # Without a matching parameter, a bracket token that is NOT a base column
        # must NOT become a bare-column measure -> defers (None) instead of the old
        # invalid "Orders[Select Year]".
        self.assertIsNone(M.translate("[Select Year]", "Orders", {"Sales"}, {}))

    def test_complex_returns_none(self):
        for f in ("{FIXED [a] : SUM([b])}", "WINDOW_SUM(SUM([x]))",
                  "RUNNING_SUM(SUM([x]))", "INDEX()", "DATEADD('day', -1, [d])",
                  "CASE [p] WHEN 'a' THEN 1 END"):
            self.assertIsNone(M.translate(f, "T"), msg=f)

    def test_ratio_general_gated(self):
        cols = {"Order ID", "Sales"}
        # any-aggregation ratio fires only when both fields are base columns
        dax, fmt = M.translate("COUNTD([Order ID]) / SUM([Sales])", "T", cols)
        self.assertEqual(dax, "DIVIDE ( DISTINCTCOUNT ( T[Order ID] ), SUM ( T[Sales] ) )")
        self.assertEqual(fmt, "#,0.00")
        # no column set -> not safe to translate
        self.assertIsNone(M.translate("COUNTD([Order ID]) / SUM([Sales])", "T"))
        # referenced field is not a base column (e.g. a calc-field token) -> bail
        self.assertIsNone(
            M.translate("COUNTD([CY (copy)_1]) / SUM([Sales])", "T", cols))

    def test_agg_arithmetic_gated(self):
        cols = {"Sales", "Profit"}
        self.assertEqual(
            M.translate("SUM([Sales]) - SUM([Profit])", "T", cols),
            ("SUM ( T[Sales] ) - SUM ( T[Profit] )", None),
        )
        # %-difference shape
        self.assertEqual(
            M.translate("(SUM([Sales]) - SUM([Profit])) / SUM([Profit])", "T", cols),
            ("(SUM ( T[Sales] ) - SUM ( T[Profit] )) / SUM ( T[Profit] )", "#,0.00"),
        )
        # constant scaling of a single aggregation
        self.assertEqual(
            M.translate("SUM([Sales]) * 100", "T", cols),
            ("SUM ( T[Sales] ) * 100", None),
        )
        # any non-base-column reference -> bail to the agent
        self.assertIsNone(
            M.translate("SUM([CY (copy)_1]) - SUM([Profit])", "T", cols))
        # stray logic / identifier in the residual -> not pure arithmetic -> bail
        self.assertIsNone(
            M.translate("SUM([Sales]) - [Profit]", "T", cols))


class TestDaxExpression(unittest.TestCase):
    COLS = {"Sales", "Profit", "Region", "Name", "Order Date", "Qty"}

    def t(self, formula):
        return M.translate(formula, "T", self.COLS)

    def test_if_simple(self):
        self.assertEqual(
            self.t("IF SUM([Sales]) > 0 THEN SUM([Profit]) ELSE 0 END"),
            ("IF ( ( SUM ( T[Sales] ) > 0 ), SUM ( T[Profit] ), 0 )", None),
        )

    def test_if_no_else(self):
        self.assertEqual(
            self.t("IF SUM([Qty]) > 0 THEN SUM([Sales]) END"),
            ("IF ( ( SUM ( T[Qty] ) > 0 ), SUM ( T[Sales] ) )", None),
        )

    def test_if_elseif_chain(self):
        dax, _ = self.t(
            "IF SUM([Qty]) > 10 THEN 'A' "
            "ELSEIF SUM([Qty]) > 5 THEN 'B' ELSE 'C' END")
        self.assertEqual(
            dax,
            'IF ( ( SUM ( T[Qty] ) > 10 ), "A", '
            'IF ( ( SUM ( T[Qty] ) > 5 ), "B", "C" ) )')

    def test_iif(self):
        dax, _ = self.t("IIF(SUM([Qty]) > 0, SUM([Sales]), 0)")
        self.assertEqual(dax, "IF ( ( SUM ( T[Qty] ) > 0 ), SUM ( T[Sales] ), 0 )")

    def test_case_to_switch(self):
        dax, _ = self.t(
            "CASE ATTR([Region]) WHEN 'N' THEN 1 WHEN 'S' THEN 2 ELSE 0 END")
        self.assertEqual(
            dax, 'SWITCH ( SELECTEDVALUE ( T[Region] ), "N", 1, "S", 2, 0 )')

    def test_logical_and_comparison(self):
        dax, _ = self.t(
            "IF SUM([Qty]) > 0 AND SUM([Sales]) > 100 THEN 1 ELSE 0 END")
        self.assertEqual(
            dax,
            "IF ( ( ( SUM ( T[Qty] ) > 0 ) && ( SUM ( T[Sales] ) > 100 ) ), 1, 0 )")

    def test_string_functions(self):
        self.assertEqual(
            self.t("LEFT(ATTR([Name]), 3)"),
            ("LEFT ( SELECTEDVALUE ( T[Name] ), 3 )", None))
        self.assertEqual(
            self.t("UPPER(ATTR([Name]))"),
            ("UPPER ( SELECTEDVALUE ( T[Name] ) )", None))

    def test_date_functions(self):
        self.assertEqual(
            self.t("YEAR(MAX([Order Date]))"),
            ("YEAR ( MAX ( T[Order Date] ) )", None))
        dax, _ = self.t("DATEDIFF('day', MIN([Order Date]), MAX([Order Date]))")
        self.assertEqual(
            dax, "DATEDIFF ( MIN ( T[Order Date] ), MAX ( T[Order Date] ), DAY )")

    def test_trig_functions(self):
        """Trig functions map 1:1 to the same-named DAX functions."""
        self.assertEqual(
            self.t("SIN(SUM([Qty]))"), ("SIN ( SUM ( T[Qty] ) )", None))
        self.assertEqual(
            self.t("DEGREES(SUM([Qty]))"), ("DEGREES ( SUM ( T[Qty] ) )", None))
        self.assertEqual(
            self.t("RADIANS(SUM([Qty]))"), ("RADIANS ( SUM ( T[Qty] ) )", None))

    def test_pi_constant_with_field(self):
        """PI() translates only when the formula also references a base column."""
        self.assertEqual(
            self.t("SUM([Qty]) * PI()"), ("( SUM ( T[Qty] ) * PI ( ) )", None))

    def test_makedate(self):
        """MAKEDATE(y, m, d) becomes DATE(y, m, d) with identical arg order."""
        self.assertEqual(
            self.t("MAKEDATE(YEAR(MAX([Order Date])), 1, 1)"),
            ("DATE ( YEAR ( MAX ( T[Order Date] ) ), 1, 1 )", None))

    def test_conversion_and_null(self):
        self.assertEqual(self.t("INT(SUM([Sales]))"), ("INT ( SUM ( T[Sales] ) )", None))
        self.assertEqual(
            self.t("ZN(SUM([Profit]))"), ("COALESCE ( SUM ( T[Profit] ), 0 )", None))
        self.assertEqual(
            self.t("ISNULL(SUM([Profit]))"), ("ISBLANK ( SUM ( T[Profit] ) )", None))

    def test_modulo(self):
        self.assertEqual(self.t("SUM([Qty]) % 2"), ("MOD ( SUM ( T[Qty] ), 2 )", None))

    def test_gating_parameter_ref_bails(self):
        # parameter-qualified refs are never base columns -> defer to agent
        self.assertIsNone(self.t(
            "CASE [Parameters].[P] WHEN 'a' THEN SUM([Sales]) END"))

    def test_gating_non_base_column_bails(self):
        self.assertIsNone(self.t("IF SUM([Calc_1]) > 0 THEN SUM([Sales]) END"))

    def test_gating_bare_column_bails(self):
        # a column outside an aggregation is invalid in a measure -> bail
        self.assertIsNone(self.t("IF [Qty] > 0 THEN [Sales] END"))
        self.assertIsNone(self.t("UPPER([Name])"))

    def test_gating_pure_constant_bails(self):
        # no base-column reference -> not translated (protects literal calcs)
        self.assertIsNone(M.translate('"(All)"', "T", self.COLS))
        self.assertIsNone(M.translate("TODAY() - 1", "T", self.COLS))

    def test_gating_string_concat_bails(self):
        # Tableau '+' on strings is ambiguous concat -> defer to agent
        self.assertIsNone(self.t("ATTR([Name]) + 'x'"))

    def test_gating_unknown_function_bails(self):
        self.assertIsNone(self.t("SPLIT(ATTR([Name]), ',', 1)"))

    def test_gating_no_columns_arg_bails(self):
        # without a column set the expression handler cannot run safely
        self.assertIsNone(M.translate("IF SUM([Qty]) > 0 THEN 1 ELSE 0 END", "T"))


class TestDaxMeasureRefs(unittest.TestCase):
    """References to sibling calc fields that become measures -> [Measure] refs."""

    COLS = {"Sales", "Profit", "Qty"}
    # internal Tableau name (no brackets) -> DAX measure name it becomes
    MEAS = {"CY Sales (copy)_1": "CY Sales", "PY Sales (copy)_2": "PY Sales"}

    def t(self, formula):
        return M.translate(formula, "T", self.COLS, self.MEAS)

    def test_measure_minus_measure_ratio(self):
        # the dominant KPI pattern: arithmetic over other measures
        dax, _ = self.t(
            "([CY Sales (copy)_1] - [PY Sales (copy)_2]) / [PY Sales (copy)_2]")
        self.assertEqual(dax, "( ( [CY Sales] - [PY Sales] ) / [PY Sales] )")

    def test_measure_ref_in_condition(self):
        dax, _ = self.t(
            "IF [CY Sales (copy)_1] > [PY Sales (copy)_2] THEN 1 ELSE 0 END")
        self.assertEqual(dax, "IF ( ( [CY Sales] > [PY Sales] ), 1, 0 )")

    def test_measure_mixed_with_base_aggregation(self):
        dax, _ = self.t("[CY Sales (copy)_1] - SUM([Profit])")
        self.assertEqual(dax, "( [CY Sales] - SUM ( T[Profit] ) )")

    def test_aggregating_a_measure_bails(self):
        # SUM([measure]) is invalid DAX -> defer to the agent
        self.assertIsNone(self.t("SUM([CY Sales (copy)_1]) - SUM([Profit])"))

    def test_unknown_calc_ref_still_bails(self):
        # a calc-field token that is NOT a known measure is still rejected
        self.assertIsNone(self.t("[CY Sales (copy)_1] - [Unknown_99]"))


class TestBuildMeasures(unittest.TestCase):
    def test_split_translated_vs_pending(self):
        ir = {
            "workbook": {"pascalName": "T"},
            "dataSources": [{"active": True}],
            "calculatedFields": [
                {"caption": "Total", "formula": "SUM([Amt])",
                 "complexity": "trivial", "suggestedDaxKind": "measure"},
                {"caption": "LOD", "formula": "{FIXED [k]: SUM([Amt])}",
                 "complexity": "complex", "suggestedDaxKind": "measure"},
            ],
        }
        out = M.build_measures(ir, "T")
        self.assertEqual(len(out["measures"]), 1)
        self.assertEqual(out["measures"][0]["name"], "Total")
        self.assertEqual(out["measures"][0]["source"], "template")
        self.assertEqual(len(out["pending"]), 1)
        self.assertEqual(out["pending"][0]["caption"], "LOD")

    def test_measure_reference_resolved_via_fieldname(self):
        # a calc field that references sibling measures by their (scrambled)
        # internal Tableau name translates to bare [Measure] references.
        ir = {
            "workbook": {"pascalName": "T"},
            "dataSources": [{"active": True}],
            "calculatedFields": [
                {"caption": "CY Sales", "fieldName": "[Calc_aaa]",
                 "formula": "SUM([Sales])", "complexity": "trivial",
                 "suggestedDaxKind": "measure"},
                {"caption": "PY Sales", "fieldName": "[Calc_bbb]",
                 "formula": "SUM([Sales])", "complexity": "trivial",
                 "suggestedDaxKind": "measure"},
                {"caption": "% Diff", "fieldName": "[Calc_ccc]",
                 "formula": "([Calc_aaa] - [Calc_bbb]) / [Calc_bbb]",
                 "complexity": "trivial", "suggestedDaxKind": "measure"},
            ],
        }
        out = M.build_measures(ir, "T")
        by_name = {m["name"]: m["dax"] for m in out["measures"]}
        self.assertEqual(
            by_name["% Diff"], "( ( [CY Sales] - [PY Sales] ) / [PY Sales] )")


class TestReconcile(unittest.TestCase):
    PARTIAL = {
        "measures": [{"table": "T", "name": "Total Sales", "dax": "SUM(T[Sales])"}],
        "pending": [
            {"caption": "KPI Avg", "formula": "WINDOW_AVG(SUM([x]))",
             "complexity": "complex", "suggestedDaxKind": "measure"},
            {"caption": "Current Year", "formula": "YEAR(TODAY())",
             "complexity": "trivial", "suggestedDaxKind": "column"},
            {"caption": "PY Sales", "formula": "...",
             "complexity": "trivial", "suggestedDaxKind": "measure"},
        ],
    }

    def test_missing_measure_flagged(self):
        # 'PY Sales' authored, 'KPI Avg' not; column-kind 'Current Year' ignored.
        decisions = {"measures": [{"name": "PY Sales"}], "tables": []}
        missing = R.find_missing(self.PARTIAL, decisions)
        self.assertEqual([m["caption"] for m in missing], ["KPI Avg"])

    def test_name_match_is_normalized(self):
        # 'KPI Avg' present as 'kpi avg' (punctuation/case differ) -> accounted.
        decisions = {"measures": [{"name": "kpi avg"}, {"name": "PY Sales"}],
                     "tables": []}
        self.assertEqual(R.find_missing(self.PARTIAL, decisions), [])

    def test_deterministic_measure_not_flagged(self):
        # the dax-partial deterministic measure is folded in by emit -> never missing
        partial = {"measures": [{"name": "Total Sales"}],
                   "pending": [{"caption": "Total Sales", "formula": "SUM([x])",
                                "complexity": "trivial", "suggestedDaxKind": "measure"}]}
        self.assertEqual(R.find_missing(partial, {"measures": [], "tables": []}), [])

    def test_accounted_across_channels(self):
        decisions = {
            "measures": [], "tables": [{"name": "P", "role": "param"}],
            "calculatedColumns": [{"name": "Current Year"}],
            "fieldParameters": [{"name": "KPI Avg"}],
        }
        # KPI Avg via fieldParameters, PY Sales still missing
        missing = R.find_missing(self.PARTIAL, decisions)
        self.assertEqual([m["caption"] for m in missing], ["PY Sales"])


class TestMergePartialMeasures(unittest.TestCase):
    def _run(self, decisions, partial):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            analysis = os.path.join(d, "analysis.json")
            with open(analysis, "w", encoding="utf-8") as fh:
                json.dump({}, fh)
            with open(os.path.join(d, "dax-partial.json"), "w", encoding="utf-8") as fh:
                json.dump(partial, fh)
            return ET.merge_partial_measures(decisions, analysis)

    def test_deterministic_measure_merged_onto_fact(self):
        decisions = {"measures": [], "tables": [{"name": "Orders", "role": "fact"}]}
        partial = {"measures": [{"table": "Wb", "name": "Avg X",
                                 "dax": "AVERAGE(Orders[X])", "formatString": None}]}
        out = self._run(decisions, partial)
        self.assertEqual(len(out["measures"]), 1)
        self.assertEqual(out["measures"][0]["table"], "Orders")
        self.assertEqual(out["measures"][0]["source"], "deterministic")

    def test_agent_measure_wins_on_clash(self):
        decisions = {"measures": [{"table": "Orders", "name": "Avg X",
                                   "dax": "AGENT", "source": "llm"}],
                     "tables": [{"name": "Orders", "role": "fact"}]}
        partial = {"measures": [{"table": "Orders", "name": "avg x",
                                 "dax": "DET", "formatString": None}]}
        out = self._run(decisions, partial)
        self.assertEqual(len(out["measures"]), 1)
        self.assertEqual(out["measures"][0]["dax"], "AGENT")


class TestStarSchemaMeasureRepoint(unittest.TestCase):
    """Deterministic measures carry the single-flat placeholder table token in
    their DAX. When the agent designs a star schema with a differently-named fact
    table, the column qualifier inside the DAX must be re-pointed to the fact —
    otherwise the measure references a non-existent table and errors in Desktop.
    Regression for the Loan report: 'Total Loans' = COUNT(LoanPortfolioAnalysis[
    loan_id]) must become COUNT(loan[loan_id]).
    """

    def test_merge_repoints_dax_qualifier_to_fact(self):
        ir = {"workbook": {"pascalName": "LoanPortfolioAnalysis"}}
        dax_partial = {"measures": [
            {"table": "LoanPortfolioAnalysis", "name": "Total Loans",
             "dax": "COUNT ( LoanPortfolioAnalysis[loan_id] )", "source": "template"},
            {"table": "LoanPortfolioAnalysis", "name": "Total Funded Amount",
             "dax": "SUM ( LoanPortfolioAnalysis[loan_amount] )", "source": "template"},
        ]}
        agent = {
            "tableStrategy": "star-schema",
            "tables": [{"name": "loan", "role": "fact"},
                       {"name": "customer", "role": "dim"}],
            "relationships": [],
            "measures": [],
        }
        out = MD.merge(ir, dax_partial, None, agent)
        by_name = {m["name"]: m for m in out["measures"]}
        self.assertEqual(by_name["Total Loans"]["table"], "loan")
        self.assertEqual(by_name["Total Loans"]["dax"], "COUNT ( loan[loan_id] )")
        self.assertEqual(by_name["Total Funded Amount"]["dax"],
                         "SUM ( loan[loan_amount] )")
        # No stale model-name qualifier may survive anywhere.
        for m in out["measures"]:
            self.assertNotIn("LoanPortfolioAnalysis[", m["dax"])

    def test_repoint_helper_handles_quoted_and_noop(self):
        self.assertEqual(MD._repoint_dax_table("SUM(Wb[x])", "Wb", "loan"),
                         "SUM(loan[x])")
        # Quoted source token -> bare target token
        self.assertEqual(MD._repoint_dax_table("SUM('Wb Name'[x])", "Wb Name", "loan"),
                         "SUM(loan[x])")
        # Same name is a no-op
        self.assertEqual(MD._repoint_dax_table("SUM(loan[x])", "loan", "loan"),
                         "SUM(loan[x])")
        # Target needing quotes
        self.assertEqual(MD._repoint_dax_table("SUM(Wb[x])", "Wb", "My Fact"),
                         "SUM('My Fact'[x])")


class TestCalculateFilterMeasureHoist(unittest.TestCase):
    """Power BI rejects a measure reference inside a CALCULATE *boolean* filter
    predicate ("A function 'CALCULATE' has been used in a True/False expression
    that is used as a table filter expression. This is not allowed."). The merge
    step must hoist such measure refs into a VAR so the predicate compares a plain
    scalar. Regression for the Sales report's CY/PY time-intelligence measures.
    """

    MS = {"Selected Year", "CY Sales", "PY Sales", "CY Profit"}

    def test_hoists_measure_in_calculate_predicate(self):
        out = MD._sanitize_calculate_filters(
            "CALCULATE(SUM(Orders[Sales]), Orders[Order Date (Year)] = [Selected Year])",
            self.MS)
        self.assertEqual(
            out,
            "VAR __cf1 = [Selected Year]\n"
            "RETURN CALCULATE(SUM(Orders[Sales]), Orders[Order Date (Year)] = __cf1)")
        self.assertNotIn("(Year)] = [Selected Year]", out)

    def test_preserves_arithmetic_after_hoisted_measure(self):
        out = MD._sanitize_calculate_filters(
            "CALCULATE(SUM(Orders[Sales]), Orders[Order Date (Year)] = [Selected Year] - 1)",
            self.MS)
        self.assertIn("VAR __cf1 = [Selected Year]", out)
        self.assertIn("= __cf1 - 1", out)

    def test_leaves_table_expression_filters_untouched(self):
        # ALLEXCEPT is a table-expression filter; only the bare predicate's measure
        # ref is hoisted. The column refs inside ALLEXCEPT must survive verbatim.
        dax = ("CALCULATE(DISTINCTCOUNT(Orders[Order ID]), "
               "ALLEXCEPT(Orders, Orders[Customer ID]), "
               "Orders[Order Date (Year)] = [Selected Year])")
        out = MD._sanitize_calculate_filters(dax, self.MS)
        self.assertIn("ALLEXCEPT(Orders, Orders[Customer ID])", out)
        self.assertIn("VAR __cf1 = [Selected Year]", out)
        self.assertIn("= __cf1)", out)

    def test_does_not_touch_measure_refs_in_row_context(self):
        # A measure ref inside AVERAGEX/ALLSELECTED (row context) is legal — must
        # not be hoisted, and an existing VAR/RETURN body must be left intact.
        dax = ("VAR _cur = [CY Sales]\n"
               "VAR _avg = AVERAGEX(ALLSELECTED(Orders[Order Date]), [CY Sales])\n"
               "RETURN IF(_cur > _avg, 1, 0)")
        self.assertEqual(MD._sanitize_calculate_filters(dax, self.MS), dax)

    def test_does_not_hoist_column_ref_named_like_predicate(self):
        # The compared column itself ([Order Date (Year)]) is not a measure and
        # must never be hoisted, regardless of the bracket syntax.
        out = MD._sanitize_calculate_filters(
            "CALCULATE(SUM(Orders[Sales]), Orders[Order Date (Year)] = [Selected Year])",
            self.MS)
        self.assertIn("Orders[Order Date (Year)] =", out)

    def test_non_calculate_dax_unchanged(self):
        dax = "SELECTEDVALUE('Select Year'[Select Year], 2023)"
        self.assertEqual(MD._sanitize_calculate_filters(dax, self.MS), dax)

    def test_merge_applies_hoist_to_agent_measures(self):
        ir = {"workbook": {"pascalName": "Sales"}}
        agent = {
            "tableStrategy": "single-flat",
            "tables": [{"name": "Orders", "role": "fact"}],
            "relationships": [],
            "measures": [
                {"table": "Orders", "name": "Selected Year",
                 "dax": "SELECTEDVALUE('Select Year'[Select Year], 2023)",
                 "source": "llm"},
                {"table": "Orders", "name": "CY Sales",
                 "dax": "CALCULATE(SUM(Orders[Sales]), Orders[Order Date (Year)] = [Selected Year])",
                 "source": "llm"},
            ],
        }
        out = MD.merge(ir, {"measures": []}, None, agent)
        cy = next(m for m in out["measures"] if m["name"] == "CY Sales")
        self.assertIn("VAR __cf1 = [Selected Year]", cy["dax"])
        self.assertNotIn("(Year)] = [Selected Year]", cy["dax"])

    def test_hoists_bare_bracket_led_predicate(self):
        # Generalised shape: the measure leads the predicate (`[Measure] > 5`),
        # which the original Col-led regex missed. Power BI rejects it the same
        # way, so it must also be hoisted.
        out = MD._sanitize_calculate_filters(
            "CALCULATE(SUM(Orders[Sales]), [CY Sales] > 1000)", self.MS)
        self.assertEqual(
            out,
            "VAR __cf1 = [CY Sales]\n"
            "RETURN CALCULATE(SUM(Orders[Sales]), __cf1 > 1000)")

    def test_measure_in_filter_function_not_hoisted(self):
        # A measure ref inside FILTER's row-context condition is legal: the `>` is
        # nested (paren depth > 0), so it is NOT a bare predicate and stays put.
        dax = "CALCULATE(SUM(Orders[Sales]), FILTER(Orders, [CY Sales] > 1000))"
        self.assertEqual(MD._sanitize_calculate_filters(dax, self.MS), dax)

    def test_unsafe_detector_flags_unsanitized_measure(self):
        # The residual safety net: raw DAX with a measure in a CALCULATE boolean
        # filter is reported so the orchestrator can escalate to the agent.
        measures = [
            {"name": "CY Sales", "dax": "SUM(Orders[Sales])"},
            {"name": "Bad", "dax":
                "CALCULATE(SUM(Orders[Sales]), Orders[Year] = [CY Sales])"},
        ]
        self.assertEqual(MD.unsafe_calculate_filter_measures(measures), ["Bad"])

    def test_unsafe_detector_clean_after_sanitize(self):
        # After the deterministic sanitizer runs, no residual must remain.
        raw = "CALCULATE(SUM(Orders[Sales]), Orders[Year] = [CY Sales])"
        fixed = MD._sanitize_calculate_filters(raw, self.MS)
        measures = [{"name": "Bad", "dax": fixed}]
        self.assertEqual(MD.unsafe_calculate_filter_measures(measures), [])

    def test_unsafe_detector_ignores_legal_filter_function(self):
        measures = [{"name": "Ok", "dax":
            "CALCULATE(SUM(Orders[Sales]), FILTER(Orders, [CY Sales] > 1000))"}]
        self.assertEqual(MD.unsafe_calculate_filter_measures(measures), [])


class TestSemanticUnknownTable(unittest.TestCase):
    """validate_semantics must flag a measure that references a table which does
    not exist in the model (the blind spot that let the buggy Loan measures pass).
    """

    def test_unknown_table_reference_is_error(self):
        model = {
            "columns": {"loan": {"loan_id"}},
            "colTypes": {},
            "measures": {"Bad"},
            "measureHost": {"Bad": "loan"},
            "measureDax": {"Bad": "COUNT ( Ghost[loan_id] )"},
        }
        errors, warnings = [], []
        VS.check_measures(model, errors, warnings)
        self.assertTrue(any("Ghost" in e and "does not exist" in e for e in errors),
                        f"expected unknown-table error, got: {errors}")

    def test_valid_table_reference_passes(self):
        model = {
            "columns": {"loan": {"loan_id"}},
            "colTypes": {},
            "measures": {"Good"},
            "measureHost": {"Good": "loan"},
            "measureDax": {"Good": "COUNT ( loan[loan_id] )"},
        }
        errors, warnings = [], []
        VS.check_measures(model, errors, warnings)
        self.assertEqual(errors, [])



class TestVisualBinding(unittest.TestCase):
    """Deterministic visual structure decisions that fixed the broken Loan
    dashboard: KPI card detection, crosstab/heatmap detection, and mapping a
    Tableau aggregate pill to the matching model measure.
    """

    def test_measure_for_pill_maps_count_to_total_loans(self):
        # Worksheet's primary pill is COUNT(loan_id); model has a measure whose
        # DAX is COUNT(loan[loan_id]) named "Total Loans".
        ws = {"measures": [{"agg": "COUNT", "column": "loan_id"}]}
        decisions = {"measures": [
            {"name": "Total Funded Amount", "dax": "SUM ( loan[loan_amount] )"},
            {"name": "Total Loans", "dax": "COUNT ( loan[loan_id] )"},
        ]}
        mset = {"Total Funded Amount", "Total Loans"}
        self.assertEqual(PBB.measure_for_pill(ws, decisions, mset), "Total Loans")

    def test_measure_for_pill_returns_none_when_no_match(self):
        ws = {"measures": [{"agg": "AVG", "column": "ghost_col"}]}
        decisions = {"measures": [{"name": "Total Loans",
                                   "dax": "COUNT ( loan[loan_id] )"}]}
        self.assertIsNone(PBB.measure_for_pill(ws, decisions, {"Total Loans"}))

    def test_is_card_ws_true_for_single_metric_no_shelf(self):
        # KPI: no category, nothing on rows/cols, just a measure value.
        ws = {"rows": [], "cols": [], "categoryField": None,
              "values": ["loan_id"], "measures": [{"agg": "COUNT",
                                                    "column": "loan_id"}]}
        self.assertTrue(EP._is_card_ws(ws))

    def test_is_card_ws_false_when_category_present(self):
        ws = {"rows": [], "cols": [], "categoryField": "grade",
              "values": ["loan_id"], "measures": [{"agg": "COUNT",
                                                   "column": "loan_id"}]}
        self.assertFalse(EP._is_card_ws(ws))

    def test_is_crosstab_ws_true_when_rows_and_cols_have_dims(self):
        # Square heatmap: grade on rows, term on cols -> matrix, not treemap.
        ws = {"rows": ["grade"], "cols": ["term"],
              "measures": [{"agg": "SUM", "column": "Default Rate"}]}
        cols = {"grade", "term", "Default Rate"}
        self.assertTrue(EP._is_crosstab_ws(ws, cols))

    def test_is_crosstab_ws_false_with_only_one_axis(self):
        ws = {"rows": ["grade"], "cols": [],
              "measures": [{"agg": "COUNT", "column": "loan_id"}]}
        cols = {"grade", "loan_id"}
        self.assertFalse(EP._is_crosstab_ws(ws, cols))

    def test_kpi_tile_decision_builds_kpistack(self):
        # Executive KPI/BAN tile: CY/PY/% Diff measures over a date sparkline,
        # with the dashboard-filter dimensions polluting the IR dimensions list.
        ws = {
            "name": "KPI Customers",
            "title": "Total Customers\n{value}\n{value}  vs. PY",
            "categoryField": "Order Date",
            "categoryDateLevel": "month",
            "dimensions": ["Order Date", "Category", "City", "Region"],
            "measures": [
                {"field": "Current Year"}, {"field": "Previous Year"},
                {"field": "PY Customers"}, {"field": "CY Customers"},
                {"field": "% Diff Customers"}, {"field": "Min/Max Customers"},
            ],
        }
        mset = {"CY Customers", "PY Customers", "% Diff Customers",
                "Min/Max Customers", "Current Year", "Previous Year"}
        vd = EP._kpi_tile_decision(ws, mset)
        self.assertIsNotNone(vd)
        self.assertTrue(vd["kpiStack"])
        self.assertEqual(vd["kpiTitle"], "Total Customers")
        self.assertEqual(vd["kpiMeasure"], "CY Customers")
        self.assertEqual(vd["secondaryValue"], "PY Customers")
        self.assertEqual(vd["kpiPctMeasure"], "% Diff Customers")
        self.assertEqual(vd["categoryField"], "Order Date")

    def test_kpi_tile_decision_none_without_date(self):
        # No plotted date grain -> not a sparkline KPI tile.
        ws = {"name": "X", "categoryField": "Region", "categoryDateLevel": None,
              "measures": [{"field": "CY Customers"}, {"field": "PY Customers"}]}
        self.assertIsNone(EP._kpi_tile_decision(ws, {"CY Customers", "PY Customers"}))

    def test_kpi_tile_decision_none_without_cy_py_pair(self):
        # A plain date line chart (no CY/PY convention) is left to normal handling.
        ws = {"name": "Trend", "categoryField": "Order Date",
              "categoryDateLevel": "month",
              "measures": [{"field": "Total Sales"}]}
        self.assertIsNone(EP._kpi_tile_decision(ws, {"Total Sales"}))


class TestTableauFormat(unittest.TestCase):
    """Faithful Tableau number-format -> Power BI formatString conversion, so a
    KPI card renders 13.08% / $4,166M like the source workbook instead of a
    generic per-aggregation default.
    """

    def test_percent(self):
        self.assertEqual(ET._tableau_to_pbi_format("p0.00%"), "0.00%")

    def test_percent_already_has_sign(self):
        # Precision is normalised to 2 decimals for consistent KPI display.
        self.assertEqual(ET._tableau_to_pbi_format("0.0%"), "0.00%")

    def test_currency_millions_keeps_explicit_dollar(self):
        # Leading "$" must be emitted as \$ so TMDL doesn't treat the value as a
        # quoted string. Scaling (millions) now lives in the visual's display
        # units, so the format string is clean 2-decimal currency.
        self.assertEqual(ET._tableau_to_pbi_format('c"$"#,##0,,M'),
                         '\\$#,##0.00')

    def test_currency_without_symbol_does_not_inject_dollar(self):
        # A bare 'c' prefix must NOT add a "$" (Tableau shows none here).
        self.assertEqual(ET._tableau_to_pbi_format("c#,##0,K"), '#,##0.00')

    def test_number_millions_with_decimal(self):
        self.assertEqual(ET._tableau_to_pbi_format("n#,##0,,.0M"),
                         '#,##0.00')

    def test_negative_section_dropped(self):
        self.assertEqual(ET._tableau_to_pbi_format('c"$"#,##0;-"$"#,##0'),
                         '\\$#,##0.00')

    def test_empty_returns_none(self):
        self.assertIsNone(ET._tableau_to_pbi_format(""))
        self.assertIsNone(ET._tableau_to_pbi_format(None))

    def test_apply_measure_by_worksheet_name(self):
        # Same-named worksheet's primary pill wins over a column-only match.
        ir = {"worksheets": [
            {"name": "Total Funded Amount",
             "measures": [{"field": "loan_amount", "column": "loan_amount",
                           "format": 'c"$"#,##0,,M'}]},
            {"name": "Detail",
             "measures": [{"field": "loan_amount", "column": "loan_amount",
                           "format": "n#,##0,,.0M"}]},
        ]}
        measures = [{"name": "Total Funded Amount",
                     "dax": "SUM ( loan[loan_amount] )",
                     "formatString": "#,0"}]
        ET._apply_tableau_formats(measures, [], ir)
        self.assertEqual(measures[0]["formatString"], '\\$#,##0.00')

    def test_apply_measure_by_single_agg_column(self):
        ir = {"worksheets": [
            {"name": "Funding",
             "measures": [{"field": "loan_amount", "column": "loan_amount",
                           "format": "n#,##0,,.0M"}]},
        ]}
        measures = [{"name": "Total Funded Amount",
                     "dax": "SUM ( loan[loan_amount] )",
                     "formatString": "#,0"}]
        ET._apply_tableau_formats(measures, [], ir)
        self.assertEqual(measures[0]["formatString"], '#,##0.00')

    def test_apply_leaves_complex_dax_untouched(self):
        ir = {"worksheets": [
            {"name": "x", "measures": [{"field": "loan_amount",
                                        "column": "loan_amount",
                                        "format": "n#,##0,,.0M"}]},
        ]}
        measures = [{"name": "Default Rate",
                     "dax": "DIVIDE(SUM(loan[d]), COUNT(loan[loan_id]))",
                     "formatString": "0.0%"}]
        ET._apply_tableau_formats(measures, [], ir)
        self.assertEqual(measures[0]["formatString"], "0.0%")

    def test_apply_column_only_percent(self):
        # Numeric column gets a percent format (drives inline-agg cards) but not
        # a currency/number one.
        ir = {"worksheets": [
            {"name": "Avg Rate", "measures": [
                {"field": "int_rate", "column": "int_rate", "format": "p0.00%"}]},
            {"name": "Amount", "measures": [
                {"field": "loan_amount", "column": "loan_amount",
                 "format": "n#,##0,,.0M"}]},
        ]}
        cols = [{"name": "int_rate", "dataType": "real", "format": None},
                {"name": "loan_amount", "dataType": "real", "format": None}]
        ET._apply_tableau_formats([], cols, ir)
        self.assertEqual(cols[0]["format"], "0.00%")
        self.assertIsNone(cols[1]["format"])


class TestSafeFormatString(unittest.TestCase):
    """A formatString that STARTS with a double quote makes the TMDL parser treat
    the whole value as a quoted string and Power BI Desktop rejects it
    (InvalidValueFormat / un-escaped quote marker) — and tmdl-validate misses it.
    safe_format_string must neutralise that at every emit point, regardless of
    whether the value came from the deterministic converter, decisions.json, or
    an agent-authored fragment."""

    def test_leading_currency_literal_escaped(self):
        self.assertEqual(B.safe_format_string('"$"#,##0.00'), '\\$#,##0.00')

    def test_leading_literal_with_suffix(self):
        # Mid-string quote (the "M" suffix) stays; only the leading literal moves.
        self.assertEqual(B.safe_format_string('"$"#,##0,,"M"'), '\\$#,##0,,"M"')

    def test_midstring_quote_untouched(self):
        self.assertEqual(B.safe_format_string('#,##0,"K"'), '#,##0,"K"')

    def test_plain_format_untouched(self):
        self.assertEqual(B.safe_format_string("0.00%"), "0.00%")
        self.assertEqual(B.safe_format_string("#,##0.00"), "#,##0.00")

    def test_none_and_empty_passthrough(self):
        self.assertIsNone(B.safe_format_string(None))
        self.assertEqual(B.safe_format_string(""), "")

    def test_multichar_leading_literal(self):
        self.assertEqual(B.safe_format_string('"USD "#,##0'), '\\U\\S\\D\\ #,##0')

    def test_unterminated_leading_quote_drops_quote(self):
        # Malformed: never let the emitted value begin with a quote marker.
        self.assertFalse(B.safe_format_string('"#,##0').startswith('"'))

    def test_measure_block_sanitizes_agent_formatstring(self):
        # End-to-end: an agent-authored measure with a leading-quote format must
        # not emit a value that starts with a double quote.
        block = B.measure_block(
            {"name": "Total Sales", "dax": "SUM(Orders[Sales])",
             "formatString": '"$"#,##0.00'}, 7)
        line = next(l for l in block.splitlines() if "formatString:" in l)
        self.assertIn('\\$#,##0.00', line)
        self.assertNotIn('formatString: "', line)

    def test_calc_column_block_sanitizes_formatstring(self):
        block = B.calc_column_block(
            "Amt", "RELATED(Orders[Sales])", "double", '"$"#,##0.00', 8)
        line = next(l for l in block.splitlines() if "formatString:" in l)
        self.assertIn('\\$#,##0.00', line)
        self.assertNotIn('formatString: "', line)

    def test_column_block_sanitizes_formatstring(self):
        block = B.column_block(
            {"name": "Amt", "dataType": "double", "format": '"$"#,##0.00'}, 9)
        line = next(l for l in block.splitlines() if "formatString:" in l)
        self.assertIn('\\$#,##0.00', line)
        self.assertNotIn('formatString: "', line)


class TestReassignOrphanMeasures(unittest.TestCase):
    """No measure may be silently dropped by build_table_file's exact match."""

    def test_exact_match_unchanged(self):
        decisions = {"measures": [{"table": "Orders", "name": "M"}],
                     "tables": [{"name": "Orders", "role": "fact"}]}
        out = ET.reassign_orphan_measures(decisions)
        self.assertEqual(out["measures"][0]["table"], "Orders")

    def test_case_or_punctuation_mismatch_is_fixed(self):
        # Agent wrote 'midnightcensus'; real table is 'Midnight_Census_Template'.
        decisions = {"measures": [{"table": "midnightcensus", "name": "M"}],
                     "tables": [{"name": "Midnight_Census_Template", "role": "fact"}]}
        out = ET.reassign_orphan_measures(decisions)
        self.assertEqual(out["measures"][0]["table"], "Midnight_Census_Template")

    def test_unknown_table_routed_to_fact(self):
        decisions = {"measures": [{"table": "Nonexistent", "name": "M"}],
                     "tables": [{"name": "DimDate", "role": "date"},
                                {"name": "Census", "role": "fact"}]}
        out = ET.reassign_orphan_measures(decisions)
        self.assertEqual(out["measures"][0]["table"], "Census")

    def test_unknown_table_routed_to_first_when_no_fact(self):
        decisions = {"measures": [{"table": "X", "name": "M"}],
                     "tables": [{"name": "A", "role": "dim"},
                                {"name": "B", "role": "dim"}]}
        out = ET.reassign_orphan_measures(decisions)
        self.assertEqual(out["measures"][0]["table"], "A")

    def test_calculated_table_is_valid_host(self):
        decisions = {"measures": [{"table": "CalcTbl", "name": "M"}],
                     "tables": [{"name": "Fact", "role": "fact"}],
                     "calculatedTables": [{"name": "CalcTbl"}]}
        out = ET.reassign_orphan_measures(decisions)
        self.assertEqual(out["measures"][0]["table"], "CalcTbl")

    def test_measures_but_no_tables_raises(self):
        decisions = {"measures": [{"table": "X", "name": "M"}], "tables": []}
        with self.assertRaises(ValueError):
            ET.reassign_orphan_measures(decisions)


class TestNoDuplicateColumns(unittest.TestCase):
    """TMDL columns within a table must be unique by name (case-insensitive).

    Power BI Desktop's loader hard-fails ("objects cannot be merged because both
    declare the same property: expression") when two columns share a name -- which
    happened when an agent time-intelligence helper column collided with a derived
    date-part column ('Order Date (Year)'). build_table_file must emit each name
    exactly once, regardless of source.
    """

    def _build(self, cols, decisions, worksheets):
        import importlib
        orig_cols = ET._columns_for
        orig_fmt = ET._apply_tableau_formats
        orig_part = ET._partition_for
        try:
            ET._columns_for = lambda t, ir, d: cols
            ET._apply_tableau_formats = lambda m, c, ir: None
            ET._partition_for = lambda t, ir, c, d: "\t# partition"
            table = {"name": "Orders", "role": "fact"}
            ir = {"worksheets": worksheets, "columns": cols}
            return ET.build_table_file(table, ir, decisions, 5)
        finally:
            ET._columns_for = orig_cols
            ET._apply_tableau_formats = orig_fmt
            ET._partition_for = orig_part

    def _decl_count(self, tmdl, name):
        # Count table-level column declarations (line starts with one tab + column).
        return sum(1 for ln in tmdl.splitlines()
                   if ln.startswith(f"\tcolumn '{name}'")
                   or ln.startswith(f"\tcolumn {name} ")
                   or ln.rstrip() == f"\tcolumn {name}")

    def test_agent_calc_column_vs_date_part_collision(self):
        # Agent emits 'Order Date (Year)' AND a worksheet needs the year part of
        # 'Order Date' -> exactly one declaration survives.
        cols = [{"name": "Order Date", "dataType": "date",
                 "role": "dimension", "format": None}]
        decisions = {"measures": [], "tables": [],
                     "calculatedColumns": [{"name": "Order Date (Year)",
                                            "table": "Orders",
                                            "dax": "YEAR(Orders[Order Date])",
                                            "dataType": "integer",
                                            "formatString": "0"}]}
        worksheets = [{"name": "ws", "categoryField": "Order Date",
                       "categoryDateLevel": "year", "dimensions": ["Order Date"]}]
        tmdl = self._build(cols, decisions, worksheets)
        self.assertEqual(self._decl_count(tmdl, "Order Date (Year)"), 1)

    def test_source_column_vs_date_part_collision(self):
        # A physical column already named 'Order Date (Year)' suppresses the
        # derived date-part column of the same name.
        cols = [{"name": "Order Date", "dataType": "date",
                 "role": "dimension", "format": None},
                {"name": "Order Date (Year)", "dataType": "integer",
                 "role": "dimension", "format": None}]
        decisions = {"measures": [], "tables": []}
        worksheets = [{"name": "ws", "categoryField": "Order Date",
                       "categoryDateLevel": "year", "dimensions": ["Order Date"]}]
        tmdl = self._build(cols, decisions, worksheets)
        self.assertEqual(self._decl_count(tmdl, "Order Date (Year)"), 1)

    def test_distinct_date_parts_all_emitted(self):
        # Genuinely different parts are NOT collapsed.
        cols = [{"name": "Order Date", "dataType": "date",
                 "role": "dimension", "format": None}]
        decisions = {"measures": [], "tables": []}
        worksheets = [
            {"name": "a", "categoryField": "Order Date",
             "categoryDateLevel": "year", "dimensions": ["Order Date"]},
            {"name": "b", "categoryField": "Order Date",
             "categoryDateLevel": "month", "dimensions": ["Order Date"]},
        ]
        tmdl = self._build(cols, decisions, worksheets)
        self.assertEqual(self._decl_count(tmdl, "Order Date (Year)"), 1)
        self.assertEqual(self._decl_count(tmdl, "Order Date (Month)"), 1)


class TestProbeColumnScoping(unittest.TestCase):
    """Header->logical matching must be scoped to a table's own physical CSV.

    Regression for the multi-table star-schema bug: a fact's foreign key
    ('Customer ID' / 'Postal Code') shares a normalized name with a dim column
    ('Customer_ID' / 'Postal_Code').  When _probe_for_table matched the fact's
    headers against the GLOBAL IR column set, the dim's spelling won and the
    fact's key was silently renamed -> the relationship failed to resolve in
    Power BI Desktop.  Each table must only see its own physicalTable columns.
    """

    def _ir(self):
        return {"columns": [
            # Fact (Orders.csv) — note the SPACE spelling of the keys.
            {"name": "Customer ID", "dataType": "string", "physicalTable": "Orders.csv"},
            {"name": "Postal Code", "dataType": "string", "physicalTable": "Orders.csv"},
            {"name": "Sales", "dataType": "double", "physicalTable": "Orders.csv"},
            # Dim (Customers.csv) — UNDERSCORE spelling collides on normalize.
            {"name": "Customer_ID", "dataType": "string", "physicalTable": "Customers.csv"},
            {"name": "Name", "dataType": "string", "physicalTable": "Customers.csv"},
            # Dim (Location.csv).
            {"name": "Postal_Code", "dataType": "string", "physicalTable": "Location.csv"},
            {"name": "State", "dataType": "string", "physicalTable": "Location.csv"},
        ]}

    def _probe(self, table, ir, headers):
        orig = ET.CP.probe
        try:
            ET.CP.probe = lambda path: {"delimiter": ",", "headers": headers}
            return ET._probe_for_table(table, ir)
        finally:
            ET.CP.probe = orig

    def test_fact_key_keeps_own_spelling(self):
        ir = self._ir()
        table = {"name": "Orders", "role": "fact", "sourceFile": "Orders.csv"}
        probe = self._probe(table, ir, ["Customer ID", "Postal Code", "Sales"])
        names = {c["csv_name"]: c["name"] for c in probe["columns"]}
        # The fact's keys must NOT be renamed to the dim underscore spellings.
        self.assertEqual(names["Customer ID"], "Customer ID")
        self.assertEqual(names["Postal Code"], "Postal Code")

    def test_dim_key_keeps_own_spelling(self):
        ir = self._ir()
        table = {"name": "Customers", "role": "dim", "sourceFile": "Customers.csv"}
        probe = self._probe(table, ir, ["Customer_ID", "Name"])
        names = {c["csv_name"]: c["name"] for c in probe["columns"]}
        self.assertEqual(names["Customer_ID"], "Customer_ID")

    def test_unmatched_scope_in_multitable_does_not_pollute(self):
        # If a table's CSV basename matches no physicalTable, the global fallback
        # is refused (>=2 physical tables) so headers stay raw rather than being
        # corrupted by a colliding sibling column.
        ir = self._ir()
        table = {"name": "Mystery", "role": "fact", "sourceFile": "Unknown.csv"}
        probe = self._probe(table, ir, ["Customer ID", "Postal Code"])
        names = {c["csv_name"]: c["name"] for c in probe["columns"]}
        self.assertEqual(names["Customer ID"], "Customer ID")
        self.assertEqual(names["Postal Code"], "Postal Code")

    def test_single_flat_source_still_maps(self):
        # With a single physical table (or no lineage), the normal fuzzy match
        # still rewrites an underscore header to its logical spelling.
        ir = {"columns": [
            {"name": "Customer ID", "dataType": "string", "physicalTable": "Flat.csv"},
            {"name": "Sales", "dataType": "double", "physicalTable": "Flat.csv"},
        ]}
        table = {"name": "Flat", "role": "fact", "sourceFile": "Flat.csv"}
        probe = self._probe(table, ir, ["Customer_ID", "Sales"])
        names = {c["csv_name"]: c["name"] for c in probe["columns"]}
        self.assertEqual(names["Customer_ID"], "Customer ID")


@unittest.skipUnless(os.path.isfile(MIDNIGHT_TWB), "Midnight Census workbook not present")
class TestGoldenMidnightCensus(unittest.TestCase):
    """The deterministic path must reproduce the committed Output/ artifacts."""

    def test_parse_matches_committed_analysis(self):
        produced = P.build_ir(MIDNIGHT_TWB)
        committed = _load(os.path.join(MIDNIGHT_OUT, "analysis.json"))
        # sourcePath is environment-specific (absolute vs committed relative); the
        # rest of the IR must match byte-for-byte.
        produced["workbook"]["sourcePath"] = committed["workbook"]["sourcePath"]
        self.maxDiff = None
        self.assertEqual(produced, committed)

    def test_map_dax_matches_committed_partial(self):
        ir = _load(os.path.join(MIDNIGHT_OUT, "analysis.json"))
        produced = M.build_measures(ir, M._default_table(ir))
        committed = _load(os.path.join(MIDNIGHT_OUT, "dax-partial.json"))
        self.assertEqual(produced, committed)


class TestNavButtonBlock(unittest.TestCase):
    """pbir_blocks.nav_button_visual -> actionButton wired via visualLink."""

    def test_page_navigation_action(self):
        v = PB.nav_button_visual(
            "nav_1", PB.position(10, 20, 120, 40, 1000),
            "Go to Sales Dashboard", "SalesDashboard", fill="#aa0000")
        self.assertEqual(v["visual"]["visualType"], "actionButton")
        link = v["visual"]["visualContainerObjects"]["visualLink"][0]["properties"]
        # The action MUST live in visualLink (not objects.action) or it silently fails.
        self.assertEqual(link["type"]["expr"]["Literal"]["Value"], "'PageNavigation'")
        self.assertEqual(link["navigationSection"]["expr"]["Literal"]["Value"], "'SalesDashboard'")
        text = v["visual"]["objects"]["text"][0]["properties"]["text"]
        self.assertEqual(text["expr"]["Literal"]["Value"], "'Go to Sales Dashboard'")

    def test_no_target_omits_navigation_section(self):
        v = PB.nav_button_visual("nav_2", PB.position(0, 0, 80, 40, 1000), "X", None)
        link = v["visual"]["visualContainerObjects"]["visualLink"][0]["properties"]
        self.assertNotIn("navigationSection", link)


class TestButtonVisibility(unittest.TestCase):
    """Buttons must never render as an invisible (transparent) tile.

    Regression for the show/hide toggle buttons painting all-white: the
    auto-derived theme adopted a transparent Tableau mark colour ('#00000000')
    as the button fill. opaque_color() must sanitise that, and both button
    builders must emit an opaque fill plus a visible outline.
    """

    def _fill(self, visual):
        props = visual["visual"]["objects"]["fill"][0]["properties"]
        return props["fillColor"]["solid"]["color"]["expr"]["Literal"]["Value"]

    def _outline(self, visual):
        return visual["visual"]["objects"]["outline"][0]["properties"]

    def test_opaque_color_replaces_transparent(self):
        # Fully transparent ARGB -> branded fallback, never kept as a fill.
        self.assertEqual(PB.opaque_color("#00000000"), "#004263")
        self.assertEqual(PB.opaque_color("#00ABCDEF"), "#004263")

    def test_opaque_color_strips_non_transparent_alpha(self):
        # #AARRGGBB with a visible alpha keeps the RGB and drops the alpha byte.
        self.assertEqual(PB.opaque_color("#FF112233"), "#112233")

    def test_opaque_color_passes_through_and_falls_back(self):
        self.assertEqual(PB.opaque_color("#112233"), "#112233")
        self.assertEqual(PB.opaque_color(None), "#004263")
        self.assertEqual(PB.opaque_color("not-a-color"), "#004263")
        self.assertEqual(PB.opaque_color("#abc"), "#004263")  # 3-digit unsupported

    def test_bookmark_button_fill_is_opaque_with_outline(self):
        v = PB.bookmark_button_visual(
            "toggle_1", PB.position(0, 0, 80, 70, 1002),
            "Hide Filters", "bm_hide", fill="#00000000")
        self.assertEqual(self._fill(v), "'#004263'")  # transparent sanitised away
        outline = self._outline(v)
        self.assertEqual(outline["show"]["expr"]["Literal"]["Value"], "true")
        self.assertIn("lineColor", outline)
        self.assertIn("weight", outline)

    def test_nav_button_fill_is_opaque_with_outline(self):
        v = PB.nav_button_visual(
            "nav_1", PB.position(0, 0, 120, 40, 1000),
            "Go to Sales", "SalesDashboard", fill="#00000000")
        self.assertEqual(self._fill(v), "'#004263'")
        outline = self._outline(v)
        self.assertEqual(outline["show"]["expr"]["Literal"]["Value"], "true")
        self.assertIn("lineColor", outline)


class TestNavTargetResolve(unittest.TestCase):
    """emit_pbir._resolve_nav_target resolves goto-sheet targets by label."""

    PAGES = {"Sales Dashboard": "SalesDashboard", "Customer Dashboard": "CustomerDashboard"}

    def test_label_matches_other_page(self):
        btn = {"label": "Go to Sales Dashboard"}
        self.assertEqual(EP._resolve_nav_target(btn, self.PAGES, "CustomerDashboard"),
                         "SalesDashboard")

    def test_two_page_fallback_when_label_unhelpful(self):
        btn = {"label": ""}
        # exactly one other page -> unambiguous fallback target
        self.assertEqual(EP._resolve_nav_target(btn, self.PAGES, "SalesDashboard"),
                         "CustomerDashboard")


@unittest.skipUnless(os.path.isfile(SALES_TWB), "Sales & Customer workbook not present")
class TestButtonExtraction(unittest.TestCase):
    """Parser captures button label, window-id and toggle zone-ids from the .twb."""

    def test_goto_and_toggle_buttons_extracted(self):
        root = X.load_twb(SALES_TWB)
        dashes = V.extract_dashboards(root)
        buttons = [b for d in dashes for b in d.get("buttons", [])]
        gotos = [b for b in buttons if b["action"] == "goto-sheet"]
        toggles = [b for b in buttons if b["action"] == "toggle"]
        self.assertTrue(gotos, "expected goto-sheet buttons")
        self.assertTrue(all(b.get("label") for b in gotos), "goto buttons need labels")
        self.assertTrue(all(b.get("windowId") for b in gotos), "goto buttons need window-id")
        # toggle buttons carry the zone-ids they show/hide
        self.assertTrue(toggles and toggles[0].get("zoneIds"), "toggle needs zoneIds")


@unittest.skipUnless(os.path.isfile(SALES_TWB), "Sales & Customer workbook not present")
class TestToggleTargetResolution(unittest.TestCase):
    """Parser expands a toggle's container zone-ids to emitted leaf zone ids."""

    def test_toggle_resolves_drawer_leaf_zones(self):
        dashes = V.extract_dashboards(X.load_twb(SALES_TWB))
        toggles = [b for d in dashes for b in d.get("buttons", [])
                   if b["action"] == "toggle"]
        self.assertTrue(toggles)
        t = toggles[0]
        self.assertTrue(t.get("targetZoneIds"), "container expanded to leaf zones")
        # every resolved target is a concrete leaf zone id (digit string)
        self.assertTrue(all(str(z).isdigit() for z in t["targetZoneIds"]))


class TestBookmarkBlocks(unittest.TestCase):
    """pbir_blocks bookmark button + bookmark definition shape."""

    def test_bookmark_button_wires_bookmark_action(self):
        v = PB.bookmark_button_visual("toggle_show_x_1", PB.position(0, 0, 40, 40, 1000),
                                      "Show Filters", "abc123", icon="Filter")
        self.assertEqual(v["visual"]["visualType"], "actionButton")
        link = v["visual"]["visualContainerObjects"]["visualLink"][0]["properties"]
        self.assertEqual(link["type"]["expr"]["Literal"]["Value"], "'Bookmark'")
        self.assertEqual(link["bookmark"]["expr"]["Literal"]["Value"], "'abc123'")

    def test_bookmark_hides_listed_omits_others(self):
        bm = PB.bookmark_definition("id1", "Hide Filters", "SalesDashboard",
                                    ["a", "b", "btn"], ["a", "b"])
        containers = bm["explorationState"]["sections"]["SalesDashboard"]["visualContainers"]
        self.assertEqual(set(containers), {"a", "b"})
        self.assertNotIn("btn", containers)  # omitted -> visible
        self.assertEqual(containers["a"]["singleVisual"]["display"]["mode"], "hidden")
        # the schema has NO "visible" mode; ensure we never emit it
        self.assertNotIn("visible", json.dumps(bm))
        self.assertEqual(bm["options"]["targetVisualNames"], ["a", "b", "btn"])


@unittest.skipUnless(os.path.isfile(SALES_TWB), "Sales & Customer workbook not present")
class TestToggleBookmarkEmission(unittest.TestCase):
    """End-to-end: a toggle button emits a Show/Hide bookmark pair + 2 buttons."""

    def _build(self):
        dashes = V.extract_dashboards(X.load_twb(SALES_TWB))
        ir = {"workbook": {"pascalName": "X"}, "dashboards": dashes,
              "tables": [], "model": {}}
        decisions = {"theme": {}, "visuals": {}, "measures": [], "primaryEntity": "T"}
        tmp = tempfile.mkdtemp()
        sink = []
        pm = {d["name"]: EP.sanitize(d["name"]) for d in dashes}
        try:
            for d in dashes:
                EP.build_page(d, ir, decisions, tmp, pm, sink)
            page = EP.sanitize(dashes[0]["name"])
            names = os.listdir(os.path.join(tmp, page, "visuals"))
        finally:
            visuals = [list(os.listdir(os.path.join(tmp, EP.sanitize(d["name"]), "visuals")))
                       for d in dashes]
            shutil.rmtree(tmp, ignore_errors=True)
        return sink, names, visuals

    def test_pair_and_stacked_buttons(self):
        sink, names, _ = self._build()
        self.assertTrue(sink, "bookmarks generated")
        # show + hide for each dashboard with a toggle
        kinds = sorted({bm["displayName"].split()[0] for bm in sink})
        self.assertEqual(kinds, ["Hide", "Show"])
        self.assertTrue(any(n.startswith("toggle_show_") for n in names))
        self.assertTrue(any(n.startswith("toggle_hide_") for n in names))

    def test_show_bookmark_only_hides_show_button(self):
        sink, _, _ = self._build()
        show = next(bm for bm in sink if bm["displayName"].startswith("Show"))
        sect = show["explorationState"]["sections"][show["explorationState"]["activeSection"]]
        hidden = list(sect["visualContainers"])
        self.assertEqual(len(hidden), 1)
        self.assertTrue(hidden[0].startswith("toggle_show_"))

    def test_hide_bookmark_hides_drawer_and_hide_button(self):
        sink, _, _ = self._build()
        hide = next(bm for bm in sink if bm["displayName"].startswith("Hide"))
        sect = hide["explorationState"]["sections"][hide["explorationState"]["activeSection"]]
        hidden = list(sect["visualContainers"])
        self.assertTrue(any(h.startswith("toggle_hide_") for h in hidden))
        # drawer visuals (slicers/text) are hidden too
        self.assertTrue(any(h.startswith(("slicer_", "text_")) for h in hidden))


@unittest.skipUnless(os.path.isfile(SALES_TWB), "Sales & Customer workbook not present")
class TestFeatureAudit(unittest.TestCase):
    """The ground-truth auditor must detect every real feature in the .twb and
    be honest about gaps — with NO false silent-misses or false unknown
    elements. This is the scale-grade guarantee: no feature is silently lost."""

    @classmethod
    def setUpClass(cls):
        cls.man = FA.audit(SALES_TWB)
        cls.feat = {f["key"]: f for f in cls.man["features"]}

    def test_core_features_detected(self):
        for key in ("worksheets", "dashboards", "zones", "filters",
                    "calcFields", "relationships", "navButtons",
                    "toggleButtons", "actionFilter"):
            self.assertIn(key, self.feat, f"{key} catalogued")
            self.assertGreater(self.feat[key]["xmlCount"], 0, f"{key} detected")

    def test_no_false_silent_miss(self):
        misses = [f["key"] for f in self.man["features"] if f["silentMiss"]]
        self.assertEqual(self.man["summary"]["silentMisses"], 0,
                         f"unexpected silent misses: {misses}")

    def test_no_unknown_elements(self):
        self.assertEqual(self.man["summary"]["unknownTagKinds"], 0,
                         f"uncatalogued tags: {self.man['unknownTags']}")

    def test_nav_and_toggle_counts_match_ir(self):
        self.assertEqual(self.feat["navButtons"]["xmlCount"],
                         self.feat["navButtons"]["irCount"])
        self.assertEqual(self.feat["toggleButtons"]["xmlCount"],
                         self.feat["toggleButtons"]["irCount"])

    def test_genuine_gaps_are_flagged(self):
        gap_keys = {f["key"] for f in self.man["features"] if f["status"] == FA.GAP}
        self.assertIn("setControls", gap_keys)
        self.assertIn("referenceLines", gap_keys)

    def test_verdict_nonzero_when_gaps(self):
        self.assertNotEqual(FA._verdict(self.man, strict=False), 0)


class TestVisualInference(unittest.TestCase):
    """Deterministic mark -> visualType inference. Genuinely-resolvable worksheets
    must be stamped into the IR (inferredVisualType) so they never reach the agent,
    keeping 'migrate this report' seamless and cheap."""

    def test_explicit_marks_resolve(self):
        self.assertEqual(MI.infer_visual_type({"markClass": "Bar"}), "barChart")
        self.assertEqual(MI.infer_visual_type({"markClass": "Line"}), "lineChart")
        self.assertEqual(MI.infer_visual_type({"markClass": "Pie"}), "pieChart")
        self.assertEqual(MI.infer_visual_type({"markClass": "Map"}), "map")
        self.assertEqual(MI.infer_visual_type({"markClass": "Filled Map"}), "map")

    def test_automatic_heuristics(self):
        # KPI: no dims, no values
        self.assertEqual(MI.infer_visual_type({"markClass": "Automatic"}), "card")
        # date + value -> line
        self.assertEqual(MI.infer_visual_type(
            {"markClass": "Automatic", "categoryDateLevel": "month",
             "dimensions": ["Date"], "values": ["v"]}), "lineChart")
        # one dim + one value, horizontal -> bar
        self.assertEqual(MI.infer_visual_type(
            {"markClass": "Automatic", "dimensions": ["d"], "values": ["v"],
             "orientation": "horizontal"}), "barChart")
        # dims only, no measure -> confident table (a list / crosstab)
        self.assertEqual(MI.infer_visual_type(
            {"markClass": "Automatic", "dimensions": ["a", "b"], "values": []}),
            "tableEx")

    def test_continuous_date_axis_is_line_not_card(self):
        # REGRESSION: a continuous SUM(year) pill on the column shelf is the time
        # axis of a trend line, not a second measure. The parser counts it among
        # the values with dimensions=[], so the old `n_dim==0 and n_val>=1 -> card`
        # rule mislabeled "Revenue by Year" / "Profit Trend" as KPI cards. A field
        # on BOTH shelves with a temporal axis must resolve to a lineChart.
        self.assertEqual(MI.infer_visual_type(
            {"markClass": "Automatic", "dimensions": [], "rows": ["Revenue"],
             "cols": ["year"], "values": ["year", "Revenue"]}), "lineChart")
        # Two measures on both shelves (e.g. multiple metrics over a year axis)
        # still a line because the axis is temporal.
        self.assertEqual(MI.infer_visual_type(
            {"markClass": "Automatic", "dimensions": [],
             "rows": ["Revenue", "Profit Margin %"], "cols": ["year"],
             "values": ["year", "Revenue", "Profit Margin %"]}), "lineChart")
        # A genuine KPI card leaves BOTH shelves empty (value via text encoding):
        # it must stay a card.
        self.assertEqual(MI.infer_visual_type(
            {"markClass": "Automatic", "dimensions": [], "rows": [], "cols": [],
             "values": ["Revenue"]}), "card")
        # Both shelves occupied but NON-temporal axis (two measures X/Y) is a real
        # ambiguous scatter/combo shape -> route to the agent, not a card.
        self.assertIsNone(MI.infer_visual_type(
            {"markClass": "Automatic", "dimensions": [], "rows": ["Profit"],
             "cols": ["Sales"], "values": ["Sales", "Profit"]}))

    def test_multi_measure_is_ambiguous_not_table(self):
        # Several measures over a dimension is genuinely ambiguous: it must route
        # to the agent (None), NOT silently default to a table.
        self.assertIsNone(MI.infer_visual_type(
            {"markClass": "Automatic", "dimensions": ["Region"],
             "values": ["Sales", "Profit"]}))
        # Even with a date grain, multiple measures stay ambiguous (could be a
        # multi-series line, combo, or KPI tile — the agent / kpiStack decides).
        self.assertIsNone(MI.infer_visual_type(
            {"markClass": "Automatic", "categoryDateLevel": "month",
             "dimensions": ["Order Date", "Region"],
             "values": ["CY Sales", "PY Sales"]}))

    def test_flattened_multidim_no_measure_routes_to_agent(self):
        # GAP FIX: 2+ placed shelf dimensions (rows + cols) with NO measure can
        # only be FLATTENED into a guessed table, so the deterministic engine must
        # route it to the agent (None) instead of silently emitting a table.
        self.assertIsNone(MI.infer_visual_type(
            {"markClass": "Automatic", "dimensions": ["a", "b"],
             "rows": ["a", "b"], "cols": ["c"], "values": []}))
        # A Text-mark cross-tab shape with no measure is equally ambiguous.
        self.assertIsNone(MI.infer_visual_type(
            {"markClass": "Text", "rows": ["a"], "cols": ["b"], "values": []}))
        # A single placed dimension (a slicer / value list) stays a confident
        # table for the emitter's slicer/list path to handle.
        self.assertEqual(MI.infer_visual_type(
            {"markClass": "Text", "rows": ["a"], "values": []}), "tableEx")
        # A multi-dim shape that DOES carry a measure is a genuine detail table.
        self.assertEqual(MI.infer_visual_type(
            {"markClass": "Text", "rows": ["a", "b"], "cols": ["c"],
             "values": ["Sales"]}), "tableEx")

    @unittest.skipUnless(os.path.isfile(SALES_TWB), "Sales workbook not present")
    def test_ir_stamps_inferred_type(self):
        ir = P.build_ir(SALES_TWB)
        sheets = ir.get("worksheets", [])
        self.assertTrue(sheets)
        # Every worksheet carries the key. Confident shapes (explicit marks, single
        # measure, dims-only) resolve deterministically; genuinely-ambiguous
        # multi-measure worksheets are left None so they reach the agent instead of
        # being silently tabled.
        self.assertTrue(all("inferredVisualType" in w for w in sheets))
        resolved = [w for w in sheets if w.get("inferredVisualType")]
        self.assertTrue(resolved, "some Sales worksheets still resolve deterministically")


class TestMemberNameNormalization(unittest.TestCase):
    """Power BI rejects [] {} in object names; one such name (e.g. the Tableau LOD
    field '{SUM([CY Sales])}') fails the WHOLE model load so no measures appear. The
    emit choke point must rename it to a valid member everywhere (model + bindings)."""

    def test_pbi_member_name_strips_illegal_chars(self):
        self.assertEqual(MG._pbi_member_name("{SUM([CY Sales])}"), "SUM(CY Sales)")
        self.assertEqual(MG._pbi_member_name("[Profit]"), "Profit")
        self.assertEqual(MG._pbi_member_name("Total Sales"), "Total Sales")
        # never returns empty
        self.assertTrue(MG._pbi_member_name("{}[]"))

    def test_normalize_renames_decisions_and_ir_consistently(self):
        tmp = tempfile.mkdtemp()
        try:
            analysis = os.path.join(tmp, "analysis.json")
            decisions = os.path.join(tmp, "decisions.json")
            with open(decisions, "w", encoding="utf-8") as fh:
                json.dump({"measures": [
                    {"table": "Orders", "name": "{SUM([CY Sales])}",
                     "dax": "CALCULATE([CY Sales], ALLSELECTED())"},
                    {"table": "Orders", "name": "Roll", "dax": "[{SUM([CY Sales])}] * 2"},
                ], "visualDecisions": [
                    {"worksheet": "KPI", "value": "{SUM([CY Sales])}"}]}, fh)
            with open(analysis, "w", encoding="utf-8") as fh:
                json.dump({"calculatedFields": [{"caption": "{SUM([CY Sales])}"}],
                           "worksheets": [{"name": "KPI",
                                           "values": ["{SUM([CY Sales])}"],
                                           "title": "Total\n{{SUM([CY Sales])}}\n{value}"}]}, fh)
            rename = MG._normalize_member_names(analysis, decisions)
            self.assertEqual(rename, {"{SUM([CY Sales])}": "SUM(CY Sales)"})
            dec = json.load(open(decisions, encoding="utf-8"))
            ir = json.load(open(analysis, encoding="utf-8"))
            # model side: name renamed, self-ref in another measure's DAX rewritten
            self.assertEqual(dec["measures"][0]["name"], "SUM(CY Sales)")
            self.assertEqual(dec["measures"][1]["dax"], "[SUM(CY Sales)] * 2")
            self.assertEqual(dec["visualDecisions"][0]["value"], "SUM(CY Sales)")
            # binding side: caption + worksheet refs + title template all consistent
            self.assertEqual(ir["calculatedFields"][0]["caption"], "SUM(CY Sales)")
            self.assertEqual(ir["worksheets"][0]["values"], ["SUM(CY Sales)"])
            self.assertIn("{SUM(CY Sales)}", ir["worksheets"][0]["title"])
            self.assertNotIn("{SUM([CY Sales])}", json.dumps(ir))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_normalize_is_noop_when_all_names_valid(self):
        tmp = tempfile.mkdtemp()
        try:
            analysis = os.path.join(tmp, "analysis.json")
            decisions = os.path.join(tmp, "decisions.json")
            with open(decisions, "w", encoding="utf-8") as fh:
                json.dump({"measures": [{"table": "T", "name": "Total", "dax": "1"}]}, fh)
            with open(analysis, "w", encoding="utf-8") as fh:
                json.dump({"worksheets": []}, fh)
            self.assertEqual(MG._normalize_member_names(analysis, decisions), {})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestBindingConfidence(unittest.TestCase):
    """Binding-level safety net: even when a chart TYPE is known, a worksheet that
    exposes no bindable value would make the emitter guess the first model
    measure/column. Such worksheets must escalate to the agent so the engine stays
    correct for ANY workbook, not just the bundled fixtures."""

    def test_value_chart_without_value_escalates(self):
        # A bar/line/pie chart with no measure pill, no values shelf and no value
        # field has nothing real to plot -> route to agent.
        for vt in ("barChart", "lineChart", "pieChart", "treemap", "comboChart"):
            self.assertTrue(
                MI.binding_needs_agent(
                    {"inferredVisualType": vt, "measures": [], "values": [],
                     "valueField": None}),
                f"{vt} with no value must escalate")

    def test_value_chart_with_value_is_confident(self):
        # Any real value signal (measure pill, values shelf, or valueField) makes
        # the chart deterministically bindable -> no escalation.
        self.assertFalse(MI.binding_needs_agent(
            {"inferredVisualType": "barChart", "measures": [{"agg": "SUM"}]}))
        self.assertFalse(MI.binding_needs_agent(
            {"inferredVisualType": "lineChart", "values": ["Sales"]}))
        self.assertFalse(MI.binding_needs_agent(
            {"inferredVisualType": "pieChart", "valueField": "Sales"}))

    def test_table_card_and_ambiguous_never_escalate(self):
        # Detail tables and KPI cards always bind from their own fields; a None
        # type is already gated by the type-level inference.
        self.assertFalse(MI.binding_needs_agent(
            {"inferredVisualType": "tableEx", "measures": [], "values": []}))
        self.assertFalse(MI.binding_needs_agent(
            {"inferredVisualType": "card", "measures": [], "values": []}))
        self.assertFalse(MI.binding_needs_agent(
            {"inferredVisualType": None, "measures": [], "values": []}))

    def test_caption_worksheet_never_escalates(self):
        # A caption renders as a textbox, not a chart, so a missing value is fine.
        self.assertFalse(MI.binding_needs_agent(
            {"inferredVisualType": "barChart", "measures": [], "values": [],
             "valueField": None, "caption": "Filters Applied: ..."}))

    def test_empty_or_missing_worksheet(self):
        self.assertFalse(MI.binding_needs_agent(None))
        self.assertFalse(MI.binding_needs_agent({}))

    def test_guessed_category_escalates_output_first(self):
        # Output-first: a chart that needs a category but whose named field is NOT
        # a real model column (and has no date grain / real shelf dim) would be
        # guessed by the emitter -> route to the agent for a correct binding.
        cols = {"Region", "Sales"}
        self.assertTrue(MI.binding_needs_agent(
            {"inferredVisualType": "barChart", "values": ["Sales"],
             "categoryField": "Ghost Field", "rows": [], "cols": []}, cols))

    def test_real_category_is_confident(self):
        # A category field that IS a real column (directly or via a federated
        # alias) binds deterministically -> no escalation.
        cols = {"Region", "Sales", "state"}
        self.assertFalse(MI.binding_needs_agent(
            {"inferredVisualType": "barChart", "values": ["Sales"],
             "categoryField": "Region"}, cols))
        self.assertFalse(MI.binding_needs_agent(
            {"inferredVisualType": "barChart", "values": ["Sales"],
             "categoryField": "state (state_region.csv)"}, cols))
        # A real dimension on the shelf also counts as a resolvable category.
        self.assertFalse(MI.binding_needs_agent(
            {"inferredVisualType": "barChart", "values": ["Sales"],
             "categoryField": None, "rows": ["Region"], "cols": []}, cols))

    def test_no_category_intent_flips_to_card_not_agent(self):
        # A value-only chart with no category at all is a confident KPI card render,
        # not a guess -> never escalates even in output-first mode.
        cols = {"Region", "Sales"}
        self.assertFalse(MI.binding_needs_agent(
            {"inferredVisualType": "barChart", "values": ["Sales"],
             "categoryField": None, "rows": [], "cols": []}, cols))

    def test_date_grain_is_confident_category(self):
        cols = {"Sales"}
        self.assertFalse(MI.binding_needs_agent(
            {"inferredVisualType": "lineChart", "values": ["Sales"],
             "categoryField": "Order Date", "categoryDateLevel": "month"}, cols))


class TestReuseGuard(unittest.TestCase):
    """Output-first reuse guard: an agent fragment that fills measures but omits a
    visualDecision for a gated visual must NOT be reused (the visual would fall to a
    deterministic guess). _uncovered_visuals reports exactly the gated worksheets
    the merged decisions fail to cover."""

    def _decisions(self, worksheets):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"visualDecisions": [{"worksheet": w, "visualType": "barChart"}
                                           for w in worksheets]}, fh)
        self.addCleanup(os.remove, path)
        return path

    def test_all_covered_returns_empty(self):
        path = self._decisions(["A", "B"])
        self.assertEqual(MG._uncovered_visuals(path, ["A", "B"]), [])

    def test_missing_visual_decision_is_reported(self):
        path = self._decisions(["A"])
        self.assertEqual(MG._uncovered_visuals(path, ["A", "B"]), ["B"])

    def test_no_ambiguous_is_trivially_covered(self):
        path = self._decisions([])
        self.assertEqual(MG._uncovered_visuals(path, []), [])

    def test_unreadable_decisions_treats_all_uncovered(self):
        self.assertEqual(
            MG._uncovered_visuals("/no/such/decisions.json", ["A", "B"]), ["A", "B"])


class TestCategoryResolution(unittest.TestCase):
    """A chart binds exactly ONE category column. When a Tableau drill hierarchy
    is nested on a single shelf (region > subregion > state), the resolved
    category must be the LEAF, so the axis is one correct column instead of the
    coarse outer level with the inner levels stacked on as extra columns.
    """

    @staticmethod
    def _dim(field):
        return {"field": field, "isMeasure": False, "agg": None,
                "column": field, "dateLevel": None, "instanceName": f"[{field}]"}

    @staticmethod
    def _meas(field, agg="SUM"):
        return {"field": field, "isMeasure": True, "agg": agg,
                "column": field, "dateLevel": None, "instanceName": f"[{field}]"}

    def test_leaf_category_for_nested_rows_hierarchy(self):
        # rows = region > subregion > state (a drill hierarchy), measure on cols.
        instances = [self._dim("region"), self._dim("subregion"),
                     self._dim("state"), self._meas("loan_amount")]
        fields = V._resolve_fields(
            instances, rows=["region", "subregion", "state"],
            cols=["loan_amount"], enc={}, calc_map={}, measures={"loan_amount"})
        self.assertEqual(fields["category"], "state")

    def test_single_dim_on_cols_is_category(self):
        instances = [self._dim("purpose"), self._meas("loan_amount")]
        fields = V._resolve_fields(
            instances, rows=["loan_amount"], cols=["purpose"],
            enc={}, calc_map={}, measures={"loan_amount"})
        self.assertEqual(fields["category"], "purpose")

    def test_split_shelf_keeps_first_shelf_field_as_category(self):
        # grade on cols + loan_status on rows = category + series, not a leaf
        # hierarchy; the first shelf field stays the category.
        instances = [self._dim("grade"), self._dim("loan_status"),
                     self._meas("loan_amount")]
        fields = V._resolve_fields(
            instances, rows=["loan_status"], cols=["grade"],
            enc={}, calc_map={}, measures={"loan_amount"})
        self.assertEqual(fields["category"], "grade")


class TestDisplayUnitsNone(unittest.TestCase):
    """Every measure renderer pins display units to None (0) so the measure's own
    formatString scaling (e.g. ,,"M") is never re-scaled by Power BI's default
    Auto display units (which produced doubled units like "bnM" / "KK").
    """

    @staticmethod
    def _units(props_list):
        out = []
        for p in props_list:
            v = p.get("properties", {}).get("labelDisplayUnits")
            if v is not None:
                out.append(v["expr"]["Literal"]["Value"])
        return out

    def test_card_pins_display_units_none(self):
        v = PB.card_visual("c", {}, "loan", "Total Funded Amount")
        self.assertIn("1D", self._units(v["visual"]["objects"]["labels"]))

    def test_chart_labels_and_axis_pin_display_units_none(self):
        v = PB.chart_visual("c", {}, "clusteredColumnChart",
                            {"entity": "loan", "prop": "grade"},
                            {"entity": "loan", "prop": "Total Loans",
                             "isMeasure": True})
        objs = v["visual"]["objects"]
        self.assertIn("1D", self._units(objs["labels"]))
        self.assertIn("1D", self._units(objs["valueAxis"]))

    def test_treemap_labels_and_legend_pin_display_units_none(self):
        v = PB.treemap_visual("t", {}, {"entity": "loan", "prop": "purpose"},
                             {"entity": "loan", "prop": "Total Funded Amount",
                              "isMeasure": True})
        objs = v["visual"]["objects"]
        self.assertIn("1D", self._units(objs["dataLabels"]))
        self.assertIn("1D", self._units(objs["legend"]))

    def test_card_uses_measure_display_units_divisor(self):
        # A measure whose Tableau format scaled by millions carries
        # display_units=1_000_000; the card must divide+suffix via that divisor.
        v = PB.card_visual("c", {}, "loan", "Total Funded Amount",
                           display_units=1000000)
        self.assertIn("1000000D", self._units(v["visual"]["objects"]["labels"]))

    def test_chart_axis_and_labels_use_value_display_units(self):
        v = PB.chart_visual("c", {}, "clusteredColumnChart",
                            {"entity": "loan", "prop": "grade"},
                            {"entity": "loan", "prop": "Total Funded Amount",
                             "isMeasure": True, "displayUnits": 1000000})
        objs = v["visual"]["objects"]
        self.assertIn("1000000D", self._units(objs["labels"]))
        self.assertIn("1000000D", self._units(objs["valueAxis"]))


class TestScaleDivisor(unittest.TestCase):
    """The display-units divisor derived from a raw Tableau format drives the
    visual's labelDisplayUnits so a millions-scaled measure renders "$4,166.07M"
    instead of the full unscaled number.
    """

    def test_currency_millions(self):
        self.assertEqual(ET._tableau_scale_divisor('c"$"#,##0,,M'), 1000000)

    def test_thousands(self):
        self.assertEqual(ET._tableau_scale_divisor("c#,##0,K"), 1000)

    def test_percent_no_scale(self):
        self.assertEqual(ET._tableau_scale_divisor("p0.00%"), 1)

    def test_empty_no_scale(self):
        self.assertEqual(ET._tableau_scale_divisor(""), 1)
        self.assertEqual(ET._tableau_scale_divisor(None), 1)

    def test_measure_display_units_map(self):
        ir = {"worksheets": [
            {"name": "Total Funded Amount",
             "measures": [{"field": "loan_amount", "column": "loan_amount",
                           "format": 'c"$"#,##0,,M'}]},
            {"name": "Total Loans",
             "measures": [{"field": "loan_id", "column": "loan_id",
                           "format": "c#,##0,K"}]},
        ]}
        measures = [
            {"name": "Total Funded Amount", "dax": "SUM ( loan[loan_amount] )"},
            {"name": "Total Loans", "dax": "COUNT ( loan[loan_id] )"},
        ]
        units = ET.measure_display_units(measures, ir)
        self.assertEqual(units["Total Funded Amount"], 1000000)
        self.assertEqual(units["Total Loans"], 1000)



class TestTopNFilter(unittest.TestCase):
    """A Tableau Top-N filter on the plotted category must survive extraction
    (picking the leaf dimension when several nested levels each carry one) and be
    emitted as a Power BI visual Top-N filter, so 'Top 10 States' shows 10 states.
    """

    WS_XML = (
        "<worksheet name='Top 10 States'><table><view>"
        "<filter class='categorical' column='[fed].[none:region:nk]'>"
        "<groupfilter count='10' end='top' function='end'>"
        "<groupfilter direction='DESC' expression='SUM([loan_amount])' function='order'/>"
        "</groupfilter></filter>"
        "<filter class='categorical' column='[fed].[none:state (state_region.csv):nk]'>"
        "<groupfilter count='10' end='top' function='end'>"
        "<groupfilter direction='DESC' expression='SUM([loan_amount])' function='order'/>"
        "</groupfilter></filter>"
        "</view></table></worksheet>")

    @staticmethod
    def _ws(xml):
        import xml.etree.ElementTree as ETree
        return ETree.fromstring(xml)

    def test_picks_category_matched_topn(self):
        tn = V._topn_filter(self._ws(self.WS_XML), {}, "state (state_region.csv)")
        self.assertEqual(tn["field"], "state (state_region.csv)")
        self.assertEqual(tn["n"], 10)
        self.assertEqual(tn["direction"], "TOP")

    def test_falls_back_to_first_without_category(self):
        tn = V._topn_filter(self._ws(self.WS_XML), {})
        self.assertEqual(tn["field"], "region")

    def test_none_when_no_topn(self):
        ws = self._ws("<worksheet name='x'><table><view/></table></worksheet>")
        self.assertIsNone(V._topn_filter(ws, {}, "state"))

    def test_filter_config_structure(self):
        cfg = PB.topn_filter_config("loan", "state", 10, "TOP",
                                    order_measure="Total Funded Amount")
        f = cfg["filters"][0]
        self.assertEqual(f["type"], "VisualTopN")
        self.assertEqual(f["field"]["Column"]["Property"], "state")
        cond = f["filter"]["Where"][0]["Condition"]["VisualTopN"]
        self.assertEqual(cond["ItemCount"], 10)
        # VisualTopN ranks by the visual's own plotted measure -> no inline
        # OrderBy/Top is carried (those are rejected by the PBIR schema).
        self.assertNotIn("TopN", f["filter"]["Where"][0]["Condition"])

    def test_bottom_n_returns_none(self):
        # Bottom-N is not expressible as a VisualTopN, so no filter is emitted.
        cfg = PB.topn_filter_config("loan", "state", 5, "BOTTOM",
                                    order_measure="Total Funded Amount")
        self.assertIsNone(cfg)

    def test_emit_config_applies_on_matching_category(self):
        ws = {"topN": {"field": "state", "n": 10, "direction": "TOP",
                       "byMeasure": "SUM([loan_amount])"}}
        catbind = {"entity": "loan", "prop": "state"}
        valbind = {"entity": "loan", "prop": "Total Funded Amount", "isMeasure": True}
        cfg = EP._topn_config(ws, catbind, valbind)
        self.assertIsNotNone(cfg)
        self.assertEqual(cfg["filters"][0]["type"], "VisualTopN")

    def test_emit_config_skips_field_mismatch(self):
        ws = {"topN": {"field": "region", "n": 10, "direction": "TOP"}}
        catbind = {"entity": "loan", "prop": "state"}
        valbind = {"entity": "loan", "prop": "Total Funded Amount", "isMeasure": True}
        self.assertIsNone(EP._topn_config(ws, catbind, valbind))

    def test_emit_config_skips_non_measure_value(self):
        ws = {"topN": {"field": "state", "n": 10, "direction": "TOP"}}
        catbind = {"entity": "loan", "prop": "state"}
        valbind = {"entity": "loan", "prop": "state", "isMeasure": False}
        self.assertIsNone(EP._topn_config(ws, catbind, valbind))

    def test_emit_config_ranks_by_count_of_column(self):
        # 'Top 10 Genre' by COUNTD([show_id]): the plotted value is a text column,
        # not a model measure. The Top-N still resolves a ranking expression (the
        # confidence gate) and emits a schema-valid VisualTopN limited to 10 items;
        # VisualTopN ranks by the visual's own measure, so no inline OrderBy.
        ws = {"topN": {"field": "listed_in", "n": 10, "direction": "TOP",
                       "byMeasure": "COUNTD([show_id])"},
              "values": ["show_id"]}
        catbind = {"entity": "netflix_titles", "prop": "listed_in"}
        valbind = {"entity": "netflix_titles", "prop": "show_id", "isMeasure": False}
        cfg = EP._topn_config(ws, catbind, valbind, {}, {}, set())
        self.assertIsNotNone(cfg)
        f = cfg["filters"][0]
        self.assertEqual(f["type"], "VisualTopN")
        self.assertEqual(
            f["filter"]["Where"][0]["Condition"]["VisualTopN"]["ItemCount"], 10)

    def test_emit_config_resolves_copy_measure_for_table(self):
        # 'Top 10 Customers' table ranked by SUM([CY Sales (copy)_2378...]) must
        # resolve to the model measure 'CY Sales' the worksheet also plots.
        ws = {"topN": {"field": "Customer Name", "n": 10, "direction": "TOP",
                       "byMeasure": "SUM([CY Sales (copy)_237846410424180736])"},
              "values": ["CY Orders", "CY Profit", "CY Sales"]}
        tcols = [{"entity": "DimCustomer", "prop": "Customer Name", "isMeasure": False}]
        cfg = EP._topn_table_config(ws, tcols, "Sales", {}, {}, {"CY Sales", "CY Orders"})
        self.assertIsNotNone(cfg)
        f = cfg["filters"][0]
        self.assertEqual(f["field"]["Column"]["Property"], "Customer Name")
        # The copy-measure must resolve to a real model measure for the ranking
        # confidence gate to pass (else no filter); the emitted VisualTopN limits
        # the table to its 10 rows ranked by the visual's own measure.
        self.assertEqual(f["type"], "VisualTopN")
        self.assertEqual(
            f["filter"]["Where"][0]["Condition"]["VisualTopN"]["ItemCount"], 10)


class TestComboChart(unittest.TestCase):
    """Tableau dual-axis (one Bar pane + one Line pane over a shared category)
    must be recognised and emitted as a Power BI lineClusteredColumnComboChart,
    not silently dropped to a table or a single-measure bar chart.
    """

    def test_parser_extracts_pane_marks(self):
        import xml.etree.ElementTree as ETree
        ws = ETree.fromstring(
            "<worksheet name='Sales vs Profit'><table><panes>"
            "<pane><mark class='Bar'/></pane>"
            "<pane><mark class='Line'/></pane>"
            "</panes></table></worksheet>")
        self.assertEqual(V._pane_mark_classes(ws), ["Bar", "Line"])

    def test_mark_infer_detects_dual_axis(self):
        ws = {"markClass": "Bar", "paneMarks": ["Bar", "Line"],
              "dimensions": ["Month"], "values": ["CY Sales", "CY Profit"]}
        self.assertEqual(MI.infer_visual_type(ws), "comboChart")

    def test_mark_infer_single_pane_is_not_combo(self):
        ws = {"markClass": "Bar", "paneMarks": ["Bar"],
              "dimensions": ["Month"], "values": ["CY Sales"]}
        self.assertNotEqual(MI.infer_visual_type(ws), "comboChart")

    def test_mark_infer_needs_two_measures(self):
        # A bar+line dual pane with a single measure is not a combo.
        ws = {"markClass": "Bar", "paneMarks": ["Bar", "Line"],
              "dimensions": ["Month"], "values": ["CY Sales"]}
        self.assertNotEqual(MI.infer_visual_type(ws), "comboChart")

    def test_combo_visual_splits_y_and_y2(self):
        import json
        cat = {"entity": "Sales", "prop": "Month"}
        col_vals = [{"entity": "Sales", "prop": "CY Sales", "isMeasure": True}]
        line_vals = [{"entity": "Sales", "prop": "CY Profit", "isMeasure": True}]
        v = PB.combo_visual("v1", {"x": 0, "y": 0, "width": 100, "height": 100},
                            cat, col_vals, line_vals, "Sales vs Profit")
        vis = v["visual"]
        self.assertEqual(vis["visualType"], "lineClusteredColumnComboChart")
        qs = vis["query"]["queryState"]
        self.assertIn("Y", qs)
        self.assertIn("Y2", qs)
        self.assertIn("CY Sales", json.dumps(qs["Y"]))
        self.assertIn("CY Profit", json.dumps(qs["Y2"]))
        self.assertNotIn("CY Profit", json.dumps(qs["Y"]))


class TestRLSRoles(unittest.TestCase):
    """A Tableau row-level-security signal must emit a Power BI RLS role whose
    table permission filters a user-identity column by USERPRINCIPALNAME().
    """

    def test_no_role_when_not_detected(self):
        ir = {"rls": {"detected": False}}
        name, text = ET.build_roles(ir, {"tables": []})
        self.assertIsNone(name)
        self.assertIsNone(text)

    def test_mapping_table_role_filters_named_user_column(self):
        ir = {"rls": {"detected": True, "type": "Mapping-table",
                      "userColumn": "Username"}, "columns": []}
        decisions = {"tables": [
            {"name": "User Access", "role": "dim",
             "dedupKey": "Username", "keyColumns": ["Username"]},
            {"name": "Sales", "role": "fact"},
        ]}
        name, text = ET.build_roles(ir, decisions)
        self.assertEqual(name, "User Security")
        self.assertIn("role 'User Security'", text)
        self.assertIn("modelPermission: read", text)
        self.assertIn("tablePermission 'User Access' = ", text)
        self.assertIn("'User Access'[Username] = USERPRINCIPALNAME()", text)

    def test_dynamic_role_resolves_any_user_identity_column(self):
        ir = {"rls": {"detected": True, "type": "Dynamic",
                      "userColumn": None}, "columns": []}
        decisions = {"tables": [
            {"name": "Employee", "role": "dim",
             "dedupKey": "Email", "keyColumns": ["Email"]},
        ]}
        name, text = ET.build_roles(ir, decisions)
        self.assertEqual(name, "Dynamic Security")
        self.assertIn("Employee[Email] = USERPRINCIPALNAME()", text)

    def test_role_scaffold_when_no_user_column(self):
        # Detected but no user-identity column in the model -> valid read-only
        # scaffold (role + read permission, no table filter) the operator
        # completes manually.
        ir = {"rls": {"detected": True, "type": "Dynamic",
                      "userColumn": None}, "columns": []}
        decisions = {"tables": [
            {"name": "Sales", "role": "dim",
             "dedupKey": "Region", "keyColumns": ["Region"]},
        ]}
        name, text = ET.build_roles(ir, decisions)
        self.assertEqual(name, "Dynamic Security")
        self.assertIn("modelPermission: read", text)
        self.assertNotIn("tablePermission", text)

    def test_model_file_emits_ref_role(self):
        decisions = {"tables": [{"name": "Sales"}]}
        out = ET.build_model_file(decisions, ["User Security"])
        self.assertIn("ref role 'User Security'", out)

    def test_model_file_without_roles_has_no_ref_role(self):
        decisions = {"tables": [{"name": "Sales"}]}
        self.assertNotIn("ref role", ET.build_model_file(decisions))


class TestDateTrendRescue(unittest.TestCase):
    """An ambiguous worksheet that plots a DATE axis with measure pills is a time
    trend. With no agent decision it used to fall back to a flat table; the emit
    rescue must promote it to a line chart and keep every plotted measure series.
    """

    def _trend_ws(self):
        return {
            "name": "Weekly Trends", "markClass": "Automatic",
            "categoryField": "Order Date", "categoryDateLevel": "week",
            "rows": ["CY Sales", "CY Profit"], "cols": ["Order Date"],
            # dimensions is filter-inflated (the bug that made this ambiguous):
            "dimensions": ["Order Date", "Region", "Category", "State"],
            "values": ["Current Year", "CY Profit", "CY Sales"],
            "measures": [{"field": "CY Sales", "agg": "SUM", "column": "CY Sales"},
                         {"field": "CY Profit", "agg": "SUM", "column": "CY Profit"}],
            "encodings": {}, "inferredVisualType": None,
        }

    def test_is_date_trend_truth_table(self):
        mset = {"CY Sales", "CY Profit"}
        self.assertTrue(EP._is_date_trend(self._trend_ws(), mset))
        # No date grain -> not a trend.
        no_date = dict(self._trend_ws(), categoryDateLevel=None)
        self.assertFalse(EP._is_date_trend(no_date, mset))
        # Text mark (a real crosstab/table) is never promoted.
        text = dict(self._trend_ws(), markClass="Text")
        self.assertFalse(EP._is_date_trend(text, mset))
        # Date grain but no measure pill on a shelf -> not a trend.
        no_meas = dict(self._trend_ws(), rows=["Order Date"], cols=[])
        self.assertFalse(EP._is_date_trend(no_meas, {"CY Sales"}))

    def test_build_visual_promotes_trend_to_multiline(self):
        import json
        ws = self._trend_ws()
        ir = {
            "worksheets": [ws],
            "columns": [
                {"name": "Order Date", "role": "dimension", "dataType": "dateTime"},
                {"name": "CY Sales", "role": "measure", "dataType": "double"},
                {"name": "CY Profit", "role": "measure", "dataType": "double"},
            ],
        }
        decisions = {
            "tables": [{"name": "Sales", "role": "fact"}],
            "measures": [{"name": "CY Sales"}, {"name": "CY Profit"}],
            "visualDecisions": [],
        }
        zone = {"type": "worksheet", "worksheet": "Weekly Trends",
                "x": 0, "y": 0, "w": 400, "h": 300}
        v = EP.build_visual(zone, ir, decisions, 1, (0, 0, 400, 300, 300))
        self.assertIsNotNone(v)
        vis = v["visual"]
        # Promoted to a line chart, NOT a tableEx dump.
        self.assertEqual(vis["visualType"], "lineChart")
        # Both plotted measures survive as series.
        blob = json.dumps(vis)
        self.assertIn("CY Sales", blob)
        self.assertIn("CY Profit", blob)


class TestInlineAggBinding(unittest.TestCase):
    """A worksheet that plots an aggregation of a plain column (e.g.
    COUNTD([show_id])) on a fact table with NO named model measure must emit an
    inline visual-query Aggregation, not bind the raw column (which renders empty).
    Regression guard for the Netflix report whose Ratings / Top 10 Genre / pie /
    area visuals all plotted CNTD(Show Id) and came out blank.
    """

    def test_agg_func_countd_is_distinct_count(self):
        self.assertEqual(PBB.agg_func("COUNTD"), 2)
        self.assertEqual(PBB.agg_func("DISTINCTCOUNT"), 2)
        self.assertEqual(PBB.agg_label("COUNTD"), "Count")
        self.assertEqual(PBB.agg_func("MEDIAN"), None)

    def test_pill_agg_binding_builds_inline_agg(self):
        ws = {"measures": [{"column": "show_id", "agg": "COUNTD", "field": "show_id"}]}
        b = PBB.pill_agg_binding(ws, "netflix_titles", {"show_id", "rating"})
        self.assertEqual(b["prop"], "show_id")
        self.assertEqual(b["agg"], 2)
        self.assertEqual(b["entity"], "netflix_titles")

    def test_pill_agg_binding_none_without_inline_func(self):
        # MEDIAN has no single inline aggregate function -> no inline binding.
        ws = {"measures": [{"column": "x", "agg": "MEDIAN"}]}
        self.assertIsNone(PBB.pill_agg_binding(ws, "t", {"x"}))

    def test_pill_agg_binding_none_when_column_absent(self):
        ws = {"measures": [{"column": "ghost", "agg": "COUNTD"}]}
        self.assertIsNone(PBB.pill_agg_binding(ws, "t", {"show_id"}))

    def test_pill_agg_binding_resolves_calc_column_via_field(self):
        # Histogram customer-count axis: the pill's ``column`` is an internal
        # Tableau alias (not a real column) but ``field`` names an agent-authored
        # calculated column. COUNTD must bind to that calc column, NOT fall back to
        # a parameter-echo year. (Regression: 'Customer Distribution' Y-axis.)
        ws = {"measures": [{"column": "CY Sales (copy)_3221481164532666369",
                            "agg": "COUNTD", "field": "CY Customers"}]}
        decisions = {"calculatedColumns": [{"name": "CY Customers", "table": "Orders"}]}
        b = PBB.pill_agg_binding(ws, "Orders", {"Order ID", "Customer ID"},
                                 decisions=decisions)
        self.assertIsNotNone(b)
        self.assertEqual(b["prop"], "CY Customers")
        self.assertEqual(b["agg"], 2)
        self.assertEqual(b["entity"], "Orders")

    def test_binding_projection_emits_aggregation(self):
        proj = PB.binding_projection(
            {"entity": "netflix_titles", "prop": "show_id", "agg": 2,
             "aggLabel": "Count"})
        agg = proj["field"]["Aggregation"]
        self.assertEqual(agg["Function"], 2)
        self.assertEqual(agg["Expression"]["Column"]["Property"], "show_id")

    def _countd_ir(self, mark, vtype, dims, encodings=None):
        ws = {
            "name": "W", "markClass": mark, "inferredVisualType": vtype,
            "categoryField": dims[0], "valueField": "show_id",
            "dimensions": dims, "values": ["show_id"], "rows": ["show_id"],
            "cols": [dims[0]], "encodings": encodings or {},
            "measures": [{"column": "show_id", "agg": "COUNTD", "field": "show_id"}],
        }
        ir = {"worksheets": [ws],
              "columns": [{"name": d, "role": "dimension", "dataType": "string"}
                          for d in dims]
                         + [{"name": "show_id", "role": "dimension",
                             "dataType": "string"}]}
        decisions = {"tables": [{"name": "netflix_titles", "role": "fact"}],
                     "measures": [], "visualDecisions": []}
        return ws, ir, decisions

    def test_build_visual_column_chart_counts_distinct(self):
        _ws, ir, decisions = self._countd_ir("Automatic", "columnChart", ["rating"])
        zone = {"type": "worksheet", "worksheet": "W", "x": 0, "y": 0, "w": 400, "h": 300}
        v = EP.build_visual(zone, ir, decisions, 1, (0, 0, 400, 300, 300))
        qs = v["visual"]["query"]["queryState"]
        yproj = qs["Y"]["projections"][0]["field"]["Aggregation"]
        self.assertEqual(yproj["Function"], 2)
        self.assertEqual(yproj["Expression"]["Column"]["Property"], "show_id")

    def test_build_visual_circle_single_measure_is_pie(self):
        enc = {"color": "fed].[none:type:nk", "size": "fed].[ctd:show_id:qk",
               "text": "fed].[none:type:nk"}
        ws, ir, decisions = self._countd_ir("Circle", None, ["type"], enc)
        ws["rows"] = []
        ws["cols"] = []
        # No inferredVisualType -> emitter falls back to mark_infer, which must
        # promote a single-measure Circle to a pie (not a scatter -> table).
        zone = {"type": "worksheet", "worksheet": "W", "x": 0, "y": 0, "w": 400, "h": 300}
        v = EP.build_visual(zone, ir, decisions, 1, (0, 0, 400, 300, 300))
        self.assertEqual(v["visual"]["visualType"], "pieChart")
        yproj = v["visual"]["query"]["queryState"]["Y"]["projections"][0]
        self.assertEqual(yproj["field"]["Aggregation"]["Function"], 2)

    def test_mark_infer_circle_one_measure_is_pie(self):
        ws = {"markClass": "Circle", "values": ["show_id"], "dimensions": ["type"]}
        self.assertEqual(MI.infer_visual_type(ws), "pieChart")

    def test_mark_infer_circle_two_measures_stays_scatter(self):
        ws = {"markClass": "Circle", "values": ["sales", "profit"],
              "dimensions": ["region"]}
        self.assertEqual(MI.infer_visual_type(ws), "scatterChart")

    def test_category_binding_resolves_date_part_pseudo_field(self):
        ws = {"categoryField": "Year", "categoryDateLevel": "year",
              "dimensions": ["date_added", "type", "Year"]}
        ir = {"columns": [{"name": "date_added", "dataType": "date"},
                          {"name": "type", "dataType": "string"}]}
        b = PBB.category_binding(ws, "netflix_titles", {"type"}, ir)
        self.assertEqual(b["prop"], "date_added (Year)")

    def test_series_from_color_distinct_dimension(self):
        ws = {"encodings": {"color": "fed].[none:type:nk"}, "dimensions": ["type"]}
        self.assertEqual(PBB.series_from_color(ws, {"type", "Year"}, "Year"), "type")

    def test_series_from_color_none_when_equals_category(self):
        ws = {"encodings": {"color": "fed].[none:type:nk"}, "dimensions": ["type"]}
        self.assertIsNone(PBB.series_from_color(ws, {"type"}, "type"))

    def test_series_from_color_none_when_not_dimension(self):
        ws = {"encodings": {"color": "fed].[ctd:show_id:qk"}, "dimensions": ["type"]}
        self.assertIsNone(PBB.series_from_color(ws, {"show_id", "type"}, "type"))


class TestCalcFieldDedup(unittest.TestCase):
    """Calc fields dedup on the unique internal field id, NOT the display caption.

    Tableau repeats a calc's <column> in every worksheet-local datasource copy with
    the SAME internal id, so collapsing on the id keeps exactly one definition. Two
    DISTINCT fields (different ids) that merely share a display caption -- routine in
    workbooks full of renamed '(copy)' fields -- must BOTH survive, with the second
    caption suffixed, so neither measure is silently dropped downstream."""

    def _fields(self, xml):
        import xml.etree.ElementTree as XET
        return DS.extract_calculated_fields(XET.fromstring(xml))

    def test_distinct_fields_sharing_caption_both_survive(self):
        xml = (
            "<workbook><datasources><datasource name='federated.1'>"
            "<column caption='Margin' name='[Calc_a]'>"
            "<calculation class='tableau' formula='SUM([sales]) - SUM([cost])' />"
            "</column>"
            "<column caption='Margin' name='[Calc_b]'>"
            "<calculation class='tableau' formula='AVG([rate])' />"
            "</column>"
            "</datasource></datasources></workbook>"
        )
        fields = self._fields(xml)
        self.assertEqual(len(fields), 2)
        by_id = {f["fieldName"]: f["caption"] for f in fields}
        self.assertEqual(by_id["[Calc_a]"], "Margin")
        self.assertEqual(by_id["[Calc_b]"], "Margin (2)")
        # Names are unique end to end -> no merge-stage dedup can drop a measure.
        self.assertEqual(len({f["caption"] for f in fields}), 2)

    def test_worksheet_local_copies_collapse_on_id(self):
        # Same internal id repeated (federated def + a worksheet-local copy) must
        # collapse to ONE field, not produce a duplicate measure.
        xml = (
            "<workbook><datasources><datasource name='federated.1'>"
            "<column caption='Profit' name='[Calc_x]'>"
            "<calculation class='tableau' formula='SUM([p])' />"
            "</column>"
            "<column caption='Profit' name='[Calc_x]'>"
            "<calculation class='tableau' formula='SUM([p])' />"
            "</column>"
            "</datasource></datasources></workbook>"
        )
        fields = self._fields(xml)
        self.assertEqual(len(fields), 1)
        self.assertEqual(fields[0]["caption"], "Profit")

    def test_unique_captions_unchanged(self):
        xml = (
            "<workbook><datasources><datasource name='federated.1'>"
            "<column caption='Total Sales' name='[Calc_a]'>"
            "<calculation class='tableau' formula='SUM([sales])' />"
            "</column>"
            "<column caption='Total Cost' name='[Calc_b]'>"
            "<calculation class='tableau' formula='SUM([cost])' />"
            "</column>"
            "</datasource></datasources></workbook>"
        )
        caps = sorted(f["caption"] for f in self._fields(xml))
        self.assertEqual(caps, ["Total Cost", "Total Sales"])


class TestSynthesizePillMeasures(unittest.TestCase):
    """A fact table with zero measures whose worksheets aggregate a plain column
    (e.g. COUNTD(show_id)) must get a real model measure synthesized, so Power BI
    binds the chart value to a measure (renders) instead of an inline column
    aggregation (renders empty)."""

    def _ir(self):
        return {
            "columns": [{"name": "show_id", "datasource": "netflix_titles"},
                        {"name": "type", "datasource": "netflix_titles"}],
            "worksheets": [
                {"name": "Ratings",
                 "measures": [{"field": "show_id", "agg": "COUNTD",
                               "column": "show_id"}]},
                {"name": "Pie",
                 "measures": [{"field": "show_id", "agg": "COUNTD",
                               "column": "show_id"}]},
            ],
        }

    def test_synthesizes_distinctcount_measure(self):
        tables = [{"name": "netflix_titles", "role": "fact"}]
        syn = MD.synthesize_pill_measures(self._ir(), tables, [], "netflix_titles")
        self.assertEqual(len(syn), 1)  # deduped across the two worksheets
        m = syn[0]
        self.assertEqual(m["dax"], "DISTINCTCOUNT(netflix_titles[show_id])")
        self.assertEqual(m["table"], "netflix_titles")
        self.assertEqual(m["source"], "template")
        self.assertIn("show_id", m["name"])

    def test_no_synthesis_when_measure_already_expresses_pill(self):
        tables = [{"name": "netflix_titles", "role": "fact"}]
        existing = [{"name": "Count of Titles",
                     "dax": "DISTINCTCOUNT('netflix_titles'[show_id])",
                     "source": "template"}]
        syn = MD.synthesize_pill_measures(self._ir(), tables, existing,
                                          "netflix_titles")
        self.assertEqual(syn, [])

    def test_quotes_table_with_spaces(self):
        ir = {"columns": [{"name": "amount", "datasource": "Sales Orders"}],
              "worksheets": [{"name": "S",
                              "measures": [{"agg": "SUM", "column": "amount"}]}]}
        tables = [{"name": "Sales Orders", "role": "fact"}]
        syn = MD.synthesize_pill_measures(ir, tables, [], "Sales Orders")
        self.assertEqual(syn[0]["dax"], "SUM('Sales Orders'[amount])")

    def test_skips_worksheets_without_measure_pills(self):
        ir = {"columns": [{"name": "type", "datasource": "t"}],
              "worksheets": [{"name": "TextOnly", "measures": []}]}
        syn = MD.synthesize_pill_measures(ir, [{"name": "t"}], [], "t")
        self.assertEqual(syn, [])

    def test_merge_injects_synthesized_measure(self):
        ir = {"workbook": {"pascalName": "NetfixWorkbook"}, **self._ir()}
        schema_easy = {"tableStrategy": "single-flat",
                       "tables": [{"name": "netflix_titles", "role": "fact"}],
                       "relationships": []}
        out = MD.merge(ir, {"measures": []}, schema_easy, {})
        names = [m["name"] for m in out["measures"]]
        self.assertTrue(any("show_id" in n for n in names))
        dax = [m["dax"] for m in out["measures"]]
        self.assertIn("DISTINCTCOUNT(netflix_titles[show_id])", dax)

    def test_multitable_fact_column_homes_on_fact(self):
        """The Loan regression: an inline AVG(int_rate) card on a STAR schema must
        still get a real measure (was skipped when the model had >1 table). The
        column lives on the fact, so the measure is homed there."""
        ir = {
            "columns": [{"name": "int_rate", "datasource": "loan"}],
            "worksheets": [
                {"name": "Avg Interest Rate",
                 "measures": [{"agg": "AVG", "column": "int_rate"}]},
            ],
        }
        tables = [
            {"name": "loan", "role": "fact"},
            {"name": "DimRegion", "role": "dim", "sourceDatasource": "DimRegion"},
        ]
        syn = MD.synthesize_pill_measures(ir, tables, [], "loan")
        self.assertEqual(len(syn), 1)
        self.assertEqual(syn[0]["table"], "loan")
        self.assertEqual(syn[0]["dax"], "AVERAGE(loan[int_rate])")

    def test_homes_measure_on_owning_dim_in_star_schema(self):
        """A pill column owned by a dimension table is homed on that dim (not the
        fact), so the measure DAX references a table that actually holds it."""
        ir = {
            "columns": [
                {"name": "amount", "datasource": "Fact"},
                {"name": "rate", "datasource": "DimRate"},
            ],
            "worksheets": [
                {"name": "AvgRate",
                 "measures": [{"agg": "AVG", "column": "rate"}]},
            ],
        }
        tables = [
            {"name": "Fact", "role": "fact"},
            {"name": "DimRate", "role": "dim", "sourceDatasource": "DimRate"},
        ]
        syn = MD.synthesize_pill_measures(ir, tables, [], "Fact")
        self.assertEqual(len(syn), 1)
        self.assertEqual(syn[0]["table"], "DimRate")
        self.assertEqual(syn[0]["dax"], "AVERAGE(DimRate[rate])")

    def test_synthesizes_for_every_pill_not_just_first(self):
        """A combo/detail visual carries several measure pills; every one needs a
        model measure (a later pill's inline aggregation is dropped by Desktop too),
        not only the first."""
        ir = {
            "columns": [
                {"name": "sales", "datasource": "F"},
                {"name": "profit", "datasource": "F"},
            ],
            "worksheets": [
                {"name": "Combo",
                 "measures": [
                     {"agg": "SUM", "column": "sales"},
                     {"agg": "SUM", "column": "profit"},
                 ]},
            ],
        }
        tables = [{"name": "F", "role": "fact"}]
        syn = MD.synthesize_pill_measures(ir, tables, [], "F")
        self.assertEqual(sorted(m["dax"] for m in syn),
                         ["SUM(F[profit])", "SUM(F[sales])"])


class TestSynthesizeLodBinColumns(unittest.TestCase):
    """A { FIXED [A]: COUNTD([B]) } dimension-role LOD must become a calculated
    COLUMN (a histogram bin axis), never a measure -- so a chart can group on it."""

    def _ir(self):
        return {
            "columns": [
                {"name": "Customer ID", "datasource": "S"},
                {"name": "Order ID", "datasource": "S"},
                {"name": "Order Date", "datasource": "S"},
            ],
            "calculatedFields": [
                {"caption": "CY Customers", "role": "dimension",
                 "formula": "IF YEAR([Order Date]) = [Select Year] THEN [Customer ID] END",
                 "dependsOn": ["Order Date", "Select Year", "Customer ID"]},
                {"caption": "CY Orders", "role": "dimension",
                 "formula": "IF YEAR([Order Date]) = [Select Year] THEN [Order ID] END",
                 "dependsOn": ["Order Date", "Select Year", "Order ID"]},
                {"caption": "Nr of Orders per Customers", "role": "dimension",
                 "isLOD": True,
                 "formula": "{ FIXED [CY Customers]: COUNTD([CY Orders])}",
                 "dependsOn": ["CY Customers", "CY Orders"]},
            ],
        }

    def test_emits_allexcept_calc_column_on_fact(self):
        tables = [{"name": "Orders", "role": "fact"}]
        cols = MD.synthesize_lod_bin_columns(self._ir(), tables, "Orders")
        self.assertEqual(len(cols), 1)
        c = cols[0]
        self.assertEqual(c["name"], "Nr of Orders per Customers")
        self.assertEqual(c["table"], "Orders")
        # THEN-branch resolves the nested calcs to the physical columns, not the
        # IF-condition 'Order Date', and COUNTD -> DISTINCTCOUNT.
        self.assertEqual(
            c["dax"],
            "CALCULATE(DISTINCTCOUNT(Orders[Order ID]), ALLEXCEPT(Orders, Orders[Customer ID]))")
        self.assertEqual(c["dataType"], "int64")

    def test_merge_drops_shadow_measure_keeps_column(self):
        """When the agent wrongly authored the LOD as a measure, merge must drop the
        measure and keep the deterministic calc column (so the axis is groupable)."""
        ir = self._ir()
        ir["workbook"] = {"pascalName": "Sales"}
        schema = {"tableStrategy": "star-schema",
                  "tables": [{"name": "Orders", "role": "fact"}],
                  "relationships": []}
        agent = {"measures": [
                     {"table": "Orders", "name": "Nr of Orders per Customers",
                      "dax": "DIVIDE([CY Orders],[CY Customers])"}],
                 "calculatedColumns": []}
        decisions = MD.merge(ir, {}, schema, agent)
        names_m = {m["name"] for m in decisions["measures"]}
        names_c = {c["name"] for c in decisions["calculatedColumns"]}
        self.assertNotIn("Nr of Orders per Customers", names_m)
        self.assertIn("Nr of Orders per Customers", names_c)

    def test_no_lod_no_columns(self):
        ir = {"columns": [{"name": "x"}], "calculatedFields": [
            {"caption": "Plain", "role": "measure", "formula": "SUM([x])"}]}
        self.assertEqual(
            MD.synthesize_lod_bin_columns(ir, [{"name": "F", "role": "fact"}], "F"), [])


class TestPivotMatrixLayout(unittest.TestCase):
    """A Tableau text-table / cross-tab (a multi-level ROW hierarchy + measure value
    columns) must become a Power BI MATRIX -- row dimensions as the hierarchy, the
    measures as cell values bound to their OWNING table -- not a flat detail table
    (scrambled column order) nor a mis-inferred map."""

    def _ir(self):
        return {"columns": [
            {"name": "licensed_beds", "datasource": "S"},
            {"name": "department_name", "datasource": "S"},
            {"name": "department_level_of_care_group", "datasource": "S"},
            {"name": "encounter_count", "datasource": "E"},
            {"name": "parent_location_name", "datasource": "E"},
            {"name": "location_name", "datasource": "E"},
        ]}

    def _decisions(self):
        return {
            "tables": [
                {"name": "Census", "role": "fact", "sourceDatasource": "S"},
                {"name": "EDCensus", "role": "dim", "sourceDatasource": "E"},
            ],
            "calculatedColumns": [
                {"name": "Entity", "table": "Census",
                 "dax": "SWITCH(Census[parent], \"x\", \"y\")"},
                {"name": "Licensed", "table": "Census",
                 "dax": "FORMAT ( Census[licensed_beds], \"0\" )"},
            ],
            "measures": [
                {"name": "% Occ.", "table": "Census", "dax": "DIVIDE(1, 2)"},
                {"name": "Sum of encounter_count", "table": "EDCensus",
                 "dax": "SUM(EDCensus[encounter_count])"},
            ],
        }

    def test_text_crosstab_becomes_matrix_rows_and_values(self):
        ir, dec = self._ir(), self._decisions()
        cols = PBB.column_names(ir)
        ws = {"markClass": "Text",
              "rows": ["Entity", "Level Of Care (group)", "department_name",
                       "Licensed"],
              "cols": ["Measure Names"], "values": ["% Occ."],
              "measures": [{"field": "% Occ.", "agg": "SUM", "column": "calc_x"}]}
        layout = PBB.pivot_matrix_layout(ws, ir, "Census", {"% Occ."}, cols, dec)
        self.assertIsNotNone(layout)
        # 'Level Of Care (group)' resolves to its physical column; all 3 dims kept.
        self.assertEqual([r["prop"] for r in layout["rows"]],
                         ["Entity", "department_level_of_care_group", "department_name"])
        self.assertTrue(all(r["entity"] == "Census" for r in layout["rows"]))
        # FORMAT(table[col]) label -> SUM(base column) with the friendly header.
        lic = layout["values"][0]
        self.assertEqual(lic["prop"], "licensed_beds")
        self.assertEqual(lic["agg"], 0)
        self.assertEqual(lic["displayName"], "Licensed")
        self.assertEqual(lic["entity"], "Census")
        # The % ratio measure folds in from the values shelf.
        occ = layout["values"][-1]
        self.assertEqual(occ["prop"], "% Occ.")
        self.assertTrue(occ["isMeasure"])

    def test_single_dimension_stays_flat(self):
        ir, dec = self._ir(), self._decisions()
        cols = PBB.column_names(ir)
        ws = {"markClass": "Text", "rows": ["Entity"], "cols": [],
              "values": ["% Occ."],
              "measures": [{"field": "% Occ.", "agg": "SUM", "column": "calc_x"}]}
        self.assertIsNone(
            PBB.pivot_matrix_layout(ws, ir, "Census", {"% Occ."}, cols, dec))

    def test_measure_pill_binds_to_owning_table(self):
        """A cross-table aggregated pill (SUM(encounter_count)) maps to its model
        measure on the table that actually owns it, never the report's primary
        fact -- the fix for the binding-integrity cross-table warning."""
        ir, dec = self._ir(), self._decisions()
        cols = PBB.column_names(ir)
        ws = {"markClass": "Polygon",
              "rows": ["parent_location_name", "location_name", "encounter_count"],
              "cols": [], "values": ["encounter_count"],
              "measures": [{"field": "encounter_count", "agg": "SUM",
                            "column": "encounter_count"}]}
        layout = PBB.pivot_matrix_layout(
            ws, ir, "Census", {"% Occ.", "Sum of encounter_count"}, cols, dec)
        self.assertIsNotNone(layout)
        self.assertEqual([r["prop"] for r in layout["rows"]],
                         ["parent_location_name", "location_name"])
        val = layout["values"][0]
        self.assertEqual(val["prop"], "Sum of encounter_count")
        self.assertTrue(val["isMeasure"])
        self.assertEqual(val["entity"], "EDCensus")


class TestTrendTemporalAxis(unittest.TestCase):
    """A Tableau line/area trend places the continuous date pill on the COLUMNS
    shelf (the X axis) and the metric on rows. The parser can mislabel the date as
    a MAX(date) value pill -> a datetime on the Y axis, which crashes Power BI's
    cartesian renderer ('categoryIdentities is not a function'). The emitter must
    detect such a temporal value and keep dates off the value axis."""

    def _dec(self):
        return {"measures": [
            {"name": "Max of EXTRACT_DATETIME", "table": "T",
             "dax": "MAX(T[EXTRACT_DATETIME])"},
            {"name": "Daily Census Tooltip", "table": "T",
             "dax": "(SUM ( T[occupied_beds] )+SUM ( T[unavailable_beds] ))"
                    "/DISTINCTCOUNT ( T[extract_date] )"},
            {"name": "% Occ.", "table": "T",
             "dax": "DIVIDE ( SUM ( T[occupied_beds] ), SUM ( T[staffed_beds] ) )"},
        ]}

    def test_max_of_date_measure_is_temporal(self):
        vb = {"entity": "T", "prop": "Max of EXTRACT_DATETIME", "isMeasure": True}
        self.assertTrue(EP._is_temporal_value(vb, {"EXTRACT_DATETIME"}, self._dec()))

    def test_inline_date_aggregation_is_temporal(self):
        vb = {"entity": "T", "prop": "extract_date", "agg": 3}
        self.assertTrue(EP._is_temporal_value(vb, {"extract_date"}, self._dec()))

    def test_real_metric_is_not_temporal(self):
        vb = {"entity": "T", "prop": "% Occ.", "isMeasure": True}
        self.assertFalse(EP._is_temporal_value(vb, {"EXTRACT_DATETIME", "extract_date"},
                                               self._dec()))

    def test_measure_for_pill_anchors_to_exact_aggregation(self):
        """A SUM([occupied_beds]) pill must NOT bind to a composite measure that
        merely CONTAINS that aggregation (the loose-substring bug that plotted the
        'Daily Census Tooltip' ratio on a '# Occupied' trend)."""
        ws = {"measures": [{"field": "occupied_beds", "agg": "SUM",
                            "column": "occupied_beds"}]}
        # Only the composite measure exists -> no exact match -> None (inline agg).
        self.assertIsNone(PBB.measure_for_pill(
            ws, self._dec(), {"Daily Census Tooltip", "% Occ."}))
        # The exact SUM measure is preferred when present.
        dec = self._dec()
        dec["measures"].append({"name": "Sum of occupied_beds", "table": "T",
                                "dax": "SUM(T[occupied_beds])"})
        self.assertEqual(
            PBB.measure_for_pill(ws, dec,
                                 {"Daily Census Tooltip", "Sum of occupied_beds"}),
            "Sum of occupied_beds")


class TestTextTableColumns(unittest.TestCase):
    """A Tableau text-table mark (only a Text pill, no rows/cols/values) must emit a
    single-column table of the text-encoded dimension — Detail/Tooltip dims in the
    `dimensions` list must NOT leak in as extra columns."""

    def _ir(self):
        return {"columns": [{"name": "rating"}, {"name": "title"}, {"name": "type"}]}

    def test_text_only_mark_keeps_only_text_field(self):
        ws = {"rows": [], "cols": [], "values": [], "measures": [],
              "dimensions": ["rating", "title", "type"],
              "encodings": {"text": "federated.x].[none:rating:nk"}}
        out = PBB.table_columns(ws, self._ir(), "t", set(),
                                {"rating", "title", "type"})
        self.assertEqual([c["prop"] for c in out], ["rating"])

    def test_crosstab_with_rows_keeps_all_dims(self):
        # A real cross-tab (rows present) must NOT be reduced to one column.
        ws = {"rows": ["rating"], "cols": [], "values": [], "measures": [],
              "dimensions": ["rating", "title"],
              "encodings": {"text": "federated.x].[none:rating:nk"}}
        out = PBB.table_columns(ws, self._ir(), "t", set(), {"rating", "title"})
        self.assertEqual([c["prop"] for c in out], ["rating", "title"])


class TestDrillPathHierarchies(unittest.TestCase):
    """Tableau drill-path hierarchies -> Power BI model hierarchies."""

    def _root(self, drill_xml: str):
        import xml.etree.ElementTree as ETree
        xml = (
            "<workbook><datasources>"
            "<datasource caption='Sales' name='federated.1'>"
            f"{drill_xml}"
            "</datasource></datasources></workbook>"
        )
        return ETree.fromstring(xml)

    def test_compact_name_form_levels_recovered(self):
        # Compact form: levels live in the name attr, no child <field> elements.
        root = self._root(
            "<drill-paths><drill-path name='Country_Region, State, City' />"
            "</drill-paths>")
        hiers = TM.extract_hierarchies(root)
        self.assertEqual(len(hiers), 1)
        self.assertEqual(hiers[0]["levels"], ["Country_Region", "State", "City"])

    def test_verbose_field_form_still_works(self):
        # The explicit <field> form remains the primary path.
        root = self._root(
            "<drill-paths><drill-path name='Geo'>"
            "<field>[Country]</field><field>[State]</field>"
            "</drill-path></drill-paths>")
        hiers = TM.extract_hierarchies(root)
        self.assertEqual(hiers[0]["levels"], ["Country", "State"])

    def test_single_token_name_is_not_a_hierarchy(self):
        # A one-word drill-path name must not be treated as a hierarchy.
        root = self._root("<drill-paths><drill-path name='Country' /></drill-paths>")
        self.assertEqual(TM.extract_hierarchies(root), [])

    def test_hierarchy_block_references_columns(self):
        block = B.hierarchy_block(
            "Country_Region, State, City",
            [("Country_Region", "Country_Region"), ("State", "State"),
             ("City", "City")], 800)
        self.assertIn("\thierarchy 'Country_Region, State, City'", block)
        self.assertEqual(block.count("\t\tlevel "), 3)
        self.assertIn("\t\t\tcolumn: Country_Region", block)
        self.assertIn("\t\t\tcolumn: State", block)
        self.assertIn("\t\t\tcolumn: City", block)

    def test_emit_owns_hierarchy_when_all_levels_resolve(self):
        ir = {"hierarchies": [
            {"name": "Geo", "levels": ["Country_Region", "State", "City"]}]}
        blocks = ET._hierarchies_for_table(
            ["Postal_Code", "City", "State", "Region", "Country_Region"], ir, 1)
        self.assertEqual(len(blocks), 1)
        self.assertIn("\thierarchy Geo", blocks[0])

    def test_emit_skips_when_a_level_is_missing(self):
        # Only two of three levels exist here -> not this table's hierarchy.
        ir = {"hierarchies": [
            {"name": "Geo", "levels": ["Country_Region", "State", "City"]}]}
        blocks = ET._hierarchies_for_table(["State", "City"], ir, 1)
        self.assertEqual(blocks, [])

    def test_emit_tolerant_match_caption_vs_column(self):
        # Tableau caption 'Country/Region' resolves to column 'Country_Region'.
        ir = {"hierarchies": [
            {"name": "Geo", "levels": ["Country/Region", "State"]}]}
        blocks = ET._hierarchies_for_table(["Country_Region", "State"], ir, 1)
        self.assertEqual(len(blocks), 1)
        self.assertIn("column: Country_Region", blocks[0])

    def test_sales_workbook_hierarchy_round_trips(self):
        # End-to-end against the real Sales workbook: the drill-path hierarchy is
        # captured in the IR (no longer silently dropped).
        if not os.path.isfile(SALES_TWB):
            self.skipTest("Sales workbook fixture not present")
        ir = P.build_ir(SALES_TWB)
        hiers = ir.get("hierarchies") or []
        self.assertTrue(hiers, "Sales drill-path hierarchy must be captured")
        self.assertTrue(all(len(h.get("levels") or []) >= 2 for h in hiers))


class TestCategoryBinding(unittest.TestCase):
    """A chart category must bind to the worksheet's real dimension, never the
    fact key ('Order ID') just because the parser's categoryField was a measure."""

    DECISIONS = {
        "tables": [
            {"name": "Orders", "role": "fact", "sourceDatasource": None,
             "keyColumns": ["Order ID", "Sales"]},
            {"name": "Products", "role": "dim", "sourceDatasource": None,
             "keyColumns": ["Sub-Category", "Category"]},
            {"name": "Customers", "role": "dim", "sourceDatasource": None,
             "keyColumns": ["Customer_Name", "Customer_ID"]},
        ],
        "measures": [{"name": "KPI CY Less PY"}, {"name": "CY Sales"},
                     {"name": "Nr of Orders per Customers"}, {"name": "CY Customers"}],
        "calculatedColumns": [],
    }
    IR = {"columns": [{"name": "Order ID", "dataType": "string",
                       "role": "dimension"}], "worksheets": []}

    def _bind(self, ws):
        return PBB.category_binding(ws, "Orders", {"Order ID", "Sales"},
                                    self.IR, self.DECISIONS)

    def test_measure_categoryfield_recovers_dimension_from_rows(self):
        ws = {"categoryField": "KPI CY Less PY",
              "rows": ["Sub-Category", "KPI CY Less PY"], "cols": ["CY Sales"]}
        b = self._bind(ws)
        self.assertEqual(b["entity"], "Products")
        self.assertEqual(b["prop"], "Sub-Category")

    def test_tolerant_match_emits_real_model_column_name(self):
        # IR pill 'Customer Name' (space) -> model column 'Customer_Name' (underscore).
        ws = {"categoryField": "Customer Name", "rows": ["Customer Name"], "cols": []}
        b = self._bind(ws)
        self.assertEqual(b["entity"], "Customers")
        self.assertEqual(b["prop"], "Customer_Name")

    def test_all_measure_shelves_fall_back_not_crash(self):
        # No real dimension on rows/cols -> last-ditch first_dim_col (Order ID).
        ws = {"categoryField": "CY Sales", "rows": ["CY Sales"],
              "cols": ["Nr of Orders per Customers"]}
        b = self._bind(ws)
        self.assertEqual(b["prop"], "Order ID")

    def test_real_dim_categoryfield_binds_directly(self):
        ws = {"categoryField": "Sub-Category", "rows": ["Sub-Category"], "cols": []}
        b = self._bind(ws)
        self.assertEqual(b["entity"], "Products")
        self.assertEqual(b["prop"], "Sub-Category")

    def test_resolve_model_column_prefers_dim_over_fact_duplicate(self):
        # Regression: a fact that carries a denormalised 'Customer Name' (space)
        # copy AND a dim that owns the canonical 'Customer_Name' (underscore) both
        # normalise to the same key. The resolver must DETERMINISTICALLY return the
        # dimension's column (what emit_tmdl actually emits), never the fact's
        # phantom space-spelled duplicate -- which the model dropped, so binding to
        # it fails validate:bindings (and the winner flipped per run under hash
        # randomisation of the raw column set).
        decisions = {
            "tables": [
                {"name": "Orders", "role": "fact", "sourceDatasource": None,
                 "keyColumns": ["Customer Name"]},
                {"name": "Customers", "role": "dim", "sourceDatasource": None,
                 "keyColumns": ["Customer_Name"]},
            ],
            "calculatedColumns": [],
        }
        rc = PBB._resolve_model_column(
            "Customer Name", {"Customer Name", "Customer_Name"},
            {"columns": []}, decisions)
        self.assertEqual(rc, "Customer_Name")

    def test_category_binds_dim_not_fact_phantom(self):
        # End-to-end of the above through category_binding: the 'Top Customers'
        # worksheet (categoryField 'Measure Names', dimension on the rows shelf)
        # must bind to Customers.'Customer_Name', never Orders.'Customer Name'.
        decisions = {
            "tables": [
                {"name": "Orders", "role": "fact", "sourceDatasource": None,
                 "keyColumns": ["Order ID", "Sales", "Customer Name"]},
                {"name": "Customers", "role": "dim", "sourceDatasource": None,
                 "keyColumns": ["Customer_ID", "Customer_Name"]},
            ],
            "measures": [{"name": "CY Orders"}],
            "calculatedColumns": [],
        }
        ws = {"categoryField": "Measure Names",
              "rows": ["Calculation", "Customer Name", "Order Date"],
              "cols": ["Measure Names"]}
        b = PBB.category_binding(
            ws, "Orders",
            {"Order ID", "Sales", "Customer Name", "Customer_Name"},
            {"columns": [], "worksheets": []}, decisions)
        self.assertEqual(b["entity"], "Customers")
        self.assertEqual(b["prop"], "Customer_Name")

    def test_calc_column_on_dim_binds_as_category_axis(self):
        # A per-customer LOD count authored as a CALCULATED COLUMN on Customers
        # (the Tableau '{FIXED [Customer]: COUNTD([Order Id])}' histogram bin field,
        # role=dimension) must bind on its declared dim table -- NOT the fact -- so
        # the distribution chart can put it on the category axis. Before the fix
        # entity_for_field only saw physical CSV columns, so a dim calc column
        # mis-resolved to Orders and validate:bindings failed.
        decisions = {
            "tables": [
                {"name": "Orders", "role": "fact", "sourceDatasource": None,
                 "keyColumns": ["Order ID", "Sales"]},
                {"name": "Customers", "role": "dim", "sourceDatasource": None,
                 "keyColumns": ["Customer_ID"]},
            ],
            "measures": [{"name": "CY Customers"}],
            "calculatedColumns": [
                {"table": "Customers", "name": "Nr of Orders per Customers",
                 "dax": "CALCULATE(DISTINCTCOUNT(Orders[Order ID]))"},
            ],
        }
        ws = {"categoryField": "Nr of Orders per Customers",
              "rows": ["CY Customers"], "cols": ["Nr of Orders per Customers"]}
        b = PBB.category_binding(ws, "Orders", {"Order ID", "Sales"},
                                 {"columns": [], "worksheets": []}, decisions)
        self.assertEqual(b["entity"], "Customers")
        self.assertEqual(b["prop"], "Nr of Orders per Customers")

    def test_entity_for_field_resolves_dim_calc_column(self):
        decisions = {
            "tables": [
                {"name": "Orders", "role": "fact", "sourceDatasource": None,
                 "keyColumns": ["Order ID"]},
                {"name": "Customers", "role": "dim", "sourceDatasource": None,
                 "keyColumns": ["Customer_ID"]},
            ],
            "calculatedColumns": [
                {"table": "Customers", "name": "Nr of Orders per Customers"},
            ],
        }
        ent = PBB.entity_for_field("Nr of Orders per Customers", "Orders",
                                   decisions, {"columns": []})
        self.assertEqual(ent, "Customers")


class TestKpiCardValue(unittest.TestCase):
    """A KPI card's headline must be the metric, never the year-selector parameter
    echo ('Current Year' = SELECTEDVALUE of the year slicer, which returns 2023)."""

    DECISIONS = {
        "tables": [
            {"name": "Orders", "role": "fact"},
            {"name": "Select Year", "role": "param"},
        ],
        "measures": [
            {"name": "Current Year",
             "dax": "SELECTEDVALUE('Select Year'[Select Year], 2023)"},
            {"name": "Previous Year",
             "dax": "SELECTEDVALUE('Select Year'[Select Year], 2023) - 1"},
            {"name": "Select Year Value",
             "dax": "SELECTEDVALUE('Select Year'[Select Year], 2023)"},
            {"name": "CY Sales",
             "dax": "CALCULATE(SUM(Orders[Sales]), Orders[Year] = [Select Year Value])"},
            {"name": "PY Sales",
             "dax": "CALCULATE(SUM(Orders[Sales]), Orders[Year] = [Select Year Value] - 1)"},
            {"name": "% Diff Sales", "dax": "DIVIDE([CY Sales]-[PY Sales],[PY Sales])"},
            {"name": "Min/Max Sales", "dax": "MIN([CY Sales])"},
        ],
        "calculatedColumns": [],
    }

    def test_parameter_echo_detection(self):
        echo = PBB.parameter_echo_measures(self.DECISIONS)
        self.assertIn("Current Year", echo)
        self.assertIn("Previous Year", echo)
        self.assertIn("Select Year Value", echo)
        # Real aggregating metrics are never flagged as echoes.
        self.assertNotIn("CY Sales", echo)
        self.assertNotIn("% Diff Sales", echo)
        self.assertNotIn("Min/Max Sales", echo)

    def test_kpi_card_picks_metric_not_year(self):
        ws = {"valueField": "Current Year",
              "values": ["Current Year", "Previous Year", "PY Sales",
                         "CY Sales", "% Diff Sales", "Min/Max Sales"]}
        mset = {m["name"] for m in self.DECISIONS["measures"]}
        echo = PBB.parameter_echo_measures(self.DECISIONS)
        m = PBB.kpi_value_measure(ws, "KPI Sales", mset, echo, self.DECISIONS)
        self.assertEqual(m, "CY Sales")

    def test_kpi_card_per_customer_sheet(self):
        ws = {"valueField": "Current Year",
              "values": ["Current Year", "CY Sales per Customer",
                         "PY Sales per Customer", "% Diff"]}
        mset = {"Current Year", "CY Sales per Customer",
                "PY Sales per Customer", "% Diff"}
        echo = {"Current Year"}
        m = PBB.kpi_value_measure(ws, "KPI Sales Per Customers", mset, echo, self.DECISIONS)
        self.assertEqual(m, "CY Sales per Customer")


class TestScreenshotOverlay(unittest.TestCase):
    """The screenshot (vision) layer overlays visual INTENT onto the deterministic
    decisions by precedence — one decision per worksheet, never a duplicate."""

    IR = {"worksheets": [{"name": "KPI Sales"}, {"name": "Customer Distribution"}]}

    def test_dashboard_from_filename(self):
        self.assertEqual(SO._dashboard_from_filename("SalesDashboard_Close filter.png"),
                         "SalesDashboard")
        self.assertEqual(SO._dashboard_from_filename("Sales Dashboard_Open filter.png"),
                         "Sales Dashboard")
        self.assertEqual(SO._dashboard_from_filename("Customer Dashboard_Close filter.png"),
                         "Customer Dashboard")

    def test_hint_upgrades_type_and_logs_discrepancy(self):
        vds = [{"worksheet": "KPI Sales", "visualType": "card", "reason": "x"}]
        hints = [{"worksheet": "KPI Sales", "visualType": "kpiStack", "note": "tile"}]
        merged, disc = SO.apply_visual_hints(vds, hints, self.IR)
        self.assertEqual(len(merged), 1)  # no duplicate visual
        self.assertEqual(merged[0]["visualType"], "kpiStack")
        self.assertEqual(len(disc), 1)
        self.assertEqual((disc[0]["from"], disc[0]["to"]), ("card", "kpiStack"))

    def test_no_hints_is_noop(self):
        vds = [{"worksheet": "KPI Sales", "visualType": "card"}]
        merged, disc = SO.apply_visual_hints(vds, None, self.IR)
        self.assertEqual([d["visualType"] for d in merged], ["card"])
        self.assertEqual(disc, [])

    def test_unknown_worksheet_hint_skipped(self):
        vds = [{"worksheet": "KPI Sales", "visualType": "card"}]
        hints = [{"worksheet": "Ghost Sheet", "visualType": "kpiStack"}]
        merged, disc = SO.apply_visual_hints(vds, hints, self.IR)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["visualType"], "card")
        self.assertTrue(any(d.get("note") == "hint worksheet not in workbook" for d in disc))

    def test_hint_for_undecided_worksheet_appends_single(self):
        merged, _ = SO.apply_visual_hints(
            [], [{"worksheet": "Customer Distribution", "visualType": "barChart"}], self.IR)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["visualType"], "barChart")


class TestKpiTileDecision(unittest.TestCase):
    """A 'kpiStack' decision is enriched into a rich tile (CY headline + PY +
    %Diff + sparkline) from the worksheet's CY/PY measures and date grain."""

    def test_builds_kpistack_from_cy_py(self):
        ws = {"name": "KPI Sales", "categoryDateLevel": "month",
              "categoryField": "Order Date",
              "title": "Total Sales\n{value}\n{value}  vs. PY",
              "measures": [{"field": "Current Year"}, {"field": "CY Sales"},
                           {"field": "PY Sales"}, {"field": "% Diff Sales"}]}
        mset = {"CY Sales", "PY Sales", "% Diff Sales", "Current Year"}
        vd = EP._kpi_tile_decision(ws, mset)
        self.assertIsNotNone(vd)
        self.assertTrue(vd["kpiStack"])
        self.assertEqual(vd["kpiMeasure"], "CY Sales")
        self.assertEqual(vd["secondaryValue"], "PY Sales")
        self.assertEqual(vd["kpiPctMeasure"], "% Diff Sales")

    def test_returns_none_without_date_grain(self):
        ws = {"name": "X", "measures": [{"field": "CY Sales"}, {"field": "PY Sales"}]}
        self.assertIsNone(EP._kpi_tile_decision(ws, {"CY Sales", "PY Sales"}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
