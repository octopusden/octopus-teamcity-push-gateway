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
    Escape special characters for Prometheus label values.

    Args:
        value: Label value to escape

    Returns:
        str: Escaped string
    """
    if value is None:
        return ""
    return str(value).replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')


def parse_teamcity_payload(data):
    """
    Parse TeamCity webhook payload.

    Extracts required fields from JSON payload:
    - Build ID and build type ID
    - Project name and component
    - Version and branch
    - Build status
    - Build URLs

    Args:
        data (dict): JSON data from TeamCity webhook

    Returns:
        dict: Dictionary with parsed data

    Raises:
        Exception: On parsing error
    """
    try:
        event_type = data.get('eventType', '')
        payload = data.get('payload', {})

        build_type_id = payload.get('buildTypeId', '')
        build_id = payload.get('id', '')
        build_type = payload.get('buildType', {})
        build_type_name = build_type.get('name', '')
        build_type_component = build_type.get('projectName', '').split(" / ")[-1]
        version = payload.get('number', '')
        status = payload.get('status', 'UNKNOWN')

        build_url = build_type.get('webUrl', '')
        current_build_url = payload.get('webUrl', '')

        branch = payload.get('branchName', 'unknown')

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
            'event_type': event_type
        }

        logger.info(f"Parsed payload: {parsed}")
        return parsed

    except Exception as e:
        logger.error(f"Failed payload parsing: {str(e)}")
        raise


def create_prometheus_metric(parsed_data):
    """
    Format metric in Prometheus text format.

    Creates teamcity_build_status metric with labels:
    - build_type_id: Build configuration ID
    - build_type_component: Project component name
    - build_type_name: Build configuration name
    - version: Build version number
    - branch: Branch name
    - build_url: Build configuration URL

    Args:
        parsed_data (dict): Parsed data from TeamCity

    Returns:
        str: Metric in Prometheus text format
    """
    metric_name = "teamcity_build_status"

    metric_text = f"""# TYPE {metric_name} gauge
# HELP {metric_name} TeamCity build status (1=SUCCESS, 0=FAILURE)
{metric_name}{{build_type_id="{parsed_data['build_type_id']}",build_type_component="{parsed_data['build_type_component']}",build_type_name="{parsed_data['build_type_name']}",version="{parsed_data['version']}",branch="{parsed_data['branch']}",build_url="{parsed_data['build_url']}"}} {parsed_data['status_value']}
"""

    return metric_text


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


@app.route('/webhook', methods=['POST'])
def teamcity_webhook():
    """
    TeamCity webhook handler.

    Accepts POST request with JSON payload from TeamCity,
    parses data, creates metric and sends to Pushgateway.

    Returns:
        tuple: JSON response and HTTP status code
    """
    try:
        data = request.get_json()

        if not data:
            return jsonify({
                "status": "error",
                "message": "No JSON data received"
            }), 400

        logger.info(f"Get webhook: {data.get('eventType', 'UNKNOWN')}")

        parsed_data = parse_teamcity_payload(data)

        metric_text = create_prometheus_metric(parsed_data)
        logger.info(f"Metric:\n{metric_text}")

        response = send_to_pushgateway(metric_text, parsed_data)

        return jsonify({
            "status": "success",
            "message": "Metric go to Pushgateway",
            "build_type": parsed_data['build_type_name'],
            "version": parsed_data['version'],
            "build_status": parsed_data['status'],
            "pushgateway_response": response.status_code
        }), 200

    except Exception as e:
        logger.error(f"Failer wenhook parse: {str(e)}")
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500

if __name__ == '__main__':
    logger.info(f"Run TeamCity Webhook -> Pushgateway Proxy")
    logger.info(f"Listening on port: {PORT}")
    logger.info(f"Pushgateway URL: {PUSHGATEWAY_URL}")
    app.run(host='0.0.0.0', port=PORT, debug=False)