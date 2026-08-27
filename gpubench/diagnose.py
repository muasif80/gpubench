#!/usr/bin/env python3
"""Turn measurements into stated conclusions, or into a stated inability to conclude.

WHY THIS MODULE EXISTS.

The probes in this tool measure well. What they did not do was ACCOUNT for what they measured. A
previous run recorded that the serving engine's prefix-cache counters read zero, which is true and
useful, and then stopped. Explaining that reading (separating "the cache was consulted and missed"
from "the cache was never running" from "the counter is broken") took a person reading engine
source inside a container for the better part of a day. All three look identical from outside, and
they license completely different recommendations: one of them turned a change previously described
as "the cheapest possible win" into something needing a maintenance window and carrying real risk.

That investigation was mechanical. Every step of it was a rule over data the tool either already had
or could fetch in one request. So it belongs here, where it runs every time, for free, on every
machine, rather than being rediscovered by whoever next notices an odd number.

THE THREE RULES THIS MODULE FOLLOWS.

1. **A finding names its evidence.** Every conclusion carries the readings it rests on, so a reader
   can reject it. A diagnosis without its evidence is an opinion with a lab coat on.

2. **"I cannot tell" is a first-class outcome, and it must be LOUD.** Most of these rules can reach
   three states, not two: confirmed, ruled out, and undetermined-because-an-input-is-missing. The
   third is the one that matters. A diagnostic that silently passes when it could not look is the
   same defect class the whole tool exists to prevent, and it is worse than no diagnostic because it
   reads as an all-clear.

3. **Never upgrade "did not" into "cannot".** The investigation this module encodes produced exactly
   that error on its first pass: it concluded the engine *could not* prefix-cache the architecture,
   when the evidence only supported that the engine *did not* by default and would if asked. One
   word, and it inverted a recommendation. Where a rule can only establish the weaker claim, it says
   the weaker claim and names the check that would settle the stronger one.

Pure and stdlib-only, like analysis.py: every rule is testable without a device.
"""

# Severity is about what a reader should DO, not about how alarming a finding sounds.
#   blocking  a number in the report is wrong or unsupportable; fix before publishing
#   warning   a number is right but easy to misread; it needs its caveat printed beside it
#   info      a fact worth stating that a reader would otherwise have to work out
#   unknown   a check could not run; says what is missing and what to supply
SEVERITIES = ("blocking", "warning", "info", "unknown")


def _f(d, *path, **kw):
    """Nested get that tolerates missing links, returning kw['default'] (None) if any is absent."""
    cur = d
    for k in path:
        if isinstance(cur, list):
            if not isinstance(k, int) or k >= len(cur):
                return kw.get("default")
            cur = cur[k]
        elif isinstance(cur, dict):
            if k not in cur:
                return kw.get("default")
            cur = cur[k]
        else:
            return kw.get("default")
    return cur


def finding(rule, severity, headline, detail, evidence=None, action=None, weaker_claim=None):
    assert severity in SEVERITIES, severity
    f = {"rule": rule, "severity": severity, "headline": headline, "detail": detail,
         "evidence": evidence or {}}
    if action:
        f["action"] = action
    if weaker_claim:
        # Present when the evidence supports a weaker statement than a reader might jump to. See
        # rule 3 in the module docstring: this field exists because collapsing "did not" into
        # "cannot" once inverted a recommendation.
        f["do_not_overstate"] = weaker_claim
    return f


def _probe(res, name):
    return (res.get("probes") or {}).get(name) or {}


# --------------------------------------------------------------------------- rules

def d_prefix_cache(res):
    """Explain the prefix-cache counters, which is the reading that motivated this module.

    Three readings, three conclusions, and they are not interchangeable:
      queries > 0            -> the cache ran; the hit rate means what it says
      queries == 0, APC off  -> the cache never ran; no measurement here can be a cache read
      queries == 0, APC on   -> the counters are not incrementing; something is wrong
      no counters at all     -> cannot tell; cache defeat rests on construction, not measurement
    """
    sb = _probe(res, "serve_bench")
    cfg = _probe(res, "engine_config")
    apc = _f(cfg, "normalised", "prefix_caching_enabled")

    q = h = None
    for lv in (sb.get("levels") or []):
        smd = lv.get("server_metrics_delta") or {}
        if smd.get("prefix_cache_queries") is not None:
            q = (q or 0) + smd["prefix_cache_queries"]
            h = (h or 0) + smd["prefix_cache_hits"]

    ev = {"queries_during_benchmark": q, "hits_during_benchmark": h,
          "resolved_prefix_caching_enabled": apc,
          "recurrent_cache_mode": _f(cfg, "normalised", "recurrent_cache_mode"),
          "recurrent_block_size": _f(cfg, "normalised", "recurrent_block_size"),
          "block_size": _f(cfg, "normalised", "block_size")}

    if q is None:
        return finding(
            "prefix-cache", "unknown",
            "Cannot tell whether a prefix cache affected these measurements.",
            "This engine exposed no prefix-cache counters, so the claim that repeated prefixes "
            "could not be served from cache rests on how the load generator builds prompts (a "
            "unique leading salt per request) rather than on a measurement. That is a weaker claim "
            "and should be reported as one: a salt appended rather than prepended would look "
            "identical and defeat nothing.",
            ev, action="Run the engine_config probe and confirm the counter names this engine uses.")

    if q > 0:
        pct = h / q * 100.0
        sev = "warning" if pct > 1.0 else "info"
        return finding(
            "prefix-cache", sev,
            "Prefix cache was active: %.1f%% hit rate during the benchmark." % pct,
            ("A non-zero hit rate means some prefill was served from cache rather than computed, so "
             "prefill throughput here is overstated by roughly that fraction and is not a clean "
             "measurement of the model." if pct > 1.0 else
             "The cache was consulted and essentially always missed, which is what a correctly "
             "salted load generator should produce. Prefill figures are clean."),
            ev,
            action=("Add a unique prefix at the START of every prompt, not the end, and re-run."
                    if pct > 1.0 else None))

    # q == 0. The interesting case, and the one that needs the resolved config to explain.
    if apc is False:
        return finding(
            "prefix-cache", "info",
            "The prefix cache was never consulted: zero queries, not zero hits.",
            "The engine resolved prefix caching to off, so no measurement in this run can be a "
            "cache read. This is a stronger and different statement from a 0% hit rate, and the "
            "two are easy to confuse: a 0% hit rate would mean the cache was asked and had nothing. "
            "Note also what this does NOT establish. An engine that has caching off by default is "
            "not an engine that refuses it. Whether it can be turned on here, and whether it would "
            "help, is a separate question this reading cannot answer.",
            ev,
            action="If prefill is a constraint, enabling the cache is worth an experiment. Treat it "
                   "as a change with a rollback, not a tuning step: it needs an engine restart.",
            weaker_claim="Say 'off by default on this deployment', not 'unsupported' or 'cannot'. "
                         "Establishing 'cannot' needs an attempt to enable it that fails.")

    if apc is True:
        return finding(
            "prefix-cache", "blocking",
            "Prefix caching is enabled but the counters never incremented.",
            "The engine reports caching on, yet zero queries were recorded across the whole "
            "benchmark. One of these is wrong. Either the counters this tool scrapes are not the "
            "ones this engine increments, or the requests took a path that bypasses the cache. "
            "Until it is resolved, neither 'the cache was defeated' nor 'the cache was inactive' "
            "may be claimed.",
            ev,
            action="Compare the counter names in the engine's metrics output against the names the "
                   "serving probe scrapes, and check whether the request payload disables caching.")

    return finding(
        "prefix-cache", "unknown",
        "Zero cache queries, and no resolved configuration to explain them.",
        "The counters exist and read zero, which could mean the cache is off or that it is on and "
        "not being exercised. Those license different recommendations and this run cannot "
        "distinguish them.",
        ev, action="Run the engine_config probe against the same endpoint.")


def d_roof_mode(res):
    """A shared-mode roof is a floor, and every percentage derived from it inherits that."""
    mode = res.get("mode")
    if mode == "exclusive":
        return finding("roof-mode", "info", "Roofs were measured with the device to itself.",
                       "Peak figures are peaks, and achieved-fraction percentages mean what they "
                       "say.", {"mode": mode})
    if mode == "shared":
        return finding(
            "roof-mode", "warning",
            "Roofs are FLOORS, not peaks: measured with other work resident.",
            "Another workload held the device during these measurements, so every roof is a lower "
            "bound on what the hardware can do. Consequently every 'percentage of roof' in this "
            "run is an UPPER bound on efficiency: the true denominator is larger, so the true "
            "fraction is smaller. This propagates to every comparison against a datasheet.",
            {"mode": mode},
            action="Re-run in exclusive mode during a maintenance window to convert floors into "
                   "peaks. Until then, label every achieved-fraction as an upper bound.")
    return finding("roof-mode", "unknown", "Measurement mode was not recorded.",
                   "Without knowing whether the device was shared, a roof cannot be interpreted as "
                   "either a peak or a floor, and no achieved-fraction is meaningful.",
                   {"mode": mode}, action="Record 'mode' at the top level of the result.")


def d_attribution(res, attribution=None):
    """Check a step attribution adds up, and explain it when it does not."""
    att = attribution or {}
    if att.get("unavailable"):
        return finding("attribution", "unknown", "Step attribution was not computed.",
                       att["unavailable"], {},
                       action="Supply the missing deployment facts named in the message.")
    if not att.get("measured_step_ms"):
        return finding("attribution", "unknown", "No step attribution in this result.",
                       "Nothing to check.", {})
    resid = att.get("unexplained_ms")
    ev = {k: att.get(k) for k in ("measured_step_ms", "bandwidth_floor_ms", "comms_ms",
                                  "unexplained_ms")}
    if resid is not None and resid < 0:
        return finding(
            "attribution", "blocking",
            "Attribution is impossible: the modelled components exceed the measured step.",
            "A negative residual means at least one input is wrong, not that the machine is fast. "
            "The most common cause by a wide margin is using the checkpoint size on disk as the "
            "weight bytes instead of the bytes the engine actually made resident: a checkpoint can "
            "contain towers a deployment never loads. The second most common is a bandwidth roof "
            "measured on a buffer small enough to sit in cache.",
            ev,
            action="Take resident weight bytes from the engine's own startup report, and confirm "
                   "the bandwidth roof used a working set larger than last-level cache.")
    share = (resid / att["measured_step_ms"]) if resid is not None else None
    if share is not None and share > 0.35:
        return finding(
            "attribution", "warning",
            "Most of the step is unexplained (%.0f%%)." % (share * 100),
            "The model of the machine accounts for less than two thirds of the measured step. That "
            "is worth reporting as-is rather than presenting the attribution as complete: a large "
            "residual usually means a real cost is missing from the model, not that overhead is "
            "large.",
            ev, action="Look for a cost the model omits before quoting the breakdown.")
    return finding("attribution", "info",
                   "Attribution accounts for %.0f%% of the measured step."
                   % ((1 - (share or 0)) * 100),
                   "Components sum to the measurement, with the remainder reported rather than "
                   "distributed. The residual is launch overhead, scheduling and whatever the model "
                   "failed to capture.", ev)


def d_power(res):
    """Power-bound is a claim the driver itself can settle; do not infer it from a mean."""
    t = _probe(res, "torch_compute")
    sus = t.get("sustained") or []
    pw = [d["power"] for d in sus if (d.get("power") or {}).get("samples")]
    if not pw:
        return finding("power", "unknown", "No sustained power sampling in this run.",
                       "Whether the device is power-limited cannot be established from burst "
                       "measurements: a sampler slower than the kernels it is timing cannot resolve "
                       "them, and a low reading is then an artefact rather than a finding.",
                       {}, action="Run the sustained compute measurement with power sampling.")
    samples = sum(p["samples"] for p in pw)
    capped = sum(p.get("sw_power_cap_active_samples", 0) for p in pw)
    mean = sum(p["power_mean_w"] for p in pw) / len(pw)
    hw = sum(p.get("hw_slowdown_active_samples", 0) for p in pw)
    tmax = max(p.get("temp_max_c") or 0 for p in pw)
    ev = {"mean_w": round(mean, 1), "busy_samples": samples, "cap_active_samples": capped,
          "hw_slowdown_samples": hw, "temp_max_c": tmax}
    frac = capped / samples if samples else 0
    if hw:
        return finding("power", "warning", "Hardware slowdown was active: this device is throttling.",
                       "A hardware slowdown flag means thermal or electrical protection engaged, "
                       "which is a different and more serious condition than sitting at a power cap. "
                       "Figures from these samples are not representative of healthy operation.",
                       ev, action="Investigate cooling and power delivery before using these numbers.")
    if frac > 0.9:
        return finding(
            "power", "info", "Power-bound: the driver reported its cap active in %.0f%% of busy "
            "samples." % (frac * 100),
            "Throughput here is whatever the power budget buys, not what the silicon or the cooling "
            "allows. Peak temperature was %.0f C, so this is not thermal. The distinction matters "
            "because it changes the remedy: more cooling buys nothing, and a raised power limit "
            "buys throughput directly." % tmax,
            ev)
    if frac > 0.1:
        return finding("power", "info", "Intermittently power-capped (%.0f%% of samples)."
                       % (frac * 100),
                       "The cap engages under some conditions and not others, so figures depend on "
                       "which. Worth reporting alongside any throughput number from this run.", ev)
    return finding("power", "info", "Not power-limited during this run.",
                   "The cap was essentially never active, so throughput was bounded by something "
                   "else. Mean draw %.0f W." % mean, ev)


def d_device_parity(res):
    """Identical devices on non-identical paths: the finding that is invisible in a single number."""
    inv = _probe(res, "inventory")
    links = inv.get("pcie_links") or inv.get("links") or []
    if len(links) < 2:
        return finding("device-parity", "unknown", "Fewer than two devices, or no link topology.",
                       "Asymmetry between devices cannot be assessed.", {"links": len(links)})
    widths = {}
    for l in links:
        key = (l.get("bridge_max_speed"), l.get("bridge_max_width"))
        widths.setdefault(key, []).append(l.get("bdf"))
    if len(widths) == 1:
        return finding("device-parity", "info", "All devices sit on equivalent links.",
                       "No slot asymmetry to account for.", {"link_classes": 1})
    return finding(
        "device-parity", "warning",
        "Devices sit on NON-EQUIVALENT links (%d distinct classes)." % len(widths),
        "The devices may be identical and still perform differently, and the difference will appear "
        "only in workloads that move data between them or to the host. A single aggregate number "
        "hides this entirely. Any multi-device scaling figure from this machine should be read as a "
        "property of the board, not of the silicon.",
        {"link_classes": {str(k): v for k, v in widths.items()}},
        action="Measure device-to-device collectives explicitly, swept across message size, rather "
               "than assuming symmetry.")


def d_workload_disclosure(res):
    """A benchmark that cannot say what it sent cannot be reproduced."""
    sb = _probe(res, "serve_bench")
    wl = sb.get("workload")
    if not sb:
        return finding("workload", "unknown", "No serving benchmark in this run.",
                       "Nothing to disclose.", {})
    if not wl:
        return finding(
            "workload", "blocking", "The workload is undisclosed.",
            "The result records what came back and not what was sent. Prompt content, size control "
            "and uniqueness policy all change the numbers, so a result without them is not "
            "reproducible and its throughput figures cannot be compared with anyone else's.",
            {}, action="Record the corpus, size-control method and uniqueness policy in the probe "
                       "output.")
    ratios = []
    for lv in (sb.get("levels") or []):
        n, req, act = lv.get("requests_ok"), lv.get("input_tokens_requested"), lv.get("input_tokens")
        if n and req and act:
            ratios.append((act / float(n)) / req)
    if not ratios:
        return finding("workload", "warning", "Workload described, but size control unverified.",
                       "The corpus is disclosed but the requested size was never checked against "
                       "what the engine actually processed, so the size control is an assumption.",
                       {"kind": wl.get("kind")},
                       action="Record the engine's own input-size counter per level.")
    worst = max(abs(r - 1) for r in ratios) * 100
    ev = {"kind": wl.get("kind"), "levels_checked": len(ratios),
          "worst_deviation_pct": round(worst, 3)}
    if worst > 5:
        return finding("workload", "warning",
                       "Requested and actual input size differ by up to %.1f%%." % worst,
                       "Size control is drifting enough to matter. Any per-unit figure computed "
                       "from the REQUESTED size is wrong by that much, and any derived quantity "
                       "proportional to size inherits it.", ev,
                       action="Compute every derived figure from the engine's count, not the "
                              "requested value.")
    return finding("workload", "info",
                   "Workload disclosed and size control verified to within %.2f%%." % worst,
                   "Both sides of any size-dependent ratio can be put on the engine's own count.",
                   ev)


def d_reproducibility(res):
    """A best-of-N with no spread is a measurement of luck."""
    sb = _probe(res, "serve_bench")
    spread = sb.get("between_run_spread") or {}
    if not spread:
        return finding("reproducibility", "warning", "Run-to-run variation was not measured.",
                       "Without it there is no way to tell a real difference from noise, so no "
                       "comparison in this result can be called significant.", {},
                       action="Repeat at least one level and record the spread.")
    worst = max((v.get("cov_pct") or 0) for v in spread.values())
    ev = {"levels": len(spread), "worst_cov_pct": round(worst, 3)}
    if worst > 5:
        return finding("reproducibility", "warning",
                       "Run-to-run variation is high (%.1f%% CoV)." % worst,
                       "Differences smaller than roughly %.0f%% between two runs of this "
                       "configuration are not distinguishable from noise." % (worst * 2), ev)
    return finding("reproducibility", "info",
                   "Reproducible: worst run-to-run variation %.2f%%." % worst,
                   "Any difference larger than about %.1f%% between two runs of this configuration "
                   "is a real change." % (worst * 2), ev)


def d_quality_gate(res):
    """A gate whose cases are unpublished is an assertion."""
    acc = _probe(res, "accuracy")
    sm = acc.get("summary") or {}
    cases = _f(acc, "method", "cases_published")
    if not sm:
        return finding(
            "quality-gate", "warning", "No quality gate ran beside the performance measurements.",
            "A speed benchmark cannot distinguish faster from worse, so throughput can always be "
            "bought by degrading output and nothing here would notice.", {},
            action="Run a determinism and correctness gate alongside the performance work.")
    ev = {"cases": sm.get("cases"), "verdict": sm.get("verdict"),
          "exact_match_pct": sm.get("exact_match_pct"),
          "determinism_pct": sm.get("determinism_pct"), "cases_published": bool(cases)}
    if not cases:
        return finding("quality-gate", "warning",
                       "Quality gate reports %s, but its cases are not published."
                       % sm.get("verdict"),
                       "A verdict nobody can re-run is an assertion rather than an artefact. The "
                       "cases cost nothing to include and are what make the gate falsifiable.", ev,
                       action="Publish the full case list in the result.")
    if sm.get("verdict") != "PASS":
        return finding("quality-gate", "blocking",
                       "Quality gate did not pass: %s." % sm.get("verdict"),
                       "Performance figures from this run describe a stack that is not answering "
                       "correctly, which makes them meaningless rather than merely caveated. Check "
                       "the gate itself first: a gate that fails on its own truncation or timeout "
                       "is worse than no gate.", ev,
                       action="Confirm the gate is sound, then investigate the stack.")
    return finding("quality-gate", "info", "Quality gate passed, with its cases published.",
                   "Determinism and correctness held, and the gate is re-runnable by a reader.", ev)


def d_provenance(res):
    """Every value should be attributable to a run, and comparability should be checkable."""
    fp = res.get("fingerprint") or {}
    ev = {"fingerprint": fp.get("hash"), "inputs": fp.get("inputs"),
          "schema_version": res.get("schema_version"), "profile": res.get("profile")}
    if not fp.get("hash"):
        return finding("provenance", "warning", "No comparability fingerprint in this result.",
                       "Without it, nothing stops this result being compared against one taken "
                       "under different conditions, which is the most common way benchmark numbers "
                       "mislead.", ev, action="Emit a fingerprint over the parameters that must "
                                              "match before two results may be compared.")
    return finding("provenance", "info", "Comparability fingerprint present: %s." % fp["hash"],
                   "Two results may be compared when their fingerprints match, and must not be "
                   "otherwise. The inputs are recorded so the rule can be audited.", ev)


def d_sampling(res):
    """Percentiles need their sample size, and levels need whole waves.

    Two failures with one cause: a level whose request count is not a whole multiple of its
    concurrency spends its tail at reduced concurrency, which depresses throughput by an amount
    that depends on how badly the count divides. It therefore shows up at some levels and not
    others and reads as scatter. In one published report it put 233 tok/s in one table and 204 in
    another for the same nominal level, a 12% gap on the primary figure of merit.
    """
    sb = _probe(res, "serve_bench")
    levels = sb.get("levels") or []
    if not levels:
        return finding("sampling", "unknown", "No serving levels in this run.", "Nothing to check.",
                       {})
    # .get throughout: a rule that raises on an incomplete level is worse than one that reports
    # what it could not check. This crashed on a fixture during its own first test run.
    partial = [l.get("concurrency") for l in levels if l.get("whole_waves") is False]
    no_n = [l.get("concurrency") for l in levels
            if not l.get("sample_count") and l.get("ttft_s")]
    small = [(l.get("concurrency"), l.get("sample_count")) for l in levels
             if 0 < (l.get("sample_count") or 0) < 20 and l.get("ttft_s")]
    ev = {"levels": len(levels), "partial_wave_levels": partial,
          "levels_without_sample_count": no_n,
          "levels_with_fewer_than_20_samples": small}
    if partial:
        return finding(
            "sampling", "blocking",
            "Levels %s ran a PARTIAL final wave." % partial,
            "The request count is not a whole multiple of the concurrency at these levels, so the "
            "tail ran at reduced concurrency and their throughput is understated. The error is "
            "invisible in the numbers and appears only at levels where the count fails to divide, "
            "so it reads as scatter rather than as a fault.",
            ev, action="Round the request count up to a whole multiple of each concurrency.")
    if no_n:
        return finding("sampling", "warning", "Percentiles are reported without a sample size.",
                       "A p95 of eight samples is the second-worst value, not a tail estimate, and "
                       "a reader cannot tell which from the number alone.", ev,
                       action="Record sample_count and duration_s beside every percentile.")
    if small:
        return finding(
            "sampling", "warning",
            "Percentiles rest on fewer than 20 samples at %d level(s)." % len(small),
            "p95 is barely distinguishable from the maximum at these sample sizes. Report the "
            "count beside the percentile so a reader can weight it, and prefer more requests per "
            "level to more levels when time is limited.", ev)
    return finding("sampling", "info",
                   "Every level ran whole waves and reports its sample size.",
                   "Throughput is not depressed by a partial tail, and every percentile can be "
                   "weighted by the number of samples behind it.", ev)


def _open_loop_levels(res):
    """The open-loop levels of the serving sweep, with the fields the two rules below read.

    A level is open loop when it says so, not when it happens to have a rate: closed-loop levels
    have no arrival process and nothing here applies to them.
    """
    sb = _probe(res, "serve_bench")
    out = []
    for lv in (sb.get("levels") or []):
        arr = lv.get("arrival") or {}
        if arr.get("model") != "open_loop_poisson":
            continue
        out.append({
            "rate": arr.get("target_rate_req_s"),
            # The new name, falling back to the deprecated one so a document written by an older
            # probe is still read rather than silently treated as having no verdict.
            "grew": (arr["latency_grew_over_the_level"]
                     if "latency_grew_over_the_level" in arr else arr.get("fell_behind")),
            "basis": (arr.get("latency_grew_over_the_level_basis")
                      or arr.get("fell_behind_basis")),
            "capacity": arr.get("engine_did_not_keep_up"),
            "capacity_basis": arr.get("engine_did_not_keep_up_basis"),
            "generator_kept_up": arr.get("generator_kept_up"),
            "truncated": arr.get("truncated_by_harness_limit"),
            "dispatched": arr.get("requests_dispatched"),
            "ok": lv.get("requests_ok"),
            "unaccounted": lv.get("requests_unaccounted"),
            "censored": _f(arr, "queue_growth", "n_censored"),
            "fraction": _f(arr, "queue_growth", "completion_fraction"),
            "floor": _f(arr, "queue_growth", "completion_fraction_floor"),
            "engine_queue_grew": _f(arr, "queue_growth", "engine_side", "engine_queue_grew"),
        })
    return out


def _rates(levels):
    return [lv["rate"] for lv in levels]


def d_open_loop_verdict(res):
    """Did the engine keep up, and is that even a question this run asked?

    The open-loop probe produces the only verdict in this tool that a capacity decision would rest
    on, and until this rule existed nothing downstream read it: a source-level count over the
    report and the diagnostics found zero occurrences of fell_behind, generator_kept_up or
    queue_growth. A production open-loop run therefore published a report that could not say
    whether the engine kept up.

    Three states, and they are not interchangeable:
      latency did not grow            -> nothing here says the engine was short of capacity
      latency grew, engine queue too  -> a capacity limit at that offered rate
      latency grew, engine queue flat -> something slowed every request; the offered rate is NOT
                                         shown to be the cause, and this engine may be shared
      not judged                      -> loud, first-class, and never rendered as an all-clear
    """
    sb = _probe(res, "serve_bench")
    levels = _open_loop_levels(res)
    if not sb:
        return finding("open-loop", "unknown", "No serving benchmark in this run.",
                       "Nothing measured how requests arrived, so nothing can say whether the "
                       "engine kept up with them.", {},
                       action="Run the serving probe with --arrival poisson --rate.")
    if not levels:
        return finding(
            "open-loop", "info", "No open-loop level: this run never asked whether the engine "
            "keeps up.",
            "Every level here was closed loop, where the generator issues its next request only "
            "when a previous one completes. Such a harness throttles itself exactly when a real "
            "arrival stream would not, so it cannot build a queue and its latency percentiles are "
            "optimistic by construction. That is a property of the measurement, not a fault, but "
            "no number in this run supports a statement about sustained arrival rate.",
            {"open_loop_levels": 0, "levels": len(sb.get("levels") or [])},
            action="Re-run with --arrival poisson --rate to measure sustained rate.")

    not_judged = [lv for lv in levels if lv["grew"] is None]
    grew = [lv for lv in levels if lv["grew"] is True]
    capacity = [lv for lv in grew if lv["capacity"] is True]
    unattributed = [lv for lv in grew if lv["capacity"] is not True]
    voided_gen = [lv for lv in levels if lv["generator_kept_up"] is False]
    truncated = [lv for lv in levels if lv["truncated"]]
    ev = {"open_loop_levels": len(levels),
          "rates_not_judged": _rates(not_judged),
          "rates_where_latency_grew": _rates(grew),
          "rates_with_a_confirmed_engine_queue": _rates(capacity),
          "rates_where_growth_was_not_attributed": _rates(unattributed),
          "rates_voided_by_the_generator": _rates(voided_gen),
          "rates_truncated_by_the_harness": _rates(truncated),
          "reasons_not_judged": [lv["basis"] for lv in not_judged]}

    if not_judged:
        return finding(
            "open-loop", "unknown",
            "%d of %d open-loop level(s) reached NO verdict." % (len(not_judged), len(levels)),
            "These levels measured latencies and cannot say whether they were growing. Each one "
            "names its own reason: the generator missed its own schedule (so the arrivals were a "
            "catch-up burst and not the process the level names), the harness truncated the level, "
            "too few requests to test a trend, or too few of the dispatched requests came back to "
            "support the negative answer. A level in this state is not a level that passed. Its "
            "latency percentiles describe the requests that returned, which on an overloaded "
            "engine are the fast ones, so read them as a LOWER bound."
            + ("" if not capacity else
               " Separately, %d level(s) DID confirm an engine-side queue: see "
               "rates_with_a_confirmed_engine_queue." % len(capacity)),
            ev,
            action="Raise the client timeout above the tail you expect, or lower the offered rate, "
                   "and re-run the levels named in rates_not_judged.")

    if capacity:
        return finding(
            "open-loop", "warning",
            "The engine queued work at %s req/s." % ", ".join(str(r) for r in _rates(capacity)),
            "At these offered rates per-request latency grew across the level AND the engine's own "
            "waiting count grew with it, which is the pair that makes this a capacity statement "
            "rather than a latency observation. Rates at or above the lowest of them are not "
            "sustained rates for this deployment, and any service level quoted from those levels "
            "describes a queue that was still growing when the level ended.",
            ev,
            action="Size for the highest rate whose latency did not grow, and re-measure that rate "
                   "for longer than one queue's worth of arrivals.",
            weaker_claim="Say the engine queued at THIS offered rate with THIS workload. It is "
                         "not established that the engine cannot serve this rate: a different "
                         "prompt mix, batch configuration or co-tenant load changes the answer.")

    if unattributed:
        return finding(
            "open-loop", "warning",
            "Latency grew at %s req/s, and the cause is not established."
            % ", ".join(str(r) for r in _rates(unattributed)),
            "Requests got slower through these levels. What is NOT shown is that the rate offered "
            "here caused it: either the engine's own running/waiting split was unavailable, or it "
            "was available and showed no queue. An engine shared between environments produces "
            "exactly this reading when a co-tenant takes the machine, and so does a thermal or "
            "power event. Sizing hardware for these rates would be sizing for something that was "
            "measured but not diagnosed.",
            ev,
            action="Re-run with the engine's metrics endpoint reachable, and check what else was "
                   "resident on the device during the level.",
            weaker_claim="Say 'latency grew over the level', not 'the engine did not keep up'. The "
                         "second names a cause the measurement does not identify.")

    return finding(
        "open-loop", "info",
        "Latency did not grow at any offered rate (%s req/s)."
        % ", ".join(str(r) for r in _rates(levels)),
        "Every open-loop level was judged, and in each one per-request latency was flat across the "
        "arrival window. A queue that is not growing leaves latency flat however deep the in-flight "
        "count is, so these rates were absorbed rather than merely survived. Note the stated "
        "sensitivity limit in the probe's own output: the effect gate sits at half the largest "
        "value its statistic can take, so a slow ramp can pass it.",
        ev)


def d_open_loop_coverage(res):
    """How much of the offered load is actually behind the open-loop verdicts.

    The defect this rule exists for: the growth fit ran on requests that SUCCEEDED, so a client
    timeout removed exactly the requests that prove a queue, and the tighter the timeout the more
    certainly an overloaded engine was reported as fine. The probe now books errored requests into
    the fit as censored observations and refuses the negative verdict below a stated completion
    floor. This rule surfaces the same numbers to a reader, because a level judged on half its
    requests is not the same evidence as a level judged on all of them.
    """
    levels = _open_loop_levels(res)
    if not levels:
        return finding("open-loop-coverage", "unknown", "No open-loop level to account for.",
                       "Completion coverage is a property of an arrival process; a closed-loop "
                       "level has none.", {"open_loop_levels": 0})
    have = [lv for lv in levels if lv["fraction"] is not None]
    leaked = [lv for lv in levels if lv["unaccounted"]]
    ev = {"open_loop_levels": len(levels),
          "levels_reporting_completion_fraction": len(have),
          "rates_with_unaccounted_requests": _rates(leaked),
          "per_level": [{"rate": lv["rate"], "dispatched": lv["dispatched"], "ok": lv["ok"],
                         "censored_in_fit": lv["censored"], "completion_fraction": lv["fraction"]}
                        for lv in levels]}
    if leaked:
        return finding(
            "open-loop-coverage", "blocking",
            "Requests were dispatched and produced no outcome at all.",
            "At these rates the level's own accounting does not balance: a dispatched request "
            "produced neither a result, nor an engine error, nor a harness error. Every rate in "
            "such a level is understated by an unknown amount and nothing else in the document "
            "shows it.",
            ev, action="Fix the accounting before reading any rate from these levels.")
    if not have:
        return finding(
            "open-loop-coverage", "unknown",
            "The open-loop levels do not report how many of their requests completed.",
            "Without the completion fraction there is no way to tell a level judged on all its "
            "requests from one judged on the few that beat a client timeout, and those support "
            "very different conclusions.",
            ev, action="Re-run with a probe that records queue_growth.completion_fraction.")
    thin = [lv for lv in have
            if lv["floor"] is not None and lv["fraction"] < lv["floor"]]
    censored = [lv for lv in levels if (lv["censored"] or 0) > 0]
    if thin:
        return finding(
            "open-loop-coverage", "warning",
            "%d level(s) completed less than %.0f%% of the requests they offered."
            % (len(thin), 100.0 * max(lv["floor"] for lv in thin)),
            "The requests that did not come back are the ones that waited longest, so what is left "
            "is a biased sample of the fast ones. Their durations are booked into the trend as "
            "censored observations, which are LOWER bounds on the wait, not measurements of it. "
            "Every latency percentile from these levels is a lower bound, and the probe refuses "
            "the negative verdict on them for the same reason.",
            ev,
            action="Raise the client timeout above the tail you expect, or lower the offered rate, "
                   "then re-run these levels.",
            weaker_claim="Say the level did not complete enough requests to judge. Do not say the "
                         "engine failed: a harness-side ceiling produces the same shortfall.")
    if censored:
        return finding(
            "open-loop-coverage", "info",
            "Every level cleared the completion floor, with some censored requests in the fit.",
            "Requests the engine never finished are counted at their harness-timed duration, which "
            "is a lower bound on the wait, so the trend is if anything understated. The counts are "
            "in the evidence so a reader can weight the verdicts by how much of the offered load "
            "each one saw.", ev)
    return finding(
        "open-loop-coverage", "info",
        "Every open-loop request that was dispatched came back.",
        "No censoring, no unaccounted requests, so each verdict rests on the whole of the load its "
        "level offered.", ev)


RULES = (d_prefix_cache, d_roof_mode, d_power, d_device_parity, d_workload_disclosure,
         d_reproducibility, d_quality_gate, d_provenance, d_sampling,
         d_open_loop_verdict, d_open_loop_coverage)


def diagnose(res, attribution=None):
    """Run every rule. Returns findings ordered by how much a reader needs to act on them.

    Nothing here is optional or best-effort. A rule that cannot run returns an 'unknown' finding
    naming what was missing, so the count of things this tool declined to conclude is always
    visible. A diagnostics pass that produced no output would be indistinguishable from one that
    found nothing wrong.
    """
    out = [r(res) for r in RULES]
    out.append(d_attribution(res, attribution))
    order = {s: i for i, s in enumerate(SEVERITIES)}
    out.sort(key=lambda f: (order[f["severity"]], f["rule"]))
    return {
        "findings": out,
        "summary": {s: sum(1 for f in out if f["severity"] == s) for s in SEVERITIES},
        "note": "Findings are conclusions with their evidence attached, not warnings. 'unknown' "
                "means a check could not run and says what would let it: those are the ones worth "
                "reading first, because a check that did not look is not an all-clear.",
    }
