#!/usr/bin/env python3
"""Tier-0 probe: machine state with nothing installed beyond the GPU driver.

Standard library only, and it runs identically on Windows and Linux. Everything here is read-only:
no allocation, no kernels, nothing that could perturb a production workload.

The value of tier 0 is that it works on the machines that matter most, which are exactly the ones
where you are not permitted to install a benchmark suite. It also captures the context that makes
every later number interpretable: clocks, thermals, throttle reasons, PCIe link state, and what
else was resident on the GPU at the time.
"""
import json
import os
import platform
import re
import subprocess
import sys

GPU_FIELDS = [
    "index", "name", "uuid", "serial", "driver_version", "vbios_version",
    "memory.total", "memory.used", "memory.free",
    "pstate", "temperature.gpu", "fan.speed",
    "power.draw", "power.limit", "power.max_limit",
    "clocks.sm", "clocks.max.sm", "clocks.mem", "clocks.max.mem",
    "pcie.link.gen.current", "pcie.link.gen.max",
    "pcie.link.width.current", "pcie.link.width.max",
    "persistence_mode", "compute_mode", "ecc.mode.current",
    "utilization.gpu", "utilization.memory",
    "clocks_event_reasons.hw_thermal_slowdown", "clocks_event_reasons.sw_power_cap",
]

KEY = {f: f.replace(".", "_") for f in GPU_FIELDS}


def sh(argv, timeout=30):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return r.stdout
    except Exception:  # noqa: BLE001
        return ""


def numify(v):
    v = (v or "").strip()
    if v in ("", "N/A", "[N/A]", "[Not Supported]", "[Unknown Error]"):
        return None
    for cast in (int, float):
        try:
            return cast(v)
        except ValueError:
            pass
    return v


def gpus():
    raw = sh(["nvidia-smi", "--query-gpu=" + ",".join(GPU_FIELDS),
              "--format=csv,noheader,nounits"])
    rows = []
    for line in raw.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < len(GPU_FIELDS):
            continue
        rows.append({KEY[f]: numify(parts[i]) for i, f in enumerate(GPU_FIELDS)})
    return rows


def compute_processes():
    raw = sh(["nvidia-smi", "--query-compute-apps=gpu_uuid,pid,used_memory,process_name",
              "--format=csv,noheader,nounits"])
    procs = []
    for line in raw.strip().splitlines():
        p = [x.strip() for x in line.split(",")]
        if len(p) >= 4:
            procs.append({"gpu_uuid": p[0], "pid": numify(p[1]),
                          "used_mib": numify(p[2]), "process": p[3]})
    return procs


def pcie_links_linux():
    """Card link state and the bridge it plugs into, from sysfs.

    sysfs rather than `lspci -vv` on purpose: lspci only prints link capabilities as root, and a
    benchmark that needs root to see its own topology will not get run on a production host.
    Walking to the parent bridge is what distinguishes "this card is slow" from "this slot is".
    """
    links = []
    base = "/sys/bus/pci/devices"
    if not os.path.isdir(base):
        return links
    for name in sorted(os.listdir(base)):
        dev = os.path.join(base, name)
        try:
            with open(os.path.join(dev, "vendor")) as f:
                if f.read().strip() != "0x10de":
                    continue
            with open(os.path.join(dev, "class")) as f:
                if not f.read().strip().startswith("0x0300"):
                    continue
        except IOError:
            continue

        def rd(path):
            try:
                with open(path) as f:
                    return f.read().strip()
            except IOError:
                return None

        parent = os.path.dirname(os.path.realpath(dev))
        links.append({
            "bdf": name,
            "current_speed": rd(os.path.join(dev, "current_link_speed")),
            "max_speed": rd(os.path.join(dev, "max_link_speed")),
            "current_width": numify(rd(os.path.join(dev, "current_link_width"))),
            "max_width": numify(rd(os.path.join(dev, "max_link_width"))),
            "parent_bridge": os.path.basename(parent),
            "bridge_max_speed": rd(os.path.join(parent, "max_link_speed")),
            "bridge_max_width": numify(rd(os.path.join(parent, "max_link_width"))),
        })
    return links


def host_linux():
    info = {}
    try:
        with open("/etc/os-release") as f:
            for line in f:
                if line.startswith("PRETTY_NAME="):
                    info["os"] = line.split("=", 1)[1].strip().strip('"')
    except IOError:
        pass
    info["kernel"] = platform.release()
    for line in sh(["lscpu"]).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            if k.strip() == "Model name":
                info["cpu"] = v.strip()
            elif k.strip() == "CPU(s)":
                info["cpu_threads"] = numify(v.strip())
    try:
        with open("/proc/meminfo") as f:
            first = f.readline().split()
            info["memory_bytes"] = int(first[1]) * 1024
    except (IOError, IndexError, ValueError):
        pass
    for f in ("board_vendor", "board_name", "bios_version", "bios_date", "product_name"):
        p = "/sys/class/dmi/id/" + f
        try:
            with open(p) as fh:
                info[f] = fh.read().strip()
        except IOError:
            pass
    return info


def host_windows():
    info = {"os": platform.platform(), "kernel": platform.version(),
            "cpu": os.environ.get("PROCESSOR_IDENTIFIER", "")}
    ps = ("$c=Get-CimInstance Win32_Processor|Select-Object -First 1;"
          "$b=Get-CimInstance Win32_BaseBoard;"
          "$s=Get-CimInstance Win32_ComputerSystem;"
          "$i=Get-CimInstance Win32_BIOS;"
          "[pscustomobject]@{cpu=$c.Name;cpu_threads=$c.NumberOfLogicalProcessors;"
          "board_vendor=$b.Manufacturer;board_name=$b.Product;"
          "bios_version=$i.SMBIOSBIOSVersion;memory_bytes=$s.TotalPhysicalMemory}"
          "|ConvertTo-Json -Compress")
    raw = sh(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], timeout=60)
    try:
        data = json.loads(raw)
        for k, v in data.items():
            if v not in (None, ""):
                info[k] = v
    except ValueError:
        pass
    return info


def warnings_for(gpu_rows, links):
    """Conditions that silently invalidate a benchmark if nobody looks for them."""
    warn = []
    for g in gpu_rows:
        idx = g.get("index")
        if g.get("persistence_mode") == "Disabled":
            warn.append("GPU%s: persistence mode disabled, so idle clocks drop to the lowest "
                        "P-state. Any measurement without warmup times the clock ramp." % idx)
        cw, mw = g.get("pcie_link_width_current"), g.get("pcie_link_width_max")
        if cw and mw and cw < mw:
            warn.append("GPU%s: PCIe link negotiated x%s where the card supports x%s. Host and "
                        "peer transfers are link-limited." % (idx, cw, mw))
        if g.get("clocks_event_reasons_hw_thermal_slowdown") == "Active":
            warn.append("GPU%s: thermal slowdown ACTIVE at capture." % idx)
        if g.get("clocks_event_reasons_sw_power_cap") == "Active":
            warn.append("GPU%s: software power cap ACTIVE at capture." % idx)
    gens = {(l.get("bridge_max_speed"), l.get("bridge_max_width")) for l in links}
    if len(gens) > 1:
        warn.append("GPUs sit on ASYMMETRIC PCIe links. Without NVLink, collective operations "
                    "run at the slower card's rate: %s"
                    % "; ".join("%s -> %s %sx%s" % (l["bdf"], l["parent_bridge"],
                                                    l["bridge_max_speed"], l["bridge_max_width"])
                                for l in links))
    free = [g.get("memory_free") for g in gpu_rows if g.get("memory_free") is not None]
    if free and min(free) < 8000:
        warn.append("Only %s MiB free on the tightest GPU: another workload is resident, so "
                    "compute and bandwidth results are FLOORS, not peaks." % min(free))
    return warn


def run():
    system = platform.system()
    rows = gpus()
    links = pcie_links_linux() if system == "Linux" else []
    out = {
        "probe": "inventory", "tier": 0,
        "captured_at_utc": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "platform": system,
        "host": host_linux() if system == "Linux" else host_windows(),
        "gpus": rows,
        "gpu_processes": compute_processes(),
        "pcie_links": links,
        "topology": sh(["nvidia-smi", "topo", "-m"]).strip(),
        "containers": [],
    }
    docker = sh(["docker", "ps", "--format", "{{.Names}}|{{.Image}}|{{.Status}}|{{.Ports}}"])
    for line in docker.strip().splitlines():
        p = line.split("|")
        if len(p) >= 4:
            out["containers"].append({"name": p[0], "image": p[1], "status": p[2], "ports": p[3]})
    out["warnings"] = warnings_for(rows, links)
    if not rows:
        out.setdefault("errors", []).append(
            "nvidia-smi returned no GPUs: driver missing, or no NVIDIA device present")
    return out


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
