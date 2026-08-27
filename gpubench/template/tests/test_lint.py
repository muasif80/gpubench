"""One test per rule, plus the fixtures that reproduce real historical defects.

Every rule has at least one test that FAILS if the rule is removed (constraint B-C4). Two
mechanisms enforce that, and they are independent:

  * ``test_every_rule_fires_on_its_own_fixture`` asserts each rule produces at least one error on
    the fixture built for it. Deleting the rule from the registry makes ``select_rules`` refuse the
    name, so the test errors out; emptying the rule's body makes the assertion fail.
  * ``test_every_rule_test_is_load_bearing`` stubs each rule out, one at a time, and asserts the
    findings for it disappear. That proves the assertion above is actually carried by the rule and
    not by some other rule that happens to fire on the same fixture.

Three fixtures reproduce defects from the real review rounds, and the tests assert the reported
message names the same quantities the defect did:

  d1_three_values                one rate printed as 82 in a table, 80 in prose, 83 in a
                                 recommendation (D1)
  d5_unreproducible_derivation   a ceiling published as 7710.0 whose own formula gives 6564.8,
                                 because the step above it interpolated latency on a logarithmic
                                 reading of a linear axis (D5), beside a derivation carrying one
                                 machine's geometry as a literal constant (D11)
  d10_undeclared_run             a value whose run_id is absent from runs[], under a cover that
                                 claims a single run directory (D10, D3)
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
PACKAGE_PARENT = os.path.dirname(os.path.dirname(HERE))
if PACKAGE_PARENT not in sys.path:
    sys.path.insert(0, PACKAGE_PARENT)

# Two import paths, one module object. Loaded as `gpubench.template.tests.test_lint`, which is
# what `python -m tests.test_template` does from the repo root, the package import wins; loaded as
# `template.tests.test_lint` from inside the package's parent directory, the flat one does.
# Importing the flat name unconditionally would hand this file a SECOND copy of lint.py, and the
# tests below stub rules out on the module object: patching one copy while the command line runs
# the other is a test that proves nothing.
try:
    from gpubench.template import lint as linter  # noqa: E402
    from gpubench.template import outline as outline_reader  # noqa: E402
except ImportError:  # pragma: no cover - running from the directory that holds `template`
    from template import lint as linter  # noqa: E402
    from template import outline as outline_reader  # noqa: E402

FIXTURES = os.path.join(HERE, "fixtures")

# Which fixture each rule is proven against.
RULE_FIXTURES = {
    "L1": "d1_three_values",
    "L2": "mixed_defects",
    "L3": "d5_unreproducible_derivation",
    "L4": "mixed_defects",
    "L5": "mixed_defects",
    "L6": "mixed_defects",
    "L7": "d10_undeclared_run",
    "L8": "mixed_defects",
    "L9": "mixed_defects",
    "L10": "d7_version_chain",
    "L11": "mixed_defects",
}


def fixture(name):
    return os.path.join(FIXTURES, name)


def report_of(name):
    return os.path.join(FIXTURES, name, "report.html")


def run_lint(name, rules=None, **overrides):
    args = linter._default_args()
    for key, value in overrides.items():
        setattr(args, key, value)
    return linter.lint(fixture(name), report_of(name), rules, args)


def errors(findings, rule=None):
    return [
        f
        for f in findings
        if f.severity == linter.SEV_ERROR and (rule is None or f.rule == rule)
    ]


def skips(findings, rule=None):
    return [
        f
        for f in findings
        if f.severity == linter.SEV_SKIPPED and (rule is None or f.rule == rule)
    ]


def text_of(findings):
    return "\n".join("%s | %s | %s | %s" % (f.rule, f.severity, f.location, f.message) for f in findings)


class RuleCoverageTests(unittest.TestCase):
    """The two mechanisms that make every rule's test load-bearing (B-C4)."""

    def test_every_rule_fires_on_its_own_fixture(self):
        for rule, name in sorted(RULE_FIXTURES.items()):
            with self.subTest(rule=rule, fixture=name):
                _, findings = run_lint(name, [rule])
                self.assertTrue(
                    errors(findings, rule),
                    "%s produced no error on fixture %s:\n%s" % (rule, name, text_of(findings)),
                )

    def test_every_rule_test_is_load_bearing(self):
        for rule, name in sorted(RULE_FIXTURES.items()):
            with self.subTest(rule=rule, fixture=name):
                original = linter.RULES[rule]
                linter.RULES[rule] = lambda ctx: []
                try:
                    _, findings = run_lint(name, [rule])
                finally:
                    linter.RULES[rule] = original
                self.assertEqual(
                    [],
                    errors(findings, rule),
                    "%s still reported findings with its rule stubbed out, so the test above is "
                    "not carried by the rule" % rule,
                )

    def test_every_rule_has_a_dedicated_test(self):
        methods = set()
        for klass in (
            L1OrphanLiteralTests,
            L2ValueKindTests,
            L3DerivationTests,
            L4AssumptionTests,
            L5CrossReferenceTests,
            L6FigureTableTests,
            L7RunProvenanceTests,
            L8ComparisonTests,
            L9EvidenceTests,
            L10VersionHistoryTests,
            L11GateTests,
        ):
            methods.update(name for name in dir(klass) if name.startswith("test_"))
        for rule in linter.RULE_ORDER:
            self.assertTrue(
                any(name.startswith("test_%s_" % rule.lower()) for name in methods),
                "no test named test_%s_* exists; a rule without a test is a rule that can be "
                "deleted silently" % rule.lower(),
            )

    def test_rule_registry_matches_the_documented_rule_set(self):
        self.assertEqual(sorted(linter.RULES), sorted(linter.RULE_ORDER))
        self.assertEqual(sorted(linter.RULE_META), sorted(linter.RULE_ORDER))
        self.assertEqual(11, len(linter.RULE_ORDER))
        for rule in linter.RULE_ORDER:
            self.assertTrue(linter.RULE_META[rule]["cites"], "%s cites no defect" % rule)
            self.assertIn("D", linter.RULE_META[rule]["cites"])


class CleanFixtureTests(unittest.TestCase):
    def test_the_clean_fixture_passes_every_rule_with_nothing_unchecked(self):
        ctx, findings = run_lint("clean")
        self.assertEqual([], errors(findings), text_of(findings))
        self.assertEqual([], skips(findings), text_of(findings))
        self.assertTrue(ctx.authored.present)

    def test_the_clean_fixture_exercises_the_allowlist_audit_trail(self):
        ctx, _ = run_lint("clean", ["L1"])
        entry_ids = {entry_id for _, entry_id in ctx.allowlisted_hits}
        self.assertIn(
            "A6b",
            entry_ids,
            "renderer-assigned heading numbers should be exempted by a named pattern, and the "
            "exemption should be printed in the audit trail",
        )


class L1OrphanLiteralTests(unittest.TestCase):
    def test_l1_flags_one_quantity_printed_three_ways(self):
        """D1: 82 in the table, 80 in the prose, 83 in the recommendation."""
        _, findings = run_lint("d1_three_values", ["L1"])
        found = errors(findings, "L1")
        messages = text_of(found)
        self.assertIn("literal 80 collides with value 'roof_fraction'", messages)
        self.assertIn("literal 83 collides with value 'roof_fraction'", messages)
        self.assertIn("more than one printed reading", messages)
        self.assertIn("{{v:roof_fraction}}", "\n".join(f.fix for f in found))
        self.assertTrue(
            any("authored offset" in f.location for f in found),
            "the authored copy of the drifted sentence must be flagged too",
        )
        self.assertTrue(
            any("prose offset" in f.location for f in found),
            "the rendered copy must be flagged too",
        )

    def test_l1_flags_a_per_unit_literal_where_the_sentence_quantifies_the_aggregate(self):
        """D2: 'every one of the 201 busy samples' over an aggregate of 401 across two units."""
        _, findings = run_lint("mixed_defects", ["L1"])
        scope = [f for f in errors(findings, "L1") if "Scope mismatch" in f.message]
        self.assertTrue(scope, text_of(findings))
        self.assertIn("per-unit", scope[0].message)
        self.assertIn("unit_count=2", scope[0].message)
        self.assertIn("(D2)", scope[0].message)

    def test_l1_does_not_flag_a_number_rendered_from_its_envelope(self):
        _, findings = run_lint("clean", ["L1"])
        self.assertEqual([], errors(findings, "L1"), text_of(findings))

    def test_l1_allowlist_file_can_exempt_a_named_shape_and_only_that_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = os.path.join(tmp, "d1")
            shutil.copytree(fixture("d1_three_values"), work)
            with open(os.path.join(work, "lint-allowlist.json"), "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "patterns": [
                            {
                                "id": "X1",
                                "regex": r"^80$",
                                "context": "any",
                                "why": "fixture: exempting exactly one shape, to show the "
                                "allowlist is a pattern list and not a tolerance",
                            }
                        ]
                    },
                    fh,
                )
            args = linter._default_args()
            _, findings = linter.lint(work, os.path.join(work, "report.html"), ["L1"], args)
            messages = text_of(errors(findings, "L1"))
            self.assertNotIn("literal 80 collides", messages)
            self.assertIn("literal 83 collides", messages)

    def test_l1_allowlist_entry_without_a_reason_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = os.path.join(tmp, "d1")
            shutil.copytree(fixture("d1_three_values"), work)
            with open(os.path.join(work, "lint-allowlist.json"), "w", encoding="utf-8") as fh:
                json.dump({"patterns": [{"id": "X2", "regex": "^80$", "context": "any"}]}, fh)
            with self.assertRaises(linter.LinterError) as caught:
                linter.lint(work, os.path.join(work, "report.html"), ["L1"])
            self.assertIn("why", str(caught.exception))

    def test_l1_reports_what_it_did_not_look_at(self):
        _, findings = run_lint("d10_undeclared_run", ["L1"])
        self.assertTrue(
            any("authored" in f.location for f in skips(findings, "L1")),
            "with no authored text supplied, L1 must say so rather than pass silently",
        )

    def test_l1_extracts_numbers_spelled_as_words(self):
        self.assertEqual(201, linter.parse_word_number("two hundred and one"))
        self.assertEqual(80, linter.parse_word_number("eighty"))
        self.assertEqual(2400000, linter.parse_word_number("two million four hundred thousand"))
        self.assertIsNone(linter.parse_word_number("and"))

    def test_l1_drift_band_is_the_wider_of_five_per_cent_and_two_last_places(self):
        # D1's 80 sits 2.4% from 82: a band of half a rounded digit would have caught neither
        # 80 nor 83, which is why the band is deliberately wide.
        self.assertAlmostEqual(4.1, linter.drift_band("80", 82.0), places=6)
        # Five per cent of a large value dominates the last-place floor.
        self.assertAlmostEqual(28.32, linter.drift_band("570.0", 566.4), places=6)
        # For a small value the last-place floor dominates, which is why a bare integer in prose
        # collides with anything nearby and why allowlist A9 reads "collides" relatively.
        self.assertAlmostEqual(0.2, linter.drift_band("566.4", 0.4), places=6)
        self.assertAlmostEqual(2.0, linter.drift_band("1", 0.004), places=6)


class L2ValueKindTests(unittest.TestCase):
    def test_l2_flags_a_table_cell_that_is_a_typed_number(self):
        _, findings = run_lint("mixed_defects", ["L2"])
        messages = text_of(errors(findings, "L2"))
        self.assertIn("renders literal '82' with no envelope reference", messages)

    def test_l2_flags_an_assumption_with_no_rationale(self):
        _, findings = run_lint("mixed_defects", ["L2"])
        messages = text_of(errors(findings, "L2"))
        self.assertIn("kind 'assumption' requires 'rationale'", messages)
        self.assertIn("kind 'fixed-test-set' requires 'cases'", messages)

    def test_l2_flags_a_number_stored_as_a_string_and_a_missing_precision(self):
        _, findings = run_lint("mixed_defects", ["L2"])
        messages = text_of(errors(findings, "L2"))
        self.assertIn("stored as the string '238.4'", messages)
        self.assertIn("numeric value with no precision", messages)

    def test_l2_accepts_an_axis_cell_that_names_the_series_point_it_indexes(self):
        _, findings = run_lint("clean", ["L2"])
        self.assertEqual([], errors(findings, "L2"), text_of(findings))

    def test_l2_flags_a_duplicated_id_across_the_value_arrays(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = os.path.join(tmp, "clean")
            shutil.copytree(fixture("clean"), work)
            path = os.path.join(work, "bundle.json")
            with open(path, encoding="utf-8") as fh:
                bundle = json.load(fh)
            clash = dict(bundle["measurements"][0])
            clash["id"] = "roof_compute"
            bundle["measurements"].append(clash)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(bundle, fh)
            _, findings = linter.lint(work, os.path.join(work, "report.html"), ["L2"])
            self.assertIn("declared in both", text_of(errors(findings, "L2")))


class L3DerivationTests(unittest.TestCase):
    def test_l3_flags_a_derived_value_that_does_not_reproduce_from_its_inputs(self):
        """D5: published as 7710.0, recomputes to 6564.8 from its own declared inputs."""
        _, findings = run_lint("d5_unreproducible_derivation", ["L3"])
        recompute = [f for f in errors(findings, "L3") if "does not recompute" in f.message]
        self.assertTrue(recompute, text_of(findings))
        message = recompute[0].message
        self.assertIn("interconnect_ceiling_large", recompute[0].location)
        self.assertIn("recomputed 6564.8", message)
        self.assertIn("stored 7710", message)
        self.assertIn("message_size / (latency_at_size - latency_intercept)", message)

    def test_l3_flags_an_interpolation_basis_that_contradicts_the_figure_axis(self):
        """D5's actual arithmetic error: latency interpolated log-linearly on a linear axis."""
        _, findings = run_lint("d5_unreproducible_derivation", ["L3"])
        basis = [f for f in errors(findings, "L3") if "interpolation basis" in f.message]
        self.assertTrue(basis, text_of(findings))
        self.assertIn("'log-linear'", basis[0].message)
        self.assertIn("scale 'linear'", basis[0].message)

    def test_l3_flags_a_hardcoded_machine_constant_in_a_formula(self):
        """D11: one machine's geometry as a literal, so elsewhere it answers confidently wrong."""
        _, findings = run_lint("d5_unreproducible_derivation", ["L3"])
        constants = [f for f in errors(findings, "L3") if "constant 27000000000" in f.message]
        self.assertTrue(constants, text_of(findings))
        self.assertIn("(D11)", constants[0].fix)

    def test_l3_flags_a_derivation_computed_outside_the_tested_harness(self):
        _, findings = run_lint("d5_unreproducible_derivation", ["L3"])
        messages = text_of(errors(findings, "L3"))
        self.assertIn("derivations_unit_tested", messages)

    def test_l3_flags_an_input_that_is_never_printed(self):
        _, findings = run_lint("d5_unreproducible_derivation", ["L3"])
        messages = text_of(errors(findings, "L3"))
        self.assertIn("which is never rendered in the document", messages)

    def test_l3_says_when_it_could_not_execute_a_derivation(self):
        _, findings = run_lint("d5_unreproducible_derivation", ["L3"])
        self.assertTrue(
            any("recomputation of" in f.location for f in skips(findings, "L3")),
            "a derivation over a series cannot be executed, and the linter must say so",
        )

    def test_l3_formula_executor_refuses_anything_that_is_not_arithmetic(self):
        with self.assertRaises(linter.FormulaError):
            linter.parse_formula("__import__('os').system('echo unsafe')")
        with self.assertRaises(linter.FormulaError):
            linter.parse_formula("a if b else c")
        tree, names, constants = linter.parse_formula("a / b * 100")
        self.assertEqual({"a", "b"}, names)
        self.assertEqual([100.0], constants)
        self.assertAlmostEqual(50.0, linter.eval_formula(tree, {"a": 1.0, "b": 2.0}))

    def test_l3_accepts_a_derivation_that_reproduces(self):
        _, findings = run_lint("clean", ["L3"])
        self.assertEqual([], errors(findings, "L3"), text_of(findings))


class L4AssumptionTests(unittest.TestCase):
    def test_l4_flags_an_assumption_that_loses_its_label_at_a_later_appearance(self):
        """D8: labelled once in the methodology, quoted bare afterwards."""
        _, findings = run_lint("mixed_defects", ["L4"])
        unlabelled = [
            f for f in errors(findings, "L4") if "rendered without its label" in f.message
        ]
        self.assertTrue(unlabelled, text_of(findings))
        self.assertIn("mixture_share_short", unlabelled[0].location)
        self.assertIn("Labelled appearances: 1", unlabelled[0].message)

    def test_l4_forbids_a_weighted_summary_of_an_assumed_mixture(self):
        _, findings = run_lint("mixed_defects", ["L4"])
        messages = text_of(errors(findings, "L4"))
        self.assertIn("weighted_summary_permitted is not true", messages)

    def test_l4_flags_a_derived_value_that_launders_an_assumption(self):
        _, findings = run_lint("mixed_defects", ["L4"])
        messages = text_of(errors(findings, "L4"))
        self.assertIn("derived from assumed input(s) mixture_share_short", messages)

    def test_l4_flags_a_floor_rendered_without_bound_wording(self):
        _, findings = run_lint("mixed_defects", ["L4"])
        messages = text_of(errors(findings, "L4"))
        self.assertIn("is a floor, or inherits one", messages)

    def test_l4_says_when_it_cannot_see_which_values_came_from_the_mixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = os.path.join(tmp, "mixed")
            shutil.copytree(fixture("mixed_defects"), work)
            path = os.path.join(work, "bundle.json")
            with open(path, encoding="utf-8") as fh:
                bundle = json.load(fh)
            for env in bundle["derived"]:
                env.pop("from_distribution", None)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(bundle, fh)
            _, findings = linter.lint(work, os.path.join(work, "report.html"), ["L4"])
            self.assertTrue(
                any("weighted summaries" in f.location for f in skips(findings, "L4")),
                "with nothing declaring from_distribution, the prohibition is unverifiable and "
                "the linter must say so instead of passing",
            )


class L5CrossReferenceTests(unittest.TestCase):
    def test_l5_flags_a_document_number_typed_into_prose(self):
        """D6: 'see section 18' after the sections were reordered."""
        _, findings = run_lint("mixed_defects", ["L5"])
        typed = [f for f in errors(findings, "L5") if "literal document reference" in f.message]
        self.assertTrue(typed, text_of(findings))
        self.assertIn("section 18", typed[0].message)
        self.assertIn("(D6)", typed[0].fix)

    def test_l5_flags_a_marker_that_does_not_resolve(self):
        _, findings = run_lint("mixed_defects", ["L5"])
        messages = text_of(errors(findings, "L5"))
        self.assertIn("{{sec:thermal_headroom}} does not resolve", messages)

    def test_l5_accepts_a_rendered_reference_that_carries_its_number_from_the_renderer(self):
        _, findings = run_lint("clean", ["L5"])
        self.assertEqual([], errors(findings, "L5"), text_of(findings))

    def test_l5_flags_a_declared_section_that_is_never_rendered(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = os.path.join(tmp, "clean")
            shutil.copytree(fixture("clean"), work)
            path = os.path.join(work, "bundle.json")
            with open(path, encoding="utf-8") as fh:
                bundle = json.load(fh)
            bundle["sections"].append(
                {
                    "id": "thermal_headroom",
                    "title": "Thermal headroom",
                    "purpose": "a section declared but never rendered",
                }
            )
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(bundle, fh)
            _, findings = linter.lint(work, os.path.join(work, "report.html"), ["L5"])
            self.assertIn("not rendered in the document", text_of(errors(findings, "L5")))


class L6FigureTableTests(unittest.TestCase):
    def test_l6_flags_a_figure_with_no_table_view(self):
        """D5's reader experience: a curve nobody can read numbers off."""
        _, findings = run_lint("mixed_defects", ["L6"])
        missing = [f for f in errors(findings, "L6") if "has no table_view" in f.message]
        self.assertTrue(missing, text_of(findings))
        self.assertIn("fig_no_table", missing[0].location)
        self.assertIn("(D5)", missing[0].fix)

    def test_l6_flags_a_table_view_of_typed_numbers(self):
        _, findings = run_lint("mixed_defects", ["L6"])
        literals = [f for f in errors(findings, "L6") if "with no value_id" in f.message]
        self.assertTrue(literals, text_of(findings))
        self.assertIn("two sources of truth", literals[0].fix)

    def test_l6_flags_a_reference_line_that_hides_a_floor(self):
        _, findings = run_lint("mixed_defects", ["L6"])
        messages = text_of(errors(findings, "L6"))
        self.assertIn("declares is_floor=False while the roof declares is_floor=True", messages)

    def test_l6_says_when_the_drawn_series_are_not_marked(self):
        _, findings = run_lint("mixed_defects", ["L6"])
        self.assertTrue(
            any("series-versus-table" in f.location for f in skips(findings, "L6")),
            "if the chart does not say what it drew, the linter must not claim it checked",
        )

    def test_l6_accepts_a_figure_whose_table_renders_the_same_envelopes(self):
        _, findings = run_lint("clean", ["L6"])
        self.assertEqual([], errors(findings, "L6"), text_of(findings))


class L7RunProvenanceTests(unittest.TestCase):
    def test_l7_flags_a_value_whose_run_is_not_declared(self):
        """D10/D3: a value carried over from an artefact nobody declared."""
        _, findings = run_lint("d10_undeclared_run", ["L7"])
        undeclared = [f for f in errors(findings, "L7") if "is not in runs[]" in f.message]
        self.assertTrue(undeclared, text_of(findings))
        self.assertIn("cap_busy_samples", undeclared[0].location)
        self.assertIn("run_20260812", undeclared[0].message)
        self.assertIn("(D3, D10)", undeclared[0].fix)

    def test_l7_flags_a_cover_that_claims_a_single_run(self):
        """D10: the cover claimed one run directory while three artefacts contributed."""
        _, findings = run_lint("d10_undeclared_run", ["L7"])
        single = [f for f in errors(findings, "L7") if "asserts" in f.message]
        self.assertTrue(single, text_of(findings))
        self.assertIn("3 run ids are referenced", single[0].message)
        self.assertIn("run_20260812", single[0].message)

    def test_l7_flags_a_register_that_does_not_match_the_declared_runs(self):
        _, findings = run_lint("d10_undeclared_run", ["L7"])
        messages = text_of(errors(findings, "L7"))
        self.assertIn("renders 1 rows for 2 declared runs", messages)

    def test_l7_flags_a_run_whose_produced_list_does_not_match_what_it_produced(self):
        _, findings = run_lint("d10_undeclared_run", ["L7"])
        messages = text_of(errors(findings, "L7"))
        self.assertIn("produced[] claims", messages)

    def test_l7_accepts_a_single_run_bundle_with_a_generated_register(self):
        _, findings = run_lint("clean", ["L7"])
        self.assertEqual([], errors(findings, "L7"), text_of(findings))


class L8ComparisonTests(unittest.TestCase):
    def test_l8_flags_a_cross_condition_comparison_presented_as_like_for_like(self):
        """D4: 238.4 set against 237.6, at different problem sizes in different runs."""
        _, findings = run_lint("mixed_defects", ["L8"])
        found = errors(findings, "L8")
        self.assertTrue(found, text_of(findings))
        message = found[0].message
        self.assertIn("burst_rate_high_precision", message)
        self.assertIn("burst_rate_reference", message)
        self.assertIn("problem_size (8192 vs 4096)", message)
        self.assertIn("comparability_fingerprint (a91c vs 4f02)", message)
        self.assertIn("{{xcmp:", found[0].fix)

    def test_l8_accepts_two_values_measured_under_the_same_conditions(self):
        _, findings = run_lint("clean", ["L8"])
        self.assertEqual([], errors(findings, "L8"), text_of(findings))

    def test_l8_honours_a_recorded_prohibition(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = os.path.join(tmp, "clean")
            shutil.copytree(fixture("clean"), work)
            path = os.path.join(work, "bundle.json")
            with open(path, encoding="utf-8") as fh:
                bundle = json.load(fh)
            for env in bundle["measurements"]:
                if env["id"] == "rate_primary":
                    env["not_comparable_with"] = [
                        {
                            "id": "roof_compute",
                            "why": "fixture: a prohibition recorded on the value outranks a "
                            "paragraph that wants to make the comparison anyway",
                        }
                    ]
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(bundle, fh)
            _, findings = linter.lint(work, os.path.join(work, "report.html"), ["L8"])
            messages = text_of(errors(findings, "L8"))
            self.assertIn("not_comparable_with", messages)

    def test_l8_verifies_a_comparable_with_claim_instead_of_trusting_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = os.path.join(tmp, "mixed")
            shutil.copytree(fixture("mixed_defects"), work)
            path = os.path.join(work, "bundle.json")
            with open(path, encoding="utf-8") as fh:
                bundle = json.load(fh)
            for env in bundle["measurements"]:
                if env["id"] == "burst_rate_high_precision":
                    env["comparable_with"] = ["burst_rate_reference"]
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(bundle, fh)
            _, findings = linter.lint(work, os.path.join(work, "report.html"), ["L8"])
            messages = text_of(errors(findings, "L8"))
            self.assertIn("claims comparable_with", messages)


class L9EvidenceTests(unittest.TestCase):
    def test_l9_flags_a_publication_claim_the_bundle_contradicts(self):
        """D9: one section claiming publication, the field saying otherwise."""
        _, findings = run_lint("mixed_defects", ["L9"])
        found = errors(findings, "L9")
        self.assertTrue(found, text_of(findings))
        self.assertIn("claim word 'published'", found[0].message)
        self.assertIn("tool.published = False", found[0].message)
        self.assertIn("not independently reproducible", found[0].fix)

    def test_l9_accepts_a_claim_that_carries_its_evidence(self):
        _, findings = run_lint("clean", ["L9"])
        self.assertEqual([], errors(findings, "L9"), text_of(findings))

    def test_l9_flags_an_evidence_reference_that_resolves_to_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = os.path.join(tmp, "clean")
            shutil.copytree(fixture("clean"), work)
            path = os.path.join(work, "report.html")
            with open(path, encoding="utf-8") as fh:
                html = fh.read()
            html = html.replace('data-evidence="tool.source_url"', 'data-evidence="tool.someday"')
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(html)
            _, findings = linter.lint(work, path, ["L9"])
            self.assertIn("does not resolve to a real target", text_of(errors(findings, "L9")))

    def test_l9_demands_a_third_party_for_a_claim_of_independence(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = os.path.join(tmp, "clean")
            shutil.copytree(fixture("clean"), work)
            path = os.path.join(work, "report.html")
            with open(path, encoding="utf-8") as fh:
                html = fh.read()
            html = html.replace(
                "The harness is published:",
                "The harness is published and these numbers have been independently reproduced:",
            )
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(html)
            _, findings = linter.lint(work, path, ["L9"])
            messages = text_of(errors(findings, "L9"))
            self.assertIn("claims independence", messages)


class L10VersionHistoryTests(unittest.TestCase):
    def test_l10_flags_a_history_missing_two_of_its_own_editions(self):
        """D7: the chain walk reaches six of eight entries."""
        _, findings = run_lint("d7_version_chain", ["L10"])
        walk = [f for f in errors(findings, "L10") if "chain walk" in f.message]
        self.assertTrue(walk, text_of(findings))
        self.assertIn("visits 6 of 8 entries", walk[0].message)
        self.assertIn("Unreachable: 7.1, 7.2", walk[0].message)

    def test_l10_flags_a_missing_entry_for_the_version_being_built(self):
        _, findings = run_lint("d7_version_chain", ["L10"])
        messages = text_of(errors(findings, "L10"))
        self.assertIn("no entry for the version being built (8.4", messages)

    def test_l10_flags_dates_that_go_backwards_along_the_chain(self):
        _, findings = run_lint("d7_version_chain", ["L10"])
        messages = text_of(errors(findings, "L10"))
        self.assertIn("before its predecessor", messages)

    def test_l10_flags_a_version_string_typed_into_prose(self):
        _, findings = run_lint("d7_version_chain", ["L10"])
        messages = text_of(errors(findings, "L10"))
        self.assertIn("typed version string '8.2' in prose", messages)

    def test_l10_checks_moved_values_against_the_previous_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = os.path.join(tmp, "clean")
            shutil.copytree(fixture("clean"), work)
            path = os.path.join(work, "bundle.json")
            with open(path, encoding="utf-8") as fh:
                bundle = json.load(fh)
            bundle["version_history"][0]["measured_values_moved"] = False
            bundle["version_history"][0].pop("moved_values", None)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(bundle, fh)
            _, findings = linter.lint(work, os.path.join(work, "report.html"), ["L10"])
            messages = text_of(errors(findings, "L10"))
            self.assertIn("declares measured_values_moved=False", messages)
            self.assertIn("237.6 -> 238.4", messages)

    def test_l10_says_when_it_has_no_previous_edition_to_diff_against(self):
        _, findings = run_lint("mixed_defects", ["L10"])
        self.assertTrue(
            any("value diff" in f.location for f in skips(findings, "L10")),
            "without the previous bundle the diff is unchecked, and the linter must say so",
        )

    def test_l10_accepts_a_complete_chain(self):
        _, findings = run_lint("clean", ["L10"])
        self.assertEqual([], errors(findings, "L10"), text_of(findings))


class L11GateTests(unittest.TestCase):
    def test_l11_flags_a_pass_count_with_no_published_cases(self):
        """D12: '10 of 10, PASS' with the cases unpublished, hence unfalsifiable."""
        _, findings = run_lint("mixed_defects", ["L11"])
        empty = [f for f in errors(findings, "L11") if "empty cases[]" in f.message]
        self.assertTrue(empty, text_of(findings))
        self.assertIn("unfalsifiable", empty[0].fix)
        counts = [f for f in errors(findings, "L11") if "renders '10 of 10'" in f.message]
        self.assertTrue(counts, text_of(findings))

    def test_l11_flags_a_cache_defeat_claim_with_no_counter_readings(self):
        _, findings = run_lint("mixed_defects", ["L11"])
        cache = [f for f in errors(findings, "L11") if "cache" in f.message]
        self.assertTrue(cache, text_of(findings))
        self.assertIn("cache_counters is absent", cache[0].message)
        self.assertIn("a counter reading is evidence", cache[0].fix)

    def test_l11_flags_a_licence_that_is_not_rendered_beside_the_result(self):
        _, findings = run_lint("mixed_defects", ["L11"])
        messages = text_of(errors(findings, "L11"))
        self.assertIn("licenses is not rendered next to the result", messages)
        self.assertIn("does_not_license is not rendered next to the result", messages)

    def test_l11_flags_one_size_check_generalised_across_sizes(self):
        _, findings = run_lint("mixed_defects", ["L11"])
        messages = text_of(errors(findings, "L11"))
        self.assertIn("no measured requested-versus-counted check for size(s) 8192", messages)

    def test_l11_flags_a_bar_described_as_pre_set_that_was_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = os.path.join(tmp, "clean")
            shutil.copytree(fixture("clean"), work)
            bundle_path = os.path.join(work, "bundle.json")
            with open(bundle_path, encoding="utf-8") as fh:
                bundle = json.load(fh)
            for env in bundle["derived"]:
                if env["id"] == "roof_fraction":
                    env["preregistered_bar"] = {
                        "comparator": ">=",
                        "threshold": 80,
                        "set_before_measurement": False,
                        "outcome": "met",
                    }
            with open(bundle_path, "w", encoding="utf-8") as fh:
                json.dump(bundle, fh)
            report_path = os.path.join(work, "report.html")
            with open(report_path, encoding="utf-8") as fh:
                html = fh.read()
            html = html.replace(
                "of that roof.",
                "of that roof, which clears the pre-set bar the team fixed in advance.",
            )
            with open(report_path, "w", encoding="utf-8") as fh:
                fh.write(html)
            _, findings = linter.lint(work, report_path, ["L11"])
            messages = text_of(errors(findings, "L11"))
            self.assertIn("set_before_measurement=False", messages)

    def test_l11_accepts_a_gate_whose_cases_are_published(self):
        _, findings = run_lint("clean", ["L11"])
        self.assertEqual([], errors(findings, "L11"), text_of(findings))


class HonestyTests(unittest.TestCase):
    """A linter that passes because it did not look is the defect class it exists to prevent."""

    def test_missing_authored_text_produces_skipped_findings_not_silence(self):
        _, findings = run_lint("no_authored")
        self.assertEqual([], errors(findings), text_of(findings))
        rules_skipped = {f.rule for f in skips(findings)}
        self.assertEqual({"L1", "L5", "L8"}, rules_skipped, text_of(findings))
        for finding in skips(findings):
            self.assertTrue(finding.message.startswith("NOT CHECKED: "))
            self.assertTrue(finding.fix, "a skip must say how to enable the check")

    def test_a_skipped_check_does_not_exit_zero_unless_asked(self):
        code = run_cli([fixture("no_authored"), report_of("no_authored")])
        self.assertEqual(linter.EXIT_SKIPPED, code)
        code = run_cli([fixture("no_authored"), report_of("no_authored"), "--allow-skipped"])
        self.assertEqual(linter.EXIT_OK, code)

    def test_every_finding_carries_a_rule_a_location_a_message_and_a_fix(self):
        for name in sorted(set(RULE_FIXTURES.values())):
            _, findings = run_lint(name)
            self.assertTrue(findings, "fixture %s produced no findings at all" % name)
            for finding in findings:
                self.assertIn(finding.rule, linter.RULE_ORDER)
                self.assertIn(finding.severity, (linter.SEV_ERROR, linter.SEV_SKIPPED))
                self.assertTrue(finding.location.strip(), text_of([finding]))
                self.assertTrue(finding.message.strip(), text_of([finding]))
                self.assertTrue(finding.fix.strip(), text_of([finding]))
                as_dict = finding.as_dict()
                self.assertEqual(
                    {"rule", "rule_slug", "severity", "location", "message", "fix"},
                    set(as_dict),
                )


def run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = linter.main(argv)
    run_cli.stdout = out.getvalue()
    run_cli.stderr = err.getvalue()
    return code


class CliTests(unittest.TestCase):
    def test_a_violation_exits_non_zero(self):
        code = run_cli([fixture("d1_three_values"), report_of("d1_three_values")])
        self.assertEqual(linter.EXIT_VIOLATION, code)
        self.assertIn("no-orphan-literals", run_cli.stdout)

    def test_a_clean_report_exits_zero(self):
        code = run_cli([fixture("clean"), report_of("clean")])
        self.assertEqual(linter.EXIT_OK, code)
        self.assertIn("0 error(s), 0 not-checked", run_cli.stdout)

    def test_rules_selects_a_subset(self):
        code = run_cli(
            [fixture("mixed_defects"), report_of("mixed_defects"), "--rules", "L1,L3"]
        )
        self.assertEqual(linter.EXIT_VIOLATION, code)
        self.assertIn("L1", run_cli.stdout)
        self.assertNotIn("L11 gates-measured-not-argued [error]", run_cli.stdout)

    def test_an_unknown_rule_is_refused_rather_than_ignored(self):
        code = run_cli([fixture("clean"), report_of("clean"), "--rules", "L99"])
        self.assertEqual(linter.EXIT_CANNOT_RUN, code)
        self.assertIn("unknown rule", run_cli.stderr)

    def test_explain_prints_every_rule_its_defect_and_the_allowlist(self):
        code = run_cli(["--explain"])
        self.assertEqual(linter.EXIT_OK, code)
        for rule in linter.RULE_ORDER:
            self.assertIn(rule + " " + linter.RULE_META[rule]["slug"], run_cli.stdout)
        for defect in ("D1", "D5", "D7", "D9", "D10", "D11", "D12"):
            self.assertIn(defect, run_cli.stdout)
        self.assertIn("A6b", run_cli.stdout)
        self.assertIn("false-positive modes", run_cli.stdout)
        self.assertIn("report-outline.yaml", run_cli.stdout)

    def test_json_output_is_machine_readable(self):
        code = run_cli(
            [fixture("d1_three_values"), report_of("d1_three_values"), "--rules", "L1", "--json"]
        )
        self.assertEqual(linter.EXIT_VIOLATION, code)
        payload = json.loads(run_cli.stdout)
        self.assertEqual(["L1"], payload["rules"])
        self.assertEqual(4, payload["errors"])  # two in the prose, two in the authored copy
        self.assertEqual("no-orphan-literals", payload["findings"][0]["rule_slug"])

    def test_missing_arguments_are_refused(self):
        self.assertEqual(linter.EXIT_CANNOT_RUN, run_cli([]))
        self.assertIn("required", run_cli.stderr)

    def test_a_directory_of_raw_probe_output_is_not_a_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("probe_a.json", "probe_b.json"):
                with open(os.path.join(tmp, name), "w", encoding="utf-8") as fh:
                    json.dump({"samples": [1, 2, 3]}, fh)
            with self.assertRaises(linter.LinterError) as caught:
                linter.find_bundle(tmp)
            self.assertIn("not a bundle", str(caught.exception))

    def test_a_bundle_missing_required_keys_cannot_be_linted(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "bundle.json"), "w", encoding="utf-8") as fh:
                json.dump({"schema_version": "1.0.0", "runs": []}, fh)
            with self.assertRaises(linter.LinterError) as caught:
                linter.build_context(
                    tmp, report_of("clean"), linter._default_args()
                )
            self.assertIn("missing required top-level keys", str(caught.exception))


class ReportParsingTests(unittest.TestCase):
    def test_table_cells_and_scripts_are_not_scanned_as_prose(self):
        html = (
            "<section data-section='s'><p>A rate of "
            "<span data-value-id='x'>238.4 units/s</span> was held.</p>"
            "<style>.a{width:82px}</style><script>var n=82;</script>"
            "<table><tr><td data-value-id='x'>238.4</td></tr></table>"
            "<svg><text>82</text></svg></section>"
        )
        report = linter.Report(html, "memory")
        self.assertIn("A rate of", report.prose)
        self.assertNotIn("82px", report.prose)
        self.assertNotIn("var n", report.prose)
        self.assertEqual(1, len(report.tables))
        self.assertEqual("238.4", report.tables[0]["rows"][0][0]["text_joined"])
        span = [s for s in report.value_spans if not s["in_table"]][0]
        self.assertEqual("238.4 units/s", span["text"])
        self.assertTrue(report.is_interpolated(span["start"], span["end"]))

    def test_a_rendered_value_span_is_exempt_but_the_sentence_around_it_is_not(self):
        html = (
            "<section data-section='s'><p>The roof is "
            "<span data-value-id='roof_compute'>290.0 units/s</span>, so the machine reached "
            "about 80 percent of it.</p></section>"
        )
        with tempfile.TemporaryDirectory() as tmp:
            work = os.path.join(tmp, "clean")
            shutil.copytree(fixture("clean"), work)
            path = os.path.join(work, "report.html")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(html)
            _, findings = linter.lint(work, path, ["L1"])
            messages = text_of(errors(findings, "L1"))
            self.assertIn("literal 80 collides", messages)
            self.assertNotIn("literal 290", messages)


class OutlineReaderTests(unittest.TestCase):
    def test_it_reads_the_shipped_manifest(self):
        doc = outline_reader.load_outline()
        self.assertEqual("1.0.0", doc["template"]["version"])
        self.assertIsNone(doc["template"]["previous_version"])
        self.assertEqual(29, len(doc["sections"]))
        self.assertEqual(7, len(doc["archetypes"]["items"]))
        self.assertEqual(160, len(outline_reader.invariants(doc)))
        first = doc["sections"][0]
        self.assertEqual("document-control", first["id"])
        self.assertIs(True, first["required"])
        self.assertEqual(1, first["order"])
        self.assertIn("runs", first["inputs"])
        self.assertEqual("DC-1", first["invariants"][0]["id"])
        self.assertEqual("D10", first["invariants"][0]["cites"])

    def test_every_declared_invariant_carries_a_check_and_a_defect(self):
        doc = outline_reader.load_outline()
        for inv in outline_reader.invariants(doc):
            self.assertTrue(inv.get("rule"), inv)
            self.assertTrue(inv.get("check"), inv)
            self.assertTrue(inv.get("cites"), inv)

    def test_sections_by_id_includes_the_archetypes(self):
        doc = outline_reader.load_outline()
        by_id = outline_reader.sections_by_id(doc)
        self.assertIn("document-control", by_id)
        self.assertIn("attribution-breakdown", by_id)

    def test_the_supported_subset(self):
        text = """
# a comment
top:
  name: "a quoted string with: a colon and a # hash"
  count: 12
  ratio: 1.5
  flag: true
  missing: null
  quoted_flag: "true"
  list:
    - "one"
    - "two"
  records:
    - id: "a"
      note: "first"
      nested:
        - "x"
        - "y"
    - id: "b"
      note: "second"
"""
        doc = outline_reader.loads(text)
        self.assertEqual("a quoted string with: a colon and a # hash", doc["top"]["name"])
        self.assertEqual(12, doc["top"]["count"])
        self.assertEqual(1.5, doc["top"]["ratio"])
        self.assertIs(True, doc["top"]["flag"])
        self.assertIsNone(doc["top"]["missing"])
        self.assertEqual("true", doc["top"]["quoted_flag"])
        self.assertEqual(["one", "two"], doc["top"]["list"])
        self.assertEqual(2, len(doc["top"]["records"]))
        self.assertEqual("a", doc["top"]["records"][0]["id"])
        self.assertEqual(["x", "y"], doc["top"]["records"][0]["nested"])
        self.assertEqual("second", doc["top"]["records"][1]["note"])

    def test_unsupported_yaml_is_refused_rather_than_mis_parsed(self):
        for text, what in (
            ("a: &anchor 1\nb: *anchor\n", "anchors"),
            ("a: |\n  block\n", "block scalars"),
            ("a: [1, 2]\n", "flow collections"),
            ("---\na: 1\n", "multi-document"),
        ):
            with self.subTest(what=what):
                with self.assertRaises(outline_reader.OutlineError):
                    outline_reader.loads(text)

    def test_an_unterminated_quote_is_refused(self):
        with self.assertRaises(outline_reader.OutlineError):
            outline_reader.loads('a: "unterminated\n')


if __name__ == "__main__":
    unittest.main(verbosity=2)
