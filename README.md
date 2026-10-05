# The Daily Paper

A personal front page in the style of a newspaper. Three times a day it collects
headlines from trusted news and sport feeds, plus the Melbourne forecast, and
publishes them as a single page. Every headline links to the original story.

## How it works

1. GitHub Actions runs `build.py` at 6am, 12pm and 6pm Melbourne time.
2. `build.py` reads `sources.toml`, downloads every feed and the Open-Meteo forecast,
   and merges stories that several outlets ran into one headline with
   "Also covered by" links.
3. It writes `site/index.html`, which GitHub Pages publishes.

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
