#!/bin/sh
echo "🚀 GitLab → Nextcloud Poller Container started"
echo "State file : ${STATE_FILE}"
echo "Poll every  : ${POLL_INTERVAL}s"

# Support --test mode
if [ "${1}" = "--test" ]; then
    echo "🧪 Running TEST mode..."
    exec python gitlab_poller.py --test
fi

echo "Starting continuous polling loop..."
while true; do
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 🔄 Starting new check cycle..."
    python gitlab_poller.py && echo "✅ Cycle finished successfully" || echo "⚠️ Cycle finished with errors"
    echo "⏳ Sleeping ${POLL_INTERVAL} seconds..."
    sleep "${POLL_INTERVAL}"
done
