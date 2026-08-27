#!/usr/bin/env python3
"""Tests for the pre-render claims gate. No GPU, no network, no real report.

WHAT IS BEING PROVEN. `gpubench verify` existed for a while as a command someone could choose to
run, and the article command never called it. That is the same as not having a gate: the one
edition that needs it is the edition where nobody thinks to run it. So the property under test is
not "the verifier finds defects" (tests elsewhere cover that) but "a report that fails
verification does not exist as a file", because a file is the thing that gets sent to people.

Four ways of NOT publishing a checked report are covered here, and every one of them exits
non-zero, because the exit code is the only thing a pipeline reads:

    the gate blocked            errors in the manifest
    the gate was miswired       MANIFEST without claims(), or the reverse
    the gate was never armed    neither declared, and --allow-ungated was not given
    the file never appeared     the gate passed and the write failed

Each test builds a synthetic content module in a temp dir (the smallest thing that satisfies the
longform contract) and runs the real CLI over it in-process.

Run:  python -m tests.test_gate      (from the repo root)
"""
import importlib.util
import io
import json
import os
import py_compile
import re
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

sys.path.insert(0, ".")
from gpubench import cli  # noqa: E402
from gpubench import longform  # noqa: E402


# --------------------------------------------------------------------------------------
# the synthetic content module
#
# Two measured claims and one derived from them. That is enough to carry the defect class that
# survived three published editions: a derived value printed as a typed constant, which drifts the
# moment either input is re-measured.
#
# The body carries a figure with a real table view, and the manifest declares that table's cells,
# because a figure whose table view is declared and missing is itself a defect the gate checks
# for. A fixture has to be a document that could legitimately be published.

HEAD = '''\
"""Synthetic content module. Exists only for tests/test_gate.py."""
import io
import os

TITLE = "Synthetic Report"
BASENAME = "synthetic"
VERSION = "1.0"
SECTION_ORDER = ["Findings"]

CALL_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "claims-calls.log")


GATE_ARTEFACT = "gate-result.json"


def build(run_dir, out_dir=None):
    # A REAL gate artefact, written where the manifest says it is. G3 reads the file rather than
    # the manifest's own passed/cases_published booleans, so a fixture that only DECLARED a passing
    # gate would be exercising the escape hatch instead of the check.
    if out_dir:
        import json
        with io.open(os.path.join(out_dir, GATE_ARTEFACT), "w", encoding="utf-8",
                     newline="\\n") as f:
            f.write(json.dumps({"probes": {"accuracy": {
                "summary": {"cases": 2, "deterministic": 2, "exact_match_pct": 100.0},
                "method": {"cases_published": [{"id": "c1"}, {"id": "c2"}]},
                "errors": [],
            }}}, indent=2))
    return {}, {"a": 10.0, "b": 20.0}


def render(figures, data):
    return ("<section><h2>Findings</h2>"
            "<p>Together they reach 30.0 tok/s.</p>"
            \'<figure id="fig1_throughput"><figcaption>Figure 1. Throughput</figcaption>\'
            "<table><thead><tr><th>series</th><th>tok/s</th></tr></thead>"
            "<tbody><tr><td>a</td><td>10.0</td></tr>"
            "<tr><td>b</td><td>20.0</td></tr></tbody></table>"
            "</figure></section>")
'''

MANIFEST_DECL = '''
MANIFEST = "claims.json"


def claims(figures, data):
    # Every call is recorded, so a test can prove the engine asks for the manifest ONCE per build.
    # Two calls meant two independently produced copies at one path, and the file on disk was then
    # not necessarily the file the gate judged.
    with io.open(CALL_LOG, "a", encoding="utf-8", newline="\\n") as f:
        f.write("call\\n")
    m = {
        "schema": "claims/1",
        "report": {"version": VERSION, "arrival_model": "closed_loop"},
        "runs": {"primary": {"started": "2026-08-25T11:01:00Z", "artifact": GATE_ARTEFACT}},
        "claims": {
            "throughput_a": {"value": data["a"], "unit": "tok/s", "basis": "total",
                             "kind": "measured", "run": "primary",
                             "measured_at": "2026-08-25T11:05:00Z"},
            "throughput_b": {"value": data["b"], "unit": "tok/s", "basis": "total",
                             "kind": "measured", "run": "primary",
                             "measured_at": "2026-08-25T11:05:00Z"},
            "throughput_total": {"value": %(total)s, "unit": "tok/s", "basis": "total",
                                 "kind": "derived",
                                 "formula": "throughput_a + throughput_b"},
        },
        "prose": [{"id": "findings",
                   "text": "Together they reach {{throughput_total}} tokens per second."}],
        "tables": {"fig1_throughput": {"cells": ["throughput_a", "throughput_b"]}},
        "figures": [{"id": "fig1_throughput", "table_view": True}],
        "gate": {"ran_at": "2026-08-25T19:37:00Z", "passed": True, "cases_published": True,
                 "window_run": "primary"},
    }
%(extra)s
    return m
'''

# A sustained figure that states its duration and does not say whether the quantity had stopped
# moving: a D5 WARNING, not an error. Used to prove --warnings-as-errors changes the outcome and
# nothing else does. It used to be two claims sharing a label with different values; that is an
# ERROR now (A7), because the 233-versus-204.5 defect the tool exists for must block.
WARNING_ONLY = '''\
    m["sustained"] = [{"key": "throughput_a", "duration_s": 30.0}]
'''

# Declare LESS than the edition before: one claim, no prose, no figures. Everything else about the
# manifest still verifies, which is the whole problem: shrinking was the cheapest way to a clean
# run, and it printed "1 claim(s), 0 warning(s)" beside a body that still carried a wrong number.
SHRUNK = '''\
    del m["claims"]["throughput_b"]
    del m["claims"]["throughput_total"]
    m["prose"] = []
    m["figures"] = []
    m["tables"] = {}
'''

# Same numbers, weaker evidence: a measured claim restated as an assumption. Nothing visible in
# the document changes, which is why only a comparison against the previous edition can see it.
DEMOTED = '''\
    m["claims"]["throughput_a"]["kind"] = "assumption"
'''
DEMOTED_WITH_CHANGELOG = DEMOTED + '''\
    m["changelog"] = [{"version": VERSION, "claims_changed": ["throughput_a"]}]
'''


def content_module(dirpath, total="30.0", manifest=True, claims_fn=True, extra="",
                   name="content_mod.py"):
    """Write a content module and return its path.

    total="30.0"  -> the derived claim recomputes; the gate passes.
    total="33.0"  -> printed 33 where the formula gives 30. The gate must block.
    """
    src = HEAD
    if manifest and claims_fn:
        src += MANIFEST_DECL % {"total": total, "extra": extra}
    elif manifest:
        src += '\nMANIFEST = "claims.json"\n'   # half-wired: declares the file, defines no claims()
    elif claims_fn:
        src += '\n\ndef claims(figures, data):\n    return {"schema": "claims/1", "claims": {}}\n'
    path = os.path.join(dirpath, name)
    with io.open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(src)
    # THE SAME BYTECODE TRAP THE ENGINE HAD, latent here: several tests write DIFFERENT editions of
    # a module to ONE path within the same clock second, and Python trusts a cached .pyc whenever
    # the source's recorded mtime second and size both match. It was masked only because the
    # editions happened to differ in length, which is not a property a test may rest on. The cache
    # for this path is removed on every write, so what runs is what was just written.
    try:
        cached = importlib.util.cache_from_source(path)
    except (NotImplementedError, ValueError):
        cached = None
    if cached and os.path.exists(cached):
        os.remove(cached)
    return path


def warning_count(out):
    """The warning count the gate reported, read back out of its own log line."""
    m = re.search(r"(\d+) warning\(s\)", out)
    return int(m.group(1)) if m else -1


class GateCase(unittest.TestCase):
    """Runs the real `gpubench article` in-process and reports what reached disk."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gpubench-gate-test-")
        self.run_dir = os.path.join(self.tmp, "run")
        self.out_dir = os.path.join(self.tmp, "out")
        os.makedirs(self.run_dir)
        os.makedirs(self.out_dir)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def article(self, content_path, *flags):
        buf = io.StringIO()
        argv = ["article", content_path, self.run_dir, "--out-dir", self.out_dir] + list(flags)
        with redirect_stdout(buf), redirect_stderr(buf):
            rc = cli.main(argv)
        return rc, buf.getvalue()

    # convenience predicates, named after what a reader would ask
    def report_exists(self):
        return os.path.exists(os.path.join(self.out_dir, "synthetic-v1.0.html"))

    def index_exists(self):
        return os.path.exists(os.path.join(self.out_dir, "index.html"))

    def manifest_path(self):
        return os.path.join(self.out_dir, "claims.json")

    def findings(self):
        with io.open(os.path.join(self.out_dir, "claims-findings.json"), encoding="utf-8") as f:
            return json.load(f)

    def index_html(self):
        with io.open(os.path.join(self.out_dir, "index.html"), encoding="utf-8") as f:
            return f.read()

    def claims_calls(self):
        """How many times the content module's claims() was called during the build."""
        path = os.path.join(self.tmp, "claims-calls.log")
        if not os.path.exists(path):
            return 0
        with io.open(path, encoding="utf-8") as f:
            return len([ln for ln in f.read().splitlines() if ln.strip()])


class TestPassingReportRenders(GateCase):

    def test_a_manifest_that_verifies_renders(self):
        rc, out = self.article(content_module(self.tmp, total="30.0"))
        self.assertEqual(rc, 0, out)
        self.assertTrue(self.report_exists(), out)
        self.assertTrue(self.index_exists(), out)
        self.assertIn("manifest verified", out)

    def test_the_manifest_is_written_beside_the_report(self):
        """The manifest is the artifact that makes the report auditable later; it ships too."""
        rc, out = self.article(content_module(self.tmp, total="30.0"))
        self.assertEqual(rc, 0, out)
        self.assertTrue(os.path.exists(self.manifest_path()))
        with io.open(self.manifest_path(), encoding="utf-8") as f:
            m = json.load(f)
        self.assertEqual(m["claims"]["throughput_total"]["value"], 30.0)

    def test_the_log_states_what_was_declared_not_just_that_it_passed(self):
        """"1 claim(s)" beside a 40-page report is the tell that a manifest shrank. Print the
        counts, so a reader of the build log can see the shape of the evidence."""
        _rc, out = self.article(content_module(self.tmp, total="30.0"))
        self.assertIn("3 claim(s)", out)
        self.assertIn("1 prose block(s)", out)
        self.assertIn("1 figure(s)", out)

    def test_a_published_report_carries_no_draft_stamp(self):
        _rc, out = self.article(content_module(self.tmp, total="30.0"))
        self.assertNotIn(longform.DRAFT_MARKER, self.index_html())
        self.assertNotIn("NOT FOR PUBLICATION", self.index_html())
        self.assertNotIn("stamped:", out)


class TestFailingReportDoesNotExist(GateCase):
    """The whole point. A blocked report must not be a file."""

    def setUp(self):
        super().setUp()
        self.rc, self.out = self.article(content_module(self.tmp, total="33.0"))

    def test_exit_code_is_non_zero(self):
        self.assertNotEqual(self.rc, 0)

    def test_the_report_file_is_absent(self):
        self.assertFalse(self.report_exists(),
                         "a report that failed verification was written to disk")

    def test_the_index_copy_is_absent(self):
        """The index copy is what a browser lands on, so it is a published file like any other."""
        self.assertFalse(self.index_exists())

    def test_nothing_html_at_all_was_written(self):
        stray = [n for n in os.listdir(self.out_dir) if n.endswith((".html", ".pdf", ".docx"))]
        self.assertEqual(stray, [], "blocked render left artifacts behind: %s" % stray)

    def test_the_manifest_is_still_written_so_it_can_be_inspected(self):
        self.assertTrue(os.path.exists(self.manifest_path()),
                        "the gate blocked without leaving the evidence it judged on")

    def test_the_findings_are_written_as_json(self):
        p = os.path.join(self.out_dir, "claims-findings.json")
        self.assertTrue(os.path.exists(p))
        self.assertTrue(any(i["check"] == "B1" for i in self.findings()), self.findings())

    def test_the_findings_are_printed(self):
        self.assertIn("B1", self.out)
        self.assertIn("does not recompute", self.out)

    def test_it_names_the_permitted_responses(self):
        for expected in ("FIX THE GENERATOR", "FIX THE PROSE", "RE-MEASURE",
                         "DECLARE THE EXCEPTION"):
            self.assertIn(expected, self.out)

    def test_it_forbids_editing_a_measured_value(self):
        """A gate that blocks on a mismatch and does not say this invites the worst fix: a report
        that no longer disagrees with itself because it now disagrees with the machine."""
        self.assertIn("NEVER edit a measured value", self.out)

    def test_it_says_which_files_were_not_written(self):
        self.assertIn("NOT WRITTEN", self.out)
        self.assertIn("synthetic-v1.0.html", self.out)


class TestUnarmedGateIsAnError(GateCase):
    """An absent gate USED to render and exit 0. That made a fully gated build and a completely
    ungated one indistinguishable to a pipeline, which reads the exit code and nothing else.
    Renaming MANIFEST and claims() in a content module was therefore a silent way to disarm the
    gate. Absence is now no softer than half-wiring."""

    def test_an_unarmed_gate_writes_nothing_and_exits_non_zero(self):
        rc, out = self.article(content_module(self.tmp, manifest=False, claims_fn=False))
        self.assertEqual(rc, 1, out)
        self.assertFalse(self.report_exists(), out)
        self.assertFalse(self.index_exists(), out)
        self.assertIn("NOT WRITTEN", out)

    def test_it_says_the_gate_is_not_armed(self):
        """Silence here would read as a pass. An ungated render has to announce itself."""
        _rc, out = self.article(content_module(self.tmp, manifest=False, claims_fn=False))
        self.assertIn("NOT ARMED", out)

    def test_it_names_the_flag_that_would_allow_it(self):
        """A blocking error has to say what the legitimate escape is, or it reads as a bug."""
        _rc, out = self.article(content_module(self.tmp, manifest=False, claims_fn=False))
        self.assertIn("--allow-ungated", out)

    def test_no_manifest_file_is_invented(self):
        self.article(content_module(self.tmp, manifest=False, claims_fn=False))
        self.assertFalse(os.path.exists(self.manifest_path()))


class TestAllowUngatedRendersALegacyReport(GateCase):
    """The engine predates the manifest, and those reports must keep building, with one explicit
    gesture, and with the output saying what it is."""

    def setUp(self):
        super().setUp()
        self.rc, self.out = self.article(
            content_module(self.tmp, manifest=False, claims_fn=False), "--allow-ungated")

    def test_it_renders_and_exits_non_zero_because_it_is_a_draft(self):
        """It writes a file, and the exit code says the file is not publishable. Exiting 0 here
        made an escape hatch indistinguishable from a clean build to the only thing a pipeline
        reads, which is the whole reason the stamp exists."""
        self.assertEqual(self.rc, cli.DRAFT_EXIT, self.out)
        self.assertNotEqual(self.rc, 0)
        self.assertTrue(self.report_exists(), self.out)

    def test_it_still_says_the_gate_is_not_armed(self):
        self.assertIn("NOT ARMED", self.out)

    def test_the_document_says_it_is_not_for_publication(self):
        """The HTML is the thing that gets mailed. It has to carry the warning itself, because the
        build log stays behind on the machine that made it."""
        html = self.index_html()
        self.assertIn("NOT FOR PUBLICATION", html)
        self.assertIn(longform.DRAFT_MARKER, html)

    def test_the_stamp_is_greppable_by_a_pipeline(self):
        """A visible banner warns a person. The comment marker is what a build step can refuse on."""
        self.assertIn("<!-- gpubench-draft-not-for-publication", self.index_html())

    def test_no_manifest_file_is_invented(self):
        self.assertFalse(os.path.exists(self.manifest_path()))


class TestHalfWiredGate(GateCase):
    """A module with MANIFEST and no claims() looks gated and checks nothing. Say so, loudly."""

    def test_manifest_without_claims_is_a_wiring_fault(self):
        rc, out = self.article(content_module(self.tmp, claims_fn=False))
        self.assertEqual(rc, 2, out)
        self.assertIn("MISWIRED", out)
        self.assertFalse(self.report_exists())

    def test_claims_without_manifest_is_a_wiring_fault(self):
        rc, out = self.article(content_module(self.tmp, manifest=False))
        self.assertEqual(rc, 2, out)
        self.assertIn("MISWIRED", out)
        self.assertFalse(self.report_exists())

    def test_allow_ungated_does_not_excuse_a_half_wired_gate(self):
        """The flag says "this report has no gate". A half-wired one claims to have a gate."""
        rc, out = self.article(content_module(self.tmp, claims_fn=False), "--allow-ungated")
        self.assertEqual(rc, 2, out)
        self.assertFalse(self.report_exists())


class TestNoVerifyOverride(GateCase):

    def setUp(self):
        super().setUp()
        self.rc, self.out = self.article(content_module(self.tmp, total="33.0"), "--no-verify")

    def test_no_verify_renders_a_failing_draft_and_exits_non_zero(self):
        self.assertEqual(self.rc, cli.DRAFT_EXIT, self.out)
        self.assertTrue(self.report_exists(), self.out)
        self.assertIn("EXIT %d" % cli.DRAFT_EXIT, self.out)

    def test_no_verify_says_the_draft_is_not_for_publication(self):
        self.assertIn("SKIPPED", self.out)
        self.assertIn("for inspection, not publication", self.out)

    def test_it_names_the_checks_it_overrode(self):
        """"Skipped" says something was disabled. It does not say WHAT, and the difference between
        one suppressed warning and eleven suppressed errors is the whole question."""
        self.assertRegex(self.out, r"SKIPPED: \d+ error\(s\) suppressed: ")
        self.assertIn("B1", self.out)

    def test_the_manifest_and_the_findings_are_both_written(self):
        """The help text promised both. Only the manifest was appearing."""
        self.assertTrue(os.path.exists(self.manifest_path()), self.out)
        self.assertTrue(any(i["check"] == "B1" for i in self.findings()), self.findings())

    def test_the_draft_is_stamped_in_the_document(self):
        """Without this the HTML that gets emailed is indistinguishable from a gated one."""
        html = self.index_html()
        self.assertIn("NOT FOR PUBLICATION", html)
        self.assertIn(longform.DRAFT_MARKER, html)
        self.assertIn("no-verify", html)

    def test_the_companion_pages_are_stamped_too(self):
        """A companion is a page that gets read on its own, so it needs its own warning."""
        src = content_module(self.tmp, total="33.0", name="with_companion.py")
        with io.open(src, "a", encoding="utf-8", newline="\n") as f:
            f.write('\n\nCOMPANIONS = {"primer.html": ("Primer", "render_primer")}\n'
                    '\n\ndef render_primer(figures, data):\n'
                    '    return "<section><h2>Primer</h2><p>How it works.</p></section>"\n')
        rc, out = self.article(src, "--no-verify")
        self.assertEqual(rc, cli.DRAFT_EXIT, out)
        with io.open(os.path.join(self.out_dir, "primer.html"), encoding="utf-8") as f:
            self.assertIn(longform.DRAFT_MARKER, f.read())

    def test_the_help_text_says_what_it_disables(self):
        """The flag exists to inspect a failing draft. Anyone reading --help must not be able to
        mistake it for a way to publish one. Whitespace is normalised because argparse wraps to the
        terminal width, and a promise must not depend on where the line broke."""
        buf = io.StringIO()
        with redirect_stdout(buf):
            with self.assertRaises(SystemExit):
                cli.main(["article", "--help"])
        help_text = re.sub(r"\s+", " ", buf.getvalue())
        self.assertIn("--no-verify", help_text)
        self.assertIn("DISABLES", help_text)
        self.assertIn("NOT for publishing one", help_text)
        # The help text is a promise about behaviour, so it names what is still produced.
        self.assertIn("manifest and findings are still written", help_text)


class TestNoVerifyOnAPassingReport(GateCase):
    """The flag was given and there was nothing to override. Saying "skipped" would misreport the
    build, and stamping a verified document would cry wolf."""

    def setUp(self):
        super().setUp()
        self.rc, self.out = self.article(content_module(self.tmp, total="30.0"), "--no-verify")

    def test_it_renders_and_says_the_gate_passed_anyway(self):
        self.assertEqual(self.rc, 0, self.out)
        self.assertIn("manifest verified", self.out)
        self.assertIn("0 error(s) suppressed", self.out)

    def test_a_verified_document_is_not_stamped(self):
        self.assertNotIn(longform.DRAFT_MARKER, self.index_html())


class TestWarningsAsErrors(GateCase):

    def test_warnings_alone_do_not_block_by_default(self):
        rc, out = self.article(content_module(self.tmp, total="30.0", extra=WARNING_ONLY))
        self.assertEqual(rc, 0, out)
        self.assertTrue(self.report_exists(), out)
        self.assertGreaterEqual(warning_count(out), 1, out)

    def test_warnings_as_errors_blocks_the_render(self):
        rc, out = self.article(content_module(self.tmp, total="30.0", extra=WARNING_ONLY),
                               "--warnings-as-errors")
        self.assertNotEqual(rc, 0)
        self.assertFalse(self.report_exists(), out)
        self.assertIn("treated as errors", out)

    def test_no_verify_names_the_warnings_it_overrode(self):
        """With --warnings-as-errors it is warnings that get suppressed, and the log has to say so
        rather than report zero errors and leave it there."""
        rc, out = self.article(content_module(self.tmp, total="30.0", extra=WARNING_ONLY),
                               "--warnings-as-errors", "--no-verify")
        self.assertEqual(rc, cli.DRAFT_EXIT, out)
        self.assertRegex(out, r"SKIPPED: \d+ warning\(s\) suppressed: ")
        self.assertIn(longform.DRAFT_MARKER, self.index_html())


class TestDeclarationFloor(GateCase):
    """H2. Nothing established a floor on what must be DECLARED, so a manifest that shrank passed
    more cleanly than the honest one: one claim, no prose, no figures, "0 warning(s)", and a wrong
    number still in the body. The previous edition is the only floor there is."""

    def build_baseline(self):
        rc, out = self.article(content_module(self.tmp, total="30.0"))
        self.assertEqual(rc, 0, out)
        return out

    def test_a_first_build_has_no_baseline_and_says_so(self):
        out = self.build_baseline()
        self.assertIn("NO BASELINE", out)
        self.assertIn("NOT checked", out)

    def test_the_second_build_names_the_baseline_it_used(self):
        self.build_baseline()
        rc, out = self.article(content_module(self.tmp, total="30.0"))
        self.assertEqual(rc, 0, out)
        self.assertIn("baseline", out)
        self.assertIn("3 claim(s), 1 prose block(s), 1 figure(s)", out)

    def test_a_shrunken_manifest_is_blocked(self):
        self.build_baseline()
        before = self.index_html()
        rc, out = self.article(content_module(self.tmp, total="30.0", extra=SHRUNK))
        self.assertNotEqual(rc, 0, out)
        # The baseline build legitimately left a report in out_dir, so the property is that THIS
        # build published nothing: the files on disk are still the previous edition's.
        self.assertIn("NOT WRITTEN", out)
        self.assertEqual(self.index_html(), before)
        self.assertTrue(any(i["check"] == "A10" for i in self.findings()), self.findings())
        self.assertIn("A10", out)

    def test_it_names_what_went_missing(self):
        self.build_baseline()
        _rc, out = self.article(content_module(self.tmp, total="30.0", extra=SHRUNK))
        self.assertIn("throughput_b", out)
        self.assertIn("prose block(s)", out)
        self.assertIn("figure(s)", out)

    def test_a_claim_resting_on_weaker_evidence_is_blocked(self):
        """The printed number does not move when a measurement becomes an assumption, so no other
        check in the gate can see it."""
        self.build_baseline()
        rc, out = self.article(content_module(self.tmp, total="30.0", extra=DEMOTED))
        self.assertNotEqual(rc, 0, out)
        self.assertIn("measured -> assumption", out)

    def test_a_changelog_row_permits_the_change(self):
        """The floor is not a ban. It requires a sentence someone wrote, and an absence is not one."""
        self.build_baseline()
        rc, out = self.article(content_module(self.tmp, total="30.0",
                                              extra=DEMOTED_WITH_CHANGELOG))
        self.assertEqual(rc, 0, out)
        self.assertTrue(self.report_exists(), out)

    def test_an_explicit_previous_flag_is_honoured(self):
        self.build_baseline()
        saved = os.path.join(self.tmp, "claims-prev.json")
        shutil.copyfile(self.manifest_path(), saved)
        os.remove(self.manifest_path())     # no default baseline left in out_dir
        rc, out = self.article(content_module(self.tmp, total="30.0", extra=SHRUNK),
                               "--previous", saved)
        self.assertNotEqual(rc, 0, out)
        self.assertIn("A10", out)

    def test_a_previous_flag_pointing_at_nothing_is_an_error(self):
        """Asking for a baseline and silently getting none is the state the floor cannot see in."""
        rc, out = self.article(content_module(self.tmp, total="30.0"),
                               "--previous", os.path.join(self.tmp, "absent.json"))
        self.assertEqual(rc, 2, out)
        self.assertFalse(self.report_exists())

    def test_an_unreadable_baseline_is_an_error_not_a_missing_one(self):
        self.build_baseline()
        with io.open(self.manifest_path(), "w", encoding="utf-8", newline="\n") as f:
            f.write("{ this is not json")
        rc, out = self.article(content_module(self.tmp, total="30.0"))
        self.assertEqual(rc, 2, out)
        self.assertIn("cannot read", out)

    def test_a_changed_value_needs_a_changelog_row(self):
        """The supersession machinery in verify.check_provenance was written and unreachable,
        because nothing ever passed it a previous edition."""
        self.build_baseline()
        rc, out = self.article(content_module(self.tmp, total="30.0",
                                              extra='    m["claims"]["throughput_a"]'
                                                    '["value"] = 11.0\n'))
        self.assertNotEqual(rc, 0, out)
        self.assertIn("no changelog row", out)


class TestClaimsIsCalledOnce(GateCase):
    """H11. The log printed the manifest write twice: the content module wrote claims.json itself
    and the gate then overwrote the same path with different formatting. Two independently produced
    copies at one path mean the file on disk need not be the file that was judged."""

    def test_one_build_calls_claims_exactly_once(self):
        rc, out = self.article(content_module(self.tmp, total="30.0"))
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.claims_calls(), 1,
                         "claims() ran %d times in one build" % self.claims_calls())

    def test_the_gate_writes_the_manifest_once(self):
        _rc, out = self.article(content_module(self.tmp, total="30.0"))
        writes = [ln for ln in out.splitlines() if "claims.json" in ln and "wrote" in ln]
        self.assertEqual(len(writes), 1, writes)

    def test_a_renderer_that_asks_for_the_manifest_is_handed_the_same_object(self):
        """A renderer that wants to print from the manifest must be given the build's copy rather
        than call claims() again, which is what a second call would be."""
        src = content_module(self.tmp, total="30.0", name="wants_manifest.py")
        with io.open(src, "r", encoding="utf-8") as f:
            text = f.read()
        text = text.replace(
            "def render(figures, data):",
            "def render(figures, data, manifest=None):\n"
            "    assert manifest is not None, 'the engine did not hand over the manifest'\n"
            "    open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ids.txt'),"
            " 'w').write(str(id(manifest)))")
        with io.open(src, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        rc, out = self.article(src)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.claims_calls(), 1, out)


class TestPromisedFileMustAppear(GateCase):
    """A verified report that never reached disk is not a success. The exit code is the only place
    that can say so, because the log has already announced the gate passed."""

    def test_a_write_that_fails_exits_non_zero(self):
        # A directory where the report file belongs: the open() fails the way a full disk or a
        # permission problem would, without needing either.
        os.makedirs(os.path.join(self.out_dir, "synthetic-v1.0.html"))
        rc, out = self.article(content_module(self.tmp, total="30.0"))
        self.assertNotEqual(rc, 0, out)
        self.assertIn("REPORT NOT WRITTEN", out)
        self.assertIn("synthetic-v1.0.html", out)


class TestGateUnitLevel(unittest.TestCase):
    """run_claims_gate on its own, without the CLI around it."""

    class Bare:
        pass

    def test_absent_pair_is_absent_not_an_error(self):
        """The gate REPORTS absence; the CLI decides what it costs. Keeping that split means a
        caller embedding the engine can still render a legacy report deliberately."""
        with tempfile.TemporaryDirectory() as d:
            r = longform.run_claims_gate(self.Bare(), {}, {}, d)
        self.assertEqual(r["status"], longform.GATE_ABSENT)
        self.assertIsNone(r["manifest_path"])

    def test_claims_returning_a_non_dict_is_reported_not_raised(self):
        mod = self.Bare()
        mod.MANIFEST = "claims.json"
        mod.claims = lambda figures, data: ["not", "a", "manifest"]
        with tempfile.TemporaryDirectory() as d:
            r = longform.run_claims_gate(mod, {}, {}, d)
        self.assertEqual(r["status"], longform.GATE_INCOMPLETE)
        self.assertIn("not a manifest dict", r["message"])

    def test_a_supplied_manifest_is_not_recomputed(self):
        """The gate judges the dict it is handed. If it called claims() again it could be judging
        something other than what the document was rendered from."""
        calls = []
        mod = self.Bare()
        mod.MANIFEST = "claims.json"

        def claims(figures, data):
            calls.append(1)
            return {"schema": "claims/1", "claims": {}}

        mod.claims = claims
        supplied = {
            "schema": "claims/1",
            "report": {"arrival_model": "closed_loop"},
            "runs": {"primary": {}},
            "claims": {"x": {"value": 1.0, "unit": "tok/s", "basis": "total", "kind": "measured",
                             "run": "primary"}},
            "gate": {"passed": True, "cases_published": True, "window_run": "primary",
                     # No gate ever ran behind this unit-level fixture, and G3 is an error
                     # rather than a warning when no artefact is named. The waiver is the
                     # documented way to say so, and it still leaves the finding on record.
                     "artifact_waiver": "unit-level fixture: no gate run stands behind it"},
        }
        with tempfile.TemporaryDirectory() as d:
            # A document is supplied because a gate with neither a baseline nor an artifact to
            # read has checked nothing but a self-written manifest, and now says so.
            r = longform.run_claims_gate(mod, {}, {}, d, manifest=supplied,
                                         rendered_html="<p>nothing measurable</p>")
        self.assertEqual(calls, [])
        self.assertIs(r["manifest"], supplied)
        self.assertEqual(r["status"], longform.GATE_PASS, r)

    def test_the_gate_leaves_no_temporary_render_behind(self):
        """The F1 checks need the rendered document, and the gate runs before the report exists.
        The throwaway copy it makes must not survive, and must never land in out_dir."""
        mod = self.Bare()
        mod.MANIFEST = "claims.json"
        mod.claims = lambda figures, data: {
            "schema": "claims/1",
            "report": {"arrival_model": "closed_loop"},
            "runs": {"primary": {}},
            "claims": {"x": {"value": 1.0, "unit": "tok/s", "basis": "total", "kind": "measured",
                             "run": "primary"}},
            "gate": {"passed": True, "cases_published": True, "window_run": "primary",
                     # No gate ever ran behind this unit-level fixture, and G3 is an error
                     # rather than a warning when no artefact is named. The waiver is the
                     # documented way to say so, and it still leaves the finding on record.
                     "artifact_waiver": "unit-level fixture: no gate run stands behind it"},
        }
        with tempfile.TemporaryDirectory() as d:
            r = longform.run_claims_gate(mod, {}, {}, d, rendered_html="<p>hello</p>")
            left = sorted(os.listdir(d))
        self.assertEqual(r["status"], longform.GATE_PASS, r)
        self.assertEqual(left, ["claims-findings.json", "claims.json"], left)


class TestRenderedReturnValue(unittest.TestCase):
    """render_report's return value carries the manifest without widening the tuple. A content
    module's own __main__ block unpacks three values from it, and that must keep working."""

    def test_it_unpacks_as_three_values(self):
        html, figures, data = longform.Rendered("<html>", {"f": 1}, {"a": 2}, {"schema": "x"})
        self.assertEqual((html, figures, data), ("<html>", {"f": 1}, {"a": 2}))

    def test_the_manifest_rides_along_as_an_attribute(self):
        m = {"schema": "claims/1"}
        self.assertIs(longform.Rendered("<html>", {}, {}, m).manifest, m)

    def test_it_is_still_a_tuple_of_three(self):
        self.assertEqual(len(longform.Rendered("<html>", {}, {}, {})), 3)


class TestDeclarationFloorUnitLevel(unittest.TestCase):
    """check_declaration_floor on its own: it is the one check the gate owns rather than verify."""

    BASE = {
        "claims": {"a": {"value": 1.0, "kind": "measured"}, "b": {"value": 2.0, "kind": "derived"}},
        "prose": [{"id": "p1"}, {"id": "p2"}],
        "figures": [{"id": "f1"}],
    }

    def floor(self, now, previous=None):
        return longform.check_declaration_floor(now, previous if previous is not None else self.BASE)

    def test_no_previous_edition_means_no_findings(self):
        self.assertEqual(longform.check_declaration_floor(self.BASE, None), [])

    def test_an_identical_edition_is_clean(self):
        self.assertEqual(self.floor(dict(self.BASE)), [])

    def test_adding_is_always_fine(self):
        now = json.loads(json.dumps(self.BASE))
        now["claims"]["c"] = {"value": 3.0, "kind": "measured"}
        now["figures"].append({"id": "f2"})
        self.assertEqual(self.floor(now), [])

    def test_a_dropped_prose_block_is_an_error(self):
        now = json.loads(json.dumps(self.BASE))
        now["prose"] = [{"id": "p1"}]
        out = self.floor(now)
        self.assertEqual([f["check"] for f in out], ["A10"])
        self.assertIn("p2", out[0]["message"])

    def test_an_unnamed_block_still_counts(self):
        """An unnamed block that cannot be tracked by id is still countable, otherwise deleting
        one is invisible to the floor."""
        prev = {"claims": {}, "prose": [{}, {}], "figures": []}
        out = self.floor({"claims": {}, "prose": [{}], "figures": []}, prev)
        self.assertEqual([f["check"] for f in out], ["A10"])

    def test_a_kind_promotion_is_not_a_regression(self):
        now = json.loads(json.dumps(self.BASE))
        now["claims"]["a"]["kind"] = "measured"
        now["claims"]["b"]["kind"] = "measured"
        self.assertEqual(self.floor(now), [])

    def test_derived_and_measured_share_a_rung(self):
        """A derived claim is recomputed by the gate from measured inputs, so it is exactly as
        checkable as they are. Calling that a demotion would punish the right fix."""
        now = json.loads(json.dumps(self.BASE))
        now["claims"]["a"]["kind"] = "derived"
        self.assertEqual(self.floor(now), [])

    def test_a_changelog_row_waives_a_drop(self):
        now = json.loads(json.dumps(self.BASE))
        now["prose"] = [{"id": "p1"}]
        now["changelog"] = [{"version": "2", "prose_removed": ["p2"]}]
        self.assertEqual(self.floor(now), [])


ASSERTING = {
    "claims": {"a": {"value": 1.0, "kind": "measured"}, "b": {"value": 2.0, "kind": "derived"}},
    "prose": [{"id": "p1", "text": "one", "assert": {"op": "gt", "left": "a", "right": "b"}},
              {"id": "p2", "text": "two", "assert": {"op": "gt", "left": "b", "right": "a"}}],
    "figures": [{"id": "f1"}],
}


class TestAssertionsAreCountedNotBlocks(unittest.TestCase):
    """The floor counted prose BLOCKS, so popping the "assert" key from every block left the ids,
    the sentences and every count identical while removing the only falsifiable thing a prose
    block contains. The manifest still declared seven blocks and asserted nothing."""

    def stripped(self):
        now = json.loads(json.dumps(ASSERTING))
        for block in now["prose"]:
            block.pop("assert")
        return now

    def test_deleting_every_assertion_is_an_error(self):
        out = longform.check_declaration_floor(self.stripped(), ASSERTING)
        self.assertEqual([f["check"] for f in out], ["A10"], out)
        self.assertIn("assert nothing in this one", out[0]["message"])
        self.assertIn("p1", out[0]["message"])
        self.assertIn("p2", out[0]["message"])

    def test_the_block_count_alone_sees_nothing(self):
        """The property that made this invisible: every count the old floor knew about is level."""
        now = self.stripped()
        self.assertEqual(len(now["prose"]), len(ASSERTING["prose"]))
        self.assertEqual([b["id"] for b in now["prose"]],
                         [b["id"] for b in ASSERTING["prose"]])

    def test_deleting_one_assertion_names_that_one(self):
        now = json.loads(json.dumps(ASSERTING))
        now["prose"][1].pop("assert")
        out = longform.check_declaration_floor(now, ASSERTING)
        self.assertEqual([f["check"] for f in out], ["A10"], out)
        self.assertIn("p2", out[0]["message"])

    def test_a_changelog_row_waives_it(self):
        now = self.stripped()
        now["changelog"] = [{"version": "2", "prose_removed": ["p1", "p2"]}]
        self.assertEqual(longform.check_declaration_floor(now, ASSERTING), [])

    def test_keeping_every_assertion_is_clean(self):
        self.assertEqual(longform.check_declaration_floor(
            json.loads(json.dumps(ASSERTING)), ASSERTING), [])

    def test_adding_an_assertion_is_fine(self):
        now = json.loads(json.dumps(ASSERTING))
        now["prose"].append({"id": "p3", "assert": {"op": "gt", "left": "a", "right": "b"}})
        self.assertEqual(longform.check_declaration_floor(now, ASSERTING), [])


class TestNoBaselineFloor(unittest.TestCase):
    """OPEN 2. With no previous edition the floor was simply not applied, so a manifest cut to one
    claim, no prose, no figures and both coverage floors at zero exited 0. A first-ever edition
    genuinely has no baseline, so the floor has to come off the artifact instead."""

    THIN = {"claims": {"a": {"value": 1.0, "kind": "measured"}}, "prose": [], "figures": []}

    def test_absence_of_a_baseline_is_recorded_as_a_finding(self):
        """Loud and on the record. A build log stays on the machine that made it; the findings
        file travels with the manifest."""
        out = longform.check_no_baseline_floor(self.THIN, {"unit_bearing": 100.0})
        self.assertTrue(any("NO BASELINE" in f["message"] for f in out), out)
        self.assertTrue(all(f["check"] == "A10" for f in out), out)

    def test_low_document_coverage_is_an_error_when_there_is_no_baseline(self):
        out = longform.check_no_baseline_floor(self.THIN, {"unit_bearing": 12.0})
        errors = [f for f in out if f["severity"] == "error"]
        self.assertEqual(len(errors), 1, out)
        self.assertIn("no baseline", errors[0]["message"])

    def test_a_document_that_was_never_measured_is_an_error(self):
        """No baseline and no artifact means every check that ran read only the manifest, and the
        manifest is written by the same generator as the prose."""
        out = longform.check_no_baseline_floor(self.THIN, {"unit_bearing": None})
        self.assertTrue(any(f["severity"] == "error" for f in out), out)

    def test_full_coverage_clears_the_floor(self):
        out = longform.check_no_baseline_floor(self.THIN, {"unit_bearing": 100.0})
        self.assertEqual([f for f in out if f["severity"] == "error"], [], out)

    def test_the_floor_is_absolute_not_manifest_declared(self):
        """The attack declared min_unit_bearing_pct 0 and min_bare_numeral_pct 0. A floor the
        manifest sets is a floor the manifest can lower."""
        thin = dict(self.THIN, coverage={"min_unit_bearing_pct": 0.0,
                                         "min_bare_numeral_pct": 0.0})
        out = longform.check_no_baseline_floor(thin, {"unit_bearing": 3.0,
                                                      "min_unit_bearing_pct": 0.0})
        self.assertTrue(any(f["severity"] == "error" for f in out), out)


COMPANION_TAIL = '''

COMPANIONS = {"primer.html": ("Method Primer", "render_primer")}


def render_primer(figures, data):
    return "<section><h2>Primer</h2><p>%s</p></section>"
'''


class TestCompanionsAreJudged(GateCase):
    """BLOCKING 1. method-primer.html was rendered, stamped and written while run_claims_gate was
    called with the report alone, so A5, A6 and F1 had no jurisdiction over it at all. A figure
    the gate blocks in the report shipped intact on the page beside it."""

    def companion(self, body, total="30.0"):
        src = content_module(self.tmp, total=total, name="with_companion.py")
        with io.open(src, "a", encoding="utf-8", newline="\n") as f:
            f.write(COMPANION_TAIL % body)
        return src

    def test_a_fabricated_figure_in_a_companion_blocks_the_build(self):
        rc, out = self.article(self.companion("The engine sustains 9999.0 tok/s."))
        self.assertNotEqual(rc, 0, out)
        self.assertIn("9999.0", out)
        self.assertIn("primer.html", out)
        self.assertIn("A5", out)

    def test_nothing_is_written_when_a_companion_fails(self):
        """The companion is a published file like any other, so a defect in it stops the build the
        same way a defect in the report does."""
        self.article(self.companion("The engine sustains 9999.0 tok/s."))
        stray = [n for n in os.listdir(self.out_dir) if n.endswith(".html")]
        self.assertEqual(stray, [], stray)

    def test_the_finding_names_the_document_it_came_from(self):
        """"A5 fired" and "A5 fired on the primer" are different bugs with different fixes."""
        self.article(self.companion("The engine sustains 9999.0 tok/s."))
        hits = [f for f in self.findings() if f.get("document") == "primer.html"]
        self.assertTrue(hits, self.findings())

    def test_a_clean_companion_still_publishes(self):
        rc, out = self.article(self.companion("It reaches 30.0 tok/s in total."))
        self.assertEqual(rc, 0, out)
        self.assertTrue(os.path.exists(os.path.join(self.out_dir, "primer.html")), out)

    def test_the_companions_coverage_is_printed(self):
        _rc, out = self.article(self.companion("It reaches 30.0 tok/s in total."))
        self.assertIn("primer.html: document coverage", out)

    def test_the_report_is_not_re_reported_for_the_companion(self):
        """The manifest is one object however many documents it backs, so a manifest-level finding
        must not multiply by the number of companions."""
        self.article(self.companion("It reaches 30.0 tok/s in total."))
        messages = [f["message"] for f in self.findings()]
        self.assertEqual(len(messages), len(set(messages)), messages)

    def test_the_log_says_how_many_documents_were_judged(self):
        _rc, out = self.article(self.companion("It reaches 30.0 tok/s in total."))
        self.assertIn("2 document(s) judged", out)


class TestCoverageIsPrintedOnEveryBuild(GateCase):
    """The coverage figure was printed only when the gate blocked. A failing build prints its
    findings anyway; a passing build is the one where nothing else distinguishes a manifest that
    asserts everything from one that asserts almost nothing."""

    def test_a_passing_build_prints_coverage(self):
        rc, out = self.article(content_module(self.tmp, total="30.0"))
        self.assertEqual(rc, 0, out)
        self.assertIn("document coverage", out)
        self.assertRegex(out, r"\d+/\d+ unit-bearing numerals traced to a claim")

    def test_a_blocked_build_still_prints_coverage(self):
        _rc, out = self.article(content_module(self.tmp, total="33.0"))
        self.assertIn("document coverage", out)

    def test_the_coverage_line_names_the_document_it_measured(self):
        _rc, out = self.article(content_module(self.tmp, total="30.0"))
        self.assertIn("synthetic-v1.0.html: document coverage", out)


class TestStaleBytecodeCannotBuildAPreviousEdition(GateCase):
    """Loading a content module by path writes a .pyc beside it, and Python reuses that cache when
    the source's recorded mtime SECOND and its size both match. Edit a report and rebuild inside
    one second without changing the file's length and the build renders the PREVIOUS edition's
    prose while every log line and every check describes the new one. Reproduced standalone, and it
    silently defeated two tests before the cause was found.

    The two editions here differ by one 8-character token, so the file size is identical, and the
    mtime is forced back to what it was, so the cache validates. Without the fix the cached edition
    A renders; with it, edition B does."""

    def prime_stale_cache(self):
        src = content_module(self.tmp, total="30.0", name="tick.py")
        with io.open(src, encoding="utf-8") as f:
            edition_a = f.read().replace("Together", "EditionA")
        with io.open(src, "w", encoding="utf-8", newline="\n") as f:
            f.write(edition_a)
        cache = importlib.util.cache_from_source(src)
        py_compile.compile(src, cfile=cache, doraise=True)
        self.assertTrue(os.path.exists(cache))
        before = os.stat(src)

        edition_b = edition_a.replace("EditionA", "EditionB")
        self.assertEqual(len(edition_b.encode("utf-8")), len(edition_a.encode("utf-8")))
        with io.open(src, "w", encoding="utf-8", newline="\n") as f:
            f.write(edition_b)
        os.utime(src, (before.st_atime, before.st_mtime))
        return src, cache

    def test_the_edition_on_disk_is_the_edition_that_renders(self):
        src, _cache = self.prime_stale_cache()
        rc, out = self.article(src)
        self.assertEqual(rc, 0, out)
        html = self.index_html()
        self.assertIn("EditionB", html)
        self.assertNotIn("EditionA", html)

    def test_the_build_writes_no_bytecode_cache_of_its_own(self):
        """Nothing new goes stale later, which is the other half of the fix."""
        src = content_module(self.tmp, total="30.0", name="fresh.py")
        rc, out = self.article(src)
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.exists(importlib.util.cache_from_source(src)), out)


class TestJudgedBytesAreShippedBytes(GateCase):
    """--pdf reopened the published HTML after the gate had judged it, inserted contents page
    numbers and CSS, and wrote the file back over both the working copy and the versioned edition.
    The file a reader received was not the file that was verified."""

    def test_the_published_files_hash_the_same_as_what_the_gate_judged(self):
        rc, out = self.article(content_module(self.tmp, total="30.0"))
        self.assertEqual(rc, 0, out)
        self.assertIn("integrity: the bytes the gate judged are the bytes on disk", out)

    def test_the_digest_of_every_published_file_is_printed(self):
        _rc, out = self.article(content_module(self.tmp, total="30.0"))
        digests = re.findall(r"sha256 ([0-9a-f]{64})\s+(\S+)", out)
        names = sorted(n for _d, n in digests)
        self.assertEqual(names, ["index.html", "synthetic-v1.0.html"], out)

    def test_the_printed_digest_is_the_digest_of_the_file_on_disk(self):
        import hashlib
        _rc, out = self.article(content_module(self.tmp, total="30.0"))
        digests = dict((n, d) for d, n in re.findall(r"sha256 ([0-9a-f]{64})\s+(\S+)", out))
        with io.open(os.path.join(self.out_dir, "index.html"), "rb") as f:
            on_disk = hashlib.sha256(f.read()).hexdigest()
        self.assertEqual(digests["index.html"], on_disk, out)

    def test_a_draft_names_the_stamp_as_the_one_accounted_difference(self):
        """The stamp is applied deliberately after judging, so it is a difference that was
        verified rather than one that appeared."""
        _rc, out = self.article(content_module(self.tmp, total="33.0"), "--no-verify")
        self.assertIn("the one accounted difference", out)
        self.assertIn("before it was stamped", out)

    def test_pagination_refuses_to_publish_its_working_copy(self):
        """also_write took the HTML path, which is how the unjudged paginated copy replaced the
        verified edition. The refusal is in the function, not in its caller."""
        from gpubench.longform import pdf_export
        with tempfile.TemporaryDirectory() as d:
            pdf = os.path.join(d, "r.pdf")
            with io.open(pdf, "wb") as f:
                f.write(b"%PDF-1.4\n")
            with self.assertRaises(SystemExit) as caught:
                pdf_export.paginate(os.path.join(d, "r.html"), pdf,
                                    also_write=(os.path.join(d, "r.html"),))
        self.assertIn("will not write HTML", str(caught.exception))


class TestDocxCarriesTheMachineReadableMarker(unittest.TestCase):
    """The draft stamp reached the HTML in two halves: a banner for a person and a comment marker
    for a pipeline. The DOCX got only the banner, and the DOCX is one of the two formats that
    actually get mailed."""

    CORE = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/'
            'metadata/core-properties"><dc:title xmlns:dc="http://purl.org/dc/elements/1.1/">'
            'R</dc:title></cp:coreProperties>')

    def docx(self, d, core=True):
        import zipfile
        path = os.path.join(d, "r.docx")
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("word/document.xml", "<w:document/>")
            if core:
                z.writestr("docProps/core.xml", self.CORE)
        return path

    def test_the_marker_lands_in_the_core_properties(self):
        import zipfile
        with tempfile.TemporaryDirectory() as d:
            path = self.docx(d)
            self.assertTrue(longform.stamp_docx_marker(path, longform.DRAFT_MARKER, "why"))
            with zipfile.ZipFile(path) as z:
                core = z.read("docProps/core.xml").decode("utf-8")
        self.assertIn(longform.DRAFT_MARKER, core)
        self.assertIn("why", core)

    def test_the_marker_is_greppable_in_the_raw_file(self):
        """A pre-send hook runs grep far more often than it parses OOXML, and a deflated part is
        invisible to grep. The core properties part is stored uncompressed for exactly this."""
        with tempfile.TemporaryDirectory() as d:
            path = self.docx(d)
            longform.stamp_docx_marker(path, longform.DRAFT_MARKER, "why")
            with io.open(path, "rb") as f:
                raw = f.read()
        self.assertIn(longform.DRAFT_MARKER.encode("utf-8"), raw)

    def test_the_document_body_survives_the_rewrite(self):
        import zipfile
        with tempfile.TemporaryDirectory() as d:
            path = self.docx(d)
            longform.stamp_docx_marker(path, longform.DRAFT_MARKER, "why")
            with zipfile.ZipFile(path) as z:
                self.assertEqual(sorted(z.namelist()),
                                 ["docProps/core.xml", "word/document.xml"])
                self.assertEqual(z.read("word/document.xml"), b"<w:document/>")

    def test_a_file_with_no_core_properties_reports_failure(self):
        """Returning True there would ship an unmarked draft while the log said it was stamped."""
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(longform.stamp_docx_marker(self.docx(d, core=False)))

    def test_stamping_twice_does_not_double_the_marker(self):
        import zipfile
        with tempfile.TemporaryDirectory() as d:
            path = self.docx(d)
            longform.stamp_docx_marker(path, longform.DRAFT_MARKER, "why")
            longform.stamp_docx_marker(path, longform.DRAFT_MARKER, "why")
            with zipfile.ZipFile(path) as z:
                core = z.read("docProps/core.xml").decode("utf-8")
        self.assertEqual(core.count(longform.DRAFT_MARKER), 1, core)


if __name__ == "__main__":
    unittest.main(verbosity=2)
