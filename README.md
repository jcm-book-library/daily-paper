# The Overnight Sentinel

A personal front page in the style of a newspaper. Three times a day it collects
headlines from trusted news and sport feeds, plus the Melbourne forecast, and
publishes them as a single page. Every headline links to the original story.

## How it works

1. GitHub Actions runs `build.py` at 6am, 12pm and 6pm Melbourne time.
2. `build.py` reads `sources.toml`, downloads every feed and the Open-Meteo forecast,
   and merges stories that several outlets ran into one headline with
   "Also covered by" links.
3. It writes `site/index.html`, which GitHub Pages publishes.

Across the top, **Argus Watch** picks out the day's biggest stories: first the
ones several outlets are running, then the top of ABC's editorially ranked Top
Stories feed. Those stories aren't repeated in the News column below.

The page keeps the day's earlier editions: the **Morning / Afternoon / Evening**
buttons show what the paper looked like at each time, and **NEW** marks stories
that arrived since the previous edition.

## Adding or changing sources

Everything lives in `sources.toml`. To add a feed, copy a `[[section.feed]]` block:

```toml
  [[section.feed]]
  name = "ESPN"
  url = "https://example.com/rss"
  include = ["AFL"]          # optional: keep only stories mentioning these words
```

To add a new sport or topic, copy a whole `[[section]]` block. Sections with
`group = "Sport"` appear together under the Sport heading, three to a row.
Within a section, list feeds in priority order. When two outlets run the same
story, the first-listed outlet's headline is shown.

If a feed fails, the page shows a red "Couldn't load" line naming it, and the
Actions log shows the reason (for example a 404 for a wrong address).

Each section only shows stories from the last 36 hours unless it sets its own
`max_age_hours`. The sport sections use 96 hours (four days), because sport
feeds go quiet between game days.

### Testing a new source

Before adding a feed, check that the address works and is still being updated:

```sh
python build.py --check https://www.theguardian.com/sport/afl/rss
```

This prints how many stories the feed has, the date of the newest one, and its
first few headlines. A feed whose newest story is weeks old has been abandoned,
even if it still loads.

## The emblem

The eye emblem is an original drawing in `templates/emblem.svg`, also used as
the browser-tab icon. To use a different image, replace that file with another
SVG. Only use images you have the right to publish (your own, or ones marked
public domain or CC0), because the page is publicly reachable.

## Running it yourself

Needs Python 3.11 or newer. There are no packages to install.

```sh
python build.py                         # writes site/index.html
python -m unittest discover -s tests    # offline tests
```

To print an edition on demand, open **Actions → Print the paper → Run workflow**.

## Merging duplicate stories

Two headlines are treated as the same story when they share at least three key
words and most of their wording overlaps. This catches the common case of
outlets running near-identical headlines. A story rewritten with completely
different words ("Treasurer cuts forecast" vs "Budget update flags slower growth")
will still appear twice.
