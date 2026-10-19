# The Overnight Sentinel: notes for Claude

**Read the house rules first:** [HOUSE-RULES.md](https://github.com/jcm-book-library/control-room/blob/main/HOUSE-RULES.md) in `control-room` (plain text: https://raw.githubusercontent.com/jcm-book-library/control-room/main/HOUSE-RULES.md). They cover Google Sheet safety, branches and pull requests into `main`, using cheaper models to save usage credits, and the build template. The notes below are this repo's own; where they are more specific, they win. The project charter is `project.json`.

## This repo
- `build.py` reads `sources.toml` and writes `site/index.html`, which GitHub Pages publishes.
- The GitHub Action `.github/workflows/paper.yml` ("Print the paper") runs `build.py` at 6am, 12pm and 6pm Melbourne time; every push runs the tests and a trial build without publishing.
- Sources and scores boxes are all configured in `sources.toml`; check a feed with `python build.py --check <url>`.
- Tests: `python -m unittest discover -s tests`. Needs Python 3.11 or newer, no packages.
- Only use images you have the right to publish, because the page is publicly reachable.
