"""Hand-rolled SVG chart primitives, plus the table and figure wrappers.

Charts are inline SVG rather than a plotting library on purpose: the output has to be embeddable in
a page, theme-aware, crisp at any width, and free of any runtime dependency. Every figure ships a
table view underneath it, because colour alone must never be the only channel carrying information.

Extracted verbatim from the generator this engine was factored out of. Nothing here knows what is
being measured.
"""
import html
import math

def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


W, H = 780, 400
PL, PR, PT, PB = 76, 28, 30, 58

SVG_STYLE = """
  .surface{fill:#fcfcfb}
  .grid{stroke:#e1e0d9;stroke-width:1}
  .axis{stroke:#c3c2b7;stroke-width:1}
  .tick{fill:#898781;font:11px system-ui,-apple-system,"Segoe UI",sans-serif;font-variant-numeric:tabular-nums}
  .lbl{fill:#52514e;font:12px system-ui,-apple-system,"Segoe UI",sans-serif}
  .val{fill:#0b0b0b;font:12px system-ui,-apple-system,"Segoe UI",sans-serif}
  .valinv{fill:#fcfcfb;font:12px system-ui,-apple-system,"Segoe UI",sans-serif}
  .s1{stroke:#2a78d6;fill:#2a78d6}
  .s2{stroke:#eb6834;fill:#eb6834}
  .s3{stroke:#1baf7a;fill:#1baf7a}
  .ref{stroke:#898781;stroke-width:1}
  .ln{fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
  .mk{stroke:#fcfcfb;stroke-width:2}
  .gap{stroke:#fcfcfb;stroke-width:2}
@media (prefers-color-scheme: dark){
  .surface{fill:#1a1a19}
  .grid{stroke:#2c2c2a}
  .axis{stroke:#383835}
  .lbl{fill:#c3c2b7}
  .val{fill:#ffffff}
  .valinv{fill:#1a1a19}
  .s1{stroke:#3987e5;fill:#3987e5}
  .s2{stroke:#d95926;fill:#d95926}
  .s3{stroke:#199e70;fill:#199e70}
  .mk{stroke:#1a1a19}
  .gap{stroke:#1a1a19}
}
"""


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def svg_open(w=W, h=H, title=""):
    return ('<svg viewBox="0 0 %d %d" width="100%%" role="img" aria-label="%s" '
            'xmlns="http://www.w3.org/2000/svg"><style>%s</style>'
            '<rect class="surface" x="0" y="0" width="%d" height="%d"/>'
            % (w, h, esc(title), SVG_STYLE, w, h))


def svg_close():
    return "</svg>"


def lin(v, d0, d1, r0, r1):
    if d1 == d0:
        return r0
    return r0 + (float(v) - d0) / (d1 - d0) * (r1 - r0)


def lg(v, d0, d1, r0, r1):
    v = max(v, 1e-12)
    a, b = math.log10(d0), math.log10(d1)
    return r0 + (math.log10(v) - a) / (b - a) * (r1 - r0)


def fmt(v, nd=0):
    if v is None:
        return "-"
    if abs(v) >= 1000 and nd == 0:
        return "{:,.0f}".format(v)
    return ("%%.%df" % nd) % v


def nice_ticks(d0, d1, n=5):
    span = d1 - d0
    if span <= 0:
        return [d0]
    raw = span / float(n)
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        if raw <= mag * m:
            step = mag * m
            break
    else:
        step = mag * 10
    start = math.ceil(d0 / step) * step
    out = []
    v = start
    while v <= d1 + step * 1e-9:
        out.append(round(v, 10))
        v += step
    return out


def frame(y_ticks, y_to_px, x_title="", y_title="", w=W, h=H):
    """Hairline horizontal grid, solid axes, tick labels. No dashes anywhere."""
    out = []
    for t in y_ticks:
        y = y_to_px(t)
        out.append('<line class="grid" x1="%d" y1="%.1f" x2="%d" y2="%.1f"/>' % (PL, y, w - PR, y))
        out.append('<text class="tick" x="%d" y="%.1f" text-anchor="end">%s</text>'
                   % (PL - 10, y + 4, fmt(t, 0 if abs(t) >= 10 else 1)))
    out.append('<line class="axis" x1="%d" y1="%d" x2="%d" y2="%d"/>' % (PL, h - PB, w - PR, h - PB))
    if y_title:
        out.append('<text class="lbl" x="%d" y="%d" transform="rotate(-90 %d %d)" '
                   'text-anchor="middle">%s</text>' % (18, (h - PB + PT) / 2, 18, (h - PB + PT) / 2,
                                                       esc(y_title)))
    if x_title:
        out.append('<text class="lbl" x="%.0f" y="%d" text-anchor="middle">%s</text>'
                   % ((PL + w - PR) / 2, h - 12, esc(x_title)))
    return "".join(out)


def legend(items, x=PL, y=18):
    """Always present for two or more series; identity never rests on colour alone."""
    out = []
    cx = x
    for cls, label in items:
        out.append('<rect class="%s" x="%.0f" y="%.0f" width="10" height="10" rx="2"/>' % (cls, cx, y - 8))
        out.append('<text class="lbl" x="%.0f" y="%.0f">%s</text>' % (cx + 16, y + 1, esc(label)))
        cx += 20 + 7.6 * len(label)
    return "".join(out)


def polyline(pts, cls):
    d = " ".join("%.1f,%.1f" % p for p in pts)
    return '<polyline class="ln %s" points="%s"/>' % (cls, d)


def marker(x, y, cls, tip, r=4.5):
    return ('<circle class="mk %s" cx="%.1f" cy="%.1f" r="%.1f"><title>%s</title></circle>'
            % (cls, x, y, r, esc(tip)))


def table(headers, rows, caption=""):
    out = ['<div class="tablewrap"><table>']
    if caption:
        out.append("<caption>%s</caption>" % esc(caption))
    out.append("<thead><tr>" + "".join("<th>%s</th>" % esc(h) for h in headers) + "</tr></thead><tbody>")
    for r in rows:
        out.append("<tr>" + "".join("<td>%s</td>" % (c if isinstance(c, str) else esc(c)) for c in r) + "</tr>")
    out.append("</tbody></table></div>")
    return "".join(out)


def strip_style(svg):
    """Inline SVG <style> is document-global, so nine copies would collide and leak.

    The standalone .svg files keep their own style block (they have to work on their own); the
    inline copies drop it and inherit one scoped block from the page stylesheet instead.
    """
    a = svg.find("<style>")
    b = svg.find("</style>")
    if a == -1 or b == -1:
        return svg
    return svg[:a] + svg[b + len("</style>"):]


def figure(fid, num, title, svg, note, tbl):
    svg = strip_style(svg)
    return ('<figure id="%s"><div class="chart">%s</div>'
            '<figcaption><b>Figure %s.</b> %s</figcaption>'
            '%s<details><summary>Table view</summary>%s</details></figure>'
            % (fid, svg, num, title, ("<p class='note'>%s</p>" % note) if note else "", tbl))


# ----------------------------------------------------------------------------- figures
