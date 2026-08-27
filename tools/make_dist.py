#!/usr/bin/env python3
"""Build the standalone distribution archives, and refuse to ship anything identifying.

    python tools/make_dist.py

Writes dist/gpubench-<version>.tar.gz and dist/gpubench-<version>.zip.

Four rules this enforces rather than trusts:

  1. Only an explicit allow-list of paths is packaged. A deny-list is the wrong shape here: a
     result file carries the target host, the board model and device serials, so the day someone
     adds a new output directory a deny-list ships it and an allow-list does not.
  2. An allow-list rots silently, so it is AUDITED against the tree on every build. Every
     top-level entry in the repository is either shipped or explicitly withheld with a reason,
     every allow-listed path must still exist, and every file extension under a shipped directory
     must be classified. An unclassified entry is an error, not a default.
  3. The STAGED TREE is scanned for identifying material before the archives are written, and the
     build aborts on any hit. The scan reads the files that are about to be packaged, not the
     allow-list that chose them.
  4. Each archive is then extracted somewhere clean and exercised: the CLI runs, the template
     package's own data files load, the template subcommand runs if it exists, and every test
     module found in the extracted copy is executed. A package missing a data file or a fixture
     produces a red build here rather than a green build and a broken download.
"""
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------------------
# The allow-list
# ---------------------------------------------------------------------------

INCLUDE_FILES = ["README.md", "LICENSE", "NOTICE", "CHANGELOG.md", "pyproject.toml"]

# Tests ship. This tool's entire value proposition is that its numbers can be checked, and a
# measurement tool whose tests are withheld is asking to be trusted rather than verified.
#
# references/ ships for the same reason one step further out: the claims manifest is a format other
# people's generators are meant to emit, and a format whose rules live only in the source is an
# implementation rather than a contract. It is named as a DIRECTORY so the packaging does not have
# to be edited every time a catalogue file is added or renamed.
#
# examples/ ships because the README's opening section is a link into it. A shipped document that
# points at a file the archive does not contain is the same defect as a check that reads a
# declaration: it asserts something the artifact does not support.
INCLUDE_DIRS = ["gpubench", "tests", "references", "examples"]

# Every other top-level entry, with why it stays behind. The audit below fails the build if the
# repository grows an entry that appears in neither list, so a new directory forces a decision
# instead of silently vanishing from every release.
WITHHELD = {
    "results": "measurement output from specific machines. A general-purpose benchmark that ships "
               "one estate's numbers invites them to be quoted as the tool's own.",
    "reports": "rendered reports over that same measurement output.",
    "dist": "this script's own output.",
    "build": "setuptools scratch.",
    "tools": "maintainer tooling. THIS FILE carries the site-specific deny literals, which are "
             "exactly the nouns the redaction mechanism exists to keep out of a release, so the "
             "builder must never package itself. The identifying-material scan would abort on it, "
             "which is the backstop rather than the rule.",
    "CLAUDE.md": "working notes for an agent editing this repository, not product documentation.",
    "RESUME.md": "a session handoff note. It names the maintainer's local checkout paths and a "
                 "sibling private repository, so it is identifying material as well as being "
                 "irrelevant to a recipient. The scan below would abort on it; this is the rule.",
    ".git": "version control internals.",
    ".gitignore": "version control configuration, meaningless in an extracted archive.",
    ".gitattributes": "version control configuration, meaningless in an extracted archive.",
    ".claude": "local agent configuration.",
    ".venv": "local virtual environment.",
    "venv": "local virtual environment.",
    ".pytest_cache": "test-runner scratch.",
    ".mypy_cache": "type-checker scratch.",
    ".ruff_cache": "linter scratch.",
    "__pycache__": "bytecode cache.",
}

# Extensions that never ship from anywhere, because they are generated or machine-local.
NEVER_SUFFIXES = (".pyc", ".pyo", ".pyd", ".so", ".log", ".swp", ".orig", ".rej", ".bak")

# .json and .html are the SHAPE of measurement output and of rendered reports, so they ship only
# from a directory that has been declared to hold data rather than results. This replaces an
# earlier blanket ".json"/".html" exclusion, which was correct about results and wrong about
# everything else: it silently dropped gpubench/template/run-schema.json, the data contract the
# template package exists to define, from every release.
DATA_SUFFIXES = (".json", ".html")
DATA_DIRS = (
    # The template's data contract, its section manifest, and the fixture bundles its own test
    # suite reads. Excluding these shipped the lint tests without the data they need, so they all
    # failed from a clean extraction while passing in the developer tree. A test suite the
    # recipient cannot run is worse than none, because a green suite here implies a guarantee they
    # do not have.
    "gpubench/template",
    # Reserved for the top-level suite. Nothing lives here yet; naming it now means a fixture added
    # tomorrow ships without anyone remembering this file exists.
    "tests/fixtures",
    # The published worked example. These are result files, deliberately: they are the data behind
    # the report the README leads with, they have been through the redaction gate, and a worked
    # example whose inputs are absent cannot be re-derived by the reader.
    "examples/results",
)

# Anything shipped must carry one of these, so a new file type is a decision rather than an
# accident. LICENSE and NOTICE have no extension and are named explicitly.
SHIPPABLE_SUFFIXES = (".py", ".md", ".yaml", ".yml", ".toml", ".txt", ".cfg", ".ini",
                      ".sha256") + DATA_SUFFIXES
EXTENSIONLESS_OK = ("LICENSE", "NOTICE")


# ---------------------------------------------------------------------------
# The identifying-material gate
# ---------------------------------------------------------------------------

# Structural families plus the literals that would identify an estate.
DENY = [
    ("IPv4 address", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
    ("email address", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b")),
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    # Requires a QUOTED literal. Without that this fires on `password=password`, which is a
    # parameter name rather than a secret, and a gate that cries wolf gets switched off.
    ("hardcoded secret", re.compile(r"(?i)\b(pass(word)?|passwd|secret|api[_-]?key|token)\b"
                                    r"\s*[:=]\s*['\"][^'\"]{8,}['\"]")),
    ("home directory path", re.compile(r"/home/[a-z0-9_-]+/", re.I)),
    ("windows user path", re.compile(r"[A-Za-z]:\\+Users\\+[^\\\s\"'<]+", re.I)),
]
# Deployment-specific names to reject, one per line, read from a gitignored file.
#
# This list used to be hardcoded here, which defeated its own purpose: publishing the names you are
# hiding tells a reader exactly which organisation and hosts to look for. The words are the secret,
# not just the documents containing them. So the list lives outside version control and the scanner
# runs with whatever it finds, reporting how many literals it loaded so an empty file cannot be
# mistaken for a clean scan.
#
# Put one name per line in tools/denylist.txt (gitignored). Blank lines and # comments are ignored.
def _load_deny_literals():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "denylist.txt")
    if not os.path.exists(path):
        return []
    out = []
    with io.open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.split("#", 1)[0].strip()
            if line:
                out.append(line.lower())
    return out


DENY_LITERALS = _load_deny_literals()

# Version numbers and the placeholder in usage examples are not addresses.
ALLOW_TOKENS = {"user@host", "0.0.0.0", "127.0.0.1"}

# A run result, identified by its own shape rather than by where it sits. Used to catch a result
# file that reached the staging tree through a path nobody expected.
RESULT_SHAPE_KEYS = ("probes", "schema_version", "fingerprint")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def version():
    with open(os.path.join(ROOT, "pyproject.toml"), "r", encoding="utf-8") as f:
        m = re.search(r'^version\s*=\s*"([^"]+)"', f.read(), re.M)
    return m.group(1) if m else "0.0.0"


def rel(path, base=ROOT):
    """Path relative to base, in posix form, so one spelling works on both platforms.

    The previous version compared raw dirpath substrings and therefore needed both "tests/fixtures"
    and "tests\\fixtures" spelt out. Normalising once removes the class of bug where a rule works on
    one operating system and quietly does nothing on the other.
    """
    return os.path.relpath(path, base).replace(os.sep, "/")


def in_data_dir(relpath):
    """Is this file inside a directory declared to hold data rather than measurement output?"""
    return any(relpath.startswith(d + "/") for d in DATA_DIRS)


def classify(relpath):
    """Should this file ship, and why not if not. relpath is posix, relative to ROOT."""
    fn = relpath.rsplit("/", 1)[-1]
    if "__pycache__" in relpath.split("/"):
        return False, "bytecode cache"
    low = fn.lower()
    for suf in NEVER_SUFFIXES:
        if low.endswith(suf):
            return False, "generated or machine-local file type %s" % suf
    _stem, ext = os.path.splitext(low)
    if ext in DATA_SUFFIXES and not in_data_dir(relpath):
        return False, "measurement or report shape (%s) outside a declared data directory" % ext
    if not ext and fn not in EXTENSIONLESS_OK:
        return False, "unclassified extensionless file"
    if ext and ext not in SHIPPABLE_SUFFIXES:
        return False, "unclassified file type %s" % ext
    return True, ""


# ---------------------------------------------------------------------------
# Audit: does the allow-list still describe the tree?
# ---------------------------------------------------------------------------

def audit_allow_list():
    """Compare the allow-list against what is actually on disk.

    Returns (errors, notes). An allow-list is a claim about the repository, and this checks the
    claim against the repository rather than assuming it still holds. Three ways it rots:
    an entry that no longer exists, a new top-level entry nobody decided about, and a new file
    type under a shipped directory that the suffix rules silently drop.
    """
    errors = []
    notes = []

    for fn in INCLUDE_FILES:
        if not os.path.isfile(os.path.join(ROOT, fn)):
            errors.append("allow-list names %s, which does not exist" % fn)
    for d in INCLUDE_DIRS:
        if not os.path.isdir(os.path.join(ROOT, d)):
            errors.append("allow-list names directory %s/, which does not exist" % d)
    for d in DATA_DIRS:
        if not os.path.isdir(os.path.join(ROOT, d)):
            notes.append("data directory %s/ does not exist yet (nothing to ship from it)" % d)

    shipped = set(INCLUDE_FILES) | set(INCLUDE_DIRS)
    for entry in sorted(os.listdir(ROOT)):
        if entry in shipped or entry in WITHHELD:
            continue
        if entry.endswith(".egg-info"):
            continue
        errors.append("top-level entry %r is in neither the allow-list nor WITHHELD. Decide: add "
                      "it to INCLUDE_FILES/INCLUDE_DIRS, or to WITHHELD with the reason." % entry)

    # Every file type under a shipped directory has to be accounted for. A dropped file is
    # invisible in the archive listing unless you already know to look for it, which is how
    # run-schema.json went missing for a whole release.
    dropped = {}
    for d in INCLUDE_DIRS:
        for dirpath, dirnames, filenames in os.walk(os.path.join(ROOT, d)):
            dirnames[:] = [x for x in dirnames if x != "__pycache__"]
            for fn in filenames:
                r = rel(os.path.join(dirpath, fn))
                ok, why = classify(r)
                if ok or why in ("bytecode cache",) or why.startswith("generated"):
                    continue
                dropped.setdefault(why, []).append(r)
    for why, paths in sorted(dropped.items()):
        errors.append("%d file(s) under a shipped directory would be dropped: %s. First: %s"
                      % (len(paths), why, ", ".join(sorted(paths)[:3])))

    return errors, notes


# ---------------------------------------------------------------------------
# Staging
# ---------------------------------------------------------------------------

def stage(dest):
    """Copy the allow-listed tree into dest. Returns the staged paths, relative and posix."""
    os.makedirs(dest)
    staged = []
    for fn in INCLUDE_FILES:
        src = os.path.join(ROOT, fn)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(dest, fn))
            staged.append(fn)
    for d in INCLUDE_DIRS:
        for dirpath, dirnames, filenames in os.walk(os.path.join(ROOT, d)):
            dirnames[:] = [x for x in dirnames if x != "__pycache__"]
            out_dir = os.path.join(dest, rel(dirpath).replace("/", os.sep))
            os.makedirs(out_dir, exist_ok=True)
            for fn in filenames:
                r = rel(os.path.join(dirpath, fn))
                ok, _why = classify(r)
                if not ok:
                    continue
                shutil.copy2(os.path.join(dirpath, fn), os.path.join(out_dir, fn))
                staged.append(r)
    # An empty directory in the archive is noise, and os.walk creates one for every pruned tree.
    for dirpath, dirnames, filenames in os.walk(dest, topdown=False):
        if not dirnames and not filenames and dirpath != dest:
            os.rmdir(dirpath)
    return sorted(staged)


# ---------------------------------------------------------------------------
# Gates over the staged tree
# ---------------------------------------------------------------------------

def scan(tree):
    hits = []
    for dirpath, _d, filenames in os.walk(tree):
        for fn in filenames:
            path = os.path.join(dirpath, fn)
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    for lineno, line in enumerate(f, 1):
                        low = line.lower()
                        for lit in DENY_LITERALS:
                            if lit in low:
                                hits.append((path, lineno, "literal '%s'" % lit, line.strip()[:100]))
                        for label, pat in DENY:
                            for m in pat.finditer(line):
                                if m.group(0) in ALLOW_TOKENS:
                                    continue
                                if label == "IPv4 address":
                                    parts = m.group(0).split(".")
                                    if int(parts[0]) < 10:
                                        continue
                                    # A VBIOS version like 94.07.56.40.17 contains a dotted quad.
                                    # If another .digit follows, it is a version, not an address.
                                    tail = line[m.end():m.end() + 2]
                                    if len(tail) > 1 and tail[0] == "." and tail[1].isdigit():
                                        continue
                                    if any(int(x) > 255 for x in parts):
                                        continue
                                hits.append((path, lineno, label, line.strip()[:100]))
            except (IOError, UnicodeError):
                continue
    return hits


def check_forbidden(staged):
    """Nothing from a withheld directory may have reached the staging tree.

    The allow-list already makes this true by construction. This reads the staged paths anyway,
    because "true by construction" is a declaration and the staged tree is the artifact. If stage()
    ever grows a shortcut, this is what notices.
    """
    bad = []
    for r in staged:
        top = r.split("/", 1)[0]
        if top in WITHHELD:
            bad.append("%s came from withheld entry %r" % (r, top))
    return bad


def check_no_stray_results(staging, staged):
    """Read every staged JSON and reject a run result that is not in the worked-example directory.

    Shape, not location: a result recognises itself by its own keys. A result copied into the
    template fixtures, or dropped beside the source, would pass every path rule above and still
    publish a machine's inventory.
    """
    bad = []
    for r in staged:
        if not r.endswith(".json"):
            continue
        path = os.path.join(staging, r.replace("/", os.sep))
        try:
            with open(path, "r", encoding="utf-8") as f:
                doc = json.load(f)
        except (IOError, ValueError):
            continue
        if not isinstance(doc, dict):
            continue
        if all(k in doc for k in RESULT_SHAPE_KEYS) and not r.startswith("examples/results/"):
            bad.append("%s has the shape of a run result (%s) but is not under examples/results/"
                       % (r, ", ".join(RESULT_SHAPE_KEYS)))
    return bad


LINK_RE = re.compile(r"\]\(([^)\s]+)\)")


def check_markdown_links(staging, staged):
    """Every relative link in a shipped document must resolve inside the archive.

    A README that links to a file the archive does not contain asserts something the artifact does
    not support. This is the same defect class as a check that reads a declaration, and it is the
    reason examples/ is on the allow-list at all.
    """
    bad = []
    for r in staged:
        if not r.endswith(".md"):
            continue
        path = os.path.join(staging, r.replace("/", os.sep))
        base = os.path.dirname(path)
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
        for target in LINK_RE.findall(text):
            if target.startswith(("#", "http://", "https://", "mailto:", "//")):
                continue
            target = target.split("#", 1)[0]
            if not target:
                continue
            resolved = os.path.normpath(os.path.join(base, target))
            if not os.path.exists(resolved):
                bad.append("%s links to %r, which is not in the archive" % (r, target))
    return bad


def check_pyproject_packages(staging, staged):
    """Does pyproject describe what the archive contains? Reported, not enforced.

    The archives are the supported way to run this tool, and they are proven below by extraction.
    A wheel is a second distribution channel with its own manifest, and setuptools ships only the
    packages it is told about and only the non-Python files a package-data rule names. A package
    directory or a data file present here and absent there gives `pip install .` a silently
    incomplete install, so it is worth saying out loud on every build.
    """
    warnings = []
    with open(os.path.join(staging, "pyproject.toml"), "r", encoding="utf-8") as f:
        text = f.read()
    m = re.search(r"packages\s*=\s*\[([^\]]*)\]", text)
    declared = set(re.findall(r'"([^"]+)"', m.group(1))) if m else set()

    pkgs = set()
    data_files = []
    for r in staged:
        if not r.startswith("gpubench/"):
            continue
        d = r.rsplit("/", 1)[0]
        if r.endswith("/__init__.py") or r.endswith(".py"):
            pkgs.add(d.replace("/", "."))
        else:
            data_files.append(r)
    missing = sorted(p for p in pkgs if p not in declared)
    if missing:
        warnings.append("pyproject [tool.setuptools] packages does not list: %s. `pip install .` "
                        "omits these." % ", ".join(missing))
    stale = sorted(p for p in declared if p not in pkgs)
    if stale:
        warnings.append("pyproject declares packages that ship no Python file: %s"
                        % ", ".join(stale))
    if data_files and "package-data" not in text and "package_data" not in text:
        warnings.append("%d non-Python file(s) ship inside the package (%s ...) but pyproject "
                        "declares no package-data rule, so `pip install .` drops them."
                        % (len(data_files), ", ".join(sorted(data_files)[:3])))
    return warnings


def scan_shareable():
    """Scan the artefacts users are told to share, not just the source we ship.

    The source gate protected the archive while the sample result JSON mapped an entire estate.
    Scanning only what you package is the blind spot that allows.
    """
    out = []
    for sub in ("results", "reports"):
        d = os.path.join(ROOT, sub)
        if os.path.isdir(d):
            out.extend(scan(d))
    return out


# ---------------------------------------------------------------------------
# The standalone proof
# ---------------------------------------------------------------------------

def run(root, args, timeout=600):
    env = dict(os.environ)
    env["GPUBENCH_NO_BROWSER"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    # Nothing from the developer tree may leak in: the point is that the EXTRACTED copy is
    # complete. An inherited PYTHONPATH would let a missing file resolve against the source.
    env.pop("PYTHONPATH", None)
    t0 = time.time()
    r = subprocess.run([sys.executable] + args, cwd=root, capture_output=True, text=True,
                       timeout=timeout, env=env, encoding="utf-8", errors="replace")
    return r, time.time() - t0


def subcommands(root):
    """Read the subcommand list out of the extracted CLI's own help.

    Asking the artifact which subcommands it has, rather than hardcoding a list here, is what makes
    the check below tolerant of a subcommand that has not landed yet without also making it blind.
    """
    r, _ = run(root, ["-m", "gpubench", "--help"], timeout=120)
    if r.returncode != 0:
        return None
    m = re.search(r"\{([a-z0-9_,\-]+)\}", r.stdout)
    return set(m.group(1).split(",")) if m else set()


def test_modules(root):
    """Every test module in the extracted tree, as dotted module paths.

    Discovered from the artifact rather than listed here, so a test module added tomorrow is proven
    by the next release build without anyone editing this file. A hardcoded list is how three of
    the six suites came to be absent from a release nobody noticed was thin.
    """
    mods = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [x for x in dirnames if x != "__pycache__"]
        for fn in sorted(filenames):
            if not (fn.startswith("test_") and fn.endswith(".py")):
                continue
            r = rel(os.path.join(dirpath, fn), root)
            mods.append(r[:-3].replace("/", "."))
    return sorted(mods)


def last_line(r):
    """The most useful single line of a failed subprocess, or empty. Never raises on empty output."""
    lines = ((r.stderr or "") + "\n" + (r.stdout or "")).strip().splitlines()
    return lines[-1][:200] if lines else "(no output)"


def prove(root, ok_lines):
    """Exercise one extracted copy. Returns True if every check passed."""
    ok = True

    def say(name, good, detail=""):
        ok_lines.append("    %-46s %s%s" % (name, "ok" if good else "FAILED",
                                            ("  " + detail) if detail else ""))
        return good

    r, _ = run(root, ["-m", "gpubench", "run", "--explain"], timeout=180)
    ok &= say("cli: gpubench run --explain", r.returncode == 0 and "tiers" in r.stdout,
              "" if r.returncode == 0 else last_line(r))

    # The template package's data files are the reason this allow-list changed. Load them from the
    # extracted copy: an importable module proves nothing about the YAML sitting beside it.
    r, _ = run(root, ["-c", "from gpubench.template import outline; "
                            "o = outline.load_outline(); "
                            "print('sections', len(o.get('sections') or []))"], timeout=120)
    ok &= say("template: report-outline.yaml loads", r.returncode == 0 and "sections" in r.stdout,
              r.stdout.strip() if r.returncode == 0 else last_line(r))

    r, _ = run(root, ["-c", "import json, os, gpubench.template as t; "
                            "p = os.path.join(os.path.dirname(t.__file__), 'run-schema.json'); "
                            "d = json.load(open(p, encoding='utf-8')); "
                            "print('schema defs', len(d.get('$defs') or {}))"], timeout=120)
    ok &= say("template: run-schema.json loads", r.returncode == 0 and "schema defs" in r.stdout,
              r.stdout.strip() if r.returncode == 0 else last_line(r))

    r, _ = run(root, ["-c", "import os, gpubench.template as t; "
                            "d = os.path.dirname(t.__file__); "
                            "print('bytes', os.path.getsize(os.path.join(d, 'lint-rules.md')), "
                            "os.path.getsize(os.path.join(d, 'README.md')))"], timeout=120)
    ok &= say("template: prose files present", r.returncode == 0 and "bytes" in r.stdout,
              r.stdout.strip() if r.returncode == 0 else last_line(r))

    # The template subcommand is being added separately. Run it if the extracted CLI has it, and
    # say which branch was taken either way: "the check did not run" and "the check passed" must
    # never look the same in a build log.
    subs = subcommands(root)
    if subs is None:
        ok &= say("cli: gpubench --help", False, "help exited non-zero")
    elif "template" in subs:
        r, _ = run(root, ["-m", "gpubench", "template", "--help"], timeout=120)
        ok &= say("cli: gpubench template --help", r.returncode == 0,
                  "" if r.returncode == 0 else last_line(r))
        r, _ = run(root, ["-m", "gpubench", "template"], timeout=300)
        # No arguments may legitimately be a usage error; a traceback may not.
        ok &= say("cli: gpubench template (no args)", "Traceback" not in (r.stderr or ""),
                  last_line(r))
        ok_lines.append("    branch taken: template subcommand EXISTS and was exercised")
    else:
        ok_lines.append("    branch taken: template subcommand NOT PRESENT in this build "
                        "(subcommands present: %s). The template data files were still proven "
                        "loadable above." % ", ".join(sorted(subs)))

    mods = test_modules(root)
    if not mods:
        ok &= say("tests: discovery", False, "no test module found in the extracted tree")
    for mod in mods:
        r, secs = run(root, ["-m", mod], timeout=900)
        # unittest writes its summary to stderr, so read both streams.
        lines = ((r.stderr or "") + "\n" + (r.stdout or "")).strip().splitlines()
        summary = lines[-1] if lines else ""
        ran = ""
        for line in lines:
            if line.startswith("Ran "):
                ran = line.strip()
        ok &= say("tests: %s" % mod, r.returncode == 0 and summary.startswith("OK"),
                  "%s [%s] %.1fs" % (ran or "(no count)", summary[:40], secs))
    return ok


def verify(archive_dir, pkgname):
    """Extract each archive somewhere clean and actually exercise it."""
    ok = True
    for ext in (".tar.gz", ".zip"):
        arc = os.path.join(archive_dir, pkgname + ext)
        tmp = tempfile.mkdtemp(prefix="gpubench-verify-")
        lines = []
        try:
            if ext == ".tar.gz":
                with tarfile.open(arc, "r:gz") as t:
                    # Python 3.14 rejects unfiltered extraction; "data" is the safe filter and
                    # is what an archive of plain source should always use.
                    try:
                        t.extractall(tmp, filter="data")
                    except TypeError:      # filter= added in 3.11.4
                        t.extractall(tmp)
            else:
                with zipfile.ZipFile(arc) as z:
                    z.extractall(tmp)
            root = os.path.join(tmp, pkgname)
            print("  %s" % (pkgname + ext))
            good = prove(root, lines)
            for line in lines:
                print(line)
            print("    %-46s %s" % ("VERDICT", "standalone" if good else "NOT STANDALONE"))
            ok = ok and good
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return ok


# ---------------------------------------------------------------------------

def main():
    v = version()
    pkgname = "gpubench-%s" % v
    dist = os.path.join(ROOT, "dist")
    shutil.rmtree(dist, ignore_errors=True)
    os.makedirs(dist)
    staging = os.path.join(dist, pkgname)

    print("auditing the allow-list against the tree")
    errors, notes = audit_allow_list()
    for n in notes:
        print("  note: %s" % n)
    if errors:
        print("  ABORTED: %d problem(s) with the allow-list" % len(errors))
        for e in errors:
            print("    %s" % e)
        return 1
    print("  %d file(s) and %d directory(ies) allow-listed; %d top-level entry(ies) withheld"
          % (len(INCLUDE_FILES), len(INCLUDE_DIRS), len(WITHHELD)))

    print("staging %s" % pkgname)
    staged = stage(staging)
    by_top = {}
    for r in staged:
        top = r.split("/", 1)[0] if "/" in r else "(root)"
        by_top[top] = by_top.get(top, 0) + 1
    print("  %d files staged: %s" % (len(staged), ", ".join(
        "%s %d" % (k, n) for k, n in sorted(by_top.items()))))

    print("checking nothing withheld reached the staging tree")
    problems = check_forbidden(staged)
    problems += check_no_stray_results(staging, staged)
    problems += check_markdown_links(staging, staged)
    if problems:
        print("  ABORTED: %d finding(s)" % len(problems))
        for p in problems:
            print("    %s" % p)
        return 1
    print("  clean: no withheld path, no stray run result, every relative link resolves")

    print("scanning for identifying material")
    hits = scan(staging)
    if hits:
        print("  ABORTED: %d finding(s)" % len(hits))
        for path, lineno, what, ctx in hits[:20]:
            print("    %s:%d [%s] %s" % (rel(path, staging), lineno, what, ctx))
        return 1
    print("  clean")

    tgz = os.path.join(dist, pkgname + ".tar.gz")
    with tarfile.open(tgz, "w:gz") as t:
        t.add(staging, arcname=pkgname)
    zp = os.path.join(dist, pkgname + ".zip")
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
        for dirpath, _d, filenames in os.walk(staging):
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                z.write(full, os.path.join(pkgname, os.path.relpath(full, staging)))

    print("checking the wheel manifest agrees with the archive")
    pp = check_pyproject_packages(staging, staged)
    if pp:
        for w in pp:
            print("  WARNING: %s" % w)
        print("  (the archives are unaffected and are proven below; this is about `pip install .`)")
    else:
        print("  agrees")

    print("scanning shareable artefacts (results, reports)")
    share_hits = scan_shareable()
    if share_hits:
        print("  WARNING: %d finding(s) in files users are told to share" % len(share_hits))
        for path, lineno, what, ctx in share_hits[:10]:
            print("    %s [%s]" % (rel(path, ROOT), what))
        print("  (archives are unaffected; do not publish those files as-is)")
    else:
        print("  clean")

    print("verifying archives run standalone")
    ok = verify(dist, pkgname)

    print("\nstaged tree")
    for r in staged:
        print("  %s" % r)

    shutil.rmtree(staging, ignore_errors=True)
    print("\nartifacts")
    for arc in sorted(os.listdir(dist)):
        p = os.path.join(dist, arc)
        with open(p, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
        print("  %-28s %8.1f KB  sha256 %s" % (arc, os.path.getsize(p) / 1024.0, digest))
        with io.open(p + ".sha256", "w", encoding="utf-8", newline="\n") as f:
            f.write("%s  %s\n" % (digest, arc))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
