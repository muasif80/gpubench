# The check catalogue

Every check the pre-render gate enforces, what it reads, what it catches, how to satisfy it, and
what it cannot see.

This document describes two files and nothing else:

| File | What it contributes |
|---|---|
| `gpubench/verify.py` | 29 check ids (`manifest`, A1 to A9, B1, C1, C2, D1 to D7, E1, E2, F1 to F4, G1 to G3) |
| `gpubench/longform/__init__.py` | 1 check id (A10, the declaration floor), plus the gate driver that runs all 30 |

**Total: 30 check ids.** The count comes from grepping the two files for the id passed to
`Findings.error` / `Findings.warn` (verify.py) and to the local `error()` helper
(longform/__init__.py). The cross-check at the end of this document lists both directions.

Audience: an engineer who wants to emit a manifest this gate accepts, and who has never seen the
tool. Everything stated here was read out of the code or reproduced by running it.

Two conventions for quoted output. **Finding text inside a fenced block was copied verbatim** from
a run, and the command that produced it is named. Message text quoted inline, in backticks and
usually with a placeholder id such as `X`, is the code's own format string with the id filled in,
so the wording is exact and the id is illustrative.

## What this is not

`gpubench/template/lint.py` is a **different** checking engine with its own rule ids (L1 to L11),
its own data contract (`gpubench/template/run-schema.json`), and its own eight-member `kind` enum.
It lints a *run bundle plus a rendered report*. It is documented in
`gpubench/template/lint-rules.md`. Nothing in this catalogue applies to it, and none of its L rules
appear here. The two systems share vocabulary (`kind`, `value`, `unit`) and are not
interchangeable. See "The spelling and vocabulary traps" below.

## How the gate runs

Standalone, over a manifest that already exists:

```bash
gpubench verify claims.json
gpubench verify claims.json --rendered report.html
gpubench verify claims.json --rendered report.html --previous claims-prev.json
gpubench verify claims.json --findings findings.json --warnings-as-errors
gpubench verify --demo          # a fixture full of real defects, changes nothing
```

Exit codes from `verify.main`: `0` clean or warnings only, `1` one or more errors (or any warning
under `--warnings-as-errors`), `2` the manifest file could not be read or parsed as JSON.

Inside a report build, where it matters, the gate runs before anything is written:

```bash
gpubench article article/build.py results/<run-dir>
```

`gpubench/longform/run_claims_gate` calls `verify.verify()`, appends A10, writes
`<MANIFEST>` and `<MANIFEST-stem>-findings.json`, and returns one of four statuses:
`absent` (the content module declares no `MANIFEST` + `claims()`), `incomplete` (it declares
exactly one half of that pair), `pass`, `blocked`. In the CLI only `pass` publishes. `absent`
returns 1 unless `--allow-ungated` is given, `incomplete` returns 2, and `blocked` returns 1
unless `--no-verify` is given, in which case the document is written but stamped
`DRAFT, NOT FOR PUBLICATION` with the greppable HTML comment marker
`gpubench-draft-not-for-publication`.

Two differences between the two entry points matter:

1. **A10 does not run under `gpubench verify`.** `verify.verify()` never calls
   `check_declaration_floor`; only `run_claims_gate` appends it (longform/__init__.py, the line
   `findings.items.extend(check_declaration_floor(manifest, previous))`). A standalone verify with
   `--previous` runs A4's changelog checks but not the floor.
2. **The rendered-document checks need a document.** Under `gpubench verify` they run only with
   `--rendered`. Under `gpubench article` the gate always supplies one, as a temporary file that is
   deleted before the call returns. When no document is supplied, the coverage line says so
   verbatim: `document coverage: no rendered document was supplied, so no numeral was checked`.

The console prints at most **25 findings per check** (`MAX_PER_CHECK`), then one summary line
saying how many more of that check there were. The findings JSON always holds every one, so read
`--findings` output rather than the console when a check fires in bulk.

## How to read an entry

Each entry has five fields.

- **SEVERITY** is `error`, `warn`, or both, because several ids emit both. Errors block. Warnings
  do not, unless `--warnings-as-errors`. The entry also says whether the warning can be waived
  through `m["accepted_warnings"]`. **Errors are never waivable, whatever the manifest says**
  (`apply_accepted_warnings` skips any finding whose severity is not `warn`).
- **JURISDICTION** is the one field to read first: does the check read the **MANIFEST** (a
  declaration the generator wrote), the **RENDERED DOCUMENT** (what a reader will actually see), or
  an **EXTERNAL RESULT ARTEFACT** (a file on disk the manifest points at)? Every one of the 23
  holes two adversarial audits found in this gate was a check that read a declaration where it
  should have read an artifact. See "Why the gate reads the artifact".
- **WHAT IT CATCHES**, with a concrete defect.
- **HOW TO SATISFY IT**, from the content module's point of view.
- **WHAT IT CANNOT SEE**, honestly. Every check has a blind spot, and planning around a gate
  without knowing its blind spots is worse than having no gate.

## Summary

Jurisdiction: **M** manifest, **D** rendered document, **A** external result artefact,
**P** previous edition's manifest.

| Id | Name | Severity | Jur. | Waivable |
|---|---|---|---|---|
| `manifest` | Schema, claim shape, acceptance shape | error, warn | M | the stale-acceptance warning only, and it cannot match itself |
| A1 | Declared equal quantities agree | error | M | no |
| A2 | No bare numerals in declared prose | error | M | no |
| A3 | Declared prose comparisons are true | error | M | no |
| A4 | Provenance: one run per table, changes logged | error | M, P | no |
| A5 | Unit-bearing numerals in the document trace to a claim | error, warn | D | the per-numeral warnings, by `numeral` + `unit`; the summary warning carries no fields |
| A6 | Bare numerals in the document trace to a claim | error | D | no (see entry) |
| A7 | One quantity, one value, whatever it is called | error, warn | M | the near-label warning, by `keys` |
| A8 | A measurement names a run that exists | error, warn | M | the out-of-window warning, by `claim` + `run` |
| A9 | `kind` is earned, not chosen | error | M | no |
| A10 | An edition may not declare less than the last | error | M, P | no |
| B1 | Nothing derived is ever typed | error | M | no |
| C1 | Basis hygiene | error, warn | M | the mixed-basis-ratio warning, by `claim` |
| C2 | Unit hygiene | error, warn | M | the same-family warning, by `claim` |
| D1 | A percentile discloses its sample size | error | M | no |
| D2 | A percentile is not an extreme in disguise | warn | M | yes, by `claim` |
| D3 | Closed-loop wave arithmetic | error, warn | M | the two warnings, by `level` |
| D4 | An arrival model is declared | error | M | no |
| D5 | A sustained figure states its duration | error, warn | M | the steady-state warning, by `claim` |
| D6 | An open-loop level discloses rates and backlog | error | M | no |
| D7 | The arrival note does not contradict the model | error | M | no |
| E1 | A ceiling declares its measurement mode | error | M | no |
| E2 | A roof from a vendor headline | warn | M | yes, by `claim` |
| F1 | No HTML entity surfaces as visible text | error | M, D | no |
| F2 | Every declared figure has a table view | error | M | no |
| F3 | The declared table view actually rendered | error, warn | D | the free-text warning, by `figure` |
| F4 | Citations resolve; table numerals trace to that table's cells | error, warn | M, D | the stray-numeral warning, by `table`; the missing-table warning carries no fields |
| G1 | A quality gate is recorded and passed | error | M | no |
| G2 | The gate's cases are published | error | M | no |
| G3 | The gate result reads back out of the artefact | error, warn | A | the two warnings, by nothing narrower than `check` |

Acceptance is matched on `check` plus any of these fields present in the acceptance entry:
`claim`, `claims`, `keys`, `block`, `table`, `figure`, `level`, `run`, `numeral`, `unit`,
`quantity`, `label` (`ACCEPTANCE_FIELDS` in verify.py). A finding that carries none of them can
only be accepted by check id, which accepts every warning of that check, so prefer checks whose
findings carry a narrow field.

---

# The checks

## `manifest` : schema, claim shape, acceptance shape

**SEVERITY** error, except one warn (a stale acceptance). The warn is technically waivable and
practically not: `apply_accepted_warnings` appends the stale-acceptance warning *after* its
matching pass, so an acceptance for check `manifest` can never match it and will itself be
reported stale.

**JURISDICTION** MANIFEST.

**WHAT IT CATCHES** five things, all in `check_manifest_shape` and `apply_accepted_warnings`:

1. `schema` is not the string `claims/1`: `schema must be 'claims/1', found None`.
2. A claim with no `value` key at all: `claim X has no value`.
3. A claim whose `kind` is not one of the six the schema allows:
   `claim rt_cores has no valid kind`. Real reproduction, with `kind: "enumerated"` copied from
   the template linter's eight-member enum, which this schema does not share.
4. `accepted_warnings` is not a list, or an entry names no `check`, or an entry records no
   non-empty `why`: `accepted_warnings[0] (check D2) records no reason. Accepting a warning
   silently is deleting it.`
5. An acceptance that matched no warning this run:
   `accepted_warnings entry for check D2 matches no warning in this build. A stale acceptance is a
   claim about the report that is no longer true; delete it.` (warn)

**HOW TO SATISFY IT** set `schema` to `claims/1`; give every claim a `value` and a `kind` from
`measured | derived | assumption | projection | supplied | published`; give every
`accepted_warnings` entry a `check` and a `why`; delete acceptances that no longer match.

**WHAT IT CANNOT SEE** whether `value` is a number. `"value" not in c` is the whole test, so
`{"value": "fast"}` passes the shape check and then crashes A1 or A3 with
`ValueError: could not convert string to float: 'fast'` if either reads it. It also cannot see a
missing `unit`, `basis`, `label`, or `quantity`: all four are optional everywhere. And it cannot
see a manifest with no `claims` key at all: shape reports nothing, and `check_derivations` then
raises `KeyError: 'claims'`, which exits 1 with a traceback rather than the documented exit 2.

## A1 : declared equal quantities agree

**SEVERITY** error. Not waivable.

**JURISDICTION** MANIFEST (`equalities`, `claims`).

**WHAT IT CATCHES** the same quantity printed twice with two values, where the author declared the
two claims equal. Real finding text, from `gpubench verify --demo`:

```
  [ERROR] A1  the same quantity is printed with different values: throughput_c8_capacity=233, throughput_c8_repro=204.5 (spread 12.23%, tolerance 0.50%)
```

It also errors when a group names a claim that does not exist:
`equality group references unknown claims ['foo']`.

**HOW TO SATISFY IT** declare each group as `{"keys": [...], "tolerance": 0.005}` or as a bare
list of keys, and keep the spread `(max - min) / |max|` inside the tolerance. **The default
tolerance is 0.002** (0.2%), used when the group is a bare list or omits `tolerance`. Two printings
of one quantity that legitimately differ (an engine-counted and a nominal-basis ceiling, for
instance) need a stated tolerance, not a note.

**WHAT IT CANNOT SEE** any pair the author did not declare. That gap is A7's job. It also cannot
see a group of one key (the spread of one value is zero), and a `dict` group with no `keys` field
raises `KeyError: 'keys'` rather than reporting a defect.

## A2 : no bare numerals in declared prose

**SEVERITY** error. Not waivable.

**JURISDICTION** MANIFEST (`prose[].text`). **This is a declaration, and it was the hole.** See
A5 for the document-side check that has jurisdiction over what ships.

**WHAT IT CATCHES** a number typed into a sentence rather than cited from a claim, which goes
stale silently the next time the table is re-measured. Real finding text from the demo:

```
  [ERROR] A2  prose sec24_recommendation contains the bare numeral '1.91'. Numbers in prose go stale silently when a table is re-measured; cite the claim key instead, or list it under allow_literals if it is genuinely not a measurement.
```

**HOW TO SATISFY IT** three routes.

- Cite the claim: `{{claim_key}}`. Placeholders are stripped before the scan, so a cited value is
  never a bare numeral.
- List genuine non-measurements in the block's `allow_literals`.
- Write the numeral after a structural word. `STRUCTURAL_CONTEXT` exempts a numeral immediately
  preceded by `section`, `sections`, `figure`, `fig`, `fig.`, `table`, `appendix`, `chapter`,
  `step`, `version`, `v`, `item`, `note`. Four-digit years matching `19xx` or `20xx` are exempt
  unconditionally.

**The `allow_literals` trap, reproduced.** The comparison is `token in {str(x) for x in
allow_literals}`, where `token` is the numeral exactly as printed, thousands separators included:

| Prose | `allow_literals` | Result |
|---|---|---|
| `1,240 tokens per second` | `[1240]` | **error**, `'1,240'` does not equal `'1240'` |
| `1,240 tokens per second` | `["1,240"]` | clean |
| `It ran 16 requests.` | `[16]` | clean |
| `It ran 16.0 requests.` | `[16]` | **error**, `'16.0'` does not equal `'16'` |

Write the literal exactly as it is printed.

**WHAT IT CANNOT SEE** every sentence that is not in a declared prose block. This is the omission
attack, and it landed: five fabricated headline figures were injected into a report's abstract, not
one of them appeared in any declared block, and the build shipped with exit 0 and a byte-identical
`claims.json`. A2's jurisdiction was 974 characters while the document shipped 104,549. It also
cannot see a stale number in a heading, a table of contents entry, or a `title=` tooltip, for the
same reason.

## A3 : declared prose comparisons are true

**SEVERITY** error. Not waivable.

**JURISDICTION** MANIFEST (`prose[].assert`).

**WHAT IT CATCHES** a sentence that states a relationship the numbers contradict. Real finding text
from the demo:

```
  [ERROR] A3  prose sec17_closure asserts pool_residual_per_seq is greater than pool_parts_total, but pool_residual_per_seq=7.8 and pool_parts_total=92
```

Also: `prose X declares unknown comparison 'greater'` and `prose X compares unknown claims`.

**HOW TO SATISFY IT** attach `{"op": ..., "left": <key>, "right": <key>}` to the block. The eight
operators, from the `ops` table in `check_prose`:

| `op` | Meaning | Extra fields |
|---|---|---|
| `gt`, `lt`, `gte`, `lte` | ordinary comparison | none |
| `eq` | `math.isclose(rel_tol=1e-9)` | none |
| `approx` | `math.isclose` | `rel_tol`, **default 0.02** |
| `within_pct` | `abs(l - r) / abs(r) <= pct/100` | `pct`, **required** |
| `ratio_between` | `min <= l/r <= max` | `min` and `max`, **both required** |

`within_pct` without `pct` raises `KeyError: 'pct'`, and `ratio_between` without `min` raises
`KeyError: 'min'`. Both are reproduced. Supply them.

An assertion also triggers C1: comparing two claims whose `basis` differs (and neither is `scalar`
or `ratio`) is an error, `prose X compares per_device against total without conversion`.

**WHAT IT CANNOT SEE** whether the assertion has anything to do with the sentence. A block whose
text says "throughput fell" and whose assert says `gt` passes if the numbers happen to satisfy
`gt`. And nothing forces a block to carry an assertion at all.

**The failure mode to avoid.** A generator that filters its assertions down to the ones whose
operands still exist deletes each guard along with the claim it guarded. Keep the assertion and let
it fail: A3 reports `prose X compares unknown claims`, which is the finding you want.

## A4 : provenance, one run per table, changes logged

**SEVERITY** error. Not waivable.

**JURISDICTION** MANIFEST (`tables`, `claims`) and, for the last two conditions, the PREVIOUS
edition's manifest, which only reaches the check when `--previous` is passed or the article gate
found a baseline.

**WHAT IT CATCHES** four things.

1. A table whose measured cells come from more than one run without saying so. Real demo text:
   `table capacity_sweep blends runs ['primary', 'tool'] without declaring it. A blended table is
   legitimate and has to say which rows came from where.`
2. `blended: true` with no `blend_note`: `table X is blended but names no source for each row`.
3. A measured claim with no `run` at all (raised inside `check_measured_run`):
   `measured claim X names no run`.
4. With a previous edition: values that moved with no changelog row
   (`N value(s) changed since the previous edition with no changelog row: ...`), and a changelog
   row claiming a re-measurement whose `measured_at` did not move
   (`changelog 8.6 claims X was re-measured, but its measurement time did not move`).

**HOW TO SATISFY IT** put every measured claim's run id in `run`; declare `blended: true` plus a
`blend_note` on any table that spans runs; and record every value change in
`changelog[].claims_changed` or `changelog[].claims_remeasured`, moving `measured_at` when you
claim a re-measurement.

**WHAT IT CANNOT SEE** whether the run id is the *right* run (A8 only proves it exists), whether
the `blend_note` is accurate, or whether the changelog prose is honest. Without a previous
manifest, conditions 3 and 4 are the only ones that can fire, and the article CLI prints
`claims gate: NO BASELINE` when that is the case.

## A5 : unit-bearing numerals in the document trace to a claim

**SEVERITY** error when coverage falls below the floor, warn when it clears the floor and some
numerals are still untraced. Four extra errors police `coverage.allow`'s shape. The per-numeral
warning is waivable by `numeral` + `unit`.

**JURISDICTION** **RENDERED DOCUMENT.** This is the check with jurisdiction over what ships, and
the reason a rendered document is not optional in spirit. It reads `visible_text()`: the document
with `<script>` and `<style>` bodies removed, then all tags removed, **plus** the values of
double-quoted `title=`, `alt=` and `aria-label=` attributes, because the omission attack hid a
stale figure in a tooltip precisely because a tag stripper throws attributes away.

**WHAT IT CATCHES** a printed measurement that no claim backs. Reproduced here against the minimal
manifest below and a document carrying a fabricated headline:

```
  [ERROR] A5  the document prints 1,240 tok/s, which matches no claim value at that precision and no coverage.allow pattern. Context: 1.9 s. The engine sustains 1,240 tok/s across 96 concurrent users.
  [ERROR] A5  3 of 4 unit-bearing numerals in the rendered document trace to a claim (75.0%), below the 100.0% this manifest requires. Declare the missing ones as claims, or allow them in coverage.allow with a reason.
```

Coverage is printed on a pass as well as a failure, because "0 errors" over a manifest that
asserts almost nothing reads exactly like "0 errors" over one that asserts everything:

```
  document coverage: 3/4 unit-bearing numerals traced to a claim (75.0%, floor 100.0%), 0/1 bare numerals (0.0%, floor 0.0%), over 209 visible characters of the rendered document
```

**HOW TO SATISFY IT** declare every printed measurement as a claim with a `value` and a `unit`.
Matching is precision-aware and unit-aware:

- **Precision.** A printed numeral matches a claim if the claim sits within half a unit of the last
  printed digit, so a claim of 77.8523 covers `78%`, `77.9%` and `77.85%`. Rounding mode does not
  matter.
- **Units.** The comparison happens in the family's base unit, so a claim recorded in `ms` covers a
  figure printed in `us`. A claim from a **different family is not a candidate at all**: a
  throughput in `tok/s` cannot cover a printed percentage that happens to share its value.
- **Recognised printed units** (`UNIT_TOKENS`, longest first): `tokens/s`, `requests/s`,
  `microseconds`, `GFLOP/s`, `TFLOPS`, `GiB/s`, `tok/s`, `req/s`, `TOPS`, `kWh`, `GB/s`, `MB/s`,
  `MiB`, `GiB`, `GB`, `TB`, `ms`, `us`, `W`, `%`, `s`, `x`. The numeral may carry `$`, a UK pound
  or a euro sign, a leading sign, and thousands separators; all belong to the numeral, so `$0.11`
  and `+0.78%` are each one number with one unit.
- **Deliberate non-matches.** `s`, `W` and `x` attached with no separator need a decimal point in
  the numeral, so `1.93s` is a duration while `5090s`, `1950s` and `2000s` stay a plural and two
  decades. `x` followed by a digit is a lane count or a part number (`PCIe 4.0 x4`, `2x5090`), not
  a multiplier.
- **Last resort, `coverage.allow`.** A list of `{"pattern": <regex>, "why": <non-empty reason>}`.
  The pattern is matched against the token and against 30 characters of context either side. An
  entry that is not an object, has no pattern, has no reason, or whose regex does not compile is an
  **error**: an allowance that does not say why is a silenced check.
- **The floor.** `coverage.min_unit_bearing_pct`, **default 100.0**. Below the floor the untraced
  numerals are errors; at or above it they are warnings, so lowering the floor does not make them
  disappear from the output.

**WHAT IT CANNOT SEE** four things, in rough order of how likely they are to matter.

1. **Bare numerals.** `96 concurrent users` carries no unit, so A5 never looks at it. It is only
   covered when `coverage.min_bare_numeral_pct` is set above zero, which turns on A6.
2. **Units it does not know.** `MHz`, `degrees C`, `GB/day`, `tokens` on its own: not in
   `UNIT_TOKENS`, therefore not a measurement as far as this check is concerned.
3. **A coincidence.** With two hundred claims in scope, a fabricated number that lands within half
   a printed digit of some same-family claim is covered. The family restriction narrows this a lot
   and does not close it. F4 is the table-scoped, stricter version of the same idea.
4. **Attributes it does not read.** Single-quoted attribute values, and any attribute other than
   `title`, `alt` and `aria-label`. Numbers inside `<script>` or `<style>` are out of scope by
   design.

It also cannot see *where* a covered numeral appears. A number that is correct in the appendix and
wrong in the abstract is covered either way.

## A6 : bare numerals in the document trace to a claim

**SEVERITY** error in practice. Not waivable.

The code reads `report_at = f.error if bare_declared else f.warn`, but the warn branch is
**unreachable**: the undeclared floor is `0.0` and the condition is
`bare_pct + 1e-9 < floor_bare`, which no percentage satisfies against zero. So A6 fires only when
`coverage.min_bare_numeral_pct` is declared above zero, and then as an error. Verified by running
both ways.

**JURISDICTION** RENDERED DOCUMENT, the same `visible_text()` as A5, over every numeral that A5's
unit scan did not already claim.

**WHAT IT CATCHES** a fabricated count, share, or user number with no unit attached. Reproduced,
with `coverage.min_bare_numeral_pct: 50.0` and the same document:

```
  [ERROR] A6  0 of 1 bare numerals trace to a claim (0.0%), below the 50.0% floor this manifest declares. Examples: 96 in 'e sustains 1,240 tok/s across 96 concurrent users. |'
```

That is the real `96 concurrent users` case: invisible with the default floor, caught the moment a
floor exists.

**HOW TO SATISFY IT** set `coverage.min_bare_numeral_pct` to a number you can actually hold, and
declare the counts it makes you declare. Bare matching is dimensionless: every numeric claim is a
candidate and the comparison is on raw values at the printed precision. `coverage.allow` applies
here too.

**WHAT IT CANNOT SEE** anything, while the floor is undeclared, which is the default. Even with a
floor it is a percentage: a document can clear 90% and still print the one fabricated count that
matters. The named examples in the finding are capped at 12 collected and 5 printed.

## A7 : one quantity, one value, whatever it is called

**SEVERITY** error for the two structural groupings, warn for the near-label heuristic. The warn is
waivable by `keys`.

**JURISDICTION** MANIFEST (`claims[].quantity`, `claims[].label`, `claims[].unit`,
`claims[].basis`, and `equalities` as an exemption list).

**WHAT IT CATCHES** the same measurement declared twice with different values, when nobody declared
an equality group over them. Three groupings, most structural first:

1. **`quantity`**, an explicit author-declared id shared by claims that are the same quantity.
   Disagreement is an **error**: `claims declared as the same quantity 'decode_roof' disagree:
   a=233, b=204.5`.
2. **`label`**, compared as a set of meaning-carrying words, so `aggregate throughput at
   concurrency 8` and `Aggregate throughput, c8` group together. Also an **error**: the reader sees
   one name twice. Stopwords dropped before comparison: `the a an of at in on per for and to from
   by with over under its`.
3. **near-label**, inside one `(unit, basis)` bucket, when the token sets overlap by at least 0.75
   (Jaccard) and are not identical. A heuristic, so a **warn**. Pairs whose difference is entirely
   digits are skipped, because `the ceiling at 128 tokens` and `the ceiling at 2048 tokens` are two
   ceilings and are supposed to disagree.

The tolerance is **fixed at 0.002** and is not configurable per group. A pair already reported by a
more structural grouping, or covered by a declared `equalities` group, is not reported again.

**HOW TO SATISFY IT** give claims that are one quantity the same `quantity` id and make them agree,
or declare them in `equalities` with a tolerance if they legitimately differ. Do not fix a label
collision by rewording the label.

**WHAT IT CANNOT SEE** two claims that are the same quantity, carry no `quantity` id, and have
labels that share less than 75% of their words. This check was a warning that fired only on
byte-identical labels, and both halves were exploited: 2181.7 and 2850.0 shipped as warning 29 of
29, and rewording a third claim's label to say the same thing produced nothing at all. Token-set
comparison closes the rewording route, not the rewriting route.

## A8 : a measurement names a run that exists

**SEVERITY** error for a run that resolves to nothing, warn for a timestamp outside the run's
window. The warn is waivable by `claim` + `run`.

**JURISDICTION** MANIFEST (`claims[].run`, `claims[].measured_at`, `runs`).

**WHAT IT CATCHES** three things:

1. A whitespace-only run id: `measured claim X names the blank run ' '`. The old check tested `run`
   for truthiness, and a single space passes truthiness while naming nothing.
2. A run id that is not a key of `runs`: `measured claim X names run 'instrumentation', which is not
   in the run table (primary, tool). A run id that resolves to nothing makes the measurement
   unattributable, and nothing downstream can tell it from a real one.`
3. (warn) `claim X is stamped 2026-08-25T09:00:00Z, outside run primary's window (... to ...).
   Either the claim is attributed to the wrong run or the run's bounds are wrong; say which in the
   manifest.`

**HOW TO SATISFY IT** every `runs` key is a real run; every measured claim's `run` is one of those
keys; `measured_at` sits between that run's `started` and `finished`. Condition 3 is a warning on
purpose: real result files in hand contain probes whose recorded start is after the artefact's own
`finished_at_utc`, and a read-only result file must not be edited to suit a check. Document it in
the run's `window_note` and accept the warning by name.

**WHAT IT CANNOT SEE** whether the run entry describes a run that happened. A `runs` table is a
declaration: an entry with a plausible id, invented `started` and `finished` timestamps, and no
`artifact` satisfies A8 completely. Only G3 opens a file, and only for the run the gate names.

## A9 : `kind` is earned, not chosen

**SEVERITY** error. Not waivable.

**JURISDICTION** MANIFEST (`claims[].kind`, `unit`, `basis`, `source`, `derivation_waiver`).

**WHAT IT CATCHES** two laundering routes, both reproduced.

1. **A ratio that nobody recomputes.** B1 recomputes only claims whose `kind` is `derived`, and
   `kind` is the generator's free choice, so relabelling a claim `supplied` shipped a value printed
   as 3.0 whose own arithmetic gives 10.73. A percentage or a `ratio` basis is by construction a
   quotient of two other numbers in the manifest, so: `claim X is a percentage of kind 'supplied'
   with no formula and no derivation_waiver. A ratio is a quotient of two numbers that are
   somewhere else in this manifest: derive it so the quotient is checked, or state in
   derivation_waiver why it cannot be.`
2. **An exemption with no provenance.** `supplied` and `published` are exempt from B1, so the
   source is the only thing left. Real demo text:

```
  [ERROR] A9  claim decode_step_budget_ms is 'supplied' with the source 'engineering estimate', which names nothing a reader can go and look at. An exemption from recomputation has to be redeemable: name a run id from the run table, a URL, or a file or module path.
```

**HOW TO SATISFY IT** make percentages and ratios `derived` with a `formula`, or set
`derivation_waiver` to a non-empty sentence saying why the quotient is not available. For
`supplied` and `published`, set `source` to something `source_resolves()` can resolve, which is
exactly three things: a run id from `runs` (as the whole string or as one whitespace-separated
token), a URL matching `https?|ftp|file://` or `www.`, or a path-like token matching
`[A-Za-z_][\w-]*([./\\][A-Za-z_][\w-]*)+`. Deliberately mechanical: any keyword list that admitted
"specification" would admit whatever an author typed next.

**WHAT IT CANNOT SEE** whether the source says what the claim says. `source: "vendor.com/specs"`
resolves; whether that page carries the number is beyond the gate. A `derivation_waiver` is free
text and is never read for meaning. And a plain `measured` claim whose value has nothing to do with
its run is not this check's business, or any check's: see "What the gate still cannot do".

## A10 : an edition may not declare less than the last

**SEVERITY** error, all three conditions. Not waivable.

**JURISDICTION** MANIFEST against the PREVIOUS edition's MANIFEST. Lives in
`gpubench/longform/__init__.py` (`check_declaration_floor`) rather than verify.py, because locating
the baseline is the gate's job, not the verifier's. **It does not run under `gpubench verify`.**

**WHAT IT CATCHES** the cheapest way to pass a gate that reads a manifest: declare less. A manifest
cut down to one claim reported `1 claim(s), 0 warning(s)`, shipped the same wrong number in the
body, and printed a *cleaner* log line than the honest 197-claim manifest. Reproduced against the
minimal manifest below with one claim deleted and one kind demoted:

```
  [ERROR] A10  1 claim(s) declared by the previous edition are gone from this one with no changelog row naming them: ttft_p95_c8. The count fell from 4 to 3. Declaring less is how a manifest passes more cleanly than the honest one.
  [ERROR] A10  1 claim(s) rest on weaker evidence than in the previous edition, with no changelog row: throughput_c8 (measured -> published). The printed number does not move when this happens, so nothing else in the document shows it.
```

Three conditions: ids gone (claims, prose blocks, figures, each checked separately); a count that
fell while every previous id survived (reachable only when ids repeat or are blank); and a claim
whose `kind` moved down the evidence ladder `KIND_EVIDENCE` = `measured 3, derived 3, published 2,
supplied 2, projection 1, assumption 1`. `measured` and `derived` share the top rung because the
gate recomputes a derived claim from measured inputs, so it is exactly as checkable.

**HOW TO SATISFY IT** name every removal or demotion in a changelog row, in any of
`claims_changed`, `claims_remeasured`, `claims_removed`, `prose_removed`, `figures_removed`
(`CHANGELOG_WAIVER_FIELDS`). Dropping a claim is legitimate; it has to be a sentence someone wrote
rather than an absence. Give unnamed prose blocks and figures an `id`: the fallback stand-in is
positional (`prose[3]`), so reordering unnamed blocks reads as a change.

**WHAT IT CANNOT SEE** the first edition. With no previous manifest there is no floor, and the
article CLI says so in as many words: `claims gate: NO BASELINE ... so the declaration floor
(claim, prose and figure counts, and each claim's kind) was NOT checked this build.` A baseline
that exists but cannot be parsed is a hard failure (`read_manifest` raises), because degrading
quietly into "no baseline" is exactly the state in which a shrunken manifest sails through. It also
cannot see a claim that stayed, kept its kind, and had its value replaced by a worse one: that is
A4's changelog check, and it needs the same baseline.

## B1 : nothing derived is ever typed

**SEVERITY** error. Not waivable.

**JURISDICTION** MANIFEST (`claims[].formula`, `claims[].value`, `claims[].tolerance`).

**WHAT IT CATCHES** a derived value that was typed instead of computed. Real demo text:

```
  [ERROR] B1  prefill_fraction_of_ceiling_2048 does not recompute: printed 82, formula gives 77.8538 (5.06% off, tolerance 0.50%)
```

Also: `derived claim X declares no formula`, `X references unknown claim 'foo'`, and
`X formula failed: ...` for a syntax error, a division by zero, or disallowed syntax.

**HOW TO SATISFY IT** give every `derived` claim a `formula` written over other claim keys.
`safe_eval` permits only `+ - * / **`, unary plus and minus, names, numeric constants and tuples:
no calls, attributes, subscripts or comparisons. The environment is every claim whose `value` is an
`int` or `float`. The comparison is relative: `abs(got - want) / abs(want)`, against
`claims[].tolerance`, **default 0.005** (0.5%).

The same pass runs C1 and C2 over the formula's additive positions, and C1's mixed-basis-ratio
warning over a `ratio`-basis formula containing `/`.

**WHAT IT CANNOT SEE** whether the formula is the right formula. `{"value": 82.0, "kind":
"derived", "formula": "82.0"}` recomputes exactly and proves nothing; the demo fixture contains two
such constants on purpose (`"formula": "72.0"`). A derivation over the wrong two claims recomputes
cleanly too, which is why C1's basis checks exist: the arithmetic closes and the answer still
answers the wrong question.

## C1 : basis hygiene

**SEVERITY** error for three conditions, warn for the mixed-basis ratio. The warn is waivable by
`claim`.

**JURISDICTION** MANIFEST (`claims[].basis`, `claims[].formula`, `prose[].assert`).

**WHAT IT CATCHES** a per-device quantity added to, or compared against, a total.

1. An unknown basis: `claim X has unknown basis 'per_gpu'`. The allowed set (`BASES`) is
   `per_device, per_shard, total, per_sequence, per_token, per_request, ratio, scalar`.
2. An additive position in a formula whose two sides carry different bases:
   `X adds a per_device quantity to a total one with no declared conversion. This is how a
   per-device figure ends up compared against a total, and it is why a capacity derivation stops
   closing.`
3. A prose assertion across bases: `prose X compares per_device against total without conversion`.
4. (warn) A ratio over mixed bases. Real demo text:
   `seq_share_of_pool is a ratio over mixed bases ['per_sequence', 'total']. Ratios like this
   recompute cleanly while answering the wrong question; state the denominator basis beside the
   printed percentage.`

**HOW TO SATISFY IT** give every claim a `basis` from the set; keep additive positions on one
basis; set `basis_conversion` on the derived claim when the mixing is deliberate and documented.
`scalar` and `ratio` are ignored on both sides, so a unit conversion factor does not trip it.

**WHAT IT CANNOT SEE** multiplicative positions, on purpose: multiplying a per-request count by a
total-basis rate is ordinary dimensional work, while *adding* the two is the defect. It also sees
nothing when either side's basis is absent, when a side mixes two bases (the check requires exactly
one basis per side), or when `basis_conversion` is set, and it never reads what
`basis_conversion` says.

## C2 : unit hygiene

**SEVERITY** error across unit families, warn within one family. The warn is waivable by `claim`.

**JURISDICTION** MANIFEST (`claims[].unit`, `claims[].formula`).

**WHAT IT CATCHES** adding a time to a byte count (`X adds ['bytes'] to ['time']`), and, more
gently, adding two units of one family without saying which scale factor applies
(`X adds MiB to GiB; confirm the scale factor`).

**HOW TO SATISFY IT** record a `unit` on every claim, from one of the families in
`UNIT_FAMILIES`: `bytes` (B, KiB, MiB, GiB, TiB, KB, MB, GB, TB), `time` (ns, us, ms, s), `rate`
(tok/s, req/s, emb/s), `bandwidth` (GB/s, MB/s, GiB/s), `compute` (TFLOPS, TOPS, GFLOPS), `power`
(W, kW), `energy` (Wh, kWh, J), `percent` (%), `count` (`""` and `count`), `currency` (empty, so
any currency unit resolves to no family). Set `unit_conversion` on the derived claim when the
mixing is deliberate.

**WHAT IT CANNOT SEE** a unit string it does not know: `unit_family()` returns `None`, and `None`
is dropped from both sides before the family comparison, so an unrecognised unit is simply not
checked. It does not verify that the *scale factor in the formula* is right, only that the author
declared a conversion. And like C1 it only reads additive positions.

## D1 : a percentile discloses its sample size

**SEVERITY** error. Not waivable.

**JURISDICTION** MANIFEST (`percentiles[]`).

**WHAT IT CATCHES** `percentile ttft_p95_c8 discloses no sample size`. A p95 with no n is
decoration: it could be the 95th of 1000 or the second-worst of 20.

**HOW TO SATISFY IT** every entry in `percentiles` carries `key`, `q` (as a fraction, `0.95`) and
`n`. Note `if not n`, so `n: 0` errors too.

**WHAT IT CANNOT SEE** whether `n` is true, and whether a percentile printed in the document
appears in `percentiles` at all. A p99 that no manifest entry mentions is invisible here.

## D2 : a percentile is not an extreme in disguise

**SEVERITY** warn. Waivable by `claim`.

**JURISDICTION** MANIFEST (`percentiles[]`).

**WHAT IT CATCHES** a percentile whose rank lands within two of the top of the sample. Real demo
text:

```
  [warn ] D2  ttft_p95_c8 is a p95 over n=32, which resolves to ordered sample 31 of 32, the second worst value. That is an extreme wearing a percentile's name; print the rank beside it, raise n toward 100, or report a max and say so.
```

The rank is `ceil(q * n)` and the trigger is `n - rank <= 2`.

**HOW TO SATISFY IT** raise `n` (for a p95 the first clean sample size is **n = 60**: at n = 59 the
rank is 57 of 59, two from the top), print the rank beside the figure, or report a max and say so.
Otherwise accept the warning with a reason:

```json
"accepted_warnings": [
  {"check": "D2", "claim": "ttft_p95_c8",
   "why": "The sweep is 16 requests per level by design; the rank is printed beside the figure."}
]
```

Reproduced acceptance output:

```
  Accepted in the manifest, with a reason:
  [ok   ] D2  ttft_p95_c8 is a p95 over n=16, which resolves to ordered sample 16 of 16, the worst value. ...  (accepted: The sweep is 16 requests per level by design; the rank is printed beside the figure.)
```

**WHAT IT CANNOT SEE** anything about the sample's shape. It is arithmetic on `q` and `n`, nothing
more. **A surprise worth knowing:** the check mutates the manifest in place,
`p.setdefault("rank", rank)`. In the article pipeline the manifest file is written before
`verify()` runs, so the file on disk does not carry the injected `rank`; a direct caller that
serialises the dict afterwards will find it there.

## D3 : closed-loop wave arithmetic

**SEVERITY** two errors, two warns. The warns are waivable by `level`.

**JURISDICTION** MANIFEST (`levels[]`, and `claims` for the p95 cross-check).

**WHAT IT CATCHES** a concurrency level whose request count does not divide into whole waves, which
depresses throughput by an amount that depends on how badly it divides and then reads as scatter.
Real demo text:

```
  [ERROR] D3  level c48: 100 requests at concurrency 48 is not a whole number of waves. The final wave runs at concurrency 4, which depresses throughput by an amount that depends on how badly the count divides and reads as scatter.
  [warn ] D3  level c32 is 1 wave(s). With so few waves the level is a burst rather than a steady state: it is all ramp-up and drain, so a throughput figure from it should not be called sustained.
```

Also: `level X declares no concurrency or request count`, and a warn when a level's `duration_s`
equals its end-to-end p95 to within 1% while claiming more than one wave, which is the signature of
a single synchronised wave.

**HOW TO SATISFY IT** give each closed-loop level a `name`, a positive `concurrency`, and a
`requests` count that is a whole multiple of it, ideally at least 3 waves. `requests` may instead
be `requests_attempted` or `requests_ok`, which is the shape a harness document writes. Point
`e2e_p95_key` at the claim holding that level's end-to-end p95 and set `duration_s` to enable the
synchronised-wave check.

**WHAT IT CANNOT SEE** an open-loop level, deliberately: the whole block is skipped when the level's
arrival model starts with `open_loop`, and D6 replaces it. Running the wave arithmetic anyway is
what first crashed the verifier on `int(None)` and then, once guarded, produced a D3 error
demanding the very thing an open-loop level cannot have. It also cannot see whether the harness
actually ran the level it declares.

## D4 : an arrival model is declared

**SEVERITY** error. Not waivable.

**JURISDICTION** MANIFEST (`report.arrival_model`).

**WHAT IT CATCHES** a latency percentile quoted as a service level with no statement of how
requests arrived. Real demo text:

```
  [ERROR] D4  no arrival model declared. A latency percentile quoted as a service level has to say whether requests arrived in a burst or a stream: a closed-loop harness cannot produce the queue build-up that generates real tail latency.
```

**HOW TO SATISFY IT** set `report.arrival_model` to exactly one of `closed_loop`,
`open_loop_constant`, `open_loop_poisson`. Any other value, including a plausible one like
`open_loop`, is an error.

**WHAT IT CANNOT SEE** whether the declaration is true. D4 reads a string. That is precisely the
hole that shipped: flipping `closed_loop` to `open_loop_poisson` on a fixed-in-flight harness
produced zero findings. D7 closes part of it by reading the note printed beside the declaration,
and D6 closes more by demanding the evidence an open-loop run produces, but neither reads the
harness.

## D5 : a sustained figure states its duration

**SEVERITY** error for a missing duration, warn for an undeclared steady state. The warn is
waivable by `claim`.

**JURISDICTION** MANIFEST (`sustained[]`).

**WHAT IT CATCHES** `sustained figure power_mean states no duration`, and
`sustained figure power_mean does not say whether the measured quantity had stopped moving when the
run ended`.

**HOW TO SATISFY IT** every entry in `sustained` carries `key`, `duration_s`, and
`reached_steady_state` as an explicit `true` or `false` (the warn fires only on `None`, so a
recorded `false` satisfies it).

**WHAT IT CANNOT SEE** whether the run really reached a steady state, and whether the figure
printed as sustained appears in `sustained` at all.

## D6 : an open-loop level discloses rates and backlog

**SEVERITY** error. Not waivable.

**JURISDICTION** MANIFEST (`levels[]`, including nested `arrival` objects).

**WHAT IT CATCHES** an open-loop level with no way to tell a fast server from a server that was
never offered much. Real demo text:

```
  [ERROR] D6  level r40 declares arrival 'open_loop_poisson' and is missing target_rate_req_s, achieved_rate_req_s, a queue or in-flight trace (queue_depth with samples, or peak_inflight). An open-loop level is worth running precisely because offered load is fixed, so a server falling behind shows up as a rate deficit and a growing backlog. Without those the level reports latency with nothing to interpret it against.
```

Also `level X records no requests` when the request count is present and not positive.

**HOW TO SATISFY IT** three fields, each readable either on the level or inside its `arrival`
object (`level_field` looks in both):

- `target_rate_req_s`, a number or a list (a sweep records several).
- `achieved_rate_req_s`, a number.
- a queue or in-flight trace: any key matching `queue` or `in_flight` / `in-flight` / `inflight`
  whose value is a non-empty list, a number, or a dict carrying `samples` or any of `max`, `mean`,
  `at_last_arrival`, `last_sample`, `peak`.

A level's arrival model comes from its own `arrival` (a string, or a dict's `model`), and otherwise
inherits `report.arrival_model`. So one level in an open-loop report inherits open loop and must
satisfy D6.

**WHAT IT CANNOT SEE** the values. `target_rate_req_s: []` is a list, so it is accepted;
`peak_inflight: 0` is a number, so it is accepted. The check proves the level disclosed the three
things, not that the disclosure means anything. A closed-loop harness that records a queue key with
`{"sampled": false, "why": ...}` is correctly not a trace.

## D7 : the arrival note does not contradict the model

**SEVERITY** error, both directions. Not waivable.

**JURISDICTION** MANIFEST (`report.arrival_model` against `report.arrival_note`).

**WHAT IT CATCHES** exactly the attack that shipped. Flipping `closed_loop` to
`open_loop_poisson` on a fixed-in-flight harness printed `arrival_model open_loop_poisson` directly
beside `Fixed in-flight population per level; no independent arrival process`, and nothing read the
two together. A human would have needed one second.

- `arrival_model` starting `open_loop` beside any of `fixed in-flight`, `fixed in flight`,
  `no independent arrival process`, `issued when a previous one completes`,
  `issued when the previous one completes`: `arrival_model is 'open_loop_poisson' but arrival_note
  says 'fixed in-flight'. Those describe two different experiments, and the note is the one a
  reader believes. One of them is a leftover from the other mode.` Negation is **ignored** in this
  direction, so a closed-loop phrase counts even when negated.
- `arrival_model` exactly `closed_loop` beside a positive statement of independent arrivals
  (`requests arrive independently`, `arrivals are independent of completions`,
  `open-loop arrivals`, `open loop arrivals`, `independent arrival process`):
  `arrival_model is 'closed_loop' but arrival_note asserts 'independent arrival process'. A
  closed-loop harness has no arrival process to be independent of anything.` Here negation **is**
  honoured: the 14 characters before the phrase are checked for `no `, `not `, `never `,
  `without `, `cannot `, `rather than `, and a negated occurrence does not count. That is why the
  honest closed-loop note `no independent arrival process` passes, which is verified in the
  minimal example below.

**HOW TO SATISFY IT** write an `arrival_note` that describes the harness you actually ran, and keep
it beside the model. The word "poisson" on its own is not evidence of anything: a closed-loop note
may legitimately discuss Poisson arrivals to explain what it cannot do.

**WHAT IT CANNOT SEE** any wording outside the two phrase lists. The lists are deliberately narrow.
It does no NLP and cannot judge a note that contradicts the model in words nobody enumerated. It
also does nothing when either field is missing or the note is blank.

## E1 : a ceiling declares its measurement mode

**SEVERITY** error. Not waivable.

**JURISDICTION** MANIFEST (`ceilings[]`).

**WHAT IT CATCHES** a roof measured with other work resident, presented as a roof. Real demo text:

```
  [ERROR] E1  ceiling interconnect_ceiling_2048 is shared-mode with no caveat anchored beside the fraction that uses it. A caveat only in the limitations section is not where the reader meets the claim.
```

And `ceiling X declares no measurement mode. A percentage of a shared-mode roof has a floor in its
denominator and does not compare against an exclusive-mode one.`

**HOW TO SATISFY IT** every entry in `ceilings` carries `key` and `mode`, one of `shared` or
`exclusive`. A `shared` ceiling additionally carries `caveat_anchor`, which is where the caveat sits
next to the fraction that uses it, not in a limitations section at the back.

**WHAT IT CANNOT SEE** whether the mode is true, whether `caveat_anchor` points anywhere real (any
truthy value satisfies it), and whether a roof used in the document appears in `ceilings` at all.

## E2 : a roof descended from a vendor headline

**SEVERITY** warn. Waivable by `claim`.

**JURISDICTION** MANIFEST (`ceilings[].from_vendor_headline` and the ceiling claim's `kind`).

**WHAT IT CATCHES** `ceiling X descends from a vendor headline. Using a marketing figure as the roof
makes the roof-to-workload gap unfalsifiable, which is the point of drawing it.` It fires only when
the ceiling's claim `kind` is `published` or `derived` **and** the ceiling declares
`from_vendor_headline`.

**HOW TO SATISFY IT** measure the roof, or accept the warning with a reason that says what the
headline is and why no measured roof exists.

**WHAT IT CANNOT SEE** an undeclared vendor headline. `from_vendor_headline` is the author's own
admission; omitting it silences the check completely. This is the clearest self-report in the
catalogue and it is on the honour system.

## F1 : no HTML entity surfaces as visible text

**SEVERITY** error. Not waivable.

**JURISDICTION** both. The first half reads the MANIFEST (`prose[].text`); the second reads the
RENDERED DOCUMENT.

**WHAT IT CATCHES** double escaping, where `&amp;amp;` renders as the visible characters `&amp;`.
Findings: `prose X contains an unescaped HTML entity`, and
`rendered document shows literal entities in visible text: ['&amp;']`. The pattern is
`&(?:amp|mdash|ndash|lt|gt|quot|#\d+);`.

**HOW TO SATISFY IT** escape once, in the renderer, and never in the source string.

**WHAT IT CANNOT SEE**, and a real surprise: it reads the document **before** entity decoding, so
it cannot tell a correct `&amp;` from a double-escaped one. Reproduced:

```
  [ERROR] F1  rendered document shows literal entities in visible text: ['&amp;']
```

The document was `<p>R&amp;D at 240 tok/s, 80%, 1.9 s.</p>`, which is correct HTML for "R&D". So a
document that legitimately needs an ampersand in visible text must emit the raw character or a hex
entity (`&#x26;`, which the digits-only `&#\d+;` branch does not match). Documented as the code
behaves, not as one might wish. It also does not read attribute values, unlike A5: F1 uses its own
stripper without the `VISIBLE_ATTRS` addition.

## F2 : every declared figure has a table view

**SEVERITY** error. Not waivable.

**JURISDICTION** MANIFEST (`figures[]`).

**WHAT IT CATCHES** `figure fig9_latency has no table view; a chart without its values is an
assertion`. Fires on any figure where both `table_view` and `table_view_shared_with` are falsy,
including an explicit `table_view: false`.

**HOW TO SATISFY IT** set `table_view: true`, or `table_view_shared_with: "<other figure id>"` when
two figures print the same rows.

**WHAT IT CANNOT SEE** whether the table exists in the document. `table_view: true` was believed,
and a figure shipped an **empty** `<details><summary>Table view</summary></details>` directly under
a note telling the reader the series was in the table view. The series was in neither. F3 is the
check that reads the outcome. F2 also cannot see a figure the manifest never declared.

## F3 : the declared table view actually rendered

**SEVERITY** error for a missing or empty table, warn for a free-text declaration. The warn is
waivable by `figure`.

**JURISDICTION** RENDERED DOCUMENT. This is F2's outcome half, and it exists because F2's
declaration was exploited.

**WHAT IT CATCHES** four things:

1. `figure X declares a table view but does not appear in the rendered document at all`.
2. `figure X declares a table view and renders none, or renders an empty one. The values a chart
   asserts are then nowhere in the document.` "Non-empty" means at least one `<td>` or `<th>` with
   visible text after tag stripping.
3. `figure X shares its table view with 'Y', which is not a figure in this manifest`, and
   `... with Y, which renders no table of its own, so neither figure ships its values`.
4. (warn) `figure X declares its table view as free text ('shared with figure arlat: the same
   rows'). Use table_view_shared_with so the gate can confirm the table it points at actually
   renders.`

**HOW TO SATISFY IT** render each figure as `<figure id="<figure id>"> ... <table> ... </table>
</figure>`. The id on the `<figure>` element is how the check locates it (`FIGURE_BLOCK` requires a
double-quoted `id` attribute).

**WHAT IT CANNOT SEE** whether the table holds the right numbers, or all of them: one non-empty
cell satisfies it. A figure with `table_view: false` is never looked for. And with no rendered
document the whole check is skipped silently, which is why the coverage line always says whether a
document was supplied.

## F4 : citations resolve; table numerals trace to that table's cells

**SEVERITY** error for one condition, warn for two. The warns are waivable by `table` (the second
carries no fields at all, so it can only be accepted by check id).

**JURISDICTION** split, and worth reading twice. One id covers two unrelated checks:

- **MANIFEST**: a `{{placeholder}}` in a declared prose block naming a claim that does not exist:
  `prose sec24 cites unknown claim {{ttft_p95_c9}}`.
- **RENDERED DOCUMENT**: numerals printed inside a table that trace to no declared cell **of that
  table**.

**WHAT IT CATCHES** in its document half, a figure in the capacity table that is not one of the
capacity table's own claims: `table capacity_sweep prints 2 numeral(s) that match no declared cell
of it: 2850tok/s, 3400tok/s. Either the table draws on a claim it does not declare, or a figure in
it was typed.` This is stricter than A5, which accepts any claim anywhere with a matching value.

It also warns when a declared table cannot be found in the document at all: `2 declared table(s)
could not be found in the rendered document, so their cells were not checked against what shipped:
... A rendered table needs id="<table id>", or to sit inside a figure with that id, before F4 has
anything to read.`

**HOW TO SATISFY IT** declare `tables: {"<id>": {"cells": ["<claim key>", ...]}}`, list every claim
the table prints, and render the table with `id="<id>"` on the `<table>` element or inside a
`<figure id="<id>">`. Cite claims in prose by their exact keys.

**WHAT IT CANNOT SEE** a table the document does not identify (reported as a warning, not an
error), numerals outside any identified table (A5's job), and bare numerals inside a table, since
it uses the same unit-bearing scan as A5. The whole document half is skipped when `tables` is
empty.

## G1 : a quality gate is recorded and passed

**SEVERITY** error. Not waivable.

**JURISDICTION** MANIFEST (`gate`).

**WHAT IT CATCHES** three things:

1. No `gate` at all: `no quality gate recorded. A benchmark that measures only speed rewards a
   stack that got faster by getting worse, so a more aggressive quantisation or a truncated context
   reads as an improvement.` The check returns immediately after this one.
2. `gate.passed` falsy: `the quality gate did not pass; performance figures taken in this window
   are unsound`.
3. `gate.window_run` naming a run that is not in `runs`. Real demo text:
   `the gate names run 'instrumentation', which is not in the run table`.

**HOW TO SATISFY IT** record `gate` with `passed`, `cases_published`, and a `window_run` that is a
key of `runs`.

**WHAT IT CANNOT SEE**, and this is a live hole worth naming: `passed` is read with Python
truthiness. **A manifest declaring `"passed": "false"` (the string) passes G1 with zero findings**,
because a non-empty string is truthy. Reproduced: with `"passed": "false"` and a run that declares
no `artifact` path, the whole verifier reports one warning and exits 0. G3 catches the disagreement
only when the artefact is readable and actually failed. A typed `false` boolean is caught correctly
by both G1 and G3.

## G2 : the gate's cases are published

**SEVERITY** error. Not waivable.

**JURISDICTION** MANIFEST (`gate.cases_published`).

**WHAT IT CATCHES** `the gate's cases are not published. A gate nobody can re-run is an assertion.`
Fires on any falsy `cases_published`, including `false` and `0`.

**HOW TO SATISFY IT** set `cases_published` to `true` or to the number of published cases. G3 then
checks that number against the artefact, so prefer the number.

**WHAT IT CANNOT SEE** where the cases are published, or whether they can be re-run. G3 reads the
count out of the artefact; nothing reads the cases themselves.

## G3 : the gate result reads back out of the artefact

**SEVERITY** error for a disagreement or an unreadable artefact, warn when there is nothing to read
back. The warns carry no narrow field, so they can only be accepted by check id.

**JURISDICTION** **EXTERNAL RESULT ARTEFACT.** The only check in the catalogue that opens a file
the manifest points at. It resolves `runs[gate.window_run]["artifact"]`, relative to the manifest's
own directory when the path is relative (`--rendered` and `--previous` are unrelated to this; the
root is the manifest's directory, or `out_dir` under the article gate).

**WHAT IT CATCHES** a hardcoded verdict. `passed: true` with `cases_published: 999` shipped with
zero findings, because nothing ever opened the file the gate said it came from. A gate result
nobody read back is worse than no gate: it is a gate that reports success unconditionally.

Reproduced, from the one-defect example below (the manifest says 12, the artefact publishes 2):

```
  [ERROR] G3  the manifest says 12 published case(s); the artefact run.json carries 2
```

Real demo text for the unfalsifiable case:

```
  [warn ] G3  the gate result is unfalsifiable as recorded: run 'instrumentation' declares no "artifact" path, so passed=True and cases_published=False are assertions rather than readings. Point the run at the result file the gate ran in.
```

The other findings: `run X names the artifact 'p.json', which cannot be read (...)` (error),
`the artifact 'p.json' is not an object` (error), `the artifact 'p.json' records no gate result this
verifier can read (looked for probes.accuracy and a top-level gate object)` (warn), and
`the manifest says the gate passed=True; the artefact run.json says did not pass (...)` (error).

**HOW TO SATISFY IT** point the gate's run at a real file: `runs["<window_run>"]["artifact"]`. Two
artefact shapes are read (`read_gate_result`):

```json
{"probes": {"accuracy": {
  "summary": {"cases": 2, "exact_match_pct": 100.0, "deterministic": 2},
  "method": {"cases_published": ["arith-1", "arith-2"]},
  "errors": []}}}
```

or a top-level `{"gate": {...}}` object with the same field names plus an optional `passed`.

The artefact-side pass is computed, not read: it passes when **no** case errored, **every** key in
the summary ending `_pct` is at or above the threshold, and `deterministic` is not less than
`cases`, and (for the top-level shape) the artefact does not itself record `passed: false`. The
threshold is `gate.threshold_pct`, **default 100.0**, because that is what an accuracy regression
gate means; a lower bar is legitimate and has to be written down where a reader can see it.
`cases_published` is compared as a boolean if the manifest declared a boolean, and as an integer
otherwise.

**WHAT IT CANNOT SEE** a well-formed lie inside the artefact. If the result file says 100% and the
model was actually wrong, G3 agrees with it. It only reads the run the gate names, so every other
run's `artifact` is unread. And when the run declares no `artifact` at all, the result is a
**warning**: the gate reports that the declaration is unfalsifiable and does not block. A manifest
can therefore still ship a gate verdict nobody checked, and it will say so in the findings.

---

# The manifest contract

`schema` must be the string `claims/1`. Everything below is a top-level key of that object. Only
`schema` and `claims` are effectively required: `schema` because the shape check errors otherwise,
and `claims` because `check_derivations` raises `KeyError` without it. Every other top-level key is
optional, and **an omitted key means the checks that read it do not run**, which is why A5, A6, A10
and the coverage line exist.

## `claims` : object, claim id to claim

The only required field is `value`. Everything else is optional, and several checks turn themselves
off when a field is missing.

| Field | Type | Required | Default when missing |
|---|---|---|---|
| `value` | number (usually) | **yes** | `manifest` error. A non-numeric value passes the shape check and crashes A1/A3 if either reads it |
| `kind` | one of six strings | **yes** | `manifest` error |
| `unit` | string | no | no unit family, so C2 skips it, A5 treats a matching claim as dimensionless |
| `basis` | one of `BASES` | no | C1 skips it. An unknown value is a C1 error |
| `label` | string | no | A7's label groupings skip it |
| `quantity` | string | no | A7's strongest grouping is unavailable |
| `run` | key of `runs` | for `measured` | A4 error if absent, A8 error if it resolves to nothing |
| `measured_at` | ISO 8601 | no | A8's window warning cannot fire |
| `formula` | expression over claim keys | for `derived` | B1 error |
| `tolerance` | number, relative | no | **0.005** in B1 |
| `source` | resolvable string | for `supplied`, `published` | A9 error |
| `derivation_waiver` | non-empty string | alternative for a `%` or `ratio` claim that is not `derived` | A9 error |
| `basis_conversion` | any truthy | no | C1 errors on mixed additive bases |
| `unit_conversion` | any truthy | no | C2 errors across unit families |

**`kind` is a closed six-member enum**: `measured`, `derived`, `assumption`, `projection`,
`supplied`, `published`.

## `runs` : object, run id to run record

Only `artifact` is machine-read.

| Field | Read by | Meaning |
|---|---|---|
| `artifact` | **G3** | Path to the result file, absolute or relative to the manifest's directory. **This is the only field in the run record that any check opens.** |
| `started`, `finished` | A8 | ISO 8601 bounds. A claim stamped outside them is a warning |
| anything else | nobody | `artefact`, `path`, `mode`, `contributes`, `window_note`, `tool_version` and friends are for the reader |

### The spelling and vocabulary traps

**`artefact` and `artifact` both exist and they are different things.**

- `artifact` (i-spelling) is the **machine-read path**, read at exactly one place,
  `verify.py: declared_path = (run or {}).get("artifact")`. G3 opens it.
- `artefact` (e-spelling) is a **pre-existing descriptive key** carrying a basename for the reader.
  A content module in use with this tool sets it on every run entry, beside a `path` that is also
  descriptive. **No check reads either of them.** A run that declares only `artefact` gets G3's
  `declares no "artifact" path` warning, and the verdict stays unfalsifiable. Reproduced: removing
  the `artifact` key from the clean example below turns 0 findings into that one warning.
- The prose in verify.py uses the e-spelling throughout ("read back out of the artefact it names"),
  including inside G3's own finding text: `the artefact run.json carries 2`. Only the JSON key is
  the i-spelling.
- A third, unrelated `artifact`: a gpubench **result** file carries a top-level `artifact` object
  written by `runner.artifact_identity()` (tool version, source path, checksum). That is the tool's
  identity, not a path, and nothing in this gate reads it.

**Two `kind` enums exist.** `claims/1` has six members. The template linter's value envelope
(`gpubench/template/run-schema.json`, `KIND_ENUM` in `gpubench/template/lint.py`) has eight: the
same six plus `enumerated` and `fixed-test-set`. Putting either extra kind in a claims manifest is
an error, reproduced:

```
  [ERROR] manifest  claim rt_cores has no valid kind
  [ERROR] manifest  claim gate_cases has no valid kind
```

## `report` : object

| Field | Read by | Notes |
|---|---|---|
| `arrival_model` | D4, D7, and every level's default in D3/D6 | Must be `closed_loop`, `open_loop_constant` or `open_loop_poisson` |
| `arrival_note` | D7 | Free text, checked against the model's phrase lists |

Any other field (`version`, and so on) is unread.

## `equalities` : list

Each entry is either a bare list of claim keys, or `{"keys": [...], "tolerance": <number>}`.
Tolerance default **0.002**. Read by A1, and by A7 as an exemption list: a group declared here is
never reported by A7. A dict entry with no `keys` raises `KeyError`.

## `tables` : object, table id to table

| Field | Read by | Notes |
|---|---|---|
| `cells` | A4, F4 | List of claim keys the table prints |
| `blended` | A4 | Required (truthy) when the measured cells span more than one run |
| `blend_note` | A4 | Required when `blended` is set |

## `prose` : list of blocks

| Field | Read by | Notes |
|---|---|---|
| `id` | every finding | Optional; defaults to `<unnamed>` in messages and to `prose[i]` in A10 |
| `text` | A2, F1, F4 | `{{claim_key}}` placeholders are stripped before A2's numeral scan |
| `allow_literals` | A2 | Compared as strings, exactly as printed, commas included |
| `assert` | A3, C1 | `{"op", "left", "right"}` plus the operator's own fields |

## `percentiles` : list

`{"key": <claim key>, "q": <fraction>, "n": <int>}`, optionally `level`. `n` required by D1.
D2 warns when `n - ceil(q*n) <= 2` and writes `rank` back into the entry.

## `levels` : list

| Field | Read by | Notes |
|---|---|---|
| `name` | D3, D6 | Identifies the level in findings and in acceptances |
| `concurrency` | D3 | Positive integer for a closed-loop level. **`null` is correct for an open-loop level**, where concurrency is an outcome, and every cast goes through `as_int` so a `null` cannot crash the verifier |
| `requests` | D3, D6 | Falls back to `requests_attempted`, then `requests_ok` |
| `duration_s` | D3 | Enables the synchronised-wave warning |
| `e2e_p95_key` | D3 | Claim key for that level's end-to-end p95 |
| `arrival` | D3, D6 | A string, or a dict with `model` plus the rates and the queue trace. Absent means inherit `report.arrival_model` |
| `target_rate_req_s`, `achieved_rate_req_s` | D6 | Required for an open-loop level, on the level or inside `arrival` |
| a queue or in-flight key | D6 | Any key matching `queue` or `in_flight`; see the D6 entry for what counts as data |

## `ceilings` : list

`{"key": <claim key>, "mode": "shared" | "exclusive"}`, plus `caveat_anchor` (required when
`shared`) and `from_vendor_headline` (optional, triggers E2's warning for a `published` or `derived`
ceiling claim).

## `sustained` : list

`{"key": <claim key>, "duration_s": <number>, "reached_steady_state": true | false}`. Read by D5.

## `figures` : list

`{"id": <string>, "table_view": true}` or `{"id": ..., "table_view_shared_with": "<other id>"}`.
Read by F2 (manifest) and F3 (rendered document). A truthy non-`true` `table_view` is a warning.

## `changelog` : list

`{"version": ..., "claims_changed": [...], "claims_remeasured": [...], "claims_removed": [...],
"prose_removed": [...], "figures_removed": [...]}`. A4 reads the first two, and needs a previous
manifest. A10 reads all six field names as waivers.

## `coverage` : object

| Field | Default | Effect |
|---|---|---|
| `min_unit_bearing_pct` | **100.0** | Below it, A5's untraced numerals are errors |
| `min_bare_numeral_pct` | **0.0, undeclared** | A6 cannot fire at all until this is above zero |
| `allow` | empty | `[{"pattern": <regex>, "why": <non-empty>}]`. A missing or empty `why`, a missing `pattern`, a non-object entry, or a regex that does not compile is an A5 error |

## `accepted_warnings` : list

`[{"check": "D2", "claim": "ttft_p95_c8", "why": "..."}]`. `check` and a non-empty `why` are
required. Every other field present must match the finding, so an acceptance is narrow by
construction and cannot be written to swallow a whole check. Matchable fields:
`claim, claims, keys, block, table, figure, level, run, numeral, unit, quantity, label`. Accepted
warnings are printed in their own section and counted separately. Errors are never suppressible.

## `gate` : object

| Field | Read by | Notes |
|---|---|---|
| `passed` | G1, G3 | Read with `bool()`. A typed boolean, please: see G1's blind spot |
| `cases_published` | G2, G3 | `true`, or the number of cases. G3 compares it against the artefact |
| `window_run` | G1, G3 | A key of `runs`, whose `artifact` G3 opens |
| `threshold_pct` | G3 | **Default 100.0** |
| `ran_at` | nobody | For the reader |

---

# A worked example

## A minimal valid manifest

Two files in one directory. `run.json` is the result artefact:

```json
{
  "started_at_utc": "2026-08-25T10:00:00Z",
  "finished_at_utc": "2026-08-25T10:40:00Z",
  "probes": {
    "accuracy": {
      "summary": {"cases": 2, "exact_match_pct": 100.0, "deterministic": 2},
      "method": {"cases_published": ["arith-1", "arith-2"]},
      "errors": []
    }
  }
}
```

`claims.json`:

```json
{
  "schema": "claims/1",
  "report": {
    "arrival_model": "closed_loop",
    "arrival_note": "Fixed in-flight population per level; no independent arrival process."
  },
  "runs": {
    "serving": {
      "artifact": "run.json",
      "artefact": "run.json",
      "started": "2026-08-25T10:00:00Z",
      "finished": "2026-08-25T10:40:00Z",
      "contributes": "the concurrency sweep and the accuracy gate"
    }
  },
  "claims": {
    "throughput_c8": {
      "value": 240.0, "unit": "tok/s", "basis": "total", "kind": "measured",
      "run": "serving", "measured_at": "2026-08-25T10:12:00Z",
      "label": "aggregate throughput at concurrency 8"
    },
    "throughput_ceiling": {
      "value": 300.0, "unit": "tok/s", "basis": "total", "kind": "measured",
      "run": "serving", "measured_at": "2026-08-25T10:20:00Z",
      "label": "decode roof from the bandwidth sweep"
    },
    "ttft_p95_c8": {
      "value": 1.9, "unit": "s", "basis": "per_request", "kind": "measured",
      "run": "serving", "measured_at": "2026-08-25T10:12:00Z",
      "label": "time to first token p95 at concurrency 8"
    },
    "throughput_fraction_of_ceiling": {
      "value": 80.0, "unit": "%", "basis": "ratio", "kind": "derived",
      "formula": "100 * throughput_c8 / throughput_ceiling",
      "label": "measured throughput as a share of the decode roof"
    }
  },
  "levels": [
    {"name": "c8", "concurrency": 8, "requests": 64, "duration_s": 30.0,
     "e2e_p95_key": "ttft_p95_c8"}
  ],
  "percentiles": [
    {"key": "ttft_p95_c8", "q": 0.95, "n": 64, "level": "c8"}
  ],
  "ceilings": [
    {"key": "throughput_ceiling", "mode": "exclusive"}
  ],
  "prose": [
    {"id": "s3_headline",
     "text": "Measured throughput reaches {{throughput_c8}}, which is {{throughput_fraction_of_ceiling}} of the decode roof.",
     "assert": {"op": "lt", "left": "throughput_c8", "right": "throughput_ceiling"}}
  ],
  "gate": {
    "passed": true,
    "cases_published": 2,
    "window_run": "serving",
    "ran_at": "2026-08-25T10:30:00Z"
  }
}
```

```
$ gpubench verify claims.json

  document coverage: no rendered document was supplied, so no numeral was checked
  0 error(s), 0 warning(s), 0 accepted
$ echo $?
0
```

Worth noticing in that manifest:

- The `arrival_note` contains the phrase `independent arrival process`, which is on D7's open-loop
  list, and it still passes: the phrase is preceded by `no `, and D7 honours negation in the
  closed-loop direction.
- `n: 64` for a p95 gives rank 61 of 64, three from the top, so D2 stays quiet. At `n: 16` it
  warns.
- The derived percentage is `kind: "derived"` with a formula, which is what A9 demands of anything
  carrying `%` or a `ratio` basis, and B1 then recomputes it: 100 * 240 / 300 = 80.0.
- G3 opens `run.json` because run `serving` declares `artifact`. Remove that one key and the clean
  run becomes `1 warning`, not an error.

## The same manifest with one defect

Change one field: `gate.cases_published` from `2` to `12`. Nothing else.

```
$ gpubench verify claims-defect.json
  Render blocked. Fix the errors, or re-measure. Never edit a measured value to satisfy a check.
  [ERROR] G3  the manifest says 12 published case(s); the artefact run.json carries 2

  document coverage: no rendered document was supplied, so no numeral was checked
  1 error(s), 0 warning(s), 0 accepted
$ echo $?
1
```

That finding text is copied verbatim from the run. G1 and G2 both read `cases_published` and both
were satisfied by it: `12` is truthy, so the declaration was internally consistent. Only G3 opened
the file.

## The same manifest against a document that prints a fabricated headline

`report.html`:

```html
<html><body>
<h1>Serving on one machine</h1>
<p>Measured throughput reaches 240 tok/s, which is 80% of the decode roof, at a
time to first token p95 of 1.9 s.</p>
<p>The engine sustains 1,240 tok/s across 96 concurrent users.</p>
</body></html>
```

```
$ gpubench verify claims.json --rendered report.html
  Render blocked. Fix the errors, or re-measure. Never edit a measured value to satisfy a check.
  [ERROR] A5  the document prints 1,240 tok/s, which matches no claim value at that precision and no coverage.allow pattern. Context: 1.9 s. The engine sustains 1,240 tok/s across 96 concurrent users.
  [ERROR] A5  3 of 4 unit-bearing numerals in the rendered document trace to a claim (75.0%), below the 100.0% this manifest requires. Declare the missing ones as claims, or allow them in coverage.allow with a reason.

  document coverage: 3/4 unit-bearing numerals traced to a claim (75.0%, floor 100.0%), 0/1 bare numerals (0.0%, floor 0.0%), over 209 visible characters of the rendered document
  2 error(s), 0 warning(s), 0 accepted
```

The manifest did not change by one byte between the clean run and this one. Both fabrications came
from the audit that broke this gate: `1,240 tok/s` is caught, and `96 concurrent users` is **not**,
because it is a bare numeral and the default bare floor is zero. Add
`"coverage": {"min_unit_bearing_pct": 100.0, "min_bare_numeral_pct": 50.0}` and it is:

```
  [ERROR] A6  0 of 1 bare numerals trace to a claim (0.0%), below the 50.0% floor this manifest declares. Examples: 96 in 'e sustains 1,240 tok/s across 96 concurrent users. |'
```

---

# Why the gate reads the artifact

Two adversarial audits attacked this gate in one day. Both returned the verdict "defeated" with
evidence. Twenty-three holes, and every one of them was the same mistake in a different costume:

> **A check that reads a declaration instead of the artifact.**

Not twenty-three bugs. One bug, twenty-three times. What follows are the real reproductions, and
they are the reason no check in this file should ever be "simplified" back into reading a
declaration.

**The manifest is written by the same generator that writes the prose.** That single fact defeats
every manifest-only check by omission. Five fabricated headline figures were injected into a
report's abstract: `1,240 tok/s`, `96 concurrent users`, `$0.11 per million output tokens` and two
more. Not one appeared in any declared prose block. The build exited 0 and `claims.json` was
**byte-identical**. Separately, a stale `2,850` shipped in a section heading, in the table of
contents and in a `title=` tooltip, sitting beside a table that read 2182. A2 held jurisdiction
over 974 characters of declared prose; the document shipped 104,549. That is why A5 and A6 read
the rendered document, why A5 reads `title`, `alt` and `aria-label` attributes, and why the
coverage line prints on a pass.

**Asserting less scored better.** A manifest declaring ONE claim passed every check and printed a
*cleaner* log line (`1 claim(s), 0 warning(s)`) than the honest 197-claim one, while the body kept
the same wrong number. Evidence a manifest omits leaves no trace inside that manifest. That is why
A10 exists, why its only floor is the previous edition, why a baseline that cannot be parsed is a
hard error rather than "no baseline", and why the pass message now prints the counts of what was
declared.

**Renaming a field disarmed the gate.** Renaming `MANIFEST` in the content module removed the gate
entirely and the build still exited 0. That is why `run_claims_gate` distinguishes `absent` from
`incomplete` (one half of the `MANIFEST` + `claims()` pair is a wiring fault, not an opt-out), and
why the CLI refuses to publish an ungated build without `--allow-ungated`: an ungated build that
exits 0 cannot be told from a gated one by anything reading the exit code, and the exit code is all
a pipeline reads.

**A boolean was believed.** `figures[].table_view: true` was taken at face value, so a figure
shipped an empty `<details><summary>Table view</summary></details>` under a note telling the reader
its series was in the table view. The series was in neither the plot nor the table. F2 reads the
author's intention; F3 reads the outcome, in the document, and requires a cell with text in it.

**Strings nobody read back.** `arrival_model` and `gate.passed` were declarations. Flipping
`closed_loop` to `open_loop_poisson` on a fixed-in-flight harness shipped clean, with the honest
note `no independent arrival process` printed directly beside it. Hardcoding `passed: true` with
`cases_published: 999` shipped clean, because nothing ever opened the file the gate named. Hence
D7 (read the note beside the model), D6 (demand the evidence an open-loop run actually produces)
and G3 (open the artefact, recompute the verdict, compare).

**`kind` was the generator's free choice.** B1 recomputes only `derived` claims, so relabelling a
claim to `supplied` laundered a value printed as 3.0 whose own arithmetic gives 10.73. A9 now
demands either a derivation or a redeemable source, and a percentage or ratio that is not `derived`
must say in `derivation_waiver` why not.

**A run id was tested for truthiness.** A claim could name a run that does not exist, or the run
`" "`, and report nothing. A8 now strips and looks the id up in the run table.

**A guard was deleted with the claim it guarded.** The content module filtered its prose assertions
down to those whose operands still existed, so deleting a claim deleted its guard rather than
failing. Keep the assertion; A3 reports `compares unknown claims`.

**A warning was reworded into silence.** A label collision was a warning that fired only on
byte-identical labels: 2181.7 against 2850.0 landed as warning 29 of 29 and shipped, and rewording
a third label to say the same thing produced nothing. A7 is now an error, grouped by explicit
`quantity` id and by label token set, with the near-label heuristic left as a warning. And
`accepted_warnings` exists so the live warning list can honestly reach zero, with everything
knowingly waived still on the record beside it.

**The negative control passed for the wrong reason.** The open-loop probe's `fell_behind` judged
completion rate over a window that included the drain, so on a fake engine with unlimited
parallelism and zero queueing it reported the engine falling behind at 6%, 24%, 39% and 56% for
service times of 0.1, 0.5, 1 and 2 seconds. At the real machine's service times (2.18 s at
concurrency 1, 21.12 s at 64) every realistic level would have blamed a healthy engine. The test
that should have caught it used a fake serving in 5 ms, roughly 400 times faster than the real
engine, so it passed for the wrong reason. A control has to run at the magnitudes the real system
produces. `gpubench/probes/serving.py` now decides the verdict from a latency trend fitted over the
**arrival window only**, behind a significance gate and an effect-size gate, and it separates three
possible owners of a rate shortfall (the random draw, the generator, the engine) instead of blaming
the engine for all three. None of that is enforced by the manifest gate: it is upstream of it, and
it is here as the reason a gate is not the whole answer.

**The gate crashed on a correct declaration.** `check_load_shape` raised `TypeError` on an
open-loop level, because concurrency is `null` there by design, and its wave arithmetic
structurally demands a concurrency that an open-loop level does not have. The existing test passed
`"levels": []`, which is why it never showed. Coercing the `null` to 0 was worse than crashing: it
turned a correct declaration into an error saying the level declared no concurrency. Now every
load-shape cast goes through `as_int`, D3 skips open-loop levels entirely, and D6 asks the
questions that mode can answer.

**The two rules that fall out of all this.**

1. If a check can read the artifact, it must. The manifest is the standard; the document and the
   result file are the evidence. State the jurisdiction in the check's docstring so the next reader
   can see which one it holds.
2. Never edit a measured value to make a check pass. Two disagreeing numbers mean one of them is
   wrong about the machine, and overwriting either destroys the evidence of which. The four
   permitted responses are printed verbatim when the gate blocks (`BLOCKED_GUIDANCE`): fix the
   generator, fix the prose, re-measure, or declare the exception in the manifest where it is
   reviewable.

---

# What the gate still cannot do

Stated plainly, because a reader who over-trusts this gate is worse off than one who does not use
it.

**It cannot know whether a measurement is correct.** Every check in this catalogue compares the
document against the recorded measurements, or the manifest against itself. Nothing here measures
anything. A perfectly consistent report of a badly designed benchmark passes with zero findings.

**It cannot detect a well-formed lie in the result file.** G3 is the only check that opens an
external artefact, and it believes what it reads. A result file that records 100% accuracy over two
cases that were never run satisfies G1, G2 and G3 together. The artefact is a better authority than
a declaration because it is written earlier, by different code, and is harder to adjust
after the fact. It is not truth.

**It cannot audit itself.** Every hole listed above was found by an adversary, not by the test
suite, and several were found in checks whose tests passed. Two of them are instructive: a
verifier function that crashed on a valid input because its test supplied an empty list, and a
negative control that ran 400 times faster than the system it was controlling for. That is why the
attack suite exists, and why the right response to "the gate is green" is "what did it have
jurisdiction over", which is what the coverage line answers.

**It has live blind spots, named in this document rather than hidden.** The three most likely to
matter:

- `gate.passed` is read with `bool()`, so the string `"false"` reads as passing (G1).
- A6 cannot fire until `coverage.min_bare_numeral_pct` is declared above zero, so a bare fabricated
  count like `96 concurrent users` is uncovered by default (A5, A6).
- A malformed manifest can crash the verifier with a `KeyError` traceback rather than the
  documented exit 2. It fails closed, which is the right direction, and it reports nothing useful
  (`manifest`, A1, A3).

**A benchmark's bugs produce plausible numbers, not crashes.** That is the whole reason this file
exists, and the reason no gate replaces an adversarial reader. Every serious error in the work that
produced this tool looked like a fine result: a benchmark that ranked matrix sizes while appearing
to rank precisions, a weight size 0.6 GiB too large because the checkpoint carried a vision tower
the deployment never loads, a quality gate whose first run failed because the harness truncated the
model before it reached its answer. None of them raised an exception. Assume the first result is
wrong and go looking for the reason.

---

# Cross-check: this document against the code

Performed by grepping the two files for every id passed to a finding constructor, then checking
both directions. Both directions are clean.

**Every id in the code is documented here.** 29 ids in `gpubench/verify.py`
(`grep -oE '"(A|B|C|D|E|F|G)[0-9]+"|"manifest"'`, unique): `manifest`, A1, A2, A3, A4, A5, A6, A7,
A8, A9, B1, C1, C2, D1, D2, D3, D4, D5, D6, D7, E1, E2, F1, F2, F3, F4, G1, G2, G3. Plus A10 in
`gpubench/longform/__init__.py` (`check_declaration_floor`, `"check": "A10"`). Total 30, and this
document has an entry for each.

**Every id documented here exists in the code.** No entry above was written for an id that does not
appear in one of the two files. There is no A11, no B2, no C3, no D8, no E3, no F5 and no G4, and
none is documented.

**Not the same catalogue:** `gpubench/template/lint.py` uses ids L1 to L11 and a separate D1 to D12
table of historical defects. Those are a different engine over a different data contract and are
documented in `gpubench/template/lint-rules.md`. They are deliberately absent from this file, and
its D1 to D7 ids mean something else entirely.

Commands used to produce the quoted finding text:

```bash
gpubench verify --demo
gpubench verify claims.json
gpubench verify claims-defect.json
gpubench verify claims.json --rendered report.html
python -c "from gpubench.longform import check_declaration_floor; ..."   # A10, which verify does not run
```

Test suites covering these checks, all passing at the time of writing:

```
python -m tests.test_verify    87 tests
python -m tests.test_gate      67 tests
python -m tests.test_attacks   30 tests   # the 23 attacks that once landed, each still failing
```

The tests assert on `finding["check"]`, so the ids they name can be counted. Together the three
suites name **21 of the 30 ids**: `manifest`, A1, A3, A4, A5, A6, A7, A8, A9, A10, B1, D2, D3, D4,
D6, D7, E2, F2, F3, F4, G3.

The other 9 have **no id-named assertion** in any of the three: A2, C1, C2, D1, D5, E1, F1, G1, G2.
Five of them (A2, C1, E1, G1, G2) fire in the `--demo` fixture, which is a command a person runs
and not a test. Four (C2, D1, D5, F1) fire in neither the demo nor an id-named test, so their
behaviour in this document was established by reading the code and by the one-off reproductions
quoted above rather than by a suite. That gap is worth closing; see the note in this file's
companion `README.md`.


---

# What this gate does not defend against

Everything above describes what the gate checks. This section describes what it does not, and it
exists because a reference that only lists strengths is a sales document.

**The line runs between carelessness and intent.**

A **careless** author is what this gate stops, and it has stopped real ones. In this project's own
drafts it caught a clock ceiling of 2,895 MHz that appears in no artefact, a peak temperature stated
as 41 C where the sustained soak recorded 79, a stale headline sitting in a section heading beside a
table that disagreed with it, five chart axis ticks concatenated into one numeral, and a contents
entry read as a measurement once inline markup stopped becoming a space. Each of those was found by
the gate, not by a reader.

A **determined** author cannot be stopped by a check the author configures. The manifest is written
by the same person as the document, so any exemption the manifest can grant, that person can grant
themselves. Eight routes below follow from that and are documented rather than closed, because
closing one moves the exemption somewhere else rather than removing it. Three adversarial passes
against this gate each ended by finding a new variant of a route the previous one had closed.

**The mitigation that actually applies is not another check.** It is that the manifest diff between
two editions is small, structured and human-readable, and the declaration floor makes a manifest
that shrinks fail rather than pass. Read the diff. Every route below is visible in it as a
deliberate act: an added allowance, a removed formula, a widened tolerance, a new waiver.

## The eight

**R6, an equality group's tolerance is unbounded, and declaring the group also suppresses A7.**
A group says two claims should agree and states how closely. The author chooses that tolerance and
nothing bounds it, so a wide enough one makes any two values agree. Worse, declaring the group also
stops A7 reporting the collision for those keys, so a two-line object disarms the check that exists
to catch one quantity printed with two values. **Requires:** writing an equality group you know to
be false. **Visible as:** a new group, or a tolerance that grew.

**R7, F4 checks zero table cells when the manifest accepts the warning that says so.**
F4 maps declared table cells to rendered ones. Where the mapping cannot be made it warns, and an
accepted warning silences it, leaving no table-scoped check running. **Requires:** accepting a
warning whose text says the check did not run. **Visible as:** an `accepted_warnings` entry naming
F4.

**R10, deleting a formula removes a claim from recomputation.**
B1 recomputes any claim carrying inputs and a formula. A claim that declares neither is not
recomputed, so removing the formula while keeping the same evidence rung moves a value out of
reach by declaring less about it. **Requires:** deleting a formula that was there. **Visible as:**
a claim that lost its `formula` between editions.

**R12, an allowance can be broad enough to exempt a class.**
`coverage.allow` patterns are tested against decoy numerals and one matching all of them is
refused, which stops `.` but not `^\d+$`: that passes the decoys and exempts every plain integer in
the document. **Requires:** writing a pattern whose breadth you understand. **Visible as:** an
allowance whose exempted count is large; the gate prints that count per entry, so the evidence is
in the build log rather than hidden.

**R14, `gate.artifact_waiver` restores a downgrade.**
G3 reads a quality-gate result back out of the artefact that produced it and errors when the
manifest disagrees. A waiver turns that error into a warning, so a gate result nobody can check can
be published behind one line. **Requires:** writing a waiver for an artefact that exists.
**Visible as:** a waiver in the `gate` object.

**R15, an unknown printed unit drops the unit family.**
A numeral is matched against claims sharing its unit family. A unit the tool does not know has no
family, and the numeral is then matched against claim values generally, so a figure printed in an
invented unit can be covered by an unrelated claim. **Requires:** printing a measurement in a unit
the tool does not know while a claim happens to carry the same value. **Visible as:** nothing in
the manifest; this one is visible only in the document.

**R18, `table_view_shared_with` accepts a table from an unrelated figure.**
A figure may declare that its table view lives with another figure. F3 checks that the named figure
renders a non-empty table, not that the table holds the sharing figure's values. **Requires:**
naming a figure you know does not carry your rows. **Visible as:** a `table_view_shared_with` entry.

**R19, CSS generated content is not text.**
A `content:` declaration in a stylesheet renders on the page and is not in the document text, so no
numeral check sees it. **Requires:** writing a stylesheet rule that prints a figure. **Visible as:**
nothing in the manifest; a reviewer would have to read the stylesheet.

## What that adds up to

Two of the eight (R15, R19) are invisible in the manifest and would have to be caught by reading the
document or its stylesheet. The other six are visible as a specific, deliberate edit. So the honest
summary is:

> A number that no measurement supports cannot reach a published document by accident. It can be put
> there on purpose by whoever writes the manifest, and six of the eight ways to do it leave a mark in
> a diff a reviewer can read.

State it that way rather than "verified", which claims more than any gate its author configures can
deliver.
