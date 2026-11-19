"""Remove expired X posts from your profile."""
import argparse
import base64
import copy
import csv
import json
import logging
import ntpath
import os
import random
import re
import socket
import subprocess
import sys
import threading
import time
import weakref
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import time as wall_clock
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import urlopen

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

DATA_DIR = Path(__file__).resolve().parent

UTC = timezone.utc  # noqa: UP017

# Timeline articles include nested quoted articles.
ARTICLE = 'article[data-testid="tweet"]'
# The author header identifies the outer article's author.
AUTHOR = '[data-testid="User-Name"] a[href]'
# Status timestamps identify canonical post links.
TIMESTAMP = 'a[href*="/status/"] time[datetime]'
# Social context marks pins and reposts.
CONTEXT = '[data-testid="socialContext"]'
# Post text supplies the audit record.
TEXT = '[data-testid="tweetText"]'
# The caret opens the owned post menu.
CARET = '[data-testid="caret"]'
# Delete is an accessible menu item.
DELETE = '[role="menuitem"]'
DELETE_TEXT = re.compile(r'^Delete$')
# Confirmation completes deletion.
CONFIRM = '[data-testid="confirmationSheetConfirm"]'
CONFIRM_CLICK = """button => {
    if (window.__cleanupTouched === true) return false;
    button.click();
    return true;
}"""
# The active repost button opens the undo menu.
UNREPOST = '[data-testid="unretweet"]'
# Confirmation completes undoing a repost.
UNDO_CONFIRM = '[data-testid="unretweetConfirm"]'
# Profile navigation supplies a second account source.
PROFILE = '[data-testid="AppTabBar_Profile_Link"]'
ACCOUNT_SWITCHER = '[data-testid="SideNav_AccountSwitcher_Button"]'
VIEWPORT = {'width': 1280, 'height': 1000}
# Login fields and challenge frames require manual intervention.
AUTH = 'input[autocomplete="username"], input[type="password"], iframe[src*="captcha"], iframe[src*="challenge"], [data-testid="LoginForm_Login_Button"]'
LOGGER = logging.getLogger(__name__)
CLOSEST_ARTICLE = 'article'
STATUS_LINK = 'a[href*="/status/"]'
QUOTE = '[data-testid="quoteTweet"], [role="link"][tabindex="0"]'
LANGUAGE = 'html'
EMPTY = '[data-testid="emptyState"]'
# X renders visible timeline cells only after the tab loads; a tab without posts shows suggestion cells.
# The first cell is often an empty hidden spacer, so the wait looks for any visible match.
TIMELINE_CELL = '[data-testid="primaryColumn"] section[role="region"] [data-testid="cellInnerDiv"]'
LOADED = f'{ARTICLE}, {EMPTY}, {TIMELINE_CELL}'
TABS = (('posts', ''), ('replies', '/with_replies'), ('reposts', '/reposts'))
SPINNER = '[role="progressbar"]'
REPOST_BUTTON = '[data-testid="retweet"]'
CHALLENGE_DIALOG = '[role="dialog"][data-testid*="challenge"], [role="dialog"][data-testid*="captcha"]'
STATUS_ID = re.compile(r'/status/(\d+)(?:/|$)')
PIN = re.compile(r'\bpinned\b', re.IGNORECASE)
REPOST = re.compile(r'\b(?:reposted|retweeted)\b', re.IGNORECASE)
LOGIN_PATH = re.compile(r'^/(?:login|i/flow|account/access)(?:/|$)')


TOUCHED_PAGES = weakref.WeakSet()
# Setup tabs where the user logs in to X; input there is expected and never stops the run.
INPUT_ALLOWED = weakref.WeakSet()


class SessionStopped(RuntimeError):
    pass


def stop_if_touched(page):
    if page in INPUT_ALLOWED:
        return
    # The page read lets a pending input report reach TOUCHED_PAGES first.
    touched = page.evaluate('window.__cleanupTouched === true')
    if touched:
        TOUCHED_PAGES.add(page)
    if touched or page in TOUCHED_PAGES:
        raise SessionStopped('The cleanup tab was used; run stopped')


def guard_pass(page):
    last_error = None
    try:
        stop_if_touched(page)
    except PlaywrightError as exc:
        last_error = exc
    for frame in page.frames:
        try:
            if (LOGIN_PATH.search(urlparse(frame.url).path)
                    or frame.locator(f'{AUTH}, {CHALLENGE_DIALOG}').count()):
                raise SessionStopped('Login or challenge detected; run stopped')
        except PlaywrightError as exc:
            if not frame.is_detached():
                last_error = exc
    try:
        language = page.evaluate('document.documentElement.lang')
        if (language or '').lower().split('-')[0] != 'en':
            raise SessionStopped('English page language required; run stopped')
    except PlaywrightError as exc:
        last_error = exc
    if last_error is not None:
        raise last_error


def guard(page, timing=None):
    timing = timing or Timing()
    passes = max(1, timing.guard_passes)
    for attempt in range(passes):
        try:
            guard_pass(page)
            return
        except PlaywrightError as exc:
            if attempt + 1 == passes:
                raise SessionStopped('The page did not settle; run stopped') from exc
            # A Playwright wait lets frame and URL events reach the next pass.
            page.wait_for_timeout(timing.guard_wait_ms)


def save_state(path, state):
    path = Path(path)
    temp = path.with_suffix('.json.tmp')
    with temp.open('w', encoding='utf-8') as f:
        json.dump(state, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)


def snapshot(article):
    return article.evaluate(SNAPSHOT_SCRIPT, SNAPSHOT_SELECTORS)


SNAPSHOT_SCRIPT = """(a, s) => {
      const own = q => [...a.querySelectorAll(q)].filter(e => e.closest(s.closest) === a && !e.closest(s.quote));
      const t = own(s.time)[0];
      return {author: own(s.author).map(e=>e.getAttribute('href')), url:t?.closest(s.link)?.href,
        date:t?.getAttribute('datetime'), context:own(s.context).map(e=>e.textContent).join(' '),
        context_links:own(s.context).flatMap(e=>[...e.querySelectorAll('a[href]')].map(l=>l.getAttribute('href'))),
        text:own(s.text).map(e=>e.textContent).join(' ')};
    }"""
SNAPSHOT_SELECTORS = {'time': TIMESTAMP, 'author': AUTHOR, 'context': CONTEXT, 'text': TEXT,
            'closest': CLOSEST_ARTICLE, 'link': STATUS_LINK, 'quote': QUOTE}


def status_id(data):
    match = STATUS_ID.search(urlparse(data.get('url') or '').path)
    return match.group(1) if match else None


@dataclass(frozen=True)
class Timing:
    load_ms: int = 15000
    settle_ms: int = 2100
    idle_passes: int = 5
    action_ms: int = 1500
    click_ms: int = 100
    verify_seconds: float = 2
    poll_ms: int = 100
    run_seconds: float = 5400
    scan_fraction: float = 0.65
    guard_passes: int = 5
    guard_wait_ms: int = 500
    login_seconds: float = 600
    launch_seconds: float = 30
    tab_close_seconds: float = 5700
    exit_seconds: float = 6600
    wake_seconds: float = 300
    exit_cap_seconds: float = 7020


def read_account_sources(page):
    return page.evaluate("""s => ({
        switcher: [...document.querySelectorAll(s.switcher + ' [data-testid^="UserAvatar-Container-"]')]
            .map(e => e.getAttribute('data-testid').slice('UserAvatar-Container-'.length)),
        profile: [...document.querySelectorAll(s.profile)].map(e => e.getAttribute('href'))
    })""", {'switcher': ACCOUNT_SWITCHER, 'profile': PROFILE})


def account_from(sources):
    handles = []
    for source, values in sources.items():
        for value in values:
            pattern = r'/([A-Za-z0-9_]{1,15})/?' if source == 'profile' else r'([A-Za-z0-9_]{1,15})'
            match = re.fullmatch(pattern, value or '')
            if not match:
                raise SessionStopped('X shows an unclear account; run stopped')
            handles.append(match.group(1))
    if handles:
        if len({handle.lower() for handle in handles}) != 1:
            raise SessionStopped('X shows an unclear account; run stopped')
        return handles[0]
    return None


def account_handle(page, timing):
    last_error = None
    attempts = max(1, timing.load_ms // timing.poll_ms)
    for attempt in range(attempts):
        try:
            sources = read_account_sources(page)
        except PlaywrightError as exc:
            last_error = exc
        else:
            handle = account_from(sources)
            if handle is not None:
                return handle
        if attempt + 1 < attempts:
            page.wait_for_timeout(timing.poll_ms)
    raise SessionStopped('Log in to X in the browser, then run again') from last_error


def check_account(page, handle):
    current = account_from(read_account_sources(page))
    if current is None or current.lower() != handle.lower():
        raise SessionStopped('Authenticated account does not match saved handle')


def kind(data):
    return 'repost' if REPOST.search(data['context']) else 'post'


def key(data):
    return kind(data), status_id(data)


def parsed(date):
    return datetime.fromisoformat(date.replace('Z', '+00:00')).astimezone(UTC)  # noqa: FURB162


def own_repost(data, handle):
    return (bool(re.search(r'\bYou\s+(?:reposted|retweeted)\b', data['context'], re.IGNORECASE))
            or any(urlparse(link).path.rstrip('/').lower() == '/' + handle.lower()
                   for link in data.get('context_links', [])))


@dataclass
class Safety:
    pinned: str | None = None
    labelled: set = field(default_factory=set)
    end: bool = False

    def protects(self, target):
        return target in self.labelled or target == self.pinned

    def observe(self, data, handle):
        if PIN.search(data['context']) and status_id(data):
            self.pinned = status_id(data)
            self.labelled.add(self.pinned)


def eligible(data, state, now, days=30, protected=None):
    target = status_id(data)
    if not target or (protected.protects(target) if protected else target == state.get('pinned')) or PIN.search(data['context']):
        return False
    if kind(data) == 'repost':
        if not own_repost(data, state['handle']):
            return False
        date = state.get('reposts', {}).get(data['url'])
    else:
        authors = data.get('author', [])
        if not authors or urlparse(authors[0]).path.strip('/').lower() != state['handle'].lower():
            return False
        date = data['date']
    try:
        return parsed(date) < now - timedelta(days=days)
    except (ValueError, TypeError, AttributeError):
        return False


def reconcile(inventory, state, now, days, reposts_ended=False, reposts=()):
    reposts_seen = {item['url'] for item in reposts}
    old = state.get('reposts', {})
    current = {url: date for url, date in old.items()
               if not reposts_ended or url in reposts_seen}
    for (category, _), item in inventory.items():
        if category != 'repost' or not own_repost(item, state['handle']):
            continue
        date = now.isoformat()
        try:
            if not state.get('cleanup_started') and parsed(item['date']) < now - timedelta(days=days):
                date = item['date']
        except (ValueError, TypeError, AttributeError):
            pass
        current[item['url']] = old.get(item['url'], date)
    bound = None
    for item in reversed(reposts):
        try:
            original = parsed(item['date'])
        except (ValueError, TypeError, AttributeError):
            pass
        else:
            bound = original if bound is None else max(bound, original)

        url = item['url']
        if bound is None or url not in current:
            continue
        try:
            stored = parsed(current[url])
        except (ValueError, TypeError, AttributeError):
            continue
        if stored < bound:
            current[url] = bound.isoformat()
    state['reposts'] = current


def outer_articles(page):
    return page.locator(f'{ARTICLE}:not(article article)').element_handles()


def find_articles(page, status):
    articles = outer_articles(page)
    batch = page.evaluate('(s) => s.nodes.map(a => a.isConnected ? (' + SNAPSHOT_SCRIPT + ')(a, s.selectors) : null)',
                          {'nodes': articles, 'selectors': SNAPSHOT_SELECTORS})
    return [(article, data) for article, data in zip(articles, batch) if data and status_id(data) == status]


def own_control(article, selector):
    for element in article.query_selector_all(selector):
        if element.evaluate('(e,s) => e.closest(s.article) === s.node && !e.closest(s.quote)',
                            {'article': CLOSEST_ARTICLE, 'node': article, 'quote': QUOTE}):
            return element
    return None


MENU = '#layers [role="menu"]'
LAYERS = (f'{MENU}, #layers [role="dialog"], #layers [aria-modal="true"], '
          f'{CONFIRM}, {UNDO_CONFIRM}, {DELETE}')


def count_layers(page):
    return page.locator(LAYERS).count()


def close_layers(page, timing):
    for _ in range(3):
        if not count_layers(page):
            return True
        # A login or challenge dialog stops the run before any key reaches it.
        guard(page)
        if not page.evaluate('window.__cleanupTouched !== true && (window.__cleanupEscapes = 1) === 1'):
            raise SessionStopped('The cleanup tab was used; run stopped')
        try:
            page.keyboard.press('Escape')
        finally:
            page.evaluate('window.__cleanupEscapes = 0')
        page.wait_for_timeout(timing.poll_ms)
    return count_layers(page) == 0


def owns_confirmation(button, selector, menu, inside):
    # An undo confirmation sits in the action's own menu; a delete sheet opens only after that menu closed.
    return button.evaluate("""(button, s) => {
        const menus = document.querySelectorAll(s.menu);
        const buttons = document.querySelectorAll(s.confirm);
        if (buttons.length !== 1 || buttons[0] !== button) return false;
        if (s.inside) return menus.length === 1 && menus[0] === s.owner && s.owner.contains(button);
        return menus.length === 0;
    }""", {'menu': MENU, 'confirm': selector, 'owner': menu, 'inside': inside})


def act(page, article, target, state, now, days, timing, pause, protected, journal, deadline=float("inf")):

    def check():
        if time.monotonic() >= deadline:
            return False
        data = snapshot(article)
        protected.observe(data, state['handle'])
        if not article.evaluate('a => a.isConnected'):
            raise PlaywrightError('Target article is detached')
        safe = key(data) == target and eligible(data, state, now, days, protected)
        if safe:
            guard(page)
            check_account(page, state['handle'])
        return safe

    for attempt in range(3):
        cleanup_needed = False
        try:
            if not close_layers(page, timing):
                return None
            cleanup_needed = True
            if not article.evaluate('a => a.isConnected'):
                matches = [(node, data) for node, data in find_articles(page, target[1]) if key(data) == target]
                if len(matches) != 1:
                    return None
                article, _ = matches[0]
            if not check():
                return None
            repost = target[0] == 'repost'
            control = own_control(article, UNREPOST if repost else CARET)
            if control is None:
                raise PlaywrightError('Action control is absent')
            control.wait_for_element_state('visible', timeout=timing.action_ms)
            control.wait_for_element_state('enabled', timeout=timing.action_ms)
            stop_if_touched(page)
            if not control.evaluate("""(control, layers) => {
                if (window.__cleanupTouched === true) return false;
                if (!control.isConnected || document.querySelector(layers)) return false;
                control.click();
                return true;
            }""", LAYERS):
                return None
            page.locator(MENU).first.wait_for(state='visible', timeout=timing.action_ms)
            if not repost and not check():
                return None
            menus = page.query_selector_all(MENU)
            if len(menus) != 1:
                return None
            menu, = menus
            if not repost:
                hrefs = menu.eval_on_selector_all('a[href]', "links => links.map(a => a.pathname)")
                if any(match.group(1) != target[1] for href in hrefs if (match := STATUS_ID.search(href))):
                    return None
                items = page.locator(MENU).locator(DELETE).filter(has_text=DELETE_TEXT)
                items.first.wait_for(state='visible', timeout=timing.action_ms)
                matches = items.element_handles()
                if len(matches) != 1:
                    return None
                item, = matches
                # The sheet belongs to this action when no confirmation exists before the click.
                stop_if_touched(page)
                if not item.evaluate("""(item, s) => {
                    if (window.__cleanupTouched === true) return false;
                    const menus = document.querySelectorAll(s.selector);
                    if (menus.length !== 1 || menus[0] !== s.menu || !s.menu.contains(item)) return false;
                    if (document.querySelector(s.confirm)) return false;
                    item.click();
                    return true;
                }""", {'selector': MENU, 'menu': menu, 'confirm': CONFIRM}):
                    return None
            selector = UNDO_CONFIRM if repost else CONFIRM
            page.locator(selector).first.wait_for(state='visible', timeout=timing.action_ms)
            buttons = page.query_selector_all(selector)
            if len(buttons) != 1 or not owns_confirmation(buttons[0], selector, menu, repost):
                return None
            button, = buttons
            action_deadline = min(deadline, time.monotonic() + timing.action_ms / 1000)
            while not button.is_enabled():
                if time.monotonic() >= action_deadline:
                    raise PlaywrightError('Confirmation is disabled')
                page.wait_for_timeout(timing.poll_ms)
            if not check():
                return None
            if not owns_confirmation(button, selector, menu, repost):
                return None
            journal.add(target)
            stop_if_touched(page)
            clicked = button.evaluate(CONFIRM_CLICK)
            guard(page)
            if not clicked:
                return None
            verify_deadline = min(deadline, time.monotonic() + timing.verify_seconds)
            while time.monotonic() < verify_deadline:
                if repost:
                    matches = find_articles(page, target[1])
                    if (any(own_control(node, REPOST_BUTTON) for node, _ in matches)
                            and not any(own_control(node, UNREPOST) for node, _ in matches)):
                        return 'removed'
                    if (not matches and not article.evaluate('a => a.isConnected') and outer_articles(page)
                            and not page.get_by_role('button', name=re.compile(r'^Retry$')).count()
                            and not page.locator('[role="alert"], [data-testid="error-detail"]').count()):
                        return 'removed'
                elif not article.evaluate('a => a.isConnected') and not find_articles(page, target[1]):
                    return 'removed'
                page.wait_for_timeout(timing.poll_ms)
            return 'unverified'
        except PlaywrightError:
            if target in journal:
                return 'unverified'
            guard(page)
        finally:
            if cleanup_needed:
                try:
                    close_layers(page, timing)
                except PlaywrightError:
                    LOGGER.warning('Could not close the action menu')
                except SessionStopped:
                    # A sent click keeps its result; the next guard stops the run.
                    if target not in journal:
                        raise
        if attempt < 2:
            pause()
    return None


def audit(path, row, status):
    row = dict(row, status=status)
    exists = path.exists()
    with path.open('a', newline='', encoding='utf-8', errors='backslashreplace') as f:
        writer = csv.DictWriter(f, fieldnames=['url', 'posted_at', 'action', 'text', 'run_time', 'live', 'status'])
        if not exists:
            writer.writeheader()
        writer.writerow({k: "'" + v if isinstance(v, str) and v.startswith(('=', '+', '-', '@', '\t', '\r')) else v for k, v in row.items()})
        f.flush()
        os.fsync(f.fileno())


def scroll(page, method, distance=None):
    stop_if_touched(page)
    scrolled = page.evaluate("""({method, distance, allowed}) => {
        if (!allowed && window.__cleanupTouched === true) return false;
        window[method](0, distance ?? window.innerHeight * 0.8);
        return true;
    }""", {'method': method, 'distance': distance, 'allowed': page in INPUT_ALLOWED})
    if not scrolled:
        stop_if_touched(page)


def walk(page, timing, observe=None, deadline=float("inf")):
    def settle():
        page.wait_for_timeout(min(timing.settle_ms, max(0, deadline - time.monotonic()) * 1000))

    seen = set()
    idle = 0
    while idle < timing.idle_passes and time.monotonic() < deadline:
        guard(page, timing)
        added = False
        while time.monotonic() < deadline:
            candidate = None
            moved = False
            articles = outer_articles(page)
            batch = page.evaluate('(s) => s.nodes.map(a => (' + SNAPSHOT_SCRIPT + ')(a, s.selectors))',
                                  {'nodes': articles, 'selectors': SNAPSHOT_SELECTORS})
            for article, data in zip(articles, batch):
                if observe:
                    observe(data)
                identity = key(data)
                if identity[1] and identity not in seen:
                    box = article.bounding_box()
                    height = page.evaluate('window.innerHeight')
                    if box and (box['y'] < 0 or box['y'] > height * 0.6):
                        position = page.evaluate('window.scrollY')
                        scroll(page, 'scrollBy', box['y'])
                        settle()
                        if page.evaluate('window.scrollY') != position:
                            moved = True
                            break
                    candidate = article, data
                    added = True
                    seen.add(identity)
                    break
            if moved:
                continue
            if candidate is None:
                break
            position = page.evaluate('window.scrollY')
            yield candidate
            if page.evaluate('window.scrollY') != position:
                scroll(page, 'scrollTo', position)
                settle()
        idle = 0 if added else idle + 1
        scroll(page, 'scrollBy')
        settle()
        if time.monotonic() >= deadline:
            return
        try:
            page.wait_for_function("""s => [...document.querySelectorAll(s)].every(e =>
            getComputedStyle(e).display === 'none' || getComputedStyle(e).visibility === 'hidden')""",
                                   arg=SPINNER, timeout=min(timing.load_ms, max(1, (deadline - time.monotonic()) * 1000)))
        except PlaywrightError:
            if time.monotonic() < deadline:
                raise
            return


def clean(page, state, state_path, *args, **kwargs):
    live = kwargs.get('live', args[2] if len(args) > 2 else False)
    before = copy.deepcopy(state)
    try:
        return _clean(page, state, state_path, *args, **kwargs)
    finally:
        if live and state != before:
            state['cleanup_started'] = True
            save_state(state_path, state)


def read_pin(page, handle, timing):
    original = page.url
    profile = urlparse(original)._replace(path='/' + handle, query='', fragment='').geturl()
    stop_if_touched(page)
    page.goto(profile)
    guard(page, timing)
    page.locator(LOADED).filter(visible=True).first.wait_for(timeout=timing.load_ms)
    protected = Safety()
    seen = set()
    idle = 0
    while len(seen) < 5 and idle < timing.idle_passes:
        guard(page, timing)
        before = len(seen)
        for article in outer_articles(page):
            data = snapshot(article)
            identity = key(data)
            if identity in seen:
                continue
            seen.add(identity)
            protected.observe(data, handle)
            if protected.pinned or len(seen) >= 5:
                break
        if protected.pinned or len(seen) >= 5 or page.locator(EMPTY).count():
            break
        idle = idle + 1 if len(seen) == before else 0
        scroll(page, 'scrollBy')
        page.wait_for_timeout(timing.settle_ms)
    stop_if_touched(page)
    page.goto(original)
    guard(page, timing)
    page.locator(LOADED).filter(visible=True).first.wait_for(timeout=timing.load_ms)
    if account_handle(page, timing).lower() != handle.lower():
        raise SessionStopped('Authenticated account does not match saved handle')
    return protected.pinned


def open_tab(page, url, timing, deadline):
    def within_budget(operation):
        remaining_ms = (deadline - time.monotonic()) * 1000
        if remaining_ms <= 0:
            return False
        try:
            operation(timeout=min(timing.load_ms, remaining_ms))
        except PlaywrightTimeout:
            return False
        return True

    stop_if_touched(page)
    if not within_budget(lambda **options: page.goto(url, **options)):
        return False
    guard(page, timing)
    return within_budget(page.locator(LOADED).filter(visible=True).first.wait_for)


def _clean(page, state, state_path, days=30, maximum=200, live=False, pause=None, timing=None, base_url=None, deadline=None, day=None):
    today = (day or datetime.now().date().isoformat()) if live else None
    timing = timing or Timing()
    started = time.monotonic()
    deadline = deadline or started + timing.run_seconds
    pause = pause or (lambda: time.sleep(random.uniform(2, 5)))
    now = datetime.now(UTC)
    log_dir = Path(state_path).parent / 'logs'
    log_dir.mkdir(exist_ok=True)
    guard(page, timing)
    page.locator(LOADED).filter(visible=True).first.wait_for(timeout=timing.load_ms)
    if account_handle(page, timing).lower() != state['handle'].lower():
        raise SessionStopped('Authenticated account does not match saved handle')
    stored = state
    state = copy.deepcopy(state)
    protected = Safety(state.get('pinned'))
    pin = read_pin(page, state['handle'], timing)
    if pin:
        protected.pinned = pin
        protected.labelled.add(pin)
    scan_deadline = time.monotonic() + max(0, deadline - time.monotonic()) * timing.scan_fraction
    origin = urlparse(page.url)
    urls = {tab: origin._replace(path='/' + state['handle'] + suffix, query='', fragment='').geturl()
            for tab, suffix in TABS}
    inventory = {}
    identities = {tab: set() for tab in urls}
    reposts = []
    ended = {}
    partial = False

    def observe(data):
        protected.observe(data, state['handle'])

    def open_account_tab(url, deadline):
        loaded = open_tab(page, url, timing, deadline)
        if loaded and account_handle(page, timing).lower() != state['handle'].lower():
            raise SessionStopped('Authenticated account does not match saved handle')
        return loaded

    for index, tab in enumerate(urls):
        started_tab = time.monotonic()
        tab_deadline = started_tab + (scan_deadline - started_tab) / (len(urls) - index)
        loaded = open_account_tab(urls[tab], tab_deadline)
        if loaded:
            for _, data in walk(page, timing, observe, tab_deadline):
                identity = key(data)
                identities[tab].add(identity)
                if tab == 'reposts':
                    reposts.append(data)
                previous = inventory.get(identity)
                if previous:
                    data = {**data, 'context': data['context'] + ' ' + previous['context'],
                            'context_links': data['context_links'] + previous.get('context_links', [])}
                inventory[identity] = data
        expired = time.monotonic() >= tab_deadline
        if expired:
            LOGGER.warning('%s scan is partial: scan deadline reached', tab)
        elif not loaded:
            LOGGER.warning('%s tab did not load; its scan is partial', tab)
        ended[tab] = loaded and not expired and not page.get_by_role('button', name=re.compile(r'^Retry$')).count() and not page.locator(
            '[role="alert"], [data-testid="error-detail"]').count()
        partial |= not ended[tab]
        if loaded and not expired and not ended[tab]:
            LOGGER.warning('%s scan is partial: the tab shows an error', tab)
    protected.end = all(ended.values())
    if protected.pinned:
        state['pinned'] = protected.pinned
    reconcile(inventory, state, now, days, ended['reposts'], reposts)
    if live:
        state['cleanup_started'] = True
        stored.clear()
        stored.update(state)
        state = stored
        save_state(state_path, state)
    else:
        if protected.pinned:
            stored['pinned'] = protected.pinned
            save_state(state_path, stored)
        LOGGER.info('Dry run leaves repost memory and cleanup flag unchanged')
    results = []
    remaining = {identity for identity, data in inventory.items() if eligible(data, state, now, days, protected)}

    def removal_row(identity, data):
        return {'url': data['url'], 'posted_at': data['date'],
                'action': 'unrepost' if identity[0] == 'repost' else 'delete',
                'text': data['text'], 'run_time': now.isoformat(), 'live': str(live).lower()}

    if not live:
        for identity, data in inventory.items():
            if identity not in remaining:
                continue
            row = removal_row(identity, data)
            audit(log_dir / 'removed.csv', row, 'dry_run')
            text = row['text'][:80].replace('\n', ' ').replace('\r', ' ')
            print(f"{row['action']} {row['posted_at']} {row['url']} {text}")
            results.append(row)
        print(f'{len(results)} items')
        return results

    attempted = set()
    counts = state.setdefault('daily_removals', {})
    available = max(0, maximum - counts.get(today, 0))

    class Journal(set):
        def add(self, target):
            if target not in self:
                super().add(target)
                counts[today] = counts.get(today, 0) + 1
                save_state(state_path, state)

    journal = Journal()
    def live_candidates():
        nonlocal action_error
        for tab, url in urls.items():
            if len(journal) >= available or not remaining:
                break
            if not remaining.intersection(identities[tab]):
                continue
            if not open_account_tab(url, deadline):
                if time.monotonic() >= deadline:
                    LOGGER.warning('Run deadline reached; action pass stopped')
                    break
                action_error = True
                LOGGER.warning('%s tab did not load; its actions wait for the next run', tab)
                continue
            scroll(page, 'scrollTo', 0)
            page.wait_for_timeout(min(timing.settle_ms, max(0, deadline - time.monotonic()) * 1000))
            for candidate in walk(page, timing, observe, deadline):
                yield candidate
                # The next tab starts once this tab holds no remaining item.
                if not remaining.intersection(identities[tab]):
                    break

    action_error = False
    for article, data in live_candidates():
        if len(journal) >= available:
            break
        if time.monotonic() >= deadline:
            LOGGER.warning('Run deadline reached; action pass stopped')
            break
        identity = key(data)
        if identity in attempted or identity not in inventory or not eligible(data, state, now, days, protected):
            continue
        attempted.add(identity)
        remaining.discard(identity)
        row = removal_row(identity, data)
        audit(log_dir / 'removed.csv', row, 'pending')
        outcome = None
        try:
            outcome = act(page, article, identity, state, now, days, timing, pause, protected, journal, deadline)
        finally:
            # A sent undo click may have worked, so the repost clock restarts unless the undo is proved.
            if identity[0] == 'repost' and identity in journal and outcome != 'removed':
                state['reposts'][data['url']] = now.isoformat()
                save_state(state_path, state)
        action_error |= outcome != 'removed'
        audit(log_dir / 'removed.csv', row, outcome or 'failed')
        if outcome:
            results.append(row)
            if identity[0] == 'repost' and outcome == 'removed':
                state['reposts'].pop(data['url'], None)
                save_state(state_path, state)
        stop_if_touched(page)
        if time.monotonic() < deadline:
            pause()
        if not remaining or len(journal) >= available:
            break
    if len(results) < maximum:
        for identity in remaining:
            audit(log_dir / 'removed.csv', removal_row(identity, inventory[identity]), 'not reached')
    if protected.pinned != state.get('pinned'):
        state['pinned'] = protected.pinned
        save_state(state_path, state)
    finished = protected.end and not action_error and time.monotonic() < deadline
    if finished:
        guard(page, timing)
    if finished or counts.get(today, 0) >= maximum:
        state['last_live_date'] = today
    state['last_cleanup'] = {'time': now.isoformat(), 'partial': partial,
                             'results': results, 'remaining': len(remaining), 'action_error': action_error}
    save_state(state_path, state)
    return results


class ProfileOpen(SessionStopped):
    pass


CHROMIUM_BROWSERS = {'chrome.exe', 'msedge.exe', 'brave.exe', 'opera.exe', 'vivaldi.exe', 'chromium.exe'}
HTTPS_USER_CHOICE = r'Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice'
EDGE_APP_PATH = r'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe'


def browser_executable(browser_path=None):
    if browser_path:
        LOGGER.info('Using the browser at %s', browser_path)
        return browser_path

    try:
        import winreg
    except ImportError as exc:
        raise SessionStopped('Use --browser PATH to select a browser on this system') from exc

    def registry_value(root, path, name=''):
        with winreg.OpenKey(root, path) as key:
            return winreg.QueryValueEx(key, name)[0]

    try:
        progid = registry_value(winreg.HKEY_CURRENT_USER, HTTPS_USER_CHOICE, 'ProgId')
        command = registry_value(winreg.HKEY_CLASSES_ROOT, progid + r'\shell\open\command')
        match = re.match(r'\s*(?:"([^"]+)"|([^\s"]+))', command)
        if match is None:
            raise ValueError('Missing browser path')
        path = ntpath.expandvars(match[1] or match[2])
        name = ntpath.basename(path)
        if name.lower() in CHROMIUM_BROWSERS:
            LOGGER.info('Using %s as the Windows default browser', path)
            return path
        reason = f'The Windows default browser ({name}) is unsupported'
    except (OSError, TypeError, ValueError):
        reason = 'Could not read the Windows default browser'

    try:
        path = ntpath.expandvars(registry_value(winreg.HKEY_LOCAL_MACHINE, EDGE_APP_PATH)).strip().strip('"')
    except (OSError, TypeError) as exc:
        raise SessionStopped(f'{reason}. Could not find Edge. Use --browser PATH.') from exc
    if not path:
        raise SessionStopped(f'{reason}. Could not find Edge. Use --browser PATH.')
    LOGGER.info('%s. Using Edge at %s', reason, path)
    return path


def profile_open(profile):
    try:
        with (profile / 'lockfile').open('rb'):
            return False
    except FileNotFoundError:
        return False
    except PermissionError:
        return True


# Trusted input identifies use through the browser's input devices.
INPUT_SCRIPT = """(() => {
    if (window.__cleanupInput) return;
    window.__cleanupInput = true;
    let lastMove = -Infinity;
    const now = Date.now.bind(Date);
    // Same-origin frames share the top window's flags; the script reads them there.
    const shared = () => {
        try {
            return window.top.document ? window.top : window;
        } catch {
            return window;
        }
    };
    for (const name of ['pointerdown', 'keydown', 'wheel', 'pointermove'])
        addEventListener(name, e => {
            if (!e.isTrusted) return;
            const flags = shared();
            if (e.type === 'keydown' && e.key === 'Escape' && flags.__cleanupEscapes > 0) {
                flags.__cleanupEscapes--;
                return;
            }
            const when = now();
            if (e.type === 'pointermove') {
                if (when - lastMove < 500) return;
                lastMove = when;
            } else {
                window.__cleanupTouched = true;
                flags.__cleanupTouched = true;
            }
            window.cleanupInput(e.type, when);
        }, true);
})();"""


def origin(value):
    parsed = urlparse(value)
    port = parsed.port
    if port is None:
        port = {'http': 80, 'https': 443}.get(parsed.scheme)
    return parsed.scheme, parsed.hostname, port


def foreign_change(baseline, current, own, home, own_closed=False):
    if not own_closed and own in baseline:
        if own not in current:
            return 'The cleanup tab closed'
        if current[own][1] != baseline[own][1]:
            return 'The cleanup tab moved to another window'
    if (current.keys() - baseline.keys()) - {own}:
        return 'A browser tab opened'
    if (baseline.keys() - current.keys()) - {own}:
        return 'A browser tab closed'
    for target in baseline.keys() - {own}:
        url, window = current[target]
        old_url, old_window = baseline[target]
        if url != old_url:
            return 'A browser tab navigated'
        if window != old_window:
            return 'A browser tab moved to another window'
    if home and own in current:
        url = current[own][0]
        # CDP reports the requested URL when a navigation commits an error page.
        if url != 'about:blank' and origin(url) != origin(home):
            return 'The cleanup tab left its site'
    return None


class BrowserRun:
    def __init__(self, process, endpoint, port, p, state=None, state_path=None, timing=None, home=None):
        self.process = process
        self.endpoint = endpoint
        self.port = port
        self.p = p
        self.browser = None
        self.page = None
        self.own_target = None
        self.baseline = None
        self.home = home
        self.armed_at = float('inf')
        self.moves_until = float('inf')
        self.used = False
        self.closed = False
        self.cleanup_tab_changed = False
        self.watchdog_closed_tab = threading.Event()
        self.warned_open = False
        self.state = state
        self.state_path = state_path
        self.timing = timing or Timing()
        self.deadline = time.monotonic() + self.timing.run_seconds

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def connect(self):
        self.browser = self.p.chromium.connect_over_cdp(self.endpoint)
        self.context = self.browser.contexts[0]
        self.initial_pages = set(self.context.pages)
        self.context.expose_binding('cleanupInput', self.record_input)
        self.context.add_init_script(INPUT_SCRIPT)
        self.context.on('page', self.new_page)
        for page in self.initial_pages:
            for frame in page.frames:
                for attempt in range(3):
                    try:
                        frame.evaluate(INPUT_SCRIPT)
                        break
                    except PlaywrightError:
                        if frame.is_detached() or page.is_closed():
                            break
                        if attempt == 2:
                            self.mark_used()
                            LOGGER.warning('Could not monitor input on a user tab; browser stays open')
                        else:
                            time.sleep(0.1)
        self.pending_pages = []
        self.opening = True
        try:
            self.page = self.context.new_page()
            self.armed_at = wall_clock() * 1000
            self.page.set_viewport_size(VIEWPORT)
        finally:
            self.opening = False
            if any(page is not self.page for page in self.pending_pages):
                self.mark_used()

        try:
            session = self.context.new_cdp_session(self.page)
            try:
                self.own_target = session.send('Target.getTargetInfo', timeout=self.timing.load_ms)['targetInfo']['targetId']
            finally:
                session.detach()
            self.baseline = self.inventory()
        except Exception:
            self.mark_used()
            LOGGER.warning('Could not record the browser tabs; browser stays open', exc_info=True)

    def record_input(self, source, event, when):
        # Movement shows a tab switch only between the cleanup page opening and its closing.
        if event == 'pointermove' and (source['page'] is self.page or not self.armed_at <= when <= self.moves_until):
            return
        if source['page'] is self.page and self.page in INPUT_ALLOWED:
            return
        if event != 'pointermove' and source['page'] is self.page:
            TOUCHED_PAGES.add(self.page)
        self.mark_used()

    def inventory(self):
        session = self.browser.new_browser_cdp_session()
        try:
            targets = {}
            for target in session.send('Target.getTargets', timeout=self.timing.load_ms)['targetInfos']:
                if target['type'] == 'page':
                    target_id = target['targetId']
                    window_id = session.send('Browser.getWindowForTarget', {'targetId': target_id}, timeout=self.timing.load_ms)['windowId']
                    targets[target_id] = (target['url'], window_id)
            return targets
        finally:
            session.detach()

    def check_use(self, own_closed=False):
        if self.used:
            return True
        if self.baseline is None:
            self.mark_used()
            LOGGER.warning('The browser tabs were not recorded; browser stays open')
            return True
        try:
            current = self.inventory()
            reason = foreign_change(self.baseline, current, self.own_target, self.home,
                                    own_closed or self.watchdog_closed_tab.is_set())
        except Exception:
            self.mark_used()
            LOGGER.warning('Could not check the browser tabs; browser stays open', exc_info=True)
        else:
            if reason:
                if reason in ('The cleanup tab left its site', 'The cleanup tab moved to another window'):
                    self.cleanup_tab_changed = True
                self.mark_used()
                LOGGER.warning('%s; browser stays open', reason)
        return self.used

    def mark_used(self):
        self.used = True

    def new_page(self, page):
        if getattr(self, 'opening', False):
            self.pending_pages.append(page)
        else:
            self.mark_used()

    def port_closed(self):
        if self.process is None or self.process.poll() is None:
            return False
        try:
            with urlopen(self.endpoint + '/json/version', timeout=1):
                return False
        except HTTPError:
            return False
        except OSError:
            return True

    def clear_record(self):
        if self.state is None or self.state_path is None or 'open_debug_port' not in self.state:
            return
        try:
            del self.state['open_debug_port']
            save_state(self.state_path, self.state)
        except Exception:
            LOGGER.warning('Could not save the debug port record', exc_info=True)

    def warn_open(self):
        if self.port_closed():
            self.clear_record()
            return
        if self.warned_open:
            return
        self.warned_open = True
        message = f'The browser is still open with debug port {self.port}. Close the browser to close the port.'
        # Each step runs even when an earlier one fails or is interrupted.
        try:
            with suppress(Exception):
                LOGGER.warning('Debug port %s remains open; close the browser to close the port', self.port)
        finally:
            try:
                if self.state is not None and self.state_path is not None:
                    try:
                        self.state['open_debug_port'] = self.port
                        save_state(self.state_path, self.state)
                    except Exception:
                        LOGGER.warning('Could not save the debug port record', exc_info=True)
            finally:
                try:
                    notify('Tweet cleanup', message)
                except Exception:
                    with suppress(Exception):
                        LOGGER.warning('Could not show the debug port notification', exc_info=True)
                finally:
                    self.show_notice(message)

    def cleanup_tab_in_use(self):
        # The page read lets a pending input report reach TOUCHED_PAGES first.
        try:
            touched = self.page.evaluate('window.__cleanupTouched === true') and self.page not in INPUT_ALLOWED
        except Exception:  # noqa: BLE001
            return True
        if touched or self.page in TOUCHED_PAGES:
            return True
        url = self.page.url
        # A blank tab and a failed navigation's error page hold no user content.
        if self.home and url != 'about:blank' and not url.startswith('chrome-error:') and origin(url) != origin(self.home):
            return True
        return getattr(self, 'cleanup_tab_changed', False)

    def show_notice(self, message):
        if self.page is None or self.page.is_closed():
            return
        if self.cleanup_tab_in_use():
            LOGGER.info('Skipped the debug port notice because the cleanup tab may be in use')
            return
        html = f'<title>Close the browser</title><h1>Close the browser</h1><p>{message}</p>'
        try:
            self.page.set_content(html, timeout=5000)
            return
        except Exception:
            LOGGER.info('Could not write the notice into the current page', exc_info=True)
        if self.cleanup_tab_in_use():
            LOGGER.info('Skipped the debug port notice because the cleanup tab may be in use')
            return
        # A page policy such as Trusted Types can block the write; a blank document has no policy.
        try:
            self.page.goto('about:blank', timeout=5000)
            self.page.set_content(html, timeout=5000)
        except Exception:
            with suppress(Exception):
                LOGGER.warning('Could not show the debug port notice', exc_info=True)

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.process is None:
            return
        try:
            if self.browser is None:
                until = time.monotonic() + self.timing.launch_seconds
                while self.process.poll() is None:
                    try:
                        with urlopen(self.endpoint + '/json/version', timeout=1) as response:
                            json.load(response)
                    except (OSError, ValueError):
                        if self.process.poll() is not None or time.monotonic() >= until:
                            break
                        time.sleep(0.1)
                    else:
                        if self.process.poll() is None:
                            self.connect()
                        break
            if self.browser is not None:
                if not self.used and self.port_closed():
                    raise PlaywrightError('Browser closed before shutdown')
                if self.check_use():
                    self.warn_open()
                    self.browser.close()
                    return
                self.moves_until = wall_clock() * 1000
                if self.page is not None:
                    try:
                        self.page.close()
                    except Exception:
                        LOGGER.exception('Could not close the cleanup page')
                if self.check_use(own_closed=True):
                    self.warn_open()
                    self.browser.close()
                    return
                self.browser.new_browser_cdp_session().send('Browser.close', timeout=self.timing.load_ms)
                self.process.wait(timeout=30)
                until = time.monotonic() + 30
                while not self.port_closed():
                    if time.monotonic() >= until:
                        raise SessionStopped('Browser debug port did not close')
                    time.sleep(0.1)
            if self.port_closed():
                self.clear_record()
            else:
                self.warn_open()
        except BaseException:
            self.warn_open()
            raise


def notify(title, text, windows=None, run=None):
    if windows is None:
        windows = os.name == 'nt'
    if not windows:
        return
    run = run or subprocess.run

    # Base64 keeps every character of the texts out of the PowerShell syntax.
    def literal(value):
        return base64.b64encode(value.encode('utf-8')).decode('ascii')

    code = rf"""
$ErrorActionPreference = 'Stop'
$Title = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{literal(title)}'))
$Text = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{literal(text)}'))
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$nodes = $xml.GetElementsByTagName('text')
$nodes.Item(0).AppendChild($xml.CreateTextNode($Title)) | Out-Null
$nodes.Item(1).AppendChild($xml.CreateTextNode($Text)) | Out-Null
$app = '{{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show([Windows.UI.Notifications.ToastNotification]::new($xml))
"""
    encoded = base64.b64encode(code.encode('utf-16le')).decode('ascii')
    try:
        result = run(['powershell', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
                     timeout=15, capture_output=True)
        if result.returncode:
            LOGGER.warning('Could not show the debug port notification: PowerShell exited with code %s', result.returncode)
    except (subprocess.TimeoutExpired, OSError) as exc:
        LOGGER.warning('Could not show the debug port notification: %s', exc)


class Watchdog:
    def __init__(self, timing=None, *, exit_fn=os._exit, notify_fn=None,
                 monotonic=time.monotonic, wall_clock=time.time, tick=1, notify_seconds=20):
        self.timing = timing or Timing()
        self.exit_fn = exit_fn
        self.notify_fn = notify if notify_fn is None else notify_fn
        self.monotonic = monotonic
        self.wall_clock = wall_clock
        self.tick = tick
        self.notify_seconds = notify_seconds
        self.stopped = threading.Event()
        self.thread = None

    def start(self, port, endpoint, process, run=None):
        started, wall_started = self.monotonic(), self.wall_clock()
        self.thread = threading.Thread(
            target=self._watch, args=(port, endpoint, process, started, wall_started, run), daemon=True)
        self.thread.start()

    def stop(self):
        self.stopped.set()
        if self.thread is not None:
            self.thread.join()

    def _watch(self, port, endpoint, process, last_tick, last_wall, run):
        deadline = last_tick + self.timing.exit_seconds
        tab_close_at = last_tick + self.timing.tab_close_seconds
        tab_close_attempted = False
        # Clock jumps move the fire time, but never past the cap.
        cap = last_tick + self.timing.exit_cap_seconds
        while not self.stopped.wait(self.tick):
            now, wall = self.monotonic(), self.wall_clock()
            if max(now - last_tick, abs(wall - last_wall)) > self.tick + 30:
                wake_at = now + self.timing.wake_seconds
                deadline = min(max(deadline, wake_at), cap)
                tab_close_at = max(tab_close_at, wake_at)
            if now >= deadline:
                if not self.stopped.is_set():
                    self._fire(port, endpoint, process)
                return
            if run is not None and not tab_close_attempted and tab_close_at <= now < deadline:
                tab_close_attempted = True
                self._close_tab(endpoint, run)
                now, wall = self.monotonic(), self.wall_clock()
            last_tick, last_wall = now, wall

    def _close_tab(self, endpoint, run):
        def can_close():
            page = run.page
            return (not self.stopped.is_set() and not run.closed
                    and run.own_target is not None and page is not None
                    and page not in TOUCHED_PAGES and not run.cleanup_tab_changed)

        with suppress(BaseException):
            if not can_close():
                return
            with urlopen(endpoint + '/json/list', timeout=2) as response:
                targets = json.load(response)
            if not isinstance(targets, list):
                return
            target = next((item for item in targets if isinstance(item, dict) and item.get('id') == run.own_target),
                          None)
            if target is None:
                return
            if run.home is not None:
                url = target.get('url')
                if not isinstance(url, str):
                    return
                if url != 'about:blank' and not url.startswith('chrome-error:') and origin(url) != origin(run.home):
                    return
            if not can_close():
                return
            # Movement shows a tab switch only until the cleanup tab closes, as in close().
            moves_until, cut_off = run.moves_until, wall_clock() * 1000
            run.moves_until = min(moves_until, cut_off)
            run.watchdog_closed_tab.set()
            status = None
            with suppress(BaseException):
                with urlopen(endpoint + '/json/close/' + run.own_target, timeout=2) as response:
                    response_status = response.status
                status = response_status
            if status == 200:
                LOGGER.warning('The cleanup tab stopped responding; closed it so the run can end')
            else:
                run.watchdog_closed_tab.clear()
                if run.moves_until == min(moves_until, cut_off):
                    run.moves_until = moves_until
                LOGGER.warning('Could not close the cleanup tab that stopped responding')

    @staticmethod
    def _port_closed(endpoint, process):
        for _ in range(3):
            try:
                with urlopen(endpoint + '/json/version', timeout=2):
                    return False
            except HTTPError:
                return False
            except OSError:
                continue
        return process is not None and process.poll() is not None

    def _fire(self, port, endpoint, process):
        closed = False
        try:
            with suppress(BaseException):
                closed = self._port_closed(endpoint, process)
            with suppress(BaseException):
                if closed:
                    LOGGER.warning('Run did not finish before the time limit')
                else:
                    LOGGER.warning('Run did not finish before the time limit; debug port %s remains open; '
                                   'close the browser to close the port', port)
            if not closed:
                def show():
                    with suppress(BaseException):
                        self.notify_fn('Tweet cleanup',
                                       f'The browser is still open with debug port {port}. Close the browser to close the port.')

                # A notification that hangs must not keep the process from exiting.
                with suppress(BaseException):
                    shower = threading.Thread(target=show, daemon=True)
                    shower.start()
                    shower.join(self.notify_seconds)
            for handler in logging.getLogger().handlers[:]:
                with suppress(BaseException):
                    handler.flush()
        finally:
            self.exit_fn(1)


def launch_browser(p, browser_path=None, extra_args=(), *, is_running=None,
                   state=None, state_path=None, timing=None, home=None, watchdog=None):
    profile = DATA_DIR / 'browser-profile'
    is_running = is_running or profile_open
    if is_running(profile):
        raise ProfileOpen('The Tweet cleanup browser window is already open; close it and try again')
    timing = timing or Timing()
    executable = browser_executable(browser_path)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    run = BrowserRun(None, f'http://127.0.0.1:{port}', port, p, state, state_path, timing, home=home)
    run.executable = executable
    if state is not None and state_path is not None:
        state['open_debug_port'] = port
        save_state(state_path, state)
    try:
        try:
            run.process = subprocess.Popen([str(executable), f'--remote-debugging-port={port}',
                                            '--remote-debugging-address=127.0.0.1', *extra_args,
                                            f'--user-data-dir={profile}', '--no-first-run',
                                            '--no-default-browser-check'])
        except OSError:
            run.clear_record()
            raise
        if watchdog is not None:
            watchdog.start(run.port, run.endpoint, run.process, run=run)
        deadline = time.monotonic() + timing.launch_seconds
        while run.process.poll() is None:
            try:
                with urlopen(run.endpoint + '/json/version', timeout=1) as response:
                    json.load(response)
            except (OSError, ValueError):
                if run.process.poll() is not None or time.monotonic() >= deadline:
                    break
                time.sleep(0.1)
            else:
                if run.process.poll() is None:
                    run.connect()
                    return run
                break
        if run.process.poll() is not None:
            if is_running(profile):
                raise ProfileOpen('The Tweet cleanup browser window opened during launch; close it and try again')
            raise SessionStopped('The browser exited during launch')
    except BaseException:
        run.close()
        raise
    failure = SessionStopped('Browser debug endpoint did not start')
    try:
        run.close()
    except Exception as exc:
        raise failure from exc
    raise failure


def setup_account(page, timing):
    if not LOGIN_PATH.search(urlparse(page.url).path):
        with suppress(PlaywrightTimeout):
            page.locator(f'{PROFILE}, {ACCOUNT_SWITCHER}, {AUTH}').first.wait_for(
                state='attached', timeout=timing.load_ms)
        handle = account_from(read_account_sources(page))
        if handle is not None:
            return handle

    deadline = time.monotonic() + timing.login_seconds
    print('Log in to X in the browser to continue setup.', flush=True)
    if not LOGIN_PATH.search(urlparse(page.url).path):
        login = urlparse(page.url)._replace(path='/i/flow/login', query='', fragment='').geturl()
        page.goto(login, timeout=min(timing.load_ms, max(1, timing.login_seconds * 1000)))
    while time.monotonic() < deadline:
        try:
            if not LOGIN_PATH.search(urlparse(page.url).path):
                handle = account_from(read_account_sources(page))
                if handle is not None:
                    return handle
        except PlaywrightError:
            if page.is_closed():
                raise SessionStopped('The browser tab closed during setup')
        remaining_ms = (deadline - time.monotonic()) * 1000
        if remaining_ms > 0:
            page.wait_for_timeout(min(timing.poll_ms, remaining_ms))
    raise SessionStopped('Log in to X in the browser to finish setup. The login wait ended.')


def save_setup(page, state, path, timing=None, requested_handle=None):
    timing = timing or Timing()
    handle = setup_account(page, timing)
    if requested_handle is not None and requested_handle.removeprefix('@').lower() != handle.lower():
        raise SessionStopped(f'The signed-in account @{handle} does not match --handle {requested_handle}')
    pin = read_pin(page, handle, timing)
    state['handle'] = handle
    if pin:
        state['pinned'] = pin
        origin = urlparse(page.url)._replace(path='/' + handle + '/status/' + pin, query='', fragment='').geturl()
        print(f'Pinned post: {origin}')
    else:
        print('No pinned post found')
    save_state(path, state)
    print(f'Saved @{handle}.')
    print('If the setup window stays open, close it.')
    print('Use run to preview which posts would be removed.')


def configure_output():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(errors='backslashreplace')


def main(base_url="https://x.com"):
    configure_output()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['setup', 'run'])
    parser.add_argument('--browser')
    parser.add_argument('--handle', help='Confirm the signed-in X handle during setup')
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--days', type=int, default=30)
    parser.add_argument('--max', type=int, default=200, dest='maximum')
    args = parser.parse_args()
    if args.days < 1 or args.maximum < 1:
        parser.error('--days and --max must be positive')
    if args.handle is not None and args.command != 'setup':
        parser.error('--handle is only available with setup')
    live_run = args.command == 'run' and args.live
    start_date = datetime.now().date().isoformat() if live_run else None
    (DATA_DIR / 'logs').mkdir(exist_ok=True)
    logging.basicConfig(filename=DATA_DIR / 'logs' / 'run.log', level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    path = DATA_DIR / 'state.json'
    watchdog = Watchdog()
    try:
        state = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'reposts': {}}
        if state.get('open_debug_port'):
            LOGGER.warning('Debug port %s was left open; close the browser to close the port', state['open_debug_port'])
        if live_run:
            if state.get('last_live_date') == start_date:
                return 0
            if state.get('daily_removals', {}).get(start_date, 0) >= args.maximum:
                LOGGER.info('Daily removal limit reached; run skipped')
                print('Daily removal limit reached; run skipped')
                return 0
        browser = args.browser
        if args.command == 'run' and not browser and state.get('browser'):
            # The profile's login only works in the browser that setup used.
            browser = state['browser']
            if not Path(browser).exists():
                raise SessionStopped(f'The browser from setup is missing: {browser}. Run setup again.')
        with sync_playwright() as p:
            run = launch_browser(p, browser, state=state, state_path=path, home=base_url, watchdog=watchdog)
            with run:
                page = run.page
                if args.command == 'setup':
                    INPUT_ALLOWED.add(page)
                    state['browser'] = str(run.executable)
                    page.goto(base_url.rstrip('/') + '/home')
                    save_setup(page, state, path, requested_handle=args.handle)
                else:
                    if not state.get('handle'):
                        raise SessionStopped('Run setup first to save your handle')
                    page.goto(base_url.rstrip('/') + '/' + state['handle'])
                    previous_live_date = state.get('last_live_date') if live_run else None
                    clean(page, state, path, args.days, args.maximum, args.live, base_url=base_url, deadline=run.deadline, day=start_date)
                    if live_run and state.get('last_live_date') == previous_live_date:
                        raise SessionStopped('Run ends with an error')
        if live_run:
            save_state(path, state)
        return 0
    except ProfileOpen as exc:
        LOGGER.warning('Run skipped (exit 3): %s', exc)
        print(str(exc), flush=True)
        return 3
    except Exception as exc:
        LOGGER.exception('Run stopped')
        print(str(exc), flush=True)
        return 1
    finally:
        watchdog.stop()


if __name__ == '__main__':
    raise SystemExit(main())
