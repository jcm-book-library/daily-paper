"""Offline tests: run with `python -m unittest` from the repo root."""
import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import build  # noqa: E402

MEL = ZoneInfo("Australia/Melbourne")
FIXTURES = Path(__file__).resolve().parent / "fixtures"

RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Top Stories</title>
<item>
  <title><![CDATA[Budget update flags slower growth as global trade cools]]></title>
  <link>https://example.org/abc/budget</link>
  <description><![CDATA[<p>Treasury revises its forecasts down &amp; points to weaker demand.</p>]]></description>
  <pubDate>Mon, 05 Oct 2026 06:41:00 GMT</pubDate>
</item>
<item>
  <title>No link here</title>
  <pubDate>Mon, 05 Oct 2026 06:00:00 GMT</pubDate>
</item>
</channel></rss>"""

ATOM = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Example</title>
<entry>
  <title>Slower growth flagged in budget update as global trade cools</title>
  <link rel="alternate" href="https://example.org/sbs/budget"/>
  <summary>Same story, different outlet.</summary>
  <updated>2026-10-05T06:12:00Z</updated>
</entry>
</feed>"""


def story(title, source="ABC", rank=0, minutes_ago=10, now=None):
    now = now or datetime(2026, 10, 5, 17, 55, tzinfo=MEL)
    return build.Story(title, f"https://example.org/{source}/{abs(hash(title))}", source, "", now - timedelta(minutes=minutes_ago), rank)


class ParseTests(unittest.TestCase):
    def test_rss(self):
        items = build.parse_feed(RSS)
        self.assertEqual(len(items), 1)  # the item without a link is dropped
        self.assertEqual(items[0]["title"], "Budget update flags slower growth as global trade cools")
        self.assertEqual(items[0]["summary"], "Treasury revises its forecasts down & points to weaker demand.")
        self.assertEqual(items[0]["ts"], datetime(2026, 10, 5, 6, 41, tzinfo=timezone.utc))

    def test_atom(self):
        [item] = build.parse_feed(ATOM)
        self.assertEqual(item["link"], "https://example.org/sbs/budget")
        self.assertEqual(item["ts"], datetime(2026, 10, 5, 6, 12, tzinfo=timezone.utc))

    def test_summary_trims_long_text_and_guardian_suffix(self):
        self.assertEqual(build.make_summary("Short and sweet. Continue reading...", "Title"), "Short and sweet.")
        long = "First sentence is here and it runs on for quite a while to set the scene properly. " * 5
        out = build.make_summary(long, "Title")
        self.assertLessEqual(len(out), build.SUMMARY_MAX + 1)
        self.assertTrue(out.endswith("."))

    def test_summary_dropped_when_it_repeats_the_title(self):
        self.assertEqual(build.make_summary("Same as title", "Same as title"), "")

    def test_filters(self):
        feed = {"include": ["NRL"], "exclude": ["quiz"]}
        self.assertTrue(build.matches_filters({"title": "NRL grand final", "summary": ""}, feed))
        self.assertFalse(build.matches_filters({"title": "AFL grand final", "summary": ""}, feed))
        self.assertFalse(build.matches_filters({"title": "NRL quiz", "summary": ""}, feed))


class MergeTests(unittest.TestCase):
    def test_same_story_from_two_outlets_is_merged_under_priority_source(self):
        merged = build.merge_duplicates([
            story("Slower growth flagged in budget update as global trade cools", "SBS", rank=1),
            story("Budget update flags slower growth as global trade cools", "ABC", rank=0),
        ])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].source, "ABC")
        self.assertEqual([s for s, _ in merged[0].also], ["SBS"])

    def test_different_stories_sharing_a_couple_of_words_stay_apart(self):
        merged = build.merge_duplicates([
            story("Grand final wash-up: five things we learned", "Guardian"),
            story("Grand final preview: who wins the big one", "ABC Sport", rank=1),
        ])
        self.assertEqual(len(merged), 2)

    def test_same_source_twice_is_not_listed_as_also(self):
        merged = build.merge_duplicates([
            story("Storm warning issued for Melbourne and Geelong tonight", "ABC"),
            story("Storm warning issued for Melbourne and Geelong tonight", "ABC"),
        ])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].also, [])


class WatchTests(unittest.TestCase):
    def test_shared_stories_first_then_top_ranked_from_first_feed(self):
        shared = story("Budget update flags slower growth as global trade cools")
        shared.also = [("SBS", "https://example.org/sbs")]
        busier = story("Storm warning issued for Melbourne and Geelong tonight", minutes_ago=300)
        busier.also = [("SBS", "x"), ("Guardian", "y")]
        abc_second = story("Second-ranked ABC story about the train network", rank=0)
        abc_second.position = 1
        abc_first = story("Top-ranked ABC story about the hospital funding deal", rank=0, minutes_ago=600)
        abc_first.position = 0
        guardian = story("A Guardian story that nobody else ran this morning", source="Guardian", rank=2)
        picked = build.pick_watch([guardian, abc_second, shared, abc_first, busier], 4)
        self.assertEqual(picked, [busier, shared, abc_first, abc_second])

    def test_limit(self):
        stories = [story(f"Story number {n} about something", rank=0) for n in range(5)]
        self.assertEqual(len(build.pick_watch(stories, 3)), 3)


class EditionTests(unittest.TestCase):
    def test_evening(self):
        eds = build.editions_for(datetime(2026, 10, 5, 18, 5, tzinfo=MEL))
        self.assertEqual([e.name for e in eds], ["Morning", "Afternoon", "Evening"])
        self.assertEqual(eds[-1].previous.hour, 12)

    def test_early_run_counts_as_upcoming_edition(self):
        eds = build.editions_for(datetime(2026, 10, 5, 5, 50, tzinfo=MEL))
        self.assertEqual([e.name for e in eds], ["Morning"])
        self.assertEqual(eds[0].previous, datetime(2026, 10, 4, 18, tzinfo=MEL))

    def test_before_morning_shows_last_night(self):
        eds = build.editions_for(datetime(2026, 10, 5, 3, 0, tzinfo=MEL))
        self.assertEqual([e.name for e in eds], ["Evening"])

    def test_new_flag_and_cutoff(self):
        now = datetime(2026, 10, 5, 18, 5, tzinfo=MEL)
        morning, afternoon, evening = build.editions_for(now)
        s = story("Something happened this afternoon", minutes_ago=0, now=datetime(2026, 10, 5, 13, 0, tzinfo=MEL))
        self.assertEqual(build.stories_for_edition([s], afternoon, 10), [])  # 1pm story missed the noon edition
        self.assertEqual(build.stories_for_edition([s], evening, 10), [s])
        self.assertTrue(build.is_new(s, evening))
        early = story("Overnight story", minutes_ago=0, now=datetime(2026, 10, 5, 3, 0, tzinfo=MEL))
        self.assertTrue(build.is_new(early, morning))
        self.assertFalse(build.is_new(early, afternoon))


class WeatherTests(unittest.TestCase):
    def test_summary(self):
        w = build.summarise_weather(json.loads((FIXTURES / "open-meteo.json").read_text()))
        self.assertEqual(w["today"]["max"], 19)
        self.assertEqual(w["wind"], "SW 20")
        self.assertEqual(w["uv"], "6 High")
        self.assertIn("Hot on Thu", w["note"])
        self.assertIn("Rain likely on Fri", w["note"])


class RenderTests(unittest.TestCase):
    def test_full_page_from_fixture_feeds(self):
        config = build.load_config(build.ROOT / "sources.toml")
        now = datetime(2026, 10, 5, 18, 5, tzinfo=MEL)
        abc = (FIXTURES / "sample-feed.xml").read_bytes()
        weather = (FIXTURES / "open-meteo.json").read_bytes()

        def fake_fetch(url):
            if "open-meteo" in url:
                return weather
            if "sbs" in url:
                raise OSError("HTTP Error 404")
            return abc

        sections, w, failures = build.collect(config, now, fetcher=fake_fetch)
        page = build.render_page(config, sections, w, failures, now)
        self.assertIn("<title>The Overnight Sentinel</title>", page)
        self.assertIn('rel="icon" href="data:image/svg+xml,', page)
        self.assertIn("Argus Watch", page)
        # a story in the Argus Watch band isn't repeated in the News column below it
        evening_news = page.split('aria-label="News"')[1].split('data-ed="Evening"')[1].split("</section>")[0]
        evening_watch = page.split('class="watch"')[1].split('data-ed="Evening"')[1].split("</section>")[0]
        self.assertIn("Budget update flags slower growth", evening_watch)
        self.assertNotIn("Budget update flags slower growth", evening_news)
        self.assertIn("Evening edition · updated 6:05pm", page)
        self.assertIn("Couldn't load this edition: SBS (News)", page)
        self.assertIn("Full forecast &amp; warnings at BOM", page)
        self.assertNotIn("$", page.split("<script>")[0])  # every placeholder filled


if __name__ == "__main__":
    unittest.main()
