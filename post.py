#!/usr/bin/env python3
"""
Publishes due Reels to @ecom.d2c. Adapted from city-reels' post.py (same
mechanism: GitHub Actions runs this on a schedule, video files are hosted
via this repo's own raw.githubusercontent.com URL, published videos get
deleted afterward).

Safety rules baked in:
  * Only items with "status": "approved" are ever published. Drafts are ignored.
  * MAX_PER_RUN caps how many go out per run (default 1).
  * Instagram's own hard ceiling is 50 published posts per rolling 24 hours.

Credentials come from environment variables, set as GitHub Actions secrets --
never committed:
    IG_PAGE_ACCESS_TOKEN
    IG_BUSINESS_ACCOUNT_ID
"""
import datetime
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = json.load(open(os.path.join(HERE, "config.json")))
QUEUE_PATH = os.path.join(HERE, "queue.json")

PAGE_TOKEN = os.environ.get("IG_PAGE_ACCESS_TOKEN", "").strip()
IG_ACCOUNT_ID = os.environ.get("IG_BUSINESS_ACCOUNT_ID", "").strip()
MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN", CONFIG.get("max_per_run", 1)))
KEEP_VIDEOS = int(os.environ.get("KEEP_VIDEOS", CONFIG.get("keep_posted_videos", 3)))

PUBLIC_BASE = CONFIG["public_assets_base"].rstrip("/")

GRAPH = "https://graph.facebook.com/v19.0"
POLL_ATTEMPTS = 40
POLL_DELAY = 5


def now_utc():
    return datetime.datetime.now(datetime.timezone.utc)


def is_due(item):
    when = item.get("scheduled_at")
    if not when:
        return False
    try:
        dt = datetime.datetime.strptime(when, "%Y-%m-%d %H:%M").replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        return False
    return dt <= now_utc()


def post_form(url, data):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(), method="POST")
    with urllib.request.urlopen(req) as resp:
        return json.load(resp)


def get_json(url, params):
    with urllib.request.urlopen(url + "?" + urllib.parse.urlencode(params)) as resp:
        return json.load(resp)


def publish(item):
    video_url = PUBLIC_BASE + "/" + item["video"].lstrip("/")

    container = post_form(GRAPH + "/%s/media" % IG_ACCOUNT_ID, {
        "media_type": "REELS",
        "video_url": video_url,
        "caption": item.get("caption", ""),
        "share_to_feed": "true",
        "access_token": PAGE_TOKEN,
    })
    container_id = container["id"]

    for _ in range(POLL_ATTEMPTS):
        status = get_json(GRAPH + "/%s" % container_id, {"fields": "status_code,status", "access_token": PAGE_TOKEN})
        code = status.get("status_code")
        if code == "FINISHED":
            break
        if code == "ERROR":
            raise RuntimeError("Instagram could not process the video: %s" % status)
        time.sleep(POLL_DELAY)
    else:
        raise RuntimeError("Timed out waiting for Instagram to process the video")

    return post_form(GRAPH + "/%s/media_publish" % IG_ACCOUNT_ID, {
        "creation_id": container_id, "access_token": PAGE_TOKEN})["id"]


def cleanup_videos(queue):
    posted = [i for i in queue if i.get("result") == "posted" and not i.get("video_deleted")]
    posted.sort(key=lambda i: i.get("posted_at_utc") or "")
    stale = posted[:-KEEP_VIDEOS] if KEEP_VIDEOS else posted

    removed = 0
    for item in stale:
        path = os.path.join(HERE, item.get("video", ""))
        if item.get("video") and os.path.exists(path):
            try:
                os.remove(path)
                removed += 1
            except OSError as e:
                print("could not delete %s: %s" % (item["video"], e))
                continue
        item["video_deleted"] = True

    if removed:
        print("Cleaned up %d posted video(s); keeping the last %d." % (removed, KEEP_VIDEOS))
    return bool(stale)


def main():
    if not PAGE_TOKEN or not IG_ACCOUNT_ID:
        print("Instagram credentials not set. Nothing to do.")
        return

    queue = json.load(open(QUEUE_PATH))
    changed = auth_failed = False
    posted = skipped = 0

    for item in sorted(queue, key=lambda it: it.get("scheduled_at") or ""):
        if posted >= MAX_PER_RUN:
            print("Hit MAX_PER_RUN=%d; the rest stay queued for later runs." % MAX_PER_RUN)
            break
        if item.get("status") != "approved":
            continue
        if item.get("result") == "posted":
            continue
        if not is_due(item):
            continue
        if not item.get("video"):
            skipped += 1
            continue

        try:
            media_id = publish(item)
            item.update({"result": "posted", "post_id": media_id,
                         "posted_at_utc": now_utc().strftime("%Y-%m-%d %H:%M:%S"), "error": None})
            posted += 1
            changed = True
            print("POSTED %s -> %s" % (item.get("id"), media_id))
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            item.update({"result": "failed", "error": "HTTP %s: %s" % (e.code, body[:500])})
            changed = True
            print("::error::%s failed: HTTP %s %s" % (item.get("id"), e.code, body[:300]))
            if e.code in (190, 400, 401, 403):
                auth_failed = True
        except Exception as e:  # noqa: BLE001
            item.update({"result": "failed", "error": str(e)})
            changed = True
            print("::error::%s failed: %s" % (item.get("id"), e))

    if changed:
        json.dump(queue, open(QUEUE_PATH, "w"), indent=2, ensure_ascii=False)

    if cleanup_videos(queue):
        json.dump(queue, open(QUEUE_PATH, "w"), indent=2, ensure_ascii=False)

    pending = sum(1 for it in queue if it.get("status") == "approved" and it.get("result") != "posted")
    print("Done. Posted %d, skipped %d (no video). %d still queued." % (posted, skipped, pending))

    if auth_failed:
        print("::error::Instagram rejected the token — it has probably expired.")
        sys.exit(1)


if __name__ == "__main__":
    main()
