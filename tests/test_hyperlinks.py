import unittest
from pathlib import Path

from rich.text import Text

from ixel_mat.hyperlinks import hyperlink_text


class HyperlinkTextTests(unittest.TestCase):
    def test_adds_link_spans_for_urls(self):
        text = hyperlink_text('Docs: https://example.com/docs')
        self.assertIsInstance(text, Text)
        self.assertTrue(any('link https://example.com/docs' in span.style for span in text.spans if span.style))

    def test_adds_link_spans_for_file_paths(self):
        text = hyperlink_text('Config: /tmp/demo/config.toml')
        self.assertTrue(any('link file:///tmp/demo/config.toml' in span.style for span in text.spans if span.style))

    def test_expands_home_paths(self):
        text = hyperlink_text('See ~/demo/file.txt')
        home = (Path.home() / 'demo' / 'file.txt').as_uri()
        self.assertTrue(any(f'link {home}' in span.style for span in text.spans if span.style))


if __name__ == '__main__':
    unittest.main()
