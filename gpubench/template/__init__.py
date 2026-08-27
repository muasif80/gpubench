"""Reusable benchmark-report template.

Artifacts in this package:

    run-schema.json     what a run bundle must contain, and where each number lives
    lint-rules.md       the eleven rules, each citing the historical defect it prevents
    report-outline.yaml the section manifest and its per-section invariants
    outline.py          a dependency-free reader for the YAML subset the manifest uses
    lint.py             the executable linter that enforces the eleven rules
    schema.py           a standard-library validator for run-schema.json, which refuses to
                        return a verdict over a schema it cannot enforce in full
    scaffold.py         the generator behind `gpubench template init`
    cli.py              the four subcommands, attached to gpubench's own parser

REACHABLE FROM THE COMMAND LINE. Everything above used to sit inside the package with no
subcommand and no import from outside it, which is the same as not shipping it: material
nobody can run is material nobody reviews. The surface is now::

    gpubench template init <dir>                       scaffold a report that passes the gate
    gpubench template lint <run-dir> <report.html>     the eleven rules
    gpubench template outline                          the canonical sections and invariants
    gpubench template schema [--validate FILE]         the run-bundle contract

Nothing here names a domain. The template must fit a processor, a storage array or a
network fabric as well as it fits the report it was derived from, so the vocabulary is
roofs, units, workload, problem size, quality gate and mode (constraint B-C2).

Standard library only, by contract: a report build must not be able to fail because a
third-party parser or validator moved. Constraint B-C3 requires the linter to be
runnable as ``python -m template.lint <run-dir> <built-report>``.
"""

__all__ = ["cli", "lint", "outline", "scaffold", "schema"]

TEMPLATE_VERSION = "1.0.0"
