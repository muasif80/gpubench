#!/usr/bin/env python3
"""gpubench command line.

    gpubench inspect
    gpubench run  --out result.json [--container NAME] [--target ssh://user@host]
    gpubench report result.json --html report.html
"""
import argparse
import json
import os
import pathlib
import sys

from . import runner


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


def cmd_article(args):
    """Render a long-form report end to end: HTML, then optionally PDF, DOCX and the gate.

    The content module supplies only the narrative. Everything that decides what a reader sees --
    ordering, numbering, cross-references, contents, stylesheet, pagination, export -- is the
    tool's, so it is reviewed and tested in one place rather than copied per report.
    """
    import importlib.util
    from .longform import render_report

    content_path = os.path.abspath(args.content)
    if not os.path.exists(content_path):
        print("no such content module: %s" % content_path)
        return 2
    spec = importlib.util.spec_from_file_location("_gpubench_content", content_path)
    content = importlib.util.module_from_spec(spec)
    sys.modules["_gpubench_content"] = content
    spec.loader.exec_module(content)

    for attr in ("TITLE", "SECTION_ORDER", "build", "render"):
        if not hasattr(content, attr):
            print("content module is missing %r. See gpubench/longform/__init__.py for the "
                  "contract." % attr)
            return 2

    out_dir = os.path.abspath(args.out_dir or os.path.dirname(content_path))
    base = args.basename or getattr(content, "BASENAME", "report")
    version = getattr(content, "VERSION", None)
    stem = "%s-v%s" % (base, version) if version else base

    html, figs, _data = render_report(content, args.run_dir, out_dir,
                                      warn=lambda m: print("  note: %s" % m))
    html_path = os.path.join(out_dir, stem + ".html")
    index_path = os.path.join(out_dir, "index.html")
    for out in (html_path, index_path):
        with open(out, "w", encoding="utf-8") as f:
            f.write(html)
    # Companion pages, if the content module declares any.
    from .longform import render_companions
    for fname, chtml in (render_companions(content, figs, _data,
                                           warn=lambda m: print("  note: %s" % m)) or {}).items():
        cpath = os.path.join(out_dir, fname)
        with open(cpath, "w", encoding="utf-8") as f:
            f.write(chtml)
        print("wrote %s (%.0f KB, companion)" % (cpath, len(chtml) / 1024.0))

    print("built %d figure(s)" % len(figs))
    print("wrote %s (%.0f KB)" % (html_path, len(html) / 1024.0))

    if args.pdf:
        from .longform import pdf_export
        pdf_export.render(index_path, footer_left=content.TITLE)
        info = pdf_export.paginate(index_path, os.path.splitext(index_path)[0] + ".pdf",
                                   footer_left=content.TITLE,
                                   also_write=(html_path, os.path.join(out_dir, stem + ".pdf")))
        print("wrote %s (%d sections numbered, %d outline entries, contents %s)"
              % (os.path.join(out_dir, stem + ".pdf"), info["sections"], info["outline_entries"],
                 "verified" if info["verified"] else "DRIFTED: %s" % info["drift"]))

    if args.docx:
        from .longform import docx_export
        docx_export.main([index_path]) if hasattr(docx_export, "main") else None
        src = os.path.splitext(index_path)[0] + ".docx"
        if os.path.exists(src):
            with open(src, "rb") as f_in, open(os.path.join(out_dir, stem + ".docx"), "wb") as f_out:
                f_out.write(f_in.read())
            print("wrote %s" % os.path.join(out_dir, stem + ".docx"))

    if args.check:
        from .longform import redact
        rc = redact.main([out_dir]) if hasattr(redact, "main") else 0
        if rc:
            print("REDACTION GATE FAILED -- not fit to publish")
            return rc

    return 0


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
