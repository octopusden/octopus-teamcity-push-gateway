#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from flask import Flask, request, jsonify
import requests
import logging
import os
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

INFLUXDB_URL      = os.getenv('INFLUXDB_URL', 'http://localhost:8086')
INFLUXDB_TOKEN    = os.getenv('INFLUXDB_TOKEN', '')
INFLUXDB_ORG      = os.getenv('INFLUXDB_ORG', 'my-org')
INFLUXDB_BUCKET   = os.getenv('INFLUXDB_BUCKET', 'teamcity')
PORT              = int(os.getenv('PORT', '8000'))


def escape_label_value(value):
    """
    Escape a value for use as a label value by escaping backslashes, double quotes, and newlines.
    
    Parameters:
        value: The value to escape; if None an empty string is returned.
    
    Returns:
        str: The input converted to a string with backslashes (`\`), double quotes (`"`), and newline characters escaped; returns an empty string when `value` is `None`.
    """
    if value is None:
        return ""
    return str(value).replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')


def get_property(properties, name, default=None):
    """
    Retrieve the value of a named property from a properties collection.
    
    Parameters:
        properties (dict | list[dict] | None): A single property dict or a list of property dicts, where each dict may contain "name" and "value" keys.
        name (str): The property name to look for.
        default (Any, optional): Value to return if the named property is not found. Defaults to None.
    
    Returns:
        The matched property's "value" if found, otherwise `default`.
    """
    if isinstance(properties, dict):
        properties = [properties]
    if not isinstance(properties, list):
        return default
    for prop in properties:
        if isinstance(prop, dict) and prop.get("name") == name:
            return prop.get("value", default)
    return default


def parse_teamcity_payload(data):
    """
    Parse a TeamCity webhook JSON payload into a sanitized dictionary of build metadata.
    
    Parameters:
        data (dict): The raw JSON payload received from TeamCity.
    
    Returns:
        dict: Parsed and escaped build metadata containing:
            - build_type_id: Escaped build configuration identifier.
            - build_type_name: Escaped build configuration display name.
            - version: Escaped build number/version.
            - branch: Escaped branch name.
            - build_url: Escaped project-level web URL for the build type.
            - current_build_url: Escaped web URL for this specific build.
            - build_type_component: Escaped project component name (last segment after " / ").
            - status: Build status string as provided by TeamCity (e.g., "SUCCESS", "FAILURE", "UNKNOWN").
            - status_value: Integer status metric (1 when status == "SUCCESS", otherwise 0).
            - build_id: Escaped build identifier.
            - event_type: Event type string from the webhook payload.
            - template_name: Escaped value of the MONITORING_TEMPLATE_ID property or "empty" if absent.
    """
    try:
        event_type = data.get('eventType', '')
        payload = data.get('payload', {})

        build_type_id = payload.get('buildTypeId', '')
        build_id = payload.get('id', 'empty')
        build_type = payload.get('buildType', {})
        build_type_name = build_type.get('name', '')
        build_type_component = build_type.get('projectName', '').split(" / ")[-1]
        version = payload.get('number', '')
        status = payload.get('status', 'UNKNOWN')
        build_url = build_type.get('webUrl', '')
        current_build_url = payload.get('webUrl', '')
        branch = payload.get('branchName', 'unknown')
        properties = payload.get('properties', {}).get('property', [])
        template_name = escape_label_value(
            get_property(properties, 'MONITORING_TEMPLATE_ID', default='empty')
        )
        status_value = 1 if status == 'SUCCESS' else 0

        parsed = {
            'build_type_id': escape_label_value(build_type_id),
            'build_type_name': escape_label_value(build_type_name),
            'version': escape_label_value(version),
            'branch': escape_label_value(branch),
            'build_url': escape_label_value(build_url),
            'current_build_url': escape_label_value(current_build_url),
            'build_type_component': escape_label_value(build_type_component),
            'status': status,
            'status_value': status_value,
            'build_id': escape_label_value(build_id),
            'event_type': event_type,
            'template_name': template_name
        }

        logger.info(f"Parsed payload: {parsed}")
        return parsed

    except Exception as e:
        logger.error(f"Failed payload parsing: {str(e)}")
        raise


def escape_tag(value: str) -> str:
    """
    Escape commas, equals signs, and spaces in a tag key or value for use with InfluxDB line protocol.
    
    Parameters:
        value (str): Tag key or value to escape.
    
    Returns:
        str: The input converted to string with commas (','), equals ('='), and spaces (' ') escaped with a backslash.
    """
    return str(value).replace(',', '\\,').replace('=', '\\=').replace(' ', '\\ ')


def build_line_protocol(parsed_data: dict) -> str:
    """
    Construct an InfluxDB line protocol record representing a TeamCity build status.
    
    Parameters:
        parsed_data (dict): Parsed TeamCity payload containing the following keys:
            - build_type_id (str)
            - build_type_component (str)
            - build_type_name (str)
            - branch (str)
            - template_name (str)
            - status_value (int): numeric status (e.g., 1 for success, 0 otherwise)
            - status (str)
            - version (str)
            - build_url (str)
            - build_id (str)
    
    Returns:
        str: A single-line InfluxDB line protocol string containing measurement, tags,
        fields, and a UTC nanosecond-precision timestamp.
    """
    measurement = "teamcity_build_status"

    tags = ",".join([
        f"build_type_id={escape_tag(parsed_data['build_type_id'])}",
        f"build_type_component={escape_tag(parsed_data['build_type_component'])}",
        f"build_type_name={escape_tag(parsed_data['build_type_name'])}",
        f"branch={escape_tag(parsed_data['branch'])}",
        f"template_name={escape_tag(parsed_data['template_name'])}",
    ])

    # string fields must be wrapped in quotes; numeric — без кавычек
    fields = ",".join([
        f"status_value={parsed_data['status_value']}i",
        f'status="{parsed_data["status"]}"',
        f'version="{escape_tag(parsed_data["version"])}"',
        f'build_url="{parsed_data["build_url"]}"',
        f'build_id="{parsed_data["build_id"]}"',
    ])

    timestamp_ns = int(datetime.now(timezone.utc).timestamp() * 1e9)

    return f"{measurement},{tags} {fields} {timestamp_ns}"


def send_to_influxdb(line: str) -> requests.Response:
    """
    Write a single InfluxDB line protocol record to the configured InfluxDB v2 write API.
    
    Parameters:
        line (str): A single line in InfluxDB line protocol format to send.
    
    Returns:
        response (requests.Response): HTTP response returned by the InfluxDB write endpoint.
    
    Raises:
        requests.exceptions.RequestException: If the HTTP request fails (network error, timeout, etc.).
    """
    url = f"{INFLUXDB_URL}/api/v2/write"
    params = {
        "org":       INFLUXDB_ORG,
        "bucket":    INFLUXDB_BUCKET,
        "precision": "ns",
    }
    headers = {
        "Authorization": f"Token {INFLUXDB_TOKEN}",
        "Content-Type":  "text/plain; charset=utf-8",
    }

    try:
        response = requests.post(
            url,
            params=params,
            headers=headers,
            data=line.encode("utf-8"),
            timeout=5,
        )
        logger.info(f"InfluxDB write → {response.status_code}  line: {line}")
        return response
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to write to InfluxDB: {e}")
        raise


@app.route('/webhook', defaults={'template_name': None}, methods=['POST'])
@app.route('/webhook/<template_name>', methods=['POST'])
def teamcity_webhook(template_name=None):
    """
    Handle incoming TeamCity webhook POSTs, parse the payload, write a build status point to InfluxDB, and return a JSON status response.
    
    Parameters:
        template_name (str | None): Optional template name captured from the request URL; when provided it is available to the handler alongside any template identifier present in the payload.
    
    Returns:
        tuple: A Flask response tuple (JSON body, HTTP status code). On success the JSON includes "status": "success", "message", build metadata ("build_type", "version", "build_status", "template_name") and "influxdb_response" (HTTP status code from InfluxDB). On failure the JSON includes "status": "error" and an error "message".
    """
    try:
        data = request.get_json(silent=True)
        if not data:
            return jsonify({"status": "error", "message": "No JSON data received"}), 400

        parsed_data = parse_teamcity_payload(data)
        line = build_line_protocol(parsed_data)

        response = send_to_influxdb(line)
        response.raise_for_status()

        return jsonify({
            "status": "success",
            "message": "Metric written to InfluxDB",
            "build_type":      parsed_data['build_type_name'],
            "version":         parsed_data['version'],
            "build_status":    parsed_data['status'],
            "template_name":   parsed_data['template_name'],
            "influxdb_response": response.status_code,
        }), 200

    except Exception as e:
        logger.error(f"Failed webhook processing: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


if __name__ == '__main__':
    logger.info("Run TeamCity Webhook → InfluxDB")
    logger.info(f"Listening on port: {PORT}")
    logger.info(f"InfluxDB URL: {INFLUXDB_URL} / org: {INFLUXDB_ORG} / bucket: {INFLUXDB_BUCKET}")
    app.run(host='0.0.0.0', port=PORT, debug=False)