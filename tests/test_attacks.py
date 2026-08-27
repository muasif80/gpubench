#!/usr/bin/env python3
"""The 23 ways this tool was once fooled. Run them all.

WHY THIS FILE EXISTS, and why it is not a duplicate of the other suites.

Two adversarial audits went at gpubench in one day and beat it 23 times. The verdict on both was
"defeated". Every one of the 23 was the SAME mistake wearing a different costume:

    A CHECK THAT READS A DECLARATION INSTEAD OF THE ARTEFACT.

The manifest said a figure had a table view, and nothing opened the document. The manifest said the
quality gate passed, and nothing opened the result file. The manifest listed five hand-written
prose blocks, and the report shipped a hundred thousand characters no check had jurisdiction over,
into which five fabricated headline figures were injected with no effect on the exit code at all.
The probe declared a Poisson arrival process, and nothing measured whether one had happened.

Every one of those 23 is fixed. The individual fixes are covered piecemeal across
tests/test_serving.py, tests/test_verify.py and tests/test_gate.py, which is the right place for
them: those suites test the mechanisms. What did NOT exist anywhere was a single list saying "these
are the 23 attacks, and here is each one still failing to land". Without that list a refactor
silently reopens a hole and the tool merely LOOKS hardened, which is strictly worse than being
visibly unhardened: an unarmed gate at least reads as unarmed.

HOW THESE TESTS ARE BUILT, and why it matters more than the count.

Where an attack was originally end to end, the test is end to end. A throwaway content module is
written into a temp directory, the real CLI is driven over it, and the assertions are on THE EXIT
CODE and on WHAT DID OR DID NOT REACH DISK. Asserting on a function's return value where the
original attack shipped a FILE is exactly the substitution that created these holes in the first
place, so it is not done here.

Where an attack IS already covered by a function-level test elsewhere, this suite drives it through
a DIFFERENT surface on purpose: the CLI rather than the function, the written JSON document rather
than the returned dict, the rendered HTML rather than the manifest dict. Two independent surfaces
for one defect is the point; one surface twice is not.

Nothing here touches a network, a GPU or a real engine. The engine is a sleep, the HTTP client
opens nothing, and the report is four paragraphs of synthetic prose.

Run:  python -m tests.test_attacks          (from the repo root)
      python -m tests.test_attacks -k omission
"""
import errno
import io
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import types
import unittest
from contextlib import redirect_stdout, redirect_stderr

sys.path.insert(0, ".")
from gpubench import cli  # noqa: E402
from gpubench import longform  # noqa: E402
from gpubench.probes import serving as S  # noqa: E402


# --------------------------------------------------------------------------------------
# THE REGISTER
#
# One id per attack that landed. The last test in this file fails if any id has no test naming it,
# so deleting a test is a visible failure and not a silent gap. Each entry records what the attack
# did and what it achieved BEFORE the fix; the test's own docstring carries the detail.

HOLES = {
    # numbers reaching the rendered document with no claim behind them
    "A5-fabricated-headline-figures":
        "five invented figures went into an abstract; exit 0, byte-identical claims.json",
    "A5-stale-headline-in-a-heading-and-the-contents":
        "a stale 2,850 in a section heading and the contents, beside a table reading 2182",
    "A5-stale-headline-in-a-title-attribute":
        "a stale figure in a title= tooltip, which the tag stripper threw away",
    # a manifest that shrinks, disarms, relabels a kind, or rewords a label
    "A10-shrunken-manifest":
        "a manifest cut to ONE claim reported 1 claim(s), 0 warning(s) and shipped the wrong number",
    "GATE-renamed-manifest-disarms-the-gate":
        "renaming the MANIFEST attribute disarmed the gate entirely and still exited 0",
    "A9-kind-relabelled-to-launder-arithmetic":
        "derived -> supplied laundered a printed 3.0 whose own arithmetic gives 10.73",
    "A7-label-reworded-to-silence-a-collision":
        "a label collision was warning 29 of 29, and rewording the label silenced it entirely",
    "A2-self-pruning-prose-guard":
        "the generator kept only guards whose operands existed, so deleting a claim deleted its guard",
    # a run that never happened, a gate nothing verified
    "A8-claim-names-a-run-that-never-happened":
        "run was tested for truthiness only, so a claim could name a run that does not exist",
    "A8-blank-run-id":
        "a run id of a single space passed the truthiness test while naming nothing",
    "G3-gate-passed-never-read-back":
        "gate.passed=True and cases_published=999 shipped; nothing opened the artefact",
    "A9-supplied-source-that-resolves-to-nothing":
        "kind supplied plus the phrase 'engineering estimate' bought exemption from recomputation",
    # declared table views and table cells the document does not have
    "F3-declared-table-view-renders-empty":
        "table_view: true was believed, so a figure shipped an empty Table view disclosure",
    "F3-declared-figure-absent-from-the-document":
        "a figure declared in the manifest was not in the rendered document at all",
    "F4-table-cell-the-table-does-not-declare":
        "a table printed a figure that was not one of its own declared cells",
    # the arrival process: drain bias, catch-up bursts, vanished requests, sweeps never run
    "SERV-fell-behind-drain-bias":
        "fell_behind judged completions over a window INCLUDING the drain: 6/24/39/56% false alarms",
    "SERV-negative-control-too-fast-to-bite":
        "the control used a 5 ms fake engine, about 400x faster than the real one; it passed wrongly",
    "D3-open-loop-level-crashes-the-load-shape-check":
        "check_load_shape raised TypeError on concurrency None, then demanded whole waves",
    "D7-arrival-model-flipped-beside-a-closed-loop-note":
        "closed_loop flipped to open_loop_poisson on a fixed-in-flight harness shipped clean",
    "SERV-catch-up-burst-passes-span-against-span":
        "a 600 ms stall became a catch-up burst and the span-against-span check said all was well",
    "SERV-vanished-request-with-no-error-recorded":
        "a client that could not be built took its request and its in-flight slot with it",
    "SERV-declared-rate-sweep-that-never-ran":
        "--rate 2,4,8 --mode prefill declared three rates in the document and ran rates[0] twice",
    # the meta attack
    "CLI-no-verify-emits-an-unmarked-publishable-file":
        "a draft written past the gate has to be unmistakable in every format it reaches",
}


def covers(*hole_ids):
    """Tag a test with the hole ids it holds shut. Read by the register test at the bottom."""
    def annotate(fn):
        fn.holes = tuple(hole_ids)
        return fn
    return annotate


# --------------------------------------------------------------------------------------
# the synthetic report: a content module small enough to read and real enough to publish
#
# Two measured series and one derived total. The body prints the total, and a figure ships the two
# series in a table view whose cells the manifest declares. Every attack below is one edit to this.

CONTENT_HEAD = '''\
"""Synthetic content module. Exists only for tests/test_attacks.py.

Deliberately publishable: a fixture that could not legitimately ship cannot show that an attack
turned a publishable document into a blocked one.
"""
import io
import os

TITLE = "Synthetic Machine Report"
BASENAME = "attacks"
VERSION = "1.0"
SECTION_ORDER = ["Abstract", "Findings"]
COMPANIONS = {"method-primer.html": ("Method primer", "render_primer")}
COMPANION_ORDER = {"method-primer.html": ["Method"]}

TOTAL = %(total)s
ABSTRACT_EXTRA = %(abstract_extra)s
FINDINGS_HEADING = %(findings_heading)s
FIGURE_HTML = %(figure_html)s


def build(run_dir, out_dir=None):
    return {}, {"a": 10.0, "b": 20.0}


def render(figures, data):
    return (
        \'<div class="titlepage"><h1>Synthetic Machine Report</h1>\'
        \'<p class="sub">edition 1.0</p></div>\'
        \'<section><h2>1. Abstract</h2>\'
        \'<p>Two series were measured on one machine and added.</p>\'
        + ABSTRACT_EXTRA +
        \'</section>\'
        \'<section><h2>2. \' + FINDINGS_HEADING + \'</h2>\'
        \'<p>Together they reach \' + TOTAL + \' tok/s.</p>\'
        + FIGURE_HTML +
        \'</section>\')


def render_primer(figures, data):
    return (\'<section><h2>1. Method</h2><p>Both series came from one harness run against one \'
            \'engine, with the prompt salted per request so no prefix could be cached.</p>\'
            \'</section>\')
'''

MANIFEST_DECL = '''

MANIFEST = "claims.json"


def claims(figures, data):
    m = {
        "schema": "claims/1",
        "report": {
            "version": VERSION,
            "arrival_model": "closed_loop",
            "arrival_note": ("Closed loop: a fixed in-flight population per level, no independent "
                             "arrival process, each request is issued when a previous one "
                             "completes."),
        },
        "runs": {"primary": {"started": "2026-08-25T11:00:00Z",
                             "finished": "2026-08-25T12:00:00Z",
                             "artifact": "result.json"}},
        "claims": {
            "throughput_a": {"value": data["a"], "unit": "tok/s", "basis": "total",
                             "kind": "measured", "run": "primary",
                             "measured_at": "2026-08-25T11:05:00Z",
                             "label": "throughput of the alpha series"},
            "throughput_b": {"value": data["b"], "unit": "tok/s", "basis": "total",
                             "kind": "measured", "run": "primary",
                             "measured_at": "2026-08-25T11:05:00Z",
                             "label": "throughput of the beta series"},
            "throughput_total": {"value": float(TOTAL), "unit": "tok/s", "basis": "total",
                                 "kind": "derived", "formula": "throughput_a + throughput_b",
                                 "label": "combined throughput"},
        },
        "tables": {"fig1_throughput": {"cells": ["throughput_a", "throughput_b"]}},
        "figures": [{"id": "fig1_throughput", "table_view": True}],
        "levels": [{"name": "c8", "concurrency": 8, "requests": 32, "duration_s": 17.6}],
        "gate": {"ran_at": "2026-08-25T19:37:00Z", "passed": True, "cases_published": 3,
                 "window_run": "primary"},
    }
%(extra)s
    # THE SELF-PRUNING GUARD, reproduced exactly as the generator wrote it: a prose assertion is
    # kept only while both of its operands still exist. Deleting a claim therefore deletes the
    # guard that would have failed on it, which is why the thing that blocks has to be the
    # declaration floor and the rendered document rather than the generator's own conscience.
    guards = [{"id": "findings",
               "text": "Together they reach {{throughput_total}} tokens per second.",
               "assert": {"op": "gt", "left": "throughput_total", "right": "throughput_a"}}]
    m["prose"] = [g for g in guards
                  if all(k in m["claims"] for k in (g["assert"]["left"], g["assert"]["right"]))]
    return m
'''

# The figure as it legitimately ships: a chart's values, in a table, inside the figure that asserts
# them, with every cell declared in the manifest.
FIGURE_OK = ('<figure id="fig1_throughput">'
             '<figcaption>Figure 1. Throughput by series</figcaption>'
             '<table><thead><tr><th>series</th><th>rate</th></tr></thead>'
             '<tbody><tr><td>alpha</td><td>10.0 tok/s</td></tr>'
             '<tr><td>beta</td><td>20.0 tok/s</td></tr></tbody></table></figure>')

# The same figure with its table replaced by an empty disclosure. This is what shipped: a
# "Table view" summary a reader could click, with nothing inside it, directly under a note saying
# the series was in the table view rather than on the plot. It was in neither.
FIGURE_EMPTY_DISCLOSURE = ('<figure id="fig1_throughput">'
                           '<figcaption>Figure 1. Throughput by series</figcaption>'
                           '<details><summary>Table view</summary></details></figure>')

# A third row carrying the measured prefill figure, so a stale headline has something to disagree
# with inside the same document.
FIGURE_WITH_PREFILL = ('<figure id="fig1_throughput">'
                       '<figcaption>Figure 1. Throughput by series</figcaption>'
                       '<table><thead><tr><th>series</th><th>rate</th></tr></thead>'
                       '<tbody><tr><td>alpha</td><td>10.0 tok/s</td></tr>'
                       '<tr><td>beta</td><td>20.0 tok/s</td></tr>'
                       '<tr><td>prefill</td><td>2182.0 tok/s</td></tr></tbody></table></figure>')

# A fourth row printing a figure that is a claim somewhere in the manifest but NOT one of this
# table's declared cells. A5 is satisfied by it; only the table-scoped check is not.
FIGURE_WITH_UNDECLARED_CELL = (
    '<figure id="fig1_throughput">'
    '<figcaption>Figure 1. Throughput by series</figcaption>'
    '<table><thead><tr><th>series</th><th>rate</th></tr></thead>'
    '<tbody><tr><td>alpha</td><td>10.0 tok/s</td></tr>'
    '<tr><td>beta</td><td>20.0 tok/s</td></tr>'
    '<tr><td>standby</td><td>45.0 tok/s</td></tr></tbody></table></figure>')

# ---- manifest edits, each one an attack ----

# Cut the manifest down to one claim. Everything left in it still verifies, which is the whole
# problem: asserting less scores better when the score counts only findings.
SHRINK = '''\
    del m["claims"]["throughput_b"]
    del m["claims"]["throughput_total"]
    m["figures"] = []
    m["tables"] = {}
'''

# Delete one claim and nothing else. The guard above prunes itself in response.
DROP_TOTAL = '''\
    del m["claims"]["throughput_total"]
'''

PREFILL_CLAIM = '''\
    m["claims"]["prefill_measured"] = {
        "value": 2182.0, "unit": "tok/s", "basis": "total", "kind": "measured", "run": "primary",
        "measured_at": "2026-08-25T11:05:00Z", "label": "measured prefill throughput"}
    m["tables"]["fig1_throughput"]["cells"].append("prefill_measured")
'''

# A derived value that was typed. 32.2 / 3 is 10.7333, and the manifest prints 3.0.
DERIVED_THAT_DISAGREES = '''\
    m["claims"]["decode_window_ms"] = {
        "value": 32.2, "unit": "ms", "basis": "per_request", "kind": "measured", "run": "primary",
        "measured_at": "2026-08-25T11:05:00Z", "label": "decode window"}
    m["claims"]["decode_steps"] = {
        "value": 3.0, "unit": "count", "basis": "per_request", "kind": "measured", "run": "primary",
        "measured_at": "2026-08-25T11:05:00Z", "label": "steps counted in the window"}
    m["claims"]["decode_step_budget_ms"] = {
        "value": 3.0, "unit": "ms", "basis": "per_token", "kind": "derived",
        "formula": "decode_window_ms / decode_steps", "label": "decode step budget"}
'''

# The same three claims with the answer relabelled. kind is the generator's free choice, so this
# edit changes no printed number and buys exemption from recomputation.
def relabelled_kind(source):
    return DERIVED_THAT_DISAGREES + (
        '    m["claims"]["decode_step_budget_ms"]["kind"] = "supplied"\n'
        '    m["claims"]["decode_step_budget_ms"]["source"] = %r\n' % (source,))


def supplied_claim(source):
    return ('    m["claims"]["decode_step_budget_ms"] = {\n'
            '        "value": 3.0, "unit": "ms", "basis": "per_token", "kind": "supplied",\n'
            '        "source": %r, "label": "decode step budget"}\n' % (source,))


def label_pair(label_a, label_b, value_b=2850.0):
    return ('    m["claims"]["prefill_a"] = {\n'
            '        "value": 2181.7, "unit": "tok/s", "basis": "total", "kind": "measured",\n'
            '        "run": "primary", "measured_at": "2026-08-25T11:05:00Z", "label": %r}\n'
            '    m["claims"]["prefill_b"] = {\n'
            '        "value": %r, "unit": "tok/s", "basis": "total", "kind": "measured",\n'
            '        "run": "primary", "measured_at": "2026-08-25T11:05:00Z", "label": %r}\n'
            % (label_a, value_b, label_b))


def standby_claim():
    return ('    m["claims"]["throughput_standby"] = {\n'
            '        "value": 45.0, "unit": "tok/s", "basis": "total", "kind": "measured",\n'
            '        "run": "primary", "measured_at": "2026-08-25T11:05:00Z",\n'
            '        "label": "throughput of the standby series"}\n')


def set_field(path, value):
    """A one-line manifest edit, as a source snippet. `path` is a python subscript expression."""
    return "    m%s = %r\n" % (path, value)


def content_source(total="30.0", abstract_extra="", findings_heading="Findings",
                   figure_html=None, extra="", manifest_attr="MANIFEST", claims_attr="claims"):
    """The content module for one attack, as python source.

    `manifest_attr` and `claims_attr` exist so the gate can be DISARMED the way the audit disarmed
    it: by renaming the attributes the engine looks for, which is a one-word edit no reader of the
    module would notice.
    """
    src = CONTENT_HEAD % {"total": repr(total),
                          "abstract_extra": repr(abstract_extra),
                          "findings_heading": repr(findings_heading),
                          "figure_html": repr(FIGURE_OK if figure_html is None else figure_html)}
    decl = MANIFEST_DECL % {"extra": extra}
    if manifest_attr != "MANIFEST":
        decl = decl.replace('MANIFEST = "claims.json"',
                            '%s = "claims.json"' % manifest_attr, 1)
    if claims_attr != "claims":
        decl = decl.replace("def claims(figures, data):",
                            "def %s(figures, data):" % claims_attr, 1)
    return src + decl


# --------------------------------------------------------------------------------------
# driving the real CLI


class Build(object):
    """What one `gpubench article` run did: its exit code, its log, and what reached disk."""

    STEM = "attacks-v1.0"

    def __init__(self, rc, out, out_dir):
        self.rc = rc
        self.out = out
        self.out_dir = out_dir

    def path(self, name):
        return os.path.join(self.out_dir, name)

    def report_exists(self):
        return os.path.isfile(self.path(self.STEM + ".html"))

    def index_exists(self):
        return os.path.isfile(self.path("index.html"))

    def companion_exists(self):
        return os.path.isfile(self.path("method-primer.html"))

    def html_files(self):
        return sorted(n for n in os.listdir(self.out_dir) if n.endswith(".html"))

    def read(self, name):
        with io.open(self.path(name), encoding="utf-8") as f:
            return f.read()

    def manifest_bytes(self):
        if not os.path.isfile(self.path("claims.json")):
            return None
        with open(self.path("claims.json"), "rb") as f:
            return f.read()

    def findings(self):
        if not os.path.isfile(self.path("claims-findings.json")):
            return []
        with io.open(self.path("claims-findings.json"), encoding="utf-8") as f:
            return json.load(f)

    def checks(self, check, severity=None):
        return [i for i in self.findings()
                if i.get("check") == check and (severity is None or i.get("severity") == severity)]

    def messages(self, check):
        return " ".join(i.get("message", "") for i in self.checks(check))

    def check_ids(self, severity="error"):
        return sorted({i.get("check") for i in self.findings() if i.get("severity") == severity})

    def coverage(self):
        """(covered, total, floor) as the gate printed them, or None when it printed no line."""
        m = re.search(r"document coverage: (\d+)/(\d+) unit-bearing numerals traced to a claim "
                      r"\(([\d.]+)%, floor ([\d.]+)%\)", self.out)
        return (int(m.group(1)), int(m.group(2)), float(m.group(4))) if m else None

    def declared(self):
        """(claims, prose, figures) as the passing log line stated them, or None."""
        m = re.search(r"manifest verified: (\d+) claim\(s\), (\d+) prose block\(s\), "
                      r"(\d+) figure\(s\)", self.out)
        return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


class ArticleCase(unittest.TestCase):
    """Writes a synthetic content module and runs the real `gpubench article` over it."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gpubench-attack-")
        self.run_dir = os.path.join(self.tmp, "run")
        self.out_dir = os.path.join(self.tmp, "out")
        os.makedirs(self.run_dir)
        os.makedirs(self.out_dir)
        self.builds = 0
        self.write_artefact()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- the gate's own evidence ----

    def write_artefact(self, cases=3, exact_match_pct=100.0, errors=(), deterministic=None,
                       published=None):
        """The result file the manifest's run points at, in the shape gpubench writes one.

        G3 opens this. Without it on disk, gate.passed would be exactly the unfalsifiable
        declaration the attack exploited, so every fixture here carries one.
        """
        n_published = cases if published is None else published
        doc = {"probes": {"accuracy": {
            "summary": {"cases": cases,
                        "deterministic": cases if deterministic is None else deterministic,
                        "exact_match_pct": exact_match_pct},
            "method": {"cases_published": [{"id": "case%d" % i} for i in range(n_published)]},
            "errors": list(errors),
        }}}
        with io.open(os.path.join(self.out_dir, "result.json"), "w",
                     encoding="utf-8", newline="\n") as f:
            json.dump(doc, f, indent=2)

    # ---- running a build ----

    def article(self, *flags, **opts):
        # A FRESH FILENAME per build, and not for tidiness. Two editions of this module differ by a
        # few characters and are usually the same length, so writing them to one path inside the
        # same clock second let importlib serve the first one's cached bytecode to the second: the
        # attack silently ran against the honest module and the test passed for no reason. A unique
        # name is the only thing that cannot be defeated by mtime granularity.
        self.builds += 1
        path = os.path.join(self.tmp, "content_mod_%d.py" % self.builds)
        with io.open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(content_source(**opts))
        buf = io.StringIO()
        argv = ["article", path, self.run_dir, "--out-dir", self.out_dir] + list(flags)
        with redirect_stdout(buf), redirect_stderr(buf):
            rc = cli.main(argv)
        return Build(rc, buf.getvalue(), self.out_dir)

    def clear_out(self, keep_manifest=False):
        """Empty the output directory so the next build's disk state means only its own thing.

        result.json is kept: it is the gate's evidence, not the build's output. claims.json is kept
        only when the test wants the previous edition as a baseline.
        """
        keep = {"result.json"} | ({"claims.json"} if keep_manifest else set())
        for name in os.listdir(self.out_dir):
            if name not in keep:
                os.remove(os.path.join(self.out_dir, name))

    def restore_manifest(self, raw):
        """Put a specific edition back as the baseline the next build is measured against.

        Needed because the gate WRITES the manifest on every build, blocked or not, so a chain of
        three attacks would otherwise compare each edition against the previous attack rather than
        against the honest one.
        """
        with open(os.path.join(self.out_dir, "claims.json"), "wb") as f:
            f.write(raw)

    # ---- assertions worth a name ----

    def assertNothingPublished(self, build):
        self.assertFalse(build.report_exists(), "a blocked report was written to disk")
        self.assertFalse(build.index_exists(), "a blocked report reached index.html")
        self.assertFalse(build.companion_exists(), "a blocked report's companion was written")
        self.assertEqual(build.html_files(), [], "HTML reached disk: %s" % build.html_files())
        self.assertIn("NOT WRITTEN", build.out)


# --------------------------------------------------------------------------------------
# the fixture itself has to be publishable, or nothing below proves anything


class TestTheFixtureIsPublishable(ArticleCase):
    """A baseline that already carries findings cannot show that an attack introduced one."""

    def test_the_synthetic_report_verifies_and_ships(self):
        # A first-ever build has no previous edition to measure its declaration floor against, and
        # the gate now says so out loud instead of passing in silence over a floor it never
        # checked. That NO BASELINE warning is the one and only thing between this fixture and a
        # clean sheet, so it is named here rather than tolerated in a loosened count below.
        first = self.article()
        self.assertEqual(first.rc, 0, first.out)
        self.assertEqual(sorted(i["check"] for i in first.findings()), ["A10"], first.findings())
        self.assertIn("NO BASELINE", first.out)

        # The same build again, with the first edition left on disk as the baseline. Now nothing at
        # all is outstanding, which is the property this class exists to establish.
        self.clear_out(keep_manifest=True)
        b = self.article()
        self.assertEqual(b.rc, 0, b.out)
        self.assertTrue(b.report_exists())
        self.assertTrue(b.index_exists())
        self.assertTrue(b.companion_exists())
        self.assertEqual(b.declared(), (3, 1, 1), b.out)
        self.assertIn("0 warning(s)", b.out)
        self.assertEqual(b.findings(), [], "the baseline must be clean, not merely unblocked")
        self.assertNotIn(longform.DRAFT_MARKER, b.read("index.html"))


# --------------------------------------------------------------------------------------
# 1. OMISSION: numbers reaching the rendered document with no claim behind them


class TestOmissionAttacks(ArticleCase):
    """The hole that mattered most, and the one every other check depended on not existing.

    A2 policed the manifest's `prose` list. That list is written by the same generator that writes
    the document, so anything the generator never declared was invisible to the entire gate. The
    audit measured the jurisdiction: 974 characters of manifest prose against 104,549 characters
    shipped.
    """

    @covers("A5-fabricated-headline-figures")
    def test_five_fabricated_headline_figures_never_reach_disk(self):
        """WHAT THE ATTACK DOES. Injects invented headline figures straight into the abstract,
        touching no claim: "1,240 tok/s", "96 concurrent users", "$0.11 per million output tokens".

        WHAT IT ACHIEVED BEFORE THE FIX. All five shipped, the build exited 0, and claims.json was
        BYTE-IDENTICAL to the honest edition's. There was nothing in the manifest to notice,
        because the manifest was never the thing that changed.

        WHAT MUST HAPPEN NOW. A5 reads the rendered document, so the build blocks, no HTML reaches
        disk, and the manifest is still byte-identical, which is what proves the manifest could
        never have caught this and that the document check is doing the work.
        """
        clean = self.article()
        self.assertEqual(clean.rc, 0, clean.out)
        honest_manifest = clean.manifest_bytes()
        # No baseline for the second build: this must be caught by the document alone, not by a
        # comparison against the previous edition.
        self.clear_out()

        attacked = self.article(abstract_extra=(
            '<p class="lede">The machine sustains 1,240 tok/s for 96 concurrent users at '
            '$0.11 per million output tokens.</p>'))
        self.assertNotEqual(attacked.rc, 0, attacked.out)
        self.assertNothingPublished(attacked)
        self.assertEqual(attacked.manifest_bytes(), honest_manifest,
                         "the manifest did change, so this is no longer the omission attack")
        self.assertTrue(attacked.checks("A5", "error"), attacked.findings())
        self.assertIn("1,240", attacked.messages("A5"))
        # and the gate says how much of the document it held jurisdiction over, which is the number
        # that made the shrunken manifest visible as well
        covered, total, floor = attacked.coverage()
        self.assertEqual(floor, 100.0)
        self.assertLess(covered, total)

    @covers("A5-stale-headline-in-a-heading-and-the-contents")
    def test_a_stale_headline_in_a_heading_and_the_contents_is_caught(self):
        """WHAT THE ATTACK DOES. Puts a superseded figure in a SECTION HEADING, from which the
        engine also builds the contents page, while the table two lines below prints the measured
        value. The manifest is untouched.

        WHAT IT ACHIEVED BEFORE THE FIX. A stale "2,850" shipped in the heading, in the table of
        contents and in a title= attribute, directly beside a table reading 2182. Three copies of
        a wrong number, one copy of the right one, zero findings.

        WHAT MUST HAPPEN NOW. Blocked, with the numeral named, and reported as occurring more than
        once so the contents copy is demonstrably in scope rather than accidentally excluded.
        """
        b = self.article(findings_heading="Findings at 2,850 tok/s",
                         figure_html=FIGURE_WITH_PREFILL, extra=PREFILL_CLAIM)
        self.assertNotEqual(b.rc, 0, b.out)
        self.assertNothingPublished(b)
        stale = [i for i in b.checks("A5", "error") if i.get("numeral") == "2,850"]
        self.assertTrue(stale, b.findings())
        self.assertGreaterEqual(stale[0].get("occurrences", 0), 2,
                                "the contents page copy of the heading was not in scope")
        # The document disagrees with itself, and the gate has to name the WRONG half. 2182.0 is
        # printed in the table two lines below and is a declared claim, so it must produce no
        # finding at all: a check that reported both halves would leave the author to guess.
        uncovered = {i.get("numeral") for i in b.checks("A5", "error") if i.get("numeral")}
        self.assertEqual(uncovered, {"2,850"},
                         "only the stale heading figure should be unbacked, got %s" % uncovered)

    @covers("A5-stale-headline-in-a-title-attribute")
    def test_a_numeral_hidden_in_a_title_attribute_is_caught(self):
        """WHAT THE ATTACK DOES. Hides the figure where no tag stripper looks: in a title=
        tooltip, whose text a reader and a screen reader both get.

        WHAT IT ACHIEVED BEFORE THE FIX. The F1 stripper threw attribute values away, so a stale
        figure in a title= attribute was outside every check while being visible on the page.

        WHAT MUST HAPPEN NOW. Blocked, with the tooltip's numeral named. The negative control in
        the same test is a numeral inside a <script> body: script and style are NOT what a reader
        sees, and a check that reported them would be reading bytes rather than the document.
        """
        b = self.article(abstract_extra=(
            '<p><span title="2,850 tok/s at the roof">peak prefill</span></p>'
            '<script>var stale = "2,852 tok/s";</script>'))
        self.assertNotEqual(b.rc, 0, b.out)
        self.assertNothingPublished(b)
        self.assertIn("2,850", b.messages("A5"))
        self.assertNotIn("2,852", b.messages("A5"),
                         "a numeral inside a script body is not visible text")


# --------------------------------------------------------------------------------------
# 2. WEAKENING: a manifest that shrinks, disarms, relabels a kind, or rewords a label


class TestWeakeningAttacks(ArticleCase):
    """Every check in the gate reads the manifest as the standard the document is held to. The
    cheapest way to a clean run is therefore to lower the standard, and each of these does."""

    @covers("A10-shrunken-manifest")
    def test_a_shrunken_manifest_is_blocked_and_scores_worse_not_better(self):
        """WHAT THE ATTACK DOES. Cuts the manifest down to ONE claim and drops the prose blocks,
        the figures and the tables. Everything remaining still verifies.

        WHAT IT ACHIEVED BEFORE THE FIX. The build reported "1 claim(s), 0 warning(s)", which is a
        CLEANER log line than the honest 197-claim manifest's, and shipped the same wrong number in
        the body. Nothing else in the gate could see it: evidence a manifest omits leaves no trace
        inside that manifest.

        WHAT MUST HAPPEN NOW. Two independent things block it, and both are checked here because
        either one alone can be refactored away. A10 compares the declaration against the previous
        edition. A5 measures coverage against the DOCUMENT, so the numerals the manifest stopped
        declaring become unbacked numerals and shrinking scores WORSE.
        """
        clean = self.article()
        self.assertEqual(clean.rc, 0, clean.out)
        self.assertEqual(clean.declared(), (3, 1, 1))
        self.clear_out(keep_manifest=True)

        shrunk = self.article(extra=SHRINK)
        self.assertNotEqual(shrunk.rc, 0, shrunk.out)
        self.assertNothingPublished(shrunk)
        self.assertNotIn("manifest verified", shrunk.out,
                         "a shrunken manifest must not report a clean verdict at all")
        self.assertTrue(shrunk.checks("A10", "error"), shrunk.findings())
        self.assertIn("throughput_b", shrunk.messages("A10"))
        self.assertIn("throughput_total", shrunk.messages("A10"))
        # and the document check, which needs no baseline at all
        self.assertTrue(shrunk.checks("A5", "error"))
        self.assertIn("20.0", shrunk.messages("A5"))

    @covers("A10-shrunken-manifest")
    def test_shrinking_still_blocks_with_no_previous_edition_to_compare_against(self):
        """The floor needs a baseline; a first-ever build has none, and "no baseline" is precisely
        the state in which a shrunken manifest used to sail through. So the document check must
        block it on its own. Same hole, second independent guard."""
        self.clear_out()
        shrunk = self.article(extra=SHRINK)
        self.assertNotEqual(shrunk.rc, 0, shrunk.out)
        # Nothing A10 says here may rest on a comparison with a previous edition, because there is
        # not one: every A10 finding on this run is marked baseline False. What it no longer does
        # is go quiet in that state. It says the floor could not be checked, and it holds the
        # document to a coverage floor of its own instead, which is a second guard that needs no
        # baseline either. The assertion that used to stand here was "A10 says nothing at all",
        # which would now read as a silence this build does not have.
        a10 = shrunk.checks("A10")
        self.assertTrue(a10, "a missing baseline has to be stated, not passed over: %s"
                        % shrunk.findings())
        self.assertEqual([i.get("baseline") for i in a10], [False] * len(a10),
                         "no A10 finding on this run may claim to have compared anything: %s" % a10)
        self.assertIn("NO BASELINE", shrunk.messages("A10"),
                      "a floor that could not be checked has to say so, or silence reads as a pass")
        # and the document check, which needs no baseline at all
        self.assertTrue(shrunk.checks("A5", "error"))
        self.assertNothingPublished(shrunk)

    @covers("GATE-renamed-manifest-disarms-the-gate")
    def test_renaming_the_gate_attributes_never_exits_zero(self):
        """WHAT THE ATTACK DOES. Renames the attributes the engine looks for. MANIFEST becomes
        CLAIMS_MANIFEST; claims() becomes _claims(). Not one number changes and the module still
        renders the same document.

        WHAT IT ACHIEVED BEFORE THE FIX. The gate found nothing to run, said so in a log line
        nobody reads, rendered the report and exited 0. An ungated build and a fully gated one were
        indistinguishable to anything reading the exit code, and the exit code is all a pipeline
        reads.

        WHAT MUST HAPPEN NOW. Half renamed is a WIRING FAULT (exit 2): a half-armed gate reads as
        an armed one. Both renamed is an UNARMED gate (exit 1). Neither writes a file, and
        --allow-ungated excuses only the second, because a gate that was never armed and a gate
        that was miswired are different problems.
        """
        miswired = self.article(manifest_attr="CLAIMS_MANIFEST")
        self.assertEqual(miswired.rc, 2, miswired.out)
        self.assertIn("MISWIRED", miswired.out)
        self.assertNothingPublished(miswired)
        self.assertIsNone(miswired.manifest_bytes(), "no manifest may be invented")

        self.clear_out()
        absent = self.article(manifest_attr="CLAIMS_MANIFEST", claims_attr="_claims")
        self.assertEqual(absent.rc, 1, absent.out)
        self.assertIn("NOT ARMED", absent.out)
        self.assertIn("--allow-ungated", absent.out)
        self.assertNothingPublished(absent)
        self.assertIsNone(absent.manifest_bytes())

        self.clear_out()
        still_miswired = self.article("--allow-ungated", manifest_attr="CLAIMS_MANIFEST")
        self.assertEqual(still_miswired.rc, 2, still_miswired.out)
        self.assertNothingPublished(still_miswired)

        self.clear_out()
        legacy = self.article("--allow-ungated", manifest_attr="CLAIMS_MANIFEST",
                              claims_attr="_claims")
        # The escape writes a file, and the exit code is the only thing a pipeline reads, so it
        # cannot be the same code a checked report exits with. DRAFT_EXIT means "there is a file
        # and it is not publishable", which is the whole promise in this test's name.
        self.assertNotEqual(legacy.rc, 0, legacy.out)
        self.assertEqual(legacy.rc, cli.DRAFT_EXIT, legacy.out)
        self.assertTrue(legacy.report_exists())
        for name in (legacy.STEM + ".html", "index.html", "method-primer.html"):
            self.assertIn(longform.DRAFT_MARKER, legacy.read(name),
                          "%s went out with no marker on an ungated build" % name)

    @covers("A9-kind-relabelled-to-launder-arithmetic")
    def test_relabelling_a_kind_does_not_launder_the_arithmetic(self):
        """WHAT THE ATTACK DOES. B1 recomputes only claims whose kind is "derived", and kind is a
        free choice of the generator. So it changes one word: derived becomes supplied.

        WHAT IT ACHIEVED BEFORE THE FIX. A claim printed as 3.0, whose own formula gives 10.73,
        shipped with zero findings. The number did not move, the formula was still sitting in the
        manifest, and nothing recomputed it.

        WHAT MUST HAPPEN NOW. Three guards, in order. As derived, B1 blocks on the arithmetic. As
        supplied with a phrase that merely SOUNDS like provenance, A9 blocks. As supplied with a
        source a reader really could open, the declaration floor blocks it, because the claim now
        rests on weaker evidence than the previous edition's with no changelog row saying so.

        AND B1 ITSELF NO LONGER HONOURS THE RELABEL. A claim that carries its own inputs and its
        own formula is recomputed whatever the generator chose to call it, so the escape this
        attack was built on is closed at the first guard as well as the second. This assertion used
        to record the escape as real ("the relabel really does exempt it from recomputation") and
        leaned on A9 alone to catch it; it now records that the exemption is gone.
        """
        derived = self.article(extra=DERIVED_THAT_DISAGREES)
        self.assertNotEqual(derived.rc, 0, derived.out)
        self.assertTrue(derived.checks("B1", "error"), derived.findings())
        self.assertIn("10.7", derived.messages("B1"))
        self.assertNothingPublished(derived)
        # The manifest is written even on a blocked build, so this edition is available as the
        # baseline that records the claim as derived.
        honest_edition = derived.manifest_bytes()

        self.clear_out()
        laundered = self.article(extra=relabelled_kind("engineering estimate"))
        self.assertNotEqual(laundered.rc, 0, laundered.out)
        self.assertTrue(laundered.checks("B1", "error"),
                        "the relabel must no longer exempt the claim from recomputation: %s"
                        % laundered.findings())
        self.assertIn("10.7", laundered.messages("B1"), "the arithmetic is still the finding")
        self.assertIn("free choice of the generator", laundered.messages("B1"),
                      "and the finding has to name the relabel as the thing that did not work")
        self.assertTrue(laundered.checks("A9", "error"), laundered.findings())
        self.assertIn("redeemable", laundered.messages("A9"))
        self.assertNothingPublished(laundered)

        self.clear_out()
        self.restore_manifest(honest_edition)
        with_source = self.article(extra=relabelled_kind("docs/engine-config.md"))
        self.assertNotEqual(with_source.rc, 0, with_source.out)
        self.assertEqual(with_source.checks("A9"), [], "the source is redeemable now")
        floor = with_source.checks("A10", "error")
        self.assertTrue(floor, with_source.findings())
        self.assertIn("decode_step_budget_ms (derived -> supplied)",
                      with_source.messages("A10"))
        self.assertNothingPublished(with_source)

    @covers("A7-label-reworded-to-silence-a-collision")
    def test_rewording_a_label_does_not_silence_a_collision(self):
        """WHAT THE ATTACK DOES. Prints one quantity twice with two different values, then edits
        one of the two labels so they are no longer byte-identical.

        WHAT IT ACHIEVED BEFORE THE FIX. The collision was a WARNING that fired only on identical
        labels, so 2181.7 beside 2850.0 landed as warning 29 of 29 in a build that already emitted
        28 and shipped unread. Rewording the label produced nothing at all.

        WHAT MUST HAPPEN NOW. An ERROR, and grouped on the label's meaning-carrying words rather
        than its bytes, so a reworded label is still the same label. The negative control is two
        printings of one quantity that AGREE, which must stay clean: this check has to be able to
        reach zero or it becomes noise again.
        """
        identical = self.article(extra=label_pair("prefill throughput at 2048 tokens",
                                                  "prefill throughput at 2048 tokens"))
        self.assertNotEqual(identical.rc, 0, identical.out)
        self.assertTrue(identical.checks("A7", "error"), identical.findings())
        self.assertIn("2181.7", identical.messages("A7"))
        self.assertNothingPublished(identical)

        self.clear_out()
        reworded = self.article(extra=label_pair("prefill throughput at 2048 tokens",
                                                 "throughput of the prefill at 2048 tokens"))
        self.assertNotEqual(reworded.rc, 0, reworded.out)
        self.assertTrue(reworded.checks("A7", "error"),
                        "rewording the label silenced the collision again")
        self.assertNothingPublished(reworded)

        self.clear_out()
        agreeing = self.article(extra=label_pair("prefill throughput at 2048 tokens",
                                                 "throughput of the prefill at 2048 tokens",
                                                 value_b=2182.0))
        self.assertEqual(agreeing.rc, 0, agreeing.out)
        self.assertEqual(agreeing.checks("A7"), [])

    @covers("A2-self-pruning-prose-guard")
    def test_deleting_a_claim_does_not_delete_the_guard_that_would_have_failed(self):
        """WHAT THE ATTACK DOES. Deletes one claim. The generator builds its prose assertions by
        filtering to the guards whose operands still exist, so the guard that would have failed
        removes itself in the same edit.

        WHAT IT ACHIEVED BEFORE THE FIX. Deleting a claim deleted its guard rather than failing:
        the manifest came back smaller, better-behaved and silent. Every assertion the generator
        wrote was conditional on the generator still wanting it.

        WHAT MUST HAPPEN NOW. The generator is still allowed to prune (the fixture prunes, exactly
        as the real one did), and it buys nothing. The declaration floor sees the claim and its
        prose block gone with no changelog row, and A5 sees the total still printed in the body
        with nothing behind it. Neither of those is under the generator's control.
        """
        clean = self.article()
        self.assertEqual(clean.rc, 0, clean.out)
        self.assertEqual(clean.declared(), (3, 1, 1))
        self.clear_out(keep_manifest=True)

        pruned = self.article(extra=DROP_TOTAL)
        self.assertNotEqual(pruned.rc, 0, pruned.out)
        self.assertNothingPublished(pruned)
        self.assertEqual(pruned.checks("A3"), [],
                         "the guard really did prune itself, which is the trap")
        floor = pruned.messages("A10")
        self.assertIn("throughput_total", floor)
        self.assertIn("prose block(s)", floor)
        self.assertTrue(pruned.checks("A5", "error"))
        self.assertIn("30.0", pruned.messages("A5"))


# --------------------------------------------------------------------------------------
# 3. PROVENANCE: a run that never happened, a gate nothing verified


class TestProvenanceAttacks(ArticleCase):
    """A measurement is only a measurement if the thing it came from can be gone and looked at."""

    @covers("A8-claim-names-a-run-that-never-happened")
    def test_a_claim_may_not_name_a_run_that_never_happened(self):
        """WHAT THE ATTACK DOES. Points a measured claim at a run id that is not in the run table.

        WHAT IT ACHIEVED BEFORE THE FIX. `run` was tested for truthiness and nothing else, so a
        claim attributed to "run-that-never-happened" shipped with zero findings and nothing
        downstream could tell it from a real measurement.

        WHAT MUST HAPPEN NOW. Blocked, with the run table printed beside the bad id so the author
        can see what was available.
        """
        b = self.article(extra=set_field('["claims"]["throughput_a"]["run"]',
                                         "run-that-never-happened"))
        self.assertNotEqual(b.rc, 0, b.out)
        self.assertTrue(b.checks("A8", "error"), b.findings())
        self.assertIn("not in the run table", b.messages("A8"))
        self.assertNothingPublished(b)

    @covers("A8-blank-run-id")
    def test_a_run_id_of_one_space_names_nothing(self):
        """WHAT THE ATTACK DOES. Sets the run id to a single space.

        WHAT IT ACHIEVED BEFORE THE FIX. " " passes a truthiness test while naming nothing, so the
        claim was unattributable and clean at the same time.

        WHAT MUST HAPPEN NOW. Blocked as a blank run. The empty string is still the older A4 error
        it always was, and both are asserted so a future simplification cannot merge them into a
        single message that loses the distinction between "no run" and "a run that is whitespace".
        """
        blank = self.article(extra=set_field('["claims"]["throughput_a"]["run"]', " "))
        self.assertNotEqual(blank.rc, 0, blank.out)
        self.assertTrue(blank.checks("A8", "error"), blank.findings())
        self.assertIn("blank run", blank.messages("A8"))
        self.assertNothingPublished(blank)

        self.clear_out()
        empty = self.article(extra=set_field('["claims"]["throughput_a"]["run"]', ""))
        self.assertNotEqual(empty.rc, 0, empty.out)
        self.assertTrue(empty.checks("A4", "error"), empty.findings())

    @covers("G3-gate-passed-never-read-back")
    def test_a_hardcoded_gate_pass_is_read_back_out_of_the_artefact(self):
        """WHAT THE ATTACK DOES. Leaves gate.passed=True in the manifest while the result file the
        gate names records a quality regression, and separately inflates cases_published.

        WHAT IT ACHIEVED BEFORE THE FIX. passed=True and cases_published=999 shipped with zero
        findings, because nothing ever opened the file the gate said it came from. The gate exists
        to stop a stack that got faster by getting worse, so a gate result nobody read back is
        worse than no gate: it reports success unconditionally.

        WHAT MUST HAPPEN NOW. The artefact is opened and the declaration compared against it. Both
        directions are checked here: a pass the artefact contradicts, and a published-case count
        the artefact cannot support.
        """
        clean = self.article()
        self.assertEqual(clean.rc, 0, clean.out)
        self.clear_out(keep_manifest=True)

        # Same manifest, worse machine: the gate's own evidence now records a regression.
        self.write_artefact(cases=3, exact_match_pct=62.5, errors=["case2 timed out"])
        lying = self.article()
        self.assertNotEqual(lying.rc, 0, lying.out)
        self.assertTrue(lying.checks("G3", "error"), lying.findings())
        self.assertIn("did not pass", lying.messages("G3"))
        self.assertIn("62.5", lying.messages("G3"))
        self.assertNothingPublished(lying)

        self.clear_out(keep_manifest=True)
        self.write_artefact(cases=3, exact_match_pct=100.0)
        inflated = self.article(extra=set_field('["gate"]["cases_published"]', 999))
        self.assertNotEqual(inflated.rc, 0, inflated.out)
        self.assertTrue(inflated.checks("G3", "error"), inflated.findings())
        self.assertIn("carries 3", inflated.messages("G3"))
        self.assertNothingPublished(inflated)

    @covers("A9-supplied-source-that-resolves-to-nothing")
    def test_a_supplied_claim_needs_a_source_a_reader_can_open(self):
        """WHAT THE ATTACK DOES. Declares a number as "supplied" with the source "engineering
        estimate", which reads like provenance and is not.

        WHAT IT ACHIEVED BEFORE THE FIX. kind supplied exempts a claim from recomputation, and the
        source field was never resolved, so a phrase was enough to buy the exemption.

        WHAT MUST HAPPEN NOW. An exemption from recomputation has to be REDEEMABLE. A run id from
        the run table, a URL and a file or module path all clear it; a phrase does not. All four
        are asserted, because a check that rejected everything would simply be a ban on the kind.
        """
        b = self.article(extra=supplied_claim("engineering estimate"))
        self.assertNotEqual(b.rc, 0, b.out)
        self.assertTrue(b.checks("A9", "error"), b.findings())
        self.assertIn("redeemable", b.messages("A9"))
        self.assertNothingPublished(b)

        for source in ("primary", "https://vendor.example/engine-spec", "docs/engine-config.md"):
            self.clear_out(keep_manifest=True)
            ok = self.article(extra=supplied_claim(source))
            self.assertEqual(ok.rc, 0, "%r was refused: %s" % (source, ok.out))
            self.assertEqual(ok.checks("A9"), [])


# --------------------------------------------------------------------------------------
# 4. ARTEFACT: declared table views and table cells the document does not have


class TestArtifactAttacks(ArticleCase):
    """F2 read the manifest's own boolean. table_view is an author's INTENTION; these read the
    outcome."""

    @covers("F3-declared-table-view-renders-empty")
    def test_a_declared_table_view_that_renders_empty_is_caught(self):
        """WHAT THE ATTACK DOES. Renders the figure with its table replaced by an empty
        disclosure, leaving table_view: true in the manifest.

        WHAT IT ACHIEVED BEFORE THE FIX. The document shipped
        "<details><summary>Table view</summary></details>" under a note telling the reader the
        series was in the table view rather than on the plot. The series was in neither, and F2
        was satisfied because it read the boolean.

        WHAT MUST HAPPEN NOW. Blocked. A table with no body cell carrying visible text is not a
        table view, and the check says so in those terms.
        """
        b = self.article(figure_html=FIGURE_EMPTY_DISCLOSURE)
        self.assertNotEqual(b.rc, 0, b.out)
        self.assertTrue(b.checks("F3", "error"), b.findings())
        self.assertIn("renders none, or renders an empty one", b.messages("F3"))
        self.assertNothingPublished(b)

    @covers("F3-declared-figure-absent-from-the-document")
    def test_a_declared_figure_missing_from_the_document_is_caught(self):
        """WHAT THE ATTACK DOES. Drops the figure from the body and leaves it in the manifest.

        WHAT IT ACHIEVED BEFORE THE FIX. The same blindness as the empty disclosure, one step
        further on: the manifest described a figure the reader never sees, and the declaration was
        the only thing anything read.

        WHAT MUST HAPPEN NOW. Blocked, and the message distinguishes "no table" from "no figure at
        all", because those are two different repairs.
        """
        b = self.article(figure_html="")
        self.assertNotEqual(b.rc, 0, b.out)
        self.assertTrue(b.checks("F3", "error"), b.findings())
        self.assertIn("does not appear in the rendered document at all", b.messages("F3"))
        self.assertNothingPublished(b)

    @covers("F4-table-cell-the-table-does-not-declare")
    def test_a_table_numeral_that_is_not_a_declared_cell_of_that_table(self):
        """WHAT THE ATTACK DOES. Prints a figure inside the throughput table that is a claim
        SOMEWHERE in the manifest but is not one of that table's declared cells.

        WHAT IT ACHIEVED BEFORE THE FIX. A5 is document-wide, so any claim anywhere that happened
        to share the value covered the numeral. With two hundred claims in scope that happens
        constantly, and a figure typed into the wrong table was indistinguishable from a figure
        drawn from the right one.

        WHAT MUST HAPPEN NOW. F4 is table-scoped and stricter: the cell has to be one of THIS
        table's. It is a warning, because a table legitimately drawing on an undeclared claim is a
        declaration bug rather than a wrong number, so this test also proves that a final edition
        run with --warnings-as-errors refuses to publish it.
        """
        b = self.article(figure_html=FIGURE_WITH_UNDECLARED_CELL, extra=standby_claim())
        self.assertEqual(b.rc, 0, b.out)
        self.assertEqual(b.checks("A5", "error"), [], "the value IS a claim, document-wide")
        warned = b.checks("F4", "warn")
        self.assertTrue(warned, b.findings())
        self.assertIn("45.0", b.messages("F4"))

        self.clear_out()
        final = self.article("--warnings-as-errors", figure_html=FIGURE_WITH_UNDECLARED_CELL,
                             extra=standby_claim())
        self.assertNotEqual(final.rc, 0, final.out)
        self.assertNothingPublished(final)


# --------------------------------------------------------------------------------------
# 5. ARRIVAL: drain bias, catch-up bursts, vanished requests, sweeps that were never run
#
# The fakes below are the adversaries' own: a sleep for an engine, optionally behind a hard
# capacity limit, and a client that opens nothing. `capacity=None` is an engine with UNLIMITED
# parallelism and provably zero queueing, which is what makes a "the engine did not keep up"
# verdict a false positive by construction rather than by argument.


class SleepEngine(object):
    def __init__(self, service_s=0.02, capacity=None):
        self.service_s = service_s
        self.gate = threading.Semaphore(capacity) if capacity else None
        self.lock = threading.Lock()
        self.inflight = 0
        self.peak_inflight = 0
        self.completions = 0

    def one_request(self, client, args, salt, in_tok=None, out_tok=None):
        with self.lock:
            self.inflight += 1
            self.peak_inflight = max(self.peak_inflight, self.inflight)
        try:
            if self.gate is not None:
                with self.gate:
                    time.sleep(self.service_s)
            else:
                time.sleep(self.service_s)
        finally:
            with self.lock:
                self.inflight -= 1
                self.completions += 1
        n_out = 4 if out_tok is None else max(1, out_tok)
        return {"ttft_s": self.service_s / 2.0,
                "e2e_s": self.service_s,
                "itls": [self.service_s / (2.0 * n_out)] * max(1, n_out - 1),
                "completion_tokens": n_out,
                "prompt_tokens": 8 if in_tok is None else in_tok}


class NullClient(object):
    """Opens nothing. /models answers so the probe gets past its reachability check; /metrics 404s,
    which is the path the metrics scrape already has to survive on engines with no metrics port."""

    def __init__(self, base_url, timeout):
        self.base_url = base_url
        self.timeout = timeout

    def get(self, path, root=False):
        if str(path).endswith("/models"):
            return 200, json.dumps({"data": [{"id": "fake-model"}]})
        return 404, ""

    def close(self):
        pass


def probe_args(**over):
    """The attribute surface run_level reads, as argparse would hand it over."""
    base = dict(base_url="http://127.0.0.1:1/v1", model="fake-model", api_key=None,
                endpoint="completions", input_tokens=512, output_tokens=128, timeout=5.0,
                arrival="closed", rate=None, arrival_seed=S.DEFAULT_ARRIVAL_SEED,
                queue_sample_interval=0.05, requests=8)
    base.update(over)
    return types.SimpleNamespace(**base)


class ProbeRun(object):
    def __init__(self, docs, out, exit_code, run_dir):
        self.docs = docs
        self.out = out
        self.exit_code = exit_code
        self.run_dir = run_dir

    def level(self, name=None, index=0):
        doc = self.docs[name] if name else list(self.docs.values())[0]
        return doc["levels"][index]


def drive_probe(argv, engine=None, client_cls=None, sleep=None):
    """Run the REAL serving probe end to end, with the socket and nothing else replaced.

    Returns what reached disk, what was printed, and the exit code if it exited. A SystemExit is
    caught rather than raised, because "did anything get written when it refused" is itself one of
    the questions below.
    """
    engine = engine or SleepEngine(service_s=0.004, capacity=32)
    run_dir = tempfile.mkdtemp(prefix="gpubench-attack-probe-")
    saved_client, saved_one = S.Client, S.one_request
    saved_argv, saved_env, saved_time = sys.argv, os.environ.get("GPUBENCH_RUN_DIR"), S.time
    S.Client = client_cls or NullClient
    S.one_request = engine.one_request
    sys.argv = ["serving.py"] + list(argv)
    os.environ["GPUBENCH_RUN_DIR"] = run_dir
    if sleep is not None:
        S.time = types.SimpleNamespace(perf_counter=time.perf_counter, sleep=sleep,
                                       strftime=time.strftime, gmtime=time.gmtime)
    code = None
    buf = io.StringIO()
    try:
        with redirect_stdout(buf), redirect_stderr(buf):
            S.main()
    except SystemExit as exc:
        code = exc.code
    finally:
        S.Client, S.one_request, sys.argv, S.time = (saved_client, saved_one, saved_argv,
                                                     saved_time)
        if saved_env is None:
            os.environ.pop("GPUBENCH_RUN_DIR", None)
        else:
            os.environ["GPUBENCH_RUN_DIR"] = saved_env
    docs = {}
    for name in sorted(os.listdir(run_dir)):
        with io.open(os.path.join(run_dir, name), encoding="utf-8") as fh:
            docs[name] = json.loads(fh.read())
    return ProbeRun(docs, buf.getvalue(), code, run_dir)


class TestArrivalAttacks(unittest.TestCase):
    """The open-loop mode exists to stop a closed-loop harness reporting optimistic percentiles.
    These are the attacks that made it report fiction of its own instead."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gpubench-attack-verify-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- helpers for the verify-side attacks ----

    def manifest_for(self, report, levels):
        return {
            "schema": "claims/1",
            "report": report,
            "runs": {"primary": {"started": "2026-08-25T11:00:00Z",
                                 "finished": "2026-08-25T12:00:00Z"}},
            "claims": {"throughput": {"value": 233.0, "unit": "tok/s", "basis": "total",
                                      "kind": "measured", "run": "primary",
                                      "measured_at": "2026-08-25T11:05:00Z",
                                      "label": "aggregate throughput"}},
            "levels": levels,
            # G3 treats a gate result with no readable artefact as unfalsifiable and now blocks on
            # it, so a manifest built in memory has to say out loud that there is no file to read.
            # The waiver downgrades G3 to a warning; it does not remove the finding, which is why
            # the arrival checks below can still be read against a clean exit code.
            "gate": {"passed": True, "cases_published": True, "window_run": "primary",
                     "artifact_waiver": "synthetic fixture: this manifest describes no run that "
                                        "exists on disk"},
        }

    def run_verify_cli(self, manifest, name="claims.json"):
        """Drive `gpubench verify` over a manifest ON DISK, which is the surface a build uses."""
        path = os.path.join(self.tmp, name)
        findings_path = os.path.join(self.tmp, os.path.splitext(name)[0] + "-findings.json")
        with io.open(path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(manifest, f, indent=2)
        buf = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(buf):
            rc = cli.main(["verify", path, "--findings", findings_path])
        with io.open(findings_path, encoding="utf-8") as f:
            findings = json.load(f)
        return rc, buf.getvalue(), findings

    @staticmethod
    def checks(findings, check, severity=None):
        return [i for i in findings
                if i.get("check") == check and (severity is None or i.get("severity") == severity)]

    # ---- the attacks ----

    @covers("SERV-fell-behind-drain-bias")
    def test_an_engine_that_cannot_queue_is_never_blamed_in_the_written_document(self):
        """WHAT THE ATTACK DOES. Offers a steady Poisson load to an engine with UNLIMITED
        parallelism and a fixed service time, at the service times the real box shows. Such an
        engine queues nothing, so any "did not keep up" verdict is a false positive.

        WHAT IT ACHIEVED BEFORE THE FIX. fell_behind was completions over the WALL CLOCK (which
        runs to the last completion) against arrivals over the ARRIVAL SPAN (which runs to the last
        arrival). The wall clock exceeds the span by at least one service time on any engine, so
        the ratio carried a built-in deficit of about service/(span+service). Measured on the fake
        with zero queueing and zero errors: 6% at a 0.1 s service time, 24% at 0.5 s, 39% at 1 s,
        56% at 2 s, and every one of those read as TRUE. The machine this tool was written for
        serves at 2.18 s per request at concurrency 1 and 21.12 s at concurrency 64, so every
        honest open-loop level on it would have blamed a healthy engine.

        WHAT MUST HAPPEN NOW. The verdict is the slope of per-request latency against arrival
        time, which is flat on an engine that is not queueing. This test reads the WRITTEN
        DOCUMENT and the PRINTED LINE rather than a return value: the deficit is still recorded,
        still large, and explicitly labelled as not a verdict.
        """
        run = drive_probe(["--arrival", "poisson", "--rate", "20", "--requests", "32",
                           "--output-tokens", "4", "--warmup", "0",
                           "--queue-sample-interval", "0.05"],
                          engine=SleepEngine(service_s=0.25))
        self.assertIsNone(run.exit_code, run.out)
        level = run.level("serve_bench_poisson.json")
        arrival = level["arrival"]
        self.assertEqual(level["error_count"], 0, run.out)
        self.assertEqual(level["requests_ok"], 32, run.out)
        self.assertIs(arrival["fell_behind"], False, arrival["fell_behind_basis"])
        self.assertNotIn("the ENGINE did not keep up", run.out)
        # the number that used to be the verdict is still in the document, and still large
        self.assertGreater(arrival["completion_deficit_vs_offered_pct"], 5.0)
        self.assertIn("drain-biased by construction",
                      arrival["completion_deficit_is_not_a_verdict"])
        self.assertIn("WALL CLOCK", arrival["completions_per_s_incl_drain_note"])

    @covers("SERV-negative-control-too-fast-to-bite")
    def test_the_control_holds_at_the_service_times_the_real_machine_shows(self):
        """WHAT THE ATTACK DOES. Re-runs the suite's own negative control at a realistic service
        time instead of the one it was written with.

        WHAT IT ACHIEVED BEFORE THE FIX. The control asserted that a keeping-up engine does not
        report falling behind, using a fake engine that served in 5 ms: about 400 times faster than
        the real one. At 5 ms the drain is a rounding error, so the assertion held for the wrong
        reason and the drain bias was invisible to the very test written to catch it. A control
        that only passes because the fake is unrealistic is not a control.

        WHAT MUST HAPPEN NOW. The same assertion holds across three orders of magnitude of service
        time, up to and past the real machine's 2.18 s, AND the paired positive control still
        bites. Both halves are required: a verdict that is always False is as useless as one that
        is always True, and they look identical from the outside.
        """
        for service_s in (0.005, 0.05, 0.2, 1.0, 2.0):
            engine = SleepEngine(service_s=service_s)          # capacity None: cannot queue
            args = probe_args(arrival="poisson", rate="40", requests=40, arrival_seed=5,
                              queue_sample_interval=0.05)
            level = S.run_level(args, None, 40,
                                send=lambda c, salt, e=engine: e.one_request(c, args, salt),
                                make_client=lambda: NullClient(args.base_url, args.timeout))
            arrival = level["arrival"]
            self.assertEqual(level["error_count"], 0, "service_s=%.3f" % service_s)
            self.assertEqual(level["requests_ok"], 40, "service_s=%.3f" % service_s)
            self.assertIs(arrival["fell_behind"], False,
                          "service_s=%.3f: %s" % (service_s, arrival["fell_behind_basis"]))
            if service_s >= 0.1:
                self.assertGreater(arrival["completion_deficit_vs_offered_pct"], 5.0,
                                   "the drain bias itself must still be visible in the document")

        # the positive control, at a service time in the same range: a 20 req/s engine offered 40
        engine = SleepEngine(service_s=0.05, capacity=1)
        args = probe_args(arrival="poisson", rate="40", requests=60, arrival_seed=21,
                          queue_sample_interval=0.02)
        level = S.run_level(args, None, 60,
                            send=lambda c, salt: engine.one_request(c, args, salt),
                            make_client=lambda: NullClient(args.base_url, args.timeout))
        arrival = level["arrival"]
        self.assertIs(arrival["fell_behind"], True, arrival["fell_behind_basis"])
        self.assertGreater(arrival["queue_growth"]["growth_as_multiple_of_median_e2e"],
                           arrival["queue_growth"]["gate_growth_multiple"])

    @covers("D3-open-loop-level-crashes-the-load-shape-check")
    def test_an_open_loop_level_from_the_real_probe_passes_the_gate_on_disk(self):
        """WHAT THE ATTACK DOES. Puts a REAL open-loop level, exactly as the probe writes one,
        into a claims manifest and runs the gate over it. An open-loop level records concurrency as
        null, because concurrency is an outcome there and not an input.

        WHAT IT ACHIEVED BEFORE THE FIX. check_load_shape raised TypeError on int(None) and took
        the whole verifier down. Guarding the null was not enough either: the wave arithmetic
        structurally demands a concurrency and a whole number of waves, so the level then produced
        "level r40 declares no concurrency or request count", which is a correct reading of the
        wrong question. The existing test never showed any of this because it passed "levels": [].

        WHAT MUST HAPPEN NOW. The wave checks are skipped for an open-loop level and replaced by
        D6, which asks for the three things that actually make such a level readable: the rate that
        was asked for, the rate that was achieved, and the queue trace between them. Driven through
        the `gpubench verify` CLI over a manifest on disk, with a level lifted out of a real probe
        document rather than hand-written.
        """
        run = drive_probe(["--arrival", "poisson", "--rate", "40", "--requests", "20",
                           "--output-tokens", "4", "--warmup", "0",
                           "--queue-sample-interval", "0.02"])
        self.assertIsNone(run.exit_code, run.out)
        doc = run.docs["serve_bench_poisson.json"]
        probe_level = doc["levels"][0]
        self.assertIsNone(probe_level["concurrency"],
                          "an open-loop level must not invent a concurrency")

        # built the way a content module builds its level rows, straight from the document
        row = {"name": "r40",
               "concurrency": probe_level["concurrency"],
               "requests": probe_level["requests_attempted"],
               "duration_s": probe_level["duration_s"],
               "arrival": probe_level["arrival"]}
        rc, out, findings = self.run_verify_cli(self.manifest_for(doc["report"], [row]))
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.checks(findings, "D3"), [],
                         "the wave arithmetic was applied to a level that has no waves")
        self.assertEqual(self.checks(findings, "D4"), [], out)
        self.assertEqual(self.checks(findings, "D6"), [], out)

        # the negative control: strip what an open-loop level has to disclose, and D6 must fire
        stripped = dict(row["arrival"])
        for key in ("target_rate_req_s", "achieved_rate_req_s", "queue_depth", "queue_growth",
                    "requests_dispatched"):
            stripped.pop(key, None)
        rc2, out2, findings2 = self.run_verify_cli(
            self.manifest_for(doc["report"], [dict(row, arrival=stripped)]), name="stripped.json")
        self.assertEqual(rc2, 1, out2)
        self.assertTrue(self.checks(findings2, "D6", "error"), out2)
        self.assertEqual(self.checks(findings2, "D3"), [],
                         "an incomplete open-loop level must still not be asked about waves")

    @covers("D7-arrival-model-flipped-beside-a-closed-loop-note")
    def test_the_declared_arrival_model_is_read_against_its_own_note(self):
        """WHAT THE ATTACK DOES. Runs the harness closed loop and flips one string in the
        manifest: closed_loop becomes open_loop_poisson.

        WHAT IT ACHIEVED BEFORE THE FIX. It shipped clean. The document printed "arrival_model
        open_loop_poisson" directly beside "Fixed in-flight population per level; no independent
        arrival process", and nothing read the two together. A human would have needed one second.
        arrival_model was a string nobody compared against anything.

        WHAT MUST HAPPEN NOW. The model and the note travel as a pair from one switch in the
        probe, and D7 reads them together, in both directions. Driven through the verify CLI over
        the report block the REAL closed-loop probe wrote, so the note is the probe's own words and
        not a copy of the vocabulary.
        """
        run = drive_probe(["--concurrency", "2", "--requests", "4", "--output-tokens", "4",
                           "--warmup", "0"])
        self.assertIsNone(run.exit_code, run.out)
        report = run.docs["serve_bench.json"]["report"]
        self.assertEqual(report["arrival_model"], "closed_loop")
        self.assertIn("no independent arrival process", report["arrival_note"])

        rc, out, findings = self.run_verify_cli(self.manifest_for(report, []))
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.checks(findings, "D7"), [], out)

        flipped = dict(report, arrival_model="open_loop_poisson")
        rc2, out2, findings2 = self.run_verify_cli(self.manifest_for(flipped, []),
                                                  name="flipped.json")
        self.assertEqual(rc2, 1, out2)
        self.assertTrue(self.checks(findings2, "D7", "error"), out2)
        self.assertIn("two different experiments", " ".join(
            i["message"] for i in self.checks(findings2, "D7")))

        # and the reverse: a closed-loop declaration beside a note asserting independent arrivals
        open_note = dict(report, arrival_model="closed_loop",
                         arrival_note="Requests arrive independently of completions at the target "
                                      "rate.")
        rc3, out3, findings3 = self.run_verify_cli(self.manifest_for(open_note, []),
                                                  name="reverse.json")
        self.assertEqual(rc3, 1, out3)
        self.assertTrue(self.checks(findings3, "D7", "error"), out3)

    @covers("SERV-catch-up-burst-passes-span-against-span")
    def test_a_catch_up_burst_after_a_stall_is_not_read_as_a_faithful_schedule(self):
        """WHAT THE ATTACK DOES. Takes the CPU away from the generator for 600 ms in the middle of
        a level. Arrival deadlines are absolute, so the arrivals that came due during the stall all
        fire at once afterwards and the last dispatch lands back on its scheduled offset.

        WHAT IT ACHIEVED BEFORE THE FIX. The fidelity check compared the schedule's span against
        the dispatch span, and a stall followed by a catch-up burst leaves those equal to the
        millisecond: at rate 30 the two spans matched while 17 arrivals had fired back to back.
        The level was not the Poisson stream it declared, and a burst's queue is not the engine's
        fault, but the document said the generator kept up.

        WHAT MUST HAPPEN NOW. Fidelity is judged PER ARRIVAL against a fraction of the mean
        inter-arrival gap, and the span comparison is kept as its own field because it answers a
        different question. This test reads the written document: the span really is honoured and
        the verdict is still False. The printed lines must blame the generator and must not blame
        the engine, because the engine cannot be judged from a level whose arrivals were a burst.
        """
        state = {"n": 0}

        def stalling(seconds):
            state["n"] += 1
            time.sleep(0.6 if state["n"] == 5 else seconds)

        run = drive_probe(["--arrival", "poisson", "--rate", "30", "--requests", "60",
                           "--output-tokens", "4", "--warmup", "0",
                           "--queue-sample-interval", "0.05"],
                          engine=SleepEngine(service_s=0.001), sleep=stalling)
        self.assertIsNone(run.exit_code, run.out)
        arrival = run.level("serve_bench_poisson.json")["arrival"]
        fidelity = arrival["generator_fidelity"]
        self.assertIs(fidelity["schedule_span_honoured"], True,
                      "the span survived the stall, which is the trap this test exists for")
        self.assertIs(arrival["generator_kept_up"], False, json.dumps(fidelity))
        self.assertGreater(fidelity["p95_abs_deviation_ms"], fidelity["budget_ms"])
        self.assertGreater(arrival["dispatch_lateness_ms"]["max"], 400.0)
        self.assertIn("the GENERATOR missed its own schedule", run.out)
        self.assertNotIn("the ENGINE did not keep up", run.out)

    @covers("SERV-vanished-request-with-no-error-recorded")
    def test_a_request_that_vanishes_is_booked_rather_than_lost(self):
        """WHAT THE ATTACK DOES. Makes one client refuse to be constructed, mid-level, with the
        error a real harness hits first: EMFILE, too many open files.

        WHAT IT ACHIEVED BEFORE THE FIX. make_client ran BEFORE the guarded block, and only the
        completion path booked an outcome or released the flight. The result was
        requests_attempted=10, requests_ok=9, error_count=0 and errors=[]: a request vanished with
        no error recorded anywhere. It broke the accounting in the direction that LOWERS the
        completion rate, so it fed a false "the engine did not keep up", and it left the in-flight
        count one too high for the rest of the level.

        WHAT MUST HAPPEN NOW. Every dispatched request books exactly one outcome. The identity
        attempted == ok + engine errors + harness errors is asserted on the WRITTEN DOCUMENT, and
        the failure is filed against the harness rather than the engine, because a client-side
        ceiling says nothing whatever about the server.
        """
        class ExhaustedClient(NullClient):
            built = 0

            def __init__(self, base_url, timeout):
                ExhaustedClient.built += 1
                # 1 is the probe's reachability check, 2 is the level's metrics client, so this
                # lands on the fifth arrival of the level.
                if ExhaustedClient.built == 7:
                    raise OSError(errno.EMFILE, "Too many open files")
                NullClient.__init__(self, base_url, timeout)

        run = drive_probe(["--arrival", "poisson", "--rate", "100", "--requests", "10",
                           "--output-tokens", "4", "--warmup", "0",
                           "--queue-sample-interval", "0.02"],
                          engine=SleepEngine(service_s=0.005), client_cls=ExhaustedClient)
        self.assertIsNone(run.exit_code, run.out)
        level = run.level("serve_bench_poisson.json")
        self.assertEqual(level["requests_unaccounted"], 0,
                         "a dispatched request booked no outcome at all")
        self.assertEqual(level["harness_error_count"], 1, json.dumps(level["error_stages"]))
        self.assertEqual(level["error_count"], 0,
                         "a client this side of the wire is not the engine refusing work")
        self.assertEqual(level["requests_ok"] + level["error_count"] + level["harness_error_count"],
                         level["requests_attempted"])
        self.assertEqual(level["error_stages"], {"harness_client_setup": 1})
        self.assertLessEqual(level["arrival"]["queue_depth"]["max"], level["requests_dispatched"],
                             "a leaked flight slot would let the trace exceed what was dispatched")

    @covers("SERV-declared-rate-sweep-that-never-ran")
    def test_a_rate_sweep_that_cannot_run_writes_no_document_at_all(self):
        """WHAT THE ATTACK DOES. Asks for a rate sweep alongside a length sweep:
        --arrival poisson --rate 2,4,8 --mode prefill.

        WHAT IT ACHIEVED BEFORE THE FIX. The document DECLARED target_rate_req_s [2.0, 4.0, 8.0]
        and actually ran [2.0, 2.0]. main() took the rate from rates[0] for every level of those
        modes while the declaration came from the whole parsed string, so the file asserted a sweep
        that never happened, in the field a reader and the gate both believe.

        WHAT MUST HAPPEN NOW. Refused with exit 2, and, which is the part a return value cannot
        show, NO DOCUMENT IS WRITTEN: there is no file left behind declaring the sweep. The
        positive control is the mode where a rate sweep is real, and there the declaration is
        derived FROM the levels, so it cannot outrun the run whatever the parsed string said.
        """
        for mode in ("prefill", "decode", "mixed"):
            run = drive_probe(["--arrival", "poisson", "--rate", "2,4,8", "--mode", mode,
                               "--requests", "6", "--output-tokens", "4", "--warmup", "0",
                               "--input-lengths", "128,512", "--output-lengths", "16,32"])
            self.assertEqual(run.exit_code, 2, "mode=%s: %s" % (mode, run.out))
            self.assertEqual(run.docs, {},
                             "mode=%s left a document declaring a sweep it never ran: %s"
                             % (mode, sorted(run.docs)))

        run = drive_probe(["--arrival", "poisson", "--rate", "20,40,80", "--requests", "8",
                           "--output-tokens", "4", "--warmup", "0",
                           "--queue-sample-interval", "0.02"])
        self.assertIsNone(run.exit_code, run.out)
        doc = run.docs["serve_bench_poisson.json"]
        self.assertEqual([lvl["arrival"]["target_rate_req_s"] for lvl in doc["levels"]],
                         [20.0, 40.0, 80.0])
        self.assertEqual(doc["arrival"]["target_rate_req_s"], [20.0, 40.0, 80.0])
        self.assertEqual(doc["config"]["rate"], "20,40,80")


# --------------------------------------------------------------------------------------
# 6. SELF DEFEAT: the meta attacks, aimed at the gate rather than at a number


class TestSelfDefeat(ArticleCase):
    """Every way of NOT publishing a checked report has to be non-zero and has to leave no file,
    because the exit code is the only thing a pipeline reads and a file is what gets sent."""

    @covers("GATE-renamed-manifest-disarms-the-gate")
    def test_every_way_of_not_publishing_exits_non_zero_and_writes_nothing(self):
        """The exit-code contract, in one table. A blocked gate, a miswired gate and a gate that
        was never armed are three different faults with one thing in common: none of them is a
        published report. Before the fix, the third of those exited 0.
        """
        cases = [
            ("the gate blocked", {"total": "33.0"}, 1),
            ("the gate was miswired", {"manifest_attr": "CLAIMS_MANIFEST"}, 2),
            ("the gate was never armed", {"manifest_attr": "CLAIMS_MANIFEST",
                                          "claims_attr": "_claims"}, 1),
        ]
        for label, opts, expected in cases:
            self.clear_out()
            build = self.article(**opts)
            self.assertEqual(build.rc, expected, "%s: %s" % (label, build.out))
            self.assertNothingPublished(build)

        self.clear_out()
        published = self.article()
        self.assertEqual(published.rc, 0, published.out)
        self.assertTrue(published.report_exists())
        self.assertTrue(published.index_exists())

    @covers("CLI-no-verify-emits-an-unmarked-publishable-file")
    def test_no_verify_writes_a_draft_that_cannot_be_mistaken_for_a_report(self):
        """WHAT THE ATTACK DOES. Uses the escape hatch as a publishing route: run the failing
        report with --no-verify, then mail the file that appears.

        WHAT IT ACHIEVED BEFORE THE FIX. A file appeared. A flag on a command line leaves no trace
        in the artefact, so the document was indistinguishable from a verified one once it left the
        build directory, and a build log saying "skipped" names nothing that was skipped.

        WHAT MUST HAPPEN NOW. The gate still runs, the manifest and the findings are still written,
        the log names the check ids it overrode, and every document written carries the draft stamp
        in two forms: a visible banner for the reader and an HTML comment marker a mail rule or a
        grep can refuse to send. The companion pages carry it too, because a stamped report beside
        an unstamped appendix is an unstamped document. And --no-verify is not a substitute for
        --allow-ungated: a gate that failed and a gate that was never armed are different problems.
        """
        draft = self.article("--no-verify", total="33.0")
        # A stamp a reader can see is not enough on its own: a pipeline reads the exit code and
        # nothing else, so a suppressed error exits DRAFT_EXIT and never 0.
        self.assertEqual(draft.rc, cli.DRAFT_EXIT, draft.out)
        self.assertTrue(draft.report_exists())
        self.assertIn("SKIPPED", draft.out)
        self.assertIn("B1", draft.out, "the log must name the check it overrode")
        self.assertIn("not for publication", draft.out.lower())
        for name in (draft.STEM + ".html", "index.html", "method-primer.html"):
            html = draft.read(name)
            self.assertIn(longform.DRAFT_MARKER, html, "%s carries no pipeline marker" % name)
            self.assertIn("NOT FOR PUBLICATION", html, "%s carries no visible banner" % name)
        self.assertTrue(draft.checks("B1", "error"),
                        "the findings file is the record and must still hold the error")

        # a passing report is not stamped, or the stamp would mean nothing
        self.clear_out()
        clean = self.article("--no-verify")
        self.assertEqual(clean.rc, 0, clean.out)
        self.assertNotIn(longform.DRAFT_MARKER, clean.read("index.html"))
        self.assertIn("0 error(s) suppressed", clean.out)

        # and it does not stand in for --allow-ungated
        self.clear_out()
        unarmed = self.article("--no-verify", manifest_attr="CLAIMS_MANIFEST",
                               claims_attr="_claims")
        self.assertNotEqual(unarmed.rc, 0, unarmed.out)
        self.assertNothingPublished(unarmed)

    @covers("A10-shrunken-manifest")
    def test_the_gate_states_its_own_jurisdiction_on_a_pass_and_on_a_failure(self):
        """The question behind the shrunken manifest: can a reader of the build log tell "0 errors
        over a manifest that asserts everything" from "0 errors over one that asserts almost
        nothing"? Before the fix the two log lines were identical, and the shrunken manifest's was
        the cleaner of the two. Now the passing line states what was DECLARED, and the failing line
        states what the numeral checks had jurisdiction over, measured against the document, so
        shrinking the manifest lowers the number rather than raising it.
        """
        clean = self.article()
        self.assertEqual(clean.rc, 0, clean.out)
        self.assertEqual(clean.declared(), (3, 1, 1),
                         "the passing log line must state the shape of the evidence")
        self.clear_out(keep_manifest=True)

        shrunk = self.article(extra=SHRINK)
        self.assertNotEqual(shrunk.rc, 0, shrunk.out)
        covered, total, floor = shrunk.coverage()
        self.assertEqual(floor, 100.0)
        self.assertLess(covered, total,
                        "coverage is measured against the document, so shrinking must lower it")
        self.assertIn("floor", shrunk.out)


# --------------------------------------------------------------------------------------
# the register itself


class TestTheRegisterIsWhole(unittest.TestCase):
    """If this file stops covering the full set, that has to be a FAILURE and not a silent gap.

    Deleting a test, or renaming a hole, breaks this. That is the whole reason the register exists:
    a suite of attacks is only worth having while it is known to be complete, and "23 tests once
    existed" is not a property anything can check.
    """

    @staticmethod
    def coverage_map():
        out = {}
        for class_name, obj in sorted(globals().items()):
            if not (isinstance(obj, type) and issubclass(obj, unittest.TestCase)):
                continue
            for attr in sorted(dir(obj)):
                if not attr.startswith("test"):
                    continue
                for hole_id in getattr(getattr(obj, attr), "holes", ()):
                    out.setdefault(hole_id, []).append("%s.%s" % (class_name, attr))
        return out

    def test_the_register_holds_exactly_the_twenty_three_holes(self):
        self.assertEqual(len(HOLES), 23,
                         "the register must hold the 23 attacks that landed, no more and no fewer")
        for hole_id, why in HOLES.items():
            self.assertTrue(why.strip(), "%s records no description" % hole_id)

    def test_every_hole_has_at_least_one_test_naming_it(self):
        covered = self.coverage_map()
        missing = sorted(set(HOLES) - set(covered))
        self.assertEqual(missing, [],
                         "these attacks are in the register and no test drives them: %s" % missing)

    def test_no_test_claims_a_hole_the_register_does_not_know(self):
        unknown = sorted(set(self.coverage_map()) - set(HOLES))
        self.assertEqual(unknown, [],
                         "a test names a hole id that is not in the register: %s" % unknown)


if __name__ == "__main__":
    # descriptions=False so the verbose run lists the ATTACKS BY NAME rather than the first line of
    # each docstring. Every test here opens with "WHAT THE ATTACK DOES", so the default output was
    # thirty near-identical lines and a reader could not tell which attack had just run. The
    # docstrings are for whoever opens the file; the run is a checklist.
    unittest.main(testRunner=unittest.TextTestRunner(verbosity=2, descriptions=False))
