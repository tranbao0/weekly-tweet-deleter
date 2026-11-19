import functools
import http.server
import threading
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

import tweet_cleanup

REAL_NOTIFY = tweet_cleanup.notify


@pytest.fixture(scope="session")
def playwright():
    with sync_playwright() as p:
        yield p


@pytest.fixture(scope="session")
def shared_browser(playwright):
    browser = playwright.chromium.launch(headless=True, args=['--no-sandbox'])
    yield browser
    browser.close()


@pytest.fixture(autouse=True)
def app_playwright(monkeypatch, playwright):
    from contextlib import nullcontext

    import tweet_cleanup as app
    monkeypatch.setattr(app, 'sync_playwright', lambda: nullcontext(playwright))


@pytest.fixture(autouse=True)
def no_notifications(monkeypatch):
    monkeypatch.setattr(tweet_cleanup, 'notify', lambda title, text: None)


@pytest.fixture
def real_notify():
    return REAL_NOTIFY


@pytest.fixture
def fixture_server():
    class Handler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            if self.path.split('?', 1)[0] in ('/me', '/me/with_replies', '/me/reposts', '/me/media', '/home'):
                self.path = '/index.html' + ('?' + self.path.split('?', 1)[1] if '?' in self.path else '')
            super().do_GET()

    handler = functools.partial(Handler, directory=str(Path(__file__).parent / 'fixtures'))
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def data_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(tweet_cleanup, 'DATA_DIR', tmp_path)
