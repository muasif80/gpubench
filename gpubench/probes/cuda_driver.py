#!/usr/bin/env python3
"""Tier-2 probe: the CUDA driver API through ctypes. No toolkit, no PyTorch, no install.

Why this exists. The obvious way to benchmark a GPU from Python is PyTorch, but PyTorch is a
multi-gigabyte dependency with its own CUDA version matrix, and the machines worth measuring are
usually production servers where you are not allowed to install anything. The driver library
(`nvcuda.dll` on Windows, `libcuda.so.1` on Linux) is already present on any machine with a
working GPU, so loading it directly gives real measurements with a zero-byte install.

What it measures without needing a single kernel:
  * device attributes straight from the driver (SMs, clocks, bus width, L2 size, PCIe location)
  * device-to-device copy bandwidth, a lower bound on usable memory bandwidth
  * host-to-device and device-to-host bandwidth over PCIe, using pinned memory

What it cannot measure: tensor-core throughput. That needs cuBLASLt, which ships with the CUDA
toolkit rather than the driver. When a runtime with PyTorch is available the torch probe covers
it; when it is not, the report says so rather than reporting a zero.
"""
import ctypes
import os
import platform
import sys
import time

# ---------------------------------------------------------------- driver binding

CUDA_SUCCESS = 0

# cuDeviceGetAttribute selectors we care about (from cuda.h; stable across versions)
ATTR = {
    "max_threads_per_block": 1,
    "clock_rate_khz": 13,
    "multiprocessor_count": 16,
    "memory_clock_rate_khz": 36,
    "global_memory_bus_width": 37,
    "l2_cache_size": 38,
    "max_threads_per_multiprocessor": 39,
    "compute_capability_major": 75,
    "compute_capability_minor": 76,
    "pci_bus_id": 33,
    "pci_device_id": 34,
    "pci_domain_id": 50,
    "integrated": 18,
    "unified_addressing": 41,
}


class DriverError(RuntimeError):
    pass


def _library_candidates():
    if platform.system() == "Windows":
        return ["nvcuda.dll"]
    if platform.system() == "Darwin":
        return []  # NVIDIA CUDA has not been supported on macOS since 10.13
    return ["libcuda.so.1", "libcuda.so"]


class CudaDriver(object):
    def __init__(self):
        self.lib = None
        last = None
        for name in _library_candidates():
            try:
                self.lib = ctypes.CDLL(name)
                break
            except OSError as exc:
                last = exc
        if self.lib is None:
            raise DriverError("CUDA driver library not found (%s)" % (last,))
        self._bind()
        self._check(self.lib.cuInit(0), "cuInit")

    def _bind(self):
        L = self.lib
        # Explicit argtypes matter: without them ctypes truncates 64-bit pointers and sizes on
        # Windows, which produces silent corruption rather than an error.
        L.cuInit.argtypes = [ctypes.c_uint]
        L.cuDeviceGetCount.argtypes = [ctypes.POINTER(ctypes.c_int)]
        L.cuDeviceGet.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_int]
        L.cuDeviceGetName.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_int]
        L.cuDeviceGetAttribute.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_int, ctypes.c_int]
        L.cuDeviceTotalMem_v2.argtypes = [ctypes.POINTER(ctypes.c_size_t), ctypes.c_int]
        L.cuCtxCreate_v2.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint, ctypes.c_int]
        L.cuCtxDestroy_v2.argtypes = [ctypes.c_void_p]
        L.cuCtxSynchronize.argtypes = []
        L.cuMemAlloc_v2.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_size_t]
        L.cuMemFree_v2.argtypes = [ctypes.c_void_p]
        L.cuMemAllocHost_v2.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_size_t]
        L.cuMemFreeHost.argtypes = [ctypes.c_void_p]
        L.cuMemsetD8_v2.argtypes = [ctypes.c_void_p, ctypes.c_ubyte, ctypes.c_size_t]
        L.cuMemcpyDtoD_v2.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
        L.cuMemcpyHtoD_v2.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
        L.cuMemcpyDtoH_v2.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
        L.cuEventCreate.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint]
        L.cuEventRecord.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        L.cuEventSynchronize.argtypes = [ctypes.c_void_p]
        L.cuEventElapsedTime.argtypes = [ctypes.POINTER(ctypes.c_float), ctypes.c_void_p,
                                         ctypes.c_void_p]
        L.cuEventDestroy_v2.argtypes = [ctypes.c_void_p]
        L.cuMemGetInfo_v2.argtypes = [ctypes.POINTER(ctypes.c_size_t),
                                      ctypes.POINTER(ctypes.c_size_t)]
        L.cuDriverGetVersion.argtypes = [ctypes.POINTER(ctypes.c_int)]

    def _check(self, rc, what):
        if rc != CUDA_SUCCESS:
            raise DriverError("%s failed with CUDA error %d" % (what, rc))

    # ------------------------------------------------------------ enumeration

    def driver_version(self):
        v = ctypes.c_int()
        self._check(self.lib.cuDriverGetVersion(ctypes.byref(v)), "cuDriverGetVersion")
        return "%d.%d" % (v.value // 1000, (v.value % 1000) // 10)

    def device_count(self):
        n = ctypes.c_int()
        self._check(self.lib.cuDeviceGetCount(ctypes.byref(n)), "cuDeviceGetCount")
        return n.value

    def device(self, index):
        d = ctypes.c_int()
        self._check(self.lib.cuDeviceGet(ctypes.byref(d), index), "cuDeviceGet")
        return d.value

    def name(self, dev):
        buf = ctypes.create_string_buffer(256)
        self._check(self.lib.cuDeviceGetName(buf, 256, dev), "cuDeviceGetName")
        return buf.value.decode("utf-8", "replace")

    def attr(self, dev, key):
        v = ctypes.c_int()
        rc = self.lib.cuDeviceGetAttribute(ctypes.byref(v), ATTR[key], dev)
        return v.value if rc == CUDA_SUCCESS else None

    def total_mem(self, dev):
        n = ctypes.c_size_t()
        self._check(self.lib.cuDeviceTotalMem_v2(ctypes.byref(n), dev), "cuDeviceTotalMem")
        return n.value

    def describe(self, index):
        dev = self.device(index)
        out = {"index": index, "name": self.name(dev), "total_bytes": self.total_mem(dev)}
        for key in ATTR:
            out[key] = self.attr(dev, key)
        if out.get("compute_capability_major") is not None:
            out["compute_capability"] = "%s.%s" % (out.pop("compute_capability_major"),
                                                   out.pop("compute_capability_minor"))
        bus = out.get("global_memory_bus_width")
        mclk = out.get("memory_clock_rate_khz")
        if bus and mclk:
            # Reported as the DDR-style figure. GDDR6X/GDDR7 use multi-level signalling, so the
            # real peak can exceed this; it is published as a sanity check, not as the datasheet
            # bandwidth.
            out["naive_peak_bandwidth_gb_s"] = mclk * 1e3 * 2 * (bus / 8.0) / 1e9
        out["pci_bdf"] = "%04x:%02x:%02x.0" % (out.get("pci_domain_id") or 0,
                                               out.get("pci_bus_id") or 0,
                                               out.get("pci_device_id") or 0)
        return out


# ---------------------------------------------------------------- measurement

class Context(object):
    def __init__(self, drv, index):
        self.drv = drv
        self.dev = drv.device(index)
        self.ctx = ctypes.c_void_p()
        drv._check(drv.lib.cuCtxCreate_v2(ctypes.byref(self.ctx), 0, self.dev), "cuCtxCreate")

    def close(self):
        if self.ctx:
            self.drv.lib.cuCtxDestroy_v2(self.ctx)
            self.ctx = None

    def mem_info(self):
        free = ctypes.c_size_t()
        total = ctypes.c_size_t()
        self.drv._check(self.drv.lib.cuMemGetInfo_v2(ctypes.byref(free), ctypes.byref(total)),
                        "cuMemGetInfo")
        return free.value, total.value

    def _time(self, fn, iters, warmup=3):
        """Median of `iters` timings using CUDA events, warmed up first.

        Warmup is not optional: a GPU sitting in its lowest power state will spend the first
        measurement ramping clocks, which reads as a slow device rather than a cold one.
        """
        L = self.drv.lib
        start = ctypes.c_void_p()
        end = ctypes.c_void_p()
        self.drv._check(L.cuEventCreate(ctypes.byref(start), 0), "cuEventCreate")
        self.drv._check(L.cuEventCreate(ctypes.byref(end), 0), "cuEventCreate")
        try:
            for _ in range(warmup):
                fn()
            self.drv._check(L.cuCtxSynchronize(), "cuCtxSynchronize")
            samples = []
            for _ in range(iters):
                L.cuEventRecord(start, None)
                fn()
                L.cuEventRecord(end, None)
                self.drv._check(L.cuEventSynchronize(end), "cuEventSynchronize")
                ms = ctypes.c_float()
                self.drv._check(L.cuEventElapsedTime(ctypes.byref(ms), start, end),
                                "cuEventElapsedTime")
                samples.append(ms.value / 1000.0)
            samples.sort()
            return {"best_s": samples[0], "median_s": samples[len(samples) // 2],
                    "worst_s": samples[-1]}
        finally:
            L.cuEventDestroy_v2(start)
            L.cuEventDestroy_v2(end)

    def device_copy_bandwidth(self, size_bytes, iters=7):
        L = self.drv.lib
        src = ctypes.c_void_p()
        dst = ctypes.c_void_p()
        self.drv._check(L.cuMemAlloc_v2(ctypes.byref(src), size_bytes), "cuMemAlloc")
        try:
            self.drv._check(L.cuMemAlloc_v2(ctypes.byref(dst), size_bytes), "cuMemAlloc")
        except DriverError:
            L.cuMemFree_v2(src)
            raise
        try:
            L.cuMemsetD8_v2(src, 1, size_bytes)
            t = self._time(lambda: L.cuMemcpyDtoD_v2(dst, src, size_bytes), iters)
            # a copy reads once and writes once
            moved = 2.0 * size_bytes
            return {"size_bytes": size_bytes,
                    "gb_s_best": moved / t["best_s"] / 1e9,
                    "gb_s_median": moved / t["median_s"] / 1e9}
        finally:
            L.cuMemFree_v2(src)
            L.cuMemFree_v2(dst)

    def host_transfer_bandwidth(self, size_bytes, iters=7):
        L = self.drv.lib
        host = ctypes.c_void_p()
        dev = ctypes.c_void_p()
        self.drv._check(L.cuMemAllocHost_v2(ctypes.byref(host), size_bytes), "cuMemAllocHost")
        try:
            self.drv._check(L.cuMemAlloc_v2(ctypes.byref(dev), size_bytes), "cuMemAlloc")
        except DriverError:
            L.cuMemFreeHost(host)
            raise
        try:
            h2d = self._time(lambda: L.cuMemcpyHtoD_v2(dev, host, size_bytes), iters)
            d2h = self._time(lambda: L.cuMemcpyDtoH_v2(host, dev, size_bytes), iters)
            return [
                {"direction": "h2d", "size_bytes": size_bytes,
                 "gb_s_best": size_bytes / h2d["best_s"] / 1e9,
                 "gb_s_median": size_bytes / h2d["median_s"] / 1e9},
                {"direction": "d2h", "size_bytes": size_bytes,
                 "gb_s_best": size_bytes / d2h["best_s"] / 1e9,
                 "gb_s_median": size_bytes / d2h["median_s"] / 1e9},
            ]
        finally:
            L.cuMemFree_v2(dev)
            L.cuMemFreeHost(host)


MIB = 1024 * 1024


def run(vram_budget_mb=512, devices=None, cache_sweep=True):
    """Collect everything this tier can reach. Never raises: failures become recorded errors."""
    out = {"probe": "cuda_driver", "tier": 2, "devices": [], "memory_bandwidth": [],
           "host_transfer": [], "cache_sweep": [], "errors": [],
           "platform": platform.system()}
    try:
        drv = CudaDriver()
    except DriverError as exc:
        out["errors"].append(str(exc))
        return out

    out["driver_api_version"] = drv.driver_version()
    count = drv.device_count()
    want = list(range(count)) if devices is None else [d for d in devices if d < count]

    for i in range(count):
        try:
            info = drv.describe(i)
            info["selected"] = i in want
            out["devices"].append(info)
        except DriverError as exc:
            out["errors"].append("describe device %d: %s" % (i, exc))

    for i in want:
        ctx = None
        try:
            ctx = Context(drv, i)
            free, _total = ctx.mem_info()
            budget = min(vram_budget_mb * MIB, int(free * 0.35))
            if budget < 16 * MIB:
                out["errors"].append("device %d: only %.0f MiB free, skipping measurement"
                                     % (i, free / MIB))
                continue

            size = (budget // 2) & ~0xFFFFF  # two buffers, 1 MiB aligned
            r = ctx.device_copy_bandwidth(size)
            r["device"] = i
            r["test"] = "device_copy"
            out["memory_bandwidth"].append(r)

            xfer = min(size, 256 * MIB)
            for r in ctx.host_transfer_bandwidth(xfer):
                r["device"] = i
                out["host_transfer"].append(r)

            # Sweep across the L2 boundary. One buffer size gives one point on what is really a
            # curve, and the cache cliff is where a memory-bound workload's behaviour changes.
            if cache_sweep:
                l2 = next((d.get("l2_cache_size") for d in out["devices"]
                           if d["index"] == i), None) or 0
                for mb in (1, 4, 16, 64, 128, 256):
                    nbytes = mb * MIB
                    if nbytes * 2 > budget:
                        break
                    try:
                        s = ctx.device_copy_bandwidth(nbytes, iters=9)
                        s["device"] = i
                        s["size_mib"] = mb
                        s["fits_in_l2"] = bool(l2 and nbytes <= l2)
                        out["cache_sweep"].append(s)
                    except DriverError as exc:
                        out["errors"].append("cache sweep %d MiB on device %d: %s" % (mb, i, exc))
        except DriverError as exc:
            out["errors"].append("device %d: %s" % (i, exc))
        finally:
            if ctx is not None:
                ctx.close()
    return out


if __name__ == "__main__":
    import json
    budget = int(os.environ.get("GPUBENCH_VRAM_MB", "512"))
    print(json.dumps(run(vram_budget_mb=budget), indent=2))
