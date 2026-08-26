#!/usr/bin/env python3
"""Tests for the diagnostics rules. No device, no network.

Each test corresponds to a conclusion a human had to reach by hand at least once. The tests exist so
the tool keeps reaching it, and -- just as importantly -- so it keeps DECLINING to reach a stronger
conclusion than the evidence supports.

Run:  python -m tests.test_diagnose      (from the repo root)
"""
import sys
import unittest

sys.path.insert(0, ".")
from gpubench import diagnose as D  # noqa: E402


def bundle(**probes):
    b = {"mode": "shared", "probes": dict(probes)}
    return b


def only(res, rule, attribution=None):
    out = D.diagnose(res, attribution)["findings"]
    return [f for f in out if f["rule"] == rule][0]


def levels(*deltas):
    return {"levels": [{"concurrency": i + 1, "server_metrics_delta": d}
                       for i, d in enumerate(deltas)]}


class TestPrefixCache(unittest.TestCase):
    """The rule this whole module was written for. Four readings, four different conclusions."""

    def test_zero_queries_with_caching_off_is_never_consulted(self):
        r = bundle(serve_bench=levels({"prefix_cache_queries": 0.0, "prefix_cache_hits": 0.0}),
                   engine_config={"normalised": {"prefix_caching_enabled": False}})
        f = only(r, "prefix-cache")
        self.assertEqual(f["severity"], "info")
        self.assertIn("never consulted", f["headline"])
        self.assertIn("zero queries, not zero hits", f["headline"])

    def test_it_refuses_to_say_cannot(self):
        """The error that got made for real: an investigator upgraded "did not" to "cannot" and
        inverted a recommendation. The rule must carry the guard against it, in words."""
        r = bundle(serve_bench=levels({"prefix_cache_queries": 0.0, "prefix_cache_hits": 0.0}),
                   engine_config={"normalised": {"prefix_caching_enabled": False}})
        f = only(r, "prefix-cache")
        self.assertIn("do_not_overstate", f)
        self.assertIn("not 'unsupported' or 'cannot'", f["do_not_overstate"])
        # And the finding itself must not contain the overstatement.
        text = (f["headline"] + f["detail"]).lower()
        self.assertNotIn("cannot prefix", text)
        self.assertNotIn("unsupported", text)

    def test_zero_queries_with_caching_ON_is_blocking(self):
        """Caching on and the counter never moving means one of the two is wrong. Neither
        'the cache was defeated' nor 'the cache was inactive' may be claimed."""
        r = bundle(serve_bench=levels({"prefix_cache_queries": 0.0, "prefix_cache_hits": 0.0}),
                   engine_config={"normalised": {"prefix_caching_enabled": True}})
        f = only(r, "prefix-cache")
        self.assertEqual(f["severity"], "blocking")
        self.assertIn("never incremented", f["headline"])

    def test_nonzero_hit_rate_warns_that_prefill_is_overstated(self):
        r = bundle(serve_bench=levels({"prefix_cache_queries": 100.0, "prefix_cache_hits": 40.0}),
                   engine_config={"normalised": {"prefix_caching_enabled": True}})
        f = only(r, "prefix-cache")
        self.assertEqual(f["severity"], "warning")
        self.assertIn("40.0%", f["headline"])
        self.assertIn("overstated", f["detail"])

    def test_clean_miss_is_not_alarming(self):
        r = bundle(serve_bench=levels({"prefix_cache_queries": 500.0, "prefix_cache_hits": 0.0}),
                   engine_config={"normalised": {"prefix_caching_enabled": True}})
        f = only(r, "prefix-cache")
        self.assertEqual(f["severity"], "info")
        self.assertIn("clean", f["detail"])

    def test_no_counters_at_all_is_unknown_not_pass(self):
        """The distinction that makes the module honest: absence of evidence is reported as
        absence of evidence, not as evidence of absence."""
        r = bundle(serve_bench={"levels": [{"concurrency": 1}]})
        f = only(r, "prefix-cache")
        self.assertEqual(f["severity"], "unknown")
        self.assertIn("weaker claim", f["detail"])

    def test_counters_zero_but_no_resolved_config_is_unknown(self):
        """This is the state the tool was in before the engine_config probe existed: it could see
        zero queries and could not explain them. It must say so rather than pick a story."""
        r = bundle(serve_bench=levels({"prefix_cache_queries": 0.0, "prefix_cache_hits": 0.0}))
        f = only(r, "prefix-cache")
        self.assertEqual(f["severity"], "unknown")
        self.assertIn("engine_config", f["action"])


class TestRoofMode(unittest.TestCase):
    def test_shared_mode_roofs_are_floors_and_percentages_are_upper_bounds(self):
        f = only({"mode": "shared", "probes": {}}, "roof-mode")
        self.assertEqual(f["severity"], "warning")
        self.assertIn("FLOORS", f["headline"])
        self.assertIn("UPPER bound", f["detail"])

    def test_exclusive_mode_is_clean(self):
        f = only({"mode": "exclusive", "probes": {}}, "roof-mode")
        self.assertEqual(f["severity"], "info")

    def test_unrecorded_mode_is_unknown(self):
        f = only({"probes": {}}, "roof-mode")
        self.assertEqual(f["severity"], "unknown")


class TestAttribution(unittest.TestCase):
    def test_negative_residual_is_blocking_and_names_the_usual_cause(self):
        """The real failure: checkpoint size on disk used instead of resident weight bytes."""
        f = only({"probes": {}}, "attribution",
                 attribution=D.__dict__ and {"measured_step_ms": 5.0, "bandwidth_floor_ms": 9.8,
                                             "comms_ms": 3.1, "unexplained_ms": -7.9})
        self.assertEqual(f["severity"], "blocking")
        self.assertIn("checkpoint size", f["detail"])

    def test_large_residual_warns_rather_than_presenting_a_complete_story(self):
        f = only({"probes": {}}, "attribution",
                 attribution={"measured_step_ms": 15.0, "bandwidth_floor_ms": 3.0,
                              "comms_ms": 1.0, "unexplained_ms": 11.0})
        self.assertEqual(f["severity"], "warning")
        self.assertIn("unexplained", f["headline"])

    def test_missing_inputs_reported_not_defaulted(self):
        f = only({"probes": {}}, "attribution",
                 attribution={"unavailable": "needs resident weight bytes and layer count"})
        self.assertEqual(f["severity"], "unknown")


class TestPower(unittest.TestCase):
    def _sus(self, capped, samples, hw=0, temp=79.0, mean=567.9):
        return {"torch_compute": {"sustained": [{"power": {
            "samples": samples, "sw_power_cap_active_samples": capped,
            "hw_slowdown_active_samples": hw, "power_mean_w": mean, "temp_max_c": temp}}]}}

    def test_power_bound_is_distinguished_from_thermal(self):
        f = only({"mode": "shared", "probes": self._sus(200, 201)}, "power")
        self.assertIn("Power-bound", f["headline"])
        self.assertIn("not thermal", f["detail"])
        self.assertIn("more cooling buys nothing", f["detail"])

    def test_hardware_slowdown_outranks_a_power_cap(self):
        f = only({"mode": "shared", "probes": self._sus(200, 201, hw=5)}, "power")
        self.assertEqual(f["severity"], "warning")
        self.assertIn("throttling", f["headline"])

    def test_no_sampling_explains_why_a_low_reading_would_be_an_artefact(self):
        f = only({"mode": "shared", "probes": {}}, "power")
        self.assertEqual(f["severity"], "unknown")
        self.assertIn("artefact", f["detail"])


class TestDeviceParity(unittest.TestCase):
    def test_asymmetric_links_are_surfaced(self):
        r = {"mode": "shared", "probes": {"inventory": {"pcie_links": [
            {"bdf": "01:00.0", "bridge_max_speed": "32 GT/s", "bridge_max_width": 16},
            {"bdf": "02:00.0", "bridge_max_speed": "16 GT/s", "bridge_max_width": 4}]}}}
        f = only(r, "device-parity")
        self.assertEqual(f["severity"], "warning")
        self.assertIn("NON-EQUIVALENT", f["headline"])
        self.assertIn("property of the board", f["detail"])

    def test_symmetric_links_are_clean(self):
        link = {"bridge_max_speed": "32 GT/s", "bridge_max_width": 16}
        r = {"mode": "shared", "probes": {"inventory": {"pcie_links": [
            dict(link, bdf="01:00.0"), dict(link, bdf="02:00.0")]}}}
        self.assertEqual(only(r, "device-parity")["severity"], "info")


class TestWorkloadDisclosure(unittest.TestCase):
    def test_undisclosed_workload_is_blocking(self):
        r = bundle(serve_bench={"levels": [{"concurrency": 1}]})
        f = only(r, "workload")
        self.assertEqual(f["severity"], "blocking")
        self.assertIn("undisclosed", f["headline"])

    def test_size_control_verified_when_both_counts_present(self):
        r = bundle(serve_bench={"workload": {"kind": "synthetic"}, "levels": [
            {"requests_ok": 8, "input_tokens_requested": 512, "input_tokens": 4104}]})
        f = only(r, "workload")
        self.assertEqual(f["severity"], "info")
        self.assertIn("0.20%", f["headline"])

    def test_drifting_size_control_warns_and_says_what_it_invalidates(self):
        r = bundle(serve_bench={"workload": {"kind": "synthetic"}, "levels": [
            {"requests_ok": 8, "input_tokens_requested": 512, "input_tokens": 4600}]})
        f = only(r, "workload")
        self.assertEqual(f["severity"], "warning")
        self.assertIn("REQUESTED", f["detail"])


class TestQualityGate(unittest.TestCase):
    def test_unpublished_cases_make_a_pass_an_assertion(self):
        r = bundle(accuracy={"summary": {"verdict": "PASS", "cases": 10}})
        f = only(r, "quality-gate")
        self.assertEqual(f["severity"], "warning")
        self.assertIn("not published", f["headline"])

    def test_published_cases_pass_cleanly(self):
        r = bundle(accuracy={"summary": {"verdict": "PASS", "cases": 10},
                             "method": {"cases_published": [{"prompt": "x", "accept_pattern": "y"}]}})
        self.assertEqual(only(r, "quality-gate")["severity"], "info")

    def test_a_failing_gate_warns_about_the_gate_itself_first(self):
        """Learned the hard way: the gate's first ever run reported total failure, and the cause was
        the harness truncating a reasoning model rather than the stack being broken."""
        r = bundle(accuracy={"summary": {"verdict": "FAIL", "cases": 10},
                             "method": {"cases_published": [{"prompt": "x"}]}})
        f = only(r, "quality-gate")
        self.assertEqual(f["severity"], "blocking")
        self.assertIn("gate that fails on its own truncation", f["detail"])

    def test_no_gate_at_all_warns_that_speed_can_be_bought_with_quality(self):
        f = only(bundle(), "quality-gate")
        self.assertEqual(f["severity"], "warning")
        self.assertIn("faster from worse", f["detail"])


class TestHonesty(unittest.TestCase):
    """Properties of the module as a whole, rather than of any one rule."""

    def test_an_empty_result_produces_unknowns_not_an_all_clear(self):
        d = D.diagnose({"probes": {}})
        self.assertGreater(d["summary"]["unknown"], 0)
        self.assertEqual(d["summary"]["blocking"], 0)
        # The critical property: it must not look like a pass.
        self.assertTrue(any(f["severity"] == "unknown" for f in d["findings"]))

    def test_every_rule_always_reports_something(self):
        """A rule that returns nothing is indistinguishable from a rule that found nothing wrong."""
        d = D.diagnose({"probes": {}})
        rules = {f["rule"] for f in d["findings"]}
        self.assertEqual(len(d["findings"]), len(D.RULES) + 1)  # +1 for attribution
        self.assertIn("sampling", rules)
        self.assertIn("prefix-cache", rules)

    def test_every_finding_carries_evidence_and_a_valid_severity(self):
        d = D.diagnose({"probes": {}})
        for f in d["findings"]:
            self.assertIn(f["severity"], D.SEVERITIES)
            self.assertIn("evidence", f)
            self.assertTrue(f["headline"])
            self.assertTrue(f["detail"])

    def test_findings_are_ordered_by_what_needs_acting_on(self):
        r = bundle(serve_bench={"levels": [{"concurrency": 1}]})  # yields a blocking workload finding
        sev = [f["severity"] for f in D.diagnose(r)["findings"]]
        order = {s: i for i, s in enumerate(D.SEVERITIES)}
        self.assertEqual(sev, sorted(sev, key=lambda s: order[s]))



class TestSampling(unittest.TestCase):
    """The rule guarding the defect an external review found: 233 tok/s in one table and 204.5 in
    another for the same nominal concurrency, because one run's request count did not divide."""

    def _lv(self, conc, n, whole, ttft=True):
        d = {"concurrency": conc, "sample_count": n, "whole_waves": whole}
        if ttft:
            d["ttft_s"] = {"p50": 1.0, "p95": 2.0}
        return d

    def test_partial_wave_is_blocking(self):
        r = bundle(serve_bench={"levels": [self._lv(1, 12, True), self._lv(8, 12, False)]})
        f = only(r, "sampling")
        self.assertEqual(f["severity"], "blocking")
        self.assertIn("PARTIAL", f["headline"])
        self.assertIn(8, f["evidence"]["partial_wave_levels"])

    def test_partial_wave_explains_why_it_reads_as_scatter(self):
        """The reason this defect survived review: it appears only where the count fails to
        divide, so it looks like one anomalous level rather than a systematic fault."""
        r = bundle(serve_bench={"levels": [self._lv(8, 12, False)]})
        self.assertIn("scatter", only(r, "sampling")["detail"])

    def test_missing_sample_count_warns(self):
        r = bundle(serve_bench={"levels": [{"concurrency": 8, "whole_waves": True,
                                            "ttft_s": {"p50": 1.0, "p95": 2.0}}]})
        f = only(r, "sampling")
        self.assertEqual(f["severity"], "warning")
        self.assertIn("without a sample size", f["headline"])

    def test_small_sample_warns_that_p95_is_not_a_tail(self):
        r = bundle(serve_bench={"levels": [self._lv(8, 8, True)]})
        f = only(r, "sampling")
        self.assertEqual(f["severity"], "warning")
        self.assertIn("barely distinguishable from the maximum", f["detail"])

    def test_whole_waves_with_real_n_is_clean(self):
        r = bundle(serve_bench={"levels": [self._lv(1, 32, True), self._lv(8, 32, True)]})
        f = only(r, "sampling")
        self.assertEqual(f["severity"], "info")
        self.assertIn("whole waves", f["headline"])

    def test_incomplete_level_does_not_raise(self):
        """A diagnostic that crashes on partial data is worse than one that says it cannot check.
        This rule did exactly that on its first test run."""
        r = bundle(serve_bench={"levels": [{}, {"concurrency": 4}]})
        self.assertIn(only(r, "sampling")["severity"], D.SEVERITIES)

if __name__ == "__main__":
    unittest.main(verbosity=2)
