"""Shared test setup for pi_legacy unit tests.

Import this module at the top of any test file before importing pi_legacy
packages. It ensures the project root is on sys.path and mocks the AstrBot
public API so tests can run without the full AstrBot runtime.
"""

import sys
import types
from pathlib import Path

# Ensure the project root is on sys.path so pi_legacy can be imported.
project_root = Path(__file__).resolve().parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

# Mock AstrBot public API so pi_legacy modules can be imported standalone.
_NOOP_LOGGER = types.SimpleNamespace(
    info=lambda *args, **kwargs: None,
    debug=lambda *args, **kwargs: None,
    warning=lambda *args, **kwargs: None,
    error=lambda *args, **kwargs: None,
)

if "astrbot" not in sys.modules:
    _astrbot = types.ModuleType("astrbot")
    _astrbot_api = types.ModuleType("astrbot.api")
    _astrbot_api.logger = _NOOP_LOGGER
    _astrbot.api = _astrbot_api
    sys.modules["astrbot"] = _astrbot
    sys.modules["astrbot.api"] = _astrbot_api
else:
    # Ensure the logger is a no-op even if another import already populated it.
    sys.modules["astrbot.api"].logger = _NOOP_LOGGER
