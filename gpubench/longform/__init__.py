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
]


def render_report(content, run_dir, out_dir=None, warn=None):
    """Build a complete report from a content module and a run directory.

    Returns (html, figures, data). Writing files is the caller's business; this returns the
    document so a caller can diff it, lint it, or render it without touching the filesystem.
    """
    figures, data = content.build(run_dir, out_dir)
    body = content.render(figures, data)
    return assemble(body, content.TITLE, content.SECTION_ORDER, warn=warn), figures, data
