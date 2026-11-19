import base64
import inspect
import itertools
import json
import logging
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager, nullcontext, suppress
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from queue import Empty, Queue
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock
from unittest.mock import call as mock_call
from urllib.error import URLError

import pytest
from playwright.sync_api import Page
from test_tweet_cleanup import browser  # noqa: F401

import tweet_cleanup as app

pytestmark = pytest.mark.usefixtures('data_dir')


@pytest.fixture
def owned(tmp_path, playwright, monkeypatch):
    runs = []
    def start(**kwargs):
        data_dir = tmp_path / str(len(runs))
        with monkeypatch.context() as patch:
            patch.setattr(app, 'DATA_DIR', data_dir)
            run = app.launch_browser(playwright, playwright.chromium.executable_path,
                ['--headless', '--no-sandbox', '--window-size=1280,1000'], **kwargs)
        runs.append(run)
        return run
    yield start
    for run in runs:
        if run.process.poll() is None:
            port = next(arg.split('=')[1] for arg in run.process.args if arg.startswith('--remote-debugging-port='))
            b = playwright.chromium.connect_over_cdp(f'http://127.0.0.1:{port}')
            b.new_browser_cdp_session().send('Browser.close')
            run.process.wait(timeout=30)


def closed(run):
    assert run.process.poll() is not None
    with socket.socket() as sock:
        assert sock.connect_ex(('127.0.0.1', run.port)) != 0


def test_interrupt_launch(tmp_path, monkeypatch, playwright):
    original = app.urlopen
    processes = []
    popen = app.subprocess.Popen
    def spawn(*a, **kw):
        process = popen(*a, **kw)
        processes.append(process)
        return process
    calls = 0
    def interrupted(*a, **kw):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise KeyboardInterrupt
        return original(*a, **kw)
    monkeypatch.setattr(app.subprocess, 'Popen', spawn)
    with nullcontext(playwright) as p:
        monkeypatch.setattr(app, 'urlopen', interrupted)
        try:
            with pytest.raises(KeyboardInterrupt):
                app.launch_browser(p, p.chromium.executable_path,
                    ['--headless', '--no-sandbox'])
            assert processes[-1].poll() is not None
            port = int(next(arg.split('=')[1] for arg in processes[-1].args if arg.startswith('--remote-debugging-port=')))
            with socket.socket() as sock:
                assert sock.connect_ex(('127.0.0.1', port)) != 0
        finally:
            if processes[-1].poll() is None:
                port = next(arg.split('=')[1] for arg in processes[-1].args if arg.startswith('--remote-debugging-port='))
                until = time.monotonic() + 30
                while True:
                    try:
                        b = p.chromium.connect_over_cdp(f'http://127.0.0.1:{port}')
                        b.new_browser_cdp_session().send('Browser.close')
                        break
                    except app.PlaywrightError:
                        if time.monotonic() >= until:
                            raise
                        time.sleep(0.1)
                processes[-1].wait(timeout=30)


def test_scan_exception(owned, browser, monkeypatch):  # noqa: F811
    run = owned()
    run.page.goto(browser[1])
    def failed(*args, **kwargs):
        raise RuntimeError('Scan failed')
    monkeypatch.setattr(app, 'walk', failed)
    with pytest.raises(RuntimeError), run:
        app.clean(run.page, {'handle': 'me'}, browser[2] / 'state.json')
    closed(run)


def test_input_at_start(owned, tmp_path, caplog):
    state = {}
    run = owned(state=state, state_path=tmp_path / 'state.json')
    initial = next(iter(run.initial_pages))
    initial.keyboard.press('a')
    run.close()
    assert run.process.poll() is None
    assert str(run.port) in caplog.text and 'close the browser' in caplog.text
    assert json.loads((tmp_path / 'state.json').read_text())['open_debug_port'] == run.port


def test_close_layers_skips_only_script_escape(owned, fixture_server):
    with owned() as run:
        page = run.page
        page.goto(f'http://127.0.0.1:{fixture_server.server_port}/me')
        page.locator(app.CARET).first.evaluate('e => e.click()')
        menu = page.locator('#layers [role=menu]')
        assert menu.count() == 1
        assert app.close_layers(page, app.Timing())
        assert menu.count() == 0
        assert run.used is False
        page.keyboard.press('Escape')
        assert run.used is True


def test_page_close_error(owned, monkeypatch):
    run = owned()
    monkeypatch.setattr(Page, 'close', Mock(side_effect=app.PlaywrightError('Close failed')))
    run.close()
    closed(run)


def test_no_endpoint(monkeypatch):
    process = Mock()
    process.poll.return_value = None
    monkeypatch.setattr(app.subprocess, 'Popen', Mock(return_value=process))
    monkeypatch.setattr(app, 'urlopen', Mock(side_effect=OSError('No endpoint')))
    clock = itertools.count(0, 31)
    monkeypatch.setattr(app.time, 'monotonic', lambda: next(clock))
    with pytest.raises(app.SessionStopped, match='endpoint did not start'):
        app.launch_browser(Mock(), 'browser.exe')
    process.terminate.assert_not_called()
    process.kill.assert_not_called()


def test_save_before_close(browser, owned, monkeypatch, playwright):  # noqa: F811
    import sys
    from contextlib import nullcontext
    monkeypatch.setattr(app, "sync_playwright", lambda: nullcontext(playwright))
    _, base, root = browser
    monkeypatch.chdir(root)
    monkeypatch.setattr(app, "DATA_DIR", root)
    state = {'handle': 'me'}
    app.save_state(root / 'state.json', state)
    run = owned()
    monkeypatch.setattr(app, 'launch_browser', lambda *a, **kw: run)
    monkeypatch.setattr(run, 'close', Mock(side_effect=app.PlaywrightError('Shutdown failed')))
    clean = app.clean
    monkeypatch.setattr(app, 'clean', lambda *a, **kw: clean(*a, **kw, pause=lambda: None))
    monkeypatch.setattr(sys, 'argv', ['tweet_cleanup.py', 'run', '--live'])
    assert app.main(base) == 1
    assert json.loads((root / 'state.json').read_text())['last_live_date'] == app.datetime.now().date().isoformat()


def test_empty_no_second_walk(browser, monkeypatch):  # noqa: F811
    page, base, root = browser
    page.goto(base)
    monkeypatch.setattr(app, 'eligible', lambda *a: False)
    original = app.walk
    calls = []
    def counted(*a, **kw):
        calls.append(1)
        yield from original(*a, **kw)
    monkeypatch.setattr(app, 'walk', counted)
    app.clean(page, {'handle': 'me'}, root / 'state.json', live=True, pause=lambda: None)
    assert len(calls) == len(app.TABS)


def test_partial_deadline(browser, owned, monkeypatch, caplog):  # noqa: F811
    _, base, root = browser
    run = owned()
    with run:
        page = run.page
        page.goto(base + "/me/with_replies?endless=1")
        result = app.clean(page, {'handle': 'me'}, root / 'state.json', live=True,
                           pause=lambda: None, timing=replace(app.Timing(), run_seconds=20, scan_fraction=0.3))
        assert result and page.evaluate('window.removed.length')
    closed(run)
    assert 'partial' in caplog.text


def test_walk_measurement(browser):  # noqa: F811
    page, base, _ = browser
    page.goto(base + '/virtual.html')
    start = time.monotonic()
    count = sum(1 for _ in app.walk(page, app.Timing()))
    print(f'walk: {count} items, {(time.monotonic() - start) / count:.3f} seconds/item')


@pytest.fixture
def notification(monkeypatch):
    recorder = Mock()
    monkeypatch.setattr(app, 'notify', recorder)
    return recorder


@pytest.fixture
def saved_state(tmp_path):
    path = tmp_path / 'state.json'
    state = {}
    app.save_state(path, state)
    return state, path


@pytest.fixture
def missing_endpoint(monkeypatch):
    process = Mock()
    process.poll.return_value = None
    popen = Mock(return_value=process)
    monkeypatch.setattr(app.subprocess, 'Popen', popen)
    monkeypatch.setattr(app, 'urlopen', Mock(side_effect=OSError('No endpoint')))
    clock = itertools.count()
    monkeypatch.setattr(app, 'time', SimpleNamespace(monotonic=lambda: next(clock), sleep=Mock()))
    return process, popen


def test_port_recorded_before_spawn(monkeypatch, saved_state, notification):
    state, path = saved_state

    def spawn(args):
        port = int(args[1].split('=')[1])
        assert json.loads(path.read_text()).get('open_debug_port') == port
        raise OSError('Could not start process')

    monkeypatch.setattr(app.subprocess, 'Popen', spawn)
    with pytest.raises(OSError, match='Could not start process'):
        app.launch_browser(Mock(), 'browser.exe', state=state, state_path=path)
    assert 'open_debug_port' not in json.loads(path.read_text())
    notification.assert_not_called()


def test_launch_race(missing_endpoint, saved_state, notification):
    process, _ = missing_endpoint
    process.poll.return_value = 24
    state, path = saved_state
    running = Mock(side_effect=[False, True])

    with pytest.raises(app.ProfileOpen, match='during launch'):
        app.launch_browser(Mock(), 'browser.exe', is_running=running, state=state, state_path=path)

    app.time.sleep.assert_not_called()
    process.terminate.assert_not_called()
    process.kill.assert_not_called()
    notification.assert_not_called()
    assert 'open_debug_port' not in json.loads(path.read_text())


def test_process_exits_during_launch(missing_endpoint, saved_state, notification):
    process, _ = missing_endpoint
    process.poll.return_value = 24
    state, path = saved_state

    with pytest.raises(app.SessionStopped, match='exited during launch'):
        app.launch_browser(Mock(), 'browser.exe', state=state, state_path=path)

    app.time.sleep.assert_not_called()
    notification.assert_not_called()
    assert 'open_debug_port' not in json.loads(path.read_text())


def test_slow_start_without_endpoint(missing_endpoint, saved_state, notification):
    process, popen = missing_endpoint
    state, path = saved_state
    running = Mock(side_effect=lambda _: popen.called)

    with pytest.raises(app.SessionStopped, match='Browser debug endpoint did not start'):
        app.launch_browser(Mock(), 'browser.exe', is_running=running, state=state, state_path=path)

    running.assert_called_once_with(app.DATA_DIR / 'browser-profile')
    port = int(popen.call_args.args[0][1].split('=')[1])
    notification.assert_called_once_with(
        'Tweet cleanup', f'The browser is still open with debug port {port}. Close the browser to close the port.'
    )
    assert json.loads(path.read_text())['open_debug_port'] == port
    process.terminate.assert_not_called()
    process.kill.assert_not_called()


def test_slow_start_with_late_endpoint(tmp_path, playwright, monkeypatch, saved_state, notification):
    state, path = saved_state
    open_url = app.urlopen
    start_process = app.subprocess.Popen
    processes = []
    elapsed = 0
    launch_seconds = 1

    def spawn(args):
        process = start_process(args)
        processes.append(process)
        return process

    def delayed_endpoint(url, **kwargs):
        nonlocal elapsed
        if not elapsed:
            elapsed = launch_seconds
            raise OSError('Endpoint not ready')
        return open_url(url, **kwargs)

    monkeypatch.setattr(app.subprocess, 'Popen', spawn)
    monkeypatch.setattr(app, 'urlopen', delayed_endpoint)
    monkeypatch.setattr(app, 'time', SimpleNamespace(
        monotonic=lambda: elapsed + time.monotonic(), sleep=time.sleep
    ))

    try:
        with pytest.raises(app.SessionStopped, match='Browser debug endpoint did not start'):
            app.launch_browser(playwright, playwright.chromium.executable_path,
                ['--headless', '--no-sandbox'], state=state, state_path=path,
                timing=replace(app.Timing(), launch_seconds=launch_seconds))
        process = processes[0]
        port = int(process.args[1].split('=')[1])
        closed(SimpleNamespace(process=process, port=port))
        notification.assert_not_called()
        assert 'open_debug_port' not in json.loads(path.read_text())
    finally:
        if processes and processes[0].poll() is None:
            port = int(processes[0].args[1].split('=')[1])
            until = time.monotonic() + 10
            while True:
                try:
                    connection = playwright.chromium.connect_over_cdp(f'http://127.0.0.1:{port}')
                    break
                except app.PlaywrightError:
                    if time.monotonic() >= until:
                        raise
                    time.sleep(0.1)
            connection.new_browser_cdp_session().send('Browser.close')
            processes[0].wait(timeout=10)


def test_connect_error_during_close(owned, monkeypatch, saved_state, notification):
    state, path = saved_state
    run = owned(state=state, state_path=path)
    run.browser.close()
    run.browser = None
    monkeypatch.setattr(app.BrowserRun, 'connect', Mock(side_effect=app.PlaywrightError('Connection failed')))

    with pytest.raises(app.PlaywrightError, match='Connection failed'):
        run.close()

    notification.assert_called_once()
    assert json.loads(path.read_text())['open_debug_port'] == run.port


def test_used_run_shows_notice(owned, playwright, saved_state, notification):
    state, path = saved_state
    run = owned(state=state, state_path=path)
    run.page.goto('data:text/html,<title>Cleanup</title>')
    page_url = run.page.url
    run.mark_used()
    run.close()

    connection = playwright.chromium.connect_over_cdp(run.endpoint)
    try:
        page = next(page for page in connection.contexts[0].pages if page.url == page_url)
        assert page.title() == 'Close the browser'
        assert f'debug port {run.port}' in page.locator('body').inner_text()
    finally:
        connection.close()
    notification.assert_called_once()
    assert run.process.poll() is None
    assert json.loads(path.read_text())['open_debug_port'] == run.port


def test_closed_cleanup_page_does_not_open_a_notice_tab(owned, playwright, notification):
    run = owned()
    run.mark_used()
    run.page.close()
    page_count = len(run.context.pages)
    run.close()

    connection = playwright.chromium.connect_over_cdp(run.endpoint)
    try:
        assert len(connection.contexts[0].pages) == page_count
    finally:
        connection.close()
    notification.assert_called_once()


def test_disconnect_error_warns_once(owned, monkeypatch, notification):
    run = owned()
    run.mark_used()
    monkeypatch.setattr(run.browser, 'close', Mock(side_effect=app.PlaywrightError('Disconnect failed')))

    with pytest.raises(app.PlaywrightError, match='Disconnect failed'):
        run.close()

    notification.assert_called_once()


def test_notification_command(caplog, real_notify):
    runner = Mock(return_value=subprocess.CompletedProcess([], 0))
    real_notify('Tweet cleanup', "The browser's port \u2019is\u2019 open.", windows=True, run=runner)

    args, kwargs = runner.call_args
    command = args[0]
    assert isinstance(command, list)
    assert command[:3] == ['powershell', '-NoProfile', '-NonInteractive']
    assert not kwargs.get('shell', False)
    assert 0 < kwargs['timeout'] <= 15
    code = base64.b64decode(command[command.index('-EncodedCommand') + 1]).decode('utf-16le')
    assert base64.b64encode(b'Tweet cleanup').decode() in code
    assert base64.b64encode("The browser's port \u2019is\u2019 open.".encode()).decode() in code
    assert "'s port" not in code
    assert '\u2019' not in code

    for error in (subprocess.TimeoutExpired(command, 15), OSError('PowerShell could not start'), None):
        caplog.clear()
        runner.side_effect = error
        runner.return_value = subprocess.CompletedProcess(command, 1)
        real_notify('Tweet cleanup', 'The port is open.', windows=True, run=runner)
        assert any(record.levelname == 'WARNING' for record in caplog.records)


def test_normal_close_clears_port_record(owned, saved_state, notification):
    state, path = saved_state
    run = owned(state=state, state_path=path)
    run.close()
    closed(run)
    assert 'open_debug_port' not in json.loads(path.read_text())
    notification.assert_not_called()


def test_exited_process_with_answering_endpoint_keeps_record(monkeypatch, saved_state, notification):
    state, path = saved_state
    port = 9222
    state['open_debug_port'] = port
    app.save_state(path, state)
    process = Mock()
    process.poll.return_value = 24
    run = app.BrowserRun(process, f'http://127.0.0.1:{port}', port, Mock(), state, path)
    monkeypatch.setattr(app, 'urlopen', Mock(side_effect=lambda *args, **kwargs: BytesIO(b'{}')))
    clock = itertools.count(0, 31)
    monkeypatch.setattr(app, 'time', SimpleNamespace(monotonic=lambda: next(clock), sleep=Mock()))
    connect = Mock()
    monkeypatch.setattr(run, 'connect', connect)

    run.close()

    notification.assert_called_once()
    assert json.loads(path.read_text())['open_debug_port'] == port
    connect.assert_not_called()


def test_clean_short_window(browser):  # noqa: F811
    page, base, root = browser
    page.set_viewport_size({'width': 982, 'height': 657})
    page.goto(base + '/me/with_replies')
    assert app.clean(page, {'handle': 'me'}, root / 'state.json', pause=lambda: None)


@pytest.mark.parametrize('failure', ['attached', 'closed'])
def test_connect_input_failure(owned, monkeypatch, caplog, failure):
    from playwright.sync_api import Frame

    evaluate = Frame.evaluate
    attempts = []
    target = []

    def read(self, expression, *args, **kwargs):
        if expression == app.INPUT_SCRIPT and (not target or self is target[0]):
            if not target:
                target.append(self)
            attempts.append(None)
            if failure == 'closed':
                self.page.close()
            raise app.PlaywrightError('Input binding unavailable')
        return evaluate(self, expression, *args, **kwargs)

    monkeypatch.setattr(Frame, 'evaluate', read)
    run = owned()
    try:
        assert len(attempts) == (3 if failure == 'attached' else 1)
        if failure == 'attached':
            assert run.used is True
            assert len(caplog.records) == 1
        run.close()
        if failure == 'attached':
            assert run.process.poll() is None
        else:
            closed(run)
    finally:
        monkeypatch.setattr(Frame, 'evaluate', evaluate)


def test_connect_navigation(tmp_path, playwright, fixture_server, monkeypatch):
    from playwright.sync_api import Frame

    base = f'http://127.0.0.1:{fixture_server.server_port}'
    evaluate = Frame.evaluate
    navigated = []

    def read(self, expression, *args, **kwargs):
        # The tab loads a new document after the script ran in the old one.
        if expression == app.INPUT_SCRIPT and self.url.endswith('/me?usertab=1') and not navigated:
            result = evaluate(self, expression, *args, **kwargs)
            self.page.goto(base + '/me?usertab=2')
            navigated.append(None)
            return result
        return evaluate(self, expression, *args, **kwargs)

    monkeypatch.setattr(Frame, 'evaluate', read)
    run = None
    try:
        run = app.launch_browser(playwright, playwright.chromium.executable_path,
            ['--headless', '--no-sandbox',
             base + '/me?usertab=1'])
        tab = next(p for p in run.initial_pages if p.url.endswith('/me?usertab=2'))
        assert navigated
        assert run.used is False
        tab.keyboard.press('a')
        tab.wait_for_timeout(50)
        assert run.used is True
        run.close()
        assert run.process.poll() is None
    finally:
        monkeypatch.setattr(Frame, 'evaluate', evaluate)
        if run is not None and run.process.poll() is None:
            browser = playwright.chromium.connect_over_cdp(run.endpoint)
            browser.new_browser_cdp_session().send('Browser.close')
            run.process.wait(timeout=30)


def test_connect_detached_frame(tmp_path, playwright, monkeypatch):
    from playwright.sync_api import Frame

    evaluate = Frame.evaluate
    attempts = []

    def read(self, expression, *args, **kwargs):
        if expression == app.INPUT_SCRIPT and self.parent_frame is not None:
            attempts.append(None)
            evaluate(self.parent_frame, "document.querySelector('iframe').remove()")
            raise app.PlaywrightError('Frame was detached')
        return evaluate(self, expression, *args, **kwargs)

    monkeypatch.setattr(Frame, 'evaluate', read)
    run = None
    try:
        run = app.launch_browser(playwright, playwright.chromium.executable_path,
            ['--headless', '--no-sandbox',
             'data:text/html,<iframe srcdoc="x"></iframe>'])
        assert len(attempts) == 1
        assert run.used is False
        run.close()
        closed(run)
    finally:
        monkeypatch.setattr(Frame, 'evaluate', evaluate)
        if run is not None and run.process.poll() is None:
            browser = playwright.chromium.connect_over_cdp(run.endpoint)
            browser.new_browser_cdp_session().send('Browser.close')
            run.process.wait(timeout=30)

@pytest.mark.skipif(app.os.name == 'nt', reason='Windows shows notifications')
def test_notification_needs_windows(real_notify):
    runner = Mock()
    real_notify('Tweet cleanup', 'The port is open.', run=runner)
    runner.assert_not_called()


def test_notice_and_notification_after_record_failure(owned, monkeypatch, saved_state, notification):
    state, path = saved_state
    run = owned(state=state, state_path=path)
    monkeypatch.setattr(app, 'save_state', Mock(side_effect=OSError('Disk full')))
    run.warn_open()
    assert run.page.title() == 'Close the browser'
    notification.assert_called_once()


def test_notification_after_interrupted_notice(owned, monkeypatch, notification):
    run = owned()
    monkeypatch.setattr(run.page, 'set_content', Mock(side_effect=KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):
        run.warn_open()
    notification.assert_called_once()


def test_notice_after_blocked_write(owned, monkeypatch, notification):
    run = owned()
    write = run.page.set_content
    calls = Mock(side_effect=[app.PlaywrightError('TrustedHTML required'), None])

    def blocked_once(html, **kwargs):
        if calls() is None:
            write(html, **kwargs)

    monkeypatch.setattr(run.page, 'set_content', blocked_once)
    run.warn_open()
    assert run.page.title() == 'Close the browser'
    notification.assert_called_once()


@pytest.mark.parametrize('script_page_open', [True, False])
def test_notice_leaves_user_tab(owned, playwright, notification, script_page_open):
    run = owned()
    tab = run.context.new_page()
    tab.set_content('<title>Mine</title><p>Draft</p>')
    for page in run.context.pages:
        if page not in (run.page, tab):
            page.close()
    if not script_page_open:
        run.page.close()
    run.close()

    connection = playwright.chromium.connect_over_cdp(run.endpoint)
    try:
        titles = sorted(page.title() for page in connection.contexts[0].pages)
    finally:
        connection.close()
    assert titles == (['Close the browser', 'Mine'] if script_page_open else ['Mine'])
    notification.assert_called_once()


def close_from_outside(run, playwright):
    connection = playwright.chromium.connect_over_cdp(run.endpoint)
    connection.new_browser_cdp_session().send('Browser.close')
    run.process.wait(timeout=30)
    until = time.monotonic() + 30
    while not run.port_closed():
        assert time.monotonic() < until
        time.sleep(0.1)


def test_used_run_after_browser_closed(owned, playwright, saved_state, notification):
    state, path = saved_state
    run = owned(state=state, state_path=path)
    run.mark_used()
    close_from_outside(run, playwright)
    try:
        run.close()
    except app.PlaywrightError:
        pass
    notification.assert_not_called()
    assert 'open_debug_port' not in json.loads(path.read_text())


def test_unused_run_after_browser_closed(owned, playwright, saved_state, notification):
    state, path = saved_state
    run = owned(state=state, state_path=path)
    close_from_outside(run, playwright)
    with pytest.raises(app.PlaywrightError):
        run.close()
    notification.assert_not_called()
    assert 'open_debug_port' not in json.loads(path.read_text())


def test_error_reply_is_not_a_closed_port(monkeypatch, saved_state, notification):
    state, path = saved_state
    process = Mock()
    process.poll.return_value = 0
    monkeypatch.setattr(app, 'urlopen', Mock(side_effect=app.HTTPError('http://127.0.0.1:1/json/version', 500, 'Error', {}, BytesIO())))
    run = app.BrowserRun(process, 'http://127.0.0.1:1', 1, Mock(), state, path)
    run.warn_open()
    notification.assert_called_once()
    assert json.loads(path.read_text())['open_debug_port'] == 1


def test_close_waits_launch_seconds_for_endpoint(monkeypatch, notification):
    process = Mock()
    process.poll.return_value = None
    monkeypatch.setattr(app, 'urlopen', Mock(side_effect=OSError('No endpoint')))
    run = app.BrowserRun(process, 'http://127.0.0.1:1', 1, Mock(), timing=replace(app.Timing(), launch_seconds=0.3))
    start = time.monotonic()
    run.close()
    assert time.monotonic() - start < 5
    notification.assert_called_once()


def test_interrupted_spawn_keeps_port_record(monkeypatch, saved_state, notification):
    state, path = saved_state
    monkeypatch.setattr(app.subprocess, 'Popen', Mock(side_effect=KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):
        app.launch_browser(Mock(), 'browser.exe', state=state, state_path=path)
    assert 'open_debug_port' in json.loads(path.read_text())


def assert_used(run, notification):
    run.close()
    assert run.process.poll() is None
    notification.assert_called_once()
    assert run.used is True


def open_browser_window(run):
    session = run.browser.new_browser_cdp_session()
    try:
        session.send('Target.createTarget', {'url': 'about:blank', 'newWindow': True})
    finally:
        session.detach()


def test_inventory_detects_new_window(owned, notification):
    run = owned()
    run.context.remove_listener('page', run.new_page)
    open_browser_window(run)
    assert run.used is False
    assert_used(run, notification)


def test_inventory_detects_closed_user_tab(owned, notification):
    run = owned()
    next(iter(run.initial_pages)).close()
    assert_used(run, notification)


def test_inventory_detects_user_tab_navigation(owned, notification):
    run = owned()
    next(iter(run.initial_pages)).goto('data:text/html,<title>Another page</title>')
    assert_used(run, notification)


def test_inventory_detects_cleanup_page_off_site(owned, fixture_server, notification):
    port = fixture_server.server_port
    run = owned(home=f'http://127.0.0.1:{port}')
    run.page.goto(f'http://localhost:{port}/me')
    assert_used(run, notification)


def test_inventory_allows_failed_home_navigation(owned, fixture_server, notification):
    home = f'http://127.0.0.1:{fixture_server.server_port}'
    run = owned(home=home)
    run.page.route('**/*', lambda route: route.abort())
    with pytest.raises(app.PlaywrightError, match='net::ERR_FAILED'):
        run.page.goto(home + '/me')
    run.page.wait_for_url('chrome-error://chromewebdata/')
    run.close()
    closed(run)
    notification.assert_not_called()


def test_foreign_change_detects_moved_tab():
    home = 'https://example.com'
    baseline = {'user': ('about:blank', 1), 'own': ('about:blank', 1)}
    assert app.foreign_change(baseline, baseline, 'own', home) is None
    moved = {**baseline, 'user': ('about:blank', 2)}
    assert app.foreign_change(baseline, moved, 'own', home)
    navigated = {**baseline, 'own': (home + '/me', 1)}
    assert app.foreign_change(baseline, navigated, 'own', home) is None
    own_moved = {**baseline, 'own': ('about:blank', 2)}
    assert app.foreign_change(baseline, own_moved, 'own', home)
    own_closed = {'user': ('about:blank', 1)}
    assert app.foreign_change(baseline, own_closed, 'own', home)
    assert app.foreign_change(baseline, own_closed, 'own', home, own_closed=True) is None


def test_wheel_in_user_tab(owned, notification):
    run = owned()
    tab = next(iter(run.initial_pages))
    tab.mouse.wheel(0, 100)
    tab.wait_for_timeout(100)
    assert_used(run, notification)


def test_wheel_in_cleanup_page(owned, notification):
    run = owned()
    run.page.mouse.wheel(0, 100)
    run.page.wait_for_timeout(100)
    assert_used(run, notification)


def test_pointer_move_in_user_tab(owned, notification):
    run = owned()
    tab = next(iter(run.initial_pages))
    tab.mouse.move(20, 20)
    tab.mouse.move(40, 40)
    tab.wait_for_timeout(100)
    assert_used(run, notification)


def test_pointer_move_in_cleanup_page(owned, notification):
    run = owned()
    run.page.mouse.move(20, 20)
    run.page.mouse.move(40, 40)
    run.page.wait_for_timeout(100)
    run.close()
    closed(run)
    notification.assert_not_called()


def test_pointer_move_before_cleanup_page_opens(owned, monkeypatch, notification):
    from playwright.sync_api import BrowserContext

    new_page = BrowserContext.new_page

    def move_before_open(context):
        tab = context.pages[0]
        tab.mouse.move(20, 20)
        tab.mouse.move(40, 40)
        tab.wait_for_timeout(100)
        return new_page(context)

    monkeypatch.setattr(BrowserContext, 'new_page', move_before_open)
    run = owned()
    run.close()
    closed(run)
    notification.assert_not_called()


def test_inventory_error_at_close(owned, monkeypatch, notification):
    run = owned()
    inventory = Mock(side_effect=app.PlaywrightError('Target disappeared'))
    monkeypatch.setattr(app.BrowserRun, 'inventory', inventory)
    assert_used(run, notification)
    inventory.assert_called_once_with()


def test_inventory_error_at_connect(owned, monkeypatch, notification):
    inventory = Mock(side_effect=app.PlaywrightError('Target disappeared'))
    monkeypatch.setattr(app.BrowserRun, 'inventory', inventory)
    run = owned()
    assert run.used is True
    assert_used(run, notification)
    inventory.assert_called_once_with()


def test_inventory_detects_window_opened_during_page_close(owned, monkeypatch, notification):
    run = owned()
    run.context.remove_listener('page', run.new_page)
    close_page = Page.close
    used_after_open = []

    def close_then_open(page):
        close_page(page)
        open_browser_window(run)
        used_after_open.append(run.used)

    monkeypatch.setattr(Page, 'close', close_then_open)
    assert_used(run, notification)
    assert used_after_open == [False]


def test_inventory_allows_scan_and_action_pass(owned, browser, tmp_path, notification):  # noqa: F811
    _, home, _ = browser
    run = owned(home=home)
    run.page.goto(home + '/me')
    app.clean(run.page, {'handle': 'me', 'reposts': {}}, tmp_path / 'state.json',
              live=True, pause=lambda: None)
    assert run.page.evaluate('window.removed')
    assert run.used is False
    run.close()
    closed(run)
    notification.assert_not_called()


def test_closed_cleanup_page_counts_as_use(owned, notification):
    run = owned()
    run.page.close()
    assert_used(run, notification)


def test_pointer_move_while_cleanup_page_closes(owned, monkeypatch, notification):
    run = owned()
    tab = next(iter(run.initial_pages))
    close_page = Page.close

    def close_under_pointer(page):
        close_page(page)
        tab.mouse.move(20, 20)
        tab.mouse.move(40, 40)
        tab.wait_for_timeout(100)

    monkeypatch.setattr(Page, 'close', close_under_pointer)
    run.close()
    closed(run)
    notification.assert_not_called()


def test_missing_tab_record_counts_as_use(owned, notification):
    run = owned()
    run.baseline = None
    assert_used(run, notification)


def test_tab_checks_have_timeouts(owned, monkeypatch, notification):
    from playwright.sync_api import CDPSession

    send = CDPSession.send
    timeouts = []

    def timed(self, method, params=None, **kwargs):
        if method != 'Browser.close':
            timeouts.append((method, kwargs.get('timeout')))
        return send(self, method, params, **kwargs)

    monkeypatch.setattr(CDPSession, 'send', timed)
    run = owned()
    run.close()
    closed(run)
    assert {method for method, _ in timeouts} >= {'Target.getTargetInfo', 'Target.getTargets', 'Browser.getWindowForTarget'}
    assert all(timeout for _, timeout in timeouts)


@pytest.fixture
def cleanup_run(owned, fixture_server, tmp_path):
    home = f'http://127.0.0.1:{fixture_server.server_port}'
    timing = app.Timing(settle_ms=20, idle_passes=5, action_ms=150, verify_seconds=0.15, poll_ms=10)
    run = owned(home=home, timing=timing, state={'handle': 'me', 'reposts': {}},
                state_path=tmp_path / 'state.json')
    run.page.goto(home + '/me')
    return run


def clean_live(run):
    app.clean(run.page, run.state, run.state_path, live=True, pause=lambda: None, timing=run.timing)


def press_shift_before_action(monkeypatch, step):
    check_account = app.check_account
    calls = 0

    def check(page, handle):
        nonlocal calls
        check_account(page, handle)
        calls += 1
        if calls == step:
            page.keyboard.press('Shift')

    monkeypatch.setattr(app, 'check_account', check)


def test_cleanup_stops_between_actions(cleanup_run, monkeypatch):
    page = cleanup_run.page
    page.add_init_script("""addEventListener('keydown', e => {
        if (!e.isTrusted) return;
        const keys = JSON.parse(sessionStorage.cleanupKeys || '[]');
        keys.push(e.key);
        sessionStorage.cleanupKeys = JSON.stringify(keys);
    }, true)""")
    act = app.act
    pressed = False

    def act_then_press(*args, **kwargs):
        nonlocal pressed
        result = act(*args, **kwargs)
        if result == 'removed' and not pressed:
            page.keyboard.press('Shift')
            pressed = True
        return result

    monkeypatch.setattr(app, 'act', act_then_press)
    try:
        with pytest.raises(app.SessionStopped, match='^The cleanup tab was used; run stopped$'):
            clean_live(cleanup_run)
    finally:
        assert len(page.evaluate('window.removed')) == 1
        keys = page.evaluate('JSON.parse(sessionStorage.cleanupKeys)')
        assert 'Escape' not in keys[keys.index('Shift') + 1:]


def test_cleanup_refuses_caret_after_input(cleanup_run, monkeypatch):
    press_shift_before_action(monkeypatch, 1)
    page = cleanup_run.page
    try:
        with pytest.raises(app.SessionStopped, match='^The cleanup tab was used; run stopped$'):
            clean_live(cleanup_run)
    finally:
        assert page.evaluate('window.attempts') == {}
        assert page.locator(app.MENU).count() == 0
        assert page.evaluate('window.removed') == []


def test_cleanup_refuses_delete_item_after_input(cleanup_run, monkeypatch):
    press_shift_before_action(monkeypatch, 2)
    page = cleanup_run.page
    page.add_init_script(f"""new MutationObserver(records => {{
        for (const record of records)
            for (const node of record.addedNodes)
                if (node instanceof Element && node.querySelector({json.dumps(app.CONFIRM)}))
                    sessionStorage.cleanupSheetOpened = 'true';
    }}).observe(document, {{childList: true, subtree: true}});""")
    try:
        with pytest.raises(app.SessionStopped, match='^The cleanup tab was used; run stopped$'):
            clean_live(cleanup_run)
    finally:
        assert page.evaluate('sessionStorage.cleanupSheetOpened') is None
        assert page.locator(app.CONFIRM).count() == 0
        assert page.evaluate('window.removed') == []


def test_cleanup_refuses_confirm_after_input(cleanup_run, monkeypatch):
    press_shift_before_action(monkeypatch, 3)
    try:
        with pytest.raises(app.SessionStopped, match='^The cleanup tab was used; run stopped$'):
            clean_live(cleanup_run)
    finally:
        assert cleanup_run.page.evaluate('window.removed') == []
    state = json.loads(cleanup_run.state_path.read_text())
    today = app.datetime.now().date().isoformat()
    assert state['daily_removals'][today] == 1


def test_cleanup_stays_stopped_after_navigation(cleanup_run):
    page = cleanup_run.page
    page.keyboard.press('Shift')
    page.wait_for_timeout(100)
    page.goto(cleanup_run.home + '/me/with_replies')
    with pytest.raises(app.SessionStopped, match='^The cleanup tab was used; run stopped$'):
        app.guard(page)


def test_cleanup_continues_after_input_in_user_tab(cleanup_run):
    initial = next(iter(cleanup_run.initial_pages))
    initial.mouse.click(20, 20)

    clean_live(cleanup_run)

    assert set(cleanup_run.page.evaluate('window.removed')) == {'1', '2', '5', '7', '8'}


def test_cleanup_continues_after_pointer_movement(cleanup_run):
    page = cleanup_run.page
    page.mouse.move(20, 20)
    page.mouse.move(40, 40)

    clean_live(cleanup_run)

    assert page.evaluate('window.removed')


def test_cleanup_stops_scrolling_after_wheel_input(cleanup_run, monkeypatch):
    page = cleanup_run.page
    page.set_viewport_size({'width': 1280, 'height': 400})
    candidates = app.walk(page, cleanup_run.timing)
    next(candidates)
    before_wheel = page.evaluate('window.scrollY')
    page.mouse.wheel(0, 300)
    page.wait_for_function('y => window.scrollY !== y', arg=before_wheel)
    evaluate = page.evaluate
    scrolls = []

    def record_scroll(expression, *args, **kwargs):
        if 'scrollBy' in expression or 'scrollTo' in expression or 'window[method]' in expression:
            scrolls.append(expression)
        return evaluate(expression, *args, **kwargs)

    monkeypatch.setattr(page, 'evaluate', record_scroll)
    try:
        with pytest.raises(app.SessionStopped, match='^The cleanup tab was used; run stopped$'):
            next(candidates)
    finally:
        assert scrolls == []
        assert page.evaluate('window.scrollY') != before_wheel


def test_cleanup_stops_before_opening_next_tab(cleanup_run, monkeypatch):
    page = cleanup_run.page
    first_url = page.url
    walk = app.walk
    pressed = False

    def walk_then_press(*args, **kwargs):
        nonlocal pressed
        yield from walk(*args, **kwargs)
        if not pressed:
            pressed = True
            page.keyboard.press('Shift')

    monkeypatch.setattr(app, 'walk', walk_then_press)
    try:
        with pytest.raises(app.SessionStopped, match='^The cleanup tab was used; run stopped$'):
            clean_live(cleanup_run)
    finally:
        assert page.url == first_url


def press_in_last_action(monkeypatch, step, last=5, key='Shift'):
    act = app.act
    check_account = app.check_account
    actions = 0
    checks = 0

    def count_actions(*args, **kwargs):
        nonlocal actions, checks
        actions += 1
        checks = 0
        return act(*args, **kwargs)

    def check(page, handle):
        nonlocal checks
        check_account(page, handle)
        checks += 1
        if actions == last and checks == step:
            page.keyboard.press(key)

    monkeypatch.setattr(app, 'act', count_actions)
    monkeypatch.setattr(app, 'check_account', check)
    return lambda: actions


def test_cleanup_stops_when_last_caret_refused(cleanup_run, monkeypatch):
    actions = press_in_last_action(monkeypatch, 1)
    page = cleanup_run.page
    try:
        with pytest.raises(app.SessionStopped, match='^The cleanup tab was used; run stopped$'):
            clean_live(cleanup_run)
    finally:
        assert actions() == 5
        assert len(page.evaluate('window.removed')) == 4
        assert page.locator(app.MENU).count() == 0


def test_cleanup_stops_when_user_escape_closes_last_menu(cleanup_run, monkeypatch):
    eligible = app.eligible
    monkeypatch.setattr(app, 'eligible', lambda data, *args: app.key(data)[0] != 'repost' and eligible(data, *args))
    actions = press_in_last_action(monkeypatch, 2, last=3, key='Escape')
    page = cleanup_run.page
    try:
        with pytest.raises(app.SessionStopped, match='^The cleanup tab was used; run stopped$'):
            clean_live(cleanup_run)
    finally:
        assert actions() == 3
        assert len(page.evaluate('window.removed')) == 2
        assert page.locator(app.MENU).count() == 0
        assert page.locator(app.CONFIRM).count() == 0


def test_cleanup_sends_no_escape_after_input(cleanup_run, monkeypatch):
    page = cleanup_run.page
    page.evaluate("""window.cleanupKeys = [];
        addEventListener('keydown', e => e.isTrusted && window.cleanupKeys.push(e.key), true)""")
    page.locator(app.CARET).first.evaluate('e => e.click()')
    page.locator(app.MENU).first.wait_for(state='visible')
    guard = app.guard

    def guard_then_press(page, timing=None):
        guard(page, timing)
        page.keyboard.press('Shift')

    monkeypatch.setattr(app, 'guard', guard_then_press)
    with pytest.raises(app.SessionStopped, match='^The cleanup tab was used; run stopped$'):
        app.close_layers(page, cleanup_run.timing)
    assert page.evaluate('window.cleanupKeys') == ['Shift']
    assert page.locator(app.MENU).count() == 1


def test_cleanup_refuses_confirm_after_input_in_frame(cleanup_run, monkeypatch):
    page = cleanup_run.page
    check_account = app.check_account
    calls = 0

    def check(page, handle):
        nonlocal calls
        check_account(page, handle)
        calls += 1
        if calls == 3:
            page.evaluate("""() => {
                const frame = document.createElement('iframe');
                frame.srcdoc = '<html lang="en"><input id="field"></html>';
                document.body.append(frame);
            }""")
            page.frame_locator('iframe').locator('#field').focus(timeout=5000)
            page.keyboard.press('Shift')

    monkeypatch.setattr(app, 'check_account', check)
    try:
        with pytest.raises(app.SessionStopped, match='^The cleanup tab was used; run stopped$'):
            clean_live(cleanup_run)
    finally:
        assert page.evaluate('window.removed') == []


def test_scroll_refuses_input_after_check(cleanup_run, monkeypatch):
    page = cleanup_run.page
    stop_if_touched = app.stop_if_touched
    checks = 0

    def check_then_press(page):
        nonlocal checks
        stop_if_touched(page)
        checks += 1
        if checks == 1:
            page.keyboard.press('Shift')

    monkeypatch.setattr(app, 'stop_if_touched', check_then_press)
    before = page.evaluate('window.scrollY')
    with pytest.raises(app.SessionStopped, match='^The cleanup tab was used; run stopped$'):
        app.scroll(page, 'scrollBy', 300)
    assert page.evaluate('window.scrollY') == before


def test_pin_read_stops_before_navigation(cleanup_run):
    page = cleanup_run.page
    page.goto(cleanup_run.home + '/me/with_replies')
    page.keyboard.press('Shift')
    with pytest.raises(app.SessionStopped, match='^The cleanup tab was used; run stopped$'):
        app.read_pin(page, 'me', cleanup_run.timing)
    assert app.urlparse(page.url).path == '/me/with_replies'


def test_pin_read_stops_before_return(cleanup_run, monkeypatch):
    page = cleanup_run.page
    page.goto(cleanup_run.home + '/me/with_replies')
    observe = app.Safety.observe
    pressed = False

    def observe_then_press(self, *args, **kwargs):
        nonlocal pressed
        observe(self, *args, **kwargs)
        if not pressed:
            pressed = True
            page.keyboard.press('Shift')

    monkeypatch.setattr(app.Safety, 'observe', observe_then_press)
    with pytest.raises(app.SessionStopped, match='^The cleanup tab was used; run stopped$'):
        app.read_pin(page, 'me', cleanup_run.timing)
    assert app.urlparse(page.url).path == '/me'


def test_action_pass_stops_before_first_scroll(cleanup_run, monkeypatch):
    page = cleanup_run.page
    open_tab = app.open_tab
    evaluate = page.evaluate
    scrolls = []
    opened_urls = []

    def open_then_press(page, url, *args, **kwargs):
        opened = open_tab(page, url, *args, **kwargs)
        # The action pass opens a tab that the scan opened before.
        if url in opened_urls and not scrolls:
            page.keyboard.press('Shift')
            scrolls.append('pressed')
        opened_urls.append(url)
        return opened

    def record_scroll(expression, *args, **kwargs):
        if scrolls and ('scrollTo' in expression or 'window[method]' in expression):
            scrolls.append(expression)
        return evaluate(expression, *args, **kwargs)

    monkeypatch.setattr(app, 'open_tab', open_then_press)
    monkeypatch.setattr(page, 'evaluate', record_scroll)
    with pytest.raises(app.SessionStopped, match='^The cleanup tab was used; run stopped$'):
        clean_live(cleanup_run)
    assert scrolls == ['pressed']


def test_cleanup_refuses_confirm_after_input_in_cross_origin_frame(cleanup_run, monkeypatch):
    page = cleanup_run.page
    check_account = app.check_account
    calls = 0

    def check(page, handle):
        nonlocal calls
        check_account(page, handle)
        calls += 1
        if calls == 3:
            page.evaluate("""(src) => {
                const frame = document.createElement('iframe');
                frame.src = src;
                document.body.append(frame);
            }""", cleanup_run.home.replace('127.0.0.1', 'localhost') + '/home')
            frame = page.frame_locator('iframe')
            frame.locator('body').click(position={'x': 5, 'y': 5}, timeout=5000)

    monkeypatch.setattr(app, 'check_account', check)
    try:
        with pytest.raises(app.SessionStopped, match='^The cleanup tab was used; run stopped$'):
            clean_live(cleanup_run)
    finally:
        assert page.evaluate('window.removed') == []


@contextmanager
def cleanup_page_after_close(run, playwright):
    url = run.page.url
    run.close()
    connection = playwright.chromium.connect_over_cdp(run.endpoint)
    try:
        yield next(page for page in connection.contexts[0].pages if page.url == url)
    finally:
        connection.close()


def type_draft(page):
    page.evaluate("document.body.append(document.createElement('textarea'))")
    page.locator('textarea').click()
    page.keyboard.type('my draft')


def test_notice_keeps_draft_after_input_stop(cleanup_run, playwright, notification):
    run = cleanup_run
    type_draft(run.page)
    with pytest.raises(app.SessionStopped, match='cleanup tab was used'):
        app.guard(run.page)

    with cleanup_page_after_close(run, playwright) as page:
        assert page.title() != 'Close the browser'
        draft = page.locator('textarea')
        assert draft.count() == 1
        assert draft.input_value() == 'my draft'
    notification.assert_called_once()
    assert json.loads(run.state_path.read_text())['open_debug_port'] == run.port


def test_notice_keeps_late_draft_from_page_flag(cleanup_run, playwright, monkeypatch, notification):
    class DiscardedReports:
        def add(self, page):
            pass

        def __contains__(self, page):
            return False

    monkeypatch.setattr(app, 'TOUCHED_PAGES', DiscardedReports())
    run = cleanup_run
    next(iter(run.initial_pages)).locator('body').click()
    assert run.used
    type_draft(run.page)

    with cleanup_page_after_close(run, playwright) as page:
        draft = page.locator('textarea')
        assert draft.count() == 1
        assert draft.input_value() == 'my draft'
    notification.assert_called_once()


def test_notice_keeps_page_after_cross_origin_frame_input(cleanup_run, playwright, notification):
    run = cleanup_run
    title = run.page.title()
    run.page.evaluate("""(src) => {
        const frame = document.createElement('iframe');
        frame.style.cssText = 'position: fixed; top: 0; left: 0; z-index: 1000';
        frame.src = src;
        document.body.append(frame);
    }""", run.home.replace('127.0.0.1', 'localhost') + '/home')
    frame = run.page.frame_locator('iframe')
    frame.locator('body').click(position={'x': 5, 'y': 5}, timeout=5000)
    until = time.monotonic() + 5
    while not run.used and time.monotonic() < until:
        run.page.wait_for_timeout(20)
    assert run.used
    assert not run.page.evaluate('window.__cleanupTouched === true')
    assert run.page in app.TOUCHED_PAGES

    with cleanup_page_after_close(run, playwright) as page:
        assert page.title() == title
    notification.assert_called_once()


def test_notice_keeps_off_site_page_after_used_end(owned, fixture_server, playwright, notification):
    port = fixture_server.server_port
    home = f'http://127.0.0.1:{port}'
    run = owned(home=home)
    run.page.goto(home + '/me')
    run.mark_used()
    run.page.goto(f'http://localhost:{port}/me')
    title = run.page.title()

    with cleanup_page_after_close(run, playwright) as page:
        assert page.title() == title
    notification.assert_called_once()


def test_notice_keeps_cleanup_page_moved_to_another_window(cleanup_run, playwright, monkeypatch, notification):
    run = cleanup_run
    title = run.page.title()
    inventory = run.inventory()
    url, window = inventory[run.own_target]
    inventory[run.own_target] = (url, window + 1)
    monkeypatch.setattr(run, 'inventory', lambda: inventory)

    with cleanup_page_after_close(run, playwright) as page:
        assert page.title() == title
    notification.assert_called_once()


def test_notice_skips_page_when_flag_read_fails(cleanup_run, monkeypatch, notification):
    run = cleanup_run
    title = run.page.title()
    evaluate = run.page.evaluate
    write = Mock(wraps=run.page.set_content)

    def unreadable(expression, *args, **kwargs):
        if expression == 'window.__cleanupTouched === true':
            raise app.PlaywrightError('Could not read page flag')
        return evaluate(expression, *args, **kwargs)

    monkeypatch.setattr(run.page, 'evaluate', unreadable)
    monkeypatch.setattr(run.page, 'set_content', write)
    run.mark_used()
    run.warn_open()

    write.assert_not_called()
    assert run.page.title() == title
    notification.assert_called_once()


def test_notice_fallback_keeps_draft(cleanup_run, monkeypatch, notification):
    run = cleanup_run
    url = run.page.url
    write = run.page.set_content

    def blocked(html, **kwargs):
        if run.page.url == url:
            type_draft(run.page)
            raise app.PlaywrightError('TrustedHTML required')
        write(html, **kwargs)

    monkeypatch.setattr(run.page, 'set_content', blocked)
    run.warn_open()

    assert run.page.url == url
    assert run.page.locator('textarea').input_value(timeout=1000) == 'my draft'
    notification.assert_called_once()


def test_port_notification_precedes_notice(owned, monkeypatch, saved_state, notification):
    state, path = saved_state
    run = owned(state=state, state_path=path)
    records = []

    def record(message):
        records.append((notification.call_count, json.loads(path.read_text())['open_debug_port']))

    monkeypatch.setattr(run, 'show_notice', record)
    run.warn_open()

    assert records == [(1, run.port)]
    notification.assert_called_once()


def test_input_stop_records_page_from_flag(cleanup_run):
    page = cleanup_run.page
    page.evaluate('window.__cleanupTouched = true')

    with pytest.raises(app.SessionStopped, match='cleanup tab was used'):
        app.stop_if_touched(page)

    assert page in app.TOUCHED_PAGES


def test_notice_uses_untouched_cleanup_page_after_user_tab_input(cleanup_run, playwright, notification):
    run = cleanup_run
    next(iter(run.initial_pages)).locator('body').click()

    with cleanup_page_after_close(run, playwright) as page:
        assert page.title() == 'Close the browser'
    notification.assert_called_once()


def test_notice_on_error_page_after_failed_navigation(owned, fixture_server, notification):
    home = f'http://127.0.0.1:{fixture_server.server_port}'
    run = owned(home=home)
    run.page.route('**/*', lambda route: route.abort())
    with pytest.raises(app.PlaywrightError):
        run.page.goto(home + '/me')
    run.page.wait_for_url('chrome-error://chromewebdata/')
    run.page.unroute('**/*')
    run.mark_used()
    run.warn_open()
    assert run.page.title() == 'Close the browser'
    notification.assert_called_once()


def test_notice_on_blank_cleanup_page(owned, fixture_server, notification):
    run = owned(home=f'http://127.0.0.1:{fixture_server.server_port}')
    assert run.page.url == 'about:blank'
    run.mark_used()
    run.warn_open()
    assert run.page.title() == 'Close the browser'
    notification.assert_called_once()


def test_notice_after_user_tab_navigation_found_at_close(cleanup_run, playwright, notification):
    run = cleanup_run
    run.context.remove_listener('page', run.new_page)
    next(iter(run.initial_pages)).goto('data:text/html,<title>Another page</title>')
    assert not run.used

    with cleanup_page_after_close(run, playwright) as page:
        assert page.title() == 'Close the browser'
    notification.assert_called_once()


def test_notice_keeps_draft_after_same_origin_frame_input(cleanup_run, playwright, notification):
    run = cleanup_run
    title = run.page.title()
    run.page.evaluate("""(src) => {
        const frame = document.createElement('iframe');
        frame.style.cssText = 'position: fixed; top: 0; left: 0; z-index: 1000';
        frame.src = src;
        document.body.append(frame);
    }""", run.home + '/home')
    run.page.frame_locator('iframe').locator('body').click(position={'x': 5, 'y': 5}, timeout=5000)
    until = time.monotonic() + 5
    while not run.used and time.monotonic() < until:
        run.page.wait_for_timeout(20)
    assert run.used

    with cleanup_page_after_close(run, playwright) as page:
        assert page.title() == title
    notification.assert_called_once()


def test_interrupted_flag_read_writes_nothing(owned, monkeypatch, notification):
    run = owned()
    evaluate = run.page.evaluate
    write = Mock(wraps=run.page.set_content)

    def interrupted(expression, *args, **kwargs):
        if expression == 'window.__cleanupTouched === true':
            raise KeyboardInterrupt
        return evaluate(expression, *args, **kwargs)

    monkeypatch.setattr(run.page, 'evaluate', interrupted)
    monkeypatch.setattr(run.page, 'set_content', write)
    with pytest.raises(KeyboardInterrupt):
        run.warn_open()
    write.assert_not_called()
    notification.assert_called_once()


def test_interrupted_log_line_keeps_later_steps(owned, monkeypatch, saved_state, notification):
    state, path = saved_state
    run = owned(state=state, state_path=path)
    monkeypatch.setattr(app.LOGGER, 'warning', Mock(side_effect=KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):
        run.warn_open()
    assert json.loads(path.read_text())['open_debug_port'] == run.port
    notification.assert_called_once()
    assert run.page.title() == 'Close the browser'


def test_interrupted_port_record_keeps_later_steps(owned, monkeypatch, saved_state, notification):
    state, path = saved_state
    run = owned(state=state, state_path=path)
    monkeypatch.setattr(app, 'save_state', Mock(side_effect=KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):
        run.warn_open()
    notification.assert_called_once()
    assert run.page.title() == 'Close the browser'


def test_interrupted_notification_keeps_notice(owned, saved_state, notification):
    state, path = saved_state
    run = owned(state=state, state_path=path)
    notification.side_effect = KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        run.warn_open()
    assert json.loads(path.read_text())['open_debug_port'] == run.port
    assert run.page.title() == 'Close the browser'


@contextmanager
def watchdog_log(path):
    handler = logging.FileHandler(path)
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        yield
    finally:
        root.removeHandler(handler)
        handler.close()


@contextmanager
def browser_call_safety(process):
    safety_used = threading.Event()

    def unblock():
        safety_used.set()
        process.kill()

    timer = threading.Timer(12, unblock)
    timer.daemon = True
    timer.start()
    try:
        yield
    finally:
        timer.cancel()
        timer.join()
        assert not safety_used.is_set(), 'The browser call needed the safety timer'


def assert_watchdog_warning(run, path, notification, log_path, exits):
    assert exits == [(1, None)]
    notification.assert_called_once_with(
        'Tweet cleanup', f'The browser is still open with debug port {run.port}. Close the browser to close the port.')
    assert (f'Run did not finish before the time limit; debug port {run.port} remains open; '
            'close the browser to close the port') in log_path.read_text()
    assert json.loads(path.read_text())['open_debug_port'] == run.port


def test_watchdog_exits_on_hung_page(owned, saved_state, notification, tmp_path):
    state, path = saved_state
    log_path = tmp_path / 'run.log'
    blocked = threading.Event()
    exits = []

    def exit_run(code):
        exits.append((code, run.process.poll()))
        run.process.kill()

    watchdog = app.Watchdog(app.Timing(exit_seconds=0.5), exit_fn=exit_run,
                            monotonic=lambda: float(blocked.is_set()), tick=0.01)
    with watchdog_log(log_path):
        run = owned(state=state, state_path=path, watchdog=watchdog)
        run.page.on('console', lambda message: blocked.set() if message.text == 'Blocking' else None)
        try:
            with browser_call_safety(run.process), pytest.raises(app.PlaywrightError):
                run.page.evaluate("() => { console.log('Blocking'); while (true) {} }")
        finally:
            watchdog.stop()
            run.process.wait(timeout=5)
    assert_watchdog_warning(run, path, notification, log_path, exits)


@pytest.mark.skipif(os.name == 'nt', reason='SIGSTOP requires POSIX')
def test_watchdog_exits_on_frozen_browser(owned, saved_state, notification, tmp_path, monkeypatch):
    state, path = saved_state
    log_path = tmp_path / 'run.log'
    blocked = threading.Event()
    exits = []
    attempts = []
    errors = []
    urlopen = app.urlopen

    def probe(url, **kwargs):
        if not blocked.is_set():
            return urlopen(url, **kwargs)
        attempts.append((url, kwargs.get('timeout')))
        assert kwargs.get('timeout') == 2, 'The port check must have a two-second timeout'
        try:
            return urlopen(url, **kwargs)
        except OSError as exc:
            errors.append(exc.reason if isinstance(exc, URLError) else exc)
            raise

    def exit_run(code):
        exits.append((code, run.process.poll()))
        run.process.kill()

    monkeypatch.setattr(app, 'urlopen', probe)
    watchdog = app.Watchdog(app.Timing(exit_seconds=0.5), exit_fn=exit_run,
                            monotonic=lambda: float(blocked.is_set()), tick=0.01)
    with watchdog_log(log_path):
        run = owned(state=state, state_path=path, watchdog=watchdog)
        try:
            with browser_call_safety(run.process), pytest.raises(app.PlaywrightError):
                run.process.send_signal(signal.SIGSTOP)
                blocked.set()
                run.page.evaluate('1')
        finally:
            with suppress(ProcessLookupError):
                os.kill(run.process.pid, signal.SIGCONT)
            run.process.kill()
            run.process.wait(timeout=5)
            watchdog.stop()
    assert_watchdog_warning(run, path, notification, log_path, exits)
    assert 1 <= len(attempts) <= 3
    assert attempts == [(run.endpoint + '/json/version', 2)] * len(attempts)
    assert len(errors) == len(attempts)
    assert all(isinstance(error, TimeoutError) for error in errors)


def test_watchdog_flushes_file_before_real_exit(tmp_path):
    script = tmp_path / 'run.py'
    script.write_text("""
import logging
import threading
from pathlib import Path
from unittest.mock import Mock

from tweet_cleanup import Timing, Watchdog


import tweet_cleanup as app


class BufferedLog(logging.Handler):
    def emit(self, record):
        stream.write(self.format(record) + '\\n')

    def flush(self):
        stream.flush()


root = logging.getLogger()
stream = Path('run.log').open('w', encoding='utf-8')
root.addHandler(BufferedLog())
app.urlopen = Mock(side_effect=ConnectionRefusedError)
watchdog = Watchdog(
    Timing(exit_seconds=0.1),
    notify_fn=lambda title, text: Path('notice.txt').write_text(text, encoding='utf-8'),
    tick=0.01,
)
watchdog.start(12345, 'http://127.0.0.1:0', Mock(poll=Mock(return_value=None)))
threading.Event().wait()
""", encoding='utf-8')
    repo = Path(app.__file__).resolve().parent
    result = subprocess.run([sys.executable, str(script)], cwd=tmp_path,
                            env={**os.environ, 'PYTHONPATH': str(repo)},
                            timeout=5, capture_output=True, text=True, check=False)
    assert result.returncode == 1, result.stderr
    assert 'debug port 12345 remains open' in (tmp_path / 'run.log').read_text()
    assert (tmp_path / 'notice.txt').read_text() == (
        'The browser is still open with debug port 12345. Close the browser to close the port.')


class WatchdogClock:
    def __init__(self):
        self.now = (0, 0)
        self.samples = Queue()

    def monotonic(self):
        self.sample = self.now
        return self.sample[0]

    def wall(self):
        self.samples.put(self.sample)
        return self.sample[1]

    def observe_ticks(self):
        for _ in range(2):
            try:
                while self.samples.get(timeout=1) != self.now:
                    pass
            except Empty:
                pytest.fail(f'The watchdog stopped sampling at {self.now}')


def test_watchdog_stop_cancels_fire(monkeypatch, notification):
    clock = WatchdogClock()
    exited = threading.Event()
    monkeypatch.setattr(app, 'urlopen', Mock(return_value=nullcontext()))
    watchdog = app.Watchdog(app.Timing(exit_seconds=10), exit_fn=lambda code: exited.set(),
                            monotonic=clock.monotonic, wall_clock=clock.wall, tick=0.01)
    watchdog.start(12345, 'http://127.0.0.1:12345', Mock())
    try:
        clock.observe_ticks()
    finally:
        watchdog.stop()
    clock.now = (11, 11)
    assert not exited.wait(0.1)
    notification.assert_not_called()


def test_watchdog_gives_run_time_after_clock_jump(monkeypatch, notification):
    monkeypatch.setattr(app, 'urlopen', Mock(return_value=nullcontext()))
    for elapsed, wall in [(3609, 3609), (3609, 9), (9, 3609)]:
        clock = WatchdogClock()
        exit_codes = Mock()
        exited, exit_run = wait_for_watchdog(exit_codes)
        timing = app.Timing(exit_seconds=10, wake_seconds=5)
        watchdog = app.Watchdog(timing, exit_fn=exit_run, monotonic=clock.monotonic,
                                wall_clock=clock.wall, tick=0.01)
        watchdog.start(12345, 'http://127.0.0.1:12345', Mock())
        try:
            clock.now = (9, 9)
            clock.observe_ticks()
            clock.now = (elapsed, wall)
            clock.observe_ticks()
            exit_codes.assert_not_called()
            clock.now = (elapsed + 4, wall + 4)
            clock.observe_ticks()
            exit_codes.assert_not_called()
            clock.now = (elapsed + 5, wall + 5)
            assert exited.wait(1), 'The watchdog did not fire after the wake period'
            exit_codes.assert_called_once_with(1)
        finally:
            watchdog.stop()


def wait_for_watchdog(exit_fn=None):
    exited = threading.Event()

    def exit_run(code):
        if exit_fn is not None:
            exit_fn(code)
        exited.set()

    return exited, exit_run


def test_watchdog_skips_notice_when_port_is_closed(monkeypatch, notification, caplog):
    monkeypatch.setattr(app, 'urlopen', Mock(side_effect=ConnectionRefusedError))
    process = Mock(poll=Mock(return_value=0))
    exit_codes = Mock()
    exited, exit_run = wait_for_watchdog(exit_codes)
    watchdog = app.Watchdog(app.Timing(exit_seconds=0), exit_fn=exit_run, tick=0.01)
    watchdog.start(12345, 'http://127.0.0.1:12345', process)
    try:
        assert exited.wait(1), 'The watchdog did not exit when the port was closed'
    finally:
        watchdog.stop()
    exit_codes.assert_called_once_with(1)
    notification.assert_not_called()
    assert caplog.messages == ['Run did not finish before the time limit']


def test_watchdog_keeps_exiting_after_step_failure(monkeypatch, notification):
    monkeypatch.setattr(app, 'urlopen', Mock(return_value=nullcontext()))
    warning = Mock()
    monkeypatch.setattr(app.LOGGER, 'warning', warning)
    for failing_step in (warning, notification):
        failing_step.side_effect = RuntimeError('Step failed')
        exit_codes = Mock()
        exited, exit_run = wait_for_watchdog(exit_codes)
        watchdog = app.Watchdog(app.Timing(exit_seconds=0), exit_fn=exit_run, tick=0.01)
        watchdog.start(12345, 'http://127.0.0.1:12345', Mock())
        try:
            assert exited.wait(1), 'A failed step prevented the watchdog exit'
        finally:
            watchdog.stop()
        exit_codes.assert_called_once_with(1)
        warning.assert_called_once()
        notification.assert_called_once()
        failing_step.side_effect = None
        warning.reset_mock()
        notification.reset_mock()


def test_watchdog_caps_port_attempts(monkeypatch, notification):
    endpoint = 'http://127.0.0.1:12345'
    probe = Mock(side_effect=[TimeoutError, TimeoutError, TimeoutError, AssertionError('Fourth port check')])
    monkeypatch.setattr(app, 'urlopen', probe)
    exit_codes = Mock()
    exited, exit_run = wait_for_watchdog(exit_codes)
    watchdog = app.Watchdog(app.Timing(exit_seconds=0), exit_fn=exit_run, tick=0.01)
    watchdog.start(12345, endpoint, Mock(poll=Mock(return_value=None)))
    try:
        did_exit = exited.wait(1)
    finally:
        watchdog.stop()
    assert probe.call_count == 3
    assert did_exit, 'The watchdog did not exit after the port checks'
    for call in probe.call_args_list:
        assert call.args == (endpoint + '/json/version',)
        assert call.kwargs == {'timeout': 2}
    notification.assert_called_once()
    exit_codes.assert_called_once_with(1)


def test_watchdog_limit_fits_scheduled_task():
    registration = Path(app.__file__).with_name('register_task.ps1').read_text()
    hours = int(re.search(r'-ExecutionTimeLimit\s+\(New-TimeSpan\s+-Hours\s+(\d+)\)', registration)[1])
    timing = app.Timing()
    # Port checks take up to 6 s and the notification waits up to 20 s.
    assert timing.exit_cap_seconds + 6 + 20 + 60 <= hours * 3600
    assert timing.exit_seconds <= timing.exit_cap_seconds
    assert timing.exit_seconds - timing.run_seconds >= 900


def test_browser_close_send_has_timeout(owned, monkeypatch, notification):
    from playwright.sync_api import CDPSession

    send = CDPSession.send
    calls = []

    def record(self, method, params=None, **kwargs):
        if method == 'Browser.close':
            calls.append(kwargs)
        return send(self, method, params, **kwargs)

    monkeypatch.setattr(CDPSession, 'send', record)
    run = owned()
    run.close()
    closed(run)
    assert calls == [{'timeout': run.timing.load_ms}]


def test_watchdog_is_off_by_default(owned):
    before = set(threading.enumerate())
    run = owned()
    assert not set(threading.enumerate()) - before
    run.close()
    assert inspect.signature(app.launch_browser).parameters['watchdog'].default is None


def test_main_passes_and_stops_watchdog(monkeypatch, tmp_path):
    monkeypatch.setattr(app, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(sys, 'argv', ['tweet_cleanup.py', 'run'])
    app.save_state(tmp_path / 'state.json', {'handle': 'me'})
    for error, code in [(None, 0), (RuntimeError('Cleanup failed'), 1), (app.ProfileOpen('Browser open'), 3)]:
        events = []
        watchdog = Mock()
        watchdog.stop.side_effect = lambda events=events: events.append('watchdog')
        watchdog_class = Mock(return_value=watchdog)
        manager = MagicMock()
        manager.__exit__.side_effect = lambda *args, events=events: events.append('playwright')
        run = MagicMock()
        run.__enter__.return_value = run
        run.__exit__.side_effect = lambda *args, events=events: events.append('browser')
        launch = Mock(return_value=run, side_effect=error if code == 3 else None)
        monkeypatch.setattr(app, 'Watchdog', watchdog_class)
        monkeypatch.setattr(app, 'sync_playwright', lambda manager=manager: manager)
        monkeypatch.setattr(app, 'launch_browser', launch)
        monkeypatch.setattr(app, 'clean', Mock(side_effect=error))
        assert app.main('http://127.0.0.1') == code
        watchdog_class.assert_called_once_with()
        assert launch.call_args.kwargs.get('watchdog') is watchdog
        assert events == (['playwright', 'watchdog'] if code == 3 else ['browser', 'playwright', 'watchdog'])


def test_watchdog_exits_while_connect_blocks(owned, saved_state, notification, tmp_path, monkeypatch):
    from playwright.sync_api import Frame
    state, path = saved_state
    log_path = tmp_path / 'run.log'
    blocked = threading.Event()
    exits = []
    processes = []
    evaluate = Frame.evaluate

    def hang(self, expression, *args, **kwargs):
        if expression == app.INPUT_SCRIPT:
            blocked.set()
            expression = '() => { while (true) {} }'
        return evaluate(self, expression, *args, **kwargs)

    def exit_run(code):
        exits.append((code, processes[0].poll()))
        processes[0].kill()

    popen = app.subprocess.Popen

    def spawn(*args, **kwargs):
        process = popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(app.subprocess, 'Popen', spawn)
    monkeypatch.setattr(Frame, 'evaluate', hang)
    watchdog = app.Watchdog(app.Timing(exit_seconds=0.5), exit_fn=exit_run,
                            monotonic=lambda: float(blocked.is_set()), tick=0.01)
    timer = threading.Timer(12, lambda: processes and processes[0].kill())
    timer.daemon = True
    timer.start()
    try:
        with watchdog_log(log_path), pytest.raises(app.PlaywrightError):
            owned(state=state, state_path=path, watchdog=watchdog)
    finally:
        timer.cancel()
        watchdog.stop()
        monkeypatch.setattr(Frame, 'evaluate', evaluate)
    assert exits == [(1, None)]
    port = next(arg.split('=')[1] for arg in processes[0].args if arg.startswith('--remote-debugging-port='))
    # The close after the test kills the browser may warn a second time.
    assert notification.call_args_list[0] == mock_call(
        'Tweet cleanup', f'The browser is still open with debug port {port}. Close the browser to close the port.')
    assert f'Run did not finish before the time limit; debug port {port} remains open' in log_path.read_text()


def test_watchdog_cap_bounds_repeated_clock_jumps(monkeypatch, notification):
    monkeypatch.setattr(app, 'urlopen', Mock(return_value=nullcontext()))
    clock = WatchdogClock()
    exit_codes = Mock()
    exited, exit_run = wait_for_watchdog(exit_codes)
    timing = app.Timing(exit_seconds=10, wake_seconds=50, exit_cap_seconds=100)
    watchdog = app.Watchdog(timing, exit_fn=exit_run, monotonic=clock.monotonic,
                            wall_clock=clock.wall, tick=0.01)
    watchdog.start(12345, 'http://127.0.0.1:12345', Mock())
    try:
        # Two jumps move the fire time to 95 and then to the cap.
        for now in (9, 45, 90, 99):
            clock.now = (now, now)
            clock.observe_ticks()
            exit_codes.assert_not_called()
        clock.now = (100, 100)
        assert exited.wait(1), 'The watchdog did not fire at the cap'
        exit_codes.assert_called_once_with(1)
    finally:
        watchdog.stop()


def test_watchdog_warns_after_unexpected_port_check_error(monkeypatch, notification, caplog):
    monkeypatch.setattr(app, 'urlopen', Mock(side_effect=ValueError('Bad status line')))
    exit_codes = Mock()
    exited, exit_run = wait_for_watchdog(exit_codes)
    watchdog = app.Watchdog(app.Timing(exit_seconds=0), exit_fn=exit_run, tick=0.01)
    watchdog.start(12345, 'http://127.0.0.1:12345', Mock(poll=Mock(return_value=None)))
    try:
        assert exited.wait(1), 'The watchdog did not exit after the port check error'
    finally:
        watchdog.stop()
    exit_codes.assert_called_once_with(1)
    notification.assert_called_once()
    assert any('debug port 12345 remains open' in message for message in caplog.messages)


def test_watchdog_exits_when_notification_hangs(monkeypatch):
    monkeypatch.setattr(app, 'urlopen', Mock(return_value=nullcontext()))
    release = threading.Event()
    exit_codes = Mock()
    exited, exit_run = wait_for_watchdog(exit_codes)
    watchdog = app.Watchdog(app.Timing(exit_seconds=0), exit_fn=exit_run, tick=0.01,
                            notify_fn=lambda *args: release.wait(), notify_seconds=0.2)
    watchdog.start(12345, 'http://127.0.0.1:12345', Mock())
    try:
        assert exited.wait(2), 'A hanging notification kept the watchdog from exiting'
    finally:
        release.set()
        watchdog.stop()
    exit_codes.assert_called_once_with(1)


HANG_CLEANUP_TAB = "() => { console.log('Blocking cleanup tab'); while (true) {} }"


def tab_watchdog_clock():
    blocked = threading.Event()
    elapsed = 0

    def monotonic():
        nonlocal elapsed
        if blocked.is_set():
            elapsed += 1
        return elapsed

    return blocked, monotonic, lambda: elapsed


@pytest.fixture
def tab_requests(monkeypatch):
    urlopen = app.urlopen

    def record(close=None):
        requests = []

        def open_url(url, **kwargs):
            if url.endswith('/json/list') or '/json/close/' in url:
                requests.append(url)
            if '/json/close/' in url and close is not None:
                return close(url, **kwargs)
            return urlopen(url, **kwargs)

        monkeypatch.setattr(app, 'urlopen', open_url)
        return requests

    return record


@pytest.fixture
def stuck_cleanup_run(owned, saved_state, fixture_server, tmp_path, monkeypatch):
    state, path = saved_state
    home = f'http://127.0.0.1:{fixture_server.server_port}'
    log_path = tmp_path / 'run.log'

    @contextmanager
    def start(*, exit_seconds=120, kill_on_exit=True):
        blocked, monotonic, elapsed = tab_watchdog_clock()
        exits = []

        def exit_run(code):
            exits.append((code, run.process.poll(), elapsed()))
            if kill_on_exit:
                run.process.kill()

        timing = app.Timing(tab_close_seconds=2, exit_seconds=exit_seconds)
        watchdog = app.Watchdog(timing, exit_fn=exit_run, monotonic=monotonic, tick=0.01)
        close_step = Mock(wraps=watchdog._close_tab)
        monkeypatch.setattr(watchdog, '_close_tab', close_step)
        with watchdog_log(log_path):
            run = owned(state=state, state_path=path, home=home, watchdog=watchdog)
            try:
                run.page.goto(home + '/me')
                run.page.on('console', lambda message: blocked.set() if message.text == 'Blocking cleanup tab' else None)
                yield run, close_step, exits, log_path
            finally:
                watchdog.stop()
                if kill_on_exit and exits:
                    run.process.wait(timeout=5)

    return start


def hang_cleanup_tab(run):
    with browser_call_safety(run.process), pytest.raises(app.PlaywrightError):
        run.page.evaluate(HANG_CLEANUP_TAB)


def assert_tab_close_requests(run, requests):
    assert requests == [run.endpoint + '/json/list', run.endpoint + '/json/close/' + run.own_target]


def assert_hung_tab_exit(run, saved_state, notification, log_path, exits):
    assert exits == [(1, None, 120)]
    assert_watchdog_warning(run, saved_state[1], notification, log_path, [(code, status) for code, status, _ in exits])


def test_watchdog_closes_hung_cleanup_tab(stuck_cleanup_run, saved_state, tab_requests, notification):
    requests = tab_requests()
    with stuck_cleanup_run(exit_seconds=3000, kill_on_exit=False) as (run, _, exits, log_path):
        hang_cleanup_tab(run)
        assert run.process.poll() is None
        assert_tab_close_requests(run, requests)
        run.close()
        closed(run)
    assert 'open_debug_port' not in json.loads(saved_state[1].read_text())
    notification.assert_not_called()
    assert not exits
    log = log_path.read_text()
    assert 'The cleanup tab stopped responding; closed it so the run can end' in log
    assert 'The cleanup tab closed' not in log


def test_watchdog_keeps_touched_cleanup_tab(stuck_cleanup_run, saved_state, tab_requests, notification):
    requests = tab_requests()
    with stuck_cleanup_run() as (run, close_step, exits, log_path):
        app.TOUCHED_PAGES.add(run.page)
        hang_cleanup_tab(run)
    close_step.assert_called_once()
    assert not requests
    assert_hung_tab_exit(run, saved_state, notification, log_path, exits)


def test_watchdog_keeps_changed_cleanup_tab(stuck_cleanup_run, saved_state, tab_requests, notification):
    requests = tab_requests()
    with stuck_cleanup_run() as (run, close_step, exits, log_path):
        run.cleanup_tab_changed = True
        hang_cleanup_tab(run)
    close_step.assert_called_once()
    assert not requests
    assert_hung_tab_exit(run, saved_state, notification, log_path, exits)


def test_watchdog_keeps_off_site_cleanup_tab(stuck_cleanup_run, saved_state, tab_requests, notification, fixture_server):
    requests = tab_requests()
    with stuck_cleanup_run() as (run, _, exits, log_path):
        run.page.goto(f'http://localhost:{fixture_server.server_port}/me')
        assert not run.cleanup_tab_changed
        hang_cleanup_tab(run)
    assert requests == [run.endpoint + '/json/list']
    assert_hung_tab_exit(run, saved_state, notification, log_path, exits)


def test_watchdog_keeps_cleanup_tab_during_used_close(stuck_cleanup_run, tab_requests, monkeypatch):
    requests = tab_requests()
    with stuck_cleanup_run() as (run, close_step, exits, _):
        run.mark_used()
        evaluate = run.page.evaluate
        monkeypatch.setattr(run.page, 'evaluate', lambda *args: evaluate(HANG_CLEANUP_TAB))
        with browser_call_safety(run.process):
            run.close()
    close_step.assert_called_once()
    assert not requests
    assert exits == [(1, None, 120)]


def test_watchdog_keeps_hung_user_tab_during_connect(owned, saved_state, tab_requests, monkeypatch):
    from contextlib import ExitStack

    from playwright.sync_api import Frame

    requests = tab_requests()
    state, path = saved_state
    blocked, monotonic, elapsed = tab_watchdog_clock()
    processes = []
    exits = []
    evaluate = Frame.evaluate
    popen = app.subprocess.Popen

    def hang(frame, expression, *args, **kwargs):
        if expression == app.INPUT_SCRIPT:
            blocked.set()
            expression = HANG_CLEANUP_TAB
        return evaluate(frame, expression, *args, **kwargs)

    def exit_run(code):
        process = processes[0]
        exits.append((code, process.poll(), elapsed()))
        process.kill()

    timing = app.Timing(tab_close_seconds=2, exit_seconds=120)
    watchdog = app.Watchdog(timing, exit_fn=exit_run, monotonic=monotonic, tick=0.01)
    close_step = Mock(wraps=watchdog._close_tab)
    monkeypatch.setattr(watchdog, '_close_tab', close_step)
    monkeypatch.setattr(Frame, 'evaluate', hang)
    with ExitStack() as safety:
        def spawn(*args, **kwargs):
            process = popen(*args, **kwargs)
            processes.append(process)
            safety.enter_context(browser_call_safety(process))
            return process

        monkeypatch.setattr(app.subprocess, 'Popen', spawn)
        try:
            with pytest.raises(app.PlaywrightError):
                owned(state=state, state_path=path, watchdog=watchdog)
        finally:
            watchdog.stop()
            for process in processes:
                process.wait(timeout=5)
    close_step.assert_called_once()
    assert not requests
    assert exits == [(1, None, 120)]


def time_out_tab_close(url, *, timeout):
    raise TimeoutError(url)


def test_watchdog_attempts_failed_tab_close_once(stuck_cleanup_run, saved_state, tab_requests, notification):
    requests = tab_requests(close=time_out_tab_close)
    with stuck_cleanup_run() as (run, _, exits, log_path):
        hang_cleanup_tab(run)
    assert_tab_close_requests(run, requests)
    assert not run.watchdog_closed_tab.is_set()
    assert run.moves_until == float('inf')
    assert 'Could not close the cleanup tab that stopped responding' in log_path.read_text()
    assert_hung_tab_exit(run, saved_state, notification, log_path, exits)


def test_watchdog_exits_when_tab_close_has_no_effect(stuck_cleanup_run, saved_state, tab_requests, notification):
    requests = tab_requests(close=lambda url, **kwargs: nullcontext(SimpleNamespace(status=200)))
    with stuck_cleanup_run() as (run, _, exits, log_path):
        hang_cleanup_tab(run)
    assert_tab_close_requests(run, requests)
    assert_hung_tab_exit(run, saved_state, notification, log_path, exits)


def test_run_reads_watchdog_close_flag_after_inventory(owned, saved_state, notification, monkeypatch, caplog):
    state, path = saved_state
    run = owned(state=state, state_path=path)
    inventory = run.inventory

    def close_during_inventory():
        if not run.watchdog_closed_tab.is_set():
            run.watchdog_closed_tab.set()
            with app.urlopen(run.endpoint + '/json/close/' + run.own_target, timeout=2) as response:
                assert response.status == 200
            run.page.wait_for_event('close', timeout=3000)
        return inventory()

    monkeypatch.setattr(run, 'inventory', close_during_inventory)
    run.close()
    closed(run)
    assert 'open_debug_port' not in json.loads(path.read_text())
    assert 'The cleanup tab closed' not in caplog.text
    notification.assert_not_called()


def test_run_counts_user_close_after_failed_watchdog_close(owned, saved_state, notification, tab_requests, caplog):
    state, path = saved_state
    requests = tab_requests(close=time_out_tab_close)
    run = owned(state=state, state_path=path)
    app.Watchdog()._close_tab(run.endpoint, run)
    run.page.close()
    run.close()
    assert_tab_close_requests(run, requests)
    assert run.used
    assert 'The cleanup tab closed' in caplog.text
    notification.assert_called_once_with(
        'Tweet cleanup', f'The browser is still open with debug port {run.port}. Close the browser to close the port.')
    assert run.process.poll() is None


def test_watchdog_moves_tab_close_after_clock_jump(monkeypatch, notification):
    endpoint = 'http://127.0.0.1:12345'
    list_url = endpoint + '/json/list'
    run = Mock(closed=False, own_target='cleanup', page=Mock(), cleanup_tab_changed=False, home=None)
    request = Mock(side_effect=lambda url, **kwargs: nullcontext(BytesIO(b'[]')))
    monkeypatch.setattr(app, 'urlopen', request)

    for exit_seconds in (3700, 20):
        clock = WatchdogClock()
        exit_codes = Mock()
        exited, exit_run = wait_for_watchdog(exit_codes)
        timing = app.Timing(tab_close_seconds=10, exit_seconds=exit_seconds, wake_seconds=5, exit_cap_seconds=4000)
        watchdog = app.Watchdog(timing, exit_fn=exit_run, monotonic=clock.monotonic,
                                wall_clock=clock.wall, tick=0.01)
        request.reset_mock()
        watchdog.start(12345, endpoint, Mock(), run=run)
        try:
            for now in (9, 3609, 3613):
                clock.now = (now, now)
                clock.observe_ticks()
                request.assert_not_called()
                exit_codes.assert_not_called()
            clock.now = (3614, 3614)
            if exit_seconds == 3700:
                clock.observe_ticks()
                request.assert_called_once_with(list_url, timeout=2)
                exit_codes.assert_not_called()
            else:
                assert exited.wait(1), 'The watchdog did not fire after the wake period'
                exit_codes.assert_called_once_with(1)
                request.assert_called_once_with(endpoint + '/json/version', timeout=2)
        finally:
            watchdog.stop()


def test_watchdog_excludes_tab_close_step_time_from_clock_jump(monkeypatch, notification):
    endpoint = 'http://127.0.0.1:12345'
    clock = WatchdogClock()
    exit_codes = Mock()
    exited, exit_run = wait_for_watchdog(exit_codes)
    run = Mock(closed=False, own_target='cleanup', page=Mock(), cleanup_tab_changed=False, home=None)

    def open_url(url, **kwargs):
        if url == endpoint + '/json/list':
            clock.now = (50, 50)
            raise TimeoutError(url)
        return nullcontext()

    request = Mock(side_effect=open_url)
    monkeypatch.setattr(app, 'urlopen', request)
    timing = app.Timing(tab_close_seconds=10, exit_seconds=55, wake_seconds=60)
    watchdog = app.Watchdog(timing, exit_fn=exit_run, monotonic=clock.monotonic,
                            wall_clock=clock.wall, tick=0.01)
    watchdog.start(12345, endpoint, Mock(), run=run)
    try:
        clock.now = (10, 10)
        clock.observe_ticks()
        request.assert_called_once_with(endpoint + '/json/list', timeout=2)
        assert clock.now == (50, 50)
        exit_codes.assert_not_called()
        clock.now = (55, 55)
        assert exited.wait(1), 'The watchdog missed its original fire time'
        exit_codes.assert_called_once_with(1)
    finally:
        watchdog.stop()


def test_watchdog_tab_close_limit_fits_run_and_exit():
    timing = app.Timing()
    assert timing.tab_close_seconds - timing.run_seconds >= 300
    assert timing.exit_seconds - timing.tab_close_seconds >= 600


def test_watchdog_rechecks_touch_after_target_list(stuck_cleanup_run, saved_state, tab_requests, notification):
    runs = []
    requests = tab_requests()
    recorded = app.urlopen

    def open_url(url, **kwargs):
        if url.endswith('/json/list'):
            app.TOUCHED_PAGES.add(runs[0].page)
        return recorded(url, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(app, 'urlopen', open_url)
        with stuck_cleanup_run() as (run, _, exits, log_path):
            runs.append(run)
            hang_cleanup_tab(run)
    assert requests == [run.endpoint + '/json/list']
    assert_hung_tab_exit(run, saved_state, notification, log_path, exits)


def test_watchdog_rechecks_close_start_after_target_list(stuck_cleanup_run, saved_state, tab_requests, notification):
    runs = []
    requests = tab_requests()
    recorded = app.urlopen

    def open_url(url, **kwargs):
        if url.endswith('/json/list'):
            runs[0].closed = True
        return recorded(url, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(app, 'urlopen', open_url)
        with stuck_cleanup_run() as (run, _, exits, log_path):
            runs.append(run)
            hang_cleanup_tab(run)
    assert requests == [run.endpoint + '/json/list']
    assert_hung_tab_exit(run, saved_state, notification, log_path, exits)


@pytest.mark.skipif(os.name == 'nt', reason='SIGSTOP requires POSIX')
def test_watchdog_exits_when_browser_freezes_during_tab_close(stuck_cleanup_run, saved_state, tab_requests, notification):
    runs = []
    requests = tab_requests()
    recorded = app.urlopen
    timeouts = []

    def open_url(url, **kwargs):
        if '/json/' in url and not url.endswith('/json/version'):
            timeouts.append(kwargs.get('timeout'))
        if url.endswith('/json/list'):
            with recorded(url, **kwargs) as response:
                data = response.read()
            runs[0].process.send_signal(signal.SIGSTOP)
            return nullcontext(BytesIO(data))
        return recorded(url, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(app, 'urlopen', open_url)
        with stuck_cleanup_run() as (run, _, exits, log_path):
            runs.append(run)
            try:
                hang_cleanup_tab(run)
            finally:
                with suppress(ProcessLookupError):
                    os.kill(run.process.pid, signal.SIGCONT)
    assert_tab_close_requests(run, requests)
    assert timeouts == [2, 2]
    assert not run.watchdog_closed_tab.is_set()
    assert 'Could not close the cleanup tab that stopped responding' in log_path.read_text()
    assert_hung_tab_exit(run, saved_state, notification, log_path, exits)


def test_pointer_move_after_watchdog_tab_close(stuck_cleanup_run, saved_state, tab_requests, notification):
    tab_requests()
    with stuck_cleanup_run(exit_seconds=3000, kill_on_exit=False) as (run, _, exits, _):
        user_tab = next(iter(run.initial_pages))
        hang_cleanup_tab(run)
        user_tab.mouse.move(20, 20)
        user_tab.mouse.move(40, 40)
        user_tab.wait_for_timeout(100)
        run.close()
        closed(run)
    notification.assert_not_called()
    assert not exits
