# Nextcloud GitLab Streamer

A lightweight Python service that monitors GitLab activity across all your projects and streams real-time updates to Nextcloud Talk.

It tracks:

* New Issues
* New Merge Requests
* New Commits …and posts them directly into a Nextcloud chat channel.

## Arch 

```
GitLab API  --->  Python Poller  --->  Nextcloud Talk Webhook
                   (this script)
```

## Docker 

```
docker build -t gitlab-poller .
docker run -d \
  --name gitlab-poller \
  -v "$(pwd)/data:/data" \
  --env-file .env \
  -e POLL_INTERVAL=300 \
  gitlab-poller
```

## Roadmap Ideas

* Docker container image
* Web UI dashboard
* Slack / Teams / Email integrations
* Event filtering rules
* Multi-tenant support

## Contributing
Pull requests and feature ideas are welcome.

## Author
* Tamir Suliman
