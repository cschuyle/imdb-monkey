#!/usr/bin/env python3
"""imdb-monkey: automate tasks on IMDB using your own saved login session.

Subcommands:
  login   Open a browser, sign in manually, and save the session to state.json.
  remove  Find a title by name (and optional year) and remove it from your Watchlist.
"""

import argparse
import os
import re
import sys
import time
import urllib.parse

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError
except ImportError:
    sys.exit(
        "Playwright is not installed. Run:\n"
        "  pip install -r requirements.txt\n"
        "  playwright install chromium"
    )

IMDB_HOME = "https://www.imdb.com/"
DEFAULT_STATE = "state.json"

# A realistic desktop UA + Accept-Language avoids IMDB's plain 403 for the
# default automation user-agent.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


def new_context(browser, storage_state=None):
    return browser.new_context(
        storage_state=storage_state,
        user_agent=USER_AGENT,
        locale="en-US",
        extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
    )


# --------------------------------------------------------------------------- #
# login
# --------------------------------------------------------------------------- #
def cmd_login(args):
    state_path = args.state
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = new_context(browser)
        page = context.new_page()
        page.goto(IMDB_HOME, wait_until="domcontentloaded")

        print("A browser window is open on imdb.com.")
        print("Sign in manually (handle any Amazon login / 2FA / captcha yourself).")
        try:
            input("When you are fully signed in, come back here and press Enter to save the session... ")
        except (EOFError, KeyboardInterrupt):
            print("\nAborted; session not saved.")
            browser.close()
            return 1

        context.storage_state(path=state_path)
        print(f"Session saved to {state_path}")
        browser.close()
    return 0


# --------------------------------------------------------------------------- #
# search helpers
# --------------------------------------------------------------------------- #
_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")


def _normalize(title):
    return re.sub(r"\s+", " ", title.strip().lower())


# Scrapes the find-results page for title id/title/meta and whether each poster's
# Watchlist ribbon shows the "added" (checkmark) state.
_FIND_RESULTS_JS = """
() => {
  // Does this result's poster show the "on watchlist" ribbon (the checkmark in
  // the corner)? We only trust affirmative signals; anything ambiguous counts
  // as not-on-watchlist.
  const onWatchlist = (container) => {
    if (!container) return false;
    const sels = [
      '[data-testid*="watchlist" i]',
      '.ipc-watchlist-ribbon',
      'button[aria-label*="watchlist" i]',
      '[aria-label*="watchlist" i]',
    ];
    let el = null;
    for (const s of sels) { el = container.querySelector(s); if (el) break; }
    if (!el) return false;
    const label = (el.getAttribute('aria-label') || '').toLowerCase();
    if (el.getAttribute('aria-pressed') === 'true') return true;
    if (el.getAttribute('aria-checked') === 'true') return true;
    const onWords = ['remove', 'added', 'in watchlist', 'on watchlist', 'in your watchlist'];
    if (onWords.some(w => label.includes(w))) return true;
    const cls = (el.getAttribute('class') || '').toLowerCase();
    if (cls.includes('added') || cls.includes('onlist') || cls.includes('inwatchlist')) return true;
    if (el.querySelector('[data-testid*="checkmark" i], svg[class*="checkmark" i]')) return true;
    return false;
  };
  const root =
    document.querySelector('[data-testid="find-results-section-title"]') ||
    document.querySelector('main') ||
    document;
  const out = [];
  const seen = new Set();
  for (const a of root.querySelectorAll('a[href*="/title/tt"]')) {
    const href = a.getAttribute('href') || '';
    const m = href.match(/\\/title\\/(tt\\d+)/);
    if (!m) continue;
    const id = m[1];
    const title = (a.textContent || '').trim();
    if (!title) continue;
    const container =
      a.closest('li, .ipc-metadata-list-summary-item, [class*="find-result"]') ||
      a.parentElement;
    const meta = container ? (container.textContent || '').trim() : '';
    const key = id + '|' + title;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({ id, title, meta, in_watchlist: onWatchlist(container) });
  }
  return out;
}
"""


def _finalize_results(raw):
    for item in raw:
        m = _YEAR_RE.search(item.get("meta", ""))
        item["year"] = int(m.group(0)) if m else None
        item["in_watchlist"] = bool(item.get("in_watchlist"))
    return raw


def search_titles(page, query):
    """Return a list of {id, title, meta, year, in_watchlist} dicts from IMDB.

    The poster Watchlist ribbons load their added/removed state asynchronously
    after the results render, so reading the DOM immediately makes everything
    look "not on the Watchlist". We poll until a checkmark appears (or a minimum
    settle time passes when nothing is on the list) so the checkmark is accurate
    before it is used for selection and membership.
    """
    url = "https://www.imdb.com/find/?" + urllib.parse.urlencode({"q": query, "s": "tt"})
    page.goto(url, wait_until="domcontentloaded")

    # Results render client-side; wait for at least one title link if possible.
    try:
        page.wait_for_selector('a[href*="/title/tt"]', timeout=15000)
    except PWTimeoutError:
        return []

    raw = page.evaluate(_FIND_RESULTS_JS)
    hard_deadline = time.monotonic() + 6.0
    min_settle_until = time.monotonic() + 2.5
    while time.monotonic() < hard_deadline:
        if any(r.get("in_watchlist") for r in raw):
            break
        if time.monotonic() >= min_settle_until:
            break
        page.wait_for_timeout(300)
        raw = page.evaluate(_FIND_RESULTS_JS)

    # Let any remaining ribbons finish flipping, then take a final reading.
    page.wait_for_timeout(300)
    raw = page.evaluate(_FIND_RESULTS_JS)
    return _finalize_results(raw)


def _title_matches(result, interpretations):
    """True if the result's title is a close match for any interpretation.

    Close means an exact normalized match or one title contained in the other
    (so "Doctor Zhivago" matches the query "doctor zhivago 2002").
    """
    title_norm = _normalize(result["title"])
    for wanted_title, _ in interpretations:
        want = _normalize(wanted_title)
        if not want:
            continue
        if title_norm == want or want in title_norm or title_norm in want:
            return True
    return False


def pick_match(results, interpretations, wanted_year):
    """Choose a result. IMDB's relevance order (topmost) is the default.

    When a year was given, results whose year is known and different are never
    selected; results with an unknown (None) year stay eligible since the find
    page often omits it.

    Overrides, in priority order:
      1. A close title match that the find page shows as already on the
         Watchlist (only reliable when the ribbon state has loaded; treated as
         a best-effort hint, never as a veto).
      2. When a year was given, a close title match whose year equals it.

    Membership is verified for real later on the title page, so this only needs
    to pick the most plausible candidate.
    """
    if not results:
        return None

    pool = results
    if wanted_year is not None:
        pool = [r for r in results if r.get("year") in (None, wanted_year)]
        if not pool:
            return None

    for r in pool:
        if r.get("in_watchlist") and _title_matches(r, interpretations):
            return r

    if wanted_year is not None:
        for r in pool:
            if r.get("year") == wanted_year and _title_matches(r, interpretations):
                return r

    return pool[0]


# --------------------------------------------------------------------------- #
# watchlist helpers
# --------------------------------------------------------------------------- #
HERO_WL_SELECTOR = '[data-testid="tm-box-wl-button"]'


def find_watchlist_button(page):
    """Return a locator for the title-page hero Watchlist button, or None.

    The hero button (``tm-box-wl-button``) is tried first so we never grab a
    Watchlist ribbon from a recommendations rail elsewhere on the page.
    """
    selectors = [
        HERO_WL_SELECTOR,
        'button[aria-label*="watchlist" i]',
        'button.ipc-watchlist-ribbon',
        '[aria-label*="watchlist" i]',
    ]
    for sel in selectors:
        loc = page.locator(sel)
        try:
            if loc.count() > 0:
                return loc.first
        except Exception:
            continue
    return None


def wait_for_watchlist_status(page, timeout_ms=5000):
    """Wait until the hero Watchlist button reports it is on the Watchlist.

    IMDB server-renders the button as "Add to Watchlist" and only flips it to
    the added state after an async logged-in status call. Callers only reach
    here for titles the search page already showed as on the Watchlist, so we
    poll the button and return as soon as it reads 'on'. This avoids the old
    ``networkidle`` wait, which blocked for seconds on unrelated background
    traffic (ads/analytics) instead of on the button's actual state.
    """
    try:
        page.wait_for_selector(HERO_WL_SELECTOR, timeout=timeout_ms)
    except PWTimeoutError:
        return
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        button = find_watchlist_button(page)
        if button is not None and watchlist_state(button) == "on":
            return
        page.wait_for_timeout(200)


def describe_button(button):
    """Collect the button's state signals for debugging."""
    def attr(name):
        try:
            return button.get_attribute(name)
        except Exception:
            return None

    try:
        text = button.inner_text()
    except Exception:
        text = None
    try:
        outer = button.evaluate("el => el.outerHTML")
    except Exception:
        outer = None
    return {
        "aria-label": attr("aria-label"),
        "aria-pressed": attr("aria-pressed"),
        "aria-checked": attr("aria-checked"),
        "title": attr("title"),
        "text": text,
        "outerHTML": outer,
    }


# Text signals that indicate the title is currently ON the Watchlist. Note that
# "added" is deliberately excluded: the button carries an "Added by NNN users"
# popularity count in every state, so matching "added" would misread the 'off'
# state as 'on'.
_ON_SIGNALS = ("remove from watchlist", "in watchlist", "on watchlist", "in your watchlist")


def watchlist_state(button):
    """Return 'on', 'off', or 'unknown' for whether the title is on the Watchlist.

    ``aria-pressed``/``aria-checked`` are authoritative on IMDB's Watchlist
    buttons (true=on, false=off), so they are trusted first. Text is only a
    fallback for buttons that expose no pressed/checked state.
    """
    pressed = button.get_attribute("aria-pressed")
    checked = button.get_attribute("aria-checked")
    if pressed == "true" or checked == "true":
        return "on"
    if pressed == "false" or checked == "false":
        return "off"

    label = (button.get_attribute("aria-label") or "").lower()
    try:
        text = (button.inner_text() or "").lower()
    except Exception:
        text = ""
    title_attr = (button.get_attribute("title") or "").lower()
    combined = f"{label} {text} {title_attr}"

    if "add to watchlist" in combined:
        return "off"
    if any(sig in combined for sig in _ON_SIGNALS):
        return "on"
    return "unknown"


# --------------------------------------------------------------------------- #
# remove
# --------------------------------------------------------------------------- #
_YEAR_TOKEN_RE = re.compile(r"^\(?(\d{4})\)?$")


def parse_query(tokens):
    """Split the trailing 4-digit (or (4-digit)) token off as the year.

    All tokens are joined with single spaces to form the title. If the last
    token is 4 digits or 4 digits in parens (e.g. 1994 or (1994)) and there is
    at least one other token, it is taken as the year. A lone 4-digit token is
    treated as the title (so a title like "2012" still searches correctly).
    """
    year = None
    if len(tokens) > 1:
        m = _YEAR_TOKEN_RE.match(tokens[-1])
        if m:
            year = int(m.group(1))
            tokens = tokens[:-1]
    return " ".join(tokens), year


def gather_results(page, query_tokens):
    """Search IMDB and return (results, interpretations, wanted_year, full_title).

    Leaves ``page`` on the last search-results page it loaded.
    """
    full_title = " ".join(query_tokens)
    stripped_title, year = parse_query(query_tokens)

    # The trailing number might be a year or part of the title. Consider both.
    interpretations = [(full_title, None)]
    if year is not None:
        interpretations.append((stripped_title, year))

    results = search_titles(page, full_title)
    # The trailing year makes the initial query specific. If it finds nothing,
    # a broader search without the year would only surface wrong-year entries,
    # so skip it. Only fall back to the year-less search when the initial one
    # actually returned results.
    if year is not None and stripped_title != full_title and results:
        seen = {r["id"] for r in results}
        for r in search_titles(page, stripped_title):
            if r["id"] not in seen:
                seen.add(r["id"])
                results.append(r)

    return results, interpretations, year, full_title


def resolve_query(page, query_tokens, debug=False):
    """Search IMDB for the query and return (match_or_None, full_title)."""
    results, interpretations, year, full_title = gather_results(page, query_tokens)
    if not results:
        return None, full_title

    match = pick_match(results, interpretations, year)
    if debug:
        print("[debug] Search candidates (in results order):")
        for i, r in enumerate(results):
            flag = " *on-watchlist*" if r.get("in_watchlist") else ""
            chosen = " <== chosen" if match is r else ""
            print(f"  [{i}] {r['title']} ({r.get('year')}) {r['id']}{flag}{chosen}")
    return match, full_title


def read_query_file(path):
    """Return a list of token-lists, one per non-blank line in the file.

    Each line is a full title query that may contain spaces (and an optional
    trailing year), so a line is split on whitespace into tokens the same way
    command-line arguments are. Blank and whitespace-only lines are skipped.
    """
    queries = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            tokens = line.split()
            if tokens:
                queries.append(tokens)
    return queries


def remove_one(page, query_tokens, args):
    """Resolve, confirm, and remove a single title. Return 0 on success/skip, 1 on failure."""
    match, full_title = resolve_query(page, query_tokens, debug=args.debug)
    if match is None:
        print(f"No titles found for {full_title!r}.")
        return 1

    year_str = f" ({match['year']})" if match.get("year") else ""
    print(f"Matched: {match['title']}{year_str}  [{match['id']}]")

    # The search page's Watchlist checkmark (loaded fully before selection) is
    # the signal for whether this specific title is on the Watchlist.
    if not match.get("in_watchlist"):
        print("Not in watchlist")
        return 0

    page.goto(
        f"https://www.imdb.com/title/{match['id']}/",
        wait_until="domcontentloaded",
    )
    wait_for_watchlist_status(page)

    button = find_watchlist_button(page)
    if button is None:
        print("Could not locate the Watchlist button on the title page.")
        return 1

    if args.debug:
        print("[debug] Watchlist button:")
        for k, v in describe_button(button).items():
            print(f"  {k}: {v!r}")

    state = watchlist_state(button)
    if state == "off":
        print("Not in watchlist")
        if args.debug:
            print("[debug] Read state as 'off'; if that is wrong, share the button info above.")
        return 0
    if state == "unknown":
        print(
            "Warning: could not determine Watchlist state from the button; "
            "proceeding to confirm before clicking it to remove."
        )

    if args.dry_run:
        print("[dry-run] Would remove this title from your Watchlist.")
        return 0

    prompt = f"Remove {match['title']}{year_str} from your Watchlist? [y/N] "
    try:
        answer = input(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer not in ("y", "yes"):
        print("Cancelled; nothing removed.")
        return 0

    button.click()

    # Verify the state flipped to off. IMDB updates (and often re-renders)
    # the button asynchronously, so poll a fresh locator until it reports
    # 'off' rather than reading a possibly-stale button once.
    new_state = "unknown"
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        page.wait_for_timeout(300)
        button = find_watchlist_button(page) or button
        new_state = watchlist_state(button)
        if new_state == "off":
            break
    if new_state == "off":
        print("Removed from your Watchlist.")
        return 0
    if new_state == "unknown":
        print("Clicked the Watchlist button; could not confirm the new state.")
        return 0
    print("Clicked the Watchlist button, but it still appears to be on your Watchlist.")
    return 1


def cmd_remove(args):
    state_path = args.state
    if not os.path.exists(state_path):
        print(f"No saved session found at {state_path}. Run './imdb-monkey.py login' first.")
        return 1

    queries = []
    if args.file:
        try:
            queries.extend(read_query_file(args.file))
        except OSError as e:
            print(f"Could not read {args.file!r}: {e}")
            return 1
    if args.query:
        queries.append(args.query)

    if not queries:
        print("No titles to remove. Give a title as arguments or use -f FILE.")
        return 1

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=args.headless)
        context = new_context(browser, storage_state=state_path)
        page = context.new_page()

        result = 0
        multiple = len(queries) > 1
        for idx, query_tokens in enumerate(queries):
            if multiple:
                print(f"\n[{idx + 1}/{len(queries)}] {' '.join(query_tokens)}")
            if remove_one(page, query_tokens, args) != 0:
                result = 1

        browser.close()
        return result


# --------------------------------------------------------------------------- #
# find
# --------------------------------------------------------------------------- #
def cmd_find(args):
    state_path = args.state
    storage = state_path if os.path.exists(state_path) else None

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = new_context(browser, storage_state=storage)
        page = context.new_page()

        match, full_title = resolve_query(page, args.query)
        if match is None:
            print(f"No titles found for {full_title!r}; leaving the search page open.")
        else:
            year_str = f" ({match['year']})" if match.get("year") else ""
            print(f"Found: {match['title']}{year_str}  [{match['id']}]")
            page.goto(
                f"https://www.imdb.com/title/{match['id']}/",
                wait_until="domcontentloaded",
            )

        if storage is None:
            print("(Not logged in. Run './imdb-monkey.py login' first to see Watchlist state.)")

        try:
            input("Browser is open. Press Enter here to close it... ")
        except (EOFError, KeyboardInterrupt):
            pass
        browser.close()
    return 0


# --------------------------------------------------------------------------- #
# cli
# --------------------------------------------------------------------------- #
def build_parser():
    parser = argparse.ArgumentParser(
        prog="imdb-monkey",
        description="Automate IMDB tasks using your own saved login session.",
    )
    parser.add_argument(
        "--state",
        default=DEFAULT_STATE,
        help=f"Path to the saved session file (default: {DEFAULT_STATE}).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_login = sub.add_parser("login", help="Sign in manually and save the session.")
    p_login.set_defaults(func=cmd_login)

    p_remove = sub.add_parser(
        "remove",
        help="Remove a title from your Watchlist.",
        description=(
            "Give the title as plain words, optionally ending with a 4-digit year "
            "like 1994 or (1994). Example: remove The Matrix 1999"
        ),
    )
    p_remove.add_argument(
        "query",
        nargs="*",
        help="Title words, optionally followed by a 4-digit year (e.g. 1994 or (1994)).",
    )
    p_remove.add_argument(
        "-f",
        "--file",
        help=(
            "Path to a file listing one title per line (each line may contain "
            "spaces and an optional trailing year). Removes each in turn."
        ),
    )
    p_remove.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the matched title but do not remove it.",
    )
    p_remove.add_argument(
        "--headless",
        action="store_true",
        help="Run without a visible browser window (default is headed).",
    )
    p_remove.add_argument(
        "--debug",
        action="store_true",
        help="Print the Watchlist button's attributes/HTML for troubleshooting.",
    )
    p_remove.set_defaults(func=cmd_remove)

    p_find = sub.add_parser(
        "find",
        help="Find a title and leave the browser window open on it.",
        description=(
            "Search for a title (same query rules as 'remove'), open its page in a "
            "visible browser, and keep the window open until you press Enter."
        ),
    )
    p_find.add_argument(
        "query",
        nargs="+",
        help="Title words, optionally followed by a 4-digit year (e.g. 1994 or (1994)).",
    )
    p_find.set_defaults(func=cmd_find)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
