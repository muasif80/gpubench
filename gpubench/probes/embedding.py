#!/usr/bin/env python3
"""Embedding throughput benchmark for the bge-m3 vLLM service on :8001.

Standard library only. Sweeps batch size and client concurrency and reports embeddings/s,
tokens/s and latency percentiles. Indexing throughput on this platform is gated by this
service, so it belongs in any GPU capacity story alongside the LLM numbers.

    GPUBENCH_RUN_DIR=... python3 40_embed_bench.py --batch 1,8,32 --concurrency 1,4 --requests 8
"""
import argparse
import http.client
import json
import os
import statistics
import threading
import time
import urllib.parse


def pct(values, p):
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def post_json(base_url, path, payload, timeout):
    u = urllib.parse.urlparse(base_url)
    conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout)
    try:
        body = json.dumps(payload).encode("utf-8")
        conn.request("POST", u.path.rstrip("/") + path, body=body,
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        raw = resp.read()
        if resp.status != 200:
            raise RuntimeError("HTTP %d: %s" % (resp.status, raw[:300].decode("utf-8", "replace")))
        return json.loads(raw)
    finally:
        conn.close()


def make_text(approx_tokens, salt):
    return "doc %d " % salt + " ".join(["clause"] * max(1, approx_tokens - 4))


def run_level(args, batch, concurrency, total_requests):
    lat = []
    errors = []
    tokens = [0]
    lock = threading.Lock()
    counter = {"n": 0}

    def worker():
        while True:
            with lock:
                if counter["n"] >= total_requests:
                    return
                salt = counter["n"]
                counter["n"] += 1
            payload = {"model": args.model,
                       "input": [make_text(args.input_tokens, salt * batch + i) for i in range(batch)]}
            t0 = time.perf_counter()
            try:
                r = post_json(args.base_url, "/embeddings", payload, args.timeout)
                dt = time.perf_counter() - t0
                used = (r.get("usage") or {}).get("prompt_tokens", 0)
                with lock:
                    lat.append(dt)
                    tokens[0] += used
            except Exception as exc:  # noqa: BLE001
                with lock:
                    errors.append(repr(exc))

    threads = [threading.Thread(target=worker) for _ in range(concurrency)]
    t0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.perf_counter() - t0

    done = len(lat)
    return {
        "batch": batch,
        "concurrency": concurrency,
        "requests_ok": done,
        "error_count": len(errors),
        "errors": errors[:3],
        "wall_s": wall,
        "embeddings_per_s": (done * batch) / wall if wall > 0 else None,
        "tokens_per_s": tokens[0] / wall if wall > 0 else None,
        "latency_s": {"mean": statistics.fmean(lat) if lat else None,
                      "p50": pct(lat, 50), "p95": pct(lat, 95)},
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default=os.environ.get("EMBED_BASE_URL", "http://127.0.0.1:8001/v1"))
    ap.add_argument("--model", default=os.environ.get("EMBED_MODEL", "BAAI/bge-m3"))
    ap.add_argument("--batch", default="1,8,32")
    ap.add_argument("--concurrency", default="1,4")
    ap.add_argument("--requests", type=int, default=8, help="requests per (batch, concurrency) cell")
    ap.add_argument("--input-tokens", type=int, default=256)
    ap.add_argument("--timeout", type=float, default=300.0)
    args = ap.parse_args()

    run_dir = os.environ.get("GPUBENCH_RUN_DIR", "./gpubench-results")
    os.makedirs(run_dir, exist_ok=True)

    doc = {"benchmark": "embed_bench",
           "started_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "config": vars(args), "cells": [],
           "workload": {
               "kind": "synthetic",
               "requested_length_words": args.input_tokens,
               "template": "doc <salt> <filler repeated>",
               "filler_token": "clause",
               "salt": "distinct per document, so no two inputs are identical",
               "length_note": "Requested length is in WORDS. An embedding model's tokenizer may "
                              "split the filler word into more than one token, so the true token "
                              "count per document is typically higher than the requested number; "
                              "tokens_per_s divided by embeddings_per_s gives the actual figure "
                              "and is the one to quote.",
               "why_synthetic": "exact length control and no licensing entanglement. Retrieval "
                                "quality is not measured and no claim about it is supportable "
                                "from this.",
           }, "cells_note": "throughput only; recall and ranking quality are out of scope"}

    header = "%-8s %-6s %-12s %-12s %-10s %-10s %-6s" % (
        "BATCH", "CONC", "EMB/S", "TOK/S", "LATp50", "LATp95", "ERR")
    print(header)
    print("-" * len(header))
    for b in [int(x) for x in args.batch.split(",") if x.strip()]:
        for c in [int(x) for x in args.concurrency.split(",") if x.strip()]:
            cell = run_level(args, b, c, max(args.requests, c))
            doc["cells"].append(cell)
            print("%-8d %-6d %-12.1f %-12.1f %-10.3f %-10.3f %-6d" % (
                b, c, cell["embeddings_per_s"] or 0, cell["tokens_per_s"] or 0,
                cell["latency_s"]["p50"] or 0, cell["latency_s"]["p95"] or 0, cell["error_count"]))
            for e in cell["errors"]:
                print("   ! " + e)

    doc["finished_at_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    path = os.path.join(run_dir, "embed_bench.json")
    with open(path, "w") as f:
        json.dump(doc, f, indent=2)
    print("\nwrote " + path)
    # Emit the document too: the orchestrator captures stdout JSON.
    print(json.dumps(doc))


if __name__ == "__main__":
    main()
