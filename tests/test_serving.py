#!/usr/bin/env python3
"""Tests for the serving load generator: wave rounding and the arrival process.

NOTHING HERE TOUCHES A NETWORK. Not the loopback interface, and certainly not the box under test,
which serves live traffic. The engine is replaced by FakeEngine (a sleep, optionally behind a hard
capacity limit) and the HTTP client by FakeClient, so what runs is the real dispatch, the real
timing accounting and the real document assembly, with the socket removed. That is deliberate and
not a compromise: pointing this at a live engine to check that a mean inter-arrival is 1/rate would
prove something about the network and nothing about the schedule.

The properties under test are the ones the report's credibility rests on:

  * whole_waves rounds up, so no level runs a partial final wave at a lower concurrency than it
    claims to measure.
  * Poisson inter-arrivals really are exponential with mean 1/rate, mean AND spread, because a
    fixed-interval generator would pass a mean-only test while producing none of the burstiness
    that builds a queue.
  * A seed reproduces a schedule exactly, so two runs are comparable and a rate sweep's levels
    differ by rate and not by luck.
  * Open loop dispatches on schedule REGARDLESS of completions. This is the whole point, and it is
    tested against a fake engine slow enough that a closed-loop generator could not possibly have
    issued the same requests in the same window.
  * Closed loop is untouched: in-flight is still pinned at the concurrency, and every key the
    previous version emitted is still emitted.
  * The arrival model reaches the written document, in the key gpubench/verify.py actually reads.
    Asserted by running verify.check_load_shape over the document's own declaration, not by
    matching a string against a copy of the vocabulary.

Run:  python -m tests.test_serving      (from the repo root)
"""
import io
import json
import math
import os
import random
import statistics
import sys
import tempfile
import threading
import time
import types
import unittest
from contextlib import contextmanager, redirect_stdout

sys.path.insert(0, ".")
from gpubench import verify as V  # noqa: E402
from gpubench.probes import serving as S  # noqa: E402


# --------------------------------------------------------------------------------------
# fakes


class FakeEngine(object):
    """A stand-in engine: a fixed service time, optionally behind a hard concurrency limit.

    `capacity` is what makes the open-loop test bite. With no limit the fake has infinite
    parallelism and always keeps up, which is not a machine. With capacity=k it can complete at
    most k/service_s requests per second, so offering more than that has to show up as an achieved
    rate below target and a growing backlog, or the accounting is wrong.
    """

    def __init__(self, service_s=0.02, capacity=None):
        self.service_s = service_s
        self.gate = threading.Semaphore(capacity) if capacity else None
        self.lock = threading.Lock()
        self.inflight = 0
        self.peak_inflight = 0
        self.arrivals = []          # (salt, seconds since the engine was created)
        self.completions = 0
        self.t0 = time.perf_counter()

    def one_request(self, client, args, salt, in_tok=None, out_tok=None):
        with self.lock:
            self.inflight += 1
            self.peak_inflight = max(self.peak_inflight, self.inflight)
            self.arrivals.append((salt, time.perf_counter() - self.t0))
        try:
            if self.gate is not None:
                with self.gate:
                    time.sleep(self.service_s)
            else:
                time.sleep(self.service_s)
        finally:
            with self.lock:
                self.inflight -= 1
                self.completions += 1
        n_out = 4 if out_tok is None else max(1, out_tok)
        return {
            "ttft_s": self.service_s / 2.0,
            "e2e_s": self.service_s,
            "itls": [self.service_s / (2.0 * n_out)] * max(1, n_out - 1),
            "completion_tokens": n_out,
            "prompt_tokens": 8 if in_tok is None else in_tok,
        }


class FakeClient(object):
    """Opens nothing. /models answers so main() gets past its reachability check; /metrics 404s,
    which is the path scrape_metrics already has to survive on engines without a metrics port."""

    def __init__(self, base_url, timeout):
        self.base_url = base_url
        self.timeout = timeout
        self.closed = False

    def get(self, path, root=False):
        if path.endswith("/models"):
            return 200, json.dumps({"data": [{"id": "fake-model"}]})
        return 404, ""

    def close(self):
        self.closed = True


def fake_args(**over):
    """The attribute surface run_level reads. A Namespace, because that is what argparse hands it."""
    base = dict(base_url="http://127.0.0.1:1/v1", model="fake-model", api_key=None,
                endpoint="completions", input_tokens=64, output_tokens=8, timeout=5.0,
                arrival="closed", rate=None, arrival_seed=S.DEFAULT_ARRIVAL_SEED,
                queue_sample_interval=0.02, requests=8)
    base.update(over)
    return types.SimpleNamespace(**base)


def run_level_against(engine, args, concurrency, n, **kw):
    """Drive the real run_level with the fake engine wired in through its injection points."""
    return S.run_level(args, concurrency, n,
                       send=lambda client, salt: engine.one_request(client, args, salt,
                                                                    args.input_tokens,
                                                                    args.output_tokens),
                       make_client=lambda: FakeClient(args.base_url, args.timeout), **kw)


def run_main(argv, engine=None):
    """End-to-end through the real main(): the real parser, the real document assembly, the real
    per-level writes and the real file naming. The socket is the only thing replaced.

    Returns ({filename: document}, captured stdout). Module level rather than a test method,
    because several classes need it: the printed lines and the incremental writes are behaviour in
    their own right and cannot be checked from run_level alone.
    """
    engine = engine or FakeEngine(service_s=0.004, capacity=32)
    run_dir = tempfile.mkdtemp(prefix="gpubench-test-")
    saved = (S.Client, S.one_request, sys.argv, os.environ.get("GPUBENCH_RUN_DIR"))
    S.Client = FakeClient
    S.one_request = engine.one_request
    sys.argv = ["serving.py"] + argv
    os.environ["GPUBENCH_RUN_DIR"] = run_dir
    try:
        buf = io.StringIO()
        with redirect_stdout(buf):
            S.main()
    finally:
        S.Client, S.one_request, sys.argv = saved[0], saved[1], saved[2]
        if saved[3] is None:
            os.environ.pop("GPUBENCH_RUN_DIR", None)
        else:
            os.environ["GPUBENCH_RUN_DIR"] = saved[3]
    docs = {}
    for name in sorted(os.listdir(run_dir)):
        with open(os.path.join(run_dir, name), encoding="utf-8") as fh:
            docs[name] = json.loads(fh.read())
    return docs, buf.getvalue()


# --------------------------------------------------------------------------------------
# whole_waves


class TestWholeWaves(unittest.TestCase):
    def test_exact_multiple_is_left_alone(self):
        self.assertEqual(S.whole_waves(8, 32), (32, 4))
        self.assertEqual(S.whole_waves(8, 8), (8, 1))

    def test_partial_wave_is_rounded_up_never_down(self):
        """Rounding DOWN would drop requests; leaving it alone runs a final wave at a lower
        concurrency than the level claims, which depresses that level's throughput by an amount
        depending on how badly the count divides, so it reads as scatter at one level rather than
        as a systematic fault."""
        self.assertEqual(S.whole_waves(8, 9), (16, 2))
        self.assertEqual(S.whole_waves(8, 15), (16, 2))
        self.assertEqual(S.whole_waves(5, 12), (15, 3))
        self.assertEqual(S.whole_waves(32, 33), (64, 2))

    def test_request_count_below_concurrency_is_raised_to_one_full_wave(self):
        """Asking for 8 requests at concurrency 32 cannot measure concurrency 32."""
        self.assertEqual(S.whole_waves(32, 8), (32, 1))
        self.assertEqual(S.whole_waves(64, 0), (64, 1))

    def test_degenerate_concurrency_does_not_divide_by_zero(self):
        self.assertEqual(S.whole_waves(0, 5), (5, 5))
        self.assertEqual(S.whole_waves(-4, 5), (5, 5))

    def test_invariants_hold_across_the_grid(self):
        for c in range(1, 40):
            for requested in range(0, 80):
                eff, waves = S.whole_waves(c, requested)
                self.assertEqual(eff % c, 0, "level would run a partial wave")
                self.assertEqual(eff, waves * c)
                self.assertGreaterEqual(eff, requested)
                self.assertGreaterEqual(eff, c)
                self.assertLess(eff - max(requested, c), c, "rounded up by more than one wave")

    def test_open_loop_levels_are_not_wave_rounded(self):
        """Waves are a closed-loop artefact of a pool refilling in lockstep. Rounding a Poisson
        level up to a 'whole wave' would invent a wave that does not exist."""
        closed = fake_args(arrival="closed")
        poisson = fake_args(arrival="poisson", rate="10")
        self.assertEqual(S.level_requests(closed, 8, 9), 16)
        self.assertEqual(S.level_requests(poisson, 8, 9), 9)


# --------------------------------------------------------------------------------------
# the Poisson schedule


class TestPoissonSchedule(unittest.TestCase):
    def test_mean_inter_arrival_approaches_one_over_rate(self):
        for rate in (5.0, 37.0, 250.0):
            gaps = S.poisson_interarrivals(rate, 20000, seed=11)
            mean = statistics.fmean(gaps)
            self.assertAlmostEqual(mean, 1.0 / rate, delta=(1.0 / rate) * 0.03,
                                   msg="mean gap %.6g is not 1/%g within 3%%" % (mean, rate))

    def test_mean_converges_as_the_sample_grows(self):
        """A generator with the right mean by accident at one n would not tighten with n."""
        rate = 20.0
        errs = []
        for n in (200, 2000, 20000):
            gaps = S.poisson_interarrivals(rate, n, seed=7)
            errs.append(abs(statistics.fmean(gaps) - 1.0 / rate) * rate)
        self.assertLess(errs[-1], errs[0], "error should shrink as n grows: %r" % (errs,))
        self.assertLess(errs[-1], 0.03)

    def test_spread_is_exponential_not_fixed_interval(self):
        """The mean alone does not distinguish a Poisson process from a metronome, and a metronome
        does not build the queue this mode exists to build. For an exponential the standard
        deviation equals the mean (coefficient of variation 1), and about 63% of gaps fall below
        the mean, and both are asserted, because either alone is weak."""
        gaps = S.poisson_interarrivals(50.0, 40000, seed=3)
        mean = statistics.fmean(gaps)
        cv = statistics.pstdev(gaps) / mean
        self.assertAlmostEqual(cv, 1.0, delta=0.05, msg="coefficient of variation %.3f" % cv)
        below = sum(1 for g in gaps if g < mean) / float(len(gaps))
        self.assertAlmostEqual(below, 1.0 - 1.0 / 2.718281828, delta=0.02)
        self.assertGreater(max(gaps) / mean, 4.0, "no long gaps at all is not a Poisson process")

    def test_a_seed_reproduces_a_schedule_exactly(self):
        a = S.poisson_offsets(7.5, 500, seed=4242)
        b = S.poisson_offsets(7.5, 500, seed=4242)
        self.assertEqual(a, b, "same seed must replay the identical schedule, bit for bit")
        self.assertNotEqual(a, S.poisson_offsets(7.5, 500, seed=4243))

    def test_the_seed_is_private_to_the_generator(self):
        """Seeding the global random module would make the schedule depend on whatever else in the
        process had drawn from it, which is the difference between reproducible and nearly so."""
        import random as _r
        _r.seed(1)
        first = S.poisson_offsets(9.0, 50, seed=99)
        _r.random(), _r.random(), _r.random()
        self.assertEqual(first, S.poisson_offsets(9.0, 50, seed=99))

    def test_offsets_are_increasing_and_start_after_zero(self):
        offs = S.poisson_offsets(12.0, 200, seed=1)
        self.assertGreater(offs[0], 0.0, "a first arrival at t=0 is the burst artefact this avoids")
        for a, b in zip(offs, offs[1:]):
            self.assertGreater(b, a)

    def test_offsets_are_the_running_sum_of_the_gaps(self):
        gaps = S.poisson_interarrivals(3.0, 40, seed=8)
        offs = S.poisson_offsets(3.0, 40, seed=8)
        self.assertAlmostEqual(offs[-1], sum(gaps), places=12)
        self.assertAlmostEqual(offs[0], gaps[0], places=12)

    def test_bad_rate_is_refused_rather_than_silently_defaulted(self):
        for bad in (0, 0.0, -1.0):
            with self.assertRaises(ValueError):
                S.poisson_interarrivals(bad, 10, seed=1)
        self.assertEqual(S.poisson_offsets(10.0, 0, seed=1), [])

    def test_no_gap_is_ever_zero_or_negative(self):
        """-ln(1-U) with U in [0,1) can never take log(0) and can never go negative. Asserted
        because a zero gap would dispatch two arrivals simultaneously and a negative one would
        wind the clock back."""
        for seed in range(12):
            self.assertTrue(all(g > 0.0 for g in S.poisson_interarrivals(1000.0, 3000, seed=seed)))


# --------------------------------------------------------------------------------------
# closed loop is unchanged


LEGACY_LEVEL_KEYS = [
    "concurrency", "input_tokens_requested", "output_tokens_requested", "prefill_tokens_per_s",
    "requests_attempted", "requests_ok", "sample_count", "duration_s", "waves", "whole_waves",
    "errors", "error_count", "wall_s", "requests_per_s", "output_tokens", "input_tokens",
    "output_tokens_per_s", "total_tokens_per_s", "ttft_s", "itl_ms", "e2e_s",
    "per_request_output_tokens_per_s", "server_metrics_delta",
]


class TestClosedLoopUnchanged(unittest.TestCase):
    def test_default_arrival_is_closed(self):
        self.assertEqual(getattr(fake_args(), "arrival"), "closed")
        self.assertEqual(S.ARRIVAL_MODELS["closed"], "closed_loop")

    def test_in_flight_is_pinned_at_the_concurrency(self):
        """The defining property of a closed-loop harness, and the reason its percentiles are
        optimistic: it will not offer the server more work than it is currently finishing."""
        engine = FakeEngine(service_s=0.05)
        args = fake_args()
        level = run_level_against(engine, args, 4, 12)
        self.assertEqual(engine.peak_inflight, 4)
        self.assertEqual(level["peak_inflight"], 4)
        self.assertEqual(level["requests_ok"], 12)
        self.assertEqual(engine.completions, 12)

    def test_the_pool_waits_for_completions_before_reissuing(self):
        """Three waves of four must arrive in three separated groups. If the pool ever ran ahead it
        would not be a closed loop any more."""
        engine = FakeEngine(service_s=0.08)
        level = run_level_against(engine, fake_args(), 4, 12)
        stamps = sorted(t for _salt, t in engine.arrivals)
        self.assertEqual(len(stamps), 12)
        # the 5th arrival cannot precede the 1st completion
        self.assertGreater(stamps[4], 0.07)
        self.assertGreater(stamps[8], 0.15)
        self.assertEqual(level["waves"], 3)
        self.assertTrue(level["whole_waves"])

    def test_every_legacy_key_is_still_emitted(self):
        level = run_level_against(FakeEngine(0.01), fake_args(), 2, 4)
        for key in LEGACY_LEVEL_KEYS:
            self.assertIn(key, level, "closed-loop levels lost the key %r" % key)
        self.assertEqual(level["concurrency"], 2)
        self.assertEqual(level["requests_attempted"], 4)
        self.assertEqual(level["waves"], 2)
        self.assertIs(level["whole_waves"], True)

    def test_closed_levels_declare_the_model_and_the_achieved_rate(self):
        level = run_level_against(FakeEngine(0.01), fake_args(), 2, 8)
        arr = level["arrival"]
        self.assertEqual(arr["model"], "closed_loop")
        self.assertIsNone(arr["target_rate_req_s"], "closed loop has no target rate to declare")
        self.assertIsNone(arr["seed"])
        self.assertAlmostEqual(arr["achieved_rate_req_s"], level["requests_per_s"], places=9)
        self.assertFalse(arr["queue_depth"]["sampled"],
                         "sampling a depth pinned at the concurrency measures the harness")

    def test_errors_are_counted_not_swallowed(self):
        """A request that raised must not be counted as a sample. It was this accounting that had
        to survive the refactor: both dispatchers now book outcomes through one path."""
        def boom(client, salt):
            raise RuntimeError("HTTP 503")
        level = S.run_level(fake_args(), 2, 4, send=boom,
                            make_client=lambda: FakeClient("http://x/v1", 1.0))
        self.assertEqual(level["error_count"], 4)
        self.assertEqual(level["requests_ok"], 0)
        self.assertEqual(level["sample_count"], 0)
        self.assertIsNone(level["ttft_s"]["p95"])


# --------------------------------------------------------------------------------------
# open loop


class TestOpenLoopDispatch(unittest.TestCase):
    def test_dispatch_does_not_wait_for_completions(self):
        """The property the whole mode exists for. Service time is 1 s and the schedule spans about
        0.4 s, so a closed-loop generator could not have issued more than a handful of requests in
        that window; the open-loop one must have issued all of them, with nothing yet completed."""
        engine = FakeEngine(service_s=1.0)
        args = fake_args(arrival="poisson", rate="50", requests=20, arrival_seed=5)
        level = run_level_against(engine, args, None, 20)
        arr = level["arrival"]
        self.assertEqual(arr["requests_dispatched"], 20)
        self.assertLess(arr["dispatch_span_s"], 0.9,
                        "the generator waited on the server: span %.3fs" % arr["dispatch_span_s"])
        self.assertGreater(engine.peak_inflight, 10,
                           "20 arrivals inside one 1s service time must overlap")
        self.assertEqual(level["peak_inflight"], engine.peak_inflight)
        self.assertIsNone(level["concurrency"], "open loop has no concurrency to declare")
        self.assertIsNone(level["waves"])
        self.assertIsNone(level["whole_waves"])

    def test_schedule_is_followed_within_a_small_lateness(self):
        engine = FakeEngine(service_s=0.01)
        args = fake_args(arrival="poisson", rate="40", requests=60, arrival_seed=17)
        level = run_level_against(engine, args, None, 60)
        arr = level["arrival"]
        intended = S.poisson_offsets(40.0, 60, seed=S.level_seed(17, 0))
        self.assertAlmostEqual(arr["schedule_span_s"], intended[-1], places=9)
        # absolute deadlines mean lateness cannot accumulate: a late dispatch is followed by an
        # early one, so the span tracks the schedule rather than drifting past it.
        self.assertLess(abs(arr["dispatch_span_s"] - intended[-1]), 0.35,
                        "dispatch span %.3f vs schedule %.3f" % (arr["dispatch_span_s"], intended[-1]))
        self.assertAlmostEqual(arr["achieved_arrival_rate_req_s"], 40.0, delta=12.0)
        self.assertLess(arr["dispatch_lateness_ms"]["p95"], 60.0)
        # Fidelity is per arrival, and it has to be met per arrival: at a 25 ms mean gap the budget
        # is 2.5 ms, which a sleep whose overshoot is under a millisecond can hold.
        self.assertTrue(arr["generator_kept_up"],
                        "p95 deviation %.2f ms against a %.2f ms budget"
                        % (arr["generator_fidelity"]["p95_abs_deviation_ms"],
                           arr["generator_fidelity"]["budget_ms"]))
        self.assertLess(arr["generator_fidelity"]["p95_abs_deviation_ms"],
                        arr["generator_fidelity"]["budget_ms"])

    def test_a_finite_draw_realizes_a_rate_near_but_not_at_the_target(self):
        """Sampling noise, not a defect: the realized rate of n exponential gaps is about 1/sqrt(n)
        away from the nominal one. It is recorded rather than smoothed away, because a reader
        comparing achieved against target needs to see how much of the gap was the draw."""
        for n, tol in ((60, 0.40), (4000, 0.08)):
            offs = S.poisson_offsets(200.0, n, seed=99)
            realized = n / offs[-1]
            self.assertAlmostEqual(realized / 200.0, 1.0, delta=tol)
        engine = FakeEngine(service_s=0.001, capacity=64)
        args = fake_args(arrival="poisson", rate="200", requests=60, arrival_seed=99)
        arr = run_level_against(engine, args, None, 60)["arrival"]
        self.assertAlmostEqual(arr["schedule_realized_rate_req_s"],
                               60 / S.poisson_offsets(200.0, 60,
                                                      seed=S.level_seed(99, 0))[-1], places=6)
        self.assertAlmostEqual(arr["schedule_vs_target_pct"],
                               (arr["schedule_realized_rate_req_s"] - 200.0) / 2.0, places=6)

    def test_target_rate_and_seed_are_recorded(self):
        engine = FakeEngine(service_s=0.005)
        args = fake_args(arrival="poisson", rate="30", requests=15, arrival_seed=808)
        level = run_level_against(engine, args, None, 15)
        arr = level["arrival"]
        self.assertEqual(arr["model"], "open_loop_poisson")
        self.assertEqual(arr["target_rate_req_s"], 30.0)
        # The base seed is what the user typed; the seed actually drawn from is derived from it and
        # the level index, and BOTH are in the level so the schedule stays reproducible.
        self.assertEqual(arr["seed_base"], 808)
        self.assertEqual(arr["level_index"], 0)
        self.assertEqual(arr["seed"], S.level_seed(808, 0))
        self.assertIsNotNone(arr["achieved_rate_req_s"])

    def test_an_explicit_rate_overrides_the_sweep_string(self):
        """A rate sweep drives one level at a time without rewriting args, so doc['config'] keeps
        the sweep it was asked for."""
        engine = FakeEngine(service_s=0.002)
        args = fake_args(arrival="poisson", rate="10,20,40", requests=10)
        level = run_level_against(engine, args, None, 10, rate=20.0)
        self.assertEqual(level["arrival"]["target_rate_req_s"], 20.0)

    def test_a_server_that_cannot_keep_up_shows_as_achieved_below_target(self):
        """The signal closed loop hides. The fake serves at most 1 request per 50 ms, i.e. 20/s;
        offered 40/s, the achieved rate has to fall well short and the queue has to grow.

        Offered 40 rather than the 100 an earlier version used, because 100 arrivals a second is
        past the DISPATCHER's own fidelity budget on a Windows host (a 10 ms mean gap leaves 1 ms,
        and thread creation plus the lock costs about that). A level the generator cannot issue
        faithfully cannot be used to judge the engine, which is the point of the gating below.
        """
        engine = FakeEngine(service_s=0.05, capacity=1)
        args = fake_args(arrival="poisson", rate="40", requests=60, arrival_seed=21,
                         queue_sample_interval=0.02)
        level = run_level_against(engine, args, None, 60)
        arr = level["arrival"]
        self.assertLess(arr["achieved_rate_req_s"], 30.0,
                        "a 20 req/s engine cannot achieve %.1f" % arr["achieved_rate_req_s"])
        self.assertGreater(arr["rate_deficit_pct"], 25.0)
        self.assertTrue(arr["fell_behind"], arr["fell_behind_basis"])
        self.assertGreater(arr["completion_deficit_vs_offered_pct"], 25.0)
        # the generator kept its side of the bargain even while the engine did not
        self.assertGreater(arr["achieved_arrival_rate_req_s"], 30.0)
        self.assertTrue(arr["generator_kept_up"],
                        "p95 deviation %.2f ms against a %.2f ms budget"
                        % (arr["generator_fidelity"]["p95_abs_deviation_ms"],
                           arr["generator_fidelity"]["budget_ms"]))
        self.assertEqual(arr["requests_dispatched"], 60)

    def test_a_shortfall_is_attributed_to_the_generator_or_the_engine_not_both(self):
        """The misattribution this guards against was live for one commit: fell_behind was judged
        against the TARGET rate, so a level where the generator itself was a few percent late
        reported that the engine had not kept up. The engine is only answerable for the load that
        was actually offered to it."""
        engine = FakeEngine(service_s=0.002, capacity=64)
        args = fake_args(arrival="poisson", rate="60", requests=60, arrival_seed=12)
        arr = run_level_against(engine, args, None, 60)["arrival"]
        offered, done = arr["achieved_arrival_rate_req_s"], arr["achieved_rate_req_s"]
        self.assertEqual(arr["fell_behind"], done < offered * 0.95,
                         "fell_behind must be judged against the offered rate, not the target")
        self.assertEqual(arr["generator_kept_up"],
                         arr["dispatch_span_s"] <= arr["schedule_span_s"] * 1.02 + 0.01,
                         "generator fidelity is span against span, not rate against nominal")
        self.assertAlmostEqual(arr["completion_deficit_vs_offered_pct"],
                               (offered - done) / offered * 100.0, places=9)
        # an engine with 30x the needed capacity is not the thing that fell behind
        self.assertFalse(arr["fell_behind"])

    def test_queue_depth_is_traced_over_time_and_shows_the_build_up(self):
        engine = FakeEngine(service_s=0.025, capacity=1)
        args = fake_args(arrival="poisson", rate="100", requests=60, arrival_seed=21,
                         queue_sample_interval=0.02)
        level = run_level_against(engine, args, None, 60)
        q = level["arrival"]["queue_depth"]
        self.assertTrue(q["samples"], "no in-flight trace recorded")
        self.assertTrue(all(len(s) == 2 for s in q["samples"]))
        times = [t for t, _d in q["samples"]]
        self.assertEqual(times, sorted(times))
        depths = [d for _t, d in q["samples"]]
        self.assertGreaterEqual(q["max"], max(depths))
        self.assertGreater(q["max"], 5, "an overloaded engine must show a backlog")
        # the queue is deepest in the middle or at the end of the level, never at the start
        self.assertGreater(max(depths[len(depths) // 3:]), depths[0])
        self.assertFalse(q["truncated"])
        # the figure that survives the drain: the depth standing when arrivals stopped
        self.assertGreater(q["inflight_at_last_arrival"], 5)
        self.assertGreater(q["drain_s"], 0.1, "an overloaded engine has a queue left to drain")

    def test_the_last_sample_is_not_mistaken_for_the_queue(self):
        """After the last arrival the queue drains to zero whatever happened, so a depth read at
        the end of the level always looks healthy. inflight_at_last_arrival is the honest one, and
        this test exists because reporting the other would have hidden the overload entirely."""
        engine = FakeEngine(service_s=0.025, capacity=1)
        args = fake_args(arrival="poisson", rate="100", requests=60, arrival_seed=21,
                         queue_sample_interval=0.02)
        q = run_level_against(engine, args, None, 60)["arrival"]["queue_depth"]
        self.assertGreater(q["inflight_at_last_arrival"], q["last_sample"])

    def test_a_keeping_up_engine_does_not_report_falling_behind(self):
        """The negative control for the check above: with capacity to spare, achieved tracks target
        and fell_behind is false. Without this, 'fell_behind' could just be always true.

        The service time here is 5 ms, which is about 400x faster than the engine this tool was
        written for. That is exactly why this control passed while the verdict was broken, so it is
        no longer the only negative control: see TestFellBehindIsNotDrainBias.
        """
        engine = FakeEngine(service_s=0.005, capacity=64)
        args = fake_args(arrival="poisson", rate="40", requests=40, arrival_seed=6)
        level = run_level_against(engine, args, None, 40)
        arr = level["arrival"]
        self.assertAlmostEqual(arr["achieved_rate_req_s"], 40.0, delta=14.0)
        self.assertIs(arr["fell_behind"], False,
                      "achieved %.2f vs target 40" % arr["achieved_rate_req_s"])
        self.assertLess(arr["queue_depth"]["max"], 12)

    def test_poisson_without_a_rate_is_refused(self):
        args = fake_args(arrival="poisson", rate=None)
        with self.assertRaises(ValueError):
            run_level_against(FakeEngine(0.001), args, None, 4)

    def test_unknown_arrival_model_is_refused(self):
        args = fake_args(arrival="beta")
        with self.assertRaises(ValueError):
            run_level_against(FakeEngine(0.001), args, 1, 1)


# --------------------------------------------------------------------------------------
# the engine verdict: queue growth, not the drain


class TestFellBehindIsNotDrainBias(unittest.TestCase):
    """fell_behind must mean "a queue was growing", not "the wall clock is longer than the
    arrival span".

    The arithmetic that made this necessary: achieved was completions over the WALL CLOCK, which
    runs to the last completion, while offered was dispatched over the ARRIVAL SPAN. The wall clock
    exceeds the span by at least one service time on any engine, so the ratio carried a deficit of
    about service/(span+service) with nothing wrong anywhere. Measured at rate 40, n=60 on an
    unlimited-parallelism fake with zero queueing and zero errors: 0.7% at a 10 ms service time
    (False), 6.0% at 100 ms (TRUE), 24.1% at 500 ms (TRUE), 38.8% at 1 s (TRUE), 55.9% at 2 s
    (TRUE). The box this tool was written for serves at 2-21 s per request, so every honest level
    on it would have reported a false engine failure.
    """

    def _unlimited(self, service_s, rate, n, seed=5):
        engine = FakeEngine(service_s=service_s)          # capacity=None: cannot queue
        args = fake_args(arrival="poisson", rate=str(rate), requests=n, arrival_seed=seed,
                         queue_sample_interval=0.05)
        level = run_level_against(engine, args, None, n)
        self.assertEqual(level["error_count"], 0)
        self.assertEqual(level["requests_ok"], n)
        self.assertEqual(engine.peak_inflight, n if service_s * rate > n else engine.peak_inflight)
        return level["arrival"]

    def test_an_engine_that_cannot_queue_never_reports_falling_behind(self):
        """The audit's own table, verdict column inverted. No row may be True.

        False where the level is judgeable at all. At rate 40 the generator's fidelity budget is
        2.5 ms, which is inside the Windows timer tick, so a level here can legitimately come back
        NOT JUDGED because the generator missed its own schedule. That is the correct answer for
        such a level and it is not an engine verdict, so the assertion is "never blamed" rather
        than "always False", and the False is asserted wherever the generator did keep up.
        """
        for service_s in (0.010, 0.100, 0.500, 1.000, 2.000):
            arr = self._unlimited(service_s, 40, 60)
            self.assertIsNot(arr["fell_behind"], True,
                             "service_s=%.3f: %s" % (service_s, arr["fell_behind_basis"]))
            if arr["generator_kept_up"] is not False:
                self.assertIs(arr["fell_behind"], False,
                              "service_s=%.3f: %s" % (service_s, arr["fell_behind_basis"]))
            # and the drain-inclusive deficit that used to be the verdict is still LARGE, which is
            # the whole point: the number is real, it just is not evidence about the engine.
            if service_s >= 0.100:
                self.assertGreater(arr["completion_deficit_vs_offered_pct"], 5.0,
                                   "the drain bias itself should still be visible")

    def test_the_mandated_control_at_a_service_time_comparable_to_the_span(self):
        """A level of 40 requests at 40 req/s spans about one second. With a service time of 0.2 s
        and then 2.0 s, the second case cannot complete a single request before the last arrival:
        the whole level is ramp-up. Both must still be False, which is why the verdict cannot rest
        on the in-flight trace (that trace is a pure ramp here) and rests on latency instead."""
        for service_s in (0.2, 2.0):
            arr = self._unlimited(service_s, 40, 40)
            self.assertIs(arr["fell_behind"], False,
                          "service_s=%.1f: %s" % (service_s, arr["fell_behind_basis"]))
            self.assertGreater(arr["queue_growth"]["inflight_slope_req_per_s"], 0.0,
                               "the in-flight trace really is climbing here, which is exactly why "
                               "it is a diagnostic and not the verdict")

    def test_the_drain_inclusive_rate_is_named_and_kept(self):
        arr = self._unlimited(1.000, 40, 60)
        self.assertAlmostEqual(arr["completions_per_s_incl_drain"], arr["achieved_rate_req_s"],
                               places=9)
        self.assertIn("WALL CLOCK", arr["completions_per_s_incl_drain_note"])
        self.assertIsNotNone(arr["completions_per_s_in_arrival_window"])

    def test_a_growing_queue_is_still_caught(self):
        """The positive control. A 20 req/s engine offered 40 req/s: the queue grows all level, so
        each later request waits longer than the one before it."""
        engine = FakeEngine(service_s=0.05, capacity=1)
        args = fake_args(arrival="poisson", rate="40", requests=60, arrival_seed=21,
                         queue_sample_interval=0.02)
        arr = run_level_against(engine, args, None, 60)["arrival"]
        g = arr["queue_growth"]
        self.assertIs(arr["fell_behind"], True, arr["fell_behind_basis"])
        self.assertGreater(g["e2e_slope_s_per_s"], 0.0)
        self.assertGreater(g["growth_as_multiple_of_median_e2e"], g["gate_growth_multiple"])
        self.assertGreater(g["slope_t_stat"], g["gate_t_stat"])

    def test_the_verdict_does_not_trust_the_engine_s_own_latency(self):
        """FakeEngine reports e2e_s = service_s whatever it actually did, so in the test above the
        latency it CLAIMED was flat while the queue was growing. The verdict is fitted on the
        harness's own timings, which is the only reason that test can pass."""
        engine = FakeEngine(service_s=0.05, capacity=1)
        args = fake_args(arrival="poisson", rate="40", requests=40, arrival_seed=3,
                         queue_sample_interval=0.02)
        level = run_level_against(engine, args, None, 40)
        self.assertEqual(level["e2e_s"]["p50"], 0.05, "the engine's self-report really is flat")
        self.assertIs(level["arrival"]["fell_behind"], True,
                      level["arrival"]["fell_behind_basis"])
        self.assertIn("timed by the harness", level["arrival"]["queue_growth"]["latency_source"])

    def test_too_few_requests_to_fit_a_trend_says_so_instead_of_guessing(self):
        engine = FakeEngine(service_s=0.005)
        args = fake_args(arrival="poisson", rate="20", requests=3, arrival_seed=1,
                         queue_sample_interval=0.02)
        arr = run_level_against(engine, args, None, 3)["arrival"]
        self.assertIsNone(arr["fell_behind"])
        self.assertIn("too few", arr["fell_behind_basis"])


# --------------------------------------------------------------------------------------
# generator fidelity, judged per arrival


@contextmanager
def patched_sleep(fn):
    """Replace only the probe's view of time.sleep.

    serving.py reaches for time.perf_counter, time.sleep, time.strftime and time.gmtime, so the
    stand-in has to carry all four or main() breaks on the timestamp rather than on the schedule.
    """
    saved = S.time
    S.time = types.SimpleNamespace(perf_counter=time.perf_counter, sleep=fn,
                                   strftime=time.strftime, gmtime=time.gmtime)
    try:
        yield
    finally:
        S.time = saved


class TestGeneratorFidelity(unittest.TestCase):
    def test_a_uniform_overshoot_smaller_than_the_gap_is_visible(self):
        """The lateness used to be the slack computed BEFORE the sleep, so an overshoot smaller
        than the next gap left no trace at all: 25 arrivals each about 12 ms late reported
        count_late=2 and a maximum of 11.42 ms. Measured after the sleep, all 25 are late."""
        engine = FakeEngine(service_s=0.001)
        args = fake_args(arrival="poisson", rate="5", requests=25, arrival_seed=9,
                         queue_sample_interval=0.05)
        with patched_sleep(lambda x: time.sleep(x + 0.012)):
            arr = run_level_against(engine, args, None, 25)["arrival"]
        late = arr["dispatch_lateness_ms"]
        self.assertEqual(late["count_late"], 25)
        self.assertGreater(late["max"], 11.0)
        self.assertLess(late["max"], 25.0)
        # 12 ms against a 200 ms mean gap is 6%, inside the budget, so this level is still usable.
        self.assertTrue(arr["generator_kept_up"])
        self.assertAlmostEqual(arr["generator_fidelity"]["mean_inter_arrival_ms"], 200.0, places=6)

    def test_a_midlevel_stall_fails_fidelity_even_though_the_span_is_intact(self):
        """Deadlines are absolute, so a stall is followed by a catch-up burst and the last dispatch
        lands back on its scheduled offset: a 600 ms stall at rate 30 left schedule_span and
        dispatch_span equal to the millisecond while 17 arrivals fired as one burst. The span check
        cannot see that, so it is kept as its own field and fidelity is judged per arrival."""
        engine = FakeEngine(service_s=0.001)
        args = fake_args(arrival="poisson", rate="30", requests=60, arrival_seed=4,
                         queue_sample_interval=0.05)
        state = {"n": 0}

        def stalling(x):
            state["n"] += 1
            time.sleep(0.6 if state["n"] == 5 else x)

        with patched_sleep(stalling):
            arr = run_level_against(engine, args, None, 60)["arrival"]
        self.assertIs(arr["generator_fidelity"]["schedule_span_honoured"], True,
                      "the span really does survive the stall, which is the trap")
        self.assertIs(arr["generator_kept_up"], False)
        self.assertGreater(arr["generator_fidelity"]["p95_abs_deviation_ms"],
                           arr["generator_fidelity"]["budget_ms"])
        self.assertGreater(arr["dispatch_lateness_ms"]["max"], 400.0)

    def test_a_harness_stall_never_prints_as_an_engine_verdict(self):
        """The printed line is what a reader acts on. A level whose arrivals came out as a burst is
        not the Poisson stream it names, so the engine cannot be judged from it: the generator line
        prints and the engine line does not, even though this engine really is overloaded."""
        state = {"n": 0}

        def stalling(x):
            state["n"] += 1
            time.sleep(0.5 if state["n"] == 4 else x)

        engine = FakeEngine(service_s=0.05, capacity=1)
        with patched_sleep(stalling):
            docs, out = run_main(["--arrival", "poisson", "--rate", "30", "--requests", "30",
                                  "--output-tokens", "4", "--warmup", "0",
                                  "--queue-sample-interval", "0.02"], engine=engine)
        self.assertIn("the GENERATOR missed its own schedule", out)
        self.assertNotIn("the ENGINE did not keep up", out)
        doc = docs["serve_bench_poisson.json"]
        self.assertIs(doc["levels"][0]["arrival"]["generator_kept_up"], False)


# --------------------------------------------------------------------------------------
# the harness's own ceilings


class CountingClient(FakeClient):
    """A client that reports connections the way the real one does, so connections_opened is
    exercised rather than assumed."""

    def __init__(self, base_url, timeout):
        FakeClient.__init__(self, base_url, timeout)
        self.connects = 1


class TestHarnessCeilings(unittest.TestCase):
    def test_a_thread_start_failure_truncates_the_level_and_keeps_the_others(self):
        """One thread per arrival is a ceiling of the harness, not of the engine. It used to raise
        out of run_level, and because the document was written once at the end of the sweep, the
        run directory came back EMPTY: every already-measured level was destroyed by the last one.
        """
        real_thread = threading.Thread

        class CappedThread(real_thread):
            limit = 40
            started = 0

            def start(self):
                target = getattr(self, "_target", None)
                if target is not None and getattr(target, "__name__", "") == "one_shot":
                    CappedThread.started += 1
                    if CappedThread.started > CappedThread.limit:
                        raise RuntimeError("can't start new thread")
                return real_thread.start(self)

        S.threading.Thread = CappedThread
        try:
            # A service time longer than the second level's whole arrival span, so nothing has
            # completed when the ceiling is hit and the in-flight count is exactly what the
            # harness managed to dispatch.
            docs, out = run_main(["--arrival", "poisson", "--rate", "20,200", "--requests", "30",
                                  "--output-tokens", "4", "--warmup", "0"],
                                 engine=FakeEngine(service_s=0.5))
        finally:
            S.threading.Thread = real_thread
        doc = docs["serve_bench_poisson.json"]
        self.assertEqual(len(doc["levels"]), 2, "the surviving levels must be on disk")
        first, second = doc["levels"]
        self.assertIs(first["arrival"]["truncated_by_harness_limit"], False)
        self.assertEqual(first["requests_ok"], 30, "level 1 must be untouched")
        self.assertIs(second["arrival"]["truncated_by_harness_limit"], True)
        self.assertEqual(second["arrival"]["requests_dispatched"], 10)
        failure = second["arrival"]["dispatch_failures"][0]
        self.assertEqual(failure["salt"], 10)
        self.assertGreater(failure["scheduled_offset_s"], 0.0)
        self.assertIn("can't start new thread", failure["error"])
        # nothing leaked in the process of truncating
        self.assertEqual(second["requests_unaccounted"], 0)
        self.assertEqual(second["arrival"]["queue_depth"]["peak_inflight"], 10)
        self.assertIn("the HARNESS hit a ceiling", out)

    def test_the_live_thread_count_is_recorded_beside_peak_inflight(self):
        engine = FakeEngine(service_s=0.2)
        args = fake_args(arrival="poisson", rate="50", requests=20, arrival_seed=5,
                         queue_sample_interval=0.01)
        q = run_level_against(engine, args, None, 20)["arrival"]["queue_depth"]
        self.assertGreaterEqual(q["peak_threads_alive"], q["peak_inflight"],
                                "one thread per arrival, so the thread count is the ceiling that "
                                "gets hit first and it has to be visible before it is hit")

    def test_connections_opened_and_clients_created_are_recorded(self):
        engine = FakeEngine(service_s=0.002)
        args = fake_args(arrival="poisson", rate="50", requests=12, arrival_seed=2,
                         queue_sample_interval=0.02)
        level = S.run_level(args, None, 12,
                            send=lambda c, salt: engine.one_request(c, args, salt),
                            make_client=lambda: CountingClient(args.base_url, args.timeout))
        # one client per arrival plus the one used for the metrics scrape, and the metrics client's
        # own sockets count too: they come out of the same ephemeral-port budget
        self.assertEqual(level["clients_created"], 13)
        self.assertEqual(level["connections_opened"], 13)


class TestAccountingCannotLeak(unittest.TestCase):
    def test_a_client_that_cannot_be_built_is_booked_not_lost(self):
        """make_client used to run BEFORE the try, and _finish was the only thing that booked an
        outcome or released the flight. An injected OSError on the 5th arrival gave
        requests_attempted=10, requests_ok=9, error_count=0 and errors=[]: a request vanished with
        no error anywhere, which lowered the completion rate and left the in-flight count one too
        high for the rest of the level."""
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] == 5:
                raise OSError("[Errno 24] Too many open files")
            return FakeClient("http://x/v1", 1.0)

        engine = FakeEngine(service_s=0.005)
        args = fake_args(arrival="poisson", rate="100", requests=10, arrival_seed=2,
                         queue_sample_interval=0.02)
        level = S.run_level(args, None, 10,
                            send=lambda c, salt: engine.one_request(c, args, salt),
                            make_client=flaky)
        self.assertEqual(level["requests_unaccounted"], 0, "a dispatched request booked no outcome")
        self.assertEqual(level["harness_error_count"], 1)
        self.assertEqual(level["error_count"], 0, "a client this side of the wire is not an engine "
                                                  "error")
        self.assertEqual(level["requests_ok"] + level["error_count"]
                         + level["harness_error_count"], level["requests_attempted"])
        self.assertEqual(level["error_stages"], {"harness_client_setup": 1})
        # The leaked flight used to inflate every later reading of the trace. requests_unaccounted
        # is the witness for that too: the in-flight release and the outcome are booked in the same
        # block, so nothing can be unaccounted without also being stuck in flight. The last sample
        # is NOT a witness, because the sampler stops as soon as the joins return and its final
        # point can predate the final completions.
        self.assertLessEqual(level["arrival"]["queue_depth"]["max"], level["requests_dispatched"],
                             "a leaked slot would let the flight exceed what was dispatched")

    def test_engine_errors_and_harness_errors_are_told_apart(self):
        """A client-side ceiling must never read as the engine refusing work."""
        cases = [(RuntimeError("HTTP 503: overloaded"), "engine_http_status", "engine"),
                 (ConnectionRefusedError(111, "refused"), "harness_connect_refused", "harness"),
                 (OSError("[Errno 24] Too many open files"), "harness_transport", "harness"),
                 (TimeoutError("timed out"), "engine_timeout", "engine"),
                 (ConnectionResetError("reset by peer"), "engine_stream", "engine")]
        for exc, stage, side in cases:
            def boom(client, salt, exc=exc):
                raise exc
            level = S.run_level(fake_args(), 2, 4, send=boom,
                                make_client=lambda: FakeClient("http://x/v1", 1.0))
            self.assertEqual(level["error_stages"], {stage: 4}, repr(exc))
            if side == "engine":
                self.assertEqual(level["error_count"], 4, repr(exc))
                self.assertEqual(level["harness_error_count"], 0, repr(exc))
            else:
                self.assertEqual(level["error_count"], 0, repr(exc))
                self.assertEqual(level["harness_error_count"], 4, repr(exc))
            self.assertEqual(level["requests_unaccounted"], 0, repr(exc))

    def test_socket_exhaustion_is_named_as_such(self):
        import errno as E

        def boom(client, salt):
            raise OSError(E.EMFILE, "Too many open files")
        level = S.run_level(fake_args(), 2, 4, send=boom,
                            make_client=lambda: FakeClient("http://x/v1", 1.0))
        self.assertEqual(level["error_stages"], {"harness_socket_exhausted": 4})

    def test_a_closed_loop_worker_that_cannot_build_a_client_is_recorded(self):
        """The other workers still run the level, so nothing is lost, but a level run by three of
        four workers is not measuring the concurrency it claims."""
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] == 3:            # the metrics client is first, so this is worker 2
                raise OSError("no fd")
            return FakeClient("http://x/v1", 1.0)

        engine = FakeEngine(service_s=0.002)
        args = fake_args()
        level = S.run_level(args, 4, 12,
                            send=lambda c, salt: engine.one_request(c, args, salt),
                            make_client=flaky)
        self.assertEqual(level["requests_ok"], 12, "the surviving workers finish the level")
        self.assertEqual(level["requests_unaccounted"], 0)
        self.assertEqual(level["error_stages"], {"harness_worker_client_setup": 1})


class TestSampleIntervalIsSizedFromTheLevel(unittest.TestCase):
    def test_the_default_comes_from_the_level_not_from_a_constant(self):
        """--requests 20 --rate 100 is a 0.512 s level. At the old fixed 0.25 s default it got TWO
        samples, and queue_depth.max read 10 against a peak_inflight of 14."""
        args = fake_args(arrival="poisson", rate="100", requests=20, queue_sample_interval=None)
        interval, why = S.sample_interval_for(args, 20, 100.0)
        self.assertAlmostEqual(interval, 0.005)
        self.assertIn("auto", why)
        self.assertEqual(S.sample_interval_for(args, 2000, 100.0)[0], 0.2)
        self.assertEqual(S.sample_interval_for(args, 100000, 100.0)[0], 0.25)

    def test_an_explicit_interval_still_wins(self):
        args = fake_args(queue_sample_interval=0.02)
        self.assertEqual(S.sample_interval_for(args, 20, 100.0), (0.02, "explicit: "
                                                                  "--queue-sample-interval"))

    def test_a_short_level_gets_a_usable_trace(self):
        engine = FakeEngine(service_s=0.025, capacity=1)
        args = fake_args(arrival="poisson", rate="100", requests=20, arrival_seed=21,
                         queue_sample_interval=None)
        q = run_level_against(engine, args, None, 20)["arrival"]["queue_depth"]
        self.assertGreater(len(q["samples"]), 30, "a two-sample trace is not evidence of anything")
        self.assertEqual(q["sample_count"], len(q["samples"]))
        self.assertEqual(q["sample_interval_s"], 0.005)
        # The requested interval is a floor: Event.wait cannot beat the OS timer tick, so the
        # interval the trace ACHIEVED is reported next to the one that was asked for.
        self.assertGreaterEqual(q["effective_interval_s"], q["sample_interval_s"])

    def test_one_quantity_has_one_maximum(self):
        engine = FakeEngine(service_s=0.025, capacity=1)
        args = fake_args(arrival="poisson", rate="100", requests=20, arrival_seed=21,
                         queue_sample_interval=0.25)      # deliberately far too coarse
        level = run_level_against(engine, args, None, 20)
        q = level["arrival"]["queue_depth"]
        self.assertGreaterEqual(q["max"], level["peak_inflight"],
                                "the smaller of two maxima must never win")
        self.assertIn("peak_inflight", q["max_source"])

    def test_a_non_positive_interval_is_refused(self):
        for bad in (0.0, -0.25):
            args = fake_args(arrival="poisson", rate="10", queue_sample_interval=bad)
            with self.assertRaises(ValueError):
                run_level_against(FakeEngine(0.001), args, None, 4)


class TestPerLevelSeeds(unittest.TestCase):
    def test_the_derived_seed_is_stable_and_differs_per_level(self):
        self.assertEqual(S.level_seed(7, 0), S.level_seed(7, 0))
        self.assertNotEqual(S.level_seed(7, 0), S.level_seed(7, 1))
        self.assertNotEqual(S.level_seed(7, 0), S.level_seed(8, 0))

    def test_seed_zero_is_not_swallowed(self):
        """int(x or DEFAULT) turned --arrival-seed 0 into the default, so config.arrival_seed=0 sat
        beside arrival.seed=20260825 and the document carried two different seeds."""
        self.assertEqual(S.seed_or_default(0), 0)
        self.assertEqual(S.seed_or_default(None), S.DEFAULT_ARRIVAL_SEED)
        d = S.arrival_declaration(fake_args(arrival="poisson", rate="8", arrival_seed=0))
        self.assertEqual(d["arrival"]["seed"], 0)

    def test_the_levels_of_a_sweep_are_independent_draws(self):
        """gap = -ln(1-U)/rate is scale-invariant, so one seed shared across a sweep gives the
        IDENTICAL normalised draw error at every rate: -5.107658% at 2, 4, 8, 20, 50 and 200 req/s,
        to twelve decimals. One unlucky sample then biases the whole sweep in one direction."""
        docs, _out = run_main(["--arrival", "poisson", "--rate", "20,40,80", "--requests", "12",
                               "--output-tokens", "4", "--warmup", "0",
                               "--queue-sample-interval", "0.02"])
        levels = docs["serve_bench_poisson.json"]["levels"]
        seeds = [l["arrival"]["seed"] for l in levels]
        self.assertEqual(len(set(seeds)), 3, "levels shared a seed: %r" % (seeds,))
        draws = [l["arrival"]["schedule_vs_target_pct"] for l in levels]
        self.assertEqual(len(set(draws)), 3, "levels realized the same draw error: %r" % (draws,))
        self.assertEqual([l["arrival"]["level_index"] for l in levels], [0, 1, 2])
        self.assertNotIn("schedule_draw_warning", docs["serve_bench_poisson.json"]["arrival"])

    def test_a_sweep_that_shares_a_draw_is_flagged(self):
        """The negative control for the check above, and the alarm if seed derivation is ever
        removed again: force every level onto one seed and the document must say so."""
        saved = S.level_seed
        S.level_seed = lambda base, index: 4242
        try:
            docs, _out = run_main(["--arrival", "poisson", "--rate", "20,40,80",
                                   "--requests", "12", "--output-tokens", "4", "--warmup", "0",
                                   "--queue-sample-interval", "0.02"])
        finally:
            S.level_seed = saved
        doc = docs["serve_bench_poisson.json"]
        draws = [l["arrival"]["schedule_vs_target_pct"] for l in doc["levels"]]
        self.assertEqual(len(set(draws)), 1, "the forced-seed control did not share a draw: %r"
                         % (draws,))
        self.assertIn("schedule_draw_warning", doc["arrival"])


class TestTheFlightIsNotCalledABacklog(unittest.TestCase):
    def test_the_key_and_the_printed_line_say_in_flight_not_backlog(self):
        """36 requests in flight at rate 40 with a 1 s service time is rate x service: Little's
        law, on an engine that queued nothing. It was printed as "Backlog at the last arrival"."""
        engine = FakeEngine(service_s=0.2)
        args = fake_args(arrival="poisson", rate="40", requests=40, arrival_seed=5,
                         queue_sample_interval=0.02)
        q = run_level_against(engine, args, None, 40)["arrival"]["queue_depth"]
        self.assertNotIn("at_last_arrival", q, "the misleading key must be gone, not aliased")
        self.assertIn("inflight_at_last_arrival", q)
        self.assertIn("not the engine's queue", q["is_not_a_backlog"])
        self.assertGreater(q["littles_law_inflight"], 0.0)

    def test_littles_law_explains_a_deep_flight_on_a_healthy_engine(self):
        engine = FakeEngine(service_s=0.5)
        args = fake_args(arrival="poisson", rate="40", requests=40, arrival_seed=5,
                         queue_sample_interval=0.02)
        arr = run_level_against(engine, args, None, 40)["arrival"]
        q = arr["queue_depth"]
        # in flight at the last arrival is within a factor of two of rate x mean latency, and the
        # verdict is False: a deep flight is not a backlog.
        self.assertGreater(q["inflight_at_last_arrival"], 10)
        self.assertLess(abs(q["inflight_at_last_arrival"] - q["littles_law_inflight"])
                        / q["littles_law_inflight"], 0.5)
        self.assertIs(arr["fell_behind"], False)

    def test_the_engine_s_own_queue_is_polled_through_the_level(self):
        """requests_waiting_end came from the after-the-level scrape, taken once every thread had
        joined: an instant when nothing is queued, so it was about zero BY CONSTRUCTION."""
        class MetricsClient(FakeClient):
            polls = 0

            def get(self, path, root=False):
                if path.endswith("/models"):
                    return 200, json.dumps({"data": [{"id": "fake-model"}]})
                if path == "/metrics":
                    MetricsClient.polls += 1
                    return 200, ("vllm:num_requests_running %d\n"
                                 "vllm:num_requests_waiting %d\n"
                                 "vllm:prompt_tokens_total 10\n"
                                 "vllm:generation_tokens_total 20\n"
                                 % (MetricsClient.polls, MetricsClient.polls * 2))
                return 404, ""

        engine = FakeEngine(service_s=0.02)
        args = fake_args(arrival="poisson", rate="20", requests=20, arrival_seed=5,
                         queue_sample_interval=0.05)
        level = S.run_level(args, None, 20,
                            send=lambda c, salt: engine.one_request(c, args, salt),
                            make_client=lambda: MetricsClient(args.base_url, args.timeout))
        server = level["arrival"]["queue_depth"]["server_side"]
        self.assertGreater(len(server["samples"]), 2,
                           "the engine's queue must be sampled DURING the level, not only at the "
                           "two instants when it is provably empty")
        self.assertEqual(server["columns"][1], "vllm:num_requests_running")
        times = [s[0] for s in server["samples"]]
        self.assertEqual(times, sorted(times))
        self.assertGreater(max(times), 0.1, "all samples came from the start of the level")

    def test_a_missing_metrics_endpoint_says_so_rather_than_going_quiet(self):
        engine = FakeEngine(service_s=0.005)
        args = fake_args(arrival="poisson", rate="40", requests=10, arrival_seed=5,
                         queue_sample_interval=0.02)
        server = run_level_against(engine, args, None, 10)["arrival"]["queue_depth"]["server_side"]
        self.assertEqual(server["samples"], [])
        self.assertIn("no vLLM metrics endpoint answered", server["why"])


class TestClientHonoursTheScheme(unittest.TestCase):
    def test_get_uses_https_when_the_base_url_does(self):
        """Client.get always built an HTTPConnection, so against an https base_url every /metrics
        scrape failed and server_metrics_delta went silently None."""
        opened = []

        class Recorder(object):
            def __init__(self, kind):
                self.kind = kind

            def __call__(self, host, port, timeout=None, context=None):
                opened.append((self.kind, host, port))
                return FakeConn()

        class FakeConn(object):
            def request(self, *a, **k):
                pass

            def getresponse(self):
                class R(object):
                    status = 200

                    def read(self):
                        return b"ok"
                return R()

            def close(self):
                pass

        saved = (S.http.client.HTTPConnection, S.http.client.HTTPSConnection)
        S.http.client.HTTPConnection = Recorder("http")
        S.http.client.HTTPSConnection = Recorder("https")
        try:
            S.Client("https://engine.example/v1", 5.0).get("/metrics", root=True)
            S.Client("http://engine.example/v1", 5.0).get("/metrics", root=True)
        finally:
            S.http.client.HTTPConnection, S.http.client.HTTPSConnection = saved
        self.assertEqual([o[0] for o in opened], ["https", "http"])
        self.assertEqual([o[2] for o in opened], [443, 80])

    def test_a_client_counts_its_own_connections(self):
        c = S.Client("http://engine.example/v1", 5.0)
        self.assertEqual(c.connects, 0)


class TestArrivalNoteAgreesWithTheModel(unittest.TestCase):
    """D7 compares the declared model against the prose beside it. The prose has to be generated
    from the same switch as the model, or the document argues with itself."""

    CLOSED_PHRASES = ("fixed in-flight", "no independent arrival process",
                      "issued when a previous one completes")

    def test_open_loop_prose_contains_no_closed_loop_phrase(self):
        d = S.arrival_declaration(fake_args(arrival="poisson", rate="8"))
        note = d["arrival"]["arrival_note"].lower()
        for phrase in self.CLOSED_PHRASES:
            self.assertNotIn(phrase, note)
        for level_note in (d["arrival"]["inter_arrival"].lower(),):
            self.assertNotIn("issued when a previous one completes", level_note)

    def test_closed_loop_prose_says_what_closed_loop_means(self):
        note = S.arrival_declaration(fake_args())["arrival"]["arrival_note"].lower()
        for phrase in self.CLOSED_PHRASES:
            self.assertIn(phrase, note)

    def test_a_closed_run_given_a_rate_declares_no_rate(self):
        """--concurrency 2 --rate 37 declared a 37 req/s target beside an inter_arrival of "the
        next request is issued when a previous one completes". The warning went to stderr, which no
        result file preserves."""
        d = S.arrival_declaration(fake_args(arrival="closed", rate="37"))["arrival"]
        self.assertIsNone(d["target_rate_req_s"])
        self.assertIsNone(d["seed"])
        self.assertEqual(d["rate_ignored_because"], "closed loop has no arrival rate")
        self.assertEqual(d["rate_as_asked"], [37.0])
        self.assertFalse(d["independent_of_completions"])


# --------------------------------------------------------------------------------------
# the declaration reaches the document


class TestArrivalReachesTheDocument(unittest.TestCase):
    def _check_load_shape(self, doc):
        """Run the real gate over a manifest carrying this document's declaration."""
        f = V.Findings()
        V.check_load_shape({"claims": {}, "report": doc.get("report", {}), "levels": []}, f)
        return [i for i in f.items if i["check"] == "D4"]

    def test_declaration_carries_both_keys_from_one_source(self):
        for mode, rate, model in (("closed", None, "closed_loop"),
                                  ("poisson", "8", "open_loop_poisson")):
            d = S.arrival_declaration(fake_args(arrival=mode, rate=rate))
            self.assertEqual(d["arrival_model"], model)
            self.assertEqual(d["report"]["arrival_model"], model)
            self.assertEqual(d["arrival"]["model"], model)

    def test_verify_accepts_the_declared_model_in_both_modes(self):
        """Asserted behaviourally: the vocabulary lives in verify.check_load_shape, so the test
        runs that function rather than keeping a second copy of the accepted strings."""
        for mode, rate in (("closed", None), ("poisson", "8")):
            d = S.arrival_declaration(fake_args(arrival=mode, rate=rate))
            self.assertEqual(self._check_load_shape(d), [],
                             "verify rejected the %s declaration" % mode)

    def test_the_check_is_sensitive(self):
        """The negative control. If D4 never fired, the test above would pass on an empty doc."""
        self.assertTrue(self._check_load_shape({"report": {}}))
        self.assertTrue(self._check_load_shape({"report": {"arrival_model": "open_loop_maybe"}}))

    def test_poisson_declaration_records_the_seed_and_the_rate_sweep(self):
        d = S.arrival_declaration(fake_args(arrival="poisson", rate="2,4,8", arrival_seed=31337))
        self.assertEqual(d["arrival"]["target_rate_req_s"], [2.0, 4.0, 8.0])
        self.assertEqual(d["arrival"]["seed"], 31337)
        self.assertTrue(d["arrival"]["independent_of_completions"])

    def test_default_seed_is_a_constant_not_the_clock(self):
        a = S.arrival_declaration(fake_args(arrival="poisson", rate="5"))["arrival"]["seed"]
        time.sleep(0.01)
        b = S.arrival_declaration(fake_args(arrival="poisson", rate="5"))["arrival"]["seed"]
        self.assertEqual(a, b)
        self.assertEqual(a, S.DEFAULT_ARRIVAL_SEED)

    def test_parse_rates_accepts_a_scalar_a_list_and_nothing(self):
        self.assertEqual(S.parse_rates("4"), [4.0])
        self.assertEqual(S.parse_rates("1,2.5, 10 "), [1.0, 2.5, 10.0])
        self.assertEqual(S.parse_rates(7), [7.0])
        self.assertEqual(S.parse_rates(None), [])
        self.assertEqual(S.parse_rates(""), [])


class TestWrittenDocument(unittest.TestCase):
    """End-to-end through the real main(): the real parser, the real document assembly, the real
    file naming. The socket is the only thing replaced."""

    def _main(self, argv):
        return run_main(argv)

    def test_closed_run_declares_closed_loop_and_keeps_the_canonical_filename(self):
        docs, out = self._main(["--concurrency", "2,4", "--requests", "4",
                                "--output-tokens", "4", "--warmup", "0"])
        self.assertEqual(list(docs), ["serve_bench.json"])
        doc = docs["serve_bench.json"]
        self.assertEqual(doc["arrival_model"], "closed_loop")
        self.assertEqual(doc["report"]["arrival_model"], "closed_loop")
        self.assertEqual([l["concurrency"] for l in doc["levels"]], [2, 4])
        self.assertTrue(all(l["arrival"]["model"] == "closed_loop" for l in doc["levels"]))
        self.assertIn("CONC", out)

    def test_poisson_run_declares_the_model_writes_its_own_file_and_sweeps_rates(self):
        docs, out = self._main(["--arrival", "poisson", "--rate", "20,40", "--requests", "20",
                                "--output-tokens", "4", "--warmup", "0",
                                "--queue-sample-interval", "0.02"])
        self.assertEqual(list(docs), ["serve_bench_poisson.json"],
                         "a Poisson run must not overwrite the closed-loop result")
        doc = docs["serve_bench_poisson.json"]
        self.assertEqual(doc["arrival_model"], "open_loop_poisson")
        self.assertEqual(doc["report"]["arrival_model"], "open_loop_poisson")
        self.assertEqual(doc["arrival"]["target_rate_req_s"], [20.0, 40.0])
        self.assertEqual(doc["arrival"]["seed"], S.DEFAULT_ARRIVAL_SEED)
        self.assertEqual(doc["config"]["rate"], "20,40", "config must keep the sweep as asked")
        self.assertEqual([l["arrival"]["target_rate_req_s"] for l in doc["levels"]], [20.0, 40.0])
        for level in doc["levels"]:
            self.assertEqual(level["arrival"]["model"], "open_loop_poisson")
            self.assertIsNotNone(level["arrival"]["achieved_rate_req_s"])
            self.assertTrue(level["arrival"]["queue_depth"]["samples"])
        self.assertIn("TARGET", out)
        self.assertIn("ACHIEVED", out)

    def test_the_written_document_passes_the_load_shape_gate(self):
        for argv in (["--concurrency", "2", "--requests", "4"],
                     ["--arrival", "poisson", "--rate", "20", "--requests", "10"]):
            docs, _out = self._main(argv + ["--output-tokens", "4", "--warmup", "0"])
            doc = list(docs.values())[0]
            f = V.Findings()
            V.check_load_shape({"claims": {}, "report": doc["report"], "levels": []}, f)
            self.assertEqual([i for i in f.items if i["check"] == "D4"], [],
                             "verify would block a document written by %r" % (argv,))

    def test_poisson_without_a_rate_exits_two(self):
        with self.assertRaises(SystemExit) as cm:
            self._main(["--arrival", "poisson", "--warmup", "0"])
        self.assertEqual(cm.exception.code, 2)

    def test_a_non_positive_sample_interval_exits_two(self):
        for bad in ("0", "-1"):
            with self.assertRaises(SystemExit) as cm:
                self._main(["--arrival", "poisson", "--rate", "10", "--warmup", "0",
                            "--queue-sample-interval", bad])
            self.assertEqual(cm.exception.code, 2)

    def test_poisson_with_a_length_sweep_is_refused_rather_than_silently_narrowed(self):
        """--rate 2,4,8 --mode prefill declared target_rate_req_s=[2.0, 4.0, 8.0] in the document
        and actually ran [2.0, 2.0]: main() set the rate from rates[0] for every level of those
        modes while the declaration came from the whole parsed string."""
        for mode in ("prefill", "decode", "mixed"):
            with self.assertRaises(SystemExit) as cm:
                self._main(["--arrival", "poisson", "--rate", "2,4,8", "--mode", mode,
                            "--requests", "6", "--warmup", "0", "--output-tokens", "4"])
            self.assertEqual(cm.exception.code, 2, mode)

    def test_the_declared_rates_are_the_rates_the_levels_were_given(self):
        """The declaration is derived from the levels, so it cannot outrun the run whatever the
        parsed string said. Checked for every (mode, arrival) pair the CLI accepts."""
        for mode in ("concurrency", "prefill", "decode", "mixed"):
            docs, _out = self._main(["--mode", mode, "--concurrency", "2", "--requests", "4",
                                     "--output-tokens", "4", "--warmup", "0",
                                     "--input-lengths", "128,512", "--output-lengths", "8,16"])
            doc = list(docs.values())[0]
            self.assertIsNone(doc["arrival"]["target_rate_req_s"],
                              "closed loop declares no rate (mode=%s)" % mode)
            self.assertTrue(all(l["arrival"]["target_rate_req_s"] is None
                                for l in doc["levels"]), mode)
        docs, _out = self._main(["--arrival", "poisson", "--rate", "20,40,80", "--requests", "8",
                                 "--output-tokens", "4", "--warmup", "0",
                                 "--queue-sample-interval", "0.02"])
        doc = docs["serve_bench_poisson.json"]
        self.assertEqual([l["arrival"]["target_rate_req_s"] for l in doc["levels"]],
                         [20.0, 40.0, 80.0])
        self.assertEqual(doc["arrival"]["target_rate_req_s"], [20.0, 40.0, 80.0])
        self.assertEqual(doc["config"]["rate"], "20,40,80", "config keeps the sweep as asked")

    def test_a_level_that_fails_outright_costs_only_itself(self):
        """The document is written after every level, so a crash in level N leaves levels 1..N-1 on
        disk and names the one that failed."""
        real_run_level = S.run_level
        calls = {"n": 0}

        def flaky(args, *a, **kw):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("simulated level failure")
            return real_run_level(args, *a, **kw)

        S.run_level = flaky
        try:
            docs, out = run_main(["--concurrency", "1,2,4", "--requests", "4",
                                  "--output-tokens", "4", "--warmup", "0"])
        finally:
            S.run_level = real_run_level
        doc = docs["serve_bench.json"]
        self.assertEqual([l["concurrency"] for l in doc["levels"]], [1, 4])
        self.assertEqual(len(doc["level_failures"]), 1)
        self.assertEqual(doc["level_failures"][0]["level_index"], 1)
        self.assertIn("simulated level failure", doc["level_failures"][0]["error"])
        self.assertIn("level 1 failed", out)

    def test_the_document_is_written_with_lf_endings(self):
        """This file is written on Windows and read on Linux."""
        run_dir = tempfile.mkdtemp(prefix="gpubench-test-")
        saved = (S.Client, S.one_request, sys.argv, os.environ.get("GPUBENCH_RUN_DIR"))
        engine = FakeEngine(service_s=0.002)
        S.Client = FakeClient
        S.one_request = engine.one_request
        sys.argv = ["serving.py", "--concurrency", "1", "--requests", "2", "--warmup", "0",
                    "--output-tokens", "4"]
        os.environ["GPUBENCH_RUN_DIR"] = run_dir
        try:
            with redirect_stdout(io.StringIO()):
                S.main()
        finally:
            S.Client, S.one_request, sys.argv = saved[0], saved[1], saved[2]
            if saved[3] is None:
                os.environ.pop("GPUBENCH_RUN_DIR", None)
            else:
                os.environ["GPUBENCH_RUN_DIR"] = saved[3]
        with open(os.path.join(run_dir, "serve_bench.json"), "rb") as fh:
            raw = fh.read()
        self.assertNotIn(b"\r\n", raw)
        self.assertIn(b"\n", raw)

    def test_the_printed_open_loop_lines_never_call_the_flight_a_backlog(self):
        engine = FakeEngine(service_s=0.05, capacity=1)
        docs, out = run_main(["--arrival", "poisson", "--rate", "40", "--requests", "40",
                              "--output-tokens", "4", "--warmup", "0",
                              "--queue-sample-interval", "0.02"], engine=engine)
        self.assertNotIn("Backlog", out)
        self.assertIn("LATENCY GREW over this level", out)
        self.assertIn("Little's law baseline", out)
        arr = docs["serve_bench_poisson.json"]["levels"][0]["arrival"]
        self.assertIs(arr["latency_grew_over_the_level"], True)
        # This fake exposes no metrics endpoint, so nothing here can support a CAPACITY claim and
        # the printed line must not make one.
        self.assertIsNone(arr["engine_did_not_keep_up"])
        self.assertNotIn("the ENGINE did not keep up", out)
        self.assertIn("does NOT confirm this as a capacity limit", out)


# --------------------------------------------------------------------------------------
# the three false-negative reproductions, and the control that proves the healthy cases still pass


class QueueingEngine(object):
    """k servers, a fixed service time, and a CLIENT TIMEOUT that censors the slow requests.

    Unlike FakeEngine's semaphore this one gives up: a request that cannot get a server within
    `timeout_s` raises TimeoutError, which error_stage() books as engine_timeout. That is the
    shape of a real overloaded engine seen through a real client, and it is the shape that used to
    delete the evidence.
    """

    def __init__(self, servers, service_s, timeout_s=1e6):
        self.sem = threading.Semaphore(servers)
        self.service = service_s
        self.timeout = timeout_s
        self.lock = threading.Lock()
        self.true_waits = []
        self.running = 0
        self.waiting = 0

    def send(self, client, salt):
        t0 = time.perf_counter()
        with self.lock:
            self.waiting += 1
        got = self.sem.acquire(timeout=self.timeout)
        with self.lock:
            self.waiting -= 1
            if got:
                self.running += 1
        if not got:
            raise TimeoutError("client timeout after %.3fs" % self.timeout)
        try:
            with self.lock:
                self.true_waits.append(time.perf_counter() - t0)
            time.sleep(self.service)
        finally:
            with self.lock:
                self.running -= 1
            self.sem.release()
        return {"ttft_s": 0.001, "e2e_s": time.perf_counter() - t0, "itls": [],
                "completion_tokens": 4, "prompt_tokens": 8}

    # run_main() installs an engine as S.one_request, run_level takes it as `send`. Both names,
    # because an engine that has only one of them raises while run_main is halfway through
    # patching the module and leaves S.Client replaced for every test that follows.
    def one_request(self, client, args, salt, in_tok=None, out_tok=None):
        return self.send(client, salt)


class DriftingEngine(object):
    """UNLIMITED parallelism, so it cannot queue by construction, with a service time that drifts
    linearly. This is what a CO-TENANT taking the machine looks like from outside: every request
    gets slower, and none of them is waiting behind one of ours."""

    def __init__(self, base_s, growth_per_s, cv=0.0, seed=11):
        self.base = base_s
        self.g = growth_per_s
        self.cv = cv
        self.rng = random.Random(seed)
        self.lock = threading.Lock()
        self.t0 = time.perf_counter()
        self.running = 0
        self.waiting = 0

    def send(self, client, salt):
        t = time.perf_counter() - self.t0
        mu = self.base + self.g * t
        if self.cv > 0:
            sigma = math.sqrt(math.log(1.0 + self.cv ** 2))
            with self.lock:
                mu *= math.exp(self.rng.gauss(-0.5 * sigma * sigma, sigma))
        with self.lock:
            self.running += 1
        started = time.perf_counter()
        try:
            time.sleep(max(0.0005, mu))
        finally:
            with self.lock:
                self.running -= 1
        return {"ttft_s": 0.001, "e2e_s": time.perf_counter() - started, "itls": [],
                "completion_tokens": 4, "prompt_tokens": 8}

    def one_request(self, client, args, salt, in_tok=None, out_tok=None):
        return self.send(client, salt)


class MetricsClient(FakeClient):
    """A FakeClient whose /metrics answers with the engine's own running/waiting split.

    Without this the capacity verdict can only ever be "cannot tell", which is correct but does
    not exercise the gate. With it the two cases that must NOT be confused are both reachable: a
    queue inside the engine, and a slowdown with no queue anywhere.
    """

    def __init__(self, base_url, timeout, engine):
        FakeClient.__init__(self, base_url, timeout)
        self.engine = engine
        self.connects = 0

    def get(self, path, root=False):
        if path.endswith("/models"):
            return 200, json.dumps({"data": [{"id": "fake-model"}]})
        if path == "/metrics":
            with self.engine.lock:
                run_n, wait_n = self.engine.running, self.engine.waiting
            return 200, ("vllm:num_requests_running %d\n"
                         "vllm:num_requests_waiting %d\n"
                         "vllm:prompt_tokens_total 0\n"
                         "vllm:generation_tokens_total 0\n" % (run_n, wait_n))
        return 404, ""


def level_for(engine, rate, n, seed=7, interval=0.02, metrics=False, **over):
    args = fake_args(arrival="poisson", rate=str(rate), requests=n, arrival_seed=seed,
                     queue_sample_interval=interval, **over)
    mk = ((lambda: MetricsClient(args.base_url, args.timeout, engine)) if metrics
          else (lambda: FakeClient(args.base_url, args.timeout)))
    send = getattr(engine, "send", None)
    if send is None:
        def send(client, salt):
            return engine.one_request(client, args, salt, args.input_tokens, args.output_tokens)
    return S.run_level(args, None, n, send=send, make_client=mk)


class TestCensoringCannotHideAQueue(unittest.TestCase):
    """REPRODUCTION 1. The growth fit ran on requests that SUCCEEDED, so the requests that prove
    the queue were exactly the ones excluded from the evidence.

    Measured against a 20 req/s engine offered 60 req/s, 90 arrivals, true median wait 1.48 s and
    true max 2.92 s (the audit ran the same shape ten times slower: 2 req/s offered 6 req/s, true
    median 16.35 s, true max 32.2 s). Verdict column, BEFORE:
        timeout 1.00 s -> ok 50/90, TRUE     0.60 -> ok 43, FALSE     0.40 -> ok 39, FALSE
        timeout 0.30 s -> ok 37,    FALSE    0.20 -> ok 35, FALSE
    The tighter the timeout, the more certainly an overloaded engine was reported as fine, and at
    the tightest settings the level printed nothing at all.
    """

    def _censored(self, timeout_s):
        return level_for(QueueingEngine(2, 0.1, timeout_s), 60, 90, seed=31, interval=0.05)

    def test_a_tight_client_timeout_no_longer_buys_a_clean_bill_of_health(self):
        for timeout_s in (1.0, 0.6, 0.4, 0.3, 0.2):
            lv = self._censored(timeout_s)
            arr = lv["arrival"]
            self.assertIsNot(arr["latency_grew_over_the_level"], False,
                             "timeout=%.2f ok=%d: %s" % (timeout_s, lv["requests_ok"],
                                                         arr["latency_grew_over_the_level_basis"]))
            self.assertIn("floor", arr["latency_grew_over_the_level_basis"])
            self.assertEqual(arr["queue_growth"]["completion_fraction_floor"],
                             S.QUEUE_GROWTH_MIN_COMPLETION_FRACTION)

    def test_an_errored_request_is_booked_into_the_fit_as_a_censored_observation(self):
        lv = self._censored(0.4)
        g = lv["arrival"]["queue_growth"]
        self.assertGreater(lv["error_count"], 0, "this engine must actually time requests out")
        self.assertEqual(g["n_censored"], lv["error_count"])
        self.assertEqual(g["n"], lv["requests_ok"] + lv["error_count"],
                         "every dispatched request that reached the wire is in the fit")
        self.assertIn("LOWER BOUND", g["censored_note"])
        self.assertEqual(lv["requests_unaccounted"], 0, "accounting must still balance")

    def test_a_verdict_of_none_is_printed_as_loudly_as_a_verdict(self):
        """main() tested `elif arr["fell_behind"]:`, so a None printed NOTHING AT ALL: the level
        with the least evidence produced the quietest output in the run."""
        engine = QueueingEngine(2, 0.1, 0.3)
        docs, out = run_main(["--arrival", "poisson", "--rate", "60", "--requests", "90",
                              "--output-tokens", "4", "--warmup", "0",
                              "--queue-sample-interval", "0.05"], engine=engine)
        arr = docs["serve_bench_poisson.json"]["levels"][0]["arrival"]
        self.assertIsNone(arr["latency_grew_over_the_level"], out)
        self.assertIn("NOT JUDGED", out)
        self.assertIn("lower bound", out)

    def test_the_deprecated_name_still_carries_the_same_value(self):
        arr = self._censored(0.4)["arrival"]
        self.assertIs(arr["fell_behind"], arr["latency_grew_over_the_level"])
        self.assertIn("latency_grew_over_the_level", arr["fell_behind_renamed_to"])


class TestARealTrendSurvivesNoise(unittest.TestCase):
    """REPRODUCTION 2. The t-statistic gate was not a noise floor.

    A REAL trend of 0.15 s per second, with growth ratios of 1.31/1.72/1.91/2.34 all clearing the
    effect gate, gave True/True/FALSE/FALSE as multiplicative noise rose to cv 0.8 and 1.1, with t
    falling 11.51 -> 4.44 -> 2.64 -> 1.79. The trend was real in all four.
    """

    def _drifting(self, cv):
        return level_for(DriftingEngine(0.05, 0.15, cv=cv, seed=11), 20, 60, seed=13,
                         interval=0.05)["arrival"]

    def test_the_trend_is_found_at_every_noise_level(self):
        for cv in (0.0, 0.4, 0.8, 1.1):
            arr = self._drifting(cv)
            g = arr["queue_growth"]
            self.assertIs(arr["latency_grew_over_the_level"], True,
                          "cv=%.1f: %s" % (cv, arr["latency_grew_over_the_level_basis"]))
            self.assertGreaterEqual(g["mann_kendall_z"], S.QUEUE_GROWTH_MK_Z)

    def test_the_verdict_holds_where_the_old_t_gate_would_have_failed_it(self):
        """The row that flipped: at cv 1.1 the OLS t statistic falls under the old 3.0 gate while
        the rank test is still far above its own."""
        arr = self._drifting(1.1)
        g = arr["queue_growth"]
        self.assertLess(g["slope_t_stat"], S.QUEUE_GROWTH_T,
                        "if t has stopped falling under the old gate this test proves nothing")
        self.assertIs(arr["latency_grew_over_the_level"], True)
        self.assertIn("no longer a gate", g["slope_t_stat_is_reported_only"])

    def test_a_series_the_trend_test_cannot_read_is_not_judged(self):
        """Fail closed. ols_slope returns se=None on an exact fit, and the caller used to read
        that as PASSING the significance gate, which is the opposite of what the docstring says.
        On a series of identical latencies there is no rank trend to compute at all, and the honest
        answer is 'not judged', not the silent False the old branch produced."""
        xs = [i * 0.1 for i in range(20)]
        verdict, basis, stats = S.judge_latency_growth(xs, [1.0] * 20, 2.0, 20, 20)
        self.assertIsNone(stats["mann_kendall_z"])
        self.assertIsNone(stats["slope_std_error"], "this is the exact-fit branch")
        self.assertIsNone(verdict)
        self.assertIn("not judged", basis)

    def test_theil_sen_is_not_dragged_by_one_enormous_request(self):
        """Why the size of the trend is a median of pairwise slopes and not a least-squares line:
        one 30 s request among 5 s ones is a heavy tail, not a trend."""
        xs = [float(i) for i in range(40)]
        ys = [5.0 + 0.001 * i for i in range(40)]
        ys[3] = 300.0
        _v, _b, stats = S.judge_latency_growth(xs, ys, 40.0, 40, 40)
        self.assertLess(abs(stats["theil_sen_slope_s_per_s"]), 0.01)
        self.assertGreater(abs(stats["e2e_slope_s_per_s"]), 0.1,
                           "the least-squares line really is dragged, which is the point")


class TestAVoidedLevelCarriesNoVerdict(unittest.TestCase):
    """REPRODUCTION 3. A level the generator gate had voided still carried fell_behind: true in
    the JSON. Only the console suppressed it, and no key marked the level void.

    Reproduced: a 500 ms dispatcher stall at rate 30 wrote generator_kept_up false, p95 deviation
    361.05 ms against a 3.33 ms budget, and fell_behind true, in the same block.
    """

    def test_a_frozen_generator_voids_the_verdict_in_the_json_too(self):
        engine = QueueingEngine(1, 0.05)
        state = {"n": 0}

        def stalling(x):
            state["n"] += 1
            time.sleep(0.5 if state["n"] == 5 else x)

        with patched_sleep(stalling):
            arr = level_for(engine, 30, 60, seed=4, interval=0.05)["arrival"]
        self.assertIs(arr["generator_kept_up"], False)
        self.assertIsNone(arr["latency_grew_over_the_level"],
                          arr["latency_grew_over_the_level_basis"])
        self.assertIsNone(arr["fell_behind"], "the deprecated alias must agree")
        self.assertIn("the generator missed its own schedule",
                      arr["latency_grew_over_the_level_basis"])
        self.assertIsNone(arr["engine_did_not_keep_up"])

    def test_a_truncated_level_is_voided_and_its_warning_comes_first(self):
        """A truncated level used to get an engine verdict AND have it printed BEFORE the warning
        that the level had stopped early, so a reader met the conclusion above the reason to
        disbelieve it."""
        real_thread = threading.Thread

        class CappedThread(real_thread):
            limit = 12
            started = 0

            def start(self):
                target = getattr(self, "_target", None)
                if target is not None and getattr(target, "__name__", "") == "one_shot":
                    CappedThread.started += 1
                    if CappedThread.started > CappedThread.limit:
                        raise RuntimeError("can't start new thread")
                return real_thread.start(self)

        S.threading.Thread = CappedThread
        try:
            docs, out = run_main(["--arrival", "poisson", "--rate", "20", "--requests", "40",
                                  "--output-tokens", "4", "--warmup", "0",
                                  "--queue-sample-interval", "0.02"],
                                 engine=FakeEngine(service_s=0.4, capacity=1))
        finally:
            S.threading.Thread = real_thread
        arr = docs["serve_bench_poisson.json"]["levels"][0]["arrival"]
        self.assertIs(arr["truncated_by_harness_limit"], True)
        self.assertIsNone(arr["latency_grew_over_the_level"],
                          arr["latency_grew_over_the_level_basis"])
        self.assertIn("truncated", arr["latency_grew_over_the_level_basis"])
        self.assertIn("the HARNESS hit a ceiling", out)
        self.assertLess(out.index("the HARNESS hit a ceiling"), out.index("NOT JUDGED"),
                        "the reason to disbelieve the level has to print above the verdict line")


class TestTheCapacityClaimIsGatedOnTheEnginesOwnQueue(unittest.TestCase):
    """A latency trend does not say WHOSE load caused it. This box serves prod, qa, dev and live
    from ONE vLLM, so a co-tenant slowdown raises our latencies with nothing of ours queued.

    Reproduced on an unlimited-parallelism fake that CANNOT queue by construction, given a linearly
    drifting service time: 0.08 s/s of drift reported "the ENGINE did not keep up" at ratio 1.091,
    and 0.50 s/s at ratio 1.850, with zero errors and zero queueing anywhere in the system.
    """

    def test_a_co_tenant_slowdown_is_not_reported_as_our_rate_exhausting_the_engine(self):
        for g in (0.08, 0.50):
            engine = DriftingEngine(0.05, g, seed=11)
            lv = level_for(engine, 20, 60, seed=13, interval=0.05, metrics=True)
            arr = lv["arrival"]
            self.assertEqual(lv["error_count"], 0)
            self.assertIs(arr["latency_grew_over_the_level"], True,
                          "the latency really did grow, and the tool must still say so")
            self.assertIs(arr["engine_did_not_keep_up"], False,
                          arr["engine_did_not_keep_up_basis"])
            self.assertIs(arr["queue_growth"]["engine_side"]["engine_queue_grew"], False)
            self.assertIn("not shown to be the cause", arr["engine_did_not_keep_up_basis"])

    def test_a_real_capacity_limit_is_still_named_as_one(self):
        """The positive control. An engine with one server offered far more than it can serve
        queues INSIDE ITSELF, and its own waiting counter says so."""
        engine = QueueingEngine(1, 0.05)
        arr = level_for(engine, 40, 60, seed=21, interval=0.02, metrics=True)["arrival"]
        self.assertIs(arr["latency_grew_over_the_level"], True)
        self.assertIs(arr["queue_growth"]["engine_side"]["engine_queue_grew"], True)
        self.assertIs(arr["engine_did_not_keep_up"], True, arr["engine_did_not_keep_up_basis"])
        self.assertIn("queued inside the engine", arr["engine_did_not_keep_up_basis"])

    def test_without_the_engines_own_split_the_capacity_claim_is_cannot_tell(self):
        arr = level_for(QueueingEngine(1, 0.05), 40, 60, seed=21, interval=0.02)["arrival"]
        self.assertIs(arr["latency_grew_over_the_level"], True)
        self.assertIsNone(arr["engine_did_not_keep_up"])
        self.assertIn("CANNOT TELL", arr["engine_did_not_keep_up_basis"])

    def test_the_sensitivity_limit_is_stated_in_the_probes_own_output(self):
        """The structural ceiling is not fixed by the rank test and is therefore DECLARED. For a
        linear ramp median = L0 + gT/2, so growth/median tends to 2 and the 1.0x gate sits at
        exactly half the largest value the statistic can take."""
        g = level_for(QueueingEngine(1, 0.05), 40, 60, seed=21, interval=0.02)["arrival"]["queue_growth"]
        self.assertIn("1.86x", g["sensitivity_limit"])
        self.assertIn("4.1 s", g["sensitivity_limit"])
        self.assertIn("trend_detected", g["sensitivity_limit"])
        self.assertIsNotNone(g["trend_detected"])


class TestTheHealthyCasesStillPass(unittest.TestCase):
    """THE CONTROL. A verdict that is always None or always True is as useless as one that is
    always False, and from outside they look identical.

    Two tiers, and the split is stated rather than hidden. The grid tier runs the WHOLE mandated
    span of service times (5 ms to 21 s) and offered rates (0.5 to 3 req/s) through the real
    verdict function on synthetic healthy series, which is the only way 21 s of service time fits
    in a test suite. The dispatch tier runs the real generator, the real threads and the real
    timing over the affordable part of the same grid.
    """

    SERVICES = (0.005, 0.05, 0.5, 2.18, 8.0, 21.0)
    RATES = (0.5, 1.0, 1.5, 2.0, 2.5, 3.0)

    def test_thirty_six_healthy_levels_on_the_full_stated_grid(self):
        rng = random.Random(4242)
        for service_s in self.SERVICES:
            for rate in self.RATES:
                n = 30
                # a real Poisson draw for the arrivals, and a healthy engine's latency: the
                # service time with a little dispersion and no trend in it
                xs, t = [], 0.0
                for _ in range(n):
                    t += -math.log(1.0 - rng.random()) / rate
                    xs.append(t)
                ys = [service_s * (1.0 + rng.gauss(0.0, 0.05)) for _ in range(n)]
                verdict, basis, _stats = S.judge_latency_growth(xs, ys, xs[-1], n, n)
                self.assertIs(verdict, False,
                              "service=%.3f rate=%.1f: %s" % (service_s, rate, basis))

    def test_the_dispatch_tier_agrees_over_the_affordable_part_of_the_grid(self):
        """Real threads, real sleeps, real timing. Service times above 2 s at rates below 1 req/s
        are left to the grid tier: a single such level costs more wall clock than this whole file.
        """
        for service_s in (0.005, 0.05, 0.5, 2.0):
            for rate in (1.0, 2.0, 3.0):
                arr = level_for(FakeEngine(service_s=service_s), rate, 6, seed=17,
                                interval=0.05)["arrival"]
                self.assertIsNot(arr["latency_grew_over_the_level"], True,
                                 "service=%.3f rate=%.1f: %s"
                                 % (service_s, rate, arr["latency_grew_over_the_level_basis"]))
        # and one level at the lowest mandated rate, where a whole level is 12 seconds long
        arr = level_for(FakeEngine(service_s=0.05), 0.5, 6, seed=19, interval=0.1)["arrival"]
        self.assertIs(arr["latency_grew_over_the_level"], False,
                      arr["latency_grew_over_the_level_basis"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
