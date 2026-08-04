"""The one place the toolkit's version is written down.

``pyproject.toml`` reads this attribute rather than repeating the number, so a
release cannot end up with the packaging metadata and the install manifest
disagreeing about what is installed.

Bump this when deployed artifacts change in a way another machine needs to pick
up: ``templates/``, ``skills/``, ``extensions/``, or the layout of anything
setup writes into ``~/.copilot`` or ``~/.operator``. When a bump requires
rewriting state that is already on disk, add a matching ``upgrade_vX_Y_Z_to_...``
function in ``install_manifest.py``.
"""

__version__ = "1.2.10"
