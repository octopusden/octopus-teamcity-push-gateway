#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from flask import Flask, request, jsonify
import requests
import logging
import structlog
from oc_logging import setup_json_logging, setup_text_logging
import json
import os
from datetime import datetime, timezone


def get_log_level():
    """
    Resolve the logging level name from the LOG_LEVEL environment variable.

    LOG_LEVEL may be a level name (e.g. "debug", "WARNING") or a numeric string
    (10/20/30/40/50). Unset or unrecognized values resolve to "info".

    Returns:
        str: A level name accepted by oc_logging ("debug", "info", "warning", "error", "critical").
    """
    lvl = os.environ.get("LOG_LEVEL")
    if not lvl:
        return "info"

    if lvl.isdigit():
        name = logging.getLevelName(int(lvl))
    else:
        name = lvl.upper()

    if name not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        return "info"
    return name.lower()


# The stdlib logger name our structlog records are emitted under. Passing it explicitly to
# structlog.get_logger() pins it, instead of letting the factory derive it from the call site --
# ForeignLogFormatter relies on it to tell our (already rendered) records from foreign ones.
APP_LOGGER_NAME = "teamcity-push-gateway"


class ForeignLogFormatter(logging.Formatter):
    """Render records from third-party stdlib loggers in the same shape as our own.

    oc-logging sets the root format to "%(message)s" (structlog renders our records
    itself), so anything logged through plain stdlib logging -- werkzeug access logs,
    urllib3 -- is printed bare, with no level and no timestamp. Log collectors then
    merge those lines into the preceding structlog event, which stops being valid JSON
    and lands in Kibana unparsed. Wrapping them keeps every line a self-contained record.

    Records emitted by this module already went through structlog and are passed
    through untouched.
    """

    def __init__(self, json_output):
        super().__init__()
        self.json_output = json_output

    def format(self, record):
        if record.name == APP_LOGGER_NAME:
            return record.getMessage()

        message = record.getMessage()
        if record.exc_info:
            message = f"{message}\n{self.formatException(record.exc_info)}"
        timestamp = datetime.fromtimestamp(record.created, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        if self.json_output:
            # json.dumps escapes newlines, so a traceback stays on a single line.
            return json.dumps({
                "level": record.levelname.lower(),
                "message": message,
                "timestamp": timestamp,
                "func_name": record.funcName,
                "logger": record.name,
            })
        return f"[{timestamp}] [{record.levelname}] {message} func_name={record.funcName} logger={record.name}"


def setup_logging():
    """
    Configure structlog via oc-logging and return the application logger.

    LOG_FORMAT selects the renderer: "json" (default) or "text". The calling
    function name is added to every record. Third-party stdlib loggers (werkzeug,
    urllib3) are rendered in the same format, see ForeignLogFormatter.
    """
    log_format = os.environ.get("LOG_FORMAT", "json").lower()
    if log_format not in ("json", "text"):
        raise ValueError("LOG_FORMAT must be json or text")

    setup = setup_json_logging if log_format == "json" else setup_text_logging
    setup(
        get_log_level(),
        custom_processors=[
            structlog.processors.CallsiteParameterAdder(
                [structlog.processors.CallsiteParameter.FUNC_NAME]
            )
        ],
    )

    formatter = ForeignLogFormatter(log_format == "json")
    for handler in logging.getLogger().handlers:
        handler.setFormatter(formatter)

    return structlog.get_logger(APP_LOGGER_NAME)


logger = setup_logging()

app = Flask(__name__)

INFLUXDB_URL           = os.getenv('INFLUXDB_URL', 'http://localhost:8086')
INFLUXDB_TOKEN         = os.getenv('INFLUXDB_TOKEN', '')
INFLUXDB_ORG           = os.getenv('INFLUXDB_ORG', 'my-org')
INFLUXDB_BUCKET        = os.getenv('INFLUXDB_BUCKET', 'teamcity')
# Jenkins builds go to their own bucket (must exist and be writable by INFLUXDB_TOKEN).
INFLUXDB_JENKINS_BUCKET = os.getenv('INFLUXDB_JENKINS_BUCKET', 'jenkins')
PORT                   = int(os.getenv('PORT', '8000'))


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


# TeamCity timestamps look like "20260125T070716+0000".
TC_DATE_FORMAT = "%Y%m%dT%H%M%S%z"


def parse_tc_date(value):
    """Parse a TeamCity timestamp (e.g. '20260125T070716+0000') to an aware datetime, or None."""
    if not value:
        return None
    try:
        return datetime.strptime(value, TC_DATE_FORMAT)
    except (ValueError, TypeError):
        return None


def compute_duration_seconds(payload):
    """Overall build duration in seconds = finishDate - startDate. None if either is missing/unparsable."""
    start = parse_tc_date(payload.get('startDate'))
    finish = parse_tc_date(payload.get('finishDate'))
    if start is None or finish is None:
        return None
    return (finish - start).total_seconds()


def parse_teamcity_payload(data):
    try:
        event_type = data.get('eventType', '')
        payload = data.get('payload', {})

        build_type_id = payload.get('buildTypeId', '')
        build_id = payload.get('id', 'empty')
        build_type = payload.get('buildType', {})
        build_type_name = build_type.get('name', '')
        build_type_component = build_type.get('projectName', '').split(" / ")[-1]
        project_id = build_type.get('projectId', '')
        default_branch_raw = payload.get('defaultBranch')
        default_branch = (
            str(default_branch_raw).lower()
            if default_branch_raw not in (None, "")
            else "unknown"
        )
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
        duration_seconds = compute_duration_seconds(payload)

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
            'template_name': template_name,
            'project_id': escape_label_value(project_id),
            'default_branch': default_branch,
            'duration_seconds': duration_seconds
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
        build_url (string), build_id (string),
        duration_seconds (float, only when finishDate-startDate is available)
    """
    measurement = "teamcity_build_status"

    tags = ",".join([
        f"build_type_id={escape_tag(parsed_data['build_type_id'])}",
        f"build_type_component={escape_tag(parsed_data['build_type_component'])}",
        f"build_type_name={escape_tag(parsed_data['build_type_name'])}",
        f"branch={escape_tag(parsed_data['branch'])}",
        f"template_name={escape_tag(parsed_data['template_name'])}",
        f"project_id={escape_tag(parsed_data['project_id'])}",
        f"default_branch={escape_tag(parsed_data['default_branch'])}",
    ])

    field_parts = [
        f"status_value={parsed_data['status_value']}i",
        f'status="{parsed_data["status"]}"',
        f'version="{escape_tag(parsed_data["version"])}"',
        f'build_url="{parsed_data["build_url"]}"',
        f'build_id="{parsed_data["build_id"]}"',
    ]
    # duration_seconds is optional: canceled/never-started builds have no start/finish pair.
    if parsed_data.get('duration_seconds') is not None:
        field_parts.append(f"duration_seconds={float(parsed_data['duration_seconds'])}")
    fields = ",".join(field_parts)

    timestamp_ns = int(datetime.now(timezone.utc).timestamp() * 1e9)

    return f"{measurement},{tags} {fields} {timestamp_ns}"


def send_to_influxdb(line: str, bucket: str = None) -> requests.Response:
    """
    POST a single line protocol record to InfluxDB v2 /api/v2/write.

    :param bucket: target bucket; defaults to INFLUXDB_BUCKET (TeamCity). The Jenkins
                   endpoint passes INFLUXDB_JENKINS_BUCKET.
    """
    target_bucket = bucket or INFLUXDB_BUCKET
    url = f"{INFLUXDB_URL}/api/v2/write"
    params = {
        "org":       INFLUXDB_ORG,
        "bucket":    target_bucket,
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
        logger.info(f"InfluxDB write → {response.status_code}  bucket: {target_bucket}  line: {line}")
        return response
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to write to InfluxDB: {e}")
        raise


def parse_jenkins_payload(data):
    """
    Parse the compact JSON our Jenkins shared-library step (pushBuildMetric) posts to /jenkins.

    Expected keys (all optional except status; sensible defaults applied):
        job (pipeline id), component, job_name (display), number (build number/version),
        status (SUCCESS/FAILURE/UNSTABLE/ABORTED), duration_seconds (float),
        branch, template_name, build_url
    """
    try:
        status = data.get('status', 'UNKNOWN')
        duration = data.get('duration_seconds')
        parsed = {
            'build_type_id': escape_label_value(data.get('job') or 'unknown'),
            'build_type_component': escape_label_value(data.get('component') or 'unknown'),
            'build_type_name': escape_label_value(data.get('job_name') or data.get('job') or 'unknown'),
            'branch': escape_label_value(data.get('branch') or 'unknown'),
            'template_name': escape_label_value(data.get('template_name') or 'empty'),
            'version': escape_label_value(data.get('number', '')),
            'build_url': escape_label_value(data.get('build_url', '')),
            'build_id': escape_label_value(data.get('number', '')),
            'status': status,
            'status_value': 1 if status == 'SUCCESS' else 0,
            'duration_seconds': float(duration) if duration is not None else None,
        }
        logger.info(f"Parsed Jenkins payload: {parsed}")
        return parsed
    except Exception as e:
        logger.error(f"Failed Jenkins payload parsing: {str(e)}")
        raise


def build_jenkins_line_protocol(parsed_data: dict) -> str:
    """
    Build an InfluxDB line for a Jenkins build.

    Written to its own bucket (INFLUXDB_JENKINS_BUCKET), measurement `jenkins_build_status`,
    using the SAME tag/field names as teamcity_build_status so dashboards can union the two.
    """
    measurement = "jenkins_build_status"

    tags = ",".join([
        f"build_type_id={escape_tag(parsed_data['build_type_id'])}",
        f"build_type_component={escape_tag(parsed_data['build_type_component'])}",
        f"build_type_name={escape_tag(parsed_data['build_type_name'])}",
        f"branch={escape_tag(parsed_data['branch'])}",
        f"template_name={escape_tag(parsed_data['template_name'])}",
    ])

    field_parts = [
        f"status_value={parsed_data['status_value']}i",
        f'status="{parsed_data["status"]}"',
        f'version="{escape_tag(parsed_data["version"])}"',
        f'build_url="{parsed_data["build_url"]}"',
        f'build_id="{parsed_data["build_id"]}"',
    ]
    if parsed_data.get('duration_seconds') is not None:
        field_parts.append(f"duration_seconds={float(parsed_data['duration_seconds'])}")
    fields = ",".join(field_parts)

    timestamp_ns = int(datetime.now(timezone.utc).timestamp() * 1e9)

    return f"{measurement},{tags} {fields} {timestamp_ns}"


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


@app.route('/jenkins', methods=['POST'])
def jenkins_webhook():
    try:
        data = request.get_json(silent=True)
        if not data:
            return jsonify({"status": "error", "message": "No JSON data received"}), 400

        parsed_data = parse_jenkins_payload(data)
        line = build_jenkins_line_protocol(parsed_data)

        response = send_to_influxdb(line, bucket=INFLUXDB_JENKINS_BUCKET)
        response.raise_for_status()

        return jsonify({
            "status": "success",
            "message": "Metric written to InfluxDB",
            "bucket":          INFLUXDB_JENKINS_BUCKET,
            "build_type":      parsed_data['build_type_name'],
            "version":         parsed_data['version'],
            "build_status":    parsed_data['status'],
            "influxdb_response": response.status_code,
        }), 200

    except Exception as e:
        logger.error(f"Failed Jenkins webhook processing: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


if __name__ == '__main__':
    logger.info("Run TeamCity Webhook → InfluxDB")
    logger.info(f"Listening on port: {PORT}")
    logger.info(f"InfluxDB URL: {INFLUXDB_URL} / org: {INFLUXDB_ORG} / bucket: {INFLUXDB_BUCKET} / jenkins bucket: {INFLUXDB_JENKINS_BUCKET}")
    app.run(host='0.0.0.0', port=PORT, debug=False)