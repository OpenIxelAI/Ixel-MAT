"""The Agent page as a chat: its files are served, it has the composer, the slash commands and the new API calls."""
from pathlib import Path

from ixel_mat.gui import server as gui_server
from test_gui_server import make_gui, run_with_client

STATIC = Path(gui_server.__file__).parent / "static"


def test_the_agent_page_files_are_served():
    """The page, agent.js, markdown.js and style.css all come back from the server."""
    async def scenario(client):
        return [(await client.get(path)).status for path in ("/", "/agent.js", "/markdown.js", "/style.css")]

    assert run_with_client(make_gui()[0], scenario) == [200, 200, 200, 200]


def test_the_page_has_the_composer_attach_button_and_chips():
    """The page has the composer, attach button, option chips, top-bar buttons and dialog, and no side column."""
    async def scenario(client):
        return await (await client.get("/")).text()

    body = run_with_client(make_gui()[0], scenario)
    for part in ('id="agent-composer"', 'id="agent-box"', 'id="agent-attach"', 'id="agent-upload-input"',
                 'id="agent-send"', 'id="agent-chip-folder"', 'id="agent-chip-model"', 'id="agent-chip-fast"',
                 'id="agent-chip-computer"', 'id="agent-history-button"', 'id="agent-memory-button"',
                 'id="agent-dialog"', 'id="agent-slash"', 'id="agent-conversation"',
                 "Ask the agent to do something"):
        assert part in body, part
    assert 'class="agent-side"' not in body


def test_agent_js_has_every_slash_command():
    """Each of the ten slash commands is a quoted string in agent.js."""
    code = (STATIC / "agent.js").read_text(encoding="utf-8")
    for command in ("/new", "/model", "/folder", "/fast", "/effort", "/computer",
                    "/attach", "/memory", "/stop", "/help"):
        assert f'"{command}"' in code, command


def test_agent_js_talks_to_the_beta_2_api():
    """agent.js uploads files, starts runs with follow-ups, confirmation, uploads and Computer access."""
    code = (STATIC / "agent.js").read_text(encoding="utf-8")
    for part in ("/api/agent/upload", "/api/agent/start", "follows", "confirmed", "uploads", "computer"):
        assert part in code, part


def test_agent_js_never_uses_innerhtml_or_browser_storage():
    """agent.js never turns strings into markup and never keeps anything in browser storage."""
    code = (STATIC / "agent.js").read_text(encoding="utf-8")
    for api in ("innerHTML", "outerHTML", "localStorage", "sessionStorage", "document.cookie",
                "indexedDB", 'setAttribute("on'):
        assert api not in code, api


def test_the_agent_styles_cover_the_composer_and_chips():
    """style.css has the rules for the composer, the chips, the slash menu, the activity group and your messages."""
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    for selector in (".agent-composer", ".agent-chip", ".agent-slash", ".agent-activity", ".agent-you"):
        assert selector in css, selector
