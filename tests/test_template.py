#!/usr/bin/env python3
"""Repo-root entry point for the report template's own tests.

    python -m tests.test_template          (from the repo root, beside the other six suites)

WHY THIS FILE EXISTS. The template package ships 961 lines of tests over the linter and the YAML
subset reader, plus the suite for the `gpubench template` subcommand. They lived at a path only
reachable by running unittest from inside the package's parent directory, which meant they were
not run beside the other six suites and nothing noticed when they stopped being run. A suite that
is not in the run list is a suite that is already failing silently.

Two modules are collected:

    gpubench.template.tests.test_lint   one test per lint rule, plus the fixtures that reproduce
                                        the real historical defects, plus the outline reader
    gpubench.template.tests.test_cli    the subcommand: init builds and passes the gate, the
                                        gate blocks a broken scaffold, the schema validator
                                        refuses a schema it cannot fully enforce

THE COUNT FLOOR BELOW IS NOT PEDANTRY. `loadTestsFromModule` over a module that failed to import
its fixtures, or whose classes were renamed, returns a small suite and reports success. A suite
that runs nothing passes, and passing is what a pipeline reads. So the floor is asserted before
anything runs, and it fails loudly rather than quietly collecting less.
"""
import sys
import unittest

# Each module's floor: comfortably below what it holds today, high enough that losing a class or
# an import is a failure rather than a shorter run. Raise these when a module grows a section.
FLOORS = {"gpubench.template.tests.test_lint": 40,
          "gpubench.template.tests.test_cli": 25}


def suite():
    from gpubench.template.tests import test_cli, test_lint

    loader = unittest.TestLoader()
    combined = unittest.TestSuite()
    shortfalls = []
    for module in (test_lint, test_cli):
        tests = loader.loadTestsFromModule(module)
        count = tests.countTestCases()
        floor = FLOORS.get(module.__name__, 1)
        if count < floor:
            shortfalls.append("%s collected %d test(s), below its floor of %d"
                              % (module.__name__, count, floor))
        combined.addTests(tests)
    if loader.errors:
        raise SystemExit("tests could not be loaded:\n  " + "\n  ".join(str(e) for e in
                                                                       loader.errors))
    if shortfalls:
        raise SystemExit("refusing to report a result over a suite that lost tests:\n  "
                         + "\n  ".join(shortfalls))
    return combined


def load_tests(loader, tests, pattern):  # noqa: ARG001 - unittest's discovery protocol
    """So `python -m unittest tests.test_template` and discovery run the same set."""
    return suite()


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(suite())
    sys.exit(0 if result.wasSuccessful() else 1)
