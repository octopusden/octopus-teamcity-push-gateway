#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DORA Lead Time for Changes - gateway-side enrichment.

When the /jenkins endpoint persists a REAL prod deploy row
(environment=production, dry_run=false, status=SUCCESS), the gateway kicks this
enrichment on a background thread. It walks Bitbucket to find the tasks carried
by that deploy and their merge-into-staging times, then writes one
`deploy_lead_time` point per task to the Jenkins bucket.

    lead_seconds = deploy_start_time - task_merge_into_staging_time

Flow is SHA-based (no date windows):
    currSHA (deployed master HEAD = payload git_commit)
      -> GET /commits/{sha}/pull-requests        (the staging->master PR)
      -> GET /pull-requests/{M}/commits          (filter "Pull request #N:" = tasks)
      -> each task squash commit's committerTimestamp = merge-into-staging time

All connection details (Bitbucket base URL, token, project, the component->repo
map, timeouts, the feature flag) come from environment variables - NOTHING is
hard-coded here. See the config block below and the README.

Anchor = merge-into-staging (delivery lead time), per the DORA epic decision.
"""

import os
import re
import json
import threading

import requests
import structlog

logger = structlog.get_logger("teamcity-push-gateway")

# --- config (all via environment; no secrets or hosts in code) ----------------

# Master switch. Enrichment stays off until explicitly enabled.
LEAD_TIME_ENABLED = os.getenv("LEAD_TIME_ENABLED", "false").strip().lower() == "true"

# Bitbucket connection. Provide these via env / secrets; leaving any unset disables enrichment.
BITBUCKET_URL     = os.getenv("BITBUCKET_URL", "").rstrip("/")   # e.g. https://bitbucket.example.com
BITBUCKET_TOKEN   = os.getenv("BITBUCKET_TOKEN", "")             # read-scoped HTTP access token
BITBUCKET_PROJECT = os.getenv("BITBUCKET_PROJECT", "")           # project key, e.g. CDT-SI
BITBUCKET_TIMEOUT = float(os.getenv("BITBUCKET_TIMEOUT", "20"))
BITBUCKET_VERIFY_SSL = os.getenv("BITBUCKET_VERIFY_SSL", "true").strip().lower() != "false"

# Target measurement (written to the Jenkins bucket by the caller's writer).
LEAD_TIME_MEASUREMENT = os.getenv("LEAD_TIME_MEASUREMENT", "deploy_lead_time")

# Which deploy this instance counts, so a prod gateway and a staging gateway can run the
# same code against different branches/environments:
#   LEAD_TIME_DEPLOY_BRANCH  - the branch a deploy targets (prod: master, staging: staging).
#   LEAD_TIME_ANCHOR_BRANCH  - the integration branch tasks first merge into; the "committed"
#                              anchor is the merge into THIS branch (default staging).
#   LEAD_TIME_ENVIRONMENT    - the environment value this instance's deploys carry; it is both
#                              the trigger (only these deploys enrich) and the tag written.
# When deploy-branch == anchor-branch (staging gateway) each deploy PR IS the task unit (one
# level). When they differ (prod: master wraps the staging merges) the enricher descends into
# the deploy PR's commits for the nested per-task merges (two levels).
LEAD_TIME_DEPLOY_BRANCH = os.getenv("LEAD_TIME_DEPLOY_BRANCH", "master")
LEAD_TIME_ANCHOR_BRANCH = os.getenv("LEAD_TIME_ANCHOR_BRANCH", "staging")
LEAD_TIME_ENVIRONMENT   = os.getenv("LEAD_TIME_ENVIRONMENT", "production")

# component (DORA label) -> Bitbucket repo slug. No defaults - supply the whole map via
# LEAD_TIME_COMPONENT_REPOS, a JSON object, e.g.
#   {"component1": "repo1", "component2": "repo2"}
# When unset or invalid the map is empty and enrichment skips every component (logged).
def _load_component_repos():
    raw = os.getenv("LEAD_TIME_COMPONENT_REPOS")
    if not raw:
        return {}
    try:
        mapping = json.loads(raw)
        if isinstance(mapping, dict) and mapping:
            return {str(k): str(v) for k, v in mapping.items()}
        logger.error("LEAD_TIME_COMPONENT_REPOS is not a non-empty JSON object, map is empty")
    except ValueError as e:
        logger.error(f"LEAD_TIME_COMPONENT_REPOS is not valid JSON ({e}), map is empty")
    return {}


COMPONENT_REPOS = _load_component_repos()

_PR_MSG_RE = re.compile(r"Pull request #(\d+):")
# Issue-key pattern pulled from the PR/commit message (e.g. "CD-5946"). Override with
# LEAD_TIME_ISSUE_PATTERN; default = 2+ uppercase letters, a dash, digits (ignores "Feature/").
_ISSUE_RE = re.compile(os.getenv("LEAD_TIME_ISSUE_PATTERN", r"[A-Z]{2,}-\d+"))


# --- Bitbucket REST ----------------------------------------------------------

def _bb_get(path, params=None):
    url = f"{BITBUCKET_URL}/rest/api/1.0/{path}"
    resp = requests.get(
        url,
        headers={"Authorization": f"Bearer {BITBUCKET_TOKEN}"},
        params=params,
        timeout=BITBUCKET_TIMEOUT,
        verify=BITBUCKET_VERIFY_SSL,
    )
    resp.raise_for_status()
    return resp.json()


def _deploy_prs(repo, cur_sha):
    """currSHA is the deploy-branch squash commit -> the PR(s) that introduced it, keeping
    only those whose target is LEAD_TIME_DEPLOY_BRANCH. Returns the full PR objects."""
    data = _bb_get(f"projects/{BITBUCKET_PROJECT}/repos/{repo}/commits/{cur_sha}/pull-requests")
    return [pr for pr in data.get("values", [])
            if pr.get("toRef", {}).get("displayId") == LEAD_TIME_DEPLOY_BRANCH]


def _pr_as_task(pr):
    """One-level case (deploy branch == anchor branch): the deploy PR itself is the task unit.
    Its closedDate is the merge-into-anchor time; the Jira key comes from its title."""
    title = pr.get("title") or ""
    issue = _ISSUE_RE.search(title)
    return {
        "pr_id":     str(pr.get("id")),
        "merge_ms":  pr.get("closedDate"),
        "sha":       (pr.get("fromRef", {}).get("latestCommit") or "")[:11],
        "title":     title,
        "issue_key": issue.group(0) if issue else "",
    }


def _task_merges(repo, pr_id):
    """Tasks carried by a staging->master PR.

    Returns a list of dicts {pr_id, merge_ms, sha, title, issue_key}. Filters the PR's
    commits to the per-task squash commits ("Pull request #N:"); each such commit's
    committerTimestamp == that task's merge-into-staging time. issue_key is the Jira key
    parsed from the message (e.g. "CD-5946"), or "" when none is found.
    """
    out, start = [], 0
    while True:
        data = _bb_get(
            f"projects/{BITBUCKET_PROJECT}/repos/{repo}/pull-requests/{pr_id}/commits",
            params={"limit": 100, "start": start},
        )
        for c in data.get("values", []):
            msg = c.get("message", "") or ""
            first = msg.splitlines()[0] if msg else ""
            m = _PR_MSG_RE.match(first)
            if m and m.group(1) != str(pr_id):      # skip the outer staging->master PR itself
                issue = _ISSUE_RE.search(first)
                out.append({
                    "pr_id":     m.group(1),
                    "merge_ms":  c.get("committerTimestamp"),
                    "sha":       c.get("displayId") or (c.get("id", "") or "")[:11],
                    "title":     first,
                    "issue_key": issue.group(0) if issue else "",
                })
        if data.get("isLastPage", True):
            break
        start = data.get("nextPageStart", 0)
    return out


# --- line protocol -----------------------------------------------------------

def _esc_tag(value):
    return str(value).replace('\\', '\\\\').replace(',', '\\,').replace('=', '\\=').replace(' ', '\\ ')


def _esc_str(value):
    return str(value).replace('\\', '\\\\').replace('"', '\\"')


def _build_line(component, pr_id, sha, merge_ms, deploy_ms, lead_s, ts_ns, issue_key=""):
    """One deploy_lead_time point.

    ts_ns carries a per-task offset: every task of a deploy shares the same tagset
    (component/environment/dry_run) and the same deploy instant, and in InfluxDB
    measurement+tagset+timestamp is the point identity, so without the offset the
    rows would overwrite each other. pr_id and issue_key are FIELDS (not tags) to
    avoid unbounded series cardinality.
    """
    tags = (
        f"{LEAD_TIME_MEASUREMENT}"
        f",build_type_component={_esc_tag(component)}"
        f",environment={_esc_tag(LEAD_TIME_ENVIRONMENT)},dry_run=false"
    )
    fields = (
        f"lead_seconds={lead_s:.3f}"
        f",merge_time={int(merge_ms)}i"
        f",deploy_time={int(deploy_ms)}i"
        f',pr_id="{_esc_str(pr_id)}"'
        f',commit_sha="{_esc_str(sha)}"'
        f',issue_key="{_esc_str(issue_key)}"'
    )
    return f"{tags} {fields} {ts_ns}"


def compute_lead_lines(component, cur_sha, deploy_ms):
    """Pure compute: returns the deploy_lead_time line-protocol rows for one deploy."""
    repo = COMPONENT_REPOS.get(component)
    if not repo:
        logger.warning(f"lead_time: no repo mapping for component '{component}', skipping")
        return []

    deploy_ms = int(deploy_ms)
    prs = _deploy_prs(repo, cur_sha)
    if not prs:
        logger.warning(f"lead_time: no {LEAD_TIME_DEPLOY_BRANCH} PR for {cur_sha} ({repo})")
        return []

    one_level = LEAD_TIME_DEPLOY_BRANCH == LEAD_TIME_ANCHOR_BRANCH
    lines, i = [], 0
    for pr in prs:
        # one level (staging gateway): the deploy PR itself is the task; two levels (prod):
        # descend into the deploy PR's commits for the nested per-task merges into the anchor.
        tasks = [_pr_as_task(pr)] if one_level else _task_merges(repo, pr["id"])
        for task in tasks:
            merge_ms = task["merge_ms"]
            if not merge_ms:
                continue
            lead_s = (deploy_ms - merge_ms) / 1000.0
            if lead_s < 0:                               # clock skew / odd history
                logger.warning(
                    f"lead_time: negative lead for PR#{task['pr_id']} ({lead_s:.0f}s), skipping")
                continue
            ts_ns = deploy_ms * 1_000_000 + i            # per-task offset -> unique point
            i += 1
            lines.append(_build_line(component, task["pr_id"], task["sha"],
                                     merge_ms, deploy_ms, lead_s, ts_ns,
                                     issue_key=task.get("issue_key", "")))
    return lines


# --- orchestration -----------------------------------------------------------

def is_countable_deploy(parsed):
    """A row is a countable deploy when its environment matches LEAD_TIME_ENVIRONMENT (production
    by default, staging on a staging gateway) AND dry_run=false AND SUCCESS."""
    return (parsed.get("environment") == LEAD_TIME_ENVIRONMENT
            and str(parsed.get("dry_run")) == "false"
            and parsed.get("status") == "SUCCESS")


def _run_enrichment(component, cur_sha, deploy_ms, writer):
    try:
        lines = compute_lead_lines(component, cur_sha, deploy_ms)
        for line in lines:
            writer(line)
        logger.info(f"lead_time: wrote {len(lines)} deploy_lead_time rows for {component}")
    except Exception as e:                               # never let enrichment surface to the request
        logger.error(f"lead_time enrichment failed for {component}/{cur_sha}: {e}")


def maybe_enrich_lead_time(parsed, writer):
    """Fire-and-forget lead-time enrichment for a parsed Jenkins deploy row.

    Spawns a daemon thread so the /jenkins request returns immediately and a
    Bitbucket hiccup never affects the deploy metric.

    :param parsed: the dict from parse_jenkins_payload.
    :param writer: one-arg callable writing a single line-protocol row to the
                   Jenkins bucket, e.g. lambda l: send_to_influxdb(l, bucket=INFLUXDB_JENKINS_BUCKET)
    """
    if not LEAD_TIME_ENABLED:
        return
    if not is_countable_deploy(parsed):
        return
    if not (BITBUCKET_URL and BITBUCKET_TOKEN and BITBUCKET_PROJECT):
        logger.warning("lead_time: enabled but BITBUCKET_URL/TOKEN/PROJECT not fully set, skipping")
        return

    component = parsed.get("build_type_component")
    cur_sha = parsed.get("git_commit")
    deploy_ms = parsed.get("start_time")
    if not cur_sha:
        logger.warning("lead_time: deploy row has no git_commit, cannot anchor, skipping")
        return
    if deploy_ms is None:
        logger.warning("lead_time: deploy row has no start_time, cannot compute lead, skipping")
        return

    threading.Thread(
        target=_run_enrichment,
        args=(component, cur_sha, deploy_ms, writer),
        name=f"lead-time-{component}",
        daemon=True,
    ).start()


# --- standalone dry-run (prints line protocol, writes nothing) ---------------
# Usage (supply the component->repo map; there are no built-in defaults):
#   BITBUCKET_URL=... BITBUCKET_TOKEN=... BITBUCKET_PROJECT=... \
#   LEAD_TIME_COMPONENT_REPOS='{"component1":"repo1"}' \
#   python app/lead_time.py --component component1 --sha <deployed-master-HEAD-sha>
if __name__ == "__main__":
    import time
    import argparse

    ap = argparse.ArgumentParser(
        description="DORA lead-time enrichment dry-run (prints line protocol, writes nothing)")
    ap.add_argument("--component", required=True,
                    help="DORA component label; must be a key in LEAD_TIME_COMPONENT_REPOS")
    ap.add_argument("--sha", required=True, help="deployed master HEAD commit (currSHA)")
    ap.add_argument("--deploy-ms", type=int, default=int(time.time() * 1000),
                    help="deploy start_time epoch ms (default: now)")
    args = ap.parse_args()
    if not (BITBUCKET_URL and BITBUCKET_TOKEN and BITBUCKET_PROJECT):
        raise SystemExit("set BITBUCKET_URL, BITBUCKET_TOKEN and BITBUCKET_PROJECT")
    if not COMPONENT_REPOS:
        raise SystemExit("set LEAD_TIME_COMPONENT_REPOS (JSON component->repo map); it is empty")
    if args.component not in COMPONENT_REPOS:
        raise SystemExit(f"component '{args.component}' not in LEAD_TIME_COMPONENT_REPOS "
                         f"(keys: {', '.join(sorted(COMPONENT_REPOS)) or 'none'})")
    rows = compute_lead_lines(args.component, args.sha, args.deploy_ms)
    print(f"{len(rows)} deploy_lead_time rows (dry-run, not written):")
    for _ln in rows:
        print(_ln)
