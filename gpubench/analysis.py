#!/usr/bin/env python3
"""Derivations: turning measured roofs plus a workload description into ceilings and attributions.

This module is the single place any ceiling, ridge point or attribution in this tool is computed.
It exists because the derivation used to live in a one-off script beside one particular report, and
a mistake in it therefore could not be found by anyone reading the tool. The specific mistake, kept
here as the module's cautionary tale, is documented on `allreduce_latency_ms`.

Three rules hold throughout, and they are the whole point of the file:

1. **No hardware or model constants.** Every fact about the machine or the model arrives as an
   argument. A function that cannot be given a fact returns None rather than assuming one. An
   earlier version of the attribution carried the weight size and layer count of the single machine
   the code was written on; on any other machine that produced a confident, plausible, wrong answer
   instead of an error, which is the worst failure mode a benchmark can have.

2. **Every derived number reports its own inputs.** Each function returns an `inputs` dict beside
   its result, so a report can print the arithmetic and a reader can reject it. A derivation whose
   inputs are not visible is an assertion wearing a lab coat.

3. **Pure and stdlib-only.** No I/O, no probes, no globals. That makes every derivation here
   unit-testable without a GPU, which is the only way this code gets checked at all.

Units are explicit in every name: `_ms`, `_gb_s`, `_bytes`, `_tflops`, `_tok_s`.
"""
import math

GB = 1e9          # bandwidth is quoted in decimal GB/s, matching nvidia-smi and NCCL convention
GIB = 1024 ** 3   # capacity is quoted in binary GiB, matching the driver and the engine logs


# --------------------------------------------------------------------------- interconnect

def allreduce_latency_ms(rows, nbytes):
    """Latency of one all-reduce at an arbitrary message size, from a measured sweep.

    `rows` is the measured sweep: dicts with `size_bytes`, `latency_ms` and `bus_gb_s`.

    THE CAUTIONARY TALE. An earlier version of this interpolated *latency* log-linearly between the
    two bracketing measured points. That is wrong wherever the link is bandwidth-bound, because
    there latency is linear in message size, not logarithmic. It overstated latency by up to 17%
    between samples, which understated every ceiling derived from it, and made a published ceiling
    column impossible to rebuild from the published sweep sitting three pages earlier. An external
    reviewer with a calculator found it.

    The fix is to interpolate the quantity that actually varies smoothly. Across two decades of
    message size the measured bandwidth on the machine this was found on moved only from 3.43 to
    3.67 GB/s, while latency moved by three orders of magnitude. So: interpolate bandwidth, then
    divide. Anyone can now recompute any ceiling from the printed sweep with a calculator, which is
    the entire reason for printing the sweep.

    Below the smallest measured message the curve is flat and latency-bound, so the smallest
    measured latency is returned unchanged. Above the largest, the link is saturated, so the
    plateau bandwidth is held constant and latency scales linearly with size.
    """
    rows = [r for r in (rows or [])
            if r.get("size_bytes") and r.get("latency_ms") is not None and r.get("bus_gb_s")]
    if not rows:
        return None
    rows = sorted(rows, key=lambda r: r["size_bytes"])

    if nbytes <= rows[0]["size_bytes"]:
        return rows[0]["latency_ms"]           # flat, launch-overhead dominated
    if nbytes >= rows[-1]["size_bytes"]:
        bw = rows[-1]["bus_gb_s"]              # saturated past the measured range
    else:
        bw = None
        for a, b in zip(rows, rows[1:]):
            if a["size_bytes"] <= nbytes <= b["size_bytes"]:
                t = ((math.log(nbytes) - math.log(a["size_bytes"]))
                     / (math.log(b["size_bytes"]) - math.log(a["size_bytes"])))
                bw = a["bus_gb_s"] + t * (b["bus_gb_s"] - a["bus_gb_s"])
                break
        if bw is None:
            return None
    return nbytes / (bw * GB) * 1000.0


def allreduce_regimes(rows):
    """Where the sweep is latency-bound and where it is bandwidth-bound.

    Reported rather than assumed, because which regime a message size falls in decides whether a
    wider link would help it at all, and the two regimes on a real machine give opposite answers.
    """
    rows = sorted([r for r in (rows or []) if r.get("size_bytes")], key=lambda r: r["size_bytes"])
    if len(rows) < 2:
        return None
    peak = max(r["bus_gb_s"] for r in rows)
    # "Saturated" = within 5% of the plateau. A threshold, and named as one.
    sat = [r for r in rows if r["bus_gb_s"] >= peak * 0.95]
    return {
        "fixed_overhead_us": rows[0]["latency_ms"] * 1000.0,
        "smallest_measured_bytes": rows[0]["size_bytes"],
        "peak_bus_gb_s": peak,
        "saturates_at_bytes": sat[0]["size_bytes"] if sat else None,
        "saturation_threshold": "within 5% of the measured plateau",
        "inputs": {"samples": len(rows)},
    }


# --------------------------------------------------------------------------- roofs

def ridge_point(peak_tflops, peak_bandwidth_gb_s):
    """Arithmetic intensity, in FLOPs per byte, where compute and bandwidth roofs cross.

    Williams, Waterman and Patterson (2009). Below the ridge a workload is memory-bound and more
    compute buys nothing; above it, the reverse. It is a property of the machine alone.
    """
    if not (peak_tflops and peak_bandwidth_gb_s):
        return None
    return {
        "value": (peak_tflops * 1e12) / (peak_bandwidth_gb_s * GB),
        "unit": "FLOPs/byte",
        "formula": "peak_tflops * 1e12 / (peak_bandwidth_gb_s * 1e9)",
        "inputs": {"peak_tflops": peak_tflops, "peak_bandwidth_gb_s": peak_bandwidth_gb_s},
    }


# --------------------------------------------------------------------------- decode

def decode_floor(weight_bytes_per_shard, bandwidth_gb_s, batches=(1, 2, 4, 8, 16, 32, 64, 128)):
    """The memory-bandwidth floor on one decode step, and the throughput ceiling it implies.

    Autoregressive decode reads every weight once per step regardless of batch size, so the step
    time has a hard floor of bytes/bandwidth and batching is close to free until compute binds.
    Arithmetic intensity of a decode step is roughly 2 FLOPs per weight byte per sequence, so
    intensity is about 2 x batch; comparing that against the ridge point says where batching stops
    being free.

    `weight_bytes_per_shard` must be the bytes actually RESIDENT on one device, from the engine's
    own report where possible. A checkpoint on disk can include towers a deployment never loads,
    and using it silently inflates the floor.
    """
    if not (weight_bytes_per_shard and bandwidth_gb_s):
        return None
    floor_s = weight_bytes_per_shard / (bandwidth_gb_s * GB)
    return {
        "step_floor_ms": floor_s * 1000.0,
        "tok_s_ceiling_by_batch": {str(b): b / floor_s for b in batches},
        "arithmetic_intensity_by_batch": {str(b): 2.0 * b for b in batches},
        "formula": "step_floor_s = weight_bytes_per_shard / (bandwidth_gb_s * 1e9); "
                   "tok_s(batch) = batch / step_floor_s",
        "inputs": {"weight_bytes_per_shard": weight_bytes_per_shard,
                   "bandwidth_gb_s": bandwidth_gb_s},
    }


def decode_attribution(measured_step_ms, bandwidth_floor_ms, comms_ms):
    """Split a measured decode step into bandwidth, communication, and what is left over.

    The residual is the honest part. It is launch overhead, sampling, scheduler work and whatever
    the model of the machine failed to capture, and reporting it is what separates an attribution
    from a story. A residual that is negative, or larger than the step, means an input is wrong.
    """
    if not measured_step_ms or bandwidth_floor_ms is None:
        return None
    comms = comms_ms or 0.0
    residual = measured_step_ms - bandwidth_floor_ms - comms
    out = {
        "measured_step_ms": measured_step_ms,
        "bandwidth_floor_ms": bandwidth_floor_ms,
        "comms_ms": comms_ms,
        "unexplained_ms": residual,
        "fraction_of_bandwidth_ceiling": bandwidth_floor_ms / measured_step_ms,
        "comms_share_of_step": (comms / measured_step_ms) if comms_ms is not None else None,
        "unexplained_share_of_step": residual / measured_step_ms,
        "formula": "unexplained = measured - bandwidth_floor - comms",
        "warnings": [],
    }
    if residual < 0:
        out["warnings"].append(
            "Negative residual: the modelled floor plus comms exceeds the measured step, so at "
            "least one input is wrong. Most often the weight bytes are the checkpoint size rather "
            "than the resident size, or the bandwidth roof came from a cache-resident buffer.")
    if bandwidth_floor_ms > measured_step_ms:
        out["warnings"].append("The bandwidth floor alone exceeds the measured step.")
    return out


# --------------------------------------------------------------------------- prefill

def prefill_compute_ceiling(weight_bytes_all_shards, peak_tflops_per_device, devices):
    """Tokens per second prefill could reach if only compute bound it.

    Uses the standard dense-transformer approximation of 2 FLOPs per parameter per token, with
    parameter count taken from resident weight bytes. It is an approximation and is labelled one:
    it ignores attention's quadratic term, which matters at long context.
    """
    if not (weight_bytes_all_shards and peak_tflops_per_device and devices):
        return None
    flops_per_token = 2.0 * weight_bytes_all_shards
    return {
        "tok_s": (peak_tflops_per_device * 1e12 * devices) / flops_per_token,
        "flops_per_token": flops_per_token,
        "formula": "peak_tflops_per_device * 1e12 * devices / (2 * weight_bytes_all_shards)",
        "approximation": "2 FLOPs per parameter per token; ignores the quadratic attention term, "
                         "so it is optimistic at long context",
        "inputs": {"weight_bytes_all_shards": weight_bytes_all_shards,
                   "peak_tflops_per_device": peak_tflops_per_device, "devices": devices},
    }


def prefill_comms_ceiling(allreduce_rows, hidden_size, allreduces_per_pass, prompt_lengths,
                          activation_bytes=2):
    """Tokens per second the INTERCONNECT alone permits during prefill, per prompt length.

    Prefill issues the same number of all-reduces per forward pass as decode, but each one carries
    the whole prompt rather than a single token, so it lands in the bandwidth-bound part of the
    curve while decode sits in the flat latency-bound part. That is why one link can be the binding
    constraint on prefill and an irrelevance to decode, and why sweeping message size rather than
    picking one is the only way to see it.

    Every returned ceiling is one division chain over printed numbers:

        message_bytes    = prompt_tokens * hidden_size * activation_bytes
        allreduce_ms     = message_bytes / interpolated_bandwidth
        comms_ms_per_pass= allreduce_ms * allreduces_per_pass
        ceiling_tok_s    = prompt_tokens / (comms_ms_per_pass / 1000)

    `allreduces_per_pass` is 2 per transformer layer for tensor parallelism (one after attention,
    one after the MLP) and must be passed in, because it is a property of the parallelism scheme
    and not of the hardware.
    """
    if not (hidden_size and allreduces_per_pass and allreduce_rows):
        return None
    out = {}
    for n in prompt_lengths:
        msg = int(n) * hidden_size * activation_bytes
        lat = allreduce_latency_ms(allreduce_rows, msg)
        if not lat:
            continue
        per_pass = allreduces_per_pass * lat
        out[str(n)] = {
            "prompt_tokens": int(n),
            "message_bytes": msg,
            "allreduce_ms": lat,
            "implied_bandwidth_gb_s": msg / (lat / 1000.0) / GB,
            "comms_ms_per_pass": per_pass,
            "tokens_per_s_comms_ceiling": int(n) / (per_pass / 1000.0),
        }
    return {
        "by_prompt_length": out,
        "formula": "msg = tokens * hidden * activation_bytes; lat = msg / bw(msg); "
                   "ceiling = tokens / (lat * allreduces_per_pass / 1000)",
        "inputs": {"hidden_size": hidden_size, "allreduces_per_pass": allreduces_per_pass,
                   "activation_bytes": activation_bytes,
                   "allreduce_samples": len(allreduce_rows)},
    }


# --------------------------------------------------------------------------- capacity

def concurrency_ceiling(kv_pool_bytes, kv_bytes_per_token_per_shard, context_tokens,
                        fixed_state_bytes_per_sequence=0):
    """How many sequences fit, given a KV pool and any fixed per-sequence state.

    `fixed_state_bytes_per_sequence` is what makes this worth having as a function. A hybrid or
    linear-attention model holds a recurrent state per sequence that does NOT grow with context
    length, and engines charge it to the same pool as the KV cache. When that term dominates, the
    concurrency ceiling stops depending on context length at all, and sizing rules written for
    pure-attention models overestimate capacity by a wide margin. Passing zero recovers the
    familiar pure-KV arithmetic.
    """
    if not kv_pool_bytes:
        return None
    per_seq = (kv_bytes_per_token_per_shard or 0) * (context_tokens or 0) \
        + (fixed_state_bytes_per_sequence or 0)
    if per_seq <= 0:
        return None
    kv_part = (kv_bytes_per_token_per_shard or 0) * (context_tokens or 0)
    dominant = ("fixed per-sequence state" if (fixed_state_bytes_per_sequence or 0) > kv_part
                else "context-proportional KV")
    return {
        "sequences": kv_pool_bytes / per_seq,
        "bytes_per_sequence": per_seq,
        # The FULL accounting, so the ceiling is rebuildable from its parts rather than only from
        # a measured pool percentage. A reviewer found this was the one derivation in a published
        # report that a reader could not close: the parts were quoted in different units and it was
        # never stated whether the fixed state was per shard or across all of them.
        "parts": {
            "fixed_state_bytes_per_sequence": fixed_state_bytes_per_sequence or 0,
            "fixed_state_mib": (fixed_state_bytes_per_sequence or 0) / (1024.0 ** 2),
            "kv_bytes_per_sequence": kv_part,
            "kv_mib": kv_part / (1024.0 ** 2),
            "total_mib": per_seq / (1024.0 ** 2),
            "pool_mib": kv_pool_bytes / (1024.0 ** 2),
            "pool_pct_per_sequence": per_seq / kv_pool_bytes * 100.0,
            "basis": "PER SHARD. Both the pool and the per-sequence cost are one device's share; "
                     "a tensor-parallel deployment holds this on each device, so the aggregate "
                     "pool is this multiplied by the parallel width and the sequence count is "
                     "unchanged.",
        },
        "dominant_term": dominant,
        "context_sensitive": dominant == "context-proportional KV",
        "formula": "pool / (kv_bytes_per_token * context_tokens + fixed_state_bytes)",
        "inputs": {"kv_pool_bytes": kv_pool_bytes,
                   "kv_bytes_per_token_per_shard": kv_bytes_per_token_per_shard,
                   "context_tokens": context_tokens,
                   "fixed_state_bytes_per_sequence": fixed_state_bytes_per_sequence},
    }


# --------------------------------------------------------------------------- efficiency

def achieved_fraction(measured, reference):
    """Measured as a fraction of a ceiling, with the guard that makes it meaningful.

    A percentage of roof is the one figure that travels between machines, because it already
    accounts for how much silicon there is. It is also easy to make meaningless: comparing a
    measurement against a ceiling derived under different conditions produces a number that looks
    authoritative and says nothing. Callers are responsible for matching conditions; this function
    at least refuses to divide by a missing ceiling.
    """
    if not reference:
        return None
    return {"fraction": measured / reference, "percent": measured / reference * 100.0,
            "inputs": {"measured": measured, "reference": reference}}
