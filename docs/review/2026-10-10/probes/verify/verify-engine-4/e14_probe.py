from ixel_mat.runtime import settings_from
AG = {"agents": {
    "claude": {"type": "http", "url": "https://api.anthropic.com/v1/messages", "token": "x", "model": "m"},
    "gpt": {"type": "http", "url": "https://api.openai.com/v1/chat/completions", "token": "x", "model": "m"},
    "gemini": {"type": "http", "url": "https://example.com/v1/chat/completions", "token": "x", "model": "m"},
}}
def show(title, extra):
    s = settings_from({**AG, **extra})
    print(f"--- {title}")
    print("  review.agents =", s.review.agents, "| panel =", list(s.panel_configs()))
    print("  saver.drafters =", s.saver.drafters, "| verifier =", s.verifier, "| saver_configs =", list(s.saver_configs()))
    print("  warnings =", s.warnings)
show("agents all unknown", {"review": {"agents": ["typo", "gone"]}})
show("agents partly unknown", {"review": {"agents": ["claude", "typo"]}})
show("agents = []", {"review": {"agents": []}})
show("agents = [claude]", {"review": {"agents": ["claude"]}})
show("drafters = [verifier only]", {"saver": {"verifier": "gpt", "drafters": ["gpt"]}})
show("drafters = [claude]", {"saver": {"verifier": "gpt", "drafters": ["claude"]}})
show("drafters all unknown", {"saver": {"verifier": "gpt", "drafters": ["nope"]}})
show("drafters = [] explicit", {"saver": {"verifier": "gpt", "drafters": []}})
show("moderator=gpt, drafters=[gpt] (verifier defaults to moderator)", {"review": {"moderator": "gpt"}, "saver": {"drafters": ["gpt"]}})
