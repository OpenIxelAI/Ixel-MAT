"""What the `ixel` subcommands do with --help, with no terminal, and with bad arguments under --json."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

EXAMPLE = Path(__file__).resolve().parents[1] / "config.example.toml"


def ixel(home, *args, stdin=subprocess.DEVNULL):
    env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home), "PYTHONIOENCODING": "utf-8", "COLUMNS": "200"}
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY", "XAI_API_KEY"):
        env.pop(name, None)
    return subprocess.run([sys.executable, "-m", "ixel_mat", *args], cwd=home, env=env, stdin=stdin,
                          capture_output=True, text=True, encoding="utf-8", timeout=60)


@pytest.mark.parametrize("command", ["status", "config", "agents", "saves", "setup", "version"])
def test_help_shows_what_a_command_does_instead_of_running_it(tmp_path, command):
    proc = ixel(tmp_path, command, "--help")
    assert proc.returncode == 0 and proc.stdout.startswith(f"usage: ixel {command}"), proc.stdout + proc.stderr
    assert not (tmp_path / ".config").exists()  # nothing ran


def test_setup_without_a_terminal_says_so_instead_of_crashing(tmp_path):
    proc = ixel(tmp_path, "setup")
    said = "needs a terminal" if os.name != "nt" else "Setup stopped before the end"  # Git Bash: see cmd_setup
    assert proc.returncode == 1 and said in proc.stdout and "Traceback" not in proc.stdout + proc.stderr


@pytest.mark.parametrize("args", [["ask", "--json", "q"], ["image", "--json", "-n", "11", "a lighthouse"],
                                  ["ask", "--json", "--timeout", "0", "--agent", "gpt", "q"]],
                         ids=["no-agent", "bad-count", "bad-timeout"])
def test_json_mode_reports_bad_arguments_as_json(tmp_path, args):
    proc = ixel(tmp_path, *args)
    assert proc.returncode == 1, proc.stderr
    assert json.loads(proc.stdout)["error"].startswith(f"ixel {args[0]}: ")


def test_config_shows_agents_as_ixel_runs_them(tmp_path):
    folder = tmp_path / ".config" / "ixel-mat"
    folder.mkdir(parents=True)
    shutil.copy(EXAMPLE, folder / "config.toml")
    out = ixel(tmp_path, "config").stdout
    claude_code = out[out.index("claude_code — Claude Code"):].split("\n\n")[0]
    assert "type: oneshot" in claude_code and "command: claude" in claude_code and "not needed" in claude_code
    llama = out[out.index("llama —"):].split("\n\n")[0]
    assert "not needed" in llama and "missing" not in llama
    assert "type: ?" not in out
