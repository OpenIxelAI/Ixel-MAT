"""The agent's starter files: a new agent folder begins with them, and the example skill is offered."""
from ixel_mat.agent import store as agent_store
from ixel_mat.agent.store import AgentStore


def test_a_new_folder_starts_with_the_starter_files(tmp_path):
    s = AgentStore(tmp_path / "agent")
    assert s.file_text("soul") == (agent_store.STARTER / "SOUL.md").read_text(encoding="utf-8")
    assert s.file_text("user").startswith("# ")
    assert s.file_text("memory").startswith("# Memory")


def test_the_example_skill_is_copied_once_and_listed(tmp_path):
    s = AgentStore(tmp_path / "agent")
    names = [k["name"] for k in s.skills()]
    assert "summarize-folder" in names
    mine = tmp_path / "agent" / "skills" / "summarize-folder" / "SKILL.md"
    mine.write_text("---\nname: summarize-folder\ndescription: my own\n---\n", encoding="utf-8")
    AgentStore(tmp_path / "agent").skills()
    assert "my own" in mine.read_text(encoding="utf-8")


def test_a_skills_folder_of_your_own_gets_no_examples(tmp_path):
    (tmp_path / "agent" / "skills" / "mine").mkdir(parents=True)
    assert AgentStore(tmp_path / "agent").skills() == []


def test_an_older_ixels_skills_folder_gets_each_new_example_once(tmp_path):
    import shutil
    skills = tmp_path / "agent" / "skills"
    (skills / "summarize-folder").mkdir(parents=True)  # what an older Ixel copied in
    (skills / "summarize-folder" / "SKILL.md").write_bytes(
        (agent_store.STARTER / "skills" / "summarize-folder" / "SKILL.md").read_bytes())
    names = {k["name"] for k in AgentStore(tmp_path / "agent").skills()}
    assert {"summarize-folder", "describe-photos", "add-tool-from-github"} <= names
    shutil.rmtree(skills / "describe-photos")  # you deleted one: it stays deleted
    assert "describe-photos" not in {k["name"] for k in AgentStore(tmp_path / "agent").skills()}
