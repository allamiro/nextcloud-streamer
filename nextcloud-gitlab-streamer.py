import requests
import json
import os
from datetime import datetime, timedelta
from flask import Flask, request, jsonify

app = Flask(__name__)

# ================= CONFIGURATION =================
# GitLab Settings
GITLAB_URL = "http://192.168.1.50"  # Base URL of your GitLab instance
GITLAB_TOKEN = "glpat-xxxxxxxxxxxxxxxxxxxx"  # Not needed for webhook receiver, but kept if needed for additional API calls

# Nextcloud Settings
NEXTCLOUD_WEBHOOK = "https://192.168.1.60/ocs/v2.php/apps/spreed/api/v1/chat/YOUR_CONVERSATION_TOKEN"
NEXTCLOUD_USER = "your_nextcloud_username"
NEXTCLOUD_PASSWORD = "your_nextcloud_password"

# Script Settings
VERIFY_SSL = False  # Set to True in production with valid certs
SECRET_TOKEN = "your_secret_webhook_token"  # Set this in GitLab webhook settings for security

# =================================================

def format_time(iso_time):
    """Convert ISO time to readable string."""
    try:
        dt = datetime.fromisoformat(iso_time.replace('Z', '+00:00'))
        return dt.strftime("%Y-%m-%d %H:%M")
    except:
        return iso_time

def send_to_nextcloud(message):
    """Send message to Nextcloud chat."""
    try:
        payload = {"message": message}
        nc_headers = {
            "OCS-APIRequest": "true",
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain"
        }
        r = requests.post(
            NEXTCLOUD_WEBHOOK,
            json=payload,
            auth=(NEXTCLOUD_USER, NEXTCLOUD_PASSWORD),
            headers=nc_headers,
            verify=VERIFY_SSL
        )
        r.raise_for_status()
        print(f"Sent to Nextcloud: {message}")
        return True
    except Exception as e:
        print(f"Error sending to Nextcloud: {e}")
        return False

def handle_issue_event(data):
    """Handle issue events."""
    action = data.get('event_type')  # e.g., 'issue'
    issue = data.get('object_attributes', {})
    project = data.get('project', {})
    user = data.get('user', {})

    if issue.get('action') in ['open', 'close', 'reopen']:
        verb = "Created" if issue['action'] == 'open' else "Resolved" if issue['action'] == 'close' else "Reopened"
        created_at = format_time(issue.get('created_at', datetime.utcnow().isoformat()))
        msg = (f"[{created_at}] 🟢 ISSUE {verb.upper()}\n"
               f"User: {user.get('name', 'Unknown')}\n"
               f"Title: {issue.get('title', '')} (#{issue.get('iid')})\n"
               f"Project: {project.get('name', 'Unknown')} (ID: {project.get('id')})\n"
               f"URL: {issue.get('url')}")
        send_to_nextcloud(msg)

def handle_merge_request_event(data):
    """Handle merge request events."""
    mr = data.get('object_attributes', {})
    project = data.get('project', {})
    user = data.get('user', {})

    if mr.get('action') == 'open':
        created_at = format_time(mr.get('created_at', datetime.utcnow().isoformat()))
        msg = (f"[{created_at}] 🔀 MR CREATED\n"
               f"User: {user.get('name', 'Unknown')}\n"
               f"Title: {mr.get('title', '')} (!{mr.get('iid')})\n"
               f"Project: {project.get('name', 'Unknown')} (ID: {project.get('id')})\n"
               f"URL: {mr.get('url')}")
        send_to_nextcloud(msg)

def handle_push_event(data):
    """Handle push events (commits)."""
    project = data.get('project', {})
    user = data.get('user', {})
    ref = data.get('ref', '')
    commits = data.get('commits', [])

    if ref == 'refs/heads/main' and commits:
        created_at = format_time(data.get('checkout_sha', datetime.utcnow().isoformat()))  # Approximate
        for commit in commits:
            short_id = commit['id'][:8]
            msg = (f"[{created_at}] 💾 COMMIT TO MAIN\n"
                   f"User: {user.get('name', 'Unknown')}\n"
                   f"Commit: {short_id}\n"
                   f"Message: {commit.get('message', '').strip()}\n"
                   f"Project: {project.get('name', 'Unknown')} (ID: {project.get('id')})\n"
                   f"URL: {commit.get('url')}")
            send_to_nextcloud(msg)

def handle_project_event(data):
    """Handle project create/delete/archive/unarchive. Note: System webhooks needed for create/delete."""
    event_type = data.get('event_name')  # e.g., 'project_create', 'project_destroy'
    project = data.get('project', {})
    user = data.get('user', {})

    if event_type == 'project_create':
        verb = "CREATED"
    elif event_type == 'project_destroy':
        verb = "DELETED"
    elif event_type == 'project_archive':  # Custom, if available via update
        verb = "ARCHIVED"
    elif event_type == 'project_unarchive':
        verb = "UNARCHIVED"
    else:
        return

    created_at = format_time(datetime.utcnow().isoformat())  # Project events may not have timestamp
    msg = (f"[{created_at}] 📁 PROJECT {verb}\n"
           f"User: {user.get('name', 'Unknown')}\n"
           f"Name: {project.get('name', 'Unknown')}\n"
           f"ID: {project.get('id', 'N/A')}\n"
           f"URL: {project.get('web_url')}")
    send_to_nextcloud(msg)

def handle_pipeline_event(data):
    """Handle pipeline events."""
    pipeline = data.get('object_attributes', {})
    project = data.get('project', {})
    user = data.get('user', {})

    if pipeline.get('status') == 'created':  # Or other statuses, but you want created
        created_at = format_time(pipeline.get('created_at', datetime.utcnow().isoformat()))
        msg = (f"[{created_at}] 🚀 CI/CD PIPELINE CREATED\n"
               f"User: {user.get('name', 'Unknown')}\n"
               f"ID: {pipeline.get('id')}\n"
               f"Project: {project.get('name', 'Unknown')} (ID: {project.get('id')})\n"
               f"Ref: {pipeline.get('ref')}\n"
               f"URL: {project.get('web_url')}/pipelines/{pipeline['id']}")
        send_to_nextcloud(msg)

def handle_runner_event(data):
    """Handle runner added (system webhook)."""
    # Assuming custom event for runner added; GitLab doesn't have direct webhook for this, perhaps via audit or custom.
    # For now, placeholder.
    pass  # Implement if available

@app.route('/webhook', methods=['POST'])
def webhook():
    """Receive GitLab webhook."""
    if request.headers.get('X-Gitlab-Token') != SECRET_TOKEN:
        return jsonify({"error": "Invalid token"}), 403

    data = request.json
    event = request.headers.get('X-Gitlab-Event')

    print(f"Received event: {event}")

    if event == 'Issue Hook':
        handle_issue_event(data)
    elif event == 'Merge Request Hook':
        handle_merge_request_event(data)
    elif event == 'Push Hook':
        handle_push_event(data)
    elif event in ['System Hook']:  # For system events like project create/delete
        # System hooks have different structure
        if data.get('event_name') in ['project_create', 'project_destroy', 'project_archive', 'project_unarchive']:
            handle_project_event(data)
    elif event == 'Pipeline Hook':
        handle_pipeline_event(data)
    # Add more handlers as needed, e.g., for runners if possible

    return jsonify({"status": "ok"}), 200

if __name__ == "__main__":
    # Run the server (use ngrok or expose publicly for webhook)
    app.run(host='0.0.0.0', port=5000, debug=True)
