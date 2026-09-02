"""Sanitiser + markdown unit tests (milestone 3)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from render import (
    derive_summary,
    fold_search_text,
    html_to_text,
    render_markdown,
    sanitise_html,
)


class SanitiserTests(unittest.TestCase):
    def test_script_tag_never_survives_as_markup(self):
        # html=False escapes raw HTML to inert text; assert nothing executable.
        out = render_markdown("hello <script>alert(1)</script> world")
        self.assertNotIn("<script", out)

    def test_script_element_removed_with_content_from_html_input(self):
        out = sanitise_html("hello <script>alert(1)</script> world")
        self.assertNotIn("<script", out)
        self.assertNotIn("alert(1)", out)

    def test_raw_html_block_disabled_at_parser_level(self):
        out = render_markdown("<div onclick=\"evil()\">block</div>\n\ntext")
        self.assertNotIn("<div", out)
        # the raw markup is escaped to inert text, never live markup
        self.assertIn("&lt;div", out)
        # a live element can never carry an event handler after sanitising
        out2 = sanitise_html('<p onclick="evil()">x</p>')
        self.assertEqual(out2, "<p>x</p>")

    def test_event_handlers_stripped(self):
        out = sanitise_html('<p onmouseover="evil()">hi</p><img src=x onerror="evil()">')
        self.assertNotIn("onerror", out)
        self.assertNotIn("onmouseover", out)
        self.assertNotIn("<img", out)

    def test_javascript_href_stripped(self):
        # the parser refuses the link (literal text remains, no anchor) …
        out = render_markdown("[click](javascript:alert(1))")
        self.assertNotIn("<a", out)
        self.assertNotIn('href="javascript', out)
        # … and the sanitiser strips it from raw HTML input too
        out2 = sanitise_html('<a href="javascript:alert(1)">x</a>')
        self.assertNotIn("javascript:", out2)

    def test_data_href_stripped(self):
        out = sanitise_html('<a href="data:text/html,x">x</a>')
        self.assertNotIn("data:", out)

    def test_inline_styles_stripped(self):
        out = sanitise_html('<p style="color:red">x</p>')
        self.assertNotIn("style", out)

    def test_external_anchor_gets_rel_and_target(self):
        out = render_markdown("[site](https://example.com/a)")
        self.assertIn('target="_blank"', out)
        self.assertIn('rel="noopener noreferrer"', out)

    def test_relative_anchor_untouched(self):
        out = render_markdown("[rel](../other/)")
        self.assertNotIn("target=", out)

    def test_allowed_structure_survives(self):
        out = render_markdown("**bold** and `code`\n\n- a\n- b\n\n> quote")
        for tag in ("<strong>", "<code>", "<ul>", "<li>", "<blockquote>"):
            self.assertIn(tag, out)

    def test_h1_h2_demoted(self):
        out = render_markdown("# One\n\n## Two\n\n### Three")
        self.assertNotIn("<h1>", out)
        self.assertNotIn("<h2>", out)
        self.assertEqual(out.count("<h3>"), 3)

    def test_table_allowed(self):
        out = sanitise_html("<table><thead><tr><th>a</th></tr></thead><tbody><tr><td>b</td></tr></tbody></table>")
        self.assertIn("<table>", out)
        self.assertIn("<td>", out)

    def test_markdown_table_renders(self):
        out = render_markdown("| a | b |\n|---|:-:|\n| 1 | 2 |")
        for tag in ("<table>", "<thead>", "<th>", "<tbody>", "<td>"):
            self.assertIn(tag, out)
        self.assertNotIn("style=", out)  # alignment styles are stripped (CSP-safe)

    def test_strikethrough_renders_as_del(self):
        # markdown-it emits <s>; the allow-list carries <del>, so it is mapped.
        out = render_markdown("~~gone~~ text")
        self.assertIn("<del>gone</del>", out)
        self.assertNotIn("<s>", out)


class TextTests(unittest.TestCase):
    def test_html_to_text(self):
        self.assertEqual(html_to_text("<p>a  <b>b</b>\n c</p>"), "a b c")

    def test_fold(self):
        self.assertEqual(fold_search_text("  Zürich  Café\tnaïve "), "zurich cafe naive")

    def test_derive_summary_truncates_at_word(self):
        text = "<p>" + "word " * 100 + "</p>"
        summary = derive_summary(text)
        self.assertLessEqual(len(summary), 165)
        self.assertTrue(summary.endswith("…"))


if __name__ == "__main__":
    unittest.main()
