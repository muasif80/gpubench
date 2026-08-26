#!/usr/bin/env python3
"""Capability probe: the engines and precisions a datasheet lists but a compute benchmark skips.

Runs in the same place as the torch probe (piped into an existing runtime). It closes the gap
between "we measured the tensor cores" and "we accounted for everything the vendor advertises":

  INT4        weight-only 4-bit matmul, the kernel LLM serving actually uses
  NVENC       hardware video encode throughput, via ffmpeg if present
  NVDEC       hardware video decode throughput
  RT cores    enumerated, not measured, with the reason stated

The distinction this probe insists on: a datasheet quotes a *dense INT4 tensor rate*, while what
production inference runs is *W4A16* (4-bit weights, 16-bit activations, dequantised into a bf16
matmul). Those are different numbers by an order of magnitude, and reporting one as the other is
how a benchmark misleads without stating a single false fact.

Environment:
  GPUBENCH_DEVICES     comma list of device indices  (default all)
  GPUBENCH_PHYSICAL    physical index to report under a pinned run
  GPUBENCH_VIDEO_FRAMES frames per encode test        (default 600)
"""
import json
import os
import subprocess
import sys
import time

out = {
    "probe": "capabilities", "tier": 1,
    "started_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "int4": [], "video_encode": [], "video_decode": [],
    "engines": {}, "not_measured": [], "errors": [],
}

PHYSICAL = os.environ.get("GPUBENCH_PHYSICAL", "")
FRAMES = int(os.environ.get("GPUBENCH_VIDEO_FRAMES", "600"))


def emit(code=0):
    out["finished_at_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    print(json.dumps(out, indent=2))
    sys.exit(code)


def label(d):
    return int(PHYSICAL) if PHYSICAL != "" else d


def have(cmd):
    try:
        subprocess.run([cmd, "-version"], capture_output=True, timeout=10)
        return True
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------- INT4

try:
    import torch
except Exception as exc:  # noqa: BLE001
    out["errors"].append("import torch failed: %r" % (exc,))
    torch = None

if torch is not None and torch.cuda.is_available():
    out["torch_version"] = torch.__version__
    sel = os.environ.get("GPUBENCH_DEVICES", "")
    devices = ([int(x) for x in sel.split(",") if x.strip()]
               if sel.strip() else list(range(torch.cuda.device_count())))

    for d in devices:
        torch.cuda.set_device(d)
        n = 4096
        group = 128
        try:
            if not hasattr(torch, "_weight_int4pack_mm"):
                raise RuntimeError("runtime has no _weight_int4pack_mm")
            a = torch.randn(n, n, device=d, dtype=torch.bfloat16)
            # This build packs from uint8; older ones take int32. Try both rather than assume.
            packed = None
            for maker in (lambda: torch.randint(0, 255, (n, n // 2), device=d, dtype=torch.uint8),
                          lambda: torch.randint(0, 15, (n, n), device=d, dtype=torch.int32)):
                try:
                    packed = torch._convert_weight_to_int4pack(maker(), 8)
                    break
                except Exception:  # noqa: BLE001
                    continue
            if packed is None:
                raise RuntimeError("no accepted int4 packing layout")
            sz = torch.randn(n // group, n, 2, device=d, dtype=torch.bfloat16)

            for _ in range(3):
                torch._weight_int4pack_mm(a, packed, group, sz)
            torch.cuda.synchronize(d)
            t0 = time.time()
            iters = 10
            for _ in range(iters):
                torch._weight_int4pack_mm(a, packed, group, sz)
            torch.cuda.synchronize(d)
            dt = (time.time() - t0) / iters
            out["int4"].append({
                "device": label(d), "n": n, "group_size": group,
                "scheme": "W4A16",
                "tops_equivalent": 2.0 * n * n * n / dt / 1e12,
                "ms": dt * 1000,
                "note": "4-bit WEIGHTS with 16-bit activations, dequantised into a bf16 matmul. "
                        "This is the kernel production inference uses. It is NOT a dense INT4 "
                        "tensor-core rate and must not be compared against a datasheet INT4 "
                        "figure.",
            })
            del a, packed, sz
        except Exception as exc:  # noqa: BLE001
            out["errors"].append("int4 dev %d: %s" % (label(d), str(exc)[:180]))
        finally:
            torch.cuda.empty_cache()

# ---------------------------------------------------------------- video engines

if have("ffmpeg"):
    encoders = ""
    try:
        r = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                           capture_output=True, text=True, timeout=30)
        encoders = r.stdout
    except Exception:  # noqa: BLE001
        pass
    out["engines"]["ffmpeg"] = True
    out["engines"]["nvenc_codecs"] = sorted(
        set(c for c in ("h264_nvenc", "hevc_nvenc", "av1_nvenc") if c in encoders))

    for codec in out["engines"]["nvenc_codecs"]:
        try:
            t0 = time.time()
            r = subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error",
                 "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=60",
                 "-frames:v", str(FRAMES), "-c:v", codec, "-preset", "p1",
                 "-f", "null", "-"],
                capture_output=True, text=True, timeout=300)
            dt = time.time() - t0
            if r.returncode == 0 and dt > 0:
                out["video_encode"].append({
                    "codec": codec, "resolution": "1920x1080", "frames": FRAMES,
                    "seconds": dt, "fps": FRAMES / dt,
                    "realtime_x_at_60fps": (FRAMES / dt) / 60.0,
                })
            else:
                out["errors"].append("%s encode failed: %s" % (codec, r.stderr[:160]))
        except Exception as exc:  # noqa: BLE001
            out["errors"].append("%s encode: %s" % (codec, str(exc)[:160]))

    # Decode: encode a short clip to a real file first, then time decoding it back.
    try:
        tmp = "/tmp/gpubench_probe.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=60",
             "-frames:v", str(FRAMES), "-c:v", "h264_nvenc", "-preset", "p1", tmp],
            capture_output=True, timeout=300)
        if os.path.exists(tmp):
            t0 = time.time()
            r = subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error",
                 "-hwaccel", "cuda", "-i", tmp, "-f", "null", "-"],
                capture_output=True, text=True, timeout=300)
            dt = time.time() - t0
            if r.returncode == 0 and dt > 0:
                out["video_decode"].append({
                    "codec": "h264 (cuda hwaccel)", "resolution": "1920x1080",
                    "frames": FRAMES, "seconds": dt, "fps": FRAMES / dt,
                    "realtime_x_at_60fps": (FRAMES / dt) / 60.0})
            os.remove(tmp)
    except Exception as exc:  # noqa: BLE001
        out["errors"].append("decode: %s" % str(exc)[:160])
else:
    out["engines"]["ffmpeg"] = False
    out["not_measured"].append({
        "capability": "NVENC / NVDEC video encode and decode",
        "reason": "ffmpeg is not present in this runtime, and the tool installs nothing.",
        "how_to_close": "Run this probe in a runtime that has ffmpeg built with nvenc/cuvid, or "
                        "install ffmpeg on the target.",
    })

# ---------------------------------------------------------------- enumerated, not measured

if torch is not None and torch.cuda.is_available():
    p = torch.cuda.get_device_properties(0)
    sms = p.multi_processor_count
    out["engines"]["rt_cores"] = sms  # one RT core per SM on this architecture family
    out["engines"]["sm_count"] = sms

out["not_measured"].append({
    "capability": "Ray tracing core throughput",
    "reason": "RT cores are only reachable through a ray-tracing API (OptiX, DXR or Vulkan RT). "
              "No compute or tensor path exercises them, so a CUDA benchmark cannot measure "
              "them at all. The core count is enumerated above.",
    "how_to_close": "A dedicated OptiX or DXR trace-rate benchmark. Irrelevant to language-model "
                    "inference, which is why it is enumerated rather than measured.",
})
out["not_measured"].append({
    "capability": "Dense INT4 tensor-core rate",
    "reason": "The runtime exposes weight-only INT4 (W4A16) but no dense INT4 GEMM, so the "
              "datasheet's INT4 tensor rate cannot be reproduced. The W4A16 figure above measures "
              "a different and, for inference, more relevant kernel.",
    "how_to_close": "A cuBLASLt or CUTLASS INT4 GEMM path, if one is exposed by the runtime.",
})

emit(0)
