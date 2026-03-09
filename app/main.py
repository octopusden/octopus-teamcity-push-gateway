#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from flask import Flask, request, jsonify
import requests
import logging
import os
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

PUSHGATEWAY_URL = os.getenv('PUSHGATEWAY_URL', 'http://localhost:9091')
PORT = int(os.getenv('PORT', '8000'))
JOB_NAME = os.getenv('JOB_NAME', 'pushgateway')
INSTANCE_NAME = os.getenv('INSTANCE_NAME', 'teamcity')


def escape_label_value(value):
    """
    Escape special characters in a value so it can be used as a Prometheus label.

    If `value` is `None`, returns an empty string. Otherwise converts `value` to `str`
    and escapes backslashes (`\`), double quotes (`"`), and newlines.

    Parameters:
        value: The value to escape; may be any type (will be converted to `str`).

    Returns:
        str: Escaped string
    """
    if value is None:
        return ""
    return str(value).replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')

def get_property(properties, name, default=None):
    """
    Return the value of the first property with the given name from a list of TeamCity property dicts.
    
    Parameters:
        properties (list[dict]): Iterable of property dictionaries containing at least the keys 'name' and 'value'.
        name (str): Property name to search for.
        default: Value to return if no property with the given name is found.
    
    Returns:
        The matched property's `value`, or `default` if not found.
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
    Parse a TeamCity webhook payload into a dictionary of fields suitable for Prometheus metrics.

    Escapes label-like fields for Prometheus, derives the build type component from the project name, and maps the build status to `status_value` (1 for `SUCCESS`, 0 otherwise).

    Parameters:
        data (dict): JSON payload from a TeamCity webhook.

    Returns:
        dict: Parsed values including keys:
            - build_type_id, build_type_name, build_type_component, version, branch, build_url,
              current_build_url, build_id (all escaped for Prometheus labels)
            - status (raw status string)
            - status_value (int: 1 for SUCCESS, 0 otherwise)
            - event_type (original event type)

    Raises:
        Exception: If parsing fails.
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


def create_prometheus_metric(parsed_data):
    """
    Format a TeamCity build status into Prometheus exposition-format text.
    
    Parameters:
        parsed_data (dict): Parsed TeamCity payload containing keys:
            `build_type_id`, `build_type_component`, `build_type_name`, `version`,
            `branch`, `build_url`, `template_name`, and `status_value`.
    
    Returns:
        str: Prometheus exposition-format metric text for the build status.
    """
    metric_name = "teamcity_build_status"

    labels = (
        f'build_type_id="{parsed_data["build_type_id"]}",'
        f'build_type_component="{parsed_data["build_type_component"]}",'
        f'build_type_name="{parsed_data["build_type_name"]}",'
        f'version="{parsed_data["version"]}",'
        f'branch="{parsed_data["branch"]}",'
        f'build_url="{parsed_data["build_url"]}",'
        f'template_name="{parsed_data["template_name"]}"'
    )

    return (
        f"# TYPE {metric_name} gauge\n"
        f"# HELP {metric_name} TeamCity build status (1=SUCCESS, 0=FAILURE)\n"
        f"{metric_name}{{{labels}}} {parsed_data['status_value']}\n"
    )



def send_to_pushgateway(metric_text, parsed_data, job=JOB_NAME, instance=INSTANCE_NAME):
    """
    Send metric to Prometheus Pushgateway.

    URL format:
    /metrics/job/{job}/instance/{instance}/buildid/{build_id}

    Args:
        metric_text (str): Metric in Prometheus text format
        parsed_data (dict): Parsed data with build_id
        job (str): Job name for Pushgateway
        instance (str): Instance name for Pushgateway

    Returns:
        requests.Response: Response from Pushgateway

    Raises:
        requests.exceptions.RequestException: On HTTP request error
    """
    try:
        from urllib.parse import quote

        build_id = quote(parsed_data['build_id'], safe='')

        url = f"{PUSHGATEWAY_URL}/metrics/job/{job}/instance/{instance}/buildid/{build_id}"

        headers = {
            'Content-Type': 'text/plain; charset=utf-8'
        }

        response = requests.post(
            url,
            data=metric_text.encode('utf-8'),
            headers=headers,
            timeout=5
        )

        logger.info(f"Metrics go to Pushgateway: {url}")
        logger.info(f"Response: {response.status_code}")

        return response

    except requests.exceptions.RequestException as e:
        logger.error(f"Failed push metrics into Pushgateway: {str(e)}")
        raise


@app.route('/webhook', defaults={'template_name': None}, methods=['POST'])
@app.route('/webhook/<template_name>', methods=['POST'])
def teamcity_webhook(template_name=None):
    """
    Handle TeamCity webhook POSTs: parse the JSON payload, create a Prometheus metric, push it to the configured Pushgateway, and return a JSON result.
    
    Parameters:
        template_name (str | None): Optional template name extracted from the request path (may be None).
    
    Returns:
        tuple: (response_body, status_code) — JSON object with either success details (including build type, version, status, template_name, and Pushgateway response code) or an error message, and the corresponding HTTP status code (200 on success, 400 on bad request, 500 on processing error).
    """
    try:
        data = request.get_json(silent=True)

        if not data:
            return jsonify({
                "status": "error",
                "message": "No JSON data received"
            }), 400


        parsed_data = parse_teamcity_payload(data)

        metric_text = create_prometheus_metric(parsed_data)
        logger.info(f"Metric:\n{metric_text}")

        response = send_to_pushgateway(metric_text, parsed_data)
        response.raise_for_status()
        return jsonify({
            "status": "success",
            "message": "Metric go to Pushgateway",
            "build_type": parsed_data['build_type_name'],
            "version": parsed_data['version'],
            "build_status": parsed_data['status'],
            "template_name": parsed_data['template_name'],
            "pushgateway_response": response.status_code
        }), 200

    except Exception as e:
        logger.error(f"Failed webhook parse: {str(e)}")
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500

if __name__ == '__main__':
    logger.info("Run TeamCity Webhook -> Pushgateway Proxy")
    logger.info(f"Listening on port: {PORT}")
    logger.info(f"Pushgateway URL: {PUSHGATEWAY_URL}")
    app.run(host='0.0.0.0', port=PORT, debug=False)