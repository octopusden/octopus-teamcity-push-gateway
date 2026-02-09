# TeamCity to Prometheus Pushgateway
#
Service for receiving webhooks from TeamCity and sending metrics to Prometheus Pushgateway.

## Description

The service listens for incoming HTTP POST requests from TeamCity, parses build status data, and sends metrics to Prometheus Pushgateway. This allows tracking TeamCity build statuses in Prometheus and Grafana.

## Installation

### Dependencies

```bash
pip install -r requirements.txt
```

## Configuration

### Environment Variables

All parameters are configured via environment variables:

| Variable | Description | Default Value | Required |
|----------|-------------|---------------|----------|
| `PUSHGATEWAY_URL` | Prometheus Pushgateway URL | `http://localhost:9091` | Yes |
| `PORT` | Port for receiving webhooks | `8000` | No |
| `JOB_NAME` | Job name for Pushgateway | `pushgateway` | No |
| `INSTANCE_NAME` | Instance name for Pushgateway | `teamcity` | No |

### Configuration Examples

#### Linux/macOS

```bash
export PUSHGATEWAY_URL="http://pushgateway.example.com:9091"
export PORT="8000"
export JOB_NAME="pushgateway"
export INSTANCE_NAME="teamcity"

python3 teamcity_to_pushgateway.py
```

#### Docker

```bash
docker run -d \
  -e PUSHGATEWAY_URL="http://pushgateway:9091" \
  -e PORT="8000" \
  -e JOB_NAME="pushgateway" \
  -e INSTANCE_NAME="teamcity" \
  -p 8000:8000 \
  teamcity-webhook-proxy
```

## Usage

### Endpoints

#### POST /webhook
#### POST /webhook/<template_name>

Main endpoint for receiving webhooks from TeamCity.

The endpoint supports an optional `template_name` parameter in the URL path:
- `/webhook` - uses default value `empty` for `template_name` label
- `/webhook/<template_name>` - uses provided value for `template_name` label (e.g., `/webhook/production`, `/webhook/staging`)

**Example request:**

```bash
# Basic webhook (template_name will be "empty")
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
      "buildType": {
        "name": "Build Configuration",
        "projectName": "Department / Team / Project",
        "webUrl": "https://teamcity.example.com/buildConfiguration/Project_BuildConfig"
      },
      "webUrl": "https://teamcity.example.com/build/12345"
    }
  }'

# Webhook with template_name
curl -X POST http://localhost:8000/webhook/production \
  -H "Content-Type: application/json" \
  -d '{
    "eventType": "BUILD_FINISHED",
    "payload": {
      "id": 12345,
      "buildTypeId": "Project_BuildConfig",
      "number": "1.0.0",
      "status": "SUCCESS",
      "branchName": "master",
      "buildType": {
        "name": "Build Configuration",
        "projectName": "Department / Team / Project",
        "webUrl": "https://teamcity.example.com/buildConfiguration/Project_BuildConfig"
      },
      "webUrl": "https://teamcity.example.com/build/12345"
    }
  }'
```

**Response:**

```json
{
  "status": "success",
  "message": "Metric go to Pushgateway",
  "build_type": "Build Configuration",
  "version": "1.0.0",
  "build_status": "SUCCESS",
  "template_name": "production",
  "pushgateway_response": 200
}
```

### TeamCity Configuration

Add to build configuration parameters:

```properties
# Basic configuration (template_name will be "empty")
teamcity.internal.webhooks.enable=True
teamcity.internal.webhooks.events=BUILD_FINISHED
teamcity.internal.webhooks.url=http://your-server:8000/webhook

# Configuration with template_name
teamcity.internal.webhooks.enable=True
teamcity.internal.webhooks.events=BUILD_FINISHED
teamcity.internal.webhooks.url=http://your-server:8000/webhook/production
```

## Metric

### teamcity_build_status

**Type:** Gauge

**Description:** TeamCity build status

**Values:**
- `1` - Build successful (SUCCESS)
- `0` - Build failed (FAILURE or other status)

### Labels

The metric contains the following labels:

| Label | Description | Example Value |
|-------|-------------|---------------|
| `build_type_id` | Unique identifier of build configuration in TeamCity | `Project_BuildConfig` |
| `build_type_component` | Project component name (last element from projectName) | `Project` |
| `build_type_name` | Human-readable build configuration name | `Build Configuration` |
| `version` | Build version (build number) | `1.0.0` |
| `branch` | Branch from which build was triggered | `master` |
| `build_url` | Build configuration URL in TeamCity | `https://teamcity.example.com/buildConfiguration/Project_BuildConfig` |
| `template_name` | Template name from webhook URL path | `production`, `staging`, or `empty` (default) |
| `buildid` | Unique identifier of specific build run (passed via Pushgateway URL) | `12345` |
| `instance` | Instance name (from environment variable) | `teamcity` |
| `job` | Job name (from environment variable) | `pushgateway` |

### Metric Example

```promql
teamcity_build_status{
  build_type_id="Project_BuildConfig",
  build_type_component="Project",
  build_type_name="Build Configuration",
  version="1.0.0",
  branch="master",
  build_url="https://teamcity.example.com/buildConfiguration/Project_BuildConfig",
  template_name="production",
  buildid="12345",
  instance="teamcity",
  job="pushgateway"
} 1
```

### Pushgateway URL

Metrics are sent to URL:

```
http://pushgateway:9091/metrics/job/{job}/instance/{instance}/buildid/{build_id}
```

Where:
- `{job}` - value from `JOB_NAME` environment variable
- `{instance}` - value from `INSTANCE_NAME` environment variable
- `{build_id}` - unique build ID from TeamCity

## Prometheus Query Examples

### Example Metric

```promql
# All successful builds
teamcity_build_status{branch="master", 
                      build_type_component="Project", 
                      build_type_id="Project_BuildConfig", 
                      build_type_name="Build Configuration", 
                      build_url="https://teamcity.example.com/buildConfiguration/Project_BuildConfig", 
                      template_name="production",
                      buildid="12345", 
                      instance="teamcity", 
                      job="pushgateway", 
                      version="1.0.0"}
```

## License

MIT