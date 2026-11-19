import csv
from datetime import timedelta

from test_tweet_cleanup import browser, execute  # noqa: F401

import tweet_cleanup as app


def test_first_live_stall_then_new_repost(browser):  # noqa: F811
    state = {'handle': 'me'}
    execute(browser, live=True, query='?stall=1', state=state, route='/me/reposts')
    _, page = execute(browser, live=True, state=state, route='/me/reposts')
    assert '5' not in page.evaluate('window.removed')


def test_daily_limit(browser):  # noqa: F811
    state = {'handle': 'me'}
    for _ in range(3):
        _, page = execute(browser, live=True, limit=2, state=state, query='?endless=1&count=12')
        assert len(page.evaluate('window.removed')) == 2


def test_daily_limit_next_day(browser, monkeypatch):  # noqa: F811
    state = {'handle': 'me'}
    _, page = execute(browser, live=True, limit=2, state=state)
    assert set(page.evaluate('window.removed')) == {'1', '7'}
    original = app.datetime

    class Tomorrow(original):
        @classmethod
        def now(cls, tz=None):
            return original.now(tz) + timedelta(days=1)

    monkeypatch.setattr(app, 'datetime', Tomorrow)
    _, page = execute(browser, live=True, limit=2, state=state, route='/me/with_replies')
    assert set(page.evaluate('window.removed')) == {'1', '2', '7', '8'}


def test_setup_pin_third(browser, capsys):  # noqa: F811
    page, base, root = browser
    page.goto(base + "/me")
    page.evaluate("selected.splice(2, 0, selected.splice(1,1)[0]); document.querySelector('main').innerHTML=''; next=0; add(); add();")
    # Preserve the third article across navigation to the Posts tab.
    content = page.content()
    import re
    content = re.sub(r'<script>.*?</script>', '', content, flags=re.DOTALL)
    page.route('**/me', lambda route: route.fulfill(body=content, content_type='text/html'))
    state = {}
    app.save_setup(page, state, root / 'state.json')
    page.unroute('**/me')
    assert not state.get('cleanup_started')
    assert state.get('pinned') == '4'
    assert f'Pinned post: {base}/me/status/4' in capsys.readouterr().out
    _, page = execute(browser, live=True, query='?nopin=1', state=state)
    assert '4' not in page.evaluate('window.removed')


def test_csv_escape(tmp_path):
    path = tmp_path / 'removed.csv'
    app.audit(path, {'text': '=HYPERLINK(...)'}, 'dry_run')
    with path.open() as stream:
        assert next(csv.DictReader(stream))['text'] == "'=HYPERLINK(...)"


def test_dry_stdout(browser, capsys):  # noqa: F811
    results, _ = execute(browser)
    output = capsys.readouterr().out
    for row in results:
        assert f"{row['action']} {row['posted_at']} {row['url']} {row['text'][:80]}" in output
    assert f'{len(results)} items' in output


def test_paths_from_other_folder(tmp_path, monkeypatch):
    import sys
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, 'argv', ['tweet_cleanup.py', 'run', '--live'])
    paths = []

    def read(path, **kwargs):
        paths.append(path)
        return '{"last_live_date": "' + app.datetime.now().date().isoformat() + '"}'

    monkeypatch.setattr(app.Path, 'exists', lambda self: True)
    monkeypatch.setattr(app.Path, 'read_text', read)
    app.main()
    assert paths == [app.Path(app.__file__).resolve().parent / 'state.json']
    assert not (tmp_path / 'logs').is_dir()


def test_spent_limit_skips_live_run(monkeypatch, tmp_path, caplog, capsys):
    import sys
    from unittest.mock import Mock

    today = app.datetime.now().date().isoformat()
    monkeypatch.setattr(app, 'DATA_DIR', tmp_path)
    app.save_state(tmp_path / 'state.json', {'daily_removals': {today: 200}})
    monkeypatch.setattr(sys, 'argv', ['tweet_cleanup.py', 'run', '--live'])
    launch = Mock(side_effect=AssertionError('Browser must stay closed'))
    monkeypatch.setattr(app, 'launch_browser', launch)
    caplog.set_level('INFO')

    assert app.main('http://127.0.0.1') == 0
    launch.assert_not_called()
    message = 'Daily removal limit reached; run skipped'
    assert any(record.levelname == 'INFO' and record.getMessage() == message for record in caplog.records)
    assert capsys.readouterr().out.strip() == message


def test_larger_limit_runs_without_done_date(monkeypatch, tmp_path):
    import sys
    from unittest.mock import Mock

    today = app.datetime.now().date().isoformat()
    monkeypatch.setattr(app, 'DATA_DIR', tmp_path)
    app.save_state(tmp_path / 'state.json', {'daily_removals': {today: 200}})
    monkeypatch.setattr(sys, 'argv', ['tweet_cleanup.py', 'run', '--live', '--max', '300'])
    launch = Mock(side_effect=app.ProfileOpen('The Tweet cleanup browser window is already open; close it and try again'))
    monkeypatch.setattr(app, 'launch_browser', launch)

    assert app.main('http://127.0.0.1') == 3
    launch.assert_called_once()


def test_partial_run_that_spends_limit_records_done_date(browser):  # noqa: F811
    today = app.datetime.now().date().isoformat()
    state = {'handle': 'me'}

    results, page = execute(browser, live=True, limit=2, query='?stalltab=reposts', state=state)

    assert len(results) == len(page.evaluate('window.removed')) == 2
    assert state['daily_removals'] == {today: 2}
    assert state.get('last_live_date') == today


def test_live_run_uses_start_date_across_midnight(browser, monkeypatch):  # noqa: F811
    original = app.datetime
    start = original.now().replace(hour=23, minute=59)
    current = start

    class Clock(original):
        @classmethod
        def now(cls, tz=None):
            return current if tz is None else current.astimezone(tz)

    save_state = app.save_state

    def save(path, state):
        nonlocal current
        current = start + timedelta(minutes=2)
        save_state(path, state)

    monkeypatch.setattr(app, 'datetime', Clock)
    monkeypatch.setattr(app, 'save_state', save)
    state = {'handle': 'me'}

    results, _ = execute(browser, live=True, limit=2, state=state)

    assert len(results) == 2
    assert state['daily_removals'] == {start.date().isoformat(): 2}
    assert state.get('last_live_date') == start.date().isoformat()


def test_live_exit_uses_written_done_date_after_midnight(monkeypatch, tmp_path, capsys):
    import sys
    from unittest.mock import MagicMock, Mock

    original = app.datetime
    start = original.now().replace(hour=23, minute=59)
    current = start

    class Clock(original):
        @classmethod
        def now(cls, tz=None):
            return current if tz is None else current.astimezone(tz)

    def finish(page, state, path, *args, **kwargs):
        nonlocal current
        current = start + timedelta(minutes=2)
        state['last_live_date'] = start.date().isoformat()

    monkeypatch.setattr(app, 'datetime', Clock)
    monkeypatch.setattr(app, 'DATA_DIR', tmp_path)
    path = tmp_path / 'state.json'
    app.save_state(path, {'handle': 'me'})
    monkeypatch.setattr(sys, 'argv', ['tweet_cleanup.py', 'run', '--live'])
    run = MagicMock(page=Mock(), deadline=None)
    monkeypatch.setattr(app, 'launch_browser', Mock(return_value=run))
    monkeypatch.setattr(app, 'clean', finish)

    assert app.main('http://127.0.0.1') == 0

    app.save_state(path, {'handle': 'me', 'last_live_date': start.date().isoformat()})
    clean = Mock()
    monkeypatch.setattr(app, 'clean', clean)
    assert app.main('http://127.0.0.1') == 1
    clean.assert_called_once()
    assert 'Run ends with an error' in capsys.readouterr().out


def test_dry_run_lists_items_after_limit_is_spent(browser):  # noqa: F811
    today = app.datetime.now().date().isoformat()
    state = {'handle': 'me', 'daily_removals': {today: 200}}

    results, page = execute(browser, limit=200, state=state)

    assert results
    assert not page.evaluate('window.removed')
    assert state['daily_removals'] == {today: 200}
    assert 'last_live_date' not in state


def test_run_that_spends_the_rest_of_the_limit_records_done_date(browser):  # noqa: F811
    today = app.datetime.now().date().isoformat()
    state = {'handle': 'me', 'daily_removals': {today: 1}}

    results, page = execute(browser, live=True, limit=2, query='?stalltab=reposts', state=state)

    assert len(results) == len(page.evaluate('window.removed')) == 1
    assert state['daily_removals'] == {today: 2}
    assert state.get('last_live_date') == today


def test_dry_run_starts_browser_after_limit_is_spent(monkeypatch, tmp_path):
    import sys
    from unittest.mock import Mock

    today = app.datetime.now().date().isoformat()
    monkeypatch.setattr(app, 'DATA_DIR', tmp_path)
    app.save_state(tmp_path / 'state.json', {'daily_removals': {today: 200}})
    monkeypatch.setattr(sys, 'argv', ['tweet_cleanup.py', 'run'])
    launch = Mock(side_effect=app.ProfileOpen('The Tweet cleanup browser window is already open; close it and try again'))
    monkeypatch.setattr(app, 'launch_browser', launch)

    assert app.main('http://127.0.0.1') == 3
    launch.assert_called_once()


def test_live_run_passes_its_start_date_to_cleanup(monkeypatch, tmp_path):
    import sys
    from unittest.mock import MagicMock, Mock

    original = app.datetime
    start = original.now().replace(hour=23, minute=59)
    current = start

    class Clock(original):
        @classmethod
        def now(cls, tz=None):
            return current if tz is None else current.astimezone(tz)

    def launch(*args, **kwargs):
        nonlocal current
        current = start + timedelta(minutes=2)
        return MagicMock(page=Mock(), deadline=None)

    days = []

    def finish(page, state, path, *args, **kwargs):
        days.append(kwargs.get('day'))
        state['last_live_date'] = kwargs.get('day')

    monkeypatch.setattr(app, 'datetime', Clock)
    monkeypatch.setattr(app, 'DATA_DIR', tmp_path)
    app.save_state(tmp_path / 'state.json', {'handle': 'me'})
    monkeypatch.setattr(sys, 'argv', ['tweet_cleanup.py', 'run', '--live'])
    monkeypatch.setattr(app, 'launch_browser', launch)
    monkeypatch.setattr(app, 'clean', finish)

    assert app.main('http://127.0.0.1') == 0
    assert days == [start.date().isoformat()]


def test_cleanup_counts_under_the_given_day(browser, monkeypatch):  # noqa: F811
    original = app.datetime
    day = original.now().date()

    class Clock(original):
        @classmethod
        def now(cls, tz=None):
            return original.now(tz) + timedelta(days=1)

    monkeypatch.setattr(app, 'datetime', Clock)
    page, base, root = browser
    page.goto(base + '/me?fresh=1')
    state = {'handle': 'me'}

    app.clean(page, state, root / 'state.json', days=30, maximum=2, live=True, pause=lambda: None, day=day.isoformat())

    assert state['daily_removals'] == {day.isoformat(): 2}
    assert state['last_live_date'] == day.isoformat()


def test_run_past_deadline_does_not_record_done_date(browser):  # noqa: F811
    import time

    page, base, root = browser
    page.goto(base + '/me/with_replies?fresh=1')
    state = {'handle': 'me'}
    deadline = time.monotonic() + 12
    pauses = []

    def pause():
        pauses.append(1)
        if len(pauses) == 5:
            time.sleep(max(0, deadline - time.monotonic()) + 0.1)

    app.clean(page, state, root / 'state.json', days=30, maximum=200, live=True, pause=pause, deadline=deadline)

    assert len(page.evaluate('window.removed')) == 5
    assert state['last_cleanup']['partial'] is False
    assert state['last_cleanup']['action_error'] is False
    assert state['last_cleanup']['remaining'] == 0
    assert 'last_live_date' not in state


def test_finished_run_checks_the_page_before_it_records_done_date(browser, monkeypatch):  # noqa: F811
    import pytest

    page, base, root = browser
    page.goto(base + '/me/with_replies?fresh=1')
    state = {'handle': 'me'}
    pauses = []
    guard = app.guard

    def checked(page, timing=None):
        if len(pauses) == 5:
            raise app.SessionStopped('Login page')
        guard(page, timing)

    monkeypatch.setattr(app, 'guard', checked)

    with pytest.raises(app.SessionStopped, match='Login page'):
        app.clean(page, state, root / 'state.json', days=30, maximum=200, live=True, pause=lambda: pauses.append(1))

    assert len(page.evaluate('window.removed')) == 5
    assert 'last_live_date' not in state


def test_unverified_undo_timeout_refreshes_repost_date(browser):  # noqa: F811
    import json
    from datetime import UTC, datetime

    from test_tweet_cleanup import audit_statuses

    _page, base, root = browser
    url = f'{base}/other/status/8'
    state = {'handle': 'me', 'cleanup_started': True, 'reposts': {url: '2020-01-01T00:00:00+00:00'}}
    before = datetime.now(UTC)
    execute(browser, live=True, query='?undodrop=timeline', state=state)
    after = datetime.now(UTC)

    assert audit_statuses(browser, '8') == ['pending', 'unverified']
    stored_date = datetime.fromisoformat(state['reposts'][url])
    assert before <= stored_date <= after
    assert state['reposts'][url] == state['last_cleanup']['time']
    assert json.loads((root / 'state.json').read_text())['reposts'][url] == state['reposts'][url]


def test_unverified_undo_playwright_error_refreshes_repost_date(browser, monkeypatch):  # noqa: F811
    from datetime import UTC, datetime

    from test_tweet_cleanup import audit_statuses

    _page, base, _root = browser
    url = f'{base}/other/status/8'
    state = {'handle': 'me', 'cleanup_started': True, 'reposts': {url: '2020-01-01T00:00:00+00:00'}}
    original_guard = app.guard
    raised = False

    def fail_after_click(target_page, timing=None):
        nonlocal raised
        if not raised and '8' in target_page.evaluate('window.removed'):
            raised = True
            raise app.PlaywrightError('page lost after click')
        return original_guard(target_page, timing)

    monkeypatch.setattr(app, 'guard', fail_after_click)
    before = datetime.now(UTC)
    execute(browser, live=True, state=state)
    after = datetime.now(UTC)

    assert audit_statuses(browser, '8') == ['pending', 'unverified']
    stored_date = datetime.fromisoformat(state['reposts'][url])
    assert before <= stored_date <= after
    assert state['reposts'][url] == state['last_cleanup']['time']


def test_repost_after_unverified_undo_is_not_undone_next_run(browser):  # noqa: F811
    page, base, _root = browser
    url = f'{base}/other/status/8'
    state = {'handle': 'me', 'cleanup_started': True, 'reposts': {url: '2020-01-01T00:00:00+00:00'}}

    execute(browser, live=True, query='?undodrop=timeline', state=state)
    _, page = execute(browser, live=True, state=state)

    assert '8' not in page.evaluate('window.removed')


def test_no_click_repost_undo_keeps_old_date(browser, monkeypatch):  # noqa: F811
    from datetime import datetime, timedelta

    page, base, _root = browser
    url = f'{base}/other/status/8'
    old_date = '2020-01-01T00:00:00+00:00'
    state = {'handle': 'me', 'cleanup_started': True, 'reposts': {url: old_date}}
    original_owns = app.owns_confirmation

    def no_repost_confirmation(*args, **kwargs):
        if len(args) > 3 and args[3]:
            return False
        return original_owns(*args, **kwargs)

    monkeypatch.setattr(app, 'owns_confirmation', no_repost_confirmation)
    execute(browser, live=True, state=state)

    from test_tweet_cleanup import audit_statuses

    assert audit_statuses(browser, '8') == ['pending', 'failed']
    assert '8' not in page.evaluate('window.removed')
    stored_date = datetime.fromisoformat(state['reposts'][url])
    run_time = datetime.fromisoformat(state['last_cleanup']['time'])
    assert state['reposts'][url] != state['last_cleanup']['time']
    assert stored_date < run_time - timedelta(days=30)


def test_stop_after_undo_click_restarts_repost_date(browser, monkeypatch):  # noqa: F811
    import json
    from datetime import UTC, datetime

    import pytest

    _page, base, root = browser
    url = f'{base}/other/status/8'
    state = {'handle': 'me', 'cleanup_started': True, 'reposts': {url: '2020-01-01T00:00:00+00:00'}}
    original_guard = app.guard

    def stop_after_click(target_page, timing=None):
        if '8' in target_page.evaluate('window.removed'):
            raise app.SessionStopped('Cleanup tab was used')
        return original_guard(target_page, timing)

    monkeypatch.setattr(app, 'guard', stop_after_click)
    before = datetime.now(UTC)
    with pytest.raises(app.SessionStopped, match='Cleanup tab was used'):
        execute(browser, live=True, state=state)
    after = datetime.now(UTC)

    assert before <= datetime.fromisoformat(state['reposts'][url]) <= after
    assert json.loads((root / 'state.json').read_text())['reposts'][url] == state['reposts'][url]


def test_unverified_delete_stores_no_repost_date(browser):  # noqa: F811
    from test_tweet_cleanup import audit_statuses

    _page, base, _root = browser
    state = {'handle': 'me', 'reposts': {}}

    execute(browser, live=True, limit=1, query='?stuck=1', state=state)

    assert audit_statuses(browser, '1') == ['pending', 'unverified']
    assert f'{base}/me/status/1' not in state['reposts']


def test_retry_tab_marks_scan_partial(browser, caplog):  # noqa: F811
    state = {'handle': 'me'}
    execute(browser, live=True, query='?stalltab=reposts', state=state)

    assert state['last_cleanup']['partial'] is True
    assert 'reposts scan is partial: the tab shows an error' in caplog.text


def test_error_alert_marks_scan_partial(browser, monkeypatch, caplog):  # noqa: F811
    original_walk = app.walk

    def walk_with_alert(page, *args, **kwargs):
        yield from original_walk(page, *args, **kwargs)
        if '/me/reposts' in page.url:
            page.evaluate("document.body.insertAdjacentHTML('beforeend', '<div role=\"alert\">Error</div>')")

    monkeypatch.setattr(app, 'walk', walk_with_alert)
    state = {'handle': 'me'}
    execute(browser, live=True, state=state)

    assert state['last_cleanup']['partial'] is True
    assert 'reposts scan is partial: the tab shows an error' in caplog.text


def test_clean_scan_is_not_partial(browser, caplog):  # noqa: F811
    state = {'handle': 'me'}
    execute(browser, live=True, state=state)

    assert state['last_cleanup']['partial'] is False
    assert 'scan is partial' not in caplog.text


def test_error_on_first_tab_keeps_scan_partial(browser, caplog):  # noqa: F811
    state = {'handle': 'me'}
    execute(browser, live=True, query='?stalltab=posts', state=state)

    assert state['last_cleanup']['partial'] is True
    assert 'posts scan is partial: the tab shows an error' in caplog.text


def test_deadline_tab_logs_one_reason(browser, caplog):  # noqa: F811
    from dataclasses import replace

    page, base, root = browser
    page.goto(base + '/me/with_replies?endless=1')
    state = {'handle': 'me'}
    app.clean(page, state, root / 'state.json', live=True, pause=lambda: None,
              timing=replace(app.Timing(), run_seconds=20, scan_fraction=0.3))

    assert state['last_cleanup']['partial'] is True
    assert 'scan is partial: scan deadline reached' in caplog.text
    assert 'the tab shows an error' not in caplog.text


def test_tab_that_does_not_load_logs_one_reason(browser, caplog):  # noqa: F811
    from dataclasses import replace

    page, base, root = browser
    page.goto(base + '/me?stalltab=reposts&noempty=1')
    state = {'handle': 'me'}
    app.clean(page, state, root / 'state.json', live=True, pause=lambda: None,
              timing=replace(app.Timing(), load_ms=500))

    assert state['last_cleanup']['partial'] is True
    assert 'reposts tab did not load; its scan is partial' in caplog.text
    assert 'the tab shows an error' not in caplog.text


def test_dry_run_lists_every_old_item_past_the_limit(browser, capsys):  # noqa: F811
    full, _ = execute(browser)
    capsys.readouterr()

    results, page = execute(browser, limit=2)

    assert len(full) > 2
    assert [row['url'] for row in results] == [row['url'] for row in full]
    assert f'{len(full)} items' in capsys.readouterr().out
    assert page.evaluate('window.removed') == []


def test_dry_run_writes_one_dry_run_row_per_item(browser):  # noqa: F811
    results, _ = execute(browser, limit=2)

    with (browser[2] / 'logs' / 'removed.csv').open(newline='', encoding='utf-8') as log:
        rows = list(csv.DictReader(log))

    assert [row['status'] for row in rows] == ['dry_run'] * len(results)
    assert [row['url'] for row in rows] == [row['url'] for row in results]


def test_dry_run_lists_items_in_scan_order(browser):  # noqa: F811
    results, _ = execute(browser, limit=2)

    with (browser[2] / 'logs' / 'removed.csv').open(newline='', encoding='utf-8') as log:
        rows = list(csv.DictReader(log))

    assert [(row['action'], app.status_id(row)) for row in results] == [
        ('delete', '1'), ('delete', '7'), ('delete', '2'), ('unrepost', '8'), ('unrepost', '5')]
    assert {row['live'] for row in rows} == {'false'}


def cp1252_stdout(monkeypatch):
    import io
    import sys

    stream = io.TextIOWrapper(io.BytesIO(), encoding='cp1252')
    monkeypatch.setattr(sys, 'stdout', stream)
    return stream


def test_error_with_emoji_prints_on_cp1252_stdout(monkeypatch, tmp_path):
    import sys
    from unittest.mock import Mock

    stream = cp1252_stdout(monkeypatch)
    monkeypatch.setattr(app, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(sys, 'argv', ['tweet_cleanup.py', 'run'])
    launch = Mock(side_effect=app.SessionStopped('Post \U0001F600 failed'))
    monkeypatch.setattr(app, 'launch_browser', launch)

    assert app.main('http://127.0.0.1') == 1
    stream.flush()
    assert b'Post \\U0001f600 failed' in stream.buffer.getvalue()


def test_dry_run_line_with_emoji_prints_on_cp1252_stdout(monkeypatch, tmp_path):
    import sys
    from unittest.mock import MagicMock, Mock

    def print_line(*args, **kwargs):
        print('delete 2024 url Hi \U0001F600')

    stream = cp1252_stdout(monkeypatch)
    monkeypatch.setattr(app, 'DATA_DIR', tmp_path)
    app.save_state(tmp_path / 'state.json', {'handle': 'me'})
    monkeypatch.setattr(sys, 'argv', ['tweet_cleanup.py', 'run'])
    run = MagicMock(page=Mock(), deadline=None)
    monkeypatch.setattr(app, 'launch_browser', Mock(return_value=run))
    monkeypatch.setattr(app, 'clean', print_line)

    assert app.main('http://127.0.0.1') == 0
    stream.flush()
    assert b'delete 2024 url Hi \\U0001f600' in stream.buffer.getvalue()


def test_output_setup_skips_missing_and_plain_streams(monkeypatch, tmp_path):
    import io
    import sys
    from unittest.mock import Mock

    monkeypatch.setattr(sys, 'stdout', None)
    monkeypatch.setattr(sys, 'stderr', io.StringIO())
    monkeypatch.setattr(app, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(sys, 'argv', ['tweet_cleanup.py', 'run'])
    launch = Mock(side_effect=app.ProfileOpen('The Tweet cleanup browser window is already open; close it and try again'))
    monkeypatch.setattr(app, 'launch_browser', launch)

    assert app.main('http://127.0.0.1') == 3


def test_error_text_is_unchanged_on_utf8_stdout(monkeypatch, tmp_path):
    import io
    import sys
    from unittest.mock import Mock

    stream = io.TextIOWrapper(io.BytesIO(), encoding='utf-8')
    monkeypatch.setattr(sys, 'stdout', stream)
    monkeypatch.setattr(app, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(sys, 'argv', ['tweet_cleanup.py', 'run'])
    launch = Mock(side_effect=app.SessionStopped('Post café \U0001F600 failed'))
    monkeypatch.setattr(app, 'launch_browser', launch)

    assert app.main('http://127.0.0.1') == 1
    stream.flush()
    assert 'Post café \U0001F600 failed\n'.encode('utf-8') in stream.buffer.getvalue()


def half_emoji_in_post_text(monkeypatch, status):
    walk = app.walk

    # The scan reads half of an emoji pair, as page text can hold one.
    def read(page, timing, observe=None, deadline=float('inf')):
        for article, data in walk(page, timing, observe, deadline):
            if app.status_id(data) == status:
                data = {**data, 'text': data['text'] + ' \ud83d'}
            yield article, data

    monkeypatch.setattr(app, 'walk', read)


def audit_texts(browser, status):  # noqa: F811
    with (browser[2] / 'logs' / 'removed.csv').open(newline='', encoding='utf-8') as log:
        return [row['text'] for row in csv.DictReader(log) if app.status_id(row) == status]


def test_dry_run_lists_post_with_half_emoji(browser, monkeypatch):  # noqa: F811
    half_emoji_in_post_text(monkeypatch, '1')

    results, _ = execute(browser)

    assert '1' in {app.status_id(row) for row in results}
    assert [text.endswith(' \\ud83d') for text in audit_texts(browser, '1')] == [True]


def test_live_run_removes_post_with_half_emoji(browser, monkeypatch):  # noqa: F811
    half_emoji_in_post_text(monkeypatch, '1')

    _, page = execute(browser, live=True)

    assert '1' in page.evaluate('window.removed')
    assert [text.endswith(' \\ud83d') for text in audit_texts(browser, '1')] == [True, True]
