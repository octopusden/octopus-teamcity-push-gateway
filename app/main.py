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
    if value is None:
        return ""
    return str(value).replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')


def get_property(properties, name, default=None):
    if isinstance(properties, dict):
        properties = [properties]
    if not isinstance(properties, list):
        return default
    for prop in properties:
        if isinstance(prop, dict) and prop.get("name") == name:
            return prop.get("value", default)
    return default


def parse_teamcity_payload(data):
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
    """Escape spaces, commas, equals in tag keys/values (InfluxDB line protocol)."""
    return str(value).replace(',', '\\,').replace('=', '\\=').replace(' ', '\\ ')


def build_line_protocol(parsed_data: dict) -> str:
    """
    Build an InfluxDB line protocol string from parsed TeamCity data.

    Tags (indexed, used in filters):
        build_type_id, build_type_component, build_type_name, branch, template_name

    Fields (numeric/string values):
        status_value (int), status (string), version (string),
        build_url (string), build_id (string)
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
    POST a single line protocol record to InfluxDB v2 /api/v2/write.
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