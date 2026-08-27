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
    SECTION_ORDER  list of title fragments, in reading order
    build(run_dir, out_dir) -> (figures, data)
    render(figures, data) -> body HTML, sections in authoring order
                   or render(figures, data, manifest=None), to be handed the claims manifest
                   this build already computed instead of computing a second copy of it

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


def run_claims_gate(content, figures, data, out_dir, rendered_html=None,
                    warnings_as_errors=False, previous=None, manifest=_UNSET,
                    companions=None):
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


class Rendered(tuple):
    """(html, figures, data) as ever, carrying the build's claims manifest as `.manifest`.

    WHY A TUPLE SUBCLASS AND NOT A FOURTH ELEMENT. Callers unpack three values, including content
    modules that drive the engine directly from their own __main__ block, and widening the tuple
    would break every one of them silently at the unpack. The manifest rides along as an attribute
    because exactly one caller wants it: the gate, which must judge the same dict the document was
    rendered from.
    """
    manifest = _UNSET

    def __new__(cls, html, figures, data, manifest=_UNSET):
        self = tuple.__new__(cls, (html, figures, data))
        self.manifest = manifest
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

    Returns (html, figures, data), plus `.manifest` on the returned object: claims() is called here
    and NOWHERE ELSE in a build, so the evidence and the document come out of one call. Writing
    files is the caller's business; this returns the document so a caller can diff it, lint it, or
    render it without touching the filesystem.
    """
    figures, data = content.build(run_dir, out_dir)
    fn = getattr(content, "claims", None)
    # Computed before render() so a renderer can be handed it. Safe in that order because claims()
    # reads the build's output; a content module that needed render() to run first would have been
    # writing its manifest from values the document did not use.
    manifest = fn(figures, data) if callable(fn) else _UNSET
    body = _render_body(content, figures, data, manifest)
    html = assemble(body, content.TITLE, content.SECTION_ORDER, warn=warn)
    return Rendered(html, figures, data, manifest)
