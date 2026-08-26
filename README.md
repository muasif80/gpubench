# gpubench

A benchmark that tells you **which ceiling you are hitting**, not just how fast the card is.

Most GPU benchmarks give you a number. This one measures the machine's roofs (compute, memory
bandwidth, interconnect), measures the workload against them, reports the gap and what is eating
it, explains the readings it can explain, says plainly when it cannot, and renders the whole thing
as a publishable report. It is built to run safely on a production server you are not allowed to
install anything on.

```
$ gpubench run --target ssh://user@host --out result.json
$ gpubench report result.json
wrote result.html
```

---

## Contents

- [Why another one](#why-another-one)
- [Install](#install)
- [The six commands](#the-six-commands)
- [What it measures](#what-it-measures)
- [What it derives](#what-it-derives)
- [What it concludes](#what-it-concludes)
- [Experiments: measurements that change the machine](#experiments-measurements-that-change-the-machine)
- [Reports](#reports)
- [The report template and linter](#the-report-template-and-linter)
- [Safety](#safety)
- [Privacy](#privacy)
- [Comparing results](#comparing-results)
- [Testing](#testing)
- [What it does not do](#what-it-does-not-do)

---

## What it produced: a worked example

The tool was written to answer one question about one machine, and the report it produced is the
best documentation of what it does.

> ### Two RTX 5090s, one consumer board, and the link between them
>
> **[Read it &rarr;](examples/README.md)**
>
> Two identical cards. Identical compute, identical memory bandwidth, and a **3.4x difference in
> effective PCIe bandwidth** because one of them sits in a chipset slot. What that asymmetry costs
> turns out to depend entirely on the workload: almost nothing for chat, roughly an order of
> magnitude for prompt processing.

**The finding.** Prefill is limited by the **interconnect** between the devices; decode is limited by
**memory bandwidth**. Same machine, same silicon, two different binding constraints, and the remedy
for one is irrelevant to the other. Every figure behind that is on the example page, generated from
the published result files rather than typed &mdash; which is the discipline this tool exists to
enforce, so its own README had better follow it.

**The control that reframed it.** Single-device serving was arithmetic in earlier editions. It was
then attempted for real, in a maintenance window, through the `experiment` mechanism: the engine
fails to initialise on one device at any context length. So tensor parallelism on that deployment is
not an optimisation that could be removed &mdash; it is what makes the model servable at all.

**What it is honest about.** One machine, one engine, one model, one sample. Roofs measured in
shared mode are floors, so every percentage-of-roof is an upper bound. The load generator is
closed-loop, so the latency percentiles are optimistic. The central claim is an attribution, not a
causal proof, and the measurement that would settle it is named and has not been taken.

### The example data

`examples/results/` holds the tool's own result files from that work, including the corrected
serving sweep the report's capacity tables are built from, plus a run from a very different machine
for contrast. Every file passes `python -m gpubench.longform.redact examples/`, and
`examples/results/README.json` records what is there, what is not, and why.

---

## Why another one

| Existing tool | What it leaves out |
|---|---|
| `vllm bench serve`, genai-perf, llmperf | Serving numbers with no hardware attribution |
| nvbandwidth, gpu-burn, BabelStream | One microbenchmark each, no synthesis |
| MLPerf Inference | Rigorous, but a submission process, not a Tuesday-afternoon answer |

What this adds:

- **Attribution.** "You are at 65% of your memory-bandwidth ceiling, 21% of each step is
  interconnect, and 14% is unexplained" is a different kind of answer from "238 TFLOPS".
- **Interconnect awareness.** Multi-device systems are frequently limited by the link between the
  cards rather than the cards. This measures that directly, swept across message size, because the
  link has two regimes that give opposite answers.
- **Explanation, not just readings.** When a counter reads zero the tool says *why*, or says it
  cannot tell and names the check that would settle it.
- **Safe on production.** Shared mode by default. Anything that changes the machine is an
  *experiment*, needs two independent confirmations, and restores what it touched.
- **Honest degradation.** A metric the machine cannot supply is reported as not measured, with the
  reason. It is never rendered as a zero.

---

## Install

Nothing to install. Pure standard-library Python 3.8+, running from an extracted archive:

```bash
tar xzf gpubench-<version>.tar.gz        # or unzip gpubench-<version>.zip
cd gpubench-<version>
python -m gpubench.cli inspect
```

Optionally `pip install .` for a `gpubench` command on your PATH. Everything below works either
way; `gpubench X` and `python -m gpubench.cli X` are interchangeable.

**Optional extras**, needed only for PDF and DOCX export:

```bash
pip install ".[pdf]"        # playwright + pymupdf
playwright install chromium
```

Every archive ships with a `.sha256` beside it. Verify before use.

---

## The six commands

```
gpubench inspect      read-only machine state; changes nothing
gpubench run          the benchmark suite
gpubench report       render an operational report from a result file
gpubench article      render a long-form report from a content module
gpubench experiment   a measurement that CHANGES the machine, then restores it
gpubench index        build an index page over a directory of reports
```

### `inspect` — look without touching

```bash
gpubench inspect
gpubench inspect --target ssh://user@host
```

Allocates nothing, starts nothing, writes nothing. Use it first, always: most surprising benchmark
results are explained by clocks, thermals, link width or what else is resident, and this costs
nothing to check.

### `run` — the benchmark

```bash
# Show exactly what would run, and run nothing.
gpubench run --explain

# Local.
gpubench run --out result.json

# Remote, installing nothing on the target.
gpubench run --target ssh://user@host --out result.json

# Where the GPU runtime lives in a container rather than on the host.
gpubench run --target ssh://user@host --container my-serving-container --out result.json

# Skip probes you do not want.
gpubench run --skip torch,nccl,serving --out result.json
```

| Flag | What it does | Risk |
|---|---|---|
| `--target` | `local`, or `ssh://user@host` | none |
| `--password`, `--hostkey` | Use PuTTY plink instead of OpenSSH keys | credentials on the command line; prefer keys |
| `--container` | Run the runtime-dependent tier inside this container | none; it only executes there |
| `--vram-mb` | Scratch VRAM budget | larger values contend harder with co-resident work |
| `--sustain-s` | Seconds of sustained load | **drives the device flat out**; raises latency for anything co-resident and pushes power to the cap |
| `--profile` | `base` (comparable) or `peak` (tuned, disclosed) | none |
| `--mode` | `shared` (default) or `exclusive` | `exclusive` assumes the device is yours; roofs become peaks instead of floors |
| `--skip` | Comma list of probes to omit | a skipped probe becomes a stated gap, not a silent one |
| `--explain` | Print the plan and exit | none |
| `--keep-identifying` | Keep host, container and device identifiers | **the result is then not safe to share** |

### `report` — the operational report

```bash
gpubench report result.json
gpubench report result.json --html out.html --no-open
```

Self-contained HTML: inline SVG, no external assets, a table beside every figure. Leads with what
the run could and could not establish, before any number.

### `article` — the long-form report

```bash
gpubench article <content-module.py> <run-dir> --pdf --docx --check
```

See [Reports](#reports).

### `experiment` — changes the machine

```bash
gpubench experiment --list
gpubench experiment --write-config gpubench.json
gpubench experiment tp-scaling --confirm-disruptive --dry-run
```

See [Experiments](#experiments-measurements-that-change-the-machine).

### `index` — an index over many reports

```bash
gpubench index reports/
```

Reads an optional `extra_reports.json` sidecar so hand-written documents can be listed alongside
generated ones.

---

## What it measures

Probes are attempted in order and degrade rather than fail.

| Tier | Needs | Probe | Measures |
|---|---|---|---|
| **0** | Driver only | `inventory` | Devices, clocks, thermals, power caps, **throttle reasons**, PCIe link state and the bridge each card sits behind, resident processes, containers |
| **0** | HTTP only | `engine_config` | The serving engine's **resolved** configuration, from its own metrics and model endpoints |
| **0** | HTTP only | `serving` | TTFT, inter-token latency, throughput, prefill and decode isolated, a realistic prompt-length mixture, prefix-cache counters |
| **0** | HTTP only | `embedding` | Embedding throughput and latency by batch and client concurrency |
| **0** | HTTP only | `accuracy` | Determinism and exact match, with the full case list published in the result |
| **2** | Driver library | `cuda_driver` | Device attributes, copy bandwidth, host transfer, a working-set sweep across the cache boundary |
| **1** | An existing runtime | `torch_compute` | Compute per precision (FP4, FP8, INT8, BF16, FP16, TF32, FP32 shader), sustained throughput with power sampling, peer-to-peer bandwidth |
| **1** | An existing runtime | `capabilities` | Weight-only INT4 (W4A16), video encode/decode, and what is enumerated rather than measured, with the reason |
| **1** | An existing runtime | `nccl` | All-reduce **swept** from 4 KiB to 64 MiB |

Two design points worth knowing:

**Tier 2 loads the driver API through `ctypes`**, so it needs no toolkit and no framework. On a bare
machine with only a driver you still get real bandwidth and PCIe numbers.

**Tier 1 is never installed.** It is piped into a runtime that already exists, usually the serving
container, so you measure the exact library versions the workload actually runs on.

### The workload is part of the result

Every probe that generates input records what it sent: template, filler, salt policy, how size is
controlled, why synthetic was chosen and what that costs. Both the requested size and the engine's
own token count are recorded per level, so the approximation is auditable rather than assumed.

Request counts are **rounded up to whole multiples of the concurrency**. A partial final wave runs
underloaded and depresses that level's throughput by an amount depending on how badly the count
divides, so it appears at some levels and not others and reads as scatter. It once put 233 tok/s in
one table and 204.5 in another for the same nominal level.

---

## What it derives

`gpubench/analysis.py` is the single place any ceiling, ridge point or attribution is computed.
Pure, standard-library, unit-tested without a device.

| Derivation | What it answers |
|---|---|
| `ridge_point` | Where compute and bandwidth roofs cross, in FLOPs/byte. A property of the machine alone |
| `decode_floor` | The bandwidth floor on one decode step, and the ceiling it implies at each batch size |
| `decode_attribution` | Splits a measured step into bandwidth, communication, and **a residual reported rather than hidden** |
| `prefill_compute_ceiling` | What prefill could reach if only compute bound it |
| `prefill_comms_ceiling` | What the **interconnect alone** permits, per prompt length |
| `concurrency_ceiling` | How many sequences fit, including any **fixed per-sequence state**, with the full parts breakdown |
| `allreduce_regimes` | Where the link is latency-bound and where it is bandwidth-bound |

Three rules hold throughout:

1. **No hardware or model constants.** Every fact arrives as an argument. A function that cannot be
   given a fact returns `None` rather than assuming one. An earlier version carried one machine's
   weight size and layer count; elsewhere that produced confident, plausible, wrong answers.
2. **Every derived value reports its own inputs and formula**, so a report can print the arithmetic
   and a reader can reject it.
3. **Pure and stdlib-only**, so every derivation is testable without a GPU.

> **The cautionary tale, kept in the source.** An earlier version interpolated all-reduce *latency*
> log-linearly between measured points. In the bandwidth-bound regime latency is linear in message
> size, so that overstated it by up to 17%, understated every derived ceiling, and made a published
> column impossible to rebuild from the sweep printed three pages earlier. It now interpolates
> *bandwidth*. The load-bearing test asserts that a derived value at a **measured** size reproduces
> the measurement exactly.

---

## What it concludes

`gpubench/diagnose.py` turns readings into stated conclusions with their evidence attached, and
runs on every result.

| Rule | Catches |
|---|---|
| `prefix-cache` | Distinguishes cache-consulted-and-missed from cache-never-ran from counter-broken |
| `roof-mode` | A shared-mode roof is a **floor**, so every percentage from it is an upper bound |
| `attribution` | A negative residual means an input is wrong, not that the machine is fast |
| `power` | Power-bound versus thermally throttled, from the driver's own flag |
| `device-parity` | Identical devices on non-identical links |
| `workload` | An undisclosed corpus, or size control drifting |
| `sampling` | **Partial waves**, missing sample sizes, percentiles resting on too few requests |
| `reproducibility` | A best-of-N with no spread is a measurement of luck |
| `quality-gate` | A gate whose cases are unpublished is an assertion |
| `provenance` | A result with no comparability fingerprint |

Severity says what to **do**: `blocking` (a number is unsupportable), `warning` (right but easy to
misread), `info`, `unknown` (a check could not run, and what to supply).

Three rules the module holds itself to:

1. **A finding names its evidence.** A diagnosis without it is an opinion in a lab coat.
2. **"I cannot tell" is loud and first-class.** A diagnostic that silently passes when it could not
   look reads as an all-clear and is worse than none.
3. **Never upgrade "did not" into "cannot".** The investigation this encodes made exactly that error
   once and inverted a recommendation. Rules that can only establish the weaker claim say the weaker
   claim and name the check that would settle the stronger one.

---

## Experiments: measurements that change the machine

Everything else observes. An experiment **intervenes**, so it has its own rules.

```bash
# What exists, and what each one costs. Changes nothing.
gpubench experiment --list

# A starter config with everything DISABLED and every risk documented inline.
gpubench experiment --write-config gpubench.json

# Both gates passed, but still runs nothing.
gpubench experiment tp-scaling --confirm-disruptive --dry-run

# For real.
gpubench experiment tp-scaling --confirm-disruptive \
    --target ssh://user@host --out results/tp.json
```

| Flag | What it does | Risk |
|---|---|---|
| `--list` | Print every experiment with what it does, what it changes, its RISK, expected downtime and how it restores | none |
| `--write-config PATH` | Write a starter config, all disabled | none |
| `--config PATH` | Use this config (default `./gpubench.json`) | none |
| `--confirm-disruptive` | **Required** for a disruptive experiment | the service is **unavailable** while it runs; restoration is guaranteed and verified, but the outage is real |
| `--set KEY=VALUE` | Override one setting, repeatable | changes what is measured and can **extend downtime** |
| `--dry-run` | Print what would run, including every gate checked | none |
| `--out PATH` | Write the full result including baseline and restore verification | none |

### The four rules

1. **Declare the blast radius before it runs.** `--list` prints it. Nobody should read source to
   learn whether a command takes production down.
2. **Two independent gestures.** `enabled: true` in the config **and** `--confirm-disruptive` on the
   command line. A config file gets copied between machines; a command line gets recalled from
   history. Neither alone is evidence of intent.
3. **Restore is a guarantee, not a step.** It runs on success, on failure and on interrupt, then is
   **verified** against a baseline captured beforehand — with a real request, not a health endpoint.
4. **Stop, never remove.** Restoration is then a resume of the real thing rather than a rebuild from
   a captured spec. A spec is a *model* of the thing.

Site values — container names, model, cache paths — come from the config file, never from tool
defaults. A general-purpose tool that ships one estate's names leaks them to everyone who downloads
it, and the packaging gate enforces that.

### A worked config

```json
{
  "experiments": {
    "tp-scaling": {
      "enabled": true,
      "service_container": "my-inference-container",
      "image": "vllm/vllm-openai:v0.23.0",
      "model": "org/model-name",
      "hf_cache": "<the model cache path on your target>",
      "contexts_to_try": [131072, 8192],
      "prefill_lengths": [128, 512, 2048]
    }
  }
}
```

A broken harness is **fatal, never a finding**: if the probe never executed, the tool raises rather
than reporting "the engine refused". That distinction cost a maintenance window to learn.

---

## Reports

Two kinds.

**`report`** renders one result file into a self-contained operational HTML page.

**`article`** renders a *long-form* report. The narrative lives in a **content module** you supply;
everything that decides what a reader sees — ordering, numbering, cross-references, contents, the
stylesheet, PDF/DOCX export, the redaction gate — is the tool's.

```bash
gpubench article article/build.py results/<run-id> --pdf --docx --check
```

| Flag | What it does | Risk |
|---|---|---|
| `--out-dir` | Where to write (default: beside the content module) | none |
| `--basename` | Output filename stem | none |
| `--pdf` | Render PDF, resolve contents page numbers from the rendered document, attach an outline | needs the `pdf` extra |
| `--docx` | Render DOCX | needs the `pdf` extra |
| `--check` | Run the redaction gate over the built artifacts and **fail** if anything identifying is found | none; it only refuses |

### The content-module contract

```python
TITLE         = "..."          # document title
BASENAME      = "..."          # output filename stem
VERSION       = "8.6"          # appended to the stem
SECTION_ORDER = [...]          # title fragments, in reading order

def build(run_dir, out_dir):  -> (figures, data)
def render(figures, data):    -> section HTML, in AUTHORING order
```

Four mechanisms in the engine exist because their absence shipped a defect:

- **Reading order is declared separately from authoring order**, so a section can be written
  anywhere without disturbing the flow.
- **Section numbers are assigned in final document order**, never taken from what an author typed.
- **In-text references name a section by a fragment of its title** and resolve after renumbering. An
  unresolvable or ambiguous reference **aborts the build** rather than shipping a wrong pointer.
- **The contents is built from the sections actually present.**

### Regression diff

```bash
python -m gpubench.longform.regression OLD.html NEW.html
```

Extracts every number from two builds and diffs them, so a release claiming "no measured value
moved" can prove it. Prose is aligned by masked context; table cells are compared as a multiset,
because inside a table the neighbours are other numbers and positional alignment produces
spectacular false positives.

---

## The report template and linter

`gpubench/template/` is a hardware-agnostic contract for benchmark reports.

| File | What it is |
|---|---|
| `run-schema.json` | The data contract. **Every number is a value envelope, never a bare scalar** |
| `report-outline.yaml` | The section manifest, with per-section invariants and anti-patterns |
| `lint-rules.md` | Eleven rules, each citing the real defect it catches |
| `lint.py` | The executable linter |

```bash
python -m gpubench.template.lint <run-dir> <built-report>
python -m gpubench.template.lint <run-dir> <built-report> --rules L1,L3 --explain
```

Exits non-zero on any violation.

A value envelope:

```json
{
  "id": "sustained_power_mean",
  "value": 566.0, "unit": "W", "kind": "measured",
  "source": "device telemetry, 10 Hz over the sustained run",
  "run_id": "2026-08-25T11:01:57Z-primary",
  "n": 401, "spread": {"type": "cov", "value": 0.004}, "precision": 0
}
```

`kind` is a closed enum: `measured | derived | assumption | projection | supplied | published |
enumerated | fixed-test-set`. A `derived` value additionally **requires** `inputs` and `formula` —
one that cannot name its inputs is invalid. `assumption` and `projection` require `rationale`;
`published` requires `provenance`; `fixed-test-set` requires its `cases` in full.

The rules, and what each caught:

| Rule | Catches |
|---|---|
| L1 no orphan literals | The same quantity printed as three different values |
| L2 every value has a kind | An unsourced traffic mixture presented as measured |
| L3 derived is rebuildable | A derivation that does not reproduce from its own inputs |
| L4 assumptions stay labelled | An assumption labelled on first appearance and not after |
| L5 cross-references resolve | Stale hard-coded section numbers |
| L6 figures carry tables | The reproducibility contract |
| L7 run provenance | A value whose run is not declared |
| L8 comparison hygiene | Two values compared across different conditions |
| L9 claims need evidence | "Published" where nothing says where |
| L10 version history complete | A changelog missing its own versions |
| L11 gates measured, not argued | A quality gate with unpublished cases |

---

## Safety

Designed to be pointed at machines that matter.

- **Shared mode is the default.** It allocates a bounded VRAM scratch budget (never more than a
  third of free VRAM) and runs compute and copy kernels. It starts nothing, stops nothing, writes no
  files on the target and changes no configuration.
- It does **contend** for the device while it runs. The sustained section drives it flat out for
  `--sustain-s` seconds, raising latency for co-resident work and pushing power toward the cap.
  Transient, but not nothing: schedule accordingly on a busy or power-constrained host.
- Results measured with other work resident are labelled **floors, not peaks**, everywhere they
  appear, and the diagnostics say so.
- `--explain` and `--dry-run` print exactly what will execute before anything does.
- Anything that changes the machine is an **experiment**, requires two independent confirmations,
  and restores what it touched.

---

## Privacy

Results are meant to be shared, so a result records the **machine** and not the **deployment**.
Removed automatically: the target host (dropped, not hashed), container names, images and port
bindings, process names and PIDs, device UUIDs and board serials.

Kept, because they are the point of a hardware report: device model, driver version, board, CPU,
clocks, PCIe topology and every measurement.

`--keep-identifying` keeps them when the result stays internal. A `sanitised` block records exactly
what was removed, and **accumulates across passes** — an earlier version rewrote a shorter manifest
on a second pass and silently dropped the record of what the first had removed.

### The redaction gate

```bash
python -m gpubench.longform.redact <directory-of-built-artifacts>
```

Scans **built** artifacts, not sources: a clean generator can still render a hostname that arrived
in a result file. It reads site-specific terms from `GPUBENCH_DENY_LITERALS` or a `denylist.txt`
**beside the artifacts** — deliberately not from inside the tool, because a general-purpose tool
shipping one organisation's wordlist leaks exactly the names the list exists to hide.

A **declared** checksum is allowed through: publishing the digest of your own measurement code is a
transparency measure, and a gate that blocks it prevents the right thing from happening. The
exception is narrow — the digest must sit beside vocabulary declaring it as one. An undeclared hex
or base64 run is still caught.

> **On hashing the hostname:** an earlier version stored an unsalted truncated SHA-256 of the target
> and described it as non-reversible. It was not. `user@host` carries perhaps 20–40 bits of real
> entropy, and an adversarial review recovered the original from a 24-candidate dictionary
> instantly; being unsalted it was also a stable join key across published results. There is no safe
> way to keep a useful identifier, so the identifier is now simply dropped.

---

## Comparing results

Every result carries a **fingerprint** over the parameters that must match for two results to be
comparable: schema version, profile, mode, device count, device model and driver version. Comparing
a shared-mode run against an exclusive-mode one, or two different device models, produces a chart
that looks authoritative and means nothing. Check the fingerprint first.

Every result also carries an **artifact** block: tool name, version, and a SHA-256 over every Python
file in the package. That ties a number to the exact code that produced it. It is a historical fact
and is never updated — changing it to match a later release would break the tie, which is the only
reason to record it. Verify a *download* against the archive's own `.sha256`; verify a *result's
provenance* against the artifact block.

---

## Testing

```bash
python -m tests.test_analysis                  # derivations
python -m tests.test_diagnose                  # conclusions
python -m gpubench.template.tests.test_lint    # the report linter
```

All three run without a device, a network or a third-party package, from a clean extraction of the
release archive. Tests ship with the tool on purpose: a measurement tool whose tests are withheld is
asking to be trusted rather than verified.

---

## What it does not do

Stated plainly, because a benchmark that hides its gaps is worse than one that admits them.

- **NVIDIA only** at present. The probe layer sits behind an interface so other vendors are
  additive, but they are not implemented.
- **The quality gate is a regression gate, not a capability benchmark.** It detects a stack that has
  broken or been quantised into incoherence. It says nothing about model quality, and no claim about
  quality should rest on it.
- **Dense INT4 tensor rate is not measured.** Weight-only INT4 (W4A16) is, and the two differ by
  about an order of magnitude. They must not be compared against each other.
- **Ray-tracing throughput is enumerated, not measured.** RT cores are reachable only through a
  ray-tracing API, so no compute benchmark can measure them. The core count is reported.
- **One machine is one sample.** Repeats measure this machine's variance, not variance across parts.
  Nothing here supports a statement about a device model in general.
- **It cannot tell you what a different engine would do.** The serving stack is part of the result,
  so numbers are not portable across engine versions, quantizations or batching policies.

---

## Author

Muhammad Asif.

## Licence

Apache-2.0. See `LICENSE` and `NOTICE`.

The measurement code is open on purpose: a benchmark nobody can inspect is a benchmark nobody
should believe.
