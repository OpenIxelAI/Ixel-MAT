"""
/full mode — Parallel dispatch to all agents with structured output.

Sends the same prompt to all configured agents simultaneously,
collects responses, parses into AgentResponse schema, and 
displays side-by-side in the TUI.
"""

import asyncio
from time import time
from typing import Protocol, Callable, Awaitable

from ixel_mat.schema.response import (
    AgentResponse,
    FULL_MODE_SUFFIX,
    parse_structured_response,
    compare_responses,
)


def _masked(exc: BaseException, agent) -> str:
    """An agent's error with any key in it blanked out (a server's error can quote the key it was sent)."""
    from ixel_mat.material import mask_secrets
    token = getattr(getattr(agent, "config", None), "token", "")
    return mask_secrets(str(exc), [token] if isinstance(token, str) else [])


class AgentTransport(Protocol):
    """Interface that any agent transport must implement."""
    name: str
    label: str

    async def send_and_receive(self, message: str) -> str:
        """Send a message and return the full response text."""
        ...

    @property
    def is_connected(self) -> bool:
        ...


class FullModeDispatcher:
    """
    Orchestrates /full mode:
    1. Append structure prompt to user's query
    2. Send to all agents in parallel
    3. Collect + parse responses
    4. Return structured results + comparison
    """

    def __init__(self, agents: list[AgentTransport], timeout: float = 60.0):
        self.agents = agents
        self.timeout = timeout

    async def dispatch(
        self,
        prompt: str,
        on_agent_start: Callable[[str], Awaitable[None]] | None = None,
        on_agent_done: Callable[[AgentResponse], Awaitable[None]] | None = None,
    ) -> "FullModeResult":
        """
        Send prompt to all agents in parallel. Returns structured results.
        
        Callbacks:
            on_agent_start(agent_name) — called when agent starts processing
            on_agent_done(response) — called as each agent completes
        """
        structured_prompt = prompt + FULL_MODE_SUFFIX

        # Build tasks for all connected agents
        tasks: list[tuple[AgentTransport, asyncio.Task]] = []
        for agent in self.agents:
            if not agent.is_connected:
                continue
            task = asyncio.create_task(
                self._query_agent(agent, structured_prompt, on_agent_start, on_agent_done)
            )
            tasks.append((agent, task))

        if not tasks:
            return FullModeResult(
                prompt=prompt,
                responses=[],
                comparison={},
                error="No agents connected. Use /agent to connect first.",
            )

        # Run all agents in parallel with timeout
        results = await asyncio.gather(
            *(task for _, task in tasks),
            return_exceptions=True,
        )

        # Filter out exceptions, convert to degraded responses
        valid_responses = []
        for i, result in enumerate(results):
            agent = tasks[i][0]
            if isinstance(result, Exception):
                said = _masked(result, agent)
                valid_responses.append(AgentResponse(
                    agent=agent.name,
                    answer=f"Error: {said}",
                    raw=said,
                    degraded=True,
                    degraded_reason=f"Agent error: {type(result).__name__}: {said}",
                    failed=True,
                ))
            elif isinstance(result, AgentResponse):
                valid_responses.append(result)

        # Compare if we have multiple responses
        comparison = compare_responses(valid_responses) if len(valid_responses) >= 2 else {}

        return FullModeResult(
            prompt=prompt,
            responses=valid_responses,
            comparison=comparison,
        )

    async def _query_agent(
        self,
        agent: AgentTransport,
        prompt: str,
        on_start: Callable | None,
        on_done: Callable | None,
    ) -> AgentResponse:
        """Query a single agent with timing."""
        if on_start:
            await on_start(agent.name)

        start = time()
        try:
            raw_response = await asyncio.wait_for(
                agent.send_and_receive(prompt),
                timeout=self.timeout,
            )
            latency_ms = int((time() - start) * 1000)

            response = parse_structured_response(
                agent=agent.name,
                raw=raw_response,
                latency_ms=latency_ms,
            )

        except asyncio.TimeoutError:
            latency_ms = int((time() - start) * 1000)
            response = AgentResponse(
                agent=agent.name,
                answer=f"Timed out after {self.timeout}s",
                raw="",
                latency_ms=latency_ms,
                degraded=True,
                degraded_reason=f"Timeout after {self.timeout}s",
                failed=True,
            )

        except Exception as e:
            latency_ms = int((time() - start) * 1000)
            said = _masked(e, agent)
            response = AgentResponse(
                agent=agent.name,
                answer=f"Error: {said}",
                raw=said,
                latency_ms=latency_ms,
                degraded=True,
                degraded_reason=f"{type(e).__name__}: {said}",
                failed=True,
            )

        if on_done:
            await on_done(response)

        return response


class FullModeResult:
    """Container for /full mode results."""

    def __init__(
        self,
        prompt: str,
        responses: list[AgentResponse],
        comparison: dict,
        error: str = "",
    ):
        self.prompt = prompt
        self.responses = responses
        self.comparison = comparison
        self.error = error

    @property
    def agent_count(self) -> int:
        return len(self.responses)

    @property
    def all_complete(self) -> bool:
        return all(r.is_complete for r in self.responses)

    @property
    def has_degraded(self) -> bool:
        return any(r.degraded for r in self.responses)

    @property
    def fastest(self) -> AgentResponse | None:
        if not self.responses:
            return None
        return min(self.responses, key=lambda r: r.latency_ms)

    @property
    def most_confident(self) -> AgentResponse | None:
        if not self.responses:
            return None
        return max(self.responses, key=lambda r: r.confidence_score)

    def format_summary(self) -> str:
        """Format a text summary of the /full results."""
        if self.error:
            return f"❌ {self.error}"

        lines = []
        lines.append(f"═══ /full — {self.agent_count} agents ═══\n")

        for resp in self.responses:
            status = "⚠ degraded" if resp.degraded else "✓"
            lines.append(f"── {resp.agent} ({resp.latency_ms}ms) {status} ──")

            if resp.degraded:
                lines.append(f"  {resp.answer[:200]}")
                lines.append(f"  Reason: {resp.degraded_reason}")
            else:
                lines.append(f"  Answer: {resp.answer[:300]}")
                lines.append(f"  Confidence: {resp.confidence.value}")
                if resp.evidence:
                    lines.append(f"  Evidence: {', '.join(resp.evidence[:5])}")
                if resp.uncertainties:
                    lines.append(f"  Uncertainties: {', '.join(resp.uncertainties[:3])}")
                if resp.followup:
                    lines.append(f"  Next: {resp.followup}")

            lines.append("")

        # Comparison summary
        if self.comparison:
            c = self.comparison
            lines.append("── Comparison ──")
            lines.append(f"  Agents: {c.get('valid', 0)} valid, {c.get('degraded', 0)} degraded")
            lines.append(f"  Confidence: {c.get('confidence_spread', [])}")
            if c.get('shared_evidence'):
                lines.append(f"  Shared evidence: {', '.join(c['shared_evidence'][:3])}")
            lines.append(f"  Avg confidence: {c.get('avg_confidence', 0):.1%}")
            lines.append(f"  Avg latency: {c.get('avg_latency_ms', 0):.0f}ms")

        return "\n".join(lines)
