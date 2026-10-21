# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this is

A Flask service that receives build-finished webhooks from TeamCity, plus JSON posts from
Jenkins, and writes one InfluxDB v2 line-protocol point per request. Despite the repo name,
it does **not** use the Prometheus Pushgateway any more: the datastore is InfluxDB.

It is a single file, `app/main.py`, packaged as a Docker image (`python:3.12-slim`). It runs
on the Flask built-in server on `PORT` (8000). There are no tests.

## Run locally

```bash
pip install -r requirements.txt
INFLUXDB_URL=http://localhost:8086 INFLUXDB_TOKEN=... INFLUXDB_ORG=my-org LOG_FORMAT=text \
  python app/main.py
```

| Env | Default | Meaning |
|---|---|---|
| `INFLUXDB_URL` | `http://localhost:8086` | InfluxDB base URL |
| `INFLUXDB_TOKEN` | `""` | sent as `Authorization: Token …` |
| `INFLUXDB_ORG` | `my-org` | |
| `INFLUXDB_BUCKET` | `teamcity` | bucket for TeamCity points |
| `INFLUXDB_JENKINS_BUCKET` | `jenkins` | bucket for Jenkins points; must exist |
| `JENKINS_HOST_SUFFIX` | `""` | stripped from the Jenkins URL |
| `PORT` | `8000` | |
| `LOG_LEVEL` / `LOG_FORMAT` | `info` / `json` | `LOG_FORMAT` must be `json` or `text` |

## Request flow

- `POST /webhook` (and `/webhook/<name>`; **the path segment is ignored**):
  `parse_teamcity_payload()` → `build_line_protocol()` → `send_to_influxdb()`.
- `POST /jenkins`: `build_jenkins_line_protocol()` → Jenkins bucket.
- The response is 400 when the body has no JSON, and 500 on any error, including a non-2xx
  answer from InfluxDB.
- The timestamp is the **receive time**, not the build finish time.

## Series are a public contract

Dashboards and joins downstream use these names. Renaming a measurement, tag or field, or
turning a field into a tag or the other way round, is a breaking change: call it out in the
PR and the release notes.

**`teamcity_build_status`** (bucket `INFLUXDB_BUCKET`)
- tags:
  - `build_type_id`
  - `build_type_component`: last ` / ` segment of the project name
  - `build_type_name`
  - `branch`: `unknown` if missing
  - `template_name`: from build property `MONITORING_TEMPLATE_ID`, default `empty`
  - `project_id`
  - `default_branch`: `true`/`false`/`unknown`
  - `metric_component_name`: from build property `COMPONENT_NAME`, default `unknown`
- fields:
  - `status_value` (int, 1 = SUCCESS)
  - `status`
  - `version` (build number)
  - `build_url` (build configuration URL)
  - `build_id`
  - optional `duration_seconds`

**`jenkins_build_status`** (bucket `INFLUXDB_JENKINS_BUCKET`)
- tags: `build_type_id`, `build_type_component`, `build_type_name`, `branch`,
  `template_name`, `environment`, `dry_run`
- fields:
  - `status_value`, `status`, `version`, `build_url`, `jenkins_instance`, `build_id`
  - optional `image_tag`, `git_commit`, `duration_seconds`, `start_time`

Rules when changing them:
- **Tags add series cardinality.** Put high-cardinality values (IDs, URLs, versions) in fields.
- **Never emit an empty tag value.** Line protocol rejects `tag=`, so the whole point is
  dropped and the webhook returns 500. Default missing values to `unknown`, as `branch`,
  `metric_component_name` and `default_branch` already do. `build_type_id`,
  `build_type_name`, `build_type_component` and `project_id` still default to an empty
  string, which is a latent bug.

## CI / release

- `.github/workflows/on-push.yml`: every push builds `ghcr.io/octopusden/octopus-teamcity-push-gateway:<branch>`.
- `.github/workflows/on-release.yml`: publishing a GitHub release builds `:<tag>` and `:latest`.
- Release tags follow `release-X.Y.Z` (semver).
- Consumers usually pull images with `IfNotPresent`, so always release a new tag. Never
  re-publish an existing one.

## Conventions

- Branch names describe the change (e.g. `add-queue-metric`).
- Releases use semver: a new metric, field or env var is minor; a fix is patch; a rename or
  removal is major.

- Commit messages: a single imperative sentence, no trailers (no `Co-Authored-By`).
- This repository is public. Keep code, comments, commits and docs free of
  organisation-specific hostnames, project names and credentials.
