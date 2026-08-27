# Benchmark report template

**Template version 1.0.0** (see [section 8, Versioning](#8-versioning-the-template-versions-itself-the-report-versions-itself)).

This directory is the reusable part of a benchmark report: the contract its numbers must satisfy, the
list of sections it must have, and the gate that refuses to let it out of the door. It contains no
report, no renderer, and no vocabulary belonging to any one kind of machine.

---

## 1. What this is, and what it is not

### What it is

Three specification artifacts, two pieces of machinery, and the tests that keep them honest.

| File | Lines | Role |
|---|---|---|
| `run-schema.json` | 1632 | **The data contract.** What a run bundle must contain, and where every number is allowed to live. |
| `report-outline.yaml` | 1323 | **The section manifest.** 29 required and optional sections, 7 repeatable archetypes, 160 invariants on rendered output. |
| `lint-rules.md` | 848 | **The gate, in prose.** Eleven rules, each citing the historical defect it prevents, with its failure messages and its escape hatch. |
| `lint.py` | 4813 | The gate, executable. `python -m template.lint <run-dir> <built-report>`. |
| `outline.py` | 326 | A dependency-free reader for the small YAML subset the manifest uses. |
| `tests/test_lint.py` | 960 | 87 tests, including a mutation check that every rule's test is actually carried by its rule. |
| `tests/fixtures/` | 7 dirs | One clean bundle, one bundle with the authored text withheld, and five deliberately defective bundles. Every rule is mapped to the fixture it must fire on (`RULE_FIXTURES`, `tests/test_lint.py:48`). |

Standard library only, by contract. No PyYAML, no jsonschema, no pytest. A report build must not be
able to fail, and a rule must not be able to go unchecked, because a third-party parser moved.

### What it is not

**It is not a renderer.** Nothing here turns a bundle into HTML. It defines what the renderer must
emit (`data-value-id` and friends, listed in `lint.py`'s module docstring) and then checks that it
did. `article/build.py` in this repo is one renderer; the template is deliberately ignorant of it.

**It is not a style guide.** It has nothing to say about typography, colour, or how a sentence should
read. Every one of the 160 invariants is a decidable predicate over rendered output plus the bundle:
it returns pass or fail with a location, never an opinion.

**It is not a description of a machine.** Constraint B-C2: the template says *roofs*, not TFLOPS;
*workload*, not prompts; *unit* or *device*, not GPU; *quality gate*, not accuracy gate; *problem
size*, not matrix dimension. It must fit a processor, a storage array, or a network fabric without
editing. Domain vocabulary belongs in a report's own bundle and config. If an invariant needs domain
words to be checkable, the invariant is wrong.

**It does not make the report shorter or easier.** Constraint B-C1 runs the other way: if a report
needs to say something the template cannot express, extend the template. Never soften the sentence
and never delete the rule to let the sentence through. The pressure to get one more paragraph out of
the door is the exact force that produced every defect on the list below.

### Why it exists

The report it was derived from went through three rounds of external review. Twelve defects were
found. All twelve were the same failure mode: **a number was written into prose by hand instead of
being generated from the run data, and then drifted from the value it was supposed to restate.**

| Ref | The defect | Rule that now catches it |
|---|---|---|
| D1 | One rate printed as 82% in a table, 80% in prose, 83% in a recommendation | L1 |
| D2 | A cap given as "every one of the 201 busy samples" in one section and "399 of 401" in another | L1 (scope collision) |
| D3 | Sustained figure of 570 in prose against 566 in the data | L1, L7 |
| D4 | 238.4 set beside 237.6, where 237.6 was measured at a different problem size in a different run | L8 |
| D5 | Interconnect ceilings unreproducible from the published curve, derived by a script beside the report | L3 |
| D6 | Stale hard-coded cross-references ("see section 18") after sections were reordered | L5 |
| D7 | A version history missing two of its own versions, listed out of order | L10 |
| D8 | A workload size mixture (35/30/20/10/5%) presented as if measured; it was assumed | L2, L4 |
| D9 | "Harness and raw data published" in one section against "nobody outside has run it" in another | L9 |
| D10 | A cover claiming a single run directory while three run artefacts contributed | L7 |
| D11 | A decomposition with one machine's geometry hardcoded, so elsewhere it answered confidently and wrongly | L3 |
| D12 | A quality gate reported as "10 of 10, PASS" with its cases unpublished, hence unfalsifiable | L11 |

Three rounds of competent, motivated review did not catch these, because every one of them was
locally plausible. Nothing about "80%" looks wrong in a sentence. Catching it requires leaving the
sentence, finding the value elsewhere in a fifty-page document, comparing at the right precision,
checking the conditions match, and coming back, for every one of several hundred numbers. No reviewer
does that. `lint-rules.md` argues this at length under "Why a linter and not a review checklist".

---

## 2. Quick start

The template is reachable from the tool's own command line. This is the surface to use:

```
gpubench template init <dir>        scaffold a report that builds and passes the claims gate
gpubench template lint <run-dir> <report.html>   the eleven rules, over a built report
gpubench template outline           the canonical sections, their invariants, their anti-patterns
gpubench template schema            the run-bundle contract, printed or enforced (--validate FILE)
```

`gpubench template init` writes a content module, a synthetic run artefact and a README, and the
result builds at exit 0 with the gate armed:

```
$ gpubench template init /tmp/demo
$ gpubench article /tmp/demo/content.py /tmp/demo/run --out-dir /tmp/demo/out
claims gate: manifest verified: 4 claim(s), 2 prose block(s), 1 figure(s), ...
$ echo $?
0
```

The run artefact it writes is marked `"sample": true`, and the generated `claims()` reads that
mark: while it is there the claims are declared `supplied` with a source, and the moment the file
is a real run they are declared `measured` with a run id. The kind follows the artefact.

Everything below still works as a module invocation. Every command was run from
`c:/Users/PC/projects/onprem-gpu-bench`, the directory that *contains* the `template` package. Run
them from there, not from inside `template/`.

Read the rules and see which sections stand between the report and each defect:

```
$ python -m template.lint --explain --rules L3 | head -9
The eleven rules, and the defect each one exists to prevent.

The governing rule they implement: every number a reader sees is either measured and
labelled, or a stated derivation from labelled inputs, and it exists exactly once in the
bundle. A second copy is a copy that will drift.

L3 derived-is-rebuildable  (cites D5, D11)
  statement: Every derived value recomputes from its declared inputs, by its printed formula, to within its stated precision, and every input is printed somewhere in the report.
  why:       Two interconnect ceilings could not be reproduced from the curve printed two pages earlier: the derivation interpolated latency log-linearly where latency is linear in message size, and it lived in a script beside the report, so there was nothing to review and nothing to test. D11 is the same rule from the other side: a decomposition carried one machine's geometry as hardcoded constants and produced a confident wrong answer elsewhere.
```

Check the manifest parses and count what it holds:

```
$ python -m template.outline
template version: 1.0.0
sections: 29
archetypes: 7
invariants: 160
```

Gate a report against its bundle:

```
$ python -m template.lint template/tests/fixtures/clean template/tests/fixtures/clean/report.html
...
L1   no-orphan-literals         PASS       errors 0    not-checked 0
...
0 error(s), 0 not-checked
$ echo $?
0
```

Run the tests:

```
$ python -m unittest discover -s template/tests -t .
Ran 87 tests in 0.981s

OK
```

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Every selected rule passed and nothing was left unchecked. |
| 1 | Nothing failed, but at least one part of a rule could not be checked. `--allow-skipped` makes this 0. |
| 2 | At least one rule failed. A failing build produces no report. |
| 3 | The linter could not run at all: no bundle, or an unreadable report. |

Exit 1 is deliberate and is worth understanding before you reach for `--allow-skipped`. A linter that
passes because it could not find the authored text is making a claim that nothing is wrong without
having looked, which is the defect class it exists to prevent. Every rule reports what it did not
check, by name:

```
$ python -m template.lint template/tests/fixtures/no_authored template/tests/fixtures/no_authored/report.html
lint: authored text NOT SUPPLIED
...
L8 comparison-hygiene [skipped]
  where: comparative language in authored text
  what:  NOT CHECKED: no authored text was supplied, so comparisons were detected only in the rendered output. A comparison the renderer reworded may have been missed.
  fix:   write <run-dir>/authored.json.
...
L1   no-orphan-literals         INCOMPLETE errors 0    not-checked 1
L5   cross-references-resolve   INCOMPLETE errors 0    not-checked 1
L8   comparison-hygiene         INCOMPLETE errors 0    not-checked 1

0 error(s), 3 not-checked
$ echo $?
1
```

There is no bypass flag. The escape hatches are per rule, per occurrence, declared in the bundle or
the allowlist, and printed in the report, so using one is a visible act rather than a private one.

---

## 3. Pointing it at a new run directory

### What a run directory is

`<run-dir>` is a directory holding the bundle and, optionally, three companion files:

```
<run-dir>/
  bundle.json            REQUIRED. The run bundle. Must satisfy run-schema.json.
                         Also accepted: report-bundle.json, run-bundle.json, or the only
                         *.json in the directory carrying both schema_version and runs.
  authored.json          {"<section_id>": "text with {{markers}} still in place"}
                         Optional, but its absence makes L1, L5 and L8 report NOT CHECKED,
                         because the marker vocabulary only exists before rendering.
                         A directory authored/<section_id>.md (or .txt) works too.
  lint-allowlist.json    {"patterns": [{"id","regex","context","why"}], "waiver_budget": 5}
                         Optional. Additive only: it can never turn a rule down, only name
                         one more shape of genuinely free-standing digit. Every entry needs
                         a reason. The whole list is printed in the audit appendix.
  previous-bundle.json   The previous edition's bundle, for L10's value diff. Or pass
                         --previous-bundle <path>.
```

Then:

```
python -m template.lint <run-dir> <built-report.html> \
    --previous-bundle <run-dir>/previous-bundle.json \
    --version <the edition being built>
```

### A directory of probe output is not a run directory

This is the one thing that surprises everyone. The raw output of a harness is not a bundle:

```
$ ls results/20260825-160142-final/
embed_bench.json  engine_log.json  gpu_micro.json  inventory.json  inventory.txt
nccl_allreduce.json  raw/  roofline.json  serve_bench.json  serve_bench_decode.json
serve_bench_prefill.json

$ python -m template.lint results/20260825-160142-final article/rtx5090-dual-gpu-benchmark-v8.3.html
lint: cannot run: no run bundle in results/20260825-160142-final. Expected one of bundle.json,
report-bundle.json, run-bundle.json, or a single *.json carrying schema_version and runs. A
directory of raw probe output is not a bundle: build one that validates against run-schema.json
first.
$ echo $?
3
```

Probe output records what an instrument saw. A bundle records what the *report* is going to claim,
which is a different and larger thing: it also carries the run provenance, the conditions under which
each value is comparable, the derivations with their formulas, the assumptions with their rationales,
the figures with their table views, and the version chain. Nothing can infer those from a directory
of readings, which is why the adapter that writes them is the report's job and not the template's
(see [known gap G4](#g4-no-adapter-from-probe-output-to-a-bundle)).

### The adoption path for an existing report

Four steps, in this order, because each one makes the next one checkable.

1. **Write the adapter.** One function per probe output, emitting envelopes into
   `measurements[]`, `roofs[]`, `derived[]` and `assumptions[]`. Put it in the *tool*, beside the
   probes, not in a script next to the report. That placement is not tidiness: D5 happened because a
   derivation lived beside the report, where nothing reviewed it and nothing tested it.
2. **Declare the runs honestly.** Every artefact that contributes any value gets a `runs[]` entry,
   exactly one is `primary: true`, and anything that mixes runs is declared in `cross_run_blends[]`
   with a `why_permissible`. This is the whole of D10, and it is cheap to get right on day one and
   expensive later.
3. **Teach the renderer to leave marks.** Every generated number goes inside a
   `<span data-value-id="...">`; every section gets `<section data-section="...">`; every
   cross-reference becomes `<a data-section-ref="...">`; every figure's table view becomes
   `<table data-figure="...">`. The full list is in the `lint.py` module docstring. Text inside a
   mark is generated and therefore exempt from the orphan-literal scan; text outside a mark is
   authored and may not contain digits that restate a value. **That asymmetry is the whole linter.**
4. **Move the prose into `authored.json` with markers.** Until you do, L1, L5 and L8 report
   NOT CHECKED and you exit 1, which is the honest answer.

The current edition in this repo has not been through step 3:

```
$ grep -c "data-value-id" article/rtx5090-dual-gpu-benchmark-v8.3.html
0
```

So it cannot be linted today. That is real work, not a switch, and pretending otherwise would be a
claim made without looking.

---

## 4. The value-envelope convention, and why bare scalars are forbidden

This is the section to read if you read only one. Everything else in the template is downstream of
it, and a reader who is not convinced here will work around the whole thing in an afternoon.

### The convention

Every number in the system is an **envelope**. Not a float, not a formatted string, not an entry in a
dictionary of stats: an object that knows what it is, what produced it, which run it came from, under
what conditions, and how much it moved.

```json
{
  "id": "rate_primary",
  "label": "Primary sustained rate",
  "description": "the rate the workload held for the whole sustained window",
  "value": 238.4,
  "unit": "units/s",
  "precision": 1,
  "kind": "measured",
  "source": "engine request counter over the sustained window",
  "run_id": "run_primary",
  "conditions": {"problem_size": 4096, "concurrency": 8, "duration_class": "sustained"},
  "n": 300,
  "spread": {"type": "cov", "value": 0.004, "scope": "within-run", "n_runs": 1}
}
```

Six fields are mandatory on every envelope, with no exceptions: `id`, `value`, `unit`, `kind`,
`source`, `run_id`. Prose and tables and figures then reference it **by id** and never spell out its
digits:

```
"The workload held {{v:rate_primary}} for the whole sustained window."
```

which renders as

```html
The workload held <span data-value-id="rate_primary">238.4 units/s</span> for the whole sustained window.
```

The value exists once. Every appearance is the same appearance.

### Why bare scalars are forbidden

The weak version of the argument is that two copies of one number will drift apart. That is true, and
it is D1 and D3, and it is the argument everyone expects. It is also not the important one, because a
careful author can believe they will keep two copies in step.

The important version is this: **six of the twelve defects were not two numbers disagreeing. They
were one number with a missing attribute.** A bare scalar has nowhere to put the attribute, so the
defect is not something the author failed to check. It is something the data model made unsayable.

Work through the six (D2, D4, D8, D5, D11, D12), and then D3, which is the hybrid case.

**D2 is a scope that could not be written down.** One section said "every one of the 201 busy
samples". Another said "399 of 401". Neither sentence is wrong. The machine had two units; 201 is one
unit's sample count and 401 is the total. Two correct numbers, at two scopes, and a reader cannot
tell which they are holding, because a scalar `201` does not record what it counted over. The
envelope field is `aggregation`:

```json
"aggregation": {
  "over": "units", "method": "count", "unit_count": 2,
  "per_unit": [{"unit_index": 0, "value": 201, "n": 201},
               {"unit_index": 1, "value": 200, "n": 200}]
}
```

Now the aggregate and the per-unit values are one object, the section that wants one unit takes it
from `per_unit` rather than re-deriving it, and L1 raises a **scope collision** when a literal
matches a per-unit projection while the surrounding sentence quantifies the aggregate. There is no
version of this fix that works on a float.

**D4 is a comparison a formatter cannot refuse.** 238.4 was set beside 237.6 as though the pair meant
something. Both were correctly measured. They were measured at different problem sizes, in different
runs. The defect is not in either number, it is in the *pairing*, and a number that does not carry
its conditions cannot object to being paired. The envelope field is `conditions`, and L8 compares the
comparability keys of both sides before it will allow a like-for-like comparison. A cross-condition
comparison is still permitted, but only through `{{xcmp:a,b|why=...}}`, which forces the differing
keys onto the page. This is the crux of the argument against the most common workaround: keeping the
numbers in a dictionary and printing them through a formatter still gives you a value that cannot say
"do not compare me with that one".

**D3 is the hybrid, and it is worth seeing why.** Prose said 570 where the data said 566, which looks
like a plain second copy that drifted, and L1 does catch it as one. But the reason it drifted is that
the prose had transcribed an *older run*, and a bare `570.0` cannot say which run it came from, so
nothing on the page could distinguish "this is out of date" from "this is a different quantity".
`run_id` is therefore mandatory on every envelope, with no exemption for published or assumed values
(they take the id of the run whose report they appear in), so that a value carried over from an older
run arrives carrying the older run's id and fails L7 rather than blending in.

**D8 is a kind that could not be marked.** A traffic mixture of 35/30/20/10/5% sat in a table beside
measured throughput, in the same typeface, unmarked. It was assumed. `kind` is a **closed** enum,
closed on purpose, because an open vocabulary lets an assumption be labelled something that sounds
measured: `measured`, `derived`, `assumption`, `projection`, `supplied`, `published`, `enumerated`,
`fixed-test-set`. An `assumption` additionally requires a `rationale` saying why it was chosen and
what would change if it were wrong. An assumption that has to argue for itself in print cannot pass
as an observation.

**D5 is a derivation that could not be inspected.** Two ceilings could not be reproduced from the
curve printed two pages earlier, because the derivation interpolated latency log-linearly where
latency is linear in message size, and it lived in a script beside the report. A `derived` envelope
requires `inputs` (ids, not values), `formula` (executable with a calculator against those ids) and
`conditions`, and it should carry `computed_by`, which says where the code lives and is the field
that distinguishes a derivation inside the tested harness from one in a script nobody reviews. The
bug here was never a wrong formula. It was an unpublished one. A wrong formula in the open gets
caught on first reading.

**D11 is a fallback that could not be forbidden.** A decomposition carried one machine's geometry as
hardcoded defaults, so on a different machine it produced a confident wrong answer instead of an
error. The envelope field is `requires_inputs_present`, default `true`: the derivation must fail when
an input is missing. A `false` there needs a rationale.

**D12 is evidence that could not be attached.** A gate reported "10 of 10, PASS" with its cases
unpublished. A `fixed-test-set` envelope carries `cases[]` with each case's input, acceptance
criterion, observed output and result, plus `licenses` and `does_not_license`. L11 refuses a gate
result whose cases are not published, because an unfalsifiable pass is not a measurement.

And the shared-mode case, which is not on the numbered list but is the same shape: a roof measured
while other work was resident is a **floor**, so every percentage against it is an upper bound on
achievement rather than the achievement. `is_floor` on the roof and `inherits_floor` on everything
derived from it are the only mechanism by which that fact can travel with the number instead of being
remembered by whoever ran it.

### The tally

| Envelope field | Defect it makes impossible |
|---|---|
| `precision` (one stored rounding rule) | D1 |
| `aggregation.over` / `unit_count` / `per_unit` | D2 |
| `run_id` (mandatory, no exceptions) | D3, D10 |
| `conditions` | D4 |
| `inputs` / `formula` (+ `computed_by`, recommended) | D5 |
| `kind` (closed enum) + `rationale` | D8 |
| `requires_inputs_present` | D11 |
| `cases[]` / `licenses` / `does_not_license` | D12 |
| `is_floor` / `inherits_floor` | shared-mode floors read as ceilings |

Every row is an attribute a bare scalar cannot carry. The first, `precision`, is the one a
disciplined author believes they can hold in their head, and D1 is what happened when they tried. The
other eight are not a question of discipline at all: there is no way to write them on a float. That
is the argument. Bare scalars are not forbidden because envelopes are more elegant. They are
forbidden because **most of the defect list is unsayable in a float**, so no amount of care applied
to floats would have prevented it.

### The workarounds, and what each one gives up

* *"I will keep a dict of numbers and format them consistently."* You get D1 and D3. You still cannot
  express scope (D2), refuse a comparison (D4), fail on a missing input (D11), or attach cases (D12).
  A dict of scalars is a bare-scalar store with a nicer accessor.
* *"I will put the number in a variable and use an f-string."* This is the exact mechanism behind the
  defect list. The variable was right; the sentence was written from it once, and then the variable
  moved. Nothing else in this template matters if this happens, because L1 only works if the digits
  are not in the authored text to begin with.
* *"Just this once, it is a small number."* Every one of the twelve was a small number. The waiver
  exists for exactly this and it costs one line: `{{lit:NUMBER|why=...}}`. It is capped at five per
  build (`L1 waiver-budget-exceeded` above that, on the argument that a document needing six
  exceptions has a modelling problem rather than a lint problem), and every waiver is printed in the
  report's audit appendix with its reason. The rule for using one legitimately: the number must be a
  real quantity that genuinely cannot have an envelope, and the reason must say why it cannot. A
  waiver must never restate a value that exists. That is the defect, spelled with extra syntax.

### Digits that really are free-standing

The template is not pretending that no number may ever be typed. Ten allowlist entries (A1 to A9,
with A6b) cover the genuinely free-standing cases: calendar years, this document's own part counts
(inside the document control block only), numbers inside a citation, version identifiers, quantities
the prose itself defines, list and step ordinals, renderer-assigned heading numbers, digits embedded
in identifiers like a format name or a port, constants inside a printed formula, and trivial
cardinals not adjacent to a unit. Each is a regex **plus a context restriction**, so widening the
list widens it by one named shape rather than by degree:

```
$ python -m template.lint --explain | sed -n '/L1 allowlist/,/A2/p'
L1 allowlist (explicit patterns with a context restriction, never a tolerance):
  A1   any                      ^(19|20)\d{2}$
       A calendar year is not a measurement of the system under test. No value denotes it, so it cannot drift from one.
  A2   document_control_count   ^\d{1,3}$
```

Not allowlisted, because each one was a real defect or one step from one: percentages of any kind,
sample counts, unit counts, sizes, rates, durations, temperatures, power figures, price figures,
counts of passing cases, and any number within one line of a unit symbol.

---

## 5. Adding a new measurement type

Three tiers, in increasing order of how much you have to touch. Try tier 1 first: most things a
report wants to say are already sayable.

### Tier 1: a new value, in a shape the schema already has

Nothing to extend. Append an envelope to `measurements[]`, `roofs[]`, `derived[]` or
`assumptions[]`, per its `kind`. The three value shapes are:

**Scalar** (`value_scalar`): a number, string or boolean. Strings are for genuinely non-numeric facts
such as a link generation, an engine count as a label, or a verdict.

**Range across a condition** (`value_range`), requiring `min`, `max` and `across`. Use it when the
value genuinely is a range over something rather than a point, with `min_at` / `max_at` naming where
each end sits:

```json
"value": {"min": 8.1, "max": 12.4, "across": {"unit_index": "both units"},
          "min_at": 1, "max_at": 0}
```

**Series** (`value_series`), requiring `points`. A curve or a grid: one value per condition point, in
one envelope, with `varies_over`, an `interpolation` policy and a `monotonic` claim:

```json
"value": {"varies_over": ["problem_size"],
          "points": [{"at": {"problem_size": 1024}, "value": 210.0, "n": 100},
                     {"at": {"problem_size": 2048}, "value": 226.0, "n": 100}],
          "interpolation": "none", "monotonic": "increasing"}
```

L1 projects every point of a series, both ends of a range, `n`, `aggregation.unit_count`, every
`per_unit` entry, and `spread.value`, so all of them are protected from being restated in prose.

Which container to use:

| Container | For | Extra required fields, on top of the six |
|---|---|---|
| `roofs[]` | a ceiling the workload is measured against | `roof_class`, `is_floor` |
| `measurements[]` | values obtained rather than computed | by `kind`: `measured` needs `conditions` and `n`; `published` needs `provenance`; `supplied` needs `supplied_by`; `enumerated` needs `why_not_measured`; `fixed-test-set` needs `cases`, `licenses`, `does_not_license` |
| `derived[]` | values computed from other values | `inputs`, `formula`, `conditions`. `computed_by` is not schema-required and should be there anyway: it is the field that says whether the derivation lives in the tested harness or in a script beside the report (D5). |
| `assumptions[]` | values chosen rather than measured, plus projections past the measured envelope | `rationale`; a `projection` additionally needs `inputs`, `formula` and `conditions`, because a projection with no inputs is a guess wearing an arithmetic costume |

Three more conditionals catch themselves rather than needing to be remembered: any numeric `value`
or `value_range` requires `precision` (D1, D3); any `n` above 1 requires `spread` or
`spread_not_available` with a reason; and `requires_inputs_present: false` requires a `rationale`
(D11).

Ids must be unique across `runs[]`, `roofs[]`, `measurements[]`, `derived[]`, `assumptions[]` and
`figures[]` together, in `lower_snake_case`.

### Tier 2: a new class of ceiling, or a new condition axis

`roof_class` is an enum with nine members: `compute`, `bandwidth`, `interconnect`, `capacity`,
`power`, `thermal`, `latency`, `io`, `other`. Reach for `other` once; if you reach for it twice, add
the member. It is domain-neutral vocabulary, so a new member must be too. "Interconnect" is a
template word; a bus name is not.

`conditions` is an open object with a named core (`problem_size`, `work_unit_count`, `concurrency`,
`batch_size`, `precision`, `mode`, `duration_class`, `unit_index`, `parallelism`, `service_level`,
`percentile`, `power_state`, `workload_id`). Adding a key is legal and needs no schema edit. Do add
one when a real axis exists, because L8 compares comparability keys before allowing a like-for-like
pairing, and an axis that is not in `conditions` is an axis L8 cannot see. That is D4 reopening.

### Tier 3: a genuinely new measurement type

Two doors, and picking the right one matters.

**Domain-specific extras go under `x_`.** The bundle's `patternProperties` carries anything prefixed
`x_` through untouched, and the template's own logic never reads it. Use this for facts that belong to
your domain and that no rule should ever depend on. Nothing under `x_` may be *required* by the
template, because the template must stay free of any one domain's vocabulary (B-C2). Corollary: a
number that lives only under `x_` is not projected by L1 and therefore is not protected from being
restated in prose. Do not park real measurements there.

**A new attribute of every value goes in the envelope, plus a rule that reads it.** This is the
B-C1 door and it is the one the template has already used four times. `lint.py` reads four envelope
fields that the schema did not name, carried through by its open-object policy:
`rebuild_tolerance` and `interpolation_basis` (L3), `mode_override_reason` (L7), and
`from_distribution` (L4). Each exists because a rule needed to express something the schema had not
yet named. **None of them relaxes a rule**, and that is the test to apply to any new field you are
tempted to add. Ask: does this field let a number be printed with *less* attached to it than before?
If yes, you are weakening the gate to let a sentence through, and B-C1 says extend instead.

The full order of work for tier 3:

1. Add the field to `run-schema.json` with a `description` that names the defect it prevents. Every
   description in that file does; it is how a maintainer three years from now knows why the field is
   not optional.
2. Add or extend a rule in `lint.py`, register it in `RULES` and `RULE_ORDER`, and give it a
   `RULE_META` entry whose `cites` names at least one defect. A test enforces this:
   `test_rule_registry_matches_the_documented_rule_set` asserts the three structures agree, that
   there are eleven rules, and that every one cites a defect containing "D".
3. Write the rule up in `lint-rules.md` with its statement, its rationale, its failure messages and
   its escape hatch, and add a row to the coverage table at the end of that file.
4. Map the rule in `RULE_FIXTURES` to a bundle under `tests/fixtures/` that actually violates it,
   extending `mixed_defects` or adding a new fixture directory if none of them does.
   `test_every_rule_fires_on_its_own_fixture` proves the rule catches it;
   `test_every_rule_test_is_load_bearing` stubs the rule out and proves the fixture stops failing,
   which is what stops a test from passing for the wrong reason.
5. Add an invariant to the owning section in `report-outline.yaml` with its own `id`, `rule`, `check`
   and `cites`. `test_every_declared_invariant_carries_a_check_and_a_defect` requires all three.

---

## 6. Extending the section manifest without breaking the linter

### What the manifest holds

```yaml
template:            name, version, released, previous_version, the four governing rules
                     (versioning, ordering, numbering, extension), and the schema contract
                     (schema_contract, schema_contract_version, refuse_unknown_schema_major)
vocabulary:          the neutral terms, and the banned term classes, for THIS file
check_vocabulary:    24 named predicates the invariants are written in
universal_invariants: U-1 to U-11, applying to every section and every archetype instance
sections:            29 entries, each with id, title, required, order, purpose, inputs,
                     invariants[] and an anti_pattern
archetypes:          7 repeatable section shapes, instantiated by insert_after
coverage_map:        one row per section of the source edition, as B-C1 evidence
defect_index:        which sections and invariants stand between the report and each defect
```

The 29 sections run `document-control`, `abstract`, `headline`, `what-this-is`, `figures-of-merit`,
`system-under-test`, `software-stack`, `workload`, `metrics`, `metric-origins`, `provenance`,
`assumptions`, `strategy`, `roofs`, `datasheet-vs-measured`, `workload-vs-roofs`, `reproducibility`,
`quality-gate`, `cost`, `findings`, `recommendations`, `open`, `presentation-guidance`, `standing`,
`harness-walkthrough`, `method-primer`, `limitations`, `reproducing`, `version-history`. The seven
archetypes are `regime-analysis`, `attribution-breakdown`, `capacity-envelope`, `sensitivity-sweep`,
`co-resident-service`, `principal-finding`, `metric-self-assessment`.

### The four rules of extension

**1. Prefer an archetype instance to a new section.** If the report needs another evidence section of
a shape that already exists (another regime analysis, another attribution breakdown, another
sensitivity sweep), instantiate the archetype. Instances declare their own `id`, `title`, `purpose`,
`inputs` and `uses`, inherit every universal invariant plus the archetype's own, and are placed with
`insert_after: <existing-section-id>`. Adding an evidence section never means adding prose outside
the contract.

**2. Never write a section number anywhere.** Not in the manifest, not in the bundle, not in authored
text. Cross-references are made by section id and resolved by the renderer to whatever number the
section ends up with after optional sections are dropped. That is D6, and it is the reason `order` in
the manifest is a *canonical position*, not a printed number.

**3. Every new entry needs at least one invariant, and every invariant needs a `check` and a
`cites`.** A section with a purpose and no invariant is a table-of-contents entry, and a manifest of
those would have prevented none of the twelve defects. The `check` must be written in the predicate
vocabulary from `check_vocabulary`; add a predicate there if you need one, with a definition that is
decidable. The `cites` field names the defect, or the standing rule (`shared-mode rule`,
`recommendation inversion`), the invariant guards.

**4. Stay inside the YAML subset.** `outline.py` deliberately supports only mappings, sequences,
scalars, comments and quoted strings. Anchors and aliases, block scalars (`|`, `>`), flow collections
(`{}`, `[]`), multiple documents (`---`), tags, and tabs for indentation all raise `OutlineError`
rather than being silently mis-parsed. That refusal is the point: a mis-parsed manifest would make a
check pass because it did not look, which is the defect class the whole template exists to prevent.
Long invariant text goes in a double-quoted scalar on one line, which is why the file has some very
long lines.

### What actually breaks when you extend it

Be clear about the blast radius, because it is smaller and stranger than it looks.

**The eleven rules do not read the manifest at all.** Only `--explain` opens
`report-outline.yaml` (`lint.py:4673`), and it catches any exception and prints "could not be read".
So adding a section cannot make a rule fail, and it cannot make a rule pass either. See
[known gap G3](#g3-the-160-invariants-are-a-specification-not-an-implementation): the manifest's 160
invariants are a specification that the eleven rules implement a subset of, from the value side.

**Three assertions in the test suite are the real ratchet.** `OutlineReaderTests`
(`tests/test_lint.py:880`) hardcodes the counts:

```python
self.assertEqual(29, len(doc["sections"]))
self.assertEqual(7, len(doc["archetypes"]["items"]))
self.assertEqual(160, len(outline_reader.invariants(doc)))
```

Adding a section or an invariant fails that test. This is intended, and it is the reason to run the
tests after editing the manifest. Update the three counts in the same commit as the manifest edit,
and bump `template.version` and `template.previous_version` in the manifest and `TEMPLATE_VERSION` in
`template/__init__.py`. A silent change to the discipline is a change nobody reviewed.

**The manifest is also its own lint target.** `vocabulary.banned_in_template` applies to this file and
to every invariant string in it: no device family, no vendor product, no numeric format, no domain
throughput unit, no domain workload noun, no domain word for answer quality. That check is not
implemented yet (see [G6](#g6-no-vocabulary-substitution-surface)), so for now it is on the author.
When you add an invariant, read it back and ask whether it would still make sense for a storage
array.

**Add a `coverage_map` row when you add a section.** That block is the B-C1 evidence: one row per
section of the source edition, describing what the section *does* rather than quoting its title,
because the manifest may not carry a report's domain vocabulary and may not identify a section by
number. If a new section has no row, nothing records what it was for.

---

## 7. Worked example: one new metric, end to end

A real sequence, run against a scratch copy of the shipped clean fixture. Every command and every
line of output below is verbatim.

### Setup

```
$ cd c:/Users/PC/projects/onprem-gpu-bench
$ cp -r template/tests/fixtures/clean c:/tmp/worked-example
```

PowerShell equivalent: `Copy-Item -Recurse template/tests/fixtures/clean c:/tmp/worked-example`.

The metric to add: a 99th-percentile service time, measured over the same sustained window as the
headline rate. Domain-neutral on purpose. It could be a response time, a seek time, or a settling
time.

### Step 1: the schema entry

Append one envelope to `measurements[]` in `c:/tmp/worked-example/bundle.json`:

```json
{
  "id": "service_time_p99",
  "label": "99th-percentile service time",
  "description": "the service time the slowest one per cent of requests exceeded, over the same sustained window as the headline rate",
  "value": 412.0,
  "unit": "ms",
  "precision": 1,
  "kind": "measured",
  "source": "harness per-request timing log, one entry per request over the sustained window",
  "run_id": "run_primary",
  "conditions": {"problem_size": 4096, "concurrency": 8, "duration_class": "sustained"},
  "n": 30000,
  "spread": {"type": "iqr", "value": 38.0, "scope": "within-run", "n_runs": 1}
}
```

Every mandatory field is there: `id`, `value`, `unit`, `kind`, `source`, `run_id`. `conditions`
carries the same three keys as `rate_primary`, which is what will later let L8 accept a comparison
between the two. `n` and `spread` are what make the figure arguable: 412 ms over 30000 requests with
an interquartile range of 38 ms is a claim; 412 ms alone is a number.

### Step 2: the provenance row

Two edits, both about making the new value *findable* rather than merely present.

In `runs[]`, add it to the primary run's `produced` list, which is the run register's own account of
what came out of that window:

```json
"produced": ["roof compute", "rate primary", "rate sweep", "cap busy samples",
             "gate result", "roof fraction", "service time p99"]
```

In `sections[]`, add the id to the declaring section's `uses`, which is what lets the universal
invariant `rendered_ids_subset_of_declared` hold: a section may not print a number it did not declare
it uses.

```json
{"id": "results", "title": "What the work reached", "order_hint": 2,
 "uses": ["rate_primary", "roof_compute", "roof_fraction", "cap_busy_samples",
          "rate_sweep", "service_time_p99"],
 "cross_references": ["workload"]}
```

### Step 3: the prose, written the way it comes naturally, and why the gate stops it

Add a sentence to `authored.json` under `results`, and the corresponding paragraph to `report.html`:

```json
"results": "... The slowest one per cent of requests waited more than 412 ms."
```

```html
<p>The slowest one per cent of requests waited more than 412 ms.</p>
```

This is the natural way to write it, it is correct today, and it is exactly D1 and D3 in miniature.
Run the gate:

```
$ python -m template.lint c:/tmp/worked-example c:/tmp/worked-example/report.html --version 1.1
lint: bundle c:/tmp/worked-example\bundle.json
lint: report c:/tmp/worked-example/report.html
lint: authored text c:/tmp/worked-example\authored.json
lint: rules L1,L2,L3,L4,L5,L6,L7,L8,L9,L10,L11

L1 no-orphan-literals [error]
  where: section 'results', authored offset 530
  what:  literal 412 collides with value 'service_time_p99' (412 ms, precision 1, kind measured, run run_primary) via the value. |412 - 412| = 0 (0.00%), drift band 5.00%.
  fix:   write {{v:service_time_p99}} so every appearance moves together. If the literal is genuinely a different quantity, give it its own envelope and interpolate that; if it is free-standing, add an allowlist pattern with a reason to lint-allowlist.json.

L1 no-orphan-literals [error]
  where: section 'results', prose offset 568
  what:  literal 412 collides with value 'service_time_p99' (412 ms, precision 1, kind measured, run run_primary) via the value. |412 - 412| = 0 (0.00%), drift band 5.00%.
  fix:   write {{v:service_time_p99}} so every appearance moves together. If the literal is genuinely a different quantity, give it its own envelope and interpolate that; if it is free-standing, add an allowlist pattern with a reason to lint-allowlist.json.

L7 run-provenance [error]
  where: run 'run_primary'
  what:  produced[] claims 'service time p99', which matches no rendered value.
  fix:   correct produced[] or render the values it names. (D10)

------------------------------------------------------------------------------
L1   no-orphan-literals         FAIL       errors 2    not-checked 0
L2   every-value-has-a-kind     PASS       errors 0    not-checked 0
L3   derived-is-rebuildable     PASS       errors 0    not-checked 0
L4   assumptions-stay-labelled  PASS       errors 0    not-checked 0
L5   cross-references-resolve   PASS       errors 0    not-checked 0
L6   figures-carry-tables       PASS       errors 0    not-checked 0
L7   run-provenance             FAIL       errors 1    not-checked 0
L8   comparison-hygiene         PASS       errors 0    not-checked 0
L9   claims-need-evidence       PASS       errors 0    not-checked 0
L10  version-history-complete   PASS       errors 0    not-checked 0
L11  gates-measured-not-argued  PASS       errors 0    not-checked 0

allowlisted literals (printed in the report's audit appendix):
  1            A6b
  2            A6b
  3            A6b
  4            A6b
  5            A6b
  6            A6b
  one          A9

3 error(s), 0 not-checked
$ echo $?
2
```

Three errors, and the third one is the interesting one. L1 fires twice, once on the authored text and
once on the rendered output, because the digits are outside any provenance mark. L7 fires because
`produced[]` now claims a value the report never actually renders: the run register would have been
promising evidence that is not on the page. Note also what the linter says about itself at the
bottom: `one` in "one per cent" was exempted under allowlist entry A9 (trivial cardinals not adjacent
to a unit), and the exemption is printed rather than assumed.

### Step 4: the prose interpolation

Replace the digits with the marker in `authored.json`, and the paragraph with a marked span in the
rendered output:

```json
"results": "... The slowest one per cent of requests waited more than {{v:service_time_p99}}."
```

```html
<p>The slowest one per cent of requests waited more than
   <span data-value-id="service_time_p99">412.0 ms</span>.</p>
```

### Step 5: the lint pass

```
$ python -m template.lint c:/tmp/worked-example c:/tmp/worked-example/report.html --version 1.1
lint: bundle c:/tmp/worked-example\bundle.json
lint: report c:/tmp/worked-example/report.html
lint: authored text c:/tmp/worked-example\authored.json
lint: rules L1,L2,L3,L4,L5,L6,L7,L8,L9,L10,L11

------------------------------------------------------------------------------
L1   no-orphan-literals         PASS       errors 0    not-checked 0
L2   every-value-has-a-kind     PASS       errors 0    not-checked 0
L3   derived-is-rebuildable     PASS       errors 0    not-checked 0
L4   assumptions-stay-labelled  PASS       errors 0    not-checked 0
L5   cross-references-resolve   PASS       errors 0    not-checked 0
L6   figures-carry-tables       PASS       errors 0    not-checked 0
L7   run-provenance             PASS       errors 0    not-checked 0
L8   comparison-hygiene         PASS       errors 0    not-checked 0
L9   claims-need-evidence       PASS       errors 0    not-checked 0
L10  version-history-complete   PASS       errors 0    not-checked 0
L11  gates-measured-not-argued  PASS       errors 0    not-checked 0

allowlisted literals (printed in the report's audit appendix):
  1            A6b
  2            A6b
  3            A6b
  4            A6b
  5            A6b
  6            A6b
  one          A9

0 error(s), 0 not-checked
$ echo $?
0
```

Two edits to the bundle, one marker in the prose, and the number now exists once. When the run is
repeated and the value moves to 415.0, the sentence does not need finding, reading or editing, and it
cannot be left behind. That is the entire return on the convention, and it is why the schema entry
costs twelve lines instead of one.

### What this example does not prove

Step 5 exits 0, and there is one thing it did not check. If you now move the bundle value to 415.0
and do *not* re-render, the gate still passes: see
[known gap G2](#g2-the-digits-inside-a-provenance-mark-are-never-checked-against-the-envelope), with
the reproduction. The gate proves the prose has no second copy of the number. It does not prove the
renderer copied the first one correctly.

---

## 8. Versioning: the template versions itself, the report versions itself

**Template version: 1.0.0**, released 2026-08-26, `previous_version: null`. It lives in two places
that must agree: `template.version` in `report-outline.yaml`, and `TEMPLATE_VERSION` in
`template/__init__.py`.

The template is versioned **independently** of any report built with it. Not aligned, not derived
from, not bumped alongside. Two separate chains, and a build should print both.

The reason is that they measure different things:

* **A report edition advances when its numbers or its prose change.** That chain lives in the
  bundle, at `version_history[]`, and each entry names the edition it follows, so the chain is
  walkable and a missing edition breaks the walk instead of passing unnoticed. That is D7.
* **The template advances when the discipline changes**: a section added, an invariant tightened, a
  rule added, a field made mandatory.

Coupling them would break both directions.

Bind the template's version to the report's and a report that fixes a typo would appear to have
changed the rules it was gated by, which destroys the only useful question you can ask of a template
version: *was this edition held to the same standard as that one?* The answer has to be readable off
two numbers, and it is only readable if they move for different reasons.

Bind them the other way and it is worse: tightening an invariant would force a version bump on every
report built from the template, including the ones nobody has rebuilt. A report's version has to mean
"this is what the numbers said on this date". It cannot also mean "this is which edition of the
discipline was in force", because those two facts change on different days for different reasons.

There is a third reason, specific to this template. The whole argument of section 4 is that one fact
must exist in exactly one place. A template version that is computed from, or asserted to equal, a
report version is a second copy of one of them. The template would be committing, in its own metadata,
the defect it exists to prevent. (It is currently committing a smaller version of that: two unlinked
copies of `1.0.0`, with no test tying them together. See [G8](#g8-the-templates-own-version-is-two-unlinked-copies).)

The schema contract is versioned separately again: `template.schema_contract_version` is `1.x` and
`refuse_unknown_schema_major` is set. A generator must refuse a bundle whose schema major version it
does not know, rather than reading the fields it recognises and ignoring the rest.

### What to bump, when

| You changed | Bump |
|---|---|
| A section, an invariant, an archetype, a predicate | `template.version` minor, and `TEMPLATE_VERSION`, and the three counts in `OutlineReaderTests` |
| A rule in `lint.py`, or a required schema field | `template.version` minor or major, and write it up in `lint-rules.md` |
| A report's numbers or prose | `version_history[]` in that report's bundle. The template does not move. |
| A required schema field removed or retyped | `schema_version` major, and every bundle must be migrated |

---

## 9. Known gaps

The template exists to stop unfalsifiable claims, so a claim of completeness here would be the defect
it exists to prevent. Everything below was verified by running it.

### G1. No schema validator: CLOSED, with one finding left open

`schema.py` now validates against `run-schema.json` with the standard library only, and
`gpubench template schema --validate FILE` runs it. It implements the subset the schema uses and
**refuses to return a verdict over a schema whose keywords it cannot all enforce**, because a
partial pass reads exactly like a real one. `test_the_shipped_schema_is_fully_enforced` asserts
that `unsupported_keywords(run-schema.json)` is empty, so adding a keyword to the schema fails the
suite until the validator grows.

What that immediately surfaced is the defect this section already predicted, and it is still open:
every shipped fixture bundle declares `schema_version: "1.0.0"` while the schema's own pattern is
`^[0-9]+\.[0-9]+$`, so all seven fixtures fail validation on that one field. Nothing was edited to
make it go away. One of the two is wrong and a maintainer has to decide which:

```
$ gpubench template schema --validate template/tests/fixtures/clean/bundle.json
$.schema_version
  '1.0.0' does not match the pattern ^[0-9]+\.[0-9]+$
1 violation(s)
$ echo $?
2
```

`lint.py` itself is unchanged and still checks only that the twelve required top-level keys are
present (`BUNDLE_REQUIRED_KEYS`, `lint.py:509`); schema conformance is a separate command rather
than a twelfth rule. The original proof of the gap, which still reproduces:

```
$ python -c "import json,re; d=json.load(open('template/run-schema.json',encoding='utf-8')); \
  b=json.load(open('template/tests/fixtures/clean/bundle.json',encoding='utf-8')); \
  p=d['properties']['schema_version']['pattern']; \
  print(p, b['schema_version'], bool(re.match(p,b['schema_version'])))"
^[0-9]+\.[0-9]+$ 1.0.0 False
```

The clean fixture's `schema_version` violates its own contract and every rule passes. Closing this
means either a stdlib subset validator inside `lint.py` or an external validator as a separate
required build step, and either way the standard-library constraint has to be argued about first.

### G2. The digits inside a provenance mark are never checked against the envelope

The linter's asymmetry is that text inside a `data-value-id` span is generated and therefore trusted.
It is trusted completely: nothing compares the characters in the span to the value in the bundle. Only
the *number of decimal places* is checked, and only for cells inside a figure's `table_view`
(`lint.py:3079`). So a stale render passes clean. Continuing from section 7 step 5, with the bundle
at 412.0 and the report rendered from it:

```
$ python -c "import json; p='c:/tmp/worked-example/bundle.json'; b=json.load(open(p,encoding='utf-8')); \
  [m.__setitem__('value',415.0) for m in b['measurements'] if m['id']=='service_time_p99']; \
  json.dump(b,open(p,'w',encoding='utf-8'),indent=2)"
$ python -m template.lint c:/tmp/worked-example c:/tmp/worked-example/report.html
...
0 error(s), 0 not-checked
```

The bundle says 415.0, the report says 412.0, and the gate is satisfied. The gate proves the prose
holds no second copy of a number; it does not prove the renderer transcribed the first copy
correctly. Closing this needs a rule that re-renders every marked span from the bundle at its own
precision and diffs, which is the single highest-value addition available.

### G3. The 160 invariants are a specification, not an implementation

`report-outline.yaml` states 160 invariants in a 24-predicate vocabulary. **The eleven rules never
open the file.** The only reader is `--explain` (`lint.py:4673`). So section-level invariants such as
DC-1 ("the run register renders exactly one row per `runs[]` entry") are enforced by review and by
the renderer's good behaviour, not by the gate. The eleven rules attack the same twelve defects from
the value side and overlap the manifest heavily, but per-section coverage is not machine-checked, and
the `check` strings are not executable. Reading the two artifacts as one enforced system would
overstate what runs.

### G4. No adapter from probe output to a bundle

The template defines the bundle and refuses anything that is not one. It ships nothing that turns
`results/<run>/*.json` into `bundle.json`, and it cannot: the mapping is domain-specific by nature,
which is what B-C2 requires. The consequence is that the highest-effort, highest-risk part of
adopting the template is the part with the least support, and the thing that will be written in a
hurry is the thing D5 warns about. The mitigation available today is placement: put the adapter in
the tool, beside the probes, under test.

### G5. A marker that never gets rendered passes

If `authored.json` carries `{{v:some_id}}` and the rendered output has no corresponding span, nothing
fails. Verified: deleting the marked paragraph from step 4's report while leaving the marker in the
authored text gives `0 error(s), 0 not-checked` on all eleven rules. `Context.rendered_ids()` counts
authored `{{v:}}` markers as rendered ids, which is why L7's `produced[]` check is satisfied by the
marker alone. There is no rule asserting that every marker resolves to exactly one rendered mark, and
a dropped sentence is a silent loss of a value the run register still promises.

### G6. No vocabulary substitution surface

The manifest says a report "supplies its own words through its config, and the renderer substitutes
them", and no such config exists. The bundle schema has no term map: `presentation` holds only
`rounding`, `thousands_separator`, `percent_precision` and `range_separator`. Per-value `label` is
the only place a report's own words attach to anything. So B-C2 is honoured by discipline in the
template and by nothing at all in the pipeline, and the companion check
(`vocabulary.banned_in_template`, which is meant to fail a build when a domain word appears in the
manifest) is specified and unimplemented.

### G7. The allowlist audit appendix is promised, not enforced

Every allowlist entry and every waiver is documented as being "printed in the report's audit
appendix", and the linter dutifully prints the hits to stdout. No rule checks that the *report*
contains such an appendix. The clean fixture has none, reports six allowlisted literals, and passes.
Until that is closed, the visibility that justifies the escape hatches depends on the renderer
choosing to provide it.

### G8. The template's own version is two unlinked copies

`TEMPLATE_VERSION = "1.0.0"` in `template/__init__.py:22` and `template.version: "1.0.0"` in
`report-outline.yaml` are two copies of one fact, and no test compares them. There is also no field
anywhere in `run-schema.json` for the template version that gated a build, so a published report
cannot state which edition of the discipline it was held to, which is precisely the question section
8 says independent versioning exists to make answerable. Both are small and both are the template
failing its own standard.

### G9. Coverage is one report deep

The manifest, the schema and the rules were derived from a single report about a single class of
machine. `coverage_map` is the B-C1 evidence that nothing in that edition needed prose outside the
contract, and it is evidence about one edition. The neutral vocabulary is an argument that the
template *would* fit a processor, a storage array or a network fabric; it is not a demonstration.
The first report in another domain will find things it cannot say. When it does, the constraint is
unchanged: extend the artifact, never weaken the report.

---

## 10. Tests

From the gpubench repo root, beside the other six suites:

```
$ python -m tests.test_template
Ran 118 tests in 1.8s

OK
```

That entry point collects `test_lint.py` and `test_cli.py` and refuses to report a result if
either module collects fewer tests than its floor, because a suite that quietly runs nothing
passes. From the directory that contains the `template` package, the older invocation still works:

```
$ python -m unittest discover -s template/tests -t .
```

Verbosely, one test name per line: `python -m unittest template.tests.test_lint -v`. One class at a
time: `python -m unittest template.tests.test_lint.L1OrphanLiteralTests -v` (8 tests).

`test_cli.py` covers the subcommand. Its load-bearing test is the sequence the scaffold exists to
satisfy: init into a temp directory, build with the real `gpubench article`, and read the exit
code, the document and the manifest. Beside it sits the negative control that makes it mean
something: break the derived total in the generated module and assert the build is BLOCKED and
writes nothing. Without the control, "the scaffold passes" would also be true of a gate that
passes everything.

Two of the test classes are load-bearing in a way worth knowing about.

`RuleCoverageTests` enforces that the rule set cannot rot. `test_every_rule_fires_on_its_own_fixture`
runs each rule against a fixture built to violate it. `test_every_rule_test_is_load_bearing` then
stubs each rule out to a function returning no findings and asserts the fixture *stops* failing,
which is what stops a test from passing for a reason other than the rule it names.
`test_every_rule_has_a_dedicated_test` fails if a rule has no `test_lN_*` method, on the argument
that a rule without a test is a rule that can be deleted silently.

`HonestyTests` enforces the exit-1 discipline: a missing input must produce a NOT CHECKED finding
naming what was not looked at and how to enable the check, never silence, and a skipped check must
not exit 0 unless `--allow-skipped` was passed.

The fixtures are named for what they reproduce: `clean`, `d1_three_values`,
`d5_unreproducible_derivation`, `d7_version_chain`, `d10_undeclared_run`, `mixed_defects`,
`no_authored`. Four of them reproduce a single named defect; `mixed_defects` carries several at once
and is the fixture seven of the eleven rules currently fire on. Adding a rule means mapping it in
`RULE_FIXTURES` to a bundle that actually violates it, and writing a new fixture if none of the
existing bundles does. The fixture is the part a future maintainer will read first, so make it
reproduce the defect and nothing else.

---

## 11. File map

```
template/
  README.md               this file
  run-schema.json         the data contract (1632 lines)
  report-outline.yaml     the section manifest (1323 lines)
  lint-rules.md           the eleven rules, in prose, with their defect citations (848 lines)
  lint.py                 the executable gate (4813 lines)
  outline.py              the YAML-subset reader (326 lines)
  schema.py               the stdlib JSON Schema subset validator, and its refusal to guess
  scaffold.py             `gpubench template init`: the generated content module and sample run
  cli.py                  the four subcommands, wired into gpubench/cli.py
  __init__.py             package docstring and TEMPLATE_VERSION
  tests/
    __init__.py
    test_lint.py          one test per rule, plus the outline reader (970 lines)
    test_cli.py           the subcommand, including init -> build -> gate as one sequence
    fixtures/
      clean/                        bundle + authored + previous-bundle + report, passes everything
      d1_three_values/              one quantity printed three ways
      d5_unreproducible_derivation/ a derivation that does not rebuild
      d7_version_chain/             a broken version chain
      d10_undeclared_run/           a contributing run that is not declared
      mixed_defects/                several at once
      no_authored/                  clean, but with the authored text withheld
```

## 12. Related files outside the template

These are the report and tool the template was derived from. The template does not read them, and
nothing in this directory should ever import from them.

| Path | What it is |
|---|---|
| `article/build.py` | The current renderer. Has not yet been taught to emit provenance marks. |
| `article/rtx5090-dual-gpu-benchmark-v8.3.html` | The current edition. Zero `data-value-id` marks, so not yet lintable. |
| `results/20260825-160142-final/*.json` | Raw probe output. Not a bundle; see section 3. |
| `../gpubench/gpubench/` | The harness: probes, `analysis.py`, `report.py`. The right home for an adapter and for every derivation (D5). |
