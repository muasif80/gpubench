#!/usr/bin/env python3
"""NCCL all-reduce benchmark for the two GPUs, 2 ranks, one process per GPU.

This is the tensor-parallel primitive. vLLM at TP=2 issues two all-reduces per transformer
layer per forward step, so with 64 layers that is 128 all-reduces per decoded token. At batch 1
each message is only hidden_size x 2 bytes = 10 KB, which means **small-message latency matters
far more than large-message bandwidth**. The size list below is chosen around that.

MUST be run as a real file, not piped on stdin: torch.multiprocessing spawn re-imports __main__
by path, and `<stdin>` is not a path. 25_nccl_allreduce.sh stages it into the container first.
The `if __name__ == "__main__"` guard below is what stops each spawned child from re-running the
whole benchmark.

Environment:
  GPUBENCH_SIZES_KB   comma list of message sizes in KiB (default 4,10,20,40,256,1024,4096,16384,65536)
  GPUBENCH_ITERS      timed iterations per size (default 50; small sizes get 10x this)
  GPUBENCH_MODE       label recorded in the output (default shared)
  MASTER_PORT      rendezvous port (default 29577)
"""
import json
import os
import sys
import time

KIB = 1024


def env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def sizes_bytes():
    raw = os.environ.get("GPUBENCH_SIZES_KB", "4,10,20,40,256,1024,4096,16384,65536")
    out = []
    for part in raw.split(","):
        part = part.strip()
        if part:
            out.append(int(float(part) * KIB))
    return out


def worker(rank, world, sizes, iters, port, queue):
    import torch
    import torch.distributed as dist

    os.environ.setdefault("MASTER_ADDR", "127.0.0.1")
    os.environ["MASTER_PORT"] = str(port)
    torch.cuda.set_device(rank)
    dist.init_process_group("nccl", rank=rank, world_size=world)

    results = []
    for nbytes in sizes:
        numel = max(1, nbytes // 4)  # float32
        # Small messages are latency-dominated and noisy, so run many more of them.
        n_iter = iters * 10 if nbytes <= 64 * KIB else iters
        buf = torch.ones(numel, device=rank, dtype=torch.float32)

        for _ in range(10):
            dist.all_reduce(buf)
        torch.cuda.synchronize(rank)
        dist.barrier()

        start = torch.cuda.Event(enable_timing=True)
        end = torch.cuda.Event(enable_timing=True)
        start.record()
        for _ in range(n_iter):
            dist.all_reduce(buf)
        end.record()
        torch.cuda.synchronize(rank)
        latency_s = (start.elapsed_time(end) / 1000.0) / n_iter

        actual = numel * 4
        # Ring all-reduce moves 2*(world-1)/world * bytes per rank across the wire.
        bus_gb_s = actual * 2.0 * (world - 1) / world / latency_s / 1e9
        algo_gb_s = actual / latency_s / 1e9
        if rank == 0:
            results.append({
                "size_bytes": actual,
                "size_kib": actual / 1024.0,
                "iterations": n_iter,
                "latency_ms": latency_s * 1000.0,
                "algo_gb_s": algo_gb_s,
                "bus_gb_s": bus_gb_s,
            })
        del buf
        torch.cuda.empty_cache()
        dist.barrier()

    if rank == 0:
        queue.put(results)
    dist.destroy_process_group()


def main():
    out = {
        "benchmark": "nccl_allreduce",
        "mode": os.environ.get("GPUBENCH_MODE", "shared"),
        "started_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "results": [],
        "errors": [],
    }
    try:
        import torch
        import torch.multiprocessing as mp
    except Exception as exc:  # noqa: BLE001
        out["errors"].append("import torch failed: %r" % (exc,))
        print(json.dumps(out, indent=2))
        return 1

    out["torch_version"] = torch.__version__
    out["cuda_version"] = torch.version.cuda
    out["nccl_version"] = ".".join(str(x) for x in torch.cuda.nccl.version()) \
        if hasattr(torch.cuda, "nccl") else None
    out["device_count"] = torch.cuda.device_count()
    out["nccl_env"] = {k: v for k, v in os.environ.items() if k.startswith("NCCL_")}

    if torch.cuda.device_count() < 2:
        out["errors"].append("need 2 GPUs, found %d" % torch.cuda.device_count())
        print(json.dumps(out, indent=2))
        return 1

    sizes = sizes_bytes()
    iters = env_int("GPUBENCH_ITERS", 50)
    port = env_int("MASTER_PORT", 29577)
    out["config"] = {"sizes_bytes": sizes, "iters": iters, "port": port}

    ctx = mp.get_context("spawn")
    queue = ctx.Queue()
    procs = [ctx.Process(target=worker, args=(r, 2, sizes, iters, port, queue)) for r in range(2)]
    try:
        for p in procs:
            p.start()
        out["results"] = queue.get(timeout=600)
    except Exception as exc:  # noqa: BLE001
        out["errors"].append("all-reduce run failed: %r" % (exc,))
    finally:
        for p in procs:
            p.join(timeout=120)
            if p.is_alive():
                p.terminate()
                out["errors"].append("rank process did not exit, terminated")

    out["finished_at_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    print(json.dumps(out, indent=2))
    return 0 if out["results"] else 1


if __name__ == "__main__":
    sys.exit(main())
