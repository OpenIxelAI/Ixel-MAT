# Security

Ixel MAT sends one question to several AI services and shows you what comes back. It holds your API keys,
runs other companies' command-line tools, and passes one model's output to another. This page covers what
it protects, from whom, and how each protection is checked.

## Reporting a problem

Please report vulnerabilities privately, not in a public issue: email **openixel.ai@proton.me**, or use
GitHub's **Report a vulnerability** button (Security tab) on this repository. The button works only while
the repository is public, so if you don't see it, email us.

## What Ixel protects

| Asset | Where it lives |
|---|---|
| Your API keys | `~/.config/ixel-mat/keys.enc` (0600), encrypted with a key kept in your system's keychain (below). On a computer with no keychain Ixel can use, or one that won't keep that key, `~/.config/ixel-mat/.env` (0600), in plain text. Or your shell environment |
| Your subscription logins | Each CLI's own storage (`~/.claude`, `~/.codex`, `~/.gemini`…). Ixel never reads these |
| Your files | Anything your user account can read |
| Your code | Only the diff or files you name for a review (`--diff`, `--file`…) are read, once, and sent to the models on your panel (below) |
| Your questions | Sent only to the models on your panel, and to TypeSafe if you set triage to use it (below). The last three `ixel review` exchanges are kept in `~/.config/ixel-mat/conversation.json` (0600) for `--continue`, each with the time it was saved. Each one is deleted once it's 24 hours old, by the next `ixel` command after that (continuing the conversation doesn't keep its earlier questions longer), and all of them at once by `ixel forget` (or Forget in the app's Settings); the terminal app keeps its conversation, and the questions you can recall with the up arrow, in memory only, and the browser app in that tab's `sessionStorage`, which the browser clears when you close the tab. `ixel forget` also deletes the app window's browser storage (`ixel app`'s Edge or Chrome profile, and on a Mac Ixel.app's WebKit storage) |
| Your usage and money | Every model call is billed to you |

## Who we assume might be hostile

1. **Model output.** Any answer can contain terminal escape codes, HTML, script, or text written to
   hijack the next model ("ignore your instructions and…"). That includes answers you asked for, because
   a question can carry someone else's text: a pasted email, a web page, a diff.
2. **Websites you visit** while `ixel gui` is running, and **other devices on your network**.
3. **The folder you run Ixel in**, for example a cloned repository.
4. **Other programs' settings** that make a CLI more permissive than Ixel expects.
5. **Lookalike services.** Sites that resell a model's API under its name see everything sent through them.

We don't try to protect against someone who already controls your user account. They can get your keys
anyway: from your keychain, which gives them to programs you run (below), or from Ixel while it runs.

## Protections

### Keys

- **Encrypted, with the key in your keychain.** A key you save with `ixel setup` or in Settings goes in
  `~/.config/ixel-mat/keys.enc`, encrypted (Fernet: AES-128 with an HMAC-SHA256), and the key that opens
  that file is one item in your system's keychain, as Chrome keeps its saved passwords: the macOS Keychain,
  Windows Credential Manager, or on Linux GNOME Keyring, KWallet or another keyring that offers the Secret
  Service, through the `keyring` package. The item is named `Ixel`, account `keys`. On Windows it goes with
  a profile that roams, as `keys.enc` in it does, so the keys open on each computer you sign in to. Ixel
  reads that item once per run and keeps it in memory while it runs.
- **A copy of your files.** A backup, a synced folder or a copy of your home folder holds no key anyone can
  read without your keychain's password. Your keychain is kept in your home folder too (on Windows, in your
  profile's AppData folder), so a full copy of your home folder is only as safe as that password, which on
  a Mac and on Windows is usually the one you sign in with. A Linux keyring given a blank password, so that
  it stops asking, protects nothing.
- **Moved out of `.env`.** Keys an earlier Ixel kept in `~/.config/ixel-mat/.env` move into `keys.enc` the
  first time Ixel runs with a keychain it can use, and so does a line you add to `.env` by hand later.
  `.env` is deleted once nothing but comments is left in it.
- **A keychain that doesn't answer.** Ixel gives the keychain 30 seconds (time to type your password when a
  Mac asks, or to unlock a Linux keyring). If it doesn't answer in time, or fails, the keys in `keys.enc`
  aren't used in that run, Health says so, and saving a key fails with "unlock it and try again". A key is
  never written in plain text instead, and `keys.enc` is never overwritten while it can't be opened. The
  app's pages never wait for the keychain, except to save or remove a key: the app asks it once as it
  starts, and after that on a thread of its own (for keys another Ixel saved meanwhile, or ones to move out
  of `.env`).
- **A keychain that won't keep the key.** If your keychain opens but refuses to keep Ixel's item (a company
  policy against saved passwords, say, or a Linux keyring with nowhere to keep it), and there's no
  `keys.enc` yet, keys go in `.env` in plain text, readable only by you, as on a computer with no keychain,
  and Health, `ixel doctor`, `ixel status`, `ixel setup` and Settings say the keychain refused. Ixel asks
  again each time you save a key, and each time it starts with keys in `.env`; they move into `keys.enc`
  once the keychain keeps the item. Beside a `keys.enc`, a refusal means nothing is saved. A keychain
  that's locked, or a password prompt you turn down, isn't a refusal: saving then fails with "unlock it
  and try again".
- **No keychain, plain text.** Only those keychains count. On a computer with none, such as a Linux server
  with no Secret Service, or with `keyring` turned off (`PYTHON_KEYRING_BACKEND`, or its own settings
  file), keys stay in `.env` in plain text, readable only by you (0600), and Health, `ixel doctor`,
  `ixel status`, `ixel setup` and Settings say so. keyring's other backends, such as the plain-text files of
  `keyrings.alt`, are never used: they'd keep the key next to the file it opens.
- **If the key is gone.** When `keys.enc` can't be opened with the keychain's key (the item was deleted, or
  the file came from another computer), Ixel uses none of its keys, leaves the file as it is, and Health
  says to add them again. Saving a key then renames it `keys.enc.unreadable` and starts a new `keys.enc`.
  A file set aside this way is never overwritten: an older `keys.enc.unreadable` is kept as
  `keys.enc.unreadable-2` (then `-3`…), unless it opens with the key of the new `keys.enc` (it's this
  computer's own, from before another computer's keychain wrote `keys.enc` in a folder that's synced, or
  from before the keychain's item came back). Then its keys go into the new file, and it's deleted.
- Written with owner-only permissions (0600) and atomically, so a crash never leaves a half-written or
  world-readable file. Two Ixels saving at once take turns (through `keys.enc.lock`, which is taken over
  if an Ixel that stopped mid-save left it behind for 2 minutes). When the keychain is still asking for
  your password as Ixel stops waiting for a new item, the lock stays until it answers (or those 2 minutes
  pass), so no other Ixel makes a different one meanwhile.
- **What the keychain doesn't stop.** A program running as you can usually ask the keychain too: Windows
  gives it to any program you run, an unlocked Linux keyring to any program in your session, and a Mac
  asks first, except for the Python that saved it (a script run with that same Python counts as it). While
  Ixel runs, your keys are in its memory and environment, as before.
- Never written to `config.toml` (it holds only the *name* of the variable), the browser app, the
  plugin's output, or `ixel config` output. `ixel_panel` reports "missing API key", never a key.
- Sent only over HTTPS/WSS, or over plain `http://` and `ws://` to this machine, and refused for any
  other host. That holds for every request that carries a key: answers and reviews, and also the checks
  in `ixel agents`, `ixel status`, `ixel doctor --check`, `ixel model` and Health's **Check now**. A model
  server on your own network that needs no key (an Ollama on another PC) may use plain `http://`, and gets
  no `Authorization` header; give it a key and Ixel refuses to send it over `http://`.
- **Checked only with their own provider.** `ixel status` checks each provider key it finds (saved in
  Ixel, or in your environment) by asking that provider for its list of models, even a key no agent uses
  yet. That request carries the key and nothing else: no question, no other key.
- **Redirects are never followed** when a key is involved (API calls, the setup wizard's model lists, the
  gateway connection). A redirect would re-send the request to another host, cleartext `http://`
  included. HTTP libraries strip `Authorization` on cross-host redirects, but not headers like Anthropic's
  `x-api-key`, and Python's `urllib` strips nothing.

### Command-line agents (Claude Code, Codex, Gemini CLI, Copilot, OpenCode, and your own)

The subscription CLIs are coding agents: left to their defaults they can run shell commands and edit
files. Ixel uses them only to answer, and for each preset:

- **Every tool is off**, using the CLI's own switches (Gemini CLI keeps a few, below):

  | CLI | How |
  |---|---|
  | Claude Code | `--tools ""` (no tools), `--strict-mcp-config` (none of your MCP servers) |
  | Codex | `--ignore-user-config` (no MCP servers, plugins or hooks), shell/exec/image/sub-agent tools disabled, web search off, read-only sandbox |
  | Gemini CLI | plan (read-only) mode in a trusted empty folder, no extensions, no MCP servers. Plan mode still offers the model tools, among them reading files (in that empty folder), Google web search and web fetch; it blocks writes and shell commands, and the test below checks that |
  | Copilot | `--available-tools=ixel_none` (no tools), built-in MCP off, `COPILOT_ALLOW_ALL` removed |
  | OpenCode | runs Ixel's own agent, defined with every tool denied in config that outranks yours, on a server started for that run (OpenCode 2's background service runs with your own settings) |

- **OpenCode never fetches its model catalog.** Whenever Ixel runs an agent whose command is `opencode`, with
  or without its preset (a question, its model list in Settings, Check now), it sets
  `OPENCODE_DISABLE_MODELS_FETCH=1` and the locked-down config, which the agent's own `env` can't change (Ixel
  warns if it tries), and it requires `--standalone` (OpenCode 2) or `--pure` (OpenCode 1) in its args, so a
  question never goes to your own background service, which runs with your settings. OpenCode then uses the
  model list it already has, or the one built into it, and doesn't contact models.opencode.ai (models.dev for
  older versions). A model newer than that list works once you add it to your `opencode.json` or update
  OpenCode. OpenCode 2's list comes from a server Ixel starts for it on 127.0.0.1 with a one-time
  password and stops afterwards. Checked against OpenCode 1.18.18, 1.18.34 and 2.0.22; a later version could
  change the switch, which the test below would catch. Not covered: a command that only starts OpenCode (`npx`,
  a script of yours), OpenCode you run yourself (it still fetches unless you set that variable too), and
  OpenCode 1's attempt to install its plugin package from npm the first time it runs with a new config folder.
- **A fresh empty folder** for every run, deleted afterwards (`workdir = "temp"`).
- **No terminal:** stdin is closed or carries only the question, so a CLI can't stop and ask for approval.
- **The question can't become a flag.** Every preset, and every command-line agent of your own whose
  config doesn't say otherwise, gets it on stdin, so it never shows on a command line that other programs
  and other users of the computer can read (`ps`, Task Manager). An agent you set up with
  `prompt_via = "auto"` or `"arg"` gets it after `--`, never as a bare argument, so a question that starts
  with `--dangerously-…` stays a question; `"flag"` puts it after `-q`. Those three show it on the command
  line. A chat-style (`"subprocess"`) agent always gets it on stdin.
- **None of Ixel's keys:** keys saved in Ixel are withheld unless an agent lists them in
  `pass_env`. The Claude Code, Codex, Gemini CLI and Copilot presets also drop every AI vendor's key from
  your environment with `drop_env` (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`…): their own
  would make the CLI bill an API account instead of your plan, and the others are no business of theirs.
  A key you list in `pass_env` is passed even so. Gemini CLI gets your Gemini API key (`GEMINI_API_KEY`, or
  the Google (Gemini) key saved in Ixel) only while it has no sign-in of its own, in its settings or
  variables: without one it stops before asking anything, so the key can't be billed in place of a login.
- **Every call ends everything the CLI started**, not just the CLI, whether it answered, timed out or was
  cancelled: its process group on macOS and Linux, a job object on Windows. That includes helpers still
  running after the CLI itself exited. Stopping Ixel with Ctrl+C, `kill`, or by closing its terminal ends
  them too (`ixel review`, `ask`, `image`, `mcp`, `gui` and `app`; a `nohup` is respected).
- **The right program, from PATH only.** A CLI (and `git`, for code review and updates) is looked up on
  `PATH` alone, never in the current folder, so a `claude.bat` or `git.exe` in a cloned repository isn't
  what runs (on Windows the current folder would otherwise be searched first). A command that isn't on
  `PATH` isn't started at all, since Windows would then look for it in the current folder.
- **No `cmd.exe` in between (Windows).** `npm install -g` makes each CLI a `.cmd` file, and a `.cmd` runs
  through `cmd.exe`, which reads `&`, `|`, `<`, `>`, `^`, `%` and quotes in its arguments as commands.
  Since a prompt can quote a model's answer, Ixel reads the npm shim and runs what it points to (`node.exe`
  and the CLI's script, or its `.exe`) directly, but only for a plain shim: one that runs node with no
  extra arguments, with a real `node.exe` (a version manager's `node.cmd` would put `cmd.exe` back). Any
  other batch file runs only with arguments free of those characters; otherwise the call is refused with a
  hint to use `prompt_via = "stdin"`.

**How it's checked:** `tests/test_cli_presets_live.py` runs each real CLI against a fake model API. The
fake model answers with tool calls: run a shell command, write a file, read Ixel's key file. Some tests
also plant a user MCP server that would leave a file behind if started, a user config that turns every
tool back on, API keys in the environment, and notes for agents (`AGENTS.md`) in the folder Ixel runs from.
The test fails if anything runs, if the key file's contents or those notes reach the model, if a planted API
key is used, or if any tool is offered beyond a short list of harmless
ones (for Gemini CLI, the list plan mode keeps, web search and web fetch included). For OpenCode it also
checks, against a stand-in catalog, that neither a question nor the model list asks for the catalog, and that
the same list without Ixel's setting does (so the check can see a fetch at all). The tests are skipped on Windows. Nothing
runs them on their own: run them yourself on macOS or Linux when a CLI updates. The GitHub workflow has a
job that installs the **latest** release of every CLI and runs them, but it runs only when someone starts
it by hand, so it catches a vendor changing what a flag means only if it's run after that change. One known
limit: Copilot and OpenCode still *start* MCP servers you configured yourself, and OpenCode 2 loads plugins
you installed in it (OpenCode 1 left them out, and 2 has no switch for that). They're your own programs,
and their tools are hidden from the model.

`ixel setup` saves each CLI agent as a reference to its preset (`preset = "claude_code"`), not a copy of
its flags, so when a preset is tightened, updating Ixel is enough to get the change. The presets live in
`ixel_mat/presets.py`. If you give an agent its own `args`, those replace the preset's and can undo these
protections. An agent's own `env` adds to the preset's, and can't replace OpenCode's locked-down agent or
its catalog setting.

### Model output

- **Terminal:** control characters and escape sequences (colors, cursor moves, OSC 8 links, OSC 52
  clipboard writes, title changes) are stripped before printing, and Rich markup in model text is escaped.
  Markdown links are printed with their real address next to the text instead of as clickable links, so
  a link labeled `https://your-bank.com` can't quietly point somewhere else.
- **No stalls:** the parsers that read model replies run in linear time. Hostile text such as unclosed code
  fences or unterminated escape sequences used to take tens of seconds at 200 KB, and a test now pins that
  down.
- **Browser app:** model text is only ever set as text (`textContent`), never as HTML. Markdown is rendered
  by building elements, links are shown as text rather than made clickable, and a strict
  Content-Security-Policy allows only the app's own script and styles, with no inline script and no
  outside connections.
- **Follow-ups:** earlier answers go back to the panel fenced like any other model output, with the
  note that it's background, never instructions. Each is capped in length, and only the last three
  exchanges are sent.
- **Code to review:** fenced as material in every round, like model output, so instructions hidden in a
  comment or a string are code to review, not orders. Bidirectional overrides and zero-width characters
  ("Trojan Source") are replaced by visible markers such as `[U+202E]`, so the models can see and report
  them.
- **Model to model:** when answers are sent to other models for review, each is wrapped in markers with a
  random ID the author can't guess, and the reviewer is told that everything inside is data to judge, not
  instructions. Reviews are anonymous (A, B, C in a random order, and a different order for each
  reviewer), so a model can't recognize and favor its own answer, and grades a model gives its own answer
  are ignored.
- **Plugin:** results returned to Claude Desktop, Codex and the rest start by saying the content was
  written by other AI models and is to be evaluated, not obeyed.

### Browser app (`ixel gui`, and `ixel app` in a window)

- Listens on `127.0.0.1` only, on a random port.
- A new random key for every launch. It reaches your browser in the link's `#fragment`, which browsers
  never send over the network, and the page moves it out of the address bar immediately. API calls
  without it are refused. Ixel opens the browser through a private (0600) redirect file, deleted as soon
  as the page has loaded (or when Ixel stops, if it never does), rather than passing the link on the
  launcher's command line, where other users of the same computer could read it.
- The `Host` header must be this server, which blocks DNS rebinding. Requests must come from the app's own
  page (`Origin` check), which blocks other websites.
- `X-Frame-Options: DENY`, `nosniff`, `no-referrer`, same-origin isolation headers.
- Request size and question length limits; the mode and every field a request uses are checked, and
  fields it doesn't use are ignored.
- When the page goes away (tab closed, Stop pressed), the running review is cancelled, so model calls
  stop (a call already under way may still be billed for what it used). Stopping Ixel cancels it too.
- The page keeps this tab's conversations (questions, answers, verdicts; never keys) in `sessionStorage`
  so a reload doesn't lose them. It belongs to that one tab and this server's random port, and the
  browser deletes it when the tab closes. Restored text is rendered the same way as live text.
- `ixel app` serves the same page, under the same rules, to an Edge or Chrome app window. The window gets
  the same redirect file (deleted as soon as the page has loaded), so the key isn't on the browser's
  command line either, and it runs with a browser profile of its own (`%LOCALAPPDATA%\IxelMAT\window` on
  Windows): your normal browser's extensions, cookies and history aren't in it. It never syncs
  (`--disable-sync`): Edge may still sign it in to the Microsoft account you use Windows with, but doesn't
  sync its history, Ixel's addresses included, to that account. A window opened by an earlier Ixel may have
  synced already; this stops it from now on. Each open page holds one request to `/api/presence` open (token
  required, like every API call); the server stops a few seconds after the last one closes.
- The Mac app and the Linux window start `ixel app --host` themselves, which prints the address with its
  key on a pipe only they read, and stops when they close its stdin. Both accept only an `http://127.0.0.1`
  address from it. The Linux window keeps only that server's pages inside (the address is parsed, so
  `http://127.0.0.1:80@evil.example/` doesn't count), gives the microphone only to them, opens other web
  links in your browser and ignores any other kind of link.

**How it's checked:** `tests/test_gui_browser.py` runs the app in real Chromium with a model that answers
with `<img onerror>`, `<script>`, and `javascript:` links. It asserts that nothing executes, no elements are
injected, and no CSP violations occur, both live and after a reload restores the conversation.
`tests/test_gui_server.py` covers the token, Host, Origin and input checks, and fails if the page's code
ever uses an API that turns a string into HTML (`innerHTML`, `insertAdjacentHTML`, `eval`…).

### Plugin (`ixel mcp`)

- Talks to its host app over stdin/stdout only. It never opens a network port.
- One review at a time for each connection. Each app starts its own copy of the plugin, and a copy runs
  one panel review at a time; two apps (or two sessions that each start the plugin) can each run one.
  `ixel_review` is not marked read-only, since each call spends your model usage
  and updates the saves counter, so apps that skip confirming read-only tools still ask you. Nothing it
  does is destructive. `ixel_panel` only reads your config and is marked read-only.
- **No loops:** everything Ixel launches carries `IXEL_PANEL_DEPTH`. If a panel member (say, Claude Code
  with the Ixel plugin installed) tries to start a panel of its own, Ixel refuses.

### Code review (`--diff`, `--staged`, `--base`, `--file`, the browser's Code box, the plugin's `code`)

- **Read-only.** Ixel reads the diff or the files you named once, before the panel starts, and nothing in
  your project is changed. The models get text: API models and the built-in CLI presets have no file or
  shell access. An agent you set up yourself (an OpenClaw gateway, your own command or `args`) keeps
  whatever tools it has, so only add one you trust with your code.
- **Your repository's settings run nothing.** Git is run with `core.fsmonitor=false`, `--no-ext-diff`,
  `--no-textconv` and no pager, and every clean, smudge or process filter the repository's own config
  defines is emptied for the call and marked not required, so a git-crypt or `git lfs install --local`
  repository still shows its diff (yours, such as Git LFS, keep working). Submodules are shown as commit ids,
  so git doesn't go into them. So a cloned or downloaded repository can't make reading its diff start a
  program. File names are read with fixed `a/` and `b/` prefixes, whatever your `diff.noprefix` says.
- **What the repository's filters store is left out.** Without its filter, git would read such a file as
  it is on disk, which for git-crypt is the secret in plain text (a new file has no encrypted copy to
  compare with). So the files those filters apply to (their `filter` attribute) are left out of the diff,
  and you're told which; if one turns up in it anyway (an old git), nothing is sent. A filter whose name
  git can't match files by (one with a `.` in it, say) means no diff at all: name the files instead.
- **No network.** `protocol.allow=never` and `GIT_NO_LAZY_FETCH=1`: in a partial clone, a file that was
  never downloaded is an error, not a fetch through the repository's remote (which could name a command).
  Git never prompts (`GIT_TERMINAL_PROMPT=0`, `GCM_INTERACTIVE=never`, stdin closed) and takes no optional
  locks. A `--base` that looks like a flag is refused before git sees it.
- **Secrets are refused.** Material that looks like it holds a private key (PEM, OpenSSH, PGP, PuTTY), an
  OpenAI/Anthropic-style, xAI or Groq API key, a GitHub, GitLab, AWS (an access key ID, or a secret key next
  to its usual name), Slack (tokens and webhook links), Google (API keys and OAuth client secrets), Stripe,
  npm or Hugging Face key or token, a login token (JWT), or a file such as `.env`, `id_rsa`, `*.pem` or
  `*.ppk` (by its old name too, when it's renamed or deleted) is not sent; you're told which file.
  `--allow-secrets` (in `ixel review` and `/review`) overrides this for false alarms; the browser and the
  plugin have no override, so take the secret out of what you paste. It's a pattern check: passwords and
  keys in other formats aren't caught, so review what you send.
- **Nothing hidden.** Bidirectional overrides, zero-width and other invisible characters (Unicode tag
  characters, Hangul fillers, variation selectors and line separators among them), and control codes are
  shown as `[U+…]` markers, so a stray escape code can't hide the rest of a line and a lone carriage return
  can't make code look commented out.
- **Bounded.** Up to 60,000 characters (counted with those markers), and up to 20 files named with `-f`;
  binary files, folders and files over 240 KB are refused. Lockfiles are left out of diffs and new files.
- **Code isn't saved with the question.** Follow-ups (`--continue`, the browser's conversation) and
  triage see the question, not the code.

**How it's checked:** `tests/test_code_review.py` plants an fsmonitor, external diff, textconv filter,
clean/smudge/process filter and pager in a repository's config and asserts none runs, checks that a
git-crypt-style filter's files (new, staged, changed after staging) never reach the diff, reads a partial clone
whose remote would run a command, puts a fake `git` in the current folder, forges the run's fence inside the
code, hides a payload behind an escape code, and checks secrets, hidden characters and every front end.

### Configuration

- Read only from `~/.config/ixel-mat/config.toml`. A config file in the current folder is **ignored**,
  because agent definitions can name commands to run, and a cloned repository must not be able to add one.
- The setup wizard writes TOML with every value quoted and escaped, so a model name or label can't
  inject extra settings.
- Invalid settings are ignored, never half-applied. They're reported each time Ixel starts (the terminal
  app, `ixel review`, `ixel gui` and the plugin), by `ixel doctor` and on the Health page. `ixel config`
  shows only some of them for now.

### Triage (optional, off by default)

Triage asks small questions between rounds (see [the setup guide](https://ixelai.com/docs/setup/#jobs)): how much checking a question needs, and
whether answers agree. Either one of your own models answers them, or TypeSafe AI's decision API does.
Either way it gets the question and, for the agreement checks, the models' answers, labeled A, B, C with the
authors hidden. It never gets your keys, which model wrote what, or anything else from your computer.

- **Your own model** (`agent = "…"`): the text goes to that agent's provider, which your panel already
  uses, so nothing goes anywhere new. It's fenced with a random marker and the note that it's material
  to judge, never instructions, like every other model output. The reply must be the small JSON asked
  for; anything else is "no answer", and its error is reported by type only, never by content.
- **TypeSafe** (`provider = "typesafe"`): requests go to `https://api.typesafe.ai/v1/systemone`. Any other
  address works only if you set it, and then every start warns you, naming the host that will see your
  questions: in the terminal, on the app's page and in the plugin's results. An address on this computer
  (`localhost`, `127.0.0.1`) isn't flagged, since only a program you run there can answer it. `http://` is refused except to this machine. The key is never accepted in `config.toml`; it
  lives in `TYPESAFE_API_KEY`, which `ixel setup` saves with your other keys (see Keys, above). Keys saved
  in Ixel are withheld from the CLIs it runs, like every other key. A redirect is an error, never followed, so the key
  and your text can't be forwarded. Replies over 64 KB are refused.
- **Checked replies.** Probabilities and confidences must be numbers from 0 to 1, and anything unexpected
  counts as "no answer". A model that leaves its confidence out is taken as a coin flip, which never
  skips anything.
- **Failing safe.** No key, a timeout, an error or an odd reply only means the usual rounds run. A
  decision can only ever *skip a check*. It never runs anything, and it never changes an answer.
- **It can be lobbied, so it needs to be sure.** The answers triage reads were written by models, and one
  could try to talk it into "these all agree". So skipping needs high confidence (`threshold`, 0.9 by
  default), and skipping peer review is off unless you turn it on. In saver mode triage can only *add*
  caution to the old rule, or stand in for a reviewer who failed or was unsure. It can't overrule a
  reviewer who found a problem, and it can't skip a draft no other model graded.
- **Visible.** Every run shows what triage asked, what came back and what changed because of it: in the
  terminal, the browser app, the plugin's result and `--json`.

**How it's checked:** `tests/test_triage.py` covers the settings (a missing agent or unknown provider is
reported; for TypeSafe, another host is named in a warning, cleartext turns triage off, and a key in the
config file is refused), both clients (the fence around what your model reads, including an answer that
fakes the end marker; the Authorization header and no redirects for TypeSafe; timeouts, oversize and
malformed replies, and errors that never contain a key or model text), and every decision rule against
scripted panels. End-to-end runs check that only the question and the anonymous answers are sent.

### Installing on Windows

- The `ixel` command `install.ps1` makes (`ixel.cmd`) runs the environment's signed `python.exe` with
  `-I -m ixel_mat`, not the unsigned `ixel.exe` pip writes, which Smart App Control blocks. `-I` keeps the
  folder you run `ixel` in off Python's import path: plain `python -m` would look there first, so a cloned
  repository's `ixel_mat` folder or `mcp.py` would run. It also makes Python ignore `PYTHON*` variables,
  `PYTHONUTF8` and `PYTHONIOENCODING` among them, so text piped to or from `ixel` is in Windows' ANSI code
  page. A pipx or uv copy only has `ixel.exe`; if Windows blocks it, run `python -I -m ixel_mat` with that
  copy's Python.
- `ixel mcp --setup` gives apps `python.exe -m ixel_mat mcp` on Windows too, never `ixel.exe`. From a
  virtualenv (the installer's, pipx's, uv's) it adds `-I`. Outside one it adds `-P` (Python 3.11+), which
  keeps the folder the app starts it in off the import path but, unlike `-I`, keeps user site-packages,
  where `pip install` puts Ixel when it can't write to Python's own folder. Python 3.10 has no such flag, so
  outside a virtualenv it's plain `python -m` there.

### Machines (`ixel machines`, and the app's Machines page)

- **Only what you type or pick runs.** Machines is not something a model can use: nothing a model writes
  reaches it. Connect runs the machine's saved command (its agent's own, a shell, or one you typed), and
  Run on machines runs the one line you type, as typed.
- **No option or shell injection here.** A machine's host must be a DNS name, an IP address or a plain
  `Host` name, and its user a plain login name, so neither can start with `-` and be read by ssh as an
  option (`-oProxyCommand=…` would run a program on your computer). The destination always follows `--`.
  ssh, the terminal and `ssh-keygen` get their arguments as a list; Windows Terminal, which splits its own
  command line at `;`, gets only Python and a private one-shot script holding the exact list, and
  `cmd.exe` is never asked to read one. On a Mac, Terminal.app opens a private one-shot `/bin/sh` script
  that deletes itself, with each argument quoted (`shlex`). The line the page offers to paste
  (`ixel machines connect NAME`) is quoted for your shell too.
- **Trust on first use, then strict.** A server's key is learned by ssh itself (so `~/.ssh/config`'s
  HostName, Port and ProxyJump apply) into a temporary file, with every way of signing in switched off.
  A server can still let a client in with no credentials at all (and so could someone in between, before
  the key is checked), so nothing of yours goes along either: no ssh-agent, agent forwarding, X11, tunnel,
  port forward, `RemoteCommand`, or environment variables your config's `SendEnv` or `SetEnv` would send.
  A machine whose key is already pinned is asked for that key's type, so a server with several keys isn't
  taken for a changed one. The key is pinned in
  `~/.config/ixel-mat/machines_known_hosts` (0600) only when you say yes to the fingerprint you were shown,
  and the page must send back that exact fingerprint from a check made in the last 15 minutes. Importing
  from Ixel Console brings over the keys you already said yes to there, without asking again. Every
  connection then runs with `StrictHostKeyChecking=yes` against that file alone (not `~/.ssh/known_hosts`,
  not the system's, no DNS records, no `KnownHostsCommand`), with connection sharing off, so a server whose
  key changed is refused by ssh itself. A key is never pinned over or beside a different one already
  pinned for the same address (from the page, the terminal or an import): you forget the old one first, on
  purpose. A machine reached through a jump host or a `ProxyCommand` has its key pinned under that route,
  so the same private address on two networks is two servers.
- **A jump host is ssh's own hop.** With `ProxyJump`, ssh checks the jump host's key in your own
  `~/.ssh/known_hosts`, as it always does, and Ixel's options reach only the machine at the end, whose key
  is checked against the pin end to end. Connect to a new jump host once in a terminal first.
- **Runs ask nothing.** They run with `BatchMode=yes` and no terminal, so a password prompt or an askpass
  window can't appear; a key or ssh-agent signs in. (A jump host's hop is ssh's own and could ask; it
  can't wait past the run's time limit.) Each machine has a time limit, and output past 64 KB is counted
  but not kept. Stop, or closing Ixel, ends the ssh it started on your computer; ssh then closes its
  connection, which ends most commands, but one that writes nothing may keep running on the server. On
  Linux and macOS, if Ixel is killed outright, an ssh it started can carry on until its command ends.
- **Nothing leaves your computer but ssh itself.** The log (`machines.log`, 0600) records each connection,
  run, import, new key and key pinned, forgotten or changed: which machine, its address, how it went and the
  key fingerprints, never a command's text. It stays on your computer, in one file of at most 1 MB (the
  oldest lines go first), and a line older than 30 days goes the next time Ixel starts or writes to the
  log. A log an earlier Ixel wrote loses its commands, and its `machines.log.1`, the first time this
  version starts. An app window opened before the update still runs the earlier Ixel and can add a command
  until you close it; Ixel takes that out the next time it starts or writes to the log.
  `ixel forget` deletes it.
  A new key is made with no passphrase so runs need no prompt, and the page says how to add one
  (`ssh-keygen -p`); it becomes a machine's key only when you save. Copy my key sends the public key's own
  characters only.

### Updates

- `ixel update` runs `git pull --ff-only` in the folder you installed from, then that folder's installer.
  It refuses if you've changed files there, and never merges, resets or switches branches. A pipx or uv copy
  is updated by that tool (`pipx reinstall ixel-mat`, `uv tool upgrade ixel-mat`). A copy installed with
  plain pip is left alone, and `ixel update` tells you the command to run.
- The once-a-day check is a `git fetch` (or `git ls-remote`, for a pipx or uv install) that can't ask for
  anything. Terminal prompts, every askpass program and SSH passwords are switched off (`GIT_TERMINAL_PROMPT=0`,
  `GIT_ASKPASS=`, `ssh -o BatchMode=yes`). Credential helpers stay on only if they just read a login you've
  already saved: Git Credential Manager in never-interactive mode, the macOS keychain, libsecret, `store`,
  `cache`, `gh`. That way a private repository can still be checked. If any other helper is configured, the
  helper list is cleared for the check. It runs on a background thread with a time limit, so it can't hold
  up the app. It only counts new commits;
  nothing is installed until you run `ixel update`. Turn it off with `[updates] check = false` or
  `IXEL_NO_UPDATE_CHECK=1`.

## Deliberately not supported (yet)

- **Letting panel models change files or run commands.** Useful, but it needs per-action approval
  and a real sandbox first. See [ROADMAP.md](ROADMAP.md).
- **A network-reachable server** (for example, for ChatGPT's internet-only connectors). Ixel stays local.
- **Cursor's and Cline's CLIs.** Cline auto-approves every tool by default, and we haven't yet been
  able to prove either can be locked down the way the presets above are. Once a live test like the others
  passes, they'll be added.
