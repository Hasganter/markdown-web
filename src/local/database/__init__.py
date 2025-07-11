"""
This module initializes the local database management system.
It imports the necessary database managers for logs and content.
"""

from .log import LogDBManager
from .content import ContentDBManager

__all__ = ["LogDBManager", "ContentDBManager"]