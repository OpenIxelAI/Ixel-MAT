import asyncio
import time
import unittest

from ixel_mat.modes.full import FullModeDispatcher


VALID = '{"answer":"ok","confidence":"high","evidence":[],"uncertainties":[],"commands":[],"next_step":"done"}'


class FakeAgent:
    def __init__(self, name, delay):
        self.name = name
        self.label = name
        self.is_connected = True
        self.delay = delay

    async def send_and_receive(self, message: str) -> str:
        await asyncio.sleep(self.delay)
        return VALID


class FullDispatcherCallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_on_agent_done_fires_before_all_agents_finish(self):
        dispatcher = FullModeDispatcher(
            [FakeAgent("fast", 0.01), FakeAgent("slow", 0.20)],
            timeout=1.0,
        )

        done_times = []
        start = time.perf_counter()

        async def on_done(resp):
            done_times.append((resp.agent, time.perf_counter() - start))

        result = await dispatcher.dispatch("hello", on_agent_done=on_done)

        self.assertEqual(len(result.responses), 2)
        self.assertEqual(done_times[0][0], "fast")
        self.assertLess(done_times[0][1], 0.10)
        self.assertGreater(done_times[1][1], 0.18)


if __name__ == "__main__":
    unittest.main()
