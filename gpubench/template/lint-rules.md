# Lint rules for a benchmark report

Eleven rules. They are the enforcement half of `run-schema.json`. The schema says where a number is
allowed to live; these rules say what the renderer and the authored text are allowed to do with it.

**The one governing rule the eleven implement:** every number a reader sees is either measured and
labelled, or a stated derivation from labelled inputs, and it exists exactly once in the bundle.
Anything a sentence spells out for itself is a second copy, and a second copy is a copy that will
drift.

**Where this came from.** The source report went through three rounds of external review. Twelve
defects were found. All twelve were the same failure mode: a number was written into prose by hand
instead of being generated from the run data, and then drifted from the value it was supposed to
restate. Each rule below cites the defect it prevents, by number, so that a future maintainer who
thinks a rule is pedantry can read the real bug before deleting the rule.

| Ref | The historical defect |
|---|---|
| D1 | A rate reported as 82% in a table, 80% in prose, 83% in a recommendation. The prose carried values from before the matrices were size controlled. |
| D2 | A cap reported as "every one of the 201 busy samples" in one section and "399 of 401" in another. Prose counted one unit; the data held two. |
| D3 | A sustained figure given as 570 in prose against 566 in the data. Prose transcribed an older run. |
| D4 | A burst figure of 238.4 set beside 237.6, where the second was measured at a different problem size in a different run. A cross condition comparison presented as like for like. |
| D5 | Interconnect ceilings unreproducible from the published curve. The derivation interpolated latency log linearly where latency is linear in message size, and it lived in a script beside the report, so nobody could check it. |
| D6 | Stale hard coded cross references ("see section 18") after sections were reordered. |
| D7 | A version history missing two of its own versions, and listed out of order. |
| D8 | A workload size mixture (35/30/20/10/5%) presented as if measured. It was assumed. |
| D9 | "Harness and raw data published" in one section against "nobody outside has run it" in another. |
| D10 | The cover claimed a single run directory while three run artefacts contributed. |
| D11 | A decomposition with one machine's geometry hardcoded, so on any other machine it produced a confident wrong answer instead of an error. |
| D12 | A quality gate reported as "10 of 10, PASS" with its cases unpublished, hence unfalsifiable. |

---

## Vocabulary constraint that binds every rule (B-C2)

Nothing in the rules, the messages, or the allowlists may name a domain. The rules speak of **roofs**
(not TFLOPS), **workload** (not prompts), **quality gate** (not accuracy gate), **units** or
**devices** (not GPUs), **problem size** (not matrix dimension or token count). The same rule set
must lint a report about a CPU, a storage array or a network fabric. Domain vocabulary belongs in a
report's own bundle and config; if a rule needs domain words to be checkable, the rule is wrong.

## Extension constraint that binds every rule (B-C1)

If the report needs to say something these rules cannot express, **extend the rules or the schema**.
Never soften the sentence and never delete the rule to let the sentence through. The failure mode
these rules exist to stop is exactly the pressure to get one more paragraph out of the door.

---

## How the linter runs

* It runs inside the build, before any output file is written. It is not a separate step a busy
  author skips.
* Input: the run bundle (validated against `run-schema.json`), the authored text with its reference
  markers still in place, and the rendered output. Rules that need all three are marked.
* Severity is binary. Every rule is an **error**. There is no warning level, because a warning level
  is where all twelve defects would have lived: each one was locally plausible enough that a warning
  would have been read and dismissed.
* A failing build produces no report. Exit code 2 means at least one rule failed; exit code 0 means
  every rule passed and every waiver used was registered. There is no bypass flag. The escape hatches
  are per rule, they are declared in the bundle or the allowlist file, and they are printed in the
  report's own audit appendix, so using one is a visible act rather than a private one.
* Every message names: rule id, the exact location (section id, then figure or table id, then
  character offset), the offending text, the value it collided with or failed to resolve to, and the
  concrete remediation. A message that does not tell the author what to type is a message that gets
  worked around.

## The reference vocabulary the rules lint

Authored text carries markers, never digits. These are the forms the rules understand.

| Marker | Renders | Checked by |
|---|---|---|
| `{{v:id}}` | value at its own precision, with unit | L1, L2, L4, L7 |
| `{{v:id\|value}}` `{{v:id\|unit}}` `{{v:id\|label}}` | one component of the envelope | L1, L2 |
| `{{v:id\|n}}` `{{v:id\|spread}}` | sample count with its aggregation scope; spread with its scope | L1, L2 |
| `{{v:id\|kind}}` | the kind word, verbatim from the enum | L2, L4 |
| `{{sec:section_id}}` | the section's assigned number and title | L5 |
| `{{fig:figure_id}}` `{{tbl:figure_id}}` | figure or table reference, by id | L5, L6 |
| `{{run:run_id}}` | run id, window and role | L7 |
| `{{cmp:id_a,id_b}}` | a like for like comparison, with the derived difference | L8 |
| `{{xcmp:id_a,id_b\|why=...}}` | an explicitly annotated cross condition comparison | L8 |
| `{{ev:artifact_ref}}` | an evidence reference: a run artefact, the harness source, a case set | L9 |
| `{{ver:field}}` | a field of the version being built | L10 |
| `{{lit:NUMBER\|why=...}}` | a waived literal (see L1) | L1 |

A marker that does not resolve is a build failure in every case. Silent fallback to empty string or
to a default is how D11 happened at the value level, and it must not be reintroduced at the render
level.

---

# L1. No orphan literals

**Statement.** No numeric literal may appear in authored text if it matches a value in the data model
to within the drift band, unless it is an explicit interpolation of that value's id or it matches a
pattern on the published allowlist.

**Why it exists.** D1, D2 and D3 directly, and D4 and D8 in part: five of the twelve defects. Every
one of them was a digit typed into a sentence beside a table that had the same quantity in it. D1 put
82, 80 and 83 in three places for one value. D2 put 201 in one section and 399 of 401 in another for
one cap. D3 put 570 in prose against 566 in the data. None of the three sentences was wrong when it
was written. Each became wrong when the run behind it was replaced, the aggregation scope changed, or
the matrices were size controlled, and nothing connected the sentence to the change. This is the rule
that matters most, it is the most annoying rule to satisfy, and its absence produced nearly half the
defect list.

**What the linter checks.**

1. It extracts every numeric literal from **all** authored text, not just body paragraphs: title,
   cover, executive summary, headings, prose, bullet lists, table captions, figure titles, figure
   notes, reference line labels, recommendations, footnotes, appendix text, image alternate text, and
   document control. Four of the five L1 defects were in summary or recommendation text, which is the
   text reviewers read as narrative rather than as data.
2. For each literal it builds the candidate set: every envelope in `roofs[]`, `measurements[]`,
   `derived[]` and `assumptions[]`, in each of its renderable projections. Projections include the
   value at every precision from 0 to the envelope's own `precision`; the value scaled by 100 where
   the unit is a ratio, and by 1/100 where it is a percent; the value under standard magnitude
   prefixes for its unit; both ends of a range; every point of a series; `n`; `aggregation.unit_count`
   and every entry of `aggregation.per_unit`; `spread.value`; and, for a `fixed-test-set`, the case
   count and the pass count.
3. A literal **collides** with a projection when the absolute relative difference is within the drift
   band, defined as the larger of five per cent and two units in the last place of the literal as
   written. The band is deliberately wide. D1's 80 sits 2.4% from 82 and D1's 83 sits 1.2% from it, so
   a band of half a rounded digit would have caught neither. False positives from a wide band cost one
   allowlist pattern each, once. A false negative costs a published contradiction.
4. Collisions are also checked across scope, which is D2's mechanism: a literal that matches a
   per unit projection while the surrounding sentence quantifies the aggregate, or the reverse, is a
   collision even when some other projection matches exactly.
5. Numbers written as words ("two hundred and one", "eighty per cent") are extracted too. Spelling a
   number does not make it a different number.
6. The rule then reports every collision that is not an interpolation and not allowlisted.

**Failure messages.**

```
L1 no-orphan-literals: section "power_envelope", prose offset 412: literal 570
  collides with value power_sustained_mean (566.4 W, precision 1, run run_final).
  |570 - 566.4| = 3.6 (0.64%), drift band 5.00%.
  Fix: write {{v:power_sustained_mean}}. If 570 is genuinely a different quantity,
  give it its own envelope and interpolate that, or add an allowlist pattern with a reason.
```

```
L1 no-orphan-literals: section "executive_summary", prose offset 88: literal 80
  collides with value roof_fraction_low_precision (82 %, precision 0, run run_final).
  |80 - 82| = 2 (2.44%), drift band 5.00%.
  Note: two other appearances of this value in the document render as 82.
  Fix: write {{v:roof_fraction_low_precision}} so all three appearances move together. (D1)
```

```
L1 no-orphan-literals: section "power_cap", prose offset 205: literal 201
  collides with value cap_busy_samples.n (401, aggregation over=units unit_count=2,
  per_unit=[201, 200]).
  Scope mismatch: the literal matches a per-unit projection while the sentence
  quantifies the aggregate ("every one of the ... busy samples").
  Fix: write {{v:cap_busy_samples|n}}, which renders the count with its aggregation
  scope, or interpolate the per-unit envelope explicitly. (D2)
```

**Allowlist.** The allowlist is an **explicit list of patterns**, not a tolerance, not a per section
opt out, and not a switch that turns the rule down. It lives in `lint-allowlist.json` beside the
bundle, every entry carries a reason, and the whole list is printed in the report's audit appendix so
a reader can see exactly which digits were exempted and why. Each entry is a regular expression plus
a context restriction, so widening the list widens it by one named shape rather than by degree.

| # | Pattern class | Regex (illustrative) | Why it is genuinely free standing |
|---|---|---|---|
| A1 | Calendar years and dates | `\b(19\|20)\d{2}\b`, ISO dates | A year is not a measurement of the system under test. It cannot drift from a value because no value denotes it. |
| A2 | Section, figure, table and item counts of this document | `\b\d{1,3}\b` only inside the document control block, and only for keys on the counted list | These are properties of the document, generated by the renderer, and L5 and L10 already own them. Note the context restriction: the pattern is not "small integers anywhere". |
| A3 | Cited literature and standards | numbers inside a citation span, a standard name, or a quoted title | A cited figure belongs to its source. It must not be silently updated to match this run; if it is being compared, it is a `published` envelope and A3 does not apply. |
| A4 | Version and revision numbers | `\bv?\d+\.\d+(\.\d+)?\b` inside a version span | Identifiers, not quantities. Governed by L10. |
| A5 | Quantities defined in the prose itself | numbers inside an explicit definition form: "we define X as N", "the bar was fixed at N before the run", where the same N is also present in the bundle as `preregistered_bar.threshold` or an `assumption` | The prose is the definition site, so there is no upstream copy to drift from. The bundle cross check keeps this from becoming a loophole: the definition must exist as an envelope or a pre registered bar. |
| A6 | Enumerated document scaffolding | list ordinals, step numbers, "first", "second" as digits in a numbered procedure | Ordinals of the text, not of the measurement. |
| A7 | Units, formats and identifiers embedded in names | `\bFP\d+\b`, `\bx\d+\b` lane widths inside an identifier, port numbers, model or part numbers, path fragments | Part of a name. A name is a string that happens to contain digits. Restricted to token position inside an identifier, never a standalone number. |
| A8 | Exact powers used in a printed formula | digits inside a `formula` string or a fenced code block | The formula is published so that a reader can execute it. Its constants are the derivation, and L3 checks them against the recomputation. |
| A9 | Trivial cardinals in generic prose | `\b(one\|two\|1\|2)\b` only where no candidate projection collides and the token is not adjacent to a unit string | Narrow on purpose: adjacency to a unit removes the exemption, which is what keeps "two units" allowed while "2 W" is not. |

Anything not on that list is not exempt. In particular these are **not** allowlisted, because each one
was a real defect or one step from one: percentages of any kind, sample counts, unit counts, sizes,
rates, durations, temperatures, power figures, price figures, counts of passing cases, and any number
that appears within one line of a unit symbol.

**Escape hatch.** `{{lit:NUMBER|why=...}}` waives one occurrence. The rule for using it legitimately:
the number must be a real quantity that genuinely has no envelope and cannot have one, the reason must
name why it cannot, and the waiver is reported in the audit appendix with its reason. It is capped:
more than five waivers in a build fails L1 with `L1 waiver-budget-exceeded`, on the argument that a
document needing six exceptions has a modelling problem, not a lint problem. A waiver must not be used
to restate a value that exists; that is the defect, spelled with extra syntax.

---

# L2. Every value has a kind

**Statement.** Every rendered number resolves to an envelope, and that envelope's `kind` is one of the
closed enum values, with the fields that kind requires present.

**Why it exists.** D8: a workload size mixture of 35/30/20/10/5% was printed in a table beside
measured throughput figures, in the same typeface, with no marking. It was assumed. Readers, including
three rounds of reviewers, took it for measurement because everything around it was measurement. A
closed kind enum with per kind required fields means a number cannot enter the document without
declaring what it is. The enum is closed for exactly this reason: an open vocabulary lets an
assumption be labelled something that sounds measured.

**What the linter checks.**

1. Every `{{v:...}}` marker resolves to exactly one envelope in the bundle.
2. Every rendered numeric cell in every table and every figure series traces to an envelope id. A
   literal in a table cell fails, as does a chart series with inline data.
3. `kind` is present and is one of: `measured`, `derived`, `assumption`, `projection`, `supplied`,
   `published`, `enumerated`, `fixed-test-set`.
4. The kind's obligations hold: `measured` has `conditions` and `n`; `derived` and `projection` have
   `inputs`, `formula` and `conditions`; `assumption` and `projection` have `rationale`; `published`
   has `provenance` with both citation and unpacking; `supplied` has `supplied_by`; `enumerated` has
   `why_not_measured`; `fixed-test-set` has `cases`, `licenses` and `does_not_license`.
5. Numeric values carry `precision`. A number stored as a string fails, because it cannot be rounded,
   compared or checked.
6. Ids are unique across `runs[]`, `roofs[]`, `measurements[]`, `derived[]`, `assumptions[]` and
   `figures[]` taken together. A duplicated id is how two different values come to print under one
   name.

**Failure messages.**

```
L2 every-value-has-a-kind: figure "size_mixture", table_view cell [2][1]: literal 0.35
  is not an envelope reference.
  Fix: create an envelope for this share and reference it by id. If the mixture was chosen
  rather than observed, its kind is "assumption" and it needs a rationale. (D8)
```

```
L2 every-value-has-a-kind: value "mixture_share_short" has kind "assumption"
  but no rationale.
  Fix: state what the share is based on and what the report may not conclude from a
  weighted average of these shares. (D8)
```

**Allowlist.** None on the kind requirement itself. The only relaxation is `spread_not_available`,
which lets an instrument that genuinely reports only an aggregate pass without a spread, and it takes
a string reason that is printed. Legitimate use: the instrument's own documentation says it exposes no
per sample data. Not legitimate: nobody collected it this time.

---

# L3. Derived is rebuildable

**Statement.** Every `derived` value recomputes from its declared inputs, by its printed formula, to
within its stated precision, and every input is printed somewhere in the report.

**Why it exists.** D5. Two interconnect ceilings could not be reproduced from the all reduce curve
printed two pages earlier. The cause was an arithmetic error: the derivation interpolated **latency**
log linearly, where latency is linear in message size. The reason nobody caught it was structural: the
derivation lived in a script beside the report, not in the tool, so there was nothing to review and
nothing to test. D11 is the same rule from the other side: a decomposition carried one machine's
geometry as hardcoded constants, so on a different machine it silently produced a confident wrong
answer. A derivation that is executed by the linter cannot be wrong in private, and a derivation whose
inputs must be printed cannot be checked only by its author.

**What the linter checks.**

1. Each `derived` envelope's `inputs` all resolve, and none of them resolves to the derived value
   itself, directly or through a cycle.
2. The `formula` string parses, names every id in `inputs`, and names nothing that is not in `inputs`.
   A constant that is not an input is a hardcoded constant, which is D11; it must be promoted to an
   `assumption` or `enumerated` envelope with its own kind and source.
3. The linter **executes** the formula against the input values and compares the result with the
   stored value at the stored precision. A mismatch fails with both numbers.
4. Every input id is rendered somewhere in the document, in prose, a table or a figure table view. An
   input that is used but never shown makes the derivation unreproducible by a reader even though it
   is reproducible by the linter, which is precisely D5's reader experience.
5. Interpolation and curve fitting carry their scale. Where an input is a series and the derivation
   interpolates, the envelope must state the interpolation basis (linear, log linear, per axis), and
   the basis must match the axis scale declared on the figure that publishes the series. The mismatch
   between these two is D5's actual arithmetic error, and it is mechanically checkable.
6. `requires_inputs_present` defaults true, and the linter asserts that the computing code fails on a
   missing input rather than substituting a default. A `false` here requires a rationale, and the
   rationale is printed.
7. `computed_by` must point inside the harness where `tool.derivations_unit_tested` is true. Where it
   points at a script beside the report, the linter emits the D5 message and the report must print
   that the derivation is untested.

**Failure messages.**

```
L3 derived-is-rebuildable: value "interconnect_ceiling_large" does not recompute.
  formula: message_size / (latency_at_size - latency_intercept)
  inputs:  message_size = 268435456 B, latency_at_size = 0.0412 s,
           latency_intercept = 0.00031 s
  recomputed 6564.9 MB/s, stored 7710.0 MB/s, precision 1, difference 17.4%.
  Interpolation basis declared "log-linear" but figure "allreduce_sweep" declares
  axis y scale "linear". Latency is linear in message size. (D5)
```

```
L3 derived-is-rebuildable: value "decode_attribution_weights" names constant 27000000000
  in its formula but not in inputs.
  Fix: promote the constant to an envelope with a kind and a source. A derivation that
  carries one machine's geometry as a literal produces a confident wrong answer on any
  other machine instead of an error. (D11)
```

```
L3 derived-is-rebuildable: value "ridge_point" recomputes correctly, but input
  "roof_bandwidth_shared" is never rendered in the document.
  Fix: print the input, or drop the derived value. A derivation the reader cannot
  execute is the defect even when the arithmetic is right. (D5)
```

**Allowlist.** One escape hatch, `rebuild_tolerance`, a per envelope numeric tolerance for a
derivation that legitimately cannot be reproduced to the last digit, for example one that runs a
solver or a fit. Legitimate use: the derivation is iterative and the tolerance is the solver's own
convergence criterion, stated. Not legitimate: widening the tolerance until an existing mismatch
passes. The tolerance is printed next to the value, and a tolerance above one per cent additionally
requires `caveat` text, because at that point the reader needs to know the value is approximate.

---

# L4. Assumptions stay labelled

**Statement.** A value with kind `assumption` or `projection` carries its label at **every**
appearance, not just at the first, and not only in the section that introduces it.

**Why it exists.** D8. The mixture was labelled once, in the methodology, and then quoted three more
times bare, including in the recommendation a reader would act on. A number is cited from wherever it
is read, not from wherever it was first defined. The same applies to `illustrative_only` values, whose
whole purpose is to show a shape and which get lifted out and quoted as results.

**What the linter checks.**

1. Every rendered appearance of an `assumption` or `projection` envelope carries the kind marking: the
   word, in the same visual weight as the number, adjacent to it. Not a superscript that survives only
   in the HTML, and not a legend entry two hundred lines away.
2. Any `derived` value whose input closure includes an assumption or a projection inherits the marking,
   with the assumed input named. An unmarked derived value with an assumed input is an assumption
   laundered through arithmetic.
3. Any value with `inherits_floor` true is qualified at every appearance, and any percentage computed
   against a floor is rendered as an upper bound on achievement rather than as the achievement. A
   fraction of a floor is not a fraction of a ceiling.
4. `illustrative_only` values are marked in place, at every appearance.
5. Where a `distribution` has `kind: assumption` and `weighted_summary_permitted` is false or absent,
   no weighted single figure derived from those shares may be rendered at all. This is the D8 defect
   in its strongest form: a weighted mean of an assumed mixture is an assumption dressed as a result.

**Failure messages.**

```
L4 assumptions-stay-labelled: section "recommendations", appearance 3 of value
  "mixture_share_short" (kind assumption) is rendered without its label.
  Labelled appearances: 1 (section "workload"). Unlabelled: 2, 3.
  Fix: the renderer must emit the kind marking at every appearance. A number is cited
  from where it is read. (D8)
```

```
L4 assumptions-stay-labelled: value "throughput_weighted_mean" is derived from
  distribution "size_mixture" whose kind is "assumption" and whose
  weighted_summary_permitted is false.
  A weighted mean of an assumed mixture may not be rendered. Show the spread across
  sizes instead. (D8)
```

**Allowlist.** None on the marking itself. There is one presentational hatch: a table may hoist the
marking to a column header plus a per row marker where a whole column shares one kind, provided every
row still carries a visible marker. A footnote alone does not satisfy the rule. `label_style` may
choose the wording, for example "assumed" against "assumption", but not whether it appears.

---

# L5. Cross references resolve

**Statement.** Every reference resolves to a section, figure, table or value id that exists, and no
section, figure or table **number** appears in authored text.

**Why it exists.** D6. Sections were reordered between editions and "see section 18" now pointed at
something else. It is the most obviously mechanical of the twelve defects and it still survived three
review rounds, because a reviewer verifying a cross reference has to leave the sentence, find the
target and come back, and nobody does that for every reference in a fifty page document.

**What the linter checks.**

1. Every `{{sec:...}}`, `{{fig:...}}`, `{{tbl:...}}`, `{{run:...}}` and `{{ev:...}}` marker resolves.
2. No authored text contains a literal that reads as a document number: patterns such as `section \d+`,
   `figure \d+`, `table \d+`, `appendix [A-Z]\b`, `page \d+`, in any case. Numbers are assigned by the
   renderer, so a number in the source is by construction a guess about the renderer's output.
3. Every declared section id in `sections[]` is either rendered or reported as unrendered, and every
   rendered section has a declared id. A section nothing can reference is unreachable.
4. `cross_references` declared in `sections[]` and the markers actually present in that section's text
   agree. A declared reference that the text does not make, or a reference the text makes that is not
   declared, both fail, because the declared list is what a reviewer reads.
5. Forward and backward references both resolve after ordering. Reordering the bundle must not be able
   to break a reference, and the linter proves that by re-resolving under a shuffled order.

**Failure messages.**

```
L5 cross-references-resolve: section "power_envelope", prose offset 640:
  literal document reference "see section 18".
  Fix: write {{sec:interconnect_analysis}}. Section numbers are assigned at render time;
  a number in the source is a guess that goes stale the next time sections move. (D6)
```

```
L5 cross-references-resolve: marker {{sec:thermal_headroom}} in section
  "recommendations" does not resolve. Nearest declared ids: thermal_limits,
  power_headroom.
```

**Allowlist.** A reference to a section of an **external** document may carry that document's own
numbering, inside a citation span, which is allowlist class A3 of L1. Legitimate use: citing clause
numbering of a published standard. Not legitimate: referring to a section of this report by number
because the marker was inconvenient.

---

# L6. Figures carry tables

**Statement.** Every figure has a table view, and every value in that table is an envelope reference.

**Why it exists.** This is the rule that makes D5 discoverable by a reader rather than only by the
linter. The interconnect ceilings were derived from a curve that was published as a picture. Nobody
could check the derivation because nobody could read the numbers off the chart. A chart whose numbers
exist only inside the chart is not evidence, it is an illustration of evidence held elsewhere. It also
forecloses D1's mechanism: a table of literals beside a chart of envelopes is two sources of truth for
one quantity.

**What the linter checks.**

1. Every entry of `figures[]` has `table_view`, either with `columns` and `value_ids`, or with
   `same_as_figure` naming another figure whose table it shares.
2. Every id in `value_ids` resolves, and every rendered cell traces to one of them. No literal cells.
3. Where `same_as_figure` is used, both figures' series resolve to the same underlying envelope ids.
   Sharing a table between two figures that plot different data fails.
4. Every series drawn on the chart appears in the table, and every table column appears on the chart or
   is declared as table only. A chart that draws more than its table publishes is the D5 shape again.
5. Axis `scale` is declared for any axis where a reader might infer linearity, and reference lines are
   envelope references with `is_floor` mirrored from the roof, so a floor is drawn and labelled as a
   floor.
6. The table renders at the envelopes' own precision. A table that rounds differently from the prose
   is D1 with a ruled border.

**Failure messages.**

```
L6 figures-carry-tables: figure "allreduce_sweep" has no table_view.
  Fix: add columns and value_ids, or declare same_as_figure if it is a second view of
  another figure's data. A curve that a reader cannot read numbers off cannot support a
  derived ceiling. (D5)
```

```
L6 figures-carry-tables: figure "roof_utilisation" table_view cell [3][2] renders
  literal 82 with no value_id.
  Fix: reference the envelope. A table of literals beside a chart of envelopes is two
  sources of truth for one number. (D1)
```

**Allowlist.** `chart_type: "table-only"` is a legitimate figure with no chart. A schematic or a
topology diagram that carries no measured quantities may declare `no_quantities: true` with a reason,
and then needs no table; the linter verifies the claim by checking that the figure renders no numeric
text. Legitimate use: a block diagram of the system under test. Not legitimate: a chart with axes and
data, on the argument that the numbers are "in the text somewhere".

---

# L7. Run provenance

**Statement.** Every value's `run_id` exists in `runs[]`, and where more than one run contributes to
the report, document control names all of them.

**Why it exists.** D10: the cover said the report came from a single run directory while three run
artefacts contributed. D3 is the same rule at value scale: a sustained figure had been transcribed
from an older run, and carrying the run id with the value is what makes that visible instead of
invisible. Provenance stated once on a cover page is a sentence someone remembered to write; provenance
carried by every value is structural.

**What the linter checks.**

1. Every envelope's `run_id` resolves to an entry in `runs[]`. There is no exemption for `published`,
   `supplied` or `assumption` values; those take the id of the run whose report they appear in, so that
   the set of run ids in use is complete by construction.
2. Exactly one run is `primary`. Every headline value, meaning every value referenced from
   `figures_of_merit` and from the executive summary, comes from the primary run, or is visibly
   attributed at the point of use.
3. The set of run ids actually referenced by rendered values equals the set of runs declared. A
   declared run that contributes nothing must be removed; an undeclared run cannot be referenced.
4. Document control renders the full run register: one row per contributing run, with window,
   harness version, machine fingerprint, comparability fingerprint and `produced`. The count of rows
   is generated, never typed, which is what makes the D10 sentence impossible to write.
5. Any figure, table or claim that mixes runs is declared in `cross_run_blends[]`, and
   `disclosed_in_place` is true, so the blend is stated where it is used and not only in an appendix.
6. The rebuild test: for each run, the linter recomputes which rendered values disappear if that run
   is removed, and asserts that the set matches the run's own `produced` list. A run whose declared
   contribution does not match its actual contribution fails.
7. Every envelope's `mode` either matches its run's mode or overrides it explicitly. A silent mismatch
   fails.

**Failure messages.**

```
L7 run-provenance: document control renders "single run directory" while 3 run ids are
  referenced by rendered values: run_final, run_instrumented, run_sustained.
  Fix: generate the run register from runs[]. The count of contributing runs is data,
  not narrative. (D10)
```

```
L7 run-provenance: value "power_sustained_mean" has run_id "run_20260812" which is not
  in runs[]. Declared runs: run_final, run_instrumented, run_sustained.
  Fix: either declare the run or re-measure. A value from an undeclared run is how a
  figure from an older run reaches a current report. (D3)
```

```
L7 run-provenance: run "run_instrumented" declares produced=["cache counters"] but
  removing it also removes rendered values: decode_step_time, decode_attribution_weights.
  Fix: correct produced[] or move the values. (D10)
```

**Allowlist.** None on resolution. One narrow hatch on the primary run rule: a value may come from a
supporting run when the point of use carries the attribution, and the pair is registered in
`cross_run_blends[]` with `why_permissible`. Legitimate use: an instrumentation pass that reads a
counter the primary run cannot read without perturbing it. Not legitimate: a value from whichever run
happened to produce the more attractive number.

---

# L8. Comparison hygiene

**Statement.** Any two values compared in authored text share matching comparability conditions, or
the comparison is annotated as cross condition, in place.

**Why it exists.** D4. A burst figure of 238.4 was set beside 237.6 as though the two were like for
like. The second had been measured at a different problem size, in a different run. The sentence read
perfectly. The comparison was meaningless, and worse, it was the basis of a conclusion about stability.
Conditions on the envelope make this mechanical: two values are comparable when the keys that matter
match, and the linter can check that where a reviewer cannot.

**What the linter checks.**

1. It detects comparisons in three ways: explicit `{{cmp:a,b}}` markers; comparative language adjacent
   to two value markers ("against", "versus", "compared with", "higher than", "the same as", "within",
   "unchanged from"); and two markers rendered into one table row or one chart series pair.
2. For each detected pair, it compares `conditions` key by key: `problem_size`, `concurrency`,
   `batch_size`, `precision`, `duration_class`, `service_level`, `percentile`, `mode`, `parallelism`,
   `workload_id`, `power_state`, `unit_index`, and any additional keys present. Unknown keys are
   compared for equality too, so adding a condition key makes comparison stricter and never looser.
3. It compares run comparability: differing `comparability_fingerprint` hashes fail unless the pair is
   registered in `cross_run_blends[]`.
4. It honours `not_comparable_with`: a pair listed there fails unconditionally, with the recorded
   reason printed. It verifies `comparable_with` against conditions rather than trusting the claim.
5. It checks aggregation compatibility, which is D2 in comparison form: a per unit value against an
   aggregate, or a mean against a sum, fails.
6. A percentage computed against a value with `is_floor` true may not be compared with one computed
   against a true ceiling, in either direction.
7. `{{xcmp:a,b|why=...}}` renders the comparison **with** the differing keys named in the output, so the
   annotation reaches the reader and not just the source.

**Failure messages.**

```
L8 comparison-hygiene: section "stability", prose offset 301 compares
  burst_rate_high_precision (238.4, problem_size 8192, run run_final) with
  burst_rate_reference (237.6, problem_size 4096, run run_instrumented).
  Differing conditions: problem_size (8192 vs 4096); run comparability_fingerprint
  (a91c vs 4f02).
  Fix: compare like for like, or use {{xcmp:...|why=...}} so the difference in
  conditions is printed where the comparison is made. (D4)
```

```
L8 comparison-hygiene: value "quantised_rate" lists not_comparable_with
  "published_dense_rate" ("weight-only quantisation has no dense counterpart on this
  runtime"), and section "roofs" compares them.
  Fix: remove the comparison. A prohibition recorded on the value outranks a paragraph
  that wants to make the comparison anyway. (D4)
```

**Allowlist.** `{{xcmp:a,b|why=...}}` is the escape hatch, and it is not a suppression: it changes what
is printed. Legitimate use: the report's point **is** the difference in conditions, for example showing
that a figure moves with problem size. Not legitimate: annotating a comparison to get it past the
linter while the sentence still reads as like for like. The linter enforces this asymmetry by requiring
that the rendered output name the differing keys, so a reader sees the caveat whether or not the author
worded it in.

---

# L9. Claims need evidence

**Statement.** Words that assert external verification require a resolvable artifact reference at the
point of the claim.

**Why it exists.** D9. One section said the harness and raw data were published; another said nobody
outside had run it. Both were written by the same author about the same artefact, months apart, and both
were locally plausible. Reproducibility status is a property of the world, so it must be read from one
field and rendered, never asserted per section.

**What the linter checks.**

1. It scans authored text for the claim vocabulary: published, open source, available, obtainable,
   reproduced, reproducible, independently, verified, validated, audited, confirmed, third party,
   peer reviewed, certified, replicated, attested. The list lives in the config and is additive only.
2. Each hit must be adjacent to an `{{ev:...}}` marker that resolves to a real evidence target:
   `tool.source_url` with `tool.published` true; an entry of `runs[].artifacts`; a `cases[]` set; a
   `workload.generation` recipe with its seed; a `provenance.citation`; or an entry of an `x_evidence`
   registry where a domain has added one. If a claim's evidence has nowhere to resolve, **extend the
   bundle with an evidence entry**; do not soften the sentence into something unfalsifiable, and do not
   delete the rule (B-C1).
3. Consistency across the whole document: every sentence about publication or reproduction is generated
   from `tool.published` and `tool.source_url`. Two sections cannot disagree, because neither section
   holds the fact.
4. `verified_by` in a version history entry, and any claim of verification about the report's own build,
   must name the procedure. Verified by assertion is not verified.
5. "Independently" additionally requires an evidence target whose owner is not the report's own author,
   recorded on the evidence entry. Where none exists, the linter demands the weaker true sentence,
   generated for the author, and prints what would need to happen for the strong one to become true.

**Failure messages.**

```
L9 claims-need-evidence: section "reproducibility", prose offset 55: claim word
  "published" with no adjacent {{ev:...}} marker.
  tool.published = false, tool.source_url = absent.
  Fix: the generated sentence for this state is "the harness is not obtainable by a
  reader, so these results are not independently reproducible". Publish the harness and
  set the field, or print that sentence. (D9)
```

```
L9 claims-need-evidence: section "quality_gate", prose offset 120: claim word
  "verified" resolves to {{ev:gate_cases}} but that evidence entry has no cases.
  Fix: publish the cases, or drop the claim. (D12)
```

**Allowlist.** Two hatches. First, a claim **about a cited third party's own work** may resolve to a
`provenance.citation` rather than to a local artefact; legitimate when the sentence is reporting what
someone else published, not what this report established. Second, `claim_words_exempt_context` allows
the claim vocabulary inside a quoted passage or inside a stated non claim (the "we do not claim to have
independently verified" form), because the words appear there in negation. Both hatches are context
restrictions, not per sentence waivers, and neither can be used to leave a positive claim standing
without a target.

---

# L10. Version history complete

**Statement.** A row exists for the version being built, the ordering is monotonic, and there are no
gaps in the chain.

**Why it exists.** D7. A published version history was missing two of its own editions and listed the
rest out of order. The history is the one part of a report a reader uses to decide whether a number
they cited last quarter still holds, so a hole in it silently invalidates that use. Since editions get
written in a hurry, the check has to be mechanical.

**What the linter checks.**

1. `version_history[]` contains an entry whose `version` equals the version being built, with a date and
   a summary.
2. Every entry names `previous_version`, exactly one entry has `previous_version: null`, and following
   the chain visits every entry exactly once. This is what turns a list into a chain: a missing edition
   breaks the walk instead of passing unnoticed.
3. Versions sort monotonically along that chain, and dates are non decreasing along it.
4. No duplicate versions, and no entry whose version is greater than the version being built.
5. `measured_values_moved` is checked against the values, not trusted: where a previous build's bundle
   is available, the linter diffs the rendered values, and where an envelope carries `supersedes`, the
   entry must list it in `moved_values` with both readings and a reason. A moved value that is not
   listed fails.
6. `run_provenance_changed` is checked the same way, against the set of contributing run ids, so an
   edition that added or dropped a run artefact without moving a printed number still flags it.
7. `{{ver:...}}` markers are the only way version facts reach the text. A typed version string in prose
   fails, which is L1 class A4 read from the other direction.

**Failure messages.**

```
L10 version-history-complete: no entry for the version being built (8.4).
  Latest entry: 8.3 (2026-08-25).
  Fix: add the entry before building. A report that cannot say what changed since the
  edition a reader cited is not a report they can keep using. (D7)
```

```
L10 version-history-complete: chain walk from the null-predecessor entry (1.0) visits
  6 of 8 entries. Unreachable: 7.1, 7.2. Entry 8.0 names previous_version 6.4.
  Fix: repair previous_version so the chain covers every edition. (D7)
```

```
L10 version-history-complete: entry 8.4 declares measured_values_moved=false but
  2 rendered values differ from the 8.3 build: power_sustained_mean (566.4 -> 561.2),
  roof_fraction_low_precision (82 -> 79).
  Fix: set the flag and list both readings with a reason. A silently revised number
  breaks everyone who cited the old one. (D7)
```

**Allowlist.** One hatch: `history_starts_at`, for a report whose earlier editions genuinely predate
the template and cannot be reconstructed. It takes a version and a reason, both printed, and the chain
walk then starts there. Legitimate use: migrating an existing report into the template, once. Not
legitimate: dropping the two editions that are inconvenient to describe, which is D7 exactly.

---

# L11. Gates measured, not argued

**Statement.** A claim about a gate must rest on readings, not on reasoning. If the workload claims
cache defeat, counter readings must exist. If a quality gate is claimed, its cases must be published.

**Why it exists.** D12: a quality gate was reported as "10 of 10, PASS" with its test cases unpublished,
so the claim was unfalsifiable. A reader could neither reproduce it nor object to it, which makes it a
claim about the author rather than about the machine. D9's contradiction had the same root on the cache
side: sections disagreed about whether a cache was active because nobody had read the counters and put
them in one place, so each section reasoned its way to an answer. An argument that a cache was defeated
by construction is a claim; the cache's own counters are evidence.

**What the linter checks.**

1. Where authored text claims that repeated work was not served from a cache, or that inputs were
   unique, `workload.uniqueness_and_cache_defeat.cache_counters` must be present, with `source`,
   `resolved_state` and `interpretation`. `resolved_state: off` is a perfectly good reading, and it is a
   different statement from "on but defeated"; conflating the two is what produced the contradiction.
2. Where counters exist, the report renders them, and every sentence about cache behaviour is generated
   from `interpretation` rather than written per section.
3. Any value with kind `fixed-test-set` publishes `cases[]` in full, in the bundle and in the report,
   with each case's input verbatim, its acceptance criterion, its observed result and its pass flag.
4. A pass count is rendered only alongside the case count and the published cases. "10 of 10" with the
   cases elsewhere or absent fails.
5. `licenses` and `does_not_license` are both present and both rendered next to the result. The gap
   between them is where an overstated quality claim would otherwise live.
6. Where the gate claims repeat stability, `repeats` and `stable_across_repeats` are present per case.
   A gate run once cannot make a stability claim.
7. Where a threshold is described as pre set, `preregistered_bar.set_before_measurement` must be true
   and `set_on` must precede the run's timestamp. A residual declared acceptable after it was seen is a
   rationalisation, and the linter says so.
8. Where `size_control.verification` is claimed, there must be one entry per size actually used, with
   the system's own counter, not the harness's belief. One headline ratio generalised across sizes fails.

**Failure messages.**

```
L11 gates-measured-not-argued: value "quality_gate_result" has kind fixed-test-set and
  renders "10 of 10" but cases[] is empty.
  Fix: publish every case with its input, acceptance criterion and observed result. A
  gate whose cases are secret is unfalsifiable, and an unfalsifiable pass is worth less
  than a published failure. (D12)
```

```
L11 gates-measured-not-argued: section "workload" claims cache defeat, but
  uniqueness_and_cache_defeat.cache_counters is absent.
  Fix: read the counters and record source, resolved_state and interpretation. "Unique
  by construction" is an argument; a counter reading is evidence. If the cache resolved
  to off, say that: it is a different statement, and stating which one it is prevents two
  sections from reasoning to different answers. (D9, D12)
```

```
L11 gates-measured-not-argued: preregistered_bar on "attribution_residual" has
  set_before_measurement=false and is described as a pre-set bar in section "decode".
  Fix: describe it as a post-hoc observation, or record the date it was actually fixed.
```

**Allowlist.** One hatch, `cases_withheld`, for a case set that genuinely cannot be published, for
example one containing third party or personal data. It requires: a reason, a count, a hash of the
withheld set so a later reader can confirm it did not change, a description of how a reader could
construct an equivalent set, and it forces the result to render as "not independently checkable" at
every appearance. Legitimate use: real corpus material under a licence that forbids redistribution.
Not legitimate: cases that are embarrassing, unfinished, or would take an afternoon to write up. The
withheld path is deliberately more work than publishing, because publishing is what the rule wants.

---

# Why a linter and not a review checklist

The obvious objection to all of this is that a careful reviewer with a checklist would have caught
these twelve defects, and a checklist costs nothing to write. The report is the counterexample. It had
three rounds of external review by people who were looking for exactly this, and the twelve defects
survived. They survived for a reason that generalises.

**Every one of the twelve was locally plausible.** That is the whole finding. Nothing about "80%" looks
wrong in a sentence. It looks like a rounded, sensible, slightly conservative restatement of a real
measurement, which is what it was, until the matrices were size controlled and the real measurement
became 82%. Nothing about "570 W" looks wrong beside a 575 W part. Nothing about "every one of the 201
busy samples" looks wrong until you know the machine had two units and the data had 401 samples.
Nothing about "see section 18" looks wrong; it looks like a cross reference. A reviewer reading a
sentence judges whether the sentence is plausible, and all twelve sentences were.

Catching them requires the opposite of reading: it requires **leaving** the sentence, finding the value
elsewhere in a fifty page document, comparing at the right precision, checking that the conditions
match, and coming back. For every number. There are several hundred numbers. No reviewer does that,
and a checklist that asks them to is a checklist that gets signed rather than executed. The three
review rounds are the evidence: the reviewers were competent and motivated and they still missed all
twelve, which means the process was at fault, not the people.

Four further properties of the defects put them outside what review can reach.

* **They were created by editing, not by writing.** D1, D3 and D4 all appeared when something upstream
  changed: a run was replaced, the matrices were size controlled, an aggregation scope shifted. The
  prose was correct on the day it was typed. Review catches wrong statements; it does not catch
  statements that will become wrong. Only a mechanical link between the sentence and the value catches
  that, and the link has to be re-checked on every build, which is what a linter is.
* **They were invisible in the diff.** Reordering sections does not touch the paragraph containing "see
  section 18". Adding a run artefact does not touch the cover sentence claiming one run directory. The
  edits that broke D6 and D10 were nowhere near the text they broke, so no diff review could have shown
  them.
* **Some were absences.** D7's missing versions, D8's missing label, D12's missing cases, D9's missing
  publication field. A reviewer sees what is on the page. Absence is the hardest thing for a reader to
  notice and the easiest thing for a schema to require.
* **They were self consistent locally and contradictory globally.** D9's two sentences were each fine
  where they sat; they contradicted a section thirty pages away. Human review is local by construction,
  because reading is local. Contradiction detection over a whole document is a machine's job.

The economics settle it. A linter costs a fixed amount once and then runs on every build for free, in
seconds, at full coverage, without fatigue, and without being the last thing before a deadline. Review
costs a person's full attention every time, degrades with document length, and is exactly the resource
that is scarcest when the edition is late, which is when these defects are introduced. The right
division of labour is that the linter owns everything mechanical, so that human review can spend all
of its scarce attention on the thing no rule can check: whether the conclusions follow from the
numbers.

One consequence is worth stating plainly, because it is the usual reason a rule like L1 gets deleted.
**The rules will be most annoying exactly when they are most valuable**, which is during a hurried
edit close to publication. That is the moment every one of the twelve defects was created. A rule that
can be switched off under deadline pressure is a rule that is off whenever it matters, which is why the
escape hatches here are per occurrence, reasoned, capped, and printed in the report rather than global
and silent.

---

# Coverage: every historical defect maps to a rule

| Defect | Primary rule | Also caught by |
|---|---|---|
| D1 three values for one rate | L1 | L6 (table cell literals), L2 |
| D2 cap counted at two scopes | L1 (scope collision) | L8 (aggregation compatibility) |
| D3 figure from an older run | L1 | L7 (run_id resolution) |
| D4 cross condition comparison as like for like | L8 | L7 (comparability fingerprint), L1 |
| D5 unreproducible derivation | L3 | L6 (figure needs a table), L2 |
| D6 stale section numbers | L5 | |
| D7 incomplete version history | L10 | |
| D8 assumption presented as measured | L2, L4 | L1 (the shares as literals) |
| D9 contradictory publication claims | L9 | L11 (counter readings) |
| D10 run provenance understated | L7 | L1 (the run count as a literal) |
| D11 hardcoded machine constants | L3 (constants must be inputs) | L2 |
| D12 unfalsifiable quality gate | L11 | L9 |

Every rule traces to at least one defect, and every defect is covered by at least one rule. A future
maintainer proposing to remove a rule should first identify which row of this table they are choosing
to reopen.
