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

## Author
* Tamir Suliman
