import asyncio, sys
sys.path.insert(0, "/home/user/Ixel-MAT")
from ixel_mat import mcp_server as m
from ixel_mat.runtime import load_settings, choose_mode
async def go():
    s = load_settings()
    print("warnings:", s.warnings)
    print("default_mode:", s.default_mode, "| review.mode:", s.review.mode, "| panel:", list(s.panel_configs()), "| saver:", list(s.saver_configs()))
    async with m._default_panel(None) as (settings, agents, problems):
        print("agents connected by _default_panel(None):", [a.name for a in agents], problems)
        mode, decision = await choose_mode(settings, None, "what is 2+2")
        print("choose_mode(None) ->", mode, "| configs_for(mode):", list(settings.configs_for(mode)))
asyncio.run(go())
