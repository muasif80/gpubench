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
    build(run_dir) -> (figures, data)
    render(figures, data) -> body HTML, sections in authoring order

and the engine does the rest:

    from gpubench.longform import render_report
    html = render_report(content, run_dir, out_dir)
"""
from .css import PAGE_CSS
from .doc import assemble, contents, renumber, reorder_sections, resolve_refs, stat
from .svg import (esc, figure, fmt, frame, legend, lg, lin, marker, nice_ticks, polyline,
                  strip_style, svg_close, svg_open, table)

__all__ = [
    "PAGE_CSS", "assemble", "contents", "renumber", "reorder_sections", "resolve_refs", "stat",
    "esc", "figure", "fmt", "frame", "legend", "lg", "lin", "marker", "nice_ticks", "polyline",
    "strip_style", "svg_close", "svg_open", "table", "render_report",
    "render_companions",
]


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


def render_report(content, run_dir, out_dir=None, warn=None):
    """Build a complete report from a content module and a run directory.

    Returns (html, figures, data). Writing files is the caller's business; this returns the
    document so a caller can diff it, lint it, or render it without touching the filesystem.
    """
    figures, data = content.build(run_dir, out_dir)
    body = content.render(figures, data)
    return assemble(body, content.TITLE, content.SECTION_ORDER, warn=warn), figures, data
