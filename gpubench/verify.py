#!/usr/bin/env python3
"""
verify_report.py - deterministic pre-render gate for a measurement report.

Reads a claims manifest emitted by the report generator and checks the invariants
catalogued in references/checks.md. Every check here fires on a defect that once got
past a careful author, which is the only reason it is in the list.

    python verify_report.py claims.json
    python verify_report.py claims.json --previous claims-prev.json
    python verify_report.py claims.json --rendered report.html --findings findings.json
    python verify_report.py --demo          # run against a fixture full of real defects

Exit codes: 0 clean or warnings only, 1 one or more errors, 2 the manifest itself is broken.

Standard library only, so it can run inside any build without adding a dependency.
"""

from __future__ import annotations

import argparse
import ast
import json
import math
import re
import sys
from datetime import datetime
from pathlib import Path

SCHEMA = "claims/1"

# Basis values a quantity can carry. Mixing them in one expression without an explicit
# conversion is the defect that keeps capacity arithmetic from closing.
BASES = {"per_device", "per_shard", "total", "per_sequence", "per_token", "per_request", "ratio", "scalar"}

UNIT_FAMILIES = {
    "bytes": {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "TiB": 1024**4,
              "KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12},
    "time": {"ns": 1e-9, "us": 1e-6, "ms": 1e-3, "s": 1.0},
    "rate": {"tok/s": 1.0, "req/s": 1.0, "emb/s": 1.0},
    "bandwidth": {"GB/s": 1e9, "MB/s": 1e6, "GiB/s": float(1024**3)},
    "compute": {"TFLOPS": 1e12, "TOPS": 1e12, "GFLOPS": 1e9},
    "power": {"W": 1.0, "kW": 1e3},
    "percent": {"%": 1.0},
    "count": {"": 1.0, "count": 1.0},
    "currency": {},
}

# Numerals in prose that are not measurements. Everything else must be a citation.
# Deliberately narrow: a decimal that looks like a version number is also what a stale
# latency figure looks like, so versions are allowed only where the word says so.
YEAR = re.compile(r"^(?:19|20)\d{2}$")
STRUCTURAL_CONTEXT = re.compile(
    r"(?i)\b(?:section|sections|figure|fig\.?|table|appendix|chapter|step|version|v|item|note)\s*$"
)

ENTITY_LEAK = re.compile(r"&(?:amp|mdash|ndash|lt|gt|quot|#\d+);")
PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z0-9_.\-]+)\s*\}\}")
BARE_NUMERAL = re.compile(r"(?<![\w.]) -? \d[\d,]* (?: \.\d+ )? (?![\w])", re.X)


# --------------------------------------------------------------------------------------
# findings


class Findings:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def add(self, severity: str, check: str, message: str, **extra) -> None:
        self.items.append({"severity": severity, "check": check, "message": message, **extra})

    def error(self, check: str, message: str, **extra) -> None:
        self.add("error", check, message, **extra)

    def warn(self, check: str, message: str, **extra) -> None:
        self.add("warn", check, message, **extra)

    @property
    def errors(self) -> list[dict]:
        return [f for f in self.items if f["severity"] == "error"]

    @property
    def warnings(self) -> list[dict]:
        return [f for f in self.items if f["severity"] == "warn"]


# --------------------------------------------------------------------------------------
# formula evaluation


_ALLOWED_NODES = (
    ast.Expression, ast.BinOp, ast.UnaryOp, ast.Name, ast.Load,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow, ast.USub, ast.UAdd,
    ast.Constant, ast.Tuple,
)


def safe_eval(expr: str, env: dict[str, float]) -> float:
    """Arithmetic over claim keys. No calls, no attributes, no subscripts, no comprehensions."""
    tree = ast.parse(expr, mode="eval")
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise ValueError(f"disallowed syntax {type(node).__name__} in formula: {expr}")
        if isinstance(node, ast.Name) and node.id not in env:
            raise KeyError(node.id)
    return float(eval(compile(tree, "<formula>", "eval"), {"__builtins__": {}}, dict(env)))


def formula_names(expr: str) -> list[str]:
    return sorted({n.id for n in ast.walk(ast.parse(expr, mode="eval")) if isinstance(n, ast.Name)})


def additive_operands(expr: str) -> list[tuple[list[str], list[str]]]:
    """Name leaves on each side of every + and - in the expression.

    Multiplying a per-request count by a total-basis rate is ordinary dimensional work.
    *Adding* a per-device quantity to a total-basis one is the defect. So the basis and
    unit checks look only at additive positions, where the two sides must agree.
    """
    def leaves(node: ast.AST) -> list[str]:
        return [n.id for n in ast.walk(node) if isinstance(n, ast.Name)]

    out = []
    for node in ast.walk(ast.parse(expr, mode="eval")):
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub)):
            out.append((leaves(node.left), leaves(node.right)))
    return out


def unit_family(unit: str) -> str | None:
    for family, members in UNIT_FAMILIES.items():
        if unit in members:
            return family
    return None


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


# --------------------------------------------------------------------------------------
# checks


def check_manifest_shape(m: dict, f: Findings) -> None:
    if m.get("schema") != SCHEMA:
        f.error("manifest", f"schema must be {SCHEMA!r}, found {m.get('schema')!r}")
    for claim_id, c in m.get("claims", {}).items():
        if "value" not in c:
            f.error("manifest", f"claim {claim_id} has no value")
        if c.get("kind") not in {"measured", "derived", "assumption", "published", "projection", "supplied"}:
            f.error("manifest", f"claim {claim_id} has no valid kind", claim=claim_id)
        basis = c.get("basis")
        if basis is not None and basis not in BASES:
            f.error("C1", f"claim {claim_id} has unknown basis {basis!r}", claim=claim_id)
        if c.get("kind") == "measured" and not c.get("run"):
            f.error("A4", f"measured claim {claim_id} names no run", claim=claim_id)


def check_derivations(m: dict, f: Findings) -> None:
    """B1: nothing derived is ever typed. Recompute it or fail."""
    claims = m["claims"]
    env = {k: float(c["value"]) for k, c in claims.items() if isinstance(c.get("value"), (int, float))}
    for claim_id, c in claims.items():
        if c.get("kind") != "derived":
            continue
        expr = c.get("formula")
        if not expr:
            f.error("B1", f"derived claim {claim_id} declares no formula", claim=claim_id)
            continue
        try:
            got = safe_eval(expr, env)
        except KeyError as exc:
            f.error("B1", f"{claim_id} references unknown claim {exc.args[0]!r}", claim=claim_id)
            continue
        except (ValueError, SyntaxError, ZeroDivisionError) as exc:
            f.error("B1", f"{claim_id} formula failed: {exc}", claim=claim_id)
            continue
        want = float(c["value"])
        tol = float(c.get("tolerance", 0.005))
        denom = abs(want) if want else 1.0
        rel = abs(got - want) / denom
        if rel > tol:
            f.error(
                "B1",
                f"{claim_id} does not recompute: printed {want:g}, formula gives {got:g} "
                f"({rel:.2%} off, tolerance {tol:.2%})",
                claim=claim_id, printed=want, computed=got,
            )
        # C1/C2: basis and unit hygiene, checked where it actually matters
        def attrs(names, field):
            vals = {claims[n].get(field) for n in names if n in claims}
            return {v for v in vals if v not in (None, "scalar", "ratio")}

        for left, right in additive_operands(expr):
            lb, rb = attrs(left, "basis"), attrs(right, "basis")
            if len(lb) == 1 and len(rb) == 1 and lb != rb and not c.get("basis_conversion"):
                f.error(
                    "C1",
                    f"{claim_id} adds a {lb.pop()} quantity to a {rb.pop()} one with no declared "
                    "conversion. This is how a per-device figure ends up compared against a total, "
                    "and it is why a capacity derivation stops closing.",
                    claim=claim_id,
                )
            lf = {unit_family(u) for u in attrs(left, "unit")} - {None}
            rf = {unit_family(u) for u in attrs(right, "unit")} - {None}
            if lf and rf and lf != rf and not c.get("unit_conversion"):
                f.error("C2", f"{claim_id} adds {sorted(lf)} to {sorted(rf)}", claim=claim_id)
            lu, ru = attrs(left, "unit"), attrs(right, "unit")
            if len(lu) == 1 and len(ru) == 1 and lu != ru and not c.get("unit_conversion"):
                f.warn("C2", f"{claim_id} adds {lu.pop()} to {ru.pop()}; confirm the scale factor",
                       claim=claim_id)

        # Comparing a per-sequence figure against a whole pool is the same defect in ratio form.
        if c.get("basis") == "ratio" and isinstance(expr, str) and "/" in expr:
            bases = attrs(formula_names(expr), "basis")
            if len(bases) > 1 and not c.get("basis_conversion"):
                f.warn(
                    "C1",
                    f"{claim_id} is a ratio over mixed bases {sorted(bases)}. Ratios like this "
                    "recompute cleanly while answering the wrong question; state the denominator "
                    "basis beside the printed percentage.",
                    claim=claim_id,
                )


def check_equalities(m: dict, f: Findings) -> None:
    """A1: one quantity, one value. The check that catches 233 against 204.5."""
    claims = m["claims"]
    for group in m.get("equalities", []):
        keys = group["keys"] if isinstance(group, dict) else group
        tol = float(group.get("tolerance", 0.002)) if isinstance(group, dict) else 0.002
        missing = [k for k in keys if k not in claims]
        if missing:
            f.error("A1", f"equality group references unknown claims {missing}")
            continue
        values = {k: float(claims[k]["value"]) for k in keys}
        lo, hi = min(values.values()), max(values.values())
        denom = abs(hi) or 1.0
        if (hi - lo) / denom > tol:
            pairs = ", ".join(f"{k}={v:g}" for k, v in values.items())
            f.error(
                "A1",
                f"the same quantity is printed with different values: {pairs} "
                f"(spread {(hi - lo) / denom:.2%}, tolerance {tol:.2%})",
                keys=list(keys),
            )
    # Two claims sharing a label are the same quantity by another name.
    by_label: dict[str, list[str]] = {}
    for claim_id, c in claims.items():
        if c.get("label"):
            by_label.setdefault(c["label"].strip().lower(), []).append(claim_id)
    declared = {frozenset(g["keys"] if isinstance(g, dict) else g) for g in m.get("equalities", [])}
    for label, keys in by_label.items():
        if len(keys) < 2 or frozenset(keys) in declared:
            continue
        values = {k: float(claims[k]["value"]) for k in keys}
        if max(values.values()) - min(values.values()) != 0:
            f.warn("A1", f"claims sharing the label {label!r} disagree: "
                         + ", ".join(f"{k}={v:g}" for k, v in values.items()), keys=keys)


def check_prose(m: dict, f: Findings) -> None:
    """A2 no bare numerals, A3 comparatives are true, F4 citations resolve."""
    claims = m["claims"]
    ops = {
        "gt": (lambda a, b: a > b, "greater than"),
        "lt": (lambda a, b: a < b, "less than"),
        "gte": (lambda a, b: a >= b, "at least"),
        "lte": (lambda a, b: a <= b, "at most"),
        "eq": (lambda a, b: math.isclose(a, b, rel_tol=1e-9), "equal to"),
        "approx": (None, "approximately equal to"),
        "within_pct": (None, "within a percentage of"),
        "ratio_between": (None, "a ratio inside a range"),
    }
    for block in m.get("prose", []):
        text = block.get("text", "")
        block_id = block.get("id", "<unnamed>")

        for key in PLACEHOLDER.findall(text):
            if key not in claims:
                f.error("F4", f"prose {block_id} cites unknown claim {{{{{key}}}}}", block=block_id)

        stripped = PLACEHOLDER.sub(" ", text)
        allowed = {str(x) for x in block.get("allow_literals", [])}
        for match in BARE_NUMERAL.finditer(stripped):
            token = match.group().strip().lstrip("-")
            if token in allowed or YEAR.match(token.replace(",", "")):
                continue
            # "section 17", "figure 3", "version 8.6": structural, not measured.
            if STRUCTURAL_CONTEXT.search(stripped[: match.start()]):
                continue
            f.error(
                "A2",
                f"prose {block_id} contains the bare numeral {token!r}. Numbers in prose go "
                "stale silently when a table is re-measured; cite the claim key instead, or "
                "list it under allow_literals if it is genuinely not a measurement.",
                block=block_id, numeral=token,
            )

        assertion = block.get("assert")
        if not assertion:
            continue
        op = assertion.get("op")
        if op not in ops:
            f.error("A3", f"prose {block_id} declares unknown comparison {op!r}", block=block_id)
            continue
        left_key, right_key = assertion.get("left"), assertion.get("right")
        if left_key not in claims or right_key not in claims:
            f.error("A3", f"prose {block_id} compares unknown claims", block=block_id)
            continue
        left, right = float(claims[left_key]["value"]), float(claims[right_key]["value"])
        lb, rb = claims[left_key].get("basis"), claims[right_key].get("basis")
        if lb and rb and lb != rb and lb not in ("scalar", "ratio") and rb not in ("scalar", "ratio"):
            f.error("C1", f"prose {block_id} compares {lb} against {rb} without conversion", block=block_id)
        if op in ("gt", "lt", "gte", "lte", "eq"):
            ok = ops[op][0](left, right)
        elif op == "approx":
            ok = math.isclose(left, right, rel_tol=float(assertion.get("rel_tol", 0.02)))
        elif op == "within_pct":
            ok = abs(left - right) / (abs(right) or 1.0) <= float(assertion["pct"]) / 100.0
        else:  # ratio_between
            ratio = left / right if right else float("inf")
            ok = float(assertion["min"]) <= ratio <= float(assertion["max"])
        if not ok:
            f.error(
                "A3",
                f"prose {block_id} asserts {left_key} is {ops[op][1]} {right_key}, but "
                f"{left_key}={left:g} and {right_key}={right:g}",
                block=block_id,
            )


def check_provenance(m: dict, prev: dict | None, f: Findings) -> None:
    """A4: tables draw from one run, and re-measurement claims are true."""
    claims = m["claims"]
    for table_id, table in m.get("tables", {}).items():
        cells = [claims[k] for k in table.get("cells", []) if k in claims]
        runs = {c.get("run") for c in cells if c.get("kind") == "measured" and c.get("run")}
        if len(runs) > 1 and not table.get("blended"):
            f.error(
                "A4",
                f"table {table_id} blends runs {sorted(runs)} without declaring it. A blended "
                "table is legitimate and has to say which rows came from where.",
                table=table_id,
            )
        if table.get("blended") and not table.get("blend_note"):
            f.error("A4", f"table {table_id} is blended but names no source for each row", table=table_id)

    if prev is None:
        return
    prev_claims = prev.get("claims", {})

    # Values that moved must appear in the changelog.
    changed = []
    for claim_id, c in claims.items():
        old = prev_claims.get(claim_id)
        if not old or not isinstance(old.get("value"), (int, float)):
            continue
        if not isinstance(c.get("value"), (int, float)):
            continue
        if abs(float(c["value"]) - float(old["value"])) > 1e-12:
            changed.append(claim_id)
    logged: set[str] = set()
    for entry in m.get("changelog", []):
        logged |= set(entry.get("claims_changed", []))
        logged |= set(entry.get("claims_remeasured", []))
    silent = sorted(set(changed) - logged)
    if silent:
        f.error(
            "A4",
            f"{len(silent)} value(s) changed since the previous edition with no changelog row: "
            + ", ".join(silent[:8]) + ("..." if len(silent) > 8 else ""),
            claims=silent,
        )

    # A re-measurement claim is checked against the timestamps, not taken on trust.
    for entry in m.get("changelog", []):
        for claim_id in entry.get("claims_remeasured", []):
            now, before = claims.get(claim_id), prev_claims.get(claim_id)
            if not now or not before:
                continue
            t_now, t_before = parse_ts(now.get("measured_at")), parse_ts(before.get("measured_at"))
            if t_now and t_before and t_now <= t_before:
                f.error(
                    "A4",
                    f"changelog {entry.get('version')} claims {claim_id} was re-measured, but its "
                    f"measurement time did not move ({now.get('measured_at')}).",
                    claim=claim_id,
                )


def check_sampling(m: dict, f: Findings) -> None:
    """D1 and D2: a percentile without its sample size is decoration."""
    for p in m.get("percentiles", []):
        key, q, n = p.get("key"), p.get("q"), p.get("n")
        if not n:
            f.error("D1", f"percentile {key} discloses no sample size", claim=key)
            continue
        rank = math.ceil(float(q) * int(n))
        from_top = int(n) - rank
        p.setdefault("rank", rank)
        if from_top <= 2:
            f.warn(
                "D2",
                f"{key} is a p{float(q) * 100:g} over n={n}, which resolves to ordered sample "
                f"{rank} of {n}, the {['worst', 'second worst', 'third worst'][from_top]} value. "
                "That is an extreme wearing a percentile's name; print the rank beside it, "
                "raise n toward 100, or report a max and say so.",
                claim=key, n=n, rank=rank,
            )


def check_load_shape(m: dict, f: Findings) -> None:
    """D3 and D4: whole waves, enough of them, and an arrival model that is stated."""
    arrival = (m.get("report") or {}).get("arrival_model")
    if arrival not in {"closed_loop", "open_loop_constant", "open_loop_poisson"}:
        f.error(
            "D4",
            "no arrival model declared. A latency percentile quoted as a service level has to say "
            "whether requests arrived in a burst or a stream: a closed-loop harness cannot produce "
            "the queue build-up that generates real tail latency.",
        )

    claims = m["claims"]
    for level in m.get("levels", []):
        name, conc, n = level.get("name"), int(level.get("concurrency", 0)), int(level.get("requests", 0))
        if conc <= 0 or n <= 0:
            f.error("D3", f"level {name} declares no concurrency or request count", level=name)
            continue
        if n % conc:
            f.error(
                "D3",
                f"level {name}: {n} requests at concurrency {conc} is not a whole number of waves. "
                f"The final wave runs at concurrency {n % conc}, which depresses throughput by an "
                "amount that depends on how badly the count divides and reads as scatter.",
                level=name,
            )
        waves = n / conc
        if waves < 3:
            f.warn(
                "D3",
                f"level {name} is {waves:g} wave(s). With so few waves the level is a burst rather "
                "than a steady state: it is all ramp-up and drain, so a throughput figure from it "
                "should not be called sustained.",
                level=name, waves=waves,
            )
        # The tell for a single synchronised wave: duration equals the slowest request.
        e2e_key, dur = level.get("e2e_p95_key"), level.get("duration_s")
        if e2e_key in claims and dur:
            e2e = float(claims[e2e_key]["value"])
            if e2e and abs(float(dur) - e2e) / e2e < 0.01 and waves > 1:
                f.warn(
                    "D3",
                    f"level {name} reports {waves:g} waves, but its duration equals the end-to-end "
                    "p95, which is the signature of one synchronised wave. Check the wave accounting.",
                    level=name,
                )

    for s in m.get("sustained", []):
        if not s.get("duration_s"):
            f.error("D5", f"sustained figure {s.get('key')} states no duration", claim=s.get("key"))
        elif s.get("reached_steady_state") is None:
            f.warn("D5", f"sustained figure {s.get('key')} does not say whether the measured quantity "
                         "had stopped moving when the run ended", claim=s.get("key"))


def check_roofs(m: dict, f: Findings) -> None:
    """E1 and E2: a roof measured with other work resident is a floor, and it must say so."""
    claims = m["claims"]
    for ceiling in m.get("ceilings", []):
        key, mode = ceiling.get("key"), ceiling.get("mode")
        if mode not in {"shared", "exclusive"}:
            f.error(
                "E1",
                f"ceiling {key} declares no measurement mode. A percentage of a shared-mode roof has "
                "a floor in its denominator and does not compare against an exclusive-mode one.",
                claim=key,
            )
        elif mode == "shared" and not ceiling.get("caveat_anchor"):
            f.error(
                "E1",
                f"ceiling {key} is shared-mode with no caveat anchored beside the fraction that uses "
                "it. A caveat only in the limitations section is not where the reader meets the claim.",
                claim=key,
            )
        kind = claims.get(key, {}).get("kind")
        if kind in {"published", "derived"} and ceiling.get("from_vendor_headline"):
            f.warn(
                "E2",
                f"ceiling {key} descends from a vendor headline. Using a marketing figure as the roof "
                "makes the roof-to-workload gap unfalsifiable, which is the point of drawing it.",
                claim=key,
            )


def check_gate(m: dict, f: Findings) -> None:
    """G1 and G2: speed is only accepted beside a quality gate that ran and passed."""
    gate = m.get("gate")
    if not gate:
        f.error(
            "G1",
            "no quality gate recorded. A benchmark that measures only speed rewards a stack that got "
            "faster by getting worse, so a more aggressive quantisation or a truncated context reads "
            "as an improvement.",
        )
        return
    if not gate.get("passed"):
        f.error("G1", "the quality gate did not pass; performance figures taken in this window are unsound")
    if gate.get("window_run") and gate["window_run"] not in (m.get("runs") or {}):
        f.error("G1", f"the gate names run {gate['window_run']!r}, which is not in the run table")
    if not gate.get("cases_published"):
        f.error("G2", "the gate's cases are not published. A gate nobody can re-run is an assertion.")


def check_render(m: dict, rendered: Path | None, f: Findings) -> None:
    """F1 and F2: escaping, and figures that carry their data."""
    for fig in m.get("figures", []):
        if not fig.get("table_view"):
            f.error("F2", f"figure {fig.get('id')} has no table view; a chart without its values is "
                          "an assertion", figure=fig.get("id"))
    for block in m.get("prose", []):
        if ENTITY_LEAK.search(block.get("text", "")):
            f.error("F1", f"prose {block.get('id')} contains an unescaped HTML entity", block=block.get("id"))
    if rendered and rendered.exists():
        text = rendered.read_text(encoding="utf-8", errors="replace")
        body = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", text)
        body = re.sub(r"(?s)<[^>]+>", " ", body)
        leaks = sorted(set(ENTITY_LEAK.findall(body)))
        if leaks:
            f.error("F1", f"rendered document shows literal entities in visible text: {leaks}")


# --------------------------------------------------------------------------------------
# driver


def verify(manifest: dict, previous: dict | None = None, rendered: Path | None = None) -> Findings:
    f = Findings()
    check_manifest_shape(manifest, f)
    if f.errors and "claims" not in manifest:
        return f
    check_derivations(manifest, f)
    check_equalities(manifest, f)
    check_prose(manifest, f)
    check_provenance(manifest, previous, f)
    check_sampling(manifest, f)
    check_load_shape(manifest, f)
    check_roofs(manifest, f)
    check_gate(manifest, f)
    check_render(manifest, rendered, f)
    return f


def report(f: Findings, stream=sys.stdout) -> None:
    order = {"error": 0, "warn": 1}
    for item in sorted(f.items, key=lambda i: (order[i["severity"]], i["check"])):
        mark = "ERROR" if item["severity"] == "error" else "warn "
        print(f"  [{mark}] {item['check']}  {item['message']}", file=stream)
    print(file=stream)
    print(f"  {len(f.errors)} error(s), {len(f.warnings)} warning(s)", file=stream)


DEMO = {
    "schema": SCHEMA,
    "report": {"version": "8.6-demo"},  # D4: arrival model missing
    "runs": {"primary": {"started": "2026-08-25T11:01:00Z"},
             "tool": {"started": "2026-08-25T17:08:00Z"}},
    "claims": {
        "generation_length": {"value": 128, "unit": "count", "basis": "per_request",
                              "kind": "assumption", "label": "forced generation length"},
        "requests_per_second_c8": {"value": 1.82, "unit": "req/s", "basis": "total",
                                   "kind": "measured", "run": "primary",
                                   "measured_at": "2026-08-25T11:05:00Z"},
        "throughput_c8_capacity": {"value": 233, "unit": "tok/s", "basis": "total",
                                   "kind": "measured", "run": "primary",
                                   "measured_at": "2026-08-25T11:05:00Z",
                                   "label": "aggregate throughput at concurrency 8"},
        "throughput_c8_repro": {"value": 204.5, "unit": "tok/s", "basis": "total",
                                "kind": "measured", "run": "tool",
                                "measured_at": "2026-08-25T17:08:00Z",
                                "label": "aggregate throughput at concurrency 8"},
        "ttft_p95_c8": {"value": 1.93, "unit": "s", "basis": "per_request", "kind": "measured",
                        "run": "primary", "measured_at": "2026-08-25T11:05:00Z"},
        "ttft_p95_c16": {"value": 3.79, "unit": "s", "basis": "per_request", "kind": "measured",
                         "run": "primary", "measured_at": "2026-08-25T11:05:00Z"},
        "e2e_p95_c32": {"value": 11.50, "unit": "s", "basis": "per_request", "kind": "measured",
                        "run": "primary", "measured_at": "2026-08-25T11:05:00Z"},
        "pool_fixed_state": {"value": 72.0, "unit": "MiB", "basis": "per_sequence", "kind": "derived",
                             "formula": "72.0", "label": "fixed recurrent state per sequence"},
        "pool_kv_per_seq": {"value": 20.0, "unit": "MiB", "basis": "per_sequence", "kind": "derived",
                            "formula": "20.0"},
        "pool_parts_total": {"value": 92.0, "unit": "MiB", "basis": "per_sequence", "kind": "derived",
                             "formula": "pool_fixed_state + pool_kv_per_seq"},
        "pool_measured_per_seq": {"value": 99.8, "unit": "MiB", "basis": "per_sequence",
                                  "kind": "measured", "run": "primary",
                                  "measured_at": "2026-08-25T11:05:00Z"},
        "pool_residual_per_seq": {"value": 7.8, "unit": "MiB", "basis": "per_sequence", "kind": "derived",
                                  "formula": "pool_measured_per_seq - pool_parts_total"},
        "interconnect_ceiling_2048": {"value": 2777, "unit": "tok/s", "basis": "total", "kind": "derived",
                                      "formula": "2777"},
        "prefill_measured_2048": {"value": 2162, "unit": "tok/s", "basis": "total", "kind": "measured",
                                  "run": "primary", "measured_at": "2026-08-25T11:05:00Z"},
        # B1: printed as 82%, but 2162/2777 is 78%. A derived value that was typed.
        "prefill_fraction_of_ceiling_2048": {
            "value": 82.0, "unit": "%", "basis": "ratio", "kind": "derived",
            "formula": "100 * prefill_measured_2048 / interconnect_ceiling_2048", "tolerance": 0.005},
        # C1: a per-device state compared against a total-basis pool with no conversion.
        "pool_total": {"value": 14.44, "unit": "GiB", "basis": "total", "kind": "measured",
                       "run": "primary", "measured_at": "2026-08-25T11:05:00Z"},
        # The arithmetic closes and the answer is still wrong: a per-sequence cost measured
        # per device, divided by a total-basis pool. Only the basis check catches this.
        "seq_share_of_pool": {"value": 0.00675, "unit": "", "basis": "ratio", "kind": "derived",
                              "formula": "pool_measured_per_seq / pool_total / 1024"},
    },
    "equalities": [{"keys": ["throughput_c8_capacity", "throughput_c8_repro"], "tolerance": 0.005}],
    "tables": {
        "capacity_sweep": {"cells": ["throughput_c8_capacity", "throughput_c8_repro"]},
    },
    "prose": [
        {"id": "sec24_recommendation",
         "text": "Time to first token at p95 rises from 1.91 s to {{ttft_p95_c16}} at 16 concurrent.",
         "allow_literals": [16]},
        {"id": "sec17_closure",
         "text": "The residual is larger than the parts, which is the direction block allocation predicts.",
         "assert": {"op": "gt", "left": "pool_residual_per_seq", "right": "pool_parts_total"}},
    ],
    "percentiles": [
        {"key": "ttft_p95_c8", "q": 0.95, "n": 32, "level": "c8"},
        {"key": "e2e_p95_c32", "q": 0.95, "n": 32, "level": "c32"},
    ],
    "levels": [
        {"name": "c8", "concurrency": 8, "requests": 32, "duration_s": 17.6},
        {"name": "c32", "concurrency": 32, "requests": 32, "duration_s": 11.5,
         "e2e_p95_key": "e2e_p95_c32"},
        {"name": "c48", "concurrency": 48, "requests": 100, "duration_s": 30.0},
    ],
    "ceilings": [
        {"key": "interconnect_ceiling_2048", "mode": "shared"},
    ],
    "figures": [{"id": "fig8_concurrency", "table_view": True},
                {"id": "fig9_latency", "table_view": False}],
    "gate": {"ran_at": "2026-08-25T19:37:00Z", "passed": True, "cases_published": False,
             "window_run": "instrumentation"},
}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("manifest", nargs="?", help="path to claims.json")
    ap.add_argument("--previous", help="the previous edition's claims.json, for staleness checks")
    ap.add_argument("--rendered", help="the rendered document, for escaping checks")
    ap.add_argument("--findings", help="write findings as JSON to this path")
    ap.add_argument("--warnings-as-errors", action="store_true")
    ap.add_argument("--demo", action="store_true", help="run against a fixture of real defects")
    args = ap.parse_args(argv)

    if args.demo:
        manifest, previous = DEMO, None
        print("verify_report.py --demo: a fixture carrying defects taken from real editions\n")
    else:
        if not args.manifest:
            ap.error("a manifest path is required unless --demo is given")
        try:
            manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"cannot read manifest: {exc}", file=sys.stderr)
            return 2
        previous = None
        if args.previous:
            try:
                previous = json.loads(Path(args.previous).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                print(f"cannot read previous manifest: {exc}", file=sys.stderr)
                return 2

    findings = verify(manifest, previous, Path(args.rendered) if args.rendered else None)
    report(findings)

    if args.findings:
        Path(args.findings).write_text(json.dumps(findings.items, indent=2), encoding="utf-8")

    failed = bool(findings.errors) or (args.warnings_as_errors and bool(findings.warnings))
    if failed:
        print("\n  Render blocked. Fix the errors, or re-measure. Never edit a measured value to "
              "satisfy a check.", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
