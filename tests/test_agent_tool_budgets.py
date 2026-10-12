"""
The agent's result budgets (ixel_mat/agent/tools.py and protocol.py): read_file's 400 lines, the prompt's result
clipping, heavy folders skipped, and the nearest lines on a missed edit.
"""
import pytest

from ixel_mat.agent.protocol import build_prompt, MAX_RECENT_RESULT_CHARS, MAX_OLDER_RESULT_CHARS, RECENT_STEPS
from ixel_mat.agent.tools import Workspace, WorkspaceError

FENCE = "IXEL-test"


@pytest.fixture
def ws(tmp_path) -> Workspace:
    root = tmp_path / "ws"
    root.mkdir()
    return Workspace(root)


def _write(ws: Workspace, rel: str, text: str) -> None:
    path = ws.root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exactly these bytes: read_file keeps a file's line endings, and write_text would write \r\n on Windows
    path.write_bytes(text.encode("utf-8"))


def _step(n: int, result: str) -> dict:
    return {"n": n, "thought": "look", "action": "read_file", "args": {"path": "big.txt"}, "ok": True,
            "result": result}


# ── read_file and the prompt ─────────────────────────────────────────────────

def test_read_file_without_a_range_gives_the_first_400_lines(ws):
    _write(ws, "long.txt", "".join(f"line {n}\n" for n in range(1, 1001)))
    text = ws.read_file("long.txt")
    lines = text.splitlines()
    assert "line 400" in lines
    assert "line 401" not in lines
    assert "1000 lines in all; use start and end for more" in text


def test_read_file_returns_a_file_of_400_lines_or_fewer_whole(ws):
    exact = "".join(f"row {n}\n" for n in range(1, 401))
    _write(ws, "exact.txt", exact)
    assert ws.read_file("exact.txt") == exact
    _write(ws, "short.txt", "a\nb\nc\n")
    assert ws.read_file("short.txt") == "a\nb\nc\n"


def test_read_file_with_a_range_past_line_400(ws):
    _write(ws, "long.txt", "".join(f"line {n}\n" for n in range(1, 1001)))
    assert ws.read_file("long.txt", start=401, end=402) == "line 401\nline 402\n"


def test_read_file_still_refuses_a_binary_file_with_many_lines(ws):
    # A NUL byte after line 400 must not be cut away with the lines past the limit, or the file would read as text
    _write(ws, "mixed.bin", "a\n" * 500 + "\x00" + "b\n" * 10)
    with pytest.raises(WorkspaceError, match="isn't a text file"):
        ws.read_file("mixed.bin")


def test_prompt_sends_only_the_newest_result_in_full():
    steps = [
        _step(1, "HEAD" + "m" * 10_000 + "TAIL"),
        _step(2, "HEAD" + "m" * 10_000 + "TAIL"),
        _step(3, "NEWEST" + "n" * 20_000),
    ]
    prompt = build_prompt("goal", {}, [], steps, fence=FENCE)
    assert "HEAD" in prompt and "TAIL" in prompt
    assert "m" * 5_000 not in prompt
    assert "characters cut" in prompt
    assert "NEWEST" + "n" * 20_000 in prompt


def test_newest_result_longer_than_the_limit_ends_with_a_note():
    prompt = build_prompt("goal", {}, [], [_step(1, "x" * (MAX_RECENT_RESULT_CHARS + 500))], fence=FENCE)
    assert "x" * MAX_RECENT_RESULT_CHARS + "\n[… truncated]\n</IXEL-test>" in prompt
    assert "x" * (MAX_RECENT_RESULT_CHARS + 1) not in prompt


def test_older_result_of_3000_characters_is_kept_whole():
    result = "older " + "o" * 2_994  # 3,000 characters: under twice MAX_OLDER_RESULT_CHARS
    assert len(result) < 2 * MAX_OLDER_RESULT_CHARS
    prompt = build_prompt("goal", {}, [], [_step(1, result), _step(2, "newest")], fence=FENCE)
    assert result in prompt
    assert "characters cut" not in prompt


def test_only_the_newest_step_is_sent_in_full():
    assert RECENT_STEPS == 1


# ── heavy folders ────────────────────────────────────────────────────────────

def test_list_dir_leaves_out_heavy_folders_and_names_them(ws):
    _write(ws, "a.txt", "hello")
    _write(ws, "node_modules/pkg/index.js", "x")
    _write(ws, "__pycache__/x.pyc", "x")
    _write(ws, "src/app.py", "x")
    lines = ws.list_dir(".").splitlines()
    assert "d src/" in lines and "f a.txt 5" in lines
    assert lines[-1] == "[skipped __pycache__/, node_modules/: heavy folders; list one by naming it as path]"
    assert not any("node_modules" in line or "__pycache__" in line for line in lines[:-1])


def test_list_dir_of_a_heavy_folder_named_as_path_lists_inside_it(ws):
    _write(ws, "node_modules/pkg/index.js", "x")
    assert ws.list_dir("node_modules") == "d pkg/"


def test_a_folder_holding_only_a_heavy_folder_says_so(ws):
    _write(ws, "web/dist/app.js", "x")
    assert ws.list_dir("web") == "[skipped dist/: heavy folders; list one by naming it as path]"


def test_search_skips_heavy_folders_and_names_what_it_skipped(ws):
    _write(ws, "src/app.py", "needle\n")
    _write(ws, "node_modules/pkg/index.js", "needle\n")
    _write(ws, "build/out.js", "needle\n")
    assert ws.search("needle") == ("src/app.py:1: needle\n"
                                   "[skipped build/, node_modules/: heavy folders; search one by naming it as path]")


def test_search_inside_a_heavy_folder_named_as_path_searches_it(ws):
    _write(ws, "node_modules/pkg/index.js", "needle\n")
    assert ws.search("needle", "node_modules") == "node_modules/pkg/index.js:1: needle"


def test_search_under_a_nested_heavy_folder_named_as_path_searches_it(ws):
    _write(ws, "web/dist/app.js", "needle\n")
    assert ws.search("needle", "web/dist") == "web/dist/app.js:1: needle"


def test_search_with_no_match_still_names_the_skipped_folder(ws):
    _write(ws, "src/app.py", "hello\n")
    _write(ws, "node_modules/pkg/index.js", "needle\n")
    assert ws.search("needle") == ("no lines contain 'needle'\n"
                                   "[skipped node_modules/: heavy folders; search one by naming it as path]")


def test_a_file_named_build_is_still_searched(ws):
    _write(ws, "build", "needle\n")
    assert ws.search("needle") == "build:1: needle"
