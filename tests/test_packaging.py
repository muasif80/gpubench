"""What `pip install .` actually produces, and the two contract fields that decide its filename.

WHY ONE FILE COVERS BOTH. They are the same defect wearing different clothes: something was
DECLARED and nothing READ IT. pyproject listed four packages while the tree held five, and named
no package-data rule at all, so twenty-four files that the tar.gz and the zip both carry were
dropped from every `pip install .` and the installed linter could not load its own data contract.
The content-module contract documented BASENAME and VERSION while the engine derived the output
filename from getattr defaults, so a module could declare neither and still publish. A declaration
nobody reads is not a contract; it is a comment that looks like one.

So none of the checks below read a declaration. The packaging tests BUILD A WHEEL with the
project's own backend and compare its contents against the package directory on disk, which is why
they cannot go stale the way a hand-written allowlist did: a data file added tomorrow is in the
comparison the moment it is on disk. Two negative controls then rebuild the wheel with the fix
removed and require the same comparison to FAIL, because a test that passes with the fix and
without it is proving something other than the fix.

Run: python -m tests.test_packaging
"""
import glob
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, ".")
from gpubench import longform  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "gpubench")

# Calling the backend directly rather than shelling out to pip: pip would either reach the network
# for build isolation or install into somewhere this test would then have to clean up. The wheel is
# the artifact `pip install .` unpacks, so reading the wheel is reading what an install lands.
_BUILD_WHEEL = "import sys, setuptools.build_meta as backend; backend.build_wheel(sys.argv[1])"

# Run inside the unpacked wheel, with the repository nowhere on the path. It proves three separate
# things about the INSTALLED copy, in the order they would break: the package resolves to the
# unpacked tree and not to a checkout, its data contract is on disk and parses, and its own test
# suite, whose twenty fixtures are data files too, passes against it.
_INSTALLED_CHECK = '''\
import os, sys, unittest

root = os.path.abspath(sys.argv[1])
import gpubench
where = os.path.abspath(os.path.dirname(gpubench.__file__))
if not where.startswith(root):
    sys.exit("imported %s, which is not the unpacked wheel at %s" % (where, root))

from gpubench.template import outline
path = outline.default_outline_path()
if not os.path.exists(path):
    sys.exit("the installed copy has no %s: its data contract did not ship" % path)
doc = outline.load_outline()
if not (doc.get("sections") or []):
    sys.exit("the shipped report-outline.yaml parsed to no sections")

suite = unittest.defaultTestLoader.loadTestsFromName("gpubench.template.tests.test_lint")
if suite.countTestCases() < 1:
    sys.exit("the installed copy exposes no template tests, so nothing read a single fixture")
result = unittest.TextTestRunner(verbosity=0, stream=sys.stderr).run(suite)
sys.exit(0 if result.wasSuccessful() else "the template suite failed against the installed copy")
'''


def package_files_on_disk():
    """Every file the package directory holds, as the wheel would name it.

    The resolved contents, walked. Nothing here is written down, which is the point: the allowlist
    this replaces went stale the moment somebody added a fixture, and said nothing when it did.
    """
    out = set()
    for dirpath, dirnames, filenames in os.walk(PKG):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for name in filenames:
            if name.endswith((".pyc", ".pyo")):
                continue
            rel = os.path.relpath(os.path.join(dirpath, name), ROOT)
            out.add(rel.replace(os.sep, "/"))
    return out


def copy_source(dest, pyproject_text=None):
    """A buildable copy of the project in `dest`, optionally with an edited pyproject.

    Built in a copy so a negative control can edit pyproject without touching the repository, and
    so the build's own droppings (build/, .egg-info) never land in the tree under test.
    """
    src = os.path.join(dest, "src")
    shutil.copytree(PKG, os.path.join(src, "gpubench"),
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"))
    if pyproject_text is None:
        with io.open(os.path.join(ROOT, "pyproject.toml"), "r", encoding="utf-8") as f:
            pyproject_text = f.read()
    with io.open(os.path.join(src, "pyproject.toml"), "w", encoding="utf-8", newline="\n") as f:
        f.write(pyproject_text)
    for name in ("README.md", "LICENSE", "NOTICE"):
        path = os.path.join(ROOT, name)
        if os.path.exists(path):
            shutil.copy2(path, os.path.join(src, name))
    return src


def build_wheel(dest, pyproject_text=None):
    """Build a wheel with the project's own backend. Returns (member names, wheel path)."""
    src = copy_source(dest, pyproject_text)
    out = os.path.join(dest, "wheel")
    os.makedirs(out)
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    proc = subprocess.run([sys.executable, "-c", _BUILD_WHEEL, out], cwd=src, env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    wheels = glob.glob(os.path.join(out, "*.whl"))
    if proc.returncode != 0 or not wheels:
        raise AssertionError("building a wheel with the project's own backend failed:\n%s"
                             % proc.stdout.decode("utf-8", "replace"))
    with zipfile.ZipFile(wheels[0]) as archive:
        names = set(n for n in archive.namelist() if n.startswith("gpubench/"))
    return names, wheels[0]


def without_table(text, header):
    """pyproject text with one table removed, for a negative control."""
    kept, dropping = [], False
    for line in text.split("\n"):
        if line.strip() == header:
            dropping = True
            continue
        if dropping and line.startswith("["):
            dropping = False
        if not dropping:
            kept.append(line)
    return "\n".join(kept)


class Packaging(unittest.TestCase):
    """The installed copy, judged by building one and reading it."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="gpubench-packaging-")
        cls.names, cls.wheel = build_wheel(os.path.join(cls.tmp, "asbuilt"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_the_wheel_holds_every_file_the_package_directory_holds(self):
        """The check that fails when a data file stops shipping, whatever the data file is.

        Both directions, because the two ways to break an install look nothing alike: a file on
        disk and not in the wheel is the bug this fixes, and a file in the wheel and not on disk is
        a stale build directory being packaged as if it were source.
        """
        on_disk = package_files_on_disk()
        self.assertTrue(on_disk, "walked the package directory and found no files at all, so this "
                                 "comparison would pass against an empty wheel")
        missing = sorted(on_disk - self.names)
        self.assertEqual([], missing,
                         "%d file(s) are in gpubench/ on disk and absent from the wheel, so "
                         "`pip install .` lands a package without them: %s"
                         % (len(missing), ", ".join(missing)))
        extra = sorted(self.names - on_disk)
        self.assertEqual([], extra,
                         "%d file(s) are in the wheel and not in gpubench/ on disk: %s"
                         % (len(extra), ", ".join(extra)))

    def test_the_wheel_carries_data_files_and_not_only_python(self):
        """A guard on the guard above: equal sets prove nothing if both sets are all .py.

        If package-data were dropped AND the files deleted from the tree, the comparison would go
        green on two empty halves. This says out loud that non-Python files are part of what ships.
        """
        data = sorted(n for n in self.names if not n.endswith(".py"))
        self.assertTrue(data, "the wheel carries no non-Python file at all, so the linter's data "
                              "contract and every lint fixture are missing from an install")
        for required in ("gpubench/template/report-outline.yaml",
                         "gpubench/template/run-schema.json",
                         "gpubench/template/lint-rules.md"):
            self.assertIn(required, self.names,
                          "%s is what the linter loads at runtime and it is not in the wheel"
                          % required)
        fixtures = [n for n in data if "/tests/fixtures/" in n]
        self.assertGreaterEqual(len(fixtures), 20,
                                "only %d lint fixture(s) shipped; the template suite proves each "
                                "rule against a fixture, so a missing one silently disarms a rule"
                                % len(fixtures))

    def test_the_installed_copy_loads_its_data_contract_and_passes_the_template_suite(self):
        """End to end on the path that was broken: unpack the wheel, run the package from there.

        This is the failure as a user met it. `python -m gpubench.template.lint` could not load
        report-outline.yaml after an install, and nothing in the repository could see it because
        the repository has the file.
        """
        root = os.path.join(self.tmp, "installed")
        os.makedirs(root)
        with zipfile.ZipFile(self.wheel) as archive:
            archive.extractall(root)
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        proc = subprocess.run([sys.executable, "-c", _INSTALLED_CHECK, root], cwd=root, env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.assertEqual(0, proc.returncode,
                         "the unpacked wheel did not behave as an installed package:\n%s"
                         % proc.stdout.decode("utf-8", "replace"))

    def test_removing_the_package_data_rule_makes_the_comparison_fail(self):
        """Negative control: the rule is what ships the data, not something else.

        Without this, every assertion above is consistent with setuptools having shipped the data
        files for some unrelated reason, and the pyproject rule being decoration. Removing the
        rule has to break exactly the thing the rule is credited with.
        """
        with io.open(os.path.join(ROOT, "pyproject.toml"), "r", encoding="utf-8") as f:
            text = f.read()
        stripped = without_table(text, "[tool.setuptools.package-data]")
        self.assertNotEqual(text, stripped,
                            "pyproject holds no [tool.setuptools.package-data] table, so this "
                            "control removed nothing and would pass against any tree")
        names, _ = build_wheel(os.path.join(self.tmp, "nodata"), stripped)
        dropped = sorted(package_files_on_disk() - names)
        self.assertTrue(dropped,
                        "the wheel built WITHOUT the package-data rule is complete, so the rule "
                        "is not what makes the data files ship and the test above passes for a "
                        "reason nobody has established")
        self.assertIn("gpubench/template/report-outline.yaml", dropped,
                      "the data contract survived removal of the rule that is supposed to carry "
                      "it; what actually ships it is unknown")

    def test_removing_the_tests_package_makes_its_modules_vanish(self):
        """Negative control for the other half: an unlisted package ships no Python either.

        gpubench.template.tests was missing from `packages` while its directory sat in the tree,
        which is the same class of defect as the missing data rule and is invisible in a checkout.
        """
        with io.open(os.path.join(ROOT, "pyproject.toml"), "r", encoding="utf-8") as f:
            text = f.read()
        stripped = text.replace('    "gpubench.template.tests",\n', "")
        self.assertNotEqual(text, stripped,
                            "pyproject does not list gpubench.template.tests on its own line, so "
                            "this control removed nothing")
        names, _ = build_wheel(os.path.join(self.tmp, "notests"), stripped)
        gone = sorted(n for n in package_files_on_disk() - names if n.endswith(".py"))
        self.assertIn("gpubench/template/tests/test_lint.py", gone,
                      "the template test module shipped even though its package was not listed, "
                      "so listing it is not what put it in the wheel")


# --------------------------------------------------------------------------------------
# the content-module contract: BASENAME and VERSION
#
# The engine documented both and read neither. The filename was reassembled at the call site from
# getattr defaults, so a missing declaration and a deliberate one produced the same build and the
# same silence. These tests read the resolved stem and the effect on a real build, never the
# module's attributes.

class _Content(object):
    """A minimal content module. Attributes are set, or deliberately not set, per test."""

    TITLE = "Synthetic"
    SECTION_ORDER = ["Findings"]

    def __init__(self, **fields):
        self.built = []
        for name, value in fields.items():
            setattr(self, name, value)

    def build(self, run_dir, out_dir=None):
        self.built.append(run_dir)
        return {}, {}

    def render(self, figures, data):
        return "<section><h2>Findings</h2><p>Nothing to report.</p></section>"


class ContentContract(unittest.TestCase):

    def test_the_stem_is_the_declared_basename_and_the_declared_version(self):
        content = _Content(BASENAME="rtx5090-dual-gpu-benchmark", VERSION="8.9")
        self.assertEqual("rtx5090-dual-gpu-benchmark-v8.9", longform.report_stem(content))

    def test_a_caller_may_override_the_basename_but_never_the_version(self):
        """--basename replaces the value. The edition belongs to the content, not the command."""
        content = _Content(BASENAME="declared", VERSION="2.0")
        self.assertEqual("chosen-v2.0", longform.report_stem(content, "chosen"))

    def test_a_missing_declaration_is_refused_and_the_message_names_the_field(self):
        for field, present in (("BASENAME", {"VERSION": "1.0"}),
                               ("VERSION", {"BASENAME": "report"})):
            with self.subTest(field=field):
                with self.assertRaises(SystemExit) as caught:
                    longform.report_stem(_Content(**present))
                self.assertIn(field, str(caught.exception))

    def test_a_malformed_declaration_is_refused_rather_than_published(self):
        """Each of these once produced a file, under a name nobody chose."""
        for fields, what in (
            ({"BASENAME": "../escape", "VERSION": "1.0"}, "a parent reference"),
            ({"BASENAME": "reports/name", "VERSION": "1.0"}, "a path separator"),
            ({"BASENAME": "", "VERSION": "1.0"}, "an empty stem"),
            ({"BASENAME": " leading", "VERSION": "1.0"}, "a leading space"),
            ({"BASENAME": "ok", "VERSION": 8.9}, "a float edition, which loses 8.10"),
            ({"BASENAME": "ok", "VERSION": "v8.9"}, "an edition carrying its own v"),
            ({"BASENAME": "ok", "VERSION": ""}, "an empty edition"),
            ({"BASENAME": "ok", "VERSION": "draft"}, "a word where an edition belongs"),
        ):
            with self.subTest(what=what):
                with self.assertRaises(SystemExit):
                    longform.report_stem(_Content(**fields))

    def test_an_override_is_checked_as_strictly_as_a_declaration(self):
        content = _Content(BASENAME="fine", VERSION="1.0")
        with self.assertRaises(SystemExit):
            longform.report_stem(content, "../escape")

    def test_render_report_refuses_before_it_builds(self):
        """Read the artifact, not the code: build() records every call, and it must record none.

        Checking the declarations after build() would report a one-line fault only once the
        measurements had been reduced, which is how a check becomes something people route around.
        """
        content = _Content(BASENAME="report")
        with self.assertRaises(SystemExit):
            longform.render_report(content, "run-dir")
        self.assertEqual([], content.built,
                         "build() ran before the declarations were checked")

    def test_render_report_carries_the_stem_it_resolved(self):
        """The caller that writes the files gets the engine's name for them, not its own."""
        content = _Content(BASENAME="synthetic", VERSION="1.2")
        rendered = longform.render_report(content, "run-dir")
        self.assertEqual("synthetic-v1.2", rendered.stem)
        self.assertEqual(["run-dir"], content.built)
        html, figures, data = rendered
        self.assertIn("Nothing to report.", html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
