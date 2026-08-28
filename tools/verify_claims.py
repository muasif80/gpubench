#!/usr/bin/env python3
"""Rebuild every derived claim in a manifest from the RAW RESULT ARTEFACTS, not from the manifest.

    python tools/verify_claims.py <manifest.json> [--root DIR] [--json out.json] [--strict]
    python tools/verify_claims.py --selftest

Exit status: 0 when every derived claim rebuilt, 1 when any did not, 2 on a usage or IO error.

WHY THIS EXISTS, AND WHY IT IS NOT THE GATE
-------------------------------------------
The claims gate (`gpubench verify`, check B1) already recomputes every claim that carries a
formula. It evaluates that formula in an environment built from THE MANIFEST'S OWN VALUES. That
catches a typed quotient and an inconsistent arithmetic chain, and it is the right check for what
it is, but it has one blind spot that no amount of internal consistency can close:

    A manifest can be perfectly self-consistent and still describe a machine nobody measured.

Change a measured value in the generator, let every derived value recompute around it, and the
gate stays green. Every number agrees with every other number. Nothing agrees with the run.

This tool closes that hole from the other side. It never trusts a measured value in the manifest.
For each one it goes back to the result artefact the manifest names, LOCATES THE NUMBER THERE, and
rebuilds the derived claims from what the files actually contain. The gate proves the report is
consistent. This proves the report is about the run.

The two are complementary and the difference matters in a second way: this rebuild carries FULL
PRECISION from the artefact through the whole chain, where the gate reads whatever precision the
manifest chose to store. That difference is not noise. It is how an intermediate value that was
stored rounded, and then divided into, gets found.

HOW A MEASURED CLAIM IS LOCATED
-------------------------------
A measured claim in this format names its run and its value but not a pointer into the file, so
locating it is a search, and a search can be wrong. Every tier below is an EXACT predicate over
recorded bytes, and every hit records the file, the JSON pointer and the arithmetic, so a reader
can check the location and not just the verdict. Tiers are tried in order and the first hit wins:

  exact    a numeric leaf in the artefact equals the claim value.
  text     the number appears verbatim inside a string the artefact recorded, which is how engine
           log lines captured into a result file carry their numbers. Thousands separators are
           tolerated because the engine prints them.
  rounded  a numeric leaf rounds to the claim value at the claim value's own printed precision.
           The raw leaf, not the rounded value, is what feeds the rebuild.
  unit     a numeric leaf divided by ONE factor from a table keyed on the claim's declared unit.
           MiB to GiB, bytes to GiB, ms to s, fraction to percent. The factor has to be justified
           by the unit the claim itself declares, so this cannot become a fishing expedition.
  ratio    a numeric leaf divided by another scalar IN THE SAME PARENT OBJECT, which is how a
           per-request figure sits in a file that records a per-level total and a request count.
           Both pointers are recorded, so the relation is named rather than fitted.

Anything else is UNGROUNDED and is reported as one. There is deliberately no free-factor tier: an
earlier draft of this tool allowed division by a small set of bare constants and it "grounded"
four per-request token counts by dividing a per-level total by 8, which was arithmetically right
and evidentially wrong. 8 was the request count in the same object, and a check that cannot say
WHERE its divisor came from is fitting a number, not finding one.

WHAT COUNTS AS A PASS
---------------------
  REBUILT          the rebuilt value equals the declared value to 1e-9 relative.
  REBUILT_ROUNDED  the declared value IS the rebuilt value rounded to the declared value's own
                   printed precision. This is an exact predicate, not a slackened tolerance:
                   104.9 == round(104.8832, 1) passes, and 104.8 would not.

and as a finding, which fails the run:

  DRIFT            neither of the above, but inside the gate's own 0.5% recomputation tolerance.
                   A drift is usually a rounded intermediate being divided into downstream, and
                   the report names the upstream claim responsible when it can find it.
  MISMATCH         outside that tolerance. The manifest and the artefacts disagree.
  UNGROUNDED       a measured claim in the input tree could not be located in any artefact, so
                   the derived claim cannot be rebuilt from raw data at all.

Tolerances are never widened to make a run green. If a claim drifts, the drift is the output.
"""
import argparse
import ast
import hashlib
import json
import operator
import os
import re
import sys
import tempfile

TOOL_VERSION = "1.0"

# 1e-9 relative is "the same float, allowing for the last bit or two of a different summation
# order". It is not a tolerance in the judgement sense and is never widened by a flag.
EXACT_REL = 1e-9

# The gate's own recomputation tolerance. Anything past this is a MISMATCH rather than a DRIFT;
# the distinction is reporting only, both are findings and both fail the run.
GATE_REL = 0.005

# Unit conversions, keyed on the unit the CLAIM declares. A factor is only ever tried when the
# claim's own declared unit justifies it, and only one factor is applied. Values are divisors.
UNIT_FACTORS = {
    "GiB": ((1024.0, "MiB to GiB"), (1048576.0, "KiB to GiB"), (1073741824.0, "bytes to GiB")),
    "MiB": ((1024.0, "KiB to MiB"), (1048576.0, "bytes to MiB")),
    "KiB": ((1024.0, "bytes to KiB"),),
    "GB": ((1e9, "bytes to GB"), (1000.0, "MB to GB")),
    "MB": ((1e6, "bytes to MB"),),
    "GB/s": ((1e9, "bytes/s to GB/s"),),
    "TB/s": ((1e12, "bytes/s to TB/s"),),
    "TFLOPS": ((1e12, "FLOP/s to TFLOPS"), (1000.0, "GFLOPS to TFLOPS")),
    "s": ((1000.0, "ms to s"),),
    "ms": ((0.001, "s to ms"),),
    "%": ((0.01, "fraction to percent"),),
}

NUMBER_IN_TEXT = re.compile(r"-?\d[\d,]*(?:\.\d+)?")

OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.Pow: operator.pow, ast.Mod: operator.mod,
    ast.USub: operator.neg, ast.UAdd: operator.pos,
}


# ---------------------------------------------------------------------------
# Reading the manifest and the artefacts
# ---------------------------------------------------------------------------

def load_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def claims_of(manifest):
    """The claims map. Accepts the dict form this format uses and a list form defensively."""
    claims = manifest.get("claims")
    if isinstance(claims, dict):
        return claims
    if isinstance(claims, list):
        out = {}
        for i, c in enumerate(claims):
            if isinstance(c, dict):
                out[c.get("id", "claim_%d" % i)] = c
        return out
    raise ValueError("manifest has no claims map")


def numeric_leaves(node, pointer=""):
    """Every number in a JSON tree, with its pointer. Booleans are not numbers here."""
    if isinstance(node, dict):
        for key, value in node.items():
            for item in numeric_leaves(value, pointer + "/" + str(key)):
                yield item
    elif isinstance(node, list):
        for i, value in enumerate(node):
            for item in numeric_leaves(value, pointer + "/" + str(i)):
                yield item
    elif isinstance(node, bool):
        return
    elif isinstance(node, (int, float)):
        yield pointer, float(node)


def string_leaves(node, pointer=""):
    if isinstance(node, dict):
        for key, value in node.items():
            for item in string_leaves(value, pointer + "/" + str(key)):
                yield item
    elif isinstance(node, list):
        for i, value in enumerate(node):
            for item in string_leaves(value, pointer + "/" + str(i)):
                yield item
    elif isinstance(node, str):
        yield pointer, node


def sibling_scalars(node, pointer=""):
    """Parent pointer -> {key: number} for every object holding at least two numbers.

    This is what makes the ratio tier nameable. A per-request figure is a per-level total over a
    request count recorded beside it, and both sides of that division live in one object.
    """
    if isinstance(node, dict):
        here = {}
        for key, value in node.items():
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                here[str(key)] = float(value)
        if len(here) >= 2:
            yield pointer, here
        for key, value in node.items():
            for item in sibling_scalars(value, pointer + "/" + str(key)):
                yield item
    elif isinstance(node, list):
        for i, value in enumerate(node):
            for item in sibling_scalars(value, pointer + "/" + str(i)):
                yield item


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(65536)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def resolve_root(manifest, manifest_path, explicit):
    """Find the directory the manifest's run paths are relative to.

    The run paths in this format are relative to the report repository's root, and the manifest
    itself is written several directories below it. Guessing is fine as long as the guess is
    PRINTED and as long as it has to satisfy every run path rather than one, so a half-resolved
    root is an error and not a quietly reduced artefact set.
    """
    runs = manifest.get("runs") or {}
    wanted = [r.get("path") for r in runs.values() if isinstance(r, dict) and r.get("path")]
    candidates = []
    if explicit:
        candidates.append(os.path.abspath(explicit))
    else:
        here = os.path.dirname(os.path.abspath(manifest_path))
        for _ in range(6):
            candidates.append(here)
            parent = os.path.dirname(here)
            if parent == here:
                break
            here = parent
        candidates.append(os.path.abspath(os.getcwd()))
    for root in candidates:
        if wanted and all(os.path.exists(os.path.join(root, p)) for p in wanted):
            return root, []
    # Nothing satisfied every path. Report against the best candidate so the output names the
    # runs that are missing rather than dying with one line.
    best, best_hits, best_missing = candidates[0], -1, wanted
    for root in candidates:
        missing = [p for p in wanted if not os.path.exists(os.path.join(root, p))]
        hits = len(wanted) - len(missing)
        if hits > best_hits:
            best, best_hits, best_missing = root, hits, missing
    return best, best_missing


def index_runs(manifest, root):
    """Read every artefact, hash it, and index its numbers. Returns (index, files, problems)."""
    index, files, problems = {}, [], []
    runs = manifest.get("runs") or {}
    for run_id in sorted(runs):
        run = runs[run_id] if isinstance(runs[run_id], dict) else {}
        rel = run.get("path")
        if not rel:
            problems.append("run %s declares no path" % run_id)
            index[run_id] = {"numbers": [], "strings": [], "siblings": []}
            continue
        target = os.path.normpath(os.path.join(root, rel))
        paths = []
        if os.path.isdir(target):
            for name in sorted(os.listdir(target)):
                if name.endswith(".json"):
                    paths.append(os.path.join(target, name))
        elif os.path.isfile(target):
            paths = [target]
        else:
            problems.append("run %s: no artefact at %s" % (run_id, target))
        numbers, strings, siblings = [], [], []
        for path in paths:
            try:
                data = load_json(path)
            except (ValueError, OSError) as exc:
                problems.append("run %s: cannot read %s: %s" % (run_id, path, exc))
                continue
            shown = os.path.relpath(path, root).replace("\\", "/")
            files.append({"run": run_id, "path": shown, "sha256": sha256_of(path),
                          "bytes": os.path.getsize(path)})
            for pointer, value in numeric_leaves(data):
                numbers.append((shown, pointer, value))
            for pointer, text in string_leaves(data):
                strings.append((shown, pointer, text))
            for pointer, scalars in sibling_scalars(data):
                siblings.append((shown, pointer, scalars))
        index[run_id] = {"numbers": numbers, "strings": strings, "siblings": siblings}
    return index, files, problems


# ---------------------------------------------------------------------------
# Locating a measured claim in the artefacts
# ---------------------------------------------------------------------------

def close(got, want, rel=EXACT_REL):
    if want == 0.0:
        return abs(got) <= rel
    return abs(got - want) <= rel * abs(want)


def decimals(value):
    """Printed decimal places of a float, or None when it is a full-precision float.

    A value carrying fifteen or more significant decimals was not rounded by anybody, so the
    rounded tier must not fire on it.
    """
    text = repr(float(value))
    if "e" in text or "E" in text or "." not in text:
        return 0
    places = len(text.split(".")[1])
    return None if places > 6 else places


def ground(claim, index):
    """Locate one measured claim. Returns (value, tier, evidence) with value None when unfound."""
    run_id = claim.get("run")
    want = float(claim["value"])
    bucket = index.get(run_id)
    if bucket is None:
        return None, "no_such_run", "the claim names run %r, which the manifest does not" % run_id

    for path, pointer, value in bucket["numbers"]:
        if close(value, want):
            return value, "exact", "%s#%s" % (path, pointer)

    for path, pointer, text in bucket["strings"]:
        for token in NUMBER_IN_TEXT.findall(text):
            try:
                value = float(token.replace(",", ""))
            except ValueError:
                continue
            if close(value, want, 1e-12):
                return value, "text", "%s#%s contains %r" % (path, pointer, token)

    places = decimals(want)
    if places is not None:
        for path, pointer, value in bucket["numbers"]:
            if round(value, places) == round(want, places) and value != want:
                return value, "rounded", ("%s#%s holds %r, which is %r at the claim's own %d "
                                          "decimal place(s)" % (path, pointer, value, want, places))

    for factor, label in UNIT_FACTORS.get(claim.get("unit") or "", ()):
        for path, pointer, value in bucket["numbers"]:
            if close(value / factor, want):
                return value / factor, "unit", "%s#%s holds %r, %s" % (path, pointer, value, label)

    for path, pointer, scalars in bucket["siblings"]:
        for num_key, num in scalars.items():
            for den_key, den in scalars.items():
                if den_key == num_key or den in (0.0, 1.0):
                    continue
                if close(num / den, want):
                    return num / den, "ratio", ("%s#%s/%s divided by %s#%s/%s (%r / %r)"
                                                % (path, pointer, num_key, path, pointer,
                                                   den_key, num, den))
    return None, "ungrounded", "no artefact of run %r carries this number" % run_id


# ---------------------------------------------------------------------------
# Rebuilding
# ---------------------------------------------------------------------------

def evaluate(expression, env):
    """Arithmetic only. No calls, no attributes, no names except claim ids already in env."""
    tree = ast.parse(expression, mode="eval")

    def walk(node):
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise ValueError("non-numeric constant %r" % (node.value,))
            return float(node.value)
        if isinstance(node, ast.Name):
            if node.id not in env:
                raise KeyError(node.id)
            return env[node.id]
        if isinstance(node, ast.BinOp) and type(node.op) in OPS:
            return OPS[type(node.op)](walk(node.left), walk(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in OPS:
            return OPS[type(node.op)](walk(node.operand))
        raise ValueError("unsupported expression element %s" % type(node).__name__)

    return walk(tree)


class Rebuilder(object):
    """Resolves any claim to a number, using artefact values wherever the manifest says measured."""

    def __init__(self, claims, index):
        self.claims = claims
        self.index = index
        self.values = {}
        self.tiers = {}
        self.evidence = {}
        self.failed = {}

    def value(self, claim_id, stack=()):
        if claim_id in self.values:
            return self.values[claim_id]
        if claim_id in stack:
            raise ValueError("circular definition: %s" % " -> ".join(stack + (claim_id,)))
        claim = self.claims.get(claim_id)
        if claim is None:
            raise KeyError(claim_id)
        kind = claim.get("kind")
        if kind == "measured":
            found, tier, why = ground(claim, self.index)
            self.tiers[claim_id] = tier
            self.evidence[claim_id] = why
            if found is None:
                self.failed[claim_id] = why
                # The declared value keeps the walk going so one hole does not hide the rest of
                # the tree, but every claim above it is reported UNGROUNDED regardless.
                found = float(claim["value"])
            self.values[claim_id] = found
            return found
        if claim.get("formula"):
            env = {}
            for name in claim.get("inputs") or []:
                env[name] = self.value(name, stack + (claim_id,))
            result = evaluate(claim["formula"], env)
            self.tiers[claim_id] = "rebuilt"
            self.values[claim_id] = result
            return result
        # supplied, published, assumption, projection: an input to the report by declaration.
        # There is no artefact behind them and it would be dishonest to invent one.
        self.tiers[claim_id] = kind or "undeclared"
        self.evidence[claim_id] = "declared %r, not measured, so no artefact backs it" % kind
        self.values[claim_id] = float(claim["value"])
        return self.values[claim_id]

    def tree(self, claim_id, seen=None):
        """Every claim in the input tree of claim_id, itself included."""
        if seen is None:
            seen = set()
        if claim_id in seen:
            return seen
        seen.add(claim_id)
        claim = self.claims.get(claim_id) or {}
        if claim.get("formula"):
            for name in claim.get("inputs") or []:
                self.tree(name, seen)
        return seen


def verdict_for(declared, rebuilt, strict):
    rel = abs(rebuilt - declared) / (abs(declared) if declared else 1.0)
    if close(rebuilt, declared):
        return "REBUILT", rel
    places = decimals(declared)
    if not strict and places is not None and round(rebuilt, places) == declared:
        return "REBUILT_ROUNDED", rel
    if rel <= GATE_REL:
        return "DRIFT", rel
    return "MISMATCH", rel


def blame_rounded_input(rebuilder, claim_id, records):
    """Name the upstream claim whose stored value was rounded, when there is one.

    A drift almost always has a single cause: some intermediate is stored to fewer digits than it
    computes to, and something downstream divided by the stored one. Saying which claim it was
    turns a number nobody can act on into a one-line fix.
    """
    for other in sorted(rebuilder.tree(claim_id)):
        if other == claim_id:
            continue
        record = records.get(other)
        if record and record["verdict"] == "REBUILT_ROUNDED":
            return ("upstream %s is stored rounded: the manifest holds %r, the artefacts rebuild "
                    "it to %r" % (other, record["declared"], record["rebuilt"]))
    return None


def run_verification(manifest_path, root_arg, strict, require_all_measured):
    manifest = load_json(manifest_path)
    claims = claims_of(manifest)
    root, missing = resolve_root(manifest, manifest_path, root_arg)
    index, files, problems = index_runs(manifest, root)
    for path in missing:
        problems.append("run path %s does not exist under root %s" % (path, root))

    rebuilder = Rebuilder(claims, index)
    records, findings = {}, []
    derived_ids = [cid for cid, c in claims.items()
                   if isinstance(c, dict) and c.get("kind") == "derived"]

    for claim_id in sorted(derived_ids):
        claim = claims[claim_id]
        declared = claim.get("value")
        if not isinstance(declared, (int, float)) or isinstance(declared, bool):
            records[claim_id] = {"verdict": "ERROR", "declared": declared, "rebuilt": None,
                                 "rel": None, "why": "declared value is not a number"}
            continue
        declared = float(declared)
        try:
            rebuilt = rebuilder.value(claim_id)
        except KeyError as exc:
            records[claim_id] = {"verdict": "ERROR", "declared": declared, "rebuilt": None,
                                 "rel": None, "why": "references unknown claim %r" % exc.args[0]}
            continue
        except (ValueError, ZeroDivisionError, SyntaxError) as exc:
            records[claim_id] = {"verdict": "ERROR", "declared": declared, "rebuilt": None,
                                 "rel": None, "why": "formula failed: %s" % exc}
            continue
        holes = sorted(cid for cid in rebuilder.tree(claim_id) if cid in rebuilder.failed)
        if holes:
            records[claim_id] = {
                "verdict": "UNGROUNDED", "declared": declared, "rebuilt": rebuilt, "rel": None,
                "why": "measured input(s) not found in any artefact: "
                       + "; ".join("%s (%s)" % (h, rebuilder.failed[h]) for h in holes)}
            continue
        verdict, rel = verdict_for(declared, rebuilt, strict)
        record = {"verdict": verdict, "declared": declared, "rebuilt": rebuilt, "rel": rel,
                  "why": ""}
        records[claim_id] = record

    # Second pass so a drift can name an upstream claim whose own verdict is now known.
    for claim_id in sorted(derived_ids):
        record = records.get(claim_id)
        if record and record["verdict"] in ("DRIFT", "MISMATCH"):
            cause = blame_rounded_input(rebuilder, claim_id, records)
            record["why"] = cause or "no rounded upstream input explains it"

    for claim_id in sorted(derived_ids):
        record = records.get(claim_id)
        if record and record["verdict"] in ("DRIFT", "MISMATCH", "UNGROUNDED", "ERROR"):
            findings.append((claim_id, record))

    # Measured claims that feed nothing derived are still traceability gaps. They are reported,
    # and they only fail the run when the caller asks for that.
    measured_tiers = {}
    unlocated = {}
    for claim_id in sorted(claims):
        claim = claims[claim_id]
        if not isinstance(claim, dict) or claim.get("kind") != "measured":
            continue
        if claim_id not in rebuilder.values:
            try:
                rebuilder.value(claim_id)
            except (KeyError, ValueError):
                pass
        tier = rebuilder.tiers.get(claim_id, "ungrounded")
        measured_tiers[tier] = measured_tiers.get(tier, 0) + 1
        if claim_id in rebuilder.failed:
            unlocated[claim_id] = {
                "run": claim.get("run"), "value": claim.get("value"), "unit": claim.get("unit"),
                "why": rebuilder.failed[claim_id],
                "declared_waiver": claim.get("derivation_waiver", "")}

    ok = not findings and not problems
    if require_all_measured and unlocated:
        ok = False
    return {
        "tool": "verify_claims", "tool_version": TOOL_VERSION,
        "manifest": os.path.abspath(manifest_path), "root": root, "strict": bool(strict),
        "artifacts": files, "problems": problems,
        "counts": {
            "claims_total": len(claims),
            "derived_total": len(derived_ids),
            "rebuilt": sum(1 for r in records.values()
                           if r["verdict"] in ("REBUILT", "REBUILT_ROUNDED")),
            "rebuilt_exact": sum(1 for r in records.values() if r["verdict"] == "REBUILT"),
            "rebuilt_rounded": sum(1 for r in records.values()
                                   if r["verdict"] == "REBUILT_ROUNDED"),
            "failed": len(findings),
            "measured_total": sum(1 for c in claims.values()
                                  if isinstance(c, dict) and c.get("kind") == "measured"),
            "measured_unlocated": len(unlocated),
        },
        "measured_tiers": measured_tiers,
        "claims": records,
        "evidence": {cid: {"tier": rebuilder.tiers.get(cid), "where": rebuilder.evidence.get(cid)}
                     for cid in sorted(rebuilder.evidence)},
        "unlocated_measured": unlocated,
        "ok": ok,
    }


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def print_report(result):
    counts = result["counts"]
    print("verify_claims %s" % TOOL_VERSION)
    print("  manifest : %s" % result["manifest"])
    print("  root     : %s" % result["root"])
    print("  artefacts: %d file(s) read" % len(result["artifacts"]))
    for entry in result["artifacts"]:
        print("    %s  %s  %d bytes" % (entry["sha256"][:16], entry["path"], entry["bytes"]))
    if result["problems"]:
        print("  PROBLEMS reading the run set:")
        for problem in result["problems"]:
            print("    %s" % problem)
    print("")
    print("measured claims located in the artefacts, by tier:")
    for tier in sorted(result["measured_tiers"]):
        print("  %-12s %d" % (tier, result["measured_tiers"][tier]))
    if result["unlocated_measured"]:
        print("")
        print("measured claims NOT located in any artefact (%d):"
              % len(result["unlocated_measured"]))
        for claim_id in sorted(result["unlocated_measured"]):
            item = result["unlocated_measured"][claim_id]
            print("  %s = %r %s [run %s]" % (claim_id, item["value"], item["unit"] or "",
                                             item["run"]))
            print("      %s" % item["why"])
            if item["declared_waiver"]:
                print("      manifest's own waiver: %s" % item["declared_waiver"][:160])
    print("")
    print("derived claims rebuilt from raw artefacts: %d of %d"
          % (counts["rebuilt"], counts["derived_total"]))
    print("  exact                      %d" % counts["rebuilt_exact"])
    print("  equal at printed precision %d" % counts["rebuilt_rounded"])
    print("  FAILED                     %d" % counts["failed"])
    failures = [(cid, r) for cid, r in sorted(result["claims"].items())
                if r["verdict"] in ("DRIFT", "MISMATCH", "UNGROUNDED", "ERROR")]
    if failures:
        print("")
        print("findings:")
        for claim_id, record in failures:
            print("  [%s] %s" % (record["verdict"], claim_id))
            if record["rebuilt"] is not None and record["rel"] is not None:
                print("      manifest %r, artefacts rebuild %r (%.4g%% apart)"
                      % (record["declared"], record["rebuilt"], 100.0 * record["rel"]))
            if record["why"]:
                print("      %s" % record["why"])
    print("")
    print("RESULT: %s" % ("PASS" if result["ok"] else "FAIL"))


# ---------------------------------------------------------------------------
# Self test: proves the checker catches what it claims to catch, with no GPU and no network
# ---------------------------------------------------------------------------

SELFTEST_ARTEFACT = {
    "levels": [{"requests_ok": 8, "input_tokens": 16392,
                "prefill_tokens_per_s": 2161.828620466537}],
    "memory": {"total_mib": 32607},
    "log": {"raw": "GPU KV cache size: 239,148 tokens"},
}


def selftest_manifest(good=True):
    return {
        "schema": "claims/1",
        "runs": {"r": {"path": "artefact.json"}},
        "claims": {
            "tokens_per_request": {"kind": "measured", "run": "r", "unit": "count", "value": 2049.0,
                                   "basis": "per_request"},
            "card_gib": {"kind": "measured", "run": "r", "unit": "GiB", "value": 31.8427734375,
                         "basis": "per_device"},
            "kv_tokens": {"kind": "measured", "run": "r", "unit": "count", "value": 239148.0,
                          "basis": "total"},
            "prefill": {"kind": "measured", "run": "r", "unit": "tok/s",
                        "value": 2161.828620466537 if good else 9999.0, "basis": "total"},
            "sheet": {"kind": "published", "unit": "TFLOPS", "value": 100.0, "basis": "per_device"},
            "two_cards": {"kind": "derived", "unit": "GiB", "basis": "total",
                          "formula": "card_gib * 2", "inputs": ["card_gib"],
                          "value": 63.685546875},
            "prefill_pct": {"kind": "derived", "unit": "%", "basis": "ratio",
                            "formula": "100 * prefill / sheet", "inputs": ["prefill", "sheet"],
                            "value": 2161.828620466537},
            "unfindable": {"kind": "measured", "run": "r", "unit": "count", "value": 123456789.0,
                           "basis": "total"},
            "needs_unfindable": {"kind": "derived", "unit": "count", "basis": "total",
                                 "formula": "unfindable * 2", "inputs": ["unfindable"],
                                 "value": 246913578.0},
        },
    }


def selftest():
    """Three assertions, each about a different failure this tool exists to catch."""
    failures = []
    workdir = tempfile.mkdtemp(prefix="verify_claims_selftest_")
    artefact = os.path.join(workdir, "artefact.json")
    with open(artefact, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(SELFTEST_ARTEFACT, fh)

    good_path = os.path.join(workdir, "good.json")
    with open(good_path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(selftest_manifest(True), fh)
    good = run_verification(good_path, workdir, False, False)

    # 1. Every locating tier fires on data that is really there.
    tiers = good["evidence"]
    for claim_id, expected in (("prefill", "exact"), ("kv_tokens", "text"),
                               ("card_gib", "unit"), ("tokens_per_request", "ratio")):
        got = tiers.get(claim_id, {}).get("tier")
        if got != expected:
            failures.append("%s should have been located by the %s tier, got %r"
                            % (claim_id, expected, got))

    # 2. A derived claim over a measured value nobody recorded is UNGROUNDED, not a silent pass.
    if good["claims"]["needs_unfindable"]["verdict"] != "UNGROUNDED":
        failures.append("a derived claim resting on an unlocatable measurement was not reported "
                        "UNGROUNDED")
    if good["ok"]:
        failures.append("a manifest with an unlocatable measurement passed")

    # 3. A measured value edited in the manifest but not in the artefact is caught, and this is
    #    the check the gate cannot make: prefill_pct recomputes perfectly from the manifest.
    bad_path = os.path.join(workdir, "bad.json")
    bad = selftest_manifest(False)
    bad["claims"]["prefill_pct"]["value"] = 9999.0
    del bad["claims"]["unfindable"], bad["claims"]["needs_unfindable"]
    with open(bad_path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(bad, fh)
    tampered = run_verification(bad_path, workdir, False, False)
    if tampered["claims"]["prefill_pct"]["verdict"] != "UNGROUNDED":
        failures.append("a tampered measured value was not caught: prefill_pct came back %r"
                        % tampered["claims"]["prefill_pct"]["verdict"])

    for line in failures:
        print("SELFTEST FAILURE: %s" % line)
    print("selftest: %d check(s) failed" % len(failures))
    return 1 if failures else 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="rebuild a report's derived claims from the raw result artefacts")
    parser.add_argument("manifest", nargs="?", help="the claims manifest to verify")
    parser.add_argument("--root", help="directory the manifest's run paths are relative to. "
                                       "Default: walk up from the manifest until they resolve")
    parser.add_argument("--json", dest="json_out", help="write the full result record here")
    parser.add_argument("--strict", action="store_true",
                        help="require bit-exact agreement. Without it, a declared value that IS "
                             "the rebuilt value at its own printed precision passes")
    parser.add_argument("--require-all-measured", action="store_true",
                        help="also fail when a measured claim that feeds nothing derived cannot "
                             "be located in an artefact")
    parser.add_argument("--selftest", action="store_true",
                        help="run the built-in fixture checks. No GPU, no network, no manifest")
    args = parser.parse_args(argv)

    if args.selftest:
        return selftest()
    if not args.manifest:
        parser.error("a manifest path is required (or --selftest)")
    if not os.path.isfile(args.manifest):
        print("no such manifest: %s" % os.path.abspath(args.manifest))
        return 2
    try:
        result = run_verification(args.manifest, args.root, args.strict,
                                  args.require_all_measured)
    except (ValueError, OSError) as exc:
        print("cannot verify %s: %s" % (args.manifest, exc))
        return 2
    print_report(result)
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(result, fh, indent=2, sort_keys=True)
        print("wrote %s" % os.path.abspath(args.json_out))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
