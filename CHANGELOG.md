# Changelog

## 1.3.0

Checks that read a declaration were replaced by checks that read the artifact. Every item below
came from the same discovery: something reported success without having looked.

- **The redaction gate refuses instead of passing when it cannot check.** Handed a file rather than
  a directory it walked nothing and printed "PASS: 0 files scanned", which reads exactly like a
  clean result. Given artifacts copied away from their denylist it ran the structural patterns and
  skipped the half that catches a NAME, and passed an edition carrying an organisation name in
  plain text. Both now exit non-zero and say which check did not run.
- **`tests/test_redact_control.py`**, the proof that gate can fail: one planted instance of every
  class it screens for, each required back by name, plus two negative controls so it is not merely
  an unconditional failure. A gate that has only ever returned PASS is indistinguishable from a
  gate that cannot fail.
- **Header rows repeat across page breaks in exported .docx.** The first fix assigned
  `row.repeat_as_header_row = True`; python-docx has no such property, so it silently created a
  Python attribute, emitted nothing, and changed zero of 64 tables while the build log said
  success. Now written as `w:tblHeader`, and **`tests/test_docx_header_repeat.py` unzips the
  produced package and counts the element** rather than asserting the function was called, which
  is the only form of the test that would have caught the original bug.
- **Serving levels report p99, `max`, `n` and `finest_resolvable_pct`.** A saturation run came back
  with a clean monotonic p50 and p95 and no p99 at all, which is the percentile a capacity decision
  turns on. Summaries are kept rather than raw samples, so a missing percentile cannot be recovered
  after the fact and the level has to be re-run. `finest_resolvable_pct` states what a given sample
  count can actually support: 20 requests cannot resolve a p99, and printing one anyway is the
  maximum wearing a percentile's name.
- **Verdict declarations (`A12`)**: a verdict cell that disagrees with its declaration blocks the
  render, so a conclusion cannot drift from the evidence it was drawn from.
- **A raw-artefact claims verifier** (`tools/verify_claims.py`), which rebuilds derived claims from
  the result files instead of from the manifest's own values, and whose `--selftest` plants a value
  no artefact contains and requires UNGROUNDED back. An internally consistent manifest hides
  exactly this.
- **A reproduction record** (`tools/reproduce.py`) with a drift check proven in CI by mutating a
  driver version and requiring a non-zero exit.
- **Open-loop repeat spread is keyed by arrival rate, not concurrency.** Open-loop levels all carry
  concurrency 1, so keying by it collapsed every rate into one bucket and compared unrelated levels.
- **The claims verifier no longer reports a false UNGROUNDED across a unit change.** Its unit table
  knew `s to ms` and not `ns to ms`, so a kernel-trace figure printed in milliseconds could not be
  matched to the artefact holding it in nanoseconds, and two real claims were reported as
  unsupported. ns and us conversions added for the time units, and `tests/test_verify_claims.py`
  now covers the grounder, including the negative controls: a scale search that will multiply by
  anything until something matches grounds every claim and verifies nothing.
- **`svg.table(tid=...)`** puts an id on a rendered `<table>`, which is what lets F4 check its cells
  against the page. Without it, a declared table was reported as "could not be found in the
  rendered document", so the declaration existed and the check silently had nothing to read.
- **`esc_attr()` for attribute values.** `esc()` escapes `&`, `<` and `>`, which is correct for text
  and wrong inside quotes, and it was being used for `aria-label` and now for ids. Found by a test
  written for the new table id, not by review.
- **`verdicts_removed` and `tables_removed` are changelog waiver fields.** A10 waives a removal when
  a changelog row names the id, and verdicts had nowhere honest to be named: retiring one meant
  listing it under `claims_removed`, filing a change to the document's conclusions as a change to
  its numbers.
- **`document_title` moved to `gpubench/longform/doctitle.py`**, which imports nothing but `re` and
  `html.unescape`. Its test asserted in its own docstring that the rule was "testable without
  python-docx installed" and it was not: the import chain pulled in python-docx at module scope, so
  on a clean runner the whole class raised `ModuleNotFoundError` before reaching an assertion. The
  fix was to make the sentence true rather than to skip the test. `docx_export` re-exports the name.
- CI runs eight suites, one job per suite, still with no `pip install` anywhere: the absence of an
  install step is itself the check that the tool needs nothing installed.

## 1.2.0

A report whose numbers disagree with each other can no longer be built.

- **New `gpubench verify`**: a deterministic pre-render gate over a claims manifest. It checks that
  one quantity has one value, that every derived figure recomputes from its declared inputs and
  formula, that ratios name their denominator basis, that request counts are whole multiples of
  their concurrency, that percentiles resolve to a rank worth reporting, that an arrival process is
  declared, that a fraction-of-roof measured in shared mode carries its caveat where the reader
  meets it, that every figure has a table view, and that a quality gate publishes its cases.
  Exit 1 on any error, so the render does not happen: a report that fails verification should not
  exist as a file, because a file is the thing that gets sent to people.
- `gpubench verify --demo` runs it against a fixture carrying defects taken from real editions of a
  real report, including a headline number that disagreed with itself by 12%, a sentence asserting
  the opposite of its own table, and a sweep level whose request count was not a whole multiple of
  its concurrency.
- **The rule the gate is built around**: never edit a measured value to make a check pass. The
  permitted responses are fix the generator, fix the prose, re-measure, or declare the exception in
  the manifest. A verifier with edit access converges on a report that agrees with itself perfectly
  and has drifted away from what the machine did, which is worse than an inconsistent one because
  the inconsistency was the only visible symptom.
- README rewritten end to end: all six commands with their flags and the risk each carries, the
  probe/derivation/diagnostic tables, the experiment gates, the content-module contract, and an
  honest "what it does not do". The previous one claimed no accuracy validation, no variance
  measurement and no INT4 or video measurement, all of which had become false.

## 1.1.0

An independent review of a report built with this tool found a 12% contradiction on its primary
figure of merit. The cause was in here, and this release fixes it and the class it belongs to.

- **Request counts are rounded UP to whole waves.** A level whose request count is not a multiple of
  its concurrency spends its final wave underloaded, so it reports a throughput somewhere between
  its nominal concurrency and the size of its tail. The error is silent, largest at middle
  concurrencies, and vanishes wherever the count happens to divide -- so it appears at one level and
  reads as scatter rather than as a fault. It put 233 tok/s in one table and 204.5 in another for
  the same nominal level. Levels now record `waves`, `whole_waves`, `sample_count` and `duration_s`.
- **New diagnostic rule `sampling`**, which fails the run when any level ran a partial wave, and
  warns when percentiles are reported without a sample size or rest on fewer than 20 requests. Six
  tests, including one asserting the rule survives an incomplete level rather than raising -- which
  it did on its own first test run.
- **`concurrency_ceiling` returns its full parts breakdown**: fixed per-sequence state, KV per
  sequence, the total, the measured pool percentage and the residual, all on a stated per-device
  basis. The review found this was the one derivation in a published report a reader could not
  close, because the parts were quoted in mixed units and the basis was never stated.
- **Results carry an `artifact` block**: tool name, version, and a SHA-256 over every Python file in
  the installed package. A report can describe a harness in complete detail and still leave a reader
  unable to obtain it; a version and a checksum turn "the code is available" from a promise into a
  fact.
- **The redaction gate no longer blocks a DECLARED checksum.** Publishing the digest of your own
  measurement code is a transparency measure, and a gate that stops it is preventing the right thing
  from happening. The exception is narrow: the digest must sit next to vocabulary declaring it as
  one, and the proximity test strips markup first so it works inside an Office package, where the
  whole body is a single line. An undeclared hex or base64 run is still caught.

## 1.0.1

Tables in a rendered report WRAP instead of scrolling sideways.

The print stylesheet had always wrapped table cells, because paper cannot scroll and a clipped
right-hand edge is silent. The screen stylesheet kept `white-space:nowrap` and let the container
scroll instead, which is the same defect wearing a scrollbar: a reader had to drag every wide table
sideways to find out what a column said, and could not see the whole row at once.

- Screen cells now wrap with `overflow-wrap:break-word` and `word-break:normal`. Not `anywhere`,
  which also shrinks a cell's min-content width and makes the layout algorithm starve narrow
  columns and hyphenate mid-word.
- `.tablewrap`'s `overflow-x` is kept as a safety net for genuinely unbreakable content and
  documented as one. Measured in a real browser across a whole 33-section report: **51 tables, zero
  scrolling, zero page overflow at both 1280px and 900px.**
- A `nw` class is available for short number-and-unit cells that read as two values when split.
- DOCX tables are pinned to the text column and set to autofit so Word wraps within columns rather
  than running past the right margin. One table built by a separate code path had been missed --
  51 of 52 constrained -- and is now included.
- The PDF was already clean and was verified again rather than assumed: no drawn text block crosses
  the right margin on any page.

No measured or derived value changed anywhere; this is presentation only.

## 1.0.0

Self-contained. Everything needed to measure a machine, work out which ceiling binds it, say what
that means, and publish the result now lives in one place.

- **`gpubench/experiments.py`** and the `experiment` subcommand: measurements that CHANGE the system
  under test and put it back. Every experiment declares its blast radius (`--list` prints what it
  does, what it changes, its RISK, the expected downtime, and how it restores), needs BOTH
  `enabled: true` in a config file AND `--confirm-disruptive` on the command line, restores on
  success, failure and interrupt, and VERIFIES the restoration with a real request rather than a
  health endpoint. Where a service must be moved aside it is STOPPED, never removed, so restoration
  resumes the real thing instead of rebuilding from a captured model of it.
- **First experiment, `tp-scaling`**: serves the same model on one device instead of two. On its
  first real run it established that a model whose weights need both devices cannot fall back to
  one -- tensor parallelism there is not an optimisation to remove but the thing making the model
  servable.
- **A broken harness can no longer be published as a finding.** An earlier run of that experiment
  reported "the single-device engine did not start", which was coherent, quotable and false: the
  probe script had been mangled and never executed. `_harness_broke()` now checks for shell-error
  markers, missing status markers and impossibly short elapsed time, and raises rather than
  concluding. When an instrument fails the correct output is "nothing was learned".
- **CRLF corruption fixed at source.** `subprocess` with `text=True` applies universal-newline
  translation on WRITE, so a script sent from a Windows host reaches a POSIX shell as CRLF and dies
  with ``$'\r': command not found`` -- or worse, mis-parses `for ... do` and function bodies. The
  transport now sends stdin as bytes. This had cost a maintenance window, because the failure looks
  like a quoting mistake in the script rather than a property of the pipe.
- **`gpubench/longform/regression.py`**: extracts every number from two builds of a report and diffs
  them, so a release claiming "no measured value moved" can prove it. Prose is aligned by masked
  context; table cells are compared as a multiset, because inside a table the neighbours are other
  numbers and positional alignment produces spectacular false positives.
- **`gpubench/template/`**: the report data contract (value envelopes, closed `kind` enum, per-kind
  required fields), the section manifest with per-section invariants, the rule book, and a working
  linter with 87 tests and fixtures reproducing real historical defects.
- Site-specific values -- container names, model, cache paths, redaction wordlists -- are supplied by
  config and are absent from the tool. A general-purpose tool that ships one estate's names leaks
  them to everyone who downloads it, and the tool's own packaging gate now enforces that.

133 tests, all passing from a clean extraction of the release archive.

## 0.9.0

Long-form report generation moves into the tool. Until now this tool could measure a machine, derive
its ceilings and diagnose its readings, and then hand you JSON. Turning that into a document people
would actually read took a 3,200-line generator sitting beside one particular report.

- **New `gpubench/longform/`**: the report ENGINE. Inline-SVG chart primitives, tables, figures with
  their table views, section ordering, renumbering, cross-reference resolution, the contents page,
  the print stylesheet, PDF export with contents page numbers and a navigable outline, DOCX export,
  and a pre-publication redaction gate.
- **New `gpubench article` subcommand.** One command renders HTML, PDF, DOCX and runs the gate:

      gpubench article <content-module.py> <run-dir> --pdf --docx --check

- **The engine knows nothing about hardware.** A report supplies a CONTENT MODULE -- its title,
  reading order, figures and prose -- and the engine renders it. That split is what makes this
  belong in a general-purpose tool: the prose about one machine is not reusable and the machinery
  around it entirely is.
- Four mechanisms are now reviewable and testable in one place instead of buried beside one report:
  authoring order is declared separately from reading order; section numbers are assigned in final
  document order rather than taken from what an author typed; in-text references name a section by a
  fragment of its TITLE and are resolved after renumbering, with an unresolvable or ambiguous
  reference ABORTING the build; and the contents is built from the sections actually present. Each
  of those replaced a defect that had shipped.
- `pdf_export.paginate()` resolves contents page numbers from the RENDERED document, because CSS
  `target-counter()` is unimplemented in the rendering engine, then re-renders and VERIFIES that
  nothing moved rather than assuming it.
- Two path bugs were introduced and fixed by the move itself: the DOCX exporter derived its figure
  directory from `__file__`, which after the move pointed inside the tool's own package, and the
  redaction gate defaulted to scanning the tool instead of the artifacts. Both now take the source
  document or directory explicitly, and neither has a default.

The move was gated on reproducing the existing 53-page report **byte-identically**, which it does.

## 0.8.0

The tool now accounts for what it measures, instead of leaving an odd number for a human to
investigate. This is the release that closes the gap between "reports readings" and "reaches
conclusions".

The motivating case. An earlier run recorded that the serving engine's prefix-cache counters read
zero. True, useful, and where the tool stopped. Explaining that reading -- separating "the cache was
consulted and missed" from "the cache was never running" from "the counter is broken" -- took a
person reading engine source inside a container for most of a day. All three look identical from
outside and they license opposite recommendations: one of them turned a change previously called
"the cheapest possible win" into something needing a maintenance window. Every step of that
investigation was a rule over data the tool already had or could fetch in one request.

- **New `engine_config` probe.** Captures the engine's RESOLVED configuration from its own metrics
  and model endpoints, not the launch command. Read-only, two GET requests. This matters because a
  flag nobody passed still has an effective value, and an engine can resolve a different one from
  the model it loaded. Settings the diagnostics reason about are normalised across engine spellings;
  everything published is kept verbatim so a reading this tool does not yet understand is recoverable
  rather than lost.
- **New `gpubench/diagnose.py`**: nine rules that turn readings into stated conclusions with their
  evidence attached, covering prefix-cache behaviour, shared-versus-exclusive roofs, step
  attribution, power limits, device-link asymmetry, workload disclosure and size-control drift,
  reproducibility, quality gates, and comparability provenance. Findings are ordered by what a
  reader must act on.
- **Three rules the module holds itself to.** A finding names its evidence. "I cannot tell" is a
  first-class, loud outcome -- a diagnostic that silently passes when it could not look is worse
  than no diagnostic, because it reads as an all-clear. And it never upgrades "did not" into
  "cannot": the investigation this encodes made exactly that error on its first pass and inverted a
  recommendation, so rules that can only establish the weaker claim say the weaker claim and name
  the check that would settle the stronger one.
- Diagnostics run over the finished bundle on every run and travel in the result file. The report
  renders them BEFORE the numbers, because a caveat printed after a table is a caveat that gets
  skipped.
- 29 new tests, one per conclusion a human previously had to reach by hand, including tests that the
  module REFUSES to overstate.

## 0.7.0

The derivations move into the tool. Until now this tool could measure a machine but could not tell
you which ceiling was stopping it: the roofline arithmetic lived in a script beside one particular
report. That is precisely why a wrong derivation survived three published editions -- nothing a
reader of the tool would ever open contained the arithmetic.

- **New `gpubench/analysis.py`**, the single place any ceiling, ridge point or attribution is
  computed: all-reduce interpolation, ridge point, memory-bandwidth decode floor, decode
  attribution, interconnect and compute prefill ceilings, concurrency ceiling with support for a
  fixed per-sequence state, and achieved-fraction. Three rules hold throughout: no hardware or model
  constants (a function that cannot be given a fact returns None rather than assuming one), every
  derived value reports its own inputs and formula, and the module is pure and stdlib-only so every
  derivation is testable without a device.
- **17 unit tests ship with the tool** (`tests/test_analysis.py`), including a fixture that
  reproduces the historical interpolation defect and asserts the current implementation beats it.
  The load-bearing test is that a derived value at a *measured* message size reproduces the
  measurement exactly -- if that ever fails, a published table and its published derivation
  disagree, and any reader with a calculator will find it.
- `report.py` now consumes `analysis.py` instead of doing its own arithmetic, so the tool's report
  and any report built on the tool cannot diverge.
- **`sanitise()` no longer loses its own manifest.** It was not idempotent: a second pass saw the
  already-emptied container list, skipped the branch, and rewrote a shorter manifest. One shipped
  result file therefore declared only "GPU process names and PIDs" while also carrying
  `container_count: 19` beside an empty list. The manifest now accumulates and detects
  already-redacted state.
- Tests are included in the distribution archives.

## 0.6.0

The workload becomes part of the disclosure. A benchmark that does not say what it sent cannot be
reproduced, and every probe that generates input now describes that input in its own output.

- **Prefix-cache hit rate is measured, not argued.** The load generator has always put a unique
  integer at the *start* of every prompt so a serving engine cannot serve it from a shared prefix.
  That was a construction argument: had the salt been appended instead of prepended it would have
  looked identical, defeated nothing, and inflated every prefill figure with no visible symptom.
  The serving probe now reads vLLM's `prefix_cache_queries_total` and `prefix_cache_hits_total`
  around each level and reports the hit rate per level, distinguishing three cases that are easy to
  conflate: counters absent, zero queries (caching inactive), and hits from a non-zero query count.
- **Every input-generating probe emits a `workload` block**: prompt template, filler token, salt
  policy, how length is controlled, why synthetic was chosen, and what that choice costs. Both the
  requested length and the engine's own token count are recorded per level, so the word-count
  approximation is auditable instead of assumed.
- **The prompt-length mixture carries its provenance.** `input_mix_provenance` states plainly that
  the shares are assumed rather than sampled from production, that the spread between rows is the
  finding, and that the weighted average should not be used for planning without a measured
  distribution.
- **The accuracy gate publishes its cases.** The full prompt list and accept patterns travel in the
  result under `method.cases_published`, so "10 of 10, PASS" becomes an artifact someone else can
  re-run rather than an assertion.
- The embedding probe records requested document length in words and notes that the model's
  tokenizer may split the filler, so the true token count is the figure to quote.

## 0.5.0

Corrections from an external review of the report this tool produced. Both were the same class of
defect: arithmetic that looked authoritative but could not be rebuilt from the published data.

- **Interconnect-ceiling derivation fixed.** The prefill ceiling was derived by interpolating
  all-reduce *latency* log-linearly between measured points. In the bandwidth-bound part of the
  curve latency is linear in message size, not logarithmic, so this overstated latency by up to 17%
  between samples and understated every ceiling derived from it. It now interpolates *bandwidth*,
  which is the quantity that actually varies smoothly, and every ceiling can be recomputed by hand
  from the published all-reduce table.
- **Decode attribution no longer carries another machine's constants.** Resident weight bytes per
  shard and layer count were hardcoded to the deployment this tool was written on. On any other
  machine that produced a confident, plausible, wrong attribution instead of an error. They are now
  explicit inputs (`GPUBENCH_SHARD_WEIGHT_GIB`, `GPUBENCH_MODEL_LAYERS`, `GPUBENCH_TP_SIZE`), and
  when they are absent the section states why it is omitted rather than guessing.
- The nearest-sample lookup used for decode all-reduce latency now **checks** that a sample really
  is near the target message size, instead of assuming it, and refuses to attribute across regimes.
- Every input to the attribution arithmetic is printed beneath it, so a reader can rebuild or
  reject the result.

## 0.4.0

Closes the two remaining improvements that did not need exclusive access to the hardware.

- **Variance on every timing.** Each measurement now reports mean, standard deviation and
  coefficient of variation across its timed iterations, not only a best-of-N point. A best-of-N
  figure hides whether the run was stable.
- **Between-run confidence.** `run_serving_repeats()` runs the serving sweep several times and
  reports the spread per concurrency level. Within-run and between-run variance are different
  questions, and only the second says whether a number would reproduce tomorrow.
- **Realistic prompt-length replay.** A new `mixed` serving mode replays a long-tailed length
  mixture instead of a single uniform length, because uniform prompts flatter a scheduler that
  real traffic would stress differently. The mixture is deterministic so two runs stay comparable,
  and it is reported with the result so anyone can substitute their own measured distribution.


## 0.3.0

Completes the tool: it now carries every measurement the original harness did, so one run answers
the hardware, serving, capacity and quality questions together.

- **NCCL all-reduce sweep** (`probes/nccl.py`). The tensor-parallel primitive, by message size.
  This is what reveals small and large messages to be two different regimes, only one of which a
  wider slot fixes.
- **Serving benchmark** (`probes/serving.py`). Time to first token, inter-token latency,
  throughput and the concurrency curve, with prefill and decode measurable in isolation.
- **Embedding benchmark** (`probes/embedding.py`). Retrieval pipelines usually bottleneck here
  before the model does.
- **Roofline attribution** in the report: one decode step decomposed into memory bandwidth,
  interconnect and an explicitly reported residual.
- The report now renders serving, interconnect, attribution, embedding and the accuracy gate.

Not ported: exclusive-mode peak measurement, which stops the served model and therefore needs a
maintenance window rather than a code path.


## 0.2.0

- **Accuracy gate** (`probes/accuracy.py`). Determinism under greedy decode plus exact-match on
  verifiable prompts. A speed benchmark cannot otherwise distinguish faster from worse.
- **Capability probe** (`probes/capabilities.py`). INT4 weight-only, NVENC and NVDEC throughput via
  ffmpeg, and enumeration of what cannot be measured with the reason stated.
- **Size-controlled compute comparison.** Every precision is now also measured at one common
  matrix size. Sizing each precision to its own footprint made the achieved-vs-reference table
  rank matrix sizes while appearing to rank precisions.
- **Throttle capture.** The power sampler records the driver's software power-cap and hardware
  slowdown flags, so "power-bound" is evidence rather than inference from a number near the cap.
- **Privacy model changed.** The target host is dropped rather than hashed: an unsalted truncated
  SHA-256 of `user@host` was recovered from a 24-candidate dictionary. Results are now sanitised
  as a whole, removing container names, image names, port bindings, GPU process names and GPU
  UUIDs. `--keep-identifying` opts out.
- Sampler honours its stated rate (it previously waited a full interval *after* each subprocess,
  giving 7.8 Hz while claiming 10 Hz).
- `n_for` sizes on output width too: `_int_mm` writes int32 and the FP4 path writes bfloat16, so
  the previous sizing undershot the real allocation by up to 2x.
- Packaging gate also scans the result and report artefacts users are told to share, not only the
  source it ships.

## 0.1.0

- First release. Three probe tiers, local and SSH transports, HTML report and index generation.
