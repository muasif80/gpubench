"""The executable linter for a benchmark report built from a run bundle.

    python -m template.lint <run-dir> <built-report> [--rules L1,L3] [--explain]

Run it from the directory that contains this package (constraint B-C3). Exit codes::

    0   every selected rule passed and nothing was left unchecked
    1   no rule failed, but at least one part of a rule could not be checked
        (a SKIPPED finding names what was not looked at). --allow-skipped makes this 0.
    2   at least one rule failed
    3   the linter could not run at all (no bundle, unreadable report)

Exit 1 for "did not look" is deliberate. A linter that passes because it could not
find the authored text, or because the renderer emitted no provenance marks, is the
same defect class it exists to prevent: a claim that nothing is wrong, made without
looking. Every rule below reports what it could not check by name.

What the linter reads
--------------------
``<run-dir>`` is a directory holding the run bundle and, optionally, the authored text
and the allowlist:

    bundle.json            the run bundle (run-schema.json). Also accepted:
                           report-bundle.json, run-bundle.json, or the only *.json in
                           the directory that carries schema_version and runs.
    authored.json          {"<section_id>": "authored text with {{markers}} intact"}
                           or a directory authored/<section_id>.md. Optional, but its
                           absence makes several rules report SKIPPED, because the
                           marker vocabulary only exists before rendering.
    lint-allowlist.json    {"patterns":[{"id","regex","context","why"}],
                            "waiver_budget": 5}. Optional. Additive only.
    previous-bundle.json   the previous edition's bundle, for L10's value diff.

``<built-report>`` is the rendered HTML. The renderer is expected to leave provenance
marks in the output; they are how the linter tells a generated number from a typed one:

    <span data-value-id="ID">238.4 units/s</span>      a rendered envelope
    <span data-field="tool.version">2.1.0</span>       a rendered non-numeric bundle fact
    <section data-section="ID">                        a section boundary
    <a data-section-ref="ID">section 4 (Roofs)</a>     a resolved cross-reference
    <span data-fig-label="ID">Figure 2</span>          a renderer-assigned figure number
    <table data-figure="ID">                           a figure's table view
    <th data-axis-of="ID" data-axis-key="problem_size">an axis point of a series
    <td data-value-id="ID" data-point-at="1024">       one point of a series
    <g data-series-value-id="ID">                      a series actually drawn
    <table data-run-register="true"> <tr data-run-id>  the generated run register
    <span data-ver="version">1.1</span>                a version fact
    <span data-evidence="tool.source_url">             an evidence reference
    <span data-cmp="a,b"> / <span data-xcmp="a,b" data-why="..." data-differing-keys="..">
    <span data-waived-literal="570" data-why="...">    an L1 waiver
    <span data-kind-label="assumption">assumed</span>  a kind marking
    <span data-blend-disclosure="figure_id">           a cross-run blend disclosed in place

Text inside any of those marks is generated, so it is exempt from the orphan-literal
scan and from the document-reference scan. Text outside them is authored, and authored
text may not contain digits that restate a value. That asymmetry is the whole linter.

Template extensions to the schema (constraint B-C1: extend, never weaken)
------------------------------------------------------------------------
These envelope fields are read by the linter and carried through by the schema's open
object policy: ``rebuild_tolerance`` (L3), ``interpolation_basis`` (L3),
``mode_override_reason`` (L7), ``from_distribution`` (L4). They exist because the rules
need to express something the schema did not yet name; none of them relaxes a rule.
"""

from __future__ import annotations

import argparse
import ast
import difflib
import json
import math
import os
import random
import re
import sys
from html.parser import HTMLParser

try:  # pragma: no cover - import shim so the module also runs as a loose file
    from . import outline as outline_reader
except ImportError:  # pragma: no cover
    import outline as outline_reader  # type: ignore

SEV_ERROR = "error"
SEV_SKIPPED = "skipped"

EXIT_OK = 0
EXIT_SKIPPED = 1
EXIT_VIOLATION = 2
EXIT_CANNOT_RUN = 3

# --------------------------------------------------------------------------------------
# Rule metadata. Every rule cites the historical defect it prevents, by number, so that
# a maintainer who thinks a rule is pedantry reads the real bug before deleting it.
# --------------------------------------------------------------------------------------

RULE_META = {
    "L1": {
        "slug": "no-orphan-literals",
        "statement": "No numeric literal may appear in authored text if it matches a value in the "
        "data model to within the drift band, unless it is an explicit interpolation of that "
        "value's id or it matches a pattern on the published allowlist.",
        "cites": "D1, D2, D3, and D4/D8 in part",
        "rationale": "Five of the twelve defects were a digit typed into a sentence beside a table "
        "holding the same quantity. D1 printed one rate as 82 in a table, 80 in prose and 83 in a "
        "recommendation. D2 printed a cap as 201 in one section and 399 of 401 in another, because "
        "prose counted one unit and the data held two. D3 printed 570 against 566 in the data. None "
        "of those sentences was wrong when it was typed; each became wrong when the run behind it "
        "was replaced, the aggregation scope changed, or the sizes were controlled.",
    },
    "L2": {
        "slug": "every-value-has-a-kind",
        "statement": "Every rendered number resolves to an envelope, and that envelope's kind is one "
        "of the closed enum values, with the fields that kind requires present.",
        "cites": "D8",
        "rationale": "A workload size mixture was printed beside measured throughput in the same "
        "typeface with no marking. It was assumed, and three review rounds read it as measured. A "
        "closed kind enum with per-kind obligations means a number cannot enter the document without "
        "declaring what it is.",
    },
    "L3": {
        "slug": "derived-is-rebuildable",
        "statement": "Every derived value recomputes from its declared inputs, by its printed "
        "formula, to within its stated precision, and every input is printed somewhere in the report.",
        "cites": "D5, D11",
        "rationale": "Two interconnect ceilings could not be reproduced from the curve printed two "
        "pages earlier: the derivation interpolated latency log-linearly where latency is linear in "
        "message size, and it lived in a script beside the report, so there was nothing to review and "
        "nothing to test. D11 is the same rule from the other side: a decomposition carried one "
        "machine's geometry as hardcoded constants and produced a confident wrong answer elsewhere.",
    },
    "L4": {
        "slug": "assumptions-stay-labelled",
        "statement": "A value with kind assumption or projection carries its label at every "
        "appearance, not just at the first, and not only in the section that introduces it.",
        "cites": "D8",
        "rationale": "The assumed mixture was labelled once, in the methodology, then quoted three "
        "more times bare, including in the recommendation a reader would act on. A number is cited "
        "from wherever it is read, not from wherever it was defined.",
    },
    "L5": {
        "slug": "cross-references-resolve",
        "statement": "Every reference resolves to a section, figure, table or value id that exists, "
        "and no section, figure or table number appears in authored text.",
        "cites": "D6",
        "rationale": "Sections were reordered between editions and 'see section 18' now pointed at "
        "something else. The most mechanical of the twelve defects still survived three review "
        "rounds, because verifying a reference means leaving the sentence, finding the target and "
        "coming back, for every reference in a fifty-page document.",
    },
    "L6": {
        "slug": "figures-carry-tables",
        "statement": "Every figure has a table view, and every value in that table is an envelope "
        "reference.",
        "cites": "D5, D1",
        "rationale": "The unreproducible ceilings were derived from a curve published as a picture. "
        "Nobody could check the derivation because nobody could read the numbers off the chart. A "
        "chart whose numbers exist only inside the chart is not evidence, it is an illustration of "
        "evidence held elsewhere.",
    },
    "L7": {
        "slug": "run-provenance",
        "statement": "Every value's run_id exists in runs[], and where more than one run contributes "
        "to the report, document control names all of them.",
        "cites": "D10, D3",
        "rationale": "The cover said the report came from a single run directory while three run "
        "artefacts contributed. Provenance stated once on a cover page is a sentence someone "
        "remembered to write; provenance carried by every value is structural.",
    },
    "L8": {
        "slug": "comparison-hygiene",
        "statement": "Any two values compared in authored text share matching comparability "
        "conditions, or the comparison is annotated as cross-condition, in place.",
        "cites": "D4",
        "rationale": "A burst figure of 238.4 was set beside 237.6 as though the two were like for "
        "like. The second had been measured at a different problem size in a different run. The "
        "sentence read perfectly, and it was the basis of a conclusion about stability.",
    },
    "L9": {
        "slug": "claims-need-evidence",
        "statement": "Words that assert external verification require a resolvable artifact reference "
        "at the point of the claim.",
        "cites": "D9, D12",
        "rationale": "One section said the harness and raw data were published; another said nobody "
        "outside had run it. Both were written by the same author about the same artefact. "
        "Reproducibility status is a property of the world, so it is read from one field and "
        "rendered, never asserted per section.",
    },
    "L10": {
        "slug": "version-history-complete",
        "statement": "A row exists for the version being built, the ordering is monotonic, and there "
        "are no gaps in the chain.",
        "cites": "D7",
        "rationale": "A published version history was missing two of its own editions and listed the "
        "rest out of order. The history is what a reader uses to decide whether a number they cited "
        "last quarter still holds, so a hole in it silently invalidates that use.",
    },
    "L11": {
        "slug": "gates-measured-not-argued",
        "statement": "A claim about a gate must rest on readings, not on reasoning. If the workload "
        "claims cache defeat, counter readings must exist. If a quality gate is claimed, its cases "
        "must be published.",
        "cites": "D12, D9",
        "rationale": "A quality gate was reported as '10 of 10, PASS' with its cases unpublished, so "
        "the claim was unfalsifiable: a reader could neither reproduce it nor object to it. The cache "
        "contradiction had the same root, sections reasoning their way to an answer nobody had read "
        "off a counter.",
    },
}

RULE_ORDER = ["L1", "L2", "L3", "L4", "L5", "L6", "L7", "L8", "L9", "L10", "L11"]

# --------------------------------------------------------------------------------------
# L1 machinery
# --------------------------------------------------------------------------------------

DRIFT_BAND_FRACTION = 0.05  # deliberately wide: D1's 80 sits 2.4% from 82

L1_FALSE_POSITIVE_MODES = [
    "The drift band is the larger of five per cent and two units in the last place, so small "
    "integers in prose collide with unrelated counts. Cost: one allowlist pattern, once.",
    "Magnitude-prefix projections mean 566 W also projects as 0.566 and 566000, so an unrelated "
    "number of that size collides.",
    "Ratio and percent projections mean one envelope offers both 0.82 and 82, doubling the "
    "surface a literal can land on.",
    "Every point of a series is a projection, so any number near a swept axis value collides.",
    "Numbers spelled as words are extracted, so idiomatic 'one' and 'two' are candidates; "
    "allowlist class A9 is narrow by design and never suppresses an actual collision.",
    "Table detection is tag-based. A table laid out with divs is scanned as prose, and its cells "
    "will collide with the values they render.",
    "A figure quoted from an external source without a citation span looks like a collision. The "
    "fix is to make it a published envelope, not to widen the band.",
    "An identifier that contains a digit separated by whitespace (a part number written with a "
    "space) is not recognised as an identifier token and is scanned as a quantity.",
]

# The allowlist is an explicit list of patterns with a context restriction, never a
# tolerance and never a per-section opt-out. Widening it widens it by one named shape.
L1_ALLOWLIST = [
    {
        "id": "A1",
        "regex": r"^(19|20)\d{2}$",
        "context": "any",
        "why": "A calendar year is not a measurement of the system under test. No value denotes it, "
        "so it cannot drift from one.",
    },
    {
        "id": "A2",
        "regex": r"^\d{1,3}$",
        "context": "document_control_count",
        "why": "Counts of this document's own parts, generated by the renderer inside the document "
        "control block. L5 and L10 already own them. Note the context restriction: this is not "
        "'small integers anywhere'.",
    },
    {
        "id": "A3",
        "regex": r"^.+$",
        "context": "citation",
        "why": "A cited figure belongs to its source and must not be silently updated to match this "
        "run. If it is being compared, it is a published envelope and A3 does not apply.",
    },
    {
        "id": "A4",
        "regex": r"^v?\d+(\.\d+){1,2}$",
        "context": "version_span",
        "why": "Identifiers, not quantities. Governed by L10.",
    },
    {
        "id": "A5",
        "regex": r"^\d+(\.\d+)?$",
        "context": "definition_form",
        "why": "The prose is the definition site, so there is no upstream copy to drift from. The "
        "bundle cross-check keeps this from becoming a loophole: the same number must exist as a "
        "preregistered bar threshold or an assumption envelope.",
    },
    {
        "id": "A6",
        "regex": r"^\d{1,2}$",
        "context": "ordinal",
        "why": "Ordinals of the text, not of the measurement: list ordinals and step numbers in a "
        "numbered procedure.",
    },
    {
        "id": "A6b",
        "regex": r"^\d{1,3}(\.\d{1,3})?$",
        "context": "heading_ordinal",
        "why": "A renderer-assigned heading number at the head of a heading. Extension of A6 for "
        "reports whose renderer numbers its own sections; the number is generated, and L5 forbids "
        "any reference to it from prose.",
    },
    {
        "id": "A7",
        "regex": r"^\d+$",
        "context": "identifier_token",
        "why": "Part of a name. A name is a string that happens to contain digits. Restricted to "
        "token position inside an identifier, never a standalone number.",
    },
    {
        "id": "A8",
        "regex": r"^.+$",
        "context": "formula_or_code",
        "why": "The formula is published so a reader can execute it. Its constants are the "
        "derivation, and L3 checks them against the recomputation.",
    },
    {
        "id": "A9",
        "regex": r"^(1|2|one|two)$",
        "context": "trivial_cardinal",
        "why": "Narrow on purpose. 'No candidate projection collides' is read against the RELATIVE "
        "band (five per cent), not against the two-units-in-the-last-place floor, because for a "
        "bare integer that floor is 2 and would collide with every small number in the bundle. So "
        "'two units' is exempt while '2 W' is not (adjacency to a unit removes the exemption) and "
        "an exact match such as a per-unit count of 2 is not exempt either.",
    },
]

L1_WAIVER_BUDGET = 5

AGGREGATE_QUANTIFIERS = re.compile(
    r"\b(every one of|every|all of|all|each of the|both|combined|total|totalling|aggregate|"
    r"across (?:the )?(?:units|devices)|in total)\b",
    re.I,
)

# --------------------------------------------------------------------------------------
# Other rule vocabularies
# --------------------------------------------------------------------------------------

KIND_ENUM = (
    "measured",
    "derived",
    "assumption",
    "projection",
    "supplied",
    "published",
    "enumerated",
    "fixed-test-set",
)

KIND_OBLIGATIONS = {
    "measured": ["conditions", "n"],
    "derived": ["inputs", "formula", "conditions"],
    "projection": ["inputs", "formula", "conditions", "rationale"],
    "assumption": ["rationale"],
    "published": ["provenance"],
    "supplied": ["supplied_by"],
    "enumerated": ["why_not_measured"],
    "fixed-test-set": ["cases", "licenses", "does_not_license"],
}

COMPARABILITY_KEYS = (
    "problem_size",
    "concurrency",
    "batch_size",
    "precision",
    "duration_class",
    "service_level",
    "percentile",
    "mode",
    "parallelism",
    "workload_id",
    "power_state",
    "unit_index",
    "work_unit_count",
)

COMPARATIVE_LANGUAGE = re.compile(
    r"\b(against|versus|vs\.?|compared with|compared to|higher than|lower than|faster than|"
    r"slower than|greater than|less than|the same as|identical to|within|unchanged from|"
    r"matches|beside|relative to)\b",
    re.I,
)

CLAIM_WORDS = (
    "published",
    "open source",
    "open-source",
    "available",
    "obtainable",
    "reproduced",
    "reproducible",
    "independently",
    "verified",
    "validated",
    "audited",
    "confirmed",
    "third party",
    "third-party",
    "peer reviewed",
    "peer-reviewed",
    "certified",
    "replicated",
    "attested",
)

NEGATION = re.compile(
    r"\b(not|no|nobody|none|never|cannot|can't|without|lacks|absent|unpublished|"
    r"un(?:available|obtainable|verified|reproducible))\b",
    re.I,
)

PUBLICATION_CLAIM_WORDS = ("published", "open source", "open-source", "obtainable", "available")

CACHE_CLAIM = re.compile(
    r"\b(cache|cached|caching|cache-defeat|cache defeat|served from (?:a )?cache|"
    r"reuse of (?:a )?prior|unique (?:by construction|inputs|input)|non-repeating)\b",
    re.I,
)

STABILITY_CLAIM = re.compile(
    r"\b(stable across repeats|repeat[- ]stable|deterministic|reproducible across repeats|"
    r"stable under repetition)\b",
    re.I,
)

PRESET_BAR_CLAIM = re.compile(
    r"\b(pre-?set|pre-?registered|fixed in advance|declared before|set before)\b", re.I
)

DOC_REFERENCE = re.compile(
    r"\b(section|sections|figure|figures|table|tables|page|pages)\s+(\d+)|"
    r"\bappendix\s+([A-Z])\b",
    re.I,
)

SINGLE_RUN_ASSERTION = re.compile(
    r"\b(a single run|one run|single run directory|a single directory|one directory|"
    r"a single machine state|one machine state|a single pass|one measurement pass)\b",
    re.I,
)

TYPED_VERSION = re.compile(r"\bv?\d+\.\d+(?:\.\d+)?\b")

# Numeric constants a formula may contain without promoting them to envelopes. An
# explicit, published list, not a tolerance: these are scale factors and identities, and
# none of them can carry a machine's geometry (which is what D11 was).
L3_SCALE_CONSTANTS = {0.0, 1.0, 2.0, 100.0, 1000.0, 1024.0, 60.0, 3600.0, 1e6, 1e9}

BASIS_TO_SCALE = {
    "linear": {"linear", "categorical"},
    "log-linear": {"log", "log2", "log10"},
    "log_linear": {"log", "log2", "log10"},
    "loglinear": {"log", "log2", "log10"},
    "log-log": {"log", "log2", "log10"},
    "none": {"linear", "log", "log2", "log10", "categorical"},
}

# --------------------------------------------------------------------------------------
# Findings
# --------------------------------------------------------------------------------------


class Finding:
    __slots__ = ("rule", "severity", "location", "message", "fix")

    def __init__(self, rule: str, severity: str, location: str, message: str, fix: str) -> None:
        self.rule = rule
        self.severity = severity
        self.location = location
        self.message = message
        self.fix = fix

    def as_dict(self) -> dict:
        return {
            "rule": self.rule,
            "rule_slug": RULE_META[self.rule]["slug"] if self.rule in RULE_META else "",
            "severity": self.severity,
            "location": self.location,
            "message": self.message,
            "fix": self.fix,
        }

    def render(self) -> str:
        slug = RULE_META[self.rule]["slug"] if self.rule in RULE_META else ""
        head = "%s %s [%s]" % (self.rule, slug, self.severity)
        return "%s\n  where: %s\n  what:  %s\n  fix:   %s" % (
            head,
            self.location,
            self.message,
            self.fix,
        )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "Finding(%s,%s,%s)" % (self.rule, self.severity, self.location)


def err(rule, location, message, fix):
    return Finding(rule, SEV_ERROR, location, message, fix)


def skipped(rule, what_was_not_checked, why, how_to_enable):
    return Finding(
        rule,
        SEV_SKIPPED,
        what_was_not_checked,
        "NOT CHECKED: " + why,
        how_to_enable,
    )


class LinterError(Exception):
    """The linter could not run at all."""


# --------------------------------------------------------------------------------------
# Bundle
# --------------------------------------------------------------------------------------

BUNDLE_REQUIRED_KEYS = (
    "schema_version",
    "runs",
    "cross_run_blends",
    "system_under_test",
    "software_stack",
    "workload",
    "roofs",
    "measurements",
    "derived",
    "assumptions",
    "figures",
    "version_history",
)

BUNDLE_CANDIDATES = ("bundle.json", "report-bundle.json", "run-bundle.json")


def find_bundle(run_dir: str) -> str:
    if not os.path.isdir(run_dir):
        raise LinterError("run directory does not exist: %s" % run_dir)
    for name in BUNDLE_CANDIDATES:
        path = os.path.join(run_dir, name)
        if os.path.isfile(path):
            return path
    hits = []
    for name in sorted(os.listdir(run_dir)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(run_dir, name)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            continue
        if isinstance(data, dict) and "schema_version" in data and "runs" in data:
            hits.append(path)
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise LinterError(
            "no run bundle in %s. Expected one of %s, or a single *.json carrying "
            "schema_version and runs. A directory of raw probe output is not a bundle: build one "
            "that validates against run-schema.json first." % (run_dir, ", ".join(BUNDLE_CANDIDATES))
        )
    raise LinterError(
        "more than one candidate bundle in %s: %s. Name the one to lint bundle.json."
        % (run_dir, ", ".join(os.path.basename(h) for h in hits))
    )


class Bundle:
    def __init__(self, data: dict, path: str) -> None:
        self.data = data
        self.path = path
        missing = [k for k in BUNDLE_REQUIRED_KEYS if k not in data]
        if missing:
            raise LinterError(
                "bundle %s is missing required top-level keys: %s. run-schema.json requires all of "
                "them; a bundle that omits one cannot be linted because the rules would have "
                "nowhere to look." % (path, ", ".join(missing))
            )
        self.envelopes: dict = {}
        self.envelope_group: dict = {}
        for group in ("roofs", "measurements", "derived", "assumptions"):
            for env in data.get(group) or []:
                eid = env.get("id")
                if eid is None:
                    continue
                if eid not in self.envelopes:
                    self.envelopes[eid] = env
                    self.envelope_group[eid] = group
                else:
                    self.envelopes.setdefault(eid, env)
        self.figures = {f.get("id"): f for f in (data.get("figures") or []) if f.get("id")}
        self.runs = {r.get("id"): r for r in (data.get("runs") or []) if r.get("id")}
        self.sections = {s.get("id"): s for s in (data.get("sections") or []) if s.get("id")}

    # -- ids ---------------------------------------------------------------------------
    def duplicate_ids(self) -> list:
        seen = {}
        dups = []
        for group in ("runs", "roofs", "measurements", "derived", "assumptions", "figures"):
            for item in self.data.get(group) or []:
                iid = item.get("id")
                if iid is None:
                    continue
                if iid in seen:
                    dups.append((iid, seen[iid], group))
                else:
                    seen[iid] = group
        return dups

    @property
    def primary_run(self):
        for run in self.data.get("runs") or []:
            if run.get("primary") is True:
                return run
        return None

    def run_of(self, env) -> dict | None:
        return self.runs.get(env.get("run_id"))

    def mode_of(self, env) -> str | None:
        if env.get("mode"):
            return env["mode"]
        run = self.run_of(env)
        return (run or {}).get("mode")

    def label_of(self, eid) -> str:
        env = self.envelopes.get(eid) or {}
        return env.get("label") or eid

    def conditions_of(self, env) -> dict:
        cond = dict(env.get("conditions") or {})
        mode = self.mode_of(env)
        if mode and "mode" not in cond:
            cond["mode"] = mode
        return cond

    def input_closure(self, eid, seen=None) -> set:
        seen = seen if seen is not None else set()
        env = self.envelopes.get(eid)
        if env is None:
            return seen
        for dep in env.get("inputs") or []:
            if dep in seen:
                continue
            seen.add(dep)
            self.input_closure(dep, seen)
        return seen

    def units_vocabulary(self) -> set:
        out = set()
        for env in self.envelopes.values():
            unit = env.get("unit")
            if not isinstance(unit, str):
                continue
            out.add(unit.strip())
            for piece in re.split(r"[/\s]+|\bper\b", unit):
                piece = piece.strip()
                if piece:
                    out.add(piece)
        out.update({"%", "percent", "W", "s", "ms", "B", "GB", "MB", "count", "ratio", "fraction"})
        return {u for u in out if u}


def resolve_bundle_path(bundle: Bundle, path: str):
    """Resolve a dotted bundle path. List steps accept an index or an entry id.

    'runs.run_final.harness_version', 'version_history.0.date', 'tool.source_url'.
    Returns (found, value).
    """
    node = bundle.data
    for step in path.split("."):
        if isinstance(node, dict):
            if step in node:
                node = node[step]
                continue
            return False, None
        if isinstance(node, list):
            if re.fullmatch(r"\d+", step):
                idx = int(step)
                if idx >= len(node):
                    return False, None
                node = node[idx]
                continue
            hit = None
            for item in node:
                if isinstance(item, dict) and item.get("id") == step:
                    hit = item
                    break
            if hit is None:
                return False, None
            node = hit
            continue
        return False, None
    return True, node


# --------------------------------------------------------------------------------------
# Number handling
# --------------------------------------------------------------------------------------

NUMBER_RE = re.compile(
    r"(?<![\w.$-])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)(?![\w])"
)

_WORD_ONES = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
}
_WORD_TENS = {
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}
_WORD_SCALES = {"hundred": 100, "thousand": 1000, "million": 1000000, "billion": 1000000000}
_WORD_TOKENS = set(_WORD_ONES) | set(_WORD_TENS) | set(_WORD_SCALES) | {"and"}
_WORD_RUN_RE = re.compile(
    r"\b((?:%s)(?:[\s-]+(?:%s))*)\b" % ("|".join(sorted(_WORD_TOKENS)), "|".join(sorted(_WORD_TOKENS))),
    re.I,
)


def parse_word_number(phrase: str):
    """Turn 'two hundred and one' into 201. Returns None when the run is not a number."""
    tokens = [t for t in re.split(r"[\s-]+", phrase.lower()) if t]
    if not tokens or all(t == "and" for t in tokens):
        return None
    total = 0
    current = 0
    saw_digit_word = False
    for tok in tokens:
        if tok == "and":
            continue
        if tok in _WORD_ONES:
            current += _WORD_ONES[tok]
            saw_digit_word = True
        elif tok in _WORD_TENS:
            current += _WORD_TENS[tok]
            saw_digit_word = True
        elif tok in _WORD_SCALES:
            scale = _WORD_SCALES[tok]
            if scale == 100:
                current = (current or 1) * 100
            else:
                total += (current or 1) * scale
                current = 0
            saw_digit_word = True
        else:
            return None
    if not saw_digit_word:
        return None
    return total + current


def literal_decimals(text: str) -> int:
    if "." in text:
        return len(text.split(".", 1)[1])
    return 0


def drift_band(literal_text: str, projection_value: float) -> float:
    ulp2 = 2.0 * (10.0 ** -literal_decimals(literal_text))
    return max(abs(float(projection_value)) * DRIFT_BAND_FRACTION, ulp2)


def as_float(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if re.fullmatch(r"[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?", text):
            return float(text)
    return None


def fmt(value) -> str:
    if isinstance(value, float):
        if value == int(value) and abs(value) < 1e15:
            return str(int(value))
        return ("%.6g" % value)
    return str(value)


MAGNITUDE_FACTORS = (
    ("kilo", 1e3),
    ("milli", 1e-3),
    ("mega", 1e6),
    ("micro", 1e-6),
    ("giga", 1e9),
    ("nano", 1e-9),
    ("binary kilo", 1024.0),
    ("binary kilo down", 1.0 / 1024.0),
    ("binary mega", 1024.0 ** 2),
    ("binary mega down", 1.0 / 1024.0 ** 2),
    ("binary giga", 1024.0 ** 3),
    ("binary giga down", 1.0 / 1024.0 ** 3),
)


NON_PREFIXABLE_UNITS = {
    "",
    "1",
    "count",
    "counts",
    "ratio",
    "fraction",
    "percent",
    "per cent",
    "%",
    "cases",
    "samples",
    "requests",
    "items",
    "runs",
    "units",
}


def _prefixable(unit) -> bool:
    """Whether a magnitude prefix is a legitimate rendering of this unit.

    The rule says a value projects "under standard magnitude prefixes FOR ITS UNIT". A count, a
    ratio and a percentage have no prefixed forms, and pretending they do is what makes 0.4 in a
    sentence collide with a sample count of 401. Keeping the projection set honest is cheaper than
    an allowlist entry per false positive.
    """
    return (unit or "").strip().lower() not in NON_PREFIXABLE_UNITS


def projections_for(bundle: Bundle, env: dict):
    """Every renderable projection of one envelope, as dicts.

    scope is what the projection covers, so a literal that matches a per-unit
    projection while the sentence quantifies the aggregate is detectable (D2).
    """
    out = []
    eid = env.get("id")
    unit = (env.get("unit") or "").strip().lower()
    precision = env.get("precision")
    precision = precision if isinstance(precision, int) else None

    def add(value, how, scope):
        num = as_float(value)
        if num is None:
            return
        out.append({"id": eid, "value": num, "how": how, "scope": scope, "env": env})

    def add_scalar(value, base_how, scope, dimensionless=False):
        num = as_float(value)
        if num is None:
            return
        add(num, base_how, scope)
        if precision is not None:
            for places in range(0, precision + 1):
                rounded = round(num, places)
                if rounded != num:
                    add(rounded, "%s rounded to %d places" % (base_how, places), scope)
        if dimensionless or unit in ("ratio", "fraction", "1"):
            add(num * 100.0, "%s as a percentage" % base_how, scope)
        if unit in ("percent", "%", "per cent"):
            add(num / 100.0, "%s as a fraction" % base_how, scope)
        if _prefixable(unit) and not dimensionless:
            for name, factor in MAGNITUDE_FACTORS:
                add(num * factor, "%s under a %s prefix" % (base_how, name), scope)

    value = env.get("value")
    if isinstance(value, dict) and "points" in value:
        for point in value.get("points") or []:
            add_scalar(point.get("value"), "series point at %s" % json.dumps(point.get("at") or {}), "point")
            if point.get("n") is not None:
                add(point.get("n"), "sample count of a series point", "sample_count")
            spread = point.get("spread") or {}
            if isinstance(spread.get("value"), (int, float)):
                add_scalar(
                    spread["value"],
                    "spread of a series point",
                    "spread",
                    dimensionless=spread.get("type") in ("cov",),
                )
    elif isinstance(value, dict) and "min" in value and "max" in value:
        add_scalar(value.get("min"), "range minimum", "range_end")
        add_scalar(value.get("max"), "range maximum", "range_end")
        across = value.get("across") or {}
        for key in ("from", "to"):
            add_scalar(across.get(key), "range axis %s" % key, "range_axis")
        for item in across.get("values") or []:
            add_scalar(item, "range axis point", "range_axis")
        for key in ("min_at", "max_at"):
            add_scalar(value.get(key), "condition at the range %s" % key, "range_axis")
    else:
        add_scalar(value, "the value", "aggregate")

    if env.get("n") is not None:
        add(env.get("n"), "sample count n", "sample_count")
    spread = env.get("spread") or {}
    if isinstance(spread.get("value"), (int, float)):
        add_scalar(
            spread["value"],
            "spread (%s)" % spread.get("type"),
            "spread",
            dimensionless=spread.get("type") in ("cov",),
        )
    agg = env.get("aggregation") or {}
    if agg.get("unit_count") is not None:
        add(agg["unit_count"], "aggregation unit_count", "unit_count")
    for entry in agg.get("per_unit") or []:
        add_scalar(
            entry.get("value"),
            "per-unit value for unit %s" % entry.get("unit_index"),
            "per_unit",
        )
        if entry.get("n") is not None:
            add(entry["n"], "per-unit sample count for unit %s" % entry.get("unit_index"), "per_unit")
    cases = env.get("cases")
    if isinstance(cases, list) and cases:
        add(len(cases), "published case count", "case_count")
        add(sum(1 for c in cases if c.get("passed") is True), "passing case count", "case_count")
    bar = env.get("preregistered_bar") or {}
    if bar.get("threshold") is not None:
        add_scalar(bar["threshold"], "preregistered bar threshold", "threshold")
    prov = env.get("provenance") or {}
    if prov.get("headline_value") is not None:
        add_scalar(prov["headline_value"], "published headline value before unpacking", "aggregate")
    return out


def all_projections(bundle: Bundle):
    out = []
    for env in bundle.envelopes.values():
        out.extend(projections_for(bundle, env))
    return out


# --------------------------------------------------------------------------------------
# Rendered report
# --------------------------------------------------------------------------------------

PROSE_SKIP_TAGS = {
    "script",
    "style",
    "svg",
    "table",
    "pre",
    "code",
    "head",
    "title",
    "noscript",
    "template",
    "textarea",
}

BLOCK_TAGS = {
    "p",
    "li",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "dt",
    "dd",
    "figcaption",
    "summary",
    "caption",
    "blockquote",
    "div",
    "section",
    "td",
    "th",
    "aside",
    "footer",
    "header",
}

VOID_TAGS = {
    "br",
    "img",
    "hr",
    "meta",
    "link",
    "input",
    "area",
    "base",
    "col",
    "embed",
    "source",
    "track",
    "wbr",
}

INTERP_ATTRS = (
    "data-value-id",
    "data-field",
    "data-section-ref",
    "data-fig-ref",
    "data-fig-label",
    "data-tbl-ref",
    "data-run-ref",
    "data-run-id",
    "data-ver",
    "data-evidence",
    "data-cmp",
    "data-xcmp",
    "data-waived-literal",
    "data-axis-of",
    "data-kind-label",
    "data-blend-disclosure",
    "data-case-id",
    "data-verbatim",
)

MARK_ATTRS = INTERP_ATTRS + ("data-figure", "data-series-value-id", "data-run-register", "data-block")


class _ReportParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list = []
        self._prose: list = []
        self.prose_len = 0
        self.segments: list = []
        self.value_spans: list = []
        self.marks: list = []
        self.tables: list = []
        self.svg_series: list = []
        self.section_order: list = []
        self.blocks: list = []
        self.interp_intervals: list = []
        self.citation_intervals: list = []
        self.version_intervals: list = []
        self.verbatim_intervals: list = []
        self._full_text: list = []
        self._table_stack: list = []

    # -- helpers -----------------------------------------------------------------------
    def _cur(self, key, default=None):
        for frame in reversed(self.stack):
            if frame.get(key) is not None:
                return frame[key]
        return default

    def _in_skip(self) -> bool:
        return any(frame["tag"] in PROSE_SKIP_TAGS for frame in self.stack)

    def _in_svg(self) -> bool:
        return any(frame["tag"] == "svg" for frame in self.stack)

    def _classes(self):
        out = set()
        for frame in self.stack:
            out |= frame["classes"]
        return out

    def _append_prose(self, text: str):
        start = self.prose_len
        self._prose.append(text)
        self.prose_len += len(text)
        return start

    # -- parser hooks ------------------------------------------------------------------
    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        adict = {}
        for key, value in attrs:
            adict[key.lower()] = value if value is not None else ""
        classes = set((adict.get("class") or "").split())
        frame = {
            "tag": tag,
            "attrs": adict,
            "classes": classes,
            "section": adict.get("data-section"),
            "block": None,
            "interp_start": None,
            "cite_start": None,
            "ver_start": None,
            "verbatim_start": None,
            "value_id": adict.get("data-value-id"),
            "table": None,
        }
        if adict.get("data-section"):
            self.section_order.append(adict["data-section"])

        section = adict.get("data-section") or self._cur("section")

        if tag in BLOCK_TAGS:
            frame["block"] = len(self.blocks)
            self.blocks.append(
                {
                    "idx": len(self.blocks),
                    "tag": tag,
                    "start": self.prose_len,
                    "end": None,
                    "section": section,
                    "attrs": adict,
                    "value_ids": [],
                }
            )
        interp_attr = next((a for a in INTERP_ATTRS if a in adict), None)
        if interp_attr is not None:
            frame["interp_start"] = self.prose_len
        if tag == "cite" or "citation" in classes or "data-cite" in adict:
            frame["cite_start"] = self.prose_len
        if "data-ver" in adict or "version" in classes:
            frame["ver_start"] = self.prose_len
        if "data-verbatim" in adict or tag in ("pre", "code"):
            frame["verbatim_start"] = self.prose_len

        for attr in MARK_ATTRS:
            if attr in adict:
                self.marks.append(
                    {
                        "attr": attr,
                        "value": adict[attr],
                        "tag": tag,
                        "attrs": adict,
                        "section": section,
                        "block": self._cur("block") if frame["block"] is None else frame["block"],
                        "start": self.prose_len,
                        "text": [],
                    }
                )
        if adict.get("data-value-id"):
            self.value_spans.append(
                {
                    "value_id": adict["data-value-id"],
                    "section": section,
                    "block": frame["block"] if frame["block"] is not None else self._cur("block"),
                    "start": self.prose_len,
                    "end": None,
                    "attrs": adict,
                    "text": [],
                    "in_table": bool(self._table_stack),
                    "in_svg": self._in_svg(),
                }
            )
            frame["value_span"] = len(self.value_spans) - 1
            bidx = frame["block"] if frame["block"] is not None else self._cur("block")
            if bidx is not None:
                self.blocks[bidx]["value_ids"].append(adict["data-value-id"])
        if adict.get("data-series-value-id"):
            self.svg_series.append(
                {
                    "figure": self._cur_figure(adict),
                    "value_id": adict["data-series-value-id"],
                    "section": section,
                }
            )
        if tag == "table" and not self._in_svg():
            record = {
                "attrs": adict,
                "section": section,
                "rows": [],
                "figure": adict.get("data-figure"),
                "id": adict.get("id") or adict.get("data-table"),
            }
            self._table_stack.append(record)
            frame["table"] = record
        elif self._table_stack:
            if tag == "tr":
                self._table_stack[-1]["rows"].append([])
            elif tag in ("td", "th"):
                if not self._table_stack[-1]["rows"]:
                    self._table_stack[-1]["rows"].append([])
                self._table_stack[-1]["rows"][-1].append(
                    {"tag": tag, "attrs": adict, "text": [], "value_ids": []}
                )
        self.stack.append(frame)

    def _cur_figure(self, adict):
        if adict.get("data-figure"):
            return adict["data-figure"]
        for frame in reversed(self.stack):
            if frame["attrs"].get("data-figure"):
                return frame["attrs"]["data-figure"]
        return None

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in VOID_TAGS:
            return
        idx = None
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i]["tag"] == tag:
                idx = i
                break
        if idx is None:
            return
        # Close everything opened inside an unclosed element too.
        while len(self.stack) > idx:
            frame = self.stack.pop()
            self._close_frame(frame)

    def _close_frame(self, frame):
        if frame.get("value_span") is not None:
            span = self.value_spans[frame["value_span"]]
            span["end"] = self.prose_len
        if frame["interp_start"] is not None:
            self.interp_intervals.append((frame["interp_start"], self.prose_len))
        if frame["cite_start"] is not None:
            self.citation_intervals.append((frame["cite_start"], self.prose_len))
        if frame["ver_start"] is not None:
            self.version_intervals.append((frame["ver_start"], self.prose_len))
        if frame["verbatim_start"] is not None:
            self.verbatim_intervals.append((frame["verbatim_start"], self.prose_len))
        if frame["block"] is not None:
            self.blocks[frame["block"]]["end"] = self.prose_len
            self._append_prose("\n")
        if frame["table"] is not None and self._table_stack:
            self.tables.append(self._table_stack.pop())

    def handle_data(self, data):
        if not data:
            return
        self._full_text.append(data)
        if self._table_stack:
            record = self._table_stack[-1]
            if record["rows"] and record["rows"][-1]:
                cell = record["rows"][-1][-1]
                cell["text"].append(data)
                vid = self._cur("value_id")
                if vid and vid not in cell["value_ids"]:
                    cell["value_ids"].append(vid)
            return
        if self._in_skip():
            return
        start = self._append_prose(data)
        self.segments.append(
            {
                "start": start,
                "end": start + len(data),
                "section": self._cur("section"),
                "block": self._cur("block"),
                "tags": tuple(frame["tag"] for frame in self.stack),
                "classes": frozenset(self._classes()),
            }
        )
        for frame in self.stack:
            if frame.get("value_span") is not None:
                self.value_spans[frame["value_span"]]["text"].append(data)

    def close_all(self):
        while self.stack:
            self._close_frame(self.stack.pop())
        return self


def _overlaps(intervals, start, end):
    for lo, hi in intervals:
        if start < hi and end > lo:
            return True
    return False


class Report:
    def __init__(self, html: str, path: str) -> None:
        self.path = path
        self.html = html
        parser = _ReportParser()
        parser.feed(html)
        parser.close()
        parser.close_all()
        self.prose = "".join(parser._prose)
        self.full_text = "".join(parser._full_text)
        self.segments = parser.segments
        self.blocks = parser.blocks
        self.tables = parser.tables
        self.marks = parser.marks
        self.svg_series = parser.svg_series
        self.section_order = parser.section_order
        self.interp_intervals = parser.interp_intervals
        self.citation_intervals = parser.citation_intervals
        self.version_intervals = parser.version_intervals
        self.verbatim_intervals = parser.verbatim_intervals
        self.value_spans = []
        for span in parser.value_spans:
            span["text"] = "".join(span["text"]).strip()
            if span["end"] is None:
                span["end"] = span["start"] + len(span["text"])
            self.value_spans.append(span)
        for block in self.blocks:
            if block["end"] is None:
                block["end"] = self.prose_len_safe()
            block["text"] = self.prose[block["start"] : block["end"]]
        self.rendered_value_ids = {s["value_id"] for s in self.value_spans}
        for cell in self._all_cells():
            cell["text_joined"] = "".join(cell["text"]).strip()

    def prose_len_safe(self):
        return len(self.prose)

    def _all_cells(self):
        for table in self.tables:
            for row in table["rows"]:
                for cell in row:
                    yield cell

    def is_interpolated(self, start, end):
        return _overlaps(self.interp_intervals, start, end)

    def in_citation(self, start, end):
        return _overlaps(self.citation_intervals, start, end)

    def in_version_span(self, start, end):
        return _overlaps(self.version_intervals, start, end)

    def in_verbatim(self, start, end):
        return _overlaps(self.verbatim_intervals, start, end)

    def block_at(self, offset):
        best = None
        for block in self.blocks:
            if block["start"] <= offset < max(block["end"], block["start"] + 1):
                if best is None or (block["end"] - block["start"]) <= (best["end"] - best["start"]):
                    best = block
        return best

    def section_at(self, offset):
        block = self.block_at(offset)
        if block and block.get("section"):
            return block["section"]
        best = None
        for seg in self.segments:
            if seg["start"] <= offset < seg["end"]:
                best = seg
                break
        return (best or {}).get("section")

    def block_of_span(self, span):
        if span["block"] is None:
            return None
        return self.blocks[span["block"]]

    def near_text(self, span):
        """The span's own block, plus the next block in the same section."""
        block = self.block_of_span(span)
        if block is None:
            return self.prose
        text = block["text"]
        for candidate in self.blocks:
            if (
                candidate["start"] >= block["end"]
                and candidate.get("section") == block.get("section")
                and candidate["tag"] in ("p", "li", "dd", "figcaption", "caption", "td")
            ):
                text += " " + candidate["text"]
                break
        return text

    def marks_by_attr(self, attr):
        return [m for m in self.marks if m["attr"] == attr]

    def sentence_at(self, start, end):
        left = max(
            self.prose.rfind(".", 0, start),
            self.prose.rfind("!", 0, start),
            self.prose.rfind("?", 0, start),
            self.prose.rfind("\n", 0, start),
        )
        right_candidates = [
            self.prose.find(".", end),
            self.prose.find("!", end),
            self.prose.find("?", end),
            self.prose.find("\n", end),
        ]
        right_candidates = [c for c in right_candidates if c != -1]
        right = min(right_candidates) if right_candidates else len(self.prose)
        return self.prose[left + 1 : right].strip()


# --------------------------------------------------------------------------------------
# Authored text
# --------------------------------------------------------------------------------------

MARKER_RE = re.compile(r"\{\{\s*([a-z]+)\s*:\s*([^}|]+?)\s*(\|[^}]*)?\}\}")


class Authored:
    """The authored text, with its reference markers still in place."""

    def __init__(self, sections: dict, source: str) -> None:
        self.sections = sections  # section_id -> text
        self.source = source
        self.markers = []  # dicts: kind, target, options, section, start, end
        for sid, text in sections.items():
            for m in MARKER_RE.finditer(text):
                opts = {}
                if m.group(3):
                    for piece in m.group(3).lstrip("|").split("|"):
                        if "=" in piece:
                            key, _, value = piece.partition("=")
                            opts[key.strip()] = value.strip()
                        elif piece.strip():
                            opts[piece.strip()] = True
                self.markers.append(
                    {
                        "kind": m.group(1),
                        "target": m.group(2).strip(),
                        "options": opts,
                        "section": sid,
                        "start": m.start(),
                        "end": m.end(),
                        "raw": m.group(0),
                    }
                )

    @property
    def present(self) -> bool:
        return bool(self.sections)

    def markers_of(self, kind):
        return [m for m in self.markers if m["kind"] == kind]

    def interp_intervals(self, section_id):
        return [(m["start"], m["end"]) for m in self.markers if m["section"] == section_id]


def load_authored(run_dir: str) -> Authored:
    path = os.path.join(run_dir, "authored.json")
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise LinterError("authored.json must be an object of section_id -> text")
        return Authored({str(k): str(v) for k, v in data.items()}, path)
    folder = os.path.join(run_dir, "authored")
    if os.path.isdir(folder):
        sections = {}
        for name in sorted(os.listdir(folder)):
            base, ext = os.path.splitext(name)
            if ext.lower() in (".md", ".txt"):
                with open(os.path.join(folder, name), "r", encoding="utf-8") as fh:
                    sections[base] = fh.read()
        return Authored(sections, folder)
    return Authored({}, "")


def load_allowlist(run_dir: str):
    path = os.path.join(run_dir, "lint-allowlist.json")
    patterns = list(L1_ALLOWLIST)
    budget = L1_WAIVER_BUDGET
    extra = []
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        for entry in data.get("patterns") or []:
            for key in ("id", "regex", "context", "why"):
                if key not in entry:
                    raise LinterError(
                        "lint-allowlist.json entry %r is missing %r. Every entry carries a reason, "
                        "and the whole list is printed in the report's audit appendix."
                        % (entry.get("id", "?"), key)
                    )
            extra.append(entry)
        if isinstance(data.get("waiver_budget"), int):
            budget = data["waiver_budget"]
    return patterns + extra, budget, path if os.path.isfile(path) else None


# --------------------------------------------------------------------------------------
# Context object
# --------------------------------------------------------------------------------------


class Context:
    def __init__(self, bundle, report, authored, allowlist, waiver_budget, allowlist_path, args):
        self.bundle = bundle
        self.report = report
        self.authored = authored
        self.allowlist = allowlist
        self.waiver_budget = waiver_budget
        self.allowlist_path = allowlist_path
        self.args = args
        self.projections = all_projections(bundle)
        self.units = bundle.units_vocabulary()
        self.previous_bundle = None
        prev = args.previous_bundle or os.path.join(args.run_dir or "", "previous-bundle.json")
        if prev and os.path.isfile(prev):
            with open(prev, "r", encoding="utf-8") as fh:
                self.previous_bundle = json.load(fh)
        self.allowlisted_hits = []

    # ids rendered anywhere: prose spans, table cells, figure series
    def rendered_ids(self):
        out = set(self.report.rendered_value_ids)
        for cell in self.report._all_cells():
            for vid in cell["value_ids"]:
                out.add(vid)
            if cell["attrs"].get("data-value-id"):
                out.add(cell["attrs"]["data-value-id"])
            if cell["attrs"].get("data-axis-of"):
                out.add(cell["attrs"]["data-axis-of"])
        for series in self.report.svg_series:
            out.add(series["value_id"])
        for mark in self.report.marks_by_attr("data-value-id"):
            out.add(mark["value"])
        if self.authored.present:
            for marker in self.authored.markers_of("v"):
                out.add(marker["target"].split("|")[0].strip())
        return out


# --------------------------------------------------------------------------------------
# L1
# --------------------------------------------------------------------------------------


def _identifier_adjacent(text, start, end):
    before = text[max(0, start - 1) : start]
    after = text[end : end + 1]
    if before and (before.isalpha() or before in "_/.-#"):
        return True
    if after and (after.isalpha() or after in "_/"):
        return True
    return False


def _unit_adjacent(text, end, units):
    tail = text[end : end + 14]
    if tail.startswith("%"):
        return True
    m = re.match(r"\s{0,2}([A-Za-zµΩ°%/]{1,12})", tail)
    if not m:
        return False
    token = m.group(1)
    lowered = {u.lower() for u in units}
    return token in units or token.lower() in lowered


def _is_heading_ordinal(report, block, start, literal):
    if block is None or block["tag"] not in ("h1", "h2", "h3", "h4", "h5", "h6"):
        return False
    head = block["text"].lstrip()
    offset_in_block = start - block["start"]
    leading = len(block["text"]) - len(head)
    if offset_in_block > leading + 1:
        return False
    return bool(re.match(re.escape(literal) + r"\s*[.)—-]", head))


def _is_ordinal(report, block, start, end, literal):
    if block is None:
        return False
    if block["tag"] == "li":
        offset_in_block = start - block["start"]
        stripped = block["text"].lstrip()
        leading = len(block["text"]) - len(stripped)
        if offset_in_block <= leading + 1 and re.match(re.escape(literal) + r"\s*[.)]", stripped):
            return True
    window = report.prose[max(0, start - 12) : start].lower()
    return bool(re.search(r"\b(step|item|stage|phase)\s+$", window))


def _in_definition_form(bundle, doc_text, start, end, value):
    window = doc_text[max(0, start - 140) : start].lower()
    if not re.search(
        r"\b(we define|is defined as|was defined as|defined as|was fixed at|is fixed at|"
        r"was set at|the bar was|threshold of|we set)\b",
        window,
    ):
        return False
    for env in bundle.envelopes.values():
        bar = env.get("preregistered_bar") or {}
        thr = as_float(bar.get("threshold"))
        if thr is not None and abs(thr - value) <= 1e-9:
            return True
        if env.get("kind") == "assumption":
            num = as_float(env.get("value"))
            if num is not None and abs(num - value) <= 1e-9:
                return True
    return False


def _document_control_count(report, section, block, literal):
    if section not in ("document_control", "document-control", "doc_control"):
        return False
    if block is None:
        return False
    return bool(re.search(r"\b(section|figure|table|value|run|edition|page|entry|entries|"
                          r"sections|figures|tables|values|runs|editions|pages)\b",
                          block["text"], re.I))


def _allowlist_hit(ctx, occ):
    """Which allowlist entry, if any, exempts this occurrence.

    Note the two document coordinate systems. A rendered occurrence carries offsets into
    report.prose and can be tested against the marked-up regions (citation spans, version spans,
    verbatim blocks, blocks and headings). An authored occurrence carries offsets into its own
    section's authored text, where none of those regions exist yet, so the region-based contexts
    are simply not satisfied there. Using the wrong string for the wrong occurrence is how a
    literal comes to be exempted by an adjacency that exists somewhere else entirely.
    """
    report = ctx.report
    literal = occ["text"]
    doc_text = occ["doc"]
    authored = bool(occ.get("authored"))
    for entry in ctx.allowlist:
        try:
            if not re.fullmatch(entry["regex"], literal, re.I):
                continue
        except re.error as exc:
            raise LinterError("allowlist entry %s has a bad regex: %s" % (entry["id"], exc))
        context = entry["context"]
        ok = False
        if context == "any":
            ok = True
        elif context == "citation":
            ok = not authored and report.in_citation(occ["start"], occ["end"])
        elif context == "version_span":
            ok = not authored and report.in_version_span(occ["start"], occ["end"])
        elif context == "formula_or_code":
            ok = not authored and (
                report.in_verbatim(occ["start"], occ["end"]) or "formula" in occ["classes"]
            )
        elif context == "identifier_token":
            ok = _identifier_adjacent(doc_text, occ["start"], occ["end"])
        elif context == "ordinal":
            ok = not authored and _is_ordinal(
                report, occ["block"], occ["start"], occ["end"], literal
            )
        elif context == "heading_ordinal":
            ok = not authored and _is_heading_ordinal(
                report, occ["block"], occ["start"], literal
            )
        elif context == "definition_form":
            ok = _in_definition_form(ctx.bundle, doc_text, occ["start"], occ["end"], occ["value"])
        elif context == "document_control_count":
            ok = not authored and _document_control_count(
                report, occ["section"], occ["block"], literal
            )
        elif context == "trivial_cardinal":
            ok = not occ.get("relative_collisions") and not _unit_adjacent(
                doc_text, occ["end"], ctx.units
            )
        else:
            ok = False
        if ok:
            return entry
    return None


def _collect_prose_occurrences(ctx):
    report = ctx.report
    occs = []
    for m in NUMBER_RE.finditer(report.prose):
        if report.is_interpolated(m.start(), m.end()):
            continue
        text = m.group(1)
        value = as_float(text)
        if value is None:
            continue
        block = report.block_at(m.start())
        occs.append(
            {
                "text": text.replace(",", ""),
                "raw": text,
                "value": value,
                "start": m.start(),
                "end": m.end(),
                "block": block,
                "section": report.section_at(m.start()),
                "classes": set(),
                "spelled": False,
                "doc": report.prose,
            }
        )
    for m in _WORD_RUN_RE.finditer(report.prose):
        if report.is_interpolated(m.start(), m.end()):
            continue
        value = parse_word_number(m.group(1))
        if value is None:
            continue
        block = report.block_at(m.start())
        occs.append(
            {
                "text": str(value),
                "raw": m.group(1),
                "value": float(value),
                "start": m.start(),
                "end": m.end(),
                "block": block,
                "section": report.section_at(m.start()),
                "classes": set(),
                "spelled": True,
                "doc": report.prose,
            }
        )
    for occ in occs:
        seg_classes = set()
        for seg in report.segments:
            if seg["start"] <= occ["start"] < seg["end"]:
                seg_classes = set(seg["classes"])
                break
        occ["classes"] = seg_classes
    return occs


def _collect_authored_occurrences(ctx):
    out = []
    authored = ctx.authored
    for sid, text in authored.sections.items():
        intervals = authored.interp_intervals(sid)
        for m in NUMBER_RE.finditer(text):
            if _overlaps(intervals, m.start(), m.end()):
                continue
            value = as_float(m.group(1))
            if value is None:
                continue
            out.append(
                {
                    "text": m.group(1).replace(",", ""),
                    "raw": m.group(1),
                    "value": value,
                    "start": m.start(),
                    "end": m.end(),
                    "block": None,
                    "section": sid,
                    "classes": set(),
                    "spelled": False,
                    "authored": True,
                    "doc": text,
                    "context_text": text[max(0, m.start() - 80) : m.end() + 80],
                }
            )
        for m in _WORD_RUN_RE.finditer(text):
            if _overlaps(intervals, m.start(), m.end()):
                continue
            value = parse_word_number(m.group(1))
            if value is None:
                continue
            out.append(
                {
                    "text": str(value),
                    "raw": m.group(1),
                    "value": float(value),
                    "start": m.start(),
                    "end": m.end(),
                    "block": None,
                    "section": sid,
                    "classes": set(),
                    "spelled": True,
                    "authored": True,
                    "doc": text,
                    "context_text": text[max(0, m.start() - 80) : m.end() + 80],
                }
            )
    return out


def rule_l1(ctx):
    findings = []
    report = ctx.report
    projections = ctx.projections

    occurrences = _collect_prose_occurrences(ctx)
    if ctx.authored.present:
        occurrences += _collect_authored_occurrences(ctx)
    else:
        findings.append(
            skipped(
                "L1",
                "authored text",
                "no authored.json or authored/ directory in the run directory, so literals were "
                "scanned only in the RENDERED prose. Authored text that the renderer dropped, and "
                "any marker misuse, was not examined.",
                "write the authored text with its markers to <run-dir>/authored.json as "
                '{"<section_id>": "text"}.',
            )
        )

    # First pass: collisions.
    for occ in occurrences:
        hits = []
        for proj in projections:
            band = drift_band(occ["text"], proj["value"])
            diff = abs(occ["value"] - proj["value"])
            if diff <= band:
                rel = (diff / abs(proj["value"]) * 100.0) if proj["value"] else 0.0
                hits.append((diff, rel, band, proj))
        hits.sort(key=lambda h: (h[0], h[3]["id"]))
        occ["collisions"] = hits
        # The relative band alone, used only by allowlist class A9: for a bare integer the
        # two-units-in-the-last-place floor is 2, which would collide with every small number in
        # the bundle and make A9 unsatisfiable.
        occ["relative_collisions"] = [
            h for h in hits if h[0] <= abs(h[3]["value"]) * DRIFT_BAND_FRACTION
        ]

    # Allowlist first, so the audit trail and the D1 grouping below both see only real collisions.
    for occ in occurrences:
        occ["allowed"] = _allowlist_hit(ctx, occ) if occ["collisions"] else None

    # Group by envelope id so the D1 shape (one value, several printed readings) is named.
    by_env = {}
    for occ in occurrences:
        if not occ["collisions"] or occ["allowed"] is not None:
            continue
        best = occ["collisions"][0][3]
        by_env.setdefault(best["id"], set()).add(occ["text"])

    waivers = report.marks_by_attr("data-waived-literal")
    if len(waivers) > ctx.waiver_budget:
        findings.append(
            err(
                "L1",
                "document",
                "waiver-budget-exceeded: %d {{lit:...}} waivers used, budget is %d. Waived: %s"
                % (
                    len(waivers),
                    ctx.waiver_budget,
                    ", ".join(sorted(w["value"] for w in waivers)),
                ),
                "a document needing more than %d exceptions has a modelling problem, not a lint "
                "problem: give the quantities envelopes." % ctx.waiver_budget,
            )
        )
    for waiver in waivers:
        if not waiver["attrs"].get("data-why"):
            findings.append(
                err(
                    "L1",
                    "section %r, waived literal %s" % (waiver["section"], waiver["value"]),
                    "a waived literal carries no reason.",
                    'add why="..." naming why this quantity cannot have an envelope. The waiver is '
                    "printed in the audit appendix, so an empty reason is a blank line in the report.",
                )
            )

    for occ in occurrences:
        if not occ["collisions"]:
            continue
        allowed = occ["allowed"]
        if allowed is not None:
            ctx.allowlisted_hits.append((occ["raw"], allowed["id"]))
            continue
        diff, rel, band, proj = occ["collisions"][0]
        env = proj["env"]
        where = "section %r, %s offset %d" % (
            occ["section"] or "(none)",
            "authored" if occ.get("authored") else "prose",
            occ["start"],
        )
        extra = ""
        readings = by_env.get(proj["id"]) or set()
        if len(readings) > 1:
            extra = (
                " Note: this value is spelled out in the document as %s, so one quantity has more "
                "than one printed reading (D1)." % ", ".join(sorted(readings))
            )
        scope_note = ""
        per_unit_hit = next(
            (h for h in occ["collisions"] if h[3]["scope"] == "per_unit"), None
        )
        sentence = (
            occ.get("context_text")
            if occ.get("authored")
            else report.sentence_at(occ["start"], occ["end"])
        )
        if per_unit_hit is not None and AGGREGATE_QUANTIFIERS.search(sentence or ""):
            agg = env.get("aggregation") or {}
            scope_note = (
                " Scope mismatch: the literal matches a per-unit projection (%s) while the sentence "
                "quantifies the aggregate (aggregation over=%s unit_count=%s per_unit=%s). (D2)"
                % (
                    per_unit_hit[3]["how"],
                    agg.get("over"),
                    agg.get("unit_count"),
                    [p.get("value") for p in agg.get("per_unit") or []],
                )
            )
        findings.append(
            err(
                "L1",
                where,
                "literal %s%s collides with value %r (%s %s, precision %s, kind %s, run %s) via %s. "
                "|%s - %s| = %s (%.2f%%), drift band %.2f%%.%s%s"
                % (
                    occ["raw"],
                    " (spelled out)" if occ["spelled"] else "",
                    proj["id"],
                    fmt(proj["value"]),
                    env.get("unit"),
                    env.get("precision"),
                    env.get("kind"),
                    env.get("run_id"),
                    proj["how"],
                    occ["text"],
                    fmt(proj["value"]),
                    fmt(diff),
                    rel,
                    DRIFT_BAND_FRACTION * 100.0,
                    extra,
                    scope_note,
                ),
                "write {{v:%s}} so every appearance moves together. If the literal is genuinely a "
                "different quantity, give it its own envelope and interpolate that; if it is "
                "free-standing, add an allowlist pattern with a reason to lint-allowlist.json."
                % proj["id"],
            )
        )

    # Scope collisions where some other projection matched exactly are still failures.
    for occ in occurrences:
        if not occ["collisions"]:
            continue
        exact = [h for h in occ["collisions"] if h[0] == 0.0]
        per_unit_hit = next((h for h in occ["collisions"] if h[3]["scope"] == "per_unit"), None)
        if not exact or per_unit_hit is None:
            continue
        if exact[0][3]["scope"] == "per_unit":
            continue
        sentence = (
            occ.get("context_text")
            if occ.get("authored")
            else report.sentence_at(occ["start"], occ["end"])
        )
        if not AGGREGATE_QUANTIFIERS.search(sentence or ""):
            continue
        # already reported above unless allowlisted; report the scope mismatch explicitly
        already = any(f.rule == "L1" and str(occ["start"]) in f.location for f in findings)
        if already:
            continue
        findings.append(
            err(
                "L1",
                "section %r, prose offset %d" % (occ["section"] or "(none)", occ["start"]),
                "literal %s matches a per-unit projection of %r while the sentence quantifies the "
                "aggregate. (D2)" % (occ["raw"], per_unit_hit[3]["id"]),
                "write {{v:%s|n}}, which renders the count with its aggregation scope, or "
                "interpolate the per-unit envelope explicitly." % per_unit_hit[3]["id"],
            )
        )
    return findings


# --------------------------------------------------------------------------------------
# L2
# --------------------------------------------------------------------------------------


def _cell_is_numeric(text):
    return bool(re.search(r"\d", text or ""))


def _series_axis_values(env, key):
    value = env.get("value")
    out = []
    if isinstance(value, dict):
        for point in value.get("points") or []:
            at = point.get("at") or {}
            if key in at:
                out.append(at[key])
    return out


def rule_l2(ctx):
    findings = []
    bundle = ctx.bundle
    report = ctx.report

    # 1. markers resolve
    if ctx.authored.present:
        for marker in ctx.authored.markers_of("v"):
            target = marker["target"].split("|")[0].strip()
            if target not in bundle.envelopes:
                findings.append(
                    err(
                        "L2",
                        "section %r, authored offset %d" % (marker["section"], marker["start"]),
                        "marker %s does not resolve to an envelope." % marker["raw"],
                        "create the envelope, or fix the id. A marker that does not resolve must "
                        "never fall back to an empty string.",
                    )
                )
    for span in report.value_spans:
        if span["value_id"] not in bundle.envelopes:
            findings.append(
                err(
                    "L2",
                    "section %r, rendered span at prose offset %d"
                    % (span["section"], span["start"]),
                    "rendered value id %r is not an envelope in the bundle (text rendered: %r)."
                    % (span["value_id"], span["text"]),
                    "reference an envelope that exists in roofs[], measurements[], derived[] or "
                    "assumptions[].",
                )
            )

    # 2. every numeric rendered cell traces to an envelope id
    for table in report.tables:
        for r, row in enumerate(table["rows"]):
            for c, cell in enumerate(row):
                text = cell["text_joined"]
                if not _cell_is_numeric(text):
                    continue
                attrs = cell["attrs"]
                if attrs.get("data-value-id") or cell["value_ids"]:
                    continue
                if attrs.get("data-axis-of"):
                    axis_env = bundle.envelopes.get(attrs["data-axis-of"])
                    key = attrs.get("data-axis-key")
                    if axis_env is None:
                        findings.append(
                            err(
                                "L2",
                                "table %r cell [%d][%d]" % (table["id"] or table["figure"], r, c),
                                "data-axis-of names %r, which is not an envelope."
                                % attrs["data-axis-of"],
                                "point data-axis-of at the series envelope whose points this axis "
                                "indexes.",
                            )
                        )
                        continue
                    axis_values = [str(v) for v in _series_axis_values(axis_env, key)]
                    if text.strip() not in axis_values:
                        findings.append(
                            err(
                                "L2",
                                "table %r cell [%d][%d]" % (table["id"] or table["figure"], r, c),
                                "axis cell renders %r, which is not a point of %r on key %r "
                                "(points: %s)."
                                % (text, attrs["data-axis-of"], key, ", ".join(axis_values) or "none"),
                                "render an axis value that exists in the series, or fix "
                                "data-axis-key.",
                            )
                        )
                    continue
                if attrs.get("data-field"):
                    continue
                findings.append(
                    err(
                        "L2",
                        "table %r cell [%d][%d]"
                        % (table["id"] or table["figure"] or "(unnamed)", r, c),
                        "renders literal %r with no envelope reference." % text,
                        "reference the envelope with data-value-id, or, for a non-quantity fact, "
                        "data-field naming the bundle path it came from. A table of literals beside "
                        "a chart of envelopes is two sources of truth for one number. (D1)",
                    )
                )

    # 2b. data-field marks resolve and match what they render
    for mark in report.marks_by_attr("data-field"):
        found, value = resolve_bundle_path(bundle, mark["value"])
        if not found:
            findings.append(
                err(
                    "L2",
                    "section %r, data-field %r" % (mark["section"], mark["value"]),
                    "the bundle path does not resolve.",
                    "name a path that exists in the bundle, so the rendered fact has one source.",
                )
            )
    for cell in report._all_cells():
        path = cell["attrs"].get("data-field")
        if not path:
            continue
        found, value = resolve_bundle_path(bundle, path)
        if found and value is not None and str(value) not in cell["text_joined"]:
            findings.append(
                err(
                    "L2",
                    "cell rendering data-field %r" % path,
                    "the cell renders %r but the bundle holds %r." % (cell["text_joined"], value),
                    "render the bundle value verbatim; a hand-typed restatement of a bundle fact "
                    "drifts exactly as a hand-typed number does.",
                )
            )

    # 3/4. kinds and obligations
    for eid, env in sorted(bundle.envelopes.items()):
        kind = env.get("kind")
        if kind not in KIND_ENUM:
            findings.append(
                err(
                    "L2",
                    "value %r" % eid,
                    "kind %r is not one of the closed enum %s." % (kind, ", ".join(KIND_ENUM)),
                    "pick the kind that is true. The enum is closed so an assumption cannot be "
                    "labelled something that sounds measured. (D8)",
                )
            )
            continue
        for field in KIND_OBLIGATIONS.get(kind, []):
            value = env.get(field)
            if value in (None, "", [], {}):
                findings.append(
                    err(
                        "L2",
                        "value %r" % eid,
                        "kind %r requires %r, which is absent or empty." % (kind, field),
                        "supply %s. For an assumption that means stating what the choice is based "
                        "on and what the report may not conclude from it. (D8)" % field,
                    )
                )
        # 5. numeric values carry precision; a number stored as a string fails
        value = env.get("value")
        if isinstance(value, str) and as_float(value) is not None:
            findings.append(
                err(
                    "L2",
                    "value %r" % eid,
                    "the value is stored as the string %r." % value,
                    "store it as a number. A number in a string cannot be rounded, compared or "
                    "checked.",
                )
            )
        numeric = as_float(value) is not None or (
            isinstance(value, dict) and ("points" in value or "min" in value)
        )
        if numeric and not isinstance(env.get("precision"), int):
            findings.append(
                err(
                    "L2",
                    "value %r" % eid,
                    "a numeric value with no precision.",
                    "set precision. It is the single anti-drift mechanism: one stored value, one "
                    "rounding rule, rendered identically everywhere. (D1, D3)",
                )
            )
        if not env.get("unit"):
            findings.append(
                err(
                    "L2",
                    "value %r" % eid,
                    "no unit.",
                    "give it a unit, including dimensionless ones: use ratio, fraction, percent, "
                    "count or 1. There is no unitless number, only one whose unit the reader "
                    "guesses.",
                )
            )
        if env.get("n") is not None and env["n"] > 1 and not env.get("spread"):
            if not env.get("spread_not_available"):
                findings.append(
                    err(
                        "L2",
                        "value %r" % eid,
                        "n = %s with no spread and no spread_not_available reason." % env["n"],
                        "record the spread, or state why the instrument cannot report one. A mean "
                        "with no spread invites a reader to treat noise as signal.",
                    )
                )

    # 6. id uniqueness across the arrays taken together
    for iid, first, second in bundle.duplicate_ids():
        findings.append(
            err(
                "L2",
                "id %r" % iid,
                "declared in both %s[] and %s[]." % (first, second),
                "rename one. A duplicated id is how two different values come to print under one "
                "name.",
            )
        )
    return findings


# --------------------------------------------------------------------------------------
# L3
# --------------------------------------------------------------------------------------

_ALLOWED_AST = (
    ast.Expression,
    ast.BinOp,
    ast.UnaryOp,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Pow,
    ast.Mod,
    ast.USub,
    ast.UAdd,
    ast.Name,
    ast.Load,
    ast.Constant,
    ast.Call,
    ast.Tuple,
)

_ALLOWED_FUNCS = {
    "min": min,
    "max": max,
    "abs": abs,
    "sqrt": math.sqrt,
    "log": math.log,
    "log2": math.log2,
    "log10": math.log10,
    "exp": math.exp,
    "sum": sum,
    "round": round,
}


class FormulaError(Exception):
    pass


def parse_formula(formula: str):
    text = formula.strip()
    # A trailing parenthetical comment is common and harmless: "a / b * 100 (percent)".
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as exc:
        raise FormulaError("does not parse as an expression: %s" % exc)
    names = set()
    constants = []
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_AST):
            raise FormulaError("uses %s, which is not arithmetic" % type(node).__name__)
        if isinstance(node, ast.Name):
            names.add(node.id)
        if isinstance(node, ast.Constant):
            num = as_float(node.value)
            if num is None:
                raise FormulaError("contains a non-numeric constant %r" % (node.value,))
            constants.append(num)
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in _ALLOWED_FUNCS:
                raise FormulaError("calls something other than a permitted arithmetic function")
    return tree, names, constants


def eval_formula(tree, values):
    def walk(node):
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Constant):
            return float(node.value)
        if isinstance(node, ast.Name):
            if node.id in values:
                return values[node.id]
            raise FormulaError("input %r has no value" % node.id)
        if isinstance(node, ast.UnaryOp):
            operand = walk(node.operand)
            return -operand if isinstance(node.op, ast.USub) else +operand
        if isinstance(node, ast.BinOp):
            left, right = walk(node.left), walk(node.right)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, ast.Div):
                if right == 0:
                    raise FormulaError("division by zero")
                return left / right
            if isinstance(node.op, ast.FloorDiv):
                return left // right
            if isinstance(node.op, ast.Pow):
                return left ** right
            if isinstance(node.op, ast.Mod):
                return left % right
            raise FormulaError("unsupported operator")
        if isinstance(node, ast.Call):
            func = _ALLOWED_FUNCS[node.func.id]
            args = [walk(a) for a in node.args]
            if node.func.id == "sum":
                return func(args)
            return func(*args)
        if isinstance(node, ast.Tuple):
            return tuple(walk(e) for e in node.elts)
        raise FormulaError("unsupported expression")

    return walk(tree)


def _cycle_check(bundle, eid, stack=None):
    stack = stack or []
    if eid in stack:
        return stack + [eid]
    env = bundle.envelopes.get(eid)
    if env is None:
        return None
    for dep in env.get("inputs") or []:
        found = _cycle_check(bundle, dep, stack + [eid])
        if found:
            return found
    return None


def rule_l3(ctx):
    findings = []
    bundle = ctx.bundle
    rendered = ctx.rendered_ids()
    tool = bundle.data.get("tool") or {}

    for eid, env in sorted(bundle.envelopes.items()):
        if env.get("kind") not in ("derived", "projection"):
            continue
        inputs = env.get("inputs") or []
        location = "value %r" % eid
        for dep in inputs:
            if dep not in bundle.envelopes:
                findings.append(
                    err(
                        "L3",
                        location,
                        "input %r does not resolve to an envelope." % dep,
                        "declare the input as an envelope, or remove it from inputs[]. A derived "
                        "value that cannot name its inputs is invalid. (D5)",
                    )
                )
            if dep == eid:
                findings.append(
                    err(
                        "L3",
                        location,
                        "the value is its own input.",
                        "break the self-reference.",
                    )
                )
        cycle = _cycle_check(bundle, eid)
        if cycle:
            findings.append(
                err(
                    "L3",
                    location,
                    "input cycle: %s." % " -> ".join(cycle),
                    "break the cycle. A derivation that depends on itself cannot be recomputed.",
                )
            )
            continue

        formula = env.get("formula")
        if not formula:
            continue
        try:
            tree, names, constants = parse_formula(formula)
        except FormulaError as exc:
            findings.append(
                err(
                    "L3",
                    location,
                    "formula %r %s." % (formula, exc),
                    "publish the arithmetic as an expression a reader can execute, naming inputs by "
                    "id. (D5)",
                )
            )
            continue
        names = {n for n in names if n not in _ALLOWED_FUNCS}
        missing = sorted(set(inputs) - names)
        extra = sorted(names - set(inputs))
        if missing:
            findings.append(
                err(
                    "L3",
                    location,
                    "formula does not name declared input(s): %s." % ", ".join(missing),
                    "name every input in the expression, or drop the unused input.",
                )
            )
        if extra:
            findings.append(
                err(
                    "L3",
                    location,
                    "formula names %s, which is not in inputs[]." % ", ".join(extra),
                    "add it to inputs[] as an envelope id.",
                )
            )
        for const in constants:
            if const in L3_SCALE_CONSTANTS:
                continue
            findings.append(
                err(
                    "L3",
                    location,
                    "formula names constant %s in its formula but not in inputs." % fmt(const),
                    "promote the constant to an envelope with a kind and a source. A derivation "
                    "that carries one machine's geometry as a literal produces a confident wrong "
                    "answer on any other machine instead of an error. (D11) The published "
                    "exemption list is only scale factors and identities: %s"
                    % ", ".join(fmt(c) for c in sorted(L3_SCALE_CONSTANTS)),
                )
            )

        # execute
        values = {}
        unusable = []
        for dep in inputs:
            dep_env = bundle.envelopes.get(dep) or {}
            num = as_float(dep_env.get("value"))
            if num is None:
                unusable.append(dep)
            else:
                values[dep] = num
        if unusable:
            findings.append(
                skipped(
                    "L3",
                    "recomputation of %r" % eid,
                    "input(s) %s are not scalars (a range or a series), so the formula could not be "
                    "executed against them." % ", ".join(unusable),
                    "state the reduction as its own derived envelope (for example the interpolation "
                    "or the fit), so each step is scalar and executable.",
                )
            )
        else:
            stored = as_float(env.get("value"))
            if stored is None:
                findings.append(
                    skipped(
                        "L3",
                        "recomputation of %r" % eid,
                        "the stored value is not a scalar, so it could not be compared with the "
                        "recomputation.",
                        "store a scalar, or split the derivation per point.",
                    )
                )
            else:
                try:
                    got = eval_formula(tree, values)
                except FormulaError as exc:
                    findings.append(
                        err("L3", location, "formula could not be executed: %s." % exc,
                            "fix the formula or the inputs.")
                    )
                    got = None
                if got is not None:
                    precision = env.get("precision")
                    precision = precision if isinstance(precision, int) else 6
                    tolerance = 0.5 * 10.0 ** (-precision)
                    rel_tol = as_float(env.get("rebuild_tolerance"))
                    ok = abs(got - stored) <= tolerance
                    if not ok and rel_tol is not None:
                        ok = abs(got - stored) <= abs(stored) * rel_tol
                        if ok and rel_tol > 0.01 and not env.get("caveat"):
                            findings.append(
                                err(
                                    "L3",
                                    location,
                                    "rebuild_tolerance is %s, above one per cent, with no caveat."
                                    % rel_tol,
                                    "add caveat text: above one per cent the reader needs to know "
                                    "the value is approximate.",
                                )
                            )
                    if not ok:
                        # Relative to the RECOMPUTED value: the recomputation is the reference,
                        # and the stored number is the thing under suspicion.
                        diff_pct = (
                            abs(got - stored) / abs(got) * 100.0 if got else float("inf")
                        )
                        findings.append(
                            err(
                                "L3",
                                location,
                                "does not recompute. formula: %s; inputs: %s; recomputed %s, stored "
                                "%s, precision %s, difference %.2f%%."
                                % (
                                    formula,
                                    ", ".join(
                                        "%s = %s %s"
                                        % (
                                            k,
                                            fmt(v),
                                            (bundle.envelopes.get(k) or {}).get("unit", ""),
                                        )
                                        for k, v in values.items()
                                    ),
                                    fmt(round(got, precision)),
                                    fmt(round(stored, precision)),
                                    env.get("precision"),
                                    diff_pct,
                                ),
                                "fix the arithmetic or the stored value. A derivation the linter "
                                "executes cannot be wrong in private. (D5)",
                            )
                        )

        # inputs must be printed
        for dep in inputs:
            if dep not in rendered:
                findings.append(
                    err(
                        "L3",
                        location,
                        "recomputes from input %r, which is never rendered in the document." % dep,
                        "print the input, or drop the derived value. A derivation the reader cannot "
                        "execute is the defect even when the arithmetic is right. (D5)",
                    )
                )

        # interpolation basis against the figure that publishes the series
        basis = env.get("interpolation_basis")
        for dep in inputs:
            dep_env = bundle.envelopes.get(dep) or {}
            dep_value = dep_env.get("value")
            if not isinstance(dep_value, dict) or "points" not in dep_value:
                continue
            declared = basis or dep_value.get("interpolation")
            if not declared:
                findings.append(
                    err(
                        "L3",
                        location,
                        "interpolates series %r but neither the series nor the derivation declares "
                        "an interpolation basis." % dep,
                        "state the basis (linear, log-linear, per axis) and the quantity that "
                        "varies smoothly. D5 was exactly this field's absence.",
                    )
                )
                continue
            expected = BASIS_TO_SCALE.get(str(declared).strip().lower())
            for fig in bundle.figures.values():
                view = fig.get("table_view") or {}
                if dep not in (view.get("value_ids") or []):
                    continue
                for axis_name in ("x", "y"):
                    axis = ((fig.get("axes") or {}).get(axis_name)) or {}
                    scale = axis.get("scale")
                    if not scale or expected is None:
                        continue
                    if axis_name == "y" and scale not in expected:
                        findings.append(
                            err(
                                "L3",
                                location,
                                'interpolation basis declared %r but figure %r declares axis %s '
                                "scale %r. A quantity linear in the axis variable may not be "
                                "interpolated on a logarithmic reading of it."
                                % (declared, fig.get("id"), axis_name, scale),
                                "interpolate in the quantity that varies smoothly, and declare the "
                                "same basis the figure's axis declares. (D5)",
                            )
                        )

        if env.get("requires_inputs_present") is False and not env.get("rationale"):
            findings.append(
                err(
                    "L3",
                    location,
                    "requires_inputs_present is false with no rationale.",
                    "either let the derivation fail on a missing input, or state why a fallback is "
                    "legitimate. A silent default is how one machine's geometry became every "
                    "machine's answer. (D11)",
                )
            )
        computed_by = env.get("computed_by")
        if not computed_by:
            findings.append(
                err(
                    "L3",
                    location,
                    "no computed_by: the code that computes this value is not named.",
                    "point computed_by inside the tested harness. Derivations parked beside the "
                    "report are not reviewed and not tested. (D5)",
                )
            )
        elif tool.get("derivations_unit_tested") is not True:
            findings.append(
                err(
                    "L3",
                    location,
                    "computed_by is %r but tool.derivations_unit_tested is %r."
                    % (computed_by, tool.get("derivations_unit_tested")),
                    "move the derivation inside the harness and unit-test it, or the report must "
                    "print that the derivation is untested. (D5)",
                )
            )
    return findings


# --------------------------------------------------------------------------------------
# L4
# --------------------------------------------------------------------------------------

KIND_LABEL_WORDS = {
    "assumption": ("assumed", "assumption"),
    "projection": ("projected", "projection", "extrapolat"),
}

FLOOR_WORDS = ("upper bound", "at most", "no more than", "floor", "bound on achievement", "at least this high")
ILLUSTRATIVE_WORDS = ("illustrative", "not citable", "not to be cited", "shape only")


def _block_marks_kind(report, span, words):
    text = (report.near_text(span) or "").lower()
    if any(word in text for word in words):
        return True
    block = report.block_of_span(span)
    if block is None:
        return False
    for mark in report.marks_by_attr("data-kind-label"):
        if mark["block"] == block["idx"]:
            return True
    return False


def rule_l4(ctx):
    findings = []
    bundle = ctx.bundle
    report = ctx.report

    appearances = {}
    for span in report.value_spans:
        appearances.setdefault(span["value_id"], []).append(span)
    for cell in report._all_cells():
        vid = cell["attrs"].get("data-value-id")
        if vid:
            appearances.setdefault(vid, [])

    for eid, spans in sorted(appearances.items()):
        env = bundle.envelopes.get(eid)
        if env is None:
            continue
        kind = env.get("kind")
        labelled, unlabelled = [], []
        if kind in KIND_LABEL_WORDS:
            for i, span in enumerate(spans, start=1):
                if _block_marks_kind(report, span, KIND_LABEL_WORDS[kind]):
                    labelled.append((i, span))
                else:
                    unlabelled.append((i, span))
            if unlabelled:
                findings.append(
                    err(
                        "L4",
                        "value %r, appearance(s) %s"
                        % (eid, ", ".join(str(i) for i, _ in unlabelled)),
                        "kind %s rendered without its label. Labelled appearances: %s; unlabelled: "
                        "%s (sections: %s)."
                        % (
                            kind,
                            ", ".join(str(i) for i, _ in labelled) or "none",
                            ", ".join(str(i) for i, _ in unlabelled),
                            ", ".join(
                                sorted({str(s["section"]) for _, s in unlabelled})
                            ),
                        ),
                        "the renderer must emit the kind marking at every appearance, in the same "
                        "visual weight as the number. A number is cited from where it is read. (D8)",
                    )
                )
        closure = bundle.input_closure(eid)
        assumed_inputs = sorted(
            dep
            for dep in closure
            if (bundle.envelopes.get(dep) or {}).get("kind") in ("assumption", "projection")
        )
        if assumed_inputs and kind not in KIND_LABEL_WORDS:
            for i, span in enumerate(spans, start=1):
                text = (report.near_text(span) or "").lower()
                named = any(
                    dep in text or bundle.label_of(dep).lower() in text for dep in assumed_inputs
                )
                if not (
                    any(w in text for w in ("assum", "project")) and named
                ):
                    findings.append(
                        err(
                            "L4",
                            "value %r, appearance %d (section %r)" % (eid, i, span["section"]),
                            "derived from assumed input(s) %s but rendered without inheriting the "
                            "marking or naming the assumed input." % ", ".join(assumed_inputs),
                            "mark the value as resting on an assumption and name which one. An "
                            "unmarked derived value with an assumed input is an assumption "
                            "laundered through arithmetic. (D8)",
                        )
                    )
        if env.get("inherits_floor") is True or (
            bundle.envelope_group.get(eid) == "roofs" and env.get("is_floor") is True
        ):
            for i, span in enumerate(spans, start=1):
                text = (report.near_text(span) or "").lower()
                if not any(word in text for word in FLOOR_WORDS):
                    findings.append(
                        err(
                            "L4",
                            "value %r, appearance %d (section %r)" % (eid, i, span["section"]),
                            "the value is a floor, or inherits one, and is rendered without bound "
                            "wording.",
                            "render it as an upper bound on achievement. A fraction of a floor is "
                            "not a fraction of a ceiling, and the qualification travels with the "
                            "value, not with the section that introduced it.",
                        )
                    )
        if env.get("illustrative_only") is True:
            for i, span in enumerate(spans, start=1):
                text = (report.near_text(span) or "").lower()
                if not any(word in text for word in ILLUSTRATIVE_WORDS):
                    findings.append(
                        err(
                            "L4",
                            "value %r, appearance %d (section %r)" % (eid, i, span["section"]),
                            "illustrative_only is true but the appearance is not marked in place.",
                            "mark it as illustrative and not citable at every appearance.",
                        )
                    )

    dist = ((bundle.data.get("workload") or {}).get("distribution")) or {}
    if dist and dist.get("kind") == "assumption" and dist.get("weighted_summary_permitted") is not True:
        declared = [
            eid
            for eid, env in bundle.envelopes.items()
            if env.get("from_distribution")
        ]
        for eid in declared:
            if eid in ctx.rendered_ids():
                findings.append(
                    err(
                        "L4",
                        "value %r" % eid,
                        "declares from_distribution while the workload distribution kind is "
                        "assumption and weighted_summary_permitted is not true.",
                        "do not render a weighted single figure from an assumed mixture. Show the "
                        "spread across sizes instead. (D8)",
                    )
                )
        if not declared:
            findings.append(
                skipped(
                    "L4",
                    "weighted summaries of the assumed size mixture",
                    "the workload distribution is an assumption whose weighted_summary_permitted "
                    "is not true, and no envelope declares from_distribution, so the linter cannot "
                    "tell whether any rendered value is a weighted mean of those shares.",
                    "set from_distribution on any envelope computed from the mixture shares, so "
                    "the prohibition is checkable rather than a matter of reading the prose.",
                )
            )
    return findings


# --------------------------------------------------------------------------------------
# L5
# --------------------------------------------------------------------------------------


def _nearest_ids(target, pool, limit=3):
    """Ids a reader most plausibly meant, so the message tells the author what to type."""
    pool = list(pool)
    close = difflib.get_close_matches(target, pool, n=limit, cutoff=0.4)
    if close:
        return close

    def score(candidate):
        common = len(set(target) & set(candidate))
        return (-common, abs(len(candidate) - len(target)))

    return sorted(pool, key=score)[:limit]


def rule_l5(ctx):
    findings = []
    bundle = ctx.bundle
    report = ctx.report

    resolvers = {
        "sec": set(bundle.sections) | set(report.section_order),
        "fig": set(bundle.figures),
        "tbl": set(bundle.figures) | {t["id"] for t in report.tables if t["id"]},
        "run": set(bundle.runs),
        "ev": None,  # L9 owns evidence resolution
    }

    if ctx.authored.present:
        for marker in ctx.authored.markers:
            kind = marker["kind"]
            if kind not in ("sec", "fig", "tbl", "run"):
                continue
            pool = resolvers[kind]
            if marker["target"] not in pool:
                findings.append(
                    err(
                        "L5",
                        "section %r, authored offset %d" % (marker["section"], marker["start"]),
                        "marker %s does not resolve. Nearest declared ids: %s."
                        % (marker["raw"], ", ".join(_nearest_ids(marker["target"], sorted(pool)))),
                        "fix the id, or declare the target. A marker that does not resolve must be "
                        "a build failure, never a silent empty string.",
                    )
                )
    else:
        findings.append(
            skipped(
                "L5",
                "authored reference markers",
                "no authored text was supplied, so marker resolution and the declared "
                "cross_references cross-check ran only against the rendered output.",
                "write <run-dir>/authored.json with the markers still in place.",
            )
        )

    for attr, pool_name in (
        ("data-section-ref", "sec"),
        ("data-fig-ref", "fig"),
        ("data-tbl-ref", "tbl"),
        ("data-run-ref", "run"),
    ):
        for mark in report.marks_by_attr(attr):
            pool = resolvers[pool_name]
            if mark["value"] not in pool:
                findings.append(
                    err(
                        "L5",
                        "section %r, rendered %s=%r" % (mark["section"], attr, mark["value"]),
                        "the reference does not resolve. Nearest ids: %s."
                        % ", ".join(_nearest_ids(mark["value"], sorted(pool))),
                        "fix the id or declare the target.",
                    )
                )

    # 2. no document numbers in authored text (rendered prose outside generated marks)
    for m in DOC_REFERENCE.finditer(report.prose):
        if report.is_interpolated(m.start(), m.end()):
            continue
        if report.in_citation(m.start(), m.end()):
            continue
        findings.append(
            err(
                "L5",
                "section %r, prose offset %d" % (report.section_at(m.start()), m.start()),
                "literal document reference %r." % m.group(0),
                "write {{sec:<id>}} / {{fig:<id>}} / {{tbl:<id>}} instead. Numbers are assigned at "
                "render time, so a number in the source is a guess that goes stale the next time "
                "sections move. (D6)",
            )
        )
    if ctx.authored.present:
        for sid, text in ctx.authored.sections.items():
            intervals = ctx.authored.interp_intervals(sid)
            for m in DOC_REFERENCE.finditer(text):
                if _overlaps(intervals, m.start(), m.end()):
                    continue
                findings.append(
                    err(
                        "L5",
                        "section %r, authored offset %d" % (sid, m.start()),
                        "literal document reference %r in authored text." % m.group(0),
                        "replace it with a marker. (D6)",
                    )
                )

    # 3. declared sections rendered, rendered sections declared
    rendered = [s for s in report.section_order]
    declared = list(bundle.sections)
    for sid in declared:
        if sid not in rendered:
            findings.append(
                err(
                    "L5",
                    "section %r" % sid,
                    "declared in sections[] but not rendered in the document.",
                    "render it, or remove it from sections[]. A section nothing can reference is "
                    "unreachable, and a declared-but-absent section makes every cross-reference to "
                    "it a build-time guess.",
                )
            )
    for sid in rendered:
        if declared and sid not in declared:
            findings.append(
                err(
                    "L5",
                    "section %r" % sid,
                    "rendered but not declared in sections[].",
                    "declare it, with a purpose and the value ids it uses.",
                )
            )

    # 4. declared cross_references agree with the markers actually made
    if ctx.authored.present:
        for sid, section in bundle.sections.items():
            declared_refs = set(section.get("cross_references") or [])
            made = {
                m["target"]
                for m in ctx.authored.markers
                if m["section"] == sid and m["kind"] == "sec"
            }
            for missing in sorted(declared_refs - made):
                findings.append(
                    err(
                        "L5",
                        "section %r" % sid,
                        "declares a cross_reference to %r that the text never makes." % missing,
                        "make the reference or drop the declaration. The declared list is what a "
                        "reviewer reads.",
                    )
                )
            for undeclared in sorted(made - declared_refs):
                findings.append(
                    err(
                        "L5",
                        "section %r" % sid,
                        "the text references %r, which is not in the declared cross_references."
                        % undeclared,
                        "declare it in sections[].cross_references.",
                    )
                )

    # 5. reordering must not break resolution
    shuffled = list(bundle.data.get("sections") or [])
    rng = random.Random(20260826)
    rng.shuffle(shuffled)
    reshuffled_ids = {s.get("id") for s in shuffled}
    for mark in report.marks_by_attr("data-section-ref"):
        if mark["value"] in set(bundle.sections) and mark["value"] not in reshuffled_ids:
            findings.append(
                err(
                    "L5",
                    "section reference %r" % mark["value"],
                    "resolution depends on the declared order of sections[].",
                    "resolve references by id only.",
                )
            )
    return findings


# --------------------------------------------------------------------------------------
# L6
# --------------------------------------------------------------------------------------


def rule_l6(ctx):
    findings = []
    bundle = ctx.bundle
    report = ctx.report

    tables_by_figure = {}
    for table in report.tables:
        if table["figure"]:
            tables_by_figure.setdefault(table["figure"], []).append(table)
    series_by_figure = {}
    for series in report.svg_series:
        series_by_figure.setdefault(series["figure"], set()).add(series["value_id"])

    for fid, fig in sorted(bundle.figures.items()):
        view = fig.get("table_view")
        location = "figure %r" % fid
        if not view:
            findings.append(
                err(
                    "L6",
                    location,
                    "has no table_view.",
                    "add columns and value_ids, or declare same_as_figure if it is a second view of "
                    "another figure's data. A curve a reader cannot read numbers off cannot support "
                    "a derived ceiling. (D5)",
                )
            )
            continue
        if view.get("same_as_figure"):
            other_id = view["same_as_figure"]
            other = bundle.figures.get(other_id)
            if other is None:
                findings.append(
                    err(
                        "L6",
                        location,
                        "same_as_figure names %r, which is not a figure." % other_id,
                        "name a figure that exists.",
                    )
                )
                continue
            other_view = other.get("table_view") or {}
            if other_view.get("same_as_figure"):
                findings.append(
                    err(
                        "L6",
                        location,
                        "same_as_figure points at %r, which itself shares another figure's table."
                        % other_id,
                        "point at the figure that owns the table.",
                    )
                )
            mine = series_by_figure.get(fid)
            theirs = series_by_figure.get(other_id)
            if mine is None or theirs is None:
                findings.append(
                    skipped(
                        "L6",
                        "shared-table equivalence for figure %r" % fid,
                        "the rendered output does not mark which series each figure draws, so the "
                        "claim that both figures are views of the same values could not be checked.",
                        "mark each drawn series with data-series-value-id inside the figure's svg.",
                    )
                )
            elif mine != theirs:
                findings.append(
                    err(
                        "L6",
                        location,
                        "shares the table of %r but draws different values (%s vs %s)."
                        % (other_id, sorted(mine), sorted(theirs)),
                        "sharing a table between two figures that plot different data is two "
                        "sources of truth wearing one label. Give this figure its own table.",
                    )
                )
            value_ids = other_view.get("value_ids") or []
        else:
            value_ids = view.get("value_ids") or []
            if not view.get("columns"):
                findings.append(
                    err("L6", location, "table_view has no columns.", "declare the columns.")
                )
        for vid in value_ids:
            if vid not in bundle.envelopes:
                findings.append(
                    err(
                        "L6",
                        location,
                        "table_view value_id %r does not resolve." % vid,
                        "reference an envelope that exists.",
                    )
                )

        rendered_tables = tables_by_figure.get(fid) or []
        if not rendered_tables and fig.get("chart_type") != "table-only":
            findings.append(
                err(
                    "L6",
                    location,
                    "declares a table_view but no rendered table carries data-figure=%r." % fid,
                    "render the table view and mark it with data-figure, so the linter and the "
                    "reader are looking at the same object.",
                )
            )
        for table in rendered_tables:
            for r, row in enumerate(table["rows"]):
                for c, cell in enumerate(row):
                    text = cell["text_joined"]
                    if not _cell_is_numeric(text):
                        continue
                    vid = cell["attrs"].get("data-value-id") or (
                        cell["value_ids"][0] if cell["value_ids"] else None
                    )
                    axis_of = cell["attrs"].get("data-axis-of")
                    if vid is None and axis_of is None and not cell["attrs"].get("data-field"):
                        findings.append(
                            err(
                                "L6",
                                "%s table_view cell [%d][%d]" % (location, r, c),
                                "renders literal %r with no value_id." % text,
                                "reference the envelope. A table of literals beside a chart of "
                                "envelopes is two sources of truth for one number. (D1)",
                            )
                        )
                        continue
                    trace = vid or axis_of
                    if trace and value_ids and trace not in value_ids:
                        findings.append(
                            err(
                                "L6",
                                "%s table_view cell [%d][%d]" % (location, r, c),
                                "traces to %r, which is not in the figure's declared value_ids (%s)."
                                % (trace, ", ".join(value_ids)),
                                "declare the id in table_view.value_ids, or render the declared one.",
                            )
                        )
                    if vid:
                        env = bundle.envelopes.get(vid) or {}
                        precision = env.get("precision")
                        m = re.search(r"\d+\.(\d+)", text)
                        rendered_places = len(m.group(1)) if m else 0
                        if isinstance(precision, int) and rendered_places != precision:
                            findings.append(
                                err(
                                    "L6",
                                    "%s table_view cell [%d][%d]" % (location, r, c),
                                    "renders %r at %d decimal places, but %r declares precision %d."
                                    % (text, rendered_places, vid, precision),
                                    "render at the envelope's own precision. A table that rounds "
                                    "differently from the prose is D1 with a ruled border.",
                                )
                            )

        drawn = series_by_figure.get(fid)
        if drawn is None:
            if fig.get("chart_type") not in ("table-only", None):
                findings.append(
                    skipped(
                        "L6",
                        "series-versus-table equivalence for figure %r" % fid,
                        "no drawn series is marked with data-series-value-id, so whether the chart "
                        "draws more than its table publishes was not checked.",
                        "mark each drawn series with data-series-value-id.",
                    )
                )
        else:
            for vid in sorted(drawn - set(value_ids)):
                findings.append(
                    err(
                        "L6",
                        location,
                        "draws series %r which is not in the table view." % vid,
                        "publish every drawn series in the table. A chart that draws more than its "
                        "table publishes is the D5 shape again.",
                    )
                )

        if fig.get("chart_type") in ("line", "scatter", "roofline"):
            axes = fig.get("axes") or {}
            for axis_name in ("x", "y"):
                axis = axes.get(axis_name) or {}
                if not axis.get("scale"):
                    findings.append(
                        err(
                            "L6",
                            location,
                            "axis %s declares no scale on a %s chart."
                            % (axis_name, fig.get("chart_type")),
                            "declare the scale. Whether a curve looks linear is a property of the "
                            "axis, and a ceiling derived by interpolating on the wrong scale is D5.",
                        )
                    )
        for line in fig.get("reference_lines") or []:
            vid = line.get("value_id")
            env = bundle.envelopes.get(vid)
            if env is None:
                findings.append(
                    err(
                        "L6",
                        location,
                        "reference line names %r, which is not an envelope." % vid,
                        "reference the roof by id. A roof drawn as a hand-placed line is a number "
                        "outside the contract.",
                    )
                )
                continue
            if bundle.envelope_group.get(vid) == "roofs":
                if bool(env.get("is_floor")) != bool(line.get("is_floor")):
                    findings.append(
                        err(
                            "L6",
                            location,
                            "reference line for %r declares is_floor=%r while the roof declares "
                            "is_floor=%r." % (vid, line.get("is_floor"), env.get("is_floor")),
                            "mirror the roof's own flag, so a line that is really a floor is drawn "
                            "and labelled as one.",
                        )
                    )
    return findings


# --------------------------------------------------------------------------------------
# L7
# --------------------------------------------------------------------------------------

HEADLINE_SECTIONS = ("abstract", "headline", "executive_summary", "executive-summary")


def rule_l7(ctx):
    findings = []
    bundle = ctx.bundle
    report = ctx.report
    runs = bundle.data.get("runs") or []

    for eid, env in sorted(bundle.envelopes.items()):
        rid = env.get("run_id")
        if rid not in bundle.runs:
            findings.append(
                err(
                    "L7",
                    "value %r" % eid,
                    "run_id %r is not in runs[]. Declared runs: %s."
                    % (rid, ", ".join(sorted(bundle.runs)) or "none"),
                    "either declare the run or re-measure. A value from an undeclared run is how a "
                    "figure from an older run reaches a current report. (D3, D10)",
                )
            )
        env_mode = env.get("mode")
        run_mode = (bundle.runs.get(rid) or {}).get("mode")
        if env_mode and run_mode and env_mode != run_mode and not env.get("mode_override_reason"):
            findings.append(
                err(
                    "L7",
                    "value %r" % eid,
                    "mode %r differs from run %r mode %r with no mode_override_reason."
                    % (env_mode, rid, run_mode),
                    "state why this value was taken in a different mode from the rest of its run. A "
                    "silent mismatch is how a floor gets printed as a ceiling.",
                )
            )

    primaries = [r for r in runs if r.get("primary") is True]
    if len(primaries) != 1:
        findings.append(
            err(
                "L7",
                "runs[]",
                "%d runs are marked primary; exactly one must be." % len(primaries),
                "mark the one run every headline figure comes from. Anything sourced elsewhere is "
                "named as such at the point of use.",
            )
        )
    primary_id = primaries[0]["id"] if len(primaries) == 1 else None

    rendered = ctx.rendered_ids()
    referenced_runs = {
        (bundle.envelopes.get(eid) or {}).get("run_id")
        for eid in rendered
        if eid in bundle.envelopes
    }
    referenced_runs.discard(None)
    declared_runs = set(bundle.runs)
    for rid in sorted(declared_runs - referenced_runs):
        findings.append(
            err(
                "L7",
                "run %r" % rid,
                "declared in runs[] but no rendered value comes from it.",
                "remove the run, or render the values it produced. A declared run that contributes "
                "nothing overstates the evidence base. (D10)",
            )
        )
    for rid in sorted(referenced_runs - declared_runs):
        findings.append(
            err(
                "L7",
                "run %r" % rid,
                "referenced by a rendered value but not declared in runs[].",
                "declare the run with its window, harness version and fingerprints.",
            )
        )

    # headline values come from the primary run, or are attributed in place
    fom = bundle.data.get("figures_of_merit") or {}
    headline_ids = set()
    primary_fom = fom.get("primary") or {}
    if primary_fom.get("value_id"):
        headline_ids.add(primary_fom["value_id"])
    headline_ids.update(primary_fom.get("bound_by") or [])
    headline_ids.update(fom.get("secondary") or [])
    headline_ids.update(fom.get("efficiency") or [])
    for span in report.value_spans:
        if span["section"] in HEADLINE_SECTIONS:
            headline_ids.add(span["value_id"])
    blends = bundle.data.get("cross_run_blends") or []
    blend_targets = {b.get("target") for b in blends}
    for eid in sorted(headline_ids):
        env = bundle.envelopes.get(eid)
        if env is None or primary_id is None:
            continue
        if env.get("run_id") == primary_id:
            continue
        attributed = False
        for span in report.value_spans:
            if span["value_id"] != eid:
                continue
            text = report.near_text(span) or ""
            if env.get("run_id") in text or span["attrs"].get("data-run-id") == env.get("run_id"):
                attributed = True
        if not attributed and not blend_targets:
            findings.append(
                err(
                    "L7",
                    "value %r" % eid,
                    "is a headline value from run %r, not the primary run %r, and is not "
                    "attributed at the point of use." % (env.get("run_id"), primary_id),
                    "attribute the run where the value is printed, and register the pair in "
                    "cross_run_blends[] with why_permissible. Not legitimate: a value from "
                    "whichever run produced the more attractive number.",
                )
            )

    # document control renders the register, and no sentence asserts a single run
    register_rows = [
        m for m in report.marks_by_attr("data-run-id") if m["tag"] in ("tr", "li", "div")
    ]
    register_marks = report.marks_by_attr("data-run-register")
    if not register_marks:
        findings.append(
            skipped(
                "L7",
                "the generated run register",
                "no element carries data-run-register, so the row-per-run check could not run.",
                'mark the register table with data-run-register="true" and each row with '
                "data-run-id.",
            )
        )
    else:
        if len(register_rows) != len(runs):
            findings.append(
                err(
                    "L7",
                    "run register",
                    "renders %d rows for %d declared runs." % (len(register_rows), len(runs)),
                    "generate the register from runs[]. The count of contributing runs is data, not "
                    "narrative. (D10)",
                )
            )
        for run in runs:
            for field in ("window", "harness_version", "machine_fingerprint", "role"):
                path = "runs.%s.%s" % (run.get("id"), field)
                if not any(m["value"] == path for m in report.marks_by_attr("data-field")):
                    findings.append(
                        err(
                            "L7",
                            "run register, run %r" % run.get("id"),
                            "does not render %s." % field,
                            "render one row per run with id, window, role, mode, harness_version "
                            "and comparability_fingerprint.hash, each as a data-field. (D10)",
                        )
                    )
    if len(runs) > 1:
        for m in SINGLE_RUN_ASSERTION.finditer(report.prose):
            if report.is_interpolated(m.start(), m.end()):
                continue
            findings.append(
                err(
                    "L7",
                    "section %r, prose offset %d" % (report.section_at(m.start()), m.start()),
                    "asserts %r while %d run ids are referenced by rendered values: %s."
                    % (m.group(0), len(referenced_runs), ", ".join(sorted(referenced_runs))),
                    "generate the sentence from runs[]. The count of contributing runs is data, not "
                    "narrative. (D10)",
                )
            )

    # blends declared and disclosed in place
    runs_of_target = {}
    for span in report.value_spans:
        env = bundle.envelopes.get(span["value_id"]) or {}
        if not env:
            continue
        runs_of_target.setdefault(span["section"], set()).add(env.get("run_id"))
    for table in report.tables:
        target = table["figure"] or table["id"]
        if not target:
            continue
        ids = set()
        for row in table["rows"]:
            for cell in row:
                vid = cell["attrs"].get("data-value-id") or (
                    cell["value_ids"][0] if cell["value_ids"] else None
                )
                if vid:
                    ids.add(vid)
        runs_here = {
            (bundle.envelopes.get(v) or {}).get("run_id") for v in ids if v in bundle.envelopes
        }
        runs_here.discard(None)
        if len(runs_here) > 1 and target not in blend_targets:
            findings.append(
                err(
                    "L7",
                    "table/figure %r" % target,
                    "mixes runs %s but is not declared in cross_run_blends[]."
                    % ", ".join(sorted(runs_here)),
                    "declare the blend with its run ids and why_permissible, and disclose it where "
                    "it is used. A blend disclosed only in an appendix is how D4 survived two "
                    "review rounds.",
                )
            )
    for blend in blends:
        if blend.get("disclosed_in_place") is False:
            findings.append(
                err(
                    "L7",
                    "cross_run_blends target %r" % blend.get("target"),
                    "disclosed_in_place is false.",
                    "state the blend at the point of use, not only in the provenance section.",
                )
            )
        else:
            target = blend.get("target")
            disclosed = any(
                m["value"] == target for m in report.marks_by_attr("data-blend-disclosure")
            )
            if not disclosed:
                findings.append(
                    err(
                        "L7",
                        "cross_run_blends target %r" % target,
                        "no data-blend-disclosure marks the blend where it is used.",
                        'render the disclosure in place and mark it data-blend-disclosure="%s".'
                        % target,
                    )
                )

    # rebuild test: what disappears when a run is removed must be what it declares it produced
    for run in runs:
        rid = run.get("id")
        vanish = sorted(
            eid
            for eid in rendered
            if eid in bundle.envelopes and bundle.envelopes[eid].get("run_id") == rid
        )
        produced = [str(p) for p in run.get("produced") or []]
        unmatched_entries = []
        matched_ids = set()
        for entry in produced:
            entry_norm = entry.strip().lower()
            hits = []
            for eid in vanish:
                label = bundle.label_of(eid).lower()
                spaced = eid.replace("_", " ")
                if (
                    entry_norm == eid
                    or entry_norm == spaced
                    or entry_norm in spaced
                    or spaced in entry_norm
                    or entry_norm in label
                    or label in entry_norm
                ):
                    hits.append(eid)
            if hits:
                matched_ids.update(hits)
            else:
                unmatched_entries.append(entry)
        if unmatched_entries:
            findings.append(
                err(
                    "L7",
                    "run %r" % rid,
                    "produced[] claims %s, which matches no rendered value."
                    % ", ".join(repr(e) for e in unmatched_entries),
                    "correct produced[] or render the values it names. (D10)",
                )
            )
        orphans = sorted(set(vanish) - matched_ids)
        if orphans:
            findings.append(
                err(
                    "L7",
                    "run %r" % rid,
                    "removing it also removes rendered values not listed in produced[]: %s."
                    % ", ".join(orphans),
                    "correct produced[] or move the values. A run whose declared contribution does "
                    "not match its actual contribution understates provenance. (D10)",
                )
            )
    return findings


# --------------------------------------------------------------------------------------
# L8
# --------------------------------------------------------------------------------------


def _pair_conditions_diff(bundle, a_id, b_id):
    a = bundle.envelopes.get(a_id) or {}
    b = bundle.envelopes.get(b_id) or {}
    ca, cb = bundle.conditions_of(a), bundle.conditions_of(b)
    keys = set(ca) | set(cb) | set(COMPARABILITY_KEYS)
    diffs = []
    for key in sorted(keys):
        if key not in ca and key not in cb:
            continue
        if ca.get(key) != cb.get(key):
            diffs.append((key, ca.get(key), cb.get(key)))
    return diffs


def _fingerprints_differ(bundle, a_id, b_id):
    a = bundle.envelopes.get(a_id) or {}
    b = bundle.envelopes.get(b_id) or {}
    ra = bundle.runs.get(a.get("run_id")) or {}
    rb = bundle.runs.get(b.get("run_id")) or {}
    ha = ((ra.get("comparability_fingerprint") or {}).get("hash"))
    hb = ((rb.get("comparability_fingerprint") or {}).get("hash"))
    if ha is None or hb is None:
        return None
    return (ha, hb) if ha != hb else None


def _aggregation_incompatible(bundle, a_id, b_id):
    a = (bundle.envelopes.get(a_id) or {}).get("aggregation") or {}
    b = (bundle.envelopes.get(b_id) or {}).get("aggregation") or {}
    if not a and not b:
        return None
    if a.get("over") != b.get("over") or a.get("method") != b.get("method"):
        return (
            "%s/%s" % (a.get("over"), a.get("method")),
            "%s/%s" % (b.get("over"), b.get("method")),
        )
    return None


def _floor_mismatch(bundle, a_id, b_id):
    def is_floorish(eid):
        env = bundle.envelopes.get(eid) or {}
        return bool(env.get("inherits_floor")) or (
            bundle.envelope_group.get(eid) == "roofs" and bool(env.get("is_floor"))
        )

    return is_floorish(a_id) != is_floorish(b_id)


def _detect_pairs(ctx):
    """Every comparison the linter can see, as (a, b, how, location, block_text, annotated)."""
    bundle = ctx.bundle
    report = ctx.report
    pairs = []
    for mark in report.marks_by_attr("data-cmp"):
        parts = [p.strip() for p in mark["value"].split(",")]
        if len(parts) == 2:
            pairs.append((parts[0], parts[1], "rendered {{cmp}}", "section %r" % mark["section"],
                          "", False, mark))
    for mark in report.marks_by_attr("data-xcmp"):
        parts = [p.strip() for p in mark["value"].split(",")]
        if len(parts) == 2:
            pairs.append((parts[0], parts[1], "rendered {{xcmp}}", "section %r" % mark["section"],
                          "", True, mark))
    if ctx.authored.present:
        for marker in ctx.authored.markers:
            if marker["kind"] not in ("cmp", "xcmp"):
                continue
            parts = [p.strip() for p in marker["target"].split(",")]
            if len(parts) == 2:
                pairs.append(
                    (
                        parts[0],
                        parts[1],
                        "authored %s" % marker["raw"],
                        "section %r, authored offset %d" % (marker["section"], marker["start"]),
                        "",
                        marker["kind"] == "xcmp",
                        marker,
                    )
                )
    # comparative language adjacent to two value spans in one block
    by_block = {}
    for span in report.value_spans:
        if span["block"] is None:
            continue
        by_block.setdefault(span["block"], []).append(span)
    for bidx, spans in by_block.items():
        block = report.blocks[bidx]
        if not COMPARATIVE_LANGUAGE.search(block["text"] or ""):
            continue
        uniq = []
        for span in spans:
            if span["value_id"] not in uniq:
                uniq.append(span["value_id"])
        for i in range(len(uniq)):
            for j in range(i + 1, len(uniq)):
                pairs.append(
                    (
                        uniq[i],
                        uniq[j],
                        "comparative language in one block (%r)"
                        % (COMPARATIVE_LANGUAGE.search(block["text"]).group(0)),
                        "section %r, prose offset %d" % (block["section"], block["start"]),
                        block["text"],
                        False,
                        None,
                    )
                )
    # two values in one table row
    for table in report.tables:
        for r, row in enumerate(table["rows"]):
            ids = []
            for cell in row:
                vid = cell["attrs"].get("data-value-id") or (
                    cell["value_ids"][0] if cell["value_ids"] else None
                )
                if vid and vid not in ids:
                    ids.append(vid)
            for i in range(len(ids)):
                for j in range(i + 1, len(ids)):
                    pairs.append(
                        (
                            ids[i],
                            ids[j],
                            "same table row",
                            "table %r row %d" % (table["id"] or table["figure"], r),
                            " ".join(c["text_joined"] for c in row),
                            False,
                            None,
                        )
                    )
    out = []
    for a, b, how, where, text, annotated, mark in pairs:
        if a == b:
            continue
        out.append((a, b, how, where, text, annotated, mark))
    return out


def rule_l8(ctx):
    findings = []
    bundle = ctx.bundle
    if not ctx.authored.present:
        findings.append(
            skipped(
                "L8",
                "comparative language in authored text",
                "no authored text was supplied, so comparisons were detected only in the rendered "
                "output. A comparison the renderer reworded may have been missed.",
                "write <run-dir>/authored.json.",
            )
        )
    seen = set()
    for a, b, how, where, text, annotated, mark in _detect_pairs(ctx):
        key = (tuple(sorted((a, b))), where)
        if key in seen:
            continue
        seen.add(key)
        for missing in [x for x in (a, b) if x not in bundle.envelopes]:
            findings.append(
                err(
                    "L8",
                    where,
                    "compared value %r does not resolve to an envelope." % missing,
                    "reference values that exist.",
                )
            )
        if a not in bundle.envelopes or b not in bundle.envelopes:
            continue
        env_a = bundle.envelopes[a]
        env_b = bundle.envelopes[b]

        prohibited = None
        for entry in env_a.get("not_comparable_with") or []:
            if entry.get("id") == b:
                prohibited = entry
        for entry in env_b.get("not_comparable_with") or []:
            if entry.get("id") == a:
                prohibited = entry
        if prohibited is not None:
            findings.append(
                err(
                    "L8",
                    where,
                    "value %r lists not_comparable_with %r (%r), and they are set beside each other "
                    "by %s." % (a, b, prohibited.get("why"), how),
                    "remove the comparison. A prohibition recorded on the value outranks a "
                    "paragraph that wants to make the comparison anyway. (D4)",
                )
            )
            continue

        diffs = _pair_conditions_diff(bundle, a, b)
        fingerprints = _fingerprints_differ(bundle, a, b)
        agg = _aggregation_incompatible(bundle, a, b)
        floor = _floor_mismatch(bundle, a, b)
        problems = []
        if diffs:
            problems.append(
                "differing conditions: "
                + "; ".join("%s (%r vs %r)" % (k, x, y) for k, x, y in diffs)
            )
        if fingerprints:
            problems.append(
                "run comparability_fingerprint (%s vs %s)" % fingerprints
            )
        if agg:
            problems.append("aggregation scope (%s vs %s)" % agg)
        if floor:
            problems.append(
                "one side is a floor (or inherits one) and the other is not, so one percentage is "
                "an upper bound on achievement and the other is not"
            )

        blend_ok = False
        for blend in bundle.data.get("cross_run_blends") or []:
            rids = set(blend.get("run_ids") or [])
            if {env_a.get("run_id"), env_b.get("run_id")} <= rids:
                blend_ok = True
        if fingerprints and blend_ok:
            problems = [p for p in problems if not p.startswith("run comparability")]

        if annotated:
            named = []
            differing_keys = [k for k, _, _ in diffs]
            attr_keys = ""
            if mark is not None and isinstance(mark, dict):
                attr_keys = (mark.get("attrs") or {}).get("data-differing-keys", "") or ""
                if not attr_keys:
                    attr_keys = (mark.get("options") or {}).get("why", "") or ""
            for key in differing_keys:
                if key not in attr_keys and key not in (text or ""):
                    named.append(key)
            if named:
                findings.append(
                    err(
                        "L8",
                        where,
                        "cross-condition comparison of %r and %r does not name the differing key(s) "
                        "%s in the rendered output." % (a, b, ", ".join(named)),
                        "the annotation must change what is PRINTED, not only what the source says: "
                        "render the differing keys with the comparison. (D4)",
                    )
                )
            continue

        if problems:
            findings.append(
                err(
                    "L8",
                    where,
                    "compares %r (%s %s, run %s) with %r (%s %s, run %s) by %s. %s"
                    % (
                        a,
                        fmt(as_float(env_a.get("value"))) if as_float(env_a.get("value")) is not None else env_a.get("value"),
                        env_a.get("unit"),
                        env_a.get("run_id"),
                        b,
                        fmt(as_float(env_b.get("value"))) if as_float(env_b.get("value")) is not None else env_b.get("value"),
                        env_b.get("unit"),
                        env_b.get("run_id"),
                        how,
                        "; ".join(problems) + ".",
                    ),
                    "compare like for like, or use {{xcmp:%s,%s|why=...}} so the difference in "
                    "conditions is printed where the comparison is made. (D4)" % (a, b),
                )
            )

    # comparable_with claims are verified, never trusted
    for eid, env in sorted(bundle.envelopes.items()):
        for other in env.get("comparable_with") or []:
            if other not in bundle.envelopes:
                findings.append(
                    err(
                        "L8",
                        "value %r" % eid,
                        "comparable_with names %r, which does not resolve." % other,
                        "reference a value that exists.",
                    )
                )
                continue
            diffs = _pair_conditions_diff(bundle, eid, other)
            if diffs:
                findings.append(
                    err(
                        "L8",
                        "value %r" % eid,
                        "claims comparable_with %r, but the conditions differ: %s."
                        % (other, "; ".join("%s (%r vs %r)" % d for d in diffs)),
                        "fix the conditions or withdraw the claim. The linter verifies the claim "
                        "rather than trusting it.",
                    )
                )
    return findings


# --------------------------------------------------------------------------------------
# L9
# --------------------------------------------------------------------------------------


def _evidence_targets(bundle):
    """Every evidence reference that could legitimately resolve, with its owner."""
    targets = {}
    tool = bundle.data.get("tool") or {}
    if tool.get("source_url") and tool.get("published") is True:
        targets["tool.source_url"] = {"kind": "harness source", "third_party": False}
    for run in bundle.data.get("runs") or []:
        for art in run.get("artifacts") or []:
            targets["runs.%s.artifacts.%s" % (run.get("id"), art)] = {
                "kind": "run artefact",
                "third_party": False,
            }
        if run.get("artifacts"):
            targets.setdefault(
                "runs.%s.artifacts" % run.get("id"),
                {"kind": "run artefacts", "third_party": False},
            )
    for eid, env in bundle.envelopes.items():
        if env.get("cases"):
            targets["%s.cases" % eid] = {"kind": "published case set", "third_party": False}
        prov = env.get("provenance") or {}
        if prov.get("citation"):
            targets["%s.provenance.citation" % eid] = {
                "kind": "citation",
                "third_party": True,
            }
    gen = ((bundle.data.get("workload") or {}).get("generation")) or {}
    if gen.get("template") and gen.get("seed") is not None:
        targets["workload.generation"] = {"kind": "generation recipe", "third_party": False}
    for key, entry in (bundle.data.get("x_evidence") or {}).items():
        targets["x_evidence.%s" % key] = {
            "kind": entry.get("kind", "registered evidence"),
            "third_party": bool(entry.get("owner_is_third_party")),
        }
    return targets


def rule_l9(ctx):
    findings = []
    bundle = ctx.bundle
    report = ctx.report
    tool = bundle.data.get("tool") or {}
    targets = _evidence_targets(bundle)

    ev_marks = report.marks_by_attr("data-evidence")
    for mark in ev_marks:
        if mark["value"] not in targets:
            findings.append(
                err(
                    "L9",
                    "section %r, evidence %r" % (mark["section"], mark["value"]),
                    "the evidence reference does not resolve to a real target. Resolvable targets: "
                    "%s." % (", ".join(sorted(targets)) or "none"),
                    "point it at a run artefact, the harness source with tool.published true, a "
                    "published case set, the workload generation recipe with its seed, a provenance "
                    "citation, or an entry of x_evidence. If a claim's evidence has nowhere to "
                    "resolve, EXTEND the bundle with an evidence entry; do not soften the sentence.",
                )
            )

    claim_re = re.compile(
        r"\b(" + "|".join(re.escape(word) for word in CLAIM_WORDS) + r")\b", re.I
    )
    for m in claim_re.finditer(report.prose):
        if report.is_interpolated(m.start(), m.end()):
            continue
        sentence = report.sentence_at(m.start(), m.end())
        word = m.group(1).lower()
        negated = bool(NEGATION.search(sentence or ""))
        block = report.block_at(m.start())
        in_quote = '"' in (sentence or "") or "“" in (sentence or "")
        near_evidence = any(
            block is not None and mark["block"] == block["idx"] for mark in ev_marks
        )
        if negated or in_quote:
            continue
        if not near_evidence:
            state = (
                "tool.published = %r, tool.source_url = %r"
                % (tool.get("published"), tool.get("source_url") or "absent")
            )
            findings.append(
                err(
                    "L9",
                    "section %r, prose offset %d" % (report.section_at(m.start()), m.start()),
                    "claim word %r with no adjacent evidence reference. %s" % (word, state),
                    "attach {{ev:...}} at the point of the claim, or print the true weaker "
                    "sentence: for an unpublished harness that is 'the harness is not obtainable by "
                    "a reader, so these results are not independently reproducible'. (D9)",
                )
            )
            continue
        if word in PUBLICATION_CLAIM_WORDS and tool.get("published") is not True:
            findings.append(
                err(
                    "L9",
                    "section %r, prose offset %d" % (report.section_at(m.start()), m.start()),
                    "asserts %r while tool.published is %r." % (word, tool.get("published")),
                    "every sentence about publication is generated from tool.published and "
                    "tool.source_url, so two sections cannot disagree. Publish the harness and set "
                    "the field, or print the weaker sentence. (D9)",
                )
            )
        if word == "independently":
            third_party = [name for name, meta in targets.items() if meta.get("third_party")]
            owned = [
                mark["value"]
                for mark in ev_marks
                if block is not None and mark["block"] == block["idx"]
            ]
            if not any(targets.get(o, {}).get("third_party") for o in owned):
                findings.append(
                    err(
                        "L9",
                        "section %r, prose offset %d" % (report.section_at(m.start()), m.start()),
                        "claims independence, but the adjacent evidence (%s) is owned by the "
                        "report's own author. Third-party evidence available: %s."
                        % (", ".join(owned) or "none", ", ".join(third_party) or "none"),
                        "for this to become true, someone who is not the author must run the "
                        "harness and the result must be registered in x_evidence with "
                        'owner_is_third_party true. Until then print: "no independent party has run '
                        'this harness".',
                    )
                )

    # negative claims when the field says the opposite
    if tool.get("published") is True:
        for m in claim_re.finditer(report.prose):
            if report.is_interpolated(m.start(), m.end()):
                continue
            if m.group(1).lower() not in PUBLICATION_CLAIM_WORDS:
                continue
            sentence = report.sentence_at(m.start(), m.end())
            if NEGATION.search(sentence or ""):
                findings.append(
                    err(
                        "L9",
                        "section %r, prose offset %d" % (report.section_at(m.start()), m.start()),
                        "denies publication (%r) while tool.published is true." % sentence[:120],
                        "generate every publication sentence from tool.published. Two sections "
                        "cannot disagree, because neither section holds the fact. (D9)",
                    )
                )

    for entry in bundle.data.get("version_history") or []:
        verified = entry.get("verified_by")
        if entry.get("measured_values_moved") or entry.get("run_provenance_changed"):
            if not verified or len(str(verified).strip()) < 12:
                findings.append(
                    err(
                        "L9",
                        "version_history entry %r" % entry.get("version"),
                        "claims values or provenance moved but verified_by does not name a "
                        "procedure (%r)." % verified,
                        "name the procedure: a numeric diff of both builds, a rebuild with each run "
                        "removed, or similar. Verified by assertion is not verified.",
                    )
                )
    if not report.value_spans and not ev_marks:
        findings.append(
            skipped(
                "L9",
                "evidence adjacency",
                "the rendered output carries no provenance marks at all, so claim adjacency could "
                "only be judged from the prose.",
                "render evidence references as data-evidence marks.",
            )
        )
    return findings


# --------------------------------------------------------------------------------------
# L10
# --------------------------------------------------------------------------------------


def _version_key(version):
    parts = re.findall(r"\d+", str(version))
    return tuple(int(p) for p in parts) or (0,)


def rule_l10(ctx):
    findings = []
    bundle = ctx.bundle
    report = ctx.report
    history = bundle.data.get("version_history") or []
    if not history:
        findings.append(
            err(
                "L10",
                "version_history",
                "the history is empty.",
                "add an entry for this edition. A report that cannot say what changed since the "
                "edition a reader cited is not a report they can keep using. (D7)",
            )
        )
        return findings

    building = ctx.args.version
    source = "--version"
    if not building:
        marks = [m for m in report.marks_by_attr("data-ver") if m["value"] in ("version", "")]
        for mark in report.marks_by_attr("data-ver"):
            if mark["value"] == "version":
                text = report.prose[mark["start"] : mark["start"] + 40]
                found = TYPED_VERSION.search(text)
                if found:
                    building = found.group(0)
                    source = "the rendered data-ver span"
                    break
    if not building:
        found, value = resolve_bundle_path(bundle, "x_report.version")
        if found and value:
            building = str(value)
            source = "bundle x_report.version"
    if not building:
        building = str(history[0].get("version"))
        source = "version_history[0] as a fallback"
        findings.append(
            skipped(
                "L10",
                "the identity of the version being built",
                "nothing named the version under construction, so the newest history entry was "
                "assumed to be it. If the build is actually a later edition, its missing entry was "
                "NOT detected.",
                'render <span data-ver="version">X.Y</span>, or pass --version, or set '
                "x_report.version in the bundle.",
            )
        )

    versions = [str(e.get("version")) for e in history]
    if building not in versions:
        findings.append(
            err(
                "L10",
                "version_history",
                "no entry for the version being built (%s, from %s). Latest entry: %s (%s)."
                % (building, source, versions[0], history[0].get("date")),
                "add the entry before building. (D7)",
            )
        )
    duplicates = sorted({v for v in versions if versions.count(v) > 1})
    if duplicates:
        findings.append(
            err(
                "L10",
                "version_history",
                "duplicate version(s): %s." % ", ".join(duplicates),
                "one entry per edition.",
            )
        )
    for entry in history:
        if _version_key(entry.get("version")) > _version_key(building):
            findings.append(
                err(
                    "L10",
                    "version_history entry %r" % entry.get("version"),
                    "is later than the version being built (%s)." % building,
                    "remove the future entry, or correct the version being built.",
                )
            )

    by_version = {str(e.get("version")): e for e in history}
    roots = [e for e in history if e.get("previous_version") in (None, "", "null")]
    if len(roots) != 1:
        findings.append(
            err(
                "L10",
                "version_history",
                "%d entries declare previous_version null; exactly one must." % len(roots),
                "make the history a chain: every entry names its predecessor, and exactly one has "
                "none. (D7)",
            )
        )
    if roots:
        # walk forward from the root by following successors
        successors = {}
        for entry in history:
            prev = entry.get("previous_version")
            if prev:
                successors.setdefault(str(prev), []).append(entry)
        visited = []
        node = roots[0]
        seen_versions = set()
        while node is not None:
            version = str(node.get("version"))
            if version in seen_versions:
                findings.append(
                    err(
                        "L10",
                        "version_history",
                        "the chain revisits %s: previous_version forms a loop." % version,
                        "repair previous_version.",
                    )
                )
                break
            seen_versions.add(version)
            visited.append(node)
            nexts = successors.get(version) or []
            if len(nexts) > 1:
                findings.append(
                    err(
                        "L10",
                        "version_history",
                        "more than one entry claims to follow %s: %s."
                        % (version, ", ".join(str(n.get("version")) for n in nexts)),
                        "a chain has one successor per entry.",
                    )
                )
            node = nexts[0] if nexts else None
        if len(visited) != len(history):
            unreachable = sorted(
                set(versions) - {str(v.get("version")) for v in visited}, key=_version_key
            )
            findings.append(
                err(
                    "L10",
                    "version_history",
                    "chain walk from the null-predecessor entry (%s) visits %d of %d entries. "
                    "Unreachable: %s."
                    % (
                        roots[0].get("version"),
                        len(visited),
                        len(history),
                        ", ".join(unreachable),
                    ),
                    "repair previous_version so the chain covers every edition. A missing edition "
                    "must break the walk instead of passing unnoticed. (D7)",
                )
            )
        last_key = None
        last_date = None
        for entry in visited:
            key = _version_key(entry.get("version"))
            if last_key is not None and key <= last_key:
                findings.append(
                    err(
                        "L10",
                        "version_history entry %r" % entry.get("version"),
                        "does not sort after its predecessor along the chain.",
                        "versions must increase monotonically along the chain. (D7)",
                    )
                )
            date = str(entry.get("date") or "")
            if last_date is not None and date and date < last_date:
                findings.append(
                    err(
                        "L10",
                        "version_history entry %r" % entry.get("version"),
                        "is dated %s, before its predecessor's %s." % (date, last_date),
                        "dates must not decrease along the chain. (D7)",
                    )
                )
            last_key, last_date = key, date or last_date
        for entry in history:
            prev = entry.get("previous_version")
            if prev and str(prev) not in by_version:
                findings.append(
                    err(
                        "L10",
                        "version_history entry %r" % entry.get("version"),
                        "names previous_version %r, which is not in the history." % prev,
                        "add the missing edition or correct the pointer. (D7)",
                    )
                )

    # measured_values_moved and run_provenance_changed are checked, not trusted
    current = by_version.get(building)
    if current is not None:
        listed = {str(mv.get("value_id")) for mv in current.get("moved_values") or []}
        supersedes_here = {
            eid
            for eid, env in bundle.envelopes.items()
            if str((env.get("supersedes") or {}).get("version") or "") == building
        }
        for eid in sorted(supersedes_here - listed):
            findings.append(
                err(
                    "L10",
                    "version_history entry %r" % building,
                    "value %r declares supersedes for this version but is not listed in "
                    "moved_values." % eid,
                    "list it with both readings and a reason. A silently revised number breaks "
                    "everyone who cited the old one. (D7)",
                )
            )
        if supersedes_here and current.get("measured_values_moved") is not True:
            findings.append(
                err(
                    "L10",
                    "version_history entry %r" % building,
                    "declares measured_values_moved=%r while %d value(s) carry supersedes for this "
                    "version." % (current.get("measured_values_moved"), len(supersedes_here)),
                    "set the flag and list the values. (D7)",
                )
            )
        if ctx.previous_bundle is None:
            findings.append(
                skipped(
                    "L10",
                    "the value diff against the previous edition",
                    "no previous-bundle.json was available, so measured_values_moved could only be "
                    "checked against supersedes fields, not against the previous build's numbers.",
                    "keep the previous edition's bundle at <run-dir>/previous-bundle.json, or pass "
                    "--previous-bundle.",
                )
            )
        else:
            prev_envs = {}
            for group in ("roofs", "measurements", "derived", "assumptions"):
                for env in ctx.previous_bundle.get(group) or []:
                    if env.get("id"):
                        prev_envs[env["id"]] = env
            rendered = ctx.rendered_ids()
            moved = []
            for eid in sorted(rendered):
                if eid not in bundle.envelopes or eid not in prev_envs:
                    continue
                now = bundle.envelopes[eid].get("value")
                was = prev_envs[eid].get("value")
                if isinstance(now, (int, float)) and isinstance(was, (int, float)):
                    if abs(float(now) - float(was)) > 1e-12:
                        moved.append((eid, was, now))
                elif json.dumps(now, sort_keys=True) != json.dumps(was, sort_keys=True):
                    moved.append((eid, "(non-scalar)", "(non-scalar, changed)"))
            unlisted = [m for m in moved if m[0] not in listed]
            if moved and current.get("measured_values_moved") is not True:
                findings.append(
                    err(
                        "L10",
                        "version_history entry %r" % building,
                        "declares measured_values_moved=false but %d rendered value(s) differ from "
                        "the previous build: %s."
                        % (
                            len(moved),
                            ", ".join("%s (%s -> %s)" % (i, fmt(a), fmt(b)) for i, a, b in moved),
                        ),
                        "set the flag and list both readings with a reason. (D7)",
                    )
                )
            elif unlisted:
                findings.append(
                    err(
                        "L10",
                        "version_history entry %r" % building,
                        "value(s) moved since the previous build but are not in moved_values: %s."
                        % ", ".join(
                            "%s (%s -> %s)" % (i, fmt(a), fmt(b)) for i, a, b in unlisted
                        ),
                        "list every moved value with both readings and the reason. (D7)",
                    )
                )
            prev_runs = {r.get("id") for r in ctx.previous_bundle.get("runs") or []}
            if prev_runs != set(bundle.runs) and current.get("run_provenance_changed") is not True:
                findings.append(
                    err(
                        "L10",
                        "version_history entry %r" % building,
                        "the contributing run set changed (%s -> %s) but run_provenance_changed is "
                        "%r."
                        % (
                            ", ".join(sorted(prev_runs)) or "none",
                            ", ".join(sorted(bundle.runs)),
                            current.get("run_provenance_changed"),
                        ),
                        "flag it. An edition can add or drop a run artefact without a printed "
                        "number moving, and a reader tracking reproducibility needs that. (D10)",
                    )
                )

    # typed version strings in prose
    for m in TYPED_VERSION.finditer(report.prose):
        if report.is_interpolated(m.start(), m.end()) or report.in_version_span(m.start(), m.end()):
            continue
        if report.in_citation(m.start(), m.end()) or report.in_verbatim(m.start(), m.end()):
            continue
        findings.append(
            err(
                "L10",
                "section %r, prose offset %d" % (report.section_at(m.start()), m.start()),
                "typed version string %r in prose." % m.group(0),
                "render version facts through {{ver:...}} / data-ver so they come from one source. "
                "(D7, and L1 class A4 read from the other direction)",
            )
        )
    return findings


# --------------------------------------------------------------------------------------
# L11
# --------------------------------------------------------------------------------------


def rule_l11(ctx):
    findings = []
    bundle = ctx.bundle
    report = ctx.report
    workload = bundle.data.get("workload") or {}
    ucd = workload.get("uniqueness_and_cache_defeat") or {}
    counters = ucd.get("cache_counters") or {}

    cache_claims = [
        m
        for m in CACHE_CLAIM.finditer(report.prose)
        if not report.is_interpolated(m.start(), m.end())
    ]
    if cache_claims:
        if not counters:
            findings.append(
                err(
                    "L11",
                    "section %r, prose offset %d"
                    % (report.section_at(cache_claims[0].start()), cache_claims[0].start()),
                    "the text makes %d claim(s) about cache behaviour, but "
                    "uniqueness_and_cache_defeat.cache_counters is absent." % len(cache_claims),
                    "read the counters and record source, resolved_state and interpretation. "
                    "'Unique by construction' is an argument; a counter reading is evidence. If the "
                    "cache resolved to off, say that: it is a different statement, and stating which "
                    "one it is prevents two sections reasoning to different answers. (D9, D12)",
                )
            )
        else:
            for field in ("source", "resolved_state", "interpretation"):
                if not counters.get(field):
                    findings.append(
                        err(
                            "L11",
                            "workload.uniqueness_and_cache_defeat.cache_counters",
                            "%r is absent while the text claims cache behaviour." % field,
                            "record it. resolved_state 'off' is a perfectly good reading and a "
                            "different statement from 'on but defeated'.",
                        )
                    )
            interpretation = str(counters.get("interpretation") or "").strip()
            if interpretation and interpretation not in report.full_text:
                findings.append(
                    err(
                        "L11",
                        "cache counters",
                        "the counters' interpretation is not rendered in the report.",
                        "generate every sentence about cache behaviour from interpretation, so two "
                        "sections cannot reason their way to different answers. (D9)",
                    )
                )
            state = str(counters.get("resolved_state") or "")
            if state and state not in report.full_text:
                findings.append(
                    err(
                        "L11",
                        "cache counters",
                        "resolved_state %r is not rendered." % state,
                        "render the reading, not a paraphrase of it.",
                    )
                )

    # fixed-test-set gates
    for eid, env in sorted(bundle.envelopes.items()):
        if env.get("kind") != "fixed-test-set":
            continue
        cases = env.get("cases") or []
        rendered_here = [s for s in report.value_spans if s["value_id"] == eid]
        if not cases:
            findings.append(
                err(
                    "L11",
                    "value %r" % eid,
                    "kind fixed-test-set with an empty cases[].",
                    "publish every case with its input, acceptance criterion and observed result. A "
                    "gate whose cases are secret is unfalsifiable, and an unfalsifiable pass is "
                    "worth less than a published failure. (D12)",
                )
            )
        withheld = env.get("cases_withheld") or {}
        for case in cases:
            case_text = str(case.get("input") or "")
            published = (
                case_text and case_text in report.full_text
            ) or any(
                m["value"] == str(case.get("id")) for m in report.marks_by_attr("data-case-id")
            )
            if not published and not withheld:
                findings.append(
                    err(
                        "L11",
                        "value %r, case %r" % (eid, case.get("id")),
                        "the case is in the bundle but not rendered in the report.",
                        "publish the cases in the report as well as the bundle: a reader who cannot "
                        "see them cannot object to them. (D12)",
                    )
                )
            for field in ("accept", "observed", "passed"):
                if case.get(field) in (None, ""):
                    findings.append(
                        err(
                            "L11",
                            "value %r, case %r" % (eid, case.get("id")),
                            "case field %r is absent." % field,
                            "every case carries its input, its acceptance criterion, its observed "
                            "result and its pass flag. (D12)",
                        )
                    )
        if rendered_here:
            for span in rendered_here:
                text = span["text"]
                near = report.near_text(span) or ""
                m = re.search(r"(\d+)\s*(?:of|/)\s*(\d+)", text)
                if m:
                    passes, total = int(m.group(1)), int(m.group(2))
                    if total != len(cases):
                        findings.append(
                            err(
                                "L11",
                                "value %r rendered in section %r" % (eid, span["section"]),
                                "renders %r but cases[] holds %d case(s)." % (text, len(cases)),
                                "render the pass count only alongside the real case count and the "
                                "published cases. (D12)",
                            )
                        )
                    real_passes = sum(1 for c in cases if c.get("passed") is True)
                    if passes != real_passes:
                        findings.append(
                            err(
                                "L11",
                                "value %r rendered in section %r" % (eid, span["section"]),
                                "renders %d passes but %d case(s) are flagged passed."
                                % (passes, real_passes),
                                "generate the count from the cases.",
                            )
                        )
                for field in ("licenses", "does_not_license"):
                    claim = str(env.get(field) or "").strip()
                    if claim and claim[:60] not in near:
                        findings.append(
                            err(
                                "L11",
                                "value %r rendered in section %r" % (eid, span["section"]),
                                "%s is not rendered next to the result." % field,
                                "render both what a pass licenses and what it does not, beside the "
                                "result. The gap between them is where an overstated quality claim "
                                "would otherwise live. (D12)",
                            )
                        )
                if STABILITY_CLAIM.search(near):
                    for case in cases:
                        if case.get("repeats") is None or case.get("stable_across_repeats") is None:
                            findings.append(
                                err(
                                    "L11",
                                    "value %r, case %r" % (eid, case.get("id")),
                                    "the section claims repeat stability but the case records no "
                                    "repeats or stability flag.",
                                    "run the case more than once and record it. A gate run once "
                                    "cannot make a stability claim.",
                                )
                            )
            if withheld:
                need = ("reason", "count", "hash", "equivalent_construction")
                for field in need:
                    if not withheld.get(field):
                        findings.append(
                            err(
                                "L11",
                                "value %r" % eid,
                                "cases_withheld is missing %r." % field,
                                "the withheld path requires a reason, a count, a hash of the "
                                "withheld set, and how a reader could construct an equivalent set; "
                                "and the result must render as 'not independently checkable' at "
                                "every appearance.",
                            )
                        )
                for span in rendered_here:
                    if "not independently checkable" not in (report.near_text(span) or "").lower():
                        findings.append(
                            err(
                                "L11",
                                "value %r rendered in section %r" % (eid, span["section"]),
                                "cases are withheld but the result is not rendered as not "
                                "independently checkable.",
                                "render the qualification at every appearance.",
                            )
                        )
        elif cases:
            findings.append(
                skipped(
                    "L11",
                    "the rendered form of gate %r" % eid,
                    "the gate exists in the bundle but no rendered value span references it, so "
                    "whether its pass count is printed with its case count was not checked.",
                    "render the gate result as a value span, or remove the gate from the bundle.",
                )
            )

    # pre-registered bars
    for eid, env in sorted(bundle.envelopes.items()):
        bar = env.get("preregistered_bar")
        if not bar:
            continue
        described_preset = False
        for span in report.value_spans:
            if span["value_id"] != eid:
                continue
            if PRESET_BAR_CLAIM.search(report.near_text(span) or ""):
                described_preset = True
        if described_preset and bar.get("set_before_measurement") is not True:
            findings.append(
                err(
                    "L11",
                    "value %r" % eid,
                    "preregistered_bar has set_before_measurement=%r but the text describes it as a "
                    "pre-set bar." % bar.get("set_before_measurement"),
                    "describe it as a post-hoc observation, or record the date it was actually "
                    "fixed. A residual declared acceptable after it was seen is a rationalisation.",
                )
            )
        if bar.get("set_before_measurement") is True:
            set_on = str(bar.get("set_on") or "")
            run = bundle.runs.get(env.get("run_id")) or {}
            stamp = str(run.get("timestamp") or "")
            if not set_on:
                findings.append(
                    err(
                        "L11",
                        "value %r" % eid,
                        "the bar claims to predate the measurement but records no set_on date.",
                        "record the date the bar was fixed.",
                    )
                )
            elif stamp and set_on > stamp[: len(set_on)]:
                findings.append(
                    err(
                        "L11",
                        "value %r" % eid,
                        "the bar was set on %s, after its run started at %s." % (set_on, stamp),
                        "a bar fixed after the run is a post-hoc observation; describe it as one.",
                    )
                )

    # size control verification, one entry per size actually used
    size_control = workload.get("size_control") or {}
    verification = size_control.get("verification") or []
    used_sizes = set()
    for env in bundle.envelopes.values():
        if env.get("kind") not in ("measured", "derived"):
            continue
        cond = env.get("conditions") or {}
        if cond.get("problem_size") is not None:
            used_sizes.add(str(cond["problem_size"]))
        value = env.get("value")
        if isinstance(value, dict):
            for point in value.get("points") or []:
                at = point.get("at") or {}
                if at.get("problem_size") is not None:
                    used_sizes.add(str(at["problem_size"]))
    verified_sizes = set()
    for entry in verification:
        cond = entry.get("condition") or {}
        if cond.get("problem_size") is not None:
            verified_sizes.add(str(cond["problem_size"]))
        if entry.get("requested") is not None:
            verified_sizes.add(str(entry["requested"]))
        num = as_float(entry.get("requested"))
        if num is not None and num == int(num):
            verified_sizes.add(str(int(num)))
    if used_sizes:
        missing = sorted(used_sizes - verified_sizes)
        if missing:
            findings.append(
                err(
                    "L11",
                    "workload.size_control.verification",
                    "no measured requested-versus-counted check for size(s) %s (checked: %s)."
                    % (", ".join(missing), ", ".join(sorted(verified_sizes)) or "none"),
                    "verify one entry per size actually used, with the system's own counter. An "
                    "approximation that holds at one size can fail at another, and a report that "
                    "checked one size and generalised has checked nothing.",
                )
            )
    if not verification:
        findings.append(
            err(
                "L11",
                "workload.size_control.verification",
                "no verification entries at all.",
                "a requested size is an intention; the system's own counter is the fact. Publish "
                "the gap, per size.",
            )
        )
    return findings


# --------------------------------------------------------------------------------------
# Registry and runner
# --------------------------------------------------------------------------------------

RULES = {
    "L1": rule_l1,
    "L2": rule_l2,
    "L3": rule_l3,
    "L4": rule_l4,
    "L5": rule_l5,
    "L6": rule_l6,
    "L7": rule_l7,
    "L8": rule_l8,
    "L9": rule_l9,
    "L10": rule_l10,
    "L11": rule_l11,
}


def select_rules(spec):
    if not spec:
        return list(RULE_ORDER)
    wanted = []
    for piece in str(spec).replace(" ", "").split(","):
        if not piece:
            continue
        name = piece.upper()
        if name not in RULES:
            raise LinterError(
                "unknown rule %r. Known rules: %s. If a rule was deleted, the tests that prove it "
                "fires will fail, which is the point." % (piece, ", ".join(RULE_ORDER))
            )
        wanted.append(name)
    return wanted


def run_rules(ctx, rules=None):
    findings = []
    for name in select_rules(rules) if not isinstance(rules, list) else rules:
        if name not in RULES:
            raise LinterError("unknown rule %r" % name)
        findings.extend(RULES[name](ctx))
    order = {name: i for i, name in enumerate(RULE_ORDER)}
    findings.sort(key=lambda f: (order.get(f.rule, 99), f.severity != SEV_ERROR, f.location))
    return findings


def build_context(run_dir, report_path, args):
    bundle_path = find_bundle(run_dir)
    with open(bundle_path, "r", encoding="utf-8") as fh:
        try:
            data = json.load(fh)
        except json.JSONDecodeError as exc:
            raise LinterError("bundle %s is not valid JSON: %s" % (bundle_path, exc))
    bundle = Bundle(data, bundle_path)
    if not os.path.isfile(report_path):
        raise LinterError("built report does not exist: %s" % report_path)
    with open(report_path, "r", encoding="utf-8", errors="replace") as fh:
        html = fh.read()
    report = Report(html, report_path)
    authored = load_authored(run_dir)
    allowlist, budget, allowlist_path = load_allowlist(run_dir)
    args.run_dir = run_dir
    return Context(bundle, report, authored, allowlist, budget, allowlist_path, args)


def lint(run_dir, report_path, rules=None, args=None):
    args = args or _default_args()
    ctx = build_context(run_dir, report_path, args)
    return ctx, run_rules(ctx, rules)


class _Args:
    previous_bundle = None
    version = None
    run_dir = None
    max_per_rule = 40
    allow_skipped = False
    json = False


def _default_args():
    return _Args()


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def explain(selected=None):
    lines = []
    lines.append("The eleven rules, and the defect each one exists to prevent.")
    lines.append("")
    lines.append(
        "The governing rule they implement: every number a reader sees is either measured and"
    )
    lines.append(
        "labelled, or a stated derivation from labelled inputs, and it exists exactly once in the"
    )
    lines.append("bundle. A second copy is a copy that will drift.")
    lines.append("")
    for name in selected or RULE_ORDER:
        meta = RULE_META[name]
        lines.append("%s %s  (cites %s)" % (name, meta["slug"], meta["cites"]))
        lines.append("  statement: " + meta["statement"])
        lines.append("  why:       " + meta["rationale"])
        lines.append("")
    lines.append("L1 allowlist (explicit patterns with a context restriction, never a tolerance):")
    for entry in L1_ALLOWLIST:
        lines.append(
            "  %-4s %-24s %s" % (entry["id"], entry["context"], entry["regex"])
        )
        lines.append("       " + entry["why"])
    lines.append("")
    lines.append("L1 false-positive modes the author should expect:")
    for mode in L1_FALSE_POSITIVE_MODES:
        lines.append("  - " + mode)
    lines.append("")
    lines.append(
        "L3 permitted formula constants (scale factors and identities only; anything else must be"
    )
    lines.append("an envelope, which is what D11 was): " + ", ".join(fmt(c) for c in sorted(L3_SCALE_CONSTANTS)))
    lines.append("")
    try:
        doc = outline_reader.load_outline()
        lines.append(
            "report-outline.yaml %s: %d sections, %d archetypes, %d invariants."
            % (
                (doc.get("template") or {}).get("version"),
                len(doc.get("sections") or []),
                len((doc.get("archetypes") or {}).get("items") or []),
                len(outline_reader.invariants(doc)),
            )
        )
        lines.append("Which sections and invariants stand between the report and each defect:")
        for row in (doc.get("defect_index") or {}).get("rows") or []:
            lines.append("  %-22s %s" % (row.get("defect"), row.get("guarded_by")))
    except Exception as exc:  # pragma: no cover - the outline is shipped beside this file
        lines.append("report-outline.yaml could not be read: %s" % exc)
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m template.lint",
        description="Lint a built benchmark report against its run bundle. Eleven rules, each "
        "citing the historical defect it prevents.",
    )
    parser.add_argument("run_dir", nargs="?", help="directory holding bundle.json")
    parser.add_argument("report", nargs="?", help="the rendered report (HTML)")
    parser.add_argument("--rules", help="comma-separated subset, e.g. L1,L3")
    parser.add_argument(
        "--explain", action="store_true", help="print each rule's statement and rationale"
    )
    parser.add_argument("--previous-bundle", help="the previous edition's bundle, for L10")
    parser.add_argument("--version", dest="version", help="the version being built, for L10")
    parser.add_argument(
        "--allow-skipped",
        action="store_true",
        help="exit 0 when nothing failed but something could not be checked",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable findings")
    parser.add_argument(
        "--max-per-rule",
        type=int,
        default=40,
        help="how many findings to print per rule (all are counted)",
    )
    args = parser.parse_args(argv)

    try:
        selected = select_rules(args.rules)
    except LinterError as exc:
        sys.stderr.write("lint: %s\n" % exc)
        return EXIT_CANNOT_RUN

    if args.explain and not (args.run_dir and args.report):
        print(explain(selected))
        return EXIT_OK

    if not args.run_dir or not args.report:
        parser.print_usage(sys.stderr)
        sys.stderr.write("lint: a run directory and a built report are required\n")
        return EXIT_CANNOT_RUN

    if args.explain:
        print(explain(selected))
        print("")

    try:
        ctx, findings = lint(args.run_dir, args.report, selected, args)
    except LinterError as exc:
        sys.stderr.write("lint: cannot run: %s\n" % exc)
        return EXIT_CANNOT_RUN

    errors = [f for f in findings if f.severity == SEV_ERROR]
    skips = [f for f in findings if f.severity == SEV_SKIPPED]

    if args.json:
        print(
            json.dumps(
                {
                    "run_dir": args.run_dir,
                    "report": args.report,
                    "bundle": ctx.bundle.path,
                    "rules": selected,
                    "errors": len(errors),
                    "skipped": len(skips),
                    "findings": [f.as_dict() for f in findings],
                    "allowlisted": sorted({"%s (%s)" % h for h in ctx.allowlisted_hits}),
                },
                indent=2,
            )
        )
    else:
        print("lint: bundle %s" % ctx.bundle.path)
        print("lint: report %s" % args.report)
        print(
            "lint: authored text %s"
            % (ctx.authored.source if ctx.authored.present else "NOT SUPPLIED")
        )
        print("lint: rules %s" % ",".join(selected))
        print("")
        shown = {}
        for finding in findings:
            shown[finding.rule] = shown.get(finding.rule, 0) + 1
            if shown[finding.rule] > args.max_per_rule:
                continue
            print(finding.render())
            print("")
        for rule, count in sorted(shown.items(), key=lambda kv: RULE_ORDER.index(kv[0])):
            if count > args.max_per_rule:
                print(
                    "%s: %d further findings not printed (--max-per-rule %d)"
                    % (rule, count - args.max_per_rule, args.max_per_rule)
                )
        print("-" * 78)
        per_rule = {}
        for finding in findings:
            entry = per_rule.setdefault(finding.rule, [0, 0])
            entry[0 if finding.severity == SEV_ERROR else 1] += 1
        for name in selected:
            e, s = per_rule.get(name, [0, 0])
            state = "PASS" if not e and not s else ("FAIL" if e else "INCOMPLETE")
            print(
                "%-4s %-26s %-10s errors %-4d not-checked %d"
                % (name, RULE_META[name]["slug"], state, e, s)
            )
        if ctx.allowlisted_hits:
            print("")
            print("allowlisted literals (printed in the report's audit appendix):")
            for literal, entry_id in sorted(set(ctx.allowlisted_hits)):
                print("  %-12s %s" % (literal, entry_id))
        print("")
        print("%d error(s), %d not-checked" % (len(errors), len(skips)))

    if errors:
        return EXIT_VIOLATION
    if skips and not args.allow_skipped:
        return EXIT_SKIPPED
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
