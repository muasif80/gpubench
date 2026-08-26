#!/usr/bin/env python3
"""vLLM serving benchmark: concurrency sweep with TTFT / inter-token latency / throughput.

Standard library only, so it runs on the host python3 (3.12) with nothing installed.

    GPUBENCH_RUN_DIR=./gpubench-results python3 30_serve_bench.py \
        --concurrency 1,2,4,8 --requests 8 --input-tokens 512 --output-tokens 128

WARNING - this box serves on-prem prod, qa, dev and live from ONE vLLM instance. Concurrency
above ~8 competes with real traffic. Keep sweeps short outside a maintenance window.

Two things make the numbers trustworthy:
  * ignore_eos + fixed max_tokens, so every request decodes exactly the same length
  * vLLM's own /metrics counters are read before and after each level, giving a server-side
    token count to cross-check the client-side one against
"""
import argparse
import http.client
import json
import os
import statistics
import sys
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


def parse_url(url):
    u = urllib.parse.urlparse(url)
    return u.hostname, (u.port or (443 if u.scheme == "https" else 80)), u.scheme == "https", u.path.rstrip("/")


class Client(object):
    def __init__(self, base_url, timeout):
        self.host, self.port, self.tls, self.base_path = parse_url(base_url)
        self.timeout = timeout
        self.conn = None

    def _connect(self):
        if self.tls:
            import ssl
            self.conn = http.client.HTTPSConnection(self.host, self.port, timeout=self.timeout,
                                                    context=ssl.create_default_context())
        else:
            self.conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)

    def post_stream(self, path, payload, api_key=None):
        """POST and yield (timestamp, event_dict) for each SSE data line."""
        if self.conn is None:
            self._connect()
        body = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        if api_key:
            headers["Authorization"] = "Bearer " + api_key
        try:
            self.conn.request("POST", self.base_path + path, body=body, headers=headers)
            resp = self.conn.getresponse()
        except Exception:
            self.conn.close()
            self.conn = None
            raise
        if resp.status != 200:
            detail = resp.read()[:400]
            self.conn.close()
            self.conn = None
            raise RuntimeError("HTTP %d: %s" % (resp.status, detail.decode("utf-8", "replace")))
        try:
            while True:
                line = resp.readline()
                if not line:
                    break
                line = line.strip()
                if not line or not line.startswith(b"data:"):
                    continue
                data = line[5:].strip()
                if data == b"[DONE]":
                    break
                try:
                    yield time.perf_counter(), json.loads(data)
                except ValueError:
                    continue
        finally:
            # Breaking on [DONE] leaves the trailing chunk-terminator unread, which puts the
            # keep-alive connection in "Request-sent" state and makes the NEXT request on this
            # worker fail with ResponseNotReady. Drain it, and drop the socket if that fails.
            try:
                resp.read()
            except Exception:  # noqa: BLE001
                if self.conn is not None:
                    self.conn.close()
                self.conn = None

    def get(self, path, root=False):
        """GET on a fresh connection. root=True skips the /v1 prefix (metrics live at the root)."""
        conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        try:
            conn.request("GET", path if root else self.base_path + path)
            r = conn.getresponse()
            return r.status, r.read().decode("utf-8", "replace")
        finally:
            conn.close()


def scrape_metrics(client):
    """Pull the vLLM prometheus counters that matter, ignoring everything else."""
    wanted = ("vllm:prompt_tokens_total", "vllm:generation_tokens_total",
              "vllm:num_requests_running", "vllm:num_requests_waiting",
              "vllm:gpu_cache_usage_perc", "vllm:kv_cache_usage_perc",
              # Prefix-cache counters. Every prompt this probe sends carries a unique leading
              # salt precisely so the cache cannot serve it, but "cache defeated by construction"
              # is an argument, not a measurement. These two counters turn it into one: a hit rate
              # above zero means the prefill numbers are partly cache reads and are overstated.
              "vllm:prefix_cache_queries_total", "vllm:prefix_cache_hits_total",
              "vllm:gpu_prefix_cache_queries_total", "vllm:gpu_prefix_cache_hits_total")
    try:
        status, text = client.get("/metrics", root=True)
    except Exception:
        return None
    if status != 200:
        return None
    found = {}
    for line in text.splitlines():
        if line.startswith("#"):
            continue
        for w in wanted:
            if line.startswith(w):
                try:
                    found[w] = found.get(w, 0.0) + float(line.rsplit(" ", 1)[1])
                except (IndexError, ValueError):
                    pass
    return found



# A realistic prompt-length mix, replacing a single uniform length. Synthetic uniform prompts
# flatter a scheduler: real traffic is a long-tailed mixture, and a scheduler that copes with 512
# tokens everywhere may behave quite differently when a few 8k requests share the batch.
# Weights are a plausible interactive-assistant shape and are reported with the result so anyone
# can substitute their own measured distribution.
INPUT_MIX = [(128, 0.35), (512, 0.30), (1024, 0.20), (2048, 0.10), (8192, 0.05)]


def mixed_lengths(count):
    """Deterministic expansion of INPUT_MIX into `count` prompt lengths.

    Deterministic on purpose: a random draw would make two runs incomparable, which defeats the
    point of measuring variance.
    """
    out = []
    for length, share in INPUT_MIX:
        out.extend([length] * max(1, int(round(count * share))))
    while len(out) < count:
        out.append(INPUT_MIX[0][0])
    return sorted(out[:count])


def build_prompt(approx_tokens, salt):
    # " word" is one token for most BPE vocabularies; close enough for a load generator, and the
    # server-side prompt_tokens counter records the true value anyway.
    # The salt goes FIRST on purpose: vLLM's prefix cache keys on the leading tokens, and a shared
    # prefix would turn a prefill benchmark into a cache-hit benchmark.
    filler = " ".join(["lorem"] * max(1, approx_tokens - 8))
    return "Request %d. %s\nSummarize:" % (salt, filler)


def one_request(client, args, salt, in_tok=None, out_tok=None):
    in_tok = args.input_tokens if in_tok is None else in_tok
    out_tok = args.output_tokens if out_tok is None else out_tok
    if args.endpoint == "chat":
        path = "/chat/completions"
        payload = {
            "model": args.model,
            "messages": [{"role": "user", "content": build_prompt(in_tok, salt)}],
            "max_tokens": out_tok,
            "temperature": 0.0,
            "stream": True,
            "stream_options": {"include_usage": True},
            "ignore_eos": True,
        }
    else:
        path = "/completions"
        payload = {
            "model": args.model,
            "prompt": build_prompt(in_tok, salt),
            "max_tokens": out_tok,
            "temperature": 0.0,
            "stream": True,
            "stream_options": {"include_usage": True},
            "ignore_eos": True,
        }

    start = time.perf_counter()
    ttft = None
    stamps = []
    chunks = 0
    usage = None
    for ts, event in client.post_stream(path, payload, args.api_key):
        if event.get("usage"):
            usage = event["usage"]
        choices = event.get("choices") or []
        if not choices:
            continue
        piece = choices[0].get("text")
        if piece is None:
            delta = choices[0].get("delta") or {}
            piece = delta.get("content") or delta.get("reasoning_content") or ""
        if piece == "":
            continue
        chunks += 1
        if ttft is None:
            ttft = ts - start
        stamps.append(ts)
    end = time.perf_counter()

    itls = [stamps[i] - stamps[i - 1] for i in range(1, len(stamps))]
    completion_tokens = chunks
    prompt_tokens = None
    if usage:
        completion_tokens = usage.get("completion_tokens", chunks)
        prompt_tokens = usage.get("prompt_tokens")
    return {
        "ttft_s": ttft,
        "e2e_s": end - start,
        "itls": itls,
        "completion_tokens": completion_tokens,
        "prompt_tokens": prompt_tokens,
    }


def whole_waves(concurrency, requested):
    """Round a request count up to a whole multiple of the concurrency.

    A partial final wave runs at LOWER concurrency than the level claims to measure, so the level
    reports a throughput somewhere between its nominal concurrency and the size of its tail. The
    error is silent, it is largest at the middle concurrencies where the tail is a big fraction of
    the work, and it vanishes wherever the count happens to divide -- which makes it look like
    scatter at one level rather than a systematic fault.

    Returns (effective, waves).
    """
    c = max(1, int(concurrency))
    n = max(c, int(requested))
    waves = (n + c - 1) // c
    return waves * c, waves


def run_level(args, concurrency, total_requests, in_tok=None, out_tok=None):
    in_tok = args.input_tokens if in_tok is None else in_tok
    out_tok = args.output_tokens if out_tok is None else out_tok
    client_for_metrics = Client(args.base_url, args.timeout)
    before = scrape_metrics(client_for_metrics)

    results = []
    errors = []
    lock = threading.Lock()
    counter = {"n": 0}

    def worker(worker_id):
        client = Client(args.base_url, args.timeout)
        while True:
            with lock:
                if counter["n"] >= total_requests:
                    return
                salt = counter["n"]
                counter["n"] += 1
            try:
                r = one_request(client, args, salt, in_tok, out_tok)
                with lock:
                    results.append(r)
            except Exception as exc:  # noqa: BLE001
                with lock:
                    errors.append(repr(exc))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(concurrency)]
    wall_start = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.perf_counter() - wall_start

    after = scrape_metrics(client_for_metrics)

    ok = [r for r in results if r["ttft_s"] is not None]
    ttfts = [r["ttft_s"] for r in ok]
    e2es = [r["e2e_s"] for r in ok]
    all_itls = [x for r in ok for x in r["itls"]]
    out_tokens = sum(r["completion_tokens"] for r in ok)
    in_tokens = sum((r["prompt_tokens"] or 0) for r in ok)

    server = None
    if before and after:
        server = {
            "generation_tokens": after.get("vllm:generation_tokens_total", 0) - before.get("vllm:generation_tokens_total", 0),
            "prompt_tokens": after.get("vllm:prompt_tokens_total", 0) - before.get("vllm:prompt_tokens_total", 0),
            "kv_cache_usage_end": after.get("vllm:gpu_cache_usage_perc", after.get("vllm:kv_cache_usage_perc")),
            "requests_waiting_end": after.get("vllm:num_requests_waiting"),
        }
        # Prefix-cache hit rate over this level only, so it can be read as "was the prefill in
        # THIS measurement real work". Names differ across vLLM versions; try both.
        def _d(*names):
            for n in names:
                if n in after or n in before:
                    return after.get(n, 0.0) - before.get(n, 0.0)
            return None

        q = _d("vllm:prefix_cache_queries_total", "vllm:gpu_prefix_cache_queries_total")
        hits = _d("vllm:prefix_cache_hits_total", "vllm:gpu_prefix_cache_hits_total")
        if q is not None and hits is not None:
            server["prefix_cache_queries"] = q
            server["prefix_cache_hits"] = hits
            server["prefix_cache_hit_pct"] = (hits / q * 100.0) if q else 0.0
        else:
            server["prefix_cache_hit_pct"] = None
            server["prefix_cache_note"] = ("this vLLM build exposes no prefix-cache counters, so "
                                           "cache defeat rests on construction (unique leading "
                                           "salt per prompt) rather than on measurement")

    # TTFT on a max_tokens=1 request IS the prefill time, so prefill throughput is prompt
    # tokens divided by TTFT rather than by wall time.
    prefill_tok_s = None
    if out_tok == 1 and ttfts and in_tokens:
        per_req_prompt = in_tokens / float(len(ok))
        prefill_tok_s = per_req_prompt / statistics.fmean(ttfts) * concurrency

    return {
        "concurrency": concurrency,
        "input_tokens_requested": in_tok,
        "output_tokens_requested": out_tok,
        "prefill_tokens_per_s": prefill_tok_s,
        "requests_attempted": total_requests,
        "requests_ok": len(ok),
        # Sample size and duration travel WITH every percentile below, because a percentile
        # without its n is not interpretable: p95 of eight samples is the second-worst value, not
        # a tail estimate, and a reader cannot tell the difference from the number alone.
        "sample_count": len(ok),
        "duration_s": wall,
        "waves": (total_requests // concurrency) if concurrency else None,
        "whole_waves": bool(concurrency and total_requests % concurrency == 0),
        "errors": errors[:5],
        "error_count": len(errors),
        "wall_s": wall,
        "requests_per_s": len(ok) / wall if wall > 0 else None,
        "output_tokens": out_tokens,
        "input_tokens": in_tokens,
        "output_tokens_per_s": out_tokens / wall if wall > 0 else None,
        "total_tokens_per_s": (out_tokens + in_tokens) / wall if wall > 0 else None,
        "ttft_s": {"mean": statistics.fmean(ttfts) if ttfts else None,
                   "p50": pct(ttfts, 50), "p95": pct(ttfts, 95), "max": max(ttfts) if ttfts else None},
        "itl_ms": {"mean": statistics.fmean(all_itls) * 1000 if all_itls else None,
                   "p50": (pct(all_itls, 50) or 0) * 1000 if all_itls else None,
                   "p95": (pct(all_itls, 95) or 0) * 1000 if all_itls else None},
        "e2e_s": {"mean": statistics.fmean(e2es) if e2es else None,
                  "p50": pct(e2es, 50), "p95": pct(e2es, 95)},
        "per_request_output_tokens_per_s": (
            statistics.fmean([r["completion_tokens"] / r["e2e_s"] for r in ok if r["e2e_s"] > 0]) if ok else None),
        "server_metrics_delta": server,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default=os.environ.get("GPUBENCH_BASE_URL", "http://127.0.0.1:8000/v1"))
    ap.add_argument("--model", default=os.environ.get("GPUBENCH_MODEL", "Qwen/Qwen3.6-27B-FP8"))
    ap.add_argument("--api-key", default=os.environ.get("GPUBENCH_API_KEY"))
    ap.add_argument("--endpoint", choices=["completions", "chat"], default="completions")
    ap.add_argument("--concurrency", default="1,2,4,8")
    ap.add_argument("--requests", type=int, default=8, help="requests per concurrency level")
    ap.add_argument("--requests-multiplier", type=float, default=1.0,
                    help="scale requests with concurrency, e.g. 2 means 2x concurrency requests")
    ap.add_argument("--input-tokens", type=int, default=512)
    ap.add_argument("--output-tokens", type=int, default=128)
    ap.add_argument("--timeout", type=float, default=600.0)
    ap.add_argument("--warmup", type=int, default=1)
    ap.add_argument("--label", default="")
    ap.add_argument("--mode", choices=["concurrency", "prefill", "decode", "mixed"], default="concurrency",
                    help="concurrency: sweep --concurrency at fixed sizes. "
                         "prefill: sweep --input-lengths with max_tokens=1, so TTFT is the "
                         "prefill time. decode: sweep --output-lengths at a short fixed input, so "
                         "ITL is close to a pure decode step.")
    ap.add_argument("--input-lengths", default="128,512,2048,8192,32768",
                    help="prefill mode: prompt lengths to sweep")
    ap.add_argument("--output-lengths", default="128,512",
                    help="decode mode: generation lengths to sweep")
    args = ap.parse_args()

    levels = [int(x) for x in args.concurrency.split(",") if x.strip()]
    run_dir = os.environ.get("GPUBENCH_RUN_DIR", "./gpubench-results")
    os.makedirs(run_dir, exist_ok=True)

    doc = {
        "benchmark": "serve_bench",
        "label": args.label,
        "started_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "config": {k: v for k, v in vars(args).items() if k != "api_key"},
        "request_count_policy": (
            "The number of requests at each level is rounded UP to a whole multiple of that "
            "level's concurrency. A partial final wave runs at lower concurrency than the level "
            "claims to measure and depresses its throughput, by an amount that depends on how "
            "badly the count divides -- so the error appears at some levels and not others and "
            "reads as scatter. Each level records requests_attempted, sample_count and "
            "whole_waves so the adjustment is auditable."),
        # The corpus is part of the result, exactly as the engine configuration is. A reader who
        # cannot see what was sent cannot judge what came back, and cannot reproduce it.
        "workload": {
            "kind": "synthetic",
            "template": "Request <salt>. <filler> \\n Summarize:",
            "filler_token": "lorem",
            "salt": "a distinct integer per request, placed FIRST so it cannot share a cached "
                    "prefix with any other request",
            "length_control": "requested length is in words; one filler word is approximately one "
                              "BPE token. The engine's own prompt_tokens counter is what gets "
                              "reported, and both values are recorded per level so the "
                              "approximation is auditable rather than assumed.",
            "why_synthetic": "exact length control, no licensing entanglement, and prefix-cache "
                             "defeat by construction. The cost is that it says nothing about "
                             "content-sensitive behaviour, so no quality claim may rest on it.",
            "not_claimed": "This is not an MLPerf-style fixed dataset. MLPerf mandates specific "
                           "corpora (OpenORCA, CNN-DailyMail) because the input distribution is "
                           "part of the benchmark definition; results here are therefore "
                           "comparable across runs of this tool, but not with MLPerf submissions.",
        },
        "levels": [],
    }

    client = Client(args.base_url, args.timeout)
    try:
        status, text = client.get("/models")
        doc["models_endpoint"] = json.loads(text) if status == 200 else {"status": status}
    except Exception as exc:  # noqa: BLE001
        print("cannot reach %s: %r" % (args.base_url, exc), file=sys.stderr)
        raise SystemExit(2)

    for _ in range(args.warmup):
        try:
            one_request(client, args, 0)
        except Exception as exc:  # noqa: BLE001
            print("warmup failed: %r" % (exc,), file=sys.stderr)

    doc["mode"] = args.mode

    if args.mode == "mixed":
        # Replay a length mixture at one concurrency, so the scheduler is measured against
        # traffic shaped like real traffic rather than a single length.
        c = levels[0]
        lengths = mixed_lengths(max(args.requests, c))
        doc["input_mix"] = [{"tokens": t, "share": w} for t, w in INPUT_MIX]
        doc["input_mix_provenance"] = (
            "ASSUMED, not measured. These shares are a plausible interactive-assistant shape "
            "chosen to be long-tailed; they were not sampled from production traffic. They exist "
            "to show how much a single-length benchmark hides, and the spread between the rows is "
            "the finding, not the weighted average. Substitute a measured distribution before "
            "using any weighted figure for capacity planning.")
        header = "%-10s %-6s %-11s %-11s %-11s %-6s" % (
            "INPUTTOK", "CONC", "OUTtok/s", "TTFTp50", "TTFTp95", "ERR")
        print(header); print("-" * len(header))
        for n_in in sorted(set(lengths)):
            k = lengths.count(n_in)
            level = run_level(args, c, max(k, c), in_tok=n_in,
                              out_tok=args.output_tokens)
            level["mix_weight"] = k / float(len(lengths))
            doc["levels"].append(level)
            print("%-10d %-6d %-11.1f %-11.3f %-11.3f %-6d" % (
                n_in, c, level["output_tokens_per_s"] or 0,
                level["ttft_s"]["p50"] or 0, level["ttft_s"]["p95"] or 0,
                level["error_count"]))

    elif args.mode == "prefill":
        # One prompt length per level, max_tokens=1. TTFT is then the prefill time and nothing
        # else, which is the only honest way to put prefill on a roofline.
        header = ("%-9s %-6s %-11s %-11s %-11s %-11s %-6s" %
                  ("INPUTTOK", "CONC", "PROMPTTOK", "PREFILtok/s", "TTFTp50", "TTFTp95", "ERR"))
        print(header)
        print("-" * len(header))
        for n_in in [int(x) for x in args.input_lengths.split(",") if x.strip()]:
            c = levels[0]
            level = run_level(args, c, whole_waves(c, args.requests)[0],
                              in_tok=n_in, out_tok=1)
            doc["levels"].append(level)
            per_req_prompt = (level["input_tokens"] / level["requests_ok"]) if level["requests_ok"] else 0
            print("%-9d %-6d %-11.0f %-11.1f %-11.4f %-11.4f %-6d" % (
                n_in, c, per_req_prompt,
                level["prefill_tokens_per_s"] or 0,
                level["ttft_s"]["p50"] or 0, level["ttft_s"]["p95"] or 0,
                level["error_count"]))
            for e in level["errors"]:
                print("   ! " + e)

    elif args.mode == "decode":
        # Short prompt, long generation, so ITL is dominated by the decode step.
        header = ("%-9s %-6s %-11s %-11s %-11s %-11s %-6s" %
                  ("OUTTOK", "CONC", "OUTtok/s", "ITLp50ms", "ITLp95ms", "TTFTp50", "ERR"))
        print(header)
        print("-" * len(header))
        for n_out in [int(x) for x in args.output_lengths.split(",") if x.strip()]:
            c = levels[0]
            level = run_level(args, c, whole_waves(c, args.requests)[0],
                              in_tok=args.input_tokens, out_tok=n_out)
            doc["levels"].append(level)
            print("%-9d %-6d %-11.1f %-11.2f %-11.2f %-11.4f %-6d" % (
                n_out, c,
                level["output_tokens_per_s"] or 0,
                level["itl_ms"]["p50"] or 0, level["itl_ms"]["p95"] or 0,
                level["ttft_s"]["p50"] or 0, level["error_count"]))
            for e in level["errors"]:
                print("   ! " + e)

    else:
        header = ("%-6s %-9s %-9s %-9s %-9s %-9s %-10s %-9s" %
                  ("CONC", "REQ/S", "OUTTOK/S", "TTFTp50", "TTFTp95", "ITLp50ms", "E2Ep95", "ERR"))
        print(header)
        print("-" * len(header))
        for c in levels:
            if args.requests_multiplier != 1.0:
                # scale the batch with concurrency so every level runs the same number of waves
                n = max(c, int(round(c * args.requests_multiplier)))
            else:
                n = max(args.requests, c)
            n, _waves = whole_waves(c, n)
            level = run_level(args, c, n)
            doc["levels"].append(level)
            print("%-6d %-9.2f %-9.1f %-9.3f %-9.3f %-9.1f %-10.3f %-9d" % (
                c,
                level["requests_per_s"] or 0,
                level["output_tokens_per_s"] or 0,
                level["ttft_s"]["p50"] or 0,
                level["ttft_s"]["p95"] or 0,
                level["itl_ms"]["p50"] or 0,
                level["e2e_s"]["p95"] or 0,
                level["error_count"],
            ))
            for e in level["errors"]:
                print("   ! " + e)

    doc["finished_at_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    # concurrency mode with no label keeps the canonical name the report looks for first
    if args.label:
        name = "serve_bench_%s.json" % args.label
    elif args.mode != "concurrency":
        name = "serve_bench_%s.json" % args.mode
    else:
        name = "serve_bench.json"
    path = os.path.join(run_dir, name)
    with open(path, "w") as f:
        json.dump(doc, f, indent=2)
    print("\nwrote " + path)
    # Emit the document too: the orchestrator captures stdout JSON.
    print(json.dumps(doc))


if __name__ == "__main__":
    main()
