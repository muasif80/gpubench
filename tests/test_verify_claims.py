"""Unit tests for the raw-artefact claims verifier.

This tool had no unit tests at all, only its own `--selftest` fixture, and it shipped a false
UNGROUNDED: a kernel-trace claim printed in milliseconds could not be matched to the artefact that
held it in nanoseconds, because the unit table knew `s to ms` and not `ns to ms`. A verifier that
cries wolf is worse than one that is merely incomplete, because the operator learns to wave its
output through, and this one is the last check before a number reaches a reader.

The scale search is deliberately narrow: it only tries factors listed for the claim's OWN declared
unit. The negative tests below are the important half, since a verifier that will multiply by
anything until something matches grounds every claim and verifies nothing.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.verify_claims import UNIT_FACTORS, ground  # noqa: E402


def index_with(*values, **kw):
    """A one-run artefact index holding the given raw numbers."""
    run = kw.get("run", "r1")
    return {run: {"numbers": [("artefact.json", "/probes/x/%d" % i, v)
                              for i, v in enumerate(values)],
                  "strings": [], "siblings": []}}


def claim(value, unit, run="r1"):
    return {"value": value, "unit": unit, "run": run}


class TimeUnitsGround(unittest.TestCase):
    def test_a_millisecond_claim_grounds_against_nanoseconds(self):
        """The regression: a profiler writes ns, the document prints ms, and both are the same fact."""
        value, tier, evidence = ground(claim(8154.263245, "ms"), index_with(8154263245.0))
        self.assertEqual(tier, "unit", evidence)
        self.assertAlmostEqual(value, 8154.263245, places=6)
        self.assertIn("ns to ms", evidence)

    def test_a_millisecond_claim_still_grounds_against_seconds(self):
        value, tier, evidence = ground(claim(250.0, "ms"), index_with(0.25))
        self.assertEqual(tier, "unit", evidence)
        self.assertIn("s to ms", evidence)

    def test_a_second_claim_grounds_against_nanoseconds(self):
        value, tier, evidence = ground(claim(1.5, "s"), index_with(1.5e9))
        self.assertEqual(tier, "unit", evidence)
        self.assertIn("ns to s", evidence)

    def test_microseconds_ground_from_nanoseconds(self):
        value, tier, evidence = ground(claim(2500.0, "us"), index_with(2500000.0))
        self.assertEqual(tier, "unit", evidence)
        self.assertIn("ns to us", evidence)


class TheScaleSearchStaysNarrow(unittest.TestCase):
    """Negative controls. Grounding by scale is only safe while it cannot reach for any factor."""

    def test_an_unrelated_number_does_not_ground(self):
        value, tier, _ = ground(claim(8154.263245, "ms"), index_with(42.0, 1234.5))
        self.assertIsNone(value)
        self.assertEqual(tier, "ungrounded")

    def test_a_factor_from_another_unit_family_is_not_tried(self):
        """1 GiB of bytes must not ground a claim that calls itself milliseconds."""
        value, tier, _ = ground(claim(1.0, "ms"), index_with(1073741824.0))
        self.assertIsNone(value)
        self.assertEqual(tier, "ungrounded")

    def test_an_unknown_unit_gets_no_scale_search_at_all(self):
        value, tier, _ = ground(claim(5.0, "widgets"), index_with(5000.0))
        self.assertIsNone(value)
        self.assertEqual(tier, "ungrounded")

    def test_a_claim_naming_a_run_with_no_artefacts_says_so(self):
        value, tier, evidence = ground(claim(1.0, "ms", run="absent"), index_with(1.0))
        self.assertIsNone(value)
        self.assertEqual(tier, "no_such_run", evidence)


class TheUnitTableIsSelfConsistent(unittest.TestCase):
    def test_every_factor_is_a_positive_number_with_a_label(self):
        for unit, entries in UNIT_FACTORS.items():
            for factor, label in entries:
                self.assertGreater(factor, 0.0, "%s carries a non-positive factor" % unit)
                self.assertTrue(label.strip(), "%s carries a factor with no label" % unit)

    def test_an_exact_match_beats_the_scale_search(self):
        """A literal hit must win, so evidence names the artefact rather than a conversion."""
        value, tier, evidence = ground(claim(1000.0, "ms"), index_with(1000.0, 1.0))
        self.assertEqual(tier, "exact", evidence)
        self.assertEqual(value, 1000.0)


if __name__ == "__main__":
    unittest.main()
