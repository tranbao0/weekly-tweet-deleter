import csv
import json
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import pytest

import tweet_cleanup as app

UTC = timezone.utc  # noqa: UP017


@pytest.fixture
def browser(tmp_path, monkeypatch, shared_browser, fixture_server):
    original = app.Timing
    monkeypatch.setattr(app, "Timing", lambda: original(settle_ms=20, idle_passes=5, action_ms=150, verify_seconds=0.15, poll_ms=10))
    server = fixture_server
    context = shared_browser.new_context()
    page = context.new_page()
    page.set_viewport_size({'width': 1280, 'height': 1000})
    yield page, f'http://127.0.0.1:{server.server_port}', tmp_path
    context.close()


def execute(browser, live=False, limit=200, query='', state=None, route='/me'):
    page, base, root = browser
    page.goto(base + route + (query or '?fresh=1'))
    state = state if state is not None else {'handle': 'me', 'reposts': {}}
    result = app.clean(page, state, root / 'state.json', days=30, maximum=limit, live=live, pause=lambda: None)
    return result, page


def test_dry(browser):
    result, _ = execute(browser, route='/me/with_replies')
    assert {r['url'].split('/')[-1] for r in result} == {'1', '2', '5', '7', '8'}


def test_live_and_later_repost(browser):
    _result, page = execute(browser, live=True, route='/me/with_replies')
    assert set(page.evaluate('window.removed')) == {'1', '2', '5', '7', '8'}


def test_later_repost(browser):
    state = {'handle': 'me', 'cleanup_started': True, 'reposts': {browser[1] + '/other/status/5': '2020-01-01T00:00:00+00:00', browser[1] + '/other/status/8': datetime.now(UTC).isoformat()}}
    _result, page = execute(browser, live=True, state=state, route='/me/with_replies')
    assert set(page.evaluate('window.removed')) == {'1', '2', '5', '7'}


def test_login(browser):
    with pytest.raises(app.SessionStopped):
        execute(browser, live=True, query='?login=1')


def test_retry(browser):
    _result, page = execute(browser, live=True, query='?fail=5', route='/me/with_replies')
    assert set(page.evaluate('window.removed')) == {'2', '5', '7', '8'}


def test_max(browser):
    _result, page = execute(browser, live=True, limit=2)
    assert len(page.evaluate('window.removed')) == 2


def test_quote_binding(browser):
    _, page = execute(browser, live=True, query='?quotes=1', route='/me/with_replies')
    assert set(page.evaluate('window.removed')) == {'1', '2', '5', '7', '8'}


def test_stuck_confirmation(browser):
    result, page = execute(browser, live=True, query='?stuck=1')
    assert page.evaluate('window.attempts["1"]') == 1
    assert any(r['action'] == 'delete' for r in result)


def test_post_words(browser):
    _result, page = execute(browser, live=True, query='?words=1')
    assert '1' in page.evaluate('window.removed')


def test_language(browser):
    with pytest.raises(app.SessionStopped, match='English'):
        execute(browser, live=True, query='?spanish=1')
    assert browser[0].evaluate('window.removed') == []


def test_slow_render(browser):
    _result, page = execute(browser, live=True, query='?slow=1')
    assert '1' in page.evaluate('window.removed')


def test_login_state_first_cleanup(browser):
    state = {'handle': 'me', 'reposts': {}}
    browser[0].goto(browser[1] + '/index.html')
    app.save_setup(browser[0], state, browser[2] / 'state.json')
    _result, page = execute(browser, live=True, state=state, route='/me/with_replies')
    assert '5' in page.evaluate('window.removed')


def test_repost_memory(browser):
    state = {'handle': 'me', 'reposts': {}}
    result, page = execute(browser, live=True, state=state, route='/me/reposts')
    assert {r['url'].split('/')[-1] for r in result} == {'1', '2', '5', '7', '8'}
    assert state['reposts'] == {}
    removed = page.evaluate('window.removed')
    result, page = execute(browser, live=True, state=state, route='/me/reposts')
    assert not result
    assert page.evaluate('window.removed') == removed
    page.goto(browser[1] + '/me/reposts')
    assert page.locator('[data-testid=unretweet]').count() == 0



def test_delayed_batch(browser):
    _result, page = execute(browser, live=True, query='?batch=1', route='/me/with_replies')
    assert set(page.evaluate('window.removed')) == {'1', '2', '5', '7', '8'}


def test_virtual_timeline(browser):
    page, base, root = browser
    page.goto(base + '/virtual.html')
    result = app.clean(page, {'handle': 'me'}, root / 'state.json', pause=lambda: None)
    assert {r['url'].split('/')[-1] for r in result} == {str(i) for i in range(100, 140)}


def test_first_seen_age():
    now = datetime.now(UTC)
    item = {'url': 'https://x.com/other/status/99', 'author': ['/other'],
            'date': (now - timedelta(days=10)).isoformat(), 'context': 'me reposted', 'context_links': ['/me']}
    state = {'handle': 'me', 'reposts': {}}
    app.reconcile({('repost', '99'): item}, state, now, 30)
    assert not app.eligible(item, state, now + timedelta(days=20), 30)
    assert app.eligible(item, state, now + timedelta(days=31), 30)
    state['reposts'].clear()
    state['cleanup_started'] = True
    app.reconcile({('repost', '99'): item}, state, now + timedelta(days=32), 30)
    assert not app.eligible(item, state, now + timedelta(days=33), 30)



def test_first_live_run_keeps_repost_above_young_original(browser):
    page, base, _ = browser
    now = datetime.now(UTC).replace(microsecond=0)
    page.clock.set_fixed_time(now)
    state = {'handle': 'me', 'reposts': {}}

    execute(browser, live=True, query='?youngrepost=1', state=state, route='/me/reposts')

    assert '8' not in page.evaluate('window.removed')
    assert app.parsed(state['reposts'][base + '/other/status/8']) >= now - timedelta(days=20)


def repost_item(status, date):
    return {'url': f'https://x.com/other/status/{status}', 'context': 'You reposted', 'date': date}


def test_partial_reposts_scan_uses_newest_original_below():
    now = datetime.now(UTC)
    older = repost_item('1', (now - timedelta(days=100)).isoformat())
    younger = repost_item('2', (now - timedelta(days=10)).isoformat())
    inventory = {('repost', '1'): older, ('repost', '2'): younger}
    state = {'handle': 'me', 'reposts': {}}

    app.reconcile(inventory, state, now, 30, False, [older, younger])

    assert state['reposts'][older['url']] == younger['date']


def test_partial_reposts_scan_keeps_later_and_unseen_memory():
    now = datetime.now(UTC)
    old = (now - timedelta(days=100)).isoformat()
    recent = (now - timedelta(days=5)).isoformat()
    seen_later = repost_item('1', old)
    seen_earlier = repost_item('2', old)
    younger = repost_item('3', (now - timedelta(days=10)).isoformat())
    unseen = 'https://x.com/other/status/4'
    memory = {seen_later['url']: recent, seen_earlier['url']: old, unseen: old}
    state = {'handle': 'me', 'cleanup_started': True, 'reposts': memory}
    inventory = {('repost', '1'): seen_later, ('repost', '2'): seen_earlier, ('repost', '3'): younger}

    app.reconcile(inventory, state, now, 30, False, [seen_later, seen_earlier, younger])

    assert state['reposts'][seen_later['url']] == recent
    assert state['reposts'][unseen] == old
    assert state['reposts'][seen_earlier['url']] == younger['date']


def test_unparsable_original_does_not_break_repost_bound():
    now = datetime.now(UTC)
    older = repost_item('1', (now - timedelta(days=100)).isoformat())
    invalid = repost_item('2', 'invalid')
    younger = repost_item('3', (now - timedelta(days=10)).isoformat())
    inventory = {('repost', '1'): older, ('repost', '2'): invalid, ('repost', '3'): younger}
    state = {'handle': 'me', 'reposts': {}}

    app.reconcile(inventory, state, now, 30, True, [older, invalid, younger])

    assert state['reposts'][older['url']] == younger['date']


def test_max_prunes_memory(browser):
    state = {'handle': 'me', 'reposts': {'absent': datetime.now(UTC).isoformat()}}
    execute(browser, live=True, limit=1, state=state)
    assert 'absent' not in state['reposts']


def test_first_author_only():
    item = {'url': 'https://x.com/other/status/99', 'author': ['/other', '/me'],
            'date': '2020-01-01T00:00:00Z', 'context': ''}
    assert not app.eligible(item, {'handle': 'me', 'reposts': {}}, datetime.now(UTC), 30)


def test_virtual_live(browser):
    page, base, root = browser
    page.goto(base + '/virtual.html')
    result = app.clean(page, {'handle': 'me'}, root / 'state.json', live=True, pause=lambda: None)
    assert len(result) == 40
    assert set(page.evaluate('window.removed')) == {str(i) for i in range(100, 140)}


def test_unlabelled_pin_copy(browser):
    _result, page = execute(browser, live=True, query='?duplicatepin=1')
    assert '4' not in page.evaluate('window.removed')


def test_self_repost_keeps_recent_memory(browser):
    state = {'handle': 'me', 'cleanup_started': True,
             'reposts': {browser[1] + '/me/status/1': datetime.now(UTC).isoformat()}}
    result, _ = execute(browser, live=True, query='?selfrepost=1', state=state, route='/me/reposts')
    assert all(row['action'] != 'unrepost' for row in result)
    assert browser[1] + '/me/status/1' in state['reposts']


def test_pin_during_confirmation(browser):
    _result, page = execute(browser, live=True, query='?latepin=1')
    assert '1' not in page.evaluate('window.removed')


def test_late_pin_leaves_no_layer(browser, monkeypatch):
    layers = app.LAYERS
    act = app.act
    remaining = []

    def record_layers(page, article, target, *args):
        result = act(page, article, target, *args)
        if target == ('post', '1'):
            remaining.append(page.locator(layers).count())
        return result

    monkeypatch.setattr(app, 'act', record_layers)
    _, page = execute(browser, live=True, query='?latepin=1')
    assert remaining == [0]
    removed = page.evaluate('window.removed')
    assert '1' not in removed
    assert '7' in removed
    assert page.locator(layers).count() == 0


def stage_stale_layer(monkeypatch, layer, *, stuck=False):
    act = app.act
    opened = False

    def with_stale_layer(page, article, target, *args):
        nonlocal opened
        stale = page.locator('article').filter(has=page.locator('a[href="/me/status/6"]')).locator(app.CARET)
        if not opened:
            assert target == ('post', '1')
            opened = True
            stale.click()
            if layer == 'sheet':
                page.get_by_role('menuitem', name='Delete', exact=True).click()
            if stuck:
                monkeypatch.setattr(page.keyboard, 'press', lambda key: None)
        elif not stuck and target == ('post', '7') and stale.count():
            # A layer can also open after the initial Escape check.
            article.query_selector(app.CARET).evaluate("""(caret, sheet) => {
                const open = caret.onclick;
                caret.onclick = () => {
                    document.querySelector('article:has(a[href="/me/status/6"]) [data-testid=caret]').click();
                    if (sheet)
                        [...document.querySelectorAll('#layers [role=menuitem]')]
                            .find(e => e.textContent === 'Delete').click();
                    open();
                };
            }""", layer == 'sheet')
        return act(page, article, target, *args)

    monkeypatch.setattr(app, 'act', with_stale_layer)


@pytest.mark.parametrize('layer', ['menu', 'sheet'])
def test_stale_post_layer(browser, monkeypatch, layer):
    stage_stale_layer(monkeypatch, layer)
    _, page = execute(browser, live=True)
    removed = page.evaluate('window.removed')
    assert '6' not in removed
    assert '1' in removed
    assert '7' not in removed
    assert page.evaluate('window.attempts["7"]') == 1
    assert app.count_layers(page) == 0


def test_stale_sheet_that_does_not_close(browser, monkeypatch):
    stage_stale_layer(monkeypatch, 'sheet', stuck=True)
    state = {'handle': 'me', 'reposts': {}}
    _, page = execute(browser, live=True, state=state)
    assert not {'1', '6', '7'}.intersection(page.evaluate('window.removed'))
    assert state['last_cleanup']['action_error']


def test_second_undo_menu_refuses(browser, monkeypatch):
    act = app.act
    during = []

    def with_second_menu(page, article, target, *args):
        if target == ('repost', '8') and not during:
            # Clicking the undo button of repost 8 also opens the undo menu of repost 5.
            article.query_selector(app.UNREPOST).evaluate("""(undo) => {
                const open = undo.onclick;
                undo.onclick = () => {
                    document.querySelector('article:has(a[href="/other/status/5"]) [data-testid=unretweet]').click();
                    open();
                };
            }""")
            before = page.evaluate('window.removed.length')
            result = act(page, article, target, *args)
            during.append(page.evaluate('window.removed')[before:])
            return result
        return act(page, article, target, *args)

    monkeypatch.setattr(app, 'act', with_second_menu)
    _, page = execute(browser, live=True)
    assert during == [[]]
    removed = page.evaluate('window.removed')
    assert '8' not in removed
    assert '5' in removed
    assert app.count_layers(page) == 0


def rewire_action(monkeypatch, target, control, script):
    act = app.act

    def rewired(page, article, current, *args):
        if current == target:
            article.query_selector(control).evaluate(script)
        return act(page, article, current, *args)

    monkeypatch.setattr(app, 'act', rewired)


def test_stale_sheet_before_delete_click(browser, monkeypatch):
    # The caret of post 7 also opens the sheet of post 6, and the own sheet of post 7 comes late.
    rewire_action(monkeypatch, ('post', '7'), app.CARET, """(caret) => {
        const open = caret.onclick;
        caret.onclick = () => {
            document.querySelector('article:has(a[href="/me/status/6"]) [data-testid=caret]').click();
            [...document.querySelectorAll('#layers [role=menuitem]')].find(e => e.textContent === 'Delete').click();
            open();
            const item = [...document.querySelector('#layers').lastElementChild.querySelectorAll('[role=menuitem]')]
                .find(e => e.textContent === 'Delete');
            const remove = item.onclick;
            item.onclick = () => setTimeout(remove, 300);
        };
    }""")
    _, page = execute(browser, live=True)
    removed = page.evaluate('window.removed')
    assert '6' not in removed
    assert '7' not in removed
    assert page.evaluate('window.attempts["7"]') == 1


def test_own_menu_open_after_delete_click(browser, monkeypatch):
    # The Delete item of post 7 leaves its menu open and opens the sheet of post 6.
    rewire_action(monkeypatch, ('post', '7'), app.CARET, """(caret) => {
        const open = caret.onclick;
        caret.onclick = () => {
            open();
            const item = [...document.querySelector('#layers').lastElementChild.querySelectorAll('[role=menuitem]')]
                .find(e => e.textContent === 'Delete');
            item.onclick = () => {
                document.querySelector('article:has(a[href="/me/status/6"]) [data-testid=caret]').click();
                [...document.querySelector('#layers').lastElementChild.querySelectorAll('[role=menuitem]')]
                    .find(e => e.textContent === 'Delete').click();
            };
        };
    }""")
    _, page = execute(browser, live=True)
    removed = page.evaluate('window.removed')
    assert '6' not in removed
    assert '7' not in removed


def test_undo_confirmation_outside_menu(browser, monkeypatch):
    rewire_action(monkeypatch, ('repost', '8'), app.UNREPOST, """(undo) => {
        const open = undo.onclick;
        undo.onclick = () => {
            open();
            const layers = document.querySelector('#layers');
            layers.append(layers.querySelector('[data-testid=unretweetConfirm]'));
        };
    }""")
    _, page = execute(browser, live=True)
    removed = page.evaluate('window.removed')
    assert '8' not in removed
    assert '5' in removed
    assert app.count_layers(page) == 0


def test_challenge_layer_gets_no_escape(browser, monkeypatch):
    act = app.act
    presses = []

    def with_challenge(page, article, target, *args):
        if target == ('post', '7'):
            page.evaluate("""document.querySelector('#layers').insertAdjacentHTML(
                'beforeend', '<div role="dialog" data-testid="challengeDialog"></div>')""")
            press = page.keyboard.press
            monkeypatch.setattr(page.keyboard, 'press', lambda key: (presses.append(key), press(key)))
        return act(page, article, target, *args)

    monkeypatch.setattr(app, 'act', with_challenge)
    with pytest.raises(app.SessionStopped):
        execute(browser, live=True)
    assert presses == []
    assert '7' not in browser[0].evaluate('window.removed')


def test_close_error_keeps_action_result(browser, monkeypatch):
    close = app.close_layers
    raised = []

    def failing_close(page, timing):
        if '1' in page.evaluate('window.removed') and not raised:
            raised.append(True)
            raise app.PlaywrightError('Execution context was destroyed')
        return close(page, timing)

    monkeypatch.setattr(app, 'close_layers', failing_close)
    result, page = execute(browser, live=True)
    assert raised
    assert {'1', '7'} <= set(page.evaluate('window.removed'))
    assert any(r['url'].endswith('/status/1') for r in result)


def test_caret_menu_for_other_post(browser):
    _, page = execute(browser, live=True, query='?othermenu=1')
    assert '1' not in page.evaluate('window.removed')


def test_verify_error_counts_sent_click(browser, monkeypatch):
    guard = app.guard

    def verify(page, timing=None):
        guard(page, timing)
        if page.evaluate('window.removed.length'):
            raise app.PlaywrightError('Verification failed')

    monkeypatch.setattr(app, 'guard', verify)
    result, page = execute(browser, live=True, limit=1)
    assert len(result) == 1
    assert page.evaluate('window.removed') == ['1']


def test_pin_copy_before_label(browser):
    _, page = execute(browser, live=True, query='?pinfirst=1')
    assert '4' not in page.evaluate('window.removed')


def test_saved_pin(browser):
    _, page = execute(browser, live=True, query='?nopin=1', state={'handle': 'me', 'pinned': '4'})
    assert '4' not in page.evaluate('window.removed')


def test_replacement_pin(browser):
    state = {'handle': 'me', 'pinned': '1'}
    _, page = execute(browser, live=True, state=state)
    assert state['pinned'] == '4'
    assert '1' in page.evaluate('window.removed')


def test_dispatch_error(browser, monkeypatch):
    from playwright.sync_api import ElementHandle
    evaluate = ElementHandle.evaluate

    def dispatch(self, expression, *args, **kwargs):
        destructive = evaluate(self, "e => e.dataset.testid === 'confirmationSheetConfirm'")
        result = evaluate(self, expression, *args, **kwargs)
        if destructive and expression == app.CONFIRM_CLICK:
            raise app.PlaywrightError('Dispatch result lost')
        return result

    monkeypatch.setattr(ElementHandle, 'evaluate', dispatch)
    _, page = execute(browser, live=True, limit=1)
    assert page.evaluate('window.removed') == ['1']


def test_nested_challenge(browser):
    with pytest.raises(app.SessionStopped):
        execute(browser, live=True, query='?nested=1')
    assert browser[0].evaluate('window.removed') == []


def test_stall_memory(browser):
    state = {'handle': 'me', 'reposts': {'unseen': '2020-01-01T00:00:00Z'}}
    execute(browser, live=True, query='?stall=1', state=state)
    assert 'unseen' in state['reposts']
    assert state.get('cleanup_started')


def test_dry_first_live_age(browser, monkeypatch):
    state = {'handle': 'me', 'reposts': {}}
    start = datetime.now(UTC)
    execute(browser, query='?youngrepost=1', state=state, route='/me/with_replies')
    assert state.get('reposts', {}) == {}

    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return start + timedelta(days=15)

    monkeypatch.setattr(app, 'datetime', Later)
    _, page = execute(browser, live=True, query='?youngrepost=1', state=state, route='/me/with_replies')
    assert '5' in page.evaluate('window.removed')


def test_wrong_account(browser):
    with pytest.raises(app.SessionStopped):
        execute(browser, live=True, query='?wrongaccount=1')
    assert browser[0].evaluate('window.removed') == []


def test_setup_short_window(browser):
    page, base, root = browser
    page.set_viewport_size({'width': 982, 'height': 657})
    page.goto(base + '/home')
    state = {}
    app.save_setup(page, state, root / 'state.json')
    assert state['handle'] == 'me'


def test_dry_leaves_posts(browser):
    _, page = execute(browser)
    assert page.evaluate('window.removed') == []


def test_quote_results_match_actions(browser):
    result, page = execute(browser, live=True, query='?quotes=1')
    assert {r['url'].split('/')[-1] for r in result} == set(page.evaluate('window.removed'))


def test_batch_spinner(browser):
    result, page = execute(browser, live=True, query='?batch=1')
    assert {r['url'].split('/')[-1] for r in result} == {'1', '2', '5', '7', '8'}
    assert set(page.evaluate('window.removed')) == {'1', '2', '5', '7', '8'}


def test_retry_limit(browser):
    _, page = execute(browser, live=True, query='?fail=5')
    assert page.evaluate('window.attempts["1"]') == 3
    assert '1' not in page.evaluate('window.removed')


def test_dry_leaves_repost_memory(browser):
    state = {'handle': 'me', 'reposts': {}}
    execute(browser, query='?youngrepost=1', state=state, route='/me/reposts')
    assert state['reposts'] == {}


def test_dry_keeps_cleanup_unset(browser):
    state = {'handle': 'me'}
    execute(browser, state=state)
    assert not state.get('cleanup_started')


LOGIN_MESSAGE = 'Log in to X in the browser'
UNCLEAR_MESSAGE = 'X shows an unclear account; run stopped'


def account_timing(load_ms=200, poll_ms=20):
    return app.Timing().__class__(load_ms=load_ms, poll_ms=poll_ms, settle_ms=20)


def test_account_scope(browser):
    page, base, _ = browser
    page.set_viewport_size({'width': 982, 'height': 657})
    page.goto(base + '/me/with_replies?noswitcher=1')
    assert page.locator('[data-testid^="UserAvatar-Container-"]').count() > 0
    assert page.locator(app.PROFILE).count() == 0
    with pytest.raises(app.SessionStopped, match=LOGIN_MESSAGE):
        app.account_handle(page, account_timing())


def test_account_profile_only(browser):
    page, base, _ = browser
    page.goto(base + '/me?noswitcher=1')
    assert app.account_handle(page, account_timing()) == 'me'


@pytest.mark.parametrize('operation', ['setup', 'clean'])
def test_account_disagreement(browser, operation):
    page, base, root = browser
    page.goto(base + '/me/with_replies?switcherhandle=other')
    state = {'handle': 'me', 'reposts': {}}
    path = root / 'state.json'
    app.save_state(path, state)
    before = path.read_bytes()
    with pytest.raises(app.SessionStopped, match=UNCLEAR_MESSAGE):
        if operation == 'setup':
            app.save_setup(page, state, path)
        else:
            app.clean(page, state, path, live=True, pause=lambda: None)
    assert path.read_bytes() == before
    assert state == {'handle': 'me', 'reposts': {}}
    assert page.evaluate('window.removed') == []


@pytest.mark.parametrize('query', ['profilehref=%2Fme%2Fextra&noswitcher=1', 'switcherhandle=me%40x&noprofile=1',
                                   'switcherhandle=%D9%A1&noprofile=1', 'profilehref=%2Fme%3Fextra&noswitcher=1',
                                   'switcherhandle=&noprofile=1', 'profilehref=https%3A%2F%2Fx.com%2Fme&noswitcher=1',
                                   'switcherhandle=abcdefghijklmnop&noprofile=1'])
def test_account_malformed(browser, query):
    page, base, _ = browser
    page.goto(base + '/me?' + query)
    with pytest.raises(app.SessionStopped, match=UNCLEAR_MESSAGE):
        app.account_handle(page, account_timing())


def test_account_attempt_cap(browser, monkeypatch):
    page, base, _ = browser
    page.goto(base + '/home?noswitcher=1&noprofile=1')
    read = app.read_account_sources
    attempts = []
    def counted(page):
        attempts.append(1)
        return read(page)
    monkeypatch.setattr(app, 'read_account_sources', counted)
    with pytest.raises(app.SessionStopped, match=LOGIN_MESSAGE):
        app.account_handle(page, account_timing())
    assert len(attempts) == 10


def test_account_navigation_retry(browser, monkeypatch):
    page, base, _ = browser
    page.goto(base + '/home')
    read = app.read_account_sources
    attempts = []
    def interrupted(page):
        attempts.append(1)
        if len(attempts) == 1:
            raise app.PlaywrightError('Navigation interrupts the read')
        return read(page)
    monkeypatch.setattr(app, 'read_account_sources', interrupted)
    assert app.account_handle(page, account_timing()) == 'me'
    assert len(attempts) == 2


def test_account_late_render(browser):
    page, base, _ = browser
    page.goto(base + '/home?lateaccount=1')
    assert page.locator(app.PROFILE).count() == 0
    assert app.account_handle(page, account_timing(load_ms=800)) == 'me'


def test_account_return_change(browser):
    page, base, root = browser
    page.goto(base + '/me/with_replies?accountreturn=1')
    state = {'handle': 'me'}
    path = root / 'state.json'
    app.save_state(path, state)
    before = path.read_bytes()
    with pytest.raises(app.SessionStopped, match='Authenticated account does not match saved handle'):
        app.clean(page, state, path, live=True, pause=lambda: None)
    assert page.evaluate('window.removed') == []
    assert path.read_bytes() == before


def test_account_errors_spend_attempts(browser, monkeypatch):
    page, base, _ = browser
    page.goto(base + '/home')
    attempts = []
    def failing(page):
        attempts.append(1)
        raise app.PlaywrightError('Navigation interrupts the read')
    monkeypatch.setattr(app, 'read_account_sources', failing)
    with pytest.raises(app.SessionStopped, match=LOGIN_MESSAGE) as stopped:
        app.account_handle(page, account_timing())
    assert len(attempts) == 10
    assert isinstance(stopped.value.__cause__, app.PlaywrightError)


def test_setup_return_change(browser):
    page, base, root = browser
    page.goto(base + '/home?accountreturn=1')
    path = root / 'state.json'
    app.save_state(path, {'reposts': {}})
    before = path.read_bytes()
    state = {'reposts': {}}
    with pytest.raises(app.SessionStopped, match='Authenticated account does not match saved handle'):
        app.save_setup(page, state, path)
    assert state == {'reposts': {}}
    assert path.read_bytes() == before


@pytest.mark.parametrize('failure', ['language', 'scan'])
def test_failed_first_run_keeps_first_run_rule(browser, monkeypatch, failure):
    _, _, root = browser
    path = root / 'state.json'
    app.save_state(path, {'handle': 'me', 'reposts': {}})
    before = path.read_bytes()
    walk = app.walk
    if failure == 'scan':
        def failed(*args, **kwargs):
            raise RuntimeError('Scan failed')
        monkeypatch.setattr(app, 'walk', failed)
    with pytest.raises((app.SessionStopped, RuntimeError)):
        execute(browser, live=True, query='?spanish=1' if failure == 'language' else '',
                state=json.loads(path.read_text()), route='/me/reposts')
    assert path.read_bytes() == before
    monkeypatch.setattr(app, 'walk', walk)
    _, page = execute(browser, live=True, state=json.loads(path.read_text()), route='/me/reposts')
    assert {'5', '8'} <= set(page.evaluate('window.removed'))


class FramePage:
    def __init__(self, page, frames):
        self.page = page
        self.frame_source = frames

    @property
    def frames(self):
        return self.frame_source()

    def __getattr__(self, name):
        return getattr(self.page, name)


@pytest.mark.parametrize('message', [None, 'Document is gone'])
def test_guard_detached_frame(browser, message):
    page, base, _ = browser
    page.goto(base + '/me')
    page.evaluate("document.body.appendChild(document.createElement('iframe'))")
    frame = page.frames[-1]
    page.locator('iframe').evaluate('(e) => e.remove()')
    assert frame.is_detached()
    if message:
        from unittest.mock import Mock

        proxy = Mock(url=frame.url, is_detached=frame.is_detached)
        proxy.locator.return_value.count.side_effect = app.PlaywrightError(message)
        frame = proxy
    app.guard(FramePage(page, lambda: [page.main_frame, frame]))


def test_guard_frame_churn(browser):
    page, base, root = browser
    page.goto(base + '/me?framechurn=1')
    for _ in range(100):
        app.guard(page)
    result = app.clean(page, {'handle': 'me'}, root / 'state.json', live=False,
                       pause=lambda: None)
    assert result


@pytest.mark.parametrize('errors', [1, 4])
def test_guard_pass_cap(shared_browser, monkeypatch, errors):
    page = shared_browser.new_page()
    page.set_content('<html lang="en"></html>')
    timing = app.Timing(guard_passes=4, guard_wait_ms=10)
    passes = []
    error = app.PlaywrightError('Execution context was destroyed')
    original = app.guard_pass

    def check(page):
        passes.append(None)
        assert len(passes) <= 4, 'Pass cap exceeded'
        if len(passes) <= errors:
            raise error
        return original(page)

    monkeypatch.setattr(app, 'guard_pass', check)
    try:
        if errors == 4:
            with pytest.raises(app.SessionStopped, match='^The page did not settle; run stopped$') as caught:
                app.guard(page, timing)
            assert caught.value.__cause__ is error
        else:
            app.guard(page, timing)
        assert len(passes) == (4 if errors == 4 else 2)
    finally:
        page.close()


@pytest.mark.parametrize('finding', ['challenge', 'login', 'language'])
def test_guard_finding_wins(shared_browser, fixture_server, finding):
    from unittest.mock import Mock

    page = shared_browser.new_page()
    page.set_content('<html lang="en"><iframe></iframe></html>')
    attached = page.frames[-1]
    broken = Mock(url='about:blank')
    broken.is_detached.return_value = False
    broken.locator.return_value.count.side_effect = app.PlaywrightError('Frame was detached')
    if finding == 'challenge':
        attached.locator('body').evaluate('(e) => e.innerHTML = `<input type="password">`')
    elif finding == 'login':
        attached.goto(f'http://127.0.0.1:{fixture_server.server_port}/login')
    else:
        page.evaluate("document.documentElement.lang = 'fr'")
    passes = []

    def frames():
        passes.append(None)
        assert len(passes) == 1, 'A finding must stop the first pass'
        return [broken, attached]

    message = 'English page language required' if finding == 'language' else 'Login or challenge detected'
    try:
        with pytest.raises(app.SessionStopped, match=message):
            app.guard(FramePage(page, frames))
    finally:
        page.close()


def test_guard_replacement_challenge(browser):
    from unittest.mock import Mock

    page, base, _ = browser
    page.goto(base + '/me')
    page.evaluate("document.body.appendChild(document.createElement('iframe'))")
    detached = page.frames[-1]
    page.locator('iframe').evaluate('(e) => e.remove()')
    broken = Mock(url='about:blank')
    broken.is_detached.return_value = False
    broken.locator.return_value.count.side_effect = app.PlaywrightError('Navigation interrupted')
    passes = []

    def frames():
        passes.append(None)
        if len(passes) == 1:
            page.evaluate("document.body.appendChild(document.createElement('iframe'))")
            page.frames[-1].locator('body').evaluate('(e) => e.innerHTML = `<input type="password">`')
            return [detached, broken]
        return page.frames

    with pytest.raises(app.SessionStopped, match='Login or challenge detected'):
        app.guard(FramePage(page, frames))
    assert len(passes) == 2


def test_guard_language_read_retry(browser, monkeypatch):
    from playwright.sync_api import Page

    page, base, _ = browser
    page.goto(base + '/me')
    evaluate = Page.evaluate
    calls = []

    def read(self, expression, *args, **kwargs):
        if expression == 'document.documentElement.lang':
            calls.append(None)
            if len(calls) == 1:
                raise app.PlaywrightError('Execution context was destroyed')
        return evaluate(self, expression, *args, **kwargs)

    monkeypatch.setattr(Page, 'evaluate', read)
    app.guard(page)
    assert len(calls) == 2


def test_guard_missing_document(shared_browser):
    page = shared_browser.new_page()
    page.set_content('<html lang="en"></html>')
    page.evaluate('document.documentElement.remove()')
    start = app.time.monotonic()
    try:
        with pytest.raises(app.SessionStopped, match='^The page did not settle; run stopped$') as caught:
            app.guard(page, app.Timing(guard_passes=2, guard_wait_ms=1))
        assert isinstance(caught.value.__cause__, app.PlaywrightError)
        assert app.time.monotonic() - start < 5
    finally:
        page.close()


@pytest.mark.parametrize('change', ['redirect', 'login frame'])
def test_guard_sees_changes_during_wait(browser, monkeypatch, change):
    page, base, _ = browser
    page.goto(base + '/home')
    original = app.guard_pass
    passes = []

    def interrupted(page):
        passes.append(None)
        if len(passes) == 1:
            if change == 'redirect':
                page.evaluate("setTimeout(() => history.pushState({}, '', '/i/flow/login'), 20)")
            else:
                page.evaluate("""setTimeout(() => {
                    const frame = document.createElement('iframe');
                    frame.srcdoc = '<input type="password">';
                    document.body.append(frame);
                }, 20)""")
            raise app.PlaywrightError('Execution context was destroyed')
        return original(page)

    monkeypatch.setattr(app, 'guard_pass', interrupted)
    with pytest.raises(app.SessionStopped, match='Login or challenge detected'):
        app.guard(page, replace(app.Timing(), guard_passes=3, guard_wait_ms=300))
    assert len(passes) == 2


def test_guard_runs_one_pass_at_zero_passes(browser):
    page, base, _ = browser
    page.goto(base + '/me?spanish=1')
    with pytest.raises(app.SessionStopped, match='English page language required'):
        app.guard(page, replace(app.Timing(), guard_passes=0))


def test_flags_survive_tab_moves(browser):
    page, base, _ = browser
    for route in ['/me?spanish=1', '/me/reposts', '/me/with_replies']:
        page.goto(base + route)
        assert page.evaluate('document.documentElement.lang') == 'es'


def test_query_replaces_flags(browser):
    page, base, _ = browser
    page.goto(base + '/me?spanish=1')
    for route in ['/me?fresh=1', '/me/with_replies']:
        page.goto(base + route)
        assert page.evaluate('document.documentElement.lang') == 'en'
    with pytest.raises(app.SessionStopped, match='English'):
        execute(browser, query='?spanish=1')
    result, _ = execute(browser)
    assert result


def test_virtual_mode_survives_tab_moves(browser):
    page, base, _ = browser
    page.goto(base + '/virtual.html')
    page.goto(base + '/me')
    ids = page.locator('article').evaluate_all("es => es.map(e => e.querySelector('[data-testid=\"User-Name\"] a time').closest('a').getAttribute('href').split('/').pop())")
    assert '100' in ids
    assert '1' not in ids
    for route in ['/me/with_replies', '/me/reposts']:
        page.goto(base + route)
        assert page.locator('[data-testid="emptyState"]').count() == 1
        assert page.locator('article').count() == 0


def test_endless_ids_are_unique_per_tab(browser):
    page, base, _ = browser
    groups = []
    for route in ['/me', '/me/with_replies', '/me/reposts']:
        page.goto(base + route + '?endless=1&count=3')
        page.evaluate('window.loadMore()')
        ids = set(page.locator('article').evaluate_all("es => es.map(e => e.querySelector('[data-testid=\"User-Name\"] a time').closest('a').getAttribute('href').split('/').pop())"))
        assert len(ids) == 3
        assert all(ids.isdisjoint(previous) for previous in groups)
        groups.append(ids)


def test_virtual_reload_after_delete(browser):
    page, base, _ = browser
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto(base + '/virtual.html')
    page.evaluate('sessionStorage.deleted = JSON.stringify(["100"])')
    page.reload()
    ids = set()
    for y in range(0, 19000, 450):
        page.evaluate('(y) => window.scrollTo(0, y)', y)
        page.wait_for_timeout(20)
        ids.update(page.locator('article').evaluate_all("es => es.map(e => e.querySelector('[data-testid=\"User-Name\"] a time').closest('a').getAttribute('href').split('/').pop())"))
    assert not errors
    assert '139' in ids
    assert '100' not in ids


@pytest.mark.parametrize('route', ['/home', '/me', '/me/with_replies', '/me/reposts'])
def test_cleanup_covers_all_tabs_from_any_route(browser, route):
    result, _ = execute(browser, route=route)
    assert {row['url'].split('/')[-1] for row in result} == {'1', '2', '5', '7', '8'}


def test_scan_uses_clean_tab_urls(browser):
    page, _, _ = browser
    documents = []

    def record(request):
        if request.resource_type == 'document':
            url = urlparse(request.url)
            documents.append((url.path, url.query))

    page.on('request', record)
    execute(browser, route='/me/with_replies', query='?fresh=1')
    assert documents[-3:] == [('/me', ''), ('/me/with_replies', ''), ('/me/reposts', '')]


def test_complete_reposts_scan_prunes_old_absent_memory(browser):
    absent = browser[1] + '/other/status/999'
    state = {'handle': 'me', 'cleanup_started': True, 'reposts': {absent: '2000-01-01T00:00:00+00:00'}}
    execute(browser, live=True, state=state)
    assert absent not in state['reposts']


def test_action_tabs_share_removal_cap(browser):
    state = {'handle': 'me'}
    _, page = execute(browser, live=True, limit=3, state=state)
    assert set(page.evaluate('window.removed')) == {'1', '2', '7'}
    assert state['daily_removals'][datetime.now().date().isoformat()] == 3


def test_unused_scan_budget_rolls_over(browser, monkeypatch):
    page, base, root = browser
    page.goto(base + '/me?fresh=1')
    budgets = []

    def measure(page, timing, observe, deadline):
        budgets.append(deadline - time.monotonic())
        return iter(())

    monkeypatch.setattr(app, 'walk', measure)
    app.clean(page, {'handle': 'me'}, root / 'state.json',
              timing=replace(app.Timing(), run_seconds=30, scan_fraction=0.5))
    assert len(budgets) == 3
    assert 0 < budgets[0] < budgets[1] < budgets[2] <= 15


def test_stalled_tab_leaves_run_incomplete(browser):
    state = {'handle': 'me', 'reposts': {'unseen': '2020-01-01T00:00:00Z'}}
    _, page = execute(browser, live=True, query='?stalltab=reposts', state=state)
    assert 'last_live_date' not in state
    assert {'1', '2', '7'} <= set(page.evaluate('window.removed'))
    assert 'unseen' in state['reposts']

    state = {'handle': 'me'}
    execute(browser, live=True, query='?stalltab=posts', state=state)
    assert 'last_live_date' not in state


def test_full_tab_leaves_time_for_later_tabs(browser):
    page, base, root = browser
    page.goto(base + '/me?endless=1&endlesstab=posts&count=2000')
    app.clean(page, {'handle': 'me', 'reposts': {}}, root / 'state.json', live=True, pause=lambda: None,
              timing=replace(app.Timing(), run_seconds=20, scan_fraction=0.2))
    assert {'2', '5', '8'} <= set(page.evaluate('window.removed'))


def test_tab_without_content_stops_only_that_tab(browser, caplog):
    page, base, root = browser
    page.goto(base + '/me?stalltab=reposts&noempty=1')
    state = {'handle': 'me', 'reposts': {'unseen': '2020-01-01T00:00:00Z'}}
    app.clean(page, state, root / 'state.json', live=True, pause=lambda: None,
              timing=replace(app.Timing(), load_ms=500))
    assert {'1', '2', '7'} <= set(page.evaluate('window.removed'))
    assert 'unseen' in state['reposts']
    assert 'last_live_date' not in state
    assert 'reposts tab did not load' in caplog.text


def test_profile_tab_without_posts_counts_as_loaded(browser, caplog):
    page, base, root = browser
    page.goto(base + '/me?emptytab=posts')
    state = {'handle': 'me', 'reposts': {}}
    app.clean(page, state, root / 'state.json', live=True, pause=lambda: None,
              timing=replace(app.Timing(), load_ms=2000))
    assert set(page.evaluate('window.removed')) == {'2', '5', '8'}
    assert 'last_live_date' in state
    assert 'did not load' not in caplog.text


def test_action_tab_load_failure_skips_only_that_tab(browser, monkeypatch, caplog):
    opened = app.open_tab
    calls = []

    def failing(page, url, timing, deadline):
        calls.append(url)
        if url.endswith('/with_replies') and calls.count(url) == 2:
            return False
        return opened(page, url, timing, deadline)

    monkeypatch.setattr(app, 'open_tab', failing)
    state = {'handle': 'me', 'reposts': {}}
    _, page = execute(browser, live=True, state=state)
    removed = set(page.evaluate('window.removed'))
    assert '2' not in removed
    assert {'1', '5', '7', '8'} <= removed
    assert 'last_live_date' not in state
    assert state['last_cleanup']['action_error']
    assert 'replies tab did not load' in caplog.text


def test_repost_on_other_tab_keeps_memory_date():
    now = datetime.now(UTC)
    url = 'https://x.com/other/status/5'
    item = {'url': url, 'context': 'You reposted', 'context_links': [], 'date': '2020-01-01T00:00:00+00:00'}
    state = {'handle': 'me', 'cleanup_started': True, 'reposts': {}}
    app.reconcile({('repost', '5'): item}, state, now, 30, True, [])
    first = state['reposts'][url]
    for day in range(1, 4):
        app.reconcile({('repost', '5'): item}, state, now + timedelta(days=day), 30, True, [])
        assert state['reposts'][url] == first


@pytest.mark.parametrize(('switch_after', 'removed'), [
    pytest.param(1, ['1'], id='delete'),
    pytest.param(4, ['1', '7', '2', '8'], id='undo'),
])
def test_account_switch_between_actions(browser, switch_after, removed):
    page, _, _ = browser
    with pytest.raises(app.SessionStopped, match='^Authenticated account does not match saved handle$'):
        execute(browser, live=True, query=f'?switchafter={switch_after}')
    assert page.evaluate('window.removed') == removed
    if switch_after == 1:
        assert '7' not in page.evaluate('window.attempts')
    else:
        assert app.count_layers(page) == 0


@pytest.mark.parametrize(('kind', 'message'), [
    ('two', UNCLEAR_MESSAGE),
    ('none', 'Authenticated account does not match saved handle'),
])
def test_account_unavailable_between_actions(browser, kind, message):
    page, _, _ = browser
    with pytest.raises(app.SessionStopped, match=f'^{message}$'):
        execute(browser, live=True, query=f'?switchafter=1&switchkind={kind}')
    assert page.evaluate('window.removed') == ['1']


@pytest.mark.parametrize(('confirmation', 'removed'), [
    pytest.param(app.CONFIRM, [], id='delete'),
    pytest.param(app.UNDO_CONFIRM, ['1', '7', '2'], id='undo'),
])
def test_account_switch_during_confirmation(browser, monkeypatch, confirmation, removed):
    page, _, _ = browser
    owns_confirmation = app.owns_confirmation
    switched = False

    def switch(button, selector, menu, inside):
        nonlocal switched
        if selector == confirmation and not switched:
            page.evaluate('switchAccount("other")')
            switched = True
        return owns_confirmation(button, selector, menu, inside)

    monkeypatch.setattr(app, 'owns_confirmation', switch)
    with pytest.raises(app.SessionStopped, match='^Authenticated account does not match saved handle$'):
        execute(browser, live=True)
    assert page.evaluate('window.removed') == removed
    assert app.count_layers(page) == 0


@pytest.mark.parametrize('live', [False, True], ids=['dry', 'live'])
def test_account_switch_before_scan_tab(browser, monkeypatch, live):
    page, _, root = browser
    walk = app.walk
    state = {'handle': 'me', 'reposts': {}}
    path = root / 'state.json'
    app.save_state(path, state)
    before = path.read_bytes()

    # The open page keeps the saved account; only the next load shows the other one.
    def switch_after_replies(page, timing, observe, deadline):
        yield from walk(page, timing, observe, deadline)
        if urlparse(page.url).path == '/me/with_replies':
            page.evaluate('sessionStorage.accountKind = "other"')

    monkeypatch.setattr(app, 'walk', switch_after_replies)
    with pytest.raises(app.SessionStopped, match='^Authenticated account does not match saved handle$'):
        execute(browser, live=live, state=state)
    assert page.evaluate('window.removed') == []
    assert path.read_bytes() == before
    assert state == {'handle': 'me', 'reposts': {}}


def test_account_switch_before_action_tab(browser, monkeypatch):
    page, _, root = browser
    act = app.act

    # The open page keeps the saved account; only the next load shows the other one.
    def switch_after_last_post(page, article, target, *args):
        outcome = act(page, article, target, *args)
        if target == ('post', '7'):
            page.evaluate('sessionStorage.accountKind = "other"')
        return outcome

    monkeypatch.setattr(app, 'act', switch_after_last_post)
    with pytest.raises(app.SessionStopped, match='^Authenticated account does not match saved handle$'):
        execute(browser, live=True)
    assert page.evaluate('window.removed') == ['1', '7']
    with (root / 'logs' / 'removed.csv').open(newline='', encoding='utf-8') as audit:
        assert all(urlparse(row['url']).path.split('/')[-1] != '2' for row in csv.DictReader(audit))


def test_action_account_read_error_retry_cap(browser, monkeypatch):
    state = {'handle': 'me', 'reposts': {}}
    calls = 0

    def interrupted(page, handle):
        nonlocal calls
        calls += 1
        raise app.PlaywrightError('Navigation interrupts the read')

    monkeypatch.setattr(app, 'check_account', interrupted, raising=False)
    _, page = execute(browser, live=True, state=state)
    assert page.evaluate('window.removed') == []
    assert page.evaluate('window.attempts') == {}
    assert calls == 15
    assert state['last_cleanup']['action_error'] is True


def test_account_switch_with_menu_open(browser, monkeypatch):
    page, _, _ = browser
    own_control = app.own_control
    confirmations = []
    switched = False

    def switch_at_caret(article, selector):
        nonlocal switched
        control = own_control(article, selector)
        if selector == app.CARET and not switched:
            page.evaluate('switchAccount("other")')
            switched = True
        return control

    def record(*args):
        confirmations.append(args[1])
        return False

    monkeypatch.setattr(app, 'own_control', switch_at_caret)
    monkeypatch.setattr(app, 'owns_confirmation', record)
    with pytest.raises(app.SessionStopped, match='^Authenticated account does not match saved handle$'):
        execute(browser, live=True)
    assert confirmations == []
    assert page.evaluate('window.removed') == []
    assert app.count_layers(page) == 0


def replace_post_before_action(monkeypatch, copies):
    act = app.act

    def replace_post(page, article, target, *args, **kwargs):
        if target == ('post', '1'):
            page.evaluate('copies => rerender("1", copies)', copies)
        return act(page, article, target, *args, **kwargs)

    monkeypatch.setattr(app, 'act', replace_post)


def audit_statuses(browser, status):
    with (browser[2] / 'logs' / 'removed.csv').open(newline='', encoding='utf-8') as audit:
        return [row['status'] for row in csv.DictReader(audit) if app.status_id(row) == status]


def test_rerender_before_action(browser, monkeypatch):
    replace_post_before_action(monkeypatch, 1)

    _, page = execute(browser, live=True)

    assert '1' in page.evaluate('window.removed')
    assert page.evaluate('window.attempts["1"]') == 1


def test_rerender_with_delete_sheet_open(browser, monkeypatch):
    page, _, _ = browser
    owns_confirmation = app.owns_confirmation
    replaced = False

    def replace_post(button, selector, menu, inside):
        nonlocal replaced
        if selector == app.CONFIRM and not replaced:
            page.evaluate('rerender("1")')
            replaced = True
        return owns_confirmation(button, selector, menu, inside)

    monkeypatch.setattr(app, 'owns_confirmation', replace_post)

    execute(browser, live=True)

    assert '1' in page.evaluate('window.removed')
    assert page.evaluate('window.attempts["1"]') == 2
    assert app.count_layers(page) == 0


def test_rerender_with_duplicate_targets(browser, monkeypatch):
    replace_post_before_action(monkeypatch, 2)

    _, page = execute(browser, live=True)

    removed = page.evaluate('window.removed')
    assert '1' not in removed
    assert '1' not in page.evaluate('window.attempts')
    assert '7' in removed


def test_rerender_retry_cap(browser, monkeypatch):
    state = {'handle': 'me', 'reposts': {}}
    page, _, _ = browser
    act, snapshot, close_layers = app.act, app.snapshot, app.close_layers
    target_active = False
    attempt_open = False
    attempts = 0

    def track_target(page, article, target, *args, **kwargs):
        nonlocal target_active
        target_active = target == ('post', '1')
        try:
            return act(page, article, target, *args, **kwargs)
        finally:
            target_active = False

    def replace_post(article):
        if target_active:
            page.evaluate('rerender("1")')
        return snapshot(article)

    def track_attempt(page, timing):
        nonlocal attempt_open, attempts
        if target_active:
            attempt_open = not attempt_open
            attempts += int(attempt_open)
        return close_layers(page, timing)

    monkeypatch.setattr(app, 'act', track_target)
    monkeypatch.setattr(app, 'snapshot', replace_post)
    monkeypatch.setattr(app, 'close_layers', track_attempt)

    execute(browser, live=True, state=state)

    assert attempts == 3
    assert '1' not in page.evaluate('window.attempts')
    assert '1' not in page.evaluate('window.removed')
    assert state['last_cleanup']['action_error'] is True


def test_rerender_does_not_prove_delete(browser, monkeypatch):
    page, _, _ = browser
    save_state = app.save_state
    replaced = False

    def replace_post(path, state):
        nonlocal replaced
        if state.get('daily_removals') and not replaced:
            page.evaluate('rerender("1")')
            replaced = True
        save_state(path, state)

    monkeypatch.setattr(app, 'save_state', replace_post)

    execute(browser, live=True, limit=1, query='?stuck=1')

    assert audit_statuses(browser, '1') == ['pending', 'unverified']
    assert page.evaluate('window.removed') == []


def test_undo_drops_cell_from_intact_timeline(browser):
    state = {'handle': 'me', 'reposts': {}}

    _, page = execute(browser, live=True, query='?undodrop=cell', state=state)

    assert {'8', '5'} <= set(page.evaluate('window.removed'))
    for status in ('8', '5'):
        assert audit_statuses(browser, status) == ['pending', 'removed']
    assert state['last_cleanup']['action_error'] is False
    assert state['last_live_date']


def test_undo_loses_timeline_without_removing_repost(browser):
    url = browser[1] + '/other/status/8'
    state = {'handle': 'me', 'cleanup_started': True, 'reposts': {url: '2020-01-01T00:00:00+00:00'}}

    _, page = execute(browser, live=True, query='?undodrop=timeline', state=state)

    assert '8' not in page.evaluate('window.removed')
    assert audit_statuses(browser, '8') == ['pending', 'unverified']
    assert url in state['reposts']


@pytest.mark.parametrize('mode', ['stale', 'retry', 'alert', 'errordetail'])
def test_undo_without_proof_stays_unverified(browser, mode):
    url = browser[1] + '/other/status/8'
    state = {'handle': 'me', 'cleanup_started': True, 'reposts': {url: '2020-01-01T00:00:00+00:00'}}

    _, page = execute(browser, live=True, query=f'?undodrop={mode}', state=state)

    assert '8' not in page.evaluate('window.removed')
    assert audit_statuses(browser, '8') == ['pending', 'unverified']
    assert url in state['reposts']
