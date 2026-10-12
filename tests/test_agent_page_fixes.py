"""The Agent page's demo fixes: /commands run and never send, the add-on card shows its text, scratch commands are marked."""
from pathlib import Path

from ixel_mat.gui import server as gui_server

STATIC = Path(gui_server.__file__).parent / "static"


def test_enter_runs_a_slash_command_before_it_can_send():
    """The Enter path checks for a /command in the box and runs it; only when there isn't one does it send."""
    code = (STATIC / "agent.js").read_text(encoding="utf-8")
    assert r"!/^\/[a-z]*$/i.test(word)" in code
    assert "function submitBox()" in code
    assert "if (!runTyped()) send();" in code
    assert code.index("if (!runTyped()) send();") > code.index("function submitBox()")


def test_the_add_on_card_approval_shows_the_whole_card_in_a_pre():
    """write_addon_card shows pending.diff in a <pre>, says where it is written, that it stays off, and where to approve it."""
    code = (STATIC / "agent.js").read_text(encoding="utf-8")
    assert 'pending.action === "write_addon_card"' in code
    assert 'el("pre", { class: "agent-command" }, pending.diff' in code
    for part in ("addon.toml", "Settings > Add-ons", "It stays off until you approve it"):
        assert part in code, part


def test_scratch_commands_have_no_always_allow_and_say_where_they_run():
    """A run_command with a cwd starting 'scratch:' is marked, gets no Always allow, and its sandbox line names Ixel's copy."""
    code = (STATIC / "agent.js").read_text(encoding="utf-8")
    assert 'args.cwd.startsWith("scratch:")' in code
    assert "(command && !computerOn && !scratch) || addon" in code
    assert code.count("Ixel's copy of the downloaded repo, not the workspace") == 2


def test_the_fixes_add_no_innerhtml():
    """The fixes use text nodes only, so agent.js still never turns a string into markup."""
    code = (STATIC / "agent.js").read_text(encoding="utf-8")
    for api in ("innerHTML", "insertAdjacentHTML", "outerHTML"):
        assert api not in code, api
