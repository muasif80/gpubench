#!/usr/bin/env python3
"""gpubench command line.

    gpubench inspect
    gpubench run  --out result.json [--container NAME] [--target ssh://user@host]
    gpubench report result.json --html report.html
"""
import argparse
import io
import json
import os
import pathlib
import sys

from . import runner

# Exit code for a build that WROTE A STAMPED DRAFT: --no-verify over a failing gate, or
# --allow-ungated over a module with no gate at all. Distinct from 1 (the gate blocked and nothing
# was written) and 2 (miswired: the build could not be attempted), so a pipeline can tell "there is
# a file, and it is not publishable" from "there is no file". Zero was wrong: the two documented
# escapes published at exit 0, which is exactly what a pipeline reads to decide whether to send.
DRAFT_EXIT = 3


def build_transport(target, password=None, hostkey=None):
    if not target or target in ("local", "localhost"):
        return runner.LocalTransport()
    if target.startswith("ssh://"):
        dest = target[len("ssh://"):]
        if password:
            return runner.PlinkTransport(dest, password=password, hostkey=hostkey)
        return runner.SshTransport(dest)
    return runner.SshTransport(target)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="gpubench")
    sub = ap.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--target", default="local",
                        help="local, or ssh://user@host")
    common.add_argument("--password", default=os.environ.get("GPUBENCH_SSH_PASSWORD"),
                        help="use PuTTY plink with this password instead of OpenSSH keys")
    common.add_argument("--hostkey", default=os.environ.get("GPUBENCH_SSH_HOSTKEY"))

    p = sub.add_parser("inspect", parents=[common],
                       help="read-only machine state; changes nothing")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("run", parents=[common], help="run the benchmark suite")
    p.add_argument("--out", default="result.json")
    p.add_argument("--container", help="run the PyTorch tier inside this container")
    p.add_argument("--vram-mb", type=int, default=800)
    p.add_argument("--sustain-s", type=int, default=20)
    p.add_argument("--profile", default="base", choices=["base", "peak"])
    p.add_argument("--mode", default="shared", choices=["shared", "exclusive"])
    p.add_argument("--skip", default="", help="comma list: inventory,cuda_driver,torch,capabilities,nccl,serving,embedding,accuracy")
    p.add_argument("--explain", action="store_true",
                   help="print what would run, and run nothing")
    p.add_argument("--keep-identifying", "--keep-target", dest="keep_target",
                   action="store_true",
                   help="keep deployment-identifying fields (host, container names, GPU UUIDs). "
                        "By default they are removed so a result can be shared.")

    p = sub.add_parser("report", help="render a report from a result file")
    p.add_argument("result")
    p.add_argument("--html", default=None)
    p.add_argument("--no-open", action="store_true",
                   help="do not open the finished report in a browser")

    p = sub.add_parser(
        "verify",
        help="deterministic pre-render gate over a report's claims manifest",
        description="Checks that a report's numbers agree with each other BEFORE it renders. "
                    "Exit 1 on any error, so a report that fails verification never becomes a "
                    "file -- a file is the thing that gets sent to people.")
    p.add_argument("manifest", nargs="?", help="claims manifest emitted by the generator")
    p.add_argument("--previous", default=None,
                   help="the previous version's manifest. Enables staleness and supersession "
                        "checks: a re-measurement is exactly where stale copies get created.")
    p.add_argument("--rendered", default=None,
                   help="the rendered report, checked for numerals appearing in prose without "
                        "citing a claim key.")
    p.add_argument("--findings", default=None, help="write findings as JSON to this path")
    p.add_argument("--warnings-as-errors", action="store_true",
                   help="exit 1 on warnings too. A warning is a check that cannot prove a defect "
                        "on its own; for a final edition, treat each one as a loose end.")
    p.add_argument("--demo", action="store_true",
                   help="run against a fixture carrying defects taken from real report editions. "
                        "Changes nothing; use it to see what the gate catches.")

    p = sub.add_parser(
        "experiment",
        help="run a measurement that CHANGES the system under test, then restores it",
        description="Experiments intervene rather than observe. Every one declares what it "
                    "changes, how long the service is unavailable, and how it restores; run "
                    "--list to see that table before running anything.")
    p.add_argument("name", nargs="?",
                   help="experiment id to run. Omit with --list to see what is available.")
    p.add_argument("--list", action="store_true",
                   help="print every experiment with WHAT IT DOES, WHAT IT CHANGES, ITS RISK, the "
                        "expected downtime, and how it restores. Changes nothing.")
    p.add_argument("--config", default=None,
                   help="path to the experiment config file (default: ./gpubench.json, else "
                        "built-in defaults with every experiment DISABLED).")
    p.add_argument("--write-config", metavar="PATH", default=None,
                   help="write a starter config with every experiment disabled and every risk "
                        "documented inline, then exit. Changes nothing on the target.")
    p.add_argument("--confirm-disruptive", action="store_true",
                   help="REQUIRED for any experiment marked [DISRUPTIVE]. RISK: a disruptive "
                        "experiment stops or reconfigures the live service, so it is UNAVAILABLE "
                        "while the experiment runs. Restoration is guaranteed and verified, but "
                        "the outage is real. This flag alone is not enough: the experiment must "
                        "also be enabled:true in the config file. Two gestures, because a config "
                        "file gets copied and a command line gets recalled from history.")
    p.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                   help="override one experiment setting, repeatable. RISK: settings change what "
                        "is measured and can extend downtime (for example adding context lengths "
                        "to try means more engine starts). Example: --set max_model_len=8192")
    p.add_argument("--out", default=None,
                   help="write the full result, including the baseline and the restore "
                        "verification, to this JSON file.")
    p.add_argument("--dry-run", action="store_true",
                   help="print exactly what WOULD run, including every gate that was checked, and "
                        "exit without touching the target.")
    p.add_argument("--target", default="local", help="local, or ssh://user@host")
    p.add_argument("--password", default=None, help="use PuTTY plink with this password")
    p.add_argument("--hostkey", default=None)

    p = sub.add_parser("article", help="render a long-form report from a content module")
    p.add_argument("content", help="path to the content module (a .py supplying TITLE, "
                                   "SECTION_ORDER, build() and render())")
    p.add_argument("run_dir", help="the run directory to draw measurements from")
    p.add_argument("--out-dir", default=None, help="where to write (default: beside the content "
                                                   "module)")
    p.add_argument("--basename", default=None, help="output filename stem (default: from the "
                                                    "content module's BASENAME)")
    p.add_argument("--pdf", action="store_true", help="also render PDF, with contents page numbers "
                                                      "and a navigable outline")
    p.add_argument("--docx", action="store_true", help="also render DOCX")
    p.add_argument("--check", action="store_true",
                   help="run the redaction gate over the built artifacts and FAIL if anything "
                        "identifying is found")
    p.add_argument("--no-verify", action="store_true",
                   help="DISABLES the pre-render claims gate's VERDICT: the report is written even "
                        "if its numbers contradict each other, cite nothing, or fail any check in "
                        "'gpubench verify'. The gate still runs, the manifest and findings are "
                        "still written, the overridden check ids are printed, and the document "
                        "itself is stamped DRAFT, NOT FOR PUBLICATION in the HTML, the PDF and the "
                        "DOCX. This is for inspecting a failing draft, NOT for publishing one. A "
                        "report that needs this flag to exist is not fit to send to anyone. It "
                        "does NOT stand in for --allow-ungated: a gate that failed and a gate that "
                        "was never armed are different problems. The build EXITS 3 when it writes "
                        "a stamped draft, never 0, so a pipeline cannot mistake it for a "
                        "publishable report.")
    p.add_argument("--allow-ungated", action="store_true",
                   help="permit a content module that declares no MANIFEST/claims() pair. RISK: "
                        "nothing whatsoever checks the numbers in the output, so it is a legacy "
                        "escape for reports that predate the manifest, and the document is stamped "
                        "DRAFT, NOT FOR PUBLICATION, and the build EXITS 3 rather than 0. Without "
                        "this flag an unarmed gate is an error and nothing is written, because an "
                        "ungated build that exits 0 cannot be told from a gated one by anything "
                        "reading the exit code.")
    p.add_argument("--previous", default=None, metavar="PATH",
                   help="the previous edition's claims manifest. DEFAULT: the manifest already on "
                        "disk in --out-dir, which is the copy the last build published. It is read "
                        "before this build can overwrite it. Supplying it enables the two checks "
                        "that need an earlier edition: a value that moved with no changelog row, "
                        "and an edition that DECLARES less than the last one (fewer claims, prose "
                        "blocks or figures, or a claim resting on weaker evidence). A first-ever "
                        "build has no baseline, still builds, and says so.")
    p.add_argument("--warnings-as-errors", action="store_true",
                   help="the claims gate blocks the render on warnings too, not just errors. Use "
                        "for a final edition, where an unexplained warning is a loose end.")

    p = sub.add_parser("index", help="build an index page over a directory of reports")
    p.add_argument("directory")
    p.add_argument("--out", default=None)
    p.add_argument("--no-open", action="store_true")

    args = ap.parse_args(argv)

    if args.cmd == "inspect":
        tp = build_transport(args.target, args.password, args.hostkey)
        doc = runner.run_inventory(tp)
        if args.json:
            print(json.dumps(doc, indent=2))
        else:
            print_inventory(doc)
        return 0

    if args.cmd == "run":
        skip = tuple(s.strip() for s in args.skip.split(",") if s.strip())
        if args.explain:
            print("target      : %s" % args.target)
            print("profile/mode: %s / %s" % (args.profile, args.mode))
            print("container   : %s" % (args.container or "(none, run on host)"))
            print("tiers       : " + ", ".join(
                t for t in ("inventory", "cuda_driver", "torch") if t not in skip))
            print("\nThis reads GPU state, allocates up to %d MiB of scratch VRAM, and runs"
                  % args.vram_mb)
            print("matmul and copy kernels. It starts and stops nothing.")
            return 0
        tp = build_transport(args.target, args.password, args.hostkey)
        res = runner.collect(tp, container=args.container, vram_mb=args.vram_mb,
                             sustain_s=args.sustain_s, mode=args.mode,
                             profile=args.profile, skip=skip,
                             keep_target=args.keep_target)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(res, f, indent=2)
        print("wrote %s" % args.out)
        summarise(res)
        return 0

    if args.cmd == "report":
        from . import report as report_mod
        with open(args.result, "r", encoding="utf-8") as f:
            res = json.load(f)
        out = args.html or (os.path.splitext(args.result)[0] + ".html")
        report_mod.write_report(res, out)
        print("wrote %s" % os.path.abspath(out))
        if not args.no_open:
            open_in_browser(out)
        return 0

    if args.cmd == "verify":
        from . import verify as verify_mod
        argv2 = ["--demo"] if args.demo else []
        if args.warnings_as_errors:
            argv2.append("--warnings-as-errors")
        if args.manifest:
            argv2.append(args.manifest)
        for flag, val in (("--previous", args.previous), ("--rendered", args.rendered),
                          ("--findings", args.findings)):
            if val:
                argv2 += [flag, val]
        return verify_mod.main(argv2)


    if args.cmd == "experiment":
        return cmd_experiment(args)

    if args.cmd == "article":
        return cmd_article(args)

    if args.cmd == "index":
        from . import report as report_mod
        entries = report_mod.discover(args.directory)
        out = args.out or os.path.join(args.directory, "index.html")
        report_mod.write_index(entries, out)
        print("wrote %s  (%d report(s) listed)" % (os.path.abspath(out), len(entries)))
        if not args.no_open:
            open_in_browser(out)
        return 0
    return 1


def cmd_experiment(args):
    """Thin wrapper. The body lives in experiments.py, beside the risk metadata it prints."""
    from . import experiments as X
    return X.cli_main(args, build_transport)


def _load_content_module(content_path, name="_gpubench_content"):
    """Import a content module from a path, with the bytecode cache taken out of the picture.

    STALE BYTECODE CAN BUILD THE PREVIOUS EDITION OF A REPORT. Loading a .py by path writes
    __pycache__/<name>.cpython-XX.pyc beside it, and Python reuses that cache whenever the source's
    recorded mtime SECOND and size both match. Editing a content module and rebuilding inside the
    same second, with a change that does not alter the file's length, is not a contrived case: it
    is the exact rhythm of authoring a report, and it silently defeated two tests before the cause
    was found. The build then renders the PREVIOUS edition's prose while every log line, every
    manifest and every check describes the new one.

    Two gestures, because either alone leaves a window open. The cache for this file is removed, so
    a stale one written before this fix cannot be consulted; and dont_write_bytecode is set for the
    load, so none is created to go stale. invalidate_caches() covers the directory listings
    importlib memoises within one process.
    """
    import importlib
    import importlib.util

    try:
        cached = importlib.util.cache_from_source(content_path)
    except (NotImplementedError, ValueError):
        cached = None
    if cached and os.path.exists(cached):
        try:
            os.remove(cached)
        except OSError:
            pass
    prior = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        importlib.invalidate_caches()
        spec = importlib.util.spec_from_file_location(name, content_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = prior
    return module


def _sha256_file(path):
    import hashlib
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _sha256_text(text):
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _print_coverage(label, coverage):
    """Print what the numeral checks held jurisdiction over, on every build, pass or fail.

    The coverage figure used to be printed only when the gate blocked, which is precisely backwards:
    a build that fails prints its findings anyway, and a build that passes is the one where the
    reader has nothing else to tell a manifest asserting everything from one asserting nothing.
    "0 errors" reads identically over both.
    """
    from . import verify as verify_mod

    class _Scope(object):
        pass

    scope = _Scope()
    scope.coverage = coverage or {}
    print("claims gate: %s: %s" % (label, verify_mod.coverage_line(scope).strip()))


def cmd_article(args):
    """Render a long-form report end to end: HTML, then optionally PDF, DOCX and the gate.

    The content module supplies only the narrative. Everything that decides what a reader sees --
    ordering, numbering, cross-references, contents, stylesheet, pagination, export -- is the
    tool's, so it is reviewed and tested in one place rather than copied per report.

    ORDER IS THE POINT. Everything is assembled in memory, the claims gate runs, and only then does
    anything reach disk -- report, companions, index copy, PDF, DOCX. A gate that ran after the
    write would leave the failing file sitting there, and the file is what gets sent to people.

    THE EXIT CODE IS THE ONLY THING A PIPELINE READS, so every way of not publishing a checked
    report is non-zero: a blocked gate, a half-wired gate, a gate that was never armed, a promised
    file that did not appear on disk, a stamped draft written by one of the two documented escapes
    (3), and a published file whose bytes stopped matching the bytes the gate judged. The one thing
    that exits 0 is a report that was verified, written, and still byte-identical afterwards.
    """
    import importlib.util
    from . import verify as verify_mod
    from .longform import (GATE_ABSENT, GATE_BLOCKED, GATE_INCOMPLETE, GATE_PASS,
                           BLOCKED_GUIDANCE, DRAFT_MARKER, read_manifest, render_report,
                           run_claims_gate, stamp_docx_marker, stamp_draft)

    content_path = os.path.abspath(args.content)
    if not os.path.exists(content_path):
        print("no such content module: %s" % content_path)
        return 2
    content = _load_content_module(content_path)

    for attr in ("TITLE", "SECTION_ORDER", "build", "render"):
        if not hasattr(content, attr):
            print("content module is missing %r. See gpubench/longform/__init__.py for the "
                  "contract." % attr)
            return 2

    out_dir = os.path.abspath(args.out_dir or os.path.dirname(content_path))
    base = args.basename or getattr(content, "BASENAME", "report")
    version = getattr(content, "VERSION", None)
    stem = "%s-v%s" % (base, version) if version else base
    html_path = os.path.join(out_dir, stem + ".html")
    index_path = os.path.join(out_dir, "index.html")

    # ---- the baseline, read BEFORE this build can overwrite it ----
    # WHERE THE PREVIOUS EDITION COMES FROM: --previous if given, else the manifest sitting in
    # out_dir, which is what the last build published. It has to be loaded here, ahead of the
    # render, because the gate writes to that same path; reading it afterwards would compare the
    # edition against itself and find nothing, which is how the floor came to be unchecked.
    declared_manifest = str(getattr(content, "MANIFEST", "") or "")
    prev_path = args.previous or (os.path.join(out_dir, declared_manifest)
                                  if declared_manifest else None)
    if args.previous and not os.path.exists(os.path.abspath(args.previous)):
        print("no such previous manifest: %s" % os.path.abspath(args.previous))
        return 2
    try:
        previous = read_manifest(prev_path)
    except ValueError as exc:
        print("previous edition: %s" % exc)
        print("A baseline that cannot be read is not the same as no baseline: with no baseline a "
              "manifest that shrank\npasses. Fix or move the file, or point --previous at a good "
              "copy.")
        return 2
    if previous is None:
        baseline_note = (
            "claims gate: NO BASELINE. No previous manifest at %s. Nothing to compare this "
            "edition against,\n             so the declaration floor (claim, prose and figure "
            "counts, and each claim's kind) was\n             NOT checked this build."
            % (prev_path or "(the content module declares no MANIFEST)"))
    else:
        baseline_note = (
            "claims gate: baseline %s (%d claim(s), %d prose block(s), %d figure(s))"
            % (prev_path, len(previous.get("claims") or {}), len(previous.get("prose") or []),
               len(previous.get("figures") or [])))

    # claims() is called inside render_report, exactly once, and the dict it returned is handed to
    # the gate below. The gate writes the only copy of it.
    rendered = render_report(content, args.run_dir, out_dir,
                             warn=lambda m: print("  note: %s" % m))
    html, figs, _data = rendered

    # Companion pages, if the content module declares any. Assembled now, written after the gate.
    from .longform import render_companions
    companions = render_companions(content, figs, _data,
                                   warn=lambda m: print("  note: %s" % m)) or {}

    def not_written():
        return "NOT WRITTEN: %s" % ", ".join(
            [os.path.basename(html_path), "index.html"] + sorted(companions))

    # ---- the gate. Nothing above this line has written a report file. ----
    # It runs even under --no-verify: the flag overrides the verdict, not the checking. A log that
    # says only "skipped" does not say WHAT was skipped, and the findings file is the record.
    # EVERY DOCUMENT THIS BUILD WILL WRITE GOES IN, not just the report. The companions were
    # rendered, stamped and published with no check over them at all, so a figure the gate blocks
    # in the report shipped intact on the page next to it.
    gate = run_claims_gate(content, figs, _data, out_dir, rendered_html=html,
                           warnings_as_errors=args.warnings_as_errors,
                           previous=previous, manifest=rendered.manifest,
                           companions=companions)
    print(baseline_note)
    for path in (gate["manifest_path"], gate["findings_path"]):
        if path:
            print("claims gate: wrote %s" % path)
    if gate["status"] in (GATE_PASS, GATE_BLOCKED):
        _print_coverage(os.path.basename(html_path), gate["coverage"])
        for name in sorted(gate.get("companion_coverage") or {}):
            _print_coverage(name, gate["companion_coverage"][name])

    # Set when the document must carry the draft stamp: the gate did not pass, whatever the reason.
    draft_detail = None

    if gate["status"] == GATE_INCOMPLETE:
        print("claims gate: MISWIRED -- %s" % gate["message"])
        print(not_written())
        return 2

    if gate["status"] == GATE_ABSENT:
        print("claims gate: NOT ARMED -- %s" % gate["message"])
        print("             Add MANIFEST and claims(figures, data) to the content module so a "
              "report that\n             contradicts itself cannot be written. See "
              "gpubench/longform/__init__.py.")
        if not args.allow_ungated:
            print("claims gate: UNGATED, so nothing was written. An ungated build that exits 0 "
                  "cannot be told\n             from a gated one by anything reading the exit "
                  "code, and the exit code is all a\n             pipeline reads. Arm the gate, or "
                  "pass --allow-ungated for a legacy report that\n             predates the "
                  "manifest.")
            print(not_written())
            return 1
        draft_detail = ("The claims gate is not armed for this report and --allow-ungated was "
                        "given, so no number in it was checked by anything.")
    elif gate["status"] == GATE_BLOCKED:
        print("claims gate: %s\n" % gate["message"])
        findings = verify_mod.Findings()
        findings.items = gate["findings"]
        if gate.get("coverage") is not None:
            # A fresh Findings reports that the numeral checks never ran. They ran, inside the
            # gate, so hand back the scope it measured rather than print a false one.
            findings.coverage = gate["coverage"]
        # Pass the stream explicitly: verify.report()'s default is bound at import time, so a
        # caller that redirects stdout (a test, a build log) would not see the findings.
        verify_mod.report(findings, sys.stdout)
        print()
        n_over, over_ids, over_word = _overridden(gate["findings"])
        if not args.no_verify:
            print(BLOCKED_GUIDANCE)
            print("\nfindings: %s" % gate["findings_path"])
            print(not_written())
            return 1
        print("claims gate: SKIPPED: %d %s(s) suppressed: %s" % (n_over, over_word,
                                                                 ", ".join(over_ids)))
        print("             This draft is for inspection, not publication.")
        draft_detail = ("%d %s(s) from the claims gate were suppressed with --no-verify: %s."
                        % (n_over, over_word, ", ".join(over_ids)))
    else:
        print("claims gate: %s" % gate["message"])
        if args.no_verify:
            # Saying "skipped" when nothing needed skipping would misreport the build. The flag was
            # given; the gate had nothing to suppress.
            print("claims gate: --no-verify was given and the gate passed anyway: 0 error(s) "
                  "suppressed, no draft stamp applied.")

    # The stamp goes on AFTER the gate has judged the document, so what was checked is the document
    # as authored, and the banner cannot mask or trip a rendered-document check. The judged digest
    # is taken first, so the one deliberate difference between the judged bytes and the shipped
    # bytes can be named rather than assumed.
    judged_html_sha = _sha256_text(html)
    if draft_detail:
        html = stamp_draft(html, draft_detail)
        companions = dict((name, stamp_draft(chtml, draft_detail))
                          for name, chtml in companions.items())
        print("stamped: DRAFT, NOT FOR PUBLICATION (visible banner plus the HTML comment marker "
              "%r)" % DRAFT_MARKER)

    companion_files = [(os.path.join(out_dir, name), chtml)
                       for name, chtml in companions.items()]
    promised = [(html_path, html), (index_path, html)] + companion_files
    for path, text in promised:
        try:
            with io.open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
        except (IOError, OSError) as exc:
            print("could not write %s: %s" % (path, exc))
    # isfile, not exists: a directory sitting where the report belongs is not a report, and that is
    # exactly what a stale build tree hands you.
    missing = [p for p, _ in promised if not os.path.isfile(p)]
    if missing:
        # A verified report that never reached disk is not a success, and the exit code is the only
        # place that can say so: the log above has already announced the gate passed.
        print("REPORT NOT WRITTEN: %s" % ", ".join(sorted(os.path.basename(p) for p in missing)))
        return 1

    # THE BYTES THE GATE JUDGED ARE THE BYTES THAT SHIP. Recorded here and re-read after every
    # export step below, because --pdf used to reopen the published HTML, insert contents page
    # numbers and write it back, after the gate had judged it and after the versioned edition had
    # been copied. The file a reader received was then not the file that was verified, and no exit
    # code anywhere said so.
    shipped = dict((path, _sha256_text(text)) for path, text in promised)

    for path, chtml in companion_files:
        print("wrote %s (%.0f KB, companion)" % (path, len(chtml) / 1024.0))

    print("built %d figure(s)" % len(figs))
    print("wrote %s (%.0f KB)" % (html_path, len(html) / 1024.0))

    # The draft stamp has to reach the formats that get mailed, not just the HTML. The PDF carries
    # it in the page footer as well as the banner, because a PDF is read a page at a time.
    draft_note = "DRAFT, NOT FOR PUBLICATION" if draft_detail else ""
    draft_marker = DRAFT_MARKER if draft_detail else ""

    if args.pdf:
        from .longform import pdf_export
        pdf_export.render(index_path, footer_left=content.TITLE, draft_note=draft_note,
                          draft_marker=draft_marker)
        # also_write takes the PDF only. Handing it the HTML path is what published the paginated
        # working copy over the verified edition, and paginate() now refuses it outright.
        info = pdf_export.paginate(index_path, os.path.splitext(index_path)[0] + ".pdf",
                                   footer_left=content.TITLE, draft_note=draft_note,
                                   draft_marker=draft_marker,
                                   also_write=(os.path.join(out_dir, stem + ".pdf"),))
        print("wrote %s (%d sections numbered, %d outline entries, contents %s)"
              % (os.path.join(out_dir, stem + ".pdf"), info["sections"], info["outline_entries"],
                 "verified" if info["verified"] else "DRIFTED: %s" % info["drift"]))

    if args.docx:
        from .longform import docx_export
        docx_export.main([index_path]) if hasattr(docx_export, "main") else None
        src = os.path.splitext(index_path)[0] + ".docx"
        if os.path.exists(src):
            # The marker goes in before the copy, so both files carry it. A DOCX is one of the two
            # formats that actually get mailed, and until now it carried only the banner a person
            # can see and delete.
            if draft_marker and not stamp_docx_marker(src, draft_marker, draft_detail):
                print("DRAFT STAMP NOT APPLIED to %s: no core properties part to write the marker "
                      "into.\n             A draft no pipeline can detect is worse than no draft "
                      "file at all, so nothing was\n             copied and this build is a "
                      "failure." % src)
                return 1
            with open(src, "rb") as f_in, open(os.path.join(out_dir, stem + ".docx"), "wb") as f_out:
                f_out.write(f_in.read())
            print("wrote %s" % os.path.join(out_dir, stem + ".docx"))

    # Re-read from disk, after every step that could have touched a published file.
    drifted = sorted(p for p, digest in shipped.items()
                     if not os.path.isfile(p) or _sha256_file(p) != digest)
    if drifted:
        print("INTEGRITY FAILURE: %d published file(s) differ from the bytes the claims gate "
              "judged: %s" % (len(drifted), ", ".join(os.path.basename(p) for p in drifted)))
        print("             A step after the gate rewrote a verified artifact, so the file on disk "
              "is not the file\n             that was checked. It must not be sent.")
        return 1
    print("integrity: the bytes the gate judged are the bytes on disk, after every export step")
    for path in sorted(shipped):
        print("  sha256 %s  %s" % (shipped[path], os.path.basename(path)))
    if draft_detail:
        print("  the one accounted difference from the judged document is the draft stamp applied "
              "above:\n  the judged HTML was sha256 %s before it was stamped." % judged_html_sha)

    if args.check:
        from .longform import redact
        rc = redact.main([out_dir]) if hasattr(redact, "main") else 0
        if rc:
            print("REDACTION GATE FAILED -- not fit to publish")
            return rc

    if draft_detail:
        # A stamped draft is a way of NOT publishing a checked report, and the exit code is the
        # only thing a pipeline reads. Exiting 0 here made --no-verify and --allow-ungated
        # indistinguishable from a clean build to everything downstream, which is the entire
        # failure the stamp exists to prevent, restated in the one place that is machine-read.
        print("EXIT %d: a draft was written, not a publishable report. %s"
              % (DRAFT_EXIT, draft_detail))
        return DRAFT_EXIT
    return 0


def _overridden(findings):
    """What --no-verify actually suppressed: how many, of which severity, and which check ids.

    Errors if there are any, warnings otherwise (the --warnings-as-errors case). A log line saying
    "skipped" names nothing; a reader has to be able to see that A2 and B1 were the checks turned
    off, without opening the findings file.
    """
    errors = [i for i in findings if i.get("severity") == "error"]
    picked = errors or [i for i in findings if i.get("severity") == "warn"]
    ids = sorted({str(i.get("check")) for i in picked})
    return len(picked), ids, "error" if errors else "warning"


def open_in_browser(path):
    """Open the finished report if this machine has a browser and a display.

    Deliberately best-effort and never fatal: the common case for this tool is a headless server
    over SSH, where failing to open a browser is normal rather than an error.
    """
    import webbrowser
    uri = pathlib.Path(os.path.abspath(path)).as_uri()
    if os.environ.get("GPUBENCH_NO_BROWSER"):
        return False
    if sys.platform.startswith("linux") and not (
            os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        print("  (headless session: open %s yourself)" % uri)
        return False
    try:
        if webbrowser.open(uri):
            print("  opened in your browser")
            return True
    except Exception:  # noqa: BLE001
        pass
    print("  (no browser available: open %s yourself)" % uri)
    return False


def print_inventory(doc):
    host = doc.get("host", {})
    print("HOST  %s" % host.get("os", "?"))
    print("CPU   %s (%s threads)" % (host.get("cpu", "?"), host.get("cpu_threads", "?")))
    if host.get("board_name"):
        print("BOARD %s %s  BIOS %s" % (host.get("board_vendor", ""), host.get("board_name"),
                                        host.get("bios_version", "")))
    print()
    print("%-3s %-34s %9s %9s %7s %10s" % ("IDX", "NAME", "TOTAL MiB", "FREE MiB", "TEMP", "PCIe"))
    for g in doc.get("gpus", []):
        print("%-3s %-34s %9s %9s %7s %10s" % (
            g.get("index"), str(g.get("name"))[:34], g.get("memory_total"),
            g.get("memory_free"), g.get("temperature_gpu"),
            "gen%s x%s" % (g.get("pcie_link_gen_current"), g.get("pcie_link_width_current"))))
    for l in doc.get("pcie_links", []):
        print("  %s -> bridge %s (%s x%s)" % (l["bdf"], l["parent_bridge"],
                                              l.get("bridge_max_speed"), l.get("bridge_max_width")))
    if doc.get("warnings"):
        print("\nWARNINGS")
        for w in doc["warnings"]:
            print("  ! %s" % w)


def summarise(res):
    t = res.get("probes", {}).get("torch_compute", {})
    c = res.get("probes", {}).get("cuda_driver", {})
    mm = t.get("matmul", [])
    if mm:
        print("\n%-4s %-18s %10s" % ("GPU", "DTYPE", "TFLOPS/TOPS"))
        for m in mm:
            print("%-4s %-18s %10.1f" % (m["device"], m["dtype"], m["tflops_best"]))
    bw = (t.get("memory_bandwidth") or []) + (c.get("memory_bandwidth") or [])
    for b in bw:
        print("GPU%s memory copy   %8.1f GB/s" % (b.get("device"), b["gb_s_best"]))
    for h in (t.get("host_transfer") or []) + (c.get("host_transfer") or []):
        print("GPU%s %-14s %8.1f GB/s" % (h.get("device"), h["direction"], h["gb_s_best"]))
    for s in t.get("sustained", []):
        line = "GPU%s sustained bf16 %8.1f TFLOPS" % (s["device"], s["sustained_tflops"])
        if s.get("power"):
            line += "  at %.0f W mean" % s["power"]["power_mean_w"]
        if s.get("tflops_per_watt"):
            line += "  (%.2f TFLOPS/W)" % s["tflops_per_watt"]
        print(line)
    errs = []
    for name, probe in res.get("probes", {}).items():
        errs += ["%s: %s" % (name, e) for e in (probe.get("errors") or [])]
    for e in errs[:8]:
        print("  ! %s" % e)


if __name__ == "__main__":
    sys.exit(main())
