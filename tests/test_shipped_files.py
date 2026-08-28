"""Files the documentation and CI promise must actually be IN the repository.

This is the second time evidence has gone missing by sitting in a gitignored directory. In the
report, claims cited result artefacts that were never committed. Here, `.gitignore` carried
`results/` with no leading slash, which matches a directory of that name at any depth, so it
swallowed `examples/results/` -- the worked example the README describes at length and the
reproduction-record CI job reads. Both times every local check passed, because every local check
ran on the machine where the files happened to exist.

A rule that does what it says and not what it means will not be caught by reading it. It is caught
by asking git what is actually tracked, which is what this does.

Skips cleanly outside a git checkout, because the release archive is extracted without one and the
suites are required to run there.
"""
import os
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def tracked_files():
    """Every path git tracks, or None when this is not a checkout."""
    try:
        p = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                           universal_newlines=True)
    except (OSError, ValueError):
        return None
    if p.returncode != 0:
        return None
    return set(p.stdout.split())


HAVE_GIT = tracked_files() is not None

# Read by .github/workflows/verify.yml. The job copies this file and rebuilds a reproduction
# record over it; without it the job dies on `cp` before running a line of Python.
CI_READS = ["examples/results/onprem-2x5090.json"]

# Described in README.md under "The example data", including the claim that every one of them
# passes the redaction gate. A reader who clones this repo must find them.
README_PROMISES = [
    "examples/results/README.json",
    "examples/results/onprem-2x5090.json",
    "examples/results/onprem-2x5090-serving-v2.json",
    "examples/results/onprem-2x5090-workload.json",
    "examples/results/laptop-rtx3050.json",
]


@unittest.skipUnless(HAVE_GIT, "not a git checkout")
class PromisedFilesAreTracked(unittest.TestCase):
    def setUp(self):
        self.tracked = tracked_files()

    def test_the_files_ci_reads_are_committed(self):
        missing = [p for p in CI_READS if p not in self.tracked]
        self.assertEqual(missing, [], "the reproduction-record job reads these and git does not "
                                      "track them, so a clean checkout fails on cp: %s" % missing)

    def test_the_example_data_the_readme_describes_is_committed(self):
        missing = [p for p in README_PROMISES if p not in self.tracked]
        self.assertEqual(missing, [], "README.md describes these as the worked example and git "
                                      "does not track them: %s" % missing)

    def test_this_estate_s_own_results_stay_untracked(self):
        """The negative control. The ignore rules exist for a reason and must keep working.

        A general-purpose benchmark that ships one site's measurements invites those numbers to be
        quoted as the tool's own. Anchoring the rule to the repository root must not have opened
        the top-level results/ directory.
        """
        leaked = sorted(p for p in self.tracked
                        if p.startswith("results/") or p.startswith("reports/"))
        self.assertEqual(leaked, [], "site measurements are tracked and should not be: %s"
                                     % leaked[:5])

    def test_the_redaction_denylist_is_never_committed(self):
        """A denylist names an estate's own nouns. Shipping it ships what it protects."""
        leaked = sorted(p for p in self.tracked
                        if os.path.basename(p) in ("denylist.txt", ".denylist"))
        self.assertEqual(leaked, [], "a site denylist is tracked: %s" % leaked)


if __name__ == "__main__":
    unittest.main()
