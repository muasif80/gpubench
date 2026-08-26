# Two RTX 5090s, one consumer board, and the link between them

A worked example of what [`gpubench`](../README.md) produces: a roofline-grounded benchmark of a dual-GPU inference workstation, measuring **which ceiling stops it** rather than how fast it is.

> Two identical cards. Identical compute, identical memory bandwidth, and a **3.4x difference in effective PCIe bandwidth** because one of them sits in a chipset slot. What that asymmetry costs turns out to depend entirely on the workload: almost nothing for chat, roughly an order of magnitude for prompt processing.

Every number on this page is read from the result files in [`results/`](results/) by [`make_readme.py`](make_readme.py). None of it is typed.

---

## The finding

**Prefill is limited by the interconnect between the devices, not by their compute.** Decode is limited by memory bandwidth instead. The same machine, the same silicon, two different binding constraints, and the remedy for one is irrelevant to the other.

| | Binding constraint | Achieved fraction of it | Fraction of the *other* ceiling |
|---|---|---|---|
| **Prefill** | Interconnect | **78%** of the measured all-reduce curve | 11% of compute |
| **Decode** | Memory bandwidth | **65%** of its own bandwidth ceiling | 21% of the step is comms |

The decode step reconstructs to within **14%**, and that residual is printed as its own segment rather than absorbed into a rounding.

### Why sweeping the interconnect mattered

A peer-to-peer copy reads much faster than an all-reduce, and tensor parallelism performs all-reduce. Measuring the shortcut would have overstated the link and inverted the finding. Sweeping message size rather than sampling it exposes two regimes that give opposite answers:

| Message size | Latency | Effective bandwidth |
|---|---|---|
| 4 KiB | 0.0205 ms | 0.20 GB/s |
| 10 KiB | 0.0242 ms | 0.42 GB/s |
| 20 KiB | 0.0232 ms | 0.88 GB/s |
| 40 KiB | 0.0268 ms | 1.53 GB/s |
| 256 KiB | 0.0917 ms | 2.86 GB/s |
| 1,024 KiB | 0.3065 ms | 3.42 GB/s |
| 4,096 KiB | 1.1708 ms | 3.58 GB/s |
| 16,384 KiB | 4.6168 ms | 3.63 GB/s |
| 65,536 KiB | 18.2274 ms | 3.68 GB/s |

A **21 microsecond floor** at small messages that a wider slot cannot remove, and a plateau at **3.68 GB/s** where a wider slot would help proportionally. Decode's messages land in the first regime; prefill's land in the second.

---

## What it can serve

Requests per level are rounded up to whole multiples of the concurrency: a partial final wave runs underloaded and depresses that level's throughput. Sample size and level duration sit beside every percentile, because a p95 over few requests is an extreme wearing a percentile's name.

| Concurrency | n | Duration | Total tok/s | Per stream | TTFT p95 | Between-run CoV |
|---|---|---|---|---|---|---|
| 1 | 32 | 69.8 s | **59** | 59 | 0.26 s | 0.130% |
| 2 | 32 | 41.4 s | **99** | 49 | 0.52 s | 0.078% |
| 4 | 32 | 25.2 s | **162** | 41 | 1.00 s | 0.023% |
| 8 | 32 | 17.6 s | **232** | 29 | 1.93 s | 0.037% |
| 16 | 32 | 13.4 s | **307** | 19 | 3.79 s | 0.132% |
| 32 | 32 | 11.5 s | **356** | 11 | 7.56 s | 0.009% |
| 64 | 64 | 21.3 s | **384** | 6 | 14.70 s | 0.139% |

**The arrival process is closed-loop**, and at the top levels the request count equals the concurrency, so those levels are a single burst rather than a steady state. Their throughput is a burst figure and their percentiles measure dispersion within one simultaneous arrival, not a queueing tail. Stated because a p95 quoted as a service level has to say how requests arrived.

---

## Measured against the datasheet

The published headline for this card is an FP4 rate **with 2:4 sparsity**. Dense rates follow by halving for sparsity and again per precision step, so everything except the headline and the memory figures is a derivation and is labelled one.

| Quantity | Measured | Reference | Achieved |
|---|---|---|---|
| Memory bandwidth | 1534.7 GB/s | 1,792 GB/s | 86% |
| FP4 (dense) | 1374.6 TOPS | 1676.0 TOPS | 82% |
| INT8 (dense) | 731.0 TOPS | 838.0 TOPS | 87% |
| FP8 | 684.5 TFLOPS | 838.0 TFLOPS | 82% |
| BF16 | 233.9 TFLOPS | 419.0 TFLOPS | 56% |
| FP16 | 229.4 TFLOPS | 419.0 TFLOPS | 55% |
| TF32 | 114.3 TFLOPS | 209.5 TFLOPS | 55% |
| FP32 (shader) | 71.3 TFLOPS | 104.9 TFLOPS | 68% |

Every precision is measured at **one common matrix size** as well as its own best fit. Sizing each to its own footprint hands the narrow types a bigger matrix, which produces a table that ranks matrix sizes while appearing to rank precisions. An earlier edition shipped exactly that.

### Power

Under sustained load the cards are **power-bound**, not thermally limited: mean draw **566 W** of a 575 W limit, with the driver reporting its own cap active in **399 of 401 busy samples**. Throughput here is whatever the power budget buys, so more cooling would buy nothing.

---

## The control that reframed the finding

Earlier editions computed that single-device serving would not fit and said so as arithmetic. It was then actually attempted, in an agreed maintenance window, through the tool's experiment mechanism: stop the service, start a single-device engine on a different port under a different name, measure, remove it, restart the original, verify with a real request.

The engine loads **27.64 GiB of weights on one device and then fails to initialise**, at full context and again at 8,192 tokens. Shortening the context does not help, because the weights do not shrink with it.

**So tensor parallelism on this deployment is not an optimisation that could be removed. It is what makes the model servable at all** &mdash; which inverts the obvious reading of an interconnect-bound prefill.

---

## Quality, measured beside speed

A speed benchmark cannot tell *faster* from *worse*, so throughput can always be bought by degrading output. The gate runs alongside: **10 of 10 cases correct, 10 of 10 deterministic under greedy decoding, verdict PASS**. Its cases are published in full in the result file, because a gate nobody can re-run is an assertion.

It is a **regression** gate, not a capability benchmark. It detects a stack that has broken or been quantised into incoherence. It says nothing about model quality.

---

## What this is honest about

| | |
|---|---|
| **Sample** | One board, one CPU, one model, one engine, one run. Nothing here supports a statement about this GPU as a part, or this engine in general. |
| **Roofs** | Measured with the model resident, so they are **floors, not peaks**. Every percentage-of-roof therefore has a floor in its denominator and is an upper bound on efficiency. |
| **Workload** | Synthetic filler with a unique leading salt, not a real corpus. Exact length control and cache-defeat by construction; nothing content-sensitive can be claimed from it. |
| **Arrivals** | Closed-loop. The latency percentiles are optimistic against an open-loop process at the same mean rate. |
| **The central claim** | An **attribution**, not a causal proof. The interconnect model predicts the measurement closely while the compute model is off by an order of magnitude, but a kernel-level trace during prefill would settle it and has not been taken. |
| **Reproduction** | The harness is published. Nobody outside this work has run it. Those are different standards. |

The workload is disclosed as data, not described in prose: template `Request <salt>. <filler> \n Summarize:`, filler `lorem`, and a salt placed **first** so a prefix cache cannot serve any request. The engine's own token counter is what gets reported, and both the requested and counted values are recorded per level so the approximation is auditable.

---

## The full report

This page is a summary. The complete document runs to 49 pages across 27 sections with 50 tables and 10 figures, and covers the roofline placement, the decode step decomposition, the capacity arithmetic closed from its parts, cost per token, the prompt-length replay, the embedding service, and the tiered recommendations.

It also carries something most benchmarks omit: **a section on the four bugs in this work that produced plausible wrong numbers rather than error messages**, including a roofline built against the wrong model architecture and a ceiling derivation that could not be rebuilt from its own published curve. Two of its eighteen editions exist only to correct an error an external reviewer found.

## Reproducing this

```bash
git clone https://github.com/muasif80/gpubench
cd gpubench
python -m tests.test_analysis        # the derivations above, no device needed
python -m tests.test_diagnose        # the conclusions
python -m gpubench.cli report examples/results/onprem-2x5090.json
```

The last line regenerates an operational report from the published data. See [`results/README.json`](results/README.json) for what is in the result files, what is **not**, and why.
