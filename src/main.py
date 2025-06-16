import json
import logging
import sys
import threading
import time
from pathlib import Path
from typing import List

# Set up a basic root logger for early messages before full setup.
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)-8s - [console] - %(message)s',
    stream=sys.stdout
)
logger = logging.getLogger("console")

# --- Platform-specific non-blocking keypress detection ---
try:
    import msvcrt
    def is_keypress_waiting() -> bool:
        return msvcrt.kbhit()
    def clear_keypress_buffer() -> None:
        msvcrt.getch()
except ImportError:
    import select
    def is_keypress_waiting() -> bool:
        return select.select([sys.stdin], [], [], 0) == ([sys.stdin], [], [])
    def clear_keypress_buffer() -> None:
        sys.stdin.read(1)

# --- Defer other imports after initial logging setup ---
import psutil

from src.local.config import effective_settings as config
from src.local.database import LogDBManager
from src.local.manager import ProcessManager
from src.log.export import export_logs_to_excel

# --- Global State ---
stop_log_listener = threading.Event()
current_overrides: dict = {}
process_manager = ProcessManager()
CONSOLE_LOCK = threading.Lock()


class ListHandler(logging.Handler):
    """A logging handler that collects log records into a list."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.records: List[str] = []
    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(self.format(record))


def load_current_overrides() -> None:
    """
    Loads existing configuration overrides from the JSON file into a global dict.
    """
    global current_overrides
    if config.OVERRIDES_JSON_PATH.exists():
        try:
            with config.OVERRIDES_JSON_PATH.open('r') as f:
                current_overrides = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"Could not load overrides from json: {e}")
            current_overrides = {}
    else:
        current_overrides = {}


def handle_config_command(args: List[str]) -> None:
    """
    Handles all sub-commands for the 'config' command-line interface.

    :param args: A list of string arguments following the 'config' command.
    """
    global current_overrides
    if not args:
        print("Usage: config <show|set|save|load|help>")
        return

    sub_command = args[0].lower()

    if sub_command == "show":
        print("\n--- Current Application Configuration ---")
        print("Settings are shown as: KEY = VALUE (Source: Default/Override)")
        # Reload from disk to show the true current state
        load_current_overrides()
        for key in sorted(config.MODIFIABLE_SETTINGS):
            if hasattr(config, key):
                value = getattr(config, key)
                source = "Override" if key in current_overrides else "Default"
                print(f"  {key} = {value} (Source: {source})")
        print("---")
        print("Use 'config set <KEY> <VALUE>' to change a setting for the current session.")
        print("Use 'config save' to persist session changes.")
        print("---------------------------------------\n")

    elif sub_command == "set":
        if len(args) < 3:
            print("Usage: config set <SETTING_NAME> <VALUE>")
            return
        key, value_str = args[1].upper(), " ".join(args[2:])

        if key not in config.MODIFIABLE_SETTINGS:
            print(f"Error: '{key}' is not a modifiable setting or does not exist.")
            return
        if not hasattr(config, key):
            # This check is somewhat redundant due to the one above but is good practice.
            print(f"Error: Setting '{key}' not found in configuration.")
            return

        original_value = getattr(config, key)
        try:
            # Handle boolean conversion gracefully
            if isinstance(original_value, bool):
                new_value = value_str.lower() in ('true', '1', 't', 'yes', 'y')
            else:
                new_value = type(original_value)(value_str)

            current_overrides[key] = new_value
            setattr(config, key, new_value) # Apply change to current session
            print(f"Set '{key}' to '{new_value}'. This change is temporary.")
            print("Use 'config save' to make it permanent (requires restart to apply fully).")
        except (ValueError, TypeError):
            err_msg = (f"Error: Could not convert '{value_str}' to the required type "
                       f"({type(original_value).__name__}).")
            print(err_msg)

    elif sub_command == "save":
        if not current_overrides:
            print("No temporary overrides to save.")
            return
        config.save_overrides(current_overrides)
        print("Overrides saved to bin/overrides.json.")
        print("A restart is required for changes to take full effect.")

    elif sub_command == "load":
        load_current_overrides()
        # To apply, settings object needs to be re-initialized, which means restart.
        print("Reloaded overrides from file. A restart is required to apply them.")

    else:
        print("\nConfig Command Help:")
        print("  config show                - Display all modifiable settings and their source.")
        print("  config set KEY VALUE       - Temporarily change a setting for the current session.")
        print("  config save                - Save temporary changes to bin/overrides.json.")
        print("  config load                - Reload overrides from the file (requires restart).")
        print("  config help                - Show this help message.")
        print("Use 'check-config' to validate external binary paths.")


def display_status() -> None:
    """Checks and displays the current status of all managed processes."""
    pids = process_manager.get_pid_info()
    if not pids:
        print("\nApplication is STOPPED (No PID file found).\n")
        return

    print("\n--- Application Status ---")
    all_stale = True
    for name, pid in sorted(pids.items()):
        try:
            if psutil.pid_exists(pid):
                p = psutil.Process(pid)
                # For more detail, you can add p.memory_info(), p.cpu_percent()
                print(f"  - {name:<20} : PID {pid:<8} | Status: {p.status().upper()}")
                all_stale = False
            else:
                print(f"  - {name:<20} : PID {pid:<8} | Status: STOPPED (Stale PID)")
        except psutil.NoSuchProcess:
            print(f"  - {name:<20} : PID {pid:<8} | Status: STOPPED (Stale PID)")
        except psutil.AccessDenied:
            print(f"  - {name:<20} : PID {pid:<8} | Status: RUNNING (Access Denied)")
            all_stale = False


    if all_stale:
        print("\nWARNING: All processes are stopped but a stale PID file exists.")
        print("You should run 'stop' to clean it up before starting again.")
    print("-" * 28 + "\n")


def handle_logs_command() -> None:
    """
    Handles the 'logs' command, providing a blocking, interactive log tail.
    """
    if not process_manager.get_pid_info():
        print("Application is not running. Cannot tail logs.")
        return

    # Instantiate the manager to interact with the log database.
    log_db = LogDBManager(config.LOG_DB_PATH)

    print(f"\n--- Displaying last {config.LOG_HISTORY_COUNT} log entries ---")
    last_ts = 0.0
    try:
        # Use the manager method to fetch logs.
        for log in log_db.fetch_last_entries(config.LOG_HISTORY_COUNT):
            print(log.message)
            last_ts = max(last_ts, log.timestamp)
    except Exception as e:
        logger.error(f"Failed to fetch initial log history: {e}")
        return

    print("\n--- Now tailing new log entries (Press any key to stop) ---\n")

    try:
        while not is_keypress_waiting():
            # Use the manager method to listen for updates.
            new_logs, last_ts = log_db.listen_for_updates(last_ts)
            for log in new_logs:
                print(log.message)
            time.sleep(1) # Poll interval
        clear_keypress_buffer()
        print("\n--- Log tailing stopped. Returning to console. ---")
    except (KeyboardInterrupt, SystemExit):
        print("\n--- Log tailing interrupted. Returning to console. ---")
    except Exception as e:
        logger.error(f"An error occurred during log tailing: {e}", exc_info=True)


def execute_command(command: str, args: List[str]) -> bool:
    """
    Executes a single command from the user.

    :param command: The main command string (e.g., 'start', 'config').
    :param args: A list of arguments for the command.
    :return bool: True if the console should exit, False otherwise.
    """
    # Command mapping to avoid a giant if/elif block
    command_map = {
        "start": lambda: process_manager.start_all(),
        "stop": lambda: process_manager.stop_all(),
        "shutdown": lambda: process_manager.stop_all(), # Foolproof alias
        "status": display_status,
        "check-config": process_manager.check_configuration,
        "config": lambda: handle_config_command(args),
        "logs": handle_logs_command,
        "help": print_help,
        "exit": lambda: True
    }

    should_exit = False
    if command in command_map:
        result = command_map[command]()
        if command == "exit" and result is True:
            should_exit = True

    elif command == "restart":
        print("Stopping services...")
        process_manager.stop_all()
        time.sleep(2)  # Give services time to shut down completely.
        print("Starting services...")
        process_manager.start_all()

    elif command == "export-logs":
        output_file = Path(args[0] if args else "logs_export.xlsx")
        print(f"Exporting logs to '{output_file}'...")
        export_logs_to_excel(config.LOG_DB_PATH, output_file)

    else:
        print(f"Unknown command: '{command}'. Type 'help' for a list of commands.")

    return should_exit

def print_help():
    """Prints the main help text for the console."""
    print("\nAvailable commands:")
    print("  start                  - Start all application services.")
    print("  stop                   - Stop all application services.")
    print("  restart                - Stop and then restart all services.")
    print("  status                 - Show the current status of all services.")
    print("  logs                   - View historical logs and tail new logs in real-time.")
    print("  export-logs [filename] - Export application logs to a styled Excel file.")
    print("  check-config           - Validate paths to external binaries (Nginx, FFmpeg).")
    print("  config <cmd>           - Manage configuration. Use 'config help' for more details.")
    print("  exit                   - Exit the management console.")
    print()


def main() -> None:
    """The main entry point for the console application."""
    load_current_overrides()

    # Non-interactive mode for one-off commands
    if len(sys.argv) > 1:
        command, args = sys.argv[1].lower(), sys.argv[2:]
        execute_command(command, args)
        return

    # Interactive mode
    print("--- Application Management Console ---")
    print("Type 'help' for a list of commands.")

    while True:
        try:
            with CONSOLE_LOCK:
                command_line_str = input("> ")
                if not command_line_str:
                    continue
                command_line = command_line_str.strip().split()

            command, args = command_line[0].lower(), command_line[1:]

            if execute_command(command, args):
                break

        except KeyboardInterrupt:
            with CONSOLE_LOCK:
                print("\nExiting console due to KeyboardInterrupt.")
                break
        except Exception as e:
            with CONSOLE_LOCK:
                logger.error(f"An unexpected error occurred in the console: {e}", exc_info=True)


if __name__ == "__main__":
    main()
