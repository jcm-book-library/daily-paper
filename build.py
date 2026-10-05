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
    also: list[tuple[str, str]] = field(default_factory=list)  # (source, link)


# ---------------------------------------------------------------- fetching

def fetch(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
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
        })
    return items


def matches_filters(item: dict, feed: dict) -> bool:
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


# ---------------------------------------------------------------- rendering

esc = html.escape


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
        links = " · ".join(f'<a href="{esc(link)}">{esc(src)}</a>' for src, link in story.also)
        also = f'<div class="also">Also covered by {links}</div>'
    return (f'<div class="{"lead" if lead else "item"}"><a class="story" href="{esc(story.link)}">'
            f"{kicker}<{tag}>{esc(story.title)}</{tag}>{summary}</a>{also}</div>")


def pick_lead(stories: list[Story]) -> Story:
    """The story most outlets are running; ties go to the top-priority source, then the newest."""
    return max(stories, key=lambda s: (len(s.also), -s.rank, s.ts.timestamp() if s.ts else 0))


def render_section(section: dict, stories: list[Story], editions: list[Edition], tz: ZoneInfo) -> str:
    blocks = []
    for ed in editions:
        chosen = stories_for_edition(stories, ed, section.get("limit", 6))
        hidden = "" if ed is editions[-1] else " hidden"
        if not chosen:
            body = '<p class="empty">No stories from these sources yet today.</p>'
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
        panel = f'<p class="empty">The forecast couldn\'t be loaded this edition.</p><div class="wx-links"><a href="{bom}">Forecast &amp; warnings at BOM</a></div>'
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
        f'<div class="wx-links"><a href="{bom}">Full forecast &amp; warnings at BOM</a><span>Forecast data: Open-Meteo</span></div>'
    )
    return ear, panel


def render_page(config: dict, sections: list[tuple[dict, list[Story]]], weather: dict | None,
                failures: list[str], now: datetime) -> str:
    paper = config["paper"]
    tz = ZoneInfo(paper["timezone"])
    editions = editions_for(now)
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

    front, groups = "", {}
    for section, stories in sections:
        if section.get("group") == "front" and not front:
            front = (f'<section aria-label="{esc(section["title"])}"><div class="sect-head"><h2>{esc(section["title"])}</h2>'
                     f'<span class="srcs">{source_names(section)}</span></div>'
                     f'{render_section(section, stories, editions, tz)}</section>')
        else:
            groups.setdefault(section.get("group", "More"), []).append((section, stories))

    group_html = []
    for name, members in groups.items():
        cols = "".join(
            f'<section aria-label="{esc(s["title"])}"><h3 class="sub">{esc(s["title"])}</h3>'
            f'<p class="sub-srcs">{source_names(s)}</p>{render_section(s, st, editions, tz)}</section>'
            for s, st in members
        )
        group_html.append(f'<div class="group"><div class="sect-head"><h2>{esc(name)}</h2></div>'
                          f'<div class="group-grid">{cols}</div></div>')

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
    )


# ---------------------------------------------------------------- main

def load_config(path: Path) -> dict:
    config = tomllib.loads(path.read_text())
    for section in config["section"]:
        section["feeds"] = section.pop("feed", [])
    return config


def collect(config: dict, now: datetime, fetcher=fetch) -> tuple[list[tuple[dict, list[Story]]], dict | None, list[str]]:
    paper = config["paper"]
    oldest = now - timedelta(hours=paper.get("max_age_hours", 36))
    jobs = {feed["url"] for s in config["section"] for feed in s["feeds"]}
    weather_src = weather_url(paper)
    failures: list[str] = []
    results: dict[str, bytes | Exception] = {}
    with cf.ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(fetcher, url): url for url in [*jobs, weather_src]}
        for fut in cf.as_completed(futures):
            url = futures[fut]
            try:
                results[url] = fut.result()
            except Exception as exc:  # noqa: BLE001 - any failure just drops that source
                results[url] = exc

    sections = []
    for section in config["section"]:
        stories: list[Story] = []
        for rank, feed in enumerate(section["feeds"]):
            data = results[feed["url"]]
            label = f'{feed["name"]} ({section["title"]})'
            if isinstance(data, Exception):
                failures.append(label)
                print(f"FAIL  {label}: {data}  {feed['url']}", file=sys.stderr)
                continue
            try:
                items = parse_feed(data)
            except ET.ParseError as exc:
                failures.append(label)
                print(f"FAIL  {label}: not a valid feed ({exc})  {feed['url']}", file=sys.stderr)
                continue
            kept = [i for i in items if matches_filters(i, feed) and (i["ts"] is None or i["ts"] >= oldest)]
            dated = [i["ts"] for i in items if i["ts"]]
            newest = max(dated).astimezone(now.tzinfo).strftime("%a %-d %b %H:%M") if dated else "no dates"
            print(f"ok    {label}: {len(items)} items, {len(kept)} kept (newest: {newest})")
            stories += [Story(i["title"], i["link"], feed["name"], i["summary"], i["ts"], rank) for i in kept]
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
    return sections, weather, failures


def check_feeds(urls: list[str]) -> int:
    """Report whether each address is a working feed and how fresh it is."""
    for url in urls:
        try:
            items = parse_feed(fetch(url))
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
    sections, weather, failures = collect(config, now)
    if not any(stories for _, stories in sections):
        print("No stories loaded from any feed; not overwriting the page.", file=sys.stderr)
        return 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render_page(config, sections, weather, failures, now))
    print(f"Wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
