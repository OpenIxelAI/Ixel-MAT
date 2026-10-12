"""Add-on cards: reading and checking a card, approving one, installing its pinned release safely, the Settings
routes that list and switch cards, and Diagram Design in Ask. Nothing here uses the network, and everything it
writes is in the test's own folder."""
import asyncio
import copy
import hashlib
import io
import json
import os
import re
import shutil
import stat
import tarfile
from datetime import datetime
from pathlib import Path

import pytest

from ixel_mat import addons
from ixel_mat.addons import installer, paths
from ixel_mat.agents.base import AgentConfig
from ixel_mat.ask import AskError, AskResult
from ixel_mat.config import edit, loader
from ixel_mat.gui import server as gui_server
from ixel_mat.gui.server import GuiServer
from ixel_mat.material import MAX_MATERIAL_CHARS
from ixel_mat.runtime import Settings, load_settings
from ixel_mat.usage import Price
from test_gui_server import AUTH, JSON_AUTH, TOKEN, read_events, run_with_client

DIAGRAM = addons.DIAGRAM_ID
TOP = "diagram-design-7a2e22170ee8"
SUB = "skills/diagram-design"
REAL_CARD = Path(addons.__file__).parent / "diagram-design.toml"
PIN_COMMIT = "c" * 40
# The release Ixel pinned before cards, which the built-in card must still pin
OLD_COMMIT = "7a2e22170ee838e859d8142ba825d83f8b6375e6"
OLD_TREE = "e4adecc695f6367ce202995133e6e1fe5d18b6e4e142a06bd30926d72bfbb4a1"

SELF_CHECK = '''\
import sys
from pathlib import Path

if "<!doctype html" in Path(sys.argv[1]).read_text(encoding="utf-8"):
    sys.exit(0)
print("missing doctype")
sys.exit(1)
'''

# The skill, as the files under skills/diagram-design/ in the archive
SKILL_FILES = {
    "SKILL.md": "# Diagram Design\n\nMake one HTML file. Follow the style guide and the type's reference.\n",
    "references/style-guide.md": "# Style guide\n\nUse the brand's colours and one typeface.\n",
    "references/type-flowchart.md": "# Flowchart\n\nBoxes joined by arrows, top to bottom.\n",
    "references/type-architecture.md": "# Architecture\n\nLayers, services and what talks to what.\n",
    "assets/example-flowchart.html": "<!doctype html>\n<html><body><p>a flowchart example</p></body></html>\n",
    "scripts/self_check.py": SELF_CHECK,
}
# Everything else in the repository's archive: none of it is unpacked when a card's subdir is the skill's folder
OUTSIDE = {
    "README.md": "# The repository\n",
    "LICENSE": "MIT\n",
    "docs/guide.md": "Not part of the skill.\n",
    "skills/other-skill/SKILL.md": "# Another skill\n",
    "skills/diagram-design-extras/notes.md": "A folder whose name only starts the same.\n",
}

GOOD = "<!doctype html>\n<html><head><title>Release</title></head><body><svg></svg></body></html>\n"
BAD = "<html><body>no doctype here</body></html>\n"

CFG = AgentConfig(name="gpt", label="GPT", type="http", url="https://api.example.com", token="sk-never-shown",
                  model="m-test")
LOCAL = AgentConfig(name="llama", label="Llama", type="http", url="http://127.0.0.1:11434/v1/chat/completions",
                    model="llama3")
CLAUDE_CODE = AgentConfig(name="claude", label="Claude Code", type="oneshot", command="claude",
                          model_args=["--model", "{model}"])


def _add(tar, name, data=b"", kind=tarfile.REGTYPE, link=""):
    info = tarfile.TarInfo(name)
    info.type = kind
    info.mode = 0o755 if kind == tarfile.DIRTYPE else 0o600  # the archive's modes are never used
    info.linkname = link
    if kind == tarfile.REGTYPE:
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    else:
        tar.addfile(info)


def tarball(files=None, *, extra=(), top=TOP) -> bytes:
    """A GitHub-style .tar.gz in memory: one top folder with the repository's files, the skill's under
    skills/diagram-design/. extra: more members, each a dict of _add's arguments."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for folder in (top, f"{top}/skills", f"{top}/{SUB}", f"{top}/{SUB}/references", f"{top}/{SUB}/assets"):
            _add(tar, folder, kind=tarfile.DIRTYPE)
        for rel, text in (SKILL_FILES if files is None else files).items():
            _add(tar, f"{top}/{SUB}/{rel}", text.encode("utf-8"))
        for rel, text in OUTSIDE.items():
            _add(tar, f"{top}/{rel}", text.encode("utf-8"))
        for member in extra:
            _add(tar, **member)
    return buf.getvalue()


def expected_tree(files) -> tuple[int, str]:
    """The pin of a set of files, worked out from how addons.tree_hash describes itself: the sha256 of
    f"{rel}\\0{sha}\\n" for every file, sorted by path."""
    lines = "".join(f"{rel}\0{hashlib.sha256(text.encode('utf-8')).hexdigest()}\n"
                    for rel, text in sorted(files.items()))
    return len(files), hashlib.sha256(lines.encode("utf-8")).hexdigest()


TARBALL = tarball()
TREE_SHA = expected_tree(SKILL_FILES)[1]
URL = "https://codeload.github.com/cathrynlavery/diagram-design/tar.gz/" + PIN_COMMIT


# ── Cards written for the tests ──────────────────────────────────────────────

DROP = object()  # as a change to card_text: take the key out

# A valid agent card whose pin is the test tarball's skill, so it can be installed from TARBALL
CARD = {
    "id": "pdf-notes",
    "name": "PDF Notes",
    "summary": "Turns a PDF into short notes.",
    "by": "A. Writer",
    "license": "MIT",
    "homepage": "https://github.com/someone/pdf-notes",
    "kind": "skill",
    "terms": "MIT: keep the copyright notice with copies of its files.",
    "source": {"repo": "https://github.com/someone/pdf-notes", "commit": "d" * 40, "subdir": SUB,
               "tree_sha256": TREE_SHA, "files": len(SKILL_FILES)},
    "permissions": {"files": "none", "network": False, "commands": []},
    "use": {"where": "agent", "trigger": "/notes", "output": "text"},
}


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value)  # JSON's string escapes are TOML's
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise TypeError(value)


def card_text(**changes) -> str:
    """Valid card text, with changes made to it: a top-level key as is (id="x"), one in a table with a double
    underscore (source__commit="x", permissions__commands=[[...]]), DROP to take a key out."""
    data = copy.deepcopy(CARD)
    for key, value in changes.items():
        *parents, name = key.split("__")
        target = data
        for part in parents:
            target = target[part]
        if value is DROP:
            del target[name]
        else:
            target[name] = value
    lines = [f"{key} = {_toml_value(value)}" for key, value in data.items() if not isinstance(value, dict)]
    for table, values in data.items():
        if isinstance(values, dict):
            lines += ["", f"[{table}]"] + [f"{key} = {_toml_value(value)}" for key, value in values.items()]
    return "\n".join(lines) + "\n"


def put_card(folder_name: str, text: str) -> Path:
    """A card as the agent would leave it: ADDONS_DIR/<folder_name>/addon.toml, with exactly these bytes."""
    path = paths.ADDONS_DIR / folder_name / "addon.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def pinned_text() -> str:
    """The real built-in card, with only its pin changed to the test tarball's."""
    text = REAL_CARD.read_text(encoding="utf-8")
    text = re.sub(r'(?m)^commit = "[^"]*"', f'commit = "{PIN_COMMIT}"', text)
    text = re.sub(r'(?m)^tree_sha256 = "[^"]*"', f'tree_sha256 = "{TREE_SHA}"', text)
    return re.sub(r"(?m)^files = \d+", f"files = {len(SKILL_FILES)}", text)


def saved_state() -> dict:
    return json.loads(paths.state_path().read_text(encoding="utf-8"))


def write_old_layout(card) -> None:
    """The skill's files straight in ~/.ixel/addons/<id>/ (SKILL.md at its root), as installed before cards."""
    for rel, text in SKILL_FILES.items():
        path = card.dir.joinpath(*rel.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _folders(tmp_path, monkeypatch):
    """Add-ons, diagrams and built-in cards are in the test's own folders, never in ~/.ixel or the package: no
    built-in cards at all, until a test asks for `builtin`."""
    monkeypatch.setattr(paths, "ADDONS_DIR", tmp_path / "ixel-home" / "addons")
    monkeypatch.setattr(paths, "DIAGRAMS_DIR", tmp_path / "ixel-home" / "diagrams")
    none = tmp_path / "no-builtin-cards"
    none.mkdir()
    monkeypatch.setattr(paths, "BUILTIN_DIR", none)


@pytest.fixture
def builtin(tmp_path, monkeypatch):
    """Diagram Design as the only built-in card: the real card, pinned to the test tarball."""
    folder = tmp_path / "builtin-cards"
    folder.mkdir()
    (folder / "diagram-design.toml").write_bytes(pinned_text().encode("utf-8"))
    monkeypatch.setattr(paths, "BUILTIN_DIR", folder)
    card = addons.card_by_id(DIAGRAM)
    assert card is not None and card.builtin
    return card


@pytest.fixture
def diagram_on(builtin):
    """Diagram Design installed from the test tarball, approved and switched on."""
    addons.install(builtin, fetch=lambda url: TARBALL)
    addons.approve_and_switch_on(builtin)
    card = addons.card_by_id(DIAGRAM)
    assert card.enabled
    return card


@pytest.fixture
def release(monkeypatch):
    """Downloading gives the test tarball. Returns the urls asked for."""
    fetched = []

    def download(url, **kwargs):
        fetched.append(url)
        return TARBALL

    monkeypatch.setattr(installer, "download", download)
    return fetched


@pytest.fixture
def skill(tmp_path):
    """The skill unpacked, as it sits once installed."""
    root = tmp_path / "skill"
    for rel, text in SKILL_FILES.items():
        path = root.joinpath(*rel.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


# ── Reading a card ───────────────────────────────────────────────────────────

def test_the_real_builtin_card_validates_and_pins_the_release_that_was_pinned_before_cards():
    card = addons.load_card(REAL_CARD, builtin=True)
    assert (card.id, card.name, card.kind, card.builtin) == (DIAGRAM, "Diagram Design", "skill", True)
    assert card.card_path == REAL_CARD
    assert card.source.commit == OLD_COMMIT and card.source.tree_sha256 == OLD_TREE
    assert card.source.files == 279 and card.source.subdir == "skills/diagram-design"
    assert card.source.repo == "https://github.com/cathrynlavery/diagram-design"
    assert card.source["repo"] == card.source.repo
    assert card.permissions.commands == [["python3", "{addon}/scripts/self_check.py", "{output}"]]
    assert card.use.trigger == "/diagram" and card.terms
    assert card.dir == paths.ADDONS_DIR / "diagram-design"
    assert card.files_dir == paths.ADDONS_DIR / "diagram-design" / "files"
    assert card.card_sha256 == hashlib.sha256(REAL_CARD.read_bytes()).hexdigest()
    assert (card.on, card.approved, card.changed_since_approval, card.installed, card.enabled) == (False,) * 5
    with pytest.raises(KeyError):
        card.source["nope"]
    data = card.to_dict()
    assert json.loads(json.dumps(data)) == data
    assert data["files_dir"] == str(card.files_dir) and data["source"]["files"] == 279


def test_a_valid_card_is_placed_as_an_agent_card_with_all_its_state_off():
    text = card_text()
    card = addons.validate_card_text(text)
    assert card.id == "pdf-notes" and card.builtin is False
    assert card.card_path == paths.ADDONS_DIR / "pdf-notes" / "addon.toml"
    assert card.dir == paths.ADDONS_DIR / "pdf-notes" and card.files_dir == card.dir / "files"
    assert card.card_sha256 == hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert (card.on, card.approved, card.changed_since_approval, card.installed, card.enabled) == (False,) * 5
    assert not paths.ADDONS_DIR.exists()  # it reads and writes nothing


def test_text_that_isnt_a_card_is_refused():
    with pytest.raises(addons.CardError) as caught:
        addons.validate_card_text("id = ")
    assert "TOML" in str(caught.value)
    with pytest.raises(addons.CardError) as caught:
        addons.validate_card_text("# " + "x" * addons.MAX_CARD_BYTES)
    assert "64 KB" in str(caught.value)
    with pytest.raises(addons.CardError):
        addons.validate_card_text(None)


@pytest.mark.parametrize("path", ["comit", "source.comit", "permissions.network_access", "use.triger"])
def test_a_key_the_format_doesnt_have_is_refused_naming_its_dotted_path(path):
    with pytest.raises(addons.CardError) as caught:
        addons.validate_card_text(card_text(**{path.replace(".", "__"): "x"}))
    assert f"`{path}`" in str(caught.value)


@pytest.mark.parametrize("change, path", [
    ("id", "id"),
    ("summary", "summary"),
    ("source__commit", "source.commit"),
    ("source__tree_sha256", "source.tree_sha256"),
    ("source__files", "source.files"),
    ("permissions__network", "permissions.network"),
    ("permissions__commands", "permissions.commands"),
    ("use__where", "use.where"),
    ("use", "use"),
])
def test_a_missing_key_is_refused_naming_it(change, path):
    with pytest.raises(addons.CardError) as caught:
        addons.validate_card_text(card_text(**{change: DROP}))
    assert f"`{path}`" in str(caught.value)


@pytest.mark.parametrize("commit", ["a" * 7, "a" * 39, "A" * 40, "g" * 40, 1234567],
                         ids=["seven-characters", "thirty-nine", "upper-case", "not-hex", "a-number"])
def test_a_commit_that_isnt_the_full_lower_case_sha_is_refused(commit):
    with pytest.raises(addons.CardError) as caught:
        addons.validate_card_text(card_text(source__commit=commit))
    assert "`source.commit`" in str(caught.value)


BAD_VALUES = [
    ("id", "A", "id"),
    ("id", "x", "id"),
    ("id", "has space", "id"),
    ("id", "a" * 41, "id"),
    ("name", "", "name"),
    ("name", "two\nlines", "name"),
    ("summary", " padded", "summary"),
    ("license", "not a license!", "license"),
    ("kind", "plugin", "kind"),
    ("homepage", "http://example.com/notes", "homepage"),
    ("terms", "t" * 501, "terms"),
    ("source__repo", "http://github.com/someone/pdf-notes", "source.repo"),
    ("source__repo", "https://gitlab.com/someone/pdf-notes", "source.repo"),
    ("source__repo", "https://github.com/someone/pdf-notes/tree/main", "source.repo"),
    ("source__repo", "https://github.com/someone/pdf-notes.git", "source.repo"),
    ("source__subdir", "/abs", "source.subdir"),
    ("source__subdir", "../x", "source.subdir"),
    ("source__subdir", "skills\\notes", "source.subdir"),
    ("source__subdir", "a//b", "source.subdir"),
    ("source__tree_sha256", "b" * 63, "source.tree_sha256"),
    ("source__files", 0, "source.files"),
    ("source__files", -1, "source.files"),
    ("source__files", True, "source.files"),
    ("source__files", "3", "source.files"),
    ("permissions__files", "everything", "permissions.files"),
    ("permissions__network", "yes", "permissions.network"),
    ("permissions__network", 1, "permissions.network"),
    ("use__where", "everywhere", "use.where"),
    ("use__output", "pdf", "use.output"),
    ("use__trigger", "diagram", "use.trigger"),
    ("use__trigger", "/two words", "use.trigger"),
]


@pytest.mark.parametrize("change, value, path", BAD_VALUES,
                         ids=[f"{change}={value!r:.24}" for change, value, _ in BAD_VALUES])
def test_a_value_the_format_doesnt_allow_is_refused_naming_its_key(change, value, path):
    with pytest.raises(addons.CardError) as caught:
        addons.validate_card_text(card_text(**{change: value}))
    assert f"`{path}`" in str(caught.value)


def test_values_at_the_limits_and_the_optional_keys_are_fine():
    card = addons.validate_card_text(card_text(id="a" * 40, terms="t" * 500, source__subdir="",
                                               use__trigger=DROP))
    assert card.id == "a" * 40 and len(card.terms) == 500 and card.source.subdir == "" and card.use.trigger == ""
    assert addons.validate_card_text(card_text(terms=DROP)).terms == ""


def test_shell_characters_in_a_command_are_plain_characters():
    argv = ["tool", "a;b", "|", "$(rm -rf ~)", "`x`", "&&", "> out", "{input}"]
    card = addons.validate_card_text(card_text(permissions__commands=[argv]))
    assert card.permissions.commands == [argv]
    filled = addons.fill_command(card, card.permissions.commands[0], input="x; y")
    assert filled == ["tool", "a;b", "|", "$(rm -rf ~)", "`x`", "&&", "> out", "x; y"]
    assert card.permissions.commands[0][-1] == "{input}"  # the card's own command wasn't changed


@pytest.mark.parametrize("item", ["{home}", "{ADDON}", "{}", "{input", "x}", "{{addon}}"])
def test_a_placeholder_ixel_doesnt_fill_is_refused_naming_the_item(item):
    with pytest.raises(addons.CardError) as caught:
        addons.validate_card_text(card_text(permissions__commands=[["tool", "ok", item]]))
    assert "`permissions.commands[0][2]`" in str(caught.value)


@pytest.mark.parametrize("first", ["/bin/sh", "./tool", "../tool", "sub/tool", "{addon}", "{addon}/../x",
                                   "{addon}//x", "{input}", "tool{output}", "", "to\\ol"])
def test_a_program_that_isnt_a_bare_name_or_a_file_in_the_add_on_is_refused(first):
    with pytest.raises(addons.CardError) as caught:
        addons.validate_card_text(card_text(permissions__commands=[[first, "arg"]]))
    assert "`permissions.commands[0][0]`" in str(caught.value)


@pytest.mark.parametrize("first", ["python3", "{addon}/scripts/run.py"])
def test_a_bare_program_name_and_a_file_in_the_add_on_are_fine_first_items(first):
    card = addons.validate_card_text(card_text(permissions__commands=[[first, "arg"]]))
    assert card.permissions.commands == [[first, "arg"]]


def test_a_command_is_filled_in_with_the_cards_files_and_the_values_given():
    card = addons.validate_card_text(card_text(
        permissions__commands=[["python3", "{addon}/scripts/x.py", "{input}", "--out={output}"]]))
    argv = card.permissions.commands[0]
    assert addons.fill_command(card, argv, input="in.md", output="out.html") == \
        ["python3", str(card.files_dir / "scripts" / "x.py"), "in.md", "--out=out.html"]
    # {addon} is the card's files, and only the card's files can come after it
    assert addons.fill_command(card, ["{addon}/scripts/x.py"]) == [str(card.files_dir / "scripts" / "x.py")]


def test_filling_a_command_without_a_value_it_uses_is_refused():
    card = addons.validate_card_text(card_text())
    with pytest.raises(addons.CardError):
        addons.fill_command(card, ["tool", "{input}"])
    with pytest.raises(addons.CardError):
        addons.fill_command(card, ["tool", "{output}"], input="x")
    with pytest.raises(addons.CardError):
        addons.fill_command(card, ["tool", "{input}"], input="a\x00b")


def test_a_value_that_looks_like_a_placeholder_is_never_filled_again():
    card = addons.validate_card_text(card_text())
    assert addons.fill_command(card, ["tool", "{output}"], output="{addon}") == ["tool", "{addon}"]
    assert addons.fill_command(card, ["tool", "{input}", "{output}"], input="{output}", output="o") == \
        ["tool", "{output}", "o"]


def test_fill_command_checks_the_command_it_is_given():
    card = addons.validate_card_text(card_text())
    with pytest.raises(addons.CardError) as caught:
        addons.fill_command(card, ["/bin/sh", "-c", "x"])
    assert "`argv[0]`" in str(caught.value)


# ── Which cards there are ────────────────────────────────────────────────────

def test_a_broken_card_is_a_problem_and_the_other_cards_still_load(builtin):
    put_card("good-one", card_text(id="good-one"))
    broken = put_card("broken-one", card_text(id="broken-one", source__commit="abc"))
    renamed = put_card("some-folder", card_text(id="other-name"))
    (paths.ADDONS_DIR / "just-files" / "files").mkdir(parents=True)  # a folder with no card isn't a problem
    assert [card.id for card in addons.all_cards()] == [DIAGRAM, "good-one"]
    problems = {problem["path"]: problem["error"] for problem in addons.card_problems()}
    assert set(problems) == {str(broken), str(renamed)}
    assert "`source.commit`" in problems[str(broken)]
    assert "other-name" in problems[str(renamed)] and "some-folder" in problems[str(renamed)]


def test_an_agent_folder_named_for_a_builtin_card_is_ignored_with_a_problem(builtin):
    impostor = put_card(DIAGRAM, card_text(id=DIAGRAM, name="Impostor"))
    [card] = addons.all_cards()
    assert card.builtin and card.name == "Diagram Design"
    [problem] = addons.card_problems()
    assert problem["path"] == str(impostor) and "built-in" in problem["error"]


def test_builtin_cards_come_first_then_the_agents_each_by_id(builtin):
    put_card("zed-notes", card_text(id="zed-notes"))
    put_card("abc-notes", card_text(id="abc-notes"))
    assert [card.id for card in addons.all_cards()] == [DIAGRAM, "abc-notes", "zed-notes"]
    assert [card.builtin for card in addons.all_cards()] == [True, False, False]
    assert addons.builtin_ids() == {DIAGRAM}
    assert addons.card_by_id("abc-notes").id == "abc-notes" and addons.card_by_id("nope") is None


def test_writing_a_card_for_a_builtin_id_is_refused_and_writes_nothing(builtin):
    with pytest.raises(addons.CardError) as caught:
        addons.write_agent_card(card_text(id=DIAGRAM))
    assert "built-in" in str(caught.value)
    assert not (paths.ADDONS_DIR / DIAGRAM / "addon.toml").exists() and not paths.state_path().exists()


def test_the_agents_card_is_written_exactly_and_never_switched_on():
    text = card_text()
    card = addons.write_agent_card(text)
    path = paths.ADDONS_DIR / "pdf-notes" / "addon.toml"
    assert path.read_bytes() == text.encode("utf-8")
    assert card.card_path == path and card.builtin is False
    assert card.card_sha256 == hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert (card.on, card.approved, card.enabled) == (False, False, False)
    assert not paths.state_path().exists()
    again = addons.write_agent_card(card_text(summary="Notes from a PDF."))  # one more edit: another sha
    assert again.card_sha256 != card.card_sha256 and not paths.state_path().exists()


def test_a_card_that_doesnt_validate_isnt_written():
    with pytest.raises(addons.CardError):
        addons.write_agent_card(card_text(kind="plugin"))
    assert not (paths.ADDONS_DIR / "pdf-notes").exists()


# ── Approval and state ───────────────────────────────────────────────────────

def test_a_card_is_enabled_once_it_is_approved_and_installed_and_not_when_it_changes():
    card = addons.write_agent_card(card_text())
    assert not card.enabled and addons.enabled_cards() == []
    addons.install(card, fetch=lambda url: TARBALL)
    assert not addons.card_by_id("pdf-notes").enabled  # installed, but nobody approved it
    addons.approve_and_switch_on(card)
    card = addons.card_by_id("pdf-notes")
    assert card.on and card.approved and card.installed and card.enabled
    assert not card.changed_since_approval
    assert [c.id for c in addons.enabled_cards()] == ["pdf-notes"]
    assert [c.id for c in addons.enabled_cards("skill")] == ["pdf-notes"]
    assert addons.enabled_cards("mcp") == []
    with pytest.raises(ValueError):
        addons.enabled_cards("bogus")

    card.card_path.write_bytes(card.card_path.read_bytes() + b"\n")  # one byte more
    changed = addons.card_by_id("pdf-notes")
    assert changed.changed_since_approval and changed.on and not changed.approved and not changed.enabled
    assert addons.enabled_cards() == []


def test_the_state_of_a_card_comes_from_its_entry_and_its_exact_bytes():
    card = addons.validate_card_text(card_text())
    sha, tree = card.card_sha256, card.source.tree_sha256
    assert addons.card_state(card, None) == {"on": False, "approved": False, "changed_since_approval": False,
                                             "installed": False, "enabled": False}
    approved = {"on": True, "approved_card_sha256": sha, "approved_tree_sha256": tree}
    assert addons.card_state(card, approved) == {"on": True, "approved": True, "changed_since_approval": False,
                                                 "installed": False, "enabled": False}  # not installed
    assert addons.card_state(card, {**approved, "on": "yes"})["on"] is False
    other_tree = addons.card_state(card, {**approved, "approved_tree_sha256": "e" * 64})
    assert other_tree["approved"] is False and other_tree["changed_since_approval"] is True
    other_card = addons.card_state(card, {**approved, "approved_card_sha256": "f" * 64})
    assert other_card["approved"] is False and other_card["changed_since_approval"] is True
    assert addons.card_state(card, {"on": True, "approved_card_sha256": ""})["changed_since_approval"] is False


def test_switching_off_keeps_what_was_approved_and_a_card_with_no_entry_writes_nothing(builtin):
    addons.switch_off(DIAGRAM)
    assert not paths.state_path().exists()
    entry = addons.approve_and_switch_on(builtin, now=datetime(2026, 10, 12, 3, 40, 0))
    assert entry == {"on": True, "approved_card_sha256": builtin.card_sha256, "approved_tree_sha256": TREE_SHA,
                     "approved_at": "2026-10-12T03:40:00Z"}
    addons.switch_off(DIAGRAM)
    assert saved_state()[DIAGRAM] == {**entry, "on": False}


# ── Unpacking ────────────────────────────────────────────────────────────────

def test_only_the_skills_folder_is_unpacked_and_its_files_counted(tmp_path):
    dest = tmp_path / "addons" / "diagram-design"
    assert addons.extract(TARBALL, SUB, dest) == len(SKILL_FILES)
    found = sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file())
    assert found == sorted(SKILL_FILES)  # no README, LICENSE, docs, other skill or look-alike folder
    assert (dest / "SKILL.md").read_text(encoding="utf-8") == SKILL_FILES["SKILL.md"]
    assert sorted(p.name for p in dest.parent.iterdir()) == ["diagram-design"]  # no .part or .old beside it


def test_an_empty_subdir_unpacks_the_whole_top_folder(tmp_path):
    dest = tmp_path / "addons" / "whole"
    expected = {f"{SUB}/{rel}" for rel in SKILL_FILES} | set(OUTSIDE)
    assert addons.extract(TARBALL, "", dest) == len(expected)
    found = {p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file()}
    assert found == expected
    assert (dest / "README.md").is_file() and (dest / SUB / "SKILL.md").is_file()


@pytest.mark.skipif(os.name != "posix", reason="file modes are POSIX's")
def test_the_archives_modes_are_ignored(tmp_path):
    dest = tmp_path / "addons" / "diagram-design"
    addons.extract(TARBALL, SUB, dest)
    assert stat.S_IMODE((dest / "SKILL.md").stat().st_mode) == 0o644
    assert stat.S_IMODE((dest / "references").stat().st_mode) == 0o755
    assert stat.S_IMODE(dest.stat().st_mode) == 0o755


def test_what_was_there_is_replaced_whole(tmp_path):
    dest = tmp_path / "addons" / "diagram-design"
    dest.mkdir(parents=True)
    (dest / "stale.txt").write_text("from an older release", encoding="utf-8")
    addons.extract(TARBALL, SUB, dest)
    assert not (dest / "stale.txt").exists() and (dest / "SKILL.md").is_file()
    assert sorted(p.name for p in dest.parent.iterdir()) == ["diagram-design"]


UNSAFE = [
    pytest.param({"name": "../evil.txt", "data": b"evil"}, id="dot-dot-at-the-top"),
    pytest.param({"name": f"{TOP}/{SUB}/../../../evil.txt", "data": b"evil"}, id="dot-dot-inside-the-skill"),
    pytest.param({"name": "/etc/evil.txt", "data": b"evil"}, id="absolute"),
    pytest.param({"name": f"{TOP}/{SUB}\\evil.txt", "data": b"evil"}, id="backslash"),
    pytest.param({"name": f"{TOP}/{SUB}/link", "kind": tarfile.SYMTYPE, "link": "/etc/passwd"}, id="symlink"),
    pytest.param({"name": f"{TOP}/docs/link", "kind": tarfile.SYMTYPE, "link": "../../.ssh"},
                 id="symlink-outside-the-skill"),
    pytest.param({"name": f"{TOP}/{SUB}/hard", "kind": tarfile.LNKTYPE, "link": f"{TOP}/{SUB}/SKILL.md"},
                 id="hardlink"),
    pytest.param({"name": f"{TOP}/{SUB}/dev", "kind": tarfile.CHRTYPE}, id="device"),
    pytest.param({"name": f"{TOP}/{SUB}/pipe", "kind": tarfile.FIFOTYPE}, id="fifo"),
]


@pytest.mark.parametrize("member", UNSAFE)
def test_one_unsafe_member_anywhere_refuses_the_whole_archive_and_writes_nothing(tmp_path, member):
    dest = tmp_path / "out" / "diagram-design"
    with pytest.raises(addons.AddonError) as caught:
        addons.extract(tarball(extra=[member]), SUB, dest)
    assert caught.value.code == "unsafe"
    assert not (tmp_path / "out").exists()  # not even the folder it would have gone in
    assert not list(tmp_path.rglob("evil.txt"))


def test_a_file_over_the_size_limit_refuses_the_archive(tmp_path):
    dest = tmp_path / "out" / "diagram-design"
    with pytest.raises(addons.AddonError) as caught:
        addons.extract(TARBALL, SUB, dest, max_file_bytes=8)
    assert caught.value.code == "unsafe" and not (tmp_path / "out").exists()


@pytest.mark.parametrize("subdir", ["..", "skills/../..", ".", "/"])
def test_a_folder_that_isnt_inside_the_archive_is_refused(tmp_path, subdir):
    with pytest.raises(addons.AddonError) as caught:
        addons.extract(TARBALL, subdir, tmp_path / "out" / "diagram-design")
    assert caught.value.code == "unsafe" and not (tmp_path / "out").exists()


def test_an_archive_without_the_skills_files_is_refused(tmp_path):
    dest = tmp_path / "out" / "diagram-design"
    for archive, subdir in ((tarball(files={}), SUB), (TARBALL, "skills/not-here")):  # empty, and not there at all
        with pytest.raises(addons.AddonError) as caught:
            addons.extract(archive, subdir, dest)
        assert f"the archive has no {subdir}" in str(caught.value)
        assert not (tmp_path / "out").exists()


def test_something_that_isnt_a_tar_gz_is_refused(tmp_path):
    with pytest.raises(addons.AddonError) as caught:
        addons.extract(b"<html>a login page, not an archive</html>", SUB, tmp_path / "out" / "diagram-design")
    assert caught.value.code == "bad_archive" and not (tmp_path / "out").exists()


# ── The pin ──────────────────────────────────────────────────────────────────

def test_the_tree_hash_is_the_documented_one(tmp_path):
    dest = tmp_path / "addons" / "diagram-design"
    addons.extract(TARBALL, SUB, dest)
    assert addons.tree_hash(dest) == expected_tree(SKILL_FILES)
    assert addons.extract(TARBALL, SUB, dest, expect=expected_tree(SKILL_FILES)) == len(SKILL_FILES)


def test_the_tree_hash_counts_only_regular_files_and_follows_no_links(tmp_path):
    dest = tmp_path / "addons" / "diagram-design"
    addons.extract(TARBALL, SUB, dest)
    outside = tmp_path / "outside.txt"
    outside.write_text("not part of the skill", encoding="utf-8")
    try:
        (dest / "link.txt").symlink_to(outside)
        (dest / "linked-folder").symlink_to(tmp_path, target_is_directory=True)
    except OSError:  # Windows lets only administrators, or Developer Mode, make links
        pytest.skip("this account can't make symbolic links")
    assert addons.tree_hash(dest) == expected_tree(SKILL_FILES)


@pytest.mark.parametrize("changed", [
    pytest.param({**SKILL_FILES, "SKILL.md": "# Not what was pinned\n"}, id="a-file-changed"),
    pytest.param({**SKILL_FILES, "references/extra.md": "one more\n"}, id="a-file-added"),
    pytest.param({k: v for k, v in SKILL_FILES.items() if k != "scripts/self_check.py"}, id="a-file-missing"),
])
def test_files_that_dont_match_the_pin_leave_what_was_there_alone(tmp_path, changed):
    dest = tmp_path / "addons" / "diagram-design"
    addons.extract(TARBALL, SUB, dest)  # an earlier install
    with pytest.raises(addons.AddonError) as caught:
        addons.extract(tarball(changed), SUB, dest, expect=expected_tree(SKILL_FILES))
    assert caught.value.code == "tree_mismatch" and caught.value.status == 409
    assert (dest / "SKILL.md").read_text(encoding="utf-8") == SKILL_FILES["SKILL.md"]
    assert addons.tree_hash(dest) == expected_tree(SKILL_FILES)
    assert sorted(p.name for p in dest.parent.iterdir()) == ["diagram-design"]  # the .part folder is gone


# ── Downloading ──────────────────────────────────────────────────────────────

class Reply:
    """What an opener gives back: read(n) in pieces, geturl(), close()."""

    def __init__(self, data: bytes, url: str = URL):
        self._data, self._url, self.closed = io.BytesIO(data), url, False

    def read(self, n=-1):
        return self._data.read(n)

    def geturl(self):
        return self._url

    def close(self):
        self.closed = True


def test_a_download_is_the_bytes_the_opener_gave():
    asked, replies = [], []

    def opener(url, timeout):
        asked.append((url, timeout))
        replies.append(Reply(TARBALL))
        return replies[-1]

    assert addons.download(URL, opener=opener) == TARBALL
    assert asked == [(URL, addons.DOWNLOAD_TIMEOUT)] and replies[0].closed


def test_a_download_over_the_limit_is_refused():
    replies = []

    def opener(url, timeout):
        replies.append(Reply(b"x" * 100_000))
        return replies[-1]

    with pytest.raises(addons.AddonError) as caught:
        addons.download(URL, opener=opener, max_bytes=1000)
    assert caught.value.code == "too_big" and replies[0].closed
    assert addons.download(URL, opener=lambda url, timeout: Reply(b"x" * 1000), max_bytes=1000) == b"x" * 1000


def test_a_download_is_https_only_even_after_a_redirect():
    def never(url, timeout):
        raise AssertionError("nothing should be opened")

    with pytest.raises(addons.AddonError) as caught:
        addons.download("http://codeload.github.com/x/y/tar.gz/abc", opener=never)
    assert caught.value.code == "not_https"

    replies = []

    def sent_away(url, timeout):
        replies.append(Reply(b"data", url="http://evil.example/archive.tar.gz"))
        return replies[-1]

    with pytest.raises(addons.AddonError) as caught:
        addons.download(URL, opener=sent_away)
    assert caught.value.code == "not_https" and replies[0].closed


def test_a_download_that_fails_says_so():
    def broken(url, timeout):
        raise OSError("connection refused")

    with pytest.raises(addons.AddonError) as caught:
        addons.download(URL, opener=broken)
    assert caught.value.code == "download" and "connection refused" in str(caught.value)


# ── Installing ───────────────────────────────────────────────────────────────

def test_the_release_is_downloaded_from_the_cards_commit_and_unpacked_into_its_files_folder(builtin, release):
    where = addons.install(builtin)  # no fetch: it asks installer.download, which the fixture stands in for
    assert release == [URL] and addons.tarball_url(builtin) == URL
    assert where == builtin.files_dir == paths.ADDONS_DIR / "diagram-design" / "files"
    found = sorted(p.relative_to(where).as_posix() for p in where.rglob("*") if p.is_file())
    assert found == sorted(SKILL_FILES)
    assert addons.installed(builtin) and addons.tree_hash(where) == expected_tree(SKILL_FILES)


def test_a_release_that_isnt_the_pinned_one_gives_409_and_leaves_nothing_behind(builtin):
    tampered = tarball({**SKILL_FILES, "scripts/self_check.py": "import os\nos.system('echo hi')\n"})
    with pytest.raises(addons.AddonError) as caught:
        addons.install(builtin, fetch=lambda url: tampered)
    assert caught.value.status == 409 and caught.value.code == "tree_mismatch"
    assert not addons.installed(builtin) and not builtin.dir.exists()
    assert list(paths.ADDONS_DIR.iterdir()) == []


def test_a_release_that_isnt_the_pinned_one_leaves_the_files_that_were_there(builtin):
    addons.install(builtin, fetch=lambda url: TARBALL)
    tampered = tarball({**SKILL_FILES, "SKILL.md": "# Something else\n"})
    with pytest.raises(addons.AddonError) as caught:
        addons.install(builtin, fetch=lambda url: tampered)
    assert caught.value.code == "tree_mismatch"
    assert addons.tree_hash(builtin.files_dir) == expected_tree(SKILL_FILES)
    assert sorted(p.name for p in builtin.dir.iterdir()) == ["files"]


def test_ensure_installed_reuses_matching_files_and_installs_again_when_one_changed(builtin):
    asked = []

    def fetch(url):
        asked.append(url)
        return TARBALL

    def never(url):
        raise AssertionError("matching files must not be downloaded again")

    assert addons.ensure_installed(builtin, fetch=fetch) is True and len(asked) == 1
    assert addons.ensure_installed(builtin, fetch=never) is False
    (builtin.files_dir / "SKILL.md").write_text("# edited by hand\n", encoding="utf-8")
    assert addons.ensure_installed(builtin, fetch=fetch) is True and len(asked) == 2
    assert (builtin.files_dir / "SKILL.md").read_text(encoding="utf-8") == SKILL_FILES["SKILL.md"]


def test_removing_an_agent_card_takes_its_files_and_keeps_its_card():
    card = addons.write_agent_card(card_text())
    assert addons.remove(card) is False  # nothing there yet
    addons.install(card, fetch=lambda url: TARBALL)
    assert addons.remove(card) is True
    assert not card.files_dir.exists() and not addons.installed(card)
    assert card.card_path.is_file() and card.card_path.read_bytes() == card_text().encode("utf-8")
    assert addons.remove(card) is False


def test_removing_a_builtin_card_takes_its_files_and_then_its_empty_folder(builtin):
    assert addons.remove(builtin) is False
    addons.install(builtin, fetch=lambda url: TARBALL)
    assert addons.remove(builtin) is True
    assert not builtin.dir.exists() and not addons.installed(builtin)
    assert addons.remove(builtin) is False


def test_an_install_from_before_cards_isnt_installed_and_install_and_remove_both_clear_it(builtin):
    write_old_layout(builtin)
    assert not addons.installed(builtin)
    addons.install(builtin, fetch=lambda url: TARBALL)
    assert addons.installed(builtin) and not (builtin.dir / "SKILL.md").exists()
    assert sorted(p.name for p in builtin.dir.iterdir()) == ["files"]

    addons.remove(builtin)
    write_old_layout(builtin)
    half = paths.ADDONS_DIR / "diagram-design.part"  # what the old installer left when it was stopped
    half.mkdir()
    (half / "half.txt").write_text("x", encoding="utf-8")
    assert addons.remove(builtin) is True
    assert not builtin.dir.exists() and not half.exists()


def test_an_approved_card_installed_the_old_way_is_not_enabled(builtin):
    write_old_layout(builtin)
    addons.approve_and_switch_on(builtin)
    card = addons.card_by_id(DIAGRAM)
    assert card.on and card.approved and not card.installed and not card.enabled
    assert addons.enabled_cards() == []


def test_a_linked_builtin_folder_is_unlinked_not_followed(tmp_path, builtin):
    elsewhere = tmp_path / "my-own-copy"
    elsewhere.mkdir()
    (elsewhere / "SKILL.md").write_text("# mine\n", encoding="utf-8")
    paths.ADDONS_DIR.mkdir(parents=True)
    try:
        builtin.dir.symlink_to(elsewhere, target_is_directory=True)
    except OSError:  # Windows lets only administrators, or Developer Mode, make links
        pytest.skip("this account can't make symbolic links")
    assert addons.remove(builtin) is True
    assert not builtin.dir.exists() and (elsewhere / "SKILL.md").is_file()


def test_nothing_is_installed_into_or_removed_through_a_linked_card_folder(tmp_path):
    card = addons.validate_card_text(card_text())  # placed at ADDONS_DIR/pdf-notes
    elsewhere = tmp_path / "my-own-copy"
    elsewhere.mkdir()
    (elsewhere / "keep.txt").write_text("mine", encoding="utf-8")
    paths.ADDONS_DIR.mkdir(parents=True)
    try:
        card.dir.symlink_to(elsewhere, target_is_directory=True)
    except OSError:  # Windows lets only administrators, or Developer Mode, make links
        pytest.skip("this account can't make symbolic links")

    def never(url):
        raise AssertionError("nothing may be downloaded into a link")

    with pytest.raises(addons.AddonError) as caught:
        addons.install(card, fetch=never)
    assert (caught.value.status, caught.value.code) == (409, "unsafe")
    with pytest.raises(addons.AddonError) as caught:
        addons.remove(card)
    assert (caught.value.status, caught.value.code) == (409, "unsafe")
    assert sorted(p.name for p in elsewhere.iterdir()) == ["keep.txt"] and not addons.installed(card)


# ── Choosing a diagram type ──────────────────────────────────────────────────

@pytest.mark.parametrize("question, kind", [
    ("Show the TLS handshake as a sequence of messages between client and server", "sequence"),
    ("A roadmap with milestones over time for next year", "timeline"),
    ("Compare postgres vs mysql: pros and cons", "comparison"),
    ("Our org chart as a hierarchy", "hierarchy"),
    ("Brainstorm ideas as a mind map", "mindmap"),
    ("The sales funnel from a visit to conversion", "funnel"),
    ("The lifecycle of a feature branch", "cycle"),
    ("A 2x2 matrix of effort versus impact", "quadrant"),
    ("Draw the steps of our release process", "flowchart"),
    ("The services and infrastructure behind the app", "architecture"),
    ("Draw a treehouse with flowers", "architecture"),  # tree and flow only count as whole words: nothing hits
    ("Draw something nice", "architecture"),
    ("flow of messages", "flowchart"),  # one hit each: the earlier type in DIAGRAM_TYPES wins
])
def test_the_type_is_the_one_the_question_sounds_like(question, kind):
    assert addons.pick_type(question) == kind


def test_the_type_is_picked_from_the_ones_the_skill_has():
    assert addons.pick_type("a flowchart of the steps", ["flowchart", "timeline"]) == "flowchart"
    assert addons.pick_type("a flowchart of the steps", ["architecture", "sequence"]) == "architecture"
    assert addons.pick_type("a flowchart of the steps", ["timeline", "sequence"]) == "timeline"  # no architecture
    assert addons.pick_type("a flowchart of the steps", []) == "flowchart"  # nothing said: all of them


def test_the_available_types_are_the_references_the_skill_has(tmp_path):
    refs = tmp_path / "refs-skill" / "references"
    refs.mkdir(parents=True)
    for name in ("type-zebra.md", "type-flowchart.md", "type-swimlane.md", "type-architecture.md", "style-guide.md",
                 "notes.md"):
        (refs / name).write_text("x", encoding="utf-8")
    assert addons.available_types(tmp_path / "refs-skill") == ["architecture", "flowchart", "swimlane", "zebra"]
    assert addons.available_types(tmp_path / "nowhere") == []


# ── What the model is given ──────────────────────────────────────────────────

def test_the_material_is_the_skill_its_style_guide_and_the_types_reference_and_example(skill):
    material = addons.diagram_material(skill, "flowchart")
    assert material.title == "the Diagram Design skill (type: flowchart)"
    for rel in ("SKILL.md", "references/style-guide.md", "references/type-flowchart.md",
                "assets/example-flowchart.html"):
        text = SKILL_FILES[rel]
        assert f"=== {rel} ===\n{text}" in material.text, rel
    assert "type-architecture" not in material.text


def test_a_type_without_an_example_goes_without_one(skill):
    material = addons.diagram_material(skill, "architecture")
    assert "=== references/type-architecture.md ===" in material.text
    assert "example-" not in material.text


def test_a_skill_that_isnt_installed_says_the_add_on_is_off(tmp_path):
    with pytest.raises(addons.AddonError) as caught:
        addons.diagram_material(tmp_path / "nothing-here", "flowchart")
    assert caught.value.code == "addon_off" and caught.value.status == 409


def test_a_file_that_isnt_utf8_is_read_with_replacements(skill):
    (skill / "references" / "style-guide.md").write_bytes(b"# Style\n\xff\xfe\n")
    assert "�" in addons.diagram_material(skill, "flowchart").text


def test_too_much_material_loses_the_example_then_the_type_and_never_the_skill(skill):
    (skill / "assets" / "example-flowchart.html").write_text("e" * 100_000, encoding="utf-8")
    text = addons.diagram_material(skill, "flowchart").text
    assert len(text) <= MAX_MATERIAL_CHARS
    assert SKILL_FILES["references/type-flowchart.md"] in text and SKILL_FILES["SKILL.md"] in text
    (skill / "references" / "type-flowchart.md").write_text("t" * 100_000, encoding="utf-8")
    text = addons.diagram_material(skill, "flowchart").text
    assert len(text) <= MAX_MATERIAL_CHARS and "[... cut to fit]" in text
    assert f"=== SKILL.md ===\n{SKILL_FILES['SKILL.md']}" in text
    assert SKILL_FILES["references/style-guide.md"] in text


def test_the_question_names_the_type_and_the_problems_to_fix():
    first = addons.diagram_question("  Draw our release steps  ", "flowchart")
    assert "flowchart" in first and "Draw our release steps" in first and "```html" in first
    assert "failed these checks" not in first
    again = addons.diagram_question("Draw our release steps", "flowchart", ["missing doctype", "no title"])
    assert "The last attempt failed these checks; fix them:" in again
    assert "missing doctype" in again and "no title" in again


# ── Finding the HTML in a reply ──────────────────────────────────────────────

def test_the_html_is_the_first_fenced_block_a_bare_document_or_nothing():
    doc = "<!doctype html>\n<html><body>hi</body></html>"
    assert addons.extract_html(f"Here you go:\n```html\n{doc}\n```\nEnjoy.") == doc
    assert addons.extract_html("```HTML\n<p>x</p>\n```") == "<p>x</p>"
    assert addons.extract_html("```html\n<p>one</p>\n```\nand\n```html\n<p>two</p>\n```") == "<p>one</p>"
    assert addons.extract_html(f"  \n{doc}\n") == doc
    assert addons.extract_html("<HTML lang='en'><body></body></HTML>") == "<HTML lang='en'><body></body></HTML>"
    assert addons.extract_html("I can't draw that.") is None
    assert addons.extract_html("```python\nprint(1)\n```") is None
    assert addons.extract_html("Sure! <html></html>") is None
    assert addons.extract_html("") is None


# ── Saving ───────────────────────────────────────────────────────────────────

def test_a_name_is_the_question_in_lower_case_words():
    assert addons.slug("Draw our CI/CD pipeline!") == "draw-our-ci-cd-pipeline"
    assert addons.slug("a" * 39 + " bbbb") == "a" * 39  # cut at 40, no dash left on the end
    assert addons.slug("one two three", limit=7) == "one-two"
    assert addons.slug("Diagramme café") == "diagramme-caf"
    assert addons.slug("???") == "diagram" and addons.slug("") == "diagram"


def test_a_diagram_is_saved_under_its_date_and_name_and_never_over_another():
    now = datetime(2026, 10, 11, 9, 30)
    html = "<!doctype html><p>café → crème</p>"
    first = addons.save_diagram(html, "Our CI pipeline", now=now)
    assert first == paths.DIAGRAMS_DIR / "2026-10-11-our-ci-pipeline.html"
    assert first.read_bytes() == html.encode("utf-8")
    second = addons.save_diagram("<p>second</p>", "Our CI pipeline", now=now)
    third = addons.save_diagram("<p>third</p>", "Our CI pipeline", now=now)
    assert [p.name for p in (second, third)] == ["2026-10-11-our-ci-pipeline-2.html",
                                                 "2026-10-11-our-ci-pipeline-3.html"]
    assert first.read_bytes() == html.encode("utf-8")  # still the first one's


def test_a_diagram_can_be_saved_in_a_folder_of_the_callers(tmp_path):
    path = addons.save_diagram(GOOD, "x", folder=tmp_path / "mine", now=datetime(2026, 1, 2))
    assert path == tmp_path / "mine" / "2026-01-02-x.html" and path.read_text(encoding="utf-8") == GOOD
    assert not paths.DIAGRAMS_DIR.exists()


def test_the_store_gives_unguessable_ids_for_the_diagrams_it_was_given(tmp_path):
    store = addons.DiagramStore()
    one, two = tmp_path / "one.html", tmp_path / "two.html"
    first, second = store.add(one), store.add(two)
    assert re.fullmatch(r"[0-9a-f]{32}", first) and first != second
    assert store.get(first) == one and store.get(second) == two and store.get("0" * 32) is None
    store.clear()
    assert store.get(first) is None


# ── Checking ─────────────────────────────────────────────────────────────────

def write_check(folder: Path, source: str) -> None:
    script = folder / "scripts" / "self_check.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(source, encoding="utf-8")


def test_a_diagram_that_passes_the_skills_own_check(skill, tmp_path):
    page = tmp_path / "made.html"
    page.write_text(GOOD, encoding="utf-8")
    result = addons.self_check(skill, page)
    assert result.ok and result.ran and result.messages == []
    assert result.to_dict() == {"ok": True, "messages": [], "ran": True}


def test_a_diagram_that_fails_it_says_why(skill, tmp_path):
    page = tmp_path / "made.html"
    page.write_text(BAD, encoding="utf-8")
    result = addons.self_check(skill, page)
    assert not result.ok and result.ran and result.messages == ["missing doctype"]


def test_a_skill_without_a_check_leaves_the_diagram_unchecked(skill, tmp_path):
    (skill / "scripts" / "self_check.py").unlink()
    page = tmp_path / "made.html"
    page.write_text(BAD, encoding="utf-8")
    result = addons.self_check(skill, page)
    assert result.ok and not result.ran and "no self_check.py" in result.messages[0]


def test_a_check_that_hangs_is_stopped_and_counts_as_failed(skill, tmp_path):
    write_check(skill, "import time\ntime.sleep(60)\n")
    page = tmp_path / "made.html"
    page.write_text(GOOD, encoding="utf-8")
    result = addons.self_check(skill, page, timeout=0.5)
    assert not result.ok and result.ran and "more than" in result.messages[0]


def test_a_check_that_says_a_great_deal_is_cut_down(skill, tmp_path):
    write_check(skill, "import sys\nfor i in range(60):\n    print('x' * 600)\nsys.exit(1)\n")
    page = tmp_path / "made.html"
    page.write_text(GOOD, encoding="utf-8")
    result = addons.self_check(skill, page)
    assert not result.ok and len(result.messages) == 40 and all(len(m) == 500 for m in result.messages)


# ── Making a diagram ─────────────────────────────────────────────────────────

def fenced(html: str) -> str:
    return f"Here it is:\n```html\n{html}\n```\n"


class FakeAsk:
    """Stands in for ixel_mat.ask.ask: gives each answer in turn, and keeps what it was asked and with what timeout."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []
        self.timeouts = []

    async def __call__(self, cfg, question, material, timeout=None):
        self.calls.append((question, material))
        self.timeouts.append(timeout)
        return AskResult(agent=cfg.name, label=cfg.label, model="m-test", answer=self.answers.pop(0), ms=12,
                         usage={"input_tokens": 5})


class FailingAsk:
    """An ask() that can't get an answer."""

    def __init__(self, message: str):
        self.message = message

    async def __call__(self, cfg, question, material, timeout=None):
        raise AskError(self.message)


def make(skill, question, *answers, on_event=None, out=None, cfg=CFG, ask=None, timeout=None):
    ask = ask or FakeAsk(*answers)
    result = asyncio.run(addons.make_diagram(cfg, question, skill, ask=ask, on_event=on_event, diagrams_dir=out,
                                             timeout=timeout))
    return result, ask


def test_a_diagram_that_passes_the_first_time_is_asked_for_once(skill, tmp_path):
    events = []
    out = tmp_path / "made"
    result, ask = make(skill, "Draw the steps of the release process", fenced(GOOD),
                       on_event=lambda kind, data: events.append((kind, data)), out=out)
    assert len(ask.calls) == 1 and len(result.calls) == 1
    assert result.retried is False and result.kind == "flowchart"
    assert result.check.ok and result.check.ran
    assert result.path.parent == out and result.path.read_text(encoding="utf-8") == GOOD.strip()
    question, material = ask.calls[0]
    assert "flowchart" in question and material.title == "the Diagram Design skill (type: flowchart)"
    assert events == [
        ("start", {"kind": "flowchart", "agent": "gpt", "label": "GPT"}),
        ("model", {"attempt": 1, "ms": 12, "usage": {"input_tokens": 5}, "model": "m-test"}),
        ("check", {"attempt": 1, "ok": True, "messages": [], "ran": True}),
    ]
    data = result.to_dict()
    assert data["file"] == str(result.path) and data["name"] == result.path.name and data["kind"] == "flowchart"
    assert data["retried"] is False and data["check"] == {"ok": True, "messages": [], "ran": True}
    assert len(data["calls"]) == 1 and "answer" not in data["calls"][0] and data["calls"][0]["model"] == "m-test"


def test_a_failed_check_earns_one_retry_with_the_problems_and_the_first_attempt(skill, tmp_path):
    events = []

    async def on_event(kind, data):  # a handler may be async too
        events.append((kind, data))

    out = tmp_path / "made"
    result, ask = make(skill, "Draw the steps of the release process", fenced(BAD), fenced(GOOD),
                       on_event=on_event, out=out)
    assert len(ask.calls) == 2 and len(result.calls) == 2
    assert result.retried is True and result.check.ok and result.check.ran
    assert result.path.read_text(encoding="utf-8") == GOOD.strip()  # the retry replaced the first file
    assert [p.name for p in out.iterdir()] == [result.path.name]
    question, material = ask.calls[1]
    assert "The last attempt failed these checks; fix them:" in question and "missing doctype" in question
    assert "=== your previous attempt ===" in material.text and BAD.strip() in material.text
    assert len(material.text) <= MAX_MATERIAL_CHARS
    assert [kind for kind, _ in events] == ["start", "model", "check", "retry", "model", "check"]
    assert events[2][1] == {"attempt": 1, "ok": False, "messages": ["missing doctype"], "ran": True}
    assert events[4][1]["attempt"] == 2
    assert events[5][1] == {"attempt": 2, "ok": True, "messages": [], "ran": True}


def test_a_second_failure_is_reported_not_retried_again(skill):
    result, ask = make(skill, "Draw the steps", fenced(BAD), fenced(BAD))
    assert len(ask.calls) == 2 and result.retried is True
    assert not result.check.ok and result.check.messages == ["missing doctype"]


def test_a_retry_that_gives_no_html_keeps_the_first_file_and_its_failed_check(skill):
    result, ask = make(skill, "Draw the steps", fenced(BAD), "Sorry, I can't draw that.")
    assert len(ask.calls) == 2 and result.retried is True
    assert result.path.read_text(encoding="utf-8") == BAD.strip()
    assert not result.check.ok and result.check.messages[0] == "missing doctype"
    assert result.check.messages[-1] == "The retry didn't answer with an HTML document."


def test_a_reply_with_no_html_is_an_error_and_saves_nothing(skill, tmp_path):
    out = tmp_path / "made"
    events = []
    with pytest.raises(addons.AddonError) as caught:
        make(skill, "Draw the steps", "I'd rather talk about it.",
             on_event=lambda kind, data: events.append(kind), out=out)
    assert caught.value.code == "no_html" and "GPT" in str(caught.value)
    assert "16k" not in str(caught.value)  # a model in the cloud has no context window to blame
    assert not out.exists() and events == ["start", "model"]


def test_a_skill_with_no_check_is_never_retried(skill):
    (skill / "scripts" / "self_check.py").unlink()
    result, ask = make(skill, "Draw the steps", fenced(BAD))
    assert len(ask.calls) == 1 and result.retried is False and not result.check.ran


# ── Local models: the time they get and why they fail ────────────────────────

def test_a_model_on_your_own_computer_gets_at_least_ten_minutes_and_a_cloud_one_its_own_time():
    assert addons.LOCAL_DIAGRAM_TIMEOUT == 600.0 and addons.CONTEXT_TOKENS == 16384
    assert addons.is_local(LOCAL) and not addons.is_local(CFG)
    assert addons.diagram_timeout(LOCAL) == 600.0
    longer = AgentConfig(name="llama", label="Llama", type="http", url=LOCAL.url, model="llama3", timeout=900)
    assert addons.diagram_timeout(longer) == 900
    assert addons.diagram_timeout(CFG) is None


def test_make_diagram_asks_a_local_model_with_the_longer_timeout(skill):
    _, ask = make(skill, "Draw the steps", fenced(GOOD), cfg=LOCAL)
    assert ask.timeouts == [600.0]
    _, ask = make(skill, "Draw the steps", fenced(GOOD), cfg=CFG)
    assert ask.timeouts == [None]
    _, ask = make(skill, "Draw the steps", fenced(GOOD), cfg=LOCAL, timeout=30)
    assert ask.timeouts == [30]  # a caller's own timeout wins


def test_a_local_model_that_fails_is_blamed_on_its_context_window(skill):
    with pytest.raises(addons.AddonError) as caught:
        make(skill, "Draw the steps", cfg=LOCAL, ask=FailingAsk("Llama stopped answering."))
    assert caught.value.code == "local_model" and caught.value.status == 502
    message = str(caught.value)
    assert "Llama stopped answering." in message and "context" in message and "16k" in message


def test_a_cloud_model_that_fails_is_not_blamed_on_a_context_window(skill):
    with pytest.raises(AskError) as caught:
        make(skill, "Draw the steps", cfg=CFG, ask=FailingAsk("GPT has no API key yet."))
    assert not isinstance(caught.value, addons.AddonError) and "GPT has no API key yet." in str(caught.value)


def test_a_local_model_that_answers_without_html_gets_the_context_note(skill):
    question = "Draw the steps"
    with pytest.raises(addons.AddonError) as caught:
        make(skill, question, "I'd rather talk about it.", cfg=LOCAL)
    assert caught.value.code == "no_html"
    kind = addons.pick_type(question, addons.available_types(skill))
    chars = len(addons.diagram_material(skill, kind).text)
    assert addons.context_note(LOCAL, chars) in str(caught.value)


def test_the_context_note_says_how_big_the_material_is_and_what_to_raise():
    note = addons.context_note(LOCAL, 40_000)
    assert "Llama" in note and "about 10k tokens" in note and "16k" in note and "16384" in note
    assert "about 1k tokens" in addons.context_note(LOCAL, 10)  # never less than 1k


# ── Which model draws ────────────────────────────────────────────────────────

def anthropic(name="opus", model="claude-opus-5-5"):
    return AgentConfig(name=name, label=name.title(), type="http", url="https://api.anthropic.com/v1/messages",
                       model=model, token="sk-never-shown")


def gateway(name, model):
    """An API that isn't Anthropic's, so there's no Haiku to switch to."""
    return AgentConfig(name=name, label=name.title(), type="http", url="https://api.example.com/v1", model=model,
                       token="sk-never-shown")


PRICES = {"claude-opus-5-5": Price(4.0, 20.0), "claude-sonnet-4-6": Price(3.0, 15.0),
          "claude-haiku-4-5": Price(1.0, 5.0)}


def test_an_anthropic_api_model_is_offered_as_itself_and_then_as_haiku():
    own, haiku = addons.diagram_choices([anthropic()], PRICES)
    assert (own.id, own.agent, own.model, own.label, own.haiku, own.local) == \
        ("opus", "opus", "", "Opus", False, False)
    assert own.detail == "claude-opus-5-5" and own.price == (4.0, 20.0)
    assert (haiku.id, haiku.agent, haiku.model, haiku.haiku) == ("opus+haiku", "opus", "latest-fast", True)
    assert haiku.label == "Haiku, via Opus" and haiku.detail == addons.HAIKU_MODEL
    assert haiku.price == (1.0, 5.0)
    assert addons.haiku_model_for(anthropic()) == "latest-fast"


def test_claude_code_is_offered_as_itself_and_then_as_haiku():
    own, haiku = addons.diagram_choices([CLAUDE_CODE])
    assert (own.id, own.model, own.haiku, own.local) == ("claude", "", False, False)
    assert (haiku.id, haiku.agent, haiku.model, haiku.haiku) == ("claude+haiku", "claude", "haiku", True)
    assert addons.haiku_model_for(CLAUDE_CODE) == "haiku"


def test_a_model_that_cant_run_haiku_is_offered_only_as_itself():
    codex = AgentConfig(name="codex", label="Codex", type="oneshot", command="codex",
                        model_args=["--model", "{model}"])
    choices = addons.diagram_choices([LOCAL, codex])
    assert [(c.id, c.model, c.haiku) for c in choices] == [("llama", "", False), ("codex", "", False)]
    assert [c.local for c in choices] == [True, False]
    assert addons.haiku_model_for(LOCAL) is None and addons.haiku_model_for(codex) is None


def test_a_model_that_already_is_haiku_is_one_choice_marked_haiku():
    [only] = addons.diagram_choices([anthropic("fast", "claude-haiku-4-5")])
    assert (only.id, only.agent, only.model, only.haiku) == ("fast", "fast", "", True)
    assert addons.haiku_model_for(anthropic("fast", "claude-haiku-4-5")) == ""


def test_the_default_is_the_first_haiku():
    choices = addons.diagram_choices([LOCAL, anthropic(), CLAUDE_CODE], PRICES)
    assert [c.id for c in choices] == ["llama", "opus", "opus+haiku", "claude", "claude+haiku"]
    assert addons.default_choice(choices).id == "opus+haiku"


def test_without_a_haiku_the_cheapest_priced_model_is_the_default():
    choices = addons.diagram_choices([gateway("opus", "claude-opus-5-5"), gateway("sonnet", "claude-sonnet-4-6")],
                                     PRICES)
    assert addons.default_choice(choices).id == "sonnet"
    choices = addons.diagram_choices([gateway("opus", "claude-opus-5-5"), gateway("sonnet", "claude-sonnet-4-6")])
    assert addons.default_choice(choices).id == "sonnet"  # and by Ixel's own price list, with none of yours
    # a model with no price loses to one that has a price, wherever it is on the panel
    choices = addons.diagram_choices([LOCAL, gateway("opus", "claude-opus-5-5")], PRICES)
    assert addons.default_choice(choices).id == "opus"


def test_with_no_price_known_the_first_model_is_the_default():
    choices = addons.diagram_choices([LOCAL, gateway("other", "mystery-model")], PRICES)
    assert addons.default_choice(choices).id == "llama"
    assert addons.default_choice([]) is None


def test_a_choice_is_found_by_its_agent_and_model_and_goes_to_the_page_as_a_dict():
    choices = addons.diagram_choices([CLAUDE_CODE, LOCAL], PRICES)
    assert addons.find_choice(choices, "claude", "haiku").id == "claude+haiku"
    assert addons.find_choice(choices, "claude", None).id == "claude"
    assert addons.find_choice(choices, "claude", "").id == "claude"
    assert addons.find_choice(choices, "claude", "sonnet") is None
    assert addons.find_choice(choices, "llama", "haiku") is None and addons.find_choice(choices, "nobody", "") is None
    assert addons.find_choice(choices, "claude", "haiku").to_dict() == {
        "id": "claude+haiku", "agent": "claude", "model": "haiku", "label": "Haiku, via Claude Code",
        "detail": addons.HAIKU_MODEL, "price": [1.0, 5.0], "haiku": True, "local": False}
    assert addons.find_choice(choices, "llama", "").to_dict()["price"] is None


# ── The Settings routes ──────────────────────────────────────────────────────

CONFIG = """\
# My Ixel settings
[agents.local]
type = "http"
url = "http://127.0.0.1:11434/v1/chat/completions"
label = "Llama"
model = "llama3"

[review]
mode = "review"   # what runs
"""
LEGACY = "\n[addons]\ndiagram_design = true\n"  # the switch the old code wrote in config.toml


@pytest.fixture
def config():
    path = loader._GLOBAL_CONFIG
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(CONFIG, encoding="utf-8")
    return path


def gui():
    return GuiServer(token=TOKEN, settings_loader=load_settings)


def panel_gui(*configs):
    """A server whose panel is exactly these models (no settings file)."""
    settings = Settings({}, {cfg.name: cfg for cfg in configs})
    return GuiServer(token=TOKEN, settings_loader=lambda: settings)


def call(*steps):
    """Each step is ("GET", path) or ("POST", path, body); returns [(status, json)]."""
    async def scenario(client):
        out = []
        for method, path, *body in steps:
            if method == "GET":
                resp = await client.get(path, headers=AUTH)
            else:
                resp = await client.post(path, headers=JSON_AUTH, data=json.dumps(body[0]))
            out.append((resp.status, await resp.json()))
        return out
    return run_with_client(gui(), scenario)


def snapshot():
    [(status, data)] = call(("GET", "/api/settings"))
    assert status == 200
    return data


def switch(card_id, on, **body):
    """POST /api/addons the way the page does, with the version of the settings file it last read."""
    body = {"id": card_id, "on": on, "version": snapshot()["version"], **body}
    [(status, data)] = call(("POST", "/api/addons", body))
    return status, data


def rows() -> dict:
    """GET /api/addons: each row by the card's id."""
    [(status, data)] = call(("GET", "/api/addons"))
    assert status == 200
    return {row["id"]: row for row in data["addons"]}


def written(config) -> dict:
    return loader.tomllib.loads(config.read_text(encoding="utf-8"))


def test_the_list_has_every_card_builtin_first_and_the_ones_that_couldnt_be_read(config, builtin):
    agent = addons.write_agent_card(card_text())
    broken = put_card("broken-one", card_text(id="broken-one", kind="plugin"))
    [(status, data)] = call(("GET", "/api/addons"))
    assert status == 200
    assert [row["id"] for row in data["addons"]] == [DIAGRAM, "pdf-notes"]
    first, second = data["addons"]
    assert (first["written_by_agent"], second["written_by_agent"]) == (False, True)
    assert first["card_sha256"] == builtin.card_sha256 and second["card_sha256"] == agent.card_sha256
    assert first["version"] == "c" * 12 and second["version"] == "d" * 12
    assert second["permissions"] == {"files": "none", "network": False, "commands": []}
    assert first["permissions"]["commands"] == [["python3", "{addon}/scripts/self_check.py", "{output}"]]
    assert any("Written by the agent, not reviewed" in line["text"] for line in second["plain"])
    assert not any("Written by the agent" in line["text"] for line in first["plain"])
    assert all(set(line) == {"text", "warn"} for line in second["plain"])
    [problem] = data["problems"]
    assert problem["path"] == str(broken) and "`kind`" in problem["error"]


def test_a_row_follows_the_cards_state(config, builtin):
    row = rows()[DIAGRAM]
    assert (row["on"], row["approved"], row["installed"], row["enabled"], row["path"]) == \
        (False, False, False, False, "")
    addons.install(builtin, fetch=lambda url: TARBALL)  # its files are there, but nobody approved it
    row = rows()[DIAGRAM]
    assert (row["installed"], row["enabled"], row["path"]) == (True, False, str(builtin.files_dir))
    addons.approve_and_switch_on(builtin)
    assert rows()[DIAGRAM]["enabled"] is True
    shutil.rmtree(builtin.files_dir)  # chosen and approved, but its files went
    row = rows()[DIAGRAM]
    assert (row["on"], row["approved"], row["installed"], row["enabled"]) == (True, True, False, False)


def test_an_old_switch_in_the_settings_file_shows_as_on_before_and_switches_nothing(config, builtin):
    config.write_text(CONFIG + LEGACY, encoding="utf-8")
    row = rows()[DIAGRAM]
    assert row["was_on_before"] is True and row["on"] is False and row["enabled"] is False
    assert addons.enabled_cards() == []


@pytest.mark.parametrize("body", [
    {"id": "nope", "on": True},
    {"id": [DIAGRAM], "on": True},
    {"id": DIAGRAM, "on": "yes"},
    {"id": DIAGRAM, "on": 1},
    {"id": DIAGRAM},
])
def test_only_a_known_card_can_be_switched_on_or_off(config, builtin, release, body):
    [(status, data)] = call(("POST", "/api/addons", {**body, "version": snapshot()["version"]}))
    assert status == 400 and data["error"]
    assert release == [] and config.read_text(encoding="utf-8") == CONFIG and not paths.state_path().exists()


# ── Turning a card on needs its approval ─────────────────────────────────────

@pytest.mark.parametrize("shown", [None, "", 5], ids=["missing", "empty", "not-text"])
def test_without_the_cards_sha_nothing_is_downloaded_approved_or_installed(config, builtin, release, shown):
    body = {"id": DIAGRAM, "on": True, **({} if shown is None else {"card_sha256": shown})}
    [(status, data)] = call(("POST", "/api/addons", body))
    assert status == 409 and "approval" in data["error"]
    assert release == [] and not paths.state_path().exists()
    assert not addons.installed(builtin) and not builtin.files_dir.exists()


def test_with_another_cards_sha_nothing_is_downloaded_approved_or_installed(config, builtin, release):
    [(status, data)] = call(("POST", "/api/addons", {"id": DIAGRAM, "on": True, "card_sha256": "0" * 64}))
    assert status == 409 and "changed" in data["error"]
    assert release == [] and not paths.state_path().exists()
    assert not addons.installed(builtin) and not builtin.files_dir.exists()


def test_with_the_right_sha_the_card_is_installed_approved_and_switched_on(config, builtin, release):
    status, data = switch(DIAGRAM, True, card_sha256=builtin.card_sha256)
    assert status == 200 and "Diagram Design is on" in data["message"]
    assert release == [URL]
    assert addons.tree_hash(builtin.files_dir) == expected_tree(SKILL_FILES)
    entry = saved_state()[DIAGRAM]
    assert entry["on"] is True and entry["approved_card_sha256"] == builtin.card_sha256
    assert entry["approved_tree_sha256"] == TREE_SHA
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", entry["approved_at"])
    [row] = data["addons"]
    assert (row["on"], row["approved"], row["installed"], row["enabled"]) == (True, True, True, True)
    assert row["path"] == str(builtin.files_dir) and row["changed_since_approval"] is False
    assert data["settings"]["version"] and data["problems"] == []
    assert config.read_text(encoding="utf-8") == CONFIG  # nothing in config.toml to tidy: it isn't touched


def test_a_release_that_doesnt_match_the_pin_isnt_installed_or_approved(config, builtin, monkeypatch):
    tampered = tarball({**SKILL_FILES, "SKILL.md": "# Something else\n"})
    monkeypatch.setattr(installer, "download", lambda url, **kwargs: tampered)
    status, data = switch(DIAGRAM, True, card_sha256=builtin.card_sha256)
    assert status == 409 and data["error"]
    assert not builtin.files_dir.exists() and not paths.state_path().exists()
    assert config.read_text(encoding="utf-8") == CONFIG


def test_with_no_settings_file_a_card_can_still_be_switched_on_and_no_file_is_made(builtin, release):
    assert not loader._GLOBAL_CONFIG.exists()
    status, data = switch(DIAGRAM, True, card_sha256=builtin.card_sha256)
    assert status == 200 and addons.card_by_id(DIAGRAM).enabled
    assert not loader._GLOBAL_CONFIG.exists()


def test_an_agent_written_card_cant_be_switched_on_without_approval(config, release):
    addons.write_agent_card(card_text())
    [(status, data)] = call(("POST", "/api/addons", {"id": "pdf-notes", "on": True}))
    assert status == 409 and "approval" in data["error"]
    assert release == [] and not paths.state_path().exists()
    assert addons.enabled_cards() == []


def test_a_card_the_agent_changed_after_approval_needs_approving_again(config, release):
    card = addons.write_agent_card(card_text())
    status, data = switch("pdf-notes", True, card_sha256=card.card_sha256)
    assert status == 200
    [row] = data["addons"]
    assert row["enabled"] is True and row["written_by_agent"] is True

    changed = addons.write_agent_card(card_text(permissions__commands=[["python3", "{addon}/scripts/run.py"]]))
    assert changed.card_sha256 != card.card_sha256
    [row] = rows().values()
    assert row["changed_since_approval"] is True and row["enabled"] is False
    assert row["card_sha256"] == changed.card_sha256
    assert addons.enabled_cards() == []

    status, data = switch("pdf-notes", True, card_sha256=card.card_sha256)  # the dialog showed the old card
    assert status == 409 and "changed" in data["error"]
    assert saved_state()["pdf-notes"]["approved_card_sha256"] == card.card_sha256
    status, data = switch("pdf-notes", True, card_sha256=changed.card_sha256)
    assert status == 200
    [row] = data["addons"]
    assert row["enabled"] is True and row["changed_since_approval"] is False
    assert saved_state()["pdf-notes"]["approved_card_sha256"] == changed.card_sha256


def test_switching_off_clears_on_and_removes_the_files_but_an_agent_card_stays(config, release):
    card = addons.write_agent_card(card_text())
    status, _ = switch("pdf-notes", True, card_sha256=card.card_sha256)
    assert status == 200 and card.files_dir.is_dir()
    status, data = switch("pdf-notes", False)
    assert status == 200 and "off" in data["message"] and "removed" in data["message"]
    assert saved_state()["pdf-notes"]["on"] is False
    assert not card.files_dir.exists() and card.card_path.is_file()
    [row] = data["addons"]
    assert (row["on"], row["installed"], row["enabled"], row["path"]) == (False, False, False, "")


def test_switching_a_builtin_card_off_removes_its_files_and_its_folder(config, builtin, release):
    status, _ = switch(DIAGRAM, True, card_sha256=builtin.card_sha256)
    assert status == 200 and builtin.files_dir.is_dir()
    status, data = switch(DIAGRAM, False)
    assert status == 200
    assert saved_state()[DIAGRAM]["on"] is False and not builtin.dir.exists()
    assert addons.enabled_cards() == []


def test_switching_off_a_card_that_was_never_on_writes_no_state(config, builtin):
    status, data = switch(DIAGRAM, False)
    assert status == 200 and not paths.state_path().exists()


# ── The old [addons] line in config.toml ─────────────────────────────────────

def assert_old_line_gone(config) -> None:
    """The line and its empty table are out; the rest of the file is as it was, and the old file is kept."""
    text = config.read_text(encoding="utf-8")
    assert "addons" not in written(config)
    assert "[addons]" not in text and "diagram_design" not in text
    assert text.startswith(CONFIG)  # its comment and its spacing as they were
    assert config.with_name("config.toml.bak").read_text(encoding="utf-8") == CONFIG + LEGACY


def test_switching_off_takes_the_old_line_and_its_empty_table_out(config, builtin):
    config.write_text(CONFIG + LEGACY, encoding="utf-8")
    status, data = switch(DIAGRAM, False)
    assert status == 200 and "off" in data["message"]
    assert_old_line_gone(config)
    assert data["addons"][0]["was_on_before"] is False


def test_switching_on_takes_the_old_line_and_its_empty_table_out_too(config, builtin, release):
    config.write_text(CONFIG + LEGACY, encoding="utf-8")
    status, data = switch(DIAGRAM, True, card_sha256=builtin.card_sha256)
    assert status == 200 and addons.card_by_id(DIAGRAM).enabled
    assert_old_line_gone(config)
    assert data["addons"][0]["was_on_before"] is False


def test_another_key_in_the_addons_table_keeps_the_table_and_that_key(config, builtin):
    config.write_text(CONFIG + LEGACY + "other = 1\n", encoding="utf-8")
    status, data = switch(DIAGRAM, False)
    assert status == 200
    assert written(config)["addons"] == {"other": 1}
    assert "diagram_design" not in config.read_text(encoding="utf-8")
    assert config.read_text(encoding="utf-8").startswith(CONFIG)


def test_a_page_that_read_an_older_file_is_told_before_anything_is_downloaded(config, builtin, release):
    config.write_text(CONFIG + LEGACY, encoding="utf-8")
    old = snapshot()["version"]
    config.write_text(config.read_text(encoding="utf-8") + "\n# edited by hand\n", encoding="utf-8")
    [(status, data)] = call(("POST", "/api/addons", {"id": DIAGRAM, "on": True, "card_sha256": builtin.card_sha256,
                                                     "version": old}))
    assert status == 409 and "changed" in data["error"]
    assert release == [] and not builtin.files_dir.exists() and not paths.state_path().exists()
    assert "diagram_design = true" in config.read_text(encoding="utf-8")


@pytest.mark.parametrize("version", [None, "", 123, ["x"]])
def test_a_page_that_didnt_say_which_file_it_read_is_told_to_look_again(config, builtin, release, version):
    config.write_text(CONFIG + LEGACY, encoding="utf-8")
    body = {"id": DIAGRAM, "on": True, "card_sha256": builtin.card_sha256}
    if version is not None:
        body["version"] = version
    [(status, data)] = call(("POST", "/api/addons", body))
    assert status == 409 and data["settings"]["version"]
    assert release == [] and not builtin.files_dir.exists() and not paths.state_path().exists()
    assert config.read_text(encoding="utf-8") == CONFIG + LEGACY


def test_with_no_old_line_a_missing_version_is_fine(config, builtin, release):
    [(status, data)] = call(("POST", "/api/addons", {"id": DIAGRAM, "on": True, "card_sha256": builtin.card_sha256}))
    assert status == 200 and addons.card_by_id(DIAGRAM).enabled
    assert config.read_text(encoding="utf-8") == CONFIG
    assert not config.with_name("config.toml.bak").exists()


def test_set_values_drops_an_emptied_table_only_when_asked_to():
    text = CONFIG + LEGACY
    kept = edit.set_values(text, ("addons",), {"diagram_design": edit.REMOVE})
    assert loader.tomllib.loads(kept)["addons"] == {}  # the default: the empty table stays
    dropped = edit.set_values(text, ("addons",), {"diagram_design": edit.REMOVE}, drop_empty=True)
    assert "addons" not in loader.tomllib.loads(dropped) and "[addons]" not in dropped
    assert dropped.rstrip("\n") == CONFIG.rstrip("\n")
    with_other = edit.set_values(text + "other = 1\n", ("addons",), {"diagram_design": edit.REMOVE},
                                 drop_empty=True)
    assert loader.tomllib.loads(with_other)["addons"] == {"other": 1}
    # a table that isn't there is left alone
    assert edit.set_values(CONFIG, ("addons",), {"diagram_design": edit.REMOVE}, drop_empty=True) == CONFIG


# ── Making a diagram from the page ───────────────────────────────────────────

def post_diagram(server, body):
    """POST /api/diagram: (200, the events of the stream) when a diagram was begun, else (status, the error)."""
    async def scenario(client):
        resp = await client.post("/api/diagram", headers=JSON_AUTH, data=json.dumps(body))
        if resp.status == 200:
            return resp.status, await read_events(resp)
        return resp.status, await resp.json()
    return run_with_client(server, scenario)


def get_panel(server):
    async def scenario(client):
        resp = await client.get("/api/panel", headers=AUTH)
        return resp.status, await resp.json()
    return run_with_client(server, scenario)


def fake_diagram(monkeypatch):
    """Stands in for make_diagram: saves a page, and returns (agent, its model, question, folder) for each call."""
    asked = []

    async def fake_make(cfg, question, folder, *, on_event=None, **kwargs):
        asked.append((cfg.name, cfg.model, question, folder))
        path = addons.save_diagram(GOOD, question)
        return addons.DiagramResult(path, "flowchart", addons.CheckResult(True, [], True), False, [])

    monkeypatch.setattr(addons, "make_diagram", fake_make)
    return asked


def test_asking_for_a_diagram_with_the_card_off_is_refused(config, builtin):
    body = {"question": "Draw the release steps"}
    status, data = post_diagram(gui(), body)
    assert status == 409 and data["code"] == "addon_off" and "Settings" in data["error"]
    addons.install(builtin, fetch=lambda url: TARBALL)  # installed isn't the same as approved and on
    status, data = post_diagram(gui(), body)
    assert status == 409 and data["code"] == "addon_off"
    addons.approve_and_switch_on(builtin)
    addons.switch_off(DIAGRAM)
    status, data = post_diagram(gui(), body)
    assert status == 409 and data["code"] == "addon_off"


@pytest.mark.parametrize("body, status", [
    ({"question": "   "}, 400),
    ({"question": 5}, 400),
    ({"question": "x" * 50_001}, 400),
    ({"question": "Draw it", "agent": ["gpt"]}, 400),
    ({"question": "Draw it", "model": 5}, 400),
    ({"question": "Draw it", "agent": "nobody"}, 409),
    ({"question": "Draw it", "agent": "local", "model": "haiku"}, 409),  # that model can't run Haiku
])
def test_a_diagram_needs_a_question_and_a_model_on_the_panel(config, diagram_on, body, status):
    got, data = post_diagram(gui(), body)
    assert got == status and data["error"]


def test_a_diagram_is_streamed_then_served_to_the_frame(config, diagram_on, monkeypatch):
    asked = []

    async def fake_make(cfg, question, folder, *, on_event=None, **kwargs):
        asked.append((cfg.name, question, folder))
        await on_event("start", {"kind": "flowchart", "agent": cfg.name, "label": cfg.label})
        path = addons.save_diagram(GOOD, question)
        return addons.DiagramResult(path, "flowchart", addons.CheckResult(True, [], True), False, [])

    monkeypatch.setattr(addons, "make_diagram", fake_make)

    async def scenario(client):
        resp = await client.post("/api/diagram", headers=JSON_AUTH,
                                 data=json.dumps({"question": "  Draw the release steps  "}))
        events = await read_events(resp)
        final = next(e for e in events if e["kind"] == "final")["data"]["diagram"]
        frame = await client.get(f"/diagram/{final['id']}")  # no token: a frame can't send one
        return resp.status, events, final, frame.status, await frame.text(), dict(frame.headers)

    status, events, final, frame_status, frame_text, frame_headers = run_with_client(gui(), scenario)
    assert status == 200 and [e["kind"] for e in events] == ["start", "final"]
    assert events[0]["data"] == {"kind": "flowchart", "agent": "local", "label": "Llama", "model": "llama3"}
    assert asked == [("local", "Draw the release steps", diagram_on.files_dir)]
    assert re.fullmatch(r"[0-9a-f]{32}", final["id"]) and final["kind"] == "flowchart"
    assert final["name"].endswith("-draw-the-release-steps.html")
    assert Path(final["file"]).parent == paths.DIAGRAMS_DIR and final["check"]["ok"] is True
    assert frame_status == 200 and frame_text == GOOD
    assert frame_headers["Content-Security-Policy"] == gui_server.DIAGRAM_CSP


def test_a_model_that_cant_draw_ends_the_stream_with_an_error(config, diagram_on, monkeypatch):
    async def fake_make(cfg, question, folder, *, on_event=None, **kwargs):
        raise addons.AddonError("Llama didn't answer with an HTML document.", 502, "no_html")

    monkeypatch.setattr(addons, "make_diagram", fake_make)
    status, events = post_diagram(gui(), {"question": "Draw it"})
    assert status == 200 and [e["kind"] for e in events] == ["error"]
    assert events[0]["data"] == {"message": "Llama didn't answer with an HTML document.", "code": "no_html"}


def test_a_local_model_failure_reaches_the_page_as_local_model(config, diagram_on, monkeypatch):
    real = addons.make_diagram

    def with_a_model_that_fails(cfg, question, folder, **kwargs):  # the real make_diagram, asking nothing real
        return real(cfg, question, folder, ask=FailingAsk("Llama stopped answering."), **kwargs)

    monkeypatch.setattr(addons, "make_diagram", with_a_model_that_fails)
    status, events = post_diagram(gui(), {"question": "Draw it"})
    assert status == 200 and [e["kind"] for e in events] == ["start", "error"]
    assert events[1]["data"]["code"] == "local_model"
    assert "Llama stopped answering." in events[1]["data"]["message"] and "16k" in events[1]["data"]["message"]


def test_a_diagram_is_made_with_the_model_and_agent_the_page_picked(diagram_on, monkeypatch):
    asked = fake_diagram(monkeypatch)
    status, events = post_diagram(panel_gui(LOCAL, CLAUDE_CODE),
                                  {"question": "Draw the release steps", "agent": "claude", "model": "haiku"})
    assert status == 200 and [e["kind"] for e in events] == ["final"]
    assert asked == [("claude", "haiku", "Draw the release steps", diagram_on.files_dir)]

    asked.clear()  # the agent's own model: none is sent in its place
    status, _ = post_diagram(panel_gui(LOCAL, CLAUDE_CODE), {"question": "Draw it", "agent": "claude", "model": ""})
    assert status == 200 and asked == [("claude", "", "Draw it", diagram_on.files_dir)]
    asked.clear()
    status, _ = post_diagram(panel_gui(LOCAL, CLAUDE_CODE), {"question": "Draw it", "agent": "llama"})
    assert status == 200 and asked == [("llama", "llama3", "Draw it", diagram_on.files_dir)]


@pytest.mark.parametrize("body", [
    {"question": "Draw it", "agent": "claude", "model": "sonnet"},
    {"question": "Draw it", "agent": "llama", "model": "haiku"},
    {"question": "Draw it", "agent": "nobody", "model": "haiku"},
], ids=["a-model-it-doesnt-offer", "a-model-that-cant-run-haiku", "an-agent-not-on-the-panel"])
def test_a_model_and_agent_that_arent_offered_together_are_refused(diagram_on, monkeypatch, body):
    asked = fake_diagram(monkeypatch)
    status, data = post_diagram(panel_gui(LOCAL, CLAUDE_CODE), body)
    assert status == 409 and data["error"]
    assert asked == []


def test_with_no_agent_named_the_default_choice_draws(diagram_on, monkeypatch):
    asked = fake_diagram(monkeypatch)
    status, _ = post_diagram(panel_gui(LOCAL, CLAUDE_CODE), {"question": "Draw it"})
    assert status == 200 and asked == [("claude", "haiku", "Draw it", diagram_on.files_dir)]  # the first Haiku
    asked.clear()
    status, _ = post_diagram(panel_gui(LOCAL), {"question": "Draw it"})  # no Haiku to be had: the first model
    assert status == 200 and asked == [("llama", "llama3", "Draw it", diagram_on.files_dir)]


def test_the_panel_lists_no_diagram_models_while_the_card_is_off(builtin):
    status, data = get_panel(panel_gui(LOCAL, CLAUDE_CODE))
    assert status == 200 and data["addons"] == {"diagram": False} and data["diagram_models"] == []
    addons.install(builtin, fetch=lambda url: TARBALL)  # installed, not approved
    status, data = get_panel(panel_gui(LOCAL, CLAUDE_CODE))
    assert data["addons"] == {"diagram": False} and data["diagram_models"] == []
    addons.approve_and_switch_on(builtin)
    addons.switch_off(DIAGRAM)  # approved, but off
    status, data = get_panel(panel_gui(LOCAL, CLAUDE_CODE))
    assert data["addons"] == {"diagram": False} and data["diagram_models"] == []


def test_the_panel_lists_the_models_diagram_can_use_with_exactly_one_default(diagram_on):
    status, data = get_panel(panel_gui(LOCAL, CLAUDE_CODE))
    assert status == 200 and data["addons"] == {"diagram": True}
    models = data["diagram_models"]
    assert [m["id"] for m in models] == ["llama", "claude", "claude+haiku"]
    assert [m["id"] for m in models if m["default"]] == ["claude+haiku"]
    assert [m["haiku"] for m in models] == [False, False, True]
    assert models[0]["local"] is True and models[0]["price"] is None
    assert (models[2]["agent"], models[2]["model"]) == ("claude", "haiku")


def test_the_panel_from_the_settings_file_offers_its_one_model_as_the_default(config, diagram_on):
    status, data = get_panel(gui())
    assert status == 200 and data["addons"] == {"diagram": True}
    assert [(m["id"], m["default"]) for m in data["diagram_models"]] == [("local", True)]


# ── Serving a diagram ────────────────────────────────────────────────────────

def directives(policy: str) -> dict:
    parts = (p.strip() for p in policy.split(";"))
    return {p.split()[0]: p.split()[1:] for p in parts if p}


def test_a_diagram_is_served_with_a_sandbox_of_its_own(tmp_path):
    page = tmp_path / "made.html"
    page.write_text(GOOD, encoding="utf-8", newline="")
    server = gui()
    key = server.diagrams.add(page)

    async def scenario(client):
        resp = await client.get(f"/diagram/{key}")  # no Authorization: the frame can't send one
        return resp.status, dict(resp.headers), await resp.text(), len(resp.headers.getall("Content-Security-Policy"))

    status, headers, body, policies = run_with_client(server, scenario)
    assert status == 200 and body == GOOD
    assert headers["Content-Type"].startswith("text/html")
    assert headers["Content-Security-Policy"] == gui_server.DIAGRAM_CSP != gui_server.CSP
    assert policies == 1  # the page's own policy isn't added beside it
    assert headers["X-Frame-Options"] == "SAMEORIGIN" and headers["Cache-Control"] == "no-store"
    assert headers["X-Content-Type-Options"] == "nosniff"
    policy = directives(gui_server.DIAGRAM_CSP)
    assert policy["sandbox"] == ["allow-scripts"] and "allow-same-origin" not in gui_server.DIAGRAM_CSP
    assert policy["default-src"] == ["'none'"] and policy["connect-src"] == ["'none'"]
    assert policy["frame-ancestors"] == ["'self'"] and policy["form-action"] == ["'none'"]
    assert policy["script-src"] == ["'unsafe-inline'"]


def test_an_id_the_server_didnt_give_is_a_404(tmp_path):
    server = gui()
    gone = server.diagrams.add(tmp_path / "gone.html")  # given, but its file was deleted

    async def scenario(client):
        return [(await client.get(path)).status for path in
                (f"/diagram/{'0' * 32}", "/diagram/not-an-id", f"/diagram/{'A' * 32}", f"/diagram/{gone}")]

    assert run_with_client(server, scenario) == [404, 404, 404, 404]


def test_open_and_show_in_folder_work_from_the_id_alone(tmp_path, monkeypatch):
    from ixel_mat import docs
    page = tmp_path / "made" / "diagram.html"
    page.parent.mkdir()
    page.write_text(GOOD, encoding="utf-8")
    opened, shown = [], []
    monkeypatch.setattr(docs, "can_open_browser", lambda: True)
    monkeypatch.setattr(docs, "open_in_browser", lambda url: opened.append(url) or True)
    monkeypatch.setattr(addons, "show_in_folder", lambda path: shown.append(path) or True)
    server = gui()
    key = server.diagrams.add(page)

    async def post(client, path, body):
        resp = await client.post(path, headers=JSON_AUTH, data=json.dumps(body))
        return resp.status, await resp.json()

    async def scenario(client):
        return {
            "open": await post(client, "/api/diagram/open", {"id": key}),
            "reveal": await post(client, "/api/diagram/reveal", {"id": key}),
            "unknown": [await post(client, path, {"id": other})
                        for path in ("/api/diagram/open", "/api/diagram/reveal")
                        for other in ("0" * 32, "../../etc/passwd", 5, str(page))],
        }

    got = run_with_client(server, scenario)
    assert got["open"] == (200, {"opened": True, "file": str(page)})
    assert got["reveal"] == (200, {"shown": True, "folder": str(page.parent)})
    assert opened == [page.as_uri()] and shown == [page]  # and nothing for the ids it didn't know
    assert all(status == 404 and data["error"] for status, data in got["unknown"])


def test_the_page_may_frame_this_server_and_nothing_else_and_still_runs_no_inline_code():
    async def scenario(client):
        resp = await client.get("/")
        return dict(resp.headers)

    headers = run_with_client(gui(), scenario)
    csp = headers["Content-Security-Policy"]
    assert csp == gui_server.CSP
    policy = directives(csp)
    assert policy["frame-src"] == ["'self'"] and policy["frame-ancestors"] == ["'none'"]
    assert policy["script-src"] == ["'self'"] and policy["default-src"] == ["'none'"]
    assert "unsafe" not in csp
    assert headers["X-Frame-Options"] == "DENY"  # the page itself still can't be framed


def test_the_frame_the_page_makes_has_scripts_and_no_more():
    static = Path(gui_server.__file__).parent / "static"
    assert 'sandbox: "allow-scripts"' in (static / "ask.js").read_text(encoding="utf-8")
    for script in sorted(static.glob("*.js")):
        assert "allow-same-origin" not in script.read_text(encoding="utf-8"), script.name
