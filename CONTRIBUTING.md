# Working on Ixel MAT

## Install from source

You need Python 3.10 or newer and Git. The installer makes a private environment in your home folder, adds the
`ixel` command and the Ixel app, and never uses `sudo` or touches system Python.

macOS and Linux (it needs bash; tried on Ubuntu, Debian, Fedora and Arch):

```bash
git clone https://github.com/OpenIxelAI/ixel-mat.git
cd ixel-mat
./install.sh
```

If Python is missing or too old, it names the command to get it (`brew install python@3.13`, or
`sudo apt install python3-venv` on Debian and Ubuntu). It uses [uv](https://docs.astral.sh/uv/) when it's
installed. Set `IXEL_PYTHON=/path/to/python` to choose the interpreter.

Windows, in a normal PowerShell window (not "Run as administrator"), one line at a time:

```powershell
cd ~
git clone https://github.com/OpenIxelAI/ixel-mat.git
cd ixel-mat
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
```

If Git or Python is missing: `winget install Git.Git` or `winget install Python.Python.3.13`, then open a new
window and start again.

**With [pipx](https://pipx.pypa.io) or [uv](https://docs.astral.sh/uv/)** (any system). These don't add Ixel to
the Start Menu, Applications or your app menu, but `ixel app` still opens its window:

```bash
pipx install git+https://github.com/OpenIxelAI/ixel-mat.git
uv tool install git+https://github.com/OpenIxelAI/ixel-mat.git
```

On Windows, Smart App Control can block the unsigned `ixel.exe` that pip writes, so the installer's `ixel` runs
the environment's Python instead (`python.exe -I -m ixel_mat`). A pipx or uv copy has only `ixel.exe`: if
Windows blocks it, run `python -I -m ixel_mat` with that copy's Python (`pipx environment` or `uv tool dir`
shows where it is). `ixel update` updates an install made by the installer, pipx or uv; for a checkout, `git
pull` and reinstall.

## Run the tests

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest                    # the full suite takes up to a quarter of an hour
```

The browser tests need Playwright (`pip install playwright && playwright install chromium`). The live CLI
checks run for each subscription CLI that's installed, on macOS and Linux.

**On Windows**, one command runs the tests, installs Ixel into a temporary folder with `install.ps1`, and checks
the `ixel` you use, including whether Windows lets it start. It writes `windows-check-report.txt`; attach it to
an issue. It sends nothing to a model provider and leaves your own install and settings alone.

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
.\.venv\Scripts\python scripts\check_windows.py
```

On GitHub, every pull request and every push to `main` runs the tests on Linux and Windows (`checks`). The full
run, with the installers, macOS, the live CLI checks and the browser test, starts only by hand (**Actions → tests →
Run workflow**). Run `pytest` before you push either way.

See [ARCHITECTURE.md](ARCHITECTURE.md) for how the code fits together.
