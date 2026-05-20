#!/usr/bin/env python3
"""Generate an RSS 2.0 feed from the MotherDuck status page API."""

import datetime
import html
import re
from email.utils import format_datetime
from xml.dom import minidom
from xml.etree.ElementTree import Element, SubElement, tostring

import requests

BASE_URL = "https://status.motherduck.com"
MONTHS_BACK = 3
OUTPUT_FILE = "feed.xml"


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def fetch_json(path, params=None):
    resp = requests.get(f"{BASE_URL}/{path}", params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def month_boundaries_ms(year, month):
    """Return (since, until) in milliseconds (JS getTime() style) for the given month."""
    since = datetime.datetime(year, month, 1, tzinfo=datetime.timezone.utc)
    if month == 12:
        until = datetime.datetime(year + 1, 1, 1, tzinfo=datetime.timezone.utc)
    else:
        until = datetime.datetime(year, month + 1, 1, tzinfo=datetime.timezone.utc)
    return int(since.timestamp() * 1000), int(until.timestamp() * 1000)


def ms_to_rfc2822(ms):
    """Convert a millisecond epoch timestamp to RFC 2822 format for RSS pubDate."""
    if not ms:
        return format_datetime(datetime.datetime.now(datetime.timezone.utc))
    dt = datetime.datetime.fromtimestamp(ms / 1000, tz=datetime.timezone.utc)
    return format_datetime(dt)


def ms_to_human(ms):
    """Convert millisecond timestamp to a human-readable UTC string."""
    if not ms:
        return ""
    dt = datetime.datetime.fromtimestamp(ms / 1000, tz=datetime.timezone.utc)
    return dt.strftime("%b %d, %Y %H:%M UTC")


# ---------------------------------------------------------------------------
# API fetchers
# ---------------------------------------------------------------------------

def fetch_posts(months=MONTHS_BACK):
    """Fetch all posts for the last N months, handling pagination."""
    posts = []
    now = datetime.datetime.now(datetime.timezone.utc)

    for i in range(months):
        # Python floor division handles year rollover correctly
        total_months = now.month - 1 - i
        year = now.year + total_months // 12
        month = total_months % 12 + 1

        since_ms, until_ms = month_boundaries_ms(year, month)
        data = fetch_json("api/posts", {"since": since_ms, "until": until_ms})
        posts.extend(data.get("posts", []))

        token = data.get("continuationToken")
        while token:
            data = fetch_json(
                "api/posts",
                {"since": since_ms, "until": until_ms, "continuation_token": token},
            )
            posts.extend(data.get("posts", []))
            token = data.get("continuationToken")

    return posts


def build_lookup(items, id_key="id", name_key="name"):
    return {item[id_key]: item[name_key] for item in items if id_key in item}


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def strip_html(text):
    """Remove HTML tags, unescape entities, and normalise whitespace."""
    if not text:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"<[^>]*$", "", text)  # strip partial/unclosed tag at string end
    text = html.unescape(text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# ---------------------------------------------------------------------------
# Feed assembly
# ---------------------------------------------------------------------------

def build_update_title(post_title, update, status_map):
    status_name = status_map.get(update.get("status_id", ""), "")
    if status_name:
        return f"[{status_name.title()}] {post_title}"
    return post_title


def build_update_description(update, severity_map, service_map):
    """Build a plain-text description for a single update RSS item."""
    lines = []

    # Affected services with per-service impact severity
    impacts = update.get("impacts") or []
    if impacts:
        affected = []
        for imp in impacts:
            svc = service_map.get(imp.get("service_id", ""), "Unknown service")
            sev = severity_map.get(imp.get("severity_id", ""), "")
            affected.append(f"{svc} ({sev})" if sev else svc)
        lines.append(f"Affected: {', '.join(affected)}")
        lines.append("")

    msg = strip_html(update.get("message", ""))
    if msg:
        lines.append(msg)

    return "\n".join(lines).strip()


def generate_rss(layout_data, posts, post_enums, services):
    layout_settings = (
        layout_data.get("layout", {})
        .get("layout_settings", {})
        .get("statusPage", {})
    )
    global_headline = layout_settings.get("globalStatusHeadline", "MotherDuck Status")

    # Build ID → name lookup tables from post_enums
    all_enums = post_enums.get("post_enums", [])
    severity_map = build_lookup(
        [e for e in all_enums if e.get("post_enum_type") == "severity"]
    )
    status_map = build_lookup(
        [e for e in all_enums if e.get("post_enum_type") == "status"]
    )
    # impacts severity uses the same enum set as incident severity
    impact_severity_map = severity_map

    service_map = build_lookup(services.get("services", []))

    # Determine channel description: list active incident titles if any, else the global headline
    active_titles = []
    for post in posts:
        updates = sorted(post.get("updates") or [], key=lambda u: u.get("reported_at", 0))
        if updates:
            latest_status = status_map.get(updates[-1].get("status_id", ""), "").lower()
            if latest_status != "resolved":
                active_titles.append(post.get("title", "Ongoing incident"))
    channel_description = "; ".join(active_titles) if active_titles else global_headline

    # Build RSS
    rss = Element("rss", version="2.0")
    channel = SubElement(rss, "channel")

    SubElement(channel, "title").text = "MotherDuck Status"
    SubElement(channel, "link").text = "https://status.motherduck.com"
    SubElement(channel, "description").text = channel_description
    SubElement(channel, "language").text = "en"
    last_build_date = SubElement(channel, "lastBuildDate")  # filled in after items
    SubElement(channel, "ttl").text = "15"

    max_pub_ms = 0

    # Collect all (post_meta, update) pairs so we can sort globally by timestamp
    all_items = []
    for post in posts:
        post_id = post.get("id", "")
        post_title = post.get("title", "Status Update")
        link = f"https://status.motherduck.com/posts/details/{post_id}"
        for upd in (post.get("updates") or []):
            all_items.append((post_id, post_title, link, upd))

    all_items.sort(key=lambda x: x[3].get("reported_at", 0), reverse=True)

    for post_id, post_title, link, upd in all_items:
        reported_ms = upd.get("reported_at") or 0
        epoch_seconds = int(reported_ms / 1000)
        item = SubElement(channel, "item")
        SubElement(item, "title").text = build_update_title(post_title, upd, status_map)
        SubElement(item, "link").text = link
        SubElement(item, "guid").text = f"{post_id}-{epoch_seconds}"
        SubElement(item, "description").text = build_update_description(
            upd, impact_severity_map, service_map
        )
        SubElement(item, "pubDate").text = ms_to_rfc2822(reported_ms)
        if reported_ms > max_pub_ms:
            max_pub_ms = reported_ms

    if not posts:
        item = SubElement(channel, "item")
        SubElement(item, "title").text = f"All Systems Operational — {global_headline}"
        SubElement(item, "link").text = "https://status.motherduck.com"
        today = datetime.date.today().isoformat()
        SubElement(item, "guid").text = f"motherduck-no-incidents-{today}"
        SubElement(item, "description").text = "No incidents reported in the past 3 months."
        # Use midnight UTC of today so pubDate is stable across runs on the same day
        midnight = datetime.datetime.combine(
            datetime.date.today(), datetime.time.min, tzinfo=datetime.timezone.utc
        )
        max_pub_ms = int(midnight.timestamp() * 1000)
        SubElement(item, "pubDate").text = ms_to_rfc2822(max_pub_ms)

    last_build_date.text = ms_to_rfc2822(max_pub_ms)

    xml_bytes = tostring(rss, encoding="unicode")
    dom = minidom.parseString(f'<?xml version="1.0" encoding="UTF-8"?>{xml_bytes}')
    return dom.toprettyxml(indent="  ", encoding=None).replace(
        '<?xml version="1.0" ?>', '<?xml version="1.0" encoding="UTF-8"?>'
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    print("Fetching layout data...")
    layout_data = fetch_json("api/data")

    print("Fetching post enumerations...")
    post_enums = fetch_json("api/post_enums")

    print("Fetching services...")
    services = fetch_json("api/services")

    print(f"Fetching posts for the last {MONTHS_BACK} months...")
    posts = fetch_posts()
    print(f"Found {len(posts)} posts.")

    feed_xml = generate_rss(layout_data, posts, post_enums, services)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(feed_xml)

    print(f"Wrote {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
