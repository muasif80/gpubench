"""Document assembly: section ordering, renumbering, cross-references, contents, page shell.

This is the part of long-form report generation that has nothing to do with what was measured, and
everything to do with a document not contradicting itself. Four mechanisms live here, and each one
exists because its absence produced a defect that survived human review:

  reorder_sections   Reading order is declared separately from authoring order. Authoring follows
                     how the work was done; reading should follow scope, method, results,
                     conclusions, actions, appendix. Keeping them separate means a section can be
                     written anywhere without disturbing the flow.

  renumber           Section numbers are assigned in final document order, never taken from the
                     literals an author typed. Hand-maintained numbers drift the moment a section
                     is inserted, and a report containing two section 18s undermines everything
                     else in it.

  resolve_refs       In-text references name a section by a fragment of its TITLE, not by number,
                     and are resolved after renumbering. A number goes stale every time a section
                     moves; a title fragment cannot. An unresolvable or ambiguous reference ABORTS
                     the build rather than shipping a wrong pointer, which is the whole point.

  contents           Built from the sections that are actually present, rather than from a
                     hand-maintained list that would drift on the first insertion.

Nothing here is hardware-specific, and nothing here knows what the report is about.
"""
import re

from .css import PAGE_CSS


def stat(n, k):
    """One headline figure with its label, for a strip of them above the fold."""
    from .svg import esc
    return '<div class="stat"><div class="n">%s</div><div class="k">%s</div></div>' % (n, esc(k))


def reorder_sections(body, section_order):
    """Split on h2 boundaries, reorder by `section_order`, reassemble.

    Anything not named in `section_order` keeps its relative position at the END, so forgetting to
    list a new section degrades to "appears last" rather than "silently disappears". Returns
    (body, count_of_unordered) so the caller can warn.

    The leading number is optional in the match so an unnumbered section (an abstract) can still be
    placed. Renumbering keys off the digit, so an unnumbered heading stays unnumbered.
    """
    parts = re.split(r'(?=<h2>)', body)
    head, chunks = parts[0], parts[1:]
    used, out = set(), []
    for key in section_order:
        for i, c in enumerate(chunks):
            if i in used:
                continue
            m = re.match(r'<h2>(?:\d+\.\s*)?(.*?)</h2>', c)
            if m and key.lower() in m.group(1).lower():
                out.append(c)
                used.add(i)
                break
    leftover = [c for i, c in enumerate(chunks) if i not in used]
    return head + "".join(out) + "".join(leftover), len(leftover)


def renumber(body):
    """Assign section numbers in document order. Unnumbered headings are left alone."""
    n = [0]

    def sub(m):
        n[0] += 1
        return '<h2 id="sec-%d">%d. %s</h2>' % (n[0], n[0], m.group(2))

    return re.sub(r'<h2>(\d+)\.\s*(.*?)</h2>', sub, body)


def resolve_refs(body):
    """Resolve {{ref:title fragment}} to the FINAL number of the matching section.

    Raises on a fragment that matches zero sections or more than one. That is deliberate: a build
    that fails is recoverable in a minute, and a shipped document pointing at the wrong section is
    the kind of error a reader finds and the author never does.
    """
    secnum = dict((t.strip().lower(), num) for num, t in
                  re.findall(r'<h2 id="sec-\d+">(\d+)\.\s*([^<]+)</h2>', body))

    def sub(m):
        key = m.group(1).strip().lower()
        hits = [n for t, n in secnum.items() if key in t]
        if len(hits) != 1:
            raise SystemExit(
                "cross-reference %r matched %d sections: %s\n"
                "Use a longer fragment of the target section's title. This aborts rather than "
                "guessing, because a wrong pointer is worse than a failed build."
                % (key, len(hits), sorted(t for t in secnum if key in t)))
        return hits[0]

    return re.sub(r"\{\{ref:([^}]+)\}\}", sub, body)


def contents(body):
    """A linked table of contents, built from the sections actually present."""
    entries = re.findall(r'<h2 id="sec-(\d+)">\d+\. (.*?)</h2>', body)
    toc = ['<nav class="toc"><h2 class="plain">Contents</h2><ol>']
    for num, title in entries:
        toc.append('<li><a href="#sec-%s"><span class="n">%s</span>'
                   '<span>%s</span></a></li>' % (num, num, title))
    toc.append('</ol></nav>')
    return "".join(toc)


def assemble(body, title, section_order, warn=None):
    """Ordering, renumbering, reference resolution, contents, and the page shell.

    `body` is the concatenated section HTML in authoring order. `warn` is an optional callable for
    a note about sections not named in `section_order`.

    Returns the complete self-contained HTML document.
    """
    body, unordered = reorder_sections(body, section_order)
    if unordered and warn:
        warn("%d section(s) not in the declared reading order, placed last" % unordered)

    body = renumber(body)
    body = resolve_refs(body)

    # The contents sits between the title page and the body, so split the title block off first.
    toc = contents(body)
    split = body.find('</div>') + len('</div>')
    body = body[:split] + toc + body[split:]

    from .svg import esc
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>%s</title>'
            '<style>%s</style></head><body><main>%s</main></body></html>'
            % (esc(title), PAGE_CSS, body))
