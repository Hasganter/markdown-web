import sys
import logging
import threading

# Basic console logger for messages BEFORE full setup is complete.
# This logger will be replaced by the full setup later.
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)-8s - [console] - %(message)s',
    stream=sys.stdout
)
log = logging.getLogger("console")

import src.local.console as console
from src.log.setup import setup_logging
from src.local.supervisor import ProcessManager

# --- Global State ---
VERBOSE_LOGGING = False
CONSOLE_LOCK = threading.Lock()
process_manager = ProcessManager()


def main() -> None:
    """The main entry point for the console application."""

    # The very first thing we do is set up logging for the console.
    # The 'start' command will call this again for the subprocesses.
    setup_logging(logging.INFO)

    # Non-interactive mode for one-off commands
    if len(sys.argv) > 1:
        command, args = sys.argv[1].lower(), sys.argv[2:]
        # Check for verbose flag in non-interactive mode
        if "--verbose" in args:
            global VERBOSE_LOGGING
            VERBOSE_LOGGING = True
            console.toggle_verbose_logging()
            args.remove("--verbose")
        
        console.execute_command(command, args)
        return

    # Interactive mode
    print("--- Application Management Console ---")
    print("Type 'help' for a list of commands.")

    # Check if the application is currently running
    with CONSOLE_LOCK:
        if process_manager.get_pid_info():
            status = "Running"
            log.debug("Console startup - Application is currently running.")
        else:
            status = "Stopped"
            log.debug("Console startup - Application is currently stopped.")

    print(f"Application is currently {status}.")
    while True:
        try:
            # The input prompt must be outside the lock to not block background threads
            command_line_str = input("> ")
            with CONSOLE_LOCK:
                if not command_line_str:
                    continue
                command_line = command_line_str.strip().split()

                command, args = command_line[0].lower(), command_line[1:]

                log.debug(f"Received command: {command}, args: {args}")

                if console.execute_command(command, args):
                    break

        except KeyboardInterrupt:
            with CONSOLE_LOCK:
                log.warning("\nExiting console due to KeyboardInterrupt.")
                break
        except Exception as e:
            with CONSOLE_LOCK:
                log.error(f"An unexpected error occurred in the console: {e}", exc_info=True)

if __name__ == "__main__":
    main()
    print("Exiting console application. See you next time!")
