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
        self.finished = None

    async def send_and_receive(self, message: str) -> str:
        await asyncio.sleep(self.delay)
        self.finished = time.perf_counter()
        return VALID


class FullDispatcherCallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_on_agent_done_fires_before_all_agents_finish(self):
        slow = FakeAgent("slow", 0.20)
        dispatcher = FullModeDispatcher(
            [FakeAgent("fast", 0.01), slow],
            timeout=1.0,
        )

        done_times = []
        start = time.perf_counter()

        async def on_done(resp):
            done_times.append((resp.agent, time.perf_counter() - start))

        result = await dispatcher.dispatch("hello", on_agent_done=on_done)

        self.assertEqual(len(result.responses), 2)
        self.assertEqual(done_times[0][0], "fast")
        # Called while the slow agent was still working. Not a bound on the clock: a busy runner can stall the
        # event loop for longer than the fast agent's whole run
        self.assertLess(start + done_times[0][1], slow.finished)
        self.assertGreater(done_times[1][1], 0.18)


if __name__ == "__main__":
    unittest.main()
