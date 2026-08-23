"""Pi legacy compatibility package for AstrBot interactive sessions.

Provides a local Pi integration for administrator-only interactive sessions.
"""

from .connection import PiConnection, PiError
from .manager import PiConnectionManager
from .models import SessionInfo, UIRequest

__all__ = ["PiConnection", "PiConnectionManager", "PiError", "SessionInfo", "UIRequest"]
