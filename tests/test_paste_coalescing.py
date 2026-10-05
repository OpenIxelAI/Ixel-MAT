import io
import unittest

from ixel_mat.mat import format_prompt_preview, read_burst_submission


class PasteCoalescingTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_burst_submission_coalesces_rapid_lines(self):
        async def prompt_fn(_label):
            return "line 1"

        stream = io.StringIO("line 2\nline 3\n")
        ready_calls = 0

        def select_fn(readers, _writers, _errors, _timeout):
            nonlocal ready_calls
            ready_calls += 1
            if ready_calls <= 2:
                return readers, [], []
            return [], [], []

        result = await read_burst_submission(
            prompt_fn,
            main_prompt="main",
            burst_window=0.01,
            stdin=stream,
            select_fn=select_fn,
        )

        self.assertEqual(result, "line 1\nline 2\nline 3")

    async def test_read_burst_submission_stops_after_timeout(self):
        async def prompt_fn(_label):
            return "first"

        stream = io.StringIO("later\n")

        def select_fn(_readers, _writers, _errors, _timeout):
            return [], [], []

        result = await read_burst_submission(
            prompt_fn,
            main_prompt="main",
            burst_window=0.01,
            stdin=stream,
            select_fn=select_fn,
        )

        self.assertEqual(result, "first")


class PromptPreviewTests(unittest.TestCase):
    def test_format_prompt_preview_compacts_large_multiline_paste(self):
        text = "header\n" + "body\n" * 10
        state = {"count": 0}
        preview = format_prompt_preview(text, state)
        self.assertEqual(preview, "[paste #1 +10 lines]")

    def test_format_prompt_preview_leaves_short_single_line_prompt(self):
        state = {"count": 0}
        preview = format_prompt_preview("hello world", state)
        self.assertEqual(preview, "hello world")
        self.assertEqual(state["count"], 0)


if __name__ == "__main__":
    unittest.main()
