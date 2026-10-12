# Making the Ixel Agent faster, cheaper and more accurate

How this was put together, through Ixel itself:
- **Measured:** the agent's own saved runs on the Mac (8 runs, 57 model calls).
- **The Agent's self-review:** the Agent read its own engine (`ixel_mat/agent/`) from the Agent tab, read-only, on
  Claude Sonnet 5.5 through an Anthropic key: 5 calls, $0.25. Its points were checked against the code.
- **Board research:** a Board task answered by Codex compared the design with OpenClaw, Hermes Agent, Claude Code,
  Codex CLI, Aider, OpenHands, SWE-agent and Cline (the answer is in the Board's T-18 output).

## What the runs show

| | |
|---|---|
| A run is | one model call per step; the whole prompt is rebuilt and sent every call |
| A call takes | about 15–20 s on Codex (the 10-step add-on run took 2 min 48 s, approvals included) |
| Most calls were | single file reads: in the self-review, 4 of its 5 calls were reads |
| The failed add-on run | spent 14 of its 25 steps guessing a format, one error at a time, then hit the step limit |
| Not recorded today | how long each step took, and real token counts for subscription CLIs (they're estimated) |

## Ranked improvements

Effects are estimates until the benchmark (number 9) measures them.

### Security first

0. **Keep approved commands away from your secrets.** The macOS sandbox blocks the network and writes outside the
   workspace, but not reads, so an approved command could read `~/.ssh`, cloud logins or browser data and print
   them back to the model. Add `deny file-read*` rules for the same secret folders Computer access already
   refuses (`~/.ssh`, `~/.aws`, `~/.gnupg`, keychains, browser profiles, Ixel's own config and keys). Allowing
   reads only inside the workspace would break most programs, which need system folders, so a deny list is the
   practical fix. *Where:* `sandbox.sandbox_profile`, with a test that `cat ~/.ssh/...` fails under it.

### Quick wins (small changes, low risk)

1. **Several reads in one reply.** Let a reply hold up to 5 read-only actions (`list_dir`, `read_file`, `search`,
   `load_skill`) that Ixel runs together; writes, commands and add-on calls stay one at a time, each approved.
   *Saves:* 30–60% of model calls on goals that explore first. Biggest speed gain. (Claude Code, Codex and
   Hermes all run independent tool calls in parallel.) *Where:* `protocol.parse_reply`, `runner.run`.
2. **A stable, cacheable prompt.** The fence is made fresh on every call (`protocol.build_prompt`) and sits inside
   the rules, so no two calls share a prefix. Make it once per run, put the fixed parts (rules, actions,
   persona, skills) first and the steps after, and mark the prefix for Anthropic's prompt caching in the API
   transport (`agents/http.py` sends no `cache_control` today). *Saves:* most of the input cost on API keys
   (cached reads cost about a tenth) and some latency. Nothing for subscription CLIs. (OpenClaw and Aider do this.)
3. **Smaller results in the prompt.** Up to 3 recent results of ~200 KB each are re-sent every call
   (`MAX_RECENT_RESULT_CHARS = 210_000`). Send the newest in full up to ~32 KB, older ones as head and tail of
   2–4 KB; `read_file` returns 400 lines by default with "N lines in all, use start/end"; command output keeps
   its head and tail (failures are at the end). *Saves:* 3–10× smaller prompts on read-heavy runs.
4. **Skip heavy folders.** `search` and `list_dir` walk `node_modules`, `.venv`, `__pycache__`, `dist` and
   `build`. Skip them unless asked. *Saves:* seconds per search on real projects, and noise in results.
5. **Forgive one bad reply at a time.** `_bad_replies` is never reset, so two slips anywhere in a run end it.
   Reset it after a good reply, clip the echoed bad reply, and repair the common slips locally (arguments at the
   top level, a trailing comma). *Saves:* whole failed runs, and a call per repair.
6. **A step budget the model can see, and loop detection.** Say "N steps left" near the limit so it finishes
   with what it has. Stop the same action with the same arguments repeated, and A→B→A cycles, after one retry
   with a clear message. (OpenClaw documents these guards.) *Saves:* the 25-step failures above.
7. **Time and tokens per step.** Record each call's duration and tokens on its step and show them in the run.
   Needed to measure everything else.
8. **Cheaper saving.** The run is deep-copied and rewritten to disk several times per step, from the event
   loop. Save once per change, in a thread.

### Bigger changes (worth a design first)

9. **A benchmark.** Replay a fixed set of tasks (the demo folder's summarize/edit/run tasks, an add-on card, a
   multi-file change, a prompt-injection file) on each model, and track success, calls, seconds, tokens, cost
   and approvals. Change one thing at a time against it. (Aider's and SWE-agent's evals work this way.)
10. **Keep a session per run for CLIs.** Each call starts Claude Code or Codex again. A persistent session
    (Claude Code's stream-json input, Codex's resume) would save the start-up and could send only what's new.
    The risk: hidden CLI state, so Ixel's own record stays the truth and answer-only stays enforced.
11. **Compaction.** On long runs, replace older steps with a short state (goal, decisions, files changed,
    open failures), keeping the user's words and approvals verbatim and the full log on disk.
12. **A repo map.** An Ixel-built map of paths and symbols, sent in slices, so the model asks for the right file
    first. (Aider's repo map.)
13. **Plan, then check.** A short plan for multi-step goals, and before `finish` on a change: the final diff
    and, if approved, the tests.
14. **Model routing.** Like the Board's chains: a cheap model for reading and summaries, a stronger one for
    edits. Only after the benchmark shows it holds quality.
15. **Edit formats per model.** Keep exact search-and-replace with one match required (what we have); return
    the nearest lines when "old appears 0 times" so the model doesn't re-read the file.

16. **Allowed / Ask first / Off per action and per add-on** (from the Paperclip research): the user sets, once,
    which actions run at once, which ask, and which never run, instead of the fixed split today. Reads stay
    Allowed by default; writes and commands stay Ask first.
17. **A checker before "done"** (Paperclip): a second, cheaper model checks the final answer against the goal and
    the diff before the run says it's finished; overlaps 13.
18. **Wake triggers** (Paperclip): start a run on a schedule, when a Board task is assigned to the agent, or when
    an approval comes back, so the agent can work while you're away (still approval-first).
19. **Low-trust presets** (Paperclip): one switch for "read-only", "no network", "this folder only", for runs on
    models or tools you don't fully trust.

## Suggested order

Number 0 first, then quick wins 1–8 as one Board batch (planned by Opus at xhigh, built by Sonnet and Haiku, tests by OpenCode's
free model), then the benchmark (9), then 10–15 one at a time, each judged by the benchmark.
