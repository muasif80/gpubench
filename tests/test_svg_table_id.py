"""A declared table has to be findable in the rendered document, or its cells are never checked.

F4 verifies that every numeral printed in a table traces to a declared cell of THAT table. It finds
a table by an id on the <table>, or by a <figure> around it carrying the id. `svg.table()` emitted
neither, so 20 declared tables came back as "could not be found in the rendered document, so their
cells were not checked against what shipped" -- the declaration existed, the check had nothing to
read, and the build still passed.

These tests assert against the gate's own locating regexes rather than against the presence of an
attribute, because what matters is not that an id was written but that the check can find it.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gpubench.longform.svg import table  # noqa: E402
from gpubench.verify import TABLE_BLOCK, TABLE_ID  # noqa: E402


def locate(html):
    """The ids F4 would resolve out of this markup."""
    found = []
    for block in TABLE_BLOCK.finditer(html):
        m = TABLE_ID.search(block.group(1))
        if m:
            found.append(m.group(1))
    return found


class TheGateCanFindATableThatDeclaresAnId(unittest.TestCase):
    def test_a_table_rendered_with_a_tid_is_located(self):
        html = table(["Metric", "Value"], [["prefill", "2,187.8"]], tid="tbl_prefill")
        self.assertEqual(locate(html), ["tbl_prefill"],
                         "F4 could not resolve the id it was given: %s" % html[:200])

    def test_a_table_rendered_without_a_tid_is_not_located(self):
        """The prior behaviour, kept as the control: this is the state that produced 20 warnings."""
        html = table(["Metric", "Value"], [["prefill", "2,187.8"]])
        self.assertEqual(locate(html), [])

    def test_the_id_survives_a_caption(self):
        html = table(["A"], [["1"]], caption="Table 4. Capacity", tid="tbl_capacity")
        self.assertEqual(locate(html), ["tbl_capacity"])

    def test_two_tables_keep_distinct_ids(self):
        html = (table(["A"], [["1"]], tid="tbl_one") + table(["B"], [["2"]], tid="tbl_two"))
        self.assertEqual(locate(html), ["tbl_one", "tbl_two"])

    def test_the_id_is_escaped_rather_than_interpolated_raw(self):
        """An id is author-supplied text and lands inside an attribute."""
        html = table(["A"], [["1"]], tid='x" onload="alert(1)')
        self.assertNotIn('onload="alert(1)"', html)
        self.assertIn("&quot;", html)

    def test_cells_and_headers_still_render(self):
        html = table(["Metric", "Value"], [["prefill", "2,187.8"]], tid="tbl_x")
        for fragment in ("<th>Metric</th>", "<th>Value</th>", "<td>prefill</td>", "<td>2,187.8</td>"):
            self.assertIn(fragment, html)


if __name__ == "__main__":
    unittest.main()
