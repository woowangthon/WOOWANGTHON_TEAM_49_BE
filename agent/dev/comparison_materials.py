"""Development compatibility import; shared validation lives in agent.services."""

from agent.services import comparison_materials as _implementation

globals().update({name: value for name, value in vars(_implementation).items() if not name.startswith("__")})
