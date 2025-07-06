import json
import logging
from pathlib import Path
from typing import Dict, Any

import src.settings as default_settings

log = logging.getLogger(__name__)


class MergedSettings:
    """
    A singleton class that merges default settings with JSON/env overrides.

    This class provides a unified, attribute-based access point for all
    application configuration. It follows a clear precedence:
    1. Base values from `settings.py`.
    2. Overrides from `.env` file (handled by `python-dotenv` in settings.py).
    3. Overrides from `overrides.json` for settings in `MODIFIABLE_SETTINGS`.
    """

    def __init__(self) -> None:
        """Initializes the settings object by loading defaults and overrides."""
        # Path to the overrides file
        self.OVERRIDES_JSON_PATH: Path = default_settings.OVERRIDES_JSON_PATH

        self._load_defaults()
        self._load_overrides()

    def _load_defaults(self) -> None:
        """
        Loads all uppercase attributes from the settings.py module as defaults.
        """
        for key in dir(default_settings):
            if key.isupper():
                setattr(self, key, getattr(default_settings, key))

    def _load_overrides(self) -> None:
        """
        Loads and applies settings from the `overrides.json` file.

        It will only apply overrides for keys that are explicitly listed in
        the `MODIFIABLE_SETTINGS` set in `settings.py`.
        """
        if not self.OVERRIDES_JSON_PATH.exists():
            return

        try:
            with self.OVERRIDES_JSON_PATH.open('r') as f:
                overrides = json.load(f)

            log.info(f"Loading runtime configuration overrides from {self.OVERRIDES_JSON_PATH}")
            for key, value in overrides.items():
                if hasattr(self, key):
                    # Security: Only allow overriding whitelisted settings.
                    if key not in self.MODIFIABLE_SETTINGS:
                        log.warning(
                            f"Attempted to override non-modifiable setting '{key}'. Ignoring."
                        )
                        continue

                    # Coerce path strings back to Path objects if necessary
                    original_value = getattr(self, key)
                    if isinstance(original_value, Path):
                        setattr(self, key, Path(value))
                    else:
                        setattr(self, key, value)
                    log.debug(f"Overridden setting: {key} = {value}")
                else:
                    log.warning(f"Override setting '{key}' not found in default settings. Ignoring.")
        except (json.JSONDecodeError, IOError) as e:
            log.error(
                f"Failed to load or parse overrides file '{self.OVERRIDES_JSON_PATH}': {e}"
            )

    def save_overrides(self, overrides_to_save: Dict[str, Any]) -> None:
        """
        Saves the provided dictionary of settings to the overrides JSON file.

        This method defensively filters the dictionary to ensure only keys
        present in `MODIFIABLE_SETTINGS` are persisted.

        :param overrides_to_save: A dictionary of settings to persist.
        """
        # Filter to only save keys that are actually modifiable.
        filtered_overrides = {
            key: value
            for key, value in overrides_to_save.items()
            if key in self.MODIFIABLE_SETTINGS
        }

        if not filtered_overrides:
            log.warning("No modifiable settings provided to save.")
            return

        try:
            with self.OVERRIDES_JSON_PATH.open('w') as f:
                json.dump(filtered_overrides, f, indent=4)
            log.info(
                f"Configuration overrides saved to {self.OVERRIDES_JSON_PATH}"
            )
        except IOError as e:
            log.error(
                f"Failed to write to overrides file '{self.OVERRIDES_JSON_PATH}': {e}"
            )

# Create a singleton instance to be imported by other modules
effective_settings = MergedSettings()
