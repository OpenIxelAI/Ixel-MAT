"""The Diagram Design add-on: unpacking its release safely, making a diagram in Ask, and the Settings route that
switches it on and off. Nothing here uses the network, and everything it writes is in the test's own folder."""
import asyncio
import dataclasses
import hashlib
import io
import json
import os
import re
import stat
import tarfile
from datetime import datetime
from pathlib import Path

import pytest

from ixel_mat import addons
from ixel_mat.agents.base import AgentConfig
from ixel_mat.ask import AskResult
from ixel_mat.config import loader
from ixel_mat.gui import server as gui_server
from ixel_mat.gui.server import GuiServer
from ixel_mat.material import MAX_MATERIAL_CHARS
from ixel_mat.runtime import load_settings
from test_gui_server import AUTH, JSON_AUTH, TOKEN, read_events, run_with_client

NAME = addons.DIAGRAM_DESIGN.name
TOP = "diagram-design-7a2e22170ee8"
SUB = "skills/diagram-design"

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
# Everything else in the repository's archive: none of it is ever unpacked
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
PINNED = dataclasses.replace(addons.DIAGRAM_DESIGN, commit="c" * 40, tree_sha256=expected_tree(SKILL_FILES)[1],
                             files=len(SKILL_FILES))


@pytest.fixture(autouse=True)
def _folders(tmp_path, monkeypatch):
    """Add-ons and diagrams go in the test's own folder, never in ~/.ixel."""
    monkeypatch.setattr(addons, "ADDONS_DIR", tmp_path / "ixel-home" / "addons")
    monkeypatch.setattr(addons, "DIAGRAMS_DIR", tmp_path / "ixel-home" / "diagrams")


@pytest.fixture
def skill(tmp_path):
    """The skill unpacked, as it sits once installed."""
    root = tmp_path / "skill"
    for rel, text in SKILL_FILES.items():
        path = root.joinpath(*rel.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


@pytest.fixture
def release(monkeypatch):
    """This Ixel's pin is the test tarball's; downloading gives that tarball. Returns the urls asked for."""
    fetched = []

    def download(url, **kwargs):
        fetched.append(url)
        return TARBALL

    monkeypatch.setattr(addons, "download", download)
    monkeypatch.setattr(addons, "DIAGRAM_DESIGN", PINNED)
    monkeypatch.setitem(addons.ADDONS, NAME, PINNED)
    return fetched


# ── Unpacking ────────────────────────────────────────────────────────────────

def test_only_the_skills_folder_is_unpacked_and_its_files_counted(tmp_path):
    dest = tmp_path / "addons" / "diagram-design"
    assert addons.extract(TARBALL, SUB, dest) == len(SKILL_FILES)
    found = sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file())
    assert found == sorted(SKILL_FILES)  # no README, LICENSE, docs, other skill or look-alike folder
    assert (dest / "SKILL.md").read_text(encoding="utf-8") == SKILL_FILES["SKILL.md"]
    assert sorted(p.name for p in dest.parent.iterdir()) == ["diagram-design"]  # no .part or .old beside it


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


@pytest.mark.parametrize("subdir", ["", "..", "skills/../.."])
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

URL = "https://codeload.github.com/cathrynlavery/diagram-design/tar.gz/" + "c" * 40


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

@pytest.mark.parametrize("commit, tree", [("", ""), ("c" * 40, ""), ("", "a" * 64)])
def test_without_a_pin_nothing_is_downloaded_or_installed(commit, tree):
    unpinned = dataclasses.replace(PINNED, commit=commit, tree_sha256=tree)

    def fetch(url):
        raise AssertionError("an unpinned add-on must not be downloaded")

    with pytest.raises(addons.AddonError) as caught:
        addons.install(unpinned, fetch=fetch)
    assert caught.value.code == "unpinned" and caught.value.status == 409
    assert not addons.ADDONS_DIR.exists()


def test_a_pinned_release_is_downloaded_from_its_commit_and_unpacked():
    asked = []

    def fetch(url):
        asked.append(url)
        return TARBALL

    where = addons.install(PINNED, fetch=fetch)
    assert asked == ["https://codeload.github.com/cathrynlavery/diagram-design/tar.gz/" + "c" * 40]
    assert where == addons.ADDONS_DIR / "diagram-design" == addons.folder(PINNED)
    assert addons.installed(PINNED) and (where / "scripts" / "self_check.py").is_file()


def test_a_release_that_isnt_the_pinned_one_is_not_installed():
    tampered = tarball({**SKILL_FILES, "scripts/self_check.py": "import os\nos.system('echo hi')\n"})
    with pytest.raises(addons.AddonError) as caught:
        addons.install(PINNED, fetch=lambda url: tampered)
    assert caught.value.code == "tree_mismatch"
    assert not addons.installed(PINNED) and not addons.folder(PINNED).exists()


def test_removing_an_add_on_takes_its_folder_and_says_whether_there_was_one():
    assert addons.remove(PINNED) is False
    addons.install(PINNED, fetch=lambda url: TARBALL)
    assert addons.remove(PINNED) is True
    assert not addons.folder(PINNED).exists() and not addons.installed(PINNED)
    assert addons.remove(PINNED) is False


def test_removing_a_link_removes_the_link_not_what_it_points_at(tmp_path):
    elsewhere = tmp_path / "my-own-copy"
    elsewhere.mkdir()
    (elsewhere / "SKILL.md").write_text("# mine\n", encoding="utf-8")
    addons.ADDONS_DIR.mkdir(parents=True)
    try:
        addons.folder(PINNED).symlink_to(elsewhere, target_is_directory=True)
    except OSError:  # Windows lets only administrators, or Developer Mode, make links
        pytest.skip("this account can't make symbolic links")
    assert addons.remove(PINNED) is True
    assert not addons.folder(PINNED).exists() and (elsewhere / "SKILL.md").is_file()


def test_an_add_on_is_on_only_when_chosen_and_installed():
    chosen = {"addons": {"diagram_design": True}}
    assert not addons.is_on(chosen, PINNED)  # chosen, but its files aren't there
    addons.install(PINNED, fetch=lambda url: TARBALL)
    assert addons.is_on(chosen, PINNED)
    assert not addons.is_on({}, PINNED) and not addons.is_on({"addons": {}}, PINNED)
    assert not addons.is_on({"addons": {"diagram_design": "yes"}}, PINNED)
    assert not addons.is_on({"addons": "on"}, PINNED)


def test_the_status_says_what_is_pinned_and_where_it_is(monkeypatch):
    monkeypatch.setitem(addons.ADDONS, NAME, PINNED)
    [row] = addons.status({})
    assert (row["name"], row["label"], row["on"], row["installed"], row["pinned"], row["path"]) == \
        (NAME, "Diagram Design", False, False, True, "")
    assert row["version"] == "c" * 12 and row["license"] == "MIT" and row["author"] == "Cathryn Lavery"
    addons.install(PINNED, fetch=lambda url: TARBALL)
    [row] = addons.status({"addons": {"diagram_design": True}})
    assert row["on"] and row["installed"] and row["path"] == str(addons.folder(PINNED))
    monkeypatch.setitem(addons.ADDONS, NAME, dataclasses.replace(PINNED, commit="", tree_sha256=""))
    [row] = addons.status({})
    assert row["pinned"] is False and row["version"] == ""


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
    assert first == addons.DIAGRAMS_DIR / "2026-10-11-our-ci-pipeline.html"
    assert first.read_bytes() == html.encode("utf-8")
    second = addons.save_diagram("<p>second</p>", "Our CI pipeline", now=now)
    third = addons.save_diagram("<p>third</p>", "Our CI pipeline", now=now)
    assert [p.name for p in (second, third)] == ["2026-10-11-our-ci-pipeline-2.html",
                                                 "2026-10-11-our-ci-pipeline-3.html"]
    assert first.read_bytes() == html.encode("utf-8")  # still the first one's


def test_a_diagram_can_be_saved_in_a_folder_of_the_callers(tmp_path):
    path = addons.save_diagram(GOOD, "x", folder=tmp_path / "mine", now=datetime(2026, 1, 2))
    assert path == tmp_path / "mine" / "2026-01-02-x.html" and path.read_text(encoding="utf-8") == GOOD
    assert not addons.DIAGRAMS_DIR.exists()


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
    """Stands in for ixel_mat.ask.ask: gives each answer in turn, and keeps what it was asked."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    async def __call__(self, cfg, question, material, timeout=None):
        self.calls.append((question, material))
        return AskResult(agent=cfg.name, label=cfg.label, model="m-test", answer=self.answers.pop(0), ms=12,
                         usage={"input_tokens": 5})


def make(skill, question, *answers, on_event=None, out=None):
    ask = FakeAsk(*answers)
    result = asyncio.run(addons.make_diagram(CFG, question, skill, ask=ask, on_event=on_event, diagrams_dir=out))
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
    assert not out.exists() and events == ["start", "model"]


def test_a_skill_with_no_check_is_never_retried(skill):
    (skill / "scripts" / "self_check.py").unlink()
    result, ask = make(skill, "Draw the steps", fenced(BAD))
    assert len(ask.calls) == 1 and result.retried is False and not result.check.ran


# ── The Settings route ───────────────────────────────────────────────────────

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
SWITCHED_ON = "\n[addons]\ndiagram_design = true\n"


@pytest.fixture
def config():
    path = loader._GLOBAL_CONFIG
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(CONFIG, encoding="utf-8")
    return path


def gui():
    return GuiServer(token=TOKEN, settings_loader=load_settings)


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


def switch(on, version=None, **body):
    body = {"name": NAME, "on": on, "version": snapshot()["version"] if version is None else version, **body}
    [(status, data)] = call(("POST", "/api/addons", body))
    return status, data


def written(config) -> dict:
    return loader.tomllib.loads(config.read_text(encoding="utf-8"))


def test_switching_on_installs_the_add_on_and_writes_the_setting(config, release):
    version = snapshot()["version"]
    status, data = switch(True, version)
    assert status == 200 and "Diagram Design is on" in data["message"]
    assert release == [addons.tarball_url(PINNED)]
    assert written(config)["addons"] == {"diagram_design": True}
    text = config.read_text(encoding="utf-8")
    assert "# My Ixel settings" in text and 'mode = "review"   # what runs' in text  # the rest of the file as it was
    assert config.with_suffix(".toml.bak").read_text(encoding="utf-8") == CONFIG
    folder = addons.ADDONS_DIR / "diagram-design"
    assert (folder / "SKILL.md").is_file() and (folder / "references" / "style-guide.md").is_file()
    [row] = data["addons"]
    assert (row["name"], row["on"], row["installed"], row["pinned"]) == (NAME, True, True, True)
    assert row["path"] == str(folder) and row["version"] == "c" * 12
    assert data["settings"]["version"] and data["settings"]["version"] != version  # the page takes the new one


def test_switching_off_takes_the_setting_and_the_files_away(config, release):
    status, data = switch(True)
    assert status == 200 and addons.installed(PINNED)
    status, data = switch(False, data["settings"]["version"])
    assert status == 200 and "off" in data["message"] and "removed" in data["message"]
    assert "diagram_design" not in written(config).get("addons", {})
    assert not addons.folder(PINNED).exists()
    [row] = data["addons"]
    assert (row["on"], row["installed"], row["path"]) == (False, False, "")
    assert "# My Ixel settings" in config.read_text(encoding="utf-8")


def test_a_page_that_read_an_older_file_is_told_before_anything_is_downloaded(config, release):
    old = snapshot()["version"]
    config.write_text(CONFIG.replace('mode = "review"', 'mode = "quick"'), encoding="utf-8")  # ixel setup ran
    status, data = switch(True, old)
    assert status == 409 and "changed" in data["error"]
    assert data["settings"]["review"]["mode"] == "quick"
    assert release == [] and not addons.folder(PINNED).exists()
    assert "addons" not in written(config)


def test_a_stale_page_cant_switch_it_off_either(config, release):
    status, data = switch(True)
    assert status == 200
    old = data["settings"]["version"]
    config.write_text(config.read_text(encoding="utf-8") + "\n# edited by hand\n", encoding="utf-8")
    status, data = switch(False, old)
    assert status == 409 and addons.installed(PINNED)
    assert written(config)["addons"] == {"diagram_design": True}


@pytest.mark.parametrize("version", [None, "", 123, ["x"]])
def test_a_page_that_didnt_say_which_file_it_read_is_told_to_look_again(config, release, version):
    body = {"name": NAME, "on": True, **({} if version is None else {"version": version})}
    [(status, data)] = call(("POST", "/api/addons", body))
    assert status == 409 and data["settings"]["version"]
    assert release == [] and not addons.folder(PINNED).exists()
    assert config.read_text(encoding="utf-8") == CONFIG


@pytest.mark.parametrize("body", [
    {"name": "nope", "on": True},
    {"name": ["diagram-design"], "on": True},
    {"name": NAME, "on": "yes"},
    {"name": NAME, "on": 1},
    {"name": NAME},
])
def test_only_a_known_add_on_can_be_switched_on_or_off(config, release, body):
    [(status, data)] = call(("POST", "/api/addons", {**body, "version": snapshot()["version"]}))
    assert status == 400 and data["error"]
    assert release == [] and config.read_text(encoding="utf-8") == CONFIG


def test_an_add_on_this_ixel_has_no_pin_for_isnt_switched_on(config, release, monkeypatch):
    monkeypatch.setitem(addons.ADDONS, NAME, dataclasses.replace(PINNED, commit="", tree_sha256=""))
    status, data = switch(True)
    assert status == 409 and "no pinned release" in data["error"]
    assert release == [] and not addons.ADDONS_DIR.exists()
    assert config.read_text(encoding="utf-8") == CONFIG


def test_a_release_that_doesnt_match_the_pin_isnt_installed_or_switched_on(config, release, monkeypatch):
    tampered = tarball({**SKILL_FILES, "SKILL.md": "# Something else\n"})
    monkeypatch.setattr(addons, "download", lambda url, **kwargs: tampered)
    status, data = switch(True)
    assert status == 409 and data["error"]
    assert not addons.folder(PINNED).exists() and config.read_text(encoding="utf-8") == CONFIG


def test_with_no_settings_file_switching_on_makes_one_and_off_makes_none(release):
    assert not loader._GLOBAL_CONFIG.exists()
    [(status, data)] = call(("POST", "/api/addons", {"name": NAME, "on": True, "version": ""}))
    assert status == 200 and addons.installed(PINNED)
    assert written(loader._GLOBAL_CONFIG) == {"addons": {"diagram_design": True}}
    loader._GLOBAL_CONFIG.unlink()
    [(status, data)] = call(("POST", "/api/addons", {"name": NAME, "on": False, "version": ""}))
    assert status == 200 and not addons.folder(PINNED).exists()
    assert not loader._GLOBAL_CONFIG.exists()


def test_the_list_says_what_is_on_and_installed_and_where(config, release):
    [(status, data)] = call(("GET", "/api/addons"))
    assert status == 200 and len(data["addons"]) == 1
    row = data["addons"][0]
    assert (row["name"], row["label"], row["on"], row["installed"], row["path"]) == \
        (NAME, "Diagram Design", False, False, "")
    addons.install(PINNED, fetch=lambda url: TARBALL)  # its files are there, but it hasn't been chosen
    [(_, data)] = call(("GET", "/api/addons"))
    assert (data["addons"][0]["on"], data["addons"][0]["installed"]) == (False, True)
    config.write_text(CONFIG + SWITCHED_ON, encoding="utf-8")
    [(_, data)] = call(("GET", "/api/addons"))
    row = data["addons"][0]
    assert (row["on"], row["installed"], row["path"]) == (True, True, str(addons.folder(PINNED)))
    (addons.folder(PINNED) / "SKILL.md").unlink()  # chosen, but its files went
    [(_, data)] = call(("GET", "/api/addons"))
    assert (data["addons"][0]["on"], data["addons"][0]["installed"]) == (False, False)


def test_the_panel_says_whether_diagram_is_on(config, release):
    [(status, data)] = call(("GET", "/api/panel"))
    assert status == 200 and data["addons"] == {"diagram": False}
    addons.install(PINNED, fetch=lambda url: TARBALL)
    [(_, data)] = call(("GET", "/api/panel"))
    assert data["addons"] == {"diagram": False}  # installed, not chosen
    config.write_text(CONFIG + SWITCHED_ON, encoding="utf-8")
    [(_, data)] = call(("GET", "/api/panel"))
    assert data["addons"] == {"diagram": True}


# ── Making a diagram from the page ───────────────────────────────────────────

def test_asking_for_a_diagram_with_the_add_on_off_is_refused(config, release):
    [(status, data)] = call(("POST", "/api/diagram", {"question": "Draw the release steps"}))
    assert status == 409 and data["code"] == "addon_off" and "Settings" in data["error"]
    addons.install(PINNED, fetch=lambda url: TARBALL)  # installed isn't the same as switched on
    [(status, data)] = call(("POST", "/api/diagram", {"question": "Draw the release steps"}))
    assert status == 409 and data["code"] == "addon_off"


@pytest.mark.parametrize("body, status", [
    ({"question": "   "}, 400),
    ({"question": 5}, 400),
    ({"question": "x" * 50_001}, 400),
    ({"question": "Draw it", "agent": ["gpt"]}, 400),
    ({"question": "Draw it", "agent": "nobody"}, 409),
])
def test_a_diagram_needs_a_question_and_a_model_on_the_panel(config, release, body, status):
    addons.install(PINNED, fetch=lambda url: TARBALL)
    config.write_text(CONFIG + SWITCHED_ON, encoding="utf-8")
    [(got, data)] = call(("POST", "/api/diagram", body))
    assert got == status and data["error"]


def test_a_diagram_is_streamed_then_served_to_the_frame(config, release, monkeypatch):
    addons.install(PINNED, fetch=lambda url: TARBALL)
    config.write_text(CONFIG + SWITCHED_ON, encoding="utf-8")
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
    assert asked == [("local", "Draw the release steps", addons.folder(PINNED))]
    assert re.fullmatch(r"[0-9a-f]{32}", final["id"]) and final["kind"] == "flowchart"
    assert final["name"].endswith("-draw-the-release-steps.html")
    assert Path(final["file"]).parent == addons.DIAGRAMS_DIR and final["check"]["ok"] is True
    assert frame_status == 200 and frame_text == GOOD
    assert frame_headers["Content-Security-Policy"] == gui_server.DIAGRAM_CSP


def test_a_model_that_cant_draw_ends_the_stream_with_an_error(config, release, monkeypatch):
    addons.install(PINNED, fetch=lambda url: TARBALL)
    config.write_text(CONFIG + SWITCHED_ON, encoding="utf-8")

    async def fake_make(cfg, question, folder, *, on_event=None, **kwargs):
        raise addons.AddonError("Llama didn't answer with an HTML document.", 502, "no_html")

    monkeypatch.setattr(addons, "make_diagram", fake_make)

    async def scenario(client):
        resp = await client.post("/api/diagram", headers=JSON_AUTH, data=json.dumps({"question": "Draw it"}))
        return await read_events(resp)

    events = run_with_client(gui(), scenario)
    assert [e["kind"] for e in events] == ["error"]
    assert events[0]["data"] == {"message": "Llama didn't answer with an HTML document.", "code": "no_html"}


# ── Serving a diagram ────────────────────────────────────────────────────────

def directives(policy: str) -> dict:
    parts = (p.strip() for p in policy.split(";"))
    return {p.split()[0]: p.split()[1:] for p in parts if p}


def test_a_diagram_is_served_with_a_sandbox_of_its_own(tmp_path):
    page = tmp_path / "made.html"
    page.write_text(GOOD, encoding="utf-8")
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
