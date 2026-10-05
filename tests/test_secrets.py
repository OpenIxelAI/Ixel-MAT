from ixel_mat.config import secrets


def test_load_env_reads_dotenv_and_populates_environment(monkeypatch, tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("# comment\nAPI_KEY='abc123'\nOTHER=value\n")
    monkeypatch.setattr(secrets, "_ENV_FILE", env_path)
    monkeypatch.delenv("API_KEY", raising=False)
    monkeypatch.delenv("OTHER", raising=False)

    loaded = secrets.load_env()

    assert loaded == {"API_KEY": "abc123", "OTHER": "value"}
    assert secrets.os.environ["API_KEY"] == "abc123"
    assert secrets.os.environ["OTHER"] == "value"


def test_load_env_does_not_override_existing_environment_variable(monkeypatch, tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("API_KEY=from_file\n")
    monkeypatch.setattr(secrets, "_ENV_FILE", env_path)
    monkeypatch.setenv("API_KEY", "from_env")

    loaded = secrets.load_env()

    assert loaded == {"API_KEY": "from_file"}
    assert secrets.os.environ["API_KEY"] == "from_env"
