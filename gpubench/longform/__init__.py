"""Long-form report generation: the engine, not the narrative.

WHAT THIS IS. Everything needed to turn a set of authored sections plus a run's measurements into a
publishable document -- charts, tables, figures with table views, section ordering, renumbering,
cross-reference resolution, a contents page, a print stylesheet, PDF and DOCX export, contents
pagination, and a pre-publication redaction gate.

WHAT THIS IS NOT. It does not know what was measured, what the sections say, or what hardware
exists. A report supplies its own narrative as a CONTENT MODULE; this renders it. That separation is
the reason the engine can live in a general-purpose tool at all: the prose about one machine is not
reusable, and the machinery around it entirely is.

WHY IT LIVES IN THE TOOL. It was extracted from a report generator that sat beside one particular
report. That arrangement meant every future benchmark either reinvented document assembly or copied
a file, and -- more seriously -- the mechanisms that keep a report from contradicting itself were
invisible to anyone reading the tool. A derivation defect survived three published editions for
exactly that reason. Anything that decides what a reader sees belongs where it can be reviewed and
tested.

A content module supplies:

    TITLE          str
    BASENAME       str, the filename stem the report publishes under
    VERSION        str, the edition, which the filename carries: <BASENAME>-v<VERSION>.html
    SECTION_ORDER  list of title fragments, in reading order
    build(run_dir, out_dir) -> (figures, data)
    render(figures, data) -> body HTML, sections in authoring order
                   or render(figures, data, manifest=None), to be handed the claims manifest
                   this build already computed instead of computing a second copy of it

BASENAME and VERSION are read by report_stem(), which render_report() calls before build() runs.
For several editions they were documented here and read nowhere: the caller rebuilt the filename
from getattr defaults, so a module that declared neither still published, as report.html. Two
contract fields that nothing reads teach the next author that the contract is a comment, and the
report that followed went on to hardcode an arrival model the manifest was supposed to declare.

and, to arm the pre-render gate (required unless the caller passes --allow-ungated, see
run_claims_gate):

    MANIFEST       str, the filename to write the claims manifest to
    claims(figures, data) -> the claims manifest, as a dict

and the engine does the rest:

    from gpubench.longform import render_report
    html = render_report(content, run_dir, out_dir)
"""
import io
import json
import os
import re
import tempfile
from html import unescape as _unescape

from .css import PAGE_CSS
from .doc import assemble, contents, renumber, reorder_sections, resolve_refs, stat
from .svg import (esc, figure, fmt, frame, legend, lg, lin, marker, nice_ticks, polyline,
                  strip_style, svg_close, svg_open, table)

__all__ = [
    "PAGE_CSS", "assemble", "contents", "renumber", "reorder_sections", "resolve_refs", "stat",
    "esc", "figure", "fmt", "frame", "legend", "lg", "lin", "marker", "nice_ticks", "polyline",
    "strip_style", "svg_close", "svg_open", "table", "render_report",
    "render_companions", "run_claims_gate", "BLOCKED_GUIDANCE",
    "GATE_ABSENT", "GATE_INCOMPLETE", "GATE_PASS", "GATE_BLOCKED",
    "Rendered", "read_manifest", "check_declaration_floor", "KIND_EVIDENCE",
    "check_no_baseline_floor", "NO_BASELINE_MIN_UNIT_BEARING_PCT", "scope_to_document",
    "DOC_CHECKS", "stamp_draft", "stamp_docx_marker", "DRAFT_MARKER", "DRAFT_HEADLINE",
    "report_basename", "report_version", "report_stem",
    "check_output_directory", "declared_outputs", "FIGURE_DIR", "FIGURE_PNG_DIR",
    "visible_numerals", "html_numerals", "docx_text", "pdf_text", "judge_exports",
    "NUMERAL_EQUIVALENTS",
]

# Gate outcomes. This module reports them; the caller decides what each one costs. In the CLI only
# GATE_PASS publishes: absence used to render and exit 0, which made a fully gated build and a
# completely ungated one indistinguishable to anything reading the exit code.
GATE_ABSENT = "absent"          # the content module does not declare MANIFEST + claims()
GATE_INCOMPLETE = "incomplete"  # it declares one half of the pair, which is a wiring fault
GATE_PASS = "pass"
GATE_BLOCKED = "blocked"

# Distinguishes "no manifest was supplied to the gate" from "claims() returned None", which is a
# defect the gate has to report rather than mistake for an absent argument.
_UNSET = object()

# Printed verbatim when the gate blocks. The list is exhaustive on purpose: every legitimate
# response changes either the generator, the prose, the measurement, or the manifest's declared
# exceptions. Editing a measured value to satisfy a check is not on the list and never will be --
# it converts a report that disagrees with itself into a report that quietly disagrees with the
# machine, which is the strictly worse failure because nothing downstream can detect it.
BLOCKED_GUIDANCE = """\
RENDER BLOCKED -- no report file was written.

A report that fails verification should not exist as a file, because a file is the thing that
gets sent to people. The manifest was still written, so the findings above can be inspected
against it.

The permitted responses, all four of which are real fixes:

  1. FIX THE GENERATOR    -- the number printed is not the number the formula computes. Make the
                             code derive it instead of carrying a typed copy.
  2. FIX THE PROSE        -- a sentence states a figure that the manifest does not back, or cites
                             a claim key that does not exist. Cite the key, or reword the claim.
  3. RE-MEASURE           -- the measurement is genuinely out of date. Take a new run, point the
                             claim at it, and record the re-measurement in the changelog.
  4. DECLARE THE EXCEPTION -- the check is firing on something legitimate. Say so IN THE MANIFEST,
                             where the exception is reviewable: allow_literals for a numeral that
                             is not a measurement, blended + blend_note for a table that spans
                             runs, basis_conversion or unit_conversion for deliberate arithmetic,
                             a stated tolerance for a rounded derivation.

NEVER edit a measured value to make a check pass. If two numbers disagree, one of them is wrong
about the machine, and overwriting either one destroys the evidence of which.

To look at the failing draft anyway, re-run with --no-verify. That still runs the gate, still
writes the manifest and the findings, names the checks it overrode, and stamps the document
itself as a draft. It inspects a draft; it does not publish one."""

# Stamped into any document written while the gate did not pass. The marker goes in an HTML
# comment because the visible banner is the reader's warning and this one is the pipeline's: a
# build step, a mail rule or a grep can refuse to send a file that carries it. Both halves are
# needed. A banner alone is removable by anyone with an editor and invisible to a script, and a
# comment alone is invisible to the person the document was mailed to.
DRAFT_MARKER = "gpubench-draft-not-for-publication"
DRAFT_HEADLINE = "DRAFT, NOT FOR PUBLICATION."

# Inline styles rather than a stylesheet rule: the stamp has to survive being pasted into mail,
# converted to PDF with print CSS in force, or opened with the stylesheet stripped. The two
# print-adjust properties keep the red ground when a browser would drop backgrounds on paper.
_DRAFT_BANNER = (
    '<!-- %(marker)s: %(headline)s %(detail)s -->'
    '<div class="draftbanner" role="alert" style="background:#7f1d1d;color:#ffffff;'
    'border:2px solid #450a0a;border-radius:4px;padding:10px 14px;margin:0 0 20px 0;'
    '-webkit-print-color-adjust:exact;print-color-adjust:exact">'
    '<p style="margin:0;color:#ffffff;font-size:0.95rem">'
    '<strong>%(headline)s</strong> %(detail)s</p></div>'
)


def stamp_draft(html, detail, headline=DRAFT_HEADLINE):
    """Mark a rendered document as one the gate did not pass, visibly and greppably.

    Idempotent: a document already carrying the marker is returned unchanged, so stamping the
    report and its companions from one place cannot double-band a page.
    """
    if DRAFT_MARKER in html:
        return html
    banner = _DRAFT_BANNER % {"marker": DRAFT_MARKER, "headline": esc(headline),
                             "detail": esc(detail)}
    # Inside <main>, above the title page, so the first thing a reader sees is the warning. The
    # <body> fallback covers a document assembled without the standard shell.
    for anchor in ("<main>", "<body>"):
        at = html.find(anchor)
        if at != -1:
            cut = at + len(anchor)
            return html[:cut] + banner + html[cut:]
    return banner + html


def stamp_docx_marker(path, marker=DRAFT_MARKER, detail=""):
    """Put the pipeline-readable half of the draft stamp inside a .docx. Returns True on success.

    WHY THIS EXISTS. The HTML carries two stamps: a banner a reader sees and a comment marker a
    build step can refuse on. The DOCX carried only the banner, because an HTML comment does not
    survive conversion, and the DOCX and the PDF are the two formats that actually get mailed. A
    stamp only a human can see is not a control; it is a note.

    A .docx is an OPC zip, so the marker goes in docProps/core.xml as the document's keywords,
    which is where every Office-aware reader already looks. That part is written UNCOMPRESSED on
    purpose: a mail rule or a pre-send hook is far more likely to run `grep -a` over the file than
    to parse OOXML, and a deflated part is invisible to grep.

    Returns False, and changes nothing, when the file has no core properties part to write into.
    The caller must treat that as a failure to stamp rather than as a stamped file: silently
    shipping an unmarked draft is the exact outcome this function exists to prevent.
    """
    import zipfile

    if not os.path.exists(path):
        return False
    try:
        with zipfile.ZipFile(path, "r") as zin:
            infos = zin.infolist()
            blobs = dict((i.filename, zin.read(i.filename)) for i in infos)
    except (zipfile.BadZipFile, IOError, OSError):
        return False
    core = "docProps/core.xml"
    if core not in blobs:
        return False
    xml = blobs[core].decode("utf-8", "replace")
    text = esc(("%s %s" % (marker, detail)).strip())
    if "<cp:keywords>" in xml:
        xml = re.sub(r"(?s)<cp:keywords>.*?</cp:keywords>",
                     "<cp:keywords>%s</cp:keywords>" % text, xml, count=1)
    elif "</cp:coreProperties>" in xml:
        xml = xml.replace("</cp:coreProperties>",
                          "<cp:keywords>%s</cp:keywords></cp:coreProperties>" % text, 1)
    else:
        return False
    blobs[core] = xml.encode("utf-8")

    tmp = path + ".stamping"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in infos:
            how = zipfile.ZIP_STORED if info.filename == core else zipfile.ZIP_DEFLATED
            zout.writestr(info.filename, blobs[info.filename], compress_type=how)
    os.replace(tmp, path)
    with io.open(path, "rb") as f:
        return marker.encode("utf-8") in f.read()


# Evidence ladder for a claim's kind, used by the declaration floor. A kind that moves DOWN this
# ladder leaves the printed number untouched while making it less falsifiable, which is the quiet
# way a measurement becomes an assertion. measured and derived share the top rung deliberately: a
# derived claim is recomputed by the gate from measured inputs, so it is exactly as checkable as
# they are. published and supplied are somebody else's number. assumption and projection are not
# measurements at all.
KIND_EVIDENCE = {"measured": 3, "derived": 3, "published": 2, "supplied": 2,
                 "projection": 1, "assumption": 1}

# Changelog fields that waive a declaration regression. A row naming the id is the whole point:
# dropping a claim is legitimate and has to be a sentence someone wrote, not an absence.
CHANGELOG_WAIVER_FIELDS = ("claims_changed", "claims_remeasured", "claims_removed",
                           "prose_removed", "figures_removed")


def read_manifest(path):
    """Load a manifest from disk. Returns the dict, or None when the file is not there.

    Raises ValueError when the file exists and is not a manifest. A baseline that cannot be read
    must not degrade quietly into no baseline at all: "no baseline" is precisely the state in
    which a shrunken manifest sails through, so the failure has to be loud.
    """
    if not path or not os.path.exists(path):
        return None
    try:
        with io.open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except (IOError, OSError, ValueError) as exc:
        raise ValueError("cannot read %s as a claims manifest: %s" % (path, exc))
    if not isinstance(doc, dict):
        raise ValueError("%s holds a %s, not a claims manifest object"
                         % (path, type(doc).__name__))
    return doc


def _sequence_ids(items, label):
    """Ids of a manifest sequence (prose blocks, figures), with a stable stand-in for the unnamed.

    An unnamed block still has to be countable, otherwise deleting one is invisible to the floor.
    The stand-in is positional, so a reordering of unnamed blocks reads as a change; that is the
    conservative direction, and naming the blocks is the fix.
    """
    out = []
    for i, item in enumerate(items or []):
        ident = (item or {}).get("id") if isinstance(item, dict) else None
        out.append(str(ident).strip() if ident and str(ident).strip()
                   else "%s[%d]" % (label, i))
    return out


def _asserted_ids(items):
    """Ids of the prose blocks that carry a CHECKABLE assertion, in _sequence_ids' naming.

    A prose block's `assert` is the only falsifiable thing in it: the id is a label and the text is
    prose no check can read. Popping `assert` from every block leaves the ids, the sentences and
    the counts exactly as they were, so a floor that counts blocks sees nothing at all.
    """
    ids = _sequence_ids(items, "prose")
    return [ids[i] for i, block in enumerate(items or [])
            if isinstance(block, dict) and block.get("assert")]


# With no previous edition there is no floor to fall from, and a first-ever edition genuinely has
# none. The floor then has to come off the ARTIFACT: how much of what the document prints traces
# to a claim. A manifest that declares almost nothing scores near zero here however cleanly its own
# checks pass, which is the property the missing baseline used to hand away for free. It is not
# 100% because a first edition is the one most likely to be still declaring its allowances, and a
# floor nobody can clear gets removed rather than met.
NO_BASELINE_MIN_UNIT_BEARING_PCT = 90.0


def check_no_baseline_floor(manifest, coverage):
    """The declaration floor for the case check_declaration_floor cannot see: no previous edition.

    "No baseline" was a silent exemption, and it is the state every other check in the gate is
    weakest in: a manifest cut down to one claim, no prose, no figures and both coverage floors at
    zero passed with exit 0 purely because there was nothing to compare it against. So absence of a
    baseline is recorded as a finding in its own right (it goes in the findings JSON, where the
    build log cannot lose it) and the artifact supplies the floor instead.

    `coverage` is verify.Findings.coverage from this build's own run over the rendered document.
    Returns findings in verify.py's shape.
    """
    out = []
    claims = manifest.get("claims") or {}
    out.append({
        "severity": "warn", "check": "A10", "baseline": False,
        "message": "NO BASELINE: no previous edition was available, so the declaration floor "
                   "(claim, prose, assertion and figure counts, and each claim's kind) could not "
                   "be checked. This edition declares %d claim(s), %d prose block(s) of which %d "
                   "assert something, and %d figure(s); nothing but this build says whether that "
                   "is more or less than last time."
                   % (len(claims), len(manifest.get("prose") or []),
                      len(_asserted_ids(manifest.get("prose"))),
                      len(manifest.get("figures") or [])),
    })
    pct = (coverage or {}).get("unit_bearing")
    if pct is None:
        out.append({
            "severity": "error", "check": "A10", "baseline": False,
            "message": "no baseline AND no rendered document was measured, so nothing in this "
                       "build read either the previous edition or the artifact. Every check that "
                       "ran read only the manifest, and a manifest is written by the same "
                       "generator as the prose. Supply the rendered document to the gate, or "
                       "supply a baseline.",
        })
        return out
    if pct + 1e-9 < NO_BASELINE_MIN_UNIT_BEARING_PCT:
        out.append({
            "severity": "error", "check": "A10", "baseline": False,
            "message": "no baseline, and only %.1f%% of the unit-bearing numerals this document "
                       "prints trace to a claim, below the %.1f%% an edition with nothing to be "
                       "compared against must reach. With no previous edition the manifest's own "
                       "floors are self-set, so they are not the floor here: the document is. "
                       "Declare the missing numerals as claims, or allow them in coverage.allow "
                       "with a reason."
                       % (pct, NO_BASELINE_MIN_UNIT_BEARING_PCT),
        })
    return out


def check_declaration_floor(manifest, previous):
    """A10: an edition may not DECLARE less than the one before it without saying so.

    WHY THIS CHECK EXISTS. Every other check in the gate reads the manifest as the standard the
    document is held to, which makes the cheapest way to pass a clean run declaring less. A
    manifest cut down to one claim reported "1 claim(s), 0 warning(s)" and shipped the same wrong
    number in the body: nothing else in the gate could see it, because evidence a manifest omits
    leaves no trace inside that manifest. The previous edition is the only floor available.

    Regressions are waived by a changelog row that NAMES the id, which is also what makes
    verify.check_provenance's re-measurement checks work. Returns findings in verify.py's shape.
    """
    if not isinstance(previous, dict):
        return []
    out = []

    def error(message, **extra):
        out.append(dict({"severity": "error", "check": "A10", "message": message}, **extra))

    logged = set()
    for entry in manifest.get("changelog") or []:
        if not isinstance(entry, dict):
            continue
        for field in CHANGELOG_WAIVER_FIELDS:
            logged |= {str(x) for x in (entry.get(field) or [])}

    def floor(label, now_ids, prev_ids, now_count, prev_count):
        gone = set(prev_ids) - set(now_ids)
        dropped = sorted(gone - logged)
        if dropped:
            error("%d %s declared by the previous edition are gone from this one with no "
                  "changelog row naming them: %s%s. The count fell from %d to %d. Declaring "
                  "less is how a manifest passes more cleanly than the honest one."
                  % (len(dropped), label, ", ".join(dropped[:8]),
                     "..." if len(dropped) > 8 else "", prev_count, now_count),
                  dropped=dropped)
        elif not gone and now_count < prev_count:
            # Reachable only when ids repeat, so the sets cannot see the loss: a count that fell
            # while every id survived. `not gone` rather than `not dropped`, because a drop the
            # changelog waived has been explained and must not come back as a count complaint.
            error("the %s count fell from %d to %d though every id the previous edition named is "
                  "still present. Duplicate or blank ids hide what went; name them."
                  % (label, prev_count, now_count))

    now_claims = manifest.get("claims") or {}
    prev_claims = previous.get("claims") or {}
    floor("claim(s)", now_claims.keys(), prev_claims.keys(), len(now_claims), len(prev_claims))
    floor("prose block(s)",
          _sequence_ids(manifest.get("prose"), "prose"),
          _sequence_ids(previous.get("prose"), "prose"),
          len(manifest.get("prose") or []), len(previous.get("prose") or []))
    floor("figure(s)",
          _sequence_ids(manifest.get("figures"), "figure"),
          _sequence_ids(previous.get("figures"), "figure"),
          len(manifest.get("figures") or []), len(previous.get("figures") or []))
    # A12's declarations are on the floor for the same reason the others are: deleting a verdict
    # is how a contradiction the gate already caught comes back silently. The check disappears
    # with the declaration, and nothing inside the manifest records that it ever existed.
    floor("declared verdict(s)",
          _sequence_ids(manifest.get("verdicts"), "verdict"),
          _sequence_ids(previous.get("verdicts"), "verdict"),
          len(manifest.get("verdicts") or []), len(previous.get("verdicts") or []))

    # Count ASSERTIONS, not blocks. Popping the "assert" key from all seven prose blocks while
    # keeping their ids and their text left every count above unchanged and disarmed the only part
    # of a prose block a check can fail. The block survives; what it promised does not.
    now_asserted = _asserted_ids(manifest.get("prose"))
    prev_asserted = _asserted_ids(previous.get("prose"))
    gone_asserts = set(prev_asserted) - set(now_asserted)
    lost = sorted(gone_asserts - logged)
    if lost:
        error("%d prose block(s) asserted something checkable in the previous edition and assert "
              "nothing in this one, with no changelog row naming them: %s%s. The blocks, their "
              "ids and their sentences all survive, so every count in this floor stayed level "
              "while the assertions fell from %d to %d."
              % (len(lost), ", ".join(lost[:8]), "..." if len(lost) > 8 else "",
                 len(prev_asserted), len(now_asserted)),
              assertions_removed=lost)
    elif not gone_asserts and len(now_asserted) < len(prev_asserted):
        # `not gone_asserts` rather than `not lost`: a loss the changelog waived has been explained
        # and must not come back as a count complaint. Reachable only when ids repeat.
        error("the prose assertion count fell from %d to %d though every asserting block the "
              "previous edition named still asserts. Duplicate or blank prose ids hide which one "
              "went; name them." % (len(prev_asserted), len(now_asserted)))

    demoted = []
    for claim_id, claim in now_claims.items():
        before = (prev_claims.get(claim_id) or {}).get("kind")
        after = (claim or {}).get("kind")
        if not before or before == after or claim_id in logged:
            continue
        if KIND_EVIDENCE.get(after, 0) < KIND_EVIDENCE.get(before, 0):
            demoted.append("%s (%s -> %s)" % (claim_id, before, after))
    if demoted:
        error("%d claim(s) rest on weaker evidence than in the previous edition, with no changelog "
              "row: %s%s. The printed number does not move when this happens, so nothing else in "
              "the document shows it."
              % (len(demoted), ", ".join(sorted(demoted)[:8]), "..." if len(demoted) > 8 else ""),
              demoted=sorted(demoted))
    return out


def render_companions(content, figures, data, warn=None):
    """Optional companion pages: appendices better shipped beside a report than inside it.

    A long-form report usually carries material that is genuinely valuable and genuinely not part
    of the argument -- a method primer, a harness walkthrough, a change log. Left in the body it
    inflates the page count and is the least-read part of the document; deleted, something worth
    keeping is lost. A companion page is the third option.

    A content module opts in by defining:

        COMPANIONS = {"method-primer.html": ("Page title", "render_fn_name")}

    where the named function takes (figures, data) and returns section HTML. Each page goes through
    the same assembly as the report, so it keeps the stylesheet, the numbering and the contents.
    Returns {filename: html}.
    """
    spec = getattr(content, "COMPANIONS", None) or {}
    out = {}
    for filename, (title, fn_name) in spec.items():
        fn = getattr(content, fn_name, None)
        if not callable(fn):
            raise SystemExit("companion %r names %r, which the content module does not define"
                             % (filename, fn_name))
        body = fn(figures, data)
        order = getattr(content, "COMPANION_ORDER", {}).get(filename, [])
        out[filename] = assemble(body, title, order, warn=warn)
    return out


# The checks whose subject is A DOCUMENT rather than the manifest: they can be run over any
# rendered artifact this build writes, because their input is the text that shipped. Everything
# else in verify() reads the manifest, and the manifest is one object however many documents it
# backs, so re-running those per companion would report the same finding several times.
DOC_CHECKS = ("A5", "A6", "F1", "F3", "F4")

_FIGURE_ID = re.compile(r'(?is)<figure\b[^>]*\bid\s*=\s*"([^"]+)"')
_TABLE_ID = re.compile(r'(?is)<table\b[^>]*\bid\s*=\s*"([^"]+)"')


def scope_to_document(manifest, html):
    """A view of the manifest whose figures and tables are the ones THIS document contains.

    F3 and F4 ask whether a declared figure rendered its values. Asked of a companion page about
    the report's figures the answer is "no" for every one of them, which is true and useless. The
    scoping is read off the artifact (which figure ids and table ids are actually in this
    document), so it cannot be written to exempt anything: a figure the companion DOES render
    with an empty table view is still an error, and a table it DOES print is still checked cell by
    cell. A5, A6 and F1 are left at full strength, because a fabricated numeral is a fabricated
    numeral wherever it is printed.
    """
    present = set(_FIGURE_ID.findall(html or ""))
    table_ids = set(_TABLE_ID.findall(html or ""))
    scoped = dict(manifest)
    scoped["figures"] = [fig for fig in (manifest.get("figures") or [])
                         if isinstance(fig, dict) and fig.get("id") in present]
    tables = manifest.get("tables") or {}
    scoped["tables"] = dict((k, v) for k, v in tables.items()
                            if k in present or k in table_ids)
    return scoped


# ---- R21: nothing may appear in the output directory that nobody declared -------------------
#
# build(run_dir, out_dir) is handed the output directory, which it needs: a report's figures are
# written there. Nothing enumerated it afterwards, so a content module could drop ANY file beside
# the report and it shipped without a check ever reading it. That is the companion-page failure
# again, one level lower: the gate held jurisdiction over the documents it was handed and none at
# all over the directory they went into.
#
# The engine knows two things a file can legitimately be: something IT writes (the report, the
# index copy, the companions, the manifest, the findings, the exports), or something the MANIFEST
# declares. The second is what keeps figures legitimate without a list of exceptions: a figure the
# manifest declares as `figures[].id` publishes as figures/<id>.svg, which is the same convention
# docx_export reads them back by, and a run's `artifact` is the evidence file that run points at.
# Anything else is undeclared, and undeclared is the whole finding.
FIGURE_DIR = "figures"
FIGURE_PNG_DIR = os.path.join("figures", "png")


def declared_outputs(out_dir, manifest):
    """Absolute paths in out_dir that the MANIFEST accounts for: figures, and run artefacts."""
    root = os.path.abspath(out_dir or ".")
    out = set()
    for fig in (manifest or {}).get("figures") or []:
        fid = (fig or {}).get("id") if isinstance(fig, dict) else None
        if not fid:
            continue
        out.add(os.path.join(root, FIGURE_DIR, "%s.svg" % fid))
        # The PNG the DOCX export renders from that SVG. It is the engine's own file, but it is
        # named after a declared figure and lives beside it, so it is derived here for the same
        # reason: one rule, read off the declaration, rather than a second list to keep in step.
        out.add(os.path.join(root, FIGURE_PNG_DIR, "%s.png" % fid))
    for run in ((manifest or {}).get("runs") or {}).values():
        artifact = (run or {}).get("artifact") if isinstance(run, dict) else None
        if artifact:
            out.add(os.path.abspath(os.path.join(root, str(artifact))))
    return out


def check_output_directory(out_dir, expected, manifest):
    """A11: every file in the output directory is one the engine writes or the manifest declares.

    `expected` is the set of paths THIS build intends to write, which the caller knows and the
    engine does not: which exports were asked for, what the report and its companions are called.

    Returns findings in verify.py's shape. They are warnings, not errors: an unaccounted file is
    proof that something reached the output directory unjudged, but not proof that what it says is
    wrong, and the honest severity for "nobody looked at this" is the one --warnings-as-errors
    turns into a block for a final edition. Every such file is NAMED, because "1 undeclared file"
    is not actionable and "editions.html" is.
    """
    root = os.path.abspath(out_dir or ".")
    if not os.path.isdir(root):
        return []
    allowed = set(os.path.abspath(p) for p in (expected or ()))
    allowed |= declared_outputs(root, manifest)
    stray = []
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            path = os.path.abspath(os.path.join(dirpath, name))
            if path not in allowed:
                stray.append(os.path.relpath(path, root).replace("\\", "/"))
    if not stray:
        return []
    return [{
        "severity": "warn", "check": "A11", "undeclared": sorted(stray),
        "message": "%d file(s) in the output directory are neither written by the engine nor "
                   "declared by the manifest, so nothing judged them and they publish beside the "
                   "report anyway: %s%s. build(run_dir, out_dir) is handed this directory, which "
                   "is how a document can be written past the gate entirely. Declare a page as a "
                   "COMPANION so it is judged, declare a figure as figures[].id, or write it "
                   "somewhere that is not published."
                   % (len(stray), ", ".join(sorted(stray)[:8]),
                      "..." if len(stray) > 8 else ""),
    }]


# ---- Every published format is a document, and only one of them was ever judged --------------
#
# WHAT WAS WRONG. The build proves the HTML on disk is the HTML the gate judged, and prints
# matching sha256 to say so. The PDF and the DOCX are exported AFTERWARDS and nothing checked them
# at all. Byte-identity of the HTML does not carry across the conversion either: the DOCX exporter
# DECODES character references that the gate scanned as raw text, so "&#57;&#44;&#53;" is three
# numerals to one reading and one number to the other. Those two formats are what people open and
# email, so "the document you received was verified" was true only of the format nobody opens.
#
# WHAT IS CHECKED. Not the bytes, which cannot match across formats, but what each format PRINTS:
# every numeral visible in an export must be one the judged HTML also printed. That is the same
# jurisdiction A5 and A6 hold over the HTML, extended to the artifacts that actually get sent. The
# direction is deliberate. An export legitimately carries LESS (the DOCX takes figures as images,
# so their axis labels do not come back out as text); an export carrying MORE is a number that
# entered the document after the last thing that could check it.
#
# Numerals only. A full text diff across three renderers reports hyphenation and ligatures forever
# and would be switched off within a week; a numeral is the thing this tool exists to protect and
# the thing a conversion bug actually corrupts.

# Two characters that mean the same thing to a reader and different things to a scanner. A PDF
# renders a non-breaking space and a typographic minus; the HTML source carries a plain space and
# a hyphen. Neither difference is a difference in what was printed.
NUMERAL_EQUIVALENTS = {0x00a0: " ", 0x2212: "-"}


def visible_numerals(text):
    """{numeral: [contexts]} for every numeral a reader can see in `text`.

    The normalisation is the one verify.check_coverage applies to the rendered document, so the
    set an export is compared against is the set the gate scored: thousands separators grouped,
    currency marks and commas removed from the key, the printed precision kept. "30" and "30.0"
    are different numerals here on purpose: a conversion that reprints a figure at a different
    precision has changed what the reader is told.
    """
    from .. import verify as verify_mod

    body = text.replace("&nbsp;", " ").translate(NUMERAL_EQUIVALENTS)
    body, _sites = verify_mod.merge_space_groups(body)
    out = {}
    for match in verify_mod.ANY_NUMERAL.finditer(body):
        token = match.group(1).lstrip(verify_mod.CURRENCY_MARKS).replace(",", "")
        out.setdefault(token, []).append(
            verify_mod.context_of(body, match.start(), match.end()))
    return out


def html_numerals(html):
    """The numerals the judged HTML prints, read the way the gate reads it."""
    from .. import verify as verify_mod

    return visible_numerals(verify_mod.visible_text(html))


_DOCX_PARAGRAPH = re.compile(r"(?s)<w:p(?:\s[^>]*)?>(.*?)</w:p>")
# A text run, or one of the three empty elements that put a gap between two runs. Without the
# second half of this alternation a line break between "10" and "20" would concatenate them into
# one numeral that neither document contains.
_DOCX_RUN = re.compile(r"(?s)<w:t(?:\s[^>]*)?>(.*?)</w:t>|<w:(?:br|tab|cr)\b[^>]*>")


def docx_text(path):
    """The text Word shows, read out of the OPC package. Pure stdlib: a .docx is a zip.

    Runs inside one paragraph are joined with NOTHING, because that is how Word lays them out: a
    bold word mid-sentence is its own run and inserting a separator there would invent a gap the
    reader never sees, and split numerals that a bold span happens to cross. Paragraphs are
    separated by the same sentinel verify.py uses, which is not whitespace, so no scanner can read
    across the boundary between a table cell and the next.

    Only word/document.xml is read. The document properties are metadata rather than text, and a
    DOCX exported by this tool puts nothing in headers or footers.
    """
    import zipfile

    from .. import verify as verify_mod

    try:
        with zipfile.ZipFile(path) as zf:
            if "word/document.xml" not in zf.namelist():
                raise ValueError(
                    "%s has no word/document.xml, so it is not a Word document" % path)
            xml = zf.read("word/document.xml").decode("utf-8", "replace")
    except zipfile.BadZipFile as exc:
        # ValueError, not BadZipFile: the caller's question is "can this export be read back and
        # checked", and a file that is not a zip at all answers it the same way as one that is a
        # zip holding no document.
        raise ValueError("%s is not readable as a Word package: %s" % (path, exc))
    paragraphs = []
    for body in _DOCX_PARAGRAPH.findall(xml):
        parts = [_unescape(m.group(1)) if m.group(1) is not None else " "
                 for m in _DOCX_RUN.finditer(body)]
        paragraphs.append("".join(parts))
    return (" %s " % verify_mod.BLOCK_BOUNDARY).join(paragraphs)


def pdf_text(path, drop=()):
    """The text a reader can extract from a PDF, page by page. Returns (text, pages, dropped).

    `drop` are compiled patterns for text the RENDERER adds rather than the document: the page
    footer's own numbering. They are removed here, and counted, so that the removal is a stated
    quantity in the build log rather than a silent exemption, because an allowance nobody sees is
    indistinguishable from a check that does not run.
    """
    fitz = _require_fitz()
    try:
        doc = fitz.open(path)
    except RuntimeError as exc:
        # Same reasoning as docx_text: "this export cannot be read back" is one answer however the
        # underlying library spells the failure, and the caller has to treat it as unjudged.
        raise ValueError("%s is not readable as a PDF: %s" % (path, exc))
    try:
        pages = [page.get_text() for page in doc]
    finally:
        doc.close()
    from .. import verify as verify_mod

    text = (" %s " % verify_mod.BLOCK_BOUNDARY).join(pages)
    dropped = 0
    for pattern in drop or ():
        text, count = pattern.subn(" %s " % verify_mod.BLOCK_BOUNDARY, text)
        dropped += count
    return text, len(pages), dropped


def _require_fitz():
    from . import pdf_export

    return pdf_export._require("fitz", "read the text back out of an exported PDF")


def judge_exports(judged_html, exports, accounted=None):
    """Judge every OTHER format this build published against the HTML the gate judged.

    `exports` is [(label, text)]: the visible text of each non-HTML artifact. `accounted` is
    {reason: iterable of numerals} for figures an export legitimately prints that the document
    does not: the contents page numbers pagination inserts, and the counts inside a draft stamp
    applied after judging. Each one is NAMED and printed with the result. An allowance that is not
    stated is the same thing as no check.

    Returns
        {"judged": {"distinct": n, "printed": n},
         "formats": [{"label":, "distinct":, "printed":, "unmatched": {numeral: [contexts]}}],
         "accounted": {reason: [numerals]},
         "unmatched": total number of numerals no judged document printed}
    """
    judged = html_numerals(judged_html)
    allowed = {}
    for reason, tokens in sorted((accounted or {}).items()):
        allowed[reason] = sorted({str(t) for t in tokens})
    every_allowance = set()
    for tokens in allowed.values():
        every_allowance |= set(tokens)
    formats = []
    total = 0
    for label, text in exports:
        printed = visible_numerals(text)
        unmatched = dict((token, contexts) for token, contexts in printed.items()
                         if token not in judged and token not in every_allowance)
        total += len(unmatched)
        formats.append({"label": label, "distinct": len(printed),
                        "printed": sum(len(v) for v in printed.values()),
                        "unmatched": unmatched})
    return {"judged": {"distinct": len(judged),
                       "printed": sum(len(v) for v in judged.values())},
            "formats": formats, "accounted": allowed, "unmatched": total}


def run_claims_gate(content, figures, data, out_dir, rendered_html=None,
                    warnings_as_errors=False, previous=None, manifest=_UNSET,
                    companions=None, expected_outputs=None):
    """Verify the claims manifest, and own the only copy of it, BEFORE anything is written.

    The verifier existed for a while as a command someone could choose to run, which is the same
    as not having it: the one edition that needed it was the one where nobody thought to. So the
    render path calls it, and a blocked report never becomes a file.

    A content module opts in by defining two things:

        MANIFEST = "claims.json"                 # where to write the manifest
        def claims(figures, data): -> dict       # the manifest, schema "claims/1"

    Modules that define neither are not gated, and the caller decides what that is worth; the CLI
    treats it as an error unless --allow-ungated is given, because an ungated build that exits 0
    is indistinguishable from a gated one to anything reading the exit code. Modules that define
    exactly one half are a wiring fault, reported as such, because a half-armed gate reads as an
    armed one.

    `manifest`, when given, is the dict this build already obtained from claims(). Pass it. The
    gate then WRITES THE ONLY COPY: two independently produced copies at one path mean the file on
    disk need not be the file that was judged, which is true the moment claims() is not
    deterministic or raises the second time. Omit it and the gate calls claims() itself, which is
    what a direct caller wants.

    `previous`, when given, is the previous edition's manifest. It reaches two checks that cannot
    work without it: verify.check_provenance (a value that moved with no changelog row) and
    check_declaration_floor (an edition that declares less than the last one). When it is absent,
    check_no_baseline_floor runs instead: absence of a baseline is recorded as a finding and the
    artifact supplies the floor, because "no baseline" was otherwise a silent exemption.

    `companions` is {filename: html} for every OTHER document this build is about to write. EVERY
    RENDERED ARTIFACT MUST BE JUDGED BEFORE IT REACHES DISK. The gate held jurisdiction over the
    report alone while the companion page beside it was rendered, stamped and written unchecked,
    so the same fabricated headline figures that A5 blocked in the report shipped untouched one
    click away. Each companion is judged by the checks whose subject is a document (DOC_CHECKS),
    against a manifest view scoped to the figures and tables that companion actually renders.

    `expected_outputs` are the paths this build intends to write. Supplying them arms A11, which
    enumerates out_dir and reports every file that is neither one of those nor declared by the
    manifest: build() is handed the output directory, so a content module can write a document
    straight past the gate. Omitting it leaves A11 unarmed, which is right for a caller that does
    not yet know what it will write and wrong for one that does. The CLI passes its list.

    Returns a dict with:
        status         GATE_ABSENT | GATE_INCOMPLETE | GATE_PASS | GATE_BLOCKED
        findings       list of finding dicts (verify.py shape), newest run
        errors/warnings  counts
        manifest       the manifest dict that was judged, or None
        baseline       True when a previous edition was compared against, False when none was
        manifest_path  where the manifest was written, or None
        findings_path  where the findings JSON was written, or None
        message        one line explaining the status

    The manifest is written even when verification fails -- a gate that hides the evidence it
    judged on cannot be argued with, and the author needs to read it to fix anything.
    """
    from .. import verify as verify_mod

    def unarmed(status, message):
        return {"status": status, "findings": [], "errors": 0, "warnings": 0,
                "manifest": None, "baseline": previous is not None, "coverage": None,
                "companion_coverage": {}, "manifest_path": None, "findings_path": None,
                "message": message}

    fname = getattr(content, "MANIFEST", None)
    fn = getattr(content, "claims", None)
    if not fname and not callable(fn):
        return unarmed(GATE_ABSENT,
                       "content module declares no MANIFEST/claims(); nothing checks its numbers")
    if not fname or not callable(fn):
        missing, present = ("claims(figures, data)", "MANIFEST") if fname else (
            "MANIFEST", "claims(figures, data)")
        return unarmed(GATE_INCOMPLETE,
                       "content module defines %s but not %s. The pre-render gate needs both, "
                       "and a half-wired gate looks armed while checking nothing."
                       % (present, missing))

    if manifest is _UNSET:
        manifest = fn(figures, data)
    if not isinstance(manifest, dict):
        return unarmed(GATE_INCOMPLETE,
                       "claims(figures, data) returned %s, not a manifest dict"
                       % type(manifest).__name__)

    out_dir = os.path.abspath(out_dir or ".")
    os.makedirs(out_dir, exist_ok=True)
    manifest_path = os.path.join(out_dir, str(fname))
    with io.open(manifest_path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(manifest, f, indent=2, sort_keys=True, default=str)

    # The rendered-document checks (F1: entities surfacing as visible text) need the document, and
    # we do not have a file yet by design. A throwaway copy in the temp dir is not the report: it
    # is never in out_dir, never named like the report, and gone before this returns.
    import pathlib

    def judge(doc_html, doc_manifest, doc_previous):
        """Run verify() over one in-memory document, via a throwaway file it never keeps."""
        tmp_path = None
        try:
            if doc_html is not None:
                fd, tmp_path = tempfile.mkstemp(suffix=".html", prefix="gpubench-gate-")
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(doc_html)
            # manifest_dir is where the manifest was just written, which is what a run's declared
            # "artifact" path is relative to. Without it the gate's artefact cross-check has no
            # root to resolve against and degrades to an unfalsifiable declaration.
            return verify_mod.verify(doc_manifest, doc_previous,
                                     pathlib.Path(tmp_path) if tmp_path else None,
                                     manifest_dir=pathlib.Path(out_dir))
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    findings = judge(rendered_html, manifest, previous)

    # Every OTHER document this build is about to write, judged by the checks that read a document.
    # A finding already raised against the report is not repeated; anything new is named with the
    # page it came from, because "A5 fired" and "A5 fired on the primer" are different bugs.
    seen = set((i["check"], i["message"]) for i in findings.items)
    companion_coverage = {}
    for name in sorted(companions or {}):
        chtml = (companions or {})[name]
        cf = judge(chtml, scope_to_document(manifest, chtml), None)
        companion_coverage[name] = getattr(cf, "coverage", None)
        for item in cf.items:
            if item.get("check") not in DOC_CHECKS:
                continue
            if (item["check"], item["message"]) in seen:
                continue
            item = dict(item, document=name,
                        message="companion %s: %s" % (name, item["message"]))
            findings.items.append(item)

    # The declaration floor runs here rather than inside verify(): it needs the previous edition,
    # and locating that is the gate's job. Appended before the findings file is written, so the
    # file on disk is the whole verdict. With no previous edition the floor comes off the artifact
    # instead, and the absence is recorded rather than assumed harmless.
    if not isinstance(previous, dict):
        findings.items.extend(
            check_no_baseline_floor(manifest, getattr(findings, "coverage", None)))
    else:
        findings.items.extend(check_declaration_floor(manifest, previous))

    # A11 runs here for the same reason: it needs the output directory as build() left it, and
    # only the caller knows which files this build set out to write. It is appended before the
    # findings file, so the file on disk is still the whole verdict.
    if expected_outputs is not None:
        findings.items.extend(check_output_directory(out_dir, expected_outputs, manifest))

    findings_path = os.path.splitext(manifest_path)[0] + "-findings.json"
    with io.open(findings_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(findings.items, indent=2, default=str))

    blocked = bool(findings.errors) or (warnings_as_errors and bool(findings.warnings))
    n_err, n_warn = len(findings.errors), len(findings.warnings)
    if blocked and not findings.errors:
        msg = "%d warning(s), treated as errors by --warnings-as-errors" % n_warn
    elif blocked:
        msg = "%d error(s), %d warning(s)" % (n_err, n_warn)
    else:
        # The counts of what was DECLARED are part of the verdict, not decoration: the failure this
        # gate could not previously see was a manifest that shrank, and a log line saying
        # "1 claim(s)" beside a 40-page report is the tell.
        # The counts stay in their established order and wording, with the new ones appended: the
        # line is parsed by tests and by build logs, and moving a field is a silent breakage.
        msg = ("manifest verified: %d claim(s), %d prose block(s), %d figure(s), "
               "%d prose assertion(s), %d document(s) judged, %d warning(s)" % (
                   len(manifest.get("claims") or {}), len(manifest.get("prose") or []),
                   len(manifest.get("figures") or []),
                   len(_asserted_ids(manifest.get("prose"))),
                   1 + len(companions or {}), n_warn))
    return {"status": GATE_BLOCKED if blocked else GATE_PASS,
            "findings": findings.items, "errors": n_err, "warnings": n_warn,
            "manifest": manifest, "baseline": isinstance(previous, dict),
            # What the numeral checks had jurisdiction over. Carried out of here because a caller
            # that reprints the findings has to reprint the scope with them: the same "0 errors"
            # over a manifest that asserts nothing and one that asserts everything is the exact
            # confusion this gate exists to prevent.
            "coverage": getattr(findings, "coverage", None),
            # One entry per companion, so a caller can print what each page was held to. A
            # companion with 40% coverage and no findings is a page nobody checked, and that has
            # to be visible without opening the findings file.
            "companion_coverage": companion_coverage,
            "manifest_path": manifest_path, "findings_path": findings_path, "message": msg}


# ---- BASENAME and VERSION: the two contract fields the engine used to document and not read ----
#
# HOW THE FILENAME IS ACTUALLY DERIVED. A build publishes <BASENAME>-v<VERSION>.html, plus an
# index.html copy of it. That formula was reassembled at the call site out of getattr defaults,
# "report" for an absent BASENAME and no version at all for an absent VERSION, so every way of
# getting the declaration wrong produced a differently named file and not one word about it: a
# misspelled attribute published as report.html, and an edition nobody bumped published straight
# over its predecessor. Reading the declarations here is what makes them contract rather than
# commentary, and keeping the formula here is what stops the name drifting between callers.
#
# A stem, not a path. It is joined to the output directory, so a separator or a parent reference
# in it writes the report somewhere no build log names.
_STEM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# An edition, not a label: digits and dots, with an optional pre-release or build suffix.
_VERSION_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)*(?:[.+-][A-Za-z0-9.+-]+)?$")

_CONTRACT_REF = "The contract is at the top of gpubench/longform/__init__.py."


def report_basename(content, override=None):
    """The filename stem a report publishes under: the module's BASENAME, or an explicit override.

    `override` is for a caller that lets an operator name the output (the CLI's --basename). It
    replaces the value, not the obligation to declare one, so a module with no BASENAME is a fault
    whichever way it is invoked.
    """
    if override is not None:
        value, where = override, "--basename"
    elif not hasattr(content, "BASENAME"):
        raise SystemExit(
            "content module declares no BASENAME. It is the filename stem the report publishes "
            'under: the engine writes <BASENAME>-v<VERSION>.html. Add BASENAME = "my-report" '
            "beside TITLE. " + _CONTRACT_REF)
    else:
        value, where = content.BASENAME, "BASENAME"
    if not isinstance(value, str) or not _STEM_RE.match(value):
        raise SystemExit(
            "%s = %r is not a filename stem. It has to start with a letter or a digit and hold "
            "only letters, digits, dot, hyphen and underscore, because it is joined to the output "
            "directory: a separator, a parent reference or a leading space in it writes the "
            "report somewhere no build log names." % (where, value))
    return value


def report_version(content):
    """The edition a report publishes as. It goes in the filename, so it is not decoration."""
    if not hasattr(content, "VERSION"):
        raise SystemExit(
            "content module declares no VERSION. The filename carries the edition, as "
            "<BASENAME>-v<VERSION>.html, so a new edition cannot quietly overwrite the one before "
            'it and leave two different documents behind one name. Add VERSION = "1.0" beside '
            "TITLE. " + _CONTRACT_REF)
    value = content.VERSION
    if not isinstance(value, str):
        raise SystemExit(
            "VERSION = %r is a %s, not a str. A number cannot carry an edition: 8.10 formats as "
            '8.1, so the edition after 8.9 publishes on top of it and reads as a step backwards. '
            'Declare VERSION = "%s".' % (value, type(value).__name__, value))
    if value[:1] in ("v", "V") and value[1:2].isdigit():
        raise SystemExit(
            'VERSION = %r must not carry its own "v". The engine adds one, so this publishes as '
            '<BASENAME>-v%s.html. Declare VERSION = "%s".' % (value, value, value[1:]))
    if not _VERSION_RE.match(value):
        raise SystemExit(
            "VERSION = %r is not an edition. It has to start with a digit and hold only digits "
            "and dots, optionally followed by a suffix of letters, digits, dot, plus or hyphen. "
            "It becomes part of a filename and it is what the changelog and the previous edition "
            "are matched against." % (value,))
    return value


def report_stem(content, basename=None):
    """The output filename stem a content module's declarations resolve to.

    Returns "<BASENAME>-v<VERSION>", which is the name reports have always been published under.
    What changed is that both halves are now read from the module and checked, instead of being
    defaulted at the call site where a missing declaration was indistinguishable from a deliberate
    one. VERSION is not overridable: the edition is a property of the content, not of the command
    that rendered it.
    """
    return "%s-v%s" % (report_basename(content, basename), report_version(content))


class Rendered(tuple):
    """(html, figures, data) as ever, carrying the build's claims manifest as `.manifest` and the
    filename stem its own declarations resolve to as `.stem`.

    WHY A TUPLE SUBCLASS AND NOT A FOURTH ELEMENT. Callers unpack three values, including content
    modules that drive the engine directly from their own __main__ block, and widening the tuple
    would break every one of them silently at the unpack. The manifest rides along as an attribute
    because exactly one caller wants it: the gate, which must judge the same dict the document was
    rendered from. The stem rides along for the same reason: the caller that writes the files needs
    the name the engine resolved, and a second derivation somewhere else is a name that can drift.
    """
    manifest = _UNSET
    stem = None

    def __new__(cls, html, figures, data, manifest=_UNSET, stem=None):
        self = tuple.__new__(cls, (html, figures, data))
        self.manifest = manifest
        self.stem = stem
        return self


def _render_body(content, figures, data, manifest):
    """Call the content module's render(), handing it the manifest only if it asks for one.

    claims() runs ONCE per build, so a renderer that wants to print from the manifest has to be
    given it rather than call claims() again. Two calls meant two independently produced copies of
    the evidence, and the log said so: it printed the manifest write twice. Modules whose render()
    takes (figures, data) keep working untouched, which is most of them.
    """
    import inspect

    if manifest is not _UNSET:
        try:
            params = inspect.signature(content.render).parameters
        except (TypeError, ValueError):  # a builtin or C callable has no signature
            params = {}
        if "manifest" in params or any(p.kind == p.VAR_KEYWORD for p in params.values()):
            return content.render(figures, data, manifest=manifest)
    return content.render(figures, data)


def render_report(content, run_dir, out_dir=None, warn=None):
    """Build a complete report from a content module and a run directory.

    Returns (html, figures, data), plus `.manifest` and `.stem` on the returned object: claims() is
    called here and NOWHERE ELSE in a build, so the evidence and the document come out of one call,
    and `.stem` is the filename the module's BASENAME and VERSION resolve to. Writing files is the
    caller's business; this returns the document so a caller can diff it, lint it, or render it
    without touching the filesystem.
    """
    # Before build(). A missing or malformed BASENAME or VERSION is a one-line fix, and a build
    # that only reports it after the measurements have been reduced is a build people learn to
    # work around rather than correct.
    stem = report_stem(content)
    figures, data = content.build(run_dir, out_dir)
    fn = getattr(content, "claims", None)
    # Computed before render() so a renderer can be handed it. Safe in that order because claims()
    # reads the build's output; a content module that needed render() to run first would have been
    # writing its manifest from values the document did not use.
    manifest = fn(figures, data) if callable(fn) else _UNSET
    body = _render_body(content, figures, data, manifest)
    html = assemble(body, content.TITLE, content.SECTION_ORDER, warn=warn)
    return Rendered(html, figures, data, manifest, stem)
