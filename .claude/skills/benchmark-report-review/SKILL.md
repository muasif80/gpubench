---
name: benchmark-report-review
description: Review, score and gate a hardware or systems measurement report - GPU inference benchmarks, roofline analyses, throughput and latency studies, capacity reports. Runs a deterministic pre-render check over a claims manifest so a report with contradictory or stale numbers cannot be built, then does an adversarial review pass that scores fourteen weighted dimensions and returns ranked findings. Use this whenever someone asks you to review, critique, grade, evaluate, verify, sanity-check or fact-check a benchmark report or performance write-up; whenever you are about to publish or re-publish one of your own; whenever a build step needs to validate that a report's numbers agree with each other; and whenever someone asks how to automate benchmark quality checks or wire report verification into CI. Also use it when re-reviewing a later version of a report to check which earlier findings actually closed.
---

# Benchmark report review

A measurement report earns trust in two different ways, and they need different machinery.

**Its numbers must agree with each other.** Every derived figure rebuilds from its inputs, every
quantity has one value, every percentile has a sample behind it. This is mechanical, and a machine
should do it every single build, because humans are unreliable at exactly this and the failure is
silent: the report renders beautifully with a wrong number on the cover.

**Its numbers must be measuring the right thing.** The figure of merit is the one that matters,
the attribution survives rival explanations, the workload resembles something real, the caveat sits
where the reader meets the claim. No invariant catches this. It needs an adversary.

So this skill has three tiers. Run the one the situation calls for.

| Tier | What it does | Where it runs | Can it fail a build? |
|---|---|---|---|
| 1 | Deterministic checks over the report's own numbers | In the generator, before render | Yes, and it should |
| 2 | Structured review against a rubric, with findings | On a finished draft, before publishing | No, it advises |
| 3 | Adversarial reading by someone who did not write it | Before a version anyone else will act on | No, it argues |

---

## The rule that governs all three

**Never edit a measured value to make a check pass.** Not once, not to unblock a build, not
because the fix is obvious.

The permitted responses to a failing check are: fix the generator, fix the prose, re-measure, or
declare the exception explicitly in the manifest. If a number is wrong, the run that produced it
is what needs to change.

This matters more than it sounds. A verifier with edit access converges on a report that agrees
with itself perfectly and has quietly drifted away from what the machine did. That is a worse
artifact than an inconsistent one, because the inconsistency was the only visible symptom. When
you are operating this skill as an agent with file access, you may edit prose, formulas, manifest
metadata and generator code. Result files and measured values are read-only.

---

## Tier 1: the build gate

The checker ships inside the `gpubench` tool. Run it against a claims manifest emitted by the
generator:

```bash
gpubench verify claims.json \
    --previous claims-prev.json \
    --rendered report.html \
    --findings findings.json
```

Equivalently `python -m gpubench.verify claims.json ...` if the tool is not installed.

Exit 1 on any error. Wire it so the render does not happen: a report that fails verification
should not exist as a file, because a file is the thing that gets sent to people.

To see what it catches, run `gpubench verify --demo`. The fixture carries defects taken from real
editions of a real report, including a headline number that disagreed with itself by 12%, a
sentence asserting the opposite of its own table, and a level of a concurrency sweep where the
request count was not a whole multiple of the concurrency.

**The manifest is the part that needs building.** The checks need the report's numbers as data
with their unit, basis, provenance and formula attached, not as text to be regexed out of a
rendered page. If the generator already builds its tables from result JSON, this is a
serialisation step.

The highest-value single change is not a check at all: **stop putting bare numerals in prose.**
Every number in narrative text becomes a citation of a claim key, substituted at render time.
That one rule retires the entire class of values that go stale when a table is re-measured and
nobody greps the paragraphs.

### What the gate checks

| Group | Catches |
|---|---|
| **A** consistency | One quantity printed with two values; bare numerals in prose; a sentence asserting the opposite of its own table; a table blending runs without declaring it |
| **B** derivations | A printed figure that does not recompute from its declared inputs and formula |
| **C** bases and ratios | A ratio over mixed bases; a percentage whose denominator is unnamed or changes between uses |
| **D** load shape and sampling | Request counts that are not whole multiples of concurrency; levels with too few waves to be called sustained; percentiles whose ordered rank makes them an extreme wearing a percentile's name; an undeclared arrival process |
| **E** roofs | A fraction-of-roof computed against a shared-mode floor with no caveat where the reader meets it |
| **F** figures | A chart with no table view |
| **G** provenance and gates | A run referenced but not declared; a quality gate whose cases are unpublished |

---

## Tier 2: the review pass

Use this when reviewing a finished draft, yours or someone else's.

**Read the whole report first.** All of it, before forming a view. Reports of this kind bury the
qualifying sentence three sections away from the claim it qualifies, and a reviewer who skims will
report a finding the report already closed. That is expensive: it costs your credibility on the
findings that are real.

**Then recompute.** Take every derived figure and rebuild it from the report's own published
inputs. Do the arithmetic; do not eyeball it. This is where real findings come from, and it is
the part that distinguishes a review from an impression. Keep a log of every check and its result,
and publish the log with the review, including the ones that passed. A review claiming "the
numbers check out" is worth about what a benchmark claiming "it is fast" is worth.

Expect roughly one defect per twenty derivations in a careful report. If you find none, you are
probably re-reading the report's own arithmetic rather than rebuilding it independently.

**Then score.** Fourteen weighted dimensions, below. Two rules keep grading honest: a report
cannot score above 8.9 while a stated conclusion rests on an unverifiable claim, and acknowledging
a limitation does not retire it.

**Then write findings.** Severity by consequence, not by size of error.

### The rubric

Weights sum to 100. Keep them identical across reviews of the same report, or the movement means
nothing.

| Dimension | Weight | What earns a 9+ |
|---|---|---|
| Experimental design | 10 | Roofs before workload, components isolated, the right primitive, sweeps rather than point samples |
| Internal consistency | 10 | Every derived figure rebuilds from published inputs; no quantity has two values |
| Causal reasoning and attribution | 10 | Residual reported rather than absorbed; attribution distinguished from proof; the obvious remedy tested |
| Metric and figure-of-merit selection | 9 | Throughput bound to a latency percentile; a transferable efficiency metric; a quality gate that is not thin |
| Statistical rigour | 9 | Between-run and within-run variance measured; sample sizes disclosed; percentiles that resolve to a real tail |
| Configuration disclosure | 8 | Verbatim launch configuration, per-flag effect, full version stack |
| Reproducibility and artifact availability | 8 | The artifact is **obtainable**, not merely named and checksummed |
| Limitations and intellectual honesty | 8 | Prior claims retracted by name; own bugs published; a list of uses the report does not license |
| Workload representativeness | 7 | A real or sampled corpus; an arrival process resembling production |
| Scope and claim discipline | 6 | What is claimed, what is explicitly not, and the boundary between them |
| Data presentation | 5 | Every figure carries its table; no dual axes; direct labelling |
| Actionability | 5 | Recommendations tiered by cost, each with its risk, ordered by reversibility |
| External validity | 3 | More than one machine, engine, model or sample |
| Editorial economy | 2 | Length proportionate to the result |

Grade bands: 9.5+ exceptional, 9.0–9.4 A, 8.5–8.9 A−, 8.0–8.4 B+, below 8.0 B and down.

**Score absolutely, not relative to typical self-published work**, and say so. A 6.0 on external
validity means "6 against what external validity can be", not "worse than average".

### Questions that produce Tier 2 findings

Ordered by how often they find something.

1. Does every number that appears twice appear identically? Check the recommendations section
   against the measurement tables specifically: that is where stale copies live.
2. Can you rebuild each ceiling from the sweep the report printed, with a calculator?
3. What is the sample size behind each percentile, and what ordered sample does it resolve to?
4. What is the arrival process? Closed-loop harnesses cannot generate real tail latency.
5. Is the denominator of every percentage named, and is it the same denominator each time?
6. Does each quantity's basis travel with it: per device, per shard, total?
7. Which figures are labelled sustained, and for how long, and had the quantity stopped moving?
8. Is the residual of every reconstruction reported, or absorbed?
9. Is a causal claim doing work that only an attribution supports? What measurement would settle it?
10. Which recommendation rests on something never tested under the conditions it recommends?
11. Where does the reader first meet each claim, and is its caveat there or in an appendix?
12. What does the report say it published, and could a stranger actually obtain it?

---

## Tier 3: the adversarial pass

Tier 1 catches contradictions. Tier 2 catches things the rubric knows to ask about. Neither
catches the failure where the report is perfectly self-consistent and measuring the wrong thing,
because every invariant only fires on something the author already thought to encode.

The worked example: a benchmark whose every table agreed with every other table, whose derivations
all rebuilt exactly, and whose load generator was closed-loop while the report quoted p95 latencies
in MLPerf Server-scenario vocabulary. No cross-check would ever have surfaced it. It took a reader
asking what the load generator was actually doing.

So for a version anyone else will act on, run a pass that is deliberately not the author:

- Spawn it as a separate agent with **only** the report and the result files. No access to the
  authoring conversation, no summary of what changed, no list of known issues. Context that helps
  an author write is exactly the context that transmits their blind spots.
- Give it one instruction beyond the rubric: **find the assumption the report does not know it is
  making.** Not an error, an assumption. Ask what the harness is doing that the report never
  describes, what a reader would have to believe for the headline to mean what it says, and what
  a hostile expert in this specific subfield would open with.
- Have it argue against the central finding rather than audit it. If prefill is called
  interconnect-bound, what else produces that curve? If the answer is "nothing plausible", the
  attribution is strong and you can say so with evidence.

Then bring the result back and treat it as evidence, not as an attack.

---

## Re-reviewing a later version

Open with a table of every prior finding and its status: closed, partly closed, or open. Judge
closure against what the finding actually said, not against how much work went into the response.
A section retitled "Where to get the harness" that still does not say where is partly closed.

Keep the weights identical to the earlier review, or the movement means nothing. Re-verify what
changed, and check what did not for staleness, because a re-measurement is precisely where stale
copies get created.

Two things to be scrupulous about:

**Say when a new finding is something you missed.** A reviewer who lets their own earlier misses
pass as the author's new failures is not doing the job, and the author will notice.

**Credit the manner of the fix separately from the fix.** An author who root-causes a reported
discrepancy to a genuine bug in their own measurement tool, re-measures everything that bug could
have touched, and publishes the whole sequence with the reviewer's framing intact has demonstrated
something no rubric line captures. Say so, outside the score.

---

## Output

For a build gate: the checker's own output plus `findings.json`. Nothing else.

For a review: an overall weighted score and grade, the per-dimension scorecard with deltas if this
is a re-review, ranked findings, the verification log with its counts, and a short list of what
would move the grade, sized honestly. If the reviewed report is a substantial document and the
user has a way to publish, a single self-contained page beats a wall of terminal text, and the
scorecard is the thing that wants to be visual.

---

## Adapting this to another domain

The rubric's dimensions and the finding taxonomy transfer to any measurement report: a load test,
a database benchmark, a model evaluation, a latency study. What changes is Group B, the derivation
formulas, which are specific to what is being measured.

The parts that transfer unchanged are the ones worth keeping: one quantity has one value, nothing
derived is ever typed, percentiles carry their sample size, ratios carry their denominator,
ceilings carry the mode they were measured in, and prose cites keys rather than numerals.
