"""HTML to PDF, then real page numbers in the contents and a navigable outline.

Two passes, and the second one is not optional polish.

CSS has a mechanism for contents page numbers, `target-counter(attr(href), page)`, and the browser
engine used here does not implement it. Rather than ship a 50-page document whose contents makes a
reader hunt, the numbers are resolved from the RENDERED document: render once, find the page each
heading actually landed on, write those numbers into the HTML, render again, and attach a PDF
outline so the viewer sidebar works too.

The second render is safe because a page number is appended to an existing single-line contents
entry, so nothing reflows. That is asserted at the end rather than assumed, because "probably still
correct" is not a standard worth publishing.

Requires playwright for rendering and pymupdf for the outline. Both are optional: the rest of the
tool has no third-party dependencies, and a caller without them gets a clear message rather than a
traceback.
"""
import io
import os
import re

FOOTER_TEMPLATE = """
<div style="width:100%%;font-size:8px;color:#666;padding:0 14mm;
            font-family:-apple-system,'Segoe UI',Roboto,sans-serif;
            display:flex;justify-content:space-between;">
  <span>%s</span>
  <span>Page <span class="pageNumber"></span> of <span class="totalPages"></span></span>
</div>
"""
EMPTY_HEADER = '<div style="display:none"></div>'


def _require(mod, why):
    """Import an optional dependency, or explain precisely what to install and why.

    importlib rather than __import__, because __import__("playwright") returns the package without
    binding its submodules and the resulting AttributeError names nothing useful.
    """
    import importlib
    try:
        return importlib.import_module(mod)
    except ImportError:
        raise SystemExit(
            "%s is needed to %s but is not installed.\n"
            "  pip install %s\n"
            "The rest of this tool has no third-party dependencies; only PDF export does."
            % (mod, why, {"playwright.sync_api": "playwright && playwright install chromium",
                          "fitz": "pymupdf"}.get(mod, mod)))


def render(html_path, out_path=None, footer_left=""):
    """Render an HTML file to PDF with a page footer. Returns the output path."""
    sync_playwright = _require("playwright.sync_api", "render a PDF").sync_playwright
    src = os.path.abspath(html_path)
    out = os.path.abspath(out_path or (os.path.splitext(src)[0] + ".pdf"))
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.emulate_media(media="print", color_scheme="light")
        page.goto("file:///" + src.replace("\\", "/"), wait_until="load")
        # Collapsed <details> content does not print. Open them all so table views survive, which
        # is what keeps the reproducibility contract intact on paper.
        page.evaluate("document.querySelectorAll('details').forEach(d => d.open = true)")
        page.wait_for_timeout(400)
        page.pdf(path=out, format="A4", print_background=True, display_header_footer=True,
                 header_template=EMPTY_HEADER, footer_template=FOOTER_TEMPLATE % footer_left,
                 margin={"top": "16mm", "bottom": "18mm", "left": "14mm", "right": "14mm"})
        browser.close()
    return out


def section_pages(pdf_path):
    """Map section number -> 1-based page, from where each heading is actually drawn.

    Contents pages are skipped by finding the last page whose head mentions the contents heading;
    everything after it is body. Cruder than parsing the layout, and far less fragile.
    """
    fitz = _require("fitz", "attach a PDF outline")
    doc = fitz.open(pdf_path)
    toc_end = 0
    for i, page in enumerate(doc):
        if "Contents" in page.get_text()[:400]:
            toc_end = i
    found = {}
    for i in range(toc_end + 1, len(doc)):
        for line in doc[i].get_text().split("\n"):
            m = re.match(r"^(\d{1,2})\.\s+(\S.*)$", line.strip())
            if m and m.group(1) not in found:
                found[m.group(1)] = i + 1
    doc.close()
    return found


def _patch_contents(html, pages):
    n = [0]

    def repl(m):
        num = m.group(2)
        pg = pages.get(num)
        if not pg:
            return m.group(0)
        n[0] += 1
        return '%s<span class="n">%s</span>%s<span class="pg">%d</span>' % (
            m.group(1), num, m.group(3), pg)

    html = re.sub(r'(<a href="#sec-\d+">)<span class="n">(\d+)</span>'
                  r'(<span>[^<]*</span>)', repl, html)
    return html, n[0]


def paginate(html_path, pdf_path, footer_left="", also_write=()):
    """Add contents page numbers and a PDF outline. Re-renders, then VERIFIES nothing moved.

    `also_write` are extra paths to receive the paginated HTML and PDF, for a versioned edition
    alongside a working copy. Without it the published edition silently lacks the page numbers,
    which is a mistake that has been made.
    """
    if not os.path.exists(pdf_path):
        raise SystemExit("render the PDF before paginating: %s is missing" % pdf_path)
    pages = section_pages(pdf_path)
    if not pages:
        raise SystemExit("found no section headings in the PDF; refusing to write a contents page "
                         "with guessed numbers")

    html = io.open(html_path, encoding="utf-8").read()
    if 'class="pg"' in html:
        raise SystemExit("contents already paginated; rebuild the HTML first")
    html, n = _patch_contents(html, pages)
    if n < len(pages) - 1:
        raise SystemExit("only matched %d of %d sections in the contents; not shipping a partial "
                         "contents page" % (n, len(pages)))

    # display:block makes each row a full-width line so the page number can float right. That also
    # collapses the gap the screen layout got from flex, so the number column is given a width back.
    css = ("\n  nav.toc .pg{float:right;color:var(--muted);font-variant-numeric:tabular-nums}"
           "\n  nav.toc li a{display:block}"
           "\n  nav.toc .n{display:inline-block;min-width:2.1em;text-align:left}\n")
    html = html.replace("@media print{", "@media print{" + css, 1)
    io.open(html_path, "w", encoding="utf-8", newline="\n").write(html)

    render(html_path, pdf_path, footer_left=footer_left)

    after = section_pages(pdf_path)
    drift = {k: (pages[k], after[k]) for k in pages if k in after and pages[k] != after[k]}

    fitz = _require("fitz", "attach a PDF outline")
    doc = fitz.open(pdf_path)
    toc = []
    for num in sorted(after, key=int):
        page = doc[after[num] - 1]
        title = None
        for line in page.get_text().split("\n"):
            m = re.match(r"^(%s)\.\s+(\S.*)$" % re.escape(num), line.strip())
            if m:
                title = "%s. %s" % (num, m.group(2))
                break
        toc.append([1, title or ("Section " + num), after[num]])
    doc.set_toc(toc)
    doc.saveIncr()
    doc.close()

    for dst in also_write:
        src = html_path if dst.endswith(".html") else pdf_path
        if os.path.exists(src):
            io.open(dst, "wb").write(io.open(src, "rb").read())

    return {"sections": n, "outline_entries": len(toc), "drift": drift,
            "verified": not drift}
