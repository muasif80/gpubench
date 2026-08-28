#!/usr/bin/env python3
"""Run the whole harness in one command, pin what can be pinned, and record what cannot.

    python tools/reproduce.py --target ssh://user@host --out-dir results/repro-20260828
    python tools/reproduce.py --explain --target ssh://user@host      # print the plan, run nothing
    python tools/reproduce.py --out-dir DIR --expect PRIOR/reproduction.json
    python tools/reproduce.py --out-dir DIR --rehash                  # no run, re-read and re-hash

Exit status: 0 when the run completed and nothing drifted, 1 when the harness failed or a pinned
field drifted from --expect, 2 on a usage error.

WHAT THIS PINS, AND WHAT IT CANNOT
----------------------------------
This tool installs nothing on the machine it measures. That is the design rule that lets it run
against a production box, and it is also the exact reason a reproduction script here MUST NOT
claim to pin a toolchain. The CUDA runtime, the driver, the inference engine build and the model
weights all belong to the target. A script that pretends otherwise is worse than no script,
because the reader stops checking.

So the record this writes separates two lists and never mixes them:

  PINNED     something this tool fixes or fully determines, and would notice changing:
             the benchmark source itself (a SHA-256 over every shipped .py, so the pin holds with
             or without git), the git commit when there is one, the result schema version, the
             exact argument vector, and the orchestrator's own Python.

  NOT PINNED something the target owns. The driver, the engine build, the model revision, the
             container image digest. For every one of these the record states WHO owns it, WHY it
             cannot be pinned from here, and, where the result schema captures it, THE VALUE THAT
             WAS ACTUALLY USED. Recording beats pretending: a value that was observed and stamped
             lets a later run be compared, which is the reproducibility that is actually available
             to a tool with this design.

--expect turns that recording into a check. Point it at a previous reproduction.json and every
observed environment field is compared against the earlier run. A changed driver or a changed
model id is then a reported drift with an exit code, rather than a silent difference in the
numbers that somebody argues about three weeks later.

THE HONEST GAPS IN THE CURRENT SCHEMA
-------------------------------------
Three fields a reader would want are NOT in result schema 1.0, so this tool cannot record them
and says so rather than leaving a blank:

  * the inference engine's build version. The result file records the model id the engine serves
    and that the endpoint is a vLLM one, from /v1/models, but not the engine's own version string.
  * the model revision. The endpoint reports a model NAME. Two runs a month apart can serve the
    same name from different weights and nothing here would show it.
  * the container image digest. Container names are stripped by the sanitiser by default because
    they are deployment-identifying, and the digest was never collected in the first place.

Each appears in not_pinned with that reason. Closing them means changing the probes, which is a
change to the measurement and belongs in its own decision, not in a reproduction script.
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RECORD_SCHEMA = "reproduction/1"
RECORD_NAME = "reproduction.json"

# Arguments whose VALUE must never reach the record. The target is deployment-identifying in the
# same sense the sanitiser means, and the password is a secret.
REDACT_VALUE_OF = ("--password", "--hostkey", "--target")


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(65536)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def digest_over(paths):
    """SHA-256 over an ordered set of files, each file's path mixed in before its bytes."""
    digest = hashlib.sha256()
    kept = []
    for path in sorted(paths):
        if not os.path.isfile(path):
            continue
        rel = os.path.relpath(path, ROOT).replace("\\", "/")
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        with open(path, "rb") as fh:
            digest.update(fh.read())
        kept.append(rel)
    return digest.hexdigest(), len(kept)


def source_digest():
    """One SHA-256 over every shipped source file, path included.

    This is the pin that survives a tarball. git is not present in every environment that runs a
    benchmark, and an archive extracted from a release has no history at all, so a pin that only
    works inside a checkout is not a pin for the people most likely to need one.

    SCOPE IS EXACTLY WHAT THE RELEASE ARCHIVES SHIP, and that is deliberate rather than lazy. If
    the maintainer's own tooling were folded in, the same code would digest differently in a
    checkout and in an extracted release, and a pin that disagrees with itself depending on where
    you stand is not a pin. The verification tools are hashed separately by tools_digest() below,
    so they are covered without contaminating the figure that has to be stable.
    """
    files = []
    for base, dirs, names in os.walk(os.path.join(ROOT, "gpubench")):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in sorted(names):
            if name.endswith(".py") or name.endswith(".json"):
                files.append(os.path.join(base, name))
    files.append(os.path.join(ROOT, "pyproject.toml"))
    return digest_over(files)


def tools_digest():
    """SHA-256 over the verification tools themselves, when they are present.

    The checker is part of what ran, so leaving it out of the record would mean a reproduction
    that pins the measurement and not the thing that judged it. It is separate from the source
    digest because tools/ is withheld from the release archives, so this one is absent by design
    in an extracted release rather than merely missing.
    """
    files = [os.path.join(ROOT, "tools", name)
             for name in ("verify_claims.py", "reproduce.py")]
    present = [p for p in files if os.path.isfile(p)]
    if not present:
        return None, 0
    return digest_over(present)


def git_state():
    """Commit, dirtiness and description, or an explicit reason there is none."""
    def run(args):
        try:
            out = subprocess.run(["git"] + args, cwd=ROOT, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE)
        except OSError as exc:
            return None, "git is not available: %s" % exc
        if out.returncode != 0:
            return None, out.stderr.decode("utf-8", "replace").strip() or "git exited %d" % \
                out.returncode
        return out.stdout.decode("utf-8", "replace").strip(), None

    commit, why = run(["rev-parse", "HEAD"])
    if commit is None:
        return {"commit": None, "unavailable": why}
    status, _ = run(["status", "--porcelain"])
    describe, _ = run(["describe", "--tags", "--always", "--dirty"])
    return {"commit": commit, "dirty": bool(status), "describe": describe,
            "modified_paths": [line[3:] for line in (status or "").splitlines()][:50]}


def tool_version():
    path = os.path.join(ROOT, "pyproject.toml")
    try:
        text = open(path, "r", encoding="utf-8").read()
    except OSError:
        return None
    match = re.search(r'(?m)^\s*version\s*=\s*"([^"]+)"', text)
    return match.group(1) if match else None


def schema_version():
    sys.path.insert(0, ROOT)
    try:
        from gpubench import runner
        return getattr(runner, "SCHEMA_VERSION", None)
    except Exception as exc:            # a broken import must not be reported as a clean pin
        return "unreadable: %s" % exc


def redact(argv):
    """The argument vector as recorded. Identifying or secret values are replaced.

    A reproduction record is meant to be published beside the numbers, so it goes through the same
    thinking the result sanitiser does. Three kinds of leak get closed here: the secret itself,
    the target host, and the LOCAL PATHS, which on Windows carry the operator's user name in every
    absolute path including the interpreter's own. Paths are reduced to their basename, which is
    the part that carries meaning to a reader.
    """
    out, skip = [], False
    for arg in argv:
        if skip:
            out.append("<redacted>")
            skip = False
            continue
        if arg in REDACT_VALUE_OF:
            out.append(arg)
            skip = True
            continue
        if arg == sys.executable:
            out.append("python")
            continue
        matched = False
        for flag in REDACT_VALUE_OF:
            if arg.startswith(flag + "="):
                out.append(flag + "=<redacted>")
                matched = True
                break
        if matched:
            continue
        if os.path.isabs(arg):
            out.append(".../" + os.path.basename(arg))
            continue
        out.append(arg)
    return out


# ---------------------------------------------------------------------------
# What the result file actually says about the machine
# ---------------------------------------------------------------------------

def observe(result):
    """Pull the environment the run really used out of a result file. Absent stays absent."""
    seen = {}
    probes = result.get("probes") or {}
    inventory = probes.get("inventory") or {}
    host = inventory.get("host") or {}
    for key in ("os", "kernel", "cpu", "cpu_threads"):
        if host.get(key) is not None:
            seen["host." + key] = host[key]
    gpus = inventory.get("gpus") or []
    for gpu in gpus:
        if not isinstance(gpu, dict):
            continue
        index = gpu.get("index")
        for key in ("name", "driver_version", "vbios_version", "memory_total", "power_limit"):
            if gpu.get(key) is not None:
                seen["gpu%s.%s" % (index, key)] = gpu[key]
    torch = probes.get("torch_compute") or {}
    for key in ("torch_version", "cuda_version"):
        if torch.get(key) is not None:
            seen["torch." + key] = torch[key]
    driver = probes.get("cuda_driver") or {}
    if driver.get("driver_api_version") is not None:
        seen["cuda.driver_api_version"] = driver["driver_api_version"]
    serve = probes.get("serve_bench") or {}
    config = serve.get("config") or {}
    if config.get("model"):
        seen["engine.model_id"] = config["model"]
    models = (serve.get("models_endpoint") or {}).get("data") or []
    if models and isinstance(models[0], dict):
        for key in ("owned_by", "max_model_len"):
            if models[0].get(key) is not None:
                seen["engine." + key] = models[0][key]
    seen["result.schema_version"] = result.get("schema_version")
    seen["result.mode"] = result.get("mode")
    seen["result.profile"] = result.get("profile")
    return seen


def not_pinned_list(seen):
    """Every field the target owns, why it is not pinned, and what was observed instead."""
    entries = [
        ("gpu driver version", "the driver belongs to the target host. This tool installs "
                               "nothing, so it can read the driver and cannot choose it.",
         "target host", [v for k, v in sorted(seen.items()) if k.endswith(".driver_version")]),
        ("CUDA runtime and torch build", "supplied by the PyTorch runtime already present on the "
                                         "target, or by the container the operator names. The "
                                         "tool is piped into it and never installs it.",
         "target host or the named container",
         [seen.get("torch.torch_version"), seen.get("torch.cuda_version")]),
        ("inference engine build version", "result schema 1.0 records the model the endpoint "
                                           "serves and that the endpoint is a vLLM one, but not "
                                           "the engine's own version string. It is not collected, "
                                           "so it cannot be recorded here.",
         "target host", [seen.get("engine.owned_by")]),
        ("model revision", "the endpoint reports a model NAME. Two runs can serve the same name "
                           "from different weights and nothing in the schema would show it.",
         "target host", [seen.get("engine.model_id")]),
        ("container image digest", "container names are stripped by the sanitiser because they "
                                   "are deployment-identifying, and the image digest was never "
                                   "collected in the first place.",
         "target host", []),
        ("host clock and thermal state", "ambient temperature, fan curve and the power cap in "
                                         "force at the moment of measurement are properties of "
                                         "the room and the board, not of this tool.",
         "target host", [v for k, v in sorted(seen.items()) if k.endswith(".power_limit")]),
    ]
    out = []
    for field, why, owner, observed in entries:
        values = [v for v in observed if v is not None]
        out.append({"field": field, "why_not_pinned": why, "owned_by": owner,
                    "observed": values if values else "not recorded by any probe"})
    return out


def build_record(argv, out_dir, result_path, harness, hash_artifacts=True):
    digest, counted = source_digest()
    tools_sha, tools_counted = tools_digest()
    record = {
        "schema": RECORD_SCHEMA,
        "written_at_utc": utc_now(),
        "tool": {
            "name": "gpubench",
            "version": tool_version(),
            "result_schema_version": schema_version(),
            "source_sha256": digest,
            "source_files_hashed": counted,
            "tools_sha256": tools_sha,
            "tools_files_hashed": tools_counted,
            "git": git_state(),
        },
        "orchestrator": {
            "python": sys.version.split()[0],
            "platform": sys.platform,
            "executable": os.path.basename(sys.executable),
        },
        "invocation": {
            "argv": redact(argv),
            "harness_command": redact(harness.get("command", [])),
            "returncode": harness.get("returncode"),
            "started_at_utc": harness.get("started_at_utc"),
            "finished_at_utc": harness.get("finished_at_utc"),
            "note": "values of %s are redacted: the target is deployment-identifying and the "
                    "password is a secret." % ", ".join(REDACT_VALUE_OF),
        },
        "pinned": [],
        "not_pinned": [],
        "environment_observed": {},
        "artifacts": [],
    }
    record["pinned"] = [
        {"field": "benchmark source", "value": digest,
         "how": "SHA-256 over %d shipped source files, path included. Holds inside a checkout "
                "and inside an extracted release archive alike." % counted},
        {"field": "verification tools", "value": tools_sha or "absent from this tree",
         "how": "SHA-256 over tools/verify_claims.py and tools/reproduce.py, %d file(s) found. "
                "Separate from the source digest because tools/ is withheld from the release "
                "archives, so its absence in an extracted release is by design and not a gap."
                % tools_counted},
        {"field": "git commit", "value": record["tool"]["git"].get("commit"),
         "how": "git rev-parse HEAD in the tool's own tree" if record["tool"]["git"].get("commit")
                else "NOT AVAILABLE: %s. The source digest above is the pin that still holds."
                     % record["tool"]["git"].get("unavailable", "no repository")},
        {"field": "working tree clean", "value": (not record["tool"]["git"].get("dirty"))
         if record["tool"]["git"].get("commit") else None,
         "how": "git status --porcelain. A dirty tree means the commit alone does not describe "
                "the code that ran, which is why the source digest is recorded beside it."},
        {"field": "result schema version", "value": record["tool"]["result_schema_version"],
         "how": "gpubench.runner.SCHEMA_VERSION, read from the code that ran"},
        {"field": "argument vector", "value": " ".join(redact(harness.get("command", []))),
         "how": "recorded verbatim, with identifying and secret values removed"},
        {"field": "orchestrator python", "value": sys.version.split()[0],
         "how": "the interpreter running this script"},
    ]

    result = None
    if result_path and os.path.isfile(result_path):
        try:
            result = json.load(open(result_path, "r", encoding="utf-8"))
        except ValueError as exc:
            record["invocation"]["result_unreadable"] = str(exc)
    if result is not None:
        record["environment_observed"] = observe(result)
    record["not_pinned"] = not_pinned_list(record["environment_observed"])

    for base, dirs, names in os.walk(out_dir if hash_artifacts else os.devnull):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        for name in sorted(names):
            if name == RECORD_NAME:
                continue
            path = os.path.join(base, name)
            record["artifacts"].append({
                "path": os.path.relpath(path, out_dir).replace("\\", "/"),
                "sha256": sha256_file(path),
                "bytes": os.path.getsize(path),
            })
    return record


def compare(record, expect_path):
    """Every observed field against a previous reproduction. Returns a drift list."""
    try:
        prior = json.load(open(expect_path, "r", encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [{"field": "--expect", "was": None, "now": None,
                 "note": "cannot read %s: %s" % (expect_path, exc)}]
    was = prior.get("environment_observed") or {}
    now = record.get("environment_observed") or {}
    drift = []
    for field in sorted(set(was) | set(now)):
        if was.get(field) != now.get(field):
            drift.append({"field": field, "was": was.get(field), "now": now.get(field)})
    for field, note in (("source_sha256", "the benchmark source itself changed between the two "
                                          "runs"),
                        ("tools_sha256", "the verification tooling changed between the two runs")):
        was_value = (prior.get("tool") or {}).get(field)
        now_value = record["tool"].get(field)
        if was_value and was_value != now_value:
            drift.append({"field": "tool." + field, "was": was_value, "now": now_value,
                          "note": note})
    return drift


def print_record(record, drift, explain_only):
    print("gpubench reproduction record %s" % RECORD_SCHEMA)
    print("")
    print("PINNED")
    for item in record["pinned"]:
        print("  %-24s %s" % (item["field"], item["value"]))
        print("      %s" % item["how"])
    print("")
    print("NOT PINNED (the target owns these; the value used is recorded instead)")
    for item in record["not_pinned"]:
        print("  %s" % item["field"])
        print("      owner    : %s" % item["owned_by"])
        print("      why      : %s" % item["why_not_pinned"])
        print("      observed : %s" % item["observed"])
    if explain_only:
        print("")
        print("--explain: nothing was run.")
        return
    print("")
    print("ENVIRONMENT OBSERVED (%d field(s))" % len(record["environment_observed"]))
    for field in sorted(record["environment_observed"]):
        print("  %-32s %s" % (field, record["environment_observed"][field]))
    print("")
    print("ARTIFACTS (%d)" % len(record["artifacts"]))
    for item in record["artifacts"]:
        print("  %s  %s  %d bytes" % (item["sha256"][:16], item["path"], item["bytes"]))
    if drift is not None:
        print("")
        if drift:
            print("DRIFT against --expect (%d field(s)):" % len(drift))
            for item in drift:
                print("  %s: was %r, now %r" % (item["field"], item.get("was"), item.get("now")))
                if item.get("note"):
                    print("      %s" % item["note"])
        else:
            print("DRIFT against --expect: none. Every observed field matched.")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(
        description="run the harness end to end and write a pinned, hashed reproduction record",
        epilog="Unrecognised arguments are passed through to `gpubench run` unchanged.")
    parser.add_argument("--out-dir", required=True,
                        help="directory for the result file and the reproduction record")
    parser.add_argument("--name", default="result.json", help="result filename inside --out-dir")
    parser.add_argument("--expect", help="a previous reproduction.json to compare against. Any "
                                         "difference in an observed field is a reported drift "
                                         "and a non-zero exit")
    parser.add_argument("--rehash", action="store_true",
                        help="do not run anything. Re-read an existing --out-dir, re-hash every "
                             "artifact and rewrite the record")
    parser.add_argument("--explain", action="store_true",
                        help="print the plan and the pins, run nothing")
    parser.add_argument("--verify-claims", dest="verify_claims",
                        help="after the run, rebuild this claims manifest from raw artefacts "
                             "using tools/verify_claims.py")
    parser.add_argument("--claims-root", help="root for the manifest's run paths")
    known, passthrough = parser.parse_known_args(argv)

    out_dir = os.path.abspath(known.out_dir)
    result_path = os.path.join(out_dir, known.name)
    command = [sys.executable, "-m", "gpubench", "run", "--out", result_path] + passthrough

    harness = {"command": command, "returncode": None}
    if known.explain:
        record = build_record(argv, out_dir, None, harness, hash_artifacts=False)
        print("would run: %s" % " ".join(redact(command)))
        print("")
        print_record(record, None, True)
        return 0

    if not known.rehash:
        if not os.path.isdir(out_dir):
            os.makedirs(out_dir)
        harness["started_at_utc"] = utc_now()
        print("running: %s" % " ".join(redact(command)))
        env = dict(os.environ)
        env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.run(command, cwd=ROOT, env=env)
        harness["returncode"] = proc.returncode
        harness["finished_at_utc"] = utc_now()
        if proc.returncode != 0:
            print("the harness exited %d. The record below describes what did run."
                  % proc.returncode)
    elif not os.path.isdir(out_dir):
        print("no such directory: %s" % out_dir)
        return 2

    record = build_record(argv, out_dir, result_path, harness)
    drift = compare(record, known.expect) if known.expect else None
    if drift is not None:
        record["drift_against"] = os.path.abspath(known.expect)
        record["drift"] = drift

    record_path = os.path.join(out_dir, RECORD_NAME)
    with open(record_path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(record, fh, indent=2, sort_keys=True)

    print_record(record, drift, False)
    print("")
    print("wrote %s" % record_path)

    status = 0
    if harness["returncode"] not in (None, 0):
        status = 1
    if drift:
        status = 1

    if known.verify_claims:
        print("")
        checker = os.path.join(ROOT, "tools", "verify_claims.py")
        args = [sys.executable, checker, known.verify_claims]
        if known.claims_root:
            args += ["--root", known.claims_root]
        print("running: %s" % " ".join(args))
        if subprocess.run(args, cwd=ROOT).returncode != 0:
            status = 1
    return status


if __name__ == "__main__":
    sys.exit(main())
