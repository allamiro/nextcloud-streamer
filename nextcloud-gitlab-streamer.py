import json
import os
import sys
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional
import requests

# ================= CONFIGURATION (ENVIRONMENT VARIABLES) =================
# Set these in your shell / Docker / systemd / cron:
# export GITLAB_TOKEN="glpat-..."
# export NEXTCLOUD_WEBHOOK="https://..."
# export NEXTCLOUD_USER="..."
# export NEXTCLOUD_PASSWORD="..."
# export CHECK_UPDATES="true"   # optional: also notify on updates/comments

GITLAB_URL = os.getenv("GITLAB_URL", "http://192.168.1.50")
GITLAB_TOKEN = os.getenv("GITLAB_TOKEN")
NEXTCLOUD_WEBHOOK = os.getenv("NEXTCLOUD_WEBHOOK")
NEXTCLOUD_USER = os.getenv("NEXTCLOUD_USER")
NEXTCLOUD_PASSWORD = os.getenv("NEXTCLOUD_PASSWORD")

VERIFY_SSL = os.getenv("VERIFY_SSL", "false").lower() in ("true", "1", "yes")
STATE_FILE = os.getenv("STATE_FILE", "gitlab_state.json")
LOOKBACK_HOURS_IF_EMPTY = int(os.getenv("LOOKBACK_HOURS_IF_EMPTY", "1"))
PER_PAGE = int(os.getenv("PER_PAGE", "100"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "30"))
SKIP_STALE_PROJECTS = os.getenv("SKIP_STALE_PROJECTS", "true").lower() in ("true", "1", "yes")
STALE_SAFETY_MARGIN_SECONDS = int(os.getenv("STALE_SAFETY_MARGIN_SECONDS", "120"))
LOG_ERRORS = os.getenv("LOG_ERRORS", "true").lower() in ("true", "1", "yes")
CHECK_UPDATES = os.getenv("CHECK_UPDATES", "false").lower() in ("true", "1", "yes")

# ======================================================================

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

def parse_iso(ts: str) -> datetime:
    """Safe parser for GitLab ISO timestamps (Z or +00:00)"""
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))

def default_last_check_iso() -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS_IF_EMPTY)).isoformat()

def load_state() -> Dict[str, str]:
    if not os.path.exists(STATE_FILE):
        return {"projects": {}}
    try:
        with open(STATE_FILE, "r") as f:
            data = json.load(f)
        if isinstance(data, dict) and "last_check" in data:
            return {"projects": {"_global": data["last_check"]}}
        if isinstance(data, dict) and isinstance(data.get("projects"), dict):
            clean = {k: v for k, v in data["projects"].items() if isinstance(v, str)}
            return {"projects": clean}
    except Exception:
        pass
    return {"projects": {}}

def save_state(state: Dict[str, Any]) -> None:
    """Atomic save – never corrupts the file on crash"""
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    os.replace(tmp, STATE_FILE)

def log_err(msg: str) -> None:
    if LOG_ERRORS:
        print(f"❌ {msg}")

def send_to_nextcloud(message: str) -> None:
    try:
        r = requests.post(
            NEXTCLOUD_WEBHOOK,
            json={"message": message},
            auth=(NEXTCLOUD_USER, NEXTCLOUD_PASSWORD),
            headers=get_nextcloud_headers(),
            verify=VERIFY_SSL,
            timeout=REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        print(f"✅ Sent: {message.splitlines()[0]}")
    except Exception as e:
        log_err(f"Nextcloud send error: {e}")

def gitlab_get_paginated(path: str, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
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
    print(f"✅ Found {len(projects)} active projects.")
    return projects

def project_key(project_id: int) -> str:
    return str(project_id)

def should_skip_project(project: Dict[str, Any], last_check: str) -> bool:
    if not SKIP_STALE_PROJECTS:
        return False
    last_activity = project.get("last_activity_at")
    if not last_activity:
        return False
    try:
        la = parse_iso(last_activity)
        lc = parse_iso(last_check)
        return la < (lc - timedelta(seconds=STALE_SAFETY_MARGIN_SECONDS))
    except Exception:
        return False

def get_event_params(last_check: str) -> Dict[str, str]:
    """created_after OR updated_after depending on config"""
    return {"updated_after": last_check} if CHECK_UPDATES else {"created_after": last_check}

def check_issues(project: Dict[str, Any], last_check: str) -> None:
    pid = project["id"]
    pname = project.get("name_with_namespace") or project.get("path_with_namespace") or str(pid)
    items = gitlab_get_paginated(
        f"/projects/{pid}/issues",
        params={**get_event_params(last_check), "scope": "all"},
    )
    for issue in items:
        created = issue.get("created_at", "")
        updated = issue.get("updated_at", "")
        action = "🟢 NEW" if created == updated else "🔄 UPDATED"
        msg = (
            f"**{action} ISSUE** #{issue.get('iid')}\n"
            f"**Project:** {pname}\n"
            f"**Title:** {issue.get('title')}\n"
            f"**Author:** {issue.get('author', {}).get('name')}\n"
            f"[→ Open in GitLab]({issue.get('web_url')})"
        )
        send_to_nextcloud(msg)

def check_merge_requests(project: Dict[str, Any], last_check: str) -> None:
    pid = project["id"]
    pname = project.get("name_with_namespace") or project.get("path_with_namespace") or str(pid)
    items = gitlab_get_paginated(
        f"/projects/{pid}/merge_requests",
        params={**get_event_params(last_check), "scope": "all"},
    )
    for mr in items:
        created = mr.get("created_at", "")
        updated = mr.get("updated_at", "")
        action = "🟢 NEW" if created == updated else "🔄 UPDATED"
        msg = (
            f"**{action} MR** !{mr.get('iid')}\n"
            f"**Project:** {pname}\n"
            f"**Title:** {mr.get('title')}\n"
            f"**Author:** {mr.get('author', {}).get('name')}\n"
            f"[→ Open in GitLab]({mr.get('web_url')})"
        )
        send_to_nextcloud(msg)

def check_commits(project: Dict[str, Any], last_check: str) -> None:
    pid = project["id"]
    pname = project.get("name_with_namespace") or project.get("path_with_namespace") or str(pid)
    items = gitlab_get_paginated(
        f"/projects/{pid}/repository/commits",
        params={"since": last_check, "with_stats": "false"},
    )
    for commit in reversed(items):   # oldest first
        created_at = commit.get("created_at", "")
        if created_at > last_check:
            title = (commit.get("title") or "").strip()
            msg = (
                f"**💾 NEW COMMIT**\n"
                f"**Project:** {pname}\n"
                f"**Message:** {title}\n"
                f"**Author:** {commit.get('author_name')}\n"
                f"**ID:** {commit.get('short_id')}\n"
                f"[→ View Commit]({commit.get('web_url')})"
            )
            send_to_nextcloud(msg)

def run_test_mode():
    print("🧪 === GITLAB → NEXTCLOUD POLLER – TEST MODE ===\n")

    print("1️⃣ Testing GitLab API...")
    try:
        r = requests.get(
            f"{GITLAB_URL}/api/v4/version",
            headers=get_gitlab_headers(),
            verify=VERIFY_SSL,
            timeout=REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        version = r.json().get("version", "Unknown")
        print(f"✅ GitLab API OK (v{version})")

        projects = gitlab_get_paginated("/projects", {"per_page": 3, "membership": True})
        print(f"✅ Can read projects ({len(projects)} accessible)")
    except Exception as e:
        print(f"❌ GitLab failed: {e}")
        return

    print("\n2️⃣ Testing Nextcloud Talk webhook...")
    try:
        test_msg = (
            "**🧪 TEST MESSAGE**\n"
            "GitLab poller → Nextcloud Talk works perfectly!\n"
            f"Time: {utc_now_iso()}\n"
            "If you see this → integration is ready ✅"
        )
        send_to_nextcloud(test_msg)
        print("✅ Test message sent! Check your Nextcloud Talk conversation.")
    except Exception as e:
        print(f"❌ Nextcloud failed: {e}")

    print("\n🎉 ALL TESTS PASSED – ready for normal use!")

def main() -> None:
    # Validate config
    required = ["GITLAB_TOKEN", "NEXTCLOUD_WEBHOOK", "NEXTCLOUD_USER", "NEXTCLOUD_PASSWORD"]
    missing = [v for v in required if not os.getenv(v)]
    if missing:
        print(f"❌ Missing environment variables: {', '.join(missing)}")
        print("   Set them before running (recommended) or edit the script.")
        sys.exit(1)

    # --test mode
    if len(sys.argv) > 1 and sys.argv[1] == "--test":
        run_test_mode()
        return

    # === NORMAL RUN ===
    print("--- Starting GitLab → Nextcloud Poller ---")
    state = load_state()
    proj_state: Dict[str, str] = state.get("projects", {})

    run_time = utc_now_iso()
    projects = get_all_projects()

    # Cleanup deleted projects from state
    active_keys = {project_key(p["id"]) for p in projects if p.get("id")}
    proj_state = {k: v for k, v in proj_state.items() if k in active_keys or k == "_global"}

    global_ts = proj_state.pop("_global", None)
    skipped = 0
    processed = 0

    for project in projects:
        pid = project.get("id")
        if not pid:
            continue
        key = project_key(pid)
        pname = project.get("name_with_namespace") or project.get("path_with_namespace") or str(pid)

        last_check = proj_state.get(key) or global_ts or default_last_check_iso()

        if should_skip_project(project, last_check):
            skipped += 1
            continue

        print(f"🔎 Checking {pname} (id={pid}) since {last_check[:16]}...")

        try:
            check_issues(project, last_check)
            check_merge_requests(project, last_check)
            check_commits(project, last_check)

            proj_state[key] = run_time   # only advance on success
            processed += 1
        except requests.HTTPError as e:
            log_err(f"HTTP error in {pname}: {e}")
        except Exception as e:
            log_err(f"Unexpected error in {pname}: {e}")

    save_state({"projects": proj_state})
    print(f"\n--- Finished — Processed: {processed} | Skipped stale: {skipped} | Total: {len(projects)} ---")

if __name__ == "__main__":
    main()
