"""
This is a minimal entry point script for the content converter process.

Its sole responsibility is to instantiate the content processing loop with
proper signal handling and multiprocessing setup. This clean separation
avoids circular dependencies and ensures the content converter process
has a simple, dedicated startup routine.
"""
import setproctitle
setproctitle.setproctitle("MDWeb - ContentConverter")

import signal
from multiprocessing import Lock, Event
from src.log.setup import setup_logging
from src.converter import content_converter_process_loop

# Global reference for signal handler
stop_event = None


def handle_shutdown_signal(signum, frame):
    """Handle shutdown signals gracefully."""
    import logging
    log = logging.getLogger(__name__)
    setup_logging()
    log.debug(f"Signal {signum} received, shutting down content converter.")
    if stop_event:
        stop_event.set()

if __name__ == "__main__":
    
    # Initialize multiprocessing primitives
    db_lock = Lock()
    stop_event = Event()
    
    # Set up signal handlers
    signal.signal(signal.SIGTERM, handle_shutdown_signal)
    signal.signal(signal.SIGINT, handle_shutdown_signal)
    
    # Start the content converter process loop
    content_converter_process_loop(stop_event, db_lock)
