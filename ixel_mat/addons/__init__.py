"""
Add-ons: other people's skills, MCP servers and command-line tools Ixel can use, each off until you switch it on.

paths: where add-ons, their state and the diagrams live.
installer: downloading a pinned GitHub release and unpacking it safely, checked against the card's tree hash.
cards: the card format in docs/addon-cards.md.
registry: every card and its on/off and approval state.
diagram: Diagram Design in Ask.
"""
from ixel_mat.addons import paths  # noqa: F401
from ixel_mat.addons.installer import *  # noqa: F401,F403
from ixel_mat.addons.cards import *  # noqa: F401,F403
from ixel_mat.addons.registry import *  # noqa: F401,F403
from ixel_mat.addons.diagram import *  # noqa: F401,F403
