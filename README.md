# imdb-monkey

A small Python + [Playwright](https://playwright.dev/python/) CLI that automates tasks on IMDB
using your own login. You sign in once manually in a real browser window; the session is saved
to `state.json` and reused for later automations.

The first automation finds a title by name (and optional year) and removes it from your Watchlist.

## Setup

Run `./imdb-monkey` — it creates `.venv`, installs dependencies, and downloads Chromium for
Playwright on first run, then execs the tool. No manual venv activation needed.

(If you'd rather manage the venv yourself: `python3 -m venv .venv && source .venv/bin/activate
&& pip install -r requirements.txt && playwright install chromium`, then run `./imdb-monkey.py`
directly.)

## Usage

### 1. Log in (once)

```
./imdb-monkey login
```

A browser window opens on imdb.com. Sign in manually (handle any Amazon login / 2FA / captcha
yourself), then return to the terminal and press Enter. Your session is saved to `state.json`.

### 2. Remove a title from your Watchlist

Type the title as plain words. Quotes are optional.

```
./imdb-monkey remove Inception 2010
./imdb-monkey remove The Beatles: Get Back
./imdb-monkey remove Blade Runner (1982)
```

The title page opens and you are asked to confirm before anything is removed:
`Remove <title> from your Watchlist? [y/N]`. The default is No, so pressing Enter (or anything
other than `y`/`yes`) cancels without changing your Watchlist.

Preview without even reaching the prompt:

```
./imdb-monkey remove The Beatles: Get Back --dry-run
```

A trailing 4-digit number (either `2010` or `(2010)`) is treated as a *possible* release
year. Because a number can also be part of a title, the tool searches both readings and lets
the IMDB results decide: `Inception 2010` resolves to Inception via the year, while
`Blade Runner 2049` keeps 2049 as part of the title. A lone 4-digit argument (e.g. `2012`) is
always treated as the title.

Options for `remove`:

- `--dry-run`: show the matched title but do not remove it.
- `--headless`: run without a visible browser window (default is headed).
- `--state PATH`: path to the saved session file (default `state.json`).
- `--debug`: print the Watchlist button's attributes/HTML (useful if the tool misreads whether a
  title is on your Watchlist).

### 3. Just find a title (leave the browser open)

Opens the matched title's page in a visible browser and keeps the window open until you press
Enter in the terminal. Same query rules as `remove`. Makes no changes.

```
./imdb-monkey find Blade Runner 2049
./imdb-monkey find Inception 2010
```

Uses your saved session if `state.json` exists (so you can see your Watchlist state); otherwise
it opens logged out.

## Notes

- Matching uses title + optional year. IMDB's search "type" is movie / tvSeries / etc.;
  "documentary" is a genre rather than a search type, so it is not used as a filter — title and
  year are the reliable key.
- `state.json` contains your login cookies. It is gitignored; keep it private.
- The browser uses a normal desktop user-agent. If IMDB ever shows a "Human Verification"
  page (more likely from unusual networks/VPNs), complete it in the headed `login` window;
  the saved session then avoids it on later runs.
