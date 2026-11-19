import sys
from contextlib import nullcontext
from unittest.mock import MagicMock, Mock

import pytest
from test_tweet_cleanup import browser  # noqa: F401

import tweet_cleanup as app

pytestmark = pytest.mark.usefixtures('data_dir')


def test_running_skips(monkeypatch, tmp_path, capsys, caplog):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(app, "DATA_DIR", tmp_path)
    monkeypatch.setattr(sys, 'argv', ['tweet_cleanup.py', 'run'])
    launch = Mock(side_effect=app.ProfileOpen('The Tweet cleanup browser window is already open; close it and try again'))
    monkeypatch.setattr(app, 'launch_browser', launch)
    assert app.main('http://127.0.0.1') == 3
    assert 'The Tweet cleanup browser window is already open; close it and try again' in capsys.readouterr().out
    assert 'exit 3' in caplog.text and 'browser window is already open' in caplog.text


def test_running_never_launches():
    with pytest.raises(app.ProfileOpen):
        app.launch_browser(Mock(), is_running=lambda _: True)


def test_same_day_skips(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(app, "DATA_DIR", tmp_path)
    app.save_state(tmp_path / 'state.json', {'last_live_date': app.datetime.now().date().isoformat()})
    monkeypatch.setattr(sys, 'argv', ['tweet_cleanup.py', 'run', '--live'])
    launch = Mock(side_effect=AssertionError('Browser must stay closed'))
    monkeypatch.setattr(app, 'launch_browser', launch)
    assert app.main('http://127.0.0.1') == 0
    launch.assert_not_called()


def test_setup_missing_profile(browser):  # noqa: F811
    page, base, root = browser
    page.goto(base)
    page.locator(app.PROFILE + ', [data-testid=SideNav_AccountSwitcher_Button]').evaluate_all('(links) => links.forEach(l => l.remove())')
    path = root / 'state.json'
    app.save_state(path, {'handle': 'saved'})
    before = path.read_bytes()
    with pytest.raises(app.SessionStopped, match='Log in to X in the browser'):
        app.save_setup(page, {'handle': 'saved'}, path, timing=app.Timing().__class__(load_ms=200, poll_ms=20, login_seconds=0.2))
    assert path.read_bytes() == before


def test_normal_close(tmp_path, playwright):
    with nullcontext(playwright) as p:
        run = app.launch_browser(p, p.chromium.executable_path,
                                 ['--headless', '--no-sandbox'])
        run.close()
        assert run.process.poll() is not None


def test_user_tab_stays_open(tmp_path, playwright):
    with nullcontext(playwright) as p:
        run = app.launch_browser(p, p.chromium.executable_path,
                                 ['--headless', '--no-sandbox'])
        tab = run.context.new_page()
        tab.goto('about:blank')
        run.close()
        assert run.process.poll() is None
        browser = p.chromium.connect_over_cdp(run.endpoint)
        browser.new_browser_cdp_session().send('Browser.close')
        run.process.wait(timeout=30)


def test_live_twice(browser, monkeypatch):  # noqa: F811
    page, base, root = browser
    monkeypatch.chdir(root)
    monkeypatch.setattr(app, "DATA_DIR", root)
    app.save_state(root / 'state.json', {'handle': 'me', 'reposts': {}})
    monkeypatch.setattr(sys, 'argv', ['tweet_cleanup.py', 'run', '--live'])
    monkeypatch.setattr(app, 'sync_playwright', lambda: nullcontext(Mock()))
    run = MagicMock(page=page, deadline=app.time.monotonic() + 5400)
    launch = Mock(return_value=run)
    monkeypatch.setattr(app, 'launch_browser', launch)
    clean = app.clean
    monkeypatch.setattr(app, 'clean', lambda *a, **kw: clean(*a, **kw, pause=lambda: None))
    assert app.main(base) == 0
    assert app.main(base) == 0
    assert launch.call_count == 1


def test_setup_exit_one(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(app, "DATA_DIR", tmp_path)
    path = tmp_path / 'state.json'
    app.save_state(path, {'handle': 'saved'})
    before = path.read_bytes()
    monkeypatch.setattr(sys, 'argv', ['tweet_cleanup.py', 'setup'])
    page = Mock()
    monkeypatch.setattr(app, 'setup_account', Mock(side_effect=app.SessionStopped(
        'Log in to X in the browser, then run again')), raising=False)
    run = MagicMock(page=page, deadline=app.time.monotonic() + 5400)
    monkeypatch.setattr(app, 'launch_browser', Mock(return_value=run))
    assert app.main('http://127.0.0.1') == 1
    assert path.read_bytes() == before
    run.__exit__.assert_called_once()


def test_owned_viewport(tmp_path, playwright):
    run = app.launch_browser(playwright, playwright.chromium.executable_path,
                            ['--headless', '--no-sandbox'])
    try:
        initial = next(iter(run.initial_pages))
        tab = run.context.new_page()
        assert run.page.evaluate('[innerWidth, innerHeight]') == [1280, 1000]
        assert tab.evaluate('[innerWidth, innerHeight]') != [1280, 1000]
        assert initial.evaluate('[innerWidth, innerHeight]') != [1280, 1000]
    finally:
        run.close()
        if run.process.poll() is None:
            browser = playwright.chromium.connect_over_cdp(run.endpoint)
            browser.new_browser_cdp_session().send('Browser.close')
            run.process.wait(timeout=30)
