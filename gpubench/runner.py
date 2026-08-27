#!/usr/bin/env python3
"""Orchestration: pick a transport, run the probe tiers that the target can support, merge.

The design rule is that the orchestrator never touches CUDA. It moves files, runs commands and
merges JSON, which is why it runs unchanged from Windows, Linux or macOS against any target.

Probe tiers are attempted in order and degrade rather than fail:

    tier 0  inventory        always; needs only the driver
    tier 2  cuda_driver      needs the driver library; no toolkit, no PyTorch
    tier 1  torch_compute    only where a PyTorch runtime already exists

A target with no PyTorch still produces a real report. It just says which measurements were out
of reach instead of reporting zeros.
"""
import json
import os
import platform
import shlex
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PROBES = os.path.join(HERE, "probes")

SCHEMA_VERSION = "1.0"


# ---------------------------------------------------------------- transports

def _send(argv, stdin_data, timeout, env=None):
    """Run a command, writing stdin as BYTES so newlines survive the trip.

    subprocess with text=True applies universal-newline translation on WRITE as well as read, so a
    script sent from a Windows host arrives at a POSIX shell with CRLF line endings. The shell then
    reports `$'\r': command not found` and, more insidiously, mis-parses `for ... do` and function
    bodies. This cost a maintenance window before it was found, because the failure looks like a
    quoting mistake in the script rather than a property of the pipe.

    Encoding explicitly and decoding explicitly is the fix; there is no text=True mode that does
    the right thing here.
    """
    payload = None
    if stdin_data is not None:
        payload = stdin_data.replace(CRLF, LF).replace(CR, LF).encode("utf-8")
    r = subprocess.run(argv, input=payload, capture_output=True, timeout=timeout, env=env)
    dec = lambda b: (b or b"").decode("utf-8", "replace")
    return r.returncode, dec(r.stdout), dec(r.stderr)


CR, LF, CRLF = chr(13), chr(10), chr(13) + chr(10)


class LocalTransport(object):
    kind = "local"

    def __init__(self):
        self.target = platform.node()

    def run(self, argv, stdin_data=None, timeout=1800, env=None):
        e = dict(os.environ)
        e.update(env or {})
        return _send(argv, stdin_data, timeout, env=e)

    def run_python(self, script_path, env=None, timeout=1800):
        with open(script_path, "r", encoding="utf-8") as f:
            src = f.read()
        return self.run([sys.executable, "-"], stdin_data=src, env=env, timeout=timeout)


class SshTransport(object):
    """Shells out to the system ssh client.

    Deliberately not an embedded SSH library: the system client already knows the user's keys,
    agent, ~/.ssh/config, jump hosts and bastions. Reimplementing that badly is the most common
    way a remote tool becomes unusable inside a real network.
    """
    kind = "ssh"

    def __init__(self, target, ssh_bin="ssh", extra_args=None):
        self.target = target
        self.ssh_bin = ssh_bin
        self.extra = extra_args or []

    def run(self, argv, stdin_data=None, timeout=1800, env=None):
        prefix = ""
        if env:
            prefix = " ".join("%s=%s" % (k, shlex.quote(str(v))) for k, v in env.items()) + " "
        remote = prefix + " ".join(shlex.quote(a) for a in argv)
        cmd = [self.ssh_bin] + self.extra + [self.target, remote]
        r = subprocess.run(cmd, input=stdin_data, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr

    def run_python(self, script_path, env=None, timeout=1800):
        with open(script_path, "r", encoding="utf-8") as f:
            src = f.read()
        return self.run(["python3", "-"], stdin_data=src, env=env, timeout=timeout)


class PlinkTransport(SshTransport):
    """Windows without OpenSSH, or a password-authenticated host: PuTTY plink."""
    kind = "plink"

    def __init__(self, target, password, hostkey=None, plink="plink"):
        SshTransport.__init__(self, target)
        self.plink = plink
        self.password = password
        self.hostkey = hostkey

    def _base(self):
        args = [self.plink, "-batch", "-ssh", "-pw", self.password]
        if self.hostkey:
            args += ["-hostkey", self.hostkey]
        return args + [self.target]

    def run(self, argv, stdin_data=None, timeout=1800, env=None):
        prefix = ""
        if env:
            prefix = " ".join("%s=%s" % (k, shlex.quote(str(v))) for k, v in env.items()) + " "
        remote = prefix + " ".join(shlex.quote(a) for a in argv)
        return _send(self._base() + [remote], stdin_data, timeout)


# ---------------------------------------------------------------- helpers

def parse_json_tail(text):
    """Extract the JSON document from output that may carry a banner ahead of it.

    NCCL prints its version to stdout before anything else runs, and a naive json.loads on the
    whole stream fails on a benchmark that actually succeeded. Start at the first brace.
    """
    if not text:
        return None
    i = text.find("{")
    if i < 0:
        return None
    try:
        return json.loads(text[i:])
    except ValueError:
        return None


def probe_path(name):
    return os.path.join(PROBES, name + ".py")


# ---------------------------------------------------------------- tiers

def run_inventory(tp):
    rc, out_s, err = tp.run_python(probe_path("inventory"), timeout=300)
    doc = parse_json_tail(out_s)
    if doc is None:
        return {"probe": "inventory", "tier": 0,
                "errors": ["inventory failed (rc=%s): %s" % (rc, (err or out_s)[:400])]}
    return doc


def run_cuda_driver(tp, vram_mb=512):
    rc, out_s, err = tp.run_python(probe_path("cuda_driver"),
                                   env={"GPUBENCH_VRAM_MB": str(vram_mb)}, timeout=900)
    doc = parse_json_tail(out_s)
    if doc is None:
        return {"probe": "cuda_driver", "tier": 2,
                "errors": ["cuda_driver failed (rc=%s): %s" % (rc, (err or out_s)[:400])]}
    return doc


def run_torch(tp, container=None, vram_mb=800, sustain_s=20, mode="shared", devices=None):
    """Run the tier-1 probe, optionally inside a container that already has PyTorch.

    FP8 and FP4 are re-run once per GPU with the device pinned. cuBLASLt returns an internal
    error for a scaled matmul on a second device inside one process, which looks exactly like a
    broken GPU and is not: a fresh process measures the same card fine.
    """
    script = probe_path("torch_compute")
    with open(script, "r", encoding="utf-8") as f:
        src = f.read()

    def invoke(env, extra_docker=()):
        if container:
            argv = ["docker", "exec", "-i"]
            for k, v in env.items():
                argv += ["-e", "%s=%s" % (k, v)]
            argv += list(extra_docker) + [container, "python3", "-"]
            return tp.run(argv, stdin_data=src, timeout=1800)
        return tp.run_python(script, env=env, timeout=1800)

    base_env = {"GPUBENCH_VRAM_MB": str(vram_mb), "GPUBENCH_SUSTAIN_S": str(sustain_s),
                "GPUBENCH_MODE": mode,
                "GPUBENCH_ONLY": "matmul,membw,host,p2p,sustained"}
    if devices:
        base_env["GPUBENCH_DEVICES"] = ",".join(str(d) for d in devices)

    rc, out_s, err = invoke(base_env)
    doc = parse_json_tail(out_s)
    if doc is None:
        return {"probe": "torch_compute", "tier": 1,
                "errors": ["torch probe failed (rc=%s): %s" % (rc, (err or out_s)[:600])]}

    # Second pass: the scaled precisions, one process per device.
    n_gpu = len(doc.get("devices") or [])
    # Discard the unpinned scaled-precision attempts AND the errors they produced: those runs are
    # superseded by the pinned passes below, so keeping their errors reports a failure the final
    # result does not contain.
    doc["matmul"] = [m for m in doc.get("matmul", [])
                     if m.get("dtype") not in ("float8_e4m3fn", "float4_e2m1")]
    doc["errors"] = [e for e in doc.get("errors", [])
                     if not (("fp8 dev" in e) or ("fp4 dev" in e))]
    for i in range(n_gpu):
        env = dict(base_env)
        env.update({"GPUBENCH_ONLY": "matmul", "GPUBENCH_PHYSICAL": str(i),
                    "CUDA_VISIBLE_DEVICES": str(i)})
        rc2, out2, err2 = invoke(env)
        part = parse_json_tail(out2)
        if not part:
            doc.setdefault("errors", []).append(
                "pinned pass for device %d failed: %s" % (i, (err2 or out2)[:200]))
            continue
        for m in part.get("matmul", []):
            if m.get("dtype") in ("float8_e4m3fn", "float4_e2m1"):
                doc["matmul"].append(m)
        doc.setdefault("errors", []).extend(
            e for e in part.get("errors", []) if "fp4" in e or "fp8" in e)
    doc["matmul"].sort(key=lambda m: (m.get("device", 0), m.get("dtype", "")))
    return doc


# ---------------------------------------------------------------- top level

def run_capabilities(tp, container=None):
    """Engines and precisions a compute benchmark skips: INT4, NVENC/NVDEC, RT enumeration."""
    script = probe_path("capabilities")
    with open(script, "r", encoding="utf-8") as f:
        src = f.read()
    if container:
        argv = ["docker", "exec", "-i", container, "python3", "-"]
        rc, out_s, err = tp.run(argv, stdin_data=src, timeout=1800)
    else:
        rc, out_s, err = tp.run_python(script, timeout=1800)
    doc = parse_json_tail(out_s)
    if doc is None:
        return {"probe": "capabilities", "tier": 1,
                "errors": ["capabilities probe failed (rc=%s): %s" % (rc, (err or out_s)[:400])]}
    return doc


def run_accuracy(tp, base_url=None, model=None):
    """Accuracy gate. Runs on the host: it only needs HTTP to the serving endpoint."""
    env = {}
    if base_url: env["BASE_URL"] = base_url
    if model: env["MODEL"] = model
    rc, o, e = tp.run_python(probe_path("accuracy"), env=env, timeout=900)
    doc = parse_json_tail(o)
    return doc or {"probe": "accuracy", "tier": 0,
                   "errors": ["accuracy probe failed (rc=%s): %s" % (rc, (e or o)[:300])]}


def run_engine_config(tp, base_url=None):
    """The engine's RESOLVED configuration. Read-only, and cheap: two GET requests.

    Runs before the serving probe on purpose. When a counter later reads zero, the difference
    between "the feature is off" and "the feature is on and the counter is broken" is only
    recoverable from the resolved config, and those license opposite recommendations.
    """
    env = {}
    if base_url: env["BASE_URL"] = base_url
    rc, o, e = tp.run_python(probe_path("engine_config"), env=env, timeout=120)
    doc = parse_json_tail(o)
    return doc or {"probe": "engine_config", "tier": 0,
                   "errors": ["engine_config probe failed (rc=%s): %s" % (rc, (e or o)[:300])]}


def run_nccl(tp, container=None, sizes_kb=None):
    """NCCL all-reduce sweep: the tensor-parallel primitive, by message size.

    Staged as a real file, never piped: spawn re-imports __main__ by path.
    """
    script = probe_path("nccl")
    with open(script, "r", encoding="utf-8") as f:
        src = f.read()
    env = {"GPUBENCH_SIZES_KB": sizes_kb or "4,10,20,40,256,1024,4096,16384,65536"}
    if container:
        remote = "/tmp/gpubench_nccl.py"
        tp.run(["sh", "-c", "cat > %s" % remote], stdin_data=src, timeout=120)
        argv = ["docker", "cp", remote, "%s:%s" % (container, remote)]
        tp.run(argv, timeout=120)
        argv = ["docker", "exec", "-i"]
        for k, v in env.items():
            argv += ["-e", "%s=%s" % (k, v)]
        argv += [container, "python3", remote]
        rc, o, e = tp.run(argv, timeout=1800)
        tp.run(["docker", "exec", container, "rm", "-f", remote], timeout=60)
    else:
        rc, o, e = tp.run_python(script, env=env, timeout=1800)
    return parse_json_tail(o) or {"probe": "nccl_allreduce", "errors": [
        "nccl probe failed (rc=%s): %s" % (rc, (e or o)[:300])]}


def run_serving(tp, base_url=None, model=None, concurrency="1,2,4,8,16,32,64", requests=16,
                input_tokens=512, output_tokens=128, mode="concurrency",
                arrival=None, rate=None, arrival_seed=None):
    """Serving benchmark: TTFT, inter-token latency, throughput, concurrency curve.

    `arrival` selects the load shape and defaults to the probe's own default (closed loop), so an
    existing caller is unaffected. Pass "poisson" with a `rate` for an open-loop run: requests are
    then issued on schedule whether or not the service is keeping up, which is the only way to
    produce the queue build-up that generates a real latency tail.
    """
    script = probe_path("serving")
    with open(script, "r", encoding="utf-8") as f:
        src = f.read()
    argv = ["python3", "-", "--mode", mode, "--concurrency", concurrency,
            "--requests", str(requests), "--input-tokens", str(input_tokens),
            "--output-tokens", str(output_tokens)]
    if arrival:
        argv += ["--arrival", str(arrival)]
    if rate:
        argv += ["--rate", str(rate)]
    if arrival_seed is not None:
        argv += ["--arrival-seed", str(arrival_seed)]
    if base_url:
        argv += ["--base-url", base_url]
    if model:
        argv += ["--model", model]
    rc, o, e = tp.run(argv, stdin_data=src, timeout=3600)
    return parse_json_tail(o) or {"probe": "serve_bench", "errors": [
        "serving probe failed (rc=%s): %s" % (rc, (e or o)[:300])]}


def run_embedding(tp, base_url=None, model=None):
    """Embedding throughput, which usually gates a retrieval pipeline before the model does."""
    script = probe_path("embedding")
    with open(script, "r", encoding="utf-8") as f:
        src = f.read()
    argv = ["python3", "-", "--batch", "1,8,32", "--concurrency", "1,4", "--requests", "8"]
    if base_url:
        argv += ["--base-url", base_url]
    if model:
        argv += ["--model", model]
    rc, o, e = tp.run(argv, stdin_data=src, timeout=1800)
    return parse_json_tail(o) or {"probe": "embed_bench", "errors": [
        "embedding probe failed (rc=%s): %s" % (rc, (e or o)[:300])]}


def run_serving_repeats(tp, repeats=3, **kw):
    """Run the serving sweep several times and report the spread between runs.

    Within-run variance (across timed iterations) and between-run variance are different things.
    Only the second tells you whether a number would reproduce tomorrow.
    """
    runs = []
    for _ in range(max(1, repeats)):
        d = run_serving(tp, **kw)
        if d.get("levels"):
            runs.append(d)
    if not runs:
        return {"probe": "serve_bench", "errors": ["no successful serving run"]}
    base = runs[-1]
    if len(runs) > 1:
        agg = {}
        for r in runs:
            for l in r["levels"]:
                agg.setdefault(l["concurrency"], []).append(l["output_tokens_per_s"])
        spread = {}
        for c, vals in agg.items():
            m = sum(vals) / len(vals)
            sd = (sum((v - m) ** 2 for v in vals) / (len(vals) - 1)) ** 0.5 if len(vals) > 1 else 0
            spread[str(c)] = {"runs": len(vals), "mean_tok_s": m, "stdev_tok_s": sd,
                              "cov_pct": (sd / m * 100.0) if m else 0.0,
                              "min_tok_s": min(vals), "max_tok_s": max(vals)}
        base["between_run_spread"] = spread
        base["repeats"] = len(runs)
    return base


def collect(tp, container=None, vram_mb=800, sustain_s=20, mode="shared",
            profile="base", skip=(), keep_target=False):
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    result = {
        "schema_version": SCHEMA_VERSION,
        "tool": "gpubench",
        "profile": profile,
        "mode": mode,
        "started_at_utc": started,
        # The target is recorded as a salted-free hash by default. Results are meant to be
        # shared, and a hostname or user@host is exactly the kind of detail that leaks an
        # organisation's estate into a public benchmark. Pass keep_target=True when the result
        # stays internal and provenance matters more than privacy.
        "orchestrator": {"platform": platform.system(), "python": platform.python_version(),
                         "transport": tp.kind,
                         "target": tp.target if keep_target else redact(tp.target)},
        "probes": {},
    }
    if "inventory" not in skip:
        result["probes"]["inventory"] = run_inventory(tp)
    if "cuda_driver" not in skip:
        result["probes"]["cuda_driver"] = run_cuda_driver(tp, vram_mb=min(vram_mb, 512))
    if "torch" not in skip:
        result["probes"]["torch_compute"] = run_torch(
            tp, container=container, vram_mb=vram_mb, sustain_s=sustain_s, mode=mode)
    if "capabilities" not in skip:
        result["probes"]["capabilities"] = run_capabilities(tp, container=container)
    if "nccl" not in skip:
        result["probes"]["nccl_allreduce"] = run_nccl(tp, container=container)
    if "serving" not in skip:
        # Resolved config FIRST: it is what makes the serving counters interpretable.
        result["probes"]["engine_config"] = run_engine_config(tp)
        result["probes"]["serve_bench"] = run_serving(tp)
    if "embedding" not in skip:
        result["probes"]["embed_bench"] = run_embedding(tp)
    if "accuracy" not in skip:
        result["probes"]["accuracy"] = run_accuracy(tp)
    result["finished_at_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if not keep_target:
        sanitise(result)
    result["fingerprint"] = fingerprint(result)
    result["artifact"] = artifact_identity()

    # Diagnostics last, over the complete bundle. Every result therefore carries its own
    # conclusions, including a count of the checks that could not run: the tool states what it
    # cannot conclude rather than leaving an anomalous reading for someone to investigate later.
    try:
        from . import diagnose as _dg
        from .report import _attribution as _att
        result["diagnostics"] = _dg.diagnose(result, _att(result))
    except Exception as exc:  # noqa: BLE001
        result["diagnostics"] = {"error": "diagnostics failed: %s" % str(exc)[:200]}
    return result


def redact(target):
    """Drop the target entirely.

    An earlier version stored an unsalted truncated SHA-256 of the target and called it
    non-reversible. It is not: a `user@host` string carries perhaps 20-40 bits of real entropy,
    and an adversarial review recovered a real one from a 24-candidate dictionary in
    milliseconds. Being unsalted it was also a stable join key across published results.
    There is no safe way to keep a useful identifier, so the identifier does not survive.
    """
    return "not recorded"



# Fields that describe the DEPLOYMENT rather than the MACHINE. A benchmark result is meant to be
# shared; a container list with image names and port bindings maps an estate. Hashing the hostname
# while shipping this was security theatre.
IDENTIFYING_GPU_FIELDS = ("uuid", "serial")


def sanitise(result):
    """Strip deployment identity, keep everything needed to interpret the measurements.

    Retained deliberately: GPU model, driver, board, CPU, memory, clocks, PCIe topology. Those are
    disclosed in the README and are the whole point of a hardware report. Removed: anything that
    says WHOSE machine this is or WHAT ELSE runs on it.
    """
    inv = (result.get("probes") or {}).get("inventory") or {}

    # Start from any manifest a previous pass already wrote. Sanitising is not guaranteed to happen
    # exactly once -- a result can be sanitised on pull and again on merge -- and the checks below
    # are all "is this field still populated?", which are FALSE the second time round. Without this
    # the second pass would rewrite a SHORTER manifest and quietly drop the record of what the
    # first pass removed. That is how one shipped result file ended up declaring only "GPU process
    # names and PIDs" while also carrying container_count: 19 with an empty containers list: the
    # container redaction had happened and the file no longer admitted it.
    removed = list(((result.get("sanitised") or {}).get("removed")) or [])

    if inv.get("containers"):
        # The count is analytically useful (how busy is the host); the names are not.
        inv["container_count"] = len(inv["containers"])
        inv["containers"] = []
        removed.append("container names, images and port bindings")
    elif "container_count" in inv and not inv.get("containers"):
        # Already redacted, by this run or an earlier pass. Say so either way: a reader comparing
        # container_count against an empty list deserves to see why it is empty.
        removed.append("container names, images and port bindings")

    procs = inv.get("gpu_processes") or []
    if procs:
        inv["gpu_processes"] = [
            {"used_mib": p.get("used_mib"), "process": "(redacted)"} for p in procs]
        removed.append("GPU process names and PIDs")
    if any(p.get("process") == "(redacted)" for p in (inv.get("gpu_processes") or [])):
        removed.append("GPU process names and PIDs")

    for g in inv.get("gpus", []):
        for f in IDENTIFYING_GPU_FIELDS:
            if g.get(f) not in (None, "", 0):
                g[f] = None
                if f not in " ".join(removed):
                    removed.append("GPU %s" % f)

    for probe in ("cuda_driver",):
        for d in ((result.get("probes") or {}).get(probe) or {}).get("devices", []):
            d.pop("pci_bdf", None)

    if removed:
        result["sanitised"] = {
            "note": "Deployment-identifying fields were removed so this result can be shared.",
            "removed": sorted(set(removed)),
            "retained": "GPU model, driver, board, CPU, clocks, PCIe topology and all measurements",
        }
    return result


def artifact_identity():
    """Who produced this, at what version, from which file, with what checksum.

    A report can describe a harness in complete detail and still leave a reader unable to obtain
    it. An independent review of one such report put it exactly right: everything else in the
    document was auditable in principle, and the single claim a reader could not act on was the one
    saying the code was available. A version string and a checksum turn that from a promise into a
    fact, and they cost nothing to emit.
    """
    import hashlib

    here = os.path.dirname(os.path.abspath(__file__))
    version = "unknown"
    try:
        with open(os.path.join(here, "..", "pyproject.toml"), "r", encoding="utf-8") as f:
            for line in f:
                if line.strip().startswith("version"):
                    version = line.split("=", 1)[1].strip().strip('"\'')
                    break
    except IOError:
        pass

    # Checksum the measurement code itself, so a result names the exact bytes that produced it.
    digest = hashlib.sha256()
    for root, dirs, files in os.walk(here):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        for fn in sorted(files):
            if fn.endswith(".py"):
                try:
                    with open(os.path.join(root, fn), "rb") as f:
                        digest.update(f.read())
                except IOError:
                    pass
    return {
        "tool": "gpubench",
        "version": version,
        "source_sha256": digest.hexdigest(),
        "note": "source_sha256 is over every .py file in the installed package, so a result can be "
                "tied to the exact measurement code that produced it. Quote the version and this "
                "digest wherever the results are published.",
    }


def fingerprint(result):
    """Parameters that must match for two results to be comparable.

    The report refuses to draw a comparison across differing fingerprints. Comparing a shared-mode
    run against an exclusive-mode one, or two different GPU models, produces a chart that looks
    authoritative and means nothing.
    """
    import hashlib
    inv = result.get("probes", {}).get("inventory", {})
    gpus = inv.get("gpus", []) or []
    parts = [
        result.get("schema_version", ""),
        result.get("profile", ""),
        result.get("mode", ""),
        str(len(gpus)),
        (gpus[0].get("name") if gpus else "") or "",
        (gpus[0].get("driver_version") if gpus else "") or "",
    ]
    blob = "|".join(str(p) for p in parts)
    return {"inputs": blob, "hash": hashlib.sha256(blob.encode()).hexdigest()[:16]}
