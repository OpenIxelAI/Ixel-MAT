import unittest

from ixel_mat.mat import describe_large_paste, should_confirm_large_paste


class PasteConfirmationTests(unittest.TestCase):
    def test_should_confirm_large_paste_for_many_lines(self):
        text = "head\n" + "body\n" * 20
        self.assertTrue(should_confirm_large_paste(text))

    def test_should_confirm_large_paste_for_many_characters(self):
        text = "x" * 5000
        self.assertTrue(should_confirm_large_paste(text))

    def test_should_not_confirm_small_prompt(self):
        self.assertFalse(should_confirm_large_paste("hello world"))

    def test_describe_large_paste_reports_chars_and_lines(self):
        text = "a\n" + ("b\n" * 4)
        self.assertEqual(describe_large_paste(text), "9 chars / 5 lines")


if __name__ == "__main__":
    unittest.main()
