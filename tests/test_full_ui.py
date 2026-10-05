import unittest

from ixel_mat.mat import build_full_status_lines


class BuildFullStatusLinesTests(unittest.TestCase):
    def test_shows_live_progress_and_timer_for_processing_agents(self):
        now = 100.0
        agent_states = {
            "fast": {
                "label": "Fast",
                "status": "done",
                "response": {"answer": "Done answer", "latency_ms": 1200, "degraded": False},
                "started_at": 98.0,
            },
            "slow": {
                "label": "Slow",
                "status": "running",
                "started_at": 95.4,
                "response": None,
            },
        }

        lines = build_full_status_lines(agent_states, now=now)
        joined = "\n".join(lines)
        self.assertIn("1/2 agents responded", joined)
        self.assertIn("slow", joined.lower())
        self.assertIn("4.6s", joined)
        self.assertIn("Done answer", joined)


class Plain:
    def __init__(self, name, reply=None, error=None):
        self.name, self.label, self.is_connected, self.reply, self.error = name, name.title(), True, reply, error

    async def send_and_receive(self, message, **kwargs):
        if self.error:
            raise RuntimeError(self.error)
        return self.reply


def test_compare_counts_plain_answers_as_answers(monkeypatch):
    import asyncio
    import io

    from rich.console import Console

    from ixel_mat import mat

    out = io.StringIO()
    monkeypatch.setattr(mat, "console", Console(file=out, width=120, color_system=None))
    agents = {"a": Plain("a", "It's 391."), "b": Plain("b", "391"), "c": Plain("c", error="no key")}
    asyncio.run(mat._compare("What is 17 x 23?", agents))
    text = out.getvalue()
    assert "── /compare ── 3 agents ── 2 answered ──" in text
    assert "Confidence: uncertain" not in text  # a plain answer doesn't claim one


if __name__ == "__main__":
    unittest.main()
