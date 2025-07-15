import os
import sys
import time
import psutil
import logging
import requests
from typing import List
from src.local import app_globals
from src.local.database import LogDBManager
from src.local.supervisor import ProcessManager
from src.local.external import DependencyManager
from src.local.config_client import post_config_to_supervisor

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
process_manager = ProcessManager()


def _config_show():
    """
    Displays the current configuration settings. It fetches live data from the
    Supervisor if running, otherwise it shows the local bootstrap config.
    """
    print("\n--- Current Application Configuration ---")
    
    config_source = "Local Bootstrap"
    effective_config = {key: app_globals.get(key) for key in app_globals.MODIFIABLE_SETTINGS}

    # If the app is running, try to get live config from the supervisor.
    if process_manager.get_pid_info():
        try:
            url = f"http://{app_globals.CONFIG_API_HOST}:{app_globals.CONFIG_API_PORT}/config"
            response = requests.get(url, timeout=2)
            response.raise_for_status()
            live_config = response.json()
            
            # Update our view with the live data for modifiable settings
            for key in app_globals.MODIFIABLE_SETTINGS:
                if key in live_config:
                    effective_config[key] = live_config[key]
            config_source = f"Live from Supervisor (PID: {process_manager.get_pid_info().get('supervisor')})"
        except requests.RequestException:
            config_source = "Local Bootstrap (Supervisor API unreachable)"
            
    print(f"(Source: {config_source})")
    
    for key in sorted(app_globals.MODIFIABLE_SETTINGS):
        value = effective_config.get(key, 'N/A')
        print(f"  {key} = {value}")
        
    print("---")
    print("Use 'config set <KEY> <VALUE>' to change a setting (requires app to be running).")
    print("A restart is required for changes to apply to all services.")
    print("---------------------------------------\n")

def _config_set(args: List[str]):
    """Sets a configuration setting by calling the Supervisor's API."""
    if not process_manager.get_pid_info():
        print("\nERROR: Cannot change configuration while the application is stopped.")
        print("Please run the 'start' command first.\n")
        return

    if len(args) < 2:
        print("Usage: config set <SETTING_NAME> <VALUE>")
        return
        
    key, value_str = args[0].upper(), " ".join(args[1:])

    if key not in app_globals.MODIFIABLE_SETTINGS:
        print(f"Error: '{key}' is not a modifiable setting.")
        return

    # Send the update to the supervisor
    success = post_config_to_supervisor(
        host=app_globals.CONFIG_API_HOST,
        port=app_globals.CONFIG_API_PORT,
        key=key,
        value=value_str
    )
    if success:
        print(f"Configuration update for '{key}' sent to Supervisor.")
        print("A restart ('restart' command) is required for the change to take full effect across all services.")
    else:
        print(f"Failed to update configuration for '{key}'. Check logs for details.")

def _config_help():
    """Displays help for the config command."""
    print("\nConfig Command Help:")
    print("  config show                - Display all modifiable settings.")
    print("  config set KEY VALUE       - Change a setting. Requires a restart to apply fully.")
    print("  config help                - Show this help message.")
    print("Use 'check-config' to validate external binary paths.")

def handle_config_command(args: List[str]) -> None:
    """
    Handles all sub-commands for the 'config' command-line interface.

    :param args: A list of string arguments following the 'config' command.
    """
    if not args:
        sub_command = "show" # Default to showing config
    else:
        sub_command = args[0].lower()

    if sub_command == "show":
        _config_show()
    elif sub_command == "set":
        _config_set(args[1:])
    elif sub_command == "help":
        _config_help()
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
    if app_globals.start_time:
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
        for log_entry in log_db.fetch_last_entries(app_globals.LOG_HISTORY_COUNT, app_globals.VERBOSE_LOGGING):
            print(log_entry.message)
            last_ts = max(last_ts, log_entry.timestamp)
    except Exception as e:
        log.error(f"Failed to fetch initial log history: {e}")
        return

    print("\n--- Now tailing new log entries (Press any key to stop) ---\n")

    try:
        while not is_keypress_waiting():
            # Use the manager method to listen for updates.
            new_logs, last_ts = log_db.listen_for_updates(last_ts)
            for log_entry in new_logs:
                if log_entry.level == "DEBUG" and not app_globals.VERBOSE_LOGGING:
                    continue
                print(log_entry.message)
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

    # Reconfigure the console handler's level directly
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
    print("  config <cmd>           - Manage configuration. Use 'config help' for more details.")
    print("  recover <name>         - Recover an archived version of a dependency (server must be stopped).")
    print("  verbose                - Toggle detailed DEBUG log output in the console.")
    print("  exit                   - Exit the management console.")
    print()
