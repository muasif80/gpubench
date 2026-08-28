"""Header repetition is asserted against the SHIPPED .docx, never against the calling code.

The first attempt at this feature set `row.repeat_as_header_row = True`. python-docx 1.2.0 has no
such property, so the assignment silently created a Python attribute, emitted nothing, and changed
zero of 64 tables while every build log said success. Reading the generator would not have caught
it. Unzipping the package and counting `w:tblHeader` did.

So these tests open the produced file and look at the XML Word will actually read. A test that
called `repeat_header()` and asserted it had been called would reproduce the original bug exactly.
"""
import io
import os
import shutil
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout

# python-docx is the one optional dependency in this project, and CI deliberately installs nothing,
# because "gpubench needs no install" is itself one of the claims under test. So this suite skips
# rather than failing when it is absent, which is the pattern test_gate.py already uses for
# HAVE_PYTHON_DOCX and HAVE_FITZ. Buying a green run with a pip install would retire the check.
try:
    import docx  # noqa: F401
    HAVE_PYTHON_DOCX = True
except ImportError:
    HAVE_PYTHON_DOCX = False


@unittest.skipUnless(HAVE_PYTHON_DOCX, "python-docx is not installed")
class HeaderRepeatsInThePackage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gpubench-hdr-")

    def tearDown(self):
        shutil.rmtree(getattr(self, "tmp", ""), ignore_errors=True)

    def build(self, html):
        from gpubench.longform import docx_export

        src = os.path.join(self.tmp, "doc.html")
        with io.open(src, "w", encoding="utf-8", newline="\n") as f:
            f.write(html)
        buf = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(buf):
            docx_export.main([src])
        with zipfile.ZipFile(os.path.splitext(src)[0] + ".docx") as zf:
            return zf.read("word/document.xml").decode("utf-8")

    TABLE = ("<html><head><title>T</title></head><body><h1>T</h1>"
             "<table><tr><th>Metric</th><th>Value</th></tr>"
             "%s</table></body></html>")

    def test_a_table_with_a_header_row_emits_tblHeader(self):
        rows = "".join("<tr><td>row %d</td><td>%d</td></tr>" % (i, i) for i in range(40))
        xml = self.build(self.TABLE % rows)
        self.assertIn("tblHeader", xml,
                      "the shipped package carries no w:tblHeader, so the header will not repeat "
                      "across the page break in a 40-row table")

    def test_one_tblHeader_per_table_not_per_row(self):
        """Marking every row as a header repeats the whole table, which is worse than not fixing it."""
        rows = "".join("<tr><td>row %d</td><td>%d</td></tr>" % (i, i) for i in range(30))
        two = ("<html><head><title>T</title></head><body><h1>T</h1>"
               "<table><tr><th>A</th><th>B</th></tr>%s</table>"
               "<p>between</p>"
               "<table><tr><th>C</th><th>D</th></tr>%s</table>"
               "</body></html>" % (rows, rows))
        xml = self.build(two)
        self.assertEqual(xml.count("tblHeader"), 2,
                         "expected exactly one marked header row per table, got %d"
                         % xml.count("tblHeader"))

    def test_a_headerless_table_is_not_marked(self):
        """The report has one such table. Marking its first data row would repeat a data row."""
        body = ("<html><head><title>T</title></head><body><h1>T</h1>"
                "<table><tr><td>only</td><td>data</td></tr>"
                "<tr><td>more</td><td>data</td></tr></table></body></html>")
        xml = self.build(body)
        self.assertEqual(xml.count("tblHeader"), 0,
                         "a table with no <th> row must not have a data row marked as its header")


if __name__ == "__main__":
    unittest.main()
