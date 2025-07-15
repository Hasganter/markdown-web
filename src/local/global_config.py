import os
import json
import logging
import setproctitle
from pathlib import Path
from typing import Dict, Any
import src.settings as default_settings
from src.local.config_client import fetch_config_from_supervisor

log = logging.getLogger(__name__)


class GlobalSync:
    """
    A singleton class that houses all application configuration.

    Its behavior is process-aware:
    - SUPERVISOR: Loads config from files and serves it via an API.
    - MANAGED CHILD: Fetches authoritative config from the Supervisor's API.
    - CLIENT/CONSOLE: Loads a minimal bootstrap config from files and acts as a
      client to the Supervisor's API for dynamic operations.
    """

    def __init__(self) -> None:
        """Initializes the settings object by loading from the correct source."""
        self._config: Dict[str, Any] = {}
        self._load_defaults()

        # Determine the current process context
        proc_title = setproctitle.getproctitle()

        if "MDWeb - Supervisor" in proc_title:
            log.debug("Config context: Supervisor. Loading from files.")
            self._load_overrides_from_file()
        elif proc_title in default_settings.MANAGED_PROCESS_TITLES:
            log.debug(f"Config context: Managed Child ('{proc_title}'). Fetching from API.")
            self._fetch_and_apply_supervisor_config()
        else:
            log.debug("Config context: Client/Console. Loading bootstrap config.")

        # Dynamically set attributes on the instance from the final config dict
        for key, value in self._config.items():
            if not hasattr(self, key):
                setattr(self, key, value)

        # Initialize global variables not part of the main config dict
        self.stop_log_listener = default_settings.stop_log_listener
        self.start_time = None

    def get(self, item: str, default: Any = None) -> Any:
        """Provides dictionary-like access to settings with a default value."""
        return self._config.get(item, default)

    def __getattr__(self, name: str) -> Any:
        """Allows attribute access to settings, raising an AttributeError if not found."""
        if name in self._config:
            return self._config[name]
        raise AttributeError(f"'{self.__class__.__name__}' object has no attribute '{name}'")

    def _load_defaults(self) -> None:
        """Loads all uppercase attributes from settings.py as the baseline."""
        for key in dir(default_settings):
            if key.isupper():
                self._config[key] = getattr(default_settings, key)

    def _load_overrides_from_file(self) -> None:
        """(Supervisor Only) Loads overrides from the JSON file."""
        overrides_path = Path(self._config["OVERRIDES_JSON_PATH"])
        if not overrides_path.exists():
            return

        try:
            with overrides_path.open('r') as f:
                overrides = json.load(f)
            log.info(f"Loading runtime config overrides from {overrides_path}")
            # Merge the overrides into our config dictionary
            self._config.update(overrides)
        except (json.JSONDecodeError, IOError) as e:
            log.error(f"Failed to load or parse overrides file: {e}")

    def _fetch_and_apply_supervisor_config(self) -> None:
        """(Managed Child Processes Only) Fetches config from Supervisor."""
        supervisor_config = fetch_config_from_supervisor(
            host=self._config["CONFIG_API_HOST"],
            port=self._config["CONFIG_API_PORT"]
        )
        if supervisor_config:
            self._config.update(supervisor_config)
            self._coerce_path_objects()
        else:
            log.critical("Managed process could not retrieve configuration. This process will likely be unstable.")

    def _coerce_path_objects(self):
        """Converts settings that should be Path objects from string to Path."""
        for key, value in self._config.items():
            default_value = getattr(default_settings, key, None)
            if isinstance(default_value, Path) and isinstance(value, str):
                self._config[key] = Path(value)

    def get_all_settings(self) -> Dict[str, Any]:
        """Returns the entire configuration dictionary."""
        return self._config

# A singleton instance to be imported by other modules
app_globals = GlobalSync()
