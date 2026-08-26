#!/usr/bin/env python3
"""Render a result file into a professional, self-contained HTML report.

Design constraints, in order of importance:
  1. Every number traceable. Measured values are labelled measured; reference values carry their
     provenance (published, or derived from a published or device-reported figure).
  2. No runtime dependencies. Charts are inline SVG, so the file opens anywhere and prints.
  3. Degrades honestly. A metric the target could not supply says so; it never renders as zero.

Chart colours come from a categorical palette validated for colour-vision deficiency in both
light and dark surfaces. Every figure is accompanied by its own table so colour is never the only
channel carrying information.
"""
import datetime
import html
import json
import math
import os

from . import analysis

# ---------------------------------------------------------------- reference data

def _rtx5090():
    """NVIDIA's published figures for the GeForce RTX 5090.

    The headline "3352 AI TOPS" is FP4 *with 2:4 structured sparsity*. Dense rates halve for
    sparsity and again for each precision step, so everything except the headline, the memory
    figures and the core count is arithmetic on a published number and is labelled as derived.
    """
    return {
        "mem_bw_gb_s": (1792.0, "published (512-bit x 28 Gbps GDDR7)"),
        "fp4_dense": (1676.0, "derived: 3352 AI TOPS sparse / 2"),
        "fp8_dense": (838.0, "derived: FP4 dense / 2"),
        "int8_dense": (838.0, "ASSUMPTION: no dense INT8 figure is published for this part; NVIDIA has kept INT8 at the FP8 tensor rate across recent architectures"),
        "bf16_dense": (419.0, "derived: FP8 dense / 2"),
        "fp16_dense": (419.0, "derived: FP8 dense / 2"),
        "tf32_dense": (209.5, "derived: BF16 dense / 2"),
        "fp32_shader": (104.9, "derived: 21760 cores x 2 x 2.41 GHz boost"),
        "tgp_w": (575.0, "published"),
        "vram_gib": (32.0, "published"),
    }


DATASHEETS = {"NVIDIA GeForce RTX 5090": _rtx5090()}

DTYPE_TO_SPEC = {
    "float4_e2m1": "fp4_dense", "float8_e4m3fn": "fp8_dense", "int8": "int8_dense",
    "bfloat16": "bf16_dense", "float16": "fp16_dense", "tf32": "tf32_dense",
    "float32_shader": "fp32_shader",
}

DTYPE_LABEL = {
    "float4_e2m1": "FP4 (e2m1, block-scaled)", "float8_e4m3fn": "FP8 (e4m3)",
    "int8": "INT8", "bfloat16": "BF16", "float16": "FP16", "tf32": "TF32",
    "float32_shader": "FP32 (shader, non-tensor)",
}

UNIT = {"int8": "TOPS", "float4_e2m1": "TOPS"}

PALETTE = {"s1": "#2a78d6", "s2": "#eb6834", "s3": "#1baf7a",
           "d1": "#3987e5", "d2": "#d95926", "d3": "#199e70"}


def esc(s):
    return html.escape(str(s), quote=True)


def fmt(v, nd=1):
    if v is None:
        return "&mdash;"
    if isinstance(v, str):
        return esc(v)
    if abs(v) >= 10000:
        return "{:,.0f}".format(v)
    return ("%%.%df" % nd) % v


# ---------------------------------------------------------------- svg helpers

W, H = 760, 340
PL, PR, PT, PB = 150, 70, 26, 44


def svg_bar(rows, title, unit, max_hint=None, ref=None, ref_label=""):
    """Horizontal bars, one series, value labelled on every bar."""
    if not rows:
        return "", ""
    vmax = max_hint or max(v for _, v, *_ in rows)
    vmax = vmax * 1.18 if vmax else 1
    h = PT + 30 + len(rows) * 38 + 30
    x0, x1 = PL, W - PR
    out = ['<svg viewBox="0 0 %d %d" width="100%%" role="img" aria-label="%s" '
           'xmlns="http://www.w3.org/2000/svg">' % (W, h, esc(title))]
    out.append('<rect x="0" y="0" width="%d" height="%d" fill="var(--surface)"/>' % (W, h))
    for frac in (0, 0.25, 0.5, 0.75, 1.0):
        x = x0 + frac * (x1 - x0)
        out.append('<line x1="%.1f" y1="%d" x2="%.1f" y2="%d" stroke="var(--rule)" '
                   'stroke-width="1"/>' % (x, PT, x, h - 30))
        out.append('<text x="%.1f" y="%d" text-anchor="middle" font-size="10" '
                   'fill="var(--muted)">%s</text>' % (x, h - 14, fmt(frac * vmax, 0)))
    if ref:
        xr = x0 + min(1.0, ref / vmax) * (x1 - x0)
        out.append('<line x1="%.1f" y1="%d" x2="%.1f" y2="%d" stroke="var(--muted)" '
                   'stroke-width="1"/>' % (xr, PT, xr, h - 30))
        out.append('<text x="%.1f" y="%d" text-anchor="middle" font-size="10" '
                   'fill="var(--muted)">%s</text>' % (xr, PT - 8, esc(ref_label)))
    for i, row in enumerate(rows):
        name, val = row[0], row[1]
        colour = row[2] if len(row) > 2 else "s1"
        y = PT + 26 + i * 38
        bw = max(2.0, (val / vmax) * (x1 - x0))
        out.append('<text x="%d" y="%.1f" text-anchor="end" font-size="11.5" '
                   'fill="var(--ink2)">%s</text>' % (x0 - 10, y + 5, esc(name)))
        out.append('<rect x="%d" y="%.1f" width="%.1f" height="17" rx="4" fill="%s">'
                   '<title>%s: %s %s</title></rect>'
                   % (x0, y - 8, bw, PALETTE[colour], esc(name), fmt(val), esc(unit)))
        out.append('<text x="%.1f" y="%.1f" font-size="11.5" fill="var(--ink)">%s</text>'
                   % (x0 + bw + 8, y + 5, fmt(val)))
    out.append("</svg>")
    tbl = table(["Metric", "Value (%s)" % unit],
                [[r[0], fmt(r[1])] for r in rows])
    return "".join(out), tbl


def svg_line(points, title, x_title, y_title, logx=True, series_label=None):
    """Single-series line over a (possibly logarithmic) x axis."""
    if len(points) < 2:
        return "", ""
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    x0d, x1d = min(xs), max(xs)
    y1d = max(ys) * 1.2
    plw, plr, plt_, plb = 78, 28, 30, 52
    xp = (lambda v: plw + (math.log10(max(v, 1e-9)) - math.log10(x0d)) /
          max(1e-9, (math.log10(x1d) - math.log10(x0d))) * (W - plr - plw)) if logx else \
         (lambda v: plw + (v - x0d) / max(1e-9, (x1d - x0d)) * (W - plr - plw))
    yp = lambda v: (H - plb) - (v / y1d) * (H - plb - plt_)

    out = ['<svg viewBox="0 0 %d %d" width="100%%" role="img" aria-label="%s" '
           'xmlns="http://www.w3.org/2000/svg">' % (W, H, esc(title))]
    out.append('<rect x="0" y="0" width="%d" height="%d" fill="var(--surface)"/>' % (W, H))
    for k in range(5):
        v = y1d * k / 4.0
        y = yp(v)
        out.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="var(--rule)" '
                   'stroke-width="1"/>' % (plw, y, W - plr, y))
        out.append('<text x="%d" y="%.1f" text-anchor="end" font-size="10" '
                   'fill="var(--muted)">%s</text>' % (plw - 9, y + 4, fmt(v, 0)))
    out.append('<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="var(--rule)"/>'
               % (plw, H - plb, W - plr, H - plb))
    for x, _y, lab in [(p[0], p[1], p[2] if len(p) > 2 else str(p[0])) for p in points]:
        out.append('<text x="%.1f" y="%d" text-anchor="middle" font-size="10" '
                   'fill="var(--muted)">%s</text>' % (xp(x), H - plb + 16, esc(lab)))
    out.append('<text x="%.0f" y="%d" text-anchor="middle" font-size="11" '
               'fill="var(--ink2)">%s</text>' % ((plw + W - plr) / 2, H - 10, esc(x_title)))
    out.append('<text x="16" y="%.0f" transform="rotate(-90 16 %.0f)" text-anchor="middle" '
               'font-size="11" fill="var(--ink2)">%s</text>'
               % ((H - plb + plt_) / 2, (H - plb + plt_) / 2, esc(y_title)))
    pts = " ".join("%.1f,%.1f" % (xp(p[0]), yp(p[1])) for p in points)
    out.append('<polyline points="%s" fill="none" stroke="%s" stroke-width="2" '
               'stroke-linejoin="round"/>' % (pts, PALETTE["s1"]))
    for p in points:
        out.append('<circle cx="%.1f" cy="%.1f" r="4.5" fill="%s" stroke="var(--surface)" '
                   'stroke-width="2"><title>%s: %s</title></circle>'
                   % (xp(p[0]), yp(p[1]), PALETTE["s1"],
                      esc(p[2] if len(p) > 2 else p[0]), fmt(p[1])))
    out.append("</svg>")
    tbl = table([x_title, y_title],
                [[p[2] if len(p) > 2 else p[0], fmt(p[1])] for p in points])
    return "".join(out), tbl


def table(headers, rows, caption=None):
    o = ['<div class="tw"><table>']
    if caption:
        o.append("<caption>%s</caption>" % esc(caption))
    o.append("<thead><tr>" + "".join("<th>%s</th>" % h for h in headers) + "</tr></thead><tbody>")
    for r in rows:
        o.append("<tr>" + "".join("<td>%s</td>" % c for c in r) + "</tr>")
    o.append("</tbody></table></div>")
    return "".join(o)


def figure(num, title, svg, tbl, note=None):
    if not svg:
        return ""
    return ('<figure><div class="chart">%s</div>'
            '<figcaption><b>Figure %d.</b> %s</figcaption>%s'
            '<details><summary>Data table</summary>%s</details></figure>'
            % (svg, num, esc(title), ('<p class="note">%s</p>' % esc(note)) if note else "", tbl))


CSS = """
:root{color-scheme:light;--bg:#f7f8fa;--surface:#fff;--panel:#f2f4f7;--ink:#0f1720;
--ink2:#3d4a57;--muted:#6b7784;--rule:#dde2e8;--accent:#1b4f8f;--accent2:#2a78d6;
--good:#0ca30c;--warn:#b8860b;--crit:#c0392b}
@media(prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#0d1117;--surface:#161b22;
--panel:#1c2230;--ink:#f0f3f6;--ink2:#c3ccd6;--muted:#8b98a5;--rule:#2b3440;--accent:#6aa9ee;
--accent2:#3987e5}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.6 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:900px;margin:0 auto;padding:0 22px 80px}
header.rpt{background:var(--accent);color:#fff;margin:0 -22px 30px;padding:34px 22px 26px}
/* The generic p rule sets a dark ink colour, which wins over the header's inherited
   white and renders these invisible on the navy banner. Set them explicitly. */
header.rpt p{color:#fff}
header.rpt .kicker{font-size:.72rem;letter-spacing:.14em;text-transform:uppercase;opacity:.85}
header.rpt h1{margin:8px 0 6px;font-size:1.85rem;line-height:1.2;letter-spacing:-.01em}
header.rpt .sub{opacity:.9;margin:0;max-width:52em}
header.rpt .facts{display:flex;flex-wrap:wrap;gap:8px 26px;margin-top:18px;font-size:.83rem;
opacity:.92}
h2{font-size:1.22rem;margin:40px 0 10px;padding-bottom:7px;border-bottom:2px solid var(--accent);
letter-spacing:-.005em}
h3{font-size:1rem;margin:24px 0 8px;color:var(--ink)}
p{margin:0 0 14px;color:var(--ink2)}
p.note{font-size:.86rem;color:var(--muted);margin:6px 0 0}
b,strong{color:var(--ink)}
ul{color:var(--ink2);padding-left:20px;margin:0 0 14px}
li{margin-bottom:6px}
code{background:var(--panel);padding:1px 5px;border-radius:4px;font-size:.87em;
font-family:ui-monospace,Consolas,monospace}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(158px,1fr));gap:12px;margin:22px 0}
.kpi{background:var(--surface);border:1px solid var(--rule);border-left:3px solid var(--accent2);
border-radius:7px;padding:14px 15px}
.kpi .v{font-size:1.5rem;font-weight:650;line-height:1.15}
.kpi .k{font-size:.78rem;color:var(--muted);margin-top:3px}
.tw{overflow-x:auto;border:1px solid var(--rule);border-radius:7px;background:var(--surface);
margin:12px 0 18px}
table{border-collapse:collapse;width:100%;font-size:.87rem}
caption{text-align:left;padding:9px 12px;color:var(--muted);font-size:.82rem}
th,td{text-align:left;padding:8px 12px;border-bottom:1px solid var(--rule);
font-variant-numeric:tabular-nums}
th{background:var(--panel);font-weight:600;color:var(--ink);white-space:nowrap}
tbody tr:last-child td{border-bottom:0}
figure{margin:22px 0 26px}
.chart{background:var(--surface);border:1px solid var(--rule);border-radius:7px;padding:8px}
figcaption{font-size:.88rem;color:var(--ink2);margin-top:9px}
details{margin-top:8px}
summary{cursor:pointer;font-size:.85rem;color:var(--muted)}
.callout{background:var(--panel);border-left:3px solid var(--accent2);border-radius:6px;
padding:13px 16px;margin:0 0 16px}
.callout.warn{border-left-color:var(--warn)}
.callout.crit{border-left-color:var(--crit)}
.callout p:last-child{margin:0}
.pill{display:inline-block;font-size:.72rem;padding:2px 8px;border-radius:20px;
background:var(--panel);color:var(--ink2);border:1px solid var(--rule)}
footer{margin-top:44px;padding-top:16px;border-top:1px solid var(--rule);
color:var(--muted);font-size:.82rem}
@media print{
 :root{--bg:#fff;--surface:#fff;--panel:#f3f3f3;--ink:#000;--ink2:#222;--rule:#bbb}
 body{font-size:10pt}main{max-width:none;padding:0}
 header.rpt{background:#fff!important;color:#000!important;border-bottom:3px solid #1b4f8f;
 margin:0 0 20px;padding:0 0 14px}
 header.rpt p{color:#000}
 header.rpt .kicker{color:#1b4f8f}
 h2{break-before:page;break-after:avoid}
 figure,table,.callout,.kpis{break-inside:avoid}
 th,td{white-space:normal;overflow-wrap:break-word;font-size:8.5pt}
 summary{display:none}
 .tw{overflow:visible}
}
@page{size:A4;margin:16mm 14mm 18mm}
"""


# ---------------------------------------------------------------- extraction

def _envf(name):
    """A float from the environment, or None. Deployment facts this tool cannot observe are
    supplied this way rather than defaulted, so a missing input yields a stated omission instead
    of a number carried over from whichever machine the code was written on."""
    v = os.environ.get(name, "").strip()
    try:
        return float(v) if v else None
    except ValueError:
        return None


def _probe(res, name):
    return (res.get("probes") or {}).get(name) or {}


def _gpu_models(res):
    inv = _probe(res, "inventory")
    return [g.get("name") for g in inv.get("gpus", []) if g.get("name")]


def _best_matmul(res, comparable_only=False):
    """Highest measured value per dtype, across devices.

    comparable_only selects the records measured at the size EVERY precision was measured at.
    Without that control, the sizer hands narrow types a larger matrix than wide ones, and a
    cross-precision table ends up ranking matrix sizes while appearing to rank precisions.
    """
    out = {}
    for m in _probe(res, "torch_compute").get("matmul", []):
        if comparable_only and not m.get("comparable"):
            continue
        dt = m.get("dtype")
        if dt and (dt not in out or m["tflops_best"] > out[dt]["tflops_best"]):
            out[dt] = m
    return out


def _bandwidth(res):
    vals = []
    for probe in ("torch_compute", "cuda_driver"):
        for b in _probe(res, probe).get("memory_bandwidth", []):
            vals.append((b.get("device"), b["gb_s_best"], probe))
    return vals


def _host_transfer(res):
    rows = []
    seen = set()
    for probe in ("torch_compute", "cuda_driver"):
        for hrec in _probe(res, probe).get("host_transfer", []):
            key = (hrec.get("device"), hrec.get("direction"))
            if key in seen:
                continue
            seen.add(key)
            rows.append((hrec.get("device"), hrec.get("direction"), hrec["gb_s_best"]))
    return rows


def _cache_sweep(res):
    for probe in ("torch_compute", "cuda_driver"):
        rows = _probe(res, probe).get("cache_sweep", [])
        if rows:
            by = {}
            for r in rows:
                by.setdefault(r.get("size_mib"), []).append(r["gb_s_best"])
            return sorted((k, max(v)) for k, v in by.items() if k)
    return []


def _reference_for(res):
    """Reference figures for the GPU under test, and where each came from."""
    models = _gpu_models(res)
    if models and models[0] in DATASHEETS:
        return DATASHEETS[models[0]], "vendor datasheet"
    # No published sheet on file: fall back to what the device itself reports. This is weaker but
    # honest, and far better than citing a spec page for an ambiguous laptop SKU.
    dev = (_probe(res, "cuda_driver").get("devices") or [{}])[0]
    ref = {}
    if dev.get("naive_peak_bandwidth_gb_s"):
        ref["mem_bw_gb_s"] = (dev["naive_peak_bandwidth_gb_s"],
                              "derived from driver-reported memory clock x bus width")
    return ref, "device-reported"


# ---------------------------------------------------------------- report

def _attribution(res):
    """Decompose one decode step into bandwidth, interconnect and unexplained.

    This is the analysis that turns a benchmark into a decision: a throughput number says how
    fast, a decomposition says what to fix. Returns None unless every input is present.
    """
    t = _probe(res, "torch_compute")
    nc = _probe(res, "nccl_allreduce")
    sb = _probe(res, "serve_bench")
    lvl = next((l for l in sb.get("levels", []) if l.get("concurrency") == 1), None)
    itl = (lvl or {}).get("itl_ms", {}).get("p50")
    rows = sorted(nc.get("results", []), key=lambda r: r["size_bytes"])
    bw = max((b["gb_s_best"] for b in t.get("memory_bandwidth", [])), default=None)
    if not (itl and rows and bw):
        return None
    # Decode attribution needs two facts about the SERVED MODEL that no probe here can observe:
    # weight bytes resident per shard, and layer count. Earlier revisions carried the values from
    # the machine this tool was written on. That is the worst kind of defect in a portable
    # benchmark: on any other deployment it produces a confident, plausible, wrong attribution
    # rather than an error. They are now explicit inputs, and absent them the section is omitted
    # with the reason printed instead of guessed at.
    shard_gib = _envf("GPUBENCH_SHARD_WEIGHT_GIB")
    layers = _envf("GPUBENCH_MODEL_LAYERS")
    if not (shard_gib and layers):
        return {"unavailable": "Decode attribution needs the served model's resident weight bytes "
                               "per shard and its layer count, which this tool cannot observe from "
                               "outside the engine. Set GPUBENCH_SHARD_WEIGHT_GIB and "
                               "GPUBENCH_MODEL_LAYERS (both readable from the engine's own startup "
                               "log) to enable it. No default is assumed, because a wrong "
                               "attribution is worse than a missing one."}

    tp = _envf("GPUBENCH_TP_SIZE") or 2.0
    # Derivations come from analysis.py, which is unit-tested without a GPU. This function's job is
    # to find the inputs, not to do the arithmetic.
    floor = analysis.decode_floor(shard_gib * analysis.GIB, bw)
    floor_ms = floor["step_floor_ms"]

    # Message size per all-reduce during decode is hidden_size * 2 bytes for one token, which for
    # every model in this class lands in the FLAT, latency-bound part of the curve. Nearest-point
    # lookup is therefore correct here and interpolation would add nothing. The guard exists so
    # that assumption is checked rather than trusted: if the sweep has no sample close to the
    # target size, say so instead of silently using a distant one.
    target_kib = (_envf("GPUBENCH_DECODE_MSG_KIB") or 10.0)
    near = min(rows, key=lambda r: abs(r["size_kib"] - target_kib))
    if near["size_kib"] > target_kib * 4 or near["size_kib"] < target_kib / 4:
        return {"unavailable": "The all-reduce sweep has no sample within a factor of four of the "
                               "%.1f KiB decode message size, so no latency can be attributed "
                               "without interpolating across regimes." % target_kib}

    comms_ms = near["latency_ms"] * layers * 2 if tp > 1 else 0.0
    att = analysis.decode_attribution(itl, floor_ms, comms_ms)
    att.update({
        "measured_itl_ms": itl,
        "smallest_us": rows[0]["latency_ms"] * 1000,
        "fraction_of_ceiling": att["fraction_of_bandwidth_ceiling"],
        "inputs": {"shard_weight_gib": shard_gib, "layers": layers, "tp_size": tp,
                   "allreduces_per_token": layers * 2 if tp > 1 else 0,
                   "sample_used_kib": near["size_kib"]},
    })
    return att


def write_report(res, path, title=None, subtitle=None):
    inv = _probe(res, "inventory")
    torch_p = _probe(res, "torch_compute")
    drv = _probe(res, "cuda_driver")
    host = inv.get("host", {})
    gpus = inv.get("gpus", [])
    models = _gpu_models(res)
    ref, ref_kind = _reference_for(res)
    mm = _best_matmul(res)
    fignum = [0]

    def fig(t, svg, tbl, note=None):
        fignum[0] += 1
        return figure(fignum[0], t, svg, tbl, note)

    name = models[0] if models else "GPU"
    count = len(gpus)
    title = title or ("%s%s benchmark report" % (("%dx " % count) if count > 1 else "", name))
    subtitle = subtitle or ("Measured capability, achieved-versus-reference, and the "
                            "constraint that actually binds this system.")

    h = []
    A = h.append

    A('<header class="rpt"><p class="kicker">GPU benchmark report</p>')
    A('<h1>%s</h1>' % esc(title))
    A('<p class="sub">%s</p>' % esc(subtitle))
    facts = [("Generated", datetime.datetime.now().strftime("%d %B %Y")),
             ("Mode", res.get("mode", "shared")),
             ("Profile", res.get("profile", "base")),
             ("Transport", (res.get("orchestrator") or {}).get("transport", "?")),
             ("Result fingerprint", (res.get("fingerprint") or {}).get("hash", "?"))]
    A('<div class="facts">%s</div>' % "".join(
        "<span><b>%s:</b> %s</span>" % (esc(k), esc(v)) for k, v in facts))
    A('</header>')

    # ---------------- executive summary
    A('<h2>Executive summary</h2>')
    kpis = []
    peak_key = max(mm, key=lambda k: mm[k]["tflops_best"]) if mm else None
    if peak_key:
        kpis.append((fmt(mm[peak_key]["tflops_best"], 0),
                     "peak %s (%s)" % (DTYPE_LABEL.get(peak_key, peak_key),
                                       UNIT.get(peak_key, "TFLOPS"))))
    bw = _bandwidth(res)
    if bw:
        kpis.append((fmt(max(b[1] for b in bw), 0) + " GB/s", "memory bandwidth"))
    ht = _host_transfer(res)
    if ht:
        kpis.append((fmt(max(x[2] for x in ht), 1) + " GB/s", "host transfer, best link"))
        if len({x[0] for x in ht}) > 1:
            worst = min(x[2] for x in ht)
            kpis.append((fmt(worst, 1) + " GB/s", "host transfer, worst link"))
    sus = torch_p.get("sustained", [])
    if sus:
        s0 = max(sus, key=lambda s: s["sustained_tflops"])
        kpis.append((fmt(s0["sustained_tflops"], 0), "sustained BF16 TFLOPS"))
        if s0.get("tflops_per_watt"):
            kpis.append((fmt(s0["tflops_per_watt"], 2), "TFLOPS per watt"))
    if gpus:
        kpis.append(("%d x %s GiB" % (count, fmt((gpus[0].get("memory_total") or 0) / 1024.0, 0)),
                     "VRAM"))
    A('<div class="kpis">%s</div>' % "".join(
        '<div class="kpi"><div class="v">%s</div><div class="k">%s</div></div>' % (v, esc(k))
        for v, k in kpis))

    findings = build_findings(res, mm, ref, bw, ht, sus)
    if findings:
        A("<ul>" + "".join("<li>%s</li>" % f for f in findings) + "</ul>")

    # ---------------- system under test
    A('<h2>System under test</h2>')
    rows = [["Host operating system", esc(host.get("os", "&mdash;"))],
            ["CPU", "%s (%s threads)" % (esc(host.get("cpu", "?")), host.get("cpu_threads", "?"))]]
    if host.get("memory_bytes"):
        rows.append(["System memory", "%s GiB" % fmt(host["memory_bytes"] / 2**30, 0)])
    if host.get("board_name"):
        rows.append(["Motherboard", "%s %s, BIOS %s" % (esc(host.get("board_vendor", "")),
                                                        esc(host["board_name"]),
                                                        esc(host.get("bios_version", "?")))])
    for g in gpus:
        rows.append(["GPU %s" % g.get("index"),
                     "%s, %s GiB, driver %s" % (esc(g.get("name")),
                                                fmt((g.get("memory_total") or 0) / 1024.0, 0),
                                                esc(g.get("driver_version")))])
        rows.append(["GPU %s PCIe link" % g.get("index"),
                     "gen%s x%s in use, card supports gen%s x%s"
                     % (g.get("pcie_link_gen_current"), g.get("pcie_link_width_current"),
                        g.get("pcie_link_gen_max"), g.get("pcie_link_width_max"))])
    if torch_p.get("torch_version"):
        rows.append(["Compute runtime", "PyTorch %s, CUDA %s"
                     % (esc(torch_p["torch_version"]), esc(torch_p.get("cuda_version")))])
    if drv.get("driver_api_version"):
        rows.append(["CUDA driver API", esc(drv["driver_api_version"])])
    for d in drv.get("devices", []):
        rows.append(["GPU %s silicon" % d.get("index"),
                     "compute capability %s, %s SMs, %s-bit bus, %s MiB L2"
                     % (d.get("compute_capability"), d.get("multiprocessor_count"),
                        d.get("global_memory_bus_width"),
                        fmt((d.get("l2_cache_size") or 0) / 2**20, 0))])
    A(table(["Item", "Detail"], rows))

    for l in inv.get("pcie_links", []):
        A('<p class="note">PCIe topology: <code>%s</code> attaches to bridge <code>%s</code>, '
          'which offers %s x%s.</p>' % (esc(l["bdf"]), esc(l["parent_bridge"]),
                                        esc(l.get("bridge_max_speed")), l.get("bridge_max_width")))

    # ---------------- compute
    A('<h2>Compute throughput by precision</h2>')
    if mm:
        A('<p>Every precision the vendor advertises, measured through the same library the '
          'production stack uses. INT8 and FP4 are quoted in TOPS; the rest in TFLOPS. The '
          'arithmetic is identical.</p>')
        order = ["float4_e2m1", "int8", "float8_e4m3fn", "bfloat16", "float16", "tf32",
                 "float32_shader"]
        rows = [(DTYPE_LABEL.get(k, k), mm[k]["tflops_best"], "s1")
                for k in order if k in mm]
        svg, tbl = svg_bar(rows, "Compute throughput by precision", "TFLOPS or TOPS")
        A(fig("Peak compute by precision, best of the measured devices.", svg, tbl,
              "Burst measurement: best of several timed iterations, not steady state."))

        # achieved vs reference
        ctrl = _best_matmul(res, comparable_only=True)
        cmp_rows = []
        sizes = set()
        for k in order:
            if k not in ctrl:
                continue
            spec = ref.get(DTYPE_TO_SPEC.get(k, ""))
            if not spec:
                continue
            meas = ctrl[k]["tflops_best"]
            sizes.add(ctrl[k]["n"])
            cmp_rows.append([DTYPE_LABEL.get(k, k), fmt(spec[0]), fmt(meas),
                             "<b>%s%%</b>" % fmt(meas / spec[0] * 100, 0), spec[1]])
        if cmp_rows:
            A('<h3>Achieved against reference</h3>')
            A('<p>Every row below was measured at <b>the same matrix size (n = %s)</b>. That '
              'control matters: sizing each precision to its own memory footprint hands the '
              'narrow types a larger matrix than the wide ones, and the resulting table ranks '
              'matrix sizes while appearing to rank precisions.</p>'
              % ", ".join(str(x) for x in sorted(sizes)))
            A(table(["Precision", "Reference", "Measured", "Achieved", "Provenance of reference"],
                    cmp_rows))
            A('<p class="note">A reference figure describes a hardware capability under ideal '
              'conditions. A single library call is not that, so a shortfall is expected; what '
              'matters is which precisions fall furthest short and why.</p>')
    else:
        A('<div class="callout warn"><p>No compute measurements: this target has no PyTorch '
          'runtime available, so tensor throughput could not be measured. Inventory, memory '
          'bandwidth and host transfer below come from the driver API and are unaffected.</p>'
          '</div>')

    # ---------------- memory
    A('<h2>Memory system</h2>')
    if bw:
        # Two probes may both report bandwidth for the same device; keep the best per device
        # rather than emitting a duplicate bar for each probe.
        per_dev = {}
        for _d, _v, _p in bw:
            per_dev[_d] = max(per_dev.get(_d, 0), _v)
        rows = [("GPU %s" % d, v, "s1") for d, v in sorted(per_dev.items())]
        spec = ref.get("mem_bw_gb_s")
        svg, tbl = svg_bar(rows, "Memory copy bandwidth", "GB/s",
                           max_hint=(spec[0] if spec else None),
                           ref=(spec[0] if spec else None),
                           ref_label="reference" if spec else "")
        A(fig("Device memory copy bandwidth, read and write both counted.", svg, tbl))
        if spec:
            best = max(v for _d, v, _p in bw)
            A('<p>Best measured <b>%s GB/s</b> against a reference of %s GB/s (%s), '
              'which is <b>%s%%</b>. A copy loop saturates a memory bus easily, so this is the '
              'precision-independent measurement that should land closest to its reference.</p>'
              % (fmt(best, 0), fmt(spec[0], 0), esc(spec[1]), fmt(best / spec[0] * 100, 0)))
    sweep = _cache_sweep(res)
    if sweep:
        pts = [(mb, gb, "%g MiB" % mb) for mb, gb in sweep]
        svg, tbl = svg_line(pts, "Bandwidth against working-set size",
                            "Working set (log)", "GB/s")
        A(fig("Copy bandwidth against working-set size, across the cache boundary.", svg, tbl,
              "Small transfers are dominated by launch overhead rather than bandwidth, which is "
              "why the curve rises before it plateaus."))

    # ---------------- interconnect
    A('<h2>Host interconnect</h2>')
    if ht:
        rows = [("GPU %s %s" % (d, direction.upper()), v,
                 "s2" if v < max(x[2] for x in ht) * 0.6 else "s1")
                for d, direction, v in sorted(ht)]
        svg, tbl = svg_bar(rows, "Host transfer bandwidth", "GB/s")
        A(fig("Host-to-device and device-to-host bandwidth over PCIe, pinned memory.", svg, tbl))
        devs = {d for d, _dir, _v in ht}
        if len(devs) > 1:
            per = {}
            for d, _dir, v in ht:
                per[d] = max(per.get(d, 0), v)
            hi, lo = max(per.values()), min(per.values())
            if lo and hi / lo > 1.5:
                slow = [d for d, v in per.items() if v == lo][0]
                A('<div class="callout crit"><p><b>GPU %s reaches only %s GB/s against %s GB/s '
                  'for its peer, a %sx gap.</b> The cards are otherwise near-identical, so this '
                  'is the slot rather than the silicon. Any workload that splits a model across '
                  'both GPUs pays this cost on every exchange between them.</p></div>'
                  % (slow, fmt(lo, 1), fmt(hi, 1), fmt(hi / lo, 1)))
    p2p = torch_p.get("p2p", [])
    if p2p:
        A(table(["From", "To", "Peer access", "GB/s"],
                [[p["src"], p["dst"], "yes" if p["peer_access"] else "no",
                  fmt(p["gb_s_best"], 1)] for p in p2p]))
        if not any(p["peer_access"] for p in p2p):
            A('<p class="note">Peer access is unavailable, so device-to-device copies stage '
              'through host memory rather than moving directly between cards.</p>')

    # ---------------- sustained and power
    if sus:
        A('<h2>Sustained load and power</h2>')
        A('<p>A burst measurement reports the best few milliseconds. This section runs the GPU '
          'continuously and samples power throughout, which is the only way to state steady-state '
          'throughput or efficiency per watt.</p>')
        rows = []
        for s in sorted(sus, key=lambda x: x["device"]):
            p = s.get("power") or {}
            burst = next((m["tflops_best"] for m in torch_p.get("matmul", [])
                          if m["device"] == s["device"] and m["dtype"] == "bfloat16"), None)
            rows.append([
                "GPU %s" % s["device"], fmt(s["seconds"], 0),
                fmt(burst) if burst else "&mdash;",
                fmt(s["sustained_tflops"]),
                ("%s%%" % fmt(s["sustained_tflops"] / burst * 100, 0)) if burst else "&mdash;",
                fmt(p.get("power_mean_w"), 0), fmt(p.get("sm_clock_mean_mhz"), 0),
                fmt(p.get("temp_max_c"), 0), fmt(s.get("tflops_per_watt"), 2)])
        A(table(["Device", "Seconds", "Burst TFLOPS", "Sustained TFLOPS", "Retained",
                 "Mean power (W)", "Mean SM clock (MHz)", "Peak temp (C)", "TFLOPS/W"], rows))
        tgp = ref.get("tgp_w")
        s0 = max(sus, key=lambda s: s["sustained_tflops"])
        pw = (s0.get("power") or {}).get("power_mean_w")
        if tgp and pw:
            pct = pw / tgp[0] * 100
            cls = "callout" if pct < 92 else "callout warn"
            A('<div class="%s"><p>Mean draw under sustained load was <b>%s W against a %s W '
              'limit (%s%%)</b>. %s</p></div>'
              % (cls, fmt(pw, 0), fmt(tgp[0], 0), fmt(pct, 0),
                 "The board is running at its power ceiling, so this is the power limit "
                 "defining throughput, not cooling and not the silicon."
                 if pct >= 92 else
                 "There is headroom against the power limit, so throughput is bounded by "
                 "something other than power."))

    # ---------------- serving, interconnect, attribution, embedding, accuracy
    sb = _probe(res, "serve_bench")
    if sb.get("levels"):
        A("<h2>Serving performance</h2>")
        A("<p>Throughput is quoted with its latency bound throughout. A throughput figure without "
          "one can always be improved by making every individual request slower.</p>")
        A(table(["Concurrent", "Output tok/s", "Per stream", "TTFT p50 (s)", "TTFT p95 (s)",
                 "ITL p50 (ms)"],
                [[l["concurrency"], fmt(l["output_tokens_per_s"], 0),
                  fmt(l.get("per_request_output_tokens_per_s"), 1),
                  fmt(l["ttft_s"]["p50"], 2), fmt(l["ttft_s"]["p95"], 2),
                  fmt(l["itl_ms"]["p50"], 1)] for l in sb["levels"]]))
    nc = _probe(res, "nccl_allreduce")
    if nc.get("results"):
        rows = sorted(nc["results"], key=lambda r: r["size_bytes"])
        A("<h2>Interconnect: the tensor-parallel primitive</h2>")
        A("<p>Swept by message size, because small and large messages behave as two different "
          "regimes and only one of them is fixed by a wider link.</p>")
        A(table(["Message", "Latency (ms)", "Effective GB/s"],
                [["%.0f KiB" % r["size_kib"] if r["size_kib"] < 1024
                  else "%.0f MiB" % (r["size_kib"] / 1024),
                  fmt(r["latency_ms"], 4), fmt(r["bus_gb_s"], 2)] for r in rows]))
        A("<p>The smallest message measured costs %s us, and the curve is flat across the first "
          "few sizes, so that floor is launch and synchronisation rather than transfer. A wider "
          "slot cannot remove it.</p>" % fmt(rows[0]["latency_ms"] * 1000, 1))
    # ---------------------------------------------------------------- diagnostics
    # Placed BEFORE the numbers on purpose. A reader who is about to quote a figure should first
    # see what this run could and could not establish about it. Putting caveats after the tables
    # is how a caveat gets skipped.
    dg = res.get("diagnostics") or {}
    if dg.get("findings"):
        sm = dg.get("summary") or {}
        A("<h2>What this run establishes, and what it does not</h2>")
        A("<p>Generated conclusions, each carrying the readings it rests on, ordered by how much a "
          "reader needs to act on them: <b>%d must fix</b>, %d carry a caveat, %d are findings, and "
          "<b>%d could not be reached</b> because an input was missing. That last count is stated "
          "up front rather than buried at the end of the table, because a check that did not run is "
          "not an all-clear; each one names what to supply.</p>"
          % (sm.get("blocking", 0), sm.get("warning", 0), sm.get("info", 0), sm.get("unknown", 0)))
        BADGE = {"blocking": ("#b4232b", "must fix"), "warning": ("#a8620a", "caveat"),
                 "info": ("#14508f", "finding"), "unknown": ("#5c6470", "not established")}
        rows = []
        for f in dg["findings"]:
            colour, label = BADGE.get(f["severity"], ("#5c6470", f["severity"]))
            bits = ["<b>%s</b>" % esc(f["headline"]), esc(f["detail"])]
            if f.get("action"):
                bits.append("<i>What to do:</i> %s" % esc(f["action"]))
            if f.get("do_not_overstate"):
                bits.append("<i>Do not overstate:</i> %s" % esc(f["do_not_overstate"]))
            ev = ", ".join("%s=%s" % (k, v) for k, v in (f.get("evidence") or {}).items()
                           if v is not None)
            if ev:
                bits.append("<span style='color:var(--muted)'>Evidence: %s</span>" % esc(ev))
            rows.append(['<span style="color:%s;font-weight:650">%s</span>' % (colour, label),
                         esc(f["rule"]), "<br>".join(bits)])
        A(table(["", "Check", "Conclusion"], rows))

    att = _attribution(res)
    if att and att.get("unavailable"):
        A("<h2>Where one decode step goes</h2>")
        A('<p class="note"><b>Not attributed.</b> %s</p>' % html.escape(att["unavailable"]))
    elif att:
        tot = att["measured_itl_ms"]
        inp = att.get("inputs") or {}
        A("<h2>Where one decode step goes</h2>")
        A(table(["Component", "ms per token", "Share"], [
            ["Reading weights from memory", fmt(att["bandwidth_floor_ms"], 2),
             "%s%%" % fmt(att["bandwidth_floor_ms"] / tot * 100, 0)],
            ["Tensor-parallel all-reduce", fmt(att["comms_ms"], 2),
             "%s%%" % fmt(att["comms_ms"] / tot * 100, 0)],
            ["Everything else", fmt(att["unexplained_ms"], 2),
             "%s%%" % fmt(att["unexplained_ms"] / tot * 100, 0)],
            ["<b>Measured step</b>", "<b>%s</b>" % fmt(tot, 2), "<b>100%</b>"]]))
        A("<p>Decode reaches <b>%s%% of this machine's own memory-bandwidth ceiling</b>. The "
          "residual is what the model of the machine failed to explain, and it is reported rather "
          "than hidden.</p>" % fmt(att["fraction_of_ceiling"] * 100, 0))
        # Every input to the arithmetic above, so a reader can rebuild it or reject it. An
        # attribution whose assumptions are not visible is an assertion.
        A('<p class="note">Derived from: %s GiB resident weights per shard, %s layers, tensor '
          'parallel %s, giving %s all-reduces per token; the all-reduce latency used is the '
          'measured sample at %s KiB, which is in the flat, latency-bound part of the sweep. '
          'Substitute any of these and the arithmetic is reproducible from the tables above.</p>'
          % (fmt(inp.get("shard_weight_gib"), 2), fmt(inp.get("layers"), 0),
             fmt(inp.get("tp_size"), 0), fmt(inp.get("allreduces_per_token"), 0),
             fmt(inp.get("sample_used_kib"), 1)))
    eb = _probe(res, "embed_bench")
    if eb.get("cells"):
        A("<h2>Embedding service</h2>")
        A("<p>Retrieval pipelines usually bottleneck here before the model becomes the "
          "constraint.</p>")
        A(table(["Batch", "Concurrency", "Embeddings/s", "Latency p95 (s)"],
                [[c["batch"], c["concurrency"], fmt(c["embeddings_per_s"], 0),
                  fmt(c["latency_s"]["p95"], 3)] for c in eb["cells"]]))
    acc = _probe(res, "accuracy")
    if acc.get("summary"):
        sm = acc["summary"]
        A("<h2>Accuracy gate</h2>")
        A("<p>A speed benchmark cannot distinguish faster from worse. This runs beside the "
          "performance measurements so a change that buys throughput by degrading output is "
          "visible instead of celebrated.</p>")
        A(table(["Check", "Result"], [
            ["Deterministic under greedy decode", "%d of %d" % (sm["deterministic"], sm["cases"])],
            ["Exact match", "%d of %d" % (sm["correct"], sm["cases"])],
            ["Verdict", "<b>%s</b>" % sm["verdict"]]]))

    # ---------------- capabilities beyond the tensor cores
    caps = _probe(res, "capabilities")
    if caps and (caps.get("int4") or caps.get("video_encode") or caps.get("not_measured")):
        A('<h2>Other advertised capabilities</h2>')
        A('<p>A vendor lists more than tensor throughput. This section accounts for the rest, '
          'including the parts that cannot be measured and why.</p>')
        if caps.get("int4"):
            best4 = max(caps["int4"], key=lambda r: r["tops_equivalent"])
            A('<h3>INT4</h3>')
            A(table(["Scheme", "Measured", "What it is"],
                    [["W4A16 (4-bit weights, 16-bit activations)",
                      "%s TOPS-equivalent" % fmt(best4["tops_equivalent"]),
                      "The kernel production inference actually runs: weights stored at 4 bits and "
                      "dequantised into a 16-bit matmul."]]))
            A('<div class="callout warn"><p><b>This must not be compared against a datasheet INT4 '
              'figure.</b> A datasheet quotes a dense INT4 tensor-core rate; this measures a '
              'weight-only quantisation kernel. They differ by roughly an order of magnitude and '
              'answer different questions. The weight-only number is the one that predicts serving '
              'behaviour; the dense rate is the one that sells the card.</p></div>')
        if caps.get("video_encode") or caps.get("video_decode"):
            A('<h3>Video encode and decode engines</h3>')
            A('<p>Separate silicon from the CUDA and tensor cores, so this runs without competing '
              'for the compute the rest of this report measures.</p>')
            rows = []
            for r in caps.get("video_encode", []):
                rows.append([r["codec"], "encode", r["resolution"], fmt(r["fps"], 0),
                             "%sx" % fmt(r["realtime_x_at_60fps"], 1)])
            for r in caps.get("video_decode", []):
                rows.append([r["codec"], "decode", r["resolution"], fmt(r["fps"], 0),
                             "%sx" % fmt(r["realtime_x_at_60fps"], 1)])
            A(table(["Codec", "Direction", "Resolution", "Frames/s", "Real-time at 60 fps"], rows))
        if caps.get("engines", {}).get("rt_cores"):
            A('<h3>Ray tracing cores</h3>')
            A('<p>%s RT cores enumerated from the device. Not measured: see below.</p>'
              % caps["engines"]["rt_cores"])
        if caps.get("not_measured"):
            A('<h3>Accounted for but not measured</h3>')
            A('<p>Listed explicitly. Silence on a published capability reads as an oversight; a '
              'stated reason is a decision.</p>')
            A(table(["Capability", "Why not", "What would close it"],
                    [[n["capability"], n["reason"], n.get("how_to_close", "")]
                     for n in caps["not_measured"]]))

    # ---------------- conditions
    A('<h2>Measurement conditions and limitations</h2>')
    warns = inv.get("warnings", [])
    if warns:
        A('<p>Conditions detected at capture that shape every number above:</p>')
        A("<ul>" + "".join("<li>%s</li>" % esc(w) for w in warns) + "</ul>")
    errs = []
    for pname, probe in (res.get("probes") or {}).items():
        errs += ["<code>%s</code>: %s" % (esc(pname), esc(e)) for e in (probe.get("errors") or [])]
    if errs:
        A('<h3>Measurements not obtained</h3>')
        A("<ul>" + "".join("<li>%s</li>" % e for e in errs) + "</ul>")
        A('<p class="note">Listed rather than omitted: a metric that could not be measured is '
          'different from a metric that measured zero.</p>')
    A('<ul>')
    A('<li>Mode <b>%s</b>. %s</li>' % (
        esc(res.get("mode")),
        "Another workload was resident, so compute and bandwidth figures are lower bounds."
        if res.get("mode") == "shared" else
        "The device was measured with no other workload resident."))
    A('<li>Single sample of a single machine. Nothing here establishes variance across parts.</li>')
    A('<li>Burst compute figures are best-of-N timed iterations; sustained figures are the '
      'steady-state equivalent and are the ones to use for capacity planning.</li>')
    A('</ul>')

    A('<footer>Generated by gpubench from result schema %s. '
      'Fingerprint <code>%s</code>: two results are only comparable when this matches. '
      'Reference figures are %s.</footer>'
      % (esc(res.get("schema_version", "?")),
         esc((res.get("fingerprint") or {}).get("hash", "?")), esc(ref_kind)))

    doc = ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
           '<meta name="viewport" content="width=device-width,initial-scale=1">'
           '<title>%s</title><style>%s</style></head><body><main>%s</main></body></html>'
           % (esc(title), CSS, "".join(h)))
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)
    return path


def build_findings(res, mm, ref, bw, ht, sus):
    """The three or four sentences someone would actually repeat from this report."""
    f = []
    inv = _probe(res, "inventory")
    gpus = inv.get("gpus", [])
    if len(gpus) > 1 and ht:
        per = {}
        for d, _dir, v in ht:
            per[d] = max(per.get(d, 0), v)
        if per and min(per.values()) and max(per.values()) / min(per.values()) > 1.5:
            f.append("The GPUs are near-identical in compute and memory but differ by "
                     "<b>%sx in host bandwidth</b>, so the interconnect, not the silicon, is the "
                     "asymmetry in this machine."
                     % fmt(max(per.values()) / min(per.values()), 1))
    ctrl = _best_matmul(res, comparable_only=True) or mm
    if ctrl and ref:
        pairs = [(k, ctrl[k]["tflops_best"] / ref[DTYPE_TO_SPEC[k]][0] * 100)
                 for k in ctrl if DTYPE_TO_SPEC.get(k) in ref]
        if pairs:
            best = max(pairs, key=lambda p: p[1])
            worst = min(pairs, key=lambda p: p[1])
            f.append("At a single controlled matrix size, throughput reaches <b>%s%% of "
                     "reference at %s</b> and only <b>%s%% at %s</b>: narrow data types get far "
                     "closer to their published rate than wide ones."
                     % (fmt(best[1], 0), DTYPE_LABEL.get(best[0], best[0]),
                        fmt(worst[1], 0), DTYPE_LABEL.get(worst[0], worst[0])))
    if sus:
        s0 = max(sus, key=lambda s: s["sustained_tflops"])
        burst = next((m["tflops_best"] for m in _probe(res, "torch_compute").get("matmul", [])
                      if m["device"] == s0["device"] and m["dtype"] == "bfloat16"), None)
        if burst:
            f.append("Sustained throughput is <b>%s%% of the burst figure</b> (%s against %s "
                     "TFLOPS BF16), which is the gap between a benchmark number and a production "
                     "one." % (fmt(s0["sustained_tflops"] / burst * 100, 0),
                               fmt(s0["sustained_tflops"]), fmt(burst)))
        pw = s0.get("power") or {}
        p = pw.get("power_mean_w")
        tgp = ref.get("tgp_w")
        capped = pw.get("sw_power_cap_active_samples")
        busy = pw.get("busy_samples")
        if capped and busy and capped >= busy * 0.9:
            f.append("The driver reported its <b>software power cap active in %d of %d samples</b> "
                     "during the sustained run, at %s W mean. Power-bound is the driver's own "
                     "signal here, not an inference from a number near the cap."
                     % (capped, busy, fmt(p, 0)))
        elif p and tgp and p / tgp[0] > 0.92:
            f.append("Under sustained load the board sits at <b>%s W of its %s W nominal limit</b>, "
                     "which is suggestive of a power ceiling but was not confirmed by a throttle "
                     "flag." % (fmt(p, 0), fmt(tgp[0], 0)))
    if not mm:
        f.append("No PyTorch runtime was available on this target, so tensor throughput was not "
                 "measured. Everything reported came from the driver API with nothing installed.")
    return f


# ---------------------------------------------------------------- index

def write_index(entries, path, title="Benchmark reports"):
    """Index of every report produced, newest first."""
    rows = []
    for e in entries:
        badge = ('<span class="pill">%s</span>' % esc(e.get("mode", "")))
        # A card is a link, so alternate formats cannot be nested inside it. They go in a
        # sibling row underneath, which keeps every edition one click away.
        extra = e.get("links") or ""
        rows.append(
            '<div class="cwrap"><a class="card" href="%s"><div class="ct"><h3>%s</h3>%s</div>'
            '<p class="cs">%s</p><div class="cm">%s</div></a>%s</div>'
            % (esc(e["href"]), esc(e["title"]), badge, esc(e.get("summary", "")),
               " &middot; ".join(esc(x) for x in e.get("meta", []) if x),
               ('<div class="formats">Also as: %s</div>' % extra) if extra else ""))
    doc = ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
           '<meta name="viewport" content="width=device-width,initial-scale=1">'
           '<title>%s</title><style>%s%s</style></head><body><main>'
           '<header class="rpt"><p class="kicker">Index</p><h1>%s</h1>'
           '<p class="sub">Every benchmark report generated so far. Reports are only comparable '
           'to each other when their fingerprint and mode match.</p></header>'
           '<div class="cards">%s</div>'
           '<footer>Generated by gpubench.</footer>'
           '</main></body></html>'
           % (esc(title), CSS, INDEX_CSS, esc(title), "".join(rows)))
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)
    return path


INDEX_CSS = """
.cards{display:grid;gap:14px;margin:26px 0}
a.card{display:block;text-decoration:none;background:var(--surface);border:1px solid var(--rule);
border-left:3px solid var(--accent2);border-radius:8px;padding:16px 18px;transition:.12s}
a.card:hover{border-left-color:var(--accent);transform:translateX(2px)}
.ct{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
a.card h3{margin:0;color:var(--ink);font-size:1.05rem}
p.cs{margin:7px 0 0;color:var(--ink2);font-size:.9rem}
.cm{margin-top:9px;color:var(--muted);font-size:.79rem}
.cwrap{position:relative}
.formats{margin:-6px 0 0 18px;padding:6px 0 0;font-size:.79rem;color:var(--muted)}
.formats a{color:var(--accent2);text-decoration:none;margin-right:10px}
.formats a:hover{text-decoration:underline}
"""


def discover(directory):
    """Find every report next to its result file and build index entries, newest first."""
    entries = []
    directory = os.path.abspath(directory)
    root = os.path.dirname(directory)
    for res_dir in (os.path.join(root, "results"), directory):
        if not os.path.isdir(res_dir):
            continue
        for fn in sorted(os.listdir(res_dir)):
            if not fn.endswith(".json"):
                continue
            stem = os.path.splitext(fn)[0]
            html_path = os.path.join(directory, stem + ".html")
            if not os.path.exists(html_path):
                continue
            try:
                with open(os.path.join(res_dir, fn), "r", encoding="utf-8") as f:
                    res = json.load(f)
            except (IOError, ValueError):
                continue
            inv = _probe(res, "inventory")
            gpus = inv.get("gpus", [])
            host = inv.get("host", {})
            model = gpus[0].get("name") if gpus else "Unknown GPU"
            count = len(gpus)
            mm = _best_matmul(res)
            bits = []
            if mm:
                k = max(mm, key=lambda x: mm[x]["tflops_best"])
                bits.append("%s %s peak" % (fmt(mm[k]["tflops_best"], 0),
                                            UNIT.get(k, "TFLOPS")))
            bwv = _bandwidth(res)
            if bwv:
                bits.append("%s GB/s memory" % fmt(max(b[1] for b in bwv), 0))
            sus = _probe(res, "torch_compute").get("sustained", [])
            if sus:
                bits.append("%s TFLOPS sustained"
                            % fmt(max(s["sustained_tflops"] for s in sus), 0))
            entries.append({
                "href": os.path.basename(html_path),
                "title": "%s%s" % (("%d x " % count) if count > 1 else "", model),
                "mode": res.get("mode", ""),
                "summary": ", ".join(bits) if bits
                           else "Inventory and driver-level measurements only",
                "meta": [host.get("os", ""),
                         "%d GPU%s" % (count, "s" if count != 1 else ""),
                         res.get("started_at_utc", "")[:10],
                         "fingerprint %s" % (res.get("fingerprint") or {}).get("hash", "?")],
                "sort": res.get("started_at_utc", ""),
            })
    # Reports produced outside this tool (for example a written analysis of the same machine)
    # can be listed by dropping an extra_reports.json next to the index. Keeps the index a
    # complete picture without hard-coding anything about a particular project.
    extra_path = os.path.join(directory, "extra_reports.json")
    if os.path.exists(extra_path):
        try:
            with open(extra_path, "r", encoding="utf-8") as f:
                for e in json.load(f):
                    e.setdefault("sort", "")
                    entries.append(e)
        except (IOError, ValueError):
            pass
    entries.sort(key=lambda e: e.get("sort", ""), reverse=True)
    return entries
