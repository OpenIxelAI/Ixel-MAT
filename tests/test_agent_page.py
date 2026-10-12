"""The Agent (Beta) page: it is served, it has its rail item, and its script stays inside the app's rules."""
from pathlib import Path

from ixel_mat.gui import server as gui_server
from test_gui_server import make_gui, run_with_client

STATIC = Path(gui_server.__file__).parent / "static"


def test_the_agent_script_and_the_pages_it_needs_are_served():
    """agent.js, app.js and style.css all come back from the server."""
    async def scenario(client):
        return [(await client.get(path)).status for path in ("/agent.js", "/app.js", "/style.css")]

    assert run_with_client(make_gui()[0], scenario) == [200, 200, 200]


def test_the_page_has_an_agent_rail_item_between_board_and_machines():
    """The Agent button has a Beta badge, and it sits in the rail after Board and before Machines."""
    async def scenario(client):
        return await (await client.get("/")).text()

    body = run_with_client(make_gui()[0], scenario)
    assert 'data-view="agent"' in body and "Agent" in body and "Beta" in body
    board = body.index('data-view="board"')
    agent = body.index('data-view="agent"')
    machines = body.index('data-view="machines"')
    assert board < agent < machines


def test_agent_js_is_listed_and_talks_to_the_run_api_without_parsing_html():
    """agent.js is in the server's list, polls the run endpoint, and never turns strings into markup."""
    assert "agent.js" in {name for name, _ in gui_server.STATIC_FILES.values()}
    code = (STATIC / "agent.js").read_text(encoding="utf-8")
    assert "/api/agent/run" in code
    for api in ("innerHTML", "href", 'setAttribute("on', "import("):
        assert api not in code, api
