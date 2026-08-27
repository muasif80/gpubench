#!/usr/bin/env python3
"""Tests for the claims gate's checks themselves. No GPU, no network, no real report.

WHAT IS BEING PROVEN. Two adversarial audits went at this gate and beat it the same way every
time: the gate checked a DECLARATION where it should have checked the ARTEFACT. The manifest said
a figure had a table view, and nothing looked at the document. The manifest said the gate passed,
and nothing opened the result file. The manifest listed five hand-written prose blocks, and the
report shipped a hundred thousand characters that no check had jurisdiction over, into which five
fabricated headline figures were injected with no effect on the exit code at all.

So every test here is built the same way: a manifest that is clean, then one defect of the class
the audit used, and the assertion is that the defect is reported. Each class also carries its
negative control, because a check that never fires and a check that always fires are equally
useless and look identical from the outside.

Run:  python -m tests.test_verify      (from the repo root)
"""
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, ".")
from gpubench import verify as V  # noqa: E402


# --------------------------------------------------------------------------------------
# fixtures


def base_manifest(**over):
    """The smallest manifest that verifies. One measured claim, one run, a gate that passed.

    It carries exactly one standing finding, a G3 warning that the gate result is unfalsifiable
    because the run declares no artifact path, and that is deliberate: the tests assert on the
    check they are about rather than on a total, so a baseline finding cannot hide a regression.
    """
    m = {
        "schema": "claims/1",
        "report": {"version": "1.0", "arrival_model": "closed_loop"},
        "runs": {"primary": {"started": "2026-08-25T11:00:00Z",
                             "finished": "2026-08-25T12:00:00Z"}},
        "claims": {
            "throughput": {"value": 233.0, "unit": "tok/s", "basis": "total",
                           "kind": "measured", "run": "primary",
                           "measured_at": "2026-08-25T11:05:00Z",
                           "label": "aggregate throughput"},
        },
        "gate": {"passed": True, "cases_published": True, "window_run": "primary"},
    }
    m.update(over)
    return m


def claim(value, **over):
    c = {"value": value, "unit": "tok/s", "basis": "total", "kind": "measured", "run": "primary",
         "measured_at": "2026-08-25T11:05:00Z"}
    c.update(over)
    return c


class Case(unittest.TestCase):
    """Shared plumbing: run verify, then ask what it said about one check."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gpubench-verify-test-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, name, text):
        path = os.path.join(self.tmp, name)
        with io.open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        return path

    def document(self, body):
        """A rendered document, in the shape the engine actually emits one."""
        html = ('<!doctype html><html><head><title>Report</title>'
                '<style>.n{color:#0b0b0b}</style></head><body><main>%s</main></body></html>'
                % body)
        from pathlib import Path
        return Path(self.write("rendered.html", html))

    def run_verify(self, manifest, rendered=None, previous=None, manifest_dir=None):
        from pathlib import Path
        return V.verify(manifest, previous, rendered,
                        Path(manifest_dir) if manifest_dir else None)

    def found(self, findings, check, severity=None):
        return [i for i in findings.items
                if i["check"] == check and (severity is None or i["severity"] == severity)]

    def messages(self, findings, check):
        return " ".join(i["message"] for i in self.found(findings, check))


# --------------------------------------------------------------------------------------
# A5: the rendered document, which is where the reader meets the number


class TestA5OmissionAttack(Case):
    """The hole that mattered most. A2 policed the manifest's prose list; the report shipped
    everything else."""

    def test_a_fabricated_headline_figure_is_caught(self):
        """The exact attack: five figures injected into an abstract, no claim key, no manifest
        change. Under A2 alone this shipped with exit 0."""
        m = base_manifest()
        doc = self.document(
            "<h2>Abstract</h2><p>The pair sustained 1,240 tok/s for 96 concurrent users at "
            "0.4 s to first token, for $0.11 per million output tokens, with 4% headroom.</p>")
        f = self.run_verify(m, doc)
        printed = self.messages(f, "A5")
        for fabrication in ("1,240", "0.4", "4%"):
            self.assertIn(fabrication, printed)
        self.assertTrue(f.errors)

    def test_the_negative_control_a_document_that_only_prints_claims_is_clean(self):
        """Without this the test above would pass on a check that fires on everything."""
        m = base_manifest()
        f = self.run_verify(m, self.document("<p>The pair sustained 233 tok/s.</p>"))
        self.assertEqual(self.found(f, "A5"), [])
        self.assertEqual(f.coverage["unit_bearing"], 100.0)

    def test_a_stale_figure_in_a_heading_and_a_tooltip_is_caught(self):
        """The second attack. A tag-stripper drops attribute values on the floor, so the stale
        number was hidden in a title= tooltip as well as in the heading and the contents."""
        m = base_manifest()
        doc = self.document(
            '<h2>18. Sustained 2,850 tok/s</h2>'
            '<p title="2,850 tok/s sustained">See the table.</p>'
            '<table><tr><td>233</td></tr></table>')
        f = self.run_verify(m, doc)
        self.assertIn("2,850", self.messages(f, "A5"))

    def test_an_svg_tooltip_is_in_scope(self):
        """Chart tooltips are text a reader sees, and they carry per-series values that exist
        nowhere else in the document."""
        m = base_manifest()
        doc = self.document('<svg><circle><title>GPU1 684.5 TFLOPS</title></circle></svg>')
        self.assertIn("684.5", self.messages(self.run_verify(m, doc), "A5"))

    def test_no_rendered_document_reports_no_jurisdiction_rather_than_a_pass(self):
        f = self.run_verify(base_manifest(), None)
        self.assertEqual(self.found(f, "A5"), [])
        self.assertIsNone(f.coverage["unit_bearing"])
        self.assertIn("no rendered document", V.coverage_line(f))


class TestA5Precision(Case):
    """A claim of 77.8523 legitimately renders as 78%, 77.9% and 77.85%. All three must trace."""

    def rounded(self, printed):
        m = base_manifest()
        m["claims"]["fraction"] = claim(77.8523, unit="%", basis="ratio", kind="derived",
                                       formula="77.8523")
        return self.run_verify(m, self.document("<p>Prefill reached %s of the roof.</p>" % printed))

    def test_every_printed_precision_of_one_claim_traces(self):
        for printed in ("78%", "77.9%", "77.85%", "77.8523%"):
            self.assertEqual(self.found(self.rounded(printed), "A5"), [], printed)

    def test_a_number_outside_the_last_printed_digit_does_not_trace(self):
        """The control on the tolerance. Half of the last digit, not any digit."""
        self.assertTrue(self.found(self.rounded("79%"), "A5"))
        self.assertTrue(self.found(self.rounded("77.7%"), "A5"))

    def test_separators_a_currency_mark_and_a_sign_belong_to_the_numeral(self):
        m = base_manifest()
        m["claims"]["big"] = claim(2777.0)
        m["claims"]["cost"] = claim(0.11, unit="", basis="per_token", kind="derived",
                                    formula="0.11")
        m["claims"]["delta"] = claim(0.78, unit="%", basis="ratio", kind="derived",
                                     formula="0.78")
        doc = self.document("<p>2,777 tok/s at $0.11x, deviation +0.78%.</p>")
        self.assertEqual(self.found(self.run_verify(m, doc), "A5"), [])

    def test_a_claim_recorded_in_one_unit_covers_a_figure_printed_in_another(self):
        """The comparison happens in the family's base unit, so re-expressing a latency in
        microseconds is a wording change and not a coverage hole."""
        m = base_manifest()
        m["claims"]["step"] = claim(0.0245, unit="ms", basis="per_token")
        doc = self.document("<p>The all-reduce cost 24.5 us per step.</p>")
        self.assertEqual(self.found(self.run_verify(m, doc), "A5"), [])

    def test_a_claim_from_another_family_does_not_cover_a_printed_figure(self):
        """With two hundred claims in scope, value-only matching would cover almost anything. A
        throughput of 65 tok/s is not evidence for a printed 65%."""
        m = base_manifest()
        m["claims"]["rate"] = claim(65.0)
        doc = self.document("<p>It reached 65% of the ceiling.</p>")
        self.assertIn("65", self.messages(self.run_verify(m, doc), "A5"))


class TestA5NotEveryNumberIsAMeasurement(Case):
    """The check has to be strict without inventing claims out of English."""

    def test_a_plural_and_a_decade_are_not_seconds(self):
        doc = self.document("<p>Two RTX 5090s, a practice from the 1950s and the mid-2000s.</p>")
        f = self.run_verify(base_manifest(), doc)
        self.assertEqual(self.found(f, "A5"), [], self.messages(f, "A5"))

    def test_a_lane_count_and_a_part_number_are_not_multipliers(self):
        doc = self.document("<p>GPU1 sits on PCIe 4.0 x4 in the 2x5090 box.</p>")
        self.assertEqual(self.found(self.run_verify(base_manifest(), doc), "A5"), [])

    def test_a_duration_written_without_a_space_is_still_a_measurement(self):
        """The control on the rule above: the discriminator is a decimal point or a separator, so
        dropping the space must not drop the check."""
        doc = self.document("<p>Time to first token was 1.93s.</p>")
        self.assertIn("1.93", self.messages(self.run_verify(base_manifest(), doc), "A5"))


class TestA5Allowances(Case):
    """An allowance is an exemption from the one thing this file requires, so it has to be earned."""

    def with_allow(self, allow):
        m = base_manifest(coverage={"allow": allow})
        return self.run_verify(m, self.document("<p>Axis ticks: 0%, 25%, 50%, 75%, 100%.</p>"))

    def test_an_allowance_with_a_reason_covers_the_numeral(self):
        f = self.with_allow([{"pattern": r"^(0|25|50|75|100)%$",
                              "why": "chart axis tick labels, generated from the scale"}])
        self.assertEqual(self.found(f, "A5"), [])
        self.assertEqual(f.coverage["unit_bearing"], 100.0)

    def test_an_allowance_with_no_reason_is_rejected(self):
        f = self.with_allow([{"pattern": r"^\d+%$"}])
        self.assertIn("records no reason", self.messages(f, "A5"))

    def test_an_allowance_that_does_not_compile_is_reported_not_ignored(self):
        f = self.with_allow([{"pattern": "([unclosed", "why": "typo"}])
        self.assertIn("does not compile", self.messages(f, "A5"))

    def test_a_pattern_reads_the_numeral_and_cannot_launder_its_neighbours(self):
        """REPRODUCED HOLE. `pattern` was matched against the numeral's sixty-character context as
        well as the numeral, so any blessed phrase exempted every numeral written near it: writing
        "DDR5" beside a fabrication was enough."""
        m = base_manifest(coverage={"allow": [{"pattern": "DDR5",
                                               "why": "host inventory, not a measurement"}]})
        f = self.run_verify(m, self.document("<p>Memory 128 GiB DDR5, and 9,999 tok/s.</p>"))
        printed = self.messages(f, "A5")
        self.assertIn("9,999", printed)
        self.assertIn("128", printed)

    def test_context_matching_is_a_separate_field_that_must_span_the_numeral(self):
        """Context matching is sometimes genuinely wanted, so it has its own name, and it only
        exempts the numeral its own match covers. A phrase carrying no digits exempts nothing."""
        m = base_manifest(coverage={"allow": [
            {"context_pattern": r"Memory \d+ GiB DDR5", "why": "the installed DIMM inventory"}]})
        f = self.run_verify(m, self.document("<p>Memory 128 GiB DDR5, and 9,999 tok/s.</p>"))
        flagged = {i.get("numeral") for i in self.found(f, "A5")}
        self.assertIn("9,999", flagged)
        self.assertNotIn("128", flagged)
        self.assertEqual(f.coverage["allowances"][0]["exempted"], 1)

    def test_a_context_phrase_beside_a_fabrication_exempts_nothing(self):
        m = base_manifest(coverage={"allow": [
            {"context_pattern": "of a reference", "why": "MLPerf's own accuracy bar"}]})
        f = self.run_verify(m, self.document("<p>We held 99.9% of a reference run.</p>"))
        self.assertIn("99.9", self.messages(f, "A5"))

    def test_an_allowance_that_matches_everything_is_refused(self):
        """REPRODUCED HOLE. A pattern of "." exempted all 490 numerals in a real report and still
        printed 490/490 and 100.0%, because an exempted numeral counts as covered."""
        m = base_manifest(coverage={"allow": [{"pattern": ".", "why": "housekeeping"}]})
        f = self.run_verify(m, self.document("<p>1,240 tok/s and 0.4 s and 4%.</p>"))
        self.assertIn("matches all", self.messages(f, "A5"))
        self.assertTrue(self.found(f, "A5", "error"))
        self.assertIn("1,240", self.messages(f, "A5"))

    def test_the_count_each_allowance_exempted_is_reported(self):
        """An allowance is a hole in the strongest check in the file, and a hole nobody can see
        the size of is a hole that grows."""
        m = base_manifest(coverage={"allow": [
            {"pattern": r"^9,999$", "why": "a decorative figure in the masthead"}]})
        f = self.run_verify(m, self.document("<p>233 tok/s beside 9,999 tok/s.</p>"))
        self.assertEqual(f.coverage["allowances"][0]["exempted"], 1)
        buf = io.StringIO()
        V.report(f, buf)
        self.assertIn("exempted 1 numeral(s)", buf.getvalue())


class TestA5CoverageFloor(Case):
    """H2: a shrinking manifest has to score WORSE, not cleaner."""

    DOC = ("<p>Throughput 233 tok/s, latency 1.93 s, pool 14.44 GiB.</p>")

    def full(self):
        m = base_manifest()
        m["claims"]["latency"] = claim(1.93, unit="s", basis="per_request")
        m["claims"]["pool"] = claim(14.44, unit="GiB", basis="total")
        return m

    def test_the_honest_manifest_covers_the_document(self):
        f = self.run_verify(self.full(), self.document(self.DOC))
        self.assertEqual(f.coverage["unit_bearing"], 100.0)
        self.assertEqual(self.found(f, "A5"), [])

    def test_deleting_claims_lowers_coverage_and_blocks(self):
        """Declaring less used to print a CLEANER line than declaring everything, because the score
        counted findings and findings come from assertions. Coverage is measured against the
        document, so the same document with fewer claims scores worse."""
        m = self.full()
        del m["claims"]["latency"]
        del m["claims"]["pool"]
        f = self.run_verify(m, self.document(self.DOC))
        self.assertLess(f.coverage["unit_bearing"], 100.0)
        self.assertTrue(f.errors)
        self.assertIn("below the 100.0%", self.messages(f, "A5"))

    def test_the_floor_is_stated_in_the_summary_line_either_way(self):
        line = V.coverage_line(self.run_verify(self.full(), self.document(self.DOC)))
        self.assertIn("floor 100.0%", line)
        self.assertIn("3/3 unit-bearing numerals", line)

    def test_a_lowered_floor_reports_the_shortfall_as_a_warning_not_silence(self):
        """Lowering the floor is a legitimate, recorded decision. It must not make the uncovered
        numerals disappear from the output."""
        m = self.full()
        del m["claims"]["pool"]
        m["coverage"] = {"min_unit_bearing_pct": 50.0}
        f = self.run_verify(m, self.document(self.DOC))
        self.assertEqual(self.found(f, "A5", "error"), [])
        self.assertIn("14.44", self.messages(f, "A5"))


class TestA6BareNumerals(Case):
    DOC = "<p>We ran 12 probes over 7 machines and 4 quantisations.</p>"

    # A page of uncited counts, long enough for a percentage over it to mean something.
    WIDE = "<p>" + " ".join("row %d" % i for i in range(300, 360)) + "</p>"

    def test_the_undeclared_floor_is_live_and_reports_at_warning_level(self):
        """REPRODUCED HOLE. The warn branch was dead code: `report_at = f.error if bare_declared
        else f.warn` sat behind `if bare_pct < floor_bare` and the undeclared floor was 0.0, a
        condition no percentage can miss. A check that cannot fire is not a check."""
        f = self.run_verify(base_manifest(), self.document(self.WIDE))
        self.assertTrue(self.found(f, "A6", "warn"), f.coverage)
        self.assertEqual(self.found(f, "A6", "error"), [])
        self.assertIsNotNone(f.coverage["bare"])

    def test_a_handful_of_numerals_is_not_a_coverage_measurement(self):
        """The negative control, and the same argument D2 makes about a p95 over n=32: a
        percentage over six numerals is an anecdote wearing a percentage's name. A floor the
        manifest DECLARES still applies at any size, which the next test holds it to."""
        f = self.run_verify(base_manifest(), self.document(self.DOC))
        self.assertEqual(self.found(f, "A6"), [], self.messages(f, "A6"))
        self.assertEqual(f.coverage["bare_total"], 3)

    def test_a_declared_floor_applies_however_few_numerals_there_are(self):
        m = base_manifest(coverage={"min_bare_numeral_pct": 90.0})
        f = self.run_verify(m, self.document(self.DOC))
        self.assertTrue(self.found(f, "A6", "error"))

    def test_the_undeclared_floor_stays_quiet_on_a_document_that_cites_its_numbers(self):
        m = base_manifest()
        for i in range(300, 360):
            m["claims"]["row_%d" % i] = claim(i, unit="count", basis="total")
        f = self.run_verify(m, self.document(self.WIDE))
        self.assertEqual(self.found(f, "A6"), [], self.messages(f, "A6"))

    def test_a_declared_floor_that_is_not_met_blocks(self):
        m = base_manifest(coverage={"min_bare_numeral_pct": 90.0})
        f = self.run_verify(m, self.document(self.DOC))
        self.assertTrue(self.found(f, "A6", "error"))
        self.assertIn("below the 90.0% floor this manifest declares", self.messages(f, "A6"))

    def test_bare_coverage_is_reported_whether_or_not_it_fires(self):
        f = self.run_verify(base_manifest(), self.document(self.DOC))
        self.assertIn("bare numerals", V.coverage_line(f))


# --------------------------------------------------------------------------------------
# A7: one quantity, one value


class TestA7OneQuantityOneValue(Case):

    def pair(self, label_a, label_b, value_b=2850.0, **extra):
        m = base_manifest()
        m["claims"]["a"] = claim(2181.7, label=label_a, **extra)
        m["claims"]["b"] = claim(value_b, label=label_b, **extra)
        return self.run_verify(m)

    def test_an_identical_label_with_two_values_is_now_an_error(self):
        """It was warning 29 of 29 in a build that already emitted 28, which is the same as not
        reporting it. The 233-versus-204.5 defect the tool exists for has to block."""
        f = self.pair("prefill throughput", "prefill throughput")
        self.assertTrue(self.found(f, "A7", "error"))
        self.assertIn("2181.7", self.messages(f, "A7"))

    def test_rewording_the_label_no_longer_silences_it(self):
        """The audit's third claim: same quantity, label edited, nothing reported at all."""
        f = self.pair("prefill throughput", "throughput of the prefill")
        self.assertTrue(self.found(f, "A7", "error"))

    def test_a_declared_quantity_id_makes_the_grouping_structural(self):
        f = self.pair("peak prefill", "prefill at the roof", quantity="prefill_throughput")
        self.assertTrue(self.found(f, "A7", "error"))
        self.assertIn("prefill_throughput", self.messages(f, "A7"))

    def test_agreeing_values_under_one_label_are_clean(self):
        self.assertEqual(self.found(self.pair("prefill throughput", "prefill throughput",
                                             value_b=2181.7), "A7"), [])

    def test_rounding_is_not_disagreement(self):
        self.assertEqual(self.found(self.pair("prefill throughput", "prefill throughput",
                                             value_b=2182.0), "A7"), [])

    def test_labels_differing_only_by_a_number_are_different_quantities(self):
        """The ceiling at 128 tokens and the ceiling at 2048 tokens must disagree: the number is
        what identifies the measurement point, so near-duplication cannot be read off the words."""
        m = base_manifest()
        for length, value in ((128, 1200.0), (2048, 2777.0)):
            m["claims"]["ceiling_%d" % length] = claim(
                value, label="interconnect ceiling at %d tokens" % length)
        self.assertEqual(self.found(self.run_verify(m), "A7"), [])

    def test_a_near_duplicate_label_warns_rather_than_blocks(self):
        """One differing WORD inside one unit and basis is a heuristic, so it reports at the
        severity a heuristic deserves and names the fix."""
        m = base_manifest()
        m["claims"]["a"] = claim(2181.7, label="sustained prefill throughput")
        m["claims"]["b"] = claim(2850.0, label="sustained prefill rate throughput")
        f = self.run_verify(m)
        self.assertTrue(self.found(f, "A7", "warn"))
        self.assertIn("quantity", self.messages(f, "A7"))

    def test_a_declared_equality_group_is_not_reported_twice(self):
        """A1 already checks a declared group against its own tolerance. Two findings for one
        defect is how a finding list becomes unreadable."""
        m = base_manifest()
        m["claims"]["a"] = claim(2181.7, label="prefill throughput")
        m["claims"]["b"] = claim(2850.0, label="prefill throughput")
        m["equalities"] = [{"keys": ["a", "b"], "tolerance": 0.005}]
        f = self.run_verify(m)
        self.assertTrue(self.found(f, "A1", "error"))
        self.assertEqual(self.found(f, "A7"), [])


# --------------------------------------------------------------------------------------
# A8 and A9: a run that happened, and a kind that is not a free pass


class TestA8TheRunMustExist(Case):

    def test_a_run_that_never_happened_is_an_error(self):
        """`run` was tested for truthiness and nothing else, so this shipped with zero findings."""
        m = base_manifest()
        m["claims"]["throughput"]["run"] = "run-that-never-happened"
        f = self.run_verify(m)
        self.assertTrue(self.found(f, "A8", "error"))
        self.assertIn("not in the run table", self.messages(f, "A8"))

    def test_a_whitespace_run_id_is_an_error(self):
        """A single space passes a truthiness test while naming nothing."""
        m = base_manifest()
        m["claims"]["throughput"]["run"] = " "
        self.assertTrue(self.found(self.run_verify(m), "A8", "error"))

    def test_a_missing_run_is_still_the_A4_error_it_always_was(self):
        m = base_manifest()
        m["claims"]["throughput"]["run"] = ""
        self.assertTrue(self.found(self.run_verify(m), "A4", "error"))

    def test_a_real_run_id_is_clean(self):
        self.assertEqual(self.found(self.run_verify(base_manifest()), "A8"), [])

    def test_a_measurement_stamped_outside_the_run_window_warns(self):
        """A warning and not an error, because result files in hand genuinely contain probes whose
        recorded start is after the artefact's own finish, and a read-only measurement must never
        be edited to satisfy a check. The gate reports it; the manifest can accept it by name."""
        m = base_manifest()
        m["claims"]["throughput"]["measured_at"] = "2026-08-25T18:07:19Z"
        f = self.run_verify(m)
        self.assertTrue(self.found(f, "A8", "warn"))
        self.assertEqual(self.found(f, "A8", "error"), [])
        self.assertIn("outside run primary's window", self.messages(f, "A8"))


class TestA9KindIsNotAFreePass(Case):

    def supplied(self, source, manifest_dir=None, **over):
        m = base_manifest(**over)
        m["claims"]["budget"] = claim(3.0, unit="ms", basis="per_token", kind="supplied",
                                      source=source)
        m["claims"]["budget"].pop("run", None)
        return self.run_verify(m, manifest_dir=manifest_dir)

    def test_engineering_estimate_is_not_provenance(self):
        """The proven attack: forcing a value while leaving kind derived was correctly blocked, and
        changing nothing but the kind to supplied shipped a claim of 3.0 whose arithmetic is
        10.73."""
        f = self.supplied("engineering estimate")
        self.assertTrue(self.found(f, "A9", "error"))
        self.assertIn("names nothing a reader can go and look at", self.messages(f, "A9"))

    def test_no_source_at_all_is_an_error(self):
        m = base_manifest()
        m["claims"]["budget"] = claim(3.0, kind="published")
        self.assertIn("names no source", self.messages(self.run_verify(m), "A9"))

    def test_a_run_id_a_url_and_a_path_that_exists_are_all_redeemable(self):
        """All four kinds still clear the check, because one that rejected everything would just
        be a ban on the kind. The path is now one that NAMES SOMETHING: the file is opened."""
        self.write("engine-config.md", "the engine settings this claim was read from\n")
        for source in ("primary",
                       "https://www.nvidia.com/en-us/geforce/graphics-cards/50-series/",
                       "gpubench.analysis.prefill_comms_ceiling",
                       "engine-config.md"):
            f = self.supplied(source, self.tmp)
            self.assertEqual(self.found(f, "A9"), [], "%s: %s" % (source, self.messages(f, "A9")))

    def test_a_percentage_that_is_not_derived_is_an_error(self):
        """A ratio is a quotient of two numbers that are somewhere else in the manifest, so a
        percentage nobody recomputed is an arithmetic result nobody checked."""
        m = base_manifest()
        m["claims"]["share"] = claim(20.0, unit="%", basis="ratio", kind="assumption")
        f = self.run_verify(m)
        self.assertTrue(self.found(f, "A9", "error"))
        self.assertIn("derivation_waiver", self.messages(f, "A9"))

    def test_a_waiver_with_a_reason_clears_it(self):
        m = base_manifest()
        m["claims"]["share"] = claim(20.0, unit="%", basis="ratio", kind="assumption",
                                     derivation_waiver="a chosen share of the prompt mixture, "
                                                       "not a quotient of anything measured")
        self.assertEqual(self.found(self.run_verify(m), "A9"), [])

    def test_an_empty_waiver_does_not_clear_it(self):
        m = base_manifest()
        m["claims"]["share"] = claim(20.0, unit="%", basis="ratio", kind="assumption",
                                     derivation_waiver="   ")
        self.assertTrue(self.found(self.run_verify(m), "A9", "error"))


# --------------------------------------------------------------------------------------
# D6 and D7: an open-loop level, and a declaration that matches the harness


# A level exactly as gpubench's own Poisson harness writes one. The concurrency is null because
# concurrency is an OUTCOME under an independent arrival process, and that null is what crashed
# check_load_shape with a TypeError before anything else could be checked.
OPEN_LOOP_LEVEL = {
    "name": "r40",
    "concurrency": None,
    "requests": 20,
    "duration_s": 0.94,
    "waves": None,
    "whole_waves": None,
    "peak_inflight": 7,
    "arrival": {
        "model": "open_loop_poisson",
        "target_rate_req_s": 40.0,
        "achieved_rate_req_s": 21.3,
        "seed": 20260826,
        "queue_depth": {"source": "client_inflight", "sample_interval_s": 0.02,
                        "samples": [[0.02, 1], [0.04, 3], [0.06, 6]],
                        "max": 7, "at_last_arrival": 6, "last_sample": 0, "drain_s": 0.31},
    },
}


def open_loop_level(**over):
    level = json.loads(json.dumps(OPEN_LOOP_LEVEL))
    for key, value in over.items():
        if key in ("target_rate_req_s", "achieved_rate_req_s", "queue_depth", "model"):
            if value is None:
                level["arrival"].pop(key, None)
            else:
                level["arrival"][key] = value
        else:
            level[key] = value
    return level


class TestD6OpenLoopLevels(Case):

    def load(self, level, arrival_model="open_loop_poisson"):
        m = base_manifest(levels=[level])
        m["report"]["arrival_model"] = arrival_model
        return self.run_verify(m)

    def test_a_null_concurrency_does_not_crash_the_gate(self):
        """It raised TypeError at the int() cast, which took the whole verifier down before any
        other check ran. Every cast that reads a load-shape field is guarded now."""
        f = self.load(open_loop_level())
        self.assertIsInstance(f.items, list)

    def test_a_complete_open_loop_level_is_clean(self):
        f = self.load(open_loop_level())
        self.assertEqual(self.found(f, "D6"), [], self.messages(f, "D6"))

    def test_the_wave_arithmetic_is_skipped_entirely(self):
        """D3 structurally demands a positive concurrency and a whole number of waves, neither of
        which an open-loop level has by design. Coercing the null to zero turned a correct
        declaration into 'level r40 declares no concurrency or request count'."""
        f = self.load(open_loop_level())
        self.assertEqual(self.found(f, "D3"), [], self.messages(f, "D3"))

    def test_a_missing_rate_pair_is_reported(self):
        for field in ("target_rate_req_s", "achieved_rate_req_s"):
            f = self.load(open_loop_level(**{field: None}))
            self.assertIn(field, self.messages(f, "D6"), field)

    def test_a_missing_queue_trace_is_reported(self):
        f = self.load(open_loop_level(queue_depth=None, peak_inflight=None))
        self.assertIn("queue", self.messages(f, "D6"))

    def test_a_closed_loop_queue_stub_is_not_a_trace(self):
        """A closed-loop harness records the key with sampled=false and a reason, because in closed
        loop the in-flight count is pinned by construction. That is correctly not a measurement."""
        stub = {"sampled": False, "why": "pinned at the concurrency by construction"}
        f = self.load(open_loop_level(queue_depth=stub, peak_inflight=None))
        self.assertTrue(self.found(f, "D6", "error"))

    def test_a_level_inherits_the_reports_arrival_model_when_it_declares_none(self):
        level = open_loop_level()
        level.pop("arrival")
        f = self.load(level, arrival_model="open_loop_constant")
        self.assertTrue(self.found(f, "D6", "error"))
        self.assertEqual(self.found(f, "D3"), [])

    def test_a_string_arrival_on_the_level_is_read_too(self):
        """A manifest hand-built beside a report writes the model as a string; a level copied from
        a harness document nests it under arrival.model. Both shapes are real."""
        f = self.load({"name": "r10", "concurrency": None, "requests": 10,
                       "arrival": "open_loop_constant"})
        self.assertTrue(self.found(f, "D6", "error"))

    def test_a_closed_loop_level_still_gets_the_wave_checks(self):
        """The negative control on skipping D3. Skipping it for open loop must not skip it for the
        mode it was written for."""
        f = self.load({"name": "c48", "concurrency": 48, "requests": 100, "arrival": "closed_loop"},
                      arrival_model="closed_loop")
        self.assertTrue(self.found(f, "D3", "error"))
        self.assertEqual(self.found(f, "D6"), [])


class TestD7ArrivalContradiction(Case):

    def note(self, model, text):
        m = base_manifest()
        m["report"]["arrival_model"] = model
        m["report"]["arrival_note"] = text
        return self.run_verify(m)

    def test_an_open_loop_declaration_beside_a_fixed_in_flight_note_is_an_error(self):
        """The proven attack: closed_loop flipped to open_loop_poisson on a fixed-in-flight harness
        shipped with zero arrival findings, printing the flipped model directly beside the honest
        note. Nothing read the two together."""
        f = self.note("open_loop_poisson",
                      "Fixed in-flight population per level; no independent arrival process.")
        self.assertTrue(self.found(f, "D7", "error"))
        self.assertIn("two different experiments", self.messages(f, "D7"))

    def test_the_completion_triggered_phrasing_is_caught_too(self):
        f = self.note("open_loop_constant",
                      "Each request is issued when a previous one completes.")
        self.assertTrue(self.found(f, "D7", "error"))

    def test_a_closed_loop_note_may_discuss_poisson_arrivals(self):
        """The real report's note explains what a closed-loop harness cannot do, and says the word
        Poisson while doing it. A check that fired on the word would be unusable."""
        f = self.note("closed_loop",
                      "Fixed in-flight population per level; no independent arrival process. "
                      "MLPerf's Server scenario uses Poisson arrivals precisely because a "
                      "closed-loop harness cannot build the queue that produces real tail latency.")
        self.assertEqual(self.found(f, "D7"), [], self.messages(f, "D7"))

    def test_a_closed_loop_declaration_asserting_independent_arrivals_is_an_error(self):
        f = self.note("closed_loop", "Requests arrive independently of completions.")
        self.assertTrue(self.found(f, "D7", "error"))

    def test_an_honest_open_loop_note_is_clean(self):
        f = self.note("open_loop_poisson",
                      "Arrivals are drawn from an exponential distribution at the target rate, "
                      "independent of when earlier requests finish.")
        self.assertEqual(self.found(f, "D7"), [])


# --------------------------------------------------------------------------------------
# F3 and F4: a figure's values reach the document


FIG_WITH_TABLE = ('<figure id="latency"><div class="chart"><svg></svg></div>'
                  '<figcaption>Figure 9.</figcaption>'
                  '<details><summary>Table view</summary>'
                  '<div class="tablewrap"><table><thead><tr><th>c</th></tr></thead>'
                  '<tbody><tr><td>233</td></tr></tbody></table></div></details></figure>')
FIG_EMPTY_DISCLOSURE = ('<figure id="latency"><div class="chart"><svg></svg></div>'
                        '<figcaption>Figure 9.</figcaption>'
                        "<p class='note'>Inter-token latency is in the table view rather than on "
                        "this plot</p><details><summary>Table view</summary></details></figure>")


class TestF3TableViews(Case):

    def figures(self, figures, body):
        return self.run_verify(base_manifest(figures=figures), self.document(body))

    def test_an_empty_disclosure_under_a_declared_table_view_is_an_error(self):
        """The proven attack. table_view stayed True in the manifest, the figure rendered with its
        table omitted, and the document shipped an empty <details> directly beneath a note telling
        the reader the missing series was in there."""
        f = self.figures([{"id": "latency", "table_view": True}], FIG_EMPTY_DISCLOSURE)
        self.assertTrue(self.found(f, "F3", "error"))
        self.assertIn("renders none, or renders an empty one", self.messages(f, "F3"))

    def test_a_figure_that_ships_its_table_is_clean(self):
        self.assertEqual(self.found(self.figures([{"id": "latency", "table_view": True}],
                                                FIG_WITH_TABLE), "F3"), [])

    def test_a_declared_figure_missing_from_the_document_is_an_error(self):
        f = self.figures([{"id": "latency", "table_view": True}], "<p>nothing here</p>")
        self.assertIn("does not appear in the rendered document", self.messages(f, "F3"))

    def test_a_shared_table_view_resolves_against_the_figure_it_names(self):
        f = self.figures([{"id": "latency", "table_view": True},
                          {"id": "arbw", "table_view_shared_with": "latency"}], FIG_WITH_TABLE)
        self.assertEqual(self.found(f, "F3"), [])
        self.assertEqual(self.found(f, "F2"), [], "sharing a table view still carries the values")

    def test_sharing_with_a_figure_that_does_not_exist_is_an_error(self):
        f = self.figures([{"id": "arbw", "table_view_shared_with": "nowhere"}], FIG_WITH_TABLE)
        self.assertIn("not a figure in this manifest", self.messages(f, "F3"))

    def test_sharing_with_a_figure_that_renders_no_table_is_an_error(self):
        f = self.figures([{"id": "latency", "table_view": True},
                          {"id": "arbw", "table_view_shared_with": "latency"}],
                         FIG_EMPTY_DISCLOSURE)
        self.assertIn("renders no table of its own", self.messages(f, "F3"))

    def test_a_table_view_declared_as_free_text_warns_and_names_the_fix(self):
        """A truthy string states the intention in prose, where no check can reach it. That is not
        a defect and not verifiable either, so it is reported as exactly that."""
        f = self.figures([{"id": "latency", "table_view": "shared with figure arlat"}],
                         FIG_WITH_TABLE)
        self.assertTrue(self.found(f, "F3", "warn"))
        self.assertIn("table_view_shared_with", self.messages(f, "F3"))


class TestF4TableCells(Case):

    def with_table(self, cells, body):
        m = base_manifest(tables={"sweep": {"cells": cells}})
        m["claims"]["latency"] = claim(1.93, unit="s", basis="per_request")
        return self.run_verify(m, self.document(body))

    def test_a_numeral_in_an_identified_table_that_is_not_a_declared_cell_warns(self):
        body = ('<table id="sweep"><tbody><tr><td>233 tok/s</td><td>2850 tok/s</td>'
                "</tr></tbody></table>")
        f = self.with_table(["throughput"], body)
        self.assertTrue(self.found(f, "F4", "warn"))
        self.assertIn("2850", self.messages(f, "F4"))

    def test_a_table_whose_numerals_are_all_declared_cells_is_clean(self):
        body = ('<table id="sweep"><tbody><tr><td>233 tok/s</td><td>1.93 s</td>'
                "</tr></tbody></table>")
        f = self.with_table(["throughput", "latency"], body)
        self.assertEqual(self.found(f, "F4", "warn"),
                         [], self.messages(f, "F4"))

    def test_a_table_inside_a_figure_of_the_same_id_is_located(self):
        body = ('<figure id="sweep"><table><tbody><tr><td>2850 tok/s</td></tr></tbody>'
                "</table></figure>")
        self.assertIn("2850", self.messages(self.with_table(["throughput"], body), "F4"))

    def test_a_table_the_document_does_not_identify_is_reported_once_with_the_remedy(self):
        """F4 has no jurisdiction over an anonymous table, and saying so is the honest outcome:
        silence here would read as a pass over twelve unchecked tables."""
        f = self.with_table(["throughput"], "<table><tbody><tr><td>2850</td></tr></tbody></table>")
        warns = self.found(f, "F4", "warn")
        self.assertEqual(len(warns), 1)
        self.assertIn('id="<table id>"', warns[0]["message"])


# --------------------------------------------------------------------------------------
# G3: the gate result read back out of the artefact


def accuracy_artefact(cases=10, determinism=100.0, exact=100.0, errors=()):
    return {
        "schema_version": 1,
        "probes": {"accuracy": {
            "cases": [{"deterministic": True} for _ in range(cases)],
            "errors": list(errors),
            "method": {"cases_published": ["case-%d" % i for i in range(cases)]},
            "summary": {"cases": cases, "deterministic": cases,
                        "determinism_pct": determinism, "exact_match_pct": exact},
        }},
    }


class TestG3GateReadBackFromTheArtefact(Case):

    def gated(self, artefact=None, artifact_path="gate.json", **gate_over):
        if artefact is not None:
            self.write(artifact_path, json.dumps(artefact))
        m = base_manifest()
        m["runs"]["primary"]["artifact"] = artifact_path
        m["gate"].update(gate_over)
        return self.run_verify(m, manifest_dir=self.tmp)

    def test_a_matching_artefact_is_clean(self):
        f = self.gated(accuracy_artefact(), passed=True, cases_published=10)
        self.assertEqual(self.found(f, "G3"), [], self.messages(f, "G3"))

    def test_a_hardcoded_pass_over_an_artefact_that_failed_is_an_error(self):
        """passed=True was never read back from anything, so it reported success unconditionally,
        which is worse than having no gate at all."""
        f = self.gated(accuracy_artefact(exact=60.0), passed=True, cases_published=10)
        self.assertTrue(self.found(f, "G3", "error"))
        self.assertIn("exact_match_pct is 60", self.messages(f, "G3"))

    def test_an_errored_case_is_not_a_pass(self):
        f = self.gated(accuracy_artefact(errors=["timeout"]), passed=True, cases_published=10)
        self.assertIn("1 case(s) errored", self.messages(f, "G3"))

    def test_a_declared_threshold_below_a_hundred_is_honoured_and_visible(self):
        """A lower bar is legitimate and has to be written down where a reader sees it."""
        f = self.gated(accuracy_artefact(exact=90.0), passed=True, cases_published=10,
                       threshold_pct=90.0)
        self.assertEqual(self.found(f, "G3"), [])

    def test_an_inflated_case_count_is_an_error(self):
        f = self.gated(accuracy_artefact(), passed=True, cases_published=999)
        self.assertIn("the artefact gate.json carries 10", self.messages(f, "G3"))

    def test_a_boolean_case_count_is_compared_against_whether_any_exist(self):
        f = self.gated(accuracy_artefact(cases=0), passed=True, cases_published=True)
        self.assertTrue(self.found(f, "G3", "error"))

    def test_no_artifact_path_is_an_error_because_the_key_was_the_opt_out(self):
        """REPRODUCED HOLE. G3 was an ERROR only while the run declared the OPTIONAL "artifact"
        key, so deleting one key shipped a gate whose artefact recorded passed=false, 40% exact
        match and 4 of 10 cases deterministic, as a warning."""
        f = self.run_verify(base_manifest())
        self.assertTrue(self.found(f, "G3", "error"))
        self.assertIn("unfalsifiable as recorded", self.messages(f, "G3"))

    def test_deleting_the_artifact_key_from_a_failing_gate_does_not_downgrade_it(self):
        """The attack itself: an artefact that records a failure, and the key that points at it
        removed. The finding has to stay blocking."""
        self.write("gate.json", json.dumps(accuracy_artefact(exact=40.0, determinism=40.0)))
        m = base_manifest()
        m["gate"].update({"passed": True, "cases_published": 10})
        f = self.run_verify(m, manifest_dir=self.tmp)
        self.assertTrue(self.found(f, "G3", "error"))

    def test_a_declared_waiver_downgrades_it_and_is_printed(self):
        """A waiver is legitimate and has to be visible: an escape hatch nobody can see in the
        output is the same unfalsifiable declaration in a different costume."""
        m = base_manifest()
        m["gate"]["artifact_waiver"] = "the gate ran on a box whose result file is not published"
        f = self.run_verify(m)
        self.assertEqual(self.found(f, "G3", "error"), [])
        self.assertIn("WAIVED IN THE MANIFEST", self.messages(f, "G3"))

    def test_an_artifact_path_that_does_not_exist_is_an_error(self):
        f = self.gated(None, artifact_path="absent.json", passed=True, cases_published=10)
        self.assertTrue(self.found(f, "G3", "error"))
        self.assertIn("cannot be read", self.messages(f, "G3"))

    def test_an_artefact_with_no_readable_gate_result_is_an_error(self):
        """Guessing a gate result out of an arbitrary document is the thing this check exists to
        stop, so an unreadable artefact is reported rather than interpreted. It blocks, because a
        file that does not record the outcome is not evidence that the gate ran."""
        f = self.gated({"probes": {"inventory": {}}}, passed=True, cases_published=10)
        self.assertTrue(self.found(f, "G3", "error"))
        self.assertIn("records no gate result", self.messages(f, "G3"))

    def test_the_string_false_is_not_a_pass(self):
        """REPRODUCED HOLE. gate.passed was read with Python truthiness, and every non-empty
        string is true, so a manifest recording passed="false" shipped as a gate that passed."""
        m = base_manifest()
        m["gate"]["passed"] = "false"
        f = self.run_verify(m)
        self.assertTrue(self.found(f, "G1", "error"))
        self.assertIn("not a boolean", self.messages(f, "G1"))
        self.assertIn("did not pass", self.messages(f, "G1"))


# --------------------------------------------------------------------------------------
# accepted warnings


class TestAcceptedWarnings(Case):
    """A build carrying 28 standing warnings is a build where a warning cannot be read, and that is
    how a printed quantity with two values shipped as warning 29 of 29."""

    def with_acceptance(self, *entries):
        m = base_manifest(percentiles=[{"key": "throughput", "q": 0.95, "n": 32}])
        if entries:
            m["accepted_warnings"] = list(entries)
        return self.run_verify(m)

    def test_the_warning_exists_to_begin_with(self):
        f = self.with_acceptance()
        self.assertTrue(self.found(f, "D2", "warn"))

    def test_an_acceptance_with_a_reason_moves_it_off_the_live_list(self):
        f = self.with_acceptance({"check": "D2", "claim": "throughput",
                                  "why": "the rank is printed beside the percentile in section 12"})
        self.assertEqual(self.found(f, "D2", "warn"), [])
        self.assertEqual(len(self.found(f, "D2", "accepted")), 1)
        self.assertNotIn("throughput", [w.get("claim") for w in f.warnings])

    def test_an_accepted_warning_is_still_printed_separately(self):
        f = self.with_acceptance({"check": "D2", "claim": "throughput", "why": "rank printed"})
        buf = io.StringIO()
        V.report(f, buf)
        out = buf.getvalue()
        self.assertIn("Accepted in the manifest, with a reason", out)
        self.assertIn("rank printed", out)
        self.assertIn("1 accepted", out)

    def test_an_acceptance_with_no_reason_is_rejected(self):
        f = self.with_acceptance({"check": "D2", "claim": "throughput"})
        self.assertTrue(f.errors)
        self.assertTrue(self.found(f, "D2", "warn"), "the warning must survive a bad acceptance")

    def test_an_acceptance_with_a_blank_reason_is_rejected(self):
        f = self.with_acceptance({"check": "D2", "claim": "throughput", "why": "  "})
        self.assertTrue(f.errors)

    def test_an_acceptance_is_narrow_and_cannot_swallow_a_whole_check(self):
        """Naming a field that does not match leaves the warning live, so an acceptance written for
        one claim cannot quietly cover the next one."""
        f = self.with_acceptance({"check": "D2", "claim": "something_else", "why": "reviewed"})
        self.assertTrue(self.found(f, "D2", "warn"))

    def test_a_stale_acceptance_is_reported(self):
        """An acceptance that matches nothing is a claim about the report that is no longer true."""
        f = self.with_acceptance({"check": "E2", "why": "reviewed in edition 7"})
        self.assertIn("matches no warning in this build", self.messages(f, "manifest"))

    def test_an_error_can_never_be_accepted(self):
        m = base_manifest()
        m["claims"]["throughput"]["run"] = "run-that-never-happened"
        m["accepted_warnings"] = [{"check": "A8", "why": "we know"}]
        f = self.run_verify(m)
        self.assertTrue(self.found(f, "A8", "error"))
        self.assertEqual(self.found(f, "A8", "accepted"), [])


class TestReportOutput(Case):

    def test_a_flood_of_one_check_is_capped_in_the_console_and_whole_in_the_findings(self):
        """Three hundred findings of one check push the errors underneath off the screen. The
        findings JSON is the complete record; the console is a summary of it."""
        m = base_manifest()
        printed = " ".join("%d.5 tok/s" % i for i in range(1, 60))
        f = self.run_verify(m, self.document("<p>%s</p>" % printed))
        buf = io.StringIO()
        V.report(f, buf)
        out = buf.getvalue()
        self.assertIn("more of this check", out)
        self.assertGreater(len(self.found(f, "A5", "error")), V.MAX_PER_CHECK)

    def test_the_summary_states_coverage_beside_the_verdict(self):
        f = self.run_verify(base_manifest(), self.document("<p>233 tok/s</p>"))
        buf = io.StringIO()
        V.report(f, buf)
        self.assertIn("document coverage:", buf.getvalue())


class TestUnitVocabularyIsStructural(Case):
    """REPRODUCED HOLE. UNIT_TOKENS was a 22-string allowlist and the document's vocabulary was
    wider, so TB/s, GT/s, GHz, MHz, KiB, TiB, KB, Wh, kW, J, ns, emb/s, "minutes" and every
    currency figure carried no unit as far as A5 was concerned. Seven fabricated figures shipped
    through that route: an allowlist fails silently, and it fails in the direction of passing."""

    WIDE = ("<p>9.5 TB/s and 32 GT/s and 2,410 MHz and 128 KiB and 4 TiB and 500 Wh and 1.2 kW "
            "and 3 J and 40 ns and 12 emb/s and 20 minutes and $0.11 and 850 GHz.</p>")

    def test_every_unit_the_old_list_missed_is_now_in_jurisdiction(self):
        f = self.run_verify(base_manifest(), self.document(self.WIDE))
        flagged = {i.get("numeral") for i in self.found(f, "A5", "error")}
        for numeral in ("9.5", "32", "2,410", "128", "4", "500", "1.2", "3", "40", "12", "20",
                        "$0.11", "850"):
            self.assertIn(numeral, flagged, "%s escaped A5" % numeral)

    def test_english_after_a_numeral_is_not_a_unit(self):
        """The negative control, and the reason this is a shape rule rather than "any short word".
        A rule that read any noun after a number as a measurement would demand a claim for every
        one of them, and an author who has to declare "5 GPUs" learns to switch the gate off.

        This test used to include "96 concurrent users" and assert a total of zero. It no longer
        does, deliberately: an adversarial pass shipped exactly that string as a fabricated headline
        figure, because a spelled-out unit was outside the vocabulary and the individual bare miss
        was never named. Promotion is by a CLOSED LIST of phrases that are measurements in any
        sentence, not by word shape, so the nouns below are still English. The positive half is
        asserted in the next test rather than deleted from this one.
        """
        doc = self.document("<p>30 tokens each, on 5 GPUs over PCIe 4.0 x16 "
                            "at 25 UTC, sampled 8 times.</p>")
        f = self.run_verify(base_manifest(), doc)
        self.assertEqual(self.found(f, "A5", "error"), [], self.messages(f, "A5"))
        self.assertEqual(f.coverage["unit_bearing_total"], 0)

    def test_a_spelled_out_unit_is_a_measurement(self):
        """The positive half, and the attack it closes.

        "96 concurrent users" and "47314 tokens per second" read as measurements to every human and
        were invisible to a vocabulary of symbols. Each must now be traced or declared like any
        other measurement, and each must be NAMED, because an aggregate percentage with slack in it
        is how a single fabricated figure hides.
        """
        doc = self.document("<p>96 concurrent users at 47314 tokens per second.</p>")
        f = self.run_verify(base_manifest(), doc)
        flagged = {i.get("numeral") for i in self.found(f, "A5", "error")}
        self.assertIn("96", flagged, self.messages(f, "A5"))
        self.assertIn("47314", flagged, self.messages(f, "A5"))
        self.assertGreaterEqual(f.coverage["unit_bearing_total"], 2)

    def test_a_currency_figure_is_held_to_the_unit_floor_not_the_bare_one(self):
        f = self.run_verify(base_manifest(), self.document("<p>It costs $0.36 per run.</p>"))
        self.assertEqual(f.coverage["unit_bearing_total"], 1)
        self.assertIn("$0.36", self.messages(f, "A5"))


class TestAttachedUnits(Case):
    """REPRODUCED HOLE. A single-letter unit glued to a number needed a decimal point, so 1240W,
    17s and 34x fell to the post-filter: exit 0, zero warnings, three fabrications through."""

    def test_an_integer_power_duration_and_multiplier_are_measurements(self):
        f = self.run_verify(base_manifest(),
                            self.document("<p>It drew 1240W, 17s into the run, a 34x gain.</p>"))
        flagged = {i.get("numeral") for i in self.found(f, "A5", "error")} - {None}
        self.assertEqual(flagged, {"1240", "17", "34"})

    def test_a_decade_and_a_model_number_are_still_protected(self):
        """The protection the old rule was buying, which has to survive: "1950s" is a decade and
        "RTX 5090s" is a pair of cards, and reading either as a duration invents a claim from a
        noun."""
        doc = self.document("<p>Since the 1950s, two RTX 5090s and the 2000s era.</p>")
        f = self.run_verify(base_manifest(), doc)
        self.assertEqual(self.found(f, "A5", "error"), [], self.messages(f, "A5"))

    def test_a_declared_exception_is_honoured_and_counted(self):
        """A plural of a number that is not a model number, such as an HTTP 502, is exactly what
        the manifest declares. The count it took is reported, so the exception is visible."""
        m = base_manifest(coverage={"attached_exceptions": [
            {"pattern": r"^502s$", "why": "HTTP 502 responses, a plural of a status code"}]})
        f = self.run_verify(m, self.document("<p>The service produced 502s under load.</p>"))
        self.assertEqual(self.found(f, "A5", "error"), [], self.messages(f, "A5"))
        self.assertEqual(f.coverage["allowances"][0]["exempted"], 1)

    def test_a_lane_count_is_not_a_multiplier(self):
        f = self.run_verify(base_manifest(), self.document("<p>PCIe 4.0 x4 and 2x5090.</p>"))
        self.assertEqual(self.found(f, "A5", "error"), [], self.messages(f, "A5"))


class TestSpaceThousandsSeparator(Case):
    """REPRODUCED HOLE. "9 526.6 tok/s" reached the gate as the two numerals 9 and 526.6, so 526.6
    validated against a real claim while the reader saw a figure seven thousand larger."""

    def test_the_gate_reads_the_number_the_reader_reads(self):
        m = base_manifest()
        m["claims"]["throughput"]["value"] = 9526.6
        f = self.run_verify(m, self.document("<p>The pair sustained 9 526.6 tok/s.</p>"))
        self.assertEqual(self.found(f, "A5", "error"), [], self.messages(f, "A5"))
        self.assertEqual(f.coverage["unit_bearing_covered"], 1)

    def test_the_split_reading_no_longer_validates_against_an_unrelated_claim(self):
        """The attack: a claim of 526.6 exists, the document prints 9 526.6, and the old scanner
        called it covered."""
        m = base_manifest()
        m["claims"]["throughput"]["value"] = 526.6
        f = self.run_verify(m, self.document("<p>The pair sustained 9 526.6 tok/s.</p>"))
        self.assertIn("9,526.6", self.messages(f, "A5"))

    def test_the_reading_it_took_is_reported_rather_than_assumed(self):
        m = base_manifest()
        m["claims"]["throughput"]["value"] = 9526.6
        f = self.run_verify(m, self.document("<p>The pair sustained 9 526.6 tok/s.</p>"))
        self.assertTrue(self.found(f, "A5", "warn"))
        self.assertIn("space where a thousands separator belongs", self.messages(f, "A5"))

    def test_a_digit_glued_to_a_word_is_not_a_thousands_group(self):
        """The false merge this rule has to avoid: "GPU1 615.2 TFLOPS" is not 1,615.2."""
        m = base_manifest()
        m["claims"]["throughput"] = claim(615.2, unit="TFLOPS")
        f = self.run_verify(m, self.document("<p>GPU1 615.2 TFLOPS on the timed kernel.</p>"))
        self.assertEqual(self.found(f, "A5"), [], self.messages(f, "A5"))


class TestA5SaysWhichClaimCoveredWhat(Case):

    def test_the_covering_claim_is_recorded_per_numeral(self):
        f = self.run_verify(base_manifest(), self.document("<p>233 tok/s, twice: 233.0 tok/s.</p>"))
        self.assertEqual(sorted(f.coverage["covered_by"]["throughput"]), ["233.0tok/s", "233tok/s"])

    def test_one_claim_covering_a_dozen_numerals_is_reported_as_coincidence(self):
        """A5 can only compare values, so with hundreds of claims in scope some fabrications are
        covered by accident. The signature is one claim standing behind numerals it cannot all
        be."""
        m = base_manifest()
        m["claims"] = {"step": claim(100.0, unit="ms", basis="per_token")}
        doc = self.document("<p>0.1 s 0.10 s 0.100 s 0.1000 s 100 ms 100.0 ms 100.00 ms "
                            "100.000 ms 100000 us 100000.0 us</p>")
        f = self.run_verify(m, doc)
        self.assertIn("signature of coincidental coverage", self.messages(f, "A5"))
        self.assertTrue(self.found(f, "A5", "warn"))

    def test_a_claim_printed_at_two_precisions_is_not_coincidence(self):
        f = self.run_verify(base_manifest(), self.document("<p>233 tok/s and 233.0 tok/s.</p>"))
        self.assertEqual(self.found(f, "A5", "warn"), [])


class TestB1RecomputesWhateverTheKindSays(Case):
    """REPRODUCED HOLE. B1 recomputed `kind == "derived"` only, so tripling a value and relabelling
    the claim "assumption", "projection" or "measured" left the contradicting formula in place and
    shipped."""

    def relabelled(self, kind):
        m = base_manifest()
        m["claims"]["base"] = claim(10.0, unit="tok/s")
        m["claims"]["doubled"] = {"value": 60.0, "unit": "tok/s", "basis": "total", "kind": kind,
                                  "formula": "base * 2", "label": "twice the base"}
        if kind in ("measured",):
            m["claims"]["doubled"].update(run="primary", measured_at="2026-08-25T11:05:00Z")
        if kind in ("supplied", "published"):
            m["claims"]["doubled"]["source"] = "docs/engine-config.md"
        return self.run_verify(m)

    def test_every_kind_carrying_a_formula_is_recomputed(self):
        for kind in ("assumption", "projection", "measured", "supplied", "published", "derived"):
            f = self.relabelled(kind)
            self.assertTrue(self.found(f, "B1", "error"), "%s escaped recomputation" % kind)
            self.assertIn("20", self.messages(f, "B1"))

    def test_the_kind_decides_the_wording_and_nothing_else(self):
        self.assertIn("free choice of the generator", self.messages(self.relabelled("assumption"),
                                                                    "B1"))
        self.assertNotIn("free choice", self.messages(self.relabelled("derived"), "B1"))

    def test_a_claim_with_no_formula_is_not_invented_one(self):
        m = base_manifest()
        m["claims"]["assumed"] = {"value": 128, "unit": "count", "basis": "per_request",
                                  "kind": "assumption", "label": "generation length"}
        self.assertEqual(self.found(self.run_verify(m), "B1"), [])


class TestA7GroupsStructurallyNotByLabel(Case):
    """REPRODUCED HOLE. Byte-identical labels became an error, and rewording one defeated it: the
    document shipped 101.9 tok/s and 142.6 tok/s for one quantity."""

    def test_two_claims_with_one_formula_must_agree(self):
        m = base_manifest()
        m["claims"]["base"] = claim(50.0)
        m["claims"]["a"] = {"value": 100.0, "unit": "tok/s", "basis": "total", "kind": "derived",
                            "formula": "base * 2", "label": "the pair at concurrency eight"}
        m["claims"]["b"] = {"value": 142.6, "unit": "tok/s", "basis": "total", "kind": "derived",
                            "formula": "base*2", "label": "what two cards together sustained"}
        f = self.run_verify(m)
        self.assertTrue(self.found(f, "A7", "error"))
        self.assertIn("evaluate the same expression", self.messages(f, "A7"))

    def test_two_claims_over_one_input_set_disagreeing_is_a_warning(self):
        m = base_manifest()
        m["claims"]["a"] = claim(10.0)
        m["claims"]["b"] = claim(4.0)
        m["claims"]["sum"] = {"value": 14.0, "unit": "tok/s", "basis": "total", "kind": "derived",
                              "formula": "a + b", "label": "combined"}
        m["claims"]["diff"] = {"value": 6.0, "unit": "tok/s", "basis": "total", "kind": "derived",
                               "formula": "a - b", "label": "the difference"}
        f = self.run_verify(m)
        self.assertTrue(self.found(f, "A7", "warn"))
        self.assertIn("same inputs", self.messages(f, "A7"))

    def test_a_share_and_the_gap_it_implies_are_not_a_disagreement(self):
        """The false positive this rule must not produce: 89.88% of a roof and the 11.26% gap it
        leaves are two views of one comparison, and a gate that cries wolf gets switched off."""
        m = base_manifest()
        m["claims"]["roof"] = claim(100.0)
        m["claims"]["got"] = claim(89.8827)
        m["claims"]["share"] = {"value": 89.8827, "unit": "%", "basis": "ratio", "kind": "derived",
                                "formula": "100 * got / roof", "label": "share of the roof"}
        m["claims"]["gap"] = {"value": 11.2562, "unit": "%", "basis": "ratio", "kind": "derived",
                              "formula": "100 * (roof - got) / got", "label": "the gap to the roof"}
        f = self.run_verify(m)
        self.assertEqual(self.found(f, "A7", "warn"), [], self.messages(f, "A7"))


class TestAcceptancesCannotSwallowACheck(Case):
    """REPRODUCED HOLE. matches() only tested the fields an entry named, so {"check": "A5"} moved
    every A5 finding to accepted and printed "0 error(s), 0 warning(s)". The docstring said an
    acceptance "cannot be written to swallow a whole check". It could."""

    def warning_heavy(self):
        m = base_manifest(coverage={"min_unit_bearing_pct": 0.0})
        m["percentiles"] = [{"key": "throughput", "q": 0.95, "n": 8},
                            {"key": "throughput", "q": 0.99, "n": 8}]
        return m

    def test_a_check_wide_acceptance_takes_nothing_and_says_why(self):
        m = self.warning_heavy()
        m["accepted_warnings"] = [{"check": "D2", "why": "we know about the ranks"}]
        f = self.run_verify(m)
        self.assertEqual(self.found(f, "D2", "accepted"), [])
        self.assertEqual(len(self.found(f, "D2", "warn")), 2)
        self.assertIn("accepts none of them", self.messages(f, "manifest"))

    def test_one_unnamed_acceptance_may_still_take_one_finding(self):
        m = base_manifest()
        m["percentiles"] = [{"key": "throughput", "q": 0.95, "n": 8}]
        m["accepted_warnings"] = [{"check": "D2", "why": "the rank is printed beside it"}]
        f = self.run_verify(m)
        self.assertEqual(len(self.found(f, "D2", "accepted")), 1)

    def test_an_unbounded_cap_is_refused(self):
        m = self.warning_heavy()
        m["accepted_warnings"] = [{"check": "D2", "accepts": 500, "why": "all of them"}]
        f = self.run_verify(m)
        self.assertEqual(self.found(f, "D2", "accepted"), [])
        self.assertIn("The cap is between 1 and", self.messages(f, "manifest"))

    def test_a_named_acceptance_may_raise_its_cap_and_the_count_is_printed(self):
        m = self.warning_heavy()
        m["accepted_warnings"] = [{"check": "D2", "claim": "throughput", "accepts": 2,
                                   "why": "both ranks are printed beside the figures"}]
        f = self.run_verify(m)
        self.assertEqual(len(self.found(f, "D2", "accepted")), 2)
        buf = io.StringIO()
        V.report(f, buf)
        self.assertIn("accepted 2 finding(s), cap 2", buf.getvalue())


class TestAMalformedManifestIsAFindingNotATraceback(Case):
    """Four reproduced crash paths. A traceback is not a finding: nothing downstream can read it,
    and the documented exit-2 path was unreachable because the process died first."""

    def verify_safely(self, m):
        try:
            return self.run_verify(m)
        except Exception as exc:  # noqa: BLE001 - the point of the test
            self.fail("the gate raised %s instead of reporting it" % exc.__class__.__name__)

    def test_no_claims_object_at_all(self):
        m = base_manifest()
        del m["claims"]
        f = self.verify_safely(m)
        self.assertTrue(f.fatal)
        self.assertTrue(self.found(f, "manifest", "error"))

    def test_claims_as_a_list(self):
        f = self.verify_safely(base_manifest(claims=[{"value": 1}]))
        self.assertTrue(f.fatal)

    def test_a_manifest_that_is_not_an_object(self):
        f = self.verify_safely([])
        self.assertTrue(f.fatal)

    def test_an_equality_group_with_no_keys(self):
        f = self.verify_safely(base_manifest(equalities=[{"tolerance": 0.01}]))
        self.assertTrue(self.found(f, "A1", "error"))

    def test_prose_percentiles_and_levels_of_the_wrong_shape(self):
        m = base_manifest(prose={"a": {"id": "a", "text": "no numerals here"}},
                          percentiles=[{"key": "throughput", "q": "most of them", "n": 8}],
                          levels=["c8"], figures={"f": {"id": "f", "table_view": True}})
        f = self.verify_safely(m)
        self.assertTrue(self.found(f, "D1", "error"))

    def test_a_claim_that_is_not_an_object(self):
        m = base_manifest()
        m["claims"]["broken"] = 233.0
        f = self.verify_safely(m)
        self.assertIn("is not an object", self.messages(f, "manifest"))

    def test_a_broken_manifest_exits_two_rather_than_one(self):
        path = self.write("broken.json", json.dumps({"schema": "claims/1"}))
        code = V.main([path])
        self.assertEqual(code, 2)


class TestF1ReadsTheDecodedDocument(Case):
    """A correctly escaped "&amp;" in visible text was reported as a leak: F1 matched the entity
    pattern against the SOURCE, where a correct escape and a double escape look the same."""

    def test_a_correctly_escaped_ampersand_is_not_a_leak(self):
        f = self.run_verify(base_manifest(), self.document("<p>Load &amp; store, 233 tok/s.</p>"))
        self.assertEqual(self.found(f, "F1"), [], self.messages(f, "F1"))

    def test_a_double_escape_is_a_leak_because_the_reader_sees_the_entity(self):
        f = self.run_verify(base_manifest(),
                            self.document("<p>Load &amp;amp; store, 233 tok/s.</p>"))
        self.assertTrue(self.found(f, "F1", "error"))


class TestTheManifestIsNotMutated(Case):

    def test_check_sampling_does_not_write_a_rank_into_the_caller_manifest(self):
        """`p.setdefault("rank", rank)` meant the manifest written to disk carried a field no
        author put there, and the dict that was judged was not the dict that was handed over."""
        m = base_manifest()
        m["percentiles"] = [{"key": "throughput", "q": 0.95, "n": 128}]
        before = json.dumps(m, sort_keys=True)
        self.run_verify(m)
        self.assertEqual(json.dumps(m, sort_keys=True), before)


class TestAttributeQuotingIsNotAHidingPlace(Case):
    """REPRODUCED HOLE. The attribute scanner matched name="value" and nothing else, so a stale
    figure in title='peak draw 1240 W' was never scanned while the double-quoted spelling was.
    Both are ordinary HTML, a browser renders them identically, and no author intends the
    distinction: a generator whose own string is double-quoted reaches for single quotes by
    accident, and that is the spelling that escaped the only check with jurisdiction over what
    shipped.
    """

    def hidden(self, attribute):
        doc = self.document('<p %s>See the table.</p><table><tr><td>233</td></tr></table>'
                            % attribute)
        return self.run_verify(base_manifest(), doc)

    def test_a_single_quoted_tooltip_is_scanned(self):
        f = self.hidden("title='peak draw 1240 W'")
        self.assertIn("1240", self.messages(f, "A5"))

    def test_the_two_quoting_styles_produce_the_same_finding(self):
        """The control that matters. It is not enough that both are scanned; the gate must not be
        deciding WHAT IT READS by how the generator happened to quote it."""
        single = self.messages(self.hidden("title='peak draw 1240 W'"), "A5")
        double = self.messages(self.hidden('title="peak draw 1240 W"'), "A5")
        self.assertIn("1240", double)
        self.assertEqual(single, double)

    def test_an_unquoted_value_is_scanned(self):
        """HTML's third quoting style. A browser reads it, a screen reader reads it, and it was
        invisible to the gate."""
        self.assertIn("1240", self.messages(self.hidden("title=1240W"), "A5"))

    def test_alt_and_aria_label_are_scanned_in_every_style(self):
        for attribute in ("alt='the 1240 W ceiling'", 'alt="the 1240 W ceiling"',
                          "aria-label='the 1240 W ceiling'", 'aria-label="the 1240 W ceiling"'):
            self.assertIn("1240", self.messages(self.hidden(attribute), "A5"), attribute)

    def test_a_closing_angle_bracket_inside_a_value_does_not_lose_the_attribute(self):
        """The regression this rewrite could have introduced. Tags are matched with their quoted
        values consumed as units, so a ">" inside one does not end the tag early and take the rest
        of its attributes out of scope."""
        f = self.hidden('title="peak draw 1240 W > budget"')
        self.assertIn("1240", self.messages(f, "A5"))

    def test_an_attribute_name_inside_another_attributes_value_is_not_an_attribute(self):
        """data-note is not a value a reader sees, so the number inside it is out of scope, and
        walking the pairs in order is what keeps the name from being read out of the middle of
        someone else's value."""
        f = self.hidden('data-note="alt=1240W"')
        self.assertEqual(self.found(f, "A5"), [], self.messages(f, "A5"))

    def test_prose_that_merely_looks_like_an_attribute_is_counted_once(self):
        """The false positive an unquoted-attribute rule invites. "Set title=1240W in the config"
        is a sentence, already visible through the stripper, and counting it a second time as an
        attribute would inflate the denominator with a number that was never in a tag."""
        doc = self.document("<p>Set title=1240W in the config.</p>")
        f = self.run_verify(base_manifest(), doc)
        flagged = [i for i in self.found(f, "A5", "error") if "numeral" in i]
        self.assertEqual([i["numeral"] for i in flagged], ["1240"], self.messages(f, "A5"))
        self.assertEqual(flagged[0]["occurrences"], 1)
        self.assertEqual(f.coverage["unit_bearing_total"], 1)


class TestA9SourceMustNameSomethingThatExists(Case):
    """REPRODUCED HOLE. source_resolves() checked the SHAPE of the string and never opened
    anything, so "results/never-existed/nope.json" redeemed a permanent exemption from
    recomputation: the strongest reason a claim can give for not being recomputed had only to be
    SPELLED like a path.
    """

    def supplied(self, source, manifest_dir=None, run_path=None):
        m = base_manifest()
        if run_path:
            m["runs"]["primary"]["path"] = run_path
        m["claims"]["budget"] = claim(3.0, unit="ms", basis="per_token", kind="supplied",
                                      source=source)
        m["claims"]["budget"].pop("run", None)
        return self.run_verify(m, manifest_dir=manifest_dir)

    def make(self, *parts):
        os.makedirs(os.path.join(self.tmp, *parts[:-1]), exist_ok=True)
        return self.write(os.path.join(*parts), "{}\n")

    def test_a_path_that_names_nothing_is_not_a_source(self):
        f = self.supplied("results/never-existed/nope.json", self.tmp)
        self.assertTrue(self.found(f, "A9", "error"))
        self.assertIn("THAT EXISTS", self.messages(f, "A9"))
        self.assertIn("Looked for", self.messages(f, "A9"))

    def test_a_file_under_the_manifest_directory_resolves(self):
        self.make("results", "run-1", "inventory.json")
        f = self.supplied("results/run-1/inventory.json", self.tmp)
        self.assertEqual(self.found(f, "A9"), [], self.messages(f, "A9"))

    def test_a_digit_led_directory_segment_is_part_of_the_path(self):
        """A real run directory is named 20260825-160142-final. The token pattern required every
        segment to start with a letter, which was harmless while nothing opened the result and
        wrong the moment something did: the path was read as "final/nccl_allreduce.json" and looked
        for in a place it had never been."""
        self.make("results", "20260825-160142-final", "nccl_allreduce.json")
        f = self.supplied("the size axis of the all-reduce sweep, "
                          "results/20260825-160142-final/nccl_allreduce.json", self.tmp)
        self.assertEqual(self.found(f, "A9"), [], self.messages(f, "A9"))

    def test_a_file_above_the_manifest_directory_resolves(self):
        """A manifest is PUBLISHED into an output directory while its sources are written relative
        to the project it was built in, so the manifest's own directory alone would reject every
        honest path in a real report."""
        out = os.path.join(self.tmp, "article", "public")
        os.makedirs(out)
        self.make("results", "nccl_allreduce.json")
        f = self.supplied("the size axis, results/nccl_allreduce.json", out)
        self.assertEqual(self.found(f, "A9"), [], self.messages(f, "A9"))

    def test_a_parent_reference_is_part_of_the_path(self):
        """"../gpubench/results/x.json" resolved, if at all, against the wrong directory, because
        the token pattern started at a letter and dropped the "../" off the front."""
        sibling = os.path.join(self.tmp, "sibling")
        os.makedirs(os.path.join(sibling, "results"))
        with io.open(os.path.join(sibling, "results", "arrivals.json"), "w",
                     encoding="utf-8", newline="\n") as fh:
            fh.write("{}\n")
        out = os.path.join(self.tmp, "project", "out")
        os.makedirs(out)
        f = self.supplied("the arrival artefact, ../../sibling/results/arrivals.json", out)
        self.assertEqual(self.found(f, "A9"), [], self.messages(f, "A9"))

    def test_a_run_directory_the_manifest_declares_is_an_anchor(self):
        """A manifest that says run "primary" lives at results/final has told the reader where to
        look, so a claim citing a file inside it names something they can open."""
        self.make("results", "final", "roofline.json")
        f = self.supplied("read by the harness and recorded in roofline.json under model",
                          self.tmp, run_path="results/final")
        self.assertEqual(self.found(f, "A9"), [], self.messages(f, "A9"))

    def test_a_run_directory_that_does_not_exist_anchors_nothing(self):
        """The control on the rule above: the anchor is a directory that is THERE, not a string
        the manifest typed."""
        f = self.supplied("read by the harness and recorded in roofline.json under model",
                          self.tmp, run_path="results/final")
        self.assertTrue(self.found(f, "A9", "error"))

    def test_a_module_path_resolves_without_importing_the_module(self):
        """Resolution is by LOOKING. importlib.find_spec imports every parent package on the way
        to the one it is asked about, and a gate that executes code named in the manifest it is
        judging has opened a hole considerably larger than the one it closed. Both files here
        raise on import, so an implementation that imported would fail this test loudly."""
        os.makedirs(os.path.join(self.tmp, "vendorpkg"))
        self.write(os.path.join("vendorpkg", "__init__.py"),
                   "raise RuntimeError('the gate imported a module it was only asked to find')\n")
        self.write(os.path.join("vendorpkg", "spec.py"),
                   "raise RuntimeError('the gate imported a module it was only asked to find')\n")
        f = self.supplied("vendorpkg.spec.decode_budget", self.tmp)
        self.assertEqual(self.found(f, "A9"), [], self.messages(f, "A9"))
        self.assertNotIn("vendorpkg", sys.modules)

    def test_a_module_path_that_names_no_module_is_not_a_source(self):
        f = self.supplied("vendorpkg.spec.decode_budget", self.tmp)
        self.assertTrue(self.found(f, "A9", "error"))

    def test_a_url_is_accepted_and_recorded_as_not_resolved(self):
        """A gate that runs offline cannot fetch a URL, so accepting one is right. Printing it
        beside a file that WAS opened, with nothing to tell them apart, is not."""
        f = self.supplied("NVIDIA product specifications, https://www.nvidia.com/rtx-5090/",
                          self.tmp)
        self.assertEqual(self.found(f, "A9"), [], self.messages(f, "A9"))
        self.assertEqual(len(f.sources), 1)
        self.assertFalse(f.sources[0]["resolved"])
        self.assertEqual(f.sources[0]["how"], "url")
        self.assertEqual(f.sources[0]["named"], "https://www.nvidia.com/rtx-5090/")

    def test_checked_and_merely_well_formed_are_distinguishable_in_the_output(self):
        self.write("engine-config.md", "the engine settings this claim was read from\n")
        opened = self.supplied("engine-config.md", self.tmp)
        cited = self.supplied("https://vendor.example/engine-spec", self.tmp)
        self.assertTrue(opened.sources[0]["resolved"])
        self.assertEqual(opened.sources[0]["how"], "file")
        self.assertFalse(cited.sources[0]["resolved"])

        read = io.StringIO()
        V.report(opened, read)
        self.assertIn("1 resolved on disk, 0 accepted unresolved", read.getvalue())
        self.assertNotIn("did NOT resolve", read.getvalue())

        merely = io.StringIO()
        V.report(cited, merely)
        self.assertIn("0 resolved on disk, 1 accepted unresolved", merely.getvalue())
        self.assertIn("did NOT resolve", merely.getvalue())

    def test_a_rejected_source_is_not_listed_as_an_acceptance(self):
        """It is already an error with its own line, and printing it again under a heading that
        says "accepted" would read as the gate waving it through."""
        f = self.supplied("results/never-existed/nope.json", self.tmp)
        self.assertEqual(f.sources, [])


class TestUnicodeSpaceSeparators(Case):
    """REPRODUCED HOLE. The thousands-separator fix knew two characters, the ordinary space and
    U+00A0. Unicode has a column of the things, each of them splits a numeral, and each split made
    the gate read a different number than the reader does.
    """

    SEPARATORS = (("ordinary space", " "), ("no-break space", "\u00a0"),
                  ("thin space", "\u2009"), ("narrow no-break space", "\u202f"),
                  ("figure space", "\u2007"), ("zero-width space", "\u200b"))

    def printed(self, separator, value):
        m = base_manifest()
        m["claims"]["throughput"]["value"] = value
        return self.run_verify(m, self.document(
            "<p>The pair sustained 9%s526.6 tok/s.</p>" % separator))

    def test_every_separator_lets_the_gate_read_the_number_the_reader_reads(self):
        for name, separator in self.SEPARATORS:
            f = self.printed(separator, 9526.6)
            self.assertEqual(self.found(f, "A5", "error"), [],
                             "%s: %s" % (name, self.messages(f, "A5")))
            self.assertEqual(f.coverage["unit_bearing_covered"], 1, name)

    def test_every_separator_stops_the_split_reading_validating_an_unrelated_claim(self):
        """The attack each of these carries: a claim of 526.6 exists, the document prints a figure
        seven thousand larger, and the gate used to call it covered."""
        for name, separator in self.SEPARATORS:
            f = self.printed(separator, 526.6)
            self.assertTrue(self.found(f, "A5", "error"), name)
            self.assertIn("9,526.6", self.messages(f, "A5"), name)

    def test_every_separator_says_which_reading_it_took(self):
        for name, separator in self.SEPARATORS:
            f = self.printed(separator, 9526.6)
            self.assertTrue(self.found(f, "A5", "warn"), name)

    def test_a_zero_width_split_is_named_as_the_invisible_thing_it_is(self):
        """A reader is shown no gap at all, so this is not a thousands separator printed wrong; it
        is a character that should not be inside a number."""
        f = self.printed("\u200b", 9526.6)
        self.assertIn("ZERO-WIDTH", self.messages(f, "A5"))
        self.assertIn("9526.6", self.messages(f, "A5"))

    def test_a_non_three_digit_split_is_a_finding_and_not_a_guess(self):
        """"9 25.2 GB/s" is 925.2 to a merger and two numbers to a reader. The difference is a
        factor of thirty-six and there is NO correct guess in either direction, so the gate reports
        it instead of answering it. Note that the split reading is fully covered here: without this
        check the build is clean and the reader is looking at an unbacked figure."""
        m = base_manifest()
        m["claims"]["bandwidth"] = claim(25.2, unit="GB/s")
        f = self.run_verify(m, self.document("<p>The link carried 9 25.2 GB/s.</p>"))
        self.assertTrue(self.found(f, "A5", "error"), self.messages(f, "A5"))
        message = self.messages(f, "A5")
        self.assertIn("925.2", message)
        self.assertIn("9 and 25.2", message)

    def test_the_ambiguous_numeral_is_not_merged_in_either_direction(self):
        """Merging it would be the gate picking one of the two readings and then checking its own
        pick, which is this file's own failure mode wearing a fix's clothes."""
        text, sites = V.merge_space_groups("The link carried 9 25.2 GB/s.")
        self.assertEqual(text, "The link carried 9 25.2 GB/s.")
        self.assertEqual([site["kind"] for site in sites], ["ambiguous"])

    def test_a_conventional_split_is_still_merged_and_only_warned_about(self):
        """The control on the rule above: three-digit groups have exactly one reading, so the gate
        takes it rather than refusing to read a number it can read."""
        text, sites = V.merge_space_groups("The pair sustained 9 526.6 tok/s.")
        self.assertIn("9,526.6", text)
        self.assertEqual([site["kind"] for site in sites], ["thousands"])

    def test_a_zero_width_split_reads_as_one_number_whatever_the_grouping(self):
        """A zero-width space is invisible, so nothing about it is ambiguous: the reader sees
        925.2 and only an unprepared scanner sees two numbers."""
        m = base_manifest()
        m["claims"]["bandwidth"] = claim(925.2, unit="GB/s")
        f = self.run_verify(m, self.document("<p>The link carried 9\u200b25.2 GB/s.</p>"))
        self.assertEqual(self.found(f, "A5", "error"), [], self.messages(f, "A5"))
        self.assertIn("ZERO-WIDTH", self.messages(f, "A5"))

    def test_a_list_of_integers_is_not_a_split_numeral(self):
        """The false positive the wider pattern invites, and the reason the ambiguous rule asks
        for a unit or a decimal: "levels 1 2 4 8 16" is five figures in a sentence, and reading it
        as one would be this gate inventing the very thing it exists to catch."""
        f = self.run_verify(base_manifest(),
                            self.document("<p>Levels 1 2 4 8 16 were swept in order.</p>"))
        self.assertEqual(self.found(f, "A5", "error"), [], self.messages(f, "A5"))

    def test_a_digit_glued_to_a_word_is_still_not_a_thousands_group(self):
        """The guard the wider separator set must not lose: "GPU1 615.2 TFLOPS" is not 1,615.2."""
        m = base_manifest()
        m["claims"]["throughput"] = claim(615.2, unit="TFLOPS")
        f = self.run_verify(m, self.document("<p>GPU1 615.2 TFLOPS on the timed kernel.</p>"))
        self.assertEqual(self.found(f, "A5"), [], self.messages(f, "A5"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
