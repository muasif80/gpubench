# CLAUDE.md

## What this is

**gpubench** — a GPU benchmark that reports which ceiling a machine is hitting (compute, memory
bandwidth, or interconnect) rather than a bare throughput number. Apache-2.0, intended for public
release. Pure standard library, no dependencies.

## Layout

```
gpubench/
  cli.py          argparse front end: inspect | run | report | index
  runner.py       transports (local, ssh, plink) + tier orchestration + result schema
  report.py       self-contained HTML report and index generation, inline SVG charts
  probes/
    inventory.py     tier 0: nvidia-smi, sysfs PCIe, DMI/WMI. Read-only, no allocation.
    cuda_driver.py   tier 2: CUDA driver API via ctypes. No toolkit, no PyTorch.
    torch_compute.py tier 1: piped into an existing PyTorch runtime, never installed.
```

The orchestrator never touches CUDA. That is what lets it run from Windows, Linux or macOS against
any target.

## Non-negotiable design rules

- **Tier 0 must be useful alone.** The machines worth measuring are the ones where you cannot
  install anything.
- **Degrade, never fake.** A metric the target cannot supply is reported as not measured. Rendering
  it as zero is a bug.
- **Shared mode is the default.** Destructive actions require an explicit confirmation token.
- **The target host is hashed by default.** Results are meant to be shared.
- **Every number traceable.** Reference figures carry provenance: published, or derived from a
  published figure, and derivations say so.

## Traps already paid for

- `threading.Thread` has an internal `_stop()` method. Assigning `self._stop = Event()` in a
  Thread subclass makes the interpreter call an Event at teardown: `'Event' object is not
  callable`. Use any other name.
- `torch._scaled_mm` (FP8/FP4) raises `CUBLAS_STATUS_INTERNAL_ERROR` on a **second** device inside
  one process. It looks exactly like a broken GPU and is not. The runner re-runs those precisions
  once per device with `CUDA_VISIBLE_DEVICES` pinned.
- NCCL prints a version banner to **stdout** before any JSON. Parse from the first `{`.
- `ctypes` needs explicit `argtypes` for the CUDA driver, or 64-bit pointers and sizes are
  truncated on Windows: silent corruption rather than an error.
- `nvidia-smi --query-gpu` with `nounits` returns `memory.total`, which maps to `memory_total`, not
  `memory_total_mib`.
- PCIe gen reads low while a GPU idles: link power management, not a fault. Trust it only under
  load, or read `max_link_speed` from sysfs.
- `lspci -vv` needs root to print link capabilities. sysfs does not, and walking to the parent
  bridge is what distinguishes a slow card from a slow slot.

## Testing

`python -c "import ast,glob;[ast.parse(open(p,encoding='utf-8').read(),p) for p in glob.glob('gpubench/**/*.py',recursive=True)]"`
then `python -m gpubench inspect` on any machine with a driver.

Packaging: `python tools/make_dist.py` writes `dist/*.tar.gz` and `dist/*.zip`. The archives
contain code only. **`results/` and `reports/` must never be shipped**: result files can carry a
target host and a board model.
