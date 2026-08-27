#!/usr/bin/env python3
"""Tests for the operational report, specifically the open-loop section.

The defect these exist for: `gpubench report` on a run that included an open-loop sweep produced a
document that could not say whether the engine kept up. A source-level count over report.py found
zero occurrences of latency_grew_over_the_level, engine_did_not_keep_up, generator_kept_up,
queue_growth or arrival, so the only verdict in the tool a capacity decision would rest on had no
consumer.

Every test below reads the RENDERED DOCUMENT, not a helper's return value, because a check that
reads a declaration is unfalsifiable. Each of the four rules is paired with a control fixture that
differs in one field, so dropping the rule flips one assertion in each direction rather than
leaving a test that passes on anything.

Run:  python -m tests.test_report      (from the repo root)
"""
import json
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, ".")
from gpubench import report as R  # noqa: E402


REAL = os.path.join("results", "onprem-2x5090-arrivals.json")


# ---------------------------------------------------------------- fixtures

def ol_level(rate=2.0, grew=False, basis="a basis the probe recorded", capacity=False,
             generator_kept_up=True, truncated=False, fraction=1.0, floor=0.7, censored=0,
             ttft95=9.87, n=96):
    """One open-loop level shaped exactly like the probe's own output.

    Defaults describe a clean level: judged, generator on schedule, fully completed. Each test
    changes ONE field, so the assertion that fires is attributable to that field.
    """
    arrival = {
        "model": "open_loop_poisson",
        "target_rate_req_s": rate,
        "achieved_arrival_rate_req_s": rate * 1.02,
        "requests_dispatched": n,
        "generator_kept_up": generator_kept_up,
        "truncated_by_harness_limit": truncated,
        "engine_did_not_keep_up": capacity,
        "queue_growth": {"n": n, "n_censored": censored,
                         "completion_fraction": fraction,
                         "completion_fraction_floor": floor},
    }
    if grew is not None or basis is not None:
        arrival["latency_grew_over_the_level"] = grew
        arrival["latency_grew_over_the_level_basis"] = basis
    return {
        "concurrency": None, "sample_count": n, "requests_ok": n,
        "arrival": arrival,
        "ttft_s": {"p50": 0.31, "p95": ttft95},
        "itl_ms": {"p50": 22.2, "p95": 260.8},
        "e2e_s": {"p50": 4.4, "p95": 7.34},
    }


def closed_level(concurrency=8, n=128):
    return {"concurrency": concurrency, "sample_count": n, "requests_ok": n,
            "arrival": {"model": "closed_loop", "target_rate_req_s": None},
            "output_tokens_per_s": 232.0, "per_request_output_tokens_per_s": 29.1,
            "ttft_s": {"p50": 1.22, "p95": 1.48}, "itl_ms": {"p50": 19.5, "p95": 44.0},
            "e2e_s": {"p50": 4.0, "p95": 6.0}}


def result(open_levels=(), closed_levels=()):
    res = {"tool": "gpubench", "mode": "exclusive", "schema_version": "1.0",
           "probes": {"inventory": {"gpus": [], "host": {}}}}
    if closed_levels:
        res["probes"]["serve_bench"] = {"levels": list(closed_levels)}
    if open_levels:
        res["probes"]["serve_bench_openloop"] = {"levels": list(open_levels)}
    return res


def render(res):
    fd, path = tempfile.mkstemp(suffix=".html")
    os.close(fd)
    try:
        R.write_report(res, path)
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    finally:
        os.remove(path)


def rows_of(doc, header):
    """The <tr> blocks of the table whose header row contains `header`.

    Reading cells out of the shipped HTML is the point: an assertion over the model dict would
    still pass if the renderer dropped the value on the floor.
    """
    tables = re.findall(r"<table>(.*?)</table>", doc, re.S)
    for t in tables:
        if header in t:
            return re.findall(r"<tr[^>]*>.*?</tr>", t, re.S)[1:]
    raise AssertionError("no table carrying header %r in the rendered document" % header)


def cells(row):
    return re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)


def section(doc, heading):
    """One <h2> section of the rendered document.

    Scoping matters for the absence assertions: "lower bound" is legitimate prose elsewhere in the
    report (a shared-mode run states that its compute figures are lower bounds), so a document-wide
    assertNotIn would fail for a reason that has nothing to do with the rule under test.
    """
    i = doc.index(heading)
    j = doc.find("<h2>", i)
    return doc[i:j if j > 0 else len(doc)]


# ---------------------------------------------------------------- rule 1

class TestRuleOneNotJudged(unittest.TestCase):
    """A None verdict renders as an explicit NOT JUDGED cell carrying its basis. Never blank,
    never a pass. A verdict that reads as silence is what made an overloaded engine look healthy.
    """

    BASIS = ("the generator missed its own schedule by 412 ms at p95, so the arrivals were a "
             "catch-up burst and not the process this level names")

    def test_none_verdict_renders_the_words_and_the_basis(self):
        doc = render(result(open_levels=[ol_level(rate=2.6, grew=None, basis=self.BASIS)]))
        self.assertIn("NOT JUDGED", doc)
        self.assertIn(self.BASIS, doc)

    def test_none_verdict_cell_is_not_blank_and_is_not_a_pass(self):
        doc = render(result(open_levels=[ol_level(rate=2.6, grew=None, basis=self.BASIS)]))
        row = rows_of(doc, "Engine queue grew")[0]
        verdict = cells(row)[3]
        self.assertIn("NOT JUDGED", verdict)
        self.assertTrue(re.sub(r"<[^>]+>", "", verdict).strip())
        self.assertNotIn("LATENCY DID NOT GROW", doc)
        self.assertNotIn("did not keep up", doc.lower())

    def test_none_verdict_without_any_basis_still_says_so(self):
        """The absent-field branch. A probe that recorded no basis must not buy silence."""
        lv = ol_level(rate=2.6, grew=None)
        lv["arrival"].pop("latency_grew_over_the_level_basis")
        doc = render(result(open_levels=[lv]))
        verdict = cells(rows_of(doc, "Engine queue grew")[0])[3]
        self.assertIn("NOT JUDGED", verdict)
        self.assertIn("no basis", verdict.lower())

    def test_control_a_judged_level_reads_as_judged(self):
        doc = render(result(open_levels=[ol_level(rate=2.6, grew=False)]))
        self.assertNotIn("NOT JUDGED", doc)
        self.assertIn("LATENCY DID NOT GROW", doc)

    def test_the_count_of_unjudged_levels_is_stated_up_front(self):
        doc = render(result(open_levels=[ol_level(rate=1.0, grew=False),
                                         ol_level(rate=2.6, grew=None)]))
        self.assertIn("<b>1 reached no verdict</b>", doc)

    def test_an_unjudged_level_never_becomes_the_headline_rate(self):
        """The KPI is a second surface the same defect can escape through."""
        doc = render(result(open_levels=[ol_level(rate=9.9, grew=None)]))
        self.assertNotIn("highest offered rate with flat latency", doc)
        doc2 = render(result(open_levels=[ol_level(rate=9.9, grew=False)]))
        self.assertIn("highest offered rate with flat latency", doc2)

    def test_deprecated_field_name_is_still_read_as_a_verdict(self):
        """A document written by an older probe carries fell_behind. Treating it as absent would
        turn a real verdict into a NOT JUDGED, which is the opposite failure but still a wrong
        document."""
        lv = ol_level(rate=2.6)
        lv["arrival"].pop("latency_grew_over_the_level")
        lv["arrival"].pop("latency_grew_over_the_level_basis")
        lv["arrival"]["fell_behind"] = True
        lv["arrival"]["fell_behind_basis"] = "the older probe's own wording"
        doc = render(result(open_levels=[lv]))
        self.assertNotIn("NOT JUDGED", doc)
        self.assertIn(R.esc("the older probe's own wording"), doc)


# ---------------------------------------------------------------- rule 2

class TestRuleTwoVoidedRows(unittest.TestCase):
    """A row whose generator missed its schedule, or that the harness truncated, is voided
    BEFORE any latency percentile on it is read."""

    def _assert_voided(self, doc):
        rows = rows_of(doc, "End-to-end p95 (s)")
        self.assertIn('class="voided"', rows[0])
        self.assertIn("VOIDED", rows[0])
        # The number is struck, and it appears nowhere in the document unstruck.
        self.assertIn("<s>9.87</s>", doc)
        self.assertEqual(doc.count("9.87"), doc.count("<s>9.87</s>"))

    def test_generator_that_missed_its_schedule_voids_the_row(self):
        self._assert_voided(render(result(open_levels=[ol_level(generator_kept_up=False)])))

    def test_truncation_by_the_harness_voids_the_row(self):
        self._assert_voided(render(result(open_levels=[ol_level(truncated=True)])))

    def test_the_void_marker_precedes_the_numbers_in_the_row(self):
        """"Voided before any percentile is read" is a statement about reading order, so the
        marker has to come first in document order, not in a footnote."""
        row = rows_of(render(result(open_levels=[ol_level(truncated=True)])),
                      "End-to-end p95 (s)")[0]
        self.assertLess(row.index("VOIDED"), row.index("9.87"))

    def test_control_a_clean_row_is_not_voided_and_its_numbers_are_plain(self):
        doc = render(result(open_levels=[ol_level()]))
        self.assertNotIn('class="voided"', doc)
        self.assertNotIn("VOIDED", doc)
        self.assertIn("9.87", doc)
        self.assertNotIn("<s>9.87</s>", doc)

    def test_a_voided_level_is_counted_and_called_out(self):
        doc = render(result(open_levels=[ol_level(rate=1.0), ol_level(rate=2.6, truncated=True)]))
        self.assertIn("1 were voided", doc)
        self.assertIn("2.60 req/s", doc)

    def test_a_voided_row_does_not_render_its_verdict_as_a_pass(self):
        """A verdict is more load-bearing than a percentile, so voiding has to reach it too. A
        document can arrive carrying both a void condition and a verdict, and reading the verdict
        anyway is the same defect one level up."""
        doc = render(result(open_levels=[ol_level(grew=False, truncated=True)]))
        self.assertNotIn("LATENCY DID NOT GROW", doc)
        self.assertIn("ROW VOIDED", doc)

    def test_a_voided_row_makes_no_capacity_claim_or_lower_bound_claim(self):
        doc = render(result(open_levels=[ol_level(grew=True, capacity=True, fraction=0.4,
                                                  generator_kept_up=False)]))
        self.assertNotIn("did not keep up", doc.lower())
        self.assertNotIn("is a lower bound", doc)

    def test_a_voided_level_never_becomes_the_headline_rate(self):
        doc = render(result(open_levels=[ol_level(rate=9.9, grew=False, truncated=True)]))
        self.assertNotIn("highest offered rate with flat latency", doc)


# ---------------------------------------------------------------- rule 3

class TestRuleThreeCapacityWording(unittest.TestCase):
    """"The engine did not keep up" appears only where engine_did_not_keep_up is True.

    latency_grew_over_the_level alone is a measurement of drift. On a machine serving several
    environments from one engine, a co-tenant slowdown produces it with no queue anywhere.
    """

    PHRASE = "did not keep up"

    def test_growth_without_a_confirmed_queue_never_claims_a_cause(self):
        doc = render(result(open_levels=[ol_level(grew=True, capacity=False)]))
        self.assertNotIn(self.PHRASE, doc.lower())
        self.assertIn("cause is not established", doc)

    def test_growth_with_no_capacity_field_at_all_never_claims_a_cause(self):
        """The absent-field branch: an engine whose metrics endpoint was unreachable reports
        nothing here, and nothing is not a no."""
        lv = ol_level(grew=True)
        lv["arrival"].pop("engine_did_not_keep_up")
        doc = render(result(open_levels=[lv]))
        self.assertNotIn(self.PHRASE, doc.lower())
        self.assertIn("not reported", doc)

    def test_a_confirmed_engine_queue_is_the_only_thing_that_earns_the_phrase(self):
        doc = render(result(open_levels=[ol_level(grew=True, capacity=True)]))
        self.assertIn(self.PHRASE, doc.lower())
        self.assertIn("waiting count grew with it", doc)

    def test_the_two_states_render_as_different_verdicts(self):
        drift = render(result(open_levels=[ol_level(grew=True, capacity=False)]))
        queued = render(result(open_levels=[ol_level(grew=True, capacity=True)]))
        self.assertIn("LATENCY GREW, CAUSE NOT ESTABLISHED", drift)
        self.assertNotIn("LATENCY GREW, CAUSE NOT ESTABLISHED", queued)
        self.assertIn("ENGINE DID NOT KEEP UP", queued)

    def test_the_restraint_is_stated_where_a_reader_would_overstate_it(self):
        doc = render(result(open_levels=[ol_level(grew=True, capacity=False)]))
        self.assertIn("does not identify a cause", doc)
        self.assertIn("co-tenant", doc)


# ---------------------------------------------------------------- rule 4

class TestRuleFourLowerBounds(unittest.TestCase):
    """A percentile from a level below its stated completion floor is labelled a lower bound,
    because the requests that would prove a queue are the ones missing from the fit."""

    def _pct_cells(self, doc):
        return cells(rows_of(doc, "End-to-end p95 (s)")[0])[4:]

    def test_below_the_floor_every_percentile_carries_the_label(self):
        doc = render(result(open_levels=[ol_level(fraction=0.42, floor=0.7, censored=56)]))
        for cell in self._pct_cells(doc):
            self.assertIn("lower bound", cell)
        self.assertIn("below the 70% completion floor", doc)
        self.assertIn("is a lower bound", doc)

    def test_control_a_complete_level_is_not_labelled(self):
        doc = render(result(open_levels=[ol_level(fraction=1.0, floor=0.7)]))
        self.assertNotIn("lower bound", doc)

    def test_exactly_at_the_floor_is_not_below_it(self):
        doc = render(result(open_levels=[ol_level(fraction=0.7, floor=0.7)]))
        self.assertNotIn("lower bound", doc)

    def test_an_omitted_completion_fraction_is_not_an_all_clear(self):
        """The opt-out branch: a field that only ever weakens a check can be left out. A level
        that never says how much of its load came back has not shown its sample is complete."""
        lv = ol_level()
        lv["arrival"]["queue_growth"].pop("completion_fraction")
        doc = render(result(open_levels=[lv]))
        self.assertIn("not reported", doc)
        for cell in self._pct_cells(doc):
            self.assertIn("lower bound", cell)

    def test_an_omitted_floor_with_a_shortfall_is_not_an_all_clear(self):
        lv = ol_level(fraction=0.5)
        lv["arrival"]["queue_growth"].pop("completion_fraction_floor")
        doc = render(result(open_levels=[lv]))
        self.assertIn("no completion floor stated", doc)
        for cell in self._pct_cells(doc):
            self.assertIn("lower bound", cell)

    def test_the_censored_count_is_visible_beside_the_fraction(self):
        doc = render(result(open_levels=[ol_level(fraction=0.42, floor=0.7, censored=56)]))
        row = rows_of(doc, "Censored in fit")[0]
        self.assertEqual(cells(row)[8].strip(), "56")


# ---------------------------------------------------------------- sample size and rank

class TestSampleSizeAndRank(unittest.TestCase):
    """A p95 over 64 samples and a p95 over 128 are different statistics. This data has both."""

    def test_rank_arithmetic_matches_the_probe_s_own_percentile(self):
        """Same method as probes/serving.py:pct, so the report describes what was computed."""
        from gpubench.probes.serving import pct
        for n in (64, 96, 128):
            r = R.percentile_rank(n, 95)
            data = list(range(1, n + 1))            # value == 1-indexed rank, so pct is the rank
            self.assertAlmostEqual(pct(data, 95), r["position"], places=9)
            self.assertEqual(r["lower"], int(r["position"]))
            self.assertEqual(r["above"], n - r["lower"])

    def test_closed_loop_table_shows_n_and_the_resolved_rank_per_level(self):
        doc = render(result(closed_levels=[closed_level(1, 64), closed_level(8, 128)]))
        row64, row128 = rows_of(doc, "p95 resolves at")[:2]
        self.assertEqual(cells(row64)[6].strip(), "64")
        self.assertIn("60-61 of 64", cells(row64)[7])
        self.assertEqual(cells(row128)[6].strip(), "128")
        self.assertIn("121-122 of 128", cells(row128)[7])

    def test_unequal_sample_sizes_are_called_out_rather_than_left_to_the_reader(self):
        mixed = render(result(closed_levels=[closed_level(1, 64), closed_level(8, 128)]))
        self.assertIn("do not all carry the same sample size", mixed)
        same = render(result(closed_levels=[closed_level(2, 128), closed_level(8, 128)]))
        self.assertNotIn("do not all carry the same sample size", same)

    def test_open_loop_levels_carry_their_n_too(self):
        doc = render(result(open_levels=[ol_level(n=96)]))
        row = rows_of(doc, "End-to-end p95 (s)")[0]
        self.assertEqual(cells(row)[2].strip(), "96")
        self.assertIn("91-92 of 96", cells(row)[3])


# ---------------------------------------------------------------- the real run

class TestAgainstTheRealArrivalsRun(unittest.TestCase):
    """The measured run this feature was written for: 7 closed-loop levels in one probe and 3
    open-loop Poisson levels in another. Reading only the first probe is how the open-loop levels
    went unreported in the first place."""

    @classmethod
    def setUpClass(cls):
        if not os.path.exists(REAL):
            raise unittest.SkipTest("%s is not in this checkout" % REAL)
        with open(REAL, "r", encoding="utf-8") as f:
            cls.res = json.load(f)
        cls.doc = render(cls.res)

    def test_both_probes_are_read_and_the_levels_are_not_mixed(self):
        self.assertEqual(len(R._closed_loop_levels(self.res)), 7)
        self.assertEqual(len(R._open_loop_views(self.res)), 3)

    def test_every_open_loop_field_named_in_the_brief_reaches_the_page(self):
        for rate in ("1.00", "1.80", "2.60"):
            self.assertIn("<td>%s</td>" % rate, self.doc)
        for achieved in ("1.09", "1.97", "2.84"):        # achieved_arrival_rate_req_s
            self.assertIn("<td>%s</td>" % achieved, self.doc)
        self.assertEqual(self.doc.count("LATENCY DID NOT GROW"), 3)
        self.assertEqual(self.doc.count("<td>100%</td>"), 3)  # completion_fraction
        self.assertIn("Censored in fit", self.doc)

    def test_the_measured_basis_text_is_carried_verbatim(self):
        basis = (self.res["probes"]["serve_bench_openloop"]["levels"][0]
                 ["arrival"]["latency_grew_over_the_level_basis"])
        self.assertIn(R.esc(basis), self.doc)

    def test_this_run_makes_no_capacity_claim(self):
        """No level in it confirmed an engine queue, so the phrase must not appear anywhere."""
        self.assertNotIn("did not keep up", self.doc.lower())

    def test_no_row_is_voided_and_no_percentile_is_a_lower_bound(self):
        ol = section(self.doc, "Offered load:")
        self.assertNotIn("VOIDED", ol)
        self.assertNotIn("lower bound", ol)

    def test_the_headline_rate_is_the_highest_judged_flat_level(self):
        self.assertIn("highest offered rate with flat latency", self.doc)
        self.assertIn('<div class="v">2.6 req/s</div>', self.doc)

    def test_the_closed_loop_n_of_64_at_concurrency_one_is_visible(self):
        row = rows_of(self.doc, "p95 resolves at")[0]
        self.assertEqual(cells(row)[0].strip(), "1")
        self.assertEqual(cells(row)[6].strip(), "64")
        self.assertIn("60-61 of 64", cells(row)[7])
        self.assertIn("do not all carry the same sample size", self.doc)


# ---------------------------------------------------------------- prose hygiene

class TestNoDashesInOutput(unittest.TestCase):
    def test_rendered_document_has_no_em_dash_or_double_hyphen(self):
        doc = render(result(open_levels=[ol_level(grew=True, capacity=True, truncated=True,
                                                  fraction=0.4)],
                            closed_levels=[closed_level()]))
        # The stylesheet is full of legitimate double hyphens (CSS custom properties), so it is
        # removed before the prose is checked rather than exempted by a weaker pattern.
        prose = re.sub(r"<style>.*?</style>", " ", doc, flags=re.S).replace("&mdash;", " ")
        text = re.sub(r"<[^>]+>", " ", prose)
        self.assertNotIn("—", text)
        self.assertNotIn("--", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
