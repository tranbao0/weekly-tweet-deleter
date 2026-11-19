import json
import sys
from contextlib import nullcontext
from pathlib import PureWindowsPath
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest
from test_tweet_cleanup import browser  # noqa: F401

import tweet_cleanup as app

EDGE = r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'


@pytest.mark.parametrize(('progid', 'command', 'expected'), [
    ('ChromeHTML', r'"C:\Program Files\Chrome\chrome.exe" -- "%1"', 'chrome.exe'),
    ('BraveHTML', r'C:\Brave\brave.exe "%1"', 'brave.exe'),
    ('Opera GXStable', r'"C:\Opera GX\opera.exe" -noautoupdate -- "%1"', 'opera.exe'),
    ('FirefoxURL', r'"C:\Program Files\Mozilla Firefox\firefox.exe" -url "%1"', 'msedge.exe'),
    ('Unknown', r'C:\Browser\unknown.exe "%1"', 'msedge.exe'),
    (None, None, 'msedge.exe'),
])
def test_browser_choice(monkeypatch, progid, command, expected):
    def open_key(root, key):
        if root == 'HKCU' and progid is None:
            raise FileNotFoundError(key)
        return nullcontext((root, key))

    def query_value(key, value):
        root, path = key
        if root == 'HKCU':
            assert path == r'Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice'
            assert value == 'ProgId'
            return progid, 1
        assert value == ''
        if root == 'HKCR':
            assert path == progid + r'\shell\open\command'
            return command, 1
        assert path == r'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe'
        return EDGE, 1

    registry = SimpleNamespace(HKEY_CURRENT_USER='HKCU', HKEY_CLASSES_ROOT='HKCR', HKEY_LOCAL_MACHINE='HKLM',
                               OpenKey=open_key, QueryValueEx=query_value)
    monkeypatch.setitem(sys.modules, 'winreg', registry)
    assert PureWindowsPath(app.browser_executable()).name == expected


def test_browser_override(monkeypatch):
    registry = Mock()
    monkeypatch.setitem(sys.modules, 'winreg', registry)
    assert app.browser_executable('custom.exe') == 'custom.exe'
    registry.OpenKey.assert_not_called()


def test_locked_profile_skips(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(app, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(sys, 'argv', ['tweet_cleanup.py', 'run'])
    lock = tmp_path / 'browser-profile' / 'lockfile'
    lock.parent.mkdir()
    lock.touch()
    original = type(lock).open
    assert not app.profile_open(lock.parent)

    def open_path(path, *args, **kwargs):
        if path == lock:
            raise PermissionError(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(type(lock), 'open', open_path)
    spawn = Mock()
    monkeypatch.setattr(app.subprocess, 'Popen', spawn)
    assert app.main('http://127.0.0.1') == 3
    assert 'Tweet cleanup browser window is already open' in capsys.readouterr().out
    spawn.assert_not_called()


@pytest.mark.parametrize(('typed', 'code'), [('@ME', 0), ('other', 1)])
def test_setup_handle(browser, monkeypatch, capsys, typed, code):  # noqa: F811
    page, base, root = browser
    monkeypatch.setattr(app, 'DATA_DIR', root)
    monkeypatch.setattr(sys, 'argv', ['tweet_cleanup.py', 'setup', '--handle', typed])
    monkeypatch.setattr(app, 'launch_browser', Mock(return_value=MagicMock(page=page, executable='C:/browser.exe')))
    assert app.main(base) == code
    output = capsys.readouterr().out
    path = root / 'state.json'
    if code == 0:
        assert json.loads(path.read_text())['handle'] == 'me'
        assert 'run' in output
    else:
        assert 'does not match' in output
        assert not path.exists()


def test_setup_waits_for_login(browser, monkeypatch):  # noqa: F811
    page, base, root = browser
    monkeypatch.setattr(app, 'DATA_DIR', root)
    monkeypatch.setattr(sys, 'argv', ['tweet_cleanup.py', 'setup'])
    monkeypatch.setattr(app, 'launch_browser', Mock(return_value=MagicMock(page=page, executable='C:/browser.exe')))

    def login(route):
        if route.request.url.endswith('/home'):
            route.fulfill(content_type='text/html', body='<html lang="en"><input autocomplete="username">')
        else:
            route.fulfill(content_type='text/html', body="""
                <html lang="en"><input autocomplete="username">
                <script>
                    document.querySelector('input').oninput = () => setTimeout(() => location.href = '/me', 200);
                </script></html>""")
            page.once('load', fill_login)

    def fill_login(*_):
        app.TOUCHED_PAGES.add(page)
        page.locator('input').press('a')

    page.route('**/home', login, times=1)
    page.route('**/i/flow/login', login)
    assert app.main(base) == 0
    assert json.loads((root / 'state.json').read_text())['handle'] == 'me'


def test_setup_input_does_not_stop_pin_read(browser, monkeypatch):  # noqa: F811
    page, base, root = browser
    monkeypatch.setattr(app, 'DATA_DIR', root)
    monkeypatch.setattr(sys, 'argv', ['tweet_cleanup.py', 'setup'])
    monkeypatch.setattr(app, 'launch_browser', Mock(return_value=MagicMock(page=page, executable='C:/browser.exe')))
    page.goto(base + '/me?nopin=1&fresh=1')
    page.evaluate('window.__cleanupTouched = true')
    app.TOUCHED_PAGES.add(page)
    assert app.main(base) == 0
    state = json.loads((root / 'state.json').read_text())
    assert state['handle'] == 'me'
    assert state['browser'] == 'C:/browser.exe'
    assert 'pinned' not in state


def test_run_uses_browser_from_setup(monkeypatch, tmp_path):
    monkeypatch.setattr(app, 'DATA_DIR', tmp_path)
    saved = tmp_path / 'browser.exe'
    saved.touch()
    app.save_state(tmp_path / 'state.json', {'handle': 'me', 'reposts': {}, 'browser': str(saved)})
    launch = Mock(side_effect=app.SessionStopped('stop'))
    monkeypatch.setattr(app, 'launch_browser', launch)
    monkeypatch.setattr(sys, 'argv', ['tweet_cleanup.py', 'run'])
    assert app.main('http://127.0.0.1') == 1
    assert launch.call_args.args[1] == str(saved)
    saved.unlink()
    launch.reset_mock()
    assert app.main('http://127.0.0.1') == 1
    launch.assert_not_called()
