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

ARRIVAL PROCESS. Two generators, selected by --arrival:

  closed  (default, unchanged) A fixed pool of `concurrency` workers, each issuing its next
          request only when its previous one completes. In-flight count is pinned at the
          concurrency by construction, so the server is never offered more work than it is
          currently finishing. That makes a closed-loop harness structurally incapable of
          building a queue, and its latency percentiles are therefore optimistic: the load
          generator throttles itself in exactly the moments a real arrival stream would not.
          Worse, when the request count equals the concurrency the level is ONE simultaneous
          burst, and its "p95" is dispersion within that burst rather than a queueing tail.

  poisson (open loop) Requests are issued on an exponential inter-arrival schedule at --rate,
          on time, whether or not the server is keeping up. Nothing about the dispatch depends
          on completions, and that independence is the entire point, and it is what MLPerf's
          Server scenario buys with its Poisson arrivals. Falling behind then shows up as a
          measurement instead of being absorbed: achieved rate below target, and in-flight
          depth climbing over the life of the level.
          The cost of that independence is that nothing bounds the backlog: one thread and one
          socket per arrival, so a stalled engine at a high rate can leave every request of the
          level in flight at once. peak_inflight records how far it went, peak_threads_alive
          records how close the harness came to its own thread ceiling, and a ceiling that IS hit
          truncates the level and says so instead of raising. Size --requests with that in mind
          rather than reintroducing a cap, which would make it closed-loop again.

HOW AN OPEN-LOOP LEVEL DECIDES THE ENGINE DID NOT KEEP UP. Not by comparing completions over the
wall clock against the offered rate. That comparison is biased against the engine by construction:
the wall clock runs to the last COMPLETION and the offered span runs to the last ARRIVAL, so the
ratio carries a deficit of roughly service/(span+service) even on an engine with unlimited
headroom and provably zero queueing. At this box's measured service times (2-21 s per request) and
its ~3 req/s ceiling that false deficit is 12-51%, which is far above any sane threshold. The
verdict therefore rests on LATENCY GROWTH: a Mann-Kendall rank trend of per-request end-to-end
latency against arrival time, over the arrival window, sized by the Theil-Sen slope. A queue that
is not growing leaves latency flat however deep the in-flight count is, and a queue that is growing
raises every later request's latency. The in-flight count itself is NOT the test: rate x service
requests in flight is Little's law, not a backlog, and a level shorter than one service time is all
ramp-up, so the in-flight slope is recorded as a diagnostic and labelled as one.

Three things bound what that verdict may be read to mean, and each is a key in the document:

  * it is called latency_grew_over_the_level, not fell_behind, because a latency trend does not
    name whose load caused it. This box serves prod, qa, dev and live from ONE vLLM, so a co-tenant
    slowdown raises these latencies with nothing of ours queued anywhere. engine_did_not_keep_up is
    the capacity claim and is gated on the ENGINE's own running/waiting split.
  * requests the engine never finished are booked into the fit as CENSORED observations at their
    harness-timed duration, which is a lower bound on the wait. Dropping them made a tighter client
    timeout into a cleaner bill of health.
  * a level that the generator or the harness voided, or that too few requests completed in, gets
    latency_grew_over_the_level = None with a basis that says which. None is printed as loudly as a
    verdict, never as silence.

The arrival model is declared at the top of the result document (doc["arrival_model"], and
doc["report"]["arrival_model"] where gpubench/verify.py's check_load_shape looks for it) and again
in every level, next to the target rate, the achieved rate and the in-flight trace.
"""
import argparse
import errno
import hashlib
import http.client
import io
import json
import math
import os
import random
import socket
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
        # Open-loop mode builds one client per arrival and never reuses a socket, so the client's
        # own connection count is part of the result: it is the number that says whether a
        # shortfall could have been the harness running out of ephemeral ports.
        self.connects = 0

    def _connect(self):
        if self.tls:
            import ssl
            self.conn = http.client.HTTPSConnection(self.host, self.port, timeout=self.timeout,
                                                    context=ssl.create_default_context())
        else:
            self.conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        self.connects += 1

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

    def close(self):
        """Drop the keep-alive socket. Open-loop mode builds one client per arrival, so the
        sockets have to be released as requests finish rather than at interpreter exit."""
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:  # noqa: BLE001
                pass
            self.conn = None

    def get(self, path, root=False):
        """GET on a fresh connection. root=True skips the /v1 prefix (metrics live at the root).

        The scheme has to be honoured here as well as in post_stream. It was not, and the
        consequence was silent: against an https base_url every /metrics scrape failed, so
        server_metrics_delta came back None and the server-side cross-check simply vanished from
        the document with no error anywhere.
        """
        if self.tls:
            import ssl
            conn = http.client.HTTPSConnection(self.host, self.port, timeout=self.timeout,
                                               context=ssl.create_default_context())
        else:
            conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        self.connects += 1
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


# --------------------------------------------------------------------------------------
# arrival process

# The vocabulary is not free: check_load_shape() in gpubench/verify.py accepts exactly
# closed_loop / open_loop_constant / open_loop_poisson and errors on anything else, so the
# result document has to speak those words. open_loop_constant is listed here for the reader's
# benefit; this probe does not implement a fixed-interval generator, and a mode that is not
# implemented must not be selectable.
ARRIVAL_MODELS = {"closed": "closed_loop", "poisson": "open_loop_poisson"}

# A run with no --arrival-seed still has to be reproducible, so the default is a constant rather
# than the clock. Two runs at the same rate and count then replay the SAME schedule, which is the
# only way a rate sweep's levels are comparable with each other or with yesterday's.
DEFAULT_ARRIVAL_SEED = 20260825

# In-flight depth is sampled on a timer, so a long level would otherwise grow an unbounded array
# inside the result document. Past the cap, sampling continues (the summary stays correct) and only
# the stored trace stops growing.
MAX_QUEUE_SAMPLES = 4000

# The depth sampler can be asked for a 5 ms tick on a short level, which is fine for a counter held
# in memory and not fine for an HTTP scrape of a production engine. The server-side queue poller
# shares the sampler's interval but never goes below this.
MIN_METRICS_POLL_S = 0.05

# Latency-growth verdict. The trend has to clear BOTH gates:
#   MK_Z    the Mann-Kendall rank trend statistic, in standard normal units. Rank-based on purpose:
#           it asks only whether later requests waited longer than earlier ones, so it survives the
#           heavy tail that queueing produces.
#   EFFECT  and the growth Theil-Sen predicts across the arrival window is at least this multiple
#           of the level's median latency, i.e. the last request waited about one whole service
#           time longer than the typical one. Trend detection alone is not enough: with a fake
#           engine of constant service time a physically meaningless trend can be highly
#           significant.
QUEUE_GROWTH_MK_Z = 1.96
QUEUE_GROWTH_EFFECT = 1.0
# The OLS t statistic is still COMPUTED AND REPORTED, and is no longer a gate. See ols_slope's
# docstring for the two measurements that took it out of the verdict.
QUEUE_GROWTH_T = 3.0

# Above this many fitted points Theil-Sen thins the series; O(n^2) pairwise slopes is the cost.
THEIL_SEN_MAX_POINTS = 400

# The fraction of dispatched requests that has to have COMPLETED before a level is allowed to
# report "latency did not grow". Below it the verdict is None and says so.
#
# This is the floor for the defect that made it necessary. The growth fit used to run on completed
# requests only, so a client timeout removed exactly the requests that prove a queue. Against a
# 2 req/s engine offered 6 req/s (90 arrivals, true median wait 16.35 s, true max 32.2 s):
#     timeout 10 s -> ok 28/90, TRUE      timeout 6 s -> ok 16, TRUE     timeout 4 s -> ok 10, TRUE
#     timeout  3 s -> ok  6/90, FALSE     timeout 2 s -> ok  4, None, and main() printed NOTHING
# The tighter the timeout, the more certainly an overloaded engine was reported as fine. Errored
# requests are now booked into the fit as censored observations, and this floor covers the residue:
# a censored duration is a LOWER bound on the wait, so a level made mostly of them cannot support
# the negative verdict however flat the fit looks.
QUEUE_GROWTH_MIN_COMPLETION_FRACTION = 0.70

# Generator fidelity. p95 of |actual dispatch - intended offset| against the mean inter-arrival.
# Judged per arrival rather than on the endpoints because deadlines here are absolute: a stall is
# followed by a catch-up burst that lands the last dispatch back on its scheduled offset, so a
# span-against-span check passes on a level whose middle was a burst and not a Poisson stream.
GENERATOR_FIDELITY_FRACTION = 0.10


def level_seed(base_seed, level_index):
    """The seed one level of a sweep actually draws from.

    Every level sharing one seed is not a saving, it is a correlated error. gap = -ln(1-U)/rate is
    scale-invariant, so the NORMALISED draw error of a shared seed is identical at every rate: a
    sweep at 2, 4, 8, 20, 50 and 200 req/s came out -5.107658% off nominal at all six levels, to
    twelve decimals. One unlucky sample then biases the whole sweep coherently in one direction and
    looks like a systematic engine effect. Deriving a per-level seed from (base, index) makes the
    levels independent draws while keeping the run reproducible from the one number the user typed.

    sha256 rather than hash(): hash() of a str is salted per process, which would make the derived
    seed differ between two runs of the same command.
    """
    digest = hashlib.sha256(("%d:%d" % (int(base_seed), int(level_index))).encode("ascii")).digest()
    return int.from_bytes(digest[:8], "big")


def seed_or_default(value):
    """int(x or DEFAULT) swallows a legitimate zero: --arrival-seed 0 recorded config.arrival_seed=0
    beside arrival.seed=20260825, so the document carried two different seeds and neither was
    wrong on its face. Only None means 'not given'."""
    return DEFAULT_ARRIVAL_SEED if value is None else int(value)


def ols_slope(xs, ys):
    """Least-squares slope of ys against xs, with its standard error and t statistic.

    REPORTED, NOT A GATE. This triple used to decide the open-loop verdict and it is no longer
    allowed to. Two reasons, both measured:

      * The t statistic is not a noise floor. Against a deterministic fake engine it never binds
        (t came out between 12,998 and 145,159 while the effect gate decided every case), and under
        realistic dispersion it becomes the whole verdict: a REAL trend of 0.15 s per second, with
        growth ratios of 1.31 / 1.72 / 1.91 / 2.34 all clearing the effect gate, flipped
        True / True / FALSE / FALSE as multiplicative noise rose to cv 0.8 and 1.1, with t falling
        11.51 -> 4.44 -> 2.64 -> 1.79. The trend was real in all four.
      * An OLS standard error assumes independent residuals. Queueing latencies violate that by
        construction: each request's wait is the previous one's wait plus its own service minus the
        gap, so the residuals are serially correlated and se is understated.

    se is None when the fit leaves no residual to estimate from. Callers must FAIL CLOSED on that
    (no statistic computed is not evidence of a trend, and it is not evidence against one either).
    The caller here used to do the opposite: `significant = (lat_t is None) or (lat_t >= T)` read an
    exact fit as PASSING the significance gate, which is the inverse of what this docstring says.
    The verdict now rests on mann_kendall() and theil_sen() below, which are rank-based and survive
    heavy tails.
    """
    n = len(xs)
    if n < 3:
        return None, None, None
    mx = statistics.fmean(xs)
    my = statistics.fmean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return None, None, None
    beta = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    alpha = my - beta * mx
    sse = sum((y - (alpha + beta * x)) ** 2 for x, y in zip(xs, ys))
    var = sse / (n - 2)
    if var <= 0:
        return beta, None, None
    se = math.sqrt(var / sxx)
    if se <= 0:
        return beta, None, None
    return beta, se, beta / se


def mann_kendall(xs, ys):
    """Rank-based monotone-trend test on ys ordered by xs. Returns (S, z, tau).

    Why a rank test and not the t statistic on an OLS slope: this asks only whether later values
    tend to exceed earlier ones. It cares nothing for the SIZE of any one value, so a single 30 s
    request among 5 s ones moves it by one concordant pair rather than by dragging a least-squares
    line. Queueing latencies are heavy-tailed by nature and that is exactly the property that broke
    the t gate.

    S = sum over i<j of sign(y_j - y_i). Under no trend S has mean zero and variance
    n(n-1)(2n+5)/18, corrected for ties, and (S - sign(S))/sd is approximately standard normal for
    n above about 10. tau is S normalised by the number of pairs, so it is comparable across levels
    of different length.

    Returns (None, None, None) when the statistic cannot be computed at all: fewer than three
    points, or every value tied. Callers must treat that as "not judged", never as a pass.
    """
    pairs = sorted(zip(xs, ys), key=lambda p: p[0])
    vals = [y for _x, y in pairs]
    n = len(vals)
    if n < 3:
        return None, None, None
    s = 0
    for i in range(n - 1):
        yi = vals[i]
        for j in range(i + 1, n):
            d = vals[j] - yi
            if d > 0:
                s += 1
            elif d < 0:
                s -= 1
    counts = {}
    for v in vals:
        counts[v] = counts.get(v, 0) + 1
    tie_term = sum(t * (t - 1) * (2 * t + 5) for t in counts.values() if t > 1)
    var = (n * (n - 1) * (2 * n + 5) - tie_term) / 18.0
    if var <= 0:
        return s, None, None
    if s > 0:
        z = (s - 1) / math.sqrt(var)
    elif s < 0:
        z = (s + 1) / math.sqrt(var)
    else:
        z = 0.0
    tau = s / (0.5 * n * (n - 1))
    return s, z, tau


def theil_sen(xs, ys):
    """Median of the pairwise slopes: the rank-based counterpart of the OLS slope.

    Used for the SIZE of the trend, for the same reason mann_kendall is used for its existence. The
    median pairwise slope has a breakdown point near 29%, so a handful of very slow requests move
    it a little where they move a least-squares line a lot.

    Cost is O(n^2), so above THEIL_SEN_MAX_POINTS the series is thinned to an evenly spaced
    subsample. Evenly spaced and not random: the result has to reproduce.
    """
    pairs = sorted(zip(xs, ys), key=lambda p: p[0])
    n = len(pairs)
    if n < 3:
        return None
    if n > THEIL_SEN_MAX_POINTS:
        step = n / float(THEIL_SEN_MAX_POINTS)
        pairs = [pairs[min(n - 1, int(i * step))] for i in range(THEIL_SEN_MAX_POINTS)]
        n = len(pairs)
    slopes = []
    for i in range(n - 1):
        xi, yi = pairs[i]
        for j in range(i + 1, n):
            dx = pairs[j][0] - xi
            if dx > 0:
                slopes.append((pairs[j][1] - yi) / dx)
    if not slopes:
        return None
    return statistics.median(slopes)


def judge_latency_growth(lat_xs, lat_ys, dispatch_span, dispatched, completed,
                         generator_kept_up=True, truncated=False, censored_n=0,
                         total_requests=None):
    """Did per-request latency grow across this level? Returns (verdict, basis, stats).

    PURE, and module level, so the verdict can be exercised without a device: the whole stated grid
    of healthy service times and offered rates runs through THIS function in a test, rather than
    through a copy of the gate arithmetic written into the test.

    The verdict is True, False, or None, and None is a real answer rather than a failure to
    produce one. It is returned in four cases, each with a basis that names itself:
      * the generator missed its own schedule, so these arrivals are not the process they name
      * the harness truncated the level, so the trend covers only the part that ran
      * too few points to test, or every latency tied
      * too few of the dispatched requests came back to support the NEGATIVE verdict

    lat_ys may contain censored durations (requests the engine never finished). Those are lower
    bounds on the wait, so their presence biases the trend DOWNWARD; the completion floor is what
    stops a level made mostly of them from reading as healthy.
    """
    lat_slope, lat_se, lat_t = ols_slope(lat_xs, lat_ys)
    mk_s, mk_z, mk_tau = mann_kendall(lat_xs, lat_ys)
    ts_slope = theil_sen(lat_xs, lat_ys)
    # Theil-Sen is the trend size the verdict uses; OLS is kept beside it so the two can be
    # compared and so a reader who wants the old number still has it.
    trend_slope = ts_slope if ts_slope is not None else lat_slope
    median_e2e = statistics.median(lat_ys) if lat_ys else None
    growth = (trend_slope * dispatch_span
              if trend_slope is not None and dispatch_span else None)
    growth_ratio = (growth / median_e2e if growth is not None and median_e2e else None)
    trend_detected = (None if mk_z is None
                      else bool(mk_z >= QUEUE_GROWTH_MK_Z and (trend_slope or 0.0) > 0))
    completion_fraction = (completed / float(dispatched)) if dispatched else None
    stats = {"mann_kendall_s": mk_s, "mann_kendall_z": mk_z, "mann_kendall_tau": mk_tau,
             "theil_sen_slope_s_per_s": ts_slope, "trend_detected": trend_detected,
             "e2e_slope_s_per_s": lat_slope, "slope_std_error": lat_se, "slope_t_stat": lat_t,
             "median_e2e_s": median_e2e, "growth_over_arrival_window_s": growth,
             "growth_as_multiple_of_median_e2e": growth_ratio,
             "completion_fraction": completion_fraction}

    # A level whose arrivals were not the schedule it names, or that stopped early because the
    # HARNESS ran out of threads, cannot be read for an engine property at all. Both used to carry
    # a verdict in the JSON while only the console suppressed one of them: a 500 ms dispatcher
    # stall at rate 30 wrote generator_kept_up false, p95 deviation 361.05 ms against a 3.33 ms
    # budget, and fell_behind true, in the same block, with no key marking it void.
    if generator_kept_up is False:
        return None, "not judged: the generator missed its own schedule", stats
    if truncated:
        return None, ("not judged: the harness hit its own ceiling after %d of %s arrivals, so "
                      "this level is truncated and the trend covers only the part that ran"
                      % (dispatched, total_requests if total_requests is not None else "?")), stats
    if mk_z is None or trend_slope is None or growth_ratio is None or len(lat_ys) < 5:
        return None, ("not judged: %d requests with a latency and an arrival time is too few to "
                      "test for a trend, or every latency tied" % len(lat_ys)), stats

    verdict = bool(trend_detected and growth_ratio >= QUEUE_GROWTH_EFFECT)
    basis = ("latency grew %.3f s over the %.3f s arrival window, %.2fx the median latency of "
             "%.3f s (Theil-Sen slope), Mann-Kendall z %.2f, tau %.2f, over %d requests of which "
             "%d censored (gates: %.1fx and z %.2f). The OLS t statistic was %s and is reported "
             "only."
             % (growth, dispatch_span, growth_ratio, median_e2e, mk_z, mk_tau, len(lat_ys),
                censored_n, QUEUE_GROWTH_EFFECT, QUEUE_GROWTH_MK_Z,
                ("%.1f" % lat_t) if lat_t is not None else "not computable (exact fit)"))
    if (verdict is False and completion_fraction is not None
            and completion_fraction < QUEUE_GROWTH_MIN_COMPLETION_FRACTION):
        # The requests that prove a queue are the ones that did not come back, and a censored
        # duration is only a lower bound on what they waited. A flat fit over what is left is not
        # evidence that nothing queued. A True verdict is left alone: the failure mode this floor
        # exists for is the quiet all-clear, and evidence of a queue is not made weaker by there
        # being more of it that never returned.
        return None, ("not judged: only %d of %d dispatched requests completed (%.0f%%, below the "
                      "stated floor of %.0f%%), so the requests that would show a queue are the "
                      "ones missing from the fit. Censored requests are booked at their "
                      "harness-timed duration, which is a LOWER bound on the wait, so a flat fit "
                      "over this level cannot support 'latency did not grow'."
                      % (completed, dispatched, completion_fraction * 100.0,
                         QUEUE_GROWTH_MIN_COMPLETION_FRACTION * 100.0)), stats
    return verdict, basis, stats


def error_stage(exc):
    """Which side of the wire failed.

    A client-side ceiling must never read as the engine refusing work. This probe opens one
    connection per arrival and never reuses one, so at roughly 28k ephemeral ports and a 60 s
    TIME_WAIT the CLIENT tops out near 470 connections per second; a socket that could not be
    created, or a name that would not resolve, says nothing whatever about the engine. Everything
    the engine actually answered (a status, a broken stream, a timeout waiting for tokens) stays in
    the engine bucket, which is the one error_count reports.
    """
    if isinstance(exc, RuntimeError) and str(exc).startswith("HTTP "):
        return "engine_http_status"
    if isinstance(exc, socket.gaierror):
        return "harness_resolve"
    if isinstance(exc, ConnectionRefusedError):
        return "harness_connect_refused"
    if isinstance(exc, TimeoutError):
        # A socket timeout on a streaming request is the engine not producing tokens in time.
        return "engine_timeout"
    if isinstance(exc, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)):
        return "engine_stream"
    if isinstance(exc, http.client.HTTPException):
        return "engine_stream"
    if isinstance(exc, OSError):
        exhaustion = {getattr(errno, n) for n in ("EMFILE", "ENFILE", "ENOBUFS", "EADDRINUSE",
                                                 "EADDRNOTAVAIL", "ENOMEM")
                      if hasattr(errno, n)}
        if exc.errno in exhaustion:
            return "harness_socket_exhausted"
        return "harness_transport"
    return "engine_other"


def sample_interval_for(args, total_requests, rate):
    """The depth-sample interval, defaulted FROM THE LEVEL rather than from a constant.

    An open-loop level lasts about requests/rate seconds and the default --requests is 8, so the
    old fixed 0.25 s default gave a 0.512 s level exactly two samples: the trace that is the
    evidence of a backlog was narrower than the backlog. Aim at about 100 points per level and let
    the flag override. Returns (interval, how it was chosen), and the second half is reported.
    """
    given = getattr(args, "queue_sample_interval", None)
    if given is not None:
        iv = float(given)
        if not iv > 0:
            # A zero or negative interval makes Event.wait return immediately, which turns the
            # sampler into a hot spin on the dispatcher's own lock and slows the arrivals it is
            # supposed to be observing.
            raise ValueError("queue sample interval must be above zero, got %r" % (given,))
        return iv, "explicit: --queue-sample-interval"
    if rate and float(rate) > 0 and total_requests:
        span = int(total_requests) / float(rate)
        return (min(0.25, max(0.005, span / 100.0)),
                "auto: (requests/rate)/100, clamped to [0.005, 0.25], aiming at about 100 points. "
                "It is a floor, not a promise: see effective_interval_s for what the OS timer "
                "actually delivered")
    return 0.25, "auto: no rate to size the level from"


def poisson_interarrivals(rate, count, seed):
    """`count` exponential inter-arrival gaps for a Poisson process of `rate` per second.

    t = -ln(1-U)/rate is the inverse CDF of the exponential distribution. U comes from
    random.random(), which is [0.0, 1.0), so 1-U is (0.0, 1.0]: the log is never taken of zero and
    the gap is never negative. Standard library only, deliberately: this has to run on a bare
    host python3 with nothing installed.

    Seeded from a private random.Random, never the global one: seeding the module singleton would
    make the schedule depend on whatever else in the process had drawn from it.
    """
    r = float(rate)
    if not r > 0:
        raise ValueError("poisson arrivals need a rate above zero, got %r" % (rate,))
    n = int(count)
    if n < 0:
        raise ValueError("negative request count %r" % (count,))
    rng = random.Random(seed)
    return [-math.log(1.0 - rng.random()) / r for _ in range(n)]


def poisson_offsets(rate, count, seed):
    """Cumulative arrival times in seconds from the start of the level.

    The first arrival lands one full inter-arrival gap after t=0, not at t=0: starting with a
    simultaneous arrival is exactly the burst artefact the open-loop mode exists to avoid.
    """
    out = []
    t = 0.0
    for gap in poisson_interarrivals(rate, count, seed):
        t += gap
        out.append(t)
    return out


def parse_rates(spec):
    """--rate as a comma-separated sweep. One value is a sweep of length one."""
    if spec is None or spec == "":
        return []
    if isinstance(spec, (int, float)):
        return [float(spec)]
    return [float(x) for x in str(spec).split(",") if x.strip()]


def arrival_declaration(args):
    """The top-level arrival statement for the result document.

    Emitted in two places from ONE source so they cannot drift apart: doc["arrival_model"], where
    a human reading the JSON looks, and doc["report"]["arrival_model"], which is the key
    check_load_shape() in gpubench/verify.py actually reads, and it raises D4 ("no arrival model
    declared") on any document whose report block does not name one of its three accepted models.
    """
    mode = getattr(args, "arrival", "closed") or "closed"
    model = ARRIVAL_MODELS[mode]
    rates = parse_rates(getattr(args, "rate", None))
    seed = seed_or_default(getattr(args, "arrival_seed", None))
    block = {
        "model": model,
        "mode": mode,
        # Gated on the mode exactly as the seed is. A closed-loop run given --rate 37 used to
        # declare a 37 req/s target beside an inter_arrival of "the next request is issued when a
        # previous one completes": a claim nothing in the run honoured, contradicted three lines
        # below it. The warning went to stderr, which no result file preserves, so the file said
        # one thing and the console said another.
        "target_rate_req_s": (rates or None) if mode == "poisson" else None,
        "seed": seed if mode == "poisson" else None,
        "seed_note": ("this is the BASE seed; each level draws from level_seed(base, level index) "
                      "so the levels of a sweep are independent draws, and every level records the "
                      "derived seed it used" if mode == "poisson" else None),
        "inter_arrival": ("exponential, gap = -ln(1-U)/rate, U from a seeded random.Random"
                          if mode == "poisson" else
                          "none: the next request is issued when a previous one completes"),
        # One sentence a reader (and check D7) can compare against the model name. It must
        # contradict nothing else in the block: an open-loop note that says "fixed in-flight" or
        # "issued when a previous one completes" is a document arguing with itself.
        "arrival_note": (
            "Open loop: arrivals follow an exponential inter-arrival schedule at the target rate "
            "and are issued on time whether or not the engine is keeping up. In-flight count is an "
            "outcome, not an input."
            if mode == "poisson" else
            "Closed loop: a fixed in-flight population per level, no independent arrival process, "
            "each request is issued when a previous one completes."),
        "independent_of_completions": mode == "poisson",
        "why_it_is_reported": (
            "A latency percentile quoted as a service level has to say how the requests arrived. "
            "Closed-loop dispatch throttles itself whenever the server slows down, so it cannot "
            "produce the queue build-up that generates real tail latency and its percentiles are "
            "optimistic by construction. Open-loop dispatch keeps offering work on schedule, so "
            "an overloaded server shows up as an achieved rate below the target rather than as a "
            "quietly slower but still 'passing' run."),
    }
    if mode != "poisson" and rates:
        # Recorded rather than dropped: the user asked for something this mode cannot do, and a
        # statement of that in the file beats a claim in the file plus a warning on a console
        # nobody kept.
        block["rate_ignored_because"] = "closed loop has no arrival rate"
        block["rate_as_asked"] = rates
    # The note travels in the report block beside the model, from the same switch, because that is
    # the pair check D7 reads: a document that declares one model and prints the other's prose next
    # to it is arguing with itself, and the prose is the half a reader believes.
    return {"arrival_model": model,
            "report": {"arrival_model": model, "arrival_note": block["arrival_note"]},
            "arrival": block}


def whole_waves(concurrency, requested):
    """Round a request count up to a whole multiple of the concurrency.

    A partial final wave runs at LOWER concurrency than the level claims to measure, so the level
    reports a throughput somewhere between its nominal concurrency and the size of its tail. The
    error is silent, it is largest at the middle concurrencies where the tail is a big fraction of
    the work, and it vanishes wherever the count happens to divide, which makes it look like
    scatter at one level rather than a systematic fault.

    Returns (effective, waves).
    """
    c = max(1, int(concurrency))
    n = max(c, int(requested))
    waves = (n + c - 1) // c
    return waves * c, waves


def level_requests(args, concurrency, requested):
    """How many requests one level issues.

    Closed loop rounds up to a whole wave, for the reason whole_waves() documents. Open loop has no
    waves: the count is taken as asked, and the level simply lasts about requested/rate seconds.
    """
    if getattr(args, "arrival", "closed") == "poisson":
        return max(1, int(requested))
    return whole_waves(concurrency, requested)[0]


def run_level(args, concurrency, total_requests, in_tok=None, out_tok=None,
              send=None, make_client=None, rate=None, level_index=0):
    """Run one level of the sweep under whichever arrival process args.arrival selects.

    `send` and `make_client` exist so the dispatch and accounting can be tested without a server:
    the scheduling code that has to be right is the code under test, and pointing it at a live
    engine to check that a mean inter-arrival is 1/rate would prove nothing about the schedule and
    everything about the network.

    `level_index` is the level's position in its sweep. It exists only to derive this level's
    arrival seed from the base one, so the levels of a sweep are independent draws.
    """
    in_tok = args.input_tokens if in_tok is None else in_tok
    out_tok = args.output_tokens if out_tok is None else out_tok
    make_client = make_client or (lambda: Client(args.base_url, args.timeout))
    if send is None:
        def send(client, salt):
            return one_request(client, args, salt, in_tok, out_tok)

    arrival = getattr(args, "arrival", "closed") or "closed"
    if arrival not in ARRIVAL_MODELS:
        raise ValueError("unknown arrival model %r" % (arrival,))
    # An explicit rate wins, so a rate sweep can drive one level at a time without rewriting args
    # (and without doc["config"] losing the sweep it was asked for).
    rates = parse_rates(getattr(args, "rate", None))
    rate = float(rate) if rate is not None else (rates[0] if rates else None)
    base_seed = seed_or_default(getattr(args, "arrival_seed", None))
    seed = level_seed(base_seed, level_index)
    sample_interval, sample_interval_source = sample_interval_for(args, total_requests, rate)

    results = []
    errors = []            # engine-attributable, and the only bucket error_count reports
    harness_errors = []    # client-side: connection setup, name resolution, fd exhaustion
    dispatch_failures = []  # arrivals the harness could not even start a thread for
    lock = threading.Lock()
    # dispatched doubles as the salt counter, exactly as `counter` did before.
    state = {"dispatched": 0, "completed": 0, "inflight": 0, "peak_inflight": 0,
             "inflight_at_last_dispatch": 0, "peak_threads_alive": 0,
             "clients_created": 0, "connections_opened": 0,
             # outcomes booked against a specific dispatched request. A worker that dies before it
             # has taken any work is NOT one of these, which is why it is counted separately.
             "harness_request_errors": 0, "stages": {}}
    depth_samples = []
    server_queue_samples = []
    deviations_s = []       # signed (actual dispatch - intended offset), one per arrival
    # (arrival offset, request start, end, censored) per dispatched request that reached the wire,
    # all timed from THIS side of it. The growth fit runs on these and not on the latency the
    # response reported, so an engine that mis-states its own timings cannot flatter the verdict.
    #
    # censored=True is a request the engine never finished: a timeout or a broken stream. Its
    # duration is a LOWER BOUND on the wait, not a missing observation, and it belongs in the fit
    # for exactly that reason. Excluding them is how a level of 90 arrivals with a true median wait
    # of 16 s reported an e2e p95 of 1.82 s: the six that survived a 3 s timeout were the only
    # evidence left, and they were the six that had not queued.
    timeline = []

    def _book_stage(stage):
        state["stages"][stage] = state["stages"].get(stage, 0) + 1

    def _finish(fn, booked, arrival_offset=None):
        """Run one request and book the outcome. Shared by both dispatchers so the two paths
        cannot disagree about what counts as an error or when a request left the flight.

        `booked` is a one-element list the caller can read afterwards: it is how the caller knows
        whether this request's outcome has already been recorded, so a failure OUTSIDE this
        function can book it without any chance of booking it twice.
        """
        began = time.perf_counter()
        try:
            r = fn()
            done = time.perf_counter()
            with lock:
                results.append(r)
                if arrival_offset is not None:
                    timeline.append((arrival_offset, began, done, False))
        except Exception as exc:  # noqa: BLE001
            failed_at = time.perf_counter()
            stage = error_stage(exc)
            with lock:
                _book_stage(stage)
                if stage.startswith("harness"):
                    harness_errors.append(stage + ": " + repr(exc))
                    state["harness_request_errors"] += 1
                else:
                    errors.append(repr(exc))
                    # An engine-side failure took a measured amount of time before it failed, and
                    # that time is evidence: a timeout at 3 s means this request waited at least
                    # 3 s. Booked as censored so the fit sees it. Harness-side failures are NOT
                    # booked here (a name that would not resolve says nothing about the engine's
                    # queue); they are covered instead by the completion-fraction floor, which
                    # counts every dispatched request that did not come back.
                    if arrival_offset is not None:
                        timeline.append((arrival_offset, began, failed_at, True))
        finally:
            booked[0] = True
            with lock:
                state["completed"] += 1
                state["inflight"] -= 1

    def _book_harness_failure(stage, exc, booked, counts_as_request):
        """Record a failure that happened outside _finish, and release the flight if _finish never
        ran. Without this a client that could not be constructed killed its daemon thread in
        silence: the request was counted as dispatched, no outcome was ever booked, in-flight was
        never released, and the identity attempted == ok + errors broke in the direction that
        lowers the completion rate and so feeds a false 'the engine did not keep up'."""
        with lock:
            _book_stage(stage)
            harness_errors.append(stage + ": " + repr(exc))
            if counts_as_request:
                state["harness_request_errors"] += 1
                if not booked[0]:
                    booked[0] = True
                    state["completed"] += 1
                    state["inflight"] -= 1

    def _new_client():
        with lock:
            state["clients_created"] += 1
        return make_client()

    def _release(client):
        closer = getattr(client, "close", None)
        if callable(closer):
            closer()
        # Clients that count their own connects hand the number back on the way out, which is how
        # a client-side connection ceiling becomes visible in the result instead of arriving as a
        # pile of engine errors.
        with lock:
            state["connections_opened"] += int(getattr(client, "connects", 0) or 0)

    def worker(worker_id):
        """Closed loop: this worker holds exactly one request in flight at a time, and issues its
        next one only once the previous has returned."""
        booked = [True]
        try:
            client = _new_client()
        except Exception as exc:  # noqa: BLE001
            # This worker never took any work, so nothing is lost: the other workers still run the
            # level to completion. Recorded anyway, because a level quietly run by three of four
            # workers is a level whose concurrency is not the one it claims.
            _book_harness_failure("harness_worker_client_setup", exc, booked, False)
            return
        try:
            while True:
                with lock:
                    if state["dispatched"] >= total_requests:
                        return
                    salt = state["dispatched"]
                    state["dispatched"] += 1
                    state["inflight"] += 1
                    state["peak_inflight"] = max(state["peak_inflight"], state["inflight"])
                booked = [False]
                _finish(lambda: send(client, salt), booked)
        finally:
            _release(client)

    def one_shot(salt, arrival_offset):
        """Open loop: one arrival, its own connection, no coordination with any other.

        Everything is inside the guarded path, close() is in a finally, and any exception at all
        books an outcome. A daemon thread that dies between the dispatch and _finish leaves the
        level's accounting broken in a way nothing else in the document would show.
        """
        booked = [False]
        client = None
        try:
            client = _new_client()
            _finish(lambda: send(client, salt), booked, arrival_offset=arrival_offset)
        except BaseException as exc:  # noqa: BLE001
            _book_harness_failure("harness_client_setup", exc, booked, True)
        finally:
            if client is not None:
                try:
                    _release(client)
                except Exception:  # noqa: BLE001
                    pass

    stop_sampler = threading.Event()

    def sample_depth(t0):
        """Client-side in-flight depth: requests dispatched minus requests completed.

        Costs nothing and needs no server cooperation, which is why it is on by default in open
        loop. It is the load generator's own view of how many requests it is holding, not the
        engine's queue, and it is not a backlog: rate x service requests in flight is Little's law.
        The engine's own split of its work into running vs waiting is polled separately.

        The live thread count rides along, because one thread per arrival means the harness has a
        ceiling of its own and a reader needs to see how close a level came to it.
        """
        while True:
            with lock:
                state["peak_threads_alive"] = max(state["peak_threads_alive"],
                                                  threading.active_count())
                if len(depth_samples) < MAX_QUEUE_SAMPLES:
                    depth_samples.append([round(time.perf_counter() - t0, 4), state["inflight"]])
            if stop_sampler.wait(sample_interval):
                return

    def poll_server_queue(t0):
        """The ENGINE's split of the work into running vs waiting, sampled through the level.

        Scraped on the depth sampler's interval (floored, because an HTTP GET per 5 ms against a
        production engine is a load test of its own) and in its own thread, so a slow scrape
        stretches this series and never the in-flight trace. The before/after gauges alone could
        not do this job: both are read at instants when nothing is queued, one before the first
        arrival and one after the last join, so requests_waiting_end was approximately zero BY
        CONSTRUCTION however deep the level's queue had been.
        """
        interval = max(sample_interval, MIN_METRICS_POLL_S)
        while True:
            m = scrape_metrics(client_for_metrics)
            if m is not None:
                with lock:
                    if len(server_queue_samples) < MAX_QUEUE_SAMPLES:
                        server_queue_samples.append([round(time.perf_counter() - t0, 4),
                                                     m.get("vllm:num_requests_running"),
                                                     m.get("vllm:num_requests_waiting")])
            if stop_sampler.wait(interval):
                return

    # Created through the counter like every other client, so clients_created is the whole truth
    # about how many connections this level asked the OS for. Scraped before the first arrival, and
    # the answer decides whether the server-side queue is pollable at all.
    client_for_metrics = _new_client()
    before = scrape_metrics(client_for_metrics)

    truncated = False
    if arrival == "poisson":
        if rate is None:
            raise ValueError("poisson arrivals need --rate")
        offsets = poisson_offsets(rate, total_requests, seed)
        threads = []
        wall_start = time.perf_counter()
        sampler = threading.Thread(target=sample_depth, args=(wall_start,))
        sampler.daemon = True
        sampler.start()
        metrics_poller = None
        if before is not None:
            # Only poll an endpoint that has already answered once. Hammering a 404 for the life of
            # the level would cost the same and measure nothing.
            metrics_poller = threading.Thread(target=poll_server_queue, args=(wall_start,))
            metrics_poller.daemon = True
            metrics_poller.start()
        last_dispatch = wall_start
        for salt, offset in enumerate(offsets):
            due = wall_start + offset
            slack = due - time.perf_counter()
            if slack > 0:
                time.sleep(slack)
            t = threading.Thread(target=one_shot, args=(salt, offset))
            t.daemon = True
            with lock:
                prev_peak = state["peak_inflight"]
                prev_at_last = state["inflight_at_last_dispatch"]
                state["dispatched"] += 1
                state["inflight"] += 1
                state["peak_inflight"] = max(state["peak_inflight"], state["inflight"])
                # Overwritten each arrival, so what survives is the depth at the LAST one. That is
                # the number worth reading: once arrivals stop the flight necessarily drains to
                # zero, so a depth sampled at the end of the level says nothing.
                state["inflight_at_last_dispatch"] = state["inflight"]
            try:
                t.start()
            except (RuntimeError, MemoryError, OSError) as exc:  # noqa: BLE001
                # The harness, not the engine, hit a ceiling: one thread per arrival cannot scale
                # without limit. Do NOT cap the in-flight count in response, which would quietly
                # turn the level closed-loop. Roll this arrival back, name it, and end the level
                # early with a flag, so the levels already finished survive and this one cannot be
                # read as a rate the machine sustained.
                with lock:
                    state["dispatched"] -= 1
                    state["inflight"] -= 1
                    state["peak_inflight"] = prev_peak
                    state["inflight_at_last_dispatch"] = prev_at_last
                    dispatch_failures.append({"salt": salt,
                                              "scheduled_offset_s": round(offset, 6),
                                              "error": repr(exc)})
                    _book_stage("harness_thread_start")
                truncated = True
                break
            now = time.perf_counter()
            last_dispatch = now
            with lock:
                # Measured AFTER the sleep, after the thread started and after the lock, which is
                # where the request actually goes out. Measured before the sleep it recorded only
                # the slack it already knew about, so an overshoot smaller than the next gap was
                # invisible: 25 arrivals each 12 ms late reported two late arrivals and a maximum
                # of 11 ms. It also exposes the dispatcher blocking on this lock against completing
                # threads, which is the one residual coupling between dispatch and completions.
                deviations_s.append(now - due)
            threads.append(t)
        for t in threads:
            t.join()
        wall = time.perf_counter() - wall_start
        stop_sampler.set()
        sampler.join(timeout=sample_interval * 4)
        if metrics_poller is not None:
            metrics_poller.join(timeout=max(sample_interval, MIN_METRICS_POLL_S) * 4)
        dispatch_span = last_dispatch - wall_start
        # The span of the schedule ACTUALLY ATTEMPTED. On a truncated level offsets[-1] is a
        # deadline that was never reached, and judging the generator against it would blame the
        # dispatcher for arrivals it was never allowed to make.
        schedule_span = offsets[state["dispatched"] - 1] if state["dispatched"] else 0.0
    else:
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(concurrency)]
        wall_start = time.perf_counter()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        wall = time.perf_counter() - wall_start
        dispatch_span = schedule_span = None

    after = scrape_metrics(client_for_metrics)
    # The metrics client opens a connection per scrape and the server-side poller scrapes on a
    # timer, so its sockets are part of the harness's own connection budget and belong in the
    # count that a client-side ceiling would show up in.
    state["connections_opened"] += int(getattr(client_for_metrics, "connects", 0) or 0)

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
    prefill_basis = None
    if out_tok == 1 and ttfts and in_tokens:
        if arrival == "poisson":
            # Open loop has no fixed concurrency to multiply by, because the in-flight count is an
            # outcome, not an input. With max_tokens=1 every completed token is prefill work, so
            # the honest figure is the one the level actually delivered over its wall clock.
            prefill_tok_s = in_tokens / wall if wall > 0 else None
            prefill_basis = "prompt tokens completed / wall time (open loop: concurrency is not an input)"
        elif concurrency:
            per_req_prompt = in_tokens / float(len(ok))
            prefill_tok_s = per_req_prompt / statistics.fmean(ttfts) * concurrency
            prefill_basis = "per-request prompt tokens / mean TTFT x concurrency (closed loop)"

    achieved_rate = len(ok) / wall if wall > 0 else None
    arrival_block = {
        "model": ARRIVAL_MODELS[arrival],
        "target_rate_req_s": rate if arrival == "poisson" else None,
        # Completions over the level's wall clock. An honest THROUGHPUT: the machine really did
        # finish this many requests per second. It is not a rate to compare against the offered
        # one, for the reason completions_per_s_incl_drain spells out below.
        "achieved_rate_req_s": achieved_rate,
        "seed": seed if arrival == "poisson" else None,
    }
    if arrival == "poisson":
        deficit = None
        if rate:
            deficit = (rate - (achieved_rate or 0.0)) / rate * 100.0
        # n arrivals over the span the generator actually took to issue them: the load that was
        # really OFFERED, as opposed to the load that was asked for.
        offered = (state["dispatched"] / dispatch_span
                   if dispatch_span and dispatch_span > 0 else None)
        # Attribution. A shortfall against the target has THREE possible owners and they must not
        # be confused: the random schedule itself (a finite draw realizes a rate about 1/sqrt(n)
        # away from the nominal one: 60 requests at 200/s land near 190/s and nothing is wrong),
        # the generator (thread dispatch cost, OS timer granularity), or the engine. So:
        #   schedule_realized_rate  what the DRAW asked for, vs the nominal target
        #   generator_kept_up       whether dispatch honoured that draw, span against span
        #   fell_behind             whether the engine completed what was actually OFFERED
        # An earlier version judged all three against the nominal target and reported that the
        # engine had not kept up whenever the draw came in slow, which is the exact confusion an
        # open-loop mode is supposed to remove.
        schedule_rate = (state["dispatched"] / schedule_span
                         if schedule_span and schedule_span > 0 else None)
        offered_deficit = (None if not (offered and achieved_rate is not None)
                           else (offered - achieved_rate) / offered * 100.0)

        # ---- generator fidelity, judged per arrival (H5/H9) -------------------------------
        lateness_s = [d for d in deviations_s if d > 0]
        abs_dev = [abs(d) for d in deviations_s]
        mean_gap = (1.0 / rate) if rate else None
        fidelity_budget = (mean_gap * GENERATOR_FIDELITY_FRACTION) if mean_gap else None
        dev_p95 = pct(abs_dev, 95) if abs_dev else None
        # Span against span is kept, as its own field, because it answers a different question:
        # whether the level as a whole finished on the clock. It cannot answer the fidelity
        # question, since absolute deadlines make lateness self-correcting, since a 600 ms stall at
        # rate 30 left schedule_span and dispatch_span equal to the millisecond while 17 arrivals
        # fired as one catch-up burst, and a burst's queue is not the engine's fault.
        schedule_span_honoured = (None if not (schedule_span and dispatch_span is not None)
                                  else bool(dispatch_span <= schedule_span * 1.02 + 0.01))
        generator_kept_up = (None if (dev_p95 is None or fidelity_budget is None)
                             else bool(dev_p95 <= fidelity_budget))

        # ---- queue growth, which is what "the engine did not keep up" has to mean ----------
        # (arrival offset, completion relative to the level start, latency the harness itself
        # timed). The latency is measured from the moment this request's own thread began, so it is
        # the engine's contribution and not the harness's thread-scheduling delay: the generator's
        # own fidelity is judged separately, just above.
        tl = [(a, done - wall_start, done - began, cens) for (a, began, done, cens) in timeline]
        completed_in_window = sum(1 for (_a, c, _e, cens) in tl
                                  if c <= dispatch_span and not cens)
        window_rate = (completed_in_window / dispatch_span
                       if dispatch_span and dispatch_span > 0 else None)
        lat_xs = [a for (a, _c, _e, _z) in tl]
        lat_ys = [e for (_a, _c, e, _z) in tl]
        censored_n = sum(1 for (_a, _c, _e, cens) in tl if cens)
        latency_grew, grew_why, gstats = judge_latency_growth(
            lat_xs, lat_ys, dispatch_span, state["dispatched"], len(ok),
            generator_kept_up=generator_kept_up, truncated=truncated, censored_n=censored_n,
            total_requests=total_requests)
        if generator_kept_up is False:
            # The pure function has no view of the fidelity numbers, so the level fills them in.
            grew_why += (" (p95 deviation %.1f ms against a %.1f ms budget), so these arrivals are "
                         "a catch-up burst and not the Poisson stream this level names"
                         % ((dev_p95 or 0.0) * 1000.0, (fidelity_budget or 0.0) * 1000.0))
        lat_slope = gstats["e2e_slope_s_per_s"]
        lat_se = gstats["slope_std_error"]
        lat_t = gstats["slope_t_stat"]
        mk_s, mk_z, mk_tau = (gstats["mann_kendall_s"], gstats["mann_kendall_z"],
                              gstats["mann_kendall_tau"])
        ts_slope = gstats["theil_sen_slope_s_per_s"]
        trend_detected = gstats["trend_detected"]
        median_e2e = gstats["median_e2e_s"]
        lat_growth = gstats["growth_over_arrival_window_s"]
        lat_growth_ratio = gstats["growth_as_multiple_of_median_e2e"]
        completion_fraction = gstats["completion_fraction"]

        # ---- what the ENGINE's own queue says, which is the only thing that can turn a latency
        # ---- trend into a capacity claim (H14). This box serves prod, qa, dev and live from ONE
        # ---- vLLM, so a co-tenant slowdown raises OUR latencies without OUR rate having caused
        # ---- anything. Reproduced on an unlimited-parallelism fake that CANNOT queue by
        # ---- construction, given a linearly drifting service time: 0.08 s/s of drift reported the
        # ---- engine had not kept up at ratio 1.091, and 0.50 s/s at ratio 1.850, with zero errors
        # ---- and zero queueing anywhere in the system.
        srv_win = [(t, w) for (t, _r, w) in server_queue_samples
                   if dispatch_span and t <= dispatch_span and w is not None]
        srv_wait_max = max([w for _t, w in srv_win], default=None)
        _srv_s, srv_wait_z, _srv_tau = mann_kendall([t for t, _w in srv_win],
                                                    [w for _t, w in srv_win])
        if len(srv_win) < 5:
            engine_queue_grew = None
        else:
            engine_queue_grew = bool(srv_wait_z is not None and srv_wait_z >= QUEUE_GROWTH_MK_Z
                                     and (srv_wait_max or 0) >= 1)
        if latency_grew is None:
            engine_did_not_keep_up = None
            capacity_why = "not judged, because the latency trend was not judged: " + grew_why
        elif latency_grew is False:
            engine_did_not_keep_up = False
            capacity_why = ("latency did not grow over this level, so nothing here says the engine "
                            "was short of capacity at this rate")
        elif engine_queue_grew is None:
            engine_did_not_keep_up = None
            capacity_why = (
                "CANNOT TELL. Latency grew, but the engine's own running/waiting split was not "
                "available for this level, so the growth cannot be attributed to the rate offered "
                "here. On an engine shared with other tenants a co-tenant slowdown produces the "
                "same latency trend with no queue of ours anywhere.")
        elif engine_queue_grew is False:
            engine_did_not_keep_up = False
            capacity_why = (
                "latency grew but the ENGINE's own waiting count did not (max waiting %s over %d "
                "server-side samples). Something slowed every request down; the rate offered here "
                "is not shown to be the cause, and sizing hardware for this rate would be sizing "
                "for the wrong thing." % (srv_wait_max, len(srv_win)))
        else:
            engine_did_not_keep_up = True
            capacity_why = (
                "latency grew AND the engine's own waiting count grew with it (max waiting %s, "
                "Mann-Kendall z %.2f over %d server-side samples): work was queued inside the "
                "engine at this offered rate." % (srv_wait_max, srv_wait_z or 0.0, len(srv_win)))
        # The in-flight trace's own slope, over the arrival window. A DIAGNOSTIC, not the verdict:
        # in-flight climbs to rate x mean latency even with zero queueing (Little's law), and on a
        # level shorter than one service time the whole trace is ramp-up, so a positive slope here
        # is expected on a perfectly healthy engine.
        win = [(t, d) for t, d in depth_samples if t <= dispatch_span]
        inflight_slope, _ise, inflight_t = ols_slope([t for t, _d in win], [d for _t, d in win])
        # Little's law, from the same harness-timed latencies the verdict uses, so the baseline and
        # the verdict cannot disagree about how long a request took.
        mean_latency = (statistics.fmean(lat_ys) if lat_ys
                        else (statistics.fmean(e2es) if e2es else None))
        littles_law = (offered * mean_latency if offered and mean_latency else None)

        arrival_block.update({
            "requests_dispatched": state["dispatched"],
            "truncated_by_harness_limit": truncated,
            "dispatch_failures": dispatch_failures,
            "schedule_span_s": schedule_span,
            "dispatch_span_s": dispatch_span,
            "achieved_arrival_rate_req_s": offered,
            "rate_deficit_pct": deficit,
            "schedule_realized_rate_req_s": schedule_rate,
            "schedule_vs_target_pct": ((schedule_rate - rate) / rate * 100.0
                                       if schedule_rate and rate else None),
            "seed_base": base_seed,
            "level_index": level_index,
            "generator_kept_up": generator_kept_up,
            "generator_fidelity": {
                "basis": ("p95 of |actual dispatch - intended offset| against "
                          "%g%% of the mean inter-arrival"
                          % (GENERATOR_FIDELITY_FRACTION * 100.0)),
                "p95_abs_deviation_ms": (dev_p95 * 1000.0) if dev_p95 is not None else None,
                "budget_ms": (fidelity_budget * 1000.0) if fidelity_budget else None,
                "mean_inter_arrival_ms": (mean_gap * 1000.0) if mean_gap else None,
                "max_abs_deviation_ms": (max(abs_dev) * 1000.0) if abs_dev else None,
                "mean_signed_deviation_ms": (statistics.fmean(deviations_s) * 1000.0
                                             if deviations_s else None),
                "schedule_span_honoured": schedule_span_honoured,
            },
            "completions_per_s_incl_drain": achieved_rate,
            "completions_per_s_incl_drain_note": (
                "completions over the level's WALL CLOCK, which runs to the last completion, while "
                "the offered rate runs to the last ARRIVAL. The wall clock therefore exceeds the "
                "arrival span by at least one service time even on an engine with unlimited "
                "headroom and provably zero queueing, so the difference between these two carries "
                "a built-in deficit of about service/(span+service): 6% at a 0.1 s service time "
                "and 39% at 1 s, on a fake engine that queued nothing. No verdict is derived from "
                "it."),
            "completions_in_arrival_window": completed_in_window,
            "completions_per_s_in_arrival_window": window_rate,
            "completions_in_arrival_window_note": (
                "completions that landed before the last arrival, over the arrival span. Free of "
                "the drain, but biased the same way by the RAMP: nothing can complete during the "
                "first service time either. Reported, not used as a verdict."),
            "completion_deficit_vs_offered_pct": offered_deficit,
            "completion_deficit_is_not_a_verdict": (
                "drain-biased by construction; see completions_per_s_incl_drain_note"),
            "queue_growth": {
                "basis": ("Mann-Kendall rank trend of per-request end-to-end latency against "
                          "arrival time over the arrival window, sized by the Theil-Sen slope. A "
                          "queue that is not growing leaves latency flat however many requests are "
                          "in flight; a queue that is growing raises every later request's "
                          "latency. Rank-based because queueing latencies are heavy-tailed."),
                "latency_source": ("timed by the harness, from the moment the request's own thread "
                                   "began to the moment it returned, so an engine that mis-states "
                                   "its own latency cannot flatter the verdict"),
                "n": len(lat_ys),
                "n_censored": censored_n,
                "censored_note": (
                    "requests the engine never finished (timeout, broken stream) are booked into "
                    "the fit at their harness-timed duration. That duration is a LOWER BOUND on "
                    "the wait, so a level with censored requests understates the trend rather than "
                    "inventing one. They used to be dropped, which removed exactly the requests "
                    "that prove a queue."),
                "requests_dispatched": state["dispatched"],
                "completion_fraction": completion_fraction,
                "completion_fraction_floor": QUEUE_GROWTH_MIN_COMPLETION_FRACTION,
                "mann_kendall_s": mk_s,
                "mann_kendall_z": mk_z,
                "mann_kendall_tau": mk_tau,
                "theil_sen_slope_s_per_s": ts_slope,
                "trend_detected": trend_detected,
                "trend_detected_note": (
                    "the rank trend ALONE, before the effect gate. It is not the verdict, and it "
                    "does not carry the effect gate's sensitivity limit below."),
                "e2e_slope_s_per_s": lat_slope,
                "slope_std_error": lat_se,
                "slope_t_stat": lat_t,
                "slope_t_stat_is_reported_only": (
                    "the OLS t statistic is no longer a gate. It never bound on deterministic "
                    "engines (12,998 to 145,159) and under realistic dispersion it became the "
                    "whole verdict, flipping a real trend to False as multiplicative noise rose to "
                    "cv 0.8 and 1.1. OLS standard errors also assume independent residuals, which "
                    "queueing latencies violate by construction."),
                "median_e2e_s": median_e2e,
                "growth_over_arrival_window_s": lat_growth,
                "growth_as_multiple_of_median_e2e": lat_growth_ratio,
                "gate_t_stat": QUEUE_GROWTH_T,
                "gate_mann_kendall_z": QUEUE_GROWTH_MK_Z,
                "gate_growth_multiple": QUEUE_GROWTH_EFFECT,
                "sensitivity_limit": (
                    "STATED LIMIT, not removed by the rank test. For a queue growing linearly "
                    "through the level the median latency is L0 + g*T/2, so growth/median tends to "
                    "2 as the ramp dominates and the %.1fx effect gate sits at half the largest "
                    "value the statistic can take. Bisected against the fake engine: a 1.86x ramp "
                    "was missed and a 1.90x ramp was caught. Scaled to a machine whose baseline "
                    "latency is 2.18 s at concurrency 1, up to about 4.1 s of latency growth per "
                    "arrival window is invisible to this gate. Read trend_detected beside the "
                    "verdict: the rank test sees a monotone trend that the effect gate discards."
                    % QUEUE_GROWTH_EFFECT),
                "engine_side": {
                    "samples_in_arrival_window": len(srv_win),
                    "max_requests_waiting": srv_wait_max,
                    "waiting_trend_mann_kendall_z": srv_wait_z,
                    "engine_queue_grew": engine_queue_grew,
                    "why_it_gates_the_capacity_claim": (
                        "the latency trend on its own cannot say WHOSE load caused it. This engine "
                        "may serve several environments at once, so a co-tenant slowdown raises "
                        "these latencies without this level's rate having queued anything. Only "
                        "the engine's own waiting count can tell those apart."),
                },
                "inflight_slope_req_per_s": inflight_slope,
                "inflight_slope_t_stat": inflight_t,
                "inflight_slope_note": (
                    "diagnostic only. In-flight rises to rate x mean latency with zero queueing "
                    "(Little's law), and a level shorter than one service time is all ramp-up, so "
                    "a positive slope here does not by itself mean a backlog."),
            },
            # What is MEASURED here is a latency trend over one level, and the key says so. It used
            # to be called fell_behind, which reads as a statement about the engine's capacity at
            # the offered rate, and it is not one: on an unlimited-parallelism fake that cannot
            # queue by construction, a linearly drifting service time produced fell_behind true at
            # ratios of 1.091 and 1.850 with zero queueing anywhere. engine_did_not_keep_up below
            # is the capacity claim, and it is gated on the ENGINE's own running/waiting split.
            "latency_grew_over_the_level": latency_grew,
            "latency_grew_over_the_level_basis": grew_why,
            "latency_grew_is_not_a_capacity_claim": (
                "a latency trend says requests got slower through the level. It does not say the "
                "rate offered here caused it. Read engine_did_not_keep_up for that."),
            "engine_did_not_keep_up": engine_did_not_keep_up,
            "engine_did_not_keep_up_basis": capacity_why,
            # Deprecated alias, kept so nothing reading the old name silently loses its value. It
            # carries the latency-trend verdict, which is what it always carried.
            "fell_behind": latency_grew,
            "fell_behind_basis": grew_why,
            "fell_behind_renamed_to": (
                "latency_grew_over_the_level. The old name claimed more than the measurement "
                "supports; engine_did_not_keep_up is the capacity claim and is gated separately."),
            "dispatch_lateness_ms": {
                "count_late": len(lateness_s),
                "p95": (pct(lateness_s, 95) or 0.0) * 1000 if lateness_s else 0.0,
                "max": max(lateness_s) * 1000 if lateness_s else 0.0,
                "measured": ("actual dispatch minus intended offset, after the sleep and after the "
                             "lock. Measured before the sleep it saw only the slack it already "
                             "knew about, so any overshoot smaller than the next gap was invisible"),
            },
            "queue_depth": {
                "source": "client_inflight (requests dispatched minus requests completed)",
                "is_not_a_backlog": (
                    "this is the load generator's in-flight count, not the engine's queue. rate x "
                    "service requests in flight is Little's law on a healthy engine. The engine's "
                    "own running/waiting split is in server_side below."),
                "sample_interval_s": sample_interval,
                "sample_interval_source": sample_interval_source,
                "sample_count": len(depth_samples),
                # The requested interval is a FLOOR. Event.wait is bounded below by the OS timer
                # tick, about 15 ms on Windows against about 1 ms on Linux, so a short level can
                # come back with fewer points than the interval asked for. Reported rather than
                # assumed, so nobody reads "100 points" off the request.
                "effective_interval_s": (
                    statistics.median([b[0] - a[0] for a, b in zip(depth_samples,
                                                                   depth_samples[1:])])
                    if len(depth_samples) > 2 else None),
                "samples": depth_samples,
                "truncated": len(depth_samples) >= MAX_QUEUE_SAMPLES,
                # Two maxima for one quantity is one maximum too many, and the sampled one loses
                # exactly when it matters: a 0.512 s level sampled twice reported max 10 against a
                # peak_inflight of 14.
                "max": max([d[1] for d in depth_samples] + [state["peak_inflight"]]),
                "max_source": "max(sampled trace, peak_inflight counted at every dispatch)",
                "peak_inflight": state["peak_inflight"],
                "peak_threads_alive": state["peak_threads_alive"],
                "mean": (statistics.fmean([d[1] for d in depth_samples]) if depth_samples else None),
                # The depth the arrival stream had reached by the time it stopped. Read this, not
                # the last sample: after the last arrival the flight necessarily drains to zero, so
                # a depth measured at the end of the level always looks healthy.
                "inflight_at_last_arrival": state["inflight_at_last_dispatch"],
                "littles_law_inflight": littles_law,
                "littles_law_note": (
                    "offered rate x mean end-to-end latency: the in-flight count a healthy engine "
                    "must show. Excess of inflight_at_last_arrival over THIS is the part that "
                    "could be a backlog."),
                "last_sample": depth_samples[-1][1] if depth_samples else None,
                # Time the level spent draining after arrivals stopped. An engine keeping up drains
                # in about one service time; a long drain is a queue that had been growing.
                "drain_s": (wall - dispatch_span) if dispatch_span is not None else None,
                "server_side": ({"samples": server_queue_samples,
                                 "columns": ["t_s", "vllm:num_requests_running",
                                             "vllm:num_requests_waiting"],
                                 "sample_interval_s": max(sample_interval, MIN_METRICS_POLL_S)}
                                if server_queue_samples else
                                {"samples": [], "why": "no vLLM metrics endpoint answered, so the "
                                                       "engine's own running/waiting split is not "
                                                       "available for this level"}),
            },
        })
    else:
        arrival_block["queue_depth"] = {
            "sampled": False,
            "why": ("in closed loop the in-flight count is pinned at the concurrency by "
                    "construction, so sampling it measures the harness rather than the queue"),
        }

    return {
        "concurrency": concurrency,
        "arrival": arrival_block,
        "peak_inflight": state["peak_inflight"],
        "prefill_basis": prefill_basis,
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
        # Waves are a closed-loop concept: they exist because the pool refills in lockstep. Under
        # Poisson arrivals there is no wave to be whole, so the keys say None rather than False,
        # which would read as "measured, and not whole".
        "waves": None if arrival == "poisson" else ((total_requests // concurrency) if concurrency else None),
        "whole_waves": None if arrival == "poisson" else bool(concurrency and total_requests % concurrency == 0),
        "errors": errors[:5],
        "error_count": len(errors),
        # Every dispatched request must produce exactly one outcome: a result, an engine error, or
        # a harness error. The difference is reported rather than assumed, because the way this
        # broke was silent and it broke in the direction that flatters nothing: a client that could
        # not be constructed took its request, its outcome and its in-flight slot with it, which
        # lowered the completion rate and inflated the in-flight trace for the rest of the level.
        "requests_dispatched": state["dispatched"],
        "requests_unaccounted": (state["dispatched"] - len(results) - len(errors)
                                 - state["harness_request_errors"]),
        "results_without_ttft": len(results) - len(ok),
        "harness_errors": harness_errors[:5],
        "harness_error_count": len(harness_errors),
        "error_stages": dict(state["stages"]),
        "error_stage_note": ("error_count counts only what the engine answered for. Connect, "
                             "resolve and socket-exhaustion failures are the harness hitting its "
                             "own ceiling (one connection per arrival, no reuse, so roughly 470 "
                             "connections per second against 28k ephemeral ports and a 60 s "
                             "TIME_WAIT) and are counted separately so they can never read as the "
                             "engine refusing work."),
        "clients_created": state["clients_created"],
        "connections_opened": state["connections_opened"],
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
    ap.add_argument("--arrival", choices=sorted(ARRIVAL_MODELS), default="closed",
                    help="closed: a fixed pool of --concurrency workers, each issuing its next "
                         "request only when the previous completes (default; unchanged). "
                         "poisson: open loop, requests issued on an exponential inter-arrival "
                         "schedule at --rate whether or not the server keeps up.")
    ap.add_argument("--rate", default=None,
                    help="poisson mode: target arrival rate in requests/second. Comma-separated "
                         "to sweep, e.g. 2,4,8. --concurrency is not a limit in this mode.")
    ap.add_argument("--arrival-seed", type=int, default=DEFAULT_ARRIVAL_SEED,
                    help="base seed for the arrival schedule, so two runs replay the same one. "
                         "Each level draws from a seed derived from this one and the level index, "
                         "so the levels of a sweep are independent draws rather than the same "
                         "draw error at every rate.")
    ap.add_argument("--queue-sample-interval", type=float, default=None,
                    help="poisson mode: seconds between in-flight depth samples. Default is sized "
                         "from the level, (requests/rate)/100 clamped to [0.005, 0.25], so every "
                         "level gets about 100 points however short it is. Pass a value to "
                         "override; it must be above zero.")
    args = ap.parse_args()

    levels = [int(x) for x in args.concurrency.split(",") if x.strip()]
    rates = parse_rates(args.rate)
    if args.queue_sample_interval is not None and not args.queue_sample_interval > 0:
        # A zero or negative interval makes Event.wait return immediately, so the sampler hot-spins
        # on the dispatcher's lock and slows the arrivals it exists to observe.
        print("--queue-sample-interval must be above zero, got %r" % (args.queue_sample_interval,),
              file=sys.stderr)
        raise SystemExit(2)
    if args.arrival == "poisson":
        if not rates:
            print("--arrival poisson requires --rate (requests/second)", file=sys.stderr)
            raise SystemExit(2)
        if any(r <= 0 for r in rates):
            print("--rate must be above zero, got %r" % (args.rate,), file=sys.stderr)
            raise SystemExit(2)
        if args.mode != "concurrency":
            # The alternative was to nest the rate loop inside the length loop, which would run a
            # sweep nobody asked for on a box that serves production. Refused instead, because the
            # third option is what used to happen: --rate 2,4,8 --mode prefill declared a
            # three-rate sweep in the document and ran rates[0] at every level.
            print("--arrival poisson is implemented for --mode concurrency only. A rate sweep and "
                  "a length sweep are two axes and this probe runs one of them at a time: run the "
                  "length sweep closed loop, or run --mode concurrency once per rate.",
                  file=sys.stderr)
            raise SystemExit(2)
    elif rates:
        print("--rate is ignored in closed-loop mode; pass --arrival poisson to use it",
              file=sys.stderr)
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
            "badly the count divides, so the error appears at some levels and not others and "
            "reads as scatter. Each level records requests_attempted, sample_count and "
            "whole_waves so the adjustment is auditable. This applies to closed-loop levels only: "
            "under Poisson arrivals there is no wave to round to, and the level runs for "
            "approximately requests/rate seconds."),
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
    # The arrival model is declared at the top of the document, not only inside each level: a
    # reader (and verify.py's check_load_shape) has to be able to see how the requests arrived
    # before reading a single percentile.
    doc.update(arrival_declaration(args))

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
    # Levels that could not be run at all. Present even when empty, so a reader can see that the
    # sweep is whole rather than having to infer it from the level count.
    doc["level_failures"] = []

    # The filename is decided BEFORE the sweep, because the document is written after every level.
    # A crash in level N used to destroy levels 1..N-1: the whole document was written once at the
    # end, so a RuntimeError("can't start new thread") at the top rate left the run directory empty.
    # concurrency mode with no label keeps the canonical name the report looks for first. A Poisson
    # run must NOT land on that name: the closed-loop and open-loop sweeps measure different
    # things, and silently overwriting one with the other would put open-loop latencies into a
    # table the report labels closed-loop.
    if args.label:
        name = "serve_bench_%s.json" % args.label
    elif args.arrival == "poisson":
        name = ("serve_bench_poisson.json" if args.mode == "concurrency"
                else "serve_bench_%s_poisson.json" % args.mode)
    elif args.mode != "concurrency":
        name = "serve_bench_%s.json" % args.mode
    else:
        name = "serve_bench.json"
    path = os.path.join(run_dir, name)

    def write_doc():
        """Flush the document as it stands. newline="\\n" because this file is read on Linux."""
        doc["finished_at_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with io.open(path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(doc, f, indent=2)

    level_no = [0]

    def do_level(**kw):
        """Run one level, keep it, and write the document out.

        Every level goes through here so that (a) the level index that derives its arrival seed is
        assigned in one place, and (b) a level that fails outright costs only itself. Returns None
        if the level failed, and the caller skips its printed row.
        """
        idx = level_no[0]
        level_no[0] += 1
        try:
            lvl = run_level(args, level_index=idx, **kw)
        except Exception as exc:  # noqa: BLE001
            doc["level_failures"].append({
                "level_index": idx,
                "requested": {k: v for k, v in kw.items() if k in ("concurrency", "total_requests",
                                                                   "in_tok", "out_tok", "rate")},
                "error": repr(exc),
            })
            write_doc()
            print("   ! level %d failed: %r. The levels before it are already on disk."
                  % (idx, exc))
            return None
        doc["levels"].append(lvl)
        write_doc()
        return lvl

    # Under Poisson arrivals concurrency is an OUTCOME, not an input: nothing caps the number of
    # requests in flight. Those levels record concurrency as null and report peak_inflight instead,
    # so no reader can mistake a nominal pool size for an enforced one. The length-sweep modes
    # below are closed loop only (--arrival poisson with them exits 2 above), so they pass their
    # concurrency straight through.

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
            level = do_level(concurrency=c, total_requests=level_requests(args, c, max(k, c)),
                             in_tok=n_in, out_tok=args.output_tokens)
            if level is None:
                continue
            level["mix_weight"] = k / float(len(lengths))
            write_doc()
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
            level = do_level(concurrency=c, total_requests=level_requests(args, c, args.requests),
                             in_tok=n_in, out_tok=1)
            if level is None:
                continue
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
            level = do_level(concurrency=c, total_requests=level_requests(args, c, args.requests),
                             in_tok=args.input_tokens, out_tok=n_out)
            if level is None:
                continue
            print("%-9d %-6d %-11.1f %-11.2f %-11.2f %-11.4f %-6d" % (
                n_out, c,
                level["output_tokens_per_s"] or 0,
                level["itl_ms"]["p50"] or 0, level["itl_ms"]["p95"] or 0,
                level["ttft_s"]["p50"] or 0, level["error_count"]))
            for e in level["errors"]:
                print("   ! " + e)

    elif args.arrival == "poisson":
        # The sweep axis is the OFFERED RATE, not the concurrency: an open-loop generator does not
        # have a concurrency to sweep. The two columns to read together are TARGET and ACHIEVED:
        # the first level where achieved falls below target is where the machine stopped keeping up,
        # and it is precisely the point a closed-loop sweep cannot locate because there the offered
        # load is defined by what the machine already finished.
        header = ("%-8s %-9s %-9s %-7s %-9s %-9s %-9s %-10s %-6s" %
                  ("TARGET", "ACHIEVED", "OUTTOK/S", "PEAKQ", "TTFTp50", "TTFTp95", "ITLp50ms",
                   "E2Ep95", "ERR"))
        print(header)
        print("-" * len(header))
        for r in rates:
            n = level_requests(args, None, args.requests)
            level = do_level(concurrency=None, total_requests=n, rate=r)
            if level is None:
                continue
            arr = level["arrival"]
            print("%-8.2f %-9.2f %-9.1f %-7d %-9.3f %-9.3f %-9.1f %-10.3f %-6d" % (
                r,
                arr["achieved_rate_req_s"] or 0,
                level["output_tokens_per_s"] or 0,
                level["peak_inflight"],
                level["ttft_s"]["p50"] or 0,
                level["ttft_s"]["p95"] or 0,
                level["itl_ms"]["p50"] or 0,
                level["e2e_s"]["p95"] or 0,
                level["error_count"],
            ))
            q = arr["queue_depth"]
            # The two things that VOID a level print first, and they print before any verdict.
            # A truncated level used to get its engine verdict printed ABOVE the warning that the
            # level had stopped early, so a reader met the conclusion before the reason to
            # disbelieve it.
            if arr["generator_kept_up"] is False:
                print("   ! the GENERATOR missed its own schedule (p95 deviation %.1f ms against a "
                      "%.1f ms budget, mean gap %.1f ms), so this level is not the Poisson stream "
                      "it names and no engine verdict is drawn from it."
                      % (arr["generator_fidelity"]["p95_abs_deviation_ms"] or 0,
                         arr["generator_fidelity"]["budget_ms"] or 0,
                         arr["generator_fidelity"]["mean_inter_arrival_ms"] or 0))
            if arr["truncated_by_harness_limit"]:
                print("   ! the HARNESS hit a ceiling after %d of %d arrivals (%s); this level is "
                      "truncated and its rate is not a rate the machine sustained."
                      % (arr["requests_dispatched"], n,
                         (arr["dispatch_failures"] or [{}])[0].get("error", "")))
            grew = arr["latency_grew_over_the_level"]
            if grew is None:
                # A verdict of None must never render as silence. It used to: main() tested
                # `elif arr["fell_behind"]:` and a None printed NOTHING AT ALL, so the loudest
                # possible level (four of ninety requests completed, true p95 wait about 30 s)
                # printed an e2e p95 of 1.82 s and no warning of any kind.
                print("   ? NOT JUDGED: %s" % arr["latency_grew_over_the_level_basis"])
                print("     Treat this level's latency percentiles as a lower bound. They describe "
                      "the requests that came back.")
            elif grew:
                print("   ! LATENCY GREW over this level: %s"
                      % arr["latency_grew_over_the_level_basis"])
                print("     in flight at the last arrival %s, against a Little's law baseline of "
                      "%.1f (offered rate x mean latency), then %.1fs of drain."
                      % (q["inflight_at_last_arrival"], q["littles_law_inflight"] or 0,
                         q["drain_s"] or 0))
                if arr["engine_did_not_keep_up"] is True:
                    print("   ! the ENGINE did not keep up at %g req/s: %s"
                          % (r, arr["engine_did_not_keep_up_basis"]))
                else:
                    print("   ? the engine's own queue does NOT confirm this as a capacity limit: "
                          "%s" % arr["engine_did_not_keep_up_basis"])
            if level["requests_unaccounted"]:
                print("   ! %d dispatched request(s) produced no outcome at all. The level's "
                      "accounting is broken and its rates are not trustworthy."
                      % level["requests_unaccounted"])
            for e in level["errors"]:
                print("   ! " + e)
            for e in level["harness_errors"]:
                print("   ~ harness: " + e)

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
            level = do_level(concurrency=c, total_requests=n)
            if level is None:
                continue
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

    # The declared target rates come from the rates the LEVELS were actually given, never from the
    # parsed CLI string. The string outran the run once already: --rate 2,4,8 --mode prefill
    # declared a three-rate sweep in the document while every level ran rates[0].
    level_rates = [l["arrival"].get("target_rate_req_s") for l in doc["levels"]]
    doc["arrival"]["target_rate_req_s"] = ([r for r in level_rates if r is not None] or None)
    # Per-level seeds should make the draw error differ level by level. Identical values are the
    # signature of one seed shared across the sweep, which correlates every level's error in the
    # same direction and reads as a systematic engine effect.
    draws = [l["arrival"].get("schedule_vs_target_pct") for l in doc["levels"]]
    draws = [d for d in draws if d is not None]
    if len(draws) > 1 and max(draws) - min(draws) < 1e-9:
        doc["arrival"]["schedule_draw_warning"] = (
            "every level realized the same schedule_vs_target_pct to within 1e-9. The levels are "
            "not independent draws, so a single unlucky sample has biased the whole sweep in one "
            "direction.")
    write_doc()
    print("\nwrote " + path)
    # Emit the document too: the orchestrator captures stdout JSON.
    print(json.dumps(doc))


if __name__ == "__main__":
    main()
