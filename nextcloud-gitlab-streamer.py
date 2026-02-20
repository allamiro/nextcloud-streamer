#!/usr/bin/env python3
import base64
import json
import os
import ssl
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

# ===================== CONFIG (EDIT LOCALLY) =====================
GITLAB_URL = "http://10.0.50.10:8088"
GITLAB_TOKEN = ""  # paste token here

NEXTCLOUD_WEBHOOK = "http://nextcloudurl/ocs/v2.php/apps/spreed/api/v1/chat/t9fw9mxd"
NEXTCLOUD_USER = "gitlabbot"
NEXTCLOUD_PASSWORD = ""  # paste Nextcloud app password here

VERIFY_SSL = False
STATE_FILE = "gitlab_state.json"

PER_PAGE = 100
REQUEST_TIMEOUT = 30
LOOKBACK_HOURS_IF_EMPTY = 1

# If True: notify on updated issues/MRs (uses updated_after), else only newly created (created_after)
CHECK_UPDATES = False

# If True: skip projects whose last_activity_at is older than last_check - margin
SKIP_STALE_PROJECTS = True
STALE_SAFETY_MARGIN_SECONDS = 120
# ================================================================


# ------------------------- time helpers --------------------------
def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().isoformat()


def parse_gitlab_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def fmt_utc(ts: str) -> str:
    """Format GitLab ISO timestamp into 'YYYY-MM-DD HH:MMZ' (UTC)."""
    if not ts:
        return ""
    try:
        dt = parse_gitlab_iso(ts).astimezone(timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%MZ")
    except Exception:
        return ts[:16]


def default_last_check_iso() -> str:
    return (utc_now() - timedelta(hours=LOOKBACK_HOURS_IF_EMPTY)).isoformat()


# ------------------------- state helpers -------------------------
def load_state(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {"projects": {}}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("projects"), dict):
            clean = {k: v for k, v in data["projects"].items() if isinstance(v, str)}
            return {"projects": clean}
    except Exception:
        pass
    return {"projects": {}}


def save_state_atomic(path: str, data: Dict[str, Any]) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


# -------------------------- HTTP core ----------------------------
def make_ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if not VERIFY_SSL:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def http_request(
    method: str,
    url: str,
    headers: Optional[Dict[str, str]] = None,
    params: Optional[Dict[str, Any]] = None,
    json_body: Optional[Dict[str, Any]] = None,
) -> Tuple[int, Dict[str, str], bytes]:
    if params:
        qs = urllib.parse.urlencode({k: str(v) for k, v in params.items()})
        url = url + ("&" if "?" in url else "?") + qs

    req_headers = dict(headers or {})
    data = None

    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        req_headers.setdefault("Content-Type", "application/json")

    req = urllib.request.Request(url, data=data, method=method, headers=req_headers)

    try:
        with urllib.request.urlopen(req, context=make_ssl_context(), timeout=REQUEST_TIMEOUT) as resp:
            status = int(getattr(resp, "status", 200))
            resp_headers = {k: v for (k, v) in resp.getheaders()}
            body = resp.read()
            return status, resp_headers, body
    except urllib.error.HTTPError as e:
        status = int(e.code)
        resp_headers = dict(e.headers.items()) if e.headers else {}
        body = e.read() if hasattr(e, "read") else b""
        return status, resp_headers, body


def require_config() -> None:
    missing = []
    if not GITLAB_URL.strip():
        missing.append("GITLAB_URL")
    if not GITLAB_TOKEN.strip():
        missing.append("GITLAB_TOKEN")
    if not NEXTCLOUD_WEBHOOK.strip():
        missing.append("NEXTCLOUD_WEBHOOK")
    if not NEXTCLOUD_USER.strip():
        missing.append("NEXTCLOUD_USER")
    if not NEXTCLOUD_PASSWORD.strip():
        missing.append("NEXTCLOUD_PASSWORD")

    if missing:
        print(f"❌ Missing config values: {', '.join(missing)}")
        sys.exit(1)


# --------------------- auth / headers helpers ---------------------
def gitlab_headers() -> Dict[str, str]:
    return {
        "PRIVATE-TOKEN": GITLAB_TOKEN,
        "Accept": "application/json",
        "User-Agent": "gitlab-nextcloud-poller/1.0",
    }


def basic_auth_header(user: str, password: str) -> str:
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def with_format_json(url: str) -> str:
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}format=json"


def nextcloud_headers() -> Dict[str, str]:
    return {
        "OCS-APIRequest": "true",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": basic_auth_header(NEXTCLOUD_USER, NEXTCLOUD_PASSWORD),
        "User-Agent": "gitlab-nextcloud-poller/1.0",
    }


# --------------------- message formatting (ONE LINE) --------------
def safe_one_line(s: str) -> str:
    s = (s or "").replace("\n", " ").replace("\r", " ").strip()
    return " ".join(s.split())


def fmt_labels(labels: Any) -> str:
    if not labels:
        return "-"
    if isinstance(labels, list):
        cleaned = [safe_one_line(str(x)) for x in labels if str(x).strip()]
        return ",".join(cleaned) if cleaned else "-"
    return safe_one_line(str(labels))


def md_link(text: str, url: str) -> str:
    # Nextcloud Talk supports Markdown links; this hides the raw URL.
    text = safe_one_line(text)
    url = (url or "").strip()
    if not url:
        return text
    return f"[{text}]({url})"


def compact_issue_message(issue: Dict[str, Any], project_name: str) -> str:
    iid = issue.get("iid")
    title = safe_one_line(issue.get("title") or "")
    author = (issue.get("author", {}) or {}).get("name") or "Unknown"
    created_at = fmt_utc(issue.get("created_at") or "")
    labels = fmt_labels(issue.get("labels"))
    url = issue.get("web_url") or ""
    return (
        f"🟢 ISSUE #{iid} | {project_name} | {title} | {author} | {created_at} | "
        f"labels [{labels}] | {md_link('→ View Issue', url)}"
    )


def compact_mr_message(mr: Dict[str, Any], project_name: str) -> str:
    iid = mr.get("iid")
    title = safe_one_line(mr.get("title") or "")
    author = (mr.get("author", {}) or {}).get("name") or "Unknown"
    created_at = fmt_utc(mr.get("created_at") or "")
    url = mr.get("web_url") or ""
    return (
        f"🟣 MR !{iid} | {project_name} | {title} | {author} | {created_at} | "
        f"{md_link('→ View MR', url)}"
    )


def compact_commit_message(commit: Dict[str, Any], project_name: str) -> str:
    title = safe_one_line(commit.get("title") or "")
    author = commit.get("author_name") or "Unknown"
    short_id = commit.get("short_id") or ""
    created_at = fmt_utc(commit.get("created_at") or "")
    url = commit.get("web_url") or ""
    return (
        f"💾 COMMIT | {project_name} | {title} | {author} | {short_id} | {created_at} | "
        f"{md_link('→ View Commit', url)}"
    )


def compact_test_message() -> str:
    return f"✅ TEST OK | GitLab → Nextcloud | {utc_now_iso()}"


# --------------------- Nextcloud notification ---------------------
def send_to_nextcloud(message: str) -> None:
    url = with_format_json(NEXTCLOUD_WEBHOOK)
    status, resp_headers, body = http_request(
        "POST",
        url,
        headers=nextcloud_headers(),
        json_body={"message": message},
    )
    if status >= 400:
        print("❌ Nextcloud send failed")
        print("   Status:", status)
        print("   Server:", resp_headers.get("Server") or resp_headers.get("server"))
        if "cf-ray" in {k.lower(): v for k, v in resp_headers.items()}:
            print("   Cloudflare:", resp_headers.get("cf-ray") or resp_headers.get("CF-RAY"))
        print("   Content-Type:", resp_headers.get("Content-Type") or resp_headers.get("content-type"))
        print("   Body prefix:", body[:200])
        raise RuntimeError(f"Nextcloud send failed: {status} {body[:200]!r}")

    print(f"✅ Sent: {message}")


# -------------------------- GitLab API ----------------------------
def gitlab_get_paginated(path: str, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    url = f"{GITLAB_URL}/api/v4{path}"
    all_items: List[Dict[str, Any]] = []
    page = 1

    base = dict(params or {})
    base.setdefault("per_page", PER_PAGE)

    while True:
        p = dict(base)
        p["page"] = page

        status, headers, body = http_request("GET", url, headers=gitlab_headers(), params=p)
        if status in (404, 410):
            return []
        if status >= 400:
            raise RuntimeError(f"GitLab GET {path} failed: {status} {body[:200]!r}")

        batch = json.loads(body.decode("utf-8") or "[]")
        if not isinstance(batch, list):
            break

        all_items.extend(batch)

        next_page = headers.get("X-Next-Page") or headers.get("x-next-page")
        if not next_page:
            break
        page = int(next_page)

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


def event_params(last_check_iso: str) -> Dict[str, str]:
    return {"updated_after": last_check_iso} if CHECK_UPDATES else {"created_after": last_check_iso}


def should_skip_project(project: Dict[str, Any], last_check_iso: str) -> bool:
    if not SKIP_STALE_PROJECTS:
        return False
    la = project.get("last_activity_at")
    if not la:
        return False
    try:
        last_activity = parse_gitlab_iso(la)
        last_check = parse_gitlab_iso(last_check_iso)
        return last_activity < (last_check - timedelta(seconds=STALE_SAFETY_MARGIN_SECONDS))
    except Exception:
        return False


# ---------------------------- checks ------------------------------
def check_issues(project: Dict[str, Any], last_check_iso: str) -> None:
    pid = project["id"]
    pname = project.get("name_with_namespace") or project.get("path_with_namespace") or str(pid)

    items = gitlab_get_paginated(
        f"/projects/{pid}/issues",
        params={**event_params(last_check_iso), "scope": "all"},
    )
    for issue in items:
        send_to_nextcloud(compact_issue_message(issue, pname))


def check_merge_requests(project: Dict[str, Any], last_check_iso: str) -> None:
    pid = project["id"]
    pname = project.get("name_with_namespace") or project.get("path_with_namespace") or str(pid)

    items = gitlab_get_paginated(
        f"/projects/{pid}/merge_requests",
        params={**event_params(last_check_iso), "scope": "all"},
    )
    for mr in items:
        send_to_nextcloud(compact_mr_message(mr, pname))


def check_commits(project: Dict[str, Any], last_check_iso: str) -> None:
    pid = project["id"]
    pname = project.get("name_with_namespace") or project.get("path_with_namespace") or str(pid)
    last_check_dt = parse_gitlab_iso(last_check_iso)

    items = gitlab_get_paginated(
        f"/projects/{pid}/repository/commits",
        params={"since": last_check_iso, "with_stats": "false"},
    )

    for commit in reversed(items):  # oldest first
        created_at = commit.get("created_at")
        if not created_at:
            continue
        try:
            created_dt = parse_gitlab_iso(created_at)
        except Exception:
            continue
        if created_dt <= last_check_dt:
            continue

        send_to_nextcloud(compact_commit_message(commit, pname))


# --------------------------- run modes ----------------------------
def test_mode() -> None:
    require_config()
    print("🧪 === TEST MODE ===\n")

    status, _, body = http_request("GET", f"{GITLAB_URL}/api/v4/version", headers=gitlab_headers())
    if status >= 400:
        raise RuntimeError(f"GitLab test failed: {status} {body[:200]!r}")
    version = json.loads(body.decode("utf-8")).get("version", "Unknown")
    print(f"✅ GitLab API OK (v{version})")

    send_to_nextcloud(compact_test_message())
    print("✅ Test message sent.")


def normal_mode() -> None:
    require_config()
    print("--- Starting GitLab → Nextcloud Poller ---")

    state = load_state(STATE_FILE)
    proj_state: Dict[str, str] = state.get("projects", {})

    run_time_iso = utc_now_iso()
    projects = get_all_projects()

    active_ids = {str(p["id"]) for p in projects if p.get("id")}
    proj_state = {k: v for k, v in proj_state.items() if k in active_ids}

    processed = skipped = 0

    for p in projects:
        pid = p.get("id")
        if not pid:
            continue
        key = str(pid)
        pname = p.get("name_with_namespace") or p.get("path_with_namespace") or key

        last_check = proj_state.get(key) or default_last_check_iso()

        if should_skip_project(p, last_check):
            skipped += 1
            continue

        print(f"🔎 Checking {pname} (id={pid}) since {last_check[:19]}...")
        try:
            check_issues(p, last_check)
            check_merge_requests(p, last_check)
            check_commits(p, last_check)

            proj_state[key] = run_time_iso  # advance only on success
            processed += 1
        except Exception as e:
            print(f"❌ Error in {pname}: {e}")

    save_state_atomic(STATE_FILE, {"projects": proj_state})
    print(f"\n--- Finished — Processed: {processed} | Skipped stale: {skipped} | Total: {len(projects)} ---")


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--test":
        test_mode()
    else:
        normal_mode()


if __name__ == "__main__":
    main()
