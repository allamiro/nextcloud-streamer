import requests
import json
import os
from datetime import datetime, timezone, timedelta

# ================= CONFIGURATION =================
# GitLab Settings
GITLAB_URL = "http://192.168.1.50"
GITLAB_TOKEN = "glpat-xxxxxxxxxxxxxxxxxxxx" # Needs 'api' or 'read_api' scope
PROJECT_ID = "5"                            # REQUIRED: The ID of the project you want to check
                                            # (Find this under the Project Name in GitLab)

# Nextcloud Settings
NEXTCLOUD_WEBHOOK = "https://192.168.1.60/ocs/v2.php/apps/spreed/api/v1/chat/YOUR_CONVERSATION_TOKEN"
NEXTCLOUD_USER = "your_nextcloud_username"
NEXTCLOUD_PASSWORD = "your_nextcloud_password"
VERIFY_SSL = False                          # Set to True in production

# Script Settings
STATE_FILE = "gitlab_state.json"            # Stores the timestamp of the last run
# =================================================

def get_gitlab_headers():
    return {"PRIVATE-TOKEN": GITLAB_TOKEN}

def get_nextcloud_headers():
    return {
        "OCS-APIRequest": "true",
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain"
    }

def load_state():
    """Load the last check time from a file."""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f:
                data = json.load(f)
                return data.get('last_check')
        except:
            pass
    
    # Default: If no state file, look back 1 hour
    default_time = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    return default_time

def save_state(timestamp):
    """Save the current time as the last check time."""
    with open(STATE_FILE, 'w') as f:
        json.dump({'last_check': timestamp}, f)

def send_to_nextcloud(message):
    """Send message to Nextcloud."""
    try:
        payload = {"message": message}
        r = requests.post(
            NEXTCLOUD_WEBHOOK,
            json=payload,
            auth=(NEXTCLOUD_USER, NEXTCLOUD_PASSWORD),
            headers=get_nextcloud_headers(),
            verify=VERIFY_SSL
        )
        r.raise_for_status()
        print(f"✅ Sent to Nextcloud: {message.split(chr(10))[0]}...") # Print first line only
    except Exception as e:
        print(f"❌ Error sending to Nextcloud: {e}")

def check_issues(last_check):
    """Poll API for issues created after the last check."""
    url = f"{GITLAB_URL}/api/v4/projects/{PROJECT_ID}/issues"
    params = {
        'created_after': last_check,
        'scope': 'all'
    }
    
    try:
        r = requests.get(url, headers=get_gitlab_headers(), params=params, verify=VERIFY_SSL)
        r.raise_for_status()
        issues = r.json()

        for issue in issues:
            # Check if actually new (API returns >= date, so we might get duplicates from the exact second)
            if issue['created_at'] > last_check:
                msg = (f"[NEW ISSUE] 🟢\n"
                       f"Title: {issue.get('title')} (#{issue.get('iid')})\n"
                       f"Author: {issue['author']['name']}\n"
                       f"URL: {issue.get('web_url')}")
                send_to_nextcloud(msg)
    except Exception as e:
        print(f"Error checking issues: {e}")

def check_merge_requests(last_check):
    """Poll API for MRs created after the last check."""
    url = f"{GITLAB_URL}/api/v4/projects/{PROJECT_ID}/merge_requests"
    params = {
        'created_after': last_check,
        'scope': 'all'
    }

    try:
        r = requests.get(url, headers=get_gitlab_headers(), params=params, verify=VERIFY_SSL)
        r.raise_for_status()
        mrs = r.json()

        for mr in mrs:
            if mr['created_at'] > last_check:
                msg = (f"[NEW MR] 🔀\n"
                       f"Title: {mr.get('title')} (!{mr.get('iid')})\n"
                       f"Author: {mr['author']['name']}\n"
                       f"URL: {mr.get('web_url')}")
                send_to_nextcloud(msg)
    except Exception as e:
        print(f"Error checking MRs: {e}")

def check_commits(last_check):
    """Poll API for commits pushed after the last check."""
    url = f"{GITLAB_URL}/api/v4/projects/{PROJECT_ID}/repository/commits"
    params = {
        'since': last_check,
        'with_stats': 'false'
    }

    try:
        r = requests.get(url, headers=get_gitlab_headers(), params=params, verify=VERIFY_SSL)
        r.raise_for_status()
        commits = r.json()

        # Commits API returns newest first, reverse to send in chronological order
        for commit in reversed(commits):
            # API 'since' is inclusive, filter strictly greater
            if commit['created_at'] > last_check:
                msg = (f"[NEW COMMIT] 💾\n"
                       f"Message: {commit.get('message').strip()}\n"
                       f"Author: {commit.get('author_name')}\n"
                       f"ID: {commit.get('short_id')}\n"
                       f"URL: {commit.get('web_url')}")
                send_to_nextcloud(msg)
    except Exception as e:
        print(f"Error checking commits: {e}")

def main():
    print("--- Starting GitLab Poller ---")
    
    # 1. Get the time of the last successful run
    last_check_time = load_state()
    print(f"Searching for items created after: {last_check_time}")

    # 2. Capture current time for the *next* run
    # We use UTC to match GitLab's standard time format
    current_run_time = datetime.now(timezone.utc).isoformat()

    # 3. Poll the APIs
    check_issues(last_check_time)
    check_merge_requests(last_check_time)
    check_commits(last_check_time)

    # 4. Save state for next time
    save_state(current_run_time)
    print("--- Check Complete ---")

if __name__ == "__main__":
    main()
