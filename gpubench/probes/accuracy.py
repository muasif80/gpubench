#!/usr/bin/env python3
"""Accuracy gate: the P0 item every previous version listed as missing.

A speed benchmark cannot tell "faster" from "worse". MLPerf gates submissions on reaching 99% of
a reference accuracy precisely so throughput cannot be bought by degrading quality. This closes
that hole with the two checks that need no external dataset and no reference model:

  determinism  same prompt, greedy decode, run twice: does the stack return the same tokens?
                A non-deterministic greedy decode means batching, kernel selection or a scheduler
                path is changing results, and any A/B comparison of throughput is then unsound.
  exact match   short prompts with a single verifiable answer. Catches a stack quantised into
                incoherence, which is the failure a speed benchmark otherwise rewards.

Standard library only. Environment: BASE_URL, MODEL, GPUBENCH_ACC_REPEATS.
"""
import json
import os
import re
import time
import urllib.request

BASE = os.environ.get("BASE_URL", "http://127.0.0.1:8000/v1")
MODEL = os.environ.get("MODEL", "Qwen/Qwen3.6-27B-FP8")
REPEATS = int(os.environ.get("GPUBENCH_ACC_REPEATS", "2"))

# Deliberately trivial and unambiguous. The point is not to measure intelligence, it is to detect
# a stack that has stopped working correctly while still being fast.
CASES = [
    ("What is 17 plus 26? Reply with the number only.", r"\b43\b"),
    ("What is 12 times 12? Reply with the number only.", r"\b144\b"),
    ("What is 100 minus 37? Reply with the number only.", r"\b63\b"),
    ("What is half of 250? Reply with the number only.", r"\b125\b"),
    ("How many days are in a week? Reply with the number only.", r"\b7\b"),
    ("What is the capital city of France? Reply with one word.", r"(?i)paris"),
    ("What colour is the sky on a clear day? Reply with one word.", r"(?i)blue"),
    ("Complete: two, four, six, ____ . Reply with the number only.", r"\b8\b|eight"),
    ("Is 9 greater than 4? Answer yes or no.", r"(?i)\byes\b"),
    ("Spell the word CAT in lowercase. Reply with one word.", r"(?i)\bcat\b"),
]


def ask(prompt):
    body = json.dumps({
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        # A reasoning model spends its first few hundred tokens thinking, so a small cap returns an
        # EMPTY answer and the gate reports FAIL on its own truncation rather than on the stack.
        "max_tokens": 640, "temperature": 0.0, "top_p": 1.0, "seed": 42,
        "chat_template_kwargs": {"enable_thinking": False},
    }).encode()
    req = urllib.request.Request(BASE.rstrip("/") + "/chat/completions", data=body,
                                headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        d = json.loads(r.read())
    m = d["choices"][0]["message"]
    txt = (m.get("content") or "").strip()
    if not txt:
        # Thinking models put the answer in content and the chain in reasoning_content. If
        # content is empty the answer never arrived; fall back so the failure is visible.
        txt = (m.get("reasoning_content") or "").strip()
    return txt


out = {"probe": "accuracy", "tier": 0,
       "started_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
       "model": MODEL, "cases": [], "errors": [],
       "method": {
           "decode": "greedy (temperature 0, top_p 1, fixed seed)",
           "repeats": REPEATS,
           "determinism": "identical output across repeats of the same prompt",
           "exact_match": "regex over the answer, single verifiable fact per case",
           # The full case list travels with the result. A gate reported as "10 of 10, PASS" that
           # nobody can re-run is an assertion; published cases make it an artifact.
           "cases_published": [{"prompt": pr, "accept_pattern": pat} for pr, pat in CASES],
           "note": "This is a REGRESSION gate, not a capability benchmark. It detects a stack "
                   "that has broken or been over-quantised; it says nothing about model quality.",
       }}

for prompt, pattern in CASES:
    reps = []
    try:
        for _ in range(REPEATS):
            reps.append(ask(prompt))
    except Exception as exc:  # noqa: BLE001
        out["errors"].append("%s: %s" % (prompt[:32], str(exc)[:120]))
        continue
    out["cases"].append({
        "prompt": prompt,
        "answers": reps,
        "deterministic": len(set(reps)) == 1,
        "correct": bool(re.search(pattern, reps[0])),
    })

n = len(out["cases"])
if n:
    det = sum(1 for c in out["cases"] if c["deterministic"])
    cor = sum(1 for c in out["cases"] if c["correct"])
    out["summary"] = {
        "cases": n,
        "deterministic": det, "determinism_pct": det * 100.0 / n,
        "correct": cor, "exact_match_pct": cor * 100.0 / n,
        "verdict": ("PASS" if det == n and cor == n else
                    "DEGRADED" if cor >= n * 0.9 else "FAIL"),
    }
print(json.dumps(out, indent=2))
