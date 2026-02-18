import requests
import json
import os
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional

# ================= CONFIGURATION =================
# GitLab Settings
GITLAB_URL = "http://192.168.1.50"
GITLAB_TOKEN = "glpat-xxxxxxxxxxxxxxxxxxxx"  # Needs 'read_api' (or 'api')

# Nextcloud Settings (Talk webhook)
NEXTCLOUD_WEBHOOK = "https://192.168.1.60/ocs/v2.php/apps/spreed/api/v1/chat/YOUR_CONVERSATION_TOKEN"
NEXTCLOUD_USER = "your_nextcloud_username"
NEXTCLOUD_PASSWORD = "your_nextcloud_password"
VERIFY_SSL = False  # Set True in production with valid certs

# Script Settings
STATE_FILE = "gitlab_state.json"         # Per-project timestamps
LOOKBACK_HOURS_IF_EMPTY = 1              # First run per project
PER_PAGE = 100                           # GitLab pagination size
REQUEST_TIMEOUT = 30                     # Seconds
SKIP_STALE_PROJECTS = True               # Speed optimization
STALE_SAFETY_MARGIN_SECONDS = 120        # Only skip if last_activity is older than last_check by this margin
LOG_ERRORS = True                        # Print errors instead of silently swallowing
# =================================================


def get_gitlab_headers() -> Dict[str, str]:
    return {"PRIVATE-TOKEN": GITLAB_TOKEN}


def get_nextcloud_headers() -> Dict[str, str]:
    return {
        "OCS-APIRequest": "true",
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain",
    }


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_last_check_iso() -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS_IF_EMPTY)).isoformat()


def load_state() -> Dict[str, str]:
    """
    State format:
      {
        "projects": {
          "12": "2026-02-18T20:00:00+00:00",
          "57": "..."
        }
      }
    Backward compatible with older format: {"last_check": "..."}.
    """
    if not os.path.exists(STATE_FILE):
        return {"projects": {}}

    try:
        with open(STATE_FILE, "r") as f:
            data = json.load(f)

        # Backward compatible: old single timestamp
        if isinstance(data, dict) and "last_check" in data and isinstance(data["last_check"], str):
            return {"projects": {"_global": data["last_check"]}}

        if isinstance(data, dict) and isinstance(data.get("projects"), dict):
            # Ensure values are strings
            clean = {k: v for k, v in data["projects"].items() if isinstance(v, str)}
            return {"projects": clean}

    except Exception:
        pass

    return {"projects": {}}


def save_state(state: Dict[str, Any]) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def log_err(msg: str) -> None:
    if LOG_ERRORS:
        print(msg)


def send_to_nextcloud(message: str) -> None:
    try:
        payload = {"message": message}
        r = requests.post(
            NEXTCLOUD_WEBHOOK,
            json=payload,
            auth=(NEXTCLOUD_USER, NEXTCLOUD_PASSWORD),
            headers=get_nextcloud_headers(),
            verify=VERIFY_SSL,
            timeout=REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        print(f"✅ Sent: {message.splitlines()[0]}...")
    except Exception as e:
        log_err(f"❌ Nextcloud send error: {e}")


def gitlab_get_paginated(path: str, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """
    Follows GitLab pagination using X-Next-Page header.
    """
    if params is None:
        params = {}
    params = dict(params)
    params.setdefault("per_page", PER_PAGE)
    params.setdefault("page", 1)

    url = f"{GITLAB_URL}/api/v4{path}"
    all_items: List[Dict[str, Any]] = []

    while True:
        r = requests.get(
            url,
            headers=get_gitlab_headers(),
            params=params,
            verify=VERIFY_SSL,
            timeout=REQUEST_TIMEOUT,
        )

        # If endpoint doesn't exist or repo is empty, treat as no items
        if r.status_code in (404, 410):
            return []

        r.raise_for_status()
        batch = r.json()
        if isinstance(batch, list):
            all_items.extend(batch)
        else:
            break

        next_page = r.headers.get("X-Next-Page")
        if not next_page:
            break
        params["page"] = int(next_page)

    return all_items


def get_all_projects() -> List[Dict[str, Any]]:
    """
    Fetch projects the token user is a member of.
    """
    print("⏳ Fetching project list...")
    projects = gitlab_get_paginated(
        "/projects",
        params={
            "membership": True,
            "simple": True,
            "archived": False,
            "order_by": "last_activity_at",
            "sort": "desc",
        },
    )
    print(f"✅ Found {len(projects)} projects.")
    return projects


def project_key(project_id: int) -> str:
    return str(project_id)


def should_skip_project(project: Dict[str, Any], last_check: str) -> bool:
    """
    Speed optimization: skip if project last_activity_at is clearly older than last_check.
    Adds a safety margin to reduce risk of skipping due to clock skew / lag.
    """
    if not SKIP_STALE_PROJECTS:
        return False

    last_activity = project.get("last_activity_at")
    if not last_activity or not isinstance(last_activity, str):
        return False

    # Compare ISO8601 strings works lexicographically when both are normalized (GitLab uses ISO UTC Z).
    # To add safety margin, do minimal parsing only when needed.
    try:
        la = datetime.fromisoformat(last_activity.replace("Z", "+00:00"))
        lc = datetime.fromisoformat(last_check.replace("Z", "+00:00"))
        return la < (lc - timedelta(seconds=STALE_SAFETY_MARGIN_SECONDS))
    except Exception:
        return False


def check_issues(project: Dict[str, Any], last_check: str) -> None:
    pid = project["id"]
    pname = project.get("name_with_namespace") or project.get("path_with_namespace") or str(pid)

    items = gitlab_get_paginated(
        f"/projects/{pid}/issues",
        params={"created_after": last_check, "scope": "all"},
    )

    for issue in items:
        created_at = issue.get("created_at", "")
        if created_at > last_check:
            msg = (
                f"[NEW ISSUE] 🟢\n"
                f"Project: {pname}\n"
                f"Title: {issue.get('title')} (#{issue.get('iid')})\n"
                f"Author: {issue.get('author', {}).get('name')}\n"
                f"URL: {issue.get('web_url')}"
            )
            send_to_nextcloud(msg)


def check_merge_requests(project: Dict[str, Any], last_check: str) -> None:
    pid = project["id"]
    pname = project.get("name_with_namespace") or project.get("path_with_namespace") or str(pid)

    items = gitlab_get_paginated(
        f"/projects/{pid}/merge_requests",
        params={"created_after": last_check, "scope": "all"},
    )

    for mr in items:
        created_at = mr.get("created_at", "")
        if created_at > last_check:
            msg = (
                f"[NEW MR] 🔀\n"
                f"Project: {pname}\n"
                f"Title: {mr.get('title')} (!{mr.get('iid')})\n"
                f"Author: {mr.get('author', {}).get('name')}\n"
                f"URL: {mr.get('web_url')}"
            )
            send_to_nextcloud(msg)


def check_commits(project: Dict[str, Any], last_check: str) -> None:
    pid = project["id"]
    pname = project.get("name_with_namespace") or project.get("path_with_namespace") or str(pid)

    items = gitlab_get_paginated(
        f"/projects/{pid}/repository/commits",
        params={"since": last_check, "with_stats": "false"},
    )

    # Commits are often returned newest-first; send oldest-first.
    for commit in reversed(items):
        created_at = commit.get("created_at", "")
        if created_at > last_check:
            title = (commit.get("title") or "").strip()
            short_id = commit.get("short_id")
            msg = (
                f"[NEW COMMIT] 💾\n"
                f"Project: {pname}\n"
                f"Message: {title}\n"
                f"Author: {commit.get('author_name')}\n"
                f"ID: {short_id}\n"
                f"URL: {commit.get('web_url')}"
            )
            send_to_nextcloud(msg)


def main() -> None:
    print("--- Starting Best-of-Both Multi-Project GitLab Poller ---")

    state = load_state()
    proj_state: Dict[str, str] = state.get("projects", {})
    run_time = utc_now_iso()

    projects = get_all_projects()

    # If we loaded an old global timestamp, seed all projects with it for first run
    global_ts = proj_state.pop("_global", None)

    for project in projects:
        pid = project.get("id")
        if not pid:
            continue

        key = project_key(pid)
        pname = project.get("name_with_namespace") or project.get("path_with_namespace") or str(pid)

        last_check = proj_state.get(key) or global_ts or default_last_check_iso()

        if should_skip_project(project, last_check):
            continue

        print(f"🔎 {pname} (id={pid}) since {last_check}")

        # IMPORTANT: Only advance per-project state if checks complete successfully.
        try:
            check_issues(project, last_check)
            check_merge_requests(project, last_check)
            check_commits(project, last_check)

            proj_state[key] = run_time  # checkpoint per project
        except requests.HTTPError as e:
            # Do not advance state on errors, so we don't miss items next run
            log_err(f"❌ HTTP error for {pname}: {e}")
        except Exception as e:
            log_err(f"❌ Unexpected error for {pname}: {e}")

    save_state({"projects": proj_state})
    print("--- Check Complete ---")


if __name__ == "__main__":
    main()
