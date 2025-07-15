import json
import time
import logging
import requests
from typing import Dict, Any, Optional

log = logging.getLogger(__name__)


def fetch_config_from_supervisor(host: str, port: int, retries: int = 5, delay: float = 0.5) -> Optional[Dict[str, Any]]:
    """
    Fetches the application configuration from the Supervisor's internal API.

    This function is called by child processes upon startup to ensure they
    receive the authoritative configuration from the single source of truth.

    :param host: The host of the Supervisor's config API.
    :param port: The port of the Supervisor's config API.
    :param retries: Number of times to retry fetching if the API isn't ready.
    :param delay: Delay in seconds between retries.
    :return: A dictionary containing the configuration, or None on failure.
    """
    url = f"http://{host}:{port}/config"
    for attempt in range(retries):
        try:
            response = requests.get(url, timeout=2)
            response.raise_for_status()
            config_data = response.json()
            log.info(f"Successfully fetched configuration from Supervisor API at '{url}'.")
            return config_data
        except requests.exceptions.RequestException as e:
            log.debug(
                f"Could not connect to Supervisor config API (attempt {attempt + 1}/{retries}): {e}. "
                f"Retrying in {delay}s..."
            )
            time.sleep(delay)
        except json.JSONDecodeError as e:
            log.error(f"Failed to decode configuration JSON from Supervisor: {e}")
            return None # Do not retry on malformed data

    log.critical(f"Failed to fetch configuration from Supervisor after {retries} attempts. Aborting.")
    return None

def post_config_to_supervisor(host: str, port: int, key: str, value: Any) -> bool:
    """
    Posts a configuration change to the Supervisor's internal API.

    :param host: The host of the Supervisor's config API.
    :param port: The port of the Supervisor's config API.
    :param key: The configuration key to update.
    :param value: The new value for the configuration key.
    :return: True if the update was successful, False otherwise.
    """
    url = f"http://{host}:{port}/config"
    payload = {"key": key, "value": value}
    try:
        response = requests.post(url, json=payload, timeout=5)
        response.raise_for_status()
        log.info(f"Successfully posted config update for '{key}' to Supervisor.")
        return True
    except requests.exceptions.RequestException as e:
        log.error(f"Failed to post configuration update to Supervisor: {e}")
        # Try to parse the error message from the supervisor
        try:
            error_detail = e.response.json().get("detail", "No details provided.")
            print(f"Error from server: {error_detail}")
        except (AttributeError, json.JSONDecodeError, TypeError):
            print("Could not retrieve error details from the server.")
        return False
