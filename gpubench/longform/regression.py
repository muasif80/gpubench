"""Numeric regression diff between two builds of the same report.

A release that claims "no measured value moved" has to prove it, and a human re-reading 900 numbers
cannot. Every defect three rounds of external review found in the report this was written for was
the same thing: a number written into prose by hand, which then drifted from the value it was
supposed to restate. Prose drift is invisible to a reader not holding both numbers at once, and
invisible to a test suite that only checks the generator runs.

So: extract every numeric token from both builds, align them, and print what changed. Every
difference must then be explainable by exactly one line of the changelog. Anything else is a bug
found before shipping rather than after.

Two alignment strategies, because one does not fit both cases:

  PROSE     numbers whose surrounding context carries real words are matched by that context, with
            digits masked so a changed value does not change its own key.
  TABLES    numbers inside a table have other numbers as their context, so they cannot be aligned
            that way. An early version tried and reported a 312-million-percent change that was two
            adjacent columns swapping places. They are compared as a MULTISET instead, which is the
            right comparison for a table anyway: if nothing moved, the bag of values is identical
            regardless of layout.

Reports rather than gates: judgement about which differences are acceptable belongs to a human
reading the changelog beside it.
"""
import argparse
import collections
import io
import os
import re
import sys

TAG = re.compile(r"<[^>]+>")
WS = re.compile(r"\s+")
# A number, with optional thousands separators and decimals, plus a trailing unit-ish token so
# "566 W" and "566 sequences" are distinguishable.
NUM = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?\s*(%|[A-Za-z/]{1,9})?")


def text_of(path):
    raw = io.open(path, encoding="utf-8", errors="replace").read()
    # Drop <style>/<script>/<svg> wholesale: they are full of coordinates that are not report
    # values, and including them buries the real diff in noise.
    for tag in ("style", "script", "svg"):
        raw = re.sub(r"<%s\b.*?</%s>" % (tag, tag), " ", raw, flags=re.S | re.I)
    # Drop the contents navigation. Its numbers are section indices and page numbers, which shift
    # whenever a section is added or the pagination changes. They are real differences but they are
    # STRUCTURAL, and leaving them in swamps the measured-value differences this tool exists to
    # surface -- on one run they accounted for 60 of 98 reported changes.
    raw = re.sub(r'<nav class="toc">.*?</nav>', " ", raw, flags=re.S | re.I)
    # Same for the leading index on each section heading.
    raw = re.sub(r'(<h2[^>]*>)\s*\d+\.\s*', r"\1", raw, flags=re.I)
    return WS.sub(" ", TAG.sub(" ", raw)).strip()


def numbers(text, ctx=28):
    """Every number with a normalised context key, so the same fact aligns across two builds."""
    out = []
    for m in NUM.finditer(text):
        whole, frac, unit = m.group(1), m.group(2), (m.group(3) or "").strip()
        val = whole.replace(",", "") + ("." + frac if frac else "")
        try:
            f = float(val)
        except ValueError:
            continue
        before = text[max(0, m.start() - ctx):m.start()]
        after = text[m.end():m.end() + ctx]
        # The key is the surrounding words with digits stripped, so a changed value does not
        # change its own key and the two builds still line up.
        key = WS.sub(" ", re.sub(r"[\d.,]+", "#", before + "|" + after)).strip().lower()
        out.append({"value": f, "unit": unit, "key": key,
                    "shown": m.group(0).strip(), "context": (before + "[" + m.group(0).strip()
                                                             + "]" + after).strip()})
    return out


WORDY = re.compile(r"[A-Za-z]")


def is_alignable(item):
    """Can this number be matched to its counterpart in the other build by its context?

    Inside a dense table the characters around a cell are other cells, so the masked context key is
    all separators and hashes, and several unrelated cells collide on it. Aligning those
    positionally produces spectacular false positives: an early run of this script reported a
    312-million-percent change that was really two adjacent table columns swapping places.

    So a number is alignable only if its surrounding context carries real words. Numbers that are
    not get compared as a MULTISET instead, which is the correct comparison for a table anyway. If
    no value moved, the bag of values is identical regardless of layout.
    """
    return len(WORDY.findall(item["key"])) >= 6


def index(items):
    d = collections.defaultdict(list)
    for it in items:
        d[(it["key"], it["unit"])].append(it)
    return d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("old")
    ap.add_argument("new")
    ap.add_argument("--context", type=int, default=28)
    ap.add_argument("--quiet-unchanged", action="store_true", default=True)
    a = ap.parse_args()

    for p in (a.old, a.new):
        if not os.path.exists(p):
            sys.exit("missing: %s" % p)

    old_all = numbers(text_of(a.old), a.context)
    new_all = numbers(text_of(a.new), a.context)
    old_items = [i for i in old_all if is_alignable(i)]
    new_items = [i for i in new_all if is_alignable(i)]
    old_cells = [i for i in old_all if not is_alignable(i)]
    new_cells = [i for i in new_all if not is_alignable(i)]
    oi, ni = index(old_items), index(new_items)

    # Table interiors, as a multiset. A value that disappears from this bag genuinely changed or
    # moved out of the document; layout churn alone cannot alter it.
    ob = collections.Counter(round(i["value"], 6) for i in old_cells)
    nb = collections.Counter(round(i["value"], 6) for i in new_cells)
    cell_gone = sorted((ob - nb).elements())
    cell_new = sorted((nb - ob).elements())

    changed, added, removed, same = [], [], [], 0
    for k in sorted(set(oi) | set(ni)):
        ov, nv = oi.get(k, []), ni.get(k, [])
        for i in range(max(len(ov), len(nv))):
            o = ov[i] if i < len(ov) else None
            n = nv[i] if i < len(nv) else None
            if o and n:
                if abs(o["value"] - n["value"]) < 1e-12:
                    same += 1
                else:
                    changed.append((o, n))
            elif n:
                added.append(n)
            else:
                removed.append(o)

    print("=" * 78)
    print("NUMERIC DIFF   %s -> %s" % (os.path.basename(a.old), os.path.basename(a.new)))
    print("=" * 78)
    print("prose numbers, old   : %d   (alignable by surrounding words)" % len(old_items))
    print("prose numbers, new   : %d" % len(new_items))
    print("table cells, old     : %d   (compared as a multiset)" % len(old_cells))
    print("table cells, new     : %d" % len(new_cells))
    print("identical            : %d" % same)
    print("CHANGED              : %d" % len(changed))
    print("added (new prose)    : %d" % len(added))
    print("removed (cut prose)  : %d" % len(removed))

    if changed:
        print("\n" + "-" * 78)
        print("CHANGED VALUES -- every one must be explained by exactly one changelog line")
        print("-" * 78)
        for o, n in sorted(changed, key=lambda p: -abs(p[1]["value"] - p[0]["value"])):
            pct = ((n["value"] - o["value"]) / o["value"] * 100.0) if o["value"] else float("inf")
            print("\n  %s  ->  %s   (%+.2f%%)" % (o["shown"], n["shown"], pct))
            print("    old: ...%s..." % o["context"])
            print("    new: ...%s..." % n["context"])

    if added:
        print("\n" + "-" * 78)
        print("ADDED (%d) -- expected where the release adds sections or sentences" % len(added))
        print("-" * 78)
        for n in added[:80]:
            print("  + %-12s ...%s..." % (n["shown"], n["context"][:96]))
        if len(added) > 80:
            print("  ... and %d more" % (len(added) - 80))

    if removed:
        print("\n" + "-" * 78)
        print("REMOVED (%d) -- expected where the release deletes or rewords prose" % len(removed))
        print("-" * 78)
        for o in removed[:80]:
            print("  - %-12s ...%s..." % (o["shown"], o["context"][:96]))
        if len(removed) > 80:
            print("  ... and %d more" % (len(removed) - 80))

    print("\n" + "-" * 78)
    print("TABLE-CELL MULTISET: values present in one build and not the other")
    print("-" * 78)
    print("  only in old (%d): %s" % (len(cell_gone), cell_gone[:40] if cell_gone else "none"))
    print("  only in new (%d): %s" % (len(cell_new), cell_new[:40] if cell_new else "none"))
    if not cell_gone and not cell_new:
        print("  -> every table value in the old build is still present in the new one")

    print("\n" + "=" * 78)
    print("Note on alignment: numbers are matched by their surrounding words with digits masked.")
    print("Rewording a sentence therefore shows up as one REMOVED plus one ADDED rather than as a")
    print("CHANGED entry. Read the added/removed lists for reworded prose; read CHANGED for values")
    print("that moved in place, which is where a regression would hide.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
