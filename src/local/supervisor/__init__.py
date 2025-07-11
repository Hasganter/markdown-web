"""
The Supervisor package.
Manages the lifecycle of the application's subprocesses.

This package contains the central ProcessManager class and its helper modules,
which together handle the starting, stopping, supervising, and configuration
of all application components.
"""
from .supervisor import ProcessManager

__all__ = ['ProcessManager']