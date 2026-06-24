"""twb_fields.py — resolve Tableau internal field references to readable names.

Tableau shelves/encodings reference fields as obfuscated tokens such as
`[ds].[usr:Calculation_2234..:qk]` or `[ds].[mn:Census Date:ok]`. This module
maps those back to the human caption (via the calculated-field id map) and picks
the primary category (dimension) and value (measure) for a worksheet visual.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

# Tableau field token carries an aggregation/date prefix + 2-char suffix:
#   usr:Calculation_2234..:qk   |   mn:Census Date:ok   |   none:Hospital:nk
_TRIPLE = re.compile(r"[a-z0-9]+:([^:\[\]]+?):[a-z]{2}\b")

# Same token, but capturing BOTH the date/agg prefix and the field name, so we
# can detect at what date granularity a field is placed on a shelf.
_PART = re.compile(r"\[?([a-z0-9]+):([^:\[\]]+?):[a-z]{2}\b")
# Tableau date-truncation prefixes -> rank (finer grain = higher rank).
_DATE_RANK = {"yr": 1, "qr": 2, "mn": 3, "my": 3, "wk": 4,
              "dy": 5, "none": 5, "md": 5, "mdy": 5}
# Prefixes that PROVE the field is a truncated date (vs a plain dimension).
_DATE_PREFIXES = {"yr", "qr", "mn", "my", "wk"}
_RANK_LEVEL = {1: "year", 2: "quarter", 3: "month", 4: "week", 5: "day"}


def build_calc_map(calc_fields: List[Dict]) -> Dict[str, str]:
    """Map internal calc id (e.g. Calculation_2234..) -> caption."""
    out: Dict[str, str] = {}
    for c in calc_fields:
        fn = (c.get("fieldName") or "").strip("[]")
        if fn:
            out[fn] = c.get("caption") or fn
    return out


# Any bracketed field reference inside a formula, optionally datasource-qualified:
#   [Calculation_4609...]   |   [federated.<hash>].[Calculation_4609...]
#   [CY Sales (copy)_3221...]  (a duplicated calc referenced by internal name)
_CALC_REF = re.compile(r"\[(?:[^\[\]]+\]\.\[)?([^\[\]]+)\]")


def resolve_formula_refs(formula: Optional[str], calc_map: Dict[str, str]) -> Optional[str]:
    """Replace internal calc references with their friendly caption.

    Tableau stores inter-calc references by internal id (`Calculation_<id>`) or by
    a duplicated internal name (`CY Sales (copy)_<hash>`), sometimes datasource-
    qualified. Inlining the caption makes the formula readable for the LLM and
    lets the DAX translator resolve measure-to-measure references instead of
    routing the whole calc to the LLM. A reference whose token is NOT a known calc
    (e.g. a real source column like `[int_rate]`) is left untouched (never guess).
    """
    if not formula:
        return formula

    def _repl(m: "re.Match") -> str:
        caption = calc_map.get(m.group(1))
        return f"[{caption}]" if caption else m.group(0)

    return _CALC_REF.sub(_repl, formula)


def formula_dependencies(formula: Optional[str]) -> List[str]:
    """Return the distinct field/column names a (resolved) formula references.

    Metadata-only: extracts every ``[Field]`` token from the formula so we record
    WHICH columns/fields each calculation uses — without ever reading the data.
    Datasource-GUID leftovers are dropped; order of first appearance is kept.
    """
    if not formula:
        return []
    out: List[str] = []
    seen = set()
    for tok in _CALC_REF.findall(formula):
        name = tok.strip()
        if name and name not in seen and not name.startswith("federated") \
                and not name.startswith("Parameters"):
            seen.add(name)
            out.append(name)
    return out


def build_param_map(parameters: List[Dict]) -> Dict[str, str]:
    """Map a parameter internal name (Parameter 1) -> caption."""
    out: Dict[str, str] = {}
    for p in parameters:
        key = (p.get("internalName") or "").strip("[]")
        if key:
            out[key] = p.get("name") or key
    return out


def measure_captions(calc_fields: List[Dict], columns: List[Dict]) -> set:
    """Captions/names that should be treated as measures (values)."""
    measures = {c["caption"] for c in calc_fields
                if c.get("role") == "measure" and not _is_param_default(c)}
    measures |= {c["name"] for c in columns if c.get("role") == "measure"}
    return measures


def _is_param_default(calc: Dict) -> bool:
    """Parameter-default calc fields (literal formulas) are not real measures."""
    f = (calc.get("formula") or "").strip()
    return bool(re.fullmatch(r'("[^"]*"|#[^#]*#|\[Parameters\]\.\[[^\]]+\])', f))


def resolve_ref(ref: Optional[str], calc_map: Dict[str, str]) -> Optional[str]:
    """Resolve one Tableau field reference token to a display field name."""
    if not ref:
        return None
    m = _TRIPLE.search(ref)
    if m:
        name = m.group(1).strip()
    else:
        # Bare reference (no agg prefix): take the segment after the datasource.
        name = ref.split("].[")[-1].strip("[]() ")
    if not name or name.startswith("federated"):
        return None  # leftover datasource GUID, not a field
    return calc_map.get(name, name)


def resolve_param(param: Optional[str], param_map: Dict[str, str]) -> Optional[str]:
    """Resolve a paramctrl param ([Parameters].[Parameter 1]) to its caption."""
    if not param:
        return None
    m = re.search(r"\.\[([^\]]+)\]\s*$", param)
    key = m.group(1) if m else param
    return param_map.get(key, key)


def _dedupe(items: List[Optional[str]]) -> List[str]:
    seen: List[str] = []
    for it in items:
        if it and it not in seen:
            seen.append(it)
    return seen


def date_level(rows: List[str], cols: List[str], category: Optional[str]) -> Optional[str]:
    """Detect the date-truncation level a category field is shown at.

    Tableau encodes the part in the shelf token prefix (yr/qr/mn/wk/dy/none).
    Returns 'year'|'quarter'|'month'|'week'|'day' for a truncated date, or None
    when the category is not a date placed at a coarser-than-exact grain.
    """
    if not category:
        return None
    ranks: List[int] = []
    has_date_prefix = False
    for ref in list(rows) + list(cols):
        for pfx, nm in _PART.findall(ref or ""):
            if nm.strip() != category:
                continue
            if pfx in _DATE_PREFIXES:
                has_date_prefix = True
            if pfx in _DATE_RANK:
                ranks.append(_DATE_RANK[pfx])
    if not has_date_prefix or not ranks:
        return None
    return _RANK_LEVEL[max(ranks)]


def summarize(rows: List[str], cols: List[str], enc: Dict[str, Optional[str]],
              calc_map: Dict[str, str], measures: set) -> Dict:
    """Pick the primary category (dimension) and value (measure) for a visual."""
    rrefs = _dedupe(resolve_ref(r, calc_map) for r in rows)
    crefs = _dedupe(resolve_ref(c, calc_map) for c in cols)
    erefs = _dedupe(resolve_ref(v, calc_map) for v in enc.values())
    pool = rrefs + crefs + erefs
    value = next((f for f in pool if f in measures), None)
    category = next((f for f in (crefs + rrefs) if f not in measures), None)
    dims = [f for f in (crefs + rrefs) if f not in measures]
    vals = [f for f in pool if f in measures]
    return {"category": category, "value": value, "dimensions": dims, "values": vals,
            "categoryDateLevel": date_level(rows, cols, category)}
