"""Scaffold a report: a content module, a manifest generator, and a runnable sample bundle.

WHAT THIS PRODUCES AND WHY IT IS SHAPED THIS WAY. ``gpubench template init`` writes a directory
that BUILDS AND PASSES THE CLAIMS GATE THE MOMENT IT IS WRITTEN. That is a hard requirement, not a
nicety: a scaffold whose first build fails the tool's own gate teaches the first-time author that
the gate is noise, and an author who has learned that will reach for ``--no-verify`` on the
edition that actually needed checking.

So the scaffold is not a set of blanks. It is a small, complete, honest report:

    content.py          the content module: TITLE, SECTION_ORDER drawn from report-outline.yaml,
                        build(), render() and the MANIFEST/claims() pair that arms the gate
    run/sample-run.json a run artefact carrying SYNTHETIC numbers, marked ``"sample": true``
    README.md           how to point it at a real run, and what changes when you do

THE SAMPLE IS MARKED, AND THE MARK CHANGES THE MANIFEST. The generated ``claims()`` reads
``sample`` out of the run artefact and declares its claims ``supplied`` with a source, not
``measured`` with a run id, because nothing was measured. Point the module at a real gpubench
result and the same code declares the same claims ``measured``. The kind follows the artefact
rather than the author's preference, which is the one habit worth building in from the first
build.

The section list comes from ``report-outline.yaml`` at init time, so the scaffold cannot drift
from the canonical outline the way a copied file does. Each section carries its own ``purpose``
from the manifest as its placeholder prose and its ``anti_pattern`` as a comment for the author.
Only ``purpose`` is rendered: the anti-pattern is guidance for whoever writes the section, not
text for the reader.
"""

from __future__ import annotations

import io
import json
import os

from . import outline as outline_reader

__all__ = ["ScaffoldError", "init", "sections_for"]


class ScaffoldError(Exception):
    """The scaffold cannot be written: the target is occupied, or the outline is unreadable."""


# Written to the run directory by init. Synthetic on purpose and marked as such: see the module
# docstring for why the mark is load-bearing rather than decorative.
SAMPLE_RUN = {
    "sample": True,
    "note": "Synthetic numbers written by `gpubench template init` so the scaffold builds before "
            "any hardware is involved. Nothing here was measured. Replace this file with a real "
            "`gpubench run` result and the generated claims() will declare its claims measured "
            "instead of supplied.",
    "run_id": "sample",
    "started": "2026-01-01T00:00:00Z",
    "finished": "2026-01-01T00:10:00Z",
    "host": {"os": "sample-host", "cpu": "sample-cpu", "cpu_threads": 8},
    "probes": {
        "torch_compute": {
            "matmul": [
                {"device": 0, "dtype": "bf16", "tflops_best": 120.0},
                {"device": 1, "dtype": "bf16", "tflops_best": 118.0},
            ],
            "memory_bandwidth": [
                {"device": 0, "gb_s_best": 1450.0},
            ],
        },
        # The quality gate. A benchmark that measures only speed rewards a stack that got faster
        # by getting worse, so the claims gate refuses a report that records none, and it reads
        # the outcome back out of this block rather than believing the manifest.
        "accuracy": {
            "summary": {"cases": 2, "deterministic": 2, "exact_match_pct": 100.0},
            "method": {"cases_published": [{"id": "sample-case-1"}, {"id": "sample-case-2"}]},
            "errors": [],
        },
    },
}


def sections_for(outline, include_optional=False):
    """The scaffold's sections, in the outline's canonical order.

    Returns a list of (id, title, purpose, anti_pattern). Optional sections are dropped by default
    because the outline's own ordering_rule says dropping one never changes the relative order of
    the required entries, so a scaffold of the required set is a legitimate report skeleton and a
    shorter one to read.
    """
    rows = []
    entries = sorted(outline.get("sections") or [], key=lambda s: s.get("order") or 0)
    for entry in entries:
        if not entry.get("required") and not include_optional:
            continue
        rows.append((str(entry.get("id")), str(entry.get("title") or entry.get("id")),
                     str(entry.get("purpose") or "").strip(),
                     str(entry.get("anti_pattern") or "").strip()))
    if not rows:
        raise ScaffoldError("the outline yielded no sections to scaffold")
    return rows


def _py_str(text):
    """A Python string literal for generated source: ASCII only, no line breaks, no smart quotes.

    Generated source is read by the author and diffed by reviewers, so it has to be plain. Any
    character outside printable ASCII is replaced rather than escaped: the outline is ASCII today
    and a stray non-ASCII byte in a generated file is a defect to see, not to smuggle through as
    an escape sequence.
    """
    clean = "".join(ch if 32 <= ord(ch) < 127 else " " for ch in text)
    clean = " ".join(clean.split())
    return json.dumps(clean)


def _wrap(text, width, indent):
    """Wrap for readability in generated source. Plain greedy fill; no hyphenation."""
    words, lines, current = text.split(), [], ""
    for word in words:
        candidate = (current + " " + word).strip()
        if len(candidate) > width and current:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return ("\n" + indent).join(lines) if lines else ""


# The generated content module. Placeholders are @@NAME@@ rather than {} or %s because the
# generated code itself contains both brace pairs (the manifest's prose markers) and percent
# formatting, and a template that fights its own output is a template that will be edited wrongly.
_CONTENT_TEMPLATE = '''\
"""@@TITLE@@

Scaffolded by `gpubench template init`. Build it with:

    gpubench article content.py run --out-dir out

WHAT TO EDIT, IN ORDER.

  1. EXTRACT, below. One entry per number the report draws from the run artefact. The `path` is a
     dotted path into the run JSON, with integers for list positions. Nothing else in this file
     needs to change to add or remove a measurement.
  2. render(). The prose. Every section starts as its purpose sentence from report-outline.yaml
     and a TO DO line; replace them with the report's own words.
  3. claims(). It is generated from EXTRACT and rarely needs editing, but read it once: it is the
     thing that makes a wrong number impossible to publish.

THE ONE RULE. Never type a measured number into a sentence. Print it from `data`, declare it in
claims(), and let the gate check that the two agree. Every defect this template exists to prevent
was a digit typed into prose that later drifted from the value it restated.
"""
import io
import json
import os

TITLE = @@TITLE_LIT@@
BASENAME = @@BASENAME_LIT@@
VERSION = "0.1"

# Arms the pre-render claims gate. Without the MANIFEST/claims() pair `gpubench article` refuses
# to write anything unless it is given --allow-ungated, and then it stamps the result a draft.
MANIFEST = "claims.json"

# The run artefact this report is built from, looked for inside the run directory. A directory
# holding exactly one .json file is also accepted.
RUN_FILE = @@RUN_FILE_LIT@@

# The figure id the headline table is rendered under. Declared in claims()["figures"] as well, so
# a figure that goes missing from the document is a gate finding rather than a silent omission.
HEADLINE_FIGURE = "fig1_headline"

# build() copies the run artefact into the output directory under this name, and the manifest's
# run table points the quality gate at it. See build() for why the copy exists.
ARTIFACT_COPY = "run-artifact.json"


# --------------------------------------------------------------------------------------
# WHAT THIS REPORT DRAWS FROM THE RUN. Edit this table, not the code under it.
#
#   id        the claim id. Prose cites it, the manifest declares it, the gate checks it.
#   path      dotted path into the run JSON. Integers index lists.
#   unit      printed beside the value, and the unit the gate matches on.
#   basis     one of: per_device, per_shard, total, per_sequence, per_token, per_request,
#             ratio, scalar. It says what the number is per, which is where two reports of the
#             same machine most often stop agreeing.
#   label     how the quantity is named in the headline table.
#   sum_into  optional. Adds this claim to a derived total of that id, recomputed by the gate.
# --------------------------------------------------------------------------------------
EXTRACT = [
    {"id": "compute_gpu0", "path": "probes.torch_compute.matmul.0.tflops_best",
     "unit": "TFLOPS", "basis": "per_device", "label": "Dense bf16 matmul, device 0",
     "sum_into": "compute_total"},
    {"id": "compute_gpu1", "path": "probes.torch_compute.matmul.1.tflops_best",
     "unit": "TFLOPS", "basis": "per_device", "label": "Dense bf16 matmul, device 1",
     "sum_into": "compute_total"},
    {"id": "bandwidth_gpu0", "path": "probes.torch_compute.memory_bandwidth.0.gb_s_best",
     "unit": "GB/s", "basis": "per_device", "label": "Device memory copy, device 0"},
]

# Derived totals, one per distinct sum_into. The value is never typed: it is summed here and
# recomputed independently by the gate from the formula, so a total that stops matching its parts
# blocks the build instead of shipping.
TOTALS = {
    "compute_total": {"unit": "TFLOPS", "basis": "total",
                      "label": "Dense bf16 matmul, both devices"},
}

# (section id, heading, placeholder prose) taken from report-outline.yaml at scaffold time.
# `gpubench template outline --section <id>` prints what each one must satisfy.
SECTIONS = [
@@SECTIONS@@
]

# The abstract carries no section number, by the outline's convention: it states the conclusion
# before the argument, so numbering it would imply it is a step in the argument.
UNNUMBERED = ("Abstract",)

SECTION_ORDER = [title for _id, title, _purpose in SECTIONS]


def _dig(node, path, source):
    """Walk a dotted path into the run JSON. Integers index lists. Raises with the path named."""
    walked = []
    for token in path.split("."):
        walked.append(token)
        try:
            node = node[int(token)] if isinstance(node, list) else node[token]
        except (KeyError, IndexError, TypeError, ValueError):
            raise ValueError(
                "%s has nothing at %s (failed at %s). Edit EXTRACT in this content module so "
                "each path names something the run artefact actually holds; a report that "
                "invents a path is a report about a machine nobody measured."
                % (source, path, ".".join(walked)))
    return node


def _find_run_file(run_dir):
    """The run artefact inside run_dir. A named file wins; otherwise a lone .json is accepted."""
    if os.path.isfile(run_dir):
        return run_dir
    if not os.path.isdir(run_dir):
        raise ValueError("no such run directory: %s" % run_dir)
    named = os.path.join(run_dir, RUN_FILE)
    if os.path.isfile(named):
        return named
    found = sorted(n for n in os.listdir(run_dir) if n.lower().endswith(".json"))
    if len(found) == 1:
        return os.path.join(run_dir, found[0])
    raise ValueError(
        "run directory %s holds %d .json file(s) (%s) and none named %s, so which one the report "
        "is about is ambiguous. Name it %s, or pass a single file as the run directory."
        % (run_dir, len(found), ", ".join(found) or "none", RUN_FILE, RUN_FILE))


def _gate_from(run, source):
    """The quality gate as the run artefact RECORDED it, never as the author would like it.

    Nothing in this file may set passed = True by hand. The gate opens this same artefact and
    checks the manifest's declaration against it, so a hardcoded pass would be caught; the deeper
    reason is that a gate result nobody read back is worse than no gate, because it reports
    success unconditionally. The pass condition mirrors the verifier's: no errored case, every
    percentage at or above the threshold, and every case deterministic.
    """
    accuracy = (run.get("probes") or {}).get("accuracy")
    if not isinstance(accuracy, dict):
        raise ValueError(
            "%s records no probes.accuracy block, so this report has no quality gate to declare "
            "and the claims gate will block it (G1). Take a run with the accuracy probe enabled, "
            "or record a gate result in the artefact. Speed without a quality gate rewards a "
            "stack that got faster by getting worse." % source)
    summary = accuracy.get("summary") or {}
    published = (accuracy.get("method") or {}).get("cases_published") or []
    cases, deterministic = summary.get("cases"), summary.get("deterministic")
    reasons = []
    if accuracy.get("errors"):
        reasons.append("%d case(s) errored" % len(accuracy["errors"]))
    for name, value in sorted(summary.items()):
        if name.endswith("_pct") and isinstance(value, (int, float)) and value + 1e-9 < 100.0:
            reasons.append("%s is %g, below the 100 threshold" % (name, value))
    if isinstance(cases, int) and isinstance(deterministic, int) and deterministic < cases:
        reasons.append("only %d of %d cases were deterministic" % (deterministic, cases))
    return {"passed": not reasons,
            "cases_published": len(published) if isinstance(published, list) else 0,
            "reasons": reasons}


def build(run_dir, out_dir=None):
    """Read the run artefact and return (figures, data). Nothing here formats or renders."""
    path = _find_run_file(run_dir)
    with io.open(path, "r", encoding="utf-8") as fh:
        run = json.load(fh)

    values = {}
    for row in EXTRACT:
        raw = _dig(run, row["path"], path)
        if not isinstance(raw, (int, float)) or isinstance(raw, bool):
            raise ValueError("%s: %s holds %r, which is not a number" % (path, row["path"], raw))
        values[row["id"]] = float(raw)
    for total_id in TOTALS:
        values[total_id] = sum(values[r["id"]] for r in EXTRACT if r.get("sum_into") == total_id)

    # THE EVIDENCE SHIPS BESIDE THE REPORT. The gate reads the quality-gate outcome back out of
    # the artefact instead of believing the manifest, so the artefact has to be somewhere the
    # manifest can name. A copy in the output directory, named relatively, keeps the published set
    # self-contained and keeps this machine's directory layout out of a document that gets mailed.
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        with io.open(os.path.join(out_dir, ARTIFACT_COPY), "w", encoding="utf-8",
                     newline="\\n") as fh:
            json.dump(run, fh, indent=2)

    data = {
        "values": values,
        "gate": _gate_from(run, path),
        "artifact": ARTIFACT_COPY,
        "run_file": os.path.basename(path),
        "run_id": str(run.get("run_id") or "primary"),
        "started": run.get("started"),
        "finished": run.get("finished"),
        # A run artefact that says it is synthetic is the only thing standing between a scaffold
        # and a report that reads as a measurement. It decides the claim kind in claims().
        "sample": bool(run.get("sample")),
    }
    return {}, data


def _fmt(value):
    """One printed form per value, used by the prose and the table alike.

    Formatting in one place is not tidiness. Two roundings of one number read as two numbers, and
    the gate matches printed text against claim values, so a second format is a second chance to
    disagree with the evidence.
    """
    return "%.1f" % value


def _row(data, claim_id):
    for row in EXTRACT:
        if row["id"] == claim_id:
            return row
    return dict(TOTALS[claim_id], id=claim_id)


def _printed(data, claim_id):
    """The value as the document prints it, unit included."""
    row = _row(data, claim_id)
    return "%s %s" % (_fmt(data["values"][claim_id]), row["unit"])


def _headline_figure(data):
    """The headline table. Every cell is declared in claims()["tables"] and checked against it."""
    ids = [row["id"] for row in EXTRACT] + sorted(TOTALS)
    cells = "".join(
        "<tr><td>%s</td><td>%s</td></tr>" % (_row(data, cid)["label"], _printed(data, cid))
        for cid in ids)
    return ('<figure id="%s"><figcaption>Figure 1. Headline figures</figcaption>'
            "<table><thead><tr><th>quantity</th><th>value</th></tr></thead>"
            "<tbody>%s</tbody></table></figure>" % (HEADLINE_FIGURE, cells))


def _sample_note(data):
    """Said in the document, not only in the manifest, while the run artefact is synthetic."""
    if not data["sample"]:
        return ""
    return ("<p><strong>These numbers are not measurements.</strong> This edition was built from "
            "the synthetic sample artefact that ships with the scaffold, so every value below is "
            "declared as supplied rather than measured. Point the build at a real run and the "
            "same code declares them measured.</p>")


# Sections that carry generated content under their placeholder prose. Everything else renders as
# the outline's purpose plus a TO DO line, which is what makes the first build honest: a section
# nobody has written yet says so.
EXTRA = {
    "abstract": lambda data: (
        _sample_note(data)
        + "<p>Both devices together reach %s of dense bf16 matmul throughput.</p>"
        % _printed(data, "compute_total")),
    "headline": _headline_figure,
    "findings": lambda data: (
        "<p>The headline figure for this system is %s, the sum of the two devices measured "
        "separately.</p>" % _printed(data, "compute_total")),
}


def render(figures, data):
    """The document body: an h2 per section, in the outline's order."""
    out = []
    number = 0
    for section_id, title, purpose in SECTIONS:
        if title in UNNUMBERED:
            out.append("<h2>%s</h2>" % title)
        else:
            number += 1
            out.append("<h2>%d. %s</h2>" % (number, title))
        out.append("<p>%s</p>" % purpose)
        extra = EXTRA.get(section_id)
        if extra:
            out.append(extra(data))
        out.append("<p>TO DO: write this section. Run <code>gpubench template outline --section "
                   "%s</code> for what it must satisfy and what it must not become.</p>"
                   % section_id)
    return "".join(out)


def claims(figures, data):
    """The claims manifest: what the document asserts, and what backs each assertion.

    Generated from EXTRACT, so adding a measurement adds its claim. Read the kind branch: a run
    artefact marked sample yields supplied claims with a source, because nothing was measured.
    Saying measured there would be the first false statement in the report, and every other check
    in the gate trusts kind.
    """
    values = data["values"]
    measured = not data["sample"]
    runs = {data["run_id"]: {"artifact": data["artifact"]}}
    for key in ("started", "finished"):
        if data.get(key):
            runs[data["run_id"]][key] = data[key]

    table = {}
    for row in EXTRACT:
        claim = {"value": values[row["id"]], "unit": row["unit"], "basis": row["basis"],
                 "label": row["label"]}
        if measured:
            claim["kind"] = "measured"
            claim["run"] = data["run_id"]
        else:
            claim["kind"] = "supplied"
            claim["source"] = data["run_file"]
        table[row["id"]] = claim
    for total_id, meta in TOTALS.items():
        parts = [row["id"] for row in EXTRACT if row.get("sum_into") == total_id]
        table[total_id] = {"value": values[total_id], "unit": meta["unit"],
                           "basis": meta["basis"], "label": meta["label"], "kind": "derived",
                           "formula": " + ".join(parts)}

    cells = [row["id"] for row in EXTRACT] + sorted(TOTALS)
    return {
        "schema": "claims/1",
        "report": {
            "version": VERSION,
            # Every report has to say how load arrived, because a latency figure cannot be read
            # without it. A compute microbenchmark is closed loop with a population of one; if
            # this becomes a serving report, change both of these together and say what the
            # harness actually did.
            "arrival_model": "closed_loop",
            "arrival_note": "Fixed in-flight population of one: the harness issues one operation, "
                            "waits for it to complete, and issues the next.",
        },
        "runs": runs,
        "claims": table,
        # Prose blocks carry the sentence with its claim markers left in, so the gate reads what
        # the sentence MEANS rather than the digits the renderer happened to print. A block's
        # `assert` is the only falsifiable thing in it: a comparison between two claims, checked
        # on every build. The one below says the two devices are within a factor of two of each
        # other, which is a real statement about this machine and blocks the build if it stops
        # being true. Both sides must share a basis, so a per_device value is never compared
        # against a total without a conversion.
        "prose": [
            {"id": "abstract",
             "text": "Both devices together reach " + "{{compute_total}}"
                     + " of dense bf16 matmul throughput."},
            {"id": "findings",
             "text": "The headline figure for this system is " + "{{compute_total}}"
                     + ", the sum of the two devices measured separately.",
             "assert": {"op": "ratio_between", "left": "compute_gpu0", "right": "compute_gpu1",
                        "min": 0.5, "max": 2.0}},
        ],
        "tables": {HEADLINE_FIGURE: {"cells": cells}},
        "figures": [{"id": HEADLINE_FIGURE, "table_view": True}],
        # Read out of the artefact by build(), not decided here. The gate opens the same file and
        # checks these two against what it finds, so a hardcoded pass is caught rather than
        # trusted.
        "gate": {"passed": data["gate"]["passed"],
                 "cases_published": data["gate"]["cases_published"],
                 "window_run": data["run_id"],
                 "ran_at": data.get("finished") or data.get("started")},
        # Every unit-bearing numeral the document prints must trace to a claim. Lowering this is
        # how a report starts carrying numbers nothing checks; raise the claim count instead.
        #
        # min_bare_numeral_pct is deliberately NOT declared. The bare numerals in this document
        # are the contents page's own section numbers, which the engine assigns, so the honest
        # options are to leave the default floor and carry the warning, or to declare a floor of
        # zero. A floor of zero is an opt-out, and an opt-out an author can simply write down is
        # the thing every check in this tool exists to refuse. Declare a real floor once the
        # report's prose carries bare numerals of its own.
        "coverage": {"min_unit_bearing_pct": 100.0},
    }
'''


_README_TEMPLATE = '''\
# @@TITLE@@

Scaffolded by `gpubench template init`. It builds and passes the claims gate as it stands.

## Build it

    gpubench article content.py run --out-dir out

Exit 0 means the gate passed and `out/@@BASENAME@@-v0.1.html` is the checked document. Exit 1
means the gate blocked and nothing was written. Exit 3 means a draft was written and stamped, not
a publishable report.

Add `--pdf --docx` for the formats that get mailed.

## What is in here

    content.py            the report: sections, prose, and the claims manifest generator
    run/@@RUN_FILE@@      a SYNTHETIC run artefact so the first build works with no hardware
    out/                  written by the build: the document, index.html and claims.json

## Point it at a real run

    gpubench run --out run/result.json
    gpubench article content.py run --out-dir out

Delete `run/@@RUN_FILE@@` first, or the named sample still wins. The moment the artefact is a
real result the generated `claims()` declares its claims **measured** with a run id instead of
**supplied** with a source, because the run artefact no longer says `"sample": true`. Nothing else
changes, which is the point: the kind follows the evidence.

If the build then fails with "has nothing at ...", edit `EXTRACT` in `content.py` so each `path`
names something your result file actually holds.

## The sections

The section list came from the tool's canonical outline. To see what any section must satisfy:

    gpubench template outline --section headline
    gpubench template outline --invariants

## What the gate will not let you do

Type a measured number into a sentence. Print it from `data`, declare it in `claims()`, and the
gate checks that the document and the evidence still agree. If two numbers disagree, one of them
is wrong about the machine: fix the generator, the prose, or the measurement. Never edit a
measured value to make a check pass.
'''


def _render_sections(rows):
    """The SECTIONS literal, one entry per section, with the anti-pattern as a comment above it."""
    out = []
    for section_id, title, purpose, anti in rows:
        if anti:
            out.append("    # AVOID: " + _wrap(anti, 92, "    #        "))
        out.append("    (%s, %s,\n     %s),"
                   % (_py_str(section_id), _py_str(title), _py_str(purpose)))
    return "\n".join(out)


def _write(path, text, force):
    if os.path.exists(path) and not force:
        raise ScaffoldError(
            "%s already exists. Refusing to overwrite it: a scaffold that silently replaces an "
            "edited content module destroys work that is not in any manifest. Pass --force if "
            "that is what you mean, or init into an empty directory." % path)
    with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def init(target, title=None, basename=None, include_optional=False, outline_path=None,
         force=False):
    """Write the scaffold into ``target``. Returns the list of paths written, in write order."""
    target = os.path.abspath(target)
    try:
        outline = outline_reader.load_outline(outline_path)
    except outline_reader.OutlineError as exc:
        raise ScaffoldError("cannot read the section outline: %s" % exc)
    rows = sections_for(outline, include_optional=include_optional)

    basename = basename or os.path.basename(target.rstrip(os.sep)) or "report"
    title = title or (basename.replace("-", " ").replace("_", " ").strip().capitalize()
                      + ": benchmark report")
    run_file = "sample-run.json"

    content = _CONTENT_TEMPLATE
    for token, value in (("@@TITLE@@", title),
                         ("@@TITLE_LIT@@", _py_str(title)),
                         ("@@BASENAME_LIT@@", _py_str(basename)),
                         ("@@RUN_FILE_LIT@@", _py_str(run_file)),
                         ("@@SECTIONS@@", _render_sections(rows))):
        content = content.replace(token, value)

    readme = _README_TEMPLATE
    for token, value in (("@@TITLE@@", title), ("@@BASENAME@@", basename),
                         ("@@RUN_FILE@@", run_file)):
        readme = readme.replace(token, value)

    run_dir = os.path.join(target, "run")
    os.makedirs(run_dir, exist_ok=True)
    written = []
    for path, text in ((os.path.join(target, "content.py"), content),
                       (os.path.join(run_dir, run_file),
                        json.dumps(SAMPLE_RUN, indent=2) + "\n"),
                       (os.path.join(target, "README.md"), readme)):
        _write(path, text, force)
        written.append(path)
    return {"paths": written, "sections": len(rows), "target": target, "basename": basename,
            "run_dir": run_dir, "title": title}
