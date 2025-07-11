import time
import logging
from typing import List
from pathlib import Path
from src.local import app_globals
from src.local.supervisor import ProcessManager
from src.log.export import export_logs_to_excel
from src.local.supervisor.config_utils import check_configuration
from src.local.console.handler import display_status, handle_config_command, handle_logs_command, toggle_verbose_logging, handle_recover_command, print_help

log = logging.getLogger(__name__)
process_manager = ProcessManager()


def execute_command(command: str, args: List[str]) -> bool:
    """
    Executes a single command from the user.

    :param command: The main command string (e.g., 'start', 'config').
    :param args: A list of arguments for the command.
    :return bool: True if the console should exit, False otherwise.
    """
    log.debug(f"Executing command: {command}, args: {args}")
    command_map = {
        "start": lambda: process_manager.start_all(app_globals.VERBOSE_LOGGING),
        "stop": lambda: process_manager.stop_all(),
        "shutdown": lambda: process_manager.stop_all(), # Foolproof alias
        "status": display_status,
        "check-config": check_configuration,
        "config": lambda: handle_config_command(args),
        "logs": handle_logs_command,
        "verbose": toggle_verbose_logging,
        "recover": lambda: handle_recover_command(args),
        "help": print_help,
        "exit": lambda: True
    }

    should_exit = False
    if command in command_map:
        result = command_map[command]()
        if command == "exit" and result is True:
            should_exit = True

    elif command == "restart":
        log.info("Stopping services...")
        process_manager.stop_all()
        time.sleep(5)
        log.info("Starting services...")
        process_manager.start_all(globals.VERBOSE_LOGGING)

    elif command == "export-logs":
        output_file = Path(args[0] if args else app_globals.LOGS_DIR / "logs_export.xlsx")
        log.info(f"Exporting logs to '{output_file}'...")
        export_logs_to_excel(app_globals.LOG_DB_PATH, output_file)

    else:
        log.info(f"Unknown command: '{command}'. Type 'help' for a list of commands.")

    return should_exit
