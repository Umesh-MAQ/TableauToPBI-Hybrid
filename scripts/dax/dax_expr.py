"""dax_expr.py — conservative recursive Tableau-expression -> DAX translator.

Fail-closed by design: this parses the *safe* subset of Tableau calc syntax
(IF / IIF / CASE, logical + comparison + arithmetic operators, and a whitelist of
scalar / date / string / aggregation functions) and raises ``Untranslatable`` for
ANYTHING it does not explicitly understand. map_dax then routes that calc field to
the agent instead of emitting risky DAX.

Two hard safety gates make it impossible to emit DAX that references something that
does not exist in the model at this (pre-decisions) stage:

  * every ``[token]`` must resolve to either a real base column (``columns``) or a
    sibling calc field that itself becomes a measure (``measures``); a parameter ref,
    a column-kind calc-field token, or a datasource-qualified ``[a].[b]`` ref aborts
    the parse;
  * at least one column or measure reference must appear, so pure-constant /
    parameter-only formulas (string literals, ``TODAY()-1``, ``CASE [Parameters]...``)
    are never translated here.

A ``[token]`` that resolves to a sibling measure is emitted as a bare DAX measure
reference ``[Measure Name]`` (already scalar/aggregated), so it is rejected inside an
aggregation (``SUM([measure])`` is invalid DAX) and accepted everywhere a scalar is
valid. This unlocks the common KPI pattern of arithmetic over other measures, e.g.
``([CY Sales] - [PY Sales]) / [PY Sales]``.

Only well-known 1:1 mappings are included; arg-order / semantic-risk functions
(SPLIT, DATEADD, DATETRUNC, FLOAT, 2-arg MIN/MAX, ...) are deliberately omitted so
they fall through to the agent.
"""
from __future__ import annotations

import re
from typing import List, Optional, Tuple


class Untranslatable(Exception):
    """Raised when a formula falls outside the safe deterministic subset."""


# Tableau aggregation keyword -> DAX function (mirrors map_dax.AGG_MAP).
_AGG = {
    "SUM": "SUM", "AVG": "AVERAGE", "MIN": "MIN", "MAX": "MAX",
    "MEDIAN": "MEDIAN", "COUNT": "COUNT", "COUNTD": "DISTINCTCOUNT",
    "STDEV": "STDEV.S", "STDEVP": "STDEV.P", "VAR": "VAR.S", "VARP": "VAR.P",
}
_KEYWORDS = {"IF", "THEN", "ELSEIF", "ELSE", "END", "CASE", "WHEN", "AND", "OR", "NOT"}
_DATEDIFF_PART = {
    "year": "YEAR", "quarter": "QUARTER", "month": "MONTH", "week": "WEEK",
    "day": "DAY", "hour": "HOUR", "minute": "MINUTE", "second": "SECOND",
}

_TOKEN_SPEC = [
    ("WS", r"[\s\r\n]+"),
    ("FIELD", r"\[[^\]]*\]"),
    ("DATELIT", r"#[^#]*#"),
    ("STRING", r"\"[^\"]*\"|'[^']*'"),
    ("NUMBER", r"\d+\.\d+|\d+"),
    ("OP", r"==|!=|<>|<=|>=|<|>|=|\+|\-|\*|/|%"),
    ("LPAREN", r"\("),
    ("RPAREN", r"\)"),
    ("COMMA", r","),
    ("DOT", r"\."),
    ("NAME", r"[A-Za-z_][A-Za-z0-9_]*"),
]
_TOKEN_RE = re.compile("|".join(f"(?P<{n}>{p})" for n, p in _TOKEN_SPEC))


class _Tok:
    __slots__ = ("kind", "value")

    def __init__(self, kind: str, value: str):
        self.kind = kind
        self.value = value


def _tokenize(text: str) -> List[_Tok]:
    toks: List[_Tok] = []
    pos = 0
    while pos < len(text):
        m = _TOKEN_RE.match(text, pos)
        if not m:
            raise Untranslatable(f"unexpected char at {pos!r}")
        pos = m.end()
        kind = m.lastgroup
        if kind == "WS":
            continue
        toks.append(_Tok(kind, m.group()))
    return toks


# A parsed node is (dax_text, is_text) where is_text is True when the value is
# known to be a string (so arithmetic '+' on it is rejected as ambiguous concat).
Node = Tuple[str, bool]


class _Parser:
    def __init__(self, toks: List[_Tok], table: str, columns: set, measures: dict):
        self.toks = toks
        self.i = 0
        self.table = table
        self.columns = columns
        self.measures = measures  # internal calc-field name -> DAX measure name
        self.col_refs = 0
        self.in_agg = 0  # >0 while parsing an aggregation's argument

    # -- token helpers --------------------------------------------------------
    def _peek(self) -> Optional[_Tok]:
        return self.toks[self.i] if self.i < len(self.toks) else None

    def _next(self) -> _Tok:
        if self.i >= len(self.toks):
            raise Untranslatable("unexpected end of formula")
        t = self.toks[self.i]
        self.i += 1
        return t

    def _is_kw(self, word: str) -> bool:
        t = self._peek()
        return bool(t and t.kind == "NAME" and t.value.upper() == word)

    def _eat_kw(self, word: str) -> None:
        if not self._is_kw(word):
            raise Untranslatable(f"expected {word}")
        self.i += 1

    # -- grammar --------------------------------------------------------------
    def parse(self) -> str:
        node = self._expr()
        if self.i != len(self.toks):
            raise Untranslatable("trailing tokens")
        if self.col_refs == 0:
            raise Untranslatable("no column or measure reference")
        return node[0]

    def _expr(self) -> Node:
        if self._is_kw("IF"):
            return self._if()
        if self._is_kw("CASE"):
            return self._case()
        return self._or()

    def _if(self) -> Node:
        self._eat_kw("IF")
        clauses: List[Tuple[str, str]] = []
        cond = self._expr()[0]
        self._eat_kw("THEN")
        then = self._expr()
        clauses.append((cond, then[0]))
        texts = [then[1]]
        while self._is_kw("ELSEIF"):
            self._eat_kw("ELSEIF")
            c = self._expr()[0]
            self._eat_kw("THEN")
            t = self._expr()
            clauses.append((c, t[0]))
            texts.append(t[1])
        else_dax = None
        if self._is_kw("ELSE"):
            self._eat_kw("ELSE")
            e = self._expr()
            else_dax = e[0]
            texts.append(e[1])
        self._eat_kw("END")
        if else_dax is None and len(clauses) == 1:
            dax = f"IF ( {clauses[0][0]}, {clauses[0][1]} )"
        else:
            dax = else_dax if else_dax is not None else "BLANK ( )"
            for c, t in reversed(clauses):
                dax = f"IF ( {c}, {t}, {dax} )"
        return dax, all(texts)

    def _case(self) -> Node:
        self._eat_kw("CASE")
        subject = self._expr()[0]
        parts = [subject]
        texts: List[bool] = []
        if not self._is_kw("WHEN"):
            raise Untranslatable("CASE without WHEN")
        while self._is_kw("WHEN"):
            self._eat_kw("WHEN")
            v = self._expr()[0]
            self._eat_kw("THEN")
            r = self._expr()
            parts.extend([v, r[0]])
            texts.append(r[1])
        if self._is_kw("ELSE"):
            self._eat_kw("ELSE")
            e = self._expr()
            parts.append(e[0])
            texts.append(e[1])
        self._eat_kw("END")
        return f"SWITCH ( {', '.join(parts)} )", all(texts) if texts else False

    def _or(self) -> Node:
        left = self._and()
        while self._is_kw("OR"):
            self._eat_kw("OR")
            right = self._and()
            left = (f"( {left[0]} || {right[0]} )", False)
        return left

    def _and(self) -> Node:
        left = self._not()
        while self._is_kw("AND"):
            self._eat_kw("AND")
            right = self._not()
            left = (f"( {left[0]} && {right[0]} )", False)
        return left

    def _not(self) -> Node:
        if self._is_kw("NOT"):
            self._eat_kw("NOT")
            inner = self._not()
            return f"NOT ( {inner[0]} )", False
        return self._comparison()

    _CMP = {"=": "=", "==": "=", "!=": "<>", "<>": "<>", "<": "<", "<=": "<=", ">": ">", ">=": ">="}

    def _comparison(self) -> Node:
        left = self._additive()
        t = self._peek()
        if t and t.kind == "OP" and t.value in self._CMP:
            op = self._next().value
            right = self._additive()
            return f"( {left[0]} {self._CMP[op]} {right[0]} )", False
        return left

    def _additive(self) -> Node:
        left = self._multiplicative()
        while True:
            t = self._peek()
            if t and t.kind == "OP" and t.value in ("+", "-"):
                op = self._next().value
                right = self._multiplicative()
                if left[1] or right[1]:
                    raise Untranslatable("string operand in arithmetic")
                left = (f"( {left[0]} {op} {right[0]} )", False)
            else:
                return left

    def _multiplicative(self) -> Node:
        left = self._unary()
        while True:
            t = self._peek()
            if t and t.kind == "OP" and t.value in ("*", "/", "%"):
                op = self._next().value
                right = self._unary()
                if left[1] or right[1]:
                    raise Untranslatable("string operand in arithmetic")
                if op == "%":
                    left = (f"MOD ( {left[0]}, {right[0]} )", False)
                else:
                    left = (f"( {left[0]} {op} {right[0]} )", False)
            else:
                return left

    def _unary(self) -> Node:
        t = self._peek()
        if t and t.kind == "OP" and t.value in ("+", "-"):
            op = self._next().value
            inner = self._unary()
            if inner[1]:
                raise Untranslatable("string operand in arithmetic")
            return f"( {op}{inner[0]} )", False
        return self._primary()

    def _primary(self) -> Node:
        t = self._peek()
        if t is None:
            raise Untranslatable("unexpected end")
        if t.kind == "LPAREN":
            self._next()
            inner = self._expr()
            if not (self._peek() and self._peek().kind == "RPAREN"):
                raise Untranslatable("missing )")
            self._next()
            return inner
        if t.kind == "NUMBER":
            self._next()
            return t.value, False
        if t.kind == "STRING":
            self._next()
            val = t.value[1:-1].replace('"', '""')
            return f'"{val}"', True
        if t.kind == "DATELIT":
            raise Untranslatable("date literal")
        if t.kind == "FIELD":
            return self._field()
        if t.kind == "NAME":
            return self._name()
        raise Untranslatable(f"unexpected token {t.value!r}")

    def _field(self) -> Node:
        tok = self._next()
        # datasource/parameter-qualified ref like [a].[b] -> not a base column
        if self._peek() and self._peek().kind == "DOT":
            raise Untranslatable("qualified reference")
        name = tok.value[1:-1]
        if name in self.columns:
            # A measure expression must aggregate every column; a column referenced
            # outside an aggregation would be invalid DAX in a measure.
            if self.in_agg == 0:
                raise Untranslatable(f"bare column reference [{name}] (needs aggregation)")
            self.col_refs += 1
            return f"{self.table}[{name}]", None  # column type unknown
        # reference to a sibling calc field that itself becomes a measure: emit a
        # bare DAX measure reference (already scalar/aggregated).
        measure = self.measures.get(name)
        if measure is not None:
            # aggregating a measure (e.g. SUM([measure])) is invalid DAX -> defer.
            if self.in_agg > 0:
                raise Untranslatable(f"aggregation over measure reference [{name}]")
            self.col_refs += 1
            return f"[{measure}]", False  # numeric scalar
        raise Untranslatable(f"non-base-column [{name}]")

    def _name(self) -> Node:
        tok = self._next()
        word = tok.value.upper()
        # bareword constants
        if not (self._peek() and self._peek().kind == "LPAREN"):
            if word == "TRUE":
                return "TRUE ( )", False
            if word == "FALSE":
                return "FALSE ( )", False
            if word == "NULL":
                return "BLANK ( )", False
            raise Untranslatable(f"bare identifier {tok.value!r}")
        self._next()  # consume (
        # aggregations + ATTR take exactly one plain base-column argument
        if word in _AGG or word == "ATTR":
            return self._agg_call(word)
        # generic function call: parse comma-separated argument expressions
        args: List[Node] = []
        if not (self._peek() and self._peek().kind == "RPAREN"):
            args.append(self._expr())
            while self._peek() and self._peek().kind == "COMMA":
                self._next()
                args.append(self._expr())
        if not (self._peek() and self._peek().kind == "RPAREN"):
            raise Untranslatable("missing ) in call")
        self._next()
        return self._call(word, args)

    def _agg_call(self, word: str) -> Node:
        """Parse AGG( [column] ) / ATTR( [column] ) -- arg must be a plain column."""
        if not (self._peek() and self._peek().kind == "FIELD"):
            raise Untranslatable(f"{word} expects a single column")
        self.in_agg += 1
        node = self._field()
        self.in_agg -= 1
        if not (self._peek() and self._peek().kind == "RPAREN"):
            raise Untranslatable(f"{word} expects a single column")
        self._next()  # consume )
        if word == "ATTR":
            return f"SELECTEDVALUE ( {node[0]} )", None
        return f"{_AGG[word]} ( {node[0]} )", False

    def _call(self, name: str, args: List[Node]) -> Node:
        a = [x[0] for x in args]
        n = len(args)
        # logical / null
        if name == "IIF" and n == 3:
            return f"IF ( {a[0]}, {a[1]}, {a[2]} )", args[1][1] and args[2][1]
        if name == "ISNULL" and n == 1:
            return f"ISBLANK ( {a[0]} )", False
        if name == "IFNULL" and n == 2:
            return f"COALESCE ( {a[0]}, {a[1]} )", args[0][1] or args[1][1]
        if name == "ZN" and n == 1:
            return f"COALESCE ( {a[0]}, 0 )", False
        # math
        if name in ("ABS", "SQRT", "SIGN", "EXP", "LN", "INT") and n == 1:
            return f"{name} ( {a[0]} )", False
        if name == "ROUND" and n in (1, 2):
            return f"ROUND ( {a[0]}, {a[1] if n == 2 else '0'} )", False
        if name == "POWER" and n == 2:
            return f"POWER ( {a[0]}, {a[1]} )", False
        # Trigonometry: Tableau and DAX share identical function names and
        # single-argument semantics, so each maps 1:1. The False flag marks a
        # numeric (non-text) result for the surrounding concatenation logic.
        if name in ("SIN", "COS", "TAN", "ASIN", "ACOS", "ATAN",
                    "COT", "DEGREES", "RADIANS") and n == 1:
            return f"{name} ( {a[0]} )", False
        # PI() is a zero-argument numeric constant, identical in both dialects.
        if name == "PI" and n == 0:
            return "PI ( )", False
        # string
        if name in ("UPPER", "LOWER", "TRIM") and n == 1:
            return f"{name} ( {a[0]} )", True
        if name == "LEN" and n == 1:
            return f"LEN ( {a[0]} )", False
        if name in ("LEFT", "RIGHT") and n == 2:
            return f"{name} ( {a[0]}, {a[1]} )", True
        if name == "MID" and n == 3:
            return f"MID ( {a[0]}, {a[1]}, {a[2]} )", True
        if name == "REPLACE" and n == 3:
            return f"SUBSTITUTE ( {a[0]}, {a[1]}, {a[2]} )", True
        if name == "CONTAINS" and n == 2:
            return f"( SEARCH ( {a[1]}, {a[0]}, 1, 0 ) > 0 )", False
        if name == "STR" and n == 1:
            return f'( {a[0]} & "" )', True
        # date
        if name in ("YEAR", "MONTH", "DAY", "HOUR", "MINUTE", "SECOND", "QUARTER") and n == 1:
            return f"{name} ( {a[0]} )", False
        if name == "WEEK" and n == 1:
            return f"WEEKNUM ( {a[0]} )", False
        if name in ("TODAY", "NOW") and n == 0:
            return f"{name} ( )", False
        if name == "DATEDIFF" and n == 3:
            lit = args[0][0]
            part = lit[1:-1].lower() if lit.startswith('"') else None
            if part not in _DATEDIFF_PART:
                raise Untranslatable("DATEDIFF part")
            return f"DATEDIFF ( {a[1]}, {a[2]}, {_DATEDIFF_PART[part]} )", False
        # MAKEDATE(year, month, day) -> DATE(year, month, day): the argument
        # order is identical, so the rename is a safe 1:1 date construction.
        if name == "MAKEDATE" and n == 3:
            return f"DATE ( {a[0]}, {a[1]}, {a[2]} )", False
        raise Untranslatable(f"unsupported function {name}/{n}")


def translate_expression(
    formula: str,
    table: str,
    columns: set,
    measures: Optional[dict] = None,
) -> str:
    """Return DAX for a safe Tableau expression, else raise ``Untranslatable``.

    ``measures`` maps a sibling calc field's internal Tableau name (without the
    surrounding brackets) to the DAX measure name it becomes, enabling references
    to other measures (e.g. ``[CY Sales]``) inside a translated expression.
    """
    if not columns and not measures:
        raise Untranslatable("no column context")
    toks = _tokenize(formula)
    if not toks:
        raise Untranslatable("empty formula")
    return _Parser(toks, table, columns or set(), measures or {}).parse()
