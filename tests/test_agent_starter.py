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


def test_a_skills_folder_you_already_have_gets_no_examples(tmp_path):
    (tmp_path / "agent" / "skills").mkdir(parents=True)
    assert AgentStore(tmp_path / "agent").skills() == []
