"""A tiny, dependency-free reader for the YAML subset ``report-outline.yaml`` uses.

Why this file exists
--------------------
The template is standard-library only (see ``template/__init__.py``). A build must not
be able to fail, or a lint rule to be skipped, because a YAML library was missing or
changed behaviour. The manifest is written in a deliberately small subset of YAML so
that a reader for it fits in one file and can be tested.

The subset, exactly
-------------------
Supported::

    # full-line comments, and trailing comments outside quotes
    key: value                  scalar mapping entry
    key:                        mapping entry whose value is a nested block
      nested: value
    - value                     sequence item holding a scalar
    - key: value                sequence item holding a mapping
      other: value              ... continued at the item's body indent

    scalars:  "double quoted"  'single quoted'  null  ~  true  false  12  1.5  bare text

Deliberately NOT supported, because the manifest does not use them: anchors and
aliases (``&``/``*``), block scalars (``|``, ``>``), flow collections (``{}``, ``[]``),
multiple documents (``---``), tags (``!!str``), complex keys, and tabs for indentation.
Any of those raises :class:`OutlineError` rather than being silently mis-parsed. A
reader that guesses is worse than a reader that refuses: a mis-parsed manifest would
make a lint rule pass because it did not look, which is the defect class this whole
template exists to prevent.

Booleans and integers are converted; note that the manifest itself quotes some of its
booleans ("true"), and this reader preserves that distinction rather than second-guessing
the author.
"""

from __future__ import annotations

import os
import re

__all__ = ["OutlineError", "loads", "load", "load_outline", "sections_by_id", "invariants"]


class OutlineError(Exception):
    """Raised when the input uses YAML outside the supported subset, or is malformed."""


_UNSUPPORTED = (
    (re.compile(r"^\s*---\s*$"), "multi-document markers (---)"),
    (re.compile(r"^\s*\.\.\.\s*$"), "document end markers (...)"),
    (re.compile(r":\s*[|>][-+]?\s*$"), "block scalars (| and >)"),
    (re.compile(r"(^\s*-?\s*|:\s+)&\S+"), "anchors (&name)"),
    (re.compile(r"(^\s*-?\s*|:\s+)\*\S+"), "aliases (*name)"),
    (re.compile(r"^\s*[^#\s][^:]*:\s*[\[{]"), "flow collections ([] and {})"),
    (re.compile(r"^\s*!!"), "tags (!!type)"),
)


class _Tok:
    __slots__ = ("indent", "is_item", "content", "lineno")

    def __init__(self, indent: int, is_item: bool, content: str, lineno: int) -> None:
        self.indent = indent
        self.is_item = is_item
        self.content = content
        self.lineno = lineno

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "_Tok(%d,%s,%r,L%d)" % (self.indent, self.is_item, self.content, self.lineno)


def _strip_comment(text: str) -> str:
    """Remove a trailing comment, respecting quotes. A '#' inside quotes is data."""
    out = []
    quote = None
    i = 0
    while i < len(text):
        ch = text[i]
        if quote:
            out.append(ch)
            if ch == "\\" and i + 1 < len(text):
                out.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
        else:
            if ch in "\"'":
                quote = ch
                out.append(ch)
            elif ch == "#":
                # A comment only starts at the beginning of the line or after whitespace.
                if not out or out[-1].isspace():
                    break
                out.append(ch)
            else:
                out.append(ch)
        i += 1
    if quote:
        raise OutlineError("unterminated quoted scalar: %s" % text.strip())
    return "".join(out).rstrip()


def _tokenize(text: str) -> list[_Tok]:
    toks: list[_Tok] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        if "\t" in raw[: len(raw) - len(raw.lstrip())]:
            raise OutlineError("line %d: tab used for indentation" % lineno)
        for pattern, what in _UNSUPPORTED:
            if pattern.search(raw):
                raise OutlineError("line %d: unsupported YAML feature: %s" % (lineno, what))
        body = _strip_comment(raw)
        if not body.strip():
            continue
        indent = len(body) - len(body.lstrip(" "))
        content = body.strip()
        if content.startswith("- "):
            toks.append(_Tok(indent, True, content[2:].strip(), lineno))
        elif content == "-":
            toks.append(_Tok(indent, True, "", lineno))
        else:
            toks.append(_Tok(indent, False, content, lineno))
    return toks


_KEY_RE = re.compile(r"^(?P<key>[^:\s][^:]*?)\s*:(?:\s+(?P<value>.*))?$")


def _scalar(raw: str, lineno: int):
    text = raw.strip()
    if text == "":
        return None
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        inner = text[1:-1]
        if text[0] == '"':
            inner = (
                inner.replace("\\n", "\n")
                .replace('\\"', '"')
                .replace("\\t", "\t")
                .replace("\\\\", "\\")
            )
        return inner
    low = text.lower()
    if low in ("null", "~"):
        return None
    if low == "true":
        return True
    if low == "false":
        return False
    if re.fullmatch(r"[+-]?\d+", text):
        return int(text)
    if re.fullmatch(r"[+-]?(\d+\.\d*|\.\d+)([eE][+-]?\d+)?", text):
        return float(text)
    if re.fullmatch(r"[+-]?\d+[eE][+-]?\d+", text):
        return float(text)
    return text


def _parse_block(toks: list[_Tok], i: int, indent: int):
    """Parse the block of tokens at ``indent`` starting at ``i``. Returns (value, next_i)."""
    if i >= len(toks):
        return None, i
    if toks[i].is_item:
        return _parse_seq(toks, i, indent)
    return _parse_map(toks, i, indent)


def _parse_seq(toks: list[_Tok], i: int, indent: int):
    items = []
    while i < len(toks) and toks[i].indent == indent and toks[i].is_item:
        tok = toks[i]
        body_indent = indent + 2
        # Collect the tokens belonging to this item: everything more indented than the dash.
        j = i + 1
        while j < len(toks) and toks[j].indent > indent:
            j += 1
        if tok.content == "":
            value, _ = _parse_block(toks[i + 1 : j], 0, toks[i + 1].indent) if j > i + 1 else (None, 0)
            items.append(value)
            i = j
            continue
        m = _KEY_RE.match(tok.content)
        if m and not _looks_like_plain_scalar_with_colon(tok.content):
            sub = [_Tok(body_indent, False, tok.content, tok.lineno)]
            sub.extend(_Tok(t.indent, t.is_item, t.content, t.lineno) for t in toks[i + 1 : j])
            value, _ = _parse_map(sub, 0, body_indent)
            items.append(value)
        else:
            if j > i + 1:
                raise OutlineError(
                    "line %d: sequence item is a scalar but has an indented block under it"
                    % tok.lineno
                )
            items.append(_scalar(tok.content, tok.lineno))
        i = j
    return items, i


def _looks_like_plain_scalar_with_colon(content: str) -> bool:
    """Distinguish ``- key: value`` from ``- "a sentence: with a colon"``."""
    stripped = content.strip()
    if stripped[:1] in "\"'":
        return True
    m = _KEY_RE.match(stripped)
    if not m:
        return True
    key = m.group("key")
    return bool(re.search(r"[\s]", key.strip())) and not re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_.\- ]*", key.strip()
    )


def _parse_map(toks: list[_Tok], i: int, indent: int):
    out: dict = {}
    while i < len(toks) and toks[i].indent == indent and not toks[i].is_item:
        tok = toks[i]
        m = _KEY_RE.match(tok.content)
        if not m:
            raise OutlineError("line %d: not a mapping entry: %r" % (tok.lineno, tok.content))
        key = m.group("key").strip()
        if len(key) >= 2 and key[0] == key[-1] and key[0] in "\"'":
            key = key[1:-1]
        raw_value = m.group("value")
        if raw_value is not None and raw_value.strip() != "":
            out[key] = _scalar(raw_value, tok.lineno)
            i += 1
            continue
        # Nested block, or an empty value.
        j = i + 1
        if j < len(toks) and (
            toks[j].indent > indent or (toks[j].indent == indent and toks[j].is_item)
        ):
            child_indent = toks[j].indent
            end = j
            while end < len(toks) and (
                toks[end].indent > indent
                or (toks[end].indent == indent and toks[end].is_item)
            ):
                end += 1
            value, _ = _parse_block(toks[j:end], 0, child_indent)
            out[key] = value
            i = end
        else:
            out[key] = None
            i = j
    return out, i


def loads(text: str):
    """Parse a YAML string in the supported subset. Returns a dict or a list."""
    toks = _tokenize(text)
    if not toks:
        return {}
    base = toks[0].indent
    for t in toks:
        if t.indent < base:
            raise OutlineError("line %d: dedent below the document's first indent" % t.lineno)
    value, next_i = _parse_block(toks, 0, base)
    if next_i != len(toks):
        raise OutlineError(
            "line %d: trailing content the parser could not attach (indentation error?)"
            % toks[next_i].lineno
        )
    return value


def load(path: str):
    """Parse a YAML file in the supported subset."""
    with open(path, "r", encoding="utf-8") as fh:
        return loads(fh.read())


def default_outline_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "report-outline.yaml")


def load_outline(path: str | None = None) -> dict:
    """Load ``report-outline.yaml`` and check the shape the linter relies on."""
    doc = load(path or default_outline_path())
    if not isinstance(doc, dict):
        raise OutlineError("outline must be a mapping at the top level")
    for key in ("template", "sections", "universal_invariants"):
        if key not in doc:
            raise OutlineError("outline is missing required top-level key %r" % key)
    if not isinstance(doc["sections"], list) or not doc["sections"]:
        raise OutlineError("outline sections must be a non-empty list")
    for entry in doc["sections"]:
        if not isinstance(entry, dict) or "id" not in entry:
            raise OutlineError("every outline section needs an id")
    return doc


def sections_by_id(outline: dict) -> dict:
    """Map section id -> section entry, including archetype items keyed by their own id."""
    out = {}
    for entry in outline.get("sections") or []:
        out[entry["id"]] = entry
    arch = (outline.get("archetypes") or {}).get("items") or []
    for entry in arch:
        out[entry["id"]] = entry
    return out


def invariants(outline: dict) -> list:
    """Every invariant in the manifest, universal and per-section, as flat records."""
    rows = []
    for inv in (outline.get("universal_invariants") or {}).get("invariants") or []:
        rows.append({"owner": "universal", **inv})
    for entry in outline.get("sections") or []:
        for inv in entry.get("invariants") or []:
            rows.append({"owner": entry["id"], **inv})
    for entry in (outline.get("archetypes") or {}).get("items") or []:
        for inv in entry.get("invariants") or []:
            rows.append({"owner": "archetype:" + entry["id"], **inv})
    return rows


if __name__ == "__main__":  # pragma: no cover - manual smoke check
    import sys

    doc = load_outline(sys.argv[1] if len(sys.argv) > 1 else None)
    print("template version:", (doc.get("template") or {}).get("version"))
    print("sections:", len(doc.get("sections") or []))
    print("archetypes:", len((doc.get("archetypes") or {}).get("items") or []))
    print("invariants:", len(invariants(doc)))
