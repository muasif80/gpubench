#!/usr/bin/env python3
"""Tests for the derivation module. No GPU, no network, no fixtures on disk.

Every test here corresponds to a mistake that was actually made and shipped, or to a property the
report's reproducibility contract depends on. The names say which.

Run:  python -m tests.test_analysis      (from the repo root)
"""
import math
import sys
import unittest

sys.path.insert(0, ".")
from gpubench import analysis as A  # noqa: E402


# The measured all-reduce sweep from the machine the historical defect was found on. Latency spans
# three orders of magnitude across it; bandwidth spans 7%. That contrast is the whole reason the
# interpolation has to go through bandwidth.
SWEEP = [
    {"size_bytes": 4096, "latency_ms": 0.0201, "bus_gb_s": 0.204},
    {"size_bytes": 65536, "latency_ms": 0.0243, "bus_gb_s": 2.697},
    {"size_bytes": 262144, "latency_ms": 0.0791, "bus_gb_s": 3.313},
    {"size_bytes": 1048576, "latency_ms": 0.3057, "bus_gb_s": 3.430},
    {"size_bytes": 4194304, "latency_ms": 1.1697, "bus_gb_s": 3.586},
    {"size_bytes": 16777216, "latency_ms": 4.6217, "bus_gb_s": 3.630},
    {"size_bytes": 67108864, "latency_ms": 18.2836, "bus_gb_s": 3.670},
]


def _log_linear_latency(rows, nbytes):
    """The defect, preserved so a test can prove the fix beats it.

    This is what the shipped code did before v8.0: interpolate LATENCY log-linearly between the
    bracketing measured points.
    """
    rows = sorted(rows, key=lambda r: r["size_bytes"])
    if nbytes <= rows[0]["size_bytes"]:
        return rows[0]["latency_ms"]
    if nbytes >= rows[-1]["size_bytes"]:
        return rows[-1]["latency_ms"] * (nbytes / float(rows[-1]["size_bytes"]))
    for a, b in zip(rows, rows[1:]):
        if a["size_bytes"] <= nbytes <= b["size_bytes"]:
            t = ((math.log(nbytes) - math.log(a["size_bytes"]))
                 / (math.log(b["size_bytes"]) - math.log(a["size_bytes"])))
            return a["latency_ms"] + t * (b["latency_ms"] - a["latency_ms"])
    return None


class TestAllreduceInterpolation(unittest.TestCase):
    """The v8.0 defect: an unreproducible derived column."""

    def test_measured_points_are_returned_unchanged(self):
        """The single most important property. A derived value at a MEASURED size must equal the
        measurement, or the published table and the published derivation disagree and a reader with
        a calculator finds it immediately."""
        for r in SWEEP[1:]:
            got = A.allreduce_latency_ms(SWEEP, r["size_bytes"])
            implied_bw = r["size_bytes"] / (got / 1000.0) / A.GB
            self.assertAlmostEqual(implied_bw, r["bus_gb_s"], places=6,
                                   msg="at %d bytes the derivation must reproduce the measured "
                                       "bandwidth exactly" % r["size_bytes"])

    def test_interpolated_bandwidth_stays_between_neighbours(self):
        """Between two samples, bandwidth must lie between the two measured bandwidths. The old
        log-linear-latency method violated this: it implied 2.95 GB/s between samples measuring
        3.43 and 3.59, i.e. a bandwidth lower than anything ever measured."""
        nbytes = 1310720          # 1.25 MiB, between the 1 MiB and 4 MiB samples
        lo, hi = 3.430, 3.586
        good = nbytes / (A.allreduce_latency_ms(SWEEP, nbytes) / 1000.0) / A.GB
        self.assertGreaterEqual(good, lo - 1e-9)
        self.assertLessEqual(good, hi + 1e-9)

        bad = nbytes / (_log_linear_latency(SWEEP, nbytes) / 1000.0) / A.GB
        self.assertLess(bad, lo, "the historical method should be shown to fall below the "
                                 "measured range -- if this assertion fails the fixture is wrong")

    def test_fix_beats_the_defect_by_the_documented_margin(self):
        """The module docstring claims the old method overstated latency by up to 17%. Assert it,
        so the claim in the prose cannot drift from the code."""
        worst = 0.0
        for n in (1310720, 5242880, 20971520):
            good = A.allreduce_latency_ms(SWEEP, n)
            bad = _log_linear_latency(SWEEP, n)
            worst = max(worst, (bad - good) / good * 100.0)
        self.assertGreater(worst, 10.0)
        self.assertLess(worst, 25.0)

    def test_flat_region_below_smallest_sample(self):
        self.assertEqual(A.allreduce_latency_ms(SWEEP, 1024), SWEEP[0]["latency_ms"])

    def test_saturated_region_scales_linearly(self):
        """Past the largest sample the link is saturated, so doubling the message must double the
        time. If this ever becomes sublinear something is extrapolating optimistically."""
        big = SWEEP[-1]["size_bytes"] * 4
        self.assertAlmostEqual(A.allreduce_latency_ms(SWEEP, big),
                               A.allreduce_latency_ms(SWEEP, big // 2) * 2, places=6)

    def test_empty_and_malformed_sweeps_return_none_not_zero(self):
        """A missing measurement must never render as a number. Returning 0 here would produce an
        infinite ceiling downstream and look like extraordinary hardware."""
        self.assertIsNone(A.allreduce_latency_ms([], 4096))
        self.assertIsNone(A.allreduce_latency_ms([{"size_bytes": 4096}], 4096))


class TestPrefillCeiling(unittest.TestCase):
    def test_ceiling_is_rebuildable_by_hand(self):
        """G6, the reproducibility contract: every derived value must be reconstructible with a
        calculator from printed numbers."""
        res = A.prefill_comms_ceiling(SWEEP, hidden_size=5120, allreduces_per_pass=128,
                                      prompt_lengths=[2048])
        row = res["by_prompt_length"]["2048"]
        self.assertEqual(row["message_bytes"], 2048 * 5120 * 2)
        by_hand = 2048 / (row["allreduce_ms"] * 128 / 1000.0)
        self.assertAlmostEqual(row["tokens_per_s_comms_ceiling"], by_hand, places=9)

    def test_ceiling_is_nearly_flat_across_lengths(self):
        """Physics check. Bandwidth is nearly constant in the saturated regime, so the token
        ceiling must be nearly constant too. A ceiling that climbs steeply with prompt length is
        the signature of the interpolation bug, not of a real machine."""
        res = A.prefill_comms_ceiling(SWEEP, 5120, 128, [512, 2048, 8192, 32768])
        vals = [v["tokens_per_s_comms_ceiling"] for v in res["by_prompt_length"].values()]
        self.assertLess((max(vals) - min(vals)) / min(vals), 0.10)

    def test_reports_its_inputs(self):
        """Rule 2 of the module: a derived value that cannot name its inputs is an assertion."""
        res = A.prefill_comms_ceiling(SWEEP, 5120, 128, [512])
        for k in ("hidden_size", "allreduces_per_pass", "activation_bytes"):
            self.assertIn(k, res["inputs"])
        self.assertIn("formula", res)

    def test_missing_inputs_yield_none(self):
        self.assertIsNone(A.prefill_comms_ceiling(SWEEP, None, 128, [512]))
        self.assertIsNone(A.prefill_comms_ceiling(SWEEP, 5120, None, [512]))
        self.assertIsNone(A.prefill_comms_ceiling([], 5120, 128, [512]))


class TestDecodeAttribution(unittest.TestCase):
    def test_components_sum_to_the_measurement(self):
        att = A.decode_attribution(15.086, 9.8155, 3.1399)
        self.assertAlmostEqual(
            att["bandwidth_floor_ms"] + att["comms_ms"] + att["unexplained_ms"],
            att["measured_step_ms"], places=9)

    def test_negative_residual_is_flagged_not_hidden(self):
        """The failure this catches: using checkpoint size instead of resident weight size inflates
        the floor past the measured step. It produced a plausible table with a negative row."""
        att = A.decode_attribution(5.0, 9.8, 3.1)
        self.assertLess(att["unexplained_ms"], 0)
        self.assertTrue(att["warnings"])
        self.assertIn("Negative residual", att["warnings"][0])

    def test_no_measurement_yields_none(self):
        self.assertIsNone(A.decode_attribution(None, 9.8, 3.1))


class TestConcurrencyCeiling(unittest.TestCase):
    def test_fixed_per_sequence_state_can_dominate(self):
        """The finding that a pure-KV sizing rule would have missed entirely: a hybrid model's
        fixed recurrent state caps concurrency independently of context length."""
        pool = 7.22 * A.GIB
        got = A.concurrency_ceiling(pool, kv_bytes_per_token_per_shard=32768,
                                    context_tokens=1024,
                                    fixed_state_bytes_per_sequence=72 * 1024 * 1024)
        self.assertEqual(got["dominant_term"], "fixed per-sequence state")
        self.assertFalse(got["context_sensitive"])

    def test_pure_kv_case_is_context_sensitive(self):
        got = A.concurrency_ceiling(7.22 * A.GIB, 32768, 8192, 0)
        self.assertEqual(got["dominant_term"], "context-proportional KV")
        self.assertTrue(got["context_sensitive"])
        # halving context must double capacity when there is no fixed term
        half = A.concurrency_ceiling(7.22 * A.GIB, 32768, 4096, 0)
        self.assertAlmostEqual(half["sequences"], got["sequences"] * 2, places=6)


class TestRidgeAndRoofs(unittest.TestCase):
    def test_ridge_point_definition(self):
        r = A.ridge_point(615.24, 1541.34)
        self.assertAlmostEqual(r["value"], 615.24e12 / 1541.34e9, places=6)

    def test_no_hardware_constants_are_assumed(self):
        """Rule 1 of the module. Every one of these must decline rather than substitute a default;
        an earlier version of the attribution carried one machine's weight size and layer count and
        produced confident wrong answers everywhere else."""
        self.assertIsNone(A.ridge_point(None, 1541.0))
        self.assertIsNone(A.decode_floor(None, 1541.0))
        self.assertIsNone(A.decode_floor(1e10, None))
        self.assertIsNone(A.prefill_compute_ceiling(None, 615.0, 2))
        self.assertIsNone(A.concurrency_ceiling(None, 1, 1, 0))
        self.assertIsNone(A.achieved_fraction(10.0, None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
