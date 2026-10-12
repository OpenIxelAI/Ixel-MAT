"""The Board Run form's fallback line: unticking single agents."""
from pathlib import Path

from test_gui_server import make_gui, run_with_client

STATIC = Path(__file__).resolve().parent.parent / "ixel_mat" / "gui" / "static"


def test_board_js_is_served_with_the_agent_toggles():
    async def scenario(client):
        resp = await client.get("/board.js")
        return resp.status, await resp.text()

    status, body = run_with_client(make_gui()[0], scenario)
    assert status == 200
    for text in ("fallback-agents", "fallback-agent", "needs-fallback-agent:", "Go on with ", "state.off"):
        assert text in body, text


def test_style_css_has_the_agent_toggles():
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert ".needs .fallback-agents {" in css and ".needs .fallback-agent {" in css


def test_needs_form_appends_the_agent_toggles_after_the_fallback_row():
    board = (STATIC / "board.js").read_text(encoding="utf-8")
    assert "return { row, agents, refresh, args };" in board
    assert "form.append(backup.row, backup.agents);" in board


def test_board_js_says_the_run_stops_and_switches_off_when_every_agent_is_unticked():
    board = (STATIC / "board.js").read_text(encoding="utf-8")
    assert "If a model hits its usage limit, the run stops there" in board
    assert "if (!kept().length) state.on = false;" in board
