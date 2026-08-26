#!/usr/bin/env python3
"""Capture the serving engine's RESOLVED configuration, not the configuration it was asked for.

Why this probe exists, and it is worth stating because the reason cost a week of investigation.

A previous run of this tool recorded that the engine's prefix-cache counters read zero. That is a
true and useful measurement, and it is where the tool stopped. Explaining it -- distinguishing "the
cache was consulted and missed" from "the cache was never running" from "the counter is broken" --
took a person reading the engine's source inside a container. All three readings look identical from
outside, they license completely different recommendations, and one of them turns a change the
report called "the cheapest possible win" into something needing a maintenance window.

The distinction is recoverable in one HTTP request, because the engine publishes what it actually
resolved. The launch command is not that: flags a deployment never passed still have values, and a
default can be overridden by the engine itself based on the model. On the deployment this was found
on, prefix caching was off despite nobody passing a flag either way, because the engine turns it off
by default for models whose layers hold a recurrent state.

So: read the resolved config, record it beside the counters, and let the diagnostics module state
the explanation instead of leaving a reader to guess or an investigator to go digging.

Standard library only. Read-only: this probe issues GET requests and loads nothing onto the device.
Environment: BASE_URL, MODEL.
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("BASE_URL", "http://127.0.0.1:8000/v1")
TIMEOUT = float(os.environ.get("GPUBENCH_HTTP_TIMEOUT", "30"))

out = {
    "probe": "engine_config", "tier": 0,
    "started_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "resolved": {}, "models": [], "endpoints_tried": [], "errors": [],
    "method": {
        "what": "The engine's own resolved configuration, read from its metrics and model "
                "endpoints.",
        "why": "A launch command says what was ASKED for. An engine can resolve a different value "
               "from the model it loaded, and an absent flag still has an effective value. Only the "
               "resolved config explains a counter that reads zero.",
        "read_only": True,
    },
}


def get(path, root=False):
    url = (BASE.rstrip("/").rsplit("/v1", 1)[0] if root else BASE.rstrip("/")) + path
    out["endpoints_tried"].append(url.split("://", 1)[-1].split("/", 1)[-1])
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception as exc:  # noqa: BLE001
        out["errors"].append("%s: %s" % (path, str(exc)[:140]))
        return None, ""


# ------------------------------------------------------------------ resolved config from metrics
# Prometheus *_config_info gauges carry the resolved settings as label key/value pairs. This is the
# authoritative view: it is what the engine is running, not what someone typed.
INFO_LINE = re.compile(r"^(?P<metric>[a-zA-Z_:][\w:]*_config_info)\{(?P<labels>.*)\}\s+[\d.eE+-]+\s*$")
LABEL = re.compile(r'(?P<k>[\w.]+)="(?P<v>(?:[^"\\]|\\.)*)"')

status, text = get("/metrics", root=True)
if status == 200 and text:
    for line in text.splitlines():
        if line.startswith("#"):
            continue
        m = INFO_LINE.match(line.strip())
        if not m:
            continue
        block = out["resolved"].setdefault(m.group("metric"), {})
        for lm in LABEL.finditer(m.group("labels")):
            block[lm.group("k")] = lm.group("v")
elif status is not None:
    out["errors"].append("metrics endpoint returned HTTP %s" % status)

# ------------------------------------------------------------------ what is being served
status, text = get("/models")
if status == 200 and text:
    try:
        for d in (json.loads(text).get("data") or []):
            out["models"].append({k: d.get(k) for k in ("id", "root", "max_model_len", "owned_by")})
    except ValueError:
        out["errors"].append("models endpoint returned unparseable JSON")

# ------------------------------------------------------------------ normalise the few settings
# that downstream diagnostics reason about, so a rule does not have to know each engine's spelling.
def find(*names):
    """First matching resolved setting, searched across every *_config_info block."""
    for block in out["resolved"].values():
        for n in names:
            if n in block:
                return block[n]
    return None


def as_bool(v):
    if v is None:
        return None
    return str(v).strip().lower() in ("1", "true", "yes", "on")


out["normalised"] = {
    # Prefix / prompt caching. Named differently by different engines; all mean "may a repeated
    # prefix skip recomputation".
    "prefix_caching_enabled": as_bool(find("enable_prefix_caching", "enable_prompt_caching",
                                           "prefix_caching")),
    "chunked_prefill_enabled": as_bool(find("enable_chunked_prefill", "chunked_prefill")),
    "block_size": find("block_size"),
    "recurrent_cache_mode": find("mamba_cache_mode", "recurrent_cache_mode", "ssm_cache_mode"),
    "recurrent_block_size": find("mamba_block_size", "recurrent_block_size"),
    "kv_blocks": find("num_gpu_blocks", "num_blocks"),
    "kv_cache_dtype": find("cache_dtype", "kv_cache_dtype"),
    "max_model_len": (out["models"][0].get("max_model_len") if out["models"] else None),
}
out["normalised_note"] = (
    "Only the settings the diagnostics reason about are normalised here. Everything the engine "
    "published is kept verbatim under 'resolved' so a reading this tool does not yet understand is "
    "still recoverable from the result file rather than lost.")

if not out["resolved"]:
    out["not_available"] = {
        "what": "resolved engine configuration",
        "reason": "This engine exposes no *_config_info metric, or the metrics endpoint was not "
                  "reachable. Nothing was inferred from the launch command, because an absent flag "
                  "still has an effective value and guessing it is how a wrong explanation gets "
                  "published.",
        "consequence": "Any counter that reads zero can be REPORTED but not EXPLAINED. Diagnostics "
                       "depending on the resolved config will say so rather than assume.",
    }

out["finished_at_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
print(json.dumps(out, indent=2))
sys.exit(0)
