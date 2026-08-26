#!/usr/bin/env python3
"""Build the standalone distribution archives, and refuse to ship anything identifying.

    python tools/make_dist.py

Writes dist/gpubench-<version>.tar.gz and dist/gpubench-<version>.zip.

Two rules this enforces rather than trusts:
  1. Only an explicit allow-list of paths is packaged. Result and report files are never shipped:
     a result can carry a target host, a board model and a driver serial.
  2. The staged tree is scanned for identifying material before the archives are written, and the
     build aborts on any hit.

It then extracts each archive to a temporary directory and runs the CLI from it, so "standalone"
is a verified property rather than a claim.
"""
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Explicit allow-list. Anything not named here does not ship.
INCLUDE_FILES = ["README.md", "LICENSE", "NOTICE", "CHANGELOG.md", "pyproject.toml"]
# Tests ship. This tool's entire value proposition is that its numbers can be checked, and a
# measurement tool whose tests are withheld is asking to be trusted rather than verified.
INCLUDE_DIRS = ["gpubench", "tests"]
EXCLUDE_PATTERNS = (".pyc", "__pycache__", ".json", ".html", ".log")

# ...except under a test fixtures directory, where .json and .html ARE the payload. The broad
# exclusion above exists to keep RESULT and REPORT files out of a release; applied to fixtures
# it shipped the lint tests without the data they need, so all 87 failed from a clean
# extraction while passing in the developer tree. A test suite the recipient cannot run is
# worse than none, because a green suite here implies a guarantee they do not have.
EXCLUDE_EXEMPT_DIRS = ("tests/fixtures", "tests\\fixtures")

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
DENY_LITERALS = ["softoo", "ecovis", "yanipro", "auditvare", "auditease", "aimachine"]

# Version numbers and the placeholder in usage examples are not addresses.
ALLOW_TOKENS = {"user@host", "0.0.0.0", "127.0.0.1"}


def version():
    with open(os.path.join(ROOT, "pyproject.toml"), "r", encoding="utf-8") as f:
        m = re.search(r'^version\s*=\s*"([^"]+)"', f.read(), re.M)
    return m.group(1) if m else "0.0.0"


def stage(dest):
    os.makedirs(dest)
    for fn in INCLUDE_FILES:
        src = os.path.join(ROOT, fn)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(dest, fn))
    for d in INCLUDE_DIRS:
        for dirpath, dirnames, filenames in os.walk(os.path.join(ROOT, d)):
            dirnames[:] = [x for x in dirnames if x != "__pycache__"]
            rel = os.path.relpath(dirpath, ROOT)
            out_dir = os.path.join(dest, rel)
            os.makedirs(out_dir, exist_ok=True)
            exempt = any(x in dirpath for x in EXCLUDE_EXEMPT_DIRS)
            for fn in filenames:
                if fn.endswith(".pyc") or "__pycache__" in fn:
                    continue
                if not exempt and any(fn.endswith(p) or p in fn for p in EXCLUDE_PATTERNS):
                    continue
                shutil.copy2(os.path.join(dirpath, fn), os.path.join(out_dir, fn))


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


def verify(archive_dir, pkgname):
    """Extract each archive somewhere clean and actually run the CLI from it."""
    ok = True
    for ext in (".tar.gz", ".zip"):
        arc = os.path.join(archive_dir, pkgname + ext)
        tmp = tempfile.mkdtemp(prefix="gpubench-verify-")
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
            r = subprocess.run([sys.executable, "-m", "gpubench", "run", "--explain"],
                               cwd=root, capture_output=True, text=True, timeout=180)
            good = r.returncode == 0 and "tiers" in r.stdout
            print("  %-8s extracted and CLI runs: %s" % (ext, "yes" if good else "NO"))
            if not good:
                print("     stdout: %s" % r.stdout[:300])
                print("     stderr: %s" % r.stderr[:300])
                ok = False
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return ok


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


def main():
    v = version()
    pkgname = "gpubench-%s" % v
    dist = os.path.join(ROOT, "dist")
    shutil.rmtree(dist, ignore_errors=True)
    os.makedirs(dist)
    staging = os.path.join(dist, pkgname)

    print("staging %s" % pkgname)
    stage(staging)
    files = sum(len(f) for _d, _s, f in os.walk(staging))
    print("  %d files staged" % files)

    print("scanning for identifying material")
    hits = scan(staging)
    if hits:
        print("  ABORTED: %d finding(s)" % len(hits))
        for path, lineno, what, ctx in hits[:20]:
            print("    %s:%d [%s] %s" % (os.path.relpath(path, staging), lineno, what, ctx))
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

    print("scanning shareable artefacts (results, reports)")
    share_hits = scan_shareable()
    if share_hits:
        print("  WARNING: %d finding(s) in files users are told to share" % len(share_hits))
        for path, lineno, what, ctx in share_hits[:10]:
            print("    %s [%s]" % (os.path.relpath(path, ROOT), what))
        print("  (archives are unaffected; do not publish those files as-is)")
    else:
        print("  clean")

    print("verifying archives run standalone")
    ok = verify(dist, pkgname)

    shutil.rmtree(staging, ignore_errors=True)
    import hashlib
    print("\nartifacts")
    for arc in sorted(os.listdir(dist)):
        p = os.path.join(dist, arc)
        with open(p, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
        print("  %-28s %7.1f KB  sha256 %s" % (arc, os.path.getsize(p) / 1024.0, digest))
        with open(p + ".sha256", "w", encoding="utf-8") as f:
            f.write("%s  %s\n" % (digest, arc))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
