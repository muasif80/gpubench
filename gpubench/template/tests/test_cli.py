"""Tests for `gpubench template`: the subcommand that made the template reachable at all.

WHAT IS BEING PROVEN. The template package held the section outline, the run-bundle schema and
the eleven lint rules, and nothing outside it could reach any of them. The subcommand is the fix,
so the properties under test are the ones that decide whether the fix is real:

    THE SCAFFOLD BUILDS AND PASSES THE GATE OUT OF THE BOX. Proven end to end, in one sequence:
    init into a temp directory, run the real `gpubench article` over it, and read the exit code,
    the file on disk and the manifest it wrote. A scaffold that fails the tool's own gate teaches
    the first-time author that the gate is noise.

    THE SCAFFOLD'S GATE IS LOAD-BEARING, not decorative. The negative control breaks the derived
    total in the generated module and asserts the build is BLOCKED and writes nothing. Without it,
    "the scaffold passes" would be consistent with a gate that passes everything.

    THE CLAIM KIND FOLLOWS THE ARTEFACT. The same generated code declares supplied claims over the
    synthetic sample and measured claims over a real run, decided by what the run file says rather
    than by what the author would prefer.

    THE VALIDATOR NEVER REPORTS A VERDICT IT DID NOT EARN. A schema using a keyword the
    standard-library validator does not implement is refused outright, because a partial pass
    reads exactly like a real one.

Every assertion reads an artefact: an exit code, a file's bytes, the parsed manifest, the printed
output. None reads a declaration about any of those.

Run:  python -m tests.test_template      (from the repo root)
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
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from gpubench import cli  # noqa: E402
from gpubench.longform import DRAFT_MARKER  # noqa: E402
from gpubench.template import outline as outline_reader  # noqa: E402
from gpubench.template import scaffold as scaffold_mod  # noqa: E402
from gpubench.template import schema as schema_mod  # noqa: E402

FIXTURES = os.path.join(HERE, "fixtures")


def run_cli(*argv):
    """Run the real command line in-process. Returns (exit_code, combined_output)."""
    buf = io.StringIO()
    with redirect_stdout(buf), redirect_stderr(buf):
        try:
            rc = cli.main(list(argv))
        except SystemExit as exc:  # argparse exits on a usage error
            rc = exc.code if isinstance(exc.code, int) else 2
    return rc, buf.getvalue()


class ScaffoldCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gpubench-template-test-")
        self.project = os.path.join(self.tmp, "demo")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def init(self, *flags):
        return run_cli("template", "init", self.project, *flags)

    def build(self, out_dir=None, *flags):
        out_dir = out_dir or os.path.join(self.project, "out")
        return run_cli("article", os.path.join(self.project, "content.py"),
                       os.path.join(self.project, "run"), "--out-dir", out_dir, *flags)

    def manifest(self, out_dir=None):
        path = os.path.join(out_dir or os.path.join(self.project, "out"), "claims.json")
        with io.open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def document(self, out_dir=None):
        path = os.path.join(out_dir or os.path.join(self.project, "out"), "demo-v0.1.html")
        with io.open(path, "r", encoding="utf-8") as fh:
            return fh.read()


class InitBuildsAndPasses(ScaffoldCase):
    """The hard requirement, as one sequence: init, build, gate, exit 0."""

    def test_init_then_build_passes_the_gate_and_exits_zero(self):
        rc, out = self.init()
        self.assertEqual(rc, 0, out)
        for name in ("content.py", "README.md", os.path.join("run", "sample-run.json")):
            self.assertTrue(os.path.isfile(os.path.join(self.project, name)),
                            "init did not write %s" % name)

        rc, out = self.build()
        self.assertEqual(rc, 0, out)
        self.assertIn("manifest verified", out)

        # The exit code is not the artefact. Read the document and the manifest it was judged
        # against, because a gate that passed and a report that never reached disk look the same
        # from a return value alone.
        html = self.document()
        self.assertNotIn(DRAFT_MARKER, html,
                         "a scaffold that passes the gate must not carry the draft stamp")
        manifest = self.manifest()
        self.assertEqual(manifest["schema"], "claims/1")
        self.assertGreaterEqual(len(manifest["claims"]), 4)

        # Every claim value the manifest declares is actually printed in the document. This is the
        # direction that catches a manifest describing a document nobody rendered.
        for claim_id, claim in manifest["claims"].items():
            printed = "%.1f %s" % (claim["value"], claim["unit"])
            self.assertIn(printed, html, "claim %s is declared but not printed" % claim_id)

    def test_the_derived_total_is_recomputed_and_agrees_with_its_parts(self):
        self.init()
        self.build()
        claims = self.manifest()["claims"]
        total = claims["compute_total"]
        parts = [claims[p.strip()]["value"] for p in total["formula"].split("+")]
        self.assertAlmostEqual(total["value"], sum(parts), places=6)

    def test_a_broken_total_blocks_the_build_and_writes_nothing(self):
        """The negative control. Without it, 'the scaffold passes' proves nothing about the gate."""
        self.init()
        path = os.path.join(self.project, "content.py")
        with io.open(path, "r", encoding="utf-8") as fh:
            src = fh.read()
        # The defect class exactly: a derived value carried as a typed constant beside a formula
        # that computes something else. A one-unit nudge would be a weaker control, because it
        # would also pass if the gate merely tolerated rounding.
        broken = src.replace(
            'values[total_id] = sum(values[r["id"]] for r in EXTRACT'
            ' if r.get("sum_into") == total_id)',
            'values[total_id] = 300.0')
        self.assertNotEqual(broken, src, "the total's arithmetic moved; update this control")
        with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(broken)

        out_dir = os.path.join(self.project, "blocked")
        rc, out = self.build(out_dir)
        self.assertEqual(rc, 1, out)
        self.assertIn("RENDER BLOCKED", out)
        self.assertFalse(os.path.exists(os.path.join(out_dir, "demo-v0.1.html")),
                         "a blocked build must not leave a report on disk")

    def test_kind_follows_the_artefact_not_the_author(self):
        self.init()
        self.build()
        supplied = self.manifest()["claims"]
        for claim_id, claim in supplied.items():
            if claim["kind"] == "derived":
                continue
            self.assertEqual(claim["kind"], "supplied", claim_id)
            self.assertTrue(claim.get("source"), "a supplied claim owes a source: %s" % claim_id)

        # Same generated code, a run artefact that no longer says it is synthetic.
        run_path = os.path.join(self.project, "run", "sample-run.json")
        with io.open(run_path, "r", encoding="utf-8") as fh:
            run = json.load(fh)
        run.pop("sample")
        run.pop("note")
        with io.open(run_path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(run, fh, indent=2)

        out_dir = os.path.join(self.project, "real")
        rc, out = self.build(out_dir)
        self.assertEqual(rc, 0, out)
        manifest = self.manifest(out_dir)
        for claim_id, claim in manifest["claims"].items():
            if claim["kind"] == "derived":
                continue
            self.assertEqual(claim["kind"], "measured", claim_id)
            self.assertIn(claim["run"], manifest["runs"],
                          "a measured claim must name a run in the run table: %s" % claim_id)

    def test_the_quality_gate_is_read_out_of_the_artefact(self):
        """G3's own property, at the scaffold level: degrade the artefact, and the build blocks."""
        self.init()
        run_path = os.path.join(self.project, "run", "sample-run.json")
        with io.open(run_path, "r", encoding="utf-8") as fh:
            run = json.load(fh)
        run["probes"]["accuracy"]["summary"]["exact_match_pct"] = 40.0
        with io.open(run_path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(run, fh, indent=2)

        out_dir = os.path.join(self.project, "failed-gate")
        rc, out = self.build(out_dir)
        self.assertEqual(rc, 1, out)
        self.assertIn("quality gate did not pass", out)

    def test_init_refuses_to_overwrite_and_force_says_so(self):
        self.init()
        path = os.path.join(self.project, "content.py")
        with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("# edited by hand\n")

        rc, out = self.init()
        self.assertEqual(rc, 2, out)
        with io.open(path, "r", encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "# edited by hand\n",
                             "a refused init must leave the file exactly as it was")

        rc, out = self.init("--force")
        self.assertEqual(rc, 0, out)
        with io.open(path, "r", encoding="utf-8") as fh:
            self.assertIn("SECTION_ORDER", fh.read())

    def test_the_generated_module_is_plain_ascii_with_unix_line_endings(self):
        self.init()
        for name in ("content.py", "README.md"):
            with io.open(os.path.join(self.project, name), "rb") as fh:
                raw = fh.read()
            self.assertNotIn(b"\r", raw, "%s must be LF only" % name)
            # decode("ascii") raises on any non-ASCII byte, which is the assertion: no em-dash,
            # no smart quote, nothing that breaks on a Windows console.
            text = raw.decode("ascii")
            self.assertNotIn(" -- ", text,
                             "%s uses a double hyphen as punctuation" % name)

    def test_every_required_outline_section_is_scaffolded(self):
        self.init()
        with io.open(os.path.join(self.project, "content.py"), "r", encoding="utf-8") as fh:
            src = fh.read()
        doc = outline_reader.load_outline()
        required = [s for s in doc["sections"] if s.get("required")]
        for entry in required:
            self.assertIn('"%s"' % entry["id"], src,
                          "required section %s is missing from the scaffold" % entry["id"])
        optional = [s for s in doc["sections"] if not s.get("required")]
        for entry in optional:
            self.assertNotIn('"%s"' % entry["id"], src,
                             "an optional section reached the default scaffold: %s" % entry["id"])

    def test_all_sections_adds_the_optional_ones(self):
        rc, out = self.init("--sections", "all")
        self.assertEqual(rc, 0, out)
        with io.open(os.path.join(self.project, "content.py"), "r", encoding="utf-8") as fh:
            src = fh.read()
        doc = outline_reader.load_outline()
        for entry in doc["sections"]:
            self.assertIn('"%s"' % entry["id"], src)


class OutlineCommand(unittest.TestCase):
    def test_it_prints_every_section_the_manifest_holds(self):
        rc, out = run_cli("template", "outline")
        self.assertEqual(rc, 0, out)
        doc = outline_reader.load_outline()
        for entry in doc["sections"]:
            self.assertIn(str(entry["id"]), out)
        self.assertIn("%d invariant(s) in total" % len(outline_reader.invariants(doc)), out)

    def test_one_section_prints_its_invariants_and_its_anti_pattern(self):
        rc, out = run_cli("template", "outline", "--section", "headline")
        self.assertEqual(rc, 0, out)
        entry = outline_reader.sections_by_id(outline_reader.load_outline())["headline"]
        for inv in entry["invariants"]:
            self.assertIn(inv["id"], out)
        self.assertIn("ANTI-PATTERN", out)

    def test_an_unknown_section_fails_and_suggests(self):
        rc, out = run_cli("template", "outline", "--section", "head")
        self.assertEqual(rc, 2, out)
        self.assertIn("headline", out)

    def test_invariants_prints_every_one(self):
        rc, out = run_cli("template", "outline", "--invariants")
        self.assertEqual(rc, 0, out)
        rows = outline_reader.invariants(outline_reader.load_outline())
        self.assertIn("%d invariant(s)" % len(rows), out)
        for row in rows[:20]:
            self.assertIn(str(row["id"]), out)

    def test_json_output_round_trips(self):
        rc, out = run_cli("template", "outline", "--json")
        self.assertEqual(rc, 0, out)
        doc = json.loads(out)
        self.assertEqual(len(doc["sections"]), len(outline_reader.load_outline()["sections"]))


class SchemaCommand(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gpubench-schema-test-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, name, doc):
        path = os.path.join(self.tmp, name)
        with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(doc, indent=2))
        return path

    def test_the_shipped_schema_is_fully_enforced(self):
        """If run-schema.json grows a keyword, this validator must grow with it, loudly."""
        gaps = schema_mod.unsupported_keywords(schema_mod.load_schema())
        self.assertEqual(gaps, [], "run-schema.json uses keywords the validator cannot enforce")

    def test_a_schema_it_cannot_enforce_is_refused_rather_than_partly_applied(self):
        path = self.write("partial-schema.json", {
            "type": "object",
            "properties": {"a": {"type": "string"}},
            "dependentRequired": {"a": ["b"]},
        })
        instance = self.write("instance.json", {"a": "x"})
        rc, out = run_cli("template", "schema", "--schema", path, "--validate", instance)
        self.assertEqual(rc, 3, out)
        self.assertIn("dependentRequired", out)
        self.assertIn("nothing was validated", out)

    def test_it_reports_a_missing_required_property_in_a_real_bundle(self):
        path = self.write("bundle.json", {"schema_version": "1.0"})
        rc, out = run_cli("template", "schema", "--validate", path)
        self.assertEqual(rc, 2, out)
        self.assertIn("required property 'runs' is missing", out)

    def test_a_valid_instance_exits_zero(self):
        schema = self.write("small.json", {
            "type": "object",
            "required": ["n"],
            "properties": {"n": {"type": "integer", "minimum": 1}},
            "additionalProperties": False,
        })
        good = self.write("good.json", {"n": 3})
        rc, out = run_cli("template", "schema", "--schema", schema, "--validate", good)
        self.assertEqual(rc, 0, out)
        self.assertIn("0 violation(s)", out)

        bad = self.write("bad.json", {"n": 0, "extra": 1})
        rc, out = run_cli("template", "schema", "--schema", schema, "--validate", bad)
        self.assertEqual(rc, 2, out)
        self.assertIn("at least 1", out)
        self.assertIn("not allowed here", out)

    def test_contains_counts_rather_than_merely_looking(self):
        """minContains/maxContains are the assertion, not decoration on `contains`."""
        schema = {"type": "array", "contains": {"const": "primary"},
                  "minContains": 1, "maxContains": 1}
        self.assertEqual(schema_mod.validate(["primary", "other"], schema), [])
        self.assertTrue(schema_mod.validate(["other"], schema))
        self.assertTrue(schema_mod.validate(["primary", "primary"], schema))

    def test_property_names_are_checked(self):
        schema = {"type": "object", "propertyNames": {"pattern": "^[a-z_]+$"}}
        self.assertEqual(schema_mod.validate({"ok_name": 1}, schema), [])
        self.assertTrue(schema_mod.validate({"BadName": 1}, schema))

    def test_booleans_are_not_numbers(self):
        self.assertTrue(schema_mod.validate(True, {"type": "integer"}))
        self.assertEqual(schema_mod.validate(True, {"type": "boolean"}), [])

    def test_the_summary_names_the_required_top_level_properties(self):
        rc, out = run_cli("template", "schema")
        self.assertEqual(rc, 0, out)
        for name in schema_mod.load_schema().get("required") or []:
            self.assertIn(name, out)


class LintCommand(unittest.TestCase):
    def clean(self, name="clean"):
        return os.path.join(FIXTURES, name), os.path.join(FIXTURES, name, "report.html")

    def test_the_clean_fixture_passes_through_the_subcommand(self):
        run_dir, report = self.clean()
        rc, out = run_cli("template", "lint", run_dir, report)
        self.assertEqual(rc, 0, out)
        self.assertIn("0 error(s)", out)

    def test_a_defect_fixture_fails_through_the_subcommand(self):
        run_dir, report = self.clean("d1_three_values")
        rc, out = run_cli("template", "lint", run_dir, report)
        self.assertEqual(rc, 2, out)
        self.assertIn("L1", out)

    def test_a_bundle_without_its_report_is_refused_rather_than_passed(self):
        run_dir, _ = self.clean()
        rc, out = run_cli("template", "lint", run_dir)
        self.assertEqual(rc, 3, out)
        self.assertIn("has nothing to be wrong about", out)

    def test_explain_needs_nothing_and_names_every_rule(self):
        rc, out = run_cli("template", "lint", "--explain")
        self.assertEqual(rc, 0, out)
        from gpubench.template import lint as lint_mod
        for rule in lint_mod.RULE_ORDER:
            self.assertIn(rule, out)

    def test_a_rule_subset_is_passed_through(self):
        run_dir, report = self.clean()
        rc, out = run_cli("template", "lint", run_dir, report, "--rules", "L1")
        self.assertEqual(rc, 0, out)
        self.assertIn("lint: rules L1", out)


class CommandSurface(unittest.TestCase):
    """The subcommands that existed before this one still exist, and are still reachable."""

    EXISTING = ("inspect", "run", "report", "verify", "experiment", "article", "index")

    def test_help_still_lists_every_pre_existing_subcommand(self):
        rc, out = run_cli("--help")
        self.assertEqual(rc, 0, out)
        for name in self.EXISTING + ("template",):
            self.assertIn(name, out)

    def test_every_pre_existing_subcommand_still_parses(self):
        # --help on each: it exercises the parser that `template` was added beside, without
        # running anything against a machine.
        for name in self.EXISTING:
            with self.subTest(subcommand=name):
                rc, out = run_cli(name, "--help")
                self.assertEqual(rc, 0, out)

    def test_template_help_lists_all_four(self):
        rc, out = run_cli("template", "--help")
        self.assertEqual(rc, 0, out)
        for name in ("init", "lint", "outline", "schema"):
            self.assertIn(name, out)

    def test_the_scaffold_module_is_importable_without_the_cli(self):
        # The package must be usable as a library too: the CLI is a surface over it, not the
        # only way in.
        self.assertTrue(callable(scaffold_mod.init))
        rows = scaffold_mod.sections_for(outline_reader.load_outline())
        self.assertTrue(all(len(row) == 4 for row in rows))


if __name__ == "__main__":
    unittest.main(verbosity=2)
