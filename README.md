# TeamCity / Jenkins build status to InfluxDB

A service that receives build webhooks from TeamCity and build JSON from Jenkins pipelines,
and writes build status points to InfluxDB v2 in line protocol. Use it to track build
history in InfluxDB and Grafana.

> The repository name is historical. Earlier versions pushed to a Prometheus Pushgateway.
> The current version writes to InfluxDB only.

## Changes

- **Build duration**: the TeamCity path writes a `duration_seconds` field
  (`finishDate - startDate`) to `teamcity_build_status`. It is computed by `parse_tc_date` /
  `compute_duration_seconds` and is left out for canceled or never-started builds, which have
  no start/finish pair.
- **Jenkins endpoint (separate bucket)**: `POST /jenkins` accepts a compact build JSON from
  Jenkins pipelines. It writes the measurement `jenkins_build_status` to its own bucket
  `INFLUXDB_JENKINS_BUCKET` (default `jenkins`), using a compatible subset of the tag and field names of
  `teamcity_build_status` (see "Data written" below). Jenkins builds can POST here with a plain `curl` in
  `post { always { ... } }`. `template_name` does not apply to Jenkins, so rows carry
  `template_name="empty"`. **The `jenkins` bucket must exist in InfluxDB and be writable by
  `INFLUXDB_TOKEN`**. Buckets are not auto-created; measurements are.
- **Component name tag**: `teamcity_build_status` carries a `metric_component_name` tag taken
  from the TeamCity build parameter `COMPONENT_NAME` (`unknown` if not set).
- **Project and default-branch tags**: `teamcity_build_status` carries `project_id` and
  `default_branch` (`true` / `false` / `unknown`).

## Installation

```bash
pip install -r requirements.txt
```

## Configuration

All parameters are configured via environment variables:

| Variable | Description | Default Value | Required |
|----------|-------------|---------------|----------|
| `INFLUXDB_URL` | InfluxDB v2 base URL | `http://localhost:8086` | Yes |
| `INFLUXDB_TOKEN` | InfluxDB API token (sent as `Authorization: Token …`) | `""` | Yes |
| `INFLUXDB_ORG` | InfluxDB organisation | `my-org` | Yes |
| `INFLUXDB_BUCKET` | Bucket for TeamCity points | `teamcity` | No |
| `INFLUXDB_JENKINS_BUCKET` | Bucket for Jenkins points (must exist) | `jenkins` | No |
| `JENKINS_HOST_SUFFIX` | Domain suffix stripped from `jenkins_url` to get a short instance name | `""` | No |
| `PORT` | Port for receiving webhooks | `8000` | No |
| `LOG_LEVEL` | Logging level (name or number: DEBUG/10 … CRITICAL/50) | `INFO` | No |
| `LOG_FORMAT` | Logging output format: `json` or `text` | `json` | No |

### Logging

Logging is configured through [octopus-oc-corelibs-logging](https://github.com/octopusden/octopus-oc-corelibs-logging)
(`oc-logging`, structlog-based). Every record carries the level, the message, a UTC timestamp
and the calling function name:

```json
{"level": "info", "message": "InfluxDB write → 204", "timestamp": "2025-10-09 15:05:43", "func_name": "send_to_influxdb"}
```

Records coming from third-party libraries (`werkzeug` access logs, `urllib3`) are rendered in the
same one-line format and carry an extra `logger` field with the library logger name. This matters
for log collectors: a bare, multi-line library message gets merged into the preceding record and
breaks JSON parsing on ingest.

```json
{"level": "info", "message": "127.0.0.1 - - [09/Oct/2025 15:05:43] \"POST /webhook HTTP/1.1\" 200 -", "timestamp": "2025-10-09 15:05:43", "func_name": "_log", "logger": "werkzeug"}
```

### Running

```bash
export INFLUXDB_URL="http://influxdb.example.com:8086"
export INFLUXDB_TOKEN="..."
export INFLUXDB_ORG="my-org"
python3 app/main.py
```

```bash
docker build -t teamcity-webhook-proxy .
docker run -d \
  -e INFLUXDB_URL="http://influxdb:8086" \
  -e INFLUXDB_TOKEN="..." \
  -e INFLUXDB_ORG="my-org" \
  -p 8000:8000 \
  teamcity-webhook-proxy
```

## Usage

### POST /webhook

Receives TeamCity `BUILD_FINISHED` webhooks and writes one point to `INFLUXDB_BUCKET`.

`POST /webhook/<anything>` is accepted for backward compatibility, but **the path segment is
ignored**. `template_name` comes from the build parameter `MONITORING_TEMPLATE_ID`, with
default `empty`.

```bash
curl -X POST http://localhost:8000/webhook \
  -H "Content-Type: application/json" \
  -d '{
    "eventType": "BUILD_FINISHED",
    "payload": {
      "id": 12345,
      "buildTypeId": "Project_BuildConfig",
      "number": "1.0.0",
      "status": "SUCCESS",
      "branchName": "master",
      "defaultBranch": true,
      "startDate": "20250101T100000+0000",
      "finishDate": "20250101T101500+0000",
      "buildType": {
        "name": "Build Configuration",
        "projectName": "Department / Team / Project",
        "projectId": "Department_Team_Project",
        "webUrl": "https://teamcity.example.com/buildConfiguration/Project_BuildConfig"
      },
      "properties": {"property": [
        {"name": "MONITORING_TEMPLATE_ID", "value": "MyTemplate"},
        {"name": "COMPONENT_NAME", "value": "my-component"}
      ]},
      "webUrl": "https://teamcity.example.com/build/12345"
    }
  }'
```

Response:

```json
{
  "status": "success",
  "message": "Metric written to InfluxDB",
  "build_type": "Build Configuration",
  "version": "1.0.0",
  "build_status": "SUCCESS",
  "template_name": "MyTemplate",
  "influxdb_response": 204
}
```

### POST /jenkins

Receives a compact JSON from a Jenkins pipeline and writes one point to
`INFLUXDB_JENKINS_BUCKET`.

Recognised keys:
- `job`, `job_name`, `component`, `branch`, `number`, `status`
- `build_url`, `jenkins_url`, `environment`, `dry_run`, `template_name`
- `image_tag`, `git_commit`, `duration_seconds`, `start_time` (epoch ms)

The response has the same shape as `/webhook`, plus `bucket`.

### Errors

- `400`: the request has no JSON body.
- `500`: any processing error, including a non-2xx answer from InfluxDB. The error message is
  in the `message` field.

### TeamCity configuration

Add these to the build configuration (or template) parameters:

```properties
teamcity.internal.webhooks.enable=True
teamcity.internal.webhooks.events=BUILD_FINISHED
teamcity.internal.webhooks.url=http://your-server:8000/webhook
# optional, become tags:
MONITORING_TEMPLATE_ID=MyTemplate
COMPONENT_NAME=my-component
```

## Data written

Points are timestamped at **receive time** (nanosecond precision).

### `teamcity_build_status` (bucket `INFLUXDB_BUCKET`)

| Kind | Name | Source / value |
|---|---|---|
| tag | `build_type_id` | `buildTypeId` |
| tag | `build_type_component` | last ` / ` segment of `buildType.projectName` |
| tag | `build_type_name` | `buildType.name` |
| tag | `branch` | `branchName`, `unknown` if missing |
| tag | `template_name` | build parameter `MONITORING_TEMPLATE_ID`, default `empty` |
| tag | `project_id` | `buildType.projectId` |
| tag | `default_branch` | `true` / `false` / `unknown` |
| tag | `metric_component_name` | build parameter `COMPONENT_NAME`, default `unknown` |
| field | `status_value` (int) | `1` for `SUCCESS`, else `0` |
| field | `status` | raw TeamCity status |
| field | `version` | build number |
| field | `build_url` | build configuration URL |
| field | `build_id` | build id |
| field | `duration_seconds` (float) | only when start and finish dates are present |

Line protocol example:

```
teamcity_build_status,build_type_id=Project_BuildConfig,build_type_component=Project,build_type_name=Build\ Configuration,branch=master,template_name=MyTemplate,project_id=Department_Team_Project,default_branch=true,metric_component_name=my-component status_value=1i,status="SUCCESS",version="1.0.0",build_url="https://teamcity.example.com/buildConfiguration/Project_BuildConfig",build_id="12345",duration_seconds=900.0 1735726500000000000
```

### `jenkins_build_status` (bucket `INFLUXDB_JENKINS_BUCKET`)

- **Tags:** `build_type_id` (`job`), `build_type_component` (`component`), `build_type_name`
  (`job_name` or `job`), `branch`, `template_name`, `environment`, `dry_run`.
- **Fields:** `status_value`, `status`, `version`, `build_url`, `jenkins_instance`,
  `build_id`.
- **Optional fields:** `image_tag`, `git_commit`, `duration_seconds`, `start_time`.

### Query example (Flux)

```flux
from(bucket: "teamcity")
  |> range(start: -7d)
  |> filter(fn: (r) => r._measurement == "teamcity_build_status" and r._field == "status_value")
  |> filter(fn: (r) => r.branch == "master")
  |> last()
```

## License

Apache License 2.0. See [LICENSE](LICENSE).
