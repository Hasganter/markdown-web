"""
This is a minimal entry point script for the supervisor process.

Its sole responsibility is to instantiate the ProcessManager and start the
supervision loop. This clean separation avoids circular dependencies and
ensures the supervisor process has a simple, dedicated startup routine.
"""
import setproctitle
from src.local.supervisor import ProcessManager


if __name__ == "__main__":
    setproctitle.setproctitle("MDWeb - Supervisor")
    manager = ProcessManager()
    manager.supervision_loop()
