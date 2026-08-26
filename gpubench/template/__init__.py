"""Reusable benchmark-report template.

Artifacts in this package:

    run-schema.json     what a run bundle must contain, and where each number lives
    lint-rules.md       the eleven rules, each citing the historical defect it prevents
    report-outline.yaml the section manifest and its per-section invariants
    outline.py          a dependency-free reader for the YAML subset the manifest uses
    lint.py             the executable linter that enforces the eleven rules

Nothing here names a domain. The template must fit a processor, a storage array or a
network fabric as well as it fits the report it was derived from, so the vocabulary is
roofs, units, workload, problem size, quality gate and mode (constraint B-C2).

Standard library only, by contract: a report build must not be able to fail because a
third-party parser or validator moved. Constraint B-C3 requires the linter to be
runnable as ``python -m template.lint <run-dir> <built-report>``.
"""

__all__ = ["lint", "outline"]

TEMPLATE_VERSION = "1.0.0"
