# Ixel MAT: review and fix plan

Date: 2026-10-10. Reviewed at commit b9024e3 (head of main; the session branch `claude/elegant-tesla-dwp9s1` carries no commits of its own). Nothing in the repository was changed by this review.

This document has two audiences. Parts 1 to 3 are the review, for the owner. Part 4 is the hand-off: numbered work packages a separate implementer (a cheaper model) can execute one at a time, with the acceptance test for each. Part 5 says what was not verified.

How it was produced: six area reviewers read the whole package (31k lines of source, 24k of tests) in full, ran the app as a new user in isolated homes, drove the setup wizard through a pty, exercised the browser app in Chromium with Playwright, and wrote probe scripts that reproduce most findings. The full test suite was run once (1906 passed, 39 skipped, 1 failed; the failure is environmental, see X1). A second, adversarial pass then tried to refute each finding; it completed for the twelve engine findings E1 to E12 (all twelve survived, with the corrections folded in below) and was cut off by the account's session limit before reaching the rest. Findings outside the engine therefore stand on the original reviewer's reproduction, which is noted per item as "reproduced" or "by reading".

Line numbers are from the reviewed commit and may drift by a few lines; search for the named function.

---

## Part 1. The latest change: PR #4

PR #4, "Find OpenCode from the app menu, and say when it only has its free models" (commit b9024e3), is sound in what it sets out to do.

What holds up:
- `add_install_folders` appends only folders that exist, are owned by the user or root, and are not world-writable, after everything PATH already had, so nothing already on PATH is shadowed. Symlinks are resolved before the ownership check. It is a no-op on Windows, where the Start Menu already passes the full PATH.
- The "only free models" check reuses the Settings page's own listing path, so it never fetches OpenCode's catalog and never touches the user's background OpenCode service.
- The tests cover the trusted-folder rules, root-owned folders on CI, the no-PATH case, a folder spelled with a trailing slash, and a real subprocess run of `ixel doctor` from a bare PATH.

What it broke or left rough:
1. **Startup regression (T1, medium, reproduced).** `cli.main()` now imports `ixel_mat.agents.launch` on every command. `ixel_mat/agents/__init__.py` eagerly imports the HTTP transport, which imports the Anthropic SDK. Measured here: `ixel version` took 0.12 s at the parent commit a589310 and 1.3 s at HEAD. Every command and the MCP plugin's startup pay this.
2. **Wizard timing (low).** `_opencode_only_free()` runs before "Add OpenCode to the panel?" is asked. For OpenCode 2 it starts a private server and polls for up to 20 s behind one dim line.
3. **The typed key reaches OpenCode during the wizard (C7, low, reproduced).** `_save_key` puts the new key in `os.environ` without registering it as Ixel-injected, so `child_env` does not withhold it from the wizard's `opencode models` call, while later runs do withhold it. The free-models answer can differ between the wizard and afterwards.
4. **Fragile test (X1, low, reproduced).** `tests/test_gui_handoff.py:120` asserts the last PATH entry handed to Handoff is the Python's folder, but the code only appends it when it is not already on PATH. It fails wherever Python lives in `/usr/bin`.

---

## Part 2. Bugs, ranked

Severity is from the user's seat. "Reproduced" means a reviewer ran it; "verified" means the adversarial pass also re-ran it.

### Tier 1: wrong or missing answers, lost data, security

- **E1 (medium, verified)** `ixel_mat/modes/review.py:711-746`, `_Run.calls`. A call that failed instantly counts as "done" for the slowest-wait cutoff. With three or more models, two instant failures (a 401 from a bad key, a CLI not on PATH) make the measured round time about 0 s, so the one healthy model gets only `SLOWEST_WAIT_MIN` (30 s) and is cancelled as "left out: still working 30s in, when the others had finished in 0s". The run ends "No agent produced an answer" though the per-call timeout is 180 s. Both failing transports pass `connect()` and fail only in the answer round, so this is reachable with ordinary misconfiguration.
- **E2 (medium, verified)** `review.py:1060-1113`, `_verify`. A verifier reply that parses but has no usable `use` (missing, "Draft A", "A and B"; natural when there is a single draft, which is the normal two-model saver setup) or a `send_back` with an empty issues list falls to the `elif raw:` branch. The JSON text itself becomes the final answer, `verifier_outcome` is set to "corrected", the terminal prints "(corrected the drafts)", and `stats.record_run` counts a big-model answer and resets the saver streak.
- **E3 (medium, verified)** `review.py:505-530`, `_parse_verdict`. If the moderator writes the notes block first, or puts the answer inside the notes JSON, the whole raw reply including the `<IXEL-… notes>` fence tags is shown as the answer, confidence falls to "uncertain", and no note explains it. No front end strips the fence.
- **M1 (medium, reproduced)** `ixel_mat/machines/terminal.py:106-108` (`_held`), also 100 and 111. The Connect terminal helper runs `python -c <HOLD_OPEN> ssh …` without `-I`, so the folder `ixel gui` was started in (typically a cloned repository being reviewed) is first on `sys.path`, and a planted `subprocess.py` there runs as the user when Connect is clicked. SECURITY.md already defends against exactly this class for the installer's `-I` wrappers.
- **G1 (medium, reproduced)** `ixel_mat/gui/static/markdown.js:57, 87, 97`. `para.append(...inline(line))` spreads an array into a call; a paragraph with tens of thousands of inline markers throws `RangeError: Maximum call stack size exceeded`. The turn is marked failed with the draft spinner still showing, and because the snapshot already saved the result, the reload path throws again and the whole conversation disappears.
- **A1 (medium, reproduced)** `ixel_mat/agents/oneshot.py:419-436`. The cleanup `finally` awaits `tree.kill()`, which can wait up to 2 s when a detached helper holds the CLI's stdout. A second cancellation in that window (timeout, then the user presses Stop) skips `tree.close()`, `remove_workdir()` and `run.clean()`, leaving the temp folder and Gemini CLI's chat file with the question on disk, contrary to SECURITY.md.
- **A2 (medium, reproduced)** `ixel_mat/agents/subprocess.py:232-237`. In PTY mode (the POSIX default) the returned answer begins with the echoed prompt, which in later rounds includes other models' fenced answers. The existing test uses `use_pty=False` and never sees it.
- **C1 (medium, reproduced)** `ixel_mat/config/setup.py:710, 715, 1154-1157, 1223`. Keys are written to `.env`/`keys.enc` the moment a provider step finishes. Declining "Write configuration now?" prints "Aborted — no files written." although the key is saved, and the green "Setup complete!" panel prints anyway.
- **E4 (medium, verified)** `review.py:784-792, 906-916, 948-959`. In deep mode the verdict prompt tags revised answers with the peer score and flagged errors from reviews of the old text, so the moderator is told a now-corrected answer scored 0.00 with the old errors. `_check_confidence` and the saver second pass already treat those grades as stale; the prompt builder does not.
- **T1 (medium, reproduced)** the startup regression from Part 1.

### Tier 2: misleading results, crashes on realistic paths, honesty of numbers

- **E6 (medium, verified)** `review.py:676-682` with `usage.py:321-322`, `http.py:321-330, 390-392`. Any call cancelled by timeout or by the cutoff reports no usage, streamed or not, so the provider's billed tokens are absent from the cost line and month totals with no "N calls not counted" notice. The Anthropic path reads usage only from the final message although `message_start` already carries input tokens.
- **E7 (medium, verified)** `review.py:213`. The standings sort puts an answer graded only "unsure" below an answer every reviewer graded incorrect. `standings()[0]` is the fallback shown when the moderator or verifier fails (972, 1103, 1119) and picks the moderator (901), so the unanimously wrong answer can be shown as "the highest-rated answer".
- **E12 (low, verified)** `ixel_mat/modes/full.py:126-129`, `mat.py:541-595`. `/compare` passes no usage hook and never records cost, so compare-mode spend is invisible in "This month".
- **E11 (low, verified)** `stats.py:198` and `stats.py:239`. A hand-edited `stats.json` with an int under `by_model` makes `record_run` raise (before `ixel review` prints its verdict) and `summary()` raise for `ixel saves` and the GUI stats route.
- **E10 (low, verified)** `stats.py:155-160, 265-302`. Month totals add estimated costs with no "≈" and no stored flag, so neither the terminal nor the GUI can mark them.
- **E5 (low, verified)** `review.py:99-109, 490-493`. A grade like "incorrect." or a label "(A)" is silently dropped with the whole entry; a punctuated `best` drops the vote. Rare because the prompt gives a fixed enum, but silent.
- **E8 (low, verified)** `review.py:239-253`. `agreement()` reports "strong" when every grade is unsure, and the terminal, MCP and GUI all print "reviewers were consistent about every answer".
- **E9 (low, verified)** `review.py:826-846`. A reviewer whose JSON names no valid answer (empty list, a dict, labels like "1" or "A/B") gets a ballot with no reviews and is shown as "reviewed" in all three front ends, with no failure.
- **T2 (medium, reproduced)** `cli.py:606-614, 669-702, 761`. `json_usage_errors` is wired for `ask` and `image` but not `review`, so every failure path of `ixel review --json` prints nothing on stdout.
- **T3 (medium, reproduced)** `mat.py:831-839, 220-221`. With piped stdin, a long line triggers the "Large paste detected" confirm, which eats the next stdin line as its answer; the following question is never asked.
- **T4 (medium-low, reproduced)** `mat.py:216-217`, `cli.py:1302-1304`. Plain-line mode (stdout not a tty): Ctrl+C prints "Interrupted." but the process waits until Enter because the prompt thread is blocked in `input()`; agents are not disconnected.
- **A3 (low-medium, reproduced)** `ixel_mat/agents/websocket.py:360-362`. One non-dict JSON frame from the gateway ends the reader loop; the agent is "not connected" for the rest of the review.
- **C2 (medium, reproduced)** `config/loader.py:113-233`, `runtime.py:191-305`. Unknown keys and tables in config.toml are dropped silently; `ixel config` says valid and `ixel doctor` points back to `ixel config`.
- **C3 (medium-low, reproduced)** `config/loader.py:100-108`. A literal API key in config.toml draws no warning although the comment says it will; the file and its `.bak` keep mode 0644.
- **C4 (medium-low, reproduced)** `config/setup.py:580-592, 673-678`. A key present only in the shell is treated as saved; config is written with `token_env` but nothing is persisted, and the closing panel says keys load automatically. The app from the menu then says "No API key".
- **C5 (medium-low, reproduced)** `config/setup.py:557-575, 933-956`. Re-running setup defaults review questions to the wizard's constants, not the file's values: Enter turns `mode = "deep"` into "review" and drops a configured moderator.
- **C6 (low-medium, propagation reproduced)** `config/secrets.py:693-706, 836-854`, `setup.py:541`, `gui/settings_api.py:702-704`. An OSError while saving a key is not wrapped: traceback in the wizard, HTTP 500 in Settings.
- **G2 (low-medium, reproduced)** `gui/static/ask.js:90-104`. `loadPanel` parses the body without checking `res.ok`; a 500 degrades the page silently.
- **M2 (medium-low, reproduced)** `connections.py:505-525`. The pull-request fetch runs git with the project's own hooks and repository config active; a planted `reference-transaction` hook ran. Needs a project that arrived as an archive rather than a clone. `material.py:195-197` already hardens its own git calls.

### Tier 3: low

- **A4** session id parsed from CLI output may start with "-" and become a flag on `--resume` (`oneshot.py:30, 47-49, 326-329`; `subprocess.py:93-94`). Custom CLIs only.
- **A5** `STATS_LINE_RE` strips a legitimate last line starting "Duration:" or "Messages:" (`oneshot.py:31, 50`).
- **A6** a `claude-stream-json` CLI that prints no events and exits 0 yields "" and its text is lost (`oneshot.py:189-191, 216-223, 381-385`).
- **A7** OpenAI-style `finish_reason: "length"` not flagged as truncated (`http.py:331-337` and the SSE path).
- **A9** `CLAUDECODE`, `CLAUDE_CODE_SESSION_ID`, `CLAUDE_CODE_ENTRYPOINT` reach the panel's `claude -p` when Ixel is the MCP plugin under Claude Code (`presets.py:41-61`). Plausible, partly observed.
- **A8** `killpg` after reap (`process_tree.py:136-137`). Theoretical.
- **T5** `ixel status` table cropped at 80 columns (`cli.py:470-474`). **T6** `ixel triage --help` and `ixel mcp --help` exit 2 (`cli.py:1051-1053, 1109-1111`). **T7** closed fd 0 gives a traceback (`cli.py:680, 248`, `mat.py:727`). **T8** `ixel agents` with no config is a dead end (`cli.py:531-565`). **T9** help text wraps under the command column at 80 and 120 columns (`cli.py:222-228`).
- **C7** wizard key reaches `opencode models` (Part 1). **C8** `.env` rewrite loses comments (`secrets.py:495-506`). **C9** lost update on config.toml between wizard and Settings (plausible). **C10** misleading loader messages for a missing `type` and for `token = "${X}"` without `token_env` (`loader.py:132-138, 303-305, 331-333`).
- **M3** `ssh.forget()` drops a whole hand-merged known_hosts line (`machines/ssh.py:331-340`). **M4** fetch flags need git 2.29 and the error does not say so (`connections.py:512-514`, plausible). **M5** an oversized transcript reply is reported as a broken connection (`sound.py:190-193`, plausible).
- **G3** any 413 is explained as "too much code" (`server.py:61, 288`, `ask.js:1363`). **G4** reload mid-review leaves a "network error" turn and empties the composer (`ask.js:1371`). **G5** focus lost after Ask and Stop (`ask.js:1080-1084, 1947`). **G6** internal exception text forwarded to the page (`server.py:1173-1175`).
- **E13** `Confidence.from_string` substring match HIGH-first ("not high" → HIGH), plausible. **E14** `[review] agents` naming nothing becomes "everyone", plausible.
- **X1** fragile handoff test (Part 1).

---

## Part 3. Experience, measured against Claude Code

What is already good and should be kept: the first-run splash points at `ixel setup`; Ctrl+C handling in the REPL (arm, then exit; cancel a running review) matches Claude Code; NO_COLOR and dumb terminals work; `ixel doctor` is the best next-step screen in the tool; the browser app is polished, legible in both themes, keyboard-navigable, with an honest verdict card and a clean phone layout; hostile model output renders as text everywhere; the MCP server keeps stdout clean.

The gaps, ranked by how many users hit them:

1. **First run ends in the terminal, from every surface.** The app's Ask, Settings and Health screens all say "run ixel setup in a terminal"; saving a company key in Settings changes nothing because only local servers can be added in-app. Claude Code gets you from install to a working prompt in three choices. Give the app an "Add your first model" flow (provider, key, one test call, panel appears) by extending `settings_api.change(section="add_model")` to the four companies, and check a key when it is saved (one models-list call: "Key works · 14 models" or "Anthropic refused this key (401)").
2. **The wizard asks too much and in the wrong order.** A user with one API key answers about twelve prompts; the first question is "Configure OpenClaw Gateway?", the most niche provider. One multi-select "Which do you use?", defaults for label and model, machines only on request, OpenClaw last. On re-run, every question should default to the file's current value so Enter means keep. A rejected key should retry in place instead of dropping the provider, and the wizard should let you remove a provider.
3. **A bare question doesn't work.** `ixel "what is 2+2"` and `ixel --diff` say "Unknown command". Claude Code's first gesture is `claude "prompt"`. Treat a first argument matching no command as `review`. `ixel ask "q"` without `--agent` should say it is for one model and point to `review`, or default to the first agent.
4. **Failures are explained in raw form.** A missing CLI shows "Command not found: opencode" with no install command, though `ixel agents` already phrases it better; HTTP errors are the server's raw text (`ClientConnectorError…`, `API 401: {"error": …}`); Claude Code's "Please run /login" reaches the user verbatim although /login is a command inside Claude Code; the `plain_error` mapping is unreachable for `claude-stream-json` (`oneshot.py:409` vs the raise at 222). Map connection refused on a local host to "Is Ollama/LM Studio running?", 401 to "check the key in Settings › Keys or ixel setup", 404 model to "ixel model <agent> lists what this account has", and add sign-in mappings for Claude Code, Codex and Copilot with the exact command to run.
5. **Silent waits.** 429/5xx retries only log; the OpenCode version probe runs up to 8 s before the first call; `ixel status` probes serially with no spinner; `ixel doctor --check` prints nothing for up to 25 s; the MCP tool sends no progress until the first model answers. Claude Code shows a spinner for any wait and the latest progress line in it.
6. **Redirected output.** `ixel review "q" > out.txt` shows nothing on the terminal for the whole run and writes the full transcript to the file. `claude -p` prints only the answer to stdout. Send progress to stderr when stdout is not a tty, and add an answer-only mode.
7. **One honest cost line.** Cut-off calls, estimated month totals, `/compare` and triage calls are all silently excluded or unmarked (E6, E10, E12). Anthropic's own surfaces mark estimates and omissions inline.
8. **Confidence that isn't checked should say so.** Quick mode's "high confidence" is the moderator's own estimate with no peer review (`review.py:966` runs the check only when a review round ran); label it. When the moderator omits the notes block, say "the moderator gave no confidence" instead of a bare "uncertain".
9. **Stream the answers, not only the verdict.** `ask` passes `on_text` only in the verdict round; for a three-model panel the user watches a spinner for the slowest model's whole latency.
10. **Smaller things.** Three stacked toasts after a saver run cover the reply; the Board's primary button is disabled beside an empty state; Settings is a 2000 px page of greyed cards for a new user; Health prints a four-line absolute path; the phone result grid loses its column labels when done; the phone empty state shows a Ctrl Enter hint under the composer fade; four overlapping "what have I got" screens (`agents`, `ask --list`, `model`, `status`); `ixel update --check` exits 1 on a checkout for an informational message; `ixel doctor` exits 1 on a fresh install while `ixel status` exits 0.

---

## Part 4. Fix plan: work packages for the implementer

Ground rules for whoever does the work:
- Work on branch `claude/elegant-tesla-dwp9s1` from `origin/main`. One commit per work package, message in the repository's style (a sentence saying what the user gets, then why). No model identifiers in commits.
- Before each commit run the affected test files, and before pushing run the full suite: `IXEL_TEST_KEYCHAIN=memory IXEL_NO_UPDATE_CHECK=1 python -m pytest -q`. It takes about five minutes here; expect X1 to fail until WP1 lands.
- Every fix gets a regression test. The reviewers' probe scripts are the seeds; they are listed in the appendix and can be lifted into `tests/` almost verbatim.
- Keep each change minimal and in the package's files. Do not refactor around a fix. Do not change prompts' wording to models unless the package says so.
- Report back per package: what changed, the test names added, the suite result, and anything found that is not in this plan (do not fix it, list it).
- Order: WP1 → WP2 → WP3 → WP4 → WP5 → WP6 → WP7 → WP8 → WP9 → WP10. WP1 to WP4 are the ones that change what users get from a review; do them first and get them reviewed before the rest.

### WP1. Startup time and the regression from PR #4 (small)
Files: `ixel_mat/agents/__init__.py`, `ixel_mat/cli.py`, `tests/test_gui_handoff.py`.
1. Make the agents package lazy: in `ixel_mat/agents/__init__.py` keep `AgentConfig` and `BaseAgent` imports (both light) and import each transport inside `create_agent()`; implement `__getattr__` for the module so `from ixel_mat.agents import HttpAgent` still works (PEP 562). Check with `grep -rn "from ixel_mat.agents import" ixel_mat tests` that every import still resolves.
2. In `cli.main()` leave the `add_install_folders` call where it is; with step 1 it no longer imports anthropic.
3. Fix X1: in `tests/test_gui_handoff.py:120` assert `os.path.dirname(sys.executable) in call["path"].split(os.pathsep)`.
Acceptance: `time python -m ixel_mat version` under 0.3 s on this machine (was 1.3 s); `python -X importtime -m ixel_mat version 2>&1 | grep anthropic` prints nothing; a new test in `tests/test_cli_usage.py` (or a new `tests/test_startup.py`) runs `ixel version` in a subprocess with `-X importtime` and asserts `anthropic` is not imported; the full suite passes including the handoff test.

### WP2. Review engine: cutoffs and parsing (medium)
File: `ixel_mat/modes/review.py`. Tests: `tests/test_review.py`, seeds in `scratchpad/area-engine/test_engine_probe.py` and `scratchpad/verify/verify-engine-1/`.
1. **E1.** In `_Run.calls`, measure `took` from the first task whose `(round_name, agent.name)` is in `self._replied`, not from any completed task. If the loop reaches one pending task and no reply is in at all, do not apply the grace cutoff: await the last task up to `limit_for(agent)`. Reword the failure to "left out: still working after Ns, when the others had answered in Ms" and, when the others failed rather than answered, say "when the other models had failed". Test: two agents failing instantly plus one needing 3 s with `SLOWEST_WAIT_MIN` patched to 0.5 must yield that model's answer; the existing cutoff test must still pass.
2. **E2.** In `_verify`: on `status == "confirmed"` with an unparseable `use`, accept the only draft when there is exactly one, else the top standing, with `verifier_outcome = "confirmed"` and a note "the verifier did not say which draft; showing the top-rated one". On `send_back` with no issues, take the unresolved branch. The final `elif raw:` branch must never set `verifier_outcome = "corrected"`: set it to "unresolved" (or a new "unparsed") and show the top draft, with the note "the verifier replied without the requested format". Check `stats.record_run` (`stats.py:206-216`) so that outcome does not count as a big-model answer or reset the streak. Tests: the three probe cases (confirmed without use, send_back with [], confirmed with "Draft A"), asserting the answer text is never JSON and the streak is unchanged.
3. **E3.** In `_parse_verdict`: when the marker is present and the text before it is empty, use `data.get("answer")` if present, else the text after the closing fence tag. In the final fallback, strip the notes block rather than returning the raw reply. Test: notes-first reply and answer-inside-notes reply both yield the plain answer and the stated confidence.
4. **E4.** In deep mode, mirror the saver second pass: `_answers_block(..., with_scores=...)` omits the score for answers with `was_revised` (or tags it "peer score of the earlier text"), and `_review_block` labels flagged errors of revised answers "before revision". Test: the existing probe 8 scenario; assert the revised answer carries no "(peer score 0.00)".
5. **E5.** Strip surrounding punctuation in `Verdict.parse` and `_parse_label` (`[\W_]*` on both ends). Test: "incorrect.", "Correct!", "(A)", "A." parse.
6. **E7.** For the three fallback sites (moderator failed, send_back without correction, verifier failed) and `_pick_moderator`, prefer a graded answer with score > 0, then an ungraded one, then score 0. Leave the scoreboard order as is. Word the note "best available answer" when the top score is None or 0. Test: probe E7 scenario yields the unjudged answer, not the unanimously wrong one.
7. **E8.** `agreement()` returns "unknown" when no standing has a score. Front ends already handle "unknown".
8. **E9.** When no review entry parsed and `best` is None, call `self.fail(agent, "review", RuntimeError("review named no answer"))` and emit `agent_failed` instead of appending an empty ballot.
Acceptance: all new tests plus `tests/test_review.py` pass; a run with the fake providers in each mode still produces the same verdicts as before for well-formed replies (the existing tests cover this).

### WP3. Honest costs and robust stats (small-medium)
Files: `ixel_mat/modes/review.py` (ask's finally), `ixel_mat/usage.py`, `ixel_mat/stats.py`, `ixel_mat/modes/full.py`, `ixel_mat/mat.py`, `ixel_mat/review_ui.py`, `ixel_mat/gui/server.py` (stats route), `ixel_mat/gui/static/ask.js` (cost tag), `ixel_mat/mcp_server.py` (format_result).
1. **E6.** In `_Run.ask`'s finally, when the reply is None, nothing was reported, and the exception is a timeout or cancellation, record an estimated `CallUsage` (prompt tokens from the prompt text at four characters a token, output unknown) flagged `estimated=True` and `cut_off=True`. In `usage.totals` count cut-off calls; in `cost_line` append "· N calls cut off, not fully counted". For the Anthropic path, read `input_tokens` from `message_start` so a cancelled streamed call still reports its prompt size.
2. **E10.** Store an `estimated` flag per month in `_record_cost`, expose it in `cost_summary`, prefix "≈ " in `cost_lines`, and have the GUI stats route pass it through so the page can show "≈".
3. **E12.** Give `FullModeDispatcher` an `on_usage` hook that builds `CallUsage` per agent like `_Run.ask` does; add `stats.record_cost(calls)` and call it from `_compare`; print `usage.cost_line` after the compare summary.
4. **E11.** In `load_stats`, sanitize `by_model` values (keep dicts only; coerce `saves` to a non-negative int and `local` to bool). Wrap `record_run`'s body so it returns `StatsUpdate(counted=False)` and logs a warning on any exception, as its docstring promises.
Acceptance: tests from `scratchpad/area-engine/probe_stats.py` and `scratchpad/verify/verify-engine-3/probe_e10_e11_e12.py` lifted into `tests/test_stats.py` and `tests/test_cost_tracking.py`; the terminal cost line for a run with one cut-off call says so.

### WP4. Transports (medium)
Files: `ixel_mat/agents/oneshot.py`, `subprocess.py`, `websocket.py`, `http.py`, `ixel_mat/presets.py`. Seeds: `scratchpad/area-agents/`.
1. **A1.** Run the cleanup in `oneshot.py:419-436` under `asyncio.shield` (or the module's existing `_finish_despite_cancellation` helper), and structure it as nested try/finally so `tree.close()`, `remove_workdir()` and `run.clean()` each run even if an earlier step raises. Test: `test_cancel_during_tree_kill_leaks_workdir_and_leftovers` from the scratch dir, inverted to assert nothing is left.
2. **A2.** In PTY mode strip the leading echo of the message (compare modulo `\r`) before returning, or turn ECHO off on the slave with `termios` right after the pty is opened. Add a `use_pty=True` variant of the existing subprocess test.
3. **A3.** In `_reader_loop`, `continue` on frames or payloads that are not dicts.
4. **A4.** Validate session ids with `^[A-Za-z0-9][A-Za-z0-9._-]*$` in both files; ignore anything else.
5. **A5.** Make `STATS_LINE_RE` require a metadata-shaped value, e.g. `^(Duration|Messages):\s*[\d.]+\s*(ms|s|messages)?\s*$`, and only strip it for CLIs known to print a footer.
6. **A6.** When a `claude-stream-json` run produced no events, raise with the first non-JSON stdout line in the message ("printed no answer; it said: …").
7. **A7.** Append the same truncation note used for Anthropic `max_tokens` when `finish_reason == "length"`, in both the plain and SSE paths.
8. **A9.** Add `CLAUDECODE`, `CLAUDE_CODE_SESSION_ID`, `CLAUDE_CODE_ENTRYPOINT` to the Claude Code preset's `drop_env`.
9. Error wording (Part 3, item 4): route the `claude-stream-json` failure at `oneshot.py:222` through `plain_error`; add `PLAIN_ERRORS` entries for Claude Code ("Not logged in" / "Please run /login" → "run `claude` once in a terminal and sign in, or take Claude Code off the panel"), Codex ("Not logged in" → "run `codex login`"), Copilot ("→ run `copilot login`"); make "Command not found" messages include the preset's `install` command; in `http.py` wrap `ClientConnectorError` on a loopback or private host as "Nothing is answering at host:port. Is Ollama or LM Studio running?", 401/403 as "<Company> refused this key: check it in Settings › Keys or ixel setup", and a 404 naming the model as "<Company> doesn't know model X: ixel model <agent> lists what this account has". Keep the raw text after the plain sentence.
Acceptance: `tests/test_launch.py`, `tests/test_factory.py`, the agent tests, and the new tests pass; `tests/test_cli_presets_live.py` is skipped here (no CLIs) and must not be changed.

### WP5. Setup wizard and config (medium)
Files: `ixel_mat/config/setup.py`, `loader.py`, `secrets.py`, `runtime.py`. Seeds: `scratchpad/area-config/`.
1. **C1.** Make the ending honest: on "Write configuration now? n", print "Your keys were saved; the settings file was not written" and skip the "Setup complete!" panel; the same when no agents were configured. Add "the key you saved is kept" to the Ctrl+C message in `cli.py:255`. (Holding keys until the final confirm is the better design but touches more; do the honest message first.)
2. **C2.** In `loader.py` and `runtime.py`, warn on unknown keys and tables with a nearest-known suggestion (difflib against the known names), e.g. "agents.gpt: unknown setting 'modle' (did you mean 'model'?)". Validate the existing file in `_existing_config` (`setup.py:1012-1032`) and show warnings on the welcome screen. Make `doctor`'s "Fix: ixel config" name the file path and the offending key.
3. **C3.** Warn when `token` is a literal value: "agents.X: an API key is written in config.toml; move it to Keys (ixel setup) and remove it here".
4. **C4.** When the key is found only in the environment, ask "Save it in Ixel's keys so the app and other shells have it? (y)" and save on yes; otherwise say in the closing panel that the key must be set in the shell.
5. **C5.** Default every re-run question to the file's current value (mode, moderator, plain_questions, saver and triage keys), so Enter means keep.
6. **C6.** In `secrets._saving` / `save_secret`, wrap OSError as `KeyStoreError` with the path and a hint ("owned by another user? run without sudo").
7. **C7.** Have `_save_key` call the same injected-key registration `load_env` uses (`set_live` or the `_INJECTED` set) so `child_env` withholds a key typed in the wizard.
8. **C8.** Rewrite `.env` line by line, keeping comments and unrelated lines; replace only the lines whose name changed.
9. **C10.** When `type` is missing, say "agents.X: no type (http, oneshot, subprocess, websocket)" and skip the token warning; when `token = "${NAME}"` has no `token_env`, say "set token_env = \"NAME\"".
10. Wizard order and length (Part 3, item 2): move OpenClaw Gateway to the end; replace the five yes/no provider steps by one multi-select; skip label and model prompts (defaults, with "change later with ixel model"); ask about other computers only when the user says they have some; on a rejected key, retry in place with "Paste it again, or Enter to skip"; default yes on connection errors, no on 401; let "Keep existing key?" offer "remove".
11. In `_configure_cli_agents`, ask the OpenCode question first and run `_opencode_only_free()` only after a yes, with "This can take a few seconds" shown.
Acceptance: the pexpect driver in `scratchpad/area-config/drive_setup.py` adapted into `tests/test_setup_wizard.py` cases: abort after a saved key, typo config, env-only key, re-run keeps mode; `tests/test_loader.py`, `tests/test_secrets.py`, `tests/test_config.py` pass.

### WP6. Terminal behaviour (medium)
Files: `ixel_mat/cli.py`, `ixel_mat/mat.py`, `ixel_mat/tui.py`. Seeds: `scratchpad/area-terminal/`.
1. **T2.** Call `json_usage_errors(parser, argv)` in `cmd_review` and route the no-agents and missing-file failures through a `fail()` like `cmd_ask`'s, so `--json` always prints `{"error": …}` on stdout with exit 1.
2. **T3.** Skip the large-paste confirmation when stdin is not a tty (the `read_burst_submission` early return already recognises the case).
3. **T4.** Read the plain-line prompt on a daemon thread, or handle the cancellation in `main()` so disconnect and "Goodbye" run and the process exits without Enter.
4. **T7.** Guard every `sys.stdin.isatty()` with `sys.stdin is None or …` (cli.py:680, 248; mat.py:727; `read_piped_text`).
5. **T6.** Let `triage` and `mcp` accept `-h/--help` with a one-paragraph description and exit 0.
6. **T8.** `ixel agents` with none configured: print "No agents configured yet. Run: ixel setup" and exit 1, as `cmd_model` does.
7. **T9 and T5.** Render `ixel help` with `tui.two_columns` (hanging indent, grouped as the REPL's `/help`); drop the `min_width`s on the status table and give Details `ratio=1`. Check both at `COLUMNS=80`.
8. Bare question (Part 3, item 3): in `_main`, when the first argument matches no command and is not an option other than `--diff`/`-f`, run `cmd_review` with the full argv. Make `ixel ask` without `--agent` say "ixel ask is one model; for the panel use ixel review \"…\"".
9. Redirected output (item 6): when stdout is not a tty, send progress and panels to stderr and print only the verdict text to stdout; add `--answer-only` for terminals.
10. Waits (item 5): wrap `ixel status` and `doctor --check` probes in a `Live` status line with `theme.spinner`, and probe agents in parallel with `asyncio.gather`.
11. Exit codes: `ixel update --check` on a checkout exits 0 with its informational line; make `doctor` and `status` agree on the fresh-install case (both 1 with "run ixel setup").
12. `ixel review` Ctrl+C prints "Cancelled" before exiting 130; the prompt rule says "ctrl+c twice to exit".
Acceptance: `tests/test_review_cli.py`, `tests/test_cli_usage.py`, `tests/test_full_ui.py`, `tests/test_command_registry.py` pass plus new cases for each item; `printf 'x%.0s' {1..2100} | ixel` followed by a question runs the question.

### WP7. Browser app (medium)
Files: `ixel_mat/gui/static/markdown.js`, `ask.js`, `settings.js`, `index.html`, `style.css`, `ixel_mat/gui/server.py`, `settings_api.py`. Seeds: `scratchpad/area-gui/e2e.py`, `shoot.py`.
1. **G1.** Append inline parts in a loop (or pass the array as one child) at markdown.js:57, 87, 97. Wrap each turn's render in try/catch so a bad answer degrades to plain text inside its own card, and never let `Turn.restore` drop the conversation: catch per turn, keep the rest.
2. **G2.** Check `res.ok` in `loadPanel`; on failure show the notice "Ixel couldn't load the panel (HTTP 500). See the terminal or Health." and keep Ask disabled.
3. **G3.** Catch aiohttp's 413 in the middleware and return the JSON error shape; in ask.js, show "The request is too large (N KB)" when the server says so rather than the code message.
4. **G4.** On reload mid-review, restore the turn as "Stopped by reload" with the question back in the composer (persist the draft question in sessionStorage before `fetch`, clear it on completion).
5. **G5.** Move focus to `#question` after Ask and after Stop.
6. **G6.** Log the exception server-side and send "Something went wrong (see the terminal or Health)".
7. First run (Part 3, item 1): add an "Add your first model" card on the empty Ask screen that opens Settings › Models; extend `settings_api.change(section="add_model")` to Anthropic, OpenAI, Google and xAI (writes the agent table, creates config.toml when missing as it does for local servers); on key save, run one models-list call and show "Key works · N models" or the provider's refusal; after the first model is added, refresh the panel without a reload.
8. Polish (item 10): coalesce toasts ("Saved one · 2 achievements"); hide or explain the disabled Board "New task"; collapse disabled Settings sections behind one line each; show `~/.config/ixel-mat/.env` with the full path in a title attribute; keep short step titles in the phone grid when done (style.css:916); hide `.keys` under 640 px; use the themed `.dialog-problem` strip for the Machines Add form; one error voice (no backticks in prose, an in-app next step such as "Open Settings").
Acceptance: Playwright tests in `tests/test_gui_browser.py` for G1 (a 500 KB inline-heavy answer renders and survives reload), G2 and the add-model flow against the fake providers; `tests/test_gui_settings.py`, `tests/test_gui_server.py` pass.

### WP8. Security hygiene (small)
Files: `ixel_mat/machines/terminal.py`, `ixel_mat/connections.py`, `ixel_mat/machines/ssh.py`.
1. **M1.** `[_console_python(), "-I", "-c", _HOLD_OPEN, *argv]` in `_held`; add `-I` in `_windows_script` and `_mac_command_file` too. Test: the scratch `helper_cwd.py` scenario inverted (planted `subprocess.py` must not run).
2. **M2.** Pass `-c core.hooksPath=<empty temp dir> -c gc.auto=0 -c core.fsmonitor=false -c protocol.allow=user` (match what `material.py:195-197` already does) to the fetch and rev-parse argv in `connections.fetch`. Test: lift `gitlab/fetch_hooks.py` and assert the hook did not run.
3. **M3.** In `forget()`, rewrite a multi-name line with the name removed; drop the line only when no names remain.
4. **M4.** Probe `git --version` once; below 2.29 drop `--no-write-fetch-head` and pass an explicit `--refmap=+refs/pull/N/head:refs/ixel/pr/N/head`, or fail with "git 2.29 or newer is needed for pull requests".
Acceptance: `tests/test_machines.py`, `tests/test_connections.py`, new tests pass.

### WP9. MCP plugin and Machines messages (small)
Files: `ixel_mat/mcp_server.py`, `ixel_mat/machines/cli.py`, `ixel_mat/gui/static/machines.js`, `ixel_mat/machines/runs.py`, `ixel_mat/sound.py`, `ixel_mat/gui/server.py`.
1. Send `report_progress` right after `choose_mode` ("asking N models, review mode") and on every `round` event ("peer review: 1 of 3 back").
2. `ixel_panel`: report a CLI agent whose program is not on PATH as "not installed (opencode not on PATH)" using `launch.find_on_path`; list the saver tier and the moderator.
3. The busy error says what is running and since when.
4. `--setup` snippets end with a verification step (`claude mcp list`, or `/mcp` in Claude Desktop).
5. Machines: `ixel machines connect` on a changed key prints both fingerprints and the pins file path, and `ixel machines forget NAME` exists; the page offers "Check its key" for `not_pinned` as well as `hostkey`; a non-zero exit row shows the last stderr line beside the hint.
6. **M5.** Report an oversized transcript as "the reply was too long" (no retry hint); return 502/503 for unreachable transcription services instead of 400.
Acceptance: `tests/test_mcp_server.py`, `tests/test_machines.py`, `tests/test_sound.py` pass with new cases.

### WP10. Documentation and tests sweep (small)
1. ARCHITECTURE.md: mention `add_install_folders` and the lazy agents package; SECURITY.md: note the cleanup guarantee now holds under double cancellation (after WP4) and that the Connect helper runs with `-I` (after WP8).
2. Add the probe scripts' scenarios that are not yet covered by the packages above as tests (see appendix), delete nothing.
3. Run the full suite on Linux and, if possible, the Windows job (`checks.yml` runs on every push).

---

## Part 5. Not verified, and open questions for the owner

- The adversarial pass covered E1 to E12 only. A1 to A9, T1 to T9, C1 to C10, M1 to M5, G1 to G6 stand on one reviewer's reproduction each (marked above); E13, E14, A8, A9, C9, M4, M5, G6 are by reading only.
- Four cross-cutting sweeps were planned and not run: Windows-only code paths (the product's main platform; nothing here could run Windows), SECURITY.md promises versus code, concurrency in the GUI server (blocking calls in handlers, unbounded conversation store, shutdown ordering), and hostile text at each boundary (Rich markup from model text, bidi and zero-width characters in material, `-`-prefixed paths reaching git). These are the next review to commission once usage allows; the Windows one matters most.
- Decision for the owner: whether the wizard should hold keys until the final confirmation (transactional) or keep saving early with honest messages (WP5 item 1 does the latter).
- Decision for the owner: whether `ixel <question>` should become the primary gesture (WP6 item 8), which changes the command surface documented in README.

---

## Appendix. Where the reproductions are

All under `/tmp/claude-0/-home-user-Ixel-MAT/c585261b-b9d6-5506-884a-19d4ae6ba125/scratchpad/` (this session's container; copy them out before the session ends):
- `area-engine/test_engine_probe.py` (12 probes, E1 to E9), `probe_stats.py` (E10, E11); `verify/verify-engine-1/test_verify_e1_e2.py`, `test_verify_e1_real_transport.py`; `verify/verify-engine-2/probe_e5.py` … `probe_e8.py`; `verify/verify-engine-3/probe_e10_e11_e12.py`.
- `area-agents/test_lifecycle.py` (A1, A4), `probe_http.py` (A7), `probe_ws.py` (A3), `probe_pty.py` (A2), `bin/` fake CLIs.
- `area-terminal/run.sh`, `panel_harness.py`, `pty_harness.py`, `bigpaste.py` (T3), `errs.py`, `redirect.py`.
- `area-config/drive_setup.py` (pexpect driver for C1, C4, C5), `test_scratch_secrets.py` (C6, C7, C8), `home-*/` (resulting configs).
- `area-mcp-machines/helper_cwd.py` (M1), `gitlab/fetch_hooks.py` (M2), `pins.py` (M3), `snippets.py`, `conn.py`, `pics.py`.
- `area-gui/shoot.py` (new-user screenshots in `shots/`), `e2e.py`, `e2e2.py` (real reviews against the fake providers, `shots2/`).
- `reports/*.md`: the six area reports in compressed form; `pytest-full.log`: the full-suite run.
