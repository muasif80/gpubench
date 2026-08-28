"""Read a document's own title out of its markup. No third-party dependency, on purpose.

This lived in docx_export, which imports python-docx at module scope and builds RGBColor constants
at import time. So a rule that is pure text handling could not be exercised without the .docx
writer installed, and the test asserting it "is testable without python-docx installed" was not:
on a clean runner it raised ModuleNotFoundError before reaching a single assertion.

Splitting it out costs one small module and makes that sentence true. docx_export re-exports these
names, so every existing import keeps working.
"""
import re
from html import unescape

TITLE_TAG = re.compile(r"(?is)<title\b[^>]*>(.*?)</title>")
H1_TAG = re.compile(r"(?is)<h1\b[^>]*>(.*?)</h1>")


def document_title(html, fallback=""):
    """The document's own title, from its <title> or its <h1>. Never this module's opinion of it.

    WHAT WAS WRONG. main() wrote one particular benchmark report's title and subject into the core
    properties of whatever it was pointed at. Every other document exported through it therefore
    described itself, in the Word properties pane and in every search index that reads them, as
    that benchmark report. A converter is a converter: the only thing it knows about the document
    is what the document says.
    """
    for pattern in (TITLE_TAG, H1_TAG):
        m = pattern.search(html or "")
        if m:
            text = re.sub(r"\s+", " ", re.sub(r"(?s)<[^>]+>", "", m.group(1))).strip()
            text = unescape(text)
            if text:
                return text
    return fallback
