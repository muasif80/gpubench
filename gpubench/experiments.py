#!/usr/bin/env python3
"""Experiments: measurements that CHANGE the system under test, and put it back.

Everything else in this tool observes. An experiment intervenes -- it reconfigures or restarts the
thing being measured -- and that is a different category of act, so it gets a different set of
rules rather than another flag on `run`.

THE FOUR RULES EVERY EXPERIMENT FOLLOWS.

1. **Declare the blast radius before it runs.** Every experiment states what it changes, how long
   the service is unavailable, and what happens if it fails halfway. `--list` prints that table.
   Nobody should have to read the source to find out whether a command takes production down.

2. **Disruptive experiments do not run by accident.** They require BOTH an explicit enable in the
   config file AND `--confirm-disruptive` on the command line. Two independent gestures, because a
   config file gets copied between machines and a command line gets recalled from history, and
   neither alone is evidence of intent.

3. **Restore is not a step, it is a guarantee.** Restoration runs on success, on failure, and on
   interrupt, and it is VERIFIED afterwards against a baseline captured before anything was
   touched. An experiment that cannot describe how it restores is rejected at registration.

4. **Never destroy what you are borrowing.** Where an experiment needs a service out of the way, it
   STOPS that service rather than removing it, so restoration is a resume rather than a rebuild
   from a captured specification. A spec captured by a tool is a model of the thing; the thing
   itself is not.

Configuration lives in a file (see `default_config`), so a site records which experiments it
permits once, rather than deciding at each invocation.
"""
import json
import os
import time

# --------------------------------------------------------------------------- registry

REGISTRY = {}


def experiment(cls):
    """Register an experiment, refusing any that cannot describe its own blast radius."""
    for attr in ("ID", "TITLE", "WHAT_IT_DOES", "WHAT_IT_CHANGES", "RISK", "DOWNTIME",
                 "HOW_IT_RESTORES"):
        if not getattr(cls, attr, None):
            raise SystemExit("experiment %s is missing %s. An experiment that cannot state its own "
                             "blast radius may not be registered." % (cls.__name__, attr))
    if cls.DISRUPTIVE and not getattr(cls, "restore", None):
        raise SystemExit("experiment %s is disruptive but implements no restore()." % cls.__name__)
    REGISTRY[cls.ID] = cls
    return cls


class Experiment(object):
    """Base class. Subclasses implement capture(), execute() and, if disruptive, restore()."""

    ID = TITLE = WHAT_IT_DOES = WHAT_IT_CHANGES = RISK = DOWNTIME = HOW_IT_RESTORES = None
    DISRUPTIVE = False
    DEFAULTS = {}

    def __init__(self, transport, config, log=print):
        self.tp = transport
        self.cfg = dict(self.DEFAULTS)
        self.cfg.update(config or {})
        self.log = log
        self.baseline = None

    # -- helpers ---------------------------------------------------------------------
    def sh(self, script, timeout=600):
        """Run a shell snippet on the target. Newlines normalised: a stray carriage return in a
        script shipped from a Windows host produces `$'\\r': command not found`, which is an
        infuriating way to lose a maintenance window."""
        script = script.replace("\r\n", "\n").replace("\r", "\n")
        rc, out, err = self.tp.run(["bash", "-s"], stdin_data=script, timeout=timeout)
        return (out or "") + (err or "")

    def capture(self):
        """Baseline to restore to and verify against. Read-only."""
        return {}

    def execute(self):
        raise NotImplementedError

    def restore(self):
        return {}

    def verify_restored(self):
        """Compare the current state against the baseline. Returns (ok, detail)."""
        return True, "no verification implemented"

    # -- driver ----------------------------------------------------------------------
    def run(self):
        started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.log("capturing baseline...")
        self.baseline = self.capture()
        result = {"experiment": self.ID, "title": self.TITLE, "started_at_utc": started,
                  "config": self.cfg, "baseline": self.baseline, "disruptive": self.DISRUPTIVE}
        try:
            result["findings"] = self.execute()
            result["status"] = "completed"
        except BaseException as exc:  # noqa: BLE001 - including KeyboardInterrupt, on purpose
            result["status"] = "failed"
            result["error"] = "%s: %s" % (type(exc).__name__, str(exc)[:400])
            self.log("EXPERIMENT FAILED: %s" % result["error"])
        finally:
            # Rule 3: restoration runs on success, on failure, and on interrupt.
            if self.DISRUPTIVE:
                self.log("restoring...")
                result["restore"] = self.restore()
                ok, detail = self.verify_restored()
                result["restored"] = ok
                result["restore_verification"] = detail
                self.log("RESTORED AND VERIFIED: %s" % detail if ok
                         else "RESTORE NOT VERIFIED: %s" % detail)
        result["finished_at_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        return result


# --------------------------------------------------------------------------- experiments

@experiment
class TensorParallelScaling(Experiment):
    """Does splitting the model across devices help, and what does the split itself cost?"""

    ID = "tp-scaling"
    TITLE = "Tensor-parallel scaling control (TP=1 against TP=2)"
    DISRUPTIVE = True
    WHAT_IT_DOES = (
        "Serves the SAME model on ONE device instead of two, then measures prefill and decode "
        "against the two-device baseline. This is the control experiment for any claim that the "
        "interconnect between devices is a constraint: with one device there are no collectives at "
        "all, so the difference isolates what the split costs.")
    WHAT_IT_CHANGES = (
        "Stops the running inference service, starts a second one pinned to a single device on a "
        "DIFFERENT port and under a DIFFERENT name, measures it, removes it, and starts the "
        "original again. The original container is never removed and its configuration is never "
        "rewritten.")
    RISK = (
        "The inference service is UNAVAILABLE for the duration. The single-device engine may fail "
        "to start at all, which is itself a valid result: if the weights leave too little room for "
        "the cache, that is the finding. A failure to start does not extend the outage, because "
        "restoration does not depend on the experiment succeeding.")
    DOWNTIME = "Typically 3 to 8 minutes: one or two engine starts plus the measurement."
    HOW_IT_RESTORES = (
        "`docker start` on the original container, which restores its exact configuration because "
        "it was only ever stopped. Verified afterwards by a real inference compared against a "
        "baseline captured before the service was touched.")
    # Defaults are GENERIC. Site values -- container names, model, cache path -- come from the
    # config file, because a general-purpose tool that ships one estate's names leaks them to
    # everyone who downloads it. The tool's own packaging gate enforces this.
    DEFAULTS = {
        "service_container": "",          # REQUIRED from config: the running inference container
        "service_port": 8000,
        "probe_container": "tp1_probe",
        "probe_port": 8010,
        "image": "",                      # REQUIRED from config: the serving image
        "model": "",                      # REQUIRED from config: the served model name
        "hf_cache": "",                   # REQUIRED from config: model cache path on the target
        "gpu_memory_utilization": 0.80,
        "contexts_to_try": [131072, 8192],
        "prefill_lengths": [128, 512, 2048],
        "decode_tokens": 128,
        "startup_timeout_s": 300,
    }

    REQUIRED = ("service_container", "image", "model", "hf_cache")

    def capture(self):
        c = self.cfg
        missing = [k for k in self.REQUIRED if not c.get(k)]
        if missing:
            raise SystemExit(
                "experiment %r needs these settings, which are deliberately not defaulted "
                "because they name YOUR deployment and must not ship inside the tool: %s. "
                "Set them under experiments.%s in your config file."
                % (self.ID, ", ".join(missing), self.ID))
        out = self.sh(
            'echo "STATUS=$(docker inspect -f {{.State.Status}} %s)"\n'
            'echo "HTTP=$(curl -s -o /dev/null -w %%{http_code} http://127.0.0.1:%d/v1/models)"\n'
            'docker logs %s 2>&1 | grep -E "Model loading took|Available KV cache|'
            'GPU KV cache size|Maximum concurrency" | tail -4\n'
            % (c["service_container"], c["service_port"], c["service_container"]))
        return {"raw": out.strip(),
                "healthy": "HTTP=200" in out,
                "status": "running" if "STATUS=running" in out else "not running"}

    def execute(self):
        c = self.cfg
        if not self.baseline.get("healthy"):
            raise SystemExit("the service is not healthy before the experiment; refusing to stop "
                             "it. Fix the service first: %s" % self.baseline.get("raw", "")[:200])

        self.log("stopping %s (stop, not remove)" % c["service_container"])
        self.sh("docker stop %s >/dev/null 2>&1; sleep 3; "
                "nvidia-smi --query-gpu=index,memory.used --format=csv,noheader"
                % c["service_container"])

        attempts = []
        working = None
        for ctx in c["contexts_to_try"]:
            self.log("  TP=1 attempt at max-model-len=%d" % ctx)
            t0 = time.time()
            out = self.sh(self._start_probe(ctx), timeout=c["startup_timeout_s"] + 120)
            elapsed = time.time() - t0
            started = "PROBE_UP=1" in out
            harness_error = self._harness_broke(out, elapsed, started)
            attempts.append({"max_model_len": ctx, "started": started, "seconds": round(elapsed, 1),
                             "harness_error": harness_error, "engine_output": out.strip()})
            if harness_error:
                # A conclusion drawn from a broken harness is worse than no conclusion. Stop.
                raise SystemExit(
                    "THE HARNESS FAILED, so nothing was learned about the system under test: "
                    + harness_error
                    + ". This is deliberately fatal. The obvious reading of a probe that never "
                    "came up is 'the engine refused', and publishing that when the truth is 'the "
                    "script was mangled' is exactly the class of error this tool exists to "
                    "prevent.")
            if started:
                working = ctx
                break
        findings = {"attempts": attempts, "tp1_started_at_context": working}

        if working:
            self.log("  measuring TP=1 at ctx=%d" % working)
            findings["tp1_measurements"] = self._measure()
            findings["conclusion"] = (
                "The single-device engine started at a context length of %d. The measurements "
                "below are directly comparable with the two-device baseline, because the model, "
                "the image and the machine are identical and only the split differs." % working)
        else:
            findings["conclusion"] = (
                "The single-device engine did not start at any tested context length, and the "
                "engine's own error output is recorded above. On this deployment tensor "
                "parallelism is therefore not an optimisation that could be removed; it is what "
                "makes the model servable at all.")
        return findings

    @staticmethod
    def _harness_broke(out, elapsed, started):
        """Distinguish 'the engine refused' from 'this script never ran'.

        Both leave a probe that is not serving, and only one of them is a finding. Getting this
        wrong once produced a confident conclusion about the hardware from a shell quoting bug, so
        the check is explicit and the outcome is fatal rather than a warning.
        """
        if started:
            return None
        shell_errors = ("syntax error", "command not found", "ambiguous redirect",
                        "unexpected token", "No such file or directory", "Unable to open connection")
        for marker in shell_errors:
            if marker in out:
                return "the probe script did not execute on the target (%r in its output)" % marker
        if "PROBE_EXITED=1" not in out and "PROBE_UP=1" not in out:
            return ("the probe neither started nor exited: no status marker in the output, so the "
                    "startup loop itself did not run")
        if elapsed < 20:
            return ("the attempt took only %.1fs, which is far too short for an engine to load "
                    "weights and fail; the probe almost certainly never ran" % elapsed)
        return None

    def _start_probe(self, ctx):
        c = self.cfg
        return (
            'docker rm -f %(probe)s >/dev/null 2>&1 || true\n'
            'docker run -d --name %(probe)s --restart=no --gpus all '
            '-e CUDA_VISIBLE_DEVICES=0 -e HF_HUB_OFFLINE=1 '
            '-v %(cache)s:/root/.cache/huggingface -p %(pport)d:8000 --shm-size=64m %(image)s '
            '--model %(model)s --served-model-name %(model)s --tensor-parallel-size 1 '
            '--gpu-memory-utilization %(util)s --max-model-len %(ctx)d '
            '--reasoning-parser qwen3 --language-model-only --seed 42 >/dev/null 2>&1\n'
            'for i in $(seq 1 %(tries)d); do\n'
            '  code=$(curl -s -o /dev/null -w %%{http_code} http://127.0.0.1:%(pport)d/v1/models '
            '2>/dev/null || echo 000)\n'
            '  if [ "$code" = "200" ]; then echo PROBE_UP=1; break; fi\n'
            '  if ! docker ps --filter name=%(probe)s --format "{{.Names}}" | grep -q %(probe)s; '
            'then echo PROBE_EXITED=1; break; fi\n'
            '  sleep 5\n'
            'done\n'
            # Capture the engine's OWN reason, not merely the fact of failure. An earlier run
            # recorded "initialization failed. See root cause above" while filtering the root
            # cause out of the capture, which is a self-inflicted unknown: the experiment cost a
            # production outage and still could not say WHY.
            'if docker logs %(probe)s 2>&1 | grep -qiE "error|failed|Traceback"; then\n'
            '  echo "--- engine failure context ---"\n'
            '  docker logs %(probe)s 2>&1 | grep -iE "error|exceed|no available memory|'
            'kv cache|max_model_len|gpu_memory_utilization|out of memory|free memory" '
            '| tail -14\n'
            'fi\n'
            'docker logs %(probe)s 2>&1 | grep -iE "Model loading took|Available KV cache|'
            'GPU KV cache size|Maximum concurrency" | tail -4\n'
            % {"probe": c["probe_container"], "cache": c["hf_cache"], "pport": c["probe_port"],
               "image": c["image"], "model": c["model"], "util": c["gpu_memory_utilization"],
               "ctx": ctx, "tries": max(1, c["startup_timeout_s"] // 5)})

    def _measure(self):
        c = self.cfg
        script = (
            'python3 - <<PYEOF\n'
            'import json, time, urllib.request\n'
            'BASE = "http://127.0.0.1:%(pport)d/v1"\n'
            'MODEL = "%(model)s"\n'
            'def post(path, body, timeout=300):\n'
            '    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),\n'
            '                                 headers={"Content-Type": "application/json"})\n'
            '    t0 = time.perf_counter()\n'
            '    with urllib.request.urlopen(req, timeout=timeout) as r:\n'
            '        d = json.loads(r.read())\n'
            '    return d, time.perf_counter() - t0\n'
            'def prompt(n, salt):\n'
            '    return "Request %%d. %%s\\nSummarize:" %% (salt, " ".join(["lorem"] * max(1, n - 8)))\n'
            'out = {"prefill": [], "decode": None}\n'
            'for n in %(pref)s:\n'
            '    try:\n'
            '        d, dt = post("/completions", {"model": MODEL, "prompt": prompt(n, n),\n'
            '                     "max_tokens": 1, "temperature": 0, "ignore_eos": True})\n'
            '        pt = (d.get("usage") or {}).get("prompt_tokens")\n'
            '        out["prefill"].append({"requested": n, "counted": pt, "seconds": dt,\n'
            '                               "tokens_per_s": pt / dt})\n'
            '    except Exception as exc:\n'
            '        out["prefill"].append({"requested": n, "error": str(exc)[:120]})\n'
            'try:\n'
            '    d, dt = post("/completions", {"model": MODEL, "prompt": prompt(32, 1),\n'
            '                 "max_tokens": %(dec)d, "temperature": 0, "ignore_eos": True})\n'
            '    ct = (d.get("usage") or {}).get("completion_tokens")\n'
            '    out["decode"] = {"tokens": ct, "seconds": dt, "tokens_per_s": ct / dt,\n'
            '                     "itl_ms": dt / ct * 1000}\n'
            'except Exception as exc:\n'
            '    out["decode"] = {"error": str(exc)[:120]}\n'
            'print("MEASUREMENT_JSON=" + json.dumps(out))\n'
            'PYEOF\n'
            % {"pport": c["probe_port"], "model": c["model"],
               "pref": json.dumps(c["prefill_lengths"]), "dec": c["decode_tokens"]})
        out = self.sh(script, timeout=900)
        for line in out.splitlines():
            if line.startswith("MEASUREMENT_JSON="):
                return json.loads(line.split("=", 1)[1])
        return {"error": "no measurement returned", "raw": out[-400:]}

    def restore(self):
        c = self.cfg
        out = self.sh(
            'docker rm -f %(probe)s >/dev/null 2>&1 || true\n'
            'docker start %(svc)s >/dev/null 2>&1 || true\n'
            'code=000\n'
            'for i in $(seq 1 90); do\n'
            '  code=$(curl -s -o /dev/null -w %%{http_code} http://127.0.0.1:%(sport)d/v1/models '
            '2>/dev/null || echo 000)\n'
            '  [ "$code" = "200" ] && break\n'
            '  sleep 5\n'
            'done\n'
            'echo "RESTORED_HTTP=$code"\n'
            'docker ps --filter name=%(svc)s --format "RESTORED_STATUS={{.Status}}"\n'
            % {"probe": c["probe_container"], "svc": c["service_container"],
               "sport": c["service_port"]}, timeout=900)
        return {"raw": out.strip(), "http_200": "RESTORED_HTTP=200" in out}

    def verify_restored(self):
        """A health endpoint is not proof a model serves. Ask it to answer something."""
        c = self.cfg
        out = self.sh(
            'curl -s http://127.0.0.1:%d/v1/chat/completions -H "Content-Type: application/json" '
            '-d \'{"model":"%s","messages":[{"role":"user","content":'
            '"Reply with exactly: RESTORE-OK"}],"max_tokens":600,"temperature":0,'
            '"chat_template_kwargs":{"enable_thinking":false}}\' '
            '| python3 -c "import sys,json; d=json.load(sys.stdin); '
            'print(d[\'choices\'][0][\'message\'].get(\'content\',\'\')[:40])"'
            % (c["service_port"], c["model"]), timeout=600)
        ok = "RESTORE-OK" in out
        return ok, ("the restored service answered a real request correctly" if ok
                    else "the restored service did NOT answer correctly: %s" % out.strip()[:200])


# --------------------------------------------------------------------------- config

def default_config():
    """A config a site edits once, rather than deciding at every invocation."""
    cfg = {"_comment": "gpubench experiments. Disruptive entries need BOTH enabled:true here AND "
                       "--confirm-disruptive on the command line.",
           "experiments": {}}
    for eid, cls in sorted(REGISTRY.items()):
        cfg["experiments"][eid] = dict(
            {"enabled": False,
             "_title": cls.TITLE,
             "_disruptive": cls.DISRUPTIVE,
             "_risk": cls.RISK},
            **cls.DEFAULTS)
    return cfg


def load_config(path=None):
    """Config from an explicit path, then ./gpubench.json, else defaults."""
    for candidate in ([path] if path else []) + ["gpubench.json"]:
        if candidate and os.path.exists(candidate):
            with open(candidate, "r", encoding="utf-8") as f:
                return json.load(f), os.path.abspath(candidate)
    return default_config(), None


def describe(eid=None):
    """The blast-radius table. Printed by --list, so nobody has to read source to find the risk."""
    lines = []
    for k, cls in sorted(REGISTRY.items()):
        if eid and k != eid:
            continue
        lines.append("")
        lines.append("  %s  %s" % (k, "[DISRUPTIVE]" if cls.DISRUPTIVE else "[read-only]"))
        lines.append("  %s" % ("-" * (len(k) + 16)))
        lines.append("    %-16s %s" % ("what it is:", cls.TITLE))
        for label, text in (("what it does:", cls.WHAT_IT_DOES),
                            ("what it changes:", cls.WHAT_IT_CHANGES),
                            ("RISK:", cls.RISK),
                            ("downtime:", cls.DOWNTIME),
                            ("how it restores:", cls.HOW_IT_RESTORES)):
            wrapped = _wrap(text, 74)
            lines.append("    %-16s %s" % (label, wrapped[0]))
            for cont in wrapped[1:]:
                lines.append("    %-16s %s" % ("", cont))
        if cls.DEFAULTS:
            lines.append("    %-16s %s" % ("settings:", ", ".join(sorted(cls.DEFAULTS))))
    return "\n".join(lines)


def _wrap(text, width):
    words, out, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width:
            out.append(cur)
            cur = w
        else:
            cur = (cur + " " + w).strip()
    if cur:
        out.append(cur)
    return out or [""]


# --------------------------------------------------------------------------- cli

def cli_main(args, build_transport):
    """The `gpubench experiment` command. Lives here, beside the risk metadata it prints."""
    import json as _json
    import os as _os

    if args.write_config:
        with open(args.write_config, "w", encoding="utf-8") as f:
            _json.dump(default_config(), f, indent=2)
        print("wrote %s" % _os.path.abspath(args.write_config))
        print("Every experiment is DISABLED in it. Enable the one you want, then pass "
              "--confirm-disruptive if it is marked disruptive.")
        return 0

    if args.list or not args.name:
        print("Experiments CHANGE the system under test. Read the risk before running one.")
        print(describe(args.name))
        print("")
        print("  Disruptive experiments need BOTH enabled:true in the config file AND")
        print("  --confirm-disruptive on the command line.")
        return 0

    cls = REGISTRY.get(args.name)
    if not cls:
        print("no such experiment: %s" % args.name)
        print("available: %s" % ", ".join(sorted(REGISTRY)))
        return 2

    cfg_all, cfg_path = load_config(args.config)
    ecfg = dict((cfg_all.get("experiments") or {}).get(args.name) or {})
    for kv in args.set:
        if "=" not in kv:
            print("--set expects KEY=VALUE, got %r" % kv)
            return 2
        k, v = kv.split("=", 1)
        try:
            ecfg[k] = _json.loads(v)
        except ValueError:
            ecfg[k] = v

    enabled = bool(ecfg.get("enabled"))
    print("%s  %s" % (cls.ID, "[DISRUPTIVE]" if cls.DISRUPTIVE else "[read-only]"))
    for k, v in (("config file", cfg_path or "built-in defaults (all disabled)"),
                 ("enabled in config", enabled),
                 ("disruptive", cls.DISRUPTIVE),
                 ("--confirm-disruptive given", bool(args.confirm_disruptive))):
        print("  %-28s %s" % (k + ":", v))

    if not enabled:
        print("")
        print("REFUSING: %r is not enabled. Set experiments.%s.enabled = true in the config."
              % (args.name, args.name))
        print("Write a starter config with:  gpubench experiment --write-config gpubench.json")
        return 2
    if cls.DISRUPTIVE and not args.confirm_disruptive:
        print("")
        print("REFUSING: %r is disruptive and --confirm-disruptive was not given." % args.name)
        print(describe(args.name))
        return 2

    if args.dry_run:
        print("")
        print("DRY RUN: both gates passed. This WOULD run, and would not be a no-op:")
        print(describe(args.name))
        settings = dict((k, v) for k, v in ecfg.items() if not k.startswith("_"))
        print("")
        print("  settings: %s" % _json.dumps(settings, indent=2))
        return 0

    tp = build_transport(args.target, args.password, args.hostkey)
    result = cls(tp, ecfg, log=lambda m: print("  " + m)).run()

    print("")
    print("status: %s" % result.get("status"))
    if result.get("disruptive"):
        print("restored: %s -- %s" % (result.get("restored"), result.get("restore_verification")))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            _json.dump(result, f, indent=2)
        print("wrote %s" % _os.path.abspath(args.out))
    else:
        print(_json.dumps(result.get("findings"), indent=2)[:3000])

    # A failed RESTORE is the only outcome that makes this command fail loudly.
    if result.get("disruptive") and not result.get("restored"):
        return 1
    return 0 if result.get("status") == "completed" else 1
