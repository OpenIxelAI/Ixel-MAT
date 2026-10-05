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
or LM Studio, and your Claude Code, Codex, Gemini CLI, GitHub Copilot and OpenCode sign-ins. Each model answers
on its own, grades the others' answers without knowing whose they are, and a moderator writes the verdict, with
the mistakes the panel caught.

**[ixelai.com/ixel-mat](https://ixelai.com/ixel-mat/)** · [Docs](https://ixelai.com/docs/) ·
[Privacy](https://ixelai.com/docs/privacy/)

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
the installer offers to get them where it can. It installs Ixel MAT with the Ixel app. For
[Handoff](https://ixelai.com/handoff/) too, [install all of Ixel](https://ixelai.com/docs/install/#pick) instead.

To update or uninstall, see the [install guide](https://ixelai.com/docs/install/). To install from source, or
with pipx or uv, see [CONTRIBUTING.md](CONTRIBUTING.md).

## Use it

- **App:** **Ixel** in the Start Menu, Applications or your app menu, or `ixel app`.
- **Terminal:** `ixel`, then type a question. `/review`, `/saver` and the rest are in [the terminal guide](https://ixelai.com/docs/terminal/).
- **Plugin** for Claude Desktop, Claude Code, Codex and Cursor: `ixel mcp --setup`, then ask your app for "a
  second opinion from the Ixel panel".

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
