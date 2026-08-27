#!/usr/bin/env python3
"""
verify_report.py - deterministic pre-render gate for a measurement report.

Reads a claims manifest emitted by the report generator, and where one is supplied the
RENDERED DOCUMENT as well. Every check here fires on a defect that once got past a
careful author, which is the only reason it is in the list.

The document is not optional in spirit. A manifest is written by the same generator that
writes the prose, so a check that only reads the manifest cannot see a number the
generator never declared, and an audit proved that exactly: five fabricated headline
figures went into a report's abstract and the manifest did not change by one byte. A5
and A6 hold jurisdiction over what shipped, F3 and F4 over whether a figure's values
reached the page, and G3 over whether the quality gate's result can be read back out of
the artefact it names. The rest read the manifest, and say so.

    python verify_report.py claims.json
    python verify_report.py claims.json --previous claims-prev.json
    python verify_report.py claims.json --rendered report.html --findings findings.json
    python verify_report.py --demo          # run against a fixture full of real defects

Exit codes: 0 clean or warnings only, 1 one or more errors, 2 the manifest itself is broken.

Standard library only, so it can run inside any build without adding a dependency.
"""

from __future__ import annotations

import argparse
import ast
import json
import math
import re
import sys
from datetime import datetime
from html import unescape as html_unescape
from pathlib import Path

SCHEMA = "claims/1"

# Basis values a quantity can carry. Mixing them in one expression without an explicit
# conversion is the defect that keeps capacity arithmetic from closing.
BASES = {"per_device", "per_shard", "total", "per_sequence", "per_token", "per_request", "ratio", "scalar"}

UNIT_FAMILIES = {
    "bytes": {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "TiB": 1024**4, "PiB": 1024**5,
              "KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12},
    "time": {"ns": 1e-9, "us": 1e-6, "ms": 1e-3, "s": 1.0, "min": 60.0, "h": 3600.0},
    "rate": {"tok/s": 1.0, "req/s": 1.0, "emb/s": 1.0, "fps": 1.0, "img/s": 1.0},
    "bandwidth": {"GB/s": 1e9, "MB/s": 1e6, "KB/s": 1e3, "TB/s": 1e12, "GT/s": 1e9,
                  "GiB/s": float(1024**3), "MiB/s": float(1024**2)},
    "compute": {"TFLOPS": 1e12, "TOPS": 1e12, "GFLOPS": 1e9, "PFLOPS": 1e15},
    "power": {"W": 1.0, "kW": 1e3, "MW": 1e6},
    "energy": {"Wh": 1.0, "kWh": 1e3, "J": 1.0 / 3600.0, "kJ": 1e3 / 3600.0},
    "frequency": {"Hz": 1.0, "kHz": 1e3, "MHz": 1e6, "GHz": 1e9},
    "percent": {"%": 1.0},
    "count": {"": 1.0, "count": 1.0},
    # The printed marks are added from CURRENCY_MARKS below, so the two lists cannot drift.
    "currency": {"USD": 1.0},
}

# Numerals in prose that are not measurements. Everything else must be a citation.
# Deliberately narrow: a decimal that looks like a version number is also what a stale
# latency figure looks like, so versions are allowed only where the word says so.
YEAR = re.compile(r"^(?:19|20)\d{2}$")
STRUCTURAL_CONTEXT = re.compile(
    r"(?i)\b(?:section|sections|figure|fig\.?|table|appendix|chapter|step|version|v|item|note)\s*$"
)

ENTITY_LEAK = re.compile(r"&(?:amp|mdash|ndash|lt|gt|quot|#\d+);")
PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z0-9_.\-]+)\s*\}\}")
BARE_NUMERAL = re.compile(r"(?<![\w.]) -? \d[\d,]* (?: \.\d+ )? (?![\w])", re.X)


# --------------------------------------------------------------------------------------
# numerals in the RENDERED DOCUMENT (A5, A6)
#
# WHY THIS EXISTS, and why it is the most important thing in the file. A2 above polices the
# manifest's `prose` list, which is a list the generator writes. An omission attack therefore
# beats it completely, and did: five fabricated headline figures were injected into a report's
# abstract, not one of them appeared in any prose block, and the build shipped with exit 0 and a
# byte-identical manifest. A stale figure in a section heading, the contents, the body and a
# title= tooltip shipped the same way, beside a table that disagreed with it. The gate held
# jurisdiction over 974 characters of shadow text while the document shipped 104,549. So these
# checks read the RENDERED DOCUMENT, which is where the reader meets the number.

# Attribute values a reader, or a screen reader, actually sees. The tag-stripper that F1 uses
# throws these away, and the attack hid a stale figure in a title= tooltip for exactly that
# reason, so attributes are in scope. SVG <title> tooltips need no special handling: they are
# text between tags and survive stripping.
VISIBLE_ATTR_NAMES = frozenset(("title", "alt", "aria-label"))

# A START TAG, with its attribute region captured, and QUOTED VALUES CONSUMED AS UNITS so a ">"
# inside one does not end the tag early. Scanning tag by tag is what makes an UNQUOTED attribute
# value safe to read: `alt=1911` written in a sentence is prose a reader already sees through the
# stripper, and counting it again as an attribute would inflate the denominator with a number that
# was never in a tag at all.
HTML_TAG = re.compile(r"""(?s)<[A-Za-z][^\s/>]*((?:"[^"]*"|'[^']*'|[^>"'])*)>""")

# One name=value pair inside a tag, in each of HTML'S THREE QUOTING STYLES.
#
# THIS USED TO MATCH DOUBLE QUOTES AND NOTHING ELSE, and the distinction is one no author intends:
# title="peak draw 1911 W" was scanned and title='peak draw 1911 W' was invisible, though both are
# ordinary HTML and a browser renders them identically. A single quote around a tooltip is what a
# generator emits the moment its own string is double-quoted, so the spelling that escaped the
# check is the one an author reaches for by accident.
#
# The pairs are walked in order rather than searched for by name, so an attribute name appearing
# INSIDE another attribute's value (data-note="alt=7") is consumed with that value and cannot be
# read as an attribute of its own.
VISIBLE_ATTRS = re.compile(
    r"""(?is)([A-Za-z_:][-\w:.]*)\s*(?:=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+)))?""")

# A printed numeral. The currency symbol, the thousands separators and a leading sign belong to
# the NUMERAL and not to the unit, so "$0.11" and "+0.78%" are each one number carrying one unit.
CURRENCY_MARKS = "$\u00a3\u20ac"
_NUMERAL = "[" + re.escape(CURRENCY_MARKS) + "]?[+-]?\\d[\\d,]*(?:\\.\\d+)?"
# A currency figure carries its unit on its LEFT, which is why the old scanner, which only ever
# looked to the right of a numeral, treated every price in the document as a bare numeral held to
# no floor at all. The mark is the unit.
for _mark in CURRENCY_MARKS:
    UNIT_FAMILIES["currency"][_mark] = 1.0

# Printed unit -> (family, factor to the family's base unit). The factor is what lets a claim
# recorded in ms cover a figure printed in us: the comparison happens in base units, so a unit
# change in the prose does not silently become an uncovered numeral. A family of None means
# "dimensionless", and those fall back to comparing the raw values.
#
# THIS USED TO BE A 22-STRING ALLOWLIST AND THE DOCUMENT'S VOCABULARY WAS WIDER. TB/s, GT/s, GHz,
# MHz, KiB, TiB, KB, Wh, kW, J, ns, emb/s, "minutes" and every currency figure carried no unit as
# far as A5 was concerned, and seven fabricated figures shipped through that gap. A list is the
# wrong shape for this: it fails silently and it fails in the direction of passing. So the map
# below is DERIVED from UNIT_FAMILIES (one place to add a unit, and claims and prose then read the
# same table), the aliases record the spellings a document uses for the same unit, and anything
# still unlisted is caught structurally by unit_of() rather than dropped.
PRINTED_ALIASES = {
    "tokens/s": "tok/s", "tokens/sec": "tok/s", "requests/s": "req/s", "requests/sec": "req/s",
    "embeddings/s": "emb/s", "images/s": "img/s",
    "GFLOP/s": "GFLOPS", "TFLOP/s": "TFLOPS", "PFLOP/s": "PFLOPS",
    "microseconds": "us", "microsecond": "us", "\u00b5s": "us", "\u03bcs": "us",
    "milliseconds": "ms", "millisecond": "ms", "nanoseconds": "ns", "nanosecond": "ns",
    "seconds": "s", "second": "s", "sec": "s", "secs": "s",
    "minutes": "min", "minute": "min", "mins": "min",
    "hours": "h", "hour": "h", "hr": "h", "hrs": "h",
    "watts": "W", "watt": "W", "joules": "J", "joule": "J",
}
PRINTED_UNIT: dict = {}
for _family, _members in UNIT_FAMILIES.items():
    for _unit, _factor in _members.items():
        if _unit:
            PRINTED_UNIT[_unit] = (_family, float(_factor))
for _alias, _canonical in PRINTED_ALIASES.items():
    if _canonical in PRINTED_UNIT:
        PRINTED_UNIT[_alias] = PRINTED_UNIT[_canonical]
PRINTED_UNIT["x"] = (None, 1.0)
# Printed on its own after a number these are magnitudes or nouns in this domain, never units:
# "27B parameters", "4K context", "1M tokens", "5 count". They stay valid units ON A CLAIM, where
# an author writes them deliberately; they are only removed from the PRINTED vocabulary.
for _ambiguous in ("B", "count", "USD"):
    PRINTED_UNIT.pop(_ambiguous, None)

# The structural rule's two disambiguation lists, and they are the only lists left. A single
# capital letter is a magnitude suffix more often than a unit in a hardware document, and a short
# capitalised token is sometimes an acronym, so those two cases are named. Everything else is
# decided by shape.
SINGLE_LETTER_UNITS = frozenset(("W", "J", "V", "A", "s", "x", "%"))
NON_UNIT_TOKENS = frozenset((
    "UTC", "GMT", "PCIe", "PCI", "KV", "GPU", "GPUs", "CPU", "CPUs", "RAM", "ROM", "DDR", "DDR5",
    "NVLink", "NVMe", "SSD", "HBM", "SM", "SMs", "MoE", "LLM", "API", "APIs", "HTTP", "JSON",
    "CUDA", "NCCL", "RTX", "GTX", "FP", "BF", "TF", "INT", "AI", "ML", "QA", "OS", "VM", "IO",
    "PC", "US", "UK", "EU", "AM", "PM", "ID", "IP", "TCP", "UDP", "NUMA", "BIOS", "OEM", "TDP",
    "MLPerf", "Gen", "GT", "MT", "Tbps", "Gbps"))


def unit_of(token: str):
    """(family, factor) if this printed token is a unit, else None.

    Structural, because the allowlist that used to do this job was an opt-out by omission: any
    unit an author had not thought to add made its numeral invisible to A5, and A5 is the only
    check with jurisdiction over what shipped. The shape rules, in order of confidence:

      * a token in the derived map above, which is every unit any claim can carry;
      * a rate written with a slash ("tok/s", "GT/s", "emb/s", "img/s"), which no English phrase
        after a numeral looks like;
      * a single letter from the short set that is genuinely a unit in this domain;
      * a short token carrying a capital ("MiB", "TFLOPS", "GHz"), because English words that
        follow a numeral are lowercase and the capitalised exceptions are acronyms, which are
        named in NON_UNIT_TOKENS.

    A token that is not in the map returns family None, which means the value is compared raw
    against every claim rather than inside a family. That is weaker than a known unit and much
    stronger than being dropped, which is what happened before.
    """
    if not token:
        return None
    known = PRINTED_UNIT.get(token)
    if known:
        return known
    if len(token) > 12 or token in NON_UNIT_TOKENS or any(ch.isdigit() for ch in token):
        return None
    if "/" in token:
        left, _, right = token.partition("/")
        if left.isalpha() and right.isalpha() and 1 <= len(right) <= 4:
            return (None, 1.0)
        return None
    if not token.isalpha():
        return None
    if len(token) == 1:
        return (None, 1.0) if token in SINGLE_LETTER_UNITS else None
    if len(token) <= 7 and any(ch.isupper() for ch in token):
        return (None, 1.0)
    return None


# A number attached to a unit letter with no separator. "5090s" is a plural and "1950s" is a
# decade, but "1240W", "17s" and "34x" are a power, a duration and a multiplier, and the old rule
# (a decimal point or nothing) erased all three: they fell out of A5's jurisdiction entirely and
# shipped with exit 0 and zero warnings. So the rule now keeps the protection and narrows it to
# the two things it was protecting: a four-digit year, and a model number, which a document names
# right before it says. A manifest can declare further exceptions in coverage.attached_exceptions,
# where a reader can see them and the gate counts how many numerals each one removed.
MODEL_CONTEXT = re.compile(
    r"(?i)\b(?:rtx|gtx|geforce|radeon|quadro|tesla|titan|xeon|ryzen|epyc|threadripper|core|"
    r"nvidia|amd|intel|arc|instinct|blackwell|ada|hopper|ampere|model|series|sm|gen)[\s-]*$")

# Where one line of reading stops. Between two block elements there is no adjacency however little
# markup separates them, so the stripper puts this character there and the numeral-unit gap, which
# is whitespace only, cannot cross it. It is NUL because NUL is not whitespace to `\s`: every other
# separator character in the C0 range is, and a "boundary" the gap can step over is not a boundary.
BLOCK_BOUNDARY = "\x00"
BLOCK_TAG = re.compile(
    r"(?is)</?(?:p|div|section|article|header|footer|main|aside|nav|figure|figcaption|h[1-6]|"
    r"ul|ol|li|dl|dt|dd|table|thead|tbody|tfoot|tr|td|th|caption|colgroup|col|blockquote|pre|"
    r"hr|br|details|summary|dialog|form|fieldset|legend|address|body|html|head|option|optgroup|"
    # SVG text carries its own boundaries. Every figure in a report of this kind draws its axis
    # with one <text> per tick, and a reader sees five separate labels. Without these the removal
    # of inline markup glues them into one string: "0%25%50%75%100%" was reported as a numeral of
    # 0 followed by the unit "%25%50%75%100%", and "012344 KiB10 KiB20 KiB" as one figure. Both
    # were errors on the real document, and both are the same mistake as the paragraph boundary,
    # made inside a chart instead of a page.
    r"svg|g|text|tspan|textPath|foreignObject|desc|"
    r"select|textarea|video|audio|iframe|canvas)\b[^>]*>")

# The gap between a numeral and its unit. A RUN of whitespace, not one character, because the
# markup between them has already been removed and what a reader sees as "4414 W" can arrive here
# as "4414" plus a newline plus the indentation of the next inline element. It stops at
# BLOCK_BOUNDARY, which is not whitespace, so nothing joins across a paragraph or a table cell.
UNIT_CANDIDATE = re.compile(
    "(?<![\\w.])(" + _NUMERAL + ")(\\s*)"
    r"([A-Za-z\u00b5\u03bc]{1,12}(?:/[A-Za-z]{1,4})?|%)(?![A-Za-z/])")
ANY_NUMERAL = re.compile(r"(?<![\w.])(" + _NUMERAL + r")")

# Units an English sentence spells out. "47314 tokens per second" and "96 concurrent users" read as
# measurements to every human being, and neither was unit-bearing: "tokens" and "concurrent" are
# not unit-shaped, so both numerals fell into the BARE pool, where the floor carries slack and no
# individual miss was ever named. A fabricated figure spelled this way therefore shipped with no
# finding at all. Each phrase maps to the printed unit it means, so a claim recorded in tok/s
# covers a figure printed as "tokens per second"; a phrase with no unit of its own keeps its own
# words as the unit, which is dimensionless and reads correctly in a finding.
SPELLED_UNIT_PHRASES = (
    (r"tokens?\s+per\s+second", "tok/s"),
    (r"requests?\s+per\s+second", "req/s"),
    (r"embeddings?\s+per\s+second", "emb/s"),
    (r"images?\s+per\s+second", "img/s"),
    (r"frames?\s+per\s+second", "fps"),
    (r"(?:queries|samples|operations|jobs|documents|files|steps)\s+per\s+"
     r"(?:second|minute|hour)", ""),
    (r"(?:tokens?|requests?|users?|images?)\s+per\s+(?:minute|hour|day)", ""),
    (r"concurrent\s+(?:users?|requests?|sessions?|streams?|clients?)", ""),
)
SPELLED_UNIT_SCANNERS = tuple(
    (re.compile("(?i)(?<![\\w.])(" + _NUMERAL + r")(\s*)(" + phrase + r")\b"), canonical)
    for phrase, canonical in SPELLED_UNIT_PHRASES)

# A numeral a SPACE SEPARATOR splits in two. "9 526.6 tok/s" reads as 9,526.6 to a human and used
# to reach the gate as the two numerals 9 and 526.6, so the gate validated 526.6 against a real
# claim while the reader saw a number seven thousand larger. The leading (?<![A-Za-z\d.,]) is what
# stops "GPU1 615.2 TFLOPS" being read as 1,615.2: a digit group glued to a word is part of that
# word.
#
# THE FIRST VERSION OF THIS KNEW TWO CHARACTERS, the ordinary space and U+00A0, and Unicode has a
# whole column of the things. A thin space, a narrow no-break space and a figure space are what
# typesetting software actually emits for a thousands separator, and each of them split a numeral
# so that the gate read a different number than the reader did. A zero-width space is worse, since
# it splits the numeral while showing the reader no gap at all.
#
# THE GROUPING IS ALSO PART OF THE READING. Three-digit groups after the first are the thousands
# convention and there is only one way to read them. "9 25.2 GB/s" is not that: a merger reads
# 925.2 and a reader reads two numbers, and NEITHER READING IS THE RIGHT ONE TO GUESS. So the
# grouping decides which of three things happens, and one of them is a finding rather than an
# answer. That is why the first group is \d+ here rather than \d{1,3}: a non-conventional split has
# to be SEEN before it can be reported, and the old pattern could not see one.
VISIBLE_GROUP_SEPARATORS = " \u00a0\u2009\u202f\u2007"
INVISIBLE_GROUP_SEPARATORS = "\u200b"
GROUP_SEPARATORS = VISIBLE_GROUP_SEPARATORS + INVISIBLE_GROUP_SEPARATORS
SPACE_GROUPED = re.compile(
    r"(?<![A-Za-z\d.,])(\d+(?:[" + GROUP_SEPARATORS + r"]\d+)+(?:\.\d+)?)(?![\d])")

# What follows an ambiguous split, when the split is a measurement rather than a list of integers.
# "levels 1 2 4 8 16" is five numbers in a sentence and reading it as 12,481.6 would be the gate
# inventing a figure; "9 25.2 GB/s" carries a unit and a decimal, which is what one quantity looks
# like. Both marks are structural, so neither depends on a word list.
AMBIGUOUS_TRAILING_UNIT = re.compile(
    r"\s*([A-Za-z\u00b5\u03bc]{1,12}(?:/[A-Za-z]{1,4})?|%)(?![A-Za-z/])")

# Words that carry no identity, dropped before comparing two labels for near-duplication.
LABEL_STOPWORDS = frozenset(("the", "a", "an", "of", "at", "in", "on", "per", "for", "and",
                             "to", "from", "by", "with", "over", "under", "its"))

# D7. Phrases that describe a fixed in-flight population. Beside arrival_model open_loop_* one of
# these is a contradiction, and the contradiction is the tell: the attack that shipped flipped the
# declared model on a closed-loop harness and left the honest note sitting next to it.
CLOSED_LOOP_PHRASES = ("fixed in-flight", "fixed in flight", "no independent arrival process",
                       "issued when a previous one completes",
                       "issued when the previous one completes")
# The reverse direction. Deliberately narrow: a closed-loop note may legitimately DISCUSS Poisson
# arrivals to explain what it cannot do, and the real report's note does exactly that, so the word
# "poisson" is not evidence of anything. Only a positive statement about this harness counts.
OPEN_LOOP_PHRASES = ("requests arrive independently", "arrivals are independent of completions",
                     "open-loop arrivals", "open loop arrivals", "independent arrival process")
NEGATORS = ("no ", "not ", "never ", "without ", "cannot ", "rather than ")


# --------------------------------------------------------------------------------------
# findings


class Findings:
    """Findings, plus the two things a reader needs beside them: what was in scope, and what was
    knowingly waived.

    `coverage` records how much of the rendered document the numeral checks actually held
    jurisdiction over. It is reported whether or not anything fired, because "0 errors" over a
    manifest that asserts almost nothing reads exactly like "0 errors" over one that asserts
    everything, and the difference is the whole value of the gate.
    """

    def __init__(self) -> None:
        self.items: list[dict] = []
        self.coverage: dict = {"scope": "the numeral checks did not run"}
        self.acceptances: list[dict] = []
        # One entry per claim whose kind exempts it from recomputation, saying what its source
        # turned out to name and WHETHER ANYTHING WAS OPENED. A URL cannot be fetched by a gate
        # that runs offline, so it is accepted unresolved, and a reader who cannot tell that apart
        # from a file that was read is being shown a check that did not happen.
        self.sources: list[dict] = []
        # Set when the manifest itself cannot be read as a manifest, which is exit 2 rather than
        # exit 1. It used to be reachable only for unparseable JSON, because every other broken
        # shape raised a traceback out of a check and killed the process before it could say so.
        self.fatal = False

    def add(self, severity: str, check: str, message: str, **extra) -> None:
        self.items.append({"severity": severity, "check": check, "message": message, **extra})

    def error(self, check: str, message: str, **extra) -> None:
        self.add("error", check, message, **extra)

    def warn(self, check: str, message: str, **extra) -> None:
        self.add("warn", check, message, **extra)

    @property
    def errors(self) -> list[dict]:
        return [f for f in self.items if f["severity"] == "error"]

    @property
    def warnings(self) -> list[dict]:
        return [f for f in self.items if f["severity"] == "warn"]

    @property
    def accepted(self) -> list[dict]:
        """Warnings the manifest waived with a recorded reason. Not live, still on the record."""
        return [f for f in self.items if f["severity"] == "accepted"]


# --------------------------------------------------------------------------------------
# formula evaluation


_ALLOWED_NODES = (
    ast.Expression, ast.BinOp, ast.UnaryOp, ast.Name, ast.Load,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow, ast.USub, ast.UAdd,
    ast.Constant, ast.Tuple,
)


def safe_eval(expr: str, env: dict[str, float]) -> float:
    """Arithmetic over claim keys. No calls, no attributes, no subscripts, no comprehensions."""
    tree = ast.parse(expr, mode="eval")
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise ValueError(f"disallowed syntax {type(node).__name__} in formula: {expr}")
        if isinstance(node, ast.Name) and node.id not in env:
            raise KeyError(node.id)
    return float(eval(compile(tree, "<formula>", "eval"), {"__builtins__": {}}, dict(env)))


def formula_names(expr: str) -> list[str]:
    return sorted({n.id for n in ast.walk(ast.parse(expr, mode="eval")) if isinstance(n, ast.Name)})


def additive_operands(expr: str) -> list[tuple[list[str], list[str]]]:
    """Name leaves on each side of every + and - in the expression.

    Multiplying a per-request count by a total-basis rate is ordinary dimensional work.
    *Adding* a per-device quantity to a total-basis one is the defect. So the basis and
    unit checks look only at additive positions, where the two sides must agree.
    """
    def leaves(node: ast.AST) -> list[str]:
        return [n.id for n in ast.walk(node) if isinstance(n, ast.Name)]

    out = []
    for node in ast.walk(ast.parse(expr, mode="eval")):
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub)):
            out.append((leaves(node.left), leaves(node.right)))
    return out


def unit_family(unit: str) -> str | None:
    for family, members in UNIT_FAMILIES.items():
        if unit in members:
            return family
    return None


def unit_scale(unit: str | None) -> tuple[str | None, float]:
    """(family, factor to the family's base unit) for a unit recorded on a claim."""
    if unit is None:
        return None, 1.0
    for family, members in UNIT_FAMILIES.items():
        if unit in members:
            return family, float(members[unit] or 1.0)
    return None, 1.0


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def as_number(value) -> float | None:
    """A claim value that is genuinely a number. Booleans are not, however much Python disagrees."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def as_int(value) -> int | None:
    """int() that cannot crash the gate.

    An open-loop level records concurrency as null on purpose, because concurrency is an OUTCOME
    there rather than an input. int(None) raised TypeError and took the whole verifier down, and
    coercing the null to 0 was worse: it turned a correct declaration into an error saying the
    level declared no concurrency. So every cast that reads a load-shape field goes through here.
    """
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def strict_bool(value) -> bool | None:
    """True, False, or None for anything that is not a JSON boolean.

    `if not gate.get("passed")` reads the STRING "false" as true, because every non-empty string
    is truthy in Python. A gate whose manifest recorded passed="false" therefore shipped as a gate
    that passed, which is the unfalsifiable-declaration defect wearing a type error's clothes.
    """
    return value if isinstance(value, bool) else None


def claims_of(m: dict) -> dict:
    """The claims table, whatever the manifest actually contains.

    `claims = m["claims"]` raised KeyError and took the whole gate down with a traceback. A
    traceback is not a finding: nothing downstream can read it, the exit code is 1 either way,
    and the documented exit-2 path ("the manifest itself is broken") was unreachable because the
    process died before reaching it.
    """
    claims = m.get("claims") if isinstance(m, dict) else None
    return claims if isinstance(claims, dict) else {}


def objects(m: dict, key: str) -> list:
    """The list of objects under `key`, ignoring anything that is not one.

    A manifest that records `prose` as a dict, or a list with a string in it, used to crash the
    check that read it. The manifest is an input, and an input that can crash the verifier is an
    input that can skip it.
    """
    value = m.get(key) if isinstance(m, dict) else None
    if isinstance(value, dict):
        value = list(value.values())
    if not isinstance(value, (list, tuple)):
        return []
    return [v for v in value if isinstance(v, dict)]


# --------------------------------------------------------------------------------------
# rendered-document text and numerals


def strip_to_visible(html: str) -> str:
    """Tags out, character references decoded, block boundaries marked. The reader's text.

    THREE THINGS HAPPEN HERE AND EACH ONE CLOSED A HOLE.

    INLINE MARKUP IS REMOVED RATHER THAN SPACED. The old stripper turned every tag into a space,
    so "4414 <b>W</b>" became "4414  W" with two spaces where the numeral scanner allowed at most
    one, and the numeral left A5's jurisdiction entirely. "<b>4414 W</b>" was checked and
    "4414 <b>W</b>" was not, which is a distinction no author intends and both spellings are
    ordinary HTML. Worse, the unit-bearing COUNT did not rise, so the document went on reporting
    100% of unit-bearing numerals traced while the denominator quietly shrank. A coverage figure
    whose denominator the document can move is not a coverage figure. A reader sees no space where
    an inline tag was, so neither does this.

    BLOCK TAGS BECOME A SENTINEL. Removing the markup must not glue a numeral ending a paragraph
    to a unit-like word opening the next, or to the next table cell. BLOCK_BOUNDARY is not
    whitespace, so the whitespace-only gap the numeral scanners allow can never cross one.

    CHARACTER REFERENCES ARE DECODED. "&#57;&#44;&#53;&#50;&#54;&#46;&#54; tok/s" reads to a human
    as 9,526.6 tok/s and reached the gate as the seven bare numerals 57 44 53 50 54 46 54: the
    digits the gate read were not the digits the reader read, which is the whole failure mode this
    file exists to prevent. Decoding happens AFTER the tags come out, so an escaped "&lt;b&gt;" is
    text on the page and not a tag to strip.

    WHICH CHECK READS WHICH FORM, AND WHY. This function returns the DECODED text, because every
    numeral check has to see what the reader sees. F1 in check_render deliberately does NOT use it:
    it needs the form where an entity is still visible after one decode pass, since that is what a
    double escape looks like. Reading the decoded text there would report nothing (the entity is
    gone); reading the raw source there would report a correctly escaped "&amp;" as a leak. One
    decode, then look, is the only reading that separates the two.
    """
    body = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", html)
    body = BLOCK_TAG.sub(" " + BLOCK_BOUNDARY + " ", body)
    return html_unescape(re.sub(r"(?s)<[^>]+>", "", body))


def visible_attr_values(body: str) -> list:
    """Every title=, alt= and aria-label= value in the markup, whatever it is quoted with.

    Walked tag by tag and then pair by pair, which is what makes all three of HTML's quoting
    styles readable without inventing attributes out of prose. The old scanner read the whole
    document for one spelling, `name="value"`, so a number in title='peak draw 1911 W' was never
    scanned while the double-quoted spelling was: a distinction no author intends, between two
    forms a browser renders identically.
    """
    out = []
    for tag in HTML_TAG.finditer(body):
        region = tag.group(1)
        if not region or "=" not in region:
            continue
        for attr in VISIBLE_ATTRS.finditer(region):
            if attr.group(1).lower() not in VISIBLE_ATTR_NAMES:
                continue
            for value in (attr.group(2), attr.group(3), attr.group(4)):
                if value is not None:
                    out.append(html_unescape(value))
                    break
    return out


def visible_text(html: str) -> str:
    """The text a reader can see, plus the attribute values they can see.

    The values of title=, alt= and aria-label= are in scope on purpose. SVG <title> tooltips need
    nothing special because they are text between tags, but a number hidden in an attribute is
    invisible to a tag-stripper, and the omission attack put one there.
    """
    body = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", html)
    attrs = (" %s " % BLOCK_BOUNDARY).join(visible_attr_values(body))
    return strip_to_visible(html) + " %s " % BLOCK_BOUNDARY + attrs


def printed_value(token: str) -> float | None:
    """The number a printed numeral denotes, thousands separators and currency mark removed."""
    try:
        return float(token.lstrip(CURRENCY_MARKS).replace(",", ""))
    except ValueError:
        return None


def printed_decimals(token: str) -> int:
    return len(token.split(".", 1)[1]) if "." in token else 0


def attached_unit_is_real(text: str, start: int, numeral: str, unit: str, exceptions=()) -> bool:
    """Whether a unit letter glued to a number is a unit or part of a noun.

    The old rule required a decimal point, so "1240W", "17s" and "34x" were all dropped: an
    integer power, an integer duration and an integer multiplier fell out of the only check with
    jurisdiction over the shipped document, and an audit walked three fabrications through the
    gap with exit 0 and zero warnings. The protection it was buying is real but narrow, so it is
    now stated narrowly:

      * a four-digit year is a decade or a plural ("1950s", "2000s"), never a duration;
      * a number a model name introduces is a part number ("RTX 5090s", "Radeon 7900s");
      * anything else the manifest knows to be a name declares itself in
        coverage.attached_exceptions, with a reason, and the gate counts what each one removed.
    """
    if YEAR.match(numeral.replace(",", "")):
        return False
    if MODEL_CONTEXT.search(text[max(0, start - 24):start]):
        return False
    token = numeral + unit
    for allowance in exceptions:
        if allowance.exempts(token, numeral, text, start):
            return False
    return True


def printed_token(numeral: str, unit: str) -> str:
    """The numeral and its unit as one string, for allowance matching and coverage grouping."""
    return numeral + (" " if " " in unit else "") + unit


def unit_bearing_numerals(text: str, attached_exceptions=()) -> list[tuple]:
    """(start, end, numeral, unit, joined) for every numeral printed with a unit.

    `joined` says the numeral and its unit were separated by more than one whitespace character,
    which is what inline markup between them collapses to. Those numerals were invisible to this
    scanner until the stripper started removing inline tags, and the count of them is reported in
    the coverage line: widening jurisdiction silently is how a denominator moves without anyone
    noticing, so the change says how much it took in.

    The unit is recognised by shape rather than from a list, because a list of units is an
    opt-out by omission: whatever an author forgot to add became a numeral with no unit, and A5
    holds no jurisdiction over those. Three post-filters the regex cannot express:

      * a token that is not unit-shaped at all is English ("96 concurrent", "30 tokens");
      * "x" followed by a digit is a lane count or a part number ("PCIe 4.0 x4", "2x5090"),
        not a multiplier, so it is not a measurement of anything;
      * a single-letter unit glued to the number goes through attached_unit_is_real above.

    A currency figure carries its unit on the left, so it is collected in a second pass. It used
    to be a bare numeral, held to the bare floor rather than the unit-bearing one, which is how a
    fabricated cost per million tokens shipped.
    """
    out = []
    taken = set()
    for m in UNIT_CANDIDATE.finditer(text):
        numeral, separator, token = m.group(1), m.group(2), m.group(3)
        if unit_of(token) is None:
            continue
        if token == "x" and text[m.end():m.end() + 1].isdigit():
            continue
        if not separator and len(token) == 1 and not attached_unit_is_real(
                text, m.start(), numeral, token, attached_exceptions):
            continue
        # A multi-letter token glued to a numeral with NO gap must be a unit we actually know.
        #
        # unit_of accepts a short capitalised token on the reasoning that English words following a
        # numeral are lowercase. That held while inline markup became a space. It stopped holding
        # the moment markup started being removed outright, which is the fix directly above this
        # one: a contents entry written "<span>22</span>The embedding service" now reaches here as
        # "22The", and "The" is short, capitalised, and not a unit in any sentence ever written.
        # Reported as an error on the real document, which is how it was found.
        #
        # The capital heuristic keeps its job wherever a reader can see a gap, because that is
        # where an author actually writes a unit. With no gap at all the evidence is much weaker,
        # so it has to be a unit the tool can name rather than one it is guessing at.
        if not separator and len(token) > 1 and token not in PRINTED_UNIT and "/" not in token:
            continue
        out.append((m.start(), m.end(), numeral, token, len(separator) > 1))
        taken.add(m.start())
    for rx, canonical in SPELLED_UNIT_SCANNERS:
        for m in rx.finditer(text):
            if m.start() in taken:
                continue
            unit = canonical or re.sub(r"\s+", " ", m.group(3).strip().lower())
            out.append((m.start(), m.end(), m.group(1), unit, len(m.group(2)) > 1))
            taken.add(m.start())
    for m in ANY_NUMERAL.finditer(text):
        numeral = m.group(1)
        if numeral[0] in CURRENCY_MARKS and m.start() not in taken:
            out.append((m.start(), m.end(), numeral, numeral[0], False))
            taken.add(m.start())
    out.sort(key=lambda item: item[0])
    return out


def split_numeral_reading(printed: str) -> tuple[str, str]:
    """(kind, the reading) for a numeral a space separator splits.

    Three kinds, and the third one is the point.

      thousands   every group after the first is three digits and the first is one to three, which
                  is the only convention there is. One reading, so the gate takes it and says so.
      invisible   every separator is zero-width, so the reader is shown no gap at all and the
                  digits are simply one number. Also one reading, and not the same one: a
                  zero-width space between 9 and 25.2 is 925.2 and nothing else.
      ambiguous   a VISIBLE gap with a grouping the thousands convention does not explain.
                  "9 25.2 GB/s" is 925.2 to a merger and two numbers to a reader, and the gate has
                  no way to know which. There is no correct guess here, in either direction, so
                  this one is reported and never answered.

    The reading is returned with commas where the separators were, which is the same length as
    what it replaces so that every offset the scanners report stays valid, and which the value
    parser strips: for the invisible case comma-removal IS the concatenation a reader performs.
    """
    merged = re.sub("[" + GROUP_SEPARATORS + "]", ",", printed)
    if not any(ch in VISIBLE_GROUP_SEPARATORS for ch in printed):
        return "invisible", merged
    groups = merged.split(".", 1)[0].split(",")
    if len(groups[0]) <= 3 and all(len(g) == 3 for g in groups[1:]):
        return "thousands", merged
    return "ambiguous", merged


def merge_space_groups(text: str) -> tuple[str, list]:
    """The text with split numerals read the one way each can be read, and the sites where that
    happened.

    "9 526.6 tok/s" reads to a human as one number and reached the gate as two, so the gate
    validated 526.6 against a real claim while the reader saw a figure seven thousand larger.
    The substitution is the same length as what it replaces, on purpose: every offset the
    scanners report stays valid against the returned text, so a context window still lands where
    the reader would look. Each site is also reported, because a space is not an unambiguous
    thousands separator and the reader deserves to be told which reading the gate took.

    AN AMBIGUOUS SPLIT IS NOT MERGED. Merging it would be the gate choosing one of two readings
    and then checking the one it chose, which is the failure this whole file exists to prevent
    wearing a fix's clothes. The site is returned carrying its kind so the caller reports it, and
    the text is left exactly as the reader meets it.

    An ambiguous run with no unit after it and no decimal in it is left alone entirely: "levels
    1 2 4 8 16" is five figures in a sentence, and reading it as one is the same invention in the
    other direction.
    """
    sites = []
    out = list(text)
    for m in SPACE_GROUPED.finditer(text):
        printed = m.group(1)
        kind, merged = split_numeral_reading(printed)
        if kind == "ambiguous":
            trailing = AMBIGUOUS_TRAILING_UNIT.match(text, m.end(1))
            unit = trailing.group(1) if trailing and unit_of(trailing.group(1)) else None
            if unit is None and "." not in printed:
                continue
            sites.append({"start": m.start(1), "printed": printed, "merged": merged,
                          "kind": kind})
            continue
        for i, ch in enumerate(merged):
            out[m.start(1) + i] = ch
        sites.append({"start": m.start(1), "printed": printed, "merged": merged, "kind": kind})
    return "".join(out), sites


def context_of(text: str, start: int, end: int, width: int = 30) -> str:
    """The printed numeral with enough of its sentence to find it in the document.

    The block-boundary sentinel is printed as " / " rather than dropped: a context that silently
    spans two paragraphs reads like one sentence and sends the author looking for text that is not
    there.
    """
    window = text[max(0, start - width):end + width].replace(BLOCK_BOUNDARY, " / ")
    return re.sub(r"\s+", " ", window).strip(" /").strip()


def round_trips(numeral: str, unit: str, claims: list[tuple[str, float, str | None]]) -> str | None:
    """The first claim this printed numeral could be, or None.

    Precision-aware, because a claim of 77.8523 legitimately renders as "78%", "77.9%" and
    "77.85%": the numeral matches if the claim sits inside half a unit of the LAST PRINTED DIGIT.
    Comparing rounded values instead would depend on the rounding mode of whatever formatted the
    document, which is not knowable from here.

    Unit-aware in two ways. The comparison happens in the family's base unit, so a claim recorded
    in ms covers a figure printed in us. And a claim from a different family is not a candidate at
    all: a throughput in tok/s must not cover a printed percentage merely because the two share a
    value, which with two hundred claims in scope happens constantly.
    """
    value = printed_value(numeral)
    if value is None:
        return None
    family, scale = PRINTED_UNIT.get(unit, (None, 1.0))
    half_digit = 0.5 * (10.0 ** -printed_decimals(numeral))
    if family is None:
        # Dimensionless: a multiplier, or a bare numeral with no unit to reason about. Every claim
        # is a candidate and the comparison is on the raw values.
        return next((key for key, claim_value, _u in claims
                     if abs(claim_value - value) <= half_digit + 1e-12), None)
    target, tolerance = value * scale, half_digit * scale + 1e-12
    for key, claim_value, claim_unit in claims:
        claim_family, claim_scale = unit_scale(claim_unit)
        if claim_unit is not None and claim_family != family:
            continue
        if abs(claim_value * claim_scale - target) <= tolerance:
            return key
    return None


def numeric_claims(m: dict) -> list[tuple[str, float, str | None]]:
    """(key, value, unit) for every claim carrying a number, in a stable order."""
    out = []
    for key in sorted((m.get("claims") or {})):
        claim = m["claims"][key]
        value = as_number(claim.get("value"))
        if value is not None:
            out.append((key, value, claim.get("unit")))
    return out


def label_tokens(label: str) -> frozenset:
    """The meaning-carrying words of a label.

    Exact-label matching is what made the disagreement attack land: two claims sharing a label
    produced one warning, and rewording the label to say the same thing produced nothing. Comparing
    token sets instead means a reworded label is still the same label.
    """
    words = re.findall(r"[a-z0-9]+", (label or "").lower())
    return frozenset(w for w in words if w not in LABEL_STOPWORDS)


# --------------------------------------------------------------------------------------
# checks


def check_manifest_shape(m: dict, f: Findings, manifest_dir: Path | None = None) -> None:
    """Shape, plus A8 (the named run exists) and A9 (a kind is not a free pass)."""
    if not isinstance(m, dict):
        f.fatal = True
        f.error("manifest", "the manifest is not an object, so there is nothing to verify")
        return
    if m.get("schema") != SCHEMA:
        f.error("manifest", f"schema must be {SCHEMA!r}, found {m.get('schema')!r}")
    if not isinstance(m.get("claims"), dict):
        f.fatal = True
        f.error("manifest", "the manifest carries no claims object (found %s). Every other check "
                            "reads that table, so there is nothing here to verify."
                            % type(m.get("claims")).__name__)
        return
    runs = m.get("runs") if isinstance(m.get("runs"), dict) else {}
    anchors = source_anchors(manifest_dir, runs)
    for claim_id, c in claims_of(m).items():
        if not isinstance(c, dict):
            f.error("manifest", f"claim {claim_id} is not an object, so nothing about it can be "
                                f"checked (found {type(c).__name__})", claim=claim_id)
            continue
        if "value" not in c:
            f.error("manifest", f"claim {claim_id} has no value")
        if c.get("kind") not in {"measured", "derived", "assumption", "published", "projection", "supplied"}:
            f.error("manifest", f"claim {claim_id} has no valid kind", claim=claim_id)
        basis = c.get("basis")
        if basis is not None and basis not in BASES:
            f.error("C1", f"claim {claim_id} has unknown basis {basis!r}", claim=claim_id)
        if c.get("kind") == "measured":
            check_measured_run(claim_id, c, runs, f)
        check_kind_is_earned(claim_id, c, runs, f, anchors)


def check_measured_run(claim_id: str, c: dict, runs: dict, f: Findings) -> None:
    """A4 and A8: the run a measurement names has to be a run that happened.

    The old check tested `run` for truthiness and stopped there, so both "run-that-never-happened"
    and a single space shipped with zero findings. A run id is a key into the run table or it is
    decoration, and a whitespace id passes a truthiness test while naming nothing, which is why the
    strip comes first.
    """
    raw = c.get("run")
    named = raw.strip() if isinstance(raw, str) else raw
    if not named:
        if isinstance(raw, str) and raw:
            f.error("A8", f"measured claim {claim_id} names the blank run {raw!r}", claim=claim_id)
        else:
            f.error("A4", f"measured claim {claim_id} names no run", claim=claim_id)
        return
    if named not in runs:
        f.error(
            "A8",
            f"measured claim {claim_id} names run {named!r}, which is not in the run table "
            f"({', '.join(sorted(runs)) or 'empty'}). A run id that resolves to nothing makes the "
            "measurement unattributable, and nothing downstream can tell it from a real one.",
            claim=claim_id, run=named,
        )
        return

    # The named run carries bounds, so a measurement stamped outside them is checkable. This is a
    # WARNING and not an error on purpose: real result files in hand contain probes whose recorded
    # start is after the artefact's own finished_at_utc, the manifest documents that in a
    # window_note, and the result files are read-only evidence that must not be edited to suit a
    # check. So the gate reports the discrepancy and lets the manifest accept it by name.
    at = parse_ts(c.get("measured_at"))
    started, finished = parse_ts(runs[named].get("started")), parse_ts(runs[named].get("finished"))
    if at and ((started and at < started) or (finished and at > finished)):
        f.warn(
            "A8",
            f"claim {claim_id} is stamped {c.get('measured_at')}, outside run {named}'s window "
            f"({runs[named].get('started')} to {runs[named].get('finished')}). Either the claim is "
            "attributed to the wrong run or the run's bounds are wrong; say which in the manifest.",
            claim=claim_id, run=named,
        )


def check_kind_is_earned(claim_id: str, c: dict, runs: dict, f: Findings, anchors=()) -> None:
    """A9: `kind` is a free choice of the generator, so it cannot be the thing that grants trust.

    Three holes closed here, all proven. B1 recomputes only claims whose kind is "derived", so
    changing nothing but the kind to "supplied" shipped a claim printed as 3.0 whose own arithmetic
    gives 10.73. A percentage is by construction a quotient of two other numbers, so a percentage
    that is not derived is an arithmetic result nobody checked. And the source that redeems the
    exemption USED TO BE CHECKED FOR ITS SHAPE ALONE: "results/never-existed/nope.json" is a
    perfectly well-formed path naming no file on this planet, and it bought a permanent exemption
    from recomputation. A source that names nothing is not a source, so the file gets opened.
    """
    kind, unit, basis = c.get("kind"), c.get("unit"), c.get("basis")
    if (unit == "%" or basis == "ratio") and kind != "derived":
        waiver = c.get("derivation_waiver")
        if not (isinstance(waiver, str) and waiver.strip()):
            f.error(
                "A9",
                f"claim {claim_id} is a {'percentage' if unit == '%' else 'ratio'} of kind {kind!r} "
                "with no formula and no derivation_waiver. A ratio is a quotient of two numbers "
                "that are somewhere else in this manifest: derive it so the quotient is checked, "
                "or state in derivation_waiver why it cannot be.",
                claim=claim_id,
            )
    if kind in {"supplied", "published"}:
        source = c.get("source")
        source = source.strip() if isinstance(source, str) else ""
        if not source:
            f.error(
                "A9",
                f"claim {claim_id} is {kind!r} and names no source. This kind exempts the claim "
                "from B1's recomputation, so the source is the only thing left standing between "
                "the number and an invention.",
                claim=claim_id,
            )
            return
        found = resolve_source(source, runs, anchors)
        if found["accepted"]:
            # Only what the check LET THROUGH goes on this record. A rejected source is already an
            # error with its own line; listing it here as well would read as an acceptance.
            f.sources.append(dict(found, claim=claim_id, kind=kind, source=source))
        else:
            f.error(
                "A9",
                f"claim {claim_id} is {kind!r} with the source {source!r}, which names nothing a "
                "reader can go and look at. An exemption from recomputation has to be redeemable: "
                "name a run id from the run table, a URL, or a file or module path THAT EXISTS. "
                + found["detail"],
                claim=claim_id,
            )


# A source that can be gone and looked at. Deliberately mechanical: "engineering estimate" reads
# like provenance and is not, and any keyword list that admitted "specification" would admit
# whatever an author typed next. A run id, a URL and a path are the three things this file can
# actually resolve, so they are the three things it accepts.
# \S+ rather than \S, because the URL is now PRINTED as well as counted: a record saying a claim
# rests on "https://w" tells a reader nothing they can go and open.
SOURCE_URL = re.compile(r"(?i)\b(?:https?|ftp|file)://\S+|\bwww\.\S+")
# A path as an author writes one INSIDE A SENTENCE, so a leading "./" or "../" and a drive letter
# are part of the token and trailing sentence punctuation is not.
#
# THE OLD PATTERN REQUIRED EVERY SEGMENT TO START WITH A LETTER, which was fine while nothing ever
# opened the result and wrong the moment something did: a real run directory is named
# "20260825-160142-final", so "results/20260825-160142-final/nccl_allreduce.json" was read as the
# token "final/nccl_allreduce.json" and looked for in the wrong place. It also started at a letter,
# which quietly dropped the "../" off a path and resolved it against the wrong directory.
SOURCE_PATH = re.compile(
    r"(?:[A-Za-z]:[\\/])?(?:\.{1,2}[\\/])*[A-Za-z0-9_][\w.\\/-]*\w")
SOURCE_PATH_PREFIX = re.compile(r"^(?:[A-Za-z]:[\\/])?(?:\.{1,2}[\\/])*")


def path_candidate(token: str) -> bool:
    """Whether a token is shaped like a path or a module path rather than a word or a number.

    A separator is what makes it one. A token whose only separator is a dot must also start with a
    letter, or "25.2" in "9 25.2 GB/s" would be a filename this gate went looking for.
    """
    body = SOURCE_PATH_PREFIX.sub("", token)
    if not body:
        return False
    if "/" in token or "\\" in token:
        return True
    return "." in body and (body[0].isalpha() or body[0] == "_")

# How far above the manifest a relative source is looked for. A manifest is PUBLISHED into an
# output directory ("article/public") while its sources are written relative to the project it was
# built in, so the manifest's own directory alone would reject every honest path in a real report.
# It is bounded rather than open so a search cannot wander up to the filesystem root.
MAX_SOURCE_ANCESTORS = 4


def source_anchors(manifest_dir: Path | None, runs: dict) -> list:
    """The directories a relative source path is resolved against, most specific first.

    The manifest's own directory, a bounded walk up from it, and the directory of every run path
    the manifest ITSELF declares. That last group is not a guess: a manifest that says run
    "harness" lives at results/20260825-160142-final has told the reader where to look, so a claim
    citing a file inside it names something the reader can open.
    """
    roots: list = []

    def add(path):
        try:
            resolved = Path(path).resolve()
        except (OSError, ValueError):
            return
        if resolved not in roots:
            roots.append(resolved)

    add(manifest_dir if manifest_dir is not None else Path.cwd())
    base = roots[0] if roots else None
    for _ in range(MAX_SOURCE_ANCESTORS):
        if base is None or base.parent == base:
            break
        base = base.parent
        add(base)
    declared = list(roots)
    for run in (runs or {}).values():
        if not isinstance(run, dict):
            continue
        for field in ("path", "artifact"):
            value = run.get(field)
            if not (isinstance(value, str) and value.strip()):
                continue
            for root in declared:
                candidate = Path(value)
                candidate = candidate if candidate.is_absolute() else root / value
                try:
                    if candidate.is_dir():
                        add(candidate)
                        break
                    if candidate.is_file():
                        add(candidate.parent)
                        break
                except OSError:
                    continue
    return roots


def path_exists(token: str, anchors) -> bool:
    """Whether this path token names something on disk, under any anchor."""
    try:
        candidate = Path(token)
    except (OSError, ValueError):
        return False
    try:
        if candidate.is_absolute():
            return candidate.exists()
    except OSError:
        return False
    for root in anchors:
        try:
            if (root / token).exists():
                return True
        except (OSError, ValueError):
            continue
    return False


def module_exists(token: str, anchors) -> bool:
    """Whether this dotted token names a module file, on the import path or under an anchor.

    Resolved BY LOOKING, never by importing. importlib.find_spec imports every parent package on
    the way to the one it is asked about, and a gate that executes code named in the manifest it is
    judging has opened a hole considerably larger than the one it closed. Prefixes are walked from
    the longest down, because "gpubench.analysis.prefill_comms_ceiling" names a function inside a
    module and the module is the thing that exists.
    """
    parts = token.split(".")
    if len(parts) < 2 or not all(part.isidentifier() for part in parts):
        return False
    roots = [Path(entry) for entry in sys.path if entry] + list(anchors)
    while len(parts) >= 2:
        relative = Path(*parts)
        for root in roots:
            try:
                if (root / (str(relative) + ".py")).exists():
                    return True
                if (root / relative / "__init__.py").exists():
                    return True
            except (OSError, ValueError):
                continue
        parts.pop()
    return False


def resolve_source(source: str, runs: dict, anchors=()) -> dict:
    """What a source string actually names, and whether anything was opened to find out.

    THE CHECK THIS REPLACES NEVER OPENED ANYTHING. It tested the SHAPE of the string, so
    "results/never-existed/nope.json" redeemed a permanent exemption from recomputation: the
    strongest reason a claim can give for not being recomputed was a path that had only to be
    spelled like a path. A source that names nothing is not a source.

    A URL is the one case that cannot be settled here, because this gate runs offline and fetching
    it would make the build depend on a web server. It is ACCEPTED, and the fact that nothing was
    opened is RECORDED rather than left to look identical to a file that was: "checked" and "merely
    well-formed" are different verdicts and a reader is entitled to know which one a claim got.
    """
    if source in runs:
        return {"accepted": True, "resolved": True, "how": "run",
                "named": source, "detail": "resolved to run %r." % source}
    for token in source.split():
        stripped = token.strip(",.;:()[]")
        if stripped in runs:
            return {"accepted": True, "resolved": True, "how": "run",
                    "named": stripped, "detail": "resolved to run %r." % stripped}
    tried = []
    for match in SOURCE_PATH.finditer(source):
        token = match.group(0)
        if not path_candidate(token):
            continue
        tried.append(token)
        if path_exists(token, anchors):
            return {"accepted": True, "resolved": True, "how": "file", "named": token,
                    "detail": "resolved to the file %r." % token}
        if module_exists(token, anchors):
            return {"accepted": True, "resolved": True, "how": "module", "named": token,
                    "detail": "resolved to the module %r." % token}
    url = SOURCE_URL.search(source)
    if url:
        named = url.group(0).rstrip(",.;:)]")
        return {
            "accepted": True, "resolved": False, "how": "url", "named": named,
            "detail": "names the URL %s, which this gate accepts and did NOT resolve: it runs "
                      "offline." % named,
        }
    if tried:
        return {
            "accepted": False, "resolved": False, "how": None, "named": None,
            "detail": "Looked for %s under %s and found none of them."
                      % (", ".join(repr(t) for t in tried[:4]),
                         ", ".join(str(a) for a in list(anchors)[:3]) or "the current directory"),
        }
    return {"accepted": False, "resolved": False, "how": None, "named": None,
            "detail": "Nothing in it is shaped like a run id, a URL or a path."}


def check_derivations(m: dict, f: Findings) -> None:
    """B1: nothing that carries its own arithmetic is ever typed. Recompute it or fail.

    This used to recompute `kind == "derived"` and nothing else, so the recomputation was opt-in
    by a field the generator chooses: triple a value, change its kind to "assumption",
    "projection" or "measured", leave the contradicting formula sitting underneath it, and the
    claim ships. A9 closed the same hole for "supplied" and "published" by demanding a source.
    The general rule is simpler and does not need a list of kinds: IF A CLAIM CARRIES INPUTS AND
    A FORMULA, IT IS RECOMPUTED. The kind decides the wording, and nothing else.
    """
    claims = claims_of(m)
    env = {k: float(c["value"]) for k, c in claims.items()
           if isinstance(c, dict) and isinstance(c.get("value"), (int, float))
           and not isinstance(c.get("value"), bool)}
    for claim_id, c in claims.items():
        if not isinstance(c, dict):
            continue
        expr = c.get("formula")
        if not (isinstance(expr, str) and expr.strip()):
            if c.get("kind") == "derived":
                f.error("B1", f"derived claim {claim_id} declares no formula", claim=claim_id)
            continue
        if as_number(c.get("value")) is None:
            f.error("B1", f"claim {claim_id} carries the formula {expr!r} and a value that is not "
                          f"a number ({c.get('value')!r}), so nothing can be recomputed",
                    claim=claim_id)
            continue
        try:
            got = safe_eval(expr, env)
        except KeyError as exc:
            f.error("B1", f"{claim_id} references unknown claim {exc.args[0]!r}", claim=claim_id)
            continue
        except (ValueError, SyntaxError, ZeroDivisionError) as exc:
            f.error("B1", f"{claim_id} formula failed: {exc}", claim=claim_id)
            continue
        want = float(c["value"])
        tol = as_number(c.get("tolerance"))
        tol = 0.005 if tol is None else tol
        denom = abs(want) if want else 1.0
        rel = abs(got - want) / denom
        if rel > tol:
            kind = c.get("kind")
            f.error(
                "B1",
                f"{claim_id} does not recompute: printed {want:g}, formula gives {got:g} "
                f"({rel:.2%} off, tolerance {tol:.2%})"
                + ("" if kind == "derived" else
                   f". The claim calls itself {kind!r}, which is a free choice of the generator "
                   "and cannot be what exempts it: a claim carrying inputs and a formula is "
                   "checked against them whatever it is called. Fix the value, fix the formula, "
                   "or delete the formula and say where the number came from."),
                claim=claim_id, printed=want, computed=got, kind=kind,
            )
        # C1/C2: basis and unit hygiene, checked where it actually matters
        def attrs(names, field):
            vals = {claims[n].get(field) for n in names if n in claims}
            return {v for v in vals if v not in (None, "scalar", "ratio")}

        for left, right in additive_operands(expr):
            lb, rb = attrs(left, "basis"), attrs(right, "basis")
            if len(lb) == 1 and len(rb) == 1 and lb != rb and not c.get("basis_conversion"):
                f.error(
                    "C1",
                    f"{claim_id} adds a {lb.pop()} quantity to a {rb.pop()} one with no declared "
                    "conversion. This is how a per-device figure ends up compared against a total, "
                    "and it is why a capacity derivation stops closing.",
                    claim=claim_id,
                )
            lf = {unit_family(u) for u in attrs(left, "unit")} - {None}
            rf = {unit_family(u) for u in attrs(right, "unit")} - {None}
            if lf and rf and lf != rf and not c.get("unit_conversion"):
                f.error("C2", f"{claim_id} adds {sorted(lf)} to {sorted(rf)}", claim=claim_id)
            lu, ru = attrs(left, "unit"), attrs(right, "unit")
            if len(lu) == 1 and len(ru) == 1 and lu != ru and not c.get("unit_conversion"):
                f.warn("C2", f"{claim_id} adds {lu.pop()} to {ru.pop()}; confirm the scale factor",
                       claim=claim_id)

        # Comparing a per-sequence figure against a whole pool is the same defect in ratio form.
        if c.get("basis") == "ratio" and isinstance(expr, str) and "/" in expr:
            bases = attrs(formula_names(expr), "basis")
            if len(bases) > 1 and not c.get("basis_conversion"):
                f.warn(
                    "C1",
                    f"{claim_id} is a ratio over mixed bases {sorted(bases)}. Ratios like this "
                    "recompute cleanly while answering the wrong question; state the denominator "
                    "basis beside the printed percentage.",
                    claim=claim_id,
                )


def check_equalities(m: dict, f: Findings) -> None:
    """A1: one quantity, one value. The check that catches 233 against 204.5."""
    claims = claims_of(m)
    groups = m.get("equalities")
    for group in (groups if isinstance(groups, (list, tuple)) else []):
        keys = group.get("keys") if isinstance(group, dict) else group
        if not isinstance(keys, (list, tuple)) or not keys:
            f.error("A1", f"equality group {group!r} lists no claim keys")
            continue
        tol = as_number(group.get("tolerance")) if isinstance(group, dict) else None
        tol = 0.002 if tol is None else tol
        missing = [k for k in keys if k not in claims]
        if missing:
            f.error("A1", f"equality group references unknown claims {missing}")
            continue
        values = {k: as_number((claims[k] or {}).get("value")) for k in keys}
        unreadable = sorted(k for k, v in values.items() if v is None)
        if unreadable:
            f.error("A1", f"equality group holds claims whose values are not numbers: "
                          f"{unreadable}. An equality over a non-number asserts nothing.")
            continue
        lo, hi = min(values.values()), max(values.values())
        denom = abs(hi) or 1.0
        if (hi - lo) / denom > tol:
            pairs = ", ".join(f"{k}={v:g}" for k, v in values.items())
            f.error(
                "A1",
                f"the same quantity is printed with different values: {pairs} "
                f"(spread {(hi - lo) / denom:.2%}, tolerance {tol:.2%})",
                keys=list(keys),
            )
    check_same_quantity(m, f)


def check_same_quantity(m: dict, f: Findings) -> None:
    """A7: one quantity, one value, whatever the quantity is called.

    This was a WARNING that only fired on byte-identical labels, and both halves of that were
    wrong. It let 2181.7 and 2850.0 ship as warning 29 of 29, unreadable in the pile; and rewording
    the label of a third claim reading 3400.0 produced nothing at all. So: an error, and grouped
    three ways, from most structural to least.

      quantity      an explicit id shared by claims that are the same quantity. Exact and
                    author-declared, so a disagreement is an error.
      label         the printed label, compared as a set of meaning-carrying words, so
                    "aggregate throughput at concurrency 8" and "Aggregate throughput, c8" group
                    together. Also an error: the reader sees one name twice.
      near-label    the same words to within one, inside one (unit, basis) group. A heuristic, so
                    a warning, but it is the one that catches a label edited rather than reworded.
    """
    claims = {k: c for k, c in claims_of(m).items() if isinstance(c, dict)}
    groups = m.get("equalities")
    declared = set()
    for g in (groups if isinstance(groups, (list, tuple)) else []):
        keys = g.get("keys") if isinstance(g, dict) else g
        if isinstance(keys, (list, tuple)):
            declared.add(frozenset(keys))
    # Rounding is not disagreement. Same default as an undeclared equality group, for the same
    # reason: two printings of one quantity may legitimately differ in the last digit.
    tol = 0.002
    reported: set[frozenset] = set()

    def disagreement(keys):
        values = {}
        for k in keys:
            v = as_number(claims[k].get("value"))
            if v is not None:
                values[k] = v
        if len(values) < 2:
            return None
        lo, hi = min(values.values()), max(values.values())
        if (hi - lo) / (abs(hi) or 1.0) <= tol:
            return None
        return ", ".join(f"{k}={v:g}" for k, v in sorted(values.items()))

    def group(keyfn):
        out: dict = {}
        for claim_id, c in claims.items():
            k = keyfn(c)
            if k:
                out.setdefault(k, []).append(claim_id)
        return {k: v for k, v in out.items() if len(v) > 1}

    for quantity, keys in group(lambda c: (c.get("quantity") or "").strip()).items():
        if frozenset(keys) in declared:
            continue
        spread = disagreement(keys)
        if spread:
            reported.add(frozenset(keys))
            f.error("A7", f"claims declared as the same quantity {quantity!r} disagree: {spread}",
                    keys=sorted(keys), quantity=quantity)

    for tokens, keys in group(lambda c: label_tokens(c.get("label") or "")).items():
        if frozenset(keys) in declared or frozenset(keys) in reported:
            continue
        spread = disagreement(keys)
        if spread:
            reported.add(frozenset(keys))
            shown = sorted({(claims[k].get("label") or "") for k in keys})
            f.error(
                "A7",
                "the same quantity is printed twice with different values under the label(s) "
                f"{shown}: {spread}. One of the two is wrong about the machine.",
                keys=sorted(keys), label=" | ".join(shown),
            )

    # STRUCTURAL GROUPING, and it comes before the near-label heuristic because a label is a
    # string an author can rewrite. Making byte-identical labels an error closed the exact
    # reproduction and not the mechanism: rewording one of the two labels still silenced the
    # check, and the document shipped 101.9 tok/s and 142.6 tok/s for one quantity. These groups
    # are properties of the arithmetic instead. Two claims that evaluate the same expression are
    # the same number whatever they are called, so a disagreement is an error; two built from the
    # same inputs in the same unit and basis are probably one quantity, so that is a warning.
    def normalized_formula(c):
        expr = c.get("formula")
        if not (isinstance(expr, str) and expr.strip()):
            return ""
        return re.sub(r"\s+", "", expr)

    def input_signature(c):
        expr = c.get("formula")
        if not (isinstance(expr, str) and expr.strip()):
            return ""
        try:
            names = formula_names(expr)
        except (SyntaxError, ValueError):
            return ""
        if not names:
            return ""
        return (frozenset(names), c.get("unit"), c.get("basis"))

    for expr, keys in sorted(group(normalized_formula).items(), key=lambda kv: str(kv[0])):
        if frozenset(keys) in declared or frozenset(keys) in reported:
            continue
        spread = disagreement(keys)
        if spread:
            reported.add(frozenset(keys))
            f.error(
                "A7",
                f"claims {sorted(keys)} evaluate the same expression {expr!r} and disagree: "
                f"{spread}. Two claims computing one arithmetic result are one quantity however "
                "they are labelled, so at most one of these values can be right.",
                keys=sorted(keys),
            )

    def share_and_its_own_gap(keys, unit):
        """A share and the gap it implies, over the same two inputs.

        100 * a / b and the shortfall it leaves are built from the same inputs and are SUPPOSED to
        differ: they are two views of one comparison, not two answers to one question. Reporting
        them is the correct-but-wrong finding that gets a gate switched off, so the pair is tested
        against the arithmetic that would explain it first. Both denominators count, because a gap
        can be quoted over either member: 89.88% against 11.26% is 100/89.88 - 1, not 100 - 89.88.
        """
        if unit != "%" or len(keys) != 2:
            return False
        a, b = (as_number(claims[k].get("value")) for k in sorted(keys))
        if a is None or b is None:
            return False
        for x, y in ((a, b), (b, a)):
            gaps = [abs(100.0 - x)] + ([abs(100.0 * (100.0 / x - 1.0))] if x else [])
            if any(abs(gap - abs(y)) <= max(0.5, 0.02 * abs(y)) for gap in gaps):
                return True
        return False

    for signature, keys in sorted(group(input_signature).items(), key=lambda kv: str(kv[0])):
        if frozenset(keys) in declared or frozenset(keys) in reported:
            continue
        if share_and_its_own_gap(keys, signature[1]):
            continue
        spread = disagreement(keys)
        if spread:
            reported.add(frozenset(keys))
            names, unit, basis = signature
            f.warn(
                "A7",
                f"claims {sorted(keys)} are built from the same inputs {sorted(names)} in "
                f"{unit or 'no unit'}/{basis or 'no basis'} and disagree: {spread}. If they are "
                "one quantity give both the same `quantity` id; if they are two, their formulas "
                "should not be reachable from an identical input set without saying how they "
                "differ.",
                keys=sorted(keys),
            )

    # Near-duplicate labels, inside one (unit, basis) group so the comparison is between things
    # that could be the same measurement in the first place. A fallback, because it reads a
    # string: everything structural is above.
    buckets: dict = {}
    for claim_id, c in claims.items():
        if c.get("label"):
            buckets.setdefault((c.get("unit"), c.get("basis")), []).append(claim_id)
    for (unit, basis), keys in sorted(buckets.items(), key=lambda kv: str(kv[0])):
        for i, a in enumerate(sorted(keys)):
            for b in sorted(keys)[i + 1:]:
                if frozenset((a, b)) in reported or frozenset((a, b)) in declared:
                    continue
                ta, tb = label_tokens(claims[a].get("label")), label_tokens(claims[b].get("label"))
                if not ta or not tb or ta == tb:
                    continue
                overlap = len(ta & tb) / float(len(ta | tb))
                if overlap < 0.75:
                    continue
                # Labels that differ only in a NUMBER are different quantities by construction:
                # the number is what identifies the measurement point. "the ceiling at 128 tokens"
                # and "the ceiling at 2048 tokens" are two ceilings and must disagree, so treating
                # them as a near-duplicate pair produced twenty warnings that were all correct
                # readings of the wrong thing.
                if all(token.isdigit() for token in (ta ^ tb)):
                    continue
                spread = disagreement((a, b))
                if spread:
                    f.warn(
                        "A7",
                        f"{a} and {b} are both {unit or 'unitless'}/{basis or 'no basis'} and their "
                        f"labels differ by one word ({claims[a].get('label')!r} against "
                        f"{claims[b].get('label')!r}), yet {spread}. If these are one quantity, "
                        "give both the same `quantity` id so the gate can hold them to it.",
                        keys=[a, b],
                    )


def check_prose(m: dict, f: Findings) -> None:
    """A2 no bare numerals, A3 comparatives are true, F4 citations resolve."""
    claims = m["claims"]
    ops = {
        "gt": (lambda a, b: a > b, "greater than"),
        "lt": (lambda a, b: a < b, "less than"),
        "gte": (lambda a, b: a >= b, "at least"),
        "lte": (lambda a, b: a <= b, "at most"),
        "eq": (lambda a, b: math.isclose(a, b, rel_tol=1e-9), "equal to"),
        "approx": (None, "approximately equal to"),
        "within_pct": (None, "within a percentage of"),
        "ratio_between": (None, "a ratio inside a range"),
    }
    claims = claims_of(m)
    for block in objects(m, "prose"):
        text = block.get("text") or ""
        block_id = block.get("id", "<unnamed>")

        for key in PLACEHOLDER.findall(text):
            if key not in claims:
                f.error("F4", f"prose {block_id} cites unknown claim {{{{{key}}}}}", block=block_id)

        stripped = PLACEHOLDER.sub(" ", text)
        allowed = {str(x) for x in block.get("allow_literals", [])}
        for match in BARE_NUMERAL.finditer(stripped):
            token = match.group().strip().lstrip("-")
            if token in allowed or YEAR.match(token.replace(",", "")):
                continue
            # "section 17", "figure 3", "version 8.6": structural, not measured.
            if STRUCTURAL_CONTEXT.search(stripped[: match.start()]):
                continue
            f.error(
                "A2",
                f"prose {block_id} contains the bare numeral {token!r}. Numbers in prose go "
                "stale silently when a table is re-measured; cite the claim key instead, or "
                "list it under allow_literals if it is genuinely not a measurement.",
                block=block_id, numeral=token,
            )

        assertion = block.get("assert")
        if not assertion:
            continue
        op = assertion.get("op")
        if op not in ops:
            f.error("A3", f"prose {block_id} declares unknown comparison {op!r}", block=block_id)
            continue
        left_key, right_key = assertion.get("left"), assertion.get("right")
        if left_key not in claims or right_key not in claims:
            f.error("A3", f"prose {block_id} compares unknown claims", block=block_id)
            continue
        left = as_number((claims[left_key] or {}).get("value"))
        right = as_number((claims[right_key] or {}).get("value"))
        if left is None or right is None:
            f.error("A3", f"prose {block_id} compares claims whose values are not numbers "
                          f"({left_key}, {right_key})", block=block_id)
            continue
        lb, rb = claims[left_key].get("basis"), claims[right_key].get("basis")
        if lb and rb and lb != rb and lb not in ("scalar", "ratio") and rb not in ("scalar", "ratio"):
            f.error("C1", f"prose {block_id} compares {lb} against {rb} without conversion", block=block_id)
        if op in ("gt", "lt", "gte", "lte", "eq"):
            ok = ops[op][0](left, right)
        elif op == "approx":
            ok = math.isclose(left, right, rel_tol=float(assertion.get("rel_tol", 0.02)))
        elif op == "within_pct":
            pct = as_number(assertion.get("pct"))
            if pct is None:
                f.error("A3", f"prose {block_id} asserts within_pct and names no pct",
                        block=block_id)
                continue
            ok = abs(left - right) / (abs(right) or 1.0) <= pct / 100.0
        else:  # ratio_between
            lo, hi = as_number(assertion.get("min")), as_number(assertion.get("max"))
            if lo is None or hi is None:
                f.error("A3", f"prose {block_id} asserts ratio_between and names no min and max",
                        block=block_id)
                continue
            ratio = left / right if right else float("inf")
            ok = lo <= ratio <= hi
        if not ok:
            f.error(
                "A3",
                f"prose {block_id} asserts {left_key} is {ops[op][1]} {right_key}, but "
                f"{left_key}={left:g} and {right_key}={right:g}",
                block=block_id,
            )


def check_provenance(m: dict, prev: dict | None, f: Findings) -> None:
    """A4: tables draw from one run, and re-measurement claims are true."""
    claims = claims_of(m)
    tables = m.get("tables") if isinstance(m.get("tables"), dict) else {}
    for table_id, table in tables.items():
        if not isinstance(table, dict):
            f.error("A4", f"table {table_id} is not an object", table=table_id)
            continue
        cells = [claims[k] for k in (table.get("cells") or []) if k in claims]
        runs = {c.get("run") for c in cells if c.get("kind") == "measured" and c.get("run")}
        if len(runs) > 1 and not table.get("blended"):
            f.error(
                "A4",
                f"table {table_id} blends runs {sorted(runs)} without declaring it. A blended "
                "table is legitimate and has to say which rows came from where.",
                table=table_id,
            )
        if table.get("blended") and not table.get("blend_note"):
            f.error("A4", f"table {table_id} is blended but names no source for each row", table=table_id)

    if prev is None:
        return
    prev_claims = prev.get("claims", {})

    # Values that moved must appear in the changelog.
    changed = []
    for claim_id, c in claims.items():
        old = prev_claims.get(claim_id)
        if not old or not isinstance(old.get("value"), (int, float)):
            continue
        if not isinstance(c.get("value"), (int, float)):
            continue
        if abs(float(c["value"]) - float(old["value"])) > 1e-12:
            changed.append(claim_id)
    logged: set[str] = set()
    for entry in objects(m, "changelog"):
        logged |= set(entry.get("claims_changed") or [])
        logged |= set(entry.get("claims_remeasured") or [])
    silent = sorted(set(changed) - logged)
    if silent:
        f.error(
            "A4",
            f"{len(silent)} value(s) changed since the previous edition with no changelog row: "
            + ", ".join(silent[:8]) + ("..." if len(silent) > 8 else ""),
            claims=silent,
        )

    # A re-measurement claim is checked against the timestamps, not taken on trust.
    for entry in objects(m, "changelog"):
        for claim_id in (entry.get("claims_remeasured") or []):
            now, before = claims.get(claim_id), prev_claims.get(claim_id)
            if not now or not before:
                continue
            t_now, t_before = parse_ts(now.get("measured_at")), parse_ts(before.get("measured_at"))
            if t_now and t_before and t_now <= t_before:
                f.error(
                    "A4",
                    f"changelog {entry.get('version')} claims {claim_id} was re-measured, but its "
                    f"measurement time did not move ({now.get('measured_at')}).",
                    claim=claim_id,
                )


def check_sampling(m: dict, f: Findings) -> None:
    """D1 and D2: a percentile without its sample size is decoration."""
    for p in objects(m, "percentiles"):
        key, q, n = p.get("key"), as_number(p.get("q")), as_int(p.get("n"))
        if not n or n <= 0:
            f.error("D1", f"percentile {key} discloses no sample size", claim=key)
            continue
        if q is None or not 0.0 < q <= 1.0:
            f.error("D1", f"percentile {key} declares q={p.get('q')!r}, which is not a quantile "
                          "between 0 and 1", claim=key)
            continue
        rank = math.ceil(q * n)
        from_top = n - rank
        # The rank is NOT written back onto the level. `p.setdefault("rank", rank)` mutated the
        # caller's manifest, so the dict that got judged was not the dict that was handed over,
        # and the manifest written to disk afterwards carried a field no author put there.
        if 0 <= from_top <= 2:
            f.warn(
                "D2",
                f"{key} is a p{float(q) * 100:g} over n={n}, which resolves to ordered sample "
                f"{rank} of {n}, the {['worst', 'second worst', 'third worst'][from_top]} value. "
                "That is an extreme wearing a percentile's name; print the rank beside it, "
                "raise n toward 100, or report a max and say so.",
                claim=key, n=n, rank=rank,
            )


def level_arrival(level: dict, report_model: str | None) -> str:
    """The arrival model in force for one level.

    Two shapes reach here and both are real. A manifest hand-built beside a report writes
    `"arrival": "closed_loop"`; levels copied straight out of a harness document carry
    `"arrival": {"model": "open_loop_poisson", ...}` with the rates and the queue trace inside. A
    level that declares neither inherits the report's model, because that is what it means for the
    report to have declared one.
    """
    arrival = level.get("arrival")
    if isinstance(arrival, dict):
        arrival = arrival.get("model")
    if isinstance(arrival, str) and arrival.strip():
        return arrival.strip()
    return report_model or ""


def level_field(level: dict, name: str):
    """A load-shape field, whether it sits on the level or inside its arrival block."""
    if name in level and level[name] is not None:
        return level[name]
    arrival = level.get("arrival")
    if isinstance(arrival, dict) and arrival.get(name) is not None:
        return arrival[name]
    return None


QUEUE_TRACE_KEY = re.compile(r"(?i)queue|in[_ -]?flight")


def has_queue_trace(level: dict) -> bool:
    """Whether the level carries an in-flight or queue-depth trace with data in it.

    Open loop is the only mode where this exists to be measured, and it is the whole reason the
    mode is worth running: offered load is fixed, so a server falling behind shows up as a growing
    backlog and nowhere else. A closed-loop harness records the key with `sampled: false` and a
    reason, and that is correctly not a trace.
    """
    sources = [level]
    if isinstance(level.get("arrival"), dict):
        sources.append(level["arrival"])
    for source in sources:
        for key, value in source.items():
            if not QUEUE_TRACE_KEY.search(str(key)):
                continue
            if isinstance(value, (list, tuple)) and len(value):
                return True
            if isinstance(value, dict):
                if value.get("samples"):
                    return True
                if any(value.get(k) is not None
                       for k in ("max", "mean", "at_last_arrival", "last_sample", "peak")):
                    return True
            elif as_number(value) is not None:
                return True
    return False


def phrase_present(text: str, phrases, allow_negated: bool) -> str | None:
    """The first phrase that occurs in `text`, optionally ignoring negated occurrences."""
    low = text.lower()
    for phrase in phrases:
        start = low.find(phrase)
        while start != -1:
            before = low[max(0, start - 14):start]
            if allow_negated or not any(before.endswith(n) for n in NEGATORS):
                return phrase
            start = low.find(phrase, start + 1)
    return None


def check_arrival_note(report: dict, f: Findings) -> None:
    """D7: the declared arrival model against the note printed beside it.

    Cheap, and it catches the attack exactly. Flipping closed_loop to open_loop_poisson on a
    fixed-in-flight harness shipped with zero findings, printing "arrival_model
    open_loop_poisson" directly beside "Fixed in-flight population per level; no independent
    arrival process". Nothing read the two together. A human would have needed one second.
    """
    model = report.get("arrival_model")
    note = report.get("arrival_note")
    if not isinstance(model, str) or not isinstance(note, str) or not note.strip():
        return
    if model.startswith("open_loop"):
        hit = phrase_present(note, CLOSED_LOOP_PHRASES, allow_negated=True)
        if hit:
            f.error(
                "D7",
                f"arrival_model is {model!r} but arrival_note says {hit!r}. Those describe two "
                "different experiments, and the note is the one a reader believes. One of them is "
                "a leftover from the other mode.",
            )
    elif model == "closed_loop":
        hit = phrase_present(note, OPEN_LOOP_PHRASES, allow_negated=False)
        if hit:
            f.error(
                "D7",
                f"arrival_model is 'closed_loop' but arrival_note asserts {hit!r}. A closed-loop "
                "harness has no arrival process to be independent of anything.",
            )


def check_open_loop_level(level: dict, name, model: str, requests: int | None, f: Findings) -> None:
    """D6: what an open-loop level has to disclose instead of waves.

    An open-loop level has no concurrency by design, so the checks that make a closed-loop level
    interpretable do not apply to it. The three that replace them are not decoration: the target
    rate is the load that was ASKED for, the achieved rate is the load the engine actually
    completed, and the queue trace is the backlog between them. Without all three, an open-loop
    latency figure cannot be read at all, because the reader cannot tell a fast server from a
    server that was never offered much.
    """
    missing = []
    if as_number(level_field(level, "target_rate_req_s")) is None:
        # A sweep records the rates as a list; either shape is a declaration.
        if not isinstance(level_field(level, "target_rate_req_s"), (list, tuple)):
            missing.append("target_rate_req_s")
    if as_number(level_field(level, "achieved_rate_req_s")) is None:
        missing.append("achieved_rate_req_s")
    if not has_queue_trace(level):
        missing.append("a queue or in-flight trace (queue_depth with samples, or peak_inflight)")
    if missing:
        f.error(
            "D6",
            f"level {name} declares arrival {model!r} and is missing {', '.join(missing)}. An "
            "open-loop level is worth running precisely because offered load is fixed, so a server "
            "falling behind shows up as a rate deficit and a growing backlog. Without those the "
            "level reports latency with nothing to interpret it against.",
            level=name,
        )
    if requests is not None and requests <= 0:
        f.error("D6", f"level {name} records no requests", level=name)


def check_load_shape(m: dict, f: Findings) -> None:
    """D3, D4, D6 and D7: the shape of the offered load, and a declaration that matches it."""
    report = m.get("report") or {}
    arrival = report.get("arrival_model")
    if arrival not in {"closed_loop", "open_loop_constant", "open_loop_poisson"}:
        f.error(
            "D4",
            "no arrival model declared. A latency percentile quoted as a service level has to say "
            "whether requests arrived in a burst or a stream: a closed-loop harness cannot produce "
            "the queue build-up that generates real tail latency.",
        )
    check_arrival_note(report, f)

    claims = claims_of(m)
    for level in objects(m, "levels"):
        name = level.get("name")
        model = level_arrival(level, arrival)
        conc = as_int(level.get("concurrency"))
        n = as_int(level.get("requests"))
        if n is None:
            # Levels copied from a harness document count attempts and successes separately.
            n = as_int(level.get("requests_attempted")) or as_int(level.get("requests_ok"))

        if model.startswith("open_loop"):
            check_open_loop_level(level, name, model, n, f)
            # The wave arithmetic is SKIPPED here, and not as a convenience. Waves exist because a
            # closed-loop pool refills in lockstep; under an independent arrival process there is
            # no wave to be whole and no concurrency to divide by, so D3 asks a question the level
            # has no answer to. Running it anyway is what turned a correct declaration into
            # "level r40 declares no concurrency or request count", after first crashing on the
            # null with a TypeError.
            continue

        if not conc or conc <= 0 or not n or n <= 0:
            f.error("D3", f"level {name} declares no concurrency or request count", level=name)
            continue
        if n % conc:
            f.error(
                "D3",
                f"level {name}: {n} requests at concurrency {conc} is not a whole number of waves. "
                f"The final wave runs at concurrency {n % conc}, which depresses throughput by an "
                "amount that depends on how badly the count divides and reads as scatter.",
                level=name,
            )
        waves = n / conc
        if waves < 3:
            f.warn(
                "D3",
                f"level {name} is {waves:g} wave(s). With so few waves the level is a burst rather "
                "than a steady state: it is all ramp-up and drain, so a throughput figure from it "
                "should not be called sustained.",
                level=name, waves=waves,
            )
        # The tell for a single synchronised wave: duration equals the slowest request.
        e2e_key, dur = level.get("e2e_p95_key"), as_number(level.get("duration_s"))
        if e2e_key in claims and dur:
            e2e = as_number((claims[e2e_key] or {}).get("value"))
            if e2e and abs(dur - e2e) / e2e < 0.01 and waves > 1:
                f.warn(
                    "D3",
                    f"level {name} reports {waves:g} waves, but its duration equals the end-to-end "
                    "p95, which is the signature of one synchronised wave. Check the wave accounting.",
                    level=name,
                )

    for s in objects(m, "sustained"):
        if not s.get("duration_s"):
            f.error("D5", f"sustained figure {s.get('key')} states no duration", claim=s.get("key"))
        elif s.get("reached_steady_state") is None:
            f.warn("D5", f"sustained figure {s.get('key')} does not say whether the measured quantity "
                         "had stopped moving when the run ended", claim=s.get("key"))


def check_roofs(m: dict, f: Findings) -> None:
    """E1 and E2: a roof measured with other work resident is a floor, and it must say so."""
    claims = claims_of(m)
    for ceiling in objects(m, "ceilings"):
        key, mode = ceiling.get("key"), ceiling.get("mode")
        if mode not in {"shared", "exclusive"}:
            f.error(
                "E1",
                f"ceiling {key} declares no measurement mode. A percentage of a shared-mode roof has "
                "a floor in its denominator and does not compare against an exclusive-mode one.",
                claim=key,
            )
        elif mode == "shared" and not ceiling.get("caveat_anchor"):
            f.error(
                "E1",
                f"ceiling {key} is shared-mode with no caveat anchored beside the fraction that uses "
                "it. A caveat only in the limitations section is not where the reader meets the claim.",
                claim=key,
            )
        kind = claims.get(key, {}).get("kind")
        if kind in {"published", "derived"} and ceiling.get("from_vendor_headline"):
            f.warn(
                "E2",
                f"ceiling {key} descends from a vendor headline. Using a marketing figure as the roof "
                "makes the roof-to-workload gap unfalsifiable, which is the point of drawing it.",
                claim=key,
            )


def check_gate(m: dict, f: Findings, manifest_dir: Path | None = None) -> None:
    """G1, G2 and G3: speed is only accepted beside a quality gate that ran, passed, and can be
    read back out of the artefact it claims to come from."""
    gate = m.get("gate")
    if gate is not None and not isinstance(gate, dict):
        f.error("G1", "gate must be an object recording how the quality gate ran, found %s"
                      % type(gate).__name__)
        return
    if not gate:
        f.error(
            "G1",
            "no quality gate recorded. A benchmark that measures only speed rewards a stack that got "
            "faster by getting worse, so a more aggressive quantisation or a truncated context reads "
            "as an improvement.",
        )
        return
    passed = strict_bool(gate.get("passed"))
    if passed is None:
        f.error(
            "G1",
            "gate.passed is %r, which is not a boolean. Every non-empty string is true in Python, "
            "so the STRING \"false\" was read as a gate that passed. Declare true or false."
            % (gate.get("passed"),))
    if passed is not True:
        f.error("G1", "the quality gate did not pass; performance figures taken in this window are unsound")
    runs_table = m.get("runs") if isinstance(m.get("runs"), dict) else {}
    if gate.get("window_run") and gate["window_run"] not in runs_table:
        f.error("G1", f"the gate names run {gate['window_run']!r}, which is not in the run table")
    if not gate.get("cases_published"):
        f.error("G2", "the gate's cases are not published. A gate nobody can re-run is an assertion.")
    check_gate_against_artefact(m, gate, f, manifest_dir)


def read_gate_result(doc: dict) -> dict | None:
    """The gate outcome as the artefact recorded it, or None if the artefact does not record one.

    Two shapes are read. A gpubench result carries `probes.accuracy` with a summary and the list of
    published cases; a hand-written artefact may carry a top-level `gate` object. Anything else is
    reported as unreadable rather than guessed at, because a gate result invented by the reader is
    exactly the thing this check exists to stop.
    """
    accuracy = ((doc.get("probes") or {}).get("accuracy")
                if isinstance(doc.get("probes"), dict) else None)
    if isinstance(accuracy, dict):
        summary = accuracy.get("summary") or {}
        method = accuracy.get("method") or {}
        published = method.get("cases_published")
        cases = as_int(summary.get("cases"))
        out = {
            "cases_published": (len(published) if isinstance(published, (list, tuple))
                                else as_int(published)),
            "cases": cases,
            "errors": len(accuracy.get("errors") or []),
            "pcts": {k: as_number(v) for k, v in summary.items()
                     if k.endswith("_pct") and as_number(v) is not None},
            "deterministic": as_int(summary.get("deterministic")),
            "where": "probes.accuracy",
        }
        return out
    inner = doc.get("gate")
    if isinstance(inner, dict):
        published = inner.get("cases_published")
        return {
            "cases_published": (len(published) if isinstance(published, (list, tuple))
                                else as_int(published)),
            "cases": as_int(inner.get("cases")),
            "errors": len(inner.get("errors") or []),
            "pcts": {k: as_number(v) for k, v in inner.items()
                     if k.endswith("_pct") and as_number(v) is not None},
            "deterministic": as_int(inner.get("deterministic")),
            "passed_declared": inner.get("passed"),
            "where": "gate",
        }
    return None


def check_gate_against_artefact(m: dict, gate: dict, f: Findings, manifest_dir: Path | None) -> None:
    """G3: gate.passed and gate.cases_published are measurements, not declarations.

    Hardcoding passed=True and cases_published=999 shipped with zero findings, because nothing ever
    opened the file the gate said it came from. A gate is the one check in this catalogue whose job
    is to stop a stack that got faster by getting worse, so a gate result nobody read back is worse
    than no gate at all: it is a gate that reports success unconditionally.
    """
    runs_table = m.get("runs") if isinstance(m.get("runs"), dict) else {}
    window = gate.get("window_run")
    run = runs_table.get(window) if window else None
    run = run if isinstance(run, dict) else None
    declared_path = (run or {}).get("artifact")
    waiver = gate.get("artifact_waiver")
    waiver = waiver.strip() if isinstance(waiver, str) else ""
    if not window or run is None or not declared_path:
        # THIS USED TO BE A WARNING, and the "artifact" key was optional, which made G3 its own
        # opt-out: a gate whose artefact recorded passed=false, 40% exact match and 4 of 10 cases
        # deterministic shipped as a warning the moment the key was deleted. An undeclared
        # artefact is not a weaker gate result, it is no gate result, and the one check whose job
        # is to stop a stack that got faster by getting worse cannot be switched off by omission.
        report_at = f.warn if waiver else f.error
        report_at(
            "G3",
            "the gate result is unfalsifiable as recorded: run "
            f"{window!r} declares no \"artifact\" path, so passed={gate.get('passed')!r} and "
            f"cases_published={gate.get('cases_published')!r} are assertions rather than readings. "
            "Point the run at the result file the gate ran in"
            + (f". WAIVED IN THE MANIFEST: {waiver}" if waiver
               else ", or record gate.artifact_waiver saying why no file can be named. Note that "
                    "the spelling is \"artifact\"; a run's descriptive \"artefact\" key is a "
                    "different field and does not point the gate at anything."),
        )
        return

    path = Path(declared_path)
    if not path.is_absolute() and manifest_dir is not None:
        path = manifest_dir / declared_path
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        f.error("G3", f"run {window!r} names the artifact {declared_path!r}, which cannot be read "
                      f"({exc}). The gate's evidence has to exist where the manifest says it does.")
        return
    if not isinstance(doc, dict):
        f.error("G3", f"the artifact {declared_path!r} is not an object")
        return

    result = read_gate_result(doc)
    if result is None:
        report_at = f.warn if waiver else f.error
        report_at("G3", f"the artifact {declared_path!r} records no gate result this verifier can "
                        "read (looked for probes.accuracy and a top-level gate object), so the "
                        "declaration is unfalsifiable"
                        + (f". WAIVED IN THE MANIFEST: {waiver}" if waiver else
                           ". A file that does not record the gate's outcome is not evidence that "
                           "the gate ran; name one that does, or record gate.artifact_waiver."))
        return

    # Passing means every case cleared, at the bar the manifest declares. The default bar is 100%
    # because that is what an accuracy REGRESSION gate means; a lower bar is legitimate and has to
    # be written down as gate.threshold_pct, where a reader can see it.
    threshold = as_number(gate.get("threshold_pct"))
    threshold = 100.0 if threshold is None else threshold
    reasons = []
    if result["errors"]:
        reasons.append("%d case(s) errored" % result["errors"])
    for name, value in sorted(result["pcts"].items()):
        if value + 1e-9 < threshold:
            reasons.append("%s is %g, below the %g threshold" % (name, value, threshold))
    if (result["deterministic"] is not None and result["cases"] is not None
            and result["deterministic"] < result["cases"]):
        reasons.append("only %d of %d cases were deterministic"
                       % (result["deterministic"], result["cases"]))
    if result.get("passed_declared") is False:
        reasons.append("the artefact itself records passed=false")
    measured_pass = not reasons

    declared_pass = strict_bool(gate.get("passed"))
    if declared_pass is not None and declared_pass != measured_pass:
        f.error(
            "G3",
            "the manifest says the gate passed=%r; the artefact %s says %s (%s)."
            % (gate.get("passed"), declared_path,
               "passed" if measured_pass else "did not pass",
               "; ".join(reasons) or "every case cleared"),
        )

    declared_cases = gate.get("cases_published")
    found = result["cases_published"]
    if isinstance(declared_cases, bool):
        if declared_cases != bool(found):
            f.error("G3", "the manifest says cases_published=%r; the artefact %s publishes %s"
                          % (declared_cases, declared_path,
                             "%d case(s)" % found if found else "none"))
    elif as_int(declared_cases) is not None:
        if found is None:
            f.error("G3", "the manifest publishes %s cases; the artefact %s records no case list"
                          % (declared_cases, declared_path))
        elif as_int(declared_cases) != found:
            f.error("G3", "the manifest says %s published case(s); the artefact %s carries %d"
                          % (declared_cases, declared_path, found))


FIGURE_BLOCK = re.compile(r'(?is)<figure\b[^>]*\bid\s*=\s*"([^"]+)"[^>]*>(.*?)</figure>')
TABLE_BLOCK = re.compile(r"(?is)<table\b([^>]*)>(.*?)</table>")
TABLE_ID = re.compile(r'(?is)\bid\s*=\s*"([^"]+)"')


def non_empty_table(html: str) -> bool:
    """A table with at least one body cell carrying visible text.

    The empty disclosure is the defect this exists for: the document shipped
    "<details><summary>Table view</summary></details>" directly under a note telling the reader
    that a series was in the table view rather than on the plot. The series was in neither.
    """
    for table in TABLE_BLOCK.finditer(html):
        for cell in re.finditer(r"(?is)<t[dh]\b[^>]*>(.*?)</t[dh]>", table.group(2)):
            if re.sub(r"(?s)<[^>]+>", "", cell.group(1)).strip():
                return True
    return False


def check_render(m: dict, rendered: Path | None, f: Findings) -> None:
    """F1 and F2 over the manifest, F3 and F4 over the rendered document."""
    for fig in objects(m, "figures"):
        if not fig.get("table_view") and not fig.get("table_view_shared_with"):
            f.error("F2", f"figure {fig.get('id')} has no table view; a chart without its values is "
                          "an assertion", figure=fig.get("id"))
    for block in objects(m, "prose"):
        if ENTITY_LEAK.search(block.get("text") or ""):
            f.error("F1", f"prose {block.get('id')} contains an unescaped HTML entity", block=block.get("id"))
    if not (rendered and rendered.exists()):
        return
    text = rendered.read_text(encoding="utf-8", errors="replace")
    body = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", text)
    body = re.sub(r"(?s)<[^>]+>", " ", body)
    # DECODE ONCE, THEN LOOK. Matching the entity pattern against the SOURCE cannot tell a
    # correctly escaped "&amp;" (which the reader sees as "&") from the double escape
    # "&amp;amp;" (which the reader sees as "&amp;"), and it reported the correct one as a leak.
    # After one decode pass, only a double escape still looks like an entity, which is exactly
    # the defect: an entity the reader can see.
    leaks = sorted(set(ENTITY_LEAK.findall(html_unescape(body))))
    if leaks:
        f.error("F1", f"rendered document shows literal entities in visible text: {leaks}. "
                      "A reader sees the entity itself, which means it was escaped twice.")
    check_table_views(m, text, f)
    check_table_cells(m, text, f)


def check_table_views(m: dict, html: str, f: Findings) -> None:
    """F3: a declared table view has to be a table in the document.

    F2 read the manifest's own boolean, so rendering the figure with its table omitted while the
    manifest still declared table_view True shipped with zero findings. table_view is an author's
    intention; this reads the outcome.
    """
    rendered_figs = {fid: inner for fid, inner in FIGURE_BLOCK.findall(html)}
    declared = {fig.get("id"): fig for fig in objects(m, "figures") if fig.get("id")}

    def has_table(fid):
        return fid in rendered_figs and non_empty_table(rendered_figs[fid])

    for fid, fig in declared.items():
        shared = fig.get("table_view_shared_with")
        if shared:
            if shared not in declared:
                f.error("F3", f"figure {fid} shares its table view with {shared!r}, which is not a "
                              "figure in this manifest", figure=fid)
            elif not has_table(shared):
                f.error("F3", f"figure {fid} shares its table view with {shared}, which renders no "
                              "table of its own, so neither figure ships its values", figure=fid)
            continue
        if fig.get("table_view") is not True:
            # A truthy string ("shared with figure arlat: the same rows") states the intention in
            # prose, where no check can reach it. It is not a defect, and it is not verifiable
            # either, so it is reported as what it is.
            if fig.get("table_view"):
                f.warn("F3", f"figure {fid} declares its table view as free text "
                             f"({fig['table_view']!r}). Use table_view_shared_with so the gate can "
                             "confirm the table it points at actually renders.", figure=fid)
            continue
        if fid not in rendered_figs:
            f.error("F3", f"figure {fid} declares a table view but does not appear in the rendered "
                          "document at all", figure=fid)
        elif not non_empty_table(rendered_figs[fid]):
            f.error("F3", f"figure {fid} declares a table view and renders none, or renders an "
                          "empty one. The values a chart asserts are then nowhere in the document.",
                    figure=fid)


def check_table_cells(m: dict, html: str, f: Findings) -> None:
    """F4: numerals printed in a table trace to a declared cell of that table.

    Table-scoped, and so stricter than A5: a figure in the capacity table has to be one of the
    capacity table's own claims, not any claim anywhere that happens to share its value. It engages
    on a table the document identifies, by an id on the <table> or on the <figure> around it.
    """
    tables = {k: v for k, v in (m.get("tables") or {}).items()
              if isinstance(v, dict)} if isinstance(m.get("tables"), dict) else {}
    claims = claims_of(m)
    if not tables:
        return
    located: dict = {}
    for table in TABLE_BLOCK.finditer(html):
        found = TABLE_ID.search(table.group(1))
        if found and found.group(1) in tables:
            located.setdefault(found.group(1), []).append(table.group(2))
    for fid, inner in FIGURE_BLOCK.findall(html):
        if fid in tables:
            for table in TABLE_BLOCK.finditer(inner):
                located.setdefault(fid, []).append(table.group(2))

    for table_id in sorted(tables):
        if table_id not in located:
            continue
        cells = [(k, as_number((claims.get(k) or {}).get("value")),
                  (claims.get(k) or {}).get("unit"))
                 for k in tables[table_id].get("cells", []) if k in claims]
        cells = [(k, v, u) for k, v, u in cells if v is not None]
        stray = []
        for inner in located[table_id]:
            # The same stripper A5 uses, and for the same reason: a cell separated from its unit by
            # inline markup, or a value written with character references, has to read here the way
            # it reads on the page. The block sentinel is what keeps one cell's numeral from
            # pairing with the next cell's word.
            text = strip_to_visible(inner)
            for start, end, numeral, unit, _joined in unit_bearing_numerals(text):
                if round_trips(numeral, unit, cells) is None:
                    stray.append(printed_token(numeral, unit))
        if stray:
            unique = sorted(set(stray))
            f.warn(
                "F4",
                "table %s prints %d numeral(s) that match no declared cell of it: %s%s. Either the "
                "table draws on a claim it does not declare, or a figure in it was typed."
                % (table_id, len(unique), ", ".join(unique[:8]),
                   " and %d more" % (len(unique) - 8) if len(unique) > 8 else ""),
                table=table_id,
            )

    missing = sorted(set(tables) - set(located))
    if missing:
        f.warn(
            "F4",
            "%d declared table(s) could not be found in the rendered document, so their cells were "
            "not checked against what shipped: %s. A rendered table needs id=\"<table id>\", or to "
            "sit inside a figure with that id, before F4 has anything to read."
            % (len(missing), ", ".join(missing[:10])),
        )


# Unrelated numerals and unrelated sentences, used to catch an allowance that is not an allowance
# but a disarm. A pattern of "." compiled, exempted every numeral in the document, and still
# reported 490/490 unit-bearing and 100.0%, because coverage counts an exempted numeral as covered.
# A pattern that matches all of these matches anything, whatever it was meant to say.
ALLOWANCE_DECOY_NUMERALS = ("1", "7", "42", "3.14", "0.001", "1,240", "99.9", "12.5",
                            "1240W", "17s", "34x", "5%", "615.2", "$0.11")
ALLOWANCE_DECOY_CONTEXTS = (
    "the pair sustained 42 tok/s across the third wave",
    "a fabricated 1,240 tok/s in the abstract, cited nowhere",
    "figure 7 puts the overhead at 0.001 s per decode step")


class Allowance:
    """One coverage.allow (or coverage.attached_exceptions) entry, and what it actually removed.

    Three defects live here, all proven against the real tool.

      * The pattern was matched against the numeral's sixty-character CONTEXT as well as the
        numeral, so any blessed phrase laundered every numeral written near it: "of a reference"
        beside a fabrication exempted the fabrication. `pattern` now reads the NUMERAL only.
      * Context matching is sometimes genuinely wanted (an axis of tick labels), so it is a
        SEPARATE field that has to be asked for by name, and it only exempts a numeral its own
        match actually SPANS. A phrase carrying no digits therefore exempts nothing.
      * Nobody could see how much an allowance was taking. `hits` is counted and printed, so a
        broad allowance is visible in the log rather than showing up as a perfect score.
    """

    def __init__(self, numeral=None, context=None, source: str = "", why: str = "") -> None:
        self.numeral = numeral
        self.context = context
        self.source = source
        self.why = why
        self.hits = 0

    def exempts(self, token: str, numeral: str, window: str, offset: int) -> bool:
        if self.numeral is not None and (self.numeral.search(token)
                                         or self.numeral.search(numeral)):
            self.hits += 1
            return True
        if self.context is not None:
            # Matched against the window with block boundaries rendered as ordinary spaces.
            #
            # An author writes a context_pattern against the sentence they can see, and BLOCK_BOUNDARY
            # is an internal marker they have no reason to know exists. Once inline markup started
            # being removed and SVG ticks became separate elements, patterns written with \s+ stopped
            # matching text that now reads "Memory \x00 128 GiB DDR5", and five allowances silently
            # exempted nothing. Silent is the problem: an allowance that stops matching does not
            # announce itself, it just lets a number become an error somewhere else.
            #
            # Substituted one character for one character so every offset in the window still lines
            # up, which is what lets the span test below decide whether the match actually covers
            # this numeral rather than merely sitting near it.
            plain = window.replace(BLOCK_BOUNDARY, " ")
            for m in self.context.finditer(plain):
                if m.start() <= offset < m.end():
                    self.hits += 1
                    return True
        return False


def compile_allowances(coverage: dict, f: Findings, key: str = "allow") -> list:
    """coverage.allow, as Allowance objects. An allowance with no reason is not an allowance.

    Every entry here exempts a printed number from having to be a claim, which is the one thing
    this whole file exists to require. An exemption with no stated reason is indistinguishable from
    the defect it hides, so it is rejected rather than honoured, and so is one that exempts
    everything.
    """
    out: list = []
    entries = coverage.get(key)
    if entries is None:
        return out
    if not isinstance(entries, (list, tuple)):
        f.error("A5", "coverage.%s must be a list of allowance objects" % key)
        return out
    for i, entry in enumerate(entries):
        where = "coverage.%s[%d]" % (key, i)
        if not isinstance(entry, dict):
            f.error("A5", "%s is not an object with pattern and why" % where)
            continue
        pattern, context = entry.get("pattern"), entry.get("context_pattern")
        why = entry.get("why")
        has_pattern = isinstance(pattern, str) and pattern
        has_context = isinstance(context, str) and context
        if not has_pattern and not has_context:
            f.error("A5", "%s declares no pattern. Give it `pattern`, which is matched against the "
                          "NUMERAL, or `context_pattern`, which is matched against the surrounding "
                          "text and only exempts the numeral its own match spans." % where)
            continue
        if not (isinstance(why, str) and why.strip()):
            f.error("A5", "%s (%r) records no reason. An allowance that does not say why is a "
                          "silenced check." % (where, pattern or context))
            continue
        compiled: dict = {}
        rejected = False
        for field, source, decoys in (("pattern", pattern, ALLOWANCE_DECOY_NUMERALS),
                                      ("context_pattern", context, ALLOWANCE_DECOY_CONTEXTS)):
            if not (isinstance(source, str) and source):
                continue
            try:
                rx = re.compile(source)
            except re.error as exc:
                f.error("A5", "%s %s %r does not compile: %s" % (where, field, source, exc))
                rejected = True
                continue
            if all(rx.search(decoy) for decoy in decoys):
                f.error(
                    "A5",
                    "%s %s %r matches all %d unrelated decoys the gate tests it against, so it "
                    "exempts every numeral in the document while coverage still reports a perfect "
                    "score. Narrow it to the numerals it is actually about."
                    % (where, field, source, len(decoys)))
                rejected = True
                continue
            compiled[field] = rx
        if rejected or not compiled:
            continue
        out.append(Allowance(compiled.get("pattern"), compiled.get("context_pattern"),
                             pattern if has_pattern else context, why.strip()))
    return out


def report_split_numerals(text: str, sites: list, f: Findings) -> None:
    """Say which reading the gate took of every numeral a space separator split, or that it took
    none.

    The two readable kinds are WARNINGS: the gate read the number the reader reads, and the only
    thing wrong is the spelling. The ambiguous kind is an ERROR, because there the gate did not
    read the number at all. It cannot: "9 25.2 GB/s" is one figure to a merger and two to a reader,
    the difference is a factor of thirty-six, and a check that picked one of them and then verified
    its own pick would be doing the thing this file exists to stop. An unreadable numeral is not a
    numeral that passed.
    """
    seen: dict = {}
    for site in sites:
        key = (site["kind"], site["printed"], site["merged"])
        seen.setdefault(key, []).append(site["start"])
    for (kind, printed, merged), starts in sorted(seen.items()):
        where = " // ".join(context_of(text, start, start + len(printed))
                            for start in starts[:2])
        if kind == "thousands":
            f.warn(
                "A5",
                "the document prints %r with a space where a thousands separator belongs. This "
                "gate reads it as %s; a reader may read it as two numbers, and until one of them "
                "is chosen the gate and the reader are checking different figures. Print the "
                "comma." % (printed, merged),
                numeral=merged,
            )
        elif kind == "invisible":
            f.warn(
                "A5",
                "the document splits a numeral with a ZERO-WIDTH space: %r. A reader is shown no "
                "gap at all and reads %s, while an unprepared scanner reads two numbers. This "
                "gate reads what the reader reads; delete the character."
                % (printed, merged.replace(",", "")),
                numeral=merged,
            )
        else:
            f.error(
                "A5",
                "the document prints %r, where a space splits a numeral into groups the thousands "
                "convention does not explain. A merger reads %s and a reader reads %s, and NEITHER "
                "IS SAFE TO GUESS, so this gate checked no number here at all. Print the figure "
                "the sentence means, with a comma if it is one number. Context: %s"
                % (printed, merged.replace(",", ""),
                   " and ".join(re.split("[" + GROUP_SEPARATORS + "]", printed)), where),
                numeral=printed,
            )


def check_coverage(m: dict, rendered: Path | None, f: Findings) -> None:
    """A5 and A6: every measurement the DOCUMENT prints traces to a claim.

    This is the check with jurisdiction over what ships. The others read a manifest, and a manifest
    is written by the same generator that writes the prose, so a number the generator never
    declared is invisible to all of them. Five fabricated headline figures went into an abstract
    and the manifest did not change by one byte.

    Coverage is reported whether or not anything fires, and that matters as much as the finding
    list. A manifest declaring one claim and nothing else passed every other check and printed a
    cleaner summary than the honest 197-claim manifest, because asserting less scores better when
    the score counts only findings. Coverage is measured against the DOCUMENT, so shrinking the
    manifest lowers it.
    """
    coverage = m.get("coverage") if isinstance(m.get("coverage"), dict) else {}
    floor_unit = as_number(coverage.get("min_unit_bearing_pct"))
    floor_unit = 100.0 if floor_unit is None else floor_unit
    floor_bare = as_number(coverage.get("min_bare_numeral_pct"))
    bare_declared = floor_bare is not None
    # An undeclared floor used to be 0.0, which made A6's warn branch unreachable: the condition
    # `bare_pct < 0.0` cannot hold, so the branch behind it was dead code that read like a check.
    # A default floor that can actually be missed is the difference between a warning and a lie.
    floor_bare = DEFAULT_BARE_FLOOR_PCT if floor_bare is None else floor_bare
    allowances = compile_allowances(coverage, f)
    attached_exceptions = compile_allowances(coverage, f, key="attached_exceptions")
    f.coverage = {"unit_bearing": None, "unit_bearing_total": 0, "unit_bearing_covered": 0,
                  "bare": None, "bare_total": 0, "bare_covered": 0,
                  "min_unit_bearing_pct": floor_unit, "min_bare_numeral_pct": floor_bare,
                  "bare_floor_declared": bare_declared, "allowances": [], "covered_by": {},
                  "scope": "no rendered document was supplied, so no numeral was checked"}
    if not (rendered and rendered.exists()):
        return

    raw = visible_text(rendered.read_text(encoding="utf-8", errors="replace"))
    text, grouped = merge_space_groups(raw.replace("&nbsp;", " "))
    claims = numeric_claims(m)
    f.coverage["scope"] = "%d visible characters of the rendered document" % len(text)

    report_split_numerals(text, grouped, f)

    def allowed(token, numeral, start, end):
        window = text[max(0, start - 60):end + 60]
        offset = start - max(0, start - 60)
        for allowance in allowances:
            if allowance.exempts(token, numeral, window, offset):
                return True
        return False

    unit_hits = unit_bearing_numerals(text, attached_exceptions)
    uncovered: dict = {}
    covered = 0
    joined = 0
    covered_by: dict = {}
    for start, end, numeral, unit, was_joined in unit_hits:
        joined += 1 if was_joined else 0
        token = printed_token(numeral, unit)
        context = context_of(text, start, end)
        by = round_trips(numeral, unit, claims)
        if by is not None:
            covered += 1
            covered_by.setdefault(by, {}).setdefault(token, []).append(context)
        elif allowed(token, numeral, start, end):
            covered += 1
        else:
            uncovered.setdefault((numeral, unit), []).append(context)
    f.coverage["covered_by"] = {key: sorted(tokens) for key, tokens in covered_by.items()}
    f.coverage["allowances"] = [{"pattern": a.source, "exempted": a.hits, "why": a.why}
                                for a in allowances + attached_exceptions]

    # A5 matches a printed numeral against every claim VALUE in the same unit family, and never
    # asks whether that claim is the quantity the sentence names. With hundreds of claims in scope
    # some fabrications are covered by coincidence, and the signature of coincidence is one claim
    # standing behind printed numerals it cannot all be: a claim legitimately appears at two or
    # three roundings of itself, not at a dozen distinct values.
    for key, tokens in sorted(covered_by.items()):
        if len(tokens) >= COINCIDENCE_DISTINCT_NUMERALS:
            f.warn(
                "A5",
                "claim %s is the cover for %d distinct printed numerals (%s). One claim standing "
                "behind that many different figures is the signature of coincidental coverage "
                "rather than citation: A5 can only test the value, so the prose has to name the "
                "claim it means."
                % (key, len(tokens), ", ".join(sorted(tokens)[:8])),
                claim=key,
            )

    total = len(unit_hits)
    pct = 100.0 * covered / total if total else 100.0
    f.coverage.update({"unit_bearing": pct, "unit_bearing_total": total,
                       "unit_bearing_covered": covered, "markup_joined": joined})

    # A shortfall against the floor is an error; a shortfall the manifest deliberately allowed for
    # is a warning, so lowering the floor does not make the uncovered numerals disappear from the
    # output. Either way every one of them is named, because "coverage is 65%" is not actionable
    # and "these thirty numerals are not backed" is.
    severity = f.error if pct + 1e-9 < floor_unit else f.warn
    for (numeral, unit), contexts in uncovered.items():
        severity(
            "A5",
            "the document prints %s%s%s, which matches no claim value at that precision and no "
            "coverage.allow pattern. Context: %s"
            % (numeral, "" if unit in ("%",) or unit in CURRENCY_MARKS else " ",
               "" if unit in CURRENCY_MARKS else unit,
               " // ".join(contexts[:2]) + (" (+%d more)" % (len(contexts) - 2)
                                            if len(contexts) > 2 else "")),
            numeral=numeral, unit=unit, occurrences=len(contexts),
        )
    if pct + 1e-9 < floor_unit:
        f.error(
            "A5",
            "%d of %d unit-bearing numerals in the rendered document trace to a claim (%.1f%%), "
            "below the %.1f%% this manifest requires. Declare the missing ones as claims, or "
            "allow them in coverage.allow with a reason." % (covered, total, pct, floor_unit),
        )
    elif uncovered:
        f.warn(
            "A5",
            "%d unit-bearing numeral(s) trace to no claim. Coverage is %.1f%%, which clears the "
            "%.1f%% floor this manifest declares, so they are reported and not blocking."
            % (sum(len(v) for v in uncovered.values()), pct, floor_unit),
        )

    unit_starts = {hit[0] for hit in unit_hits}
    bare_total = bare_covered = 0
    bare_uncovered: dict = {}
    for match in ANY_NUMERAL.finditer(text):
        if match.start() in unit_starts:
            continue
        bare_total += 1
        numeral = match.group(1)
        context = context_of(text, match.start(), match.end())
        if (round_trips(numeral, "", claims) is not None
                or allowed(numeral, numeral, match.start(), match.end())):
            bare_covered += 1
        else:
            bare_uncovered.setdefault(numeral, []).append(context)
    bare_pct = 100.0 * bare_covered / bare_total if bare_total else 100.0
    f.coverage.update({"bare": bare_pct, "bare_total": bare_total, "bare_covered": bare_covered,
                       "bare_uncovered_distinct": len(bare_uncovered)})
    # An explicitly declared floor is a promise the author made, so breaking it blocks, at any
    # sample size. The default floor is a warning, and it used to be 0.0: the branch choosing
    # between error and warn sat behind `bare_pct < 0.0`, a condition no percentage can meet, so
    # it was unreachable code that reads in a diff exactly like a check.
    applies = bare_declared or bare_total >= DEFAULT_BARE_MIN_SAMPLE

    # EVERY UNCOVERED BARE NUMERAL IS NAMED, the way A5 names an uncovered unit-bearing one. It
    # used to report a percentage and five examples, and nothing else, so a fabricated figure whose
    # unit an English sentence spells out ("47314 tokens per second", "96 concurrent users") landed
    # in a pool of a thousand numerals carrying a hundred and fifty of legitimate slack and
    # produced NO FINDING AT ALL: the aggregate moved by a tenth of a point and no line of output
    # said which numeral it was. An aggregate is not a check on any individual number. These are
    # warnings and the floor is still what blocks, because a document legitimately prints counts,
    # dates and section numbers; the point is that an author can now see WHICH numerals are
    # unaccounted for instead of only how many. They are held to the same jurisdiction as the floor
    # itself: a percentage over a handful of numerals is not a measurement, so neither is a list of
    # them worth interrupting for, unless the manifest declared a floor and made it a promise.
    if applies and bare_uncovered:
        for numeral, contexts in bare_uncovered.items():
            f.warn(
                "A6",
                "the document prints the bare numeral %s, which matches no claim value at that "
                "precision and no coverage.allow pattern. It is unit-bearing to a reader if the "
                "sentence around it names the unit. Context: %s"
                % (numeral, " // ".join(contexts[:2])
                   + (" (+%d more)" % (len(contexts) - 2) if len(contexts) > 2 else "")),
                numeral=numeral, occurrences=len(contexts),
            )
    if applies and bare_pct + 1e-9 < floor_bare:
        report_at = f.error if bare_declared else f.warn
        report_at(
            "A6",
            "%d of %d bare numerals trace to a claim (%.1f%%), below the %.1f%% floor%s. Examples: %s"
            % (bare_covered, bare_total, bare_pct, floor_bare,
               " this manifest declares" if bare_declared else
               " that applies when a manifest declares none; declare "
               "coverage.min_bare_numeral_pct to hold this document to a floor of its own",
               "; ".join("%s in %r" % (k, v[0])
                         for k, v in list(bare_uncovered.items())[:5])),
        )


# A6's floor when the manifest declares none. Zero made the whole branch unreachable, which is a
# check that cannot fire dressed as a check that passed. Fifty is deliberately low: a document
# legitimately prints counts, dates and section numbers, so this is a "the manifest has stopped
# citing anything" alarm, not a coverage target.
DEFAULT_BARE_FLOOR_PCT = 50.0

# ...and the smallest sample it is applied to. A percentage over six numerals is not a coverage
# measurement, which is the argument D2 makes two hundred lines above about p95 over n=32, so it
# would be inconsistent to fire the default floor on a page carrying a handful of numbers. A floor
# the MANIFEST declares is a promise its author made and is held at any sample size.
DEFAULT_BARE_MIN_SAMPLE = 40

# How many DISTINCT printed numerals one claim may stand behind before that is evidence of
# coincidence rather than citation. A claim renders at two or three precisions of itself; a dozen
# means the value is common, not that the sentences are about that claim.
COINCIDENCE_DISTINCT_NUMERALS = 8

ACCEPTANCE_FIELDS = ("claim", "claims", "keys", "block", "table", "figure", "level", "run",
                     "numeral", "unit", "quantity", "label")

# An acceptance that names no identity field can only ever be "this whole check". One finding is
# the most such an entry may take, and an entry that names an identity may take a handful, for the
# case where a check legitimately reports the same defect on several rows of one table.
MAX_ACCEPTED_PER_ENTRY = 6


def apply_accepted_warnings(m: dict, f: Findings) -> None:
    """A warning can be waived in the manifest, with a reason, and is then printed separately.

    Why this exists. A build carrying 28 standing warnings is a build where a warning cannot be
    read, and that is precisely how a printed quantity with two different values landed as warning
    29 of 29 and shipped. The fix is not fewer checks; it is a live list that can honestly reach
    zero, with everything knowingly accepted still on the record beside it.

        m["accepted_warnings"] = [{"check": "D2", "claim": "ttft_p95_c8", "why": "..."}]

    `check` is required, `why` is required and must be non-empty, and errors are never
    suppressible whatever the manifest says.

    THE DOCSTRING USED TO SAY an acceptance "is narrow by construction: it cannot be written to
    swallow a whole check". It could. matches() only tests the fields an entry names, so
    {"check": "A5", "why": "..."} moved every A5 finding in the build to accepted and printed
    "0 error(s), 0 warning(s)": the strongest check in the file, disarmed by a two-key object.
    Two things now make the sentence true.

      * An entry that names no identity field (claim, level, table, figure, block, run, numeral,
        quantity, label) may accept exactly ONE finding. "This whole check" is not an acceptance.
      * Every entry has a cap. It is one by default, an entry may raise it to at most
        MAX_ACCEPTED_PER_ENTRY by declaring `accepts`, and an entry that matches more findings
        than its cap accepts NONE of them and says so. The count each entry took is printed
        beside the accepted list, so a broad acceptance is visible rather than inferred.
    """
    entries = m.get("accepted_warnings") or []
    if not isinstance(entries, list):
        f.error("manifest", "accepted_warnings must be a list of acceptance objects")
        return
    matchers = []
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict) or not entry.get("check"):
            f.error("manifest", "accepted_warnings[%d] names no check" % i)
            continue
        why = entry.get("why")
        if not (isinstance(why, str) and why.strip()):
            f.error("manifest", "accepted_warnings[%d] (check %s) records no reason. Accepting a "
                                "warning silently is deleting it." % (i, entry["check"]))
            continue
        named = [field for field in ACCEPTANCE_FIELDS if field in entry]
        cap = as_int(entry.get("accepts"))
        cap = 1 if cap is None else cap
        if cap < 1 or cap > MAX_ACCEPTED_PER_ENTRY:
            f.error("manifest", "accepted_warnings[%d] (check %s) declares accepts=%r. The cap is "
                                "between 1 and %d: an acceptance is a named exception, and one "
                                "that can take an unbounded number of findings is a disarmed "
                                "check." % (i, entry["check"], entry.get("accepts"),
                                            MAX_ACCEPTED_PER_ENTRY))
            continue
        if not named and cap > 1:
            f.error("manifest", "accepted_warnings[%d] names only the check %s and asks to accept "
                                "%d findings. An acceptance with no identity field can only ever "
                                "mean the whole check; name the claim, level, table, figure, "
                                "block or numeral it is about." % (i, entry["check"], cap))
            continue
        matchers.append([entry, 0, cap, i, bool(named)])

    def matches(entry, item):
        if entry["check"] != item.get("check"):
            return False
        for field in ACCEPTANCE_FIELDS:
            if field not in entry:
                continue
            want, got = entry[field], item.get(field)
            if isinstance(want, (list, tuple)) and isinstance(got, (list, tuple)):
                if set(map(str, want)) != set(map(str, got)):
                    return False
            elif str(want) != str(got):
                return False
        return True

    for matcher in matchers:
        entry, _hits, cap, index, named = matcher
        candidates = [item for item in f.items
                      if item["severity"] == "warn" and matches(entry, item)]
        if len(candidates) > cap:
            f.error(
                "manifest",
                "accepted_warnings[%d] matches %d live warnings of check %s and its cap is %d, so "
                "it accepts none of them. %s"
                % (index, len(candidates), entry["check"], cap,
                   "Name the finding it is about, one entry per finding."
                   if not named else
                   "Split it, or raise `accepts` to at most %d if these really are one accepted "
                   "defect." % MAX_ACCEPTED_PER_ENTRY),
            )
            continue
        for item in candidates:
            item["severity"] = "accepted"
            item["accepted_because"] = entry["why"].strip()
            item["accepted_by"] = index
            matcher[1] += 1

    for entry, hits, _cap, index, _named in matchers:
        if not hits:
            f.warn("manifest", "accepted_warnings[%d], for check %s, matches no warning in this "
                               "build. A stale acceptance is a claim about the report that is no "
                               "longer true; delete it." % (index, entry["check"]))
    f.acceptances = [{"index": index, "check": entry["check"], "accepted": hits, "cap": cap}
                     for entry, hits, cap, index, _named in matchers]


# --------------------------------------------------------------------------------------
# driver


def verify(manifest: dict, previous: dict | None = None, rendered: Path | None = None,
           manifest_dir: Path | None = None) -> Findings:
    f = Findings()
    check_manifest_shape(manifest, f, manifest_dir)
    if f.fatal:
        return f
    check_derivations(manifest, f)
    check_equalities(manifest, f)
    check_prose(manifest, f)
    check_provenance(manifest, previous, f)
    check_sampling(manifest, f)
    check_load_shape(manifest, f)
    check_roofs(manifest, f)
    check_gate(manifest, f, manifest_dir)
    check_render(manifest, rendered, f)
    check_coverage(manifest, rendered, f)
    # Last, so it can only ever move a warning this run actually produced.
    apply_accepted_warnings(manifest, f)
    return f


# Printing every finding of one check is right until a check has three hundred of them, at which
# point the console stops being readable and the errors underneath stop being seen. The full list
# always goes to the findings JSON.
MAX_PER_CHECK = 25


def coverage_line(f: Findings) -> str:
    """One line saying what the numeral checks had jurisdiction over, and the floor they held it to.

    Printed on a pass as well as a failure. "0 errors" over a manifest that asserts almost nothing
    reads exactly like "0 errors" over one that asserts everything, and this is the difference.
    """
    c = f.coverage or {}
    if c.get("unit_bearing") is None:
        return "  document coverage: %s" % c.get("scope", "not measured")
    return ("  document coverage: %d/%d unit-bearing numerals traced to a claim (%.1f%%, floor "
            "%.1f%%), %d/%d bare numerals (%.1f%%, floor %.1f%%), over %s"
            % (c["unit_bearing_covered"], c["unit_bearing_total"], c["unit_bearing"],
               c["min_unit_bearing_pct"], c["bare_covered"], c["bare_total"], c["bare"],
               c["min_bare_numeral_pct"], c["scope"]))


def report(f: Findings, stream=sys.stdout) -> None:
    order = {"error": 0, "warn": 1, "accepted": 2}
    shown: dict = {}
    suppressed: dict = {}
    for item in sorted(f.items, key=lambda i: (order.get(i["severity"], 3), i["check"])):
        if item["severity"] == "accepted":
            continue
        key = (item["severity"], item["check"])
        shown[key] = shown.get(key, 0) + 1
        if shown[key] > MAX_PER_CHECK:
            suppressed[key] = suppressed.get(key, 0) + 1
            continue
        mark = "ERROR" if item["severity"] == "error" else "warn "
        print(f"  [{mark}] {item['check']}  {item['message']}", file=stream)
    for (severity, check), count in sorted(suppressed.items()):
        print("  [%s] %s  ... and %d more of this check, in the findings JSON"
              % ("ERROR" if severity == "error" else "warn ", check, count), file=stream)
    if f.accepted:
        print(file=stream)
        print("  Accepted in the manifest, with a reason:", file=stream)
        for item in f.accepted:
            print("  [ok   ] %s  %s  (accepted: %s)"
                  % (item["check"], item["message"], item.get("accepted_because", "")), file=stream)
    # How much each acceptance and each allowance took. Both are holes in a check, and a hole
    # nobody can see the size of is a hole that grows: an acceptance that swallowed a whole check
    # and an allowance that exempted every numeral both used to print as a clean build.
    taken = [a for a in getattr(f, "acceptances", []) if a["accepted"]]
    if taken:
        print(file=stream)
        for a in taken:
            print("  [ok   ] accepted_warnings[%d] (%s) accepted %d finding(s), cap %d"
                  % (a["index"], a["check"], a["accepted"], a["cap"]), file=stream)
    # WHICH SOURCES WERE OPENED, AND WHICH WERE ONLY WELL-FORMED. A claim whose kind exempts it
    # from recomputation rests entirely on its source, so "the file was read" and "the string was
    # shaped like a URL" are two different verdicts, and printing neither made them look the same.
    sources = getattr(f, "sources", None) or []
    unresolved = [s for s in sources if not s.get("resolved")]
    if sources:
        print(file=stream)
        print("  Sources behind the %d claim(s) exempt from recomputation: %d resolved on disk, "
              "%d accepted unresolved" % (len(sources), len(sources) - len(unresolved),
                                          len(unresolved)), file=stream)
        for item in unresolved[:MAX_PER_CHECK]:
            print("  [ok   ] %s: %s" % (item.get("claim"), item.get("detail", "")), file=stream)
        if len(unresolved) > MAX_PER_CHECK:
            print("  [ok   ] ... and %d more accepted unresolved"
                  % (len(unresolved) - MAX_PER_CHECK), file=stream)
    allowances = (f.coverage or {}).get("allowances") or []
    if allowances:
        print(file=stream)
        print("  Coverage allowances, and how many numerals each removed from A5:", file=stream)
        for a in allowances:
            print("  [ok   ] %-44s exempted %d numeral(s)"
                  % (a["pattern"][:44], a["exempted"]), file=stream)
    covered_by = (f.coverage or {}).get("covered_by") or {}
    if covered_by:
        top = sorted(covered_by.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:5]
        print(file=stream)
        print("  Claims standing behind the most distinct printed numerals: "
              + ", ".join("%s x%d" % (key, len(tokens)) for key, tokens in top), file=stream)
    print(file=stream)
    print(coverage_line(f), file=stream)
    print(f"  {len(f.errors)} error(s), {len(f.warnings)} warning(s), "
          f"{len(f.accepted)} accepted", file=stream)


DEMO = {
    "schema": SCHEMA,
    "report": {"version": "8.6-demo"},  # D4: arrival model missing
    "runs": {"primary": {"started": "2026-08-25T11:01:00Z"},
             "tool": {"started": "2026-08-25T17:08:00Z"}},
    "claims": {
        "generation_length": {"value": 128, "unit": "count", "basis": "per_request",
                              "kind": "assumption", "label": "forced generation length"},
        "requests_per_second_c8": {"value": 1.82, "unit": "req/s", "basis": "total",
                                   "kind": "measured", "run": "primary",
                                   "measured_at": "2026-08-25T11:05:00Z"},
        "throughput_c8_capacity": {"value": 233, "unit": "tok/s", "basis": "total",
                                   "kind": "measured", "run": "primary",
                                   "measured_at": "2026-08-25T11:05:00Z",
                                   "label": "aggregate throughput at concurrency 8"},
        "throughput_c8_repro": {"value": 204.5, "unit": "tok/s", "basis": "total",
                                "kind": "measured", "run": "tool",
                                "measured_at": "2026-08-25T17:08:00Z",
                                "label": "aggregate throughput at concurrency 8"},
        "ttft_p95_c8": {"value": 1.93, "unit": "s", "basis": "per_request", "kind": "measured",
                        "run": "primary", "measured_at": "2026-08-25T11:05:00Z"},
        "ttft_p95_c16": {"value": 3.79, "unit": "s", "basis": "per_request", "kind": "measured",
                         "run": "primary", "measured_at": "2026-08-25T11:05:00Z"},
        "e2e_p95_c32": {"value": 11.50, "unit": "s", "basis": "per_request", "kind": "measured",
                        "run": "primary", "measured_at": "2026-08-25T11:05:00Z"},
        "pool_fixed_state": {"value": 72.0, "unit": "MiB", "basis": "per_sequence", "kind": "derived",
                             "formula": "72.0", "label": "fixed recurrent state per sequence"},
        "pool_kv_per_seq": {"value": 20.0, "unit": "MiB", "basis": "per_sequence", "kind": "derived",
                            "formula": "20.0"},
        "pool_parts_total": {"value": 92.0, "unit": "MiB", "basis": "per_sequence", "kind": "derived",
                             "formula": "pool_fixed_state + pool_kv_per_seq"},
        "pool_measured_per_seq": {"value": 99.8, "unit": "MiB", "basis": "per_sequence",
                                  "kind": "measured", "run": "primary",
                                  "measured_at": "2026-08-25T11:05:00Z"},
        "pool_residual_per_seq": {"value": 7.8, "unit": "MiB", "basis": "per_sequence", "kind": "derived",
                                  "formula": "pool_measured_per_seq - pool_parts_total"},
        "interconnect_ceiling_2048": {"value": 2777, "unit": "tok/s", "basis": "total", "kind": "derived",
                                      "formula": "2777"},
        "prefill_measured_2048": {"value": 2162, "unit": "tok/s", "basis": "total", "kind": "measured",
                                  "run": "primary", "measured_at": "2026-08-25T11:05:00Z"},
        # B1: printed as 82%, but 2162/2777 is 78%. A derived value that was typed.
        "prefill_fraction_of_ceiling_2048": {
            "value": 82.0, "unit": "%", "basis": "ratio", "kind": "derived",
            "formula": "100 * prefill_measured_2048 / interconnect_ceiling_2048", "tolerance": 0.005},
        # C1: a per-device state compared against a total-basis pool with no conversion.
        "pool_total": {"value": 14.44, "unit": "GiB", "basis": "total", "kind": "measured",
                       "run": "primary", "measured_at": "2026-08-25T11:05:00Z"},
        # The arithmetic closes and the answer is still wrong: a per-sequence cost measured
        # per device, divided by a total-basis pool. Only the basis check catches this.
        "seq_share_of_pool": {"value": 0.00675, "unit": "", "basis": "ratio", "kind": "derived",
                              "formula": "pool_measured_per_seq / pool_total / 1024"},
    },
    "equalities": [{"keys": ["throughput_c8_capacity", "throughput_c8_repro"], "tolerance": 0.005}],
    "tables": {
        "capacity_sweep": {"cells": ["throughput_c8_capacity", "throughput_c8_repro"]},
    },
    "prose": [
        {"id": "sec24_recommendation",
         "text": "Time to first token at p95 rises from 1.91 s to {{ttft_p95_c16}} at 16 concurrent.",
         "allow_literals": [16]},
        {"id": "sec17_closure",
         "text": "The residual is larger than the parts, which is the direction block allocation predicts.",
         "assert": {"op": "gt", "left": "pool_residual_per_seq", "right": "pool_parts_total"}},
    ],
    "percentiles": [
        {"key": "ttft_p95_c8", "q": 0.95, "n": 32, "level": "c8"},
        {"key": "e2e_p95_c32", "q": 0.95, "n": 32, "level": "c32"},
    ],
    "levels": [
        {"name": "c8", "concurrency": 8, "requests": 32, "duration_s": 17.6},
        {"name": "c32", "concurrency": 32, "requests": 32, "duration_s": 11.5,
         "e2e_p95_key": "e2e_p95_c32"},
        {"name": "c48", "concurrency": 48, "requests": 100, "duration_s": 30.0},
        # D6: an open-loop level as a harness actually writes one. Concurrency is null because
        # concurrency is an outcome here, which used to crash the verifier on int(None) and then,
        # once guarded, produced a D3 error demanding the very thing an open-loop level cannot
        # have. The defect it does carry is the real one: no rates and no queue trace.
        {"name": "r40", "concurrency": None, "requests": 20, "duration_s": 0.9,
         "arrival": {"model": "open_loop_poisson"}},
    ],
    "ceilings": [
        {"key": "interconnect_ceiling_2048", "mode": "shared"},
    ],
    "figures": [{"id": "fig8_concurrency", "table_view": True},
                {"id": "fig9_latency", "table_view": False}],
    # G3: the gate names a run that is not in the table, so nothing can be read back, and it
    # publishes no cases. A hardcoded passed=True is exactly what this pairing used to buy.
    "gate": {"ran_at": "2026-08-25T19:37:00Z", "passed": True, "cases_published": False,
             "window_run": "instrumentation"},
    # A9: a kind is a free choice of the generator, so "supplied" plus a phrase that sounds like
    # provenance used to exempt a claim from ever being recomputed.
    "coverage": {"min_unit_bearing_pct": 100.0},
}
DEMO["claims"]["decode_step_budget_ms"] = {
    "value": 3.0, "unit": "ms", "basis": "per_token", "kind": "supplied",
    "source": "engineering estimate",
    "label": "decode step budget",
}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("manifest", nargs="?", help="path to claims.json")
    ap.add_argument("--previous", help="the previous edition's claims.json, for staleness checks")
    ap.add_argument("--rendered", help="the rendered document, for escaping checks")
    ap.add_argument("--findings", help="write findings as JSON to this path")
    ap.add_argument("--warnings-as-errors", action="store_true")
    ap.add_argument("--demo", action="store_true", help="run against a fixture of real defects")
    args = ap.parse_args(argv)

    manifest_dir = None
    if args.demo:
        manifest, previous = DEMO, None
        print("verify_report.py --demo: a fixture carrying defects taken from real editions\n")
    else:
        if not args.manifest:
            ap.error("a manifest path is required unless --demo is given")
        try:
            manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"cannot read manifest: {exc}", file=sys.stderr)
            return 2
        # G3 resolves a run's declared artifact path relative to the manifest, so the artefact
        # travels with the manifest rather than with whatever directory the command was run from.
        manifest_dir = Path(args.manifest).resolve().parent
        previous = None
        if args.previous:
            try:
                previous = json.loads(Path(args.previous).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                print(f"cannot read previous manifest: {exc}", file=sys.stderr)
                return 2

    findings = verify(manifest, previous, Path(args.rendered) if args.rendered else None,
                      manifest_dir)
    report(findings)

    if args.findings:
        Path(args.findings).write_text(json.dumps(findings.items, indent=2), encoding="utf-8")

    failed = bool(findings.errors) or (args.warnings_as_errors and bool(findings.warnings))
    if failed:
        print("\n  Render blocked. Fix the errors, or re-measure. Never edit a measured value to "
              "satisfy a check.", file=sys.stderr)
    if findings.fatal:
        # Exit 2 is documented as "the manifest itself is broken", and it used to be reachable
        # only for JSON that would not parse: every other broken shape raised a traceback out of
        # a check before anything could classify it.
        return 2
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
