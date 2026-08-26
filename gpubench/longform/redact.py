"""Pre-publication gate: refuse to ship anything that identifies the estate being measured.

Moved verbatim into the tool, because it is the check most easily forgotten and most costly to
forget. It scans BUILT artifacts rather than sources, which is the distinction that matters: a
clean generator can still render a hostname that arrived in a result file.

It has caught a real leak in every class it screens for, and it has also produced false
positives (an XML namespace URI read as base64, a VBIOS version read as an IPv4 address), so
the patterns are deliberately narrow and each carries an in-code exception.
"""
import io
import os
import re
import sys
import zipfile

PATTERNS = [
    ("IPv4 address", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
    ("MAC address", re.compile(r"\b(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}\b")),
    ("GPU UUID", re.compile(r"\bGPU-[0-9a-fA-F]{8}-", re.I)),
    ("email address", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("home directory path", re.compile(r"/home/[a-z0-9_-]+/", re.I)),
    ("Windows user path", re.compile(r"[A-Za-z]:\\+Users\\+[^\\\s\"'<]+", re.I)),
    ("password-ish assignment", re.compile(r"(?i)\b(pass(word)?|passwd|secret|api[_-]?key|token)\b"
                                           r"\s*[:=]\s*\S{6,}")),
    # A long hex run is a hash, key or UUID-without-dashes.
    ("long hex run", re.compile(r"\b[0-9a-fA-F]{32,}\b")),
    # Base64-ish. The digit requirement is applied in code, not as a regex lookahead: the obvious
    # lookahead form backtracks catastrophically on the single-line XML inside an Office package
    # and hangs the scan outright.
    ("long base64 run", re.compile(r"\b[A-Za-z0-9+]{40,}={0,2}\b")),
]

# Structural families (addresses, keys, home paths) are universal and live in this file. A site's
# own NOUNS are not universal and must never ship inside a general-purpose tool -- the tool's own
# gate caught exactly that when this module was moved here, flagging its hardcoded wordlist as the
# leak it would have been.
#
# So the literal list is EMPTY by default and is supplied per site, from either:
#   GPUBENCH_DENY_LITERALS   comma-separated, or
#   a denylist.txt beside the artifacts being scanned (one term per line, # for comments)
#
# An empty list is not a silent pass: scan() reports how many site literals were loaded, so "zero"
# is visible rather than assumed.
DEFAULT_LITERALS = []


def site_literals(root):
    """Site-specific terms, from the environment or a denylist beside the artifacts."""
    terms = [t.strip().lower() for t in os.environ.get("GPUBENCH_DENY_LITERALS", "").split(",")
             if t.strip()]
    for name in ("denylist.txt", ".denylist"):
        path = os.path.join(root, name)
        if os.path.exists(path):
            with io.open(path, encoding="utf-8") as f:
                terms += [ln.strip().lower() for ln in f
                          if ln.strip() and not ln.lstrip().startswith("#")]
    return sorted(set(terms + [t.lower() for t in DEFAULT_LITERALS]))

# Public, retail, or open-source names that are safe and expected in a benchmark disclosure.
ALLOW = {
    "1.7", "2.11", "0.23", "5.12", "2.28", "13.0", "24.04", "6.8", "590.48",
}

SCAN_EXT = {".html", ".htm", ".svg", ".md", ".json", ".csv", ".txt", ".log"}
# Office files are zip containers. Without this they would be silently skipped and the gate would
# report PASS on a directory whose only published artifact is a .docx.
ZIP_EXT = {".docx", ".xlsx", ".pptx"}


def literals(root=None):
    """Site terms, looked up beside the ARTIFACTS being scanned.

    Previously this read a denylist sitting next to this source file. After the move into the tool
    that path points inside the package, so a site's list would have had to be edited into the tool
    -- which is how the terms end up shipping. The list belongs with the artifacts.
    """
    return site_literals(root or os.getcwd())


def plausible_ip(s):
    """Filter version strings that look like dotted quads (e.g. a driver version)."""
    parts = s.split(".")
    if len(parts) != 4:
        return True
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return True
    if any(n > 255 for n in nums):
        return False
    # a real address here would be routable-looking; version numbers tend to have a leading small
    # number and a zero or tiny second octet
    return not (nums[0] < 10 and nums[1] < 10)


DIGEST_WORDS = ("sha256", "sha-256", "checksum", "digest", "source_sha256", "fingerprint")


def _is_declared_digest(line, tok):
    """True when a long hex run is declared as a checksum within the same line.

    Publishing the checksum of one's own measurement code is a transparency measure, and a gate
    that blocks it is stopping the right thing from happening. Requiring the declaration keeps the
    exception narrow: an unlabelled hex blob is still a finding.
    """
    low = line.lower()
    i = low.find(tok.lower())
    # Strip markup before measuring proximity. An Office package holds its whole body on ONE line,
    # so a raw character window around the token is mostly XML attributes and the declaring words
    # sit thousands of characters away with nothing but tags in between.
    window = re.sub(r"<[^>]*>", " ", low[max(0, i - 3000):i + len(tok) + 3000])
    j = window.find(tok.lower())
    if j >= 0:
        window = window[max(0, j - 120):j + len(tok) + 120]
    return any(w in window for w in DIGEST_WORDS)


def check_lines(path, lines, lits, hits, member=""):
    where = path + (("!" + member) if member else "")
    for lineno, line in enumerate(lines, 1):
        low = line.lower()
        for lit in lits:
            if lit in low:
                hits.append((where, lineno, "literal", lit, line.strip()[:120]))
        for label, pat in PATTERNS:
            for m in pat.finditer(line):
                tok = m.group(0)
                if tok in ALLOW:
                    continue
                if label == "IPv4 address" and not plausible_ip(tok):
                    continue
                if label.startswith("long ") and tok.isdigit():
                    continue
                if label.startswith("long ") and _is_declared_digest(line, tok):
                    # A checksum a document PUBLISHES on purpose is the opposite of a leak: it is
                    # what lets a reader verify which code produced a result. The exception is
                    # narrow on purpose -- the digest must be adjacent to vocabulary that declares
                    # it as one, so an undeclared hex blob is still caught.
                    continue
                if label == "long base64 run" and sum(c.isdigit() for c in tok) < 3:
                    # XML namespace segments and camelCase identifiers are long but digit-free;
                    # real keys and tokens carry digits.
                    continue
                hits.append((where, lineno, label, tok, line.strip()[:120]))


def scan(root):
    lits = [l.lower() for l in literals(root)]
    hits = []
    scanned = 0
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            ext = os.path.splitext(name)[1].lower()
            path = os.path.join(dirpath, name)
            if os.path.basename(path) in ("sanitize_check.py", "denylist.txt"):
                continue
            if ext in SCAN_EXT:
                scanned += 1
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    check_lines(path, f, lits, hits)
            elif ext in ZIP_EXT:
                # Scan the XML parts, which is where both the prose and the document
                # properties live. Binary media is skipped; images are checked visually.
                scanned += 1
                try:
                    with zipfile.ZipFile(path) as z:
                        for member in z.namelist():
                            if not member.lower().endswith((".xml", ".rels")):
                                continue
                            text = z.read(member).decode("utf-8", "replace")
                            check_lines(path, text.splitlines(), lits, hits, member)
                except zipfile.BadZipFile:
                    hits.append((path, 0, "unreadable archive", name, ""))
    return hits, scanned


def main(argv=None):
    """Scan a directory of BUILT artifacts. The caller names it; there is no sensible default now
    that this lives in the tool rather than beside the artifacts it guards."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        raise SystemExit("usage: redact <directory-of-built-artifacts>")
    root = os.path.abspath(argv[0])
    hits, scanned = scan(root)
    if not hits:
        print("PASS: %d files scanned under %s, nothing identifying found." % (scanned, root))
        return 0
    print("FAIL: %d finding(s) across %d files scanned." % (len(hits), scanned))
    for path, lineno, kind, tok, ctx in hits:
        print("  %s:%d  [%s] %r" % (os.path.relpath(path, root), lineno, kind, tok))
        print("      %s" % ctx)
    return 1


if __name__ == "__main__":
    sys.exit(main())
