#!/usr/bin/env python3
"""Build the daily paper.

Fetches every feed in sources.toml plus the Open-Meteo forecast, merges stories
that several outlets ran, and writes a single static page to site/index.html.
Uses only the Python standard library.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import html
import json
import re
import sys
import tomllib
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from string import Template
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
USER_AGENT = "Mozilla/5.0 (compatible; daily-paper/1.0; +https://github.com/jcm-book-library/daily-paper)"
# BOM's warnings feed turns away anything that doesn't identify as a web browser.
BROWSER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
EDITIONS = [("Morning", 6), ("Afternoon", 12), ("Evening", 18)]
# A run this close before an edition's hour counts as that edition (cron runs at :50).
EDITION_GRACE = timedelta(minutes=30)
SUMMARY_MAX = 220


@dataclass
class Story:
    title: str
    link: str
    source: str
    summary: str
    ts: datetime | None
    rank: int  # position of the feed in its section; lower wins when merging
    position: int = 0  # the story's place in its own feed, which for ranked feeds is editorial order
    also: list[tuple[str, str]] = field(default_factory=list)  # (source, link)


# ---------------------------------------------------------------- fetching

def fetch(url: str, timeout: int = 20, browser: bool = False) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": BROWSER_AGENT if browser else USER_AGENT,
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, application/json;q=0.9, */*;q=0.8",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


# ---------------------------------------------------------------- parsing

def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(el: ET.Element, *names: str) -> str:
    for name in names:
        for child in el:
            if _local(child.tag) == name and (child.text or "").strip():
                return child.text.strip()
    return ""


def _link(el: ET.Element) -> str:
    fallback = ""
    for child in el:
        if _local(child.tag) != "link":
            continue
        href = child.get("href") or (child.text or "").strip()
        if not href:
            continue
        if child.get("rel") in (None, "alternate"):
            return href
        fallback = fallback or href
    if fallback:
        return fallback
    guid = _child_text(el, "guid", "id")
    return guid if guid.startswith("http") else ""


def parse_date(value: str) -> datetime | None:
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def clean_text(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def make_summary(raw: str, title: str) -> str:
    text = clean_text(raw)
    text = re.sub(r"\s*(Continue reading|Read more)\s*(\.\.\.|…)?\s*$", "", text, flags=re.I).strip()
    if not text or text.lower() == title.lower():
        return ""
    if len(text) <= SUMMARY_MAX:
        return text
    cut = text[:SUMMARY_MAX]
    end = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
    if end > 80:
        return cut[: end + 1]
    return cut.rsplit(" ", 1)[0].rstrip(",;:") + "…"


def parse_feed(data: bytes) -> list[dict]:
    root = ET.fromstring(data)
    items = []
    for el in root.iter():
        if _local(el.tag) not in ("item", "entry"):
            continue
        title = clean_text(_child_text(el, "title"))
        link = _link(el)
        if not title or not link.startswith(("http://", "https://")):
            continue
        items.append({
            "title": title,
            "link": link,
            "summary": make_summary(_child_text(el, "description", "summary", "content"), title),
            "ts": parse_date(_child_text(el, "pubDate", "published", "updated", "date")),
            "categories": [(c.text or c.get("term") or "").strip() for c in el if _local(c.tag) == "category"],
        })
    return items


def parse_espn_news(data: bytes) -> list[dict]:
    """ESPN's news data (their RSS feeds are empty): a JSON list of articles."""
    items = []
    for article in json.loads(data).get("articles", []):
        if article.get("type") == "Media":  # video clips
            continue
        title = clean_text(article.get("headline", ""))
        link = (article.get("links", {}).get("web") or {}).get("href", "")
        if not title or not link.startswith(("http://", "https://")):
            continue
        items.append({
            "title": title,
            "link": link,
            "summary": make_summary(article.get("description", ""), title),
            "ts": parse_date(article.get("published", "")),
            "categories": [],
        })
    return items


def parse_items(data: bytes, feed: dict) -> list[dict]:
    return parse_espn_news(data) if feed.get("format") == "espn" else parse_feed(data)


def matches_filters(item: dict, feed: dict) -> bool:
    if feed.get("category") and feed["category"].lower() not in (c.lower() for c in item.get("categories", [])):
        return False
    text = f"{item['title']} {item['summary']}".lower()
    include = [w.lower() for w in feed.get("include", [])]
    exclude = [w.lower() for w in feed.get("exclude", [])]
    if include and not any(w in text for w in include):
        return False
    return not any(w in text for w in exclude)


# ---------------------------------------------------------------- merging duplicates

STOPWORDS = set("""
a an the and or but of to in on at for with from by as is are was were be been being it its this that
these those after before over under into about than then up down out off new says say said will would
could can may might has have had not no more most what who why how when where which your you our we
they their his her he she them us amp just now live latest update updates
""".split())


def title_tokens(title: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", title.lower().replace("'", "").replace("’", ""))
    return {
        w[:-1] if len(w) > 4 and w.endswith("s") else w
        for w in words
        if w not in STOPWORDS and (len(w) > 2 or w.isdigit())
    }


def same_story(a: set[str], b: set[str]) -> bool:
    shared = len(a & b)
    if shared < 3:
        return False
    return shared / min(len(a), len(b)) >= 0.6 or shared / len(a | b) >= 0.5


def merge_duplicates(stories: list[Story]) -> list[Story]:
    """Group stories whose headlines share most of their key words.

    The highest-priority source becomes the headline; the rest become 'also' links.
    """
    ordered = sorted(stories, key=lambda s: (s.rank, -(s.ts.timestamp() if s.ts else 0)))
    clusters: list[tuple[Story, list[set[str]], set[str]]] = []
    for story in ordered:
        tokens = title_tokens(story.title)
        for primary, token_sets, sources in clusters:
            if any(same_story(tokens, t) for t in token_sets):
                token_sets.append(tokens)
                if story.source not in sources:
                    sources.add(story.source)
                    primary.also.append((story.source, story.link))
                if story.ts and (primary.ts is None or story.ts > primary.ts):
                    primary.ts = story.ts  # cluster counts as fresh if anyone updated it
                break
        else:
            clusters.append((story, [tokens], {story.source}))
    return [c[0] for c in clusters]


# ---------------------------------------------------------------- editions

@dataclass
class Edition:
    name: str
    cutoff: datetime    # stories published after this aren't in the edition yet
    previous: datetime  # stories published after this are flagged NEW
    printed: datetime


def editions_for(now: datetime) -> list[Edition]:
    """Today's editions published so far, oldest first. `now` must be timezone-aware local time."""
    tz = now.tzinfo
    day = now.date()
    slots = [(name, datetime.combine(day, time(hour), tz)) for name, hour in EDITIONS]
    out: list[Edition] = []
    prev = datetime.combine(day - timedelta(days=1), time(EDITIONS[-1][1]), tz)
    for name, at in slots:
        if at > now + EDITION_GRACE:
            break
        out.append(Edition(name, at, prev, at))
        prev = at
    if not out:  # before the morning edition: show last night's evening edition
        evening = datetime.combine(day - timedelta(days=1), time(EDITIONS[-1][1]), tz)
        noon = datetime.combine(day - timedelta(days=1), time(EDITIONS[-2][1]), tz)
        out.append(Edition(EDITIONS[-1][0], evening, noon, evening))
    latest = out[-1]
    latest.cutoff = max(latest.cutoff, now)  # the current edition includes everything fetched
    latest.printed = now
    return out


def stories_for_edition(stories: list[Story], ed: Edition, limit: int) -> list[Story]:
    visible = [s for s in stories if s.ts is None or s.ts <= ed.cutoff]
    return visible[:limit]


def is_new(story: Story, ed: Edition) -> bool:
    return story.ts is not None and ed.previous < story.ts <= ed.cutoff


# ---------------------------------------------------------------- weather

WEATHER_CODES = {
    0: "Sunny", 1: "Mostly sunny", 2: "Partly cloudy", 3: "Cloudy", 45: "Fog", 48: "Fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle", 56: "Freezing drizzle", 57: "Freezing drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain", 66: "Freezing rain", 67: "Freezing rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains",
    80: "Showers", 81: "Showers", 82: "Heavy showers", 85: "Snow showers", 86: "Snow showers",
    95: "Thunderstorms", 96: "Storms with hail", 99: "Storms with hail",
}
COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def weather_url(paper: dict) -> str:
    params = {
        "latitude": paper["latitude"], "longitude": paper["longitude"], "timezone": paper["timezone"],
        "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m,wind_direction_10m",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,uv_index_max,sunrise",
        "forecast_days": 7,
    }
    return "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(params)


def uv_label(uv: float) -> str:
    for limit, label in ((3, "Low"), (6, "Moderate"), (8, "High"), (11, "Very high")):
        if uv < limit:
            return label
    return "Extreme"


def summarise_weather(raw: dict) -> dict:
    cur, daily = raw["current"], raw["daily"]
    days = []
    for i, date in enumerate(daily["time"]):
        days.append({
            "day": datetime.fromisoformat(date).strftime("%a"),
            "cond": WEATHER_CODES.get(daily["weather_code"][i], "Mixed"),
            "rain": round(daily["precipitation_probability_max"][i] or 0),
            "min": round(daily["temperature_2m_min"][i]),
            "max": round(daily["temperature_2m_max"][i]),
        })
    today = days[0]
    notes = []
    hot = max(days[1:], key=lambda d: d["max"], default=None)
    if hot and hot["max"] >= 30:
        notes.append(f"Hot on {hot['day']}, reaching {hot['max']}°.")
    wet = max(days[1:], key=lambda d: d["rain"], default=None)
    if wet and wet["rain"] >= 70:
        notes.append(f"Rain likely on {wet['day']} ({wet['rain']}% chance).")
    if not notes and hot:
        notes.append(f"Warmest day ahead is {hot['day']} at {hot['max']}°.")
    uv = daily["uv_index_max"][0] or 0
    return {
        "now": round(cur["temperature_2m"], 1),
        "feels": round(cur["apparent_temperature"]),
        "now_cond": WEATHER_CODES.get(cur["weather_code"], "Mixed"),
        "wind": f"{COMPASS[round(cur['wind_direction_10m'] / 45) % 8]} {round(cur['wind_speed_10m'])}",
        "uv": f"{round(uv)} {uv_label(uv)}",
        "sunrise": datetime.fromisoformat(daily["sunrise"][0]).strftime("%-I:%M%p").lower(),
        "today": today,
        "days": days,
        "note": " ".join(notes),
    }


# ---------------------------------------------------------------- scores, tables and alerts
#
# Each [[scores]] entry in sources.toml names a `kind`. The parsers below turn that
# source's data into one of two plain shapes the page knows how to draw:
#   {"type": "table", "headers": [...], "rows": [[...], ...], "highlight": row index or None}
#   {"type": "list", "groups": [(heading, [(text, detail, link), ...]), ...]}

def _espn_stat(entry: dict, name: str) -> str:
    for stat in entry.get("stats", []):
        if stat.get("name") == name:
            value = stat.get("displayValue")
            return str(value if value not in (None, "") else stat.get("value", "")).strip()
    return ""


def _who(entry: dict) -> dict:
    return entry.get("team") or entry.get("athlete") or {}


def table_espn(data: bytes, cfg: dict, now: datetime) -> dict:
    groups = json.loads(data).get("children") or []
    entries = groups[cfg.get("group", 0)]["standings"]["entries"]
    entries = sorted(entries, key=lambda e: int(r) if (r := _espn_stat(e, "rank")).isdigit() else 999)
    columns = cfg.get("columns", [["Pts", "points"]])
    top = cfg.get("rows", 10)
    rows, highlight = [], None
    for pos, entry in enumerate(entries, 1):
        name = _who(entry).get("displayName", "?")
        row = [str(pos), name, *(_espn_stat(entry, key) for _, key in columns)]
        mine = bool(cfg.get("highlight")) and cfg["highlight"].lower() in name.lower()
        if pos <= top:
            rows.append(row)
        elif mine:  # keep your team visible even when it's outside the top rows
            rows += [["…", "", *([""] * len(columns))], row]
        else:
            continue
        if mine:
            highlight = len(rows) - 1
    return {"type": "table", "headers": ["", cfg.get("name_header", "Team"), *(label for label, _ in columns)],
            "rows": rows, "highlight": highlight}


def table_squiggle(data: bytes, cfg: dict, now: datetime) -> dict:
    teams = sorted(json.loads(data)["standings"], key=lambda t: t["rank"])
    rows = [[str(t["rank"]), t["name"], str(t["played"]), f'{t["percentage"]:.1f}', str(t["pts"])]
            for t in teams[: cfg.get("rows", 18)]]
    return {"type": "table", "headers": ["", "Team", "P", "%", "Pts"], "rows": rows, "highlight": None}


def _local_when(iso: str, tz: ZoneInfo) -> str:
    dt = parse_date(iso)
    return dt.astimezone(tz).strftime("%a %-d %b, %-I:%M%p").replace("AM", "am").replace("PM", "pm") if dt else ""


def scores_espn(data: bytes, cfg: dict, now: datetime) -> dict:
    """Results and upcoming games from an ESPN scoreboard."""
    tz = now.tzinfo
    results, coming = [], []
    for event in sorted(json.loads(data).get("events", []), key=lambda e: e.get("date", "")):
        comp = event["competitions"][0]
        sides = {c.get("homeAway"): c for c in comp.get("competitors", [])}
        home, away = sides.get("home", {}), sides.get("away", {})
        hn, an = _who(home).get("shortDisplayName") or _who(home).get("displayName", "?"), \
            _who(away).get("shortDisplayName") or _who(away).get("displayName", "?")
        state = event.get("status", {}).get("type", {}).get("state")
        link = next((l.get("href") for l in event.get("links", []) if l.get("href", "").startswith("http")), "")
        if state == "post":
            results.append((f"{hn} {home.get('score', '')} – {away.get('score', '')} {an}", "Final", link))
        elif state == "in":
            results.append((f"{hn} {home.get('score', '')} – {away.get('score', '')} {an}", "Live", link))
        else:
            coming.append((f"{hn} v {an}", _local_when(event.get("date", ""), tz), link))
    limit = cfg.get("rows", 6)
    groups = []
    if results:
        groups.append(("Results", results[-limit:]))
    if coming:
        groups.append(("Coming up", coming[:limit]))
    return {"type": "list", "groups": groups}


def race_espn(data: bytes, cfg: dict, now: datetime) -> dict:
    """The latest Grand Prix podium and the next race, from ESPN's F1 scoreboard."""
    d = json.loads(data)
    groups = []
    for event in d.get("events", [])[:1]:
        comps = event.get("competitions", [])
        race = next((c for c in comps if (c.get("type") or {}).get("abbreviation", "").lower() in ("race", "r")),
                    comps[-1] if comps else {})
        done = event.get("status", {}).get("type", {}).get("state") == "post"
        if done and race.get("competitors"):
            podium = sorted(race["competitors"], key=lambda c: c.get("order", 99))[:3]
            groups.append((event.get("name", "Last race"),
                           [(f'{c.get("order")}. {_who(c).get("displayName", "?")}', "", "") for c in podium]))
    calendar = (d.get("leagues") or [{}])[0].get("calendar") or []
    upcoming = [c for c in calendar if isinstance(c, dict) and (parse_date(c.get("startDate", "")) or now) > now]
    if upcoming:
        nxt = upcoming[0]
        groups.append(("Next race", [(nxt.get("label", ""), _local_when(nxt.get("startDate", ""), now.tzinfo), "")]))
    return {"type": "list", "groups": groups}


def cricket_live(data: bytes, cfg: dict, now: datetime) -> dict:
    """Cricinfo's live scores feed covers every match worldwide; keep the teams you follow."""
    teams = [t.lower() for t in cfg.get("include", [])]
    lines = [(i["title"].replace(" *", "*"), "", i["link"]) for i in parse_feed(data)
             if not teams or any(t in i["title"].lower() for t in teams)]
    return {"type": "list", "groups": [("Live and recent", lines[: cfg.get("rows", 8)])] if lines else []}


SCORE_KINDS = {
    "espn_table": table_espn, "squiggle_table": table_squiggle,
    "espn_scores": scores_espn, "espn_race": race_espn, "cricket_live": cricket_live,
}


def parse_alerts(data: bytes, cfg: dict) -> list[dict]:
    """Current BOM warnings that matter here: the right kind of warning, for the right area."""
    kinds = [w.lower() for w in cfg.get("include", [])]
    places = [w.lower() for w in cfg.get("require", [])]
    out = []
    for item in parse_feed(data):
        title = re.sub(r"^\d{2}/\d{2}:\d{2} \w+ ", "", item["title"])  # drop BOM's "05/16:15 EDT " prefix
        text = f"{title} {item['summary']}".lower()
        if (not kinds or any(k in text for k in kinds)) and (not places or any(p in text for p in places)):
            out.append({"title": title, "link": item["link"]})
    return out


# ---------------------------------------------------------------- rendering

esc = html.escape
NEW_TAB = 'target="_blank" rel="noopener"'  # stories open in a new tab so the paper stays open


def fmt_time(dt: datetime, tz: ZoneInfo) -> str:
    return dt.astimezone(tz).strftime("%-I:%M%p").lower()


def render_story(story: Story, ed: Edition, tz: ZoneInfo, lead: bool = False) -> str:
    stamp = f"<span>{fmt_time(story.ts, tz)}</span>" if story.ts else ""
    new = '<span class="new">NEW</span>' if is_new(story, ed) else ""
    kicker = f'<span class="kicker"><span class="src">{esc(story.source)}</span>{stamp}{new}</span>'
    tag = "h3" if lead else "h4"
    summary = f"<p>{esc(story.summary)}</p>" if story.summary else ""
    also = ""
    if story.also:
        links = " · ".join(f'<a href="{esc(link)}" {NEW_TAB}>{esc(src)}</a>' for src, link in story.also)
        also = f'<div class="also">Also covered by {links}</div>'
    return (f'<div class="{"lead" if lead else "item"}"><a class="story" href="{esc(story.link)}" {NEW_TAB}>'
            f"{kicker}<{tag}>{esc(story.title)}</{tag}>{summary}</a>{also}</div>")


def pick_watch(stories: list[Story], limit: int) -> list[Story]:
    """The day's biggest stories: those most outlets are running, topped up with the
    first-listed feed's highest-ranked stories."""
    shared = sorted((s for s in stories if s.also),
                    key=lambda s: (len(s.also), s.ts.timestamp() if s.ts else 0), reverse=True)
    ranked = sorted((s for s in stories if s.rank == 0 and not s.also), key=lambda s: s.position)
    return (shared + ranked)[:limit]


def render_watch_story(story: Story, ed: Edition, tz: ZoneInfo) -> str:
    outlets = len(story.also) + 1
    label = f"{outlets} outlets" if outlets > 1 else f"{story.source} top story"
    stamp = f"<span>{fmt_time(story.ts, tz)}</span>" if story.ts else ""
    new = '<span class="new">NEW</span>' if is_new(story, ed) else ""
    summary = f"<p>{esc(story.summary)}</p>" if story.summary else ""
    also = ""
    if story.also:
        links = [(story.source, story.link), *story.also]
        also = '<div class="also">Read at ' + " · ".join(f'<a href="{esc(l)}" {NEW_TAB}>{esc(src)}</a>' for src, l in links) + "</div>"
    return (f'<div class="watch-item"><a class="story" href="{esc(story.link)}" {NEW_TAB}>'
            f'<span class="kicker"><span class="src">{esc(label)}</span>{stamp}{new}</span>'
            f"<h3>{esc(story.title)}</h3>{summary}</a>{also}</div>")


def pick_lead(stories: list[Story]) -> Story:
    """The story most outlets are running; ties go to the top-priority source, then the newest."""
    return max(stories, key=lambda s: (len(s.also), -s.rank, s.ts.timestamp() if s.ts else 0))


def render_section(section: dict, stories: list[Story], editions: list[Edition], tz: ZoneInfo,
                   skip: dict[str, set[int]] | None = None) -> str:
    """One block per edition. `skip` maps an edition name to ids of stories shown elsewhere."""
    blocks = []
    for ed in editions:
        hide = (skip or {}).get(ed.name, set())
        visible = [s for s in stories if id(s) not in hide]
        chosen = stories_for_edition(visible, ed, section.get("limit", 6))
        hidden = "" if ed is editions[-1] else " hidden"
        if not chosen:
            body = '<p class="empty">Nothing new from these sources lately.</p>'
        elif section.get("group") == "front":
            lead = pick_lead(chosen)
            rest = [s for s in chosen if s is not lead]
            body = render_story(lead, ed, tz, lead=True) + \
                '<div class="columns">' + "".join(render_story(s, ed, tz) for s in rest) + "</div>"
        else:
            body = "".join(render_story(s, ed, tz) for s in chosen)
        blocks.append(f'<div class="feed" data-ed="{ed.name}"{hidden}>{body}</div>')
    return "".join(blocks)


def source_names(section: dict) -> str:
    names = []
    for feed in section["feeds"]:
        if feed["name"] not in names:
            names.append(feed["name"])
    return " · ".join(esc(n) for n in names)


def render_weather(w: dict | None, paper: dict) -> tuple[str, str]:
    bom = esc(paper["bom_url"])
    if not w:
        ear = '<div class="ear-label">Weather</div><div>Forecast unavailable</div>'
        panel = f'<p class="empty">The forecast couldn\'t be loaded this edition.</p><div class="wx-links"><a href="{bom}" {NEW_TAB}>Forecast &amp; warnings at BOM</a></div>'
        return ear, panel
    t = w["today"]
    ear = (f'<div class="ear-label">{esc(paper["location"])} now</div>'
           f'<div class="now-temp">{w["now"]}° <small>feels {w["feels"]}°</small></div>'
           f'<div>{esc(w["now_cond"])} · Max {t["max"]}°</div>')
    lo = min(d["min"] for d in w["days"]) - 2
    hi = max(d["max"] for d in w["days"]) + 2
    pct = lambda v: (v - lo) / (hi - lo)  # noqa: E731
    rows = "".join(
        f'<tr><td class="day">{d["day"]}</td><td class="c">{esc(d["cond"])}</td><td class="r">{d["rain"]}%</td>'
        f'<td class="t"><div class="range" aria-label="{d["min"]} to {d["max"]} degrees">'
        f'<span class="lo">{d["min"]}°</span>'
        f'<span class="bar" style="left:calc(22px + (100% - 50px) * {pct(d["min"]):.3f});'
        f'width:calc((100% - 50px) * {pct(d["max"]) - pct(d["min"]):.3f})"></span>'
        f'<span class="hi">{d["max"]}°</span></div></td></tr>'
        for d in w["days"]
    )
    note = f'<p class="wx-note">{esc(w["note"])}</p>' if w["note"] else ""
    panel = (
        f'<div class="today"><div class="big">{t["max"]}°</div><div>'
        f'<div class="cond">{esc(t["cond"])}</div>'
        f'<div class="min">Min {t["min"]}° · sunrise {w["sunrise"]}</div></div></div>'
        f'<div class="facts"><div>Rain chance<b>{t["rain"]}%</b></div><div>Wind<b>{esc(w["wind"])}</b></div>'
        f'<div>UV<b>{esc(w["uv"])}</b></div></div>'
        f'<table class="week" aria-label="Seven-day forecast"><tbody>{rows}</tbody></table>{note}'
        f'<div class="wx-links"><a href="{bom}" {NEW_TAB}>Full forecast &amp; warnings at BOM</a><span>Forecast data: Open-Meteo</span></div>'
    )
    return ear, panel


def render_box(cfg: dict, data: dict) -> str:
    """One scores or table box."""
    title = f'<h4>{esc(cfg["title"])}</h4>'
    more = (f'<a class="more" href="{esc(cfg["more_url"])}" {NEW_TAB}>{esc(cfg.get("more_label", "More"))}</a>'
            if cfg.get("more_url") else "")
    if data["type"] == "table" and data["rows"]:
        head = "".join(f"<th>{esc(h)}</th>" for h in data["headers"])
        body = "".join(
            f'<tr{" class=mine" if i == data["highlight"] else ""}>' + "".join(f"<td>{esc(c)}</td>" for c in row) + "</tr>"
            for i, row in enumerate(data["rows"]))
        return f'<div class="box">{title}<table class="ladder"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>{more}</div>'
    if data["type"] == "list" and data["groups"]:
        parts = []
        for heading, lines in data["groups"]:
            items = "".join(
                "<li>" + (f'<a href="{esc(link)}" {NEW_TAB}>{esc(text)}</a>' if link else f"<span>{esc(text)}</span>")
                + (f'<span class="when">{esc(detail)}</span>' if detail else "") + "</li>"
                for text, detail, link in lines)
            parts.append(f"<h5>{esc(heading)}</h5><ul class=\"results\">{items}</ul>")
        return f'<div class="box">{title}{"".join(parts)}{more}</div>'
    return f'<div class="box">{title}<p class="empty">Nothing to show right now.</p>{more}</div>'


def render_alerts(alerts: list[dict]) -> str:
    if not alerts:
        return ""
    items = "".join(f'<li><span class="alert-tag">BOM warning</span><a href="{esc(a["link"])}" {NEW_TAB}>{esc(a["title"])}</a></li>'
                    for a in alerts)
    return f'<ul class="alerts">{items}</ul>'


def render_group(name: str, members: list[tuple[dict, list[Story]]], boxes: list[tuple[dict, dict]],
                 editions: list[Edition], tz: ZoneInfo) -> str:
    """A group of sections. When sections name a `tab`, each tab becomes its own page with a button."""
    def columns(items: list[tuple[dict, list[Story]]]) -> str:
        return "".join(
            f'<section aria-label="{esc(s["title"])}"><h3 class="sub">{esc(s["title"])}</h3>'
            f'<p class="sub-srcs">{source_names(s)}</p>{render_section(s, st, editions, tz)}</section>'
            for s, st in items)

    head = f'<div class="sect-head"><h2>{esc(name)}</h2></div>'
    tabs = list(dict.fromkeys(s.get("tab") for s, _ in members if s.get("tab")))
    if not tabs:
        return f'<div class="group">{head}<div class="group-grid">{columns(members)}</div></div>'
    buttons, panels = [], []
    for i, tab in enumerate(tabs):
        slug = re.sub(r"[^a-z0-9]+", "-", tab.lower()).strip("-")
        items = [m for m in members if m[0].get("tab") == tab]
        tab_boxes = "".join(render_box(cfg, data) for cfg, data in boxes if cfg.get("tab") == tab)
        scores = (f'<div class="scores"><h3 class="scores-head">Scores &amp; tables</h3>'
                  f'<div class="scores-grid">{tab_boxes}</div></div>') if tab_boxes else ""
        buttons.append(f'<button type="button" role="tab" id="tab-{slug}" data-tab="{slug}" '
                       f'aria-selected="{"true" if i == 0 else "false"}">{esc(tab)}</button>')
        panels.append(f'<div class="tab-panel" role="tabpanel" data-panel="{slug}" aria-labelledby="tab-{slug}"'
                      f'{"" if i == 0 else " hidden"}><div class="group-grid" style="--cols:{len(items)}">'
                      f'{columns(items)}</div>{scores}</div>')
    return (f'<div class="group tabbed">{head}<div class="tabs" role="tablist" aria-label="{esc(name)}">'
            f'{"".join(buttons)}</div>{"".join(panels)}</div>')


def favicon_uri(emblem: str) -> str:
    """The emblem in ink on a paper-coloured tile, as a data: URI for the browser tab."""
    inner = emblem.split(">", 1)[1].rsplit("</svg>", 1)[0].replace("currentColor", "#17191B")
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 -11 64 64">'
           '<rect x="0" y="-11" width="64" height="64" rx="12" fill="#ECEDE8"/>'
           f'<g fill="none" stroke="#17191B" stroke-width="2.4" stroke-linecap="round">{inner}</g></svg>')
    return "data:image/svg+xml," + urllib.parse.quote(re.sub(r"\s+", " ", svg))


def render_page(config: dict, sections: list[tuple[dict, list[Story]]], weather: dict | None,
                failures: list[str], now: datetime, extras: dict | None = None) -> str:
    extras = extras or {}
    paper = config["paper"]
    tz = ZoneInfo(paper["timezone"])
    editions = editions_for(now)
    emblem = (ROOT / "templates" / "emblem.svg").read_text().strip()
    current = editions[-1]

    def edition_line(ed: Edition) -> str:
        verb = "updated" if ed is current else "printed"
        return f"{ed.name} edition · {verb} {fmt_time(ed.printed, tz)}"

    published = {ed.name: ed for ed in editions}
    buttons = []
    for name, hour in EDITIONS:
        ed = published.get(name)
        if ed:
            pressed = "true" if ed is current else "false"
            buttons.append(f'<button type="button" data-edition="{name}" data-line="{esc(edition_line(ed))}" aria-pressed="{pressed}">{name}</button>')
        else:
            label = time(hour).strftime("%-I%p").lower()
            buttons.append(f'<button type="button" disabled title="Out at {label}">{name}</button>')

    next_slot = next((h for _, h in EDITIONS if datetime.combine(now.date(), time(h), tz) > now + EDITION_GRACE), None)
    if next_slot is None:
        next_line = f"Next edition: {(now + timedelta(days=1)).strftime('%A')} {time(EDITIONS[0][1]).strftime('%-I:%M%p').lower()}"
    else:
        next_line = f"Next edition: {time(next_slot).strftime('%-I:%M%p').lower()}"

    front, watch, groups = "", "", {}
    watch_title = paper.get("watch_title", "Top stories")
    for section, stories in sections:
        if section.get("group") == "front" and not front:
            skip, blocks = {}, []
            for ed in editions:
                top = pick_watch(stories_for_edition(stories, ed, len(stories)), paper.get("watch_limit", 3))
                skip[ed.name] = {id(s) for s in top}
                hidden = "" if ed is current else " hidden"
                blocks.append(f'<div class="feed watch-grid" data-ed="{ed.name}"{hidden}>'
                              + "".join(render_watch_story(s, ed, tz) for s in top) + "</div>")
            watch = (f'<section class="watch" aria-label="{esc(watch_title)}"><div class="watch-head">{emblem}'
                     f'<h2>{esc(watch_title)}</h2><span>The stories that matter most right now</span></div>'
                     + render_alerts(extras.get("alerts", [])) + "".join(blocks) + "</section>")
            front = (f'<section aria-label="{esc(section["title"])}"><div class="sect-head"><h2>{esc(section["title"])}</h2>'
                     f'<span class="srcs">{source_names(section)}</span></div>'
                     f'{render_section(section, stories, editions, tz, skip)}</section>')
        else:
            groups.setdefault(section.get("group", "More"), []).append((section, stories))

    group_html = [render_group(name, members, extras.get("scores", []), editions, tz)
                  for name, members in groups.items()]

    ear, panel = render_weather(weather, paper)
    status = ""
    if failures:
        status = f'<p class="status">Couldn\'t load this edition: {esc("; ".join(failures))}</p>'

    template = Template((ROOT / "templates" / "page.html").read_text())
    return template.substitute(
        paper_name=esc(paper["name"]),
        location=esc(paper["location"]),
        date_line=now.strftime("%A %-d %B %Y"),
        edition_buttons="".join(buttons),
        edition_line=esc(edition_line(current)),
        next_line=esc(next_line),
        weather_ear=ear,
        weather_panel=panel,
        front=front,
        groups="".join(group_html),
        status=status,
        schedule=", ".join(time(h).strftime("%-I%p").lower() for _, h in EDITIONS),
        css=(ROOT / "templates" / "page.css").read_text(),
        emblem=emblem,
        watch=watch,
        favicon=favicon_uri(emblem),
    )


# ---------------------------------------------------------------- main

def load_config(path: Path) -> dict:
    config = tomllib.loads(path.read_text())
    for section in config["section"]:
        section["feeds"] = section.pop("feed", [])
    return config


def collect(config: dict, now: datetime, fetcher=fetch):
    """Fetch everything in parallel. Returns (sections, weather, failures, extras), where
    extras holds the parsed scores boxes and any current weather alerts."""
    paper = config["paper"]
    alerts_cfg = config.get("alerts")
    jobs = {feed["url"] for s in config["section"] for feed in s["feeds"]}
    jobs |= {box["url"] for box in config.get("scores", [])}
    weather_src = weather_url(paper)
    failures: list[str] = []
    results: dict[str, bytes | Exception] = {}
    with cf.ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(fetcher, url): url for url in [*jobs, weather_src]}
        if alerts_cfg:
            futures[pool.submit(fetcher, alerts_cfg["url"], browser=True)] = "alerts:" + alerts_cfg["url"]
        for fut in cf.as_completed(futures):
            url = futures[fut]
            try:
                results[url] = fut.result()
            except Exception as exc:  # noqa: BLE001 - any failure just drops that source
                results[url] = exc

    sections = []
    for section in config["section"]:
        hours = section.get("max_age_hours", paper.get("max_age_hours", 36))
        oldest = now - timedelta(hours=hours)
        stories: list[Story] = []
        for rank, feed in enumerate(section["feeds"]):
            data = results[feed["url"]]
            label = f'{feed["name"]} ({section["title"]})'
            if isinstance(data, Exception):
                failures.append(label)
                print(f"FAIL  {label}: {data}  {feed['url']}", file=sys.stderr)
                continue
            try:
                items = parse_items(data, feed)
            except (ET.ParseError, ValueError) as exc:
                failures.append(label)
                print(f"FAIL  {label}: not a valid feed ({exc})  {feed['url']}", file=sys.stderr)
                continue
            kept = [(pos, i) for pos, i in enumerate(items)
                    if matches_filters(i, feed) and (i["ts"] is None or i["ts"] >= oldest)]
            dated = [i["ts"] for i in items if i["ts"]]
            newest = max(dated).astimezone(now.tzinfo).strftime("%a %-d %b %H:%M") if dated else "no dates"
            print(f"ok    {label}: {len(items)} items, {len(kept)} kept (newest: {newest})")
            stories += [Story(i["title"], i["link"], feed["name"], i["summary"], i["ts"], rank, pos) for pos, i in kept]
        merged = merge_duplicates(stories)
        merged.sort(key=lambda s: s.ts.timestamp() if s.ts else 0, reverse=True)
        print(f"      {section['title']}: {len(stories)} stories -> {len(merged)} after merging duplicates")
        sections.append((section, merged))

    weather = None
    raw = results[weather_src]
    if isinstance(raw, Exception):
        failures.append("weather")
        print(f"FAIL  weather: {raw}", file=sys.stderr)
    else:
        try:
            weather = summarise_weather(json.loads(raw))
            print("ok    weather")
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            failures.append("weather")
            print(f"FAIL  weather: unexpected response ({exc})", file=sys.stderr)

    scores = []
    for box in config.get("scores", []):
        raw = results[box["url"]]
        try:
            if isinstance(raw, Exception):
                raise raw
            scores.append((box, SCORE_KINDS[box["kind"]](raw, box, now)))
            print(f"ok    {box['title']} ({box['tab']})")
        except Exception as exc:  # noqa: BLE001 - unofficial data can change shape; drop just this box
            failures.append(box["title"])
            print(f"FAIL  {box['title']}: {exc}  {box['url']}", file=sys.stderr)

    alerts = []
    if alerts_cfg:
        raw = results["alerts:" + alerts_cfg["url"]]
        try:
            if isinstance(raw, Exception):
                raise raw
            alerts = parse_alerts(raw, alerts_cfg)
            print(f"ok    weather warnings: {len(alerts)} current for this area")
        except Exception as exc:  # noqa: BLE001
            failures.append("weather warnings")
            print(f"FAIL  weather warnings: {exc}", file=sys.stderr)
    return sections, weather, failures, {"scores": scores, "alerts": alerts}


def check_feeds(urls: list[str]) -> int:
    """Report whether each address is a working feed and how fresh it is."""
    for url in urls:
        try:
            data = fetch(url)
            items = parse_espn_news(data) if data.lstrip()[:1] == b"{" else parse_feed(data)
        except Exception as exc:  # noqa: BLE001
            print(f"BROKEN  {url}\n        {exc}")
            continue
        dated = sorted((i["ts"] for i in items if i["ts"]), reverse=True)
        newest = dated[0].strftime("%a %-d %b %Y %H:%M UTC") if dated else "no dates"
        print(f"OK      {url}\n        {len(items)} stories, newest {newest}")
        for item in items[:3]:
            print(f"        - {item['title']}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=ROOT / "sources.toml", type=Path)
    parser.add_argument("--out", default=ROOT / "site" / "index.html", type=Path)
    parser.add_argument("--check", nargs="+", metavar="URL", help="test feed addresses instead of building")
    args = parser.parse_args()
    if args.check:
        return check_feeds(args.check)

    config = load_config(args.config)
    now = datetime.now(ZoneInfo(config["paper"]["timezone"]))
    sections, weather, failures, extras = collect(config, now)
    if not any(stories for _, stories in sections):
        print("No stories loaded from any feed; not overwriting the page.", file=sys.stderr)
        return 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render_page(config, sections, weather, failures, now, extras))
    print(f"Wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
