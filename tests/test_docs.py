"""The shipped example config and README stay in step with the code."""
from pathlib import Path

from ixel_mat.agents.base import AgentConfig
from ixel_mat.config.loader import build_agent_configs, tomllib
from ixel_mat.config.setup import CLI_PRESETS
from ixel_mat.runtime import parse_review_settings, parse_saver_settings

ROOT = Path(__file__).resolve().parent.parent


def test_example_config_loads_without_warnings():
    data = tomllib.loads((ROOT / "config.example.toml").read_text(encoding="utf-8"))
    configs, warnings = build_agent_configs(data)
    review, review_warnings = parse_review_settings(data, set(configs))
    _, saver_warnings = parse_saver_settings(data, set(configs), review)
    assert not warnings and not review_warnings and not saver_warnings


def test_example_config_has_the_exact_cli_presets():
    # The lock-down flags are the safety boundary: the docs must not drift from the code
    configs, _ = build_agent_configs(tomllib.loads((ROOT / "config.example.toml").read_text(encoding="utf-8")))
    default = AgentConfig("x", "x", "oneshot")
    for preset in CLI_PRESETS:
        cfg = configs[preset["id"]]
        for field in ("command", "args", "args_by_version", "prompt_via", "output_flag", "env", "drop_env",
                      "effort_args", "effort_levels", "model_args"):
            assert getattr(cfg, field) == preset.get(field, getattr(default, field)), (preset["id"], field)
        assert cfg.workdir == "temp"


def test_readme_lists_every_subscription_cli():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for preset in CLI_PRESETS:
        assert preset["label"] in readme, preset["label"]


def test_example_triage_block_loads_without_warnings(monkeypatch):
    from ixel_mat.triage import parse_triage_settings
    text = (ROOT / "config.example.toml").read_text(encoding="utf-8")
    block = text[text.index("# [triage]"):text.index("# ── ", text.index("# [triage]"))]
    uncommented = "\n".join(line[2:] for line in block.splitlines() if line.startswith("# "))
    example = tomllib.loads((ROOT / "config.example.toml").read_text(encoding="utf-8"))
    configs, _ = build_agent_configs(example)
    settings, warnings = parse_triage_settings(tomllib.loads(uncommented), configs)
    assert settings.ready and settings.provider == "model" and not warnings
    # …and the TypeSafe variant it describes
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-example")
    typesafe = uncommented.replace('agent = "grok"', 'provider = "typesafe"')
    settings, warnings = parse_triage_settings(tomllib.loads(typesafe), configs)
    assert settings.ready and settings.official and not warnings


def test_example_sound_block_picks_the_service_it_names(monkeypatch):
    from ixel_mat import sound
    text = (ROOT / "config.example.toml").read_text(encoding="utf-8")
    block = text[text.index("# [sound]"):text.index("# ── ", text.index("# [sound]"))]
    example = tomllib.loads("\n".join(line[2:].split("  #")[0] for line in block.splitlines() if line.startswith("# ")))
    assert set(example["sound"]) == {"provider", "openai_model", "groq_model"}
    defaults = {p.name: p.model for p in sound.PROVIDERS.values()}
    assert {p.name: p.model for p in sound.providers(example)} == defaults  # the example shows the defaults
    monkeypatch.setenv("OPENAI_API_KEY", "sk-example")
    monkeypatch.setenv("GROQ_API_KEY", "gsk-example")
    assert sound.pick_provider(example).name == example["sound"]["provider"]


def test_example_pricing_block_loads_without_warnings():
    from ixel_mat.usage import parse_pricing
    text = (ROOT / "config.example.toml").read_text(encoding="utf-8")
    block = text[text.index("# [pricing]"):]
    uncommented = "\n".join(line[2:] for line in block.splitlines() if line.startswith("# "))
    prices, warnings = parse_pricing(tomllib.loads(uncommented))
    assert len(prices) == 2 and not warnings
