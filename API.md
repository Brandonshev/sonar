# Sonar API (for Lookout)

Sonar publishes trends as **standard events**. Lookout (or any other program) reads them over HTTPS
and never touches Sonar's database. Sonar and Lookout stay separate programs.

## `GET /api/trends`

```
GET https://<sonar-host>/api/trends?category=social.video&min_score=60&after_id=0
Authorization: Bearer <LOOKOUT_TOKEN>
```

| Query | Default | Meaning |
|---|---|---|
| `category` | all of the feed's categories | One category, e.g. `social.video`. Must be one the feed node publishes; otherwise `400`. |
| `min_score` | feed node's `min_score` (60) | Only events scoring at least this. It can only raise the node's threshold, never lower it. |
| `after_id` | `0` | **Recommended cursor.** Only events with `id` greater than this. Send back `next_after_id` from the last response. |
| `since` | `0` | Only events with `ts` (unix seconds) after this. Fine for one-off queries; use `after_id` for polling, because several events can share one `ts`. |
| `limit` | `100` | At most the node's `max_results` (200). |

Events come **oldest first**. Poll every few minutes with the last `next_after_id`; if `count == limit`,
call again straight away to fetch the rest.

### Response `200`

```json
{
  "sonar_version": "0.10.0",
  "count": 1,
  "next_since": 1795530000,
  "next_after_id": 4812,
  "events": [
    {
      "id": 4812,
      "type": "trend.found",
      "feed": "lookout",
      "ts": 1795530000,
      "source": "tiktok",
      "item": {
        "id": 931,
        "title": "#grwm",
        "url": "https://www.tiktok.com/tag/grwm",
        "category": "social.video",
        "kind": "hashtag",
        "entity_key": "tiktok:grwm"
      },
      "sources": ["tiktok", "youtube"],
      "score": 78.5,
      "heat": 9.4,
      "reasons": [
        "views 583.1B -> 589.0B in 24h, 7.5x its normal rate",
        "normal rate: +787M views/day over the previous 7 days",
        "also climbing on youtube (+25%)"
      ],
      "examples": [
        "https://www.tiktok.com/tag/grwm",
        "https://www.youtube.com/watch?v=abc123"
      ],
      "sonar_version": "0.10.0"
    }
  ]
}
```

### Event fields

| Field | Type | Meaning |
|---|---|---|
| `id` | int | Event id. Increases with time; use it as the cursor. |
| `type` | string | Always `trend.found` here. Other Sonar event types (`alert.sent`, `node.failed`, `source.disabled`) aren't served by this endpoint. |
| `feed` | string | Always `lookout`. |
| `ts` | int | When Sonar emitted it (unix seconds, UTC). |
| `source` | string | Where the item was found: `tiktok`, `youtube`, `instagram`, `github`, ... |
| `item.id` | int | Sonar's item id (stable). The dashboard page is `/item/<id>`. |
| `item.title` | string | Hashtag (`#grwm`), video title, repo name, etc. |
| `item.url` | string or null | The item itself. |
| `item.category` | string | e.g. `social.video` (TikTok hashtags and YouTube videos). |
| `item.kind` | string | `hashtag`, `video`, `repo`, `story`, `post`, `product`, `article`, `search`, `company`, ... |
| `item.entity_key` | string or null | Sonar's cross-source key (`tiktok:grwm`, `github:owner/repo`, `site:example.com`, `wiki:Title`). |
| `sources` | string[] | Every source this thing was seen on (same entity or same careful name/hashtag key). |
| `score` | number | 0-100. Log-scaled heat, normalised per source. |
| `heat` | number | Growth in the last 24h ÷ normal daily growth over the 7 days before. 5 = five times faster than usual. Includes the early and cross-source bonuses. |
| `reasons` | string[] | Plain-English explanation, most important first. |
| `examples` | string[] | Up to `max_examples` (5) URLs: the item first, then the same thing on other sources. |
| `sonar_version` | string | Sonar version that emitted the event. |

New fields may be added; never removed or renamed without a version bump. Ignore fields you don't know.

### When events are emitted

After every scoring pass (at most every 15 minutes), for each item in the feed's `categories` whose
score is at least the feed's `min_score`. The same item is emitted again only after `cooldown_hours`
(24) **or** if its score rises by `rejump_score` (20) or more. All of these are in `pipeline.json`
under the `lookout` node.

### Errors

| Code | Body | When |
|---|---|---|
| `400` | `{"error": "..."}` | Unknown category, or a non-number in `min_score` / `since` / `after_id` / `limit`. |
| `401` | `{"error": "bad or missing token"}` | Missing or wrong `Authorization: Bearer` token. |
| `404` | `{"error": "feed not enabled"}` | The `lookout` node is off, or `LOOKOUT_TOKEN` isn't set on the server. |
| `429` | `{"error": "rate limited"}`, `Retry-After: 60` | More than `rate_limit_per_minute` (10) requests in a minute from one IP. |

Every successful read is logged on the server (`/opt/sonar/logs/web.log`) with the caller's IP, query and result count.

### Example (Python)

```python
import json, os, urllib.request

def fetch(after_id=0):
    req = urllib.request.Request(
        f"https://<sonar-host>/api/trends?category=social.video&after_id={after_id}",
        headers={"Authorization": f"Bearer {os.environ['SONAR_TOKEN']}"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)

page = fetch(saved_cursor)
for event in page["events"]:
    handle(event)
saved_cursor = page["next_after_id"]
```

Scores are attention signals only, not financial advice.
