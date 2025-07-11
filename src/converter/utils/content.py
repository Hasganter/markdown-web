import re
import yaml
import logging
from typing import Any, Dict, Tuple

log = logging.getLogger("content_converter")


def parse_source_with_yaml_header(source_content: str) -> Tuple[str, Dict[str, Any]]:
    """
    Parses a source file's content, separating YAML front matter from the body.

    The YAML block, enclosed in '~~~', is safely parsed to extract context
    variables, template settings, and allowed HTTP methods.

    :param source_content: The full string content of the source file.
    :return tuple: A tuple containing (body_content, parsed_config_data).
    """
    config_data: Dict[str, Any] = {
        "CONTEXT": {},
        "TEMPLATE": {},
        "ALLOWED_METHODS": ["GET"]
    }
    body_content = source_content

    # Regex to find a YAML front matter block.
    match = re.match(r"^\s*~~~\s*\n(.*?)\n~~~\s*\n(.*)", source_content, re.DOTALL)
    if not match:
        return body_content, config_data

    yaml_header, body_content = match.group(1), match.group(2)
    try:
        # Use safe_load to prevent arbitrary code execution from malicious YAML.
        parsed_yaml = yaml.safe_load(yaml_header)

        if isinstance(parsed_yaml, dict):
            config_data["CONTEXT"] = parsed_yaml.get("CONTEXT", {})
            config_data["TEMPLATE"] = parsed_yaml.get("TEMPLATE", {})
            methods = parsed_yaml.get("ALLOWED_METHODS", ["GET"])
            # Ensure methods are uppercase strings.
            config_data["ALLOWED_METHODS"] = [str(m).upper().strip() for m in methods] if isinstance(methods, list) else ["GET"]
        else:
            log.warning("YAML front matter did not parse into a dictionary. Ignoring.")
    except yaml.YAMLError as e:
        log.error(f"Error parsing YAML front matter: {e}", exc_info=True)

    return body_content, config_data
