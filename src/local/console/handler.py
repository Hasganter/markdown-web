import os
import sys
import time
import json
import psutil
import logging
from typing import List
from src.local import app_globals
from src.local.database import LogDBManager
from src.local.supervisor import ProcessManager
from src.local.external import DependencyManager

# --- Platform-specific non-blocking keypress detection ---
try:
    import msvcrt
    def is_keypress_waiting() -> bool:
        return msvcrt.kbhit()
    def clear_keypress_buffer() -> None:
        # Read all waiting characters to clear the buffer
        while msvcrt.kbhit():
            msvcrt.getch()
except ImportError:
    import select
    import termios
    import tty
    def is_keypress_waiting() -> bool:
        return select.select([sys.stdin], [], [], 0) == ([sys.stdin], [], [])
    def clear_keypress_buffer() -> None:
        # For non-Windows, need to switch to raw mode temporarily to read without Enter
        old_settings = termios.tcgetattr(sys.stdin)
        try:
            tty.setcbreak(sys.stdin.fileno())
            while is_keypress_waiting():
                sys.stdin.read(1)
        finally:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)

log = logging.getLogger(__name__)
current_overrides: dict = {}
process_manager = ProcessManager()


def load_current_overrides() -> None:
    """
    Loads existing globalsuration overrides from the JSON file into a global dict.
    """
    global current_overrides
    if app_globals.OVERRIDES_JSON_PATH.exists():
        try:
            with app_globals.OVERRIDES_JSON_PATH.open('r') as f:
                current_overrides = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            log.warning(f"Could not load overrides from json: {e}")
            current_overrides = {}
    else:
        current_overrides = {}

def _config_show():
    """Displays the current configuration settings and their sources."""
    print("\n--- Current Application Configuration ---")
    print("Settings are shown as: KEY = VALUE (Source: Default/Override)")
    # Reload from disk to show the true current state
    load_current_overrides()
    for key in sorted(app_globals.MODIFIABLE_SETTINGS):
        if not hasattr(app_globals, key):
            continue  # Skip if the setting does not exist in the config object
        value = getattr(app_globals, key)
        source = "Override" if key in current_overrides else "Default"
        print(f"  {key} = {value} (Source: {source})")
    print("---")
    print("Use 'config set <KEY> <VALUE>' to change a setting for the current session.")
    print("Use 'config save' to persist session changes.")
    print("---------------------------------------\n")

def _config_set(args: List[str]):
    """Sets a configuration setting for the current session."""
    global current_overrides
    if len(args) < 2:
        print("Usage: config set <SETTING_NAME> <VALUE>")
        return
    key, value_str = args[0].upper(), " ".join(args[1:])

    if key not in app_globals.MODIFIABLE_SETTINGS:
        print(f"Error: '{key}' is not a modifiable setting or does not exist.")
        return

    if not hasattr(app_globals, key):
        # This check is somewhat redundant due to the one above but is good practice.
        print(f"Error: Setting '{key}' not found in configuration.")
        return

    original_value = getattr(app_globals, key)
    try:
        # Handle boolean conversion gracefully
        if isinstance(original_value, bool):
            new_value = value_str.lower() in ('true', '1', 't', 'yes', 'y')
        else:
            new_value = type(original_value)(value_str)

        current_overrides[key] = new_value
        setattr(app_globals, key, new_value) # Apply change to current session
        print(f"Set '{key}' to '{new_value}'. This change is temporary.")
        print("Use 'config save' to make it permanent (requires restart to apply fully).")
    except (ValueError, TypeError):
        err_msg = (f"Error: Could not convert '{value_str}' to the required type "
                  f"({type(original_value).__name__}).")
        print(err_msg)

def _config_save():
    """Saves the current overrides to the JSON file."""
    global current_overrides
    if not current_overrides:
        print("No temporary overrides to save.")
        return
    app_globals.save_overrides(current_overrides)
    print("Overrides saved to bin/overrides.json.")
    print("A restart is required for changes to take full effect.")

def _config_load():
    """Reloads the overrides from the JSON file."""
    load_current_overrides()
    # To apply, settings object needs to be re-initialized, which means restart.
    print("Reloaded overrides from file. A restart is required to apply them.")

def _config_help():
    """Displays help for the config command."""
    print("\nConfig Command Help:")
    print("  config show                - Display all modifiable settings and their source.")
    print("  config set KEY VALUE       - Temporarily change a setting for the current session.")
    print("  config save                - Save temporary changes to bin/overrides.json.")
    print("  config load                - Reload overrides from the file (requires restart).")
    print("  config help                - Show this help message.")
    print("Use 'check-config' to validate external binary paths.")

def handle_config_command(args: List[str]) -> None:
    """
    Handles all sub-commands for the 'config' command-line interface.

    :param args: A list of string arguments following the 'config' command.
    """
    if not args:
        print("Usage: config <show|set|save|load|help>")
        return

    sub_command = args[0].lower()
    sub_command_map = {
        "show": _config_show(),
        "set": _config_set(args),
        "save": _config_save(),
        "load": _config_load(),
        "help": _config_help()
    }
    if sub_command in sub_command_map:
        sub_command_map[sub_command]()
    else:
        print(f"Unknown config sub-command: '{sub_command}'. Type 'config help' for available commands.")

def handle_recover_command(args: List[str]):
    """Handles the 'recover' command for dependencies."""
    if process_manager.get_pid_info():
        print("\nERROR: Cannot recover dependencies while the application is running.")
        print("Please run the 'stop' command first.\n")
        return
        
    if not args:
        print("Usage: recover <dependency_name>")
        print(f"Available dependencies: {', '.join(app_globals.EXTERNAL_DEPENDENCIES.keys())}")
        return

    dep_key = args[0].lower()
    dep_manager = DependencyManager()
    dep_manager.interactive_recover(dep_key)

def display_status() -> None:
    """Checks and displays the current status of all managed processes, including resource usage."""
    pids = process_manager.get_pid_info()
    if not pids:
        print("\nApplication is STOPPED (No PID file found).\n")
        return

    print("\n--- Application Status ---")
    all_stale = True
    total_cpu = 0.0
    total_mem = 0
    seen_pids = set()

    # Add current process (the console itself)
    current_pid = os.getpid()
    try:
        p = psutil.Process(current_pid)
        cpu = p.cpu_percent(interval=0.1)
        mem = p.memory_info().rss
        total_cpu += cpu
        total_mem += mem
        seen_pids.add(current_pid)
        print(f"  - {p.name() + " (console)":<32} : PID {current_pid:<8} | Status: {p.status().upper()} | CPU: {cpu:.1f}% | MEM: {mem/1024/1024:.1f} MB")
    except Exception:
        pass

    for name, pid in sorted(pids.items()):
        if pid in seen_pids:
            continue  # Already counted
        try:
            if psutil.pid_exists(pid):
                p = psutil.Process(pid)
                cpu = p.cpu_percent(interval=0.1)
                mem = p.memory_info().rss
                print(f"  - {p.name() + ' (' + name + ')':<32} : PID {pid:<8} | Status: {p.status().upper()} | CPU: {cpu:.1f}% | MEM: {mem/1024/1024:.1f} MB")
                total_cpu += cpu
                total_mem += mem
                all_stale = False
            else:
                print(f"  - {name:<25} : PID {pid:<8} | Status: STOPPED (Stale PID)")
        except psutil.NoSuchProcess:
            print(f"  - {name:<25} : PID {pid:<8} | Status: STOPPED (Stale PID)")
        except psutil.AccessDenied:
            print(f"  - {name:<25} : PID {pid:<8} | Status: RUNNING (Access Denied)")
            all_stale = False

    print(f"\nTOTAL CPU: {total_cpu:.1f}%  |  TOTAL MEMORY: {total_mem/1024/1024:.1f} MB")
    print(f"Runtime: {time.strftime('%H:%M:%S', time.gmtime(time.time() - app_globals.start_time))}")

    if all_stale:
        print("\nWARNING: All processes are stopped but a stale PID file exists.")
        print("You should run 'stop' to clean it up before starting again.")
    print("-" * 26 + "\n")

def handle_logs_command() -> None:
    """
    Handles the 'logs' command, providing a blocking, interactive log tail.
    """
    if not process_manager.get_pid_info():
        print("Application is not running. Cannot tail logs.")
        return

    # Instantiate the manager to interact with the log database.
    log_db = LogDBManager(app_globals.LOG_DB_PATH)

    print(f"\n--- Displaying last {app_globals.LOG_HISTORY_COUNT} log entries ---")
    last_ts = 0.0
    try:
        # Use the manager method to fetch logs.
        for log in log_db.fetch_last_entries(app_globals.LOG_HISTORY_COUNT, app_globals.VERBOSE_LOGGING):
            print(log.message)
            last_ts = max(last_ts, log.timestamp)
    except Exception as e:
        log.error(f"Failed to fetch initial log history: {e}")
        return

    print("\n--- Now tailing new log entries (Press any key to stop) ---\n")

    try:
        while not is_keypress_waiting():
            # Use the manager method to listen for updates.
            new_logs, last_ts = log_db.listen_for_updates(last_ts)
            for log in new_logs:
                if log.level == "DEBUG" and not app_globals.VERBOSE_LOGGING:
                    continue
                print(log.message)
            time.sleep(1) # Poll interval
        clear_keypress_buffer()
        print("\n--- Log tailing stopped. Returning to console. ---")
    except (KeyboardInterrupt, SystemExit):
        print("\n--- Log tailing interrupted. Returning to console. ---")
        raise
    except Exception as e:
        log.error(f"An error occurred during log tailing: {e}", exc_info=True)

def toggle_verbose_logging() -> None:
    """Toggles verbose (DEBUG level) logging for the console handler."""
    app_globals.VERBOSE_LOGGING = not app_globals.VERBOSE_LOGGING
    new_level = logging.DEBUG if app_globals.VERBOSE_LOGGING else logging.INFO

    # Reglobalsure the console handler's level directly
    root_logger = logging.getLogger()
    found_handler = False
    for handler in root_logger.handlers:
        if isinstance(handler, logging.StreamHandler):
            handler.setLevel(new_level)
            found_handler = True
            break
    
    status = "ON" if app_globals.VERBOSE_LOGGING else "OFF"
    if found_handler:
        print(f"Verbose console logging is now {status}.")
        log.debug("Debug logging test: This message should only appear when verbose is ON.")
    else:
        print("Could not find console handler to modify level.")

def print_help():
    """Prints the main help text for the console."""
    print("\nAvailable commands:")
    print("  start                  - Start all application services.")
    print("  stop                   - Stop all application services gracefully.")
    print("  restart                - Stop and then restart all services.")
    print("  status                 - Show the current status of all services.")
    print("  logs                   - View historical logs and tail new logs in real-time.")
    print("  export-logs [filename] - Export application logs to a styled Excel file.")
    print("  check-config           - Validate paths to external binaries (Nginx, FFmpeg).")
    print("  config <cmd>           - Manage globalsuration. Use 'app_globals help' for more details.")
    print("  recover <name>         - Recover an archived version of a dependency (server must be stopped).")
    print("  verbose                - Toggle detailed DEBUG log output in the console.")
    print("  exit                   - Exit the management console.")
    print()
