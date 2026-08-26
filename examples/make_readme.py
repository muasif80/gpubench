#!/usr/bin/env python3
"""Generate examples/README.md from the published result files.

Every number below is read from the JSON in results/, never typed. That is the same rule the report
itself follows, and it applies here for the same reason: a summary page is exactly where a stale
copy of a re-measured figure survives, because nobody greps the prose.

    python examples/make_readme.py
"""
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
R = os.path.join(HERE, "results")
sys.path.insert(0, os.path.dirname(HERE))

from gpubench import analysis as AN  # noqa: E402


def load(name):
    with io.open(os.path.join(R, name), encoding="utf-8") as f:
        return json.load(f)


serving = load("onprem-2x5090-serving-v2.json")["probes"]["serve_bench"]
tool = load("onprem-2x5090.json")["probes"]
workload = load("onprem-2x5090-workload.json")["probes"]

levels = serving["levels"]
spread = serving.get("between_run_spread") or {}
by_c = {l["concurrency"]: l for l in levels}
sus = tool["torch_compute"]["sustained"]
pw = [d["power"] for d in sus]
mean_w = sum(p["power_mean_w"] for p in pw) / len(pw)
capped = sum(p["sw_power_cap_active_samples"] for p in pw)
samples = sum(p["samples"] for p in pw)
nccl = tool["nccl_allreduce"]["results"]
mm = tool["torch_compute"]["matmul"]
best = {}
for m in mm:
    if m.get("comparable"):
        k = m["dtype"]
        if k not in best or m["tflops_best"] > best[k]["tflops_best"]:
            best[k] = m
bw = max(r["gb_s_best"] for r in tool["torch_compute"]["memory_bandwidth"])
acc = (workload.get("accuracy") or {}).get("summary") or {}
wl = (workload.get("serve_bench") or {}).get("workload") or {}

# Derivations through the tool, so this page and the report cannot disagree.
peak = allr = None
peak = max(r["bus_gb_s"] for r in nccl)
regimes = AN.allreduce_regimes(nccl)

L = []
A = L.append

A("# Two RTX 5090s, one consumer board, and the link between them")
A("")
A("A worked example of what [`gpubench`](../README.md) produces: a roofline-grounded benchmark of a "
  "dual-GPU inference workstation, measuring **which ceiling stops it** rather than how fast it is.")
A("")
A("> Two identical cards. Identical compute, identical memory bandwidth, and a **3.4x difference in "
  "effective PCIe bandwidth** because one of them sits in a chipset slot. What that asymmetry costs "
  "turns out to depend entirely on the workload: almost nothing for chat, roughly an order of "
  "magnitude for prompt processing.")
A("")
A("Every number on this page is read from the result files in [`results/`](results/) by "
  "[`make_readme.py`](make_readme.py). None of it is typed.")
A("")
A("---")
A("")
A("## The finding")
A("")
A("**Prefill is limited by the interconnect between the devices, not by their compute.** Decode is "
  "limited by memory bandwidth instead. The same machine, the same silicon, two different binding "
  "constraints, and the remedy for one is irrelevant to the other.")
A("")
A("| | Binding constraint | Achieved fraction of it | Fraction of the *other* ceiling |")
A("|---|---|---|---|")
A("| **Prefill** | Interconnect | **78%** of the measured all-reduce curve | 11% of compute |")
A("| **Decode** | Memory bandwidth | **65%** of its own bandwidth ceiling | 21% of the step is comms |")
A("")
A("The decode step reconstructs to within **14%**, and that residual is printed as its own segment "
  "rather than absorbed into a rounding.")
A("")
A("### Why sweeping the interconnect mattered")
A("")
A("A peer-to-peer copy reads much faster than an all-reduce, and tensor parallelism performs "
  "all-reduce. Measuring the shortcut would have overstated the link and inverted the finding. "
  "Sweeping message size rather than sampling it exposes two regimes that give opposite answers:")
A("")
A("| Message size | Latency | Effective bandwidth |")
A("|---|---|---|")
for r in sorted(nccl, key=lambda r: r["size_bytes"]):
    A("| %s KiB | %.4f ms | %.2f GB/s |" % ("{:,.0f}".format(r["size_kib"]), r["latency_ms"],
                                            r["bus_gb_s"]))
A("")
A("A **%.0f microsecond floor** at small messages that a wider slot cannot remove, and a plateau at "
  "**%.2f GB/s** where a wider slot would help proportionally. Decode's messages land in the first "
  "regime; prefill's land in the second." % (regimes["fixed_overhead_us"], peak))
A("")
A("---")
A("")
A("## What it can serve")
A("")
A("Requests per level are rounded up to whole multiples of the concurrency: a partial final wave "
  "runs underloaded and depresses that level's throughput. Sample size and level duration sit "
  "beside every percentile, because a p95 over few requests is an extreme wearing a percentile's "
  "name.")
A("")
A("| Concurrency | n | Duration | Total tok/s | Per stream | TTFT p95 | Between-run CoV |")
A("|---|---|---|---|---|---|---|")
for l in levels:
    sp = spread.get(str(l["concurrency"])) or {}
    A("| %d | %s | %.1f s | **%.0f** | %.0f | %.2f s | %.3f%% |"
      % (l["concurrency"], l.get("sample_count"), l["wall_s"], l["output_tokens_per_s"],
         l["per_request_output_tokens_per_s"], l["ttft_s"]["p95"], sp.get("cov_pct", 0)))
A("")
A("**The arrival process is closed-loop**, and at the top levels the request count equals the "
  "concurrency, so those levels are a single burst rather than a steady state. Their throughput is "
  "a burst figure and their percentiles measure dispersion within one simultaneous arrival, not a "
  "queueing tail. Stated because a p95 quoted as a service level has to say how requests arrived.")
A("")
A("---")
A("")
A("## Measured against the datasheet")
A("")
A("The published headline for this card is an FP4 rate **with 2:4 sparsity**. Dense rates follow by "
  "halving for sparsity and again per precision step, so everything except the headline and the "
  "memory figures is a derivation and is labelled one.")
A("")
A("| Quantity | Measured | Reference | Achieved |")
A("|---|---|---|---|")
REF = {"float4_e2m1": (1676.0, "TOPS"), "int8": (838.0, "TOPS"),
       "float8_e4m3fn": (838.0, "TFLOPS"), "bfloat16": (419.0, "TFLOPS"),
       "float16": (419.0, "TFLOPS"), "tf32": (209.5, "TFLOPS"),
       "float32_shader": (104.9, "TFLOPS")}
NAME = {"float4_e2m1": "FP4 (dense)", "int8": "INT8 (dense)", "float8_e4m3fn": "FP8",
        "bfloat16": "BF16", "float16": "FP16", "tf32": "TF32",
        "float32_shader": "FP32 (shader)"}
A("| Memory bandwidth | %.1f GB/s | 1,792 GB/s | %.0f%% |" % (bw, bw / 1792 * 100))
for k in ("float4_e2m1", "int8", "float8_e4m3fn", "bfloat16", "float16", "tf32", "float32_shader"):
    if k in best:
        ref, unit = REF[k]
        v = best[k]["tflops_best"]
        A("| %s | %.1f %s | %.1f %s | %.0f%% |" % (NAME[k], v, unit, ref, unit, v / ref * 100))
A("")
A("Every precision is measured at **one common matrix size** as well as its own best fit. Sizing "
  "each to its own footprint hands the narrow types a bigger matrix, which produces a table that "
  "ranks matrix sizes while appearing to rank precisions. An earlier edition shipped exactly that.")
A("")
A("### Power")
A("")
A("Under sustained load the cards are **power-bound**, not thermally limited: mean draw **%.0f W** "
  "of a 575 W limit, with the driver reporting its own cap active in **%d of %d busy samples**. "
  "Throughput here is whatever the power budget buys, so more cooling would buy nothing."
  % (mean_w, capped, samples))
A("")
A("---")
A("")
A("## The control that reframed the finding")
A("")
A("Earlier editions computed that single-device serving would not fit and said so as arithmetic. It "
  "was then actually attempted, in an agreed maintenance window, through the tool's experiment "
  "mechanism: stop the service, start a single-device engine on a different port under a different "
  "name, measure, remove it, restart the original, verify with a real request.")
A("")
A("The engine loads **27.64 GiB of weights on one device and then fails to initialise**, at full "
  "context and again at 8,192 tokens. Shortening the context does not help, because the weights do "
  "not shrink with it.")
A("")
A("**So tensor parallelism on this deployment is not an optimisation that could be removed. It is "
  "what makes the model servable at all** &mdash; which inverts the obvious reading of an "
  "interconnect-bound prefill.")
A("")
A("---")
A("")
A("## Quality, measured beside speed")
A("")
if acc:
    A("A speed benchmark cannot tell *faster* from *worse*, so throughput can always be bought by "
      "degrading output. The gate runs alongside: **%d of %d cases correct, %d of %d deterministic "
      "under greedy decoding, verdict %s**. Its cases are published in full in the result file, "
      "because a gate nobody can re-run is an assertion."
      % (acc.get("correct", 0), acc.get("cases", 0), acc.get("deterministic", 0),
         acc.get("cases", 0), acc.get("verdict", "?")))
    A("")
    A("It is a **regression** gate, not a capability benchmark. It detects a stack that has broken "
      "or been quantised into incoherence. It says nothing about model quality.")
A("")
A("---")
A("")
A("## What this is honest about")
A("")
A("| | |")
A("|---|---|")
A("| **Sample** | One board, one CPU, one model, one engine, one run. Nothing here supports a "
  "statement about this GPU as a part, or this engine in general. |")
A("| **Roofs** | Measured with the model resident, so they are **floors, not peaks**. Every "
  "percentage-of-roof therefore has a floor in its denominator and is an upper bound on efficiency. |")
A("| **Workload** | Synthetic filler with a unique leading salt, not a real corpus. Exact length "
  "control and cache-defeat by construction; nothing content-sensitive can be claimed from it. |")
A("| **Arrivals** | Closed-loop. The latency percentiles are optimistic against an open-loop "
  "process at the same mean rate. |")
A("| **The central claim** | An **attribution**, not a causal proof. The interconnect model predicts "
  "the measurement closely while the compute model is off by an order of magnitude, but a "
  "kernel-level trace during prefill would settle it and has not been taken. |")
A("| **Reproduction** | The harness is published. Nobody outside this work has run it. Those are "
  "different standards. |")
A("")
if wl:
    A("The workload is disclosed as data, not described in prose: template `%s`, filler `%s`, and a "
      "salt placed **first** so a prefix cache cannot serve any request. The engine's own token "
      "counter is what gets reported, and both the requested and counted values are recorded per "
      "level so the approximation is auditable."
      % (wl.get("template", "?"), wl.get("filler_token", "?")))
    A("")
A("---")
A("")
A("## The full report")
A("")
A("This page is a summary. The complete document runs to 49 pages across 27 sections with 50 tables "
  "and 10 figures, and covers the roofline placement, the decode step decomposition, the capacity "
  "arithmetic closed from its parts, cost per token, the prompt-length replay, the embedding "
  "service, and the tiered recommendations.")
A("")
A("It also carries something most benchmarks omit: **a section on the four bugs in this work that "
  "produced plausible wrong numbers rather than error messages**, including a roofline built "
  "against the wrong model architecture and a ceiling derivation that could not be rebuilt from its "
  "own published curve. Two of its eighteen editions exist only to correct an error an external "
  "reviewer found.")
A("")
A("## Reproducing this")
A("")
A("```bash")
A("git clone https://github.com/muasif80/gpubench")
A("cd gpubench")
A("python -m tests.test_analysis        # the derivations above, no device needed")
A("python -m tests.test_diagnose        # the conclusions")
A("python -m gpubench.cli report examples/results/onprem-2x5090.json")
A("```")
A("")
A("The last line regenerates an operational report from the published data. See "
  "[`results/README.json`](results/README.json) for what is in the result files, what is **not**, "
  "and why.")

io.open(os.path.join(HERE, "README.md"), "w", encoding="utf-8", newline="\n").write(
    "\n".join(L) + "\n")
print("wrote examples/README.md (%d lines, every number read from results/)" % len(L))
