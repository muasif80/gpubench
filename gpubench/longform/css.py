"""The page stylesheet: screen and print, theme-aware, no external assets.

Extracted verbatim from the report generator this engine was factored out of, so a report renders
identically before and after the move. Nothing here is specific to any kind of hardware.

Two rules in here were learned from printed output rather than from a browser, and both are easy to
undo by accident:

  * Long tables MAY break across pages. Forbidding it pushed a page of white ahead of every table
    taller than the remaining space, which is how a 32-page document became 53 pages.
  * Cell text wraps with `overflow-wrap:break-word` and `word-break:normal`. `anywhere` also shrinks
    a cell's min-content width, so the layout algorithm starves narrow columns and hyphenates
    mid-word.
"""
PAGE_CSS = """
:root{color-scheme:light;--bg:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;
--muted:#898781;--rule:#e1e0d9;--accent:#2a78d6;--code:#f0efec}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){color-scheme:dark;
--bg:#0d0d0d;--surface:#1a1a19;--ink:#fff;--ink2:#c3c2b7;--muted:#898781;--rule:#2c2c2a;
--accent:#3987e5;--code:#222221}}
:root[data-theme=dark]{color-scheme:dark;--bg:#0d0d0d;--surface:#1a1a19;--ink:#fff;
--ink2:#c3c2b7;--muted:#898781;--rule:#2c2c2a;--accent:#3987e5;--code:#222221}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:16px/1.65 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:860px;margin:0 auto;padding:48px 20px 96px}
h1{font-size:2.1rem;line-height:1.2;margin:0 0 8px;letter-spacing:-0.01em}
h2{font-size:1.4rem;margin:56px 0 12px;letter-spacing:-0.005em}
h3{font-size:1.1rem;margin:32px 0 8px}
p{margin:0 0 16px;color:var(--ink2)}
p.lede{font-size:1.12rem;color:var(--ink)}
p.note{font-size:0.9rem;color:var(--muted);margin:8px 0 0}
strong,b{color:var(--ink)}
a{color:var(--accent)}
hr{border:0;border-top:1px solid var(--rule);margin:40px 0}
ul,ol{color:var(--ink2);padding-left:22px;margin:0 0 16px}
li{margin:0 0 8px}
code{background:var(--code);padding:1px 5px;border-radius:4px;font-size:0.88em;
font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
pre{background:var(--code);padding:14px 16px;border-radius:8px;overflow-x:auto;
border:1px solid var(--rule);margin:0 0 16px}
pre code{background:none;padding:0;font-size:0.84rem;line-height:1.5}
figure{margin:28px 0 32px}
.chart{background:var(--surface);border:1px solid var(--rule);border-radius:10px;padding:8px}
figcaption{font-size:0.92rem;color:var(--ink2);margin-top:10px}
details{margin-top:10px}
summary{cursor:pointer;font-size:0.9rem;color:var(--muted)}
/* overflow-x is a SAFETY NET for unbreakable content, not the way wide tables are handled --
   cells wrap, so this should never engage. */
.tablewrap{overflow-x:auto;margin:12px 0 20px;border:1px solid var(--rule);border-radius:8px;
background:var(--surface)}
table{border-collapse:collapse;width:100%;font-size:0.9rem;table-layout:auto}
caption{text-align:left;padding:10px 12px;color:var(--muted);font-size:0.85rem}
/* Cells WRAP. They used to be nowrap with the wrapper scrolling sideways, which made every
   wide table something a reader had to drag. break-word rather than anywhere: `anywhere` also
   shrinks a cell's min-content width, so the layout algorithm starves narrow columns and
   hyphenates mid-word. */
th,td{text-align:left;padding:9px 12px;border-bottom:1px solid var(--rule);
font-variant-numeric:tabular-nums;white-space:normal;overflow-wrap:break-word;
word-break:normal;hyphens:none;vertical-align:top}
th{color:var(--ink);font-weight:600;background:var(--surface);position:sticky;top:0}
tbody tr:last-child td{border-bottom:0}
/* Keep short numeric-and-unit cells from wrapping between the number and its unit, which reads
   as two values. Applied by class rather than by column, since column meaning varies. */
td.nw,th.nw{white-space:nowrap}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin:28px 0}
.stat{background:var(--surface);border:1px solid var(--rule);border-radius:10px;padding:16px}
.stat .n{font-size:1.7rem;font-weight:650;color:var(--ink);line-height:1.15}
.stat .k{font-size:0.82rem;color:var(--muted);margin-top:4px}
.callout{background:var(--surface);border:1px solid var(--rule);border-left:3px solid var(--accent);
border-radius:8px;padding:14px 18px;margin:0 0 20px}
.callout p:last-child{margin:0}
.meta{color:var(--muted);font-size:0.9rem;margin:0 0 28px}
.titlepage{padding:40px 0 8px;border-bottom:1px solid var(--rule);margin-bottom:32px}
.titlepage .kicker{font-size:0.82rem;letter-spacing:0.09em;text-transform:uppercase;
color:var(--muted);margin:0 0 14px}
.titlepage h1{margin:0 0 14px}
.titlepage .sub{font-size:1.1rem;color:var(--ink2);margin:0 0 26px;max-width:44em}
.docctl{display:grid;grid-template-columns:auto 1fr;gap:6px 18px;font-size:0.9rem;
max-width:34em}
.docctl dt{color:var(--muted)}
.docctl dd{margin:0;color:var(--ink)}
nav.toc{margin:0 0 40px}
nav.toc h2{margin-top:0}
nav.toc ol{list-style:none;padding:0;counter-reset:toc}
nav.toc li{counter-increment:toc;margin:0;border-bottom:1px solid var(--rule)}
nav.toc a{display:flex;gap:12px;padding:8px 2px;text-decoration:none;color:var(--ink2)}
nav.toc a:hover{color:var(--accent)}
nav.toc .n{color:var(--muted);min-width:2.2em;font-variant-numeric:tabular-nums}

@media print{
  :root{--bg:#fff;--surface:#fff;--ink:#000;--ink2:#1a1a1a;--muted:#555;--rule:#bbb;
        --accent:#14508f;--code:#f2f2f0}
  body{background:#fff;font-size:10.5pt;line-height:1.5;orphans:3;widows:3}
  main{max-width:none;padding:0}
  h1{font-size:22pt}
  /* Sections FLOW rather than each starting a new page. Forcing a break before all 32 headings
     turned a third-of-a-page section into a full page and produced a document half made of white.
     break-after:avoid still stops a heading orphaning at the foot of a page. */
  h2{font-size:14pt;break-before:auto;break-after:avoid;margin-top:20pt;
     padding-top:6pt;border-top:1px solid var(--rule)}
  h3{font-size:11.5pt;break-after:avoid}
  .titlepage{break-after:page;border-bottom:0;padding-top:0}
  .titlepage h2{break-before:auto}
  nav.toc{break-after:page}
  nav.toc h2{break-before:auto}
  figure,.callout,.stats{break-inside:avoid}
  pre{break-inside:avoid}
  /* Tables may break across pages: forbidding it pushed a page of white ahead of every
     long table. The header group repeats so a continued table stays readable. */
  table{break-inside:auto}
  thead{display:table-header-group}
  tr,td,th{break-inside:avoid}
  figcaption{break-before:avoid}
  .tablewrap{overflow:visible;border:1px solid var(--rule)}
  table{table-layout:auto;width:100%}
  /* On screen the cells never wrap and the container scrolls sideways. Paper cannot scroll,
     so nowrap silently clips the right-hand edge of any wide table. Wrap instead. */
  /* break-word, not anywhere: `anywhere` also shrinks a cell's min-content width, so the layout
     algorithm starves narrow columns and hyphenates mid-word ("Seconda/ry"). */
  th,td{white-space:normal;overflow-wrap:break-word;word-break:normal;hyphens:none;
        font-size:8.6pt;padding:5px 7px}
  thead{display:table-header-group}
  tr{break-inside:avoid}
  th{position:static}
  summary{display:none}
  a{color:inherit;text-decoration:none}
  .chart{border:1px solid var(--rule);padding:4px}
}
@page{size:A4;margin:17mm 15mm 20mm 15mm}
.chart svg .surface{fill:var(--surface)}
.chart svg .grid{stroke:var(--rule);stroke-width:1}
.chart svg .axis{stroke:var(--rule);stroke-width:1}
.chart svg .tick{fill:var(--muted);font:11px system-ui,-apple-system,"Segoe UI",sans-serif;
font-variant-numeric:tabular-nums}
.chart svg .lbl{fill:var(--ink2);font:12px system-ui,-apple-system,"Segoe UI",sans-serif}
.chart svg .val{fill:var(--ink);font:12px system-ui,-apple-system,"Segoe UI",sans-serif}
.chart svg .valinv{fill:var(--surface);font:12px system-ui,-apple-system,"Segoe UI",sans-serif}
.chart svg .s1{stroke:#2a78d6;fill:#2a78d6}
.chart svg .s2{stroke:#eb6834;fill:#eb6834}
.chart svg .s3{stroke:#1baf7a;fill:#1baf7a}
.chart svg .ref{stroke:var(--muted);stroke-width:1}
.chart svg .ln{fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.chart svg .mk{stroke:var(--surface);stroke-width:2}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]) .chart svg .s1{stroke:#3987e5;fill:#3987e5}
:root:not([data-theme=light]) .chart svg .s2{stroke:#d95926;fill:#d95926}
:root:not([data-theme=light]) .chart svg .s3{stroke:#199e70;fill:#199e70}}
:root[data-theme=dark] .chart svg .s1{stroke:#3987e5;fill:#3987e5}
:root[data-theme=dark] .chart svg .s2{stroke:#d95926;fill:#d95926}
:root[data-theme=dark] .chart svg .s3{stroke:#199e70;fill:#199e70}
"""
