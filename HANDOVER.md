# Handover: The Overnight Sentinel

Updated 17 Nov 26. Read this first, then `README.md` (how it works and how to add sources) and `CLAUDE.md` (the rules).

## Your next steps (owner)
1. **Check the radar on your phone.** Open https://jcm-book-library.github.io/daily-paper/ and find "Rain radar" under the 7-day forecast. It should loop the last hour. Tap **Open BOM radar**: it should open BOM's Melbourne radar (I couldn't test that link, BOM blocks scripts). If the radar box says it could not load, tell Claude.
2. **Fox Sports** (you planned to add it, it isn't in `sources.toml` yet). On GitHub open `sources.toml`, click the pencil, copy a `[[section.feed]]` block under the sport you want, paste Fox Sports' feed address as the `url`, choose **Create a new branch and start a pull request**, and check the pull request's Checks tab for a line like `ok    Fox Sports (AFL): 20 items`. A `FAIL` line means the address is wrong.
3. **Answer the open questions** at the bottom, especially number 1 (the 6am / 12pm / 6pm timing).

## Where we are
- **Live and working:** https://jcm-book-library.github.io/daily-paper/ (GitHub Pages, public repo).
- **Merged:** #1 name, emblem and Argus Watch (5 Oct 26) · #2 sport pages, scores and tables, BOM warnings, many new sources (5 Oct 26) · #3 project charter (20 Oct 26, another session) · #4 black-and-white look, four sport pages, rain radar (7 Nov 26). `main` is at `4259f96`.
- **Health:** 29 offline tests pass and the build passes. Every push runs the tests plus a trial build against the live feeds. The last 20 scheduled runs (10 to 17 Nov) all succeeded.
- **Known issue:** the scheduled runs succeed but land at about 1–3am, 10–11am and 6–7pm Melbourne time, not at 6am, 12pm and 6pm. See open question 1.

## What the paper does
- **Front page:** News from ABC, SBS, Guardian, BBC and Al Jazeera, with duplicates merged ("Also covered by"). **Argus Watch** on top: stories several outlets run, then ABC Top Stories in order, plus BOM warnings for Melbourne (severe storms, fire danger, heat) while they're current.
- **Weather:** Open-Meteo forecast, then a live rain radar (RainViewer over OpenStreetMap, last hour looped, loaded in the browser), then a BOM link.
- **Sport, four pages:** Football (with Manchester United) · Rugby League · Motorsport (F1 plus other series) · General Sport (AFL, Cricket, NFL, World Sport). Each has headlines plus Scores & tables (ESPN data, Squiggle for the AFL ladder, Cricinfo live scores).
- **Editions:** Morning / Afternoon / Evening buttons keep the day's earlier prints; NEW marks stories since the previous one. Every link opens in a new tab.
- **Look:** black and white, light and dark. Roboto text, Roboto Condensed headings, light blackletter (Grenze Gotisch) for the nameplate and Argus Watch only. The radar is the one coloured thing.

## What changed this session (in order)
1. Built the paper from scratch: pulls feeds, merges duplicates, three editions a day, published by GitHub Actions. Made the repo public (free Pages needs that).
2. Renamed to The Overnight Sentinel, drew the eye emblem, added Argus Watch, switched the masthead to Grenze Gotisch (#1).
3. Added sport pages and scores and tables, BOM warnings, ESPN-data support, Al Jazeera category filter, links in new tabs (#2).
4. Researched radar and API options on 31 Oct (nothing merged; findings are in Decisions).
5. Black-and-white look, four sport pages, one list in two columns for a one-section page, no repeated story on a page, live rain radar, and a symmetry check at five screen widths (#4).

## How it's built
| File | What it is |
|---|---|
| `build.py` | Fetches everything in parallel, merges duplicates, writes `site/index.html`. `python build.py --check <url>` tests one feed. |
| `sources.toml` | All sources, sections, sport pages (`tab`), scores boxes, radar settings. The file you edit. |
| `templates/page.html` | Page skeleton (`$name` placeholders) and the edition and sport-tab buttons. |
| `templates/page.css` | All colours, fonts and layout. Tokens at the top. |
| `templates/radar.js` | The live radar. Inserted into the page as-is. |
| `templates/emblem.svg` | The eye logo (original drawing, also the tab icon). |
| `tests/` | 29 offline tests with sample data in `tests/fixtures/`. |
| `.github/workflows/paper.yml` | Builds on a schedule, on a button press, and on every push (push builds don't publish). |

## Decisions and why
- **Static page, rebuilt on a schedule.** Anything that must be fresh when you open the page (the radar) loads in your browser instead.
- **Public repo, so nothing private in it.** No keys, no emails.
- **RSS first.** ESPN's feeds are empty, so its news is read from ESPN's JSON (`format = "espn"`). Al Jazeera has one feed, split by `category`. SBS uses `/news/topic/latest/feed` (the old address went stale in Aug 26). The Roar was dropped (abandoned Jan 26). Reuters, NRL.com, Premier League and Man Utd have no feed; I did not scrape them and have not read their terms.
- **Duplicates** are merged when headlines share 3+ key words and most of their wording. Rewordings ("Treasurer cuts forecast") slip through.
- **BOM:** the warnings feed only answers browser-like requests. BOM's own radar images and pages are not used (blocked to scripts, terms unread), so the paper links out to them.
- **Radar:** RainViewer (radar) and OpenStreetMap (map), zoom 7 (the free radar tiles stop there), greyscale map, last 6 frames. If it fails the box shows a message and the BOM link.
- **Look:** one 36px gutter with a hairline centred in it, equal-width columns, inverted (white on black) for warnings and NEW tags instead of colour.
- **Sport pages:** sections sharing a `tab` sit on one page; a story that two sections of one page would both show appears once. Sport looks back 96 hours (Motorsport 168).
- **Date line** keeps the newspaper style ("Monday 5 October 2026") instead of the house `19 Oct 26`.
- **Commits** carry no model name (house rules). The earliest commits do; fixing that needs a force-push.

## Gotchas
- **This container can't reach news sites** (the proxy returns 403), so `build.py` can't run for real here. Use the trial build GitHub runs on every push (its log only opens once the job finishes, and the run list can lag), or sample data from `tests/fixtures/`. For anything needing the real internet, push a throwaway branch with a temporary workflow step (done for the radar; `claude/radar-check` can be ignored).
- **The browser check scripts are not in the repo.** The sample-data page, mock map tiles and the symmetry, overlap and clipped-text measurements lived in a session scratch folder. They used headless Chromium at `/opt/pw-browsers/chromium`, and real fonts from npm `@fontsource` packages.
- **`git fetch origin main` doesn't update `origin/main` here.** Use `git fetch origin +refs/heads/main:refs/remotes/origin/main`.
- **Chain `cd` with `&&`.** A failed `cd` followed by `;` ran git in the wrong repo once (no harm done).
- **Use `<<'EOF'` for heredocs.** In an unquoted one, backticks and `$` run as commands.
- **Force-push is blocked.** After a merge, start from `main`; a push of new commits on top of the old branch tip is a normal fast-forward.
- **Padding counts toward width** unless `box-sizing: border-box`. Two header boxes overlapped on phones because of it (fixed on `.ear`). A page-wide reset would stop it recurring.
- **ESPN's data is unofficial.** NRL stories have no dates (never get NEW); IndyCar is quiet off-season; ABC's cricket tag is empty off-season. A broken scores box drops out alone.
- **The `Melbourne now` temperature in the masthead is the reading at print time**, up to hours old when you view it.
- **The saved sport tab** (browser storage key `sentinel-sport`) is ignored if its name no longer exists.
- **No keys are stored.** The Guardian's shared test key is now rejected, so a real key would be needed (kept in GitHub Secrets, used only while building).
- **GitHub warns that the Node 20 actions** (checkout, setup-python, upload and deploy pages) are being retired.

## Open questions (for you)
1. **Timing.** Scheduled runs land around 2am, 10:30am and 6:15pm, so at 6am you see last night's paper and the Afternoon edition rarely appears. Cause not confirmed (GitHub delays or drops scheduled runs). Do you want editions at true 6 / 12 / 6, and is it worth effort to chase?
2. **Radar services.** Happy to rely on RainViewer and OpenStreetMap? Their terms are unread beyond crediting them.
3. **Date style.** Keep the long newspaper date line, or switch to `19 Oct 26`?
4. **Old commit messages.** Leave the model names in the first commits, or rewrite history (needs a force-push you'd have to allow)?
5. **ESPN IndyCar.** Keep it while it's off-season?
6. **Learning track.** Which next: an issue that opens itself when a feed breaks, a required-checks rule on `main`, an AI "editor's note", or a Guardian API key?

## Next steps for Claude (suggested order)
1. **Find out why runs are late or missing**, using the run times in the Actions tab, and propose a fix (more schedule slots, or a different trigger).
2. **Save the browser check scripts** into the repo (`tools/qa/`) with a short README, so the symmetry check is repeatable.
3. **Update the GitHub Actions versions** (Node 20 warning). A good first exercise for you to review.
4. **Make "Melbourne now" honest:** either fetch the current temperature in the browser (Open-Meteo, untested from a browser) or label it "as at 6:05pm".
5. **Review your Fox Sports pull request** when it's up.
6. **Ideas, not started:** Guardian API (needs a key), Google News RSS searches for sites without feeds (untested, unofficial), a self-opening issue when a feed fails, an AI editor's note.
