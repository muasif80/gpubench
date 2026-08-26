#!/usr/bin/env python3
"""Tier-1 probe: compute and memory microbenchmarks through an existing PyTorch runtime.

Run it inside whatever already has torch and CUDA (a serving container, a venv). It is designed
to be piped in on stdin, so nothing is installed on the target:

    docker exec -i <container> python3 - < torch_compute.py

Covers the precisions a vendor datasheet advertises, so a report can state achieved-versus-
published for each rather than for a convenient subset:

    FP8 (e4m3)  torch._scaled_mm     FP4 (e2m1)  torch._scaled_mm, block-scaled
    BF16 / FP16 torch.matmul         INT8        torch._int_mm
    TF32        matmul, tf32 on      FP32 shader matmul, tf32 OFF (non-tensor path)

Also measures what a burst benchmark cannot: a sustained run at a fixed duration with power
sampled throughout, which is the only way to get steady-state throughput and efficiency per watt.

Environment:
  GPUBENCH_VRAM_MB     scratch budget per device            (default 800)
  GPUBENCH_MIN_FREE_MB abort a device below this            (default 1200)
  GPUBENCH_ITERS       timed iterations per measurement     (default 5)
  GPUBENCH_DEVICES     comma list of device indices         (default all)
  GPUBENCH_ONLY        sections: matmul,membw,host,p2p,sustained (default all)
  GPUBENCH_PHYSICAL    report this physical index (for CUDA_VISIBLE_DEVICES-pinned runs)
  GPUBENCH_SUSTAIN_S   seconds for the sustained run        (default 20)
  GPUBENCH_MODE        shared | exclusive                   (default shared)
"""
import json
import os
import subprocess
import sys
import threading
import time

MB = 1024 * 1024


def env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


VRAM_MB = env_int("GPUBENCH_VRAM_MB", 800)
MIN_FREE_MB = env_int("GPUBENCH_MIN_FREE_MB", 1200)
ITERS = env_int("GPUBENCH_ITERS", 5)
SUSTAIN_S = env_int("GPUBENCH_SUSTAIN_S", 20)
MODE = os.environ.get("GPUBENCH_MODE", "shared")
ONLY = [s.strip() for s in os.environ.get(
    "GPUBENCH_ONLY", "matmul,membw,host,p2p,sustained").split(",") if s.strip()]
PHYSICAL = os.environ.get("GPUBENCH_PHYSICAL", "")

out = {
    "probe": "torch_compute", "tier": 1, "mode": MODE,
    "started_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "config": {"vram_budget_mb": VRAM_MB, "iters": ITERS, "sections": ONLY,
               "sustain_seconds": SUSTAIN_S},
    "devices": [], "matmul": [], "memory_bandwidth": [], "cache_sweep": [],
    "host_transfer": [], "p2p": [], "sustained": [], "errors": [],
}


def want(section):
    return section in ONLY


def emit(code=0):
    out["finished_at_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    print(json.dumps(out, indent=2))
    sys.exit(code)


try:
    import torch
except Exception as exc:  # noqa: BLE001
    out["errors"].append("import torch failed: %r" % (exc,))
    emit(1)

out["torch_version"] = torch.__version__
out["cuda_version"] = torch.version.cuda
if not torch.cuda.is_available():
    out["errors"].append("torch.cuda.is_available() is False")
    emit(1)


def label(d):
    return int(PHYSICAL) if PHYSICAL != "" else d


all_devices = list(range(torch.cuda.device_count()))
sel = os.environ.get("GPUBENCH_DEVICES", "")
devices = ([int(x) for x in sel.split(",") if x.strip()] if sel.strip() else all_devices)

for d in all_devices:
    p = torch.cuda.get_device_properties(d)
    free, total = torch.cuda.mem_get_info(d)
    out["devices"].append({
        "index": label(d), "name": p.name,
        "capability": "%d.%d" % (p.major, p.minor),
        "sm_count": p.multi_processor_count,
        "l2_cache_bytes": getattr(p, "L2_cache_size", None),
        "total_mib": round(total / MB), "free_mib": round(free / MB),
        "selected": d in devices,
    })


def time_cuda(fn, device, iters=ITERS, warmup=3):
    """Median wall time via CUDA events. Warmup is mandatory, not polite: an idle GPU sits in a
    low power state and an unwarmed measurement times the clock ramp."""
    torch.cuda.set_device(device)
    for _ in range(warmup):
        fn()
    torch.cuda.synchronize(device)
    s = []
    for _ in range(iters):
        a = torch.cuda.Event(enable_timing=True)
        b = torch.cuda.Event(enable_timing=True)
        a.record()
        fn()
        b.record()
        torch.cuda.synchronize(device)
        s.append(a.elapsed_time(b) / 1000.0)
    s.sort()
    n = len(s)
    mean = sum(s) / n
    # Coefficient of variation across the timed iterations. A best-of-N figure alone hides
    # whether the run was stable; two runs agreeing is encouragement, not a variance estimate.
    var = sum((x - mean) ** 2 for x in s) / (n - 1) if n > 1 else 0.0
    sd = var ** 0.5
    return {"best_s": s[0], "median_s": s[n // 2], "worst_s": s[-1],
            "mean_s": mean, "stdev_s": sd,
            "cov_pct": (sd / mean * 100.0) if mean else 0.0, "samples": n}


def free_mib(d):
    return torch.cuda.mem_get_info(d)[0] / MB


def n_for(budget_mb, bytes_per_elem, out_bytes=None):
    """Largest square n whose operands AND output fit the budget.

    out_bytes matters: torch._int_mm writes int32 from int8 inputs, and the FP4 path writes
    bfloat16 from 4-bit inputs. Sizing on the input width alone undershoots the real allocation
    by up to 2x, which on a production GPU is an OOM rather than a rounding error.
    """
    out_bytes = bytes_per_elem if out_bytes is None else out_bytes
    per_elem = 2 * bytes_per_elem + out_bytes
    n = int(((budget_mb * MB) / float(per_elem)) ** 0.5)
    return max(1024, min((n // 256) * 256, 16384))


# Widest element in play decides the one size every precision is ALSO measured at. Without a
# common n, "achieved % of reference" ranks matrix sizes while appearing to rank precisions:
# a 1-byte type gets a 16384 GEMM and a 4-byte type gets 8192, and the bigger GEMM simply
# reaches higher utilisation. Cross-precision statements may only use the common-n records.
def common_n_for(budget_mb):
    return n_for(budget_mb, 4, out_bytes=4)


def gpu_state(index):
    """Clock and throttle reasons at this instant.

    Reference TFLOPS are a clock-dependent quantity quoted at boost. Comparing a measurement
    taken at an unrecorded clock against a spec at a different clock is not a comparison, so
    every record carries the clock it was taken at.
    """
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=clocks.sm,power.draw,temperature.gpu,"
             "clocks_event_reasons.sw_power_cap,clocks_event_reasons.hw_slowdown",
             "--format=csv,noheader,nounits", "-i", str(index)],
            capture_output=True, text=True, timeout=3)
        p = [x.strip() for x in r.stdout.strip().split(",")]
        if len(p) >= 5:
            return {"sm_clock_mhz": float(p[0]), "power_w": float(p[1]),
                    "temp_c": float(p[2]), "sw_power_cap": p[3], "hw_slowdown": p[4]}
    except Exception:  # noqa: BLE001
        pass
    return None



def dedupe_sizes(own_n, common_n):
    """(n, comparable) pairs: the precision's own best-fit size, plus the shared size.

    Returns one entry when they coincide. `comparable=True` marks the record that may be used in
    a cross-precision comparison; the other is the best this precision achieves given its own
    memory footprint.
    """
    if own_n == common_n:
        return [(own_n, True)]
    return [(own_n, False), (common_n, True)]


def record_matmul(d, dtype_name, n, t, note=None, comparable=False):
    flops = 2.0 * n * n * n
    rec = {"device": label(d), "dtype": dtype_name, "n": n,
           "comparable": comparable,
           "tflops_best": flops / t["best_s"] / 1e12,
           "tflops_median": flops / t["median_s"] / 1e12,
           "tflops_mean": flops / t["mean_s"] / 1e12 if t.get("mean_s") else None,
           "cov_pct": t.get("cov_pct"), "samples": t.get("samples"),
           "median_s": t["median_s"],
           "state_at_measurement": gpu_state(label(d))}
    if note:
        rec["note"] = note
    out["matmul"].append(rec)


# ---------------------------------------------------------------- power sampling

class PowerSampler(threading.Thread):
    """Samples nvidia-smi in a thread during a sustained run.

    A 2 Hz sampler cannot characterise a 5 ms kernel, which is why burst benchmarks cannot report
    power honestly. Pair this with the sustained section, where the GPU is busy for seconds.
    """

    def __init__(self, index, hz=10):
        threading.Thread.__init__(self)
        self.index = index
        self.interval = 1.0 / hz
        self.samples = []
        self._halt = threading.Event()   # NOT _stop: Thread._stop is an internal method
        self.daemon = True

    def run(self):
        q = ["nvidia-smi",
             "--query-gpu=power.draw,clocks.sm,temperature.gpu,utilization.gpu,"
             "clocks_event_reasons.sw_power_cap,clocks_event_reasons.hw_slowdown",
             "--format=csv,noheader,nounits", "-i", str(self.index)]
        while not self._halt.is_set():
            t0 = time.time()
            try:
                r = subprocess.run(q, capture_output=True, text=True, timeout=2)
                parts = [x.strip() for x in r.stdout.strip().split(",")]
                if len(parts) >= 4:
                    rec = [float(x) for x in parts[:4]]
                    rec += [parts[4] if len(parts) > 4 else "",
                            parts[5] if len(parts) > 5 else ""]
                    self.samples.append(tuple(rec))
            except Exception:  # noqa: BLE001
                pass
            # Subtract the time the subprocess took, or the advertised rate is not the real one:
            # querying then waiting a full interval gave 7.8 Hz when 10 Hz was claimed.
            elapsed = time.time() - t0
            self._halt.wait(max(0.0, self.interval - elapsed))

    def stop(self):
        self._halt.set()

    def summary(self):
        if not self.samples:
            return None
        busy = [s for s in self.samples if s[3] > 20] or self.samples
        p = [s[0] for s in busy]
        c = [s[1] for s in busy]
        t = [s[2] for s in busy]
        capped = sum(1 for s in busy if len(s) > 4 and s[4] == "Active")
        slowed = sum(1 for s in busy if len(s) > 5 and s[5] == "Active")
        return {"samples": len(self.samples), "busy_samples": len(busy),
                "power_mean_w": sum(p) / len(p), "power_max_w": max(p),
                "sm_clock_mean_mhz": sum(c) / len(c), "sm_clock_min_mhz": min(c),
                "temp_max_c": max(t),
                # The authoritative driver signal for "power-bound". Without it the claim is
                # inference from a number near the cap, not evidence.
                "sw_power_cap_active_samples": capped,
                "hw_slowdown_active_samples": slowed,
                "sample_rate_hz": None}


# ---------------------------------------------------------------- per device

DENSE = [("bfloat16", torch.bfloat16, 2), ("float16", torch.float16, 2)]

for d in devices:
    if free_mib(d) < MIN_FREE_MB:
        out["errors"].append("device %d skipped: only %.0f MiB free" % (label(d), free_mib(d)))
        continue
    torch.cuda.set_device(d)
    # Proportional, like the driver tier: never take more than a third of what is free, so a
    # co-resident workload that grows during the run still has room. A fixed floor alone is not
    # enough, and the true transient peak exceeds the nominal budget because the fp8/fp4 paths
    # build wider temporaries before converting, and the caching allocator holds slack.
    free_now = free_mib(d)
    budget = min(VRAM_MB, max(0.0, free_now - MIN_FREE_MB / 2.0), free_now * 0.33)

    if want("matmul"):
        cn = common_n_for(budget)
        for name, dtype, elem in DENSE:
          for n, comparable in dedupe_sizes(n_for(budget, elem), cn):
            try:
                a = torch.randn(n, n, device=d, dtype=dtype)
                b = torch.randn(n, n, device=d, dtype=dtype)
                record_matmul(d, name, n, time_cuda(lambda: torch.matmul(a, b), d),
                              comparable=comparable)
                del a, b
            except Exception as exc:  # noqa: BLE001
                out["errors"].append("matmul %s dev %d: %r" % (name, label(d), exc))
            finally:
                torch.cuda.empty_cache()

        # TF32 and FP32 are the same call with a different backend flag. Measuring both separates
        # the tensor-core path from the plain shader path, which the datasheet lists separately.
        for name, tf32 in (("tf32", True), ("float32_shader", False)):
          for n, comparable in dedupe_sizes(n_for(budget, 4, out_bytes=4), cn):
            try:
                prev = torch.backends.cuda.matmul.allow_tf32
                torch.backends.cuda.matmul.allow_tf32 = tf32
                a = torch.randn(n, n, device=d, dtype=torch.float32)
                b = torch.randn(n, n, device=d, dtype=torch.float32)
                record_matmul(d, name, n, time_cuda(lambda: torch.matmul(a, b), d),
                              note="tensor cores" if tf32 else "non-tensor FP32 path",
                              comparable=comparable)
                del a, b
                torch.backends.cuda.matmul.allow_tf32 = prev
            except Exception as exc:  # noqa: BLE001
                out["errors"].append("matmul %s dev %d: %r" % (name, label(d), exc))
            finally:
                torch.cuda.empty_cache()

        # INT8. Reported in TOPS; the arithmetic is identical to the FLOP count.
        for n, comparable in dedupe_sizes(n_for(budget, 1, out_bytes=4), cn):
          try:
            a = torch.randint(-8, 7, (n, n), device=d, dtype=torch.int8)
            b = torch.randint(-8, 7, (n, n), device=d, dtype=torch.int8).t().contiguous().t()
            record_matmul(d, "int8", n, time_cuda(lambda: torch._int_mm(a, b), d),
                          note="TOPS, not TFLOPS; output is int32", comparable=comparable)
            del a, b
          except Exception as exc:  # noqa: BLE001
            out["errors"].append("int8 dev %d unavailable: %r" % (label(d), exc))
          finally:
            torch.cuda.empty_cache()

    # FP8 and FP4 go through cuBLASLt, which fails on a second device inside one process. The
    # caller runs these pinned with CUDA_VISIBLE_DEVICES; see the runner.
    if want("matmul"):
        cn = common_n_for(budget)
        for n, comparable in dedupe_sizes(n_for(budget, 1, out_bytes=2), cn):
          try:
            fp8 = torch.float8_e4m3fn
            a = torch.randn(n, n, device=d, dtype=torch.bfloat16).to(fp8)
            b = torch.randn(n, n, device=d, dtype=torch.bfloat16).t().contiguous().t().to(fp8)
            sc = torch.tensor(1.0, device=d)
            record_matmul(d, "float8_e4m3fn", n,
                          time_cuda(lambda: torch._scaled_mm(a, b, scale_a=sc, scale_b=sc,
                                                             out_dtype=torch.bfloat16), d),
                          comparable=comparable)
            del a, b
          except Exception as exc:  # noqa: BLE001
            out["errors"].append("fp8 dev %d unavailable: %r" % (label(d), str(exc)[:160]))
          finally:
            torch.cuda.empty_cache()

        for n, comparable in dedupe_sizes(n_for(budget, 1, out_bytes=2), cn):
          try:
            if not hasattr(torch, "float4_e2m1fn_x2"):
                raise RuntimeError("torch build has no float4_e2m1fn_x2")
            fp4 = torch.float4_e2m1fn_x2
            # NVFP4 is block-scaled: two values per byte, one e4m3 scale per 16-element block.
            a = torch.randint(0, 255, (n, n // 2), device=d, dtype=torch.uint8).view(fp4)
            b = torch.randint(0, 255, (n, n // 2), device=d, dtype=torch.uint8).view(fp4)
            sa = torch.ones((n, n // 16), device=d, dtype=torch.float8_e4m3fn)
            sb = torch.ones((n, n // 16), device=d, dtype=torch.float8_e4m3fn)
            record_matmul(d, "float4_e2m1", n,
                          time_cuda(lambda: torch._scaled_mm(a, b.t(), scale_a=sa, scale_b=sb,
                                                             out_dtype=torch.bfloat16), d),
                          note="NVFP4 block-scaled, TOPS", comparable=comparable)
            del a, b, sa, sb
          except Exception as exc:  # noqa: BLE001
            out["errors"].append("fp4 dev %d unavailable: %r" % (label(d), str(exc)[:200]))
          finally:
            torch.cuda.empty_cache()

    if want("membw"):
        try:
            buf_mb = int(min(budget / 3.0, 512))
            elems = int(buf_mb * MB / 2)
            src = torch.empty(elems, device=d, dtype=torch.float16).fill_(1.0)
            dst = torch.empty_like(src)
            t = time_cuda(lambda: dst.copy_(src), d)
            moved = 2.0 * elems * 2
            out["memory_bandwidth"].append({
                "device": label(d), "test": "device_copy", "buffer_mib": buf_mb,
                "gb_s_best": moved / t["best_s"] / 1e9,
                "gb_s_median": moved / t["median_s"] / 1e9})
            del src, dst
            torch.cuda.empty_cache()

            # Sweep the cache boundary so the report shows a curve, not a point.
            l2_mb = (out["devices"][d].get("l2_cache_bytes") or 0) / MB
            for mb in (1, 4, 16, 64, 128, 256, 512):
                if mb * 2 > budget:
                    break
                e = int(mb * MB / 2)
                s2 = torch.empty(e, device=d, dtype=torch.float16).fill_(1.0)
                d2 = torch.empty_like(s2)
                t = time_cuda(lambda: d2.copy_(s2), d, iters=9)
                out["cache_sweep"].append({
                    "device": label(d), "size_mib": mb,
                    "fits_in_l2": bool(l2_mb and mb <= l2_mb),
                    "gb_s_best": (2.0 * e * 2) / t["best_s"] / 1e9})
                del s2, d2
                torch.cuda.empty_cache()
        except Exception as exc:  # noqa: BLE001
            out["errors"].append("membw dev %d: %r" % (label(d), exc))
        finally:
            torch.cuda.empty_cache()

    if want("host"):
        try:
            xfer_mb = int(min(budget / 4.0, 256))
            elems = int(xfer_mb * MB / 2)
            host = torch.empty(elems, dtype=torch.float16).pin_memory()
            dev = torch.empty(elems, device=d, dtype=torch.float16)
            for direction, fn in (("h2d", lambda: dev.copy_(host, non_blocking=True)),
                                  ("d2h", lambda: host.copy_(dev, non_blocking=True))):
                t = time_cuda(fn, d)
                out["host_transfer"].append({
                    "device": label(d), "direction": direction, "size_mib": xfer_mb,
                    "gb_s_best": (elems * 2) / t["best_s"] / 1e9,
                    "gb_s_median": (elems * 2) / t["median_s"] / 1e9})
            del host, dev
        except Exception as exc:  # noqa: BLE001
            out["errors"].append("host transfer dev %d: %r" % (label(d), exc))
        finally:
            torch.cuda.empty_cache()

    # ------------------------------------------------------------ sustained + power
    if want("sustained"):
        try:
            n = n_for(budget, 2)
            a = torch.randn(n, n, device=d, dtype=torch.bfloat16)
            b = torch.randn(n, n, device=d, dtype=torch.bfloat16)
            for _ in range(5):
                torch.matmul(a, b)
            torch.cuda.synchronize(d)

            sampler = PowerSampler(label(d))
            sampler.start()
            iters = 0
            t0 = time.time()
            deadline = t0 + SUSTAIN_S
            while time.time() < deadline:
                for _ in range(10):
                    torch.matmul(a, b)
                iters += 10
                torch.cuda.synchronize(d)
            elapsed = time.time() - t0
            sampler.stop()
            sampler.join(timeout=3)

            flops = 2.0 * n * n * n * iters
            rec = {"device": label(d), "dtype": "bfloat16", "n": n,
                   "seconds": elapsed, "iterations": iters,
                   "sustained_tflops": flops / elapsed / 1e12}
            power = sampler.summary()
            if power:
                rec["power"] = power
                if power["power_mean_w"] > 0:
                    rec["tflops_per_watt"] = rec["sustained_tflops"] / power["power_mean_w"]
                    rec["energy_joules"] = power["power_mean_w"] * elapsed
            out["sustained"].append(rec)
            del a, b
        except Exception as exc:  # noqa: BLE001
            out["errors"].append("sustained dev %d: %r" % (label(d), exc))
        finally:
            torch.cuda.empty_cache()

# ---------------------------------------------------------------- peer to peer
if want("p2p") and len(devices) >= 2:
    a_dev, b_dev = devices[0], devices[1]
    try:
        xfer_mb = int(min(VRAM_MB / 4.0, 256))
        elems = int(xfer_mb * MB / 2)
        src = torch.empty(elems, device=a_dev, dtype=torch.float16).fill_(1.0)
        dst = torch.empty(elems, device=b_dev, dtype=torch.float16)
        for s, t_, buf_from, buf_to in ((a_dev, b_dev, src, dst), (b_dev, a_dev, dst, src)):
            tt = time_cuda(lambda: buf_to.copy_(buf_from), s)
            out["p2p"].append({
                "src": s, "dst": t_,
                "peer_access": bool(torch.cuda.can_device_access_peer(s, t_)),
                "size_mib": xfer_mb,
                "gb_s_best": (elems * 2) / tt["best_s"] / 1e9})
        del src, dst
    except Exception as exc:  # noqa: BLE001
        out["errors"].append("p2p: %r" % (exc,))
    finally:
        torch.cuda.empty_cache()

emit(0)
