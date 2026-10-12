<p align="center">
  <a href="https://ixelai.com/ixel-mat/"><img src="assets/ixel-logo.png" alt="Ixel" width="200"></a>
</p>

# Ixel MAT

**Ask several AI models at once. They check each other's work anonymously and agree on one answer.**

<p align="center">
  <a href="https://ixelai.com/videos/ixel-commercial-no-voice.mp4"><img src="https://ixelai.com/videos/ixel-panel-loop.gif" alt="The Ixel app: the models grade each other blind, then one verdict comes back with the mistakes they caught" width="720"></a>
  <br>
  <sub><a href="https://ixelai.com/videos/ixel-commercial-no-voice.mp4">Watch the 44-second film</a> · a demo: the models' replies are scripted</sub>
</p>

Ixel MAT puts the models you already use on one panel: Claude, GPT, Gemini, Grok, local models through Ollama
or LM Studio, and your Claude Code, Codex, Gemini CLI, GitHub Copilot, OpenCode and Grok Build (SuperGrok)
sign-ins, and Antigravity (Google AI Pro) if you switch it on (Google's terms may restrict it). Each model answers
on its own, grades the others' answers without knowing whose they are, and a moderator writes the verdict, with
the mistakes the panel caught. Attach Word, PDF, Excel or PowerPoint files and pictures: documents are read on
your computer, and pictures go to the models that see them, your subscription sign-ins included (all but Grok
Build), with no API key.

**[ixelai.com/ixel-mat](https://ixelai.com/ixel-mat/)** · [Docs](https://ixelai.com/docs/) ·
[Privacy](https://ixelai.com/docs/privacy/)

## What it is, and what it isn't

- Ixel has no account and no telemetry. Your questions go only to the AI services you set up, and each key stays
  on your computer. Once a day it checks the repository you installed it from for updates, and installs nothing;
  you can turn that off.
- It is not a model and not a hosted service. It uses your own API keys, subscription sign-ins and local models,
  so calls are billed to you or count against your plan.
- The panel agreeing doesn't make an answer right. Models can share the same mistake, so check what matters.
- It acts only when you ask. The panel and reviews only answer; an agent you set up yourself keeps whatever tools
  it has. The Agent (beta) asks before it edits a file, runs a command or downloads a repository, and, with
  Computer access on, before a screenshot or opening a file, link or app. A command you approved with "Always
  allow" runs again without asking, while Computer access is off.
- It is not an unattended agent. A run starts when you start it, pauses whenever it needs your approval, and ends
  at a step limit (25 by default). The beta has no cron jobs and no messaging channels. The Board's Merge pushes
  nothing.
- The Agent's sandbox has limits. A command it runs has no network and can write only in the workspace and the
  temp folder, but only on macOS (`sandbox-exec`) and on Linux (bubblewrap, which has to be installed and allowed
  to start), and only while Computer access is off. It can still read your files, except that on Linux your SSH,
  cloud and browser folders and Ixel's own config folder are hidden from it. On Windows, on Linux without a
  working bubblewrap, or with Computer access on, an approved command runs as you and can do anything you can.
  The approval card says which.

## Install

Windows, in a normal PowerShell window (not "Run as administrator"):

```powershell
irm https://ixelai.com/ixel-mat/install.ps1 | iex
```

macOS or Linux:

```bash
curl -fsSL https://ixelai.com/ixel-mat/install.sh | sh
```

Then open a new terminal and run `ixel setup` to choose your models. You need Python 3.10 or newer and Git;
the installer offers to get them where it can. It installs Ixel MAT with the Ixel app. On Linux it also offers the
GTK and WebKit packages Ixel's own window needs (it asks first); without them, `ixel app` opens in your browser
instead. For [Handoff](https://ixelai.com/handoff/) too,
[install all of Ixel](https://ixelai.com/docs/install/#pick) instead.

To update or uninstall, see the [install guide](https://ixelai.com/docs/install/). To install from source, or
with pipx or uv, see [CONTRIBUTING.md](CONTRIBUTING.md).

## Use it

- **App:** **Ixel** in the Start Menu, Applications or your app menu, or `ixel app`.
- **Ask:** the panel. Several models answer, grade each other blind, and one verdict comes back.
- **Board:** [Handoff's](https://ixelai.com/handoff/) board for a project; it needs Handoff. For a task that changes
  files, pick a model or a chain of models, and the effort for each. Each task shows what it cost, and the run
  form says who takes over if a model hits its usage limit. **Merge** puts a task's branch into the branch checked
  out on this computer, which may not be `main`. Nothing is pushed.
- **Agent (beta):** a personal agent that works in a folder you pick, one step at a time. It asks you before it
  edits a file or runs a command, and, with Computer access on, before it takes a screenshot or opens a file, link
  or app. A command you approved with "Always allow" runs again without asking, but only while Computer access is
  off. You can give it a photo, a video or any file. **Computer access** is off by default. On macOS and Linux its
  commands run in a sandbox with no network, unless Computer access is on. See [AGENT_BETA.md](AGENT_BETA.md).
- **Machines:** your servers over SSH: connect to one, or run one command on several.
- **Health:** whether each part of Ixel is ready, and the one line that fixes what isn't.
- **Settings:** which models are on the panel and how each one thinks, the default mode, and your keys.
- **Terminal:** `ixel`, then type a question. `/review`, `/saver` and the rest are in [the terminal guide](https://ixelai.com/docs/terminal/).
- **Plugin** for Claude Desktop, Claude Code, Codex and Cursor: `ixel mcp --setup`, then ask your app for "a
  second opinion from the Ixel panel".
- **Add-ons:** Settings → Add-ons lists each add-on's card and asks you to approve what it may do before it's
  switched on. [Diagram Design](https://github.com/cathrynlavery/diagram-design) (MIT, by Cathryn Lavery) comes
  with Ixel; once it's on, the Diagram toggle in Ask (with a choice of model), or `/diagram` before a question,
  answers with a diagram as one HTML file.

## Privacy

Ixel has no account and no telemetry. Your questions go only to the AI companies you set up, and each key is
kept on your computer and sent only to its own company. Some companies train on what you send:
[which ones, and how to keep your questions out](https://ixelai.com/docs/privacy/#training).

## More

- [config.example.toml](config.example.toml): every setting, with an example of each.
- [CONTRIBUTING.md](CONTRIBUTING.md): run it from source and run the tests.
- [ARCHITECTURE.md](ARCHITECTURE.md): how the code fits together. [ROADMAP.md](ROADMAP.md): what's next.
- [SECURITY.md](SECURITY.md): what Ixel protects. Report a security problem privately to **openixel.ai@proton.me**.

## License

[MIT](LICENSE)
