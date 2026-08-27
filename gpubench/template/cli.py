"""`gpubench template`: the report template, reachable from the command line.

WHY THIS FILE EXISTS. The template package held the section outline, the run schema, the eleven
lint rules and the linter that enforces them, and nothing outside the package could reach any of
it. Roughly eleven thousand lines of reusable discipline sat inside the product as dead weight: no
subcommand, no import from anywhere else in ``gpubench``. Material nobody can run is material
nobody reviews, and material nobody reviews is where the next defect lives.

The four subcommands map onto the four artefacts:

    template init      report-outline.yaml  ->  a content module that builds and passes the gate
    template lint      lint.py              ->  the eleven rules, over a bundle and a built report
    template outline   report-outline.yaml  ->  the canonical sections and their invariants
    template schema    run-schema.json      ->  the bundle contract, printed or enforced

Exit codes are per subcommand and are documented in each parser's description, because the exit
code is the only thing a pipeline reads. ``lint`` passes the linter's own four codes straight
through rather than collapsing them, since 1 (something could not be checked) and 2 (a rule
failed) are different problems and only the linter can tell them apart.
"""

from __future__ import annotations

import json
import os
import sys

from . import outline as outline_reader
from . import scaffold as scaffold_mod
from . import schema as schema_mod

__all__ = ["add_arguments", "main"]

EXIT_OK = 0
EXIT_FAILED = 2      # the artefact was read and it is wrong
EXIT_CANNOT_RUN = 3  # the artefact could not be read, so nothing was judged


def add_arguments(parser):
    """Attach the template subcommands to an existing `template` parser."""
    sub = parser.add_subparsers(dest="template_cmd", required=True)

    p = sub.add_parser(
        "init",
        help="scaffold a report: content module, sample run, and an armed claims gate",
        description="Writes a directory that BUILDS AND PASSES THE CLAIMS GATE as it stands: a "
                    "content module whose sections come from the canonical outline, a synthetic "
                    "run artefact so the first build needs no hardware, and the MANIFEST/claims() "
                    "pair that arms the gate. Exit 0 written, 2 refused (something is already "
                    "there), 3 the outline could not be read.")
    p.add_argument("directory",
                   help="where to write the scaffold. Created if it does not exist.")
    p.add_argument("--title", default=None,
                   help="the report's title. Default: derived from the directory name.")
    p.add_argument("--basename", default=None,
                   help="output filename stem, so the build writes <basename>-v0.1.html. "
                        "Default: the directory name.")
    p.add_argument("--sections", default="required", choices=["required", "all"],
                   help="which outline sections to scaffold. 'required' (default) is the 22 the "
                        "outline mandates; 'all' adds the optional ones, which is more to delete "
                        "if you do not need them.")
    p.add_argument("--outline", default=None, metavar="PATH",
                   help="use a different section manifest instead of the tool's "
                        "report-outline.yaml. RISK: a scaffold built from a private outline "
                        "drifts from the canonical one, and nothing downstream will notice.")
    p.add_argument("--force", action="store_true",
                   help="overwrite files that are already there. RISK: this destroys an edited "
                        "content module, and a content module is prose nothing else holds a copy "
                        "of. Without it, init refuses rather than replaces.")

    p = sub.add_parser(
        "lint",
        help="run the eleven rules over a run bundle and the report built from it",
        description="The rules check the RENDERED DOCUMENT against the bundle behind it, because "
                    "that is where a hand-typed number becomes visible and nowhere earlier. Exit "
                    "0 everything checked and passed, 1 nothing failed but something could not be "
                    "checked, 2 a rule failed, 3 the linter could not run at all.")
    p.add_argument("run_dir", nargs="?",
                   help="directory holding bundle.json, and optionally authored.json and "
                        "lint-allowlist.json")
    p.add_argument("report", nargs="?", help="the rendered report (HTML)")
    p.add_argument("--rules", default=None,
                   help="comma-separated subset, for example L1,L3. RISK: a rule left out is a "
                        "rule that passed without running, and the summary line prints only the "
                        "rules you selected.")
    p.add_argument("--explain", action="store_true",
                   help="print each rule's statement and the historical defect it prevents. With "
                        "no run directory it prints and exits, changing nothing.")
    p.add_argument("--previous-bundle", default=None, metavar="PATH",
                   help="the previous edition's bundle. Enables L10's value diff: a number that "
                        "moved between editions with no version row.")
    p.add_argument("--version", dest="version", default=None,
                   help="the report version being built, for L10's version-chain check")
    p.add_argument("--allow-skipped", action="store_true",
                   help="exit 0 when nothing failed but something could not be checked. RISK: "
                        "'could not check' is how a linter passes without looking, so this turns "
                        "the one signal that says so into silence.")
    p.add_argument("--json", action="store_true", help="machine-readable findings on stdout")
    p.add_argument("--max-per-rule", type=int, default=40,
                   help="how many findings to print per rule. All are counted either way.")

    p = sub.add_parser(
        "outline",
        help="print the canonical section outline and its invariants",
        description="The outline fixes which sections a report has, what feeds each one, and what "
                    "must be true of each one AFTER rendering. Reads only; exit 0, or 3 if the "
                    "manifest cannot be read.")
    p.add_argument("--section", default=None, metavar="ID",
                   help="print one section in full: purpose, inputs, invariants and the "
                        "anti-pattern it exists to prevent")
    p.add_argument("--invariants", action="store_true",
                   help="print every invariant, universal and per-section, as one table")
    p.add_argument("--all", action="store_true",
                   help="include optional sections. Default prints them marked but terse.")
    p.add_argument("--json", action="store_true",
                   help="emit the parsed manifest as JSON, for a tool that wants to read it")
    p.add_argument("--outline", default=None, metavar="PATH",
                   help="read a different section manifest instead of the tool's own")

    p = sub.add_parser(
        "schema",
        help="print the run-bundle contract, or validate a file against it",
        description="run-schema.json says what a run bundle must contain and where every number "
                    "lives. With no arguments this prints a map of it; --validate enforces it. "
                    "Exit 0 valid, 2 invalid, 3 the schema or the file could not be read.")
    p.add_argument("--validate", default=None, metavar="FILE",
                   help="check a run bundle (or any JSON file) against the schema and print every "
                        "violation with its path")
    p.add_argument("--raw", action="store_true",
                   help="print the schema itself rather than a map of it. It is 88 KB; the map is "
                        "usually what you wanted.")
    p.add_argument("--json", action="store_true",
                   help="with --validate, emit the findings as JSON instead of text")
    p.add_argument("--schema", default=None, metavar="PATH",
                   help="use a different schema file. RISK: validating against a private schema "
                        "proves nothing about the contract other tools read.")
    return parser


def main(args):
    """Dispatch. `args` is the namespace argparse produced for the `template` subcommand."""
    handler = {"init": _init, "lint": _lint, "outline": _outline, "schema": _schema}
    return handler[args.template_cmd](args)


# --------------------------------------------------------------------------------------
# init
# --------------------------------------------------------------------------------------


def _init(args):
    try:
        result = scaffold_mod.init(
            args.directory, title=args.title, basename=args.basename,
            include_optional=(args.sections == "all"), outline_path=args.outline,
            force=args.force)
    except scaffold_mod.ScaffoldError as exc:
        print("template init: %s" % exc)
        return EXIT_FAILED if "already exists" in str(exc) else EXIT_CANNOT_RUN

    for path in result["paths"]:
        print("wrote %s" % path)
    print("")
    print("%d section(s) scaffolded from the canonical outline." % result["sections"])
    print("The run artefact is SYNTHETIC and marked as such, so this first edition declares its "
          "claims\nsupplied rather than measured. Point it at a real run and the same code "
          "declares them measured.")
    print("")
    print("Build it, and the claims gate will judge it:")
    print("")
    print("    gpubench article %s %s --out-dir %s"
          % (os.path.join(result["target"], "content.py"), result["run_dir"],
             os.path.join(result["target"], "out")))
    return EXIT_OK


# --------------------------------------------------------------------------------------
# lint
# --------------------------------------------------------------------------------------


def _lint(args):
    # Imported here, not at module scope: lint.py is the largest file in the package and no other
    # subcommand needs it, so `gpubench template outline` should not pay for it.
    from . import lint as lint_mod

    argv = []
    if args.rules:
        argv += ["--rules", args.rules]
    if args.explain:
        argv.append("--explain")
    if args.previous_bundle:
        argv += ["--previous-bundle", args.previous_bundle]
    if args.version:
        argv += ["--version", args.version]
    if args.allow_skipped:
        argv.append("--allow-skipped")
    if args.json:
        argv.append("--json")
    argv += ["--max-per-rule", str(args.max_per_rule)]
    if args.run_dir:
        argv.append(args.run_dir)
    if args.report:
        argv.append(args.report)
    if args.run_dir and not args.report and not args.explain:
        print("template lint: a run directory needs the report built from it. The rules compare "
              "the two;\n               a bundle on its own has nothing to be wrong about.")
        return lint_mod.EXIT_CANNOT_RUN
    return lint_mod.main(argv)


# --------------------------------------------------------------------------------------
# outline
# --------------------------------------------------------------------------------------


def _load_outline(path):
    try:
        return outline_reader.load_outline(path)
    except (outline_reader.OutlineError, IOError, OSError) as exc:
        print("template outline: %s" % exc)
        return None


def _outline(args):
    doc = _load_outline(args.outline)
    if doc is None:
        return EXIT_CANNOT_RUN

    if args.json:
        print(json.dumps(doc, indent=2, sort_keys=False))
        return EXIT_OK

    if args.section:
        return _outline_one(doc, args.section)

    if args.invariants:
        rows = outline_reader.invariants(doc)
        print("%d invariant(s). Each is a check on RENDERED OUTPUT, which is where a hand-typed "
              "number becomes visible." % len(rows))
        print("")
        print("%-24s %-8s %s" % ("OWNER", "ID", "RULE"))
        for row in rows:
            print("%-24s %-8s %s" % (row.get("owner"), row.get("id"),
                                     _oneline(row.get("rule"), 120)))
        return EXIT_OK

    meta = doc.get("template") or {}
    sections = sorted(doc.get("sections") or [], key=lambda s: s.get("order") or 0)
    archetypes = (doc.get("archetypes") or {}).get("items") or []
    print("%s %s  (%s)" % (meta.get("name"), meta.get("version"), meta.get("released")))
    print("schema contract: %s %s" % (meta.get("schema_contract"),
                                      meta.get("schema_contract_version")))
    print("")
    print("%-3s %-24s %-9s %-4s %s" % ("N", "ID", "REQUIRED", "INV", "TITLE"))
    for entry in sections:
        if not entry.get("required") and not args.all:
            pass  # still printed: an optional section that is invisible gets reinvented badly
        print("%-3s %-24s %-9s %-4d %s"
              % (entry.get("order"), entry.get("id"),
                 "required" if entry.get("required") else "optional",
                 len(entry.get("invariants") or []), entry.get("title")))
    print("")
    print("%d section(s), %d required. %d archetype(s) may be instantiated any number of times: %s"
          % (len(sections), sum(1 for s in sections if s.get("required")), len(archetypes),
             ", ".join(str(a.get("id")) for a in archetypes) or "none"))
    print("%d invariant(s) in total. `--section <id>` for one section, `--invariants` for all."
          % len(outline_reader.invariants(doc)))
    return EXIT_OK


def _outline_one(doc, section_id):
    entries = outline_reader.sections_by_id(doc)
    entry = entries.get(section_id)
    if entry is None:
        near = sorted(k for k in entries if section_id.lower() in str(k).lower())
        print("template outline: no section %r. %s"
              % (section_id, ("Did you mean: %s" % ", ".join(near)) if near
                 else "Run `gpubench template outline` for the list."))
        return EXIT_FAILED
    print("%s  %s" % (entry.get("id"), entry.get("title")))
    print("order %s, %s" % (entry.get("order"),
                            "required" if entry.get("required") else "optional"))
    print("")
    print("PURPOSE")
    print("  " + _oneline(entry.get("purpose"), 10000))
    if entry.get("inputs"):
        print("")
        print("INPUTS  (bundle fields this section is built from)")
        for item in entry["inputs"]:
            print("  %s" % item)
    invariants = entry.get("invariants") or []
    if invariants:
        print("")
        print("INVARIANTS  (%d, checked against the rendered document)" % len(invariants))
        for inv in invariants:
            print("  %-8s %s" % (inv.get("id"), _oneline(inv.get("rule"), 10000)))
            if inv.get("check"):
                print("           check: %s" % inv.get("check"))
            if inv.get("cites"):
                print("           cites: %s" % inv.get("cites"))
    if entry.get("anti_pattern"):
        print("")
        print("ANTI-PATTERN  (what this section exists to prevent)")
        print("  " + _oneline(entry.get("anti_pattern"), 10000))
    return EXIT_OK


def _oneline(text, width):
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= width else flat[: width - 3] + "..."


# --------------------------------------------------------------------------------------
# schema
# --------------------------------------------------------------------------------------


def _schema(args):
    try:
        doc = schema_mod.load_schema(args.schema)
    except schema_mod.SchemaError as exc:
        print("template schema: %s" % exc)
        return EXIT_CANNOT_RUN

    if not args.validate:
        if args.raw:
            print(json.dumps(doc, indent=2))
        else:
            print(schema_mod.summarise(doc))
        return EXIT_OK

    # A validator that cannot enforce every keyword must not return a verdict. Reporting "valid"
    # over a schema whose unimplemented half was skipped is the exact failure this template exists
    # to prevent: a claim that nothing is wrong, made without looking.
    gaps = schema_mod.unsupported_keywords(doc)
    if gaps:
        print("template schema: this schema uses %d keyword(s) the standard-library validator does "
              "not implement,\n                 so nothing was validated. A partial pass reads the "
              "same as a real one:" % len(gaps))
        for pointer, keyword in gaps[:20]:
            print("  %-8s at %s" % (keyword, pointer))
        if len(gaps) > 20:
            print("  ... and %d more" % (len(gaps) - 20))
        return EXIT_CANNOT_RUN

    path = args.validate
    try:
        with open(path, "r", encoding="utf-8") as fh:
            instance = json.load(fh)
    except (IOError, OSError) as exc:
        print("template schema: cannot read %s: %s" % (path, exc))
        return EXIT_CANNOT_RUN
    except ValueError as exc:
        print("template schema: %s is not valid JSON: %s" % (path, exc))
        return EXIT_CANNOT_RUN

    try:
        findings = schema_mod.validate(instance, doc)
    except schema_mod.SchemaError as exc:
        print("template schema: %s" % exc)
        return EXIT_CANNOT_RUN

    if args.json:
        print(json.dumps({"file": path, "valid": not findings, "findings": findings}, indent=2))
    else:
        print("schema : %s" % (args.schema or schema_mod.default_schema_path()))
        print("file   : %s" % path)
        print("")
        for item in findings:
            print("%s\n  %s" % (item["path"], item["message"]))
        print("%d violation(s)" % len(findings))
        if not findings:
            print("Every keyword in the schema was enforced; nothing was skipped.")
    return EXIT_FAILED if findings else EXIT_OK


if __name__ == "__main__":  # pragma: no cover - the real entry point is `gpubench template`
    import argparse

    _parser = argparse.ArgumentParser(prog="python -m gpubench.template.cli")
    add_arguments(_parser)
    sys.exit(main(_parser.parse_args()))
