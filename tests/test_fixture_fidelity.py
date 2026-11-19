import json
import re
from hashlib import sha256
from pathlib import Path

import pytest

from tests.fixtures.sanitize import Maps, clean, map_href, map_text, unique_object
from tests.test_tweet_cleanup import browser  # noqa: F401


def load_capture(text):
    return json.loads(text, object_pairs_hook=unique_object)


CAPTURE = load_capture((Path(__file__).parent / 'fixtures/x_capture.json').read_text())


def test_shell_heights(browser):  # noqa: F811
    page, base, _ = browser
    page.goto(base + '/me')
    for height, count in [(657, 0), (1000, 1)]:
        page.set_viewport_size({'width': 1280, 'height': height})
        page.wait_for_timeout(100)
        assert page.locator('[data-testid=AppTabBar_Profile_Link]').count() == count
        assert page.locator('[data-testid=SideNav_AccountSwitcher_Button] [data-testid=UserAvatar-Container-me]').count() == 1


@pytest.mark.parametrize('part', ['caret', 'confirmation', 'undo'])
def test_trusted_escape(browser, part):  # noqa: F811
    page, base, _ = browser
    page.goto(base + ('/me/reposts' if part == 'undo' else '/me'))
    page.locator('[data-testid=unretweet]' if part == 'undo' else '[data-testid=caret]').first.click()
    if part == 'confirmation':
        page.locator('#layers').get_by_role('menuitem', name='Delete', exact=True).click()
    page.evaluate("document.dispatchEvent(new KeyboardEvent('keydown', {key:'Escape'}))")
    assert page.locator('#layers > *').count() == 1
    page.keyboard.press('Escape')
    assert page.locator('#layers > *').count() == 0


def test_tab_contents(browser):  # noqa: F811
    page, base, _ = browser
    for route in ['/me', '/me/with_replies', '/me/reposts']:
        page.goto(base + route)
        page.wait_for_selector('article')
        for _ in range(5):
            page.evaluate('window.loadMore()')
        articles = page.locator('article')
        authors = articles.evaluate_all("nodes=>nodes.map(a=>a.querySelector('[data-testid=\"User-Name\"] a').getAttribute('href'))")
        if route.endswith('reposts'):
            assert articles.evaluate_all("nodes=>nodes.map(a=>a.querySelector('[data-testid=tweetText]').textContent)") == ['fresh repost', 'repost']
            dates = articles.locator('[data-testid=User-Name] a time').all()
            dates = [time.get_attribute('datetime') for time in dates]
            assert dates != sorted(dates, reverse=True)
            assert articles.locator('[data-testid=unretweet]').count() == articles.count()
            assert articles.locator('[data-testid=socialContext] a').count() == 0
            assert articles.locator('[data-testid=socialContext]').all_text_contents() == ['You reposted'] * articles.count()
        else:
            assert articles.locator('[data-testid=unretweet]').count() == 0
            if route == '/me':
                assert set(authors) == {'/me'}
                assert 'reply' not in articles.locator('[data-testid=tweetText]').all_text_contents()
            else:
                assert '/other' in authors
                assert 'reply' in articles.locator('[data-testid=tweetText]').all_text_contents()


def test_caret_target(browser):  # noqa: F811
    page, base, _ = browser
    page.goto(base + '/me')
    page.locator('[data-testid=caret]').first.click()
    menu = page.locator('#layers [role=menu]')
    assert menu.get_by_role('menuitem', name='Delete', exact=True).get_attribute('data-testid') is None
    assert menu.locator('a[href="/me/status/1/quotes"]').count() == 1
    assert menu.locator('a[href="/me/status/1/analytics"]').count() == 1
    assert menu.locator('a[href="/i/communitynotes/noterequest/1"]').count() == 1
    assert page.locator('article [role=menu]').count() == 0


def test_undo_repost(browser):  # noqa: F811
    page, base, _ = browser
    page.goto(base + '/me/reposts')
    button = page.locator('[data-testid=unretweet]').first
    article = page.locator('article').first
    button.click()
    assert page.locator('#layers a[href="/compose/post"][role=menuitem]').inner_text() == 'Quote'
    page.locator('#layers div[role=menuitem][data-testid=unretweetConfirm]').click()
    assert article.count() == 1
    assert article.locator('[data-testid=retweet]').count() == 1
    assert page.locator('#layers [role=menu]').count() == 0


def check_pin(content):
    assert sha256(content).hexdigest() == '028fc7ccd14fd28c89be2373b14d416d1378f363ced705ff5bbce3f9b8c518c0', 'pin'


SKELETON = {'captured': {'first': '2026-11-17T03:08:41.235537+00:00', 'second': '2026-11-17T20:24:21.933719+00:00'}, 'note': 'Sanitized real X markup. datetime="age:N" holds the age in seconds at capture time.', 'articles': [{'tab': 'posts'}, {'tab': 'posts'}, {'tab': 'posts'}, {'tab': 'posts'}, {'tab': 'posts'}, {'tab': 'with_replies'}, {'tab': 'with_replies'}, {'tab': 'with_replies'}, {'tab': 'with_replies'}, {'tab': 'with_replies'}, {'tab': 'with_replies'}, {'tab': 'with_replies'}, {'tab': 'with_replies'}, {'tab': 'with_replies'}, {'tab': 'with_replies'}, {'tab': 'with_replies'}, {'tab': 'with_replies'}, {'tab': 'with_replies'}, {'tab': 'with_replies'}, {'tab': 'with_replies'}, {'tab': 'with_replies'}, {'tab': 'with_replies'}, {'tab': 'with_replies'}, {'tab': 'with_replies'}, {'tab': 'with_replies'}, {'tab': 'with_replies'}, {'tab': 'with_replies'}, {'tab': 'with_replies'}, {'tab': 'with_replies'}, {'tab': 'with_replies'}, {'tab': 'with_replies'}, {'tab': 'with_replies'}, {'tab': 'with_replies'}, {'tab': 'with_replies'}, {'tab': 'with_replies'}, {'tab': 'with_replies'}, {'tab': 'with_replies'}, {'tab': 'with_replies'}, {'tab': 'with_replies'}, {'tab': 'with_replies'}, {'tab': 'with_replies'}, {'tab': 'with_replies'}, {'tab': 'with_replies'}, {'tab': 'with_replies'}, {'tab': 'with_replies'}, {'tab': 'reposts'}, {'tab': 'reposts'}, {'tab': 'reposts'}, {'tab': 'reposts'}], 'caret_menu': {'target': '/OwnUser/status/1900000000000000051'}, 'shell_default': {'inner': [982, 657, 1024, 768], 'profile_link': 0, 'switcher': 1}, 'shell_1280': {'inner': [1280, 1000, 1024, 768], 'profile_link': 1, 'switcher': 1}, 'reposts_shell': {'inner': [1280, 1000, 1024, 768], 'profile_link': 1, 'switcher': 1}, 'undo_menu': {'target': '/user34/status/1900000000000000052', 'items': [{'text': 'Undo repost', 'testid': 'unretweetConfirm', 'inLayers': True}, {'text': 'Quote', 'testid': None, 'inLayers': True}]}}


def check_capture(capture):
    def lock(value, expected, path="capture"):
        if isinstance(expected, dict):
            if type(value) is not dict or not expected.keys() <= value.keys():
                raise ValueError(path)
            for key, item in expected.items():
                lock(value[key], item, path + "." + key)
        elif isinstance(expected, list):
            if type(value) is not list:
                raise ValueError(path)
            if len(value) != len(expected):
                raise ValueError(path)
            for i, item in enumerate(expected):
                lock(value[i], item, path + f"[{i}]")
        elif type(value) is not type(expected) or value != expected:
            raise ValueError(path)
    lock(capture, SKELETON)
    def obj(value, fields):
        assert type(value) is dict and value.keys() == fields.keys(), 'capture keys'
        for key, kind in fields.items():
            assert type(value[key]) in (kind if isinstance(kind, tuple) else (kind,))

    obj(capture, {'captured': dict, 'note': str, 'articles': list, 'caret_menu': dict,
                      'shell_default': dict, 'shell_1280': dict, 'reposts_shell': dict, 'undo_menu': dict})
    obj(capture['captured'], {'first': str, 'second': str})
    for article in capture['articles']:
        obj(article, {'tab': str, 'html': str})
        assert article['tab'] in ('posts', 'with_replies', 'reposts')
        assert clean(article['html'], Maps.fixed()) == article['html'], 'html fixed point'
    for name in ('shell_default', 'shell_1280', 'reposts_shell'):
        shell = capture[name]
        obj(shell, {'inner': list, 'profile_link': int, 'switcher': int, 'banner': str, 'primary_top': str})
        assert len(shell['inner']) == 4 and all(type(n) is int for n in shell['inner'])
        for key in ('banner', 'primary_top'):
            assert clean(shell[key], Maps.fixed()) == shell[key]
    obj(capture['caret_menu'], {'target': str, 'html': str})
    obj(capture['undo_menu'], {'target': str, 'items': list, 'html': list})
    for name in ('caret_menu', 'undo_menu'):
        menu = capture[name]
        assert map_href(menu['target'], Maps.fixed()) == menu['target']
        for html in ([menu['html']] if name == 'caret_menu' else menu['html']):
            assert type(html) is str and clean(html, Maps.fixed()) == html
    for item in capture['undo_menu']['items']:
        obj(item, {'text': str, 'testid': (str, type(None)), 'inLayers': bool})
        assert map_text(item['text'], Maps.fixed()) == item['text']
        assert item['testid'] in ('unretweetConfirm', None)


def test_capture_data_lock():
    check_pin((Path(__file__).parent / 'fixtures/x_capture.json').read_bytes())
    check_capture(CAPTURE)


def test_pin_rejects_valid_markup():
    import copy
    capture = copy.deepcopy(CAPTURE)
    capture['articles'][0]['html'] += '<span role="status">text</span>'
    check_capture(capture)
    with pytest.raises(AssertionError, match="pin"):
        check_pin(json.dumps(capture, indent=1, ensure_ascii=False).encode())


UNCAPTURED = {'pin': '[data-testid=socialContext]', 'confirmation': '[role=dialog]'}

TREE = r"""node=>{
 const article=node.matches('article') ? node : null;
 const styledMain=node.matches('[data-testid=primaryColumn]') ? node.querySelector('[aria-label="Timeline: text"] main') : null;
 const header=article?.querySelector('[data-testid="User-Name"]');
 const author=header?.querySelector('a').getAttribute('href').slice(1).replaceAll('OwnUser','me');
 const status=header?.querySelector('time').closest('a').getAttribute('href').split('/').pop();
 const text=article?.querySelector('[data-testid="tweetText"]');
 let first=true;
 function value(v){
   v=v.replaceAll('OwnUser','me');
   if(author) v=v.replaceAll('/'+author+'/', '/AUTHOR/').replaceAll('/'+author, '/AUTHOR')
     .replaceAll('Container-'+author, 'Container-AUTHOR').replaceAll('@'+author, '@AUTHOR');
   if(status) v=v.replaceAll('/status/'+status, '/status/ID');
   return v;
 }
 function walk(e){
   if(e.nodeType===3){
     if(text?.contains(e)){const v=first?'TEXT':'';first=false;return v;}
     return value(e.data);
   }
   if(e.nodeType!==1) throw Error('Unexpected node');
   const attrs=[...e.attributes].filter(a=>!(a.name==='style'&&e===styledMain)).map(a=>{
     if(a.name==='datetime'){
       if(!/^(age:[0-9]+|[0-9]{4}-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]\.[0-9]{3}Z)$/.test(a.value)) throw Error('Invalid date');
       return [a.name,'DATE'];
     }
     return [a.name,value(a.value)];
   }).sort((a,b)=>a[0].localeCompare(b[0]));
   return [e.tagName,attrs,[...e.childNodes].filter(n=>!(n.nodeType===1&&e===article&&n===article.firstChild&&n.matches('span[data-testid=socialContext]')&&n.attributes.length===1&&n.textContent==='Pinned')).map(walk)];
 }
 return walk(node);
}"""


def template_tree(page, html, selector):
    return page.evaluate("""([html,selector,tree])=>{const t=document.createElement('template');t.innerHTML=html;return eval('('+tree+')')(t.content.querySelector(selector))}""", [html, selector, TREE])


@pytest.mark.parametrize('route,tab', [('/me', 'posts'), ('/me/with_replies', 'with_replies'), ('/me/reposts', 'reposts'), ('/virtual.html', 'posts')])
@pytest.mark.parametrize('height', [657, 1000])
def test_render_lock(browser, route, tab, height):  # noqa: F811
    page, base, _ = browser
    page.set_viewport_size({'width': 1280, 'height': height})
    page.goto(base + route)
    page.wait_for_selector('article')
    if route != '/virtual.html':
        for _ in range(6):
            page.evaluate('window.loadMore()')
    expected_profile = page.evaluate("""([html,tab,tree])=>{
        const t=document.createElement('template');t.innerHTML=html.replaceAll('OwnUser','me');
        const primary=t.content.querySelector('[data-testid=primaryColumn]');
        primary.querySelector('[aria-label="Timeline: text"]').innerHTML='<main></main>';
        for(const a of primary.querySelectorAll('[role=tab]')) a.setAttribute('aria-selected',String(a.getAttribute('href')==='/me'+(tab==='posts'?'':'/'+tab)));
        return eval('('+tree+')')(primary);
    }""", [CAPTURE['reposts_shell']['primary_top'], tab, TREE])
    actual_profile = page.locator('[data-testid=primaryColumn]').evaluate("""(node,tree)=>{
        const copy=node.cloneNode(true);copy.querySelector('main').replaceChildren();
        return eval('('+tree+')')(copy);
    }""", TREE)
    assert actual_profile == expected_profile
    assert page.locator('#layers').inner_html() == ''
    assert page.locator('body').evaluate("e=>[...e.children].map(n=>[n.tagName,n.id])") == [
        ['DIV', 'shell'], ['DIV', 'profile'], ['DIV', 'layers'], ['SCRIPT', ''],
    ]
    assert page.locator('#shell > *').count() == 1
    assert page.locator('#profile > *').count() == 1
    assert page.locator('main').evaluate("e=>[...e.childNodes].every(n=>n.nodeType===1&&n.tagName==='DIV'&&[...n.attributes].every(a=>['style','data-testid'].includes(a.name))&&n.dataset.testid==='cellInnerDiv'&&n.childNodes.length===1&&n.firstChild.matches('article'))")
    templates = page.evaluate("""([capture,tab,tree])=>{
        const result={};
        for(const item of capture.articles.filter(i=>i.tab===tab)) {
            const t=document.createElement('template');t.innerHTML=item.html;
            const a=t.content.querySelector('article');
            const author=a.querySelector('[data-testid="User-Name"] a').getAttribute('href');
            const kind=tab==='reposts'?'repost':author!=='/OwnUser'?'other':
                a.querySelector('div[role="link"][tabindex="0"]')?'quote':
                a.textContent.includes('Replying to')?'reply':'post';
            result[kind]=eval('('+tree+')')(a);
        }
        return result;
    }""", [CAPTURE, tab, TREE])
    for article in page.locator('article').all():
        for pin in article.locator(UNCAPTURED['pin']).filter(has_text=re.compile('^Pinned$')).all():
            assert pin.evaluate(TREE) == ['SPAN', [['data-testid', 'socialContext']], ['Pinned']]
        row_id = article.locator('[data-testid=User-Name] time').first.evaluate("e=>e.closest('a').getAttribute('href').split('/').pop()")
        expected_author = page.evaluate("id => selected.find(row=>row[0]===id)[1]", row_id)
        assert article.locator('[data-testid=User-Name] a').first.get_attribute('href') == '/' + expected_author
        expected_text = page.evaluate("id => selected.find(row=>row[0]===id)[4]", row_id)
        assert article.locator('[data-testid=tweetText]').first.text_content() == expected_text
        actual = article.evaluate(TREE)
        kind = page.evaluate("id => selected.find(row=>row[0]===id)[5]", row_id)
        assert actual == templates[kind], tree_diff(actual, templates[kind])
    shell = 'shell_default' if height == 657 else 'shell_1280'
    assert page.locator('header[role=banner]').evaluate(TREE) == template_tree(page, CAPTURE[shell]['banner'], 'header')
    if tab == 'reposts':
        page.locator('[data-testid=unretweet]').first.click()
        html = ''.join(CAPTURE['undo_menu']['html'])
    else:
        page.locator('[data-testid=caret]').first.click()
        target = page.locator('article').first.locator('[data-testid=User-Name] time').first.evaluate("e=>e.closest('a').getAttribute('href').split('/').pop()")
        html = CAPTURE['caret_menu']['html'].replace(CAPTURE['caret_menu']['target'].split('/')[-1], target)
    assert page.locator('#layers').evaluate(TREE) == template_tree(page, '<div id="layers">' + html + '</div>', '#layers')


def test_confirmation_exception(browser):  # noqa: F811
    page, base, _ = browser
    page.goto(base + '/me')
    page.locator('[data-testid=caret]').first.click()
    page.locator('#layers').get_by_role('menuitem', name='Delete', exact=True).click()
    sheet = page.locator('#layers').locator(UNCAPTURED['confirmation'])
    assert sheet.evaluate(TREE) == ['DIV', [['aria-modal', 'true'], ['role', 'dialog']], [
        ['BUTTON', [['data-testid', 'confirmationSheetConfirm']], ['Delete']],
        ['BUTTON', [], ['Cancel']],
    ]]
    sheet.get_by_role('button', name='Cancel', exact=True).click()
    assert page.locator('#layers').inner_html() == ''
    page.locator('[data-testid=caret]').first.click()
    page.locator('#layers').get_by_role('menuitem', name='Delete', exact=True).click()
    page.locator('[data-testid=confirmationSheetConfirm]').click()
    assert page.locator('article [href="/me/status/1"]').count() == 0


def test_profile_tabs(browser):  # noqa: F811
    page, base, _ = browser
    for path in ['/me', '/me/with_replies', '/me/reposts']:
        page.goto(base + path)
        tabs = page.locator('[role=tab]')
        assert tabs.evaluate_all("nodes=>nodes.map(a=>[a.textContent,a.getAttribute('href')])") == [
            ['Posts', '/me'], ['Replies', '/me/with_replies'], ['Reposts', '/me/reposts'], ['Media', '/me/media'],
        ]
        assert page.locator('[role=tab][aria-selected=true]').get_attribute('href') == path


@pytest.mark.parametrize('injection,channel', [
    ('<a href="/janedoe/status/1900000000000000051"></a>', 'href[0]'),
    ('<div data-testid="UserAvatar-Container-janedoe"></div>', 'data-testid'),
    ('key', 'capture keys'),
    ('number', 'capture keys'),
    ('<div aria-label="Jane Q Doe"></div>', 'aria-label'),
    ('<div id="janedoe@mail.com"></div>', 'id'),
    ('<span>private words</span>', 'text'),
    ('<!-- janedoe -->', 'html fixed point'),
    ('<!-- private words', 'incomplete markup'),
    ('<janedoe></janedoe>', 'html fixed point'),
    ('<?x janedoe?>', 'html fixed point'),
    ('<![CDATA[janedoe]]>', 'html fixed point'),
    ('<!DOCTYPE janedoe>', 'html fixed point'),
    ('<a href="https://evil.example/x"></a>', 'href'),

])
def test_reject_private_capture(injection, channel):
    import copy
    capture = copy.deepcopy(CAPTURE)
    if injection in ('key', 'number'):
        capture['janedoe'] = 15551234567 if injection == 'number' else 'posts'
    else:
        capture['articles'][0]['html'] += injection

    with pytest.raises((AssertionError, ValueError), match=channel_error(channel)):
        check_capture(capture)


@pytest.mark.parametrize('stale', ['caret', 'confirmation', 'undo'])
@pytest.mark.parametrize('next_action', ['caret', 'unretweet'])
def test_stale_layers(browser, stale, next_action):  # noqa: F811
    page, base, _ = browser
    page.goto(base + ('/me/reposts' if stale == 'undo' or next_action == 'unretweet' else '/me'))
    page.locator('[data-testid=unretweet]' if stale == 'undo' else '[data-testid=caret]').first.click()
    if stale == 'confirmation':
        page.get_by_role('menuitem', name='Delete', exact=True).click()
    page.evaluate("document.querySelector('#layers').firstElementChild.setAttribute('data-stale','true')")
    page.locator(f'[data-testid={next_action}]').first.evaluate('(e)=>e.click()')
    assert page.locator('#layers > [data-stale=true]').count() == 1
    assert page.locator('#layers > *').count() == 2
    page.keyboard.press('Escape')
    assert page.locator('#layers > [data-stale=true]').count() == 1
    assert page.locator('#layers > *').count() == 1


def tree_diff(actual, expected, path=''):
    if isinstance(actual, list) and isinstance(expected, list):
        if len(actual) != len(expected):
            return [(path, actual, expected)]
        return [d for i, (a, e) in enumerate(zip(actual, expected)) for d in tree_diff(a, e, path + '/' + str(i))]
    return [] if actual == expected else [(path, actual, expected)]


def test_reload_keeps_actions(browser):  # noqa: F811
    page, base, _ = browser
    page.goto(base + '/me')
    page.locator('[data-testid=caret]').first.click()
    page.get_by_role('menuitem', name='Delete', exact=True).click()
    page.locator('[data-testid=confirmationSheetConfirm]').click()
    page.reload()
    assert page.locator('article [href="/me/status/1"]').count() == 0
    assert page.evaluate('window.removed') == ['1']
    assert page.evaluate('window.attempts["1"]') == 1
    page.goto(base + '/me/reposts')
    page.locator('[data-testid=unretweet]').first.click()
    page.locator('[data-testid=unretweetConfirm]').click()
    page.reload()
    assert page.locator('article').first.locator('[data-testid=retweet]').count() == 1
    assert page.evaluate('window.removed') == ['1', '8']


@pytest.mark.parametrize('name,html', [
    ('role_email', '<div role="janedoe@mail.example"></div>'),
    ('role_entity', '<div role="jane&#64;mail.example"></div>'),
    ('role_tab', '<div role="jane\tdoe"></div>'),
    ('role_case', '<div RoLe="janedoe@mail.example"></div>'),
    ('duplicate', '<div role="button" role="button"></div>'),
    ('unicode_handle', '<a href="/user１２"></a>'),
    ('href_middle', '<a href="/i/janedoe@mail.example"></a>'),
    ('href_first', '<a href="/janedoe@mail.example"></a>'),
    ('text_count', '<span>janedoe views</span>'),
])
def test_closed_values(name, html):
    with pytest.raises(ValueError, match='duplicate attribute' if name == 'duplicate' else 'role' if name.startswith('role') else 'text' if name == 'text_count' else 'href'):
        clean(html, Maps.fixed())


def scalar_paths(value, path=()):
    if isinstance(value, dict):
        for key, item in value.items():
            yield from scalar_paths(item, (*path, key))
    elif isinstance(value, list):
        for i, item in enumerate(value):
            yield from scalar_paths(item, (*path, i))
    else:
        yield path


def html_paths():
    yield from (("articles", i, "html") for i in range(len(CAPTURE['articles'])))
    yield ('caret_menu', 'html')
    yield from (("undo_menu", "html", i) for i in range(len(CAPTURE['undo_menu']['html'])))
    for name in ('shell_default', 'shell_1280', 'reposts_shell'):
        for key in ('banner', 'primary_top'):
            yield (name, key)


from datetime import UTC

from tests.fixtures.sanitize import ATTRIBUTE_RULES

CHANNEL_CASES = (
    [(name, 'attribute', name) for name in ATTRIBUTE_RULES]
    + [(f'href[{i}]', 'href', i) for i in range(5)]
    + [('text', 'text', None)]
    + [('.'.join(map(str, p)), 'scalar', p) for p in scalar_paths(SKELETON)]
    + [('.'.join(map(str, p)), 'html', p) for p in html_paths()]
)


@pytest.mark.parametrize('channel,kind,path', CHANNEL_CASES)
def test_channel_rejection(channel, kind, path):
    import copy
    if kind in ('attribute', 'href', 'text'):
        if kind == 'attribute':
            html = f'<div {path}="janedoe@mail.example"></div>'
        elif kind == 'href':
            parts = ['OwnUser', 'status', '1900000000000000001', 'photo', '1']
            parts[path] = 'janedoe@mail.example'
            html = '<a href="/' + '/'.join(parts) + '"></a>'
        else:
            html = '<span>janedoe@mail.example</span>'
        with pytest.raises(ValueError, match=channel_error(channel)):
            clean(html, Maps.fixed())
        return
    capture = copy.deepcopy(CAPTURE)
    parent = capture
    for key in path[:-1]:
        parent = parent[key]
    if kind == 'html':
        parent[path[-1]] += '<span>janedoe@mail.example</span>'
        reason = 'text'
    else:
        parent[path[-1]] = 15551234567 if type(parent[path[-1]]) is int else 'janedoe@mail.example'
        reason = 'capture' + ''.join(f'[{key}]' if isinstance(key, int) else '.' + key for key in path)
    with pytest.raises(ValueError, match=channel_error(reason)):
        check_capture(capture)


@pytest.mark.parametrize('path,value', [
    pytest.param(('note',), 'janedoe@mail.example', id='note_email'),
    pytest.param(('captured', 'first'), 'janedoe@mail.example', id='captured_email'),
    pytest.param(('shell_default', 'profile_link'), 15551234567, id='shell_phone'),
])
def test_scalar_content(path, value, monkeypatch):
    import copy
    capture = copy.deepcopy(CAPTURE)
    parent = capture
    for key in path[:-1]:
        parent = parent[key]
    parent[path[-1]] = value
    monkeypatch.setitem(globals(), 'CAPTURE', capture)
    with pytest.raises(ValueError, match=channel_error('capture.' + '.'.join(path))):
        check_capture(capture)


@pytest.mark.parametrize('html,channel', [
    ('<span>@janedoe</span>', 'text'),
    ('<span>2 views</span>', 'text'),
    ('<span>12345 views</span>', 'text'),
    ('<span>48.2K</span>', 'text'),
    ('<span>11.1K</span>', 'text'),
    ('<span>1111</span>', 'text'),
    ('<span>1111 views</span>', 'text'),
    ('<div aria-label="11111111 views. View post analytics"></div>', 'aria-label'),
    ('<span>Jobs</span>', 'text'),
    ('<span>Messages</span>', 'text'),
    ('<span>Ad</span>', 'text'),
    ('<span>@</span>', 'text'),
    ('<span>Pinned</span>', 'text'),
    ('<span>Timeline: text</span>', 'text'),
    ('<span>Show\u00a0more</span>', 'text'),
    ('<span>\u3000</span>', 'whitespace'),
    ('<span>Home\u2028</span>', 'text'),
    ('<div aria-label="4821 views. View post analytics"></div>', 'aria-label'),
    ('<a href="/OwnUser/status/1987654321098765432"></a>', 'href'),
    ('<a href="/user4821"></a>', 'href'),
    ('<div id="id__5551234567"></div>', 'id'),
    ('<time datetime="age:1763350000"></time>', 'datetime'),
    ('<a href="/OwnUser/status/1900000000000000001/photo/48213"></a>', 'href'),
    ('<a href="/i/lists/user123"></a>', 'href'),
    ('<a href="/i/photo/photo/photo"></a>', 'href'),
    *[('<a href="/i/' + '/'.join(['4'] * i) + '"></a>', 'href') for i in range(1, 5)],
])
def test_exact_content(html, channel):
    with pytest.raises(ValueError, match=channel_error(channel)):
        clean(html, Maps.fixed())


BOUNDARY_PROBES = {'aria-disabled': 'true0',
 'aria-expanded': 'false0',
 'aria-haspopup': 'menu0',
 'aria-hidden': 'true0',
 'aria-label': '2111 views. View post analytics',
 'aria-labelledby': 'id__1061',
 'aria-live': 'polite0',
 'aria-selected': 'false0',
 'data-testid': 'UserAvatar-Container-user40',
 'datetime': 'age:770170',
 'dir': 'auto0',
 'disabled': '0',
 'href': '/OwnUser/status/1900000000000000058',
 'href[0]': 'user99',
 'href[1]': 'photo0',
 'href[2]': '1900000000000000058',
 'href[3]': '1900000000000000052',
 'href[4]': '4',
 'id': 'id__1061',
 'role': 'article0',
 'tabindex': '-10',
 'text': '@user40',
 'type': 'button0',
 'whitespace': '\u3000'}


@pytest.mark.parametrize('channel,probe', BOUNDARY_PROBES.items())
def test_inventory_boundary(channel, probe, monkeypatch):
    from html import escape

    from tests.fixtures import sanitize

    fixed_value = sanitize.fixed_value
    monkeypatch.setattr(sanitize, 'fixed_value', lambda value, name: fixed_value(value, name) if name == channel else value)
    if channel.startswith('href['):
        i = int(channel[5])
        parts = ['OwnUser', 'status', '1900000000000000051', 'photo', '1']
        parts[i] = probe
        monkeypatch.setitem(sanitize.HREF_RULES, i, sanitize.HREF_RULES[i] | {probe})
        html = '<a href="/' + '/'.join(parts[:i + 1]) + '"></a>'
    elif channel in ('text', 'whitespace'):
        monkeypatch.setattr(sanitize, 'text_valid', lambda *args: True)
        html = '<span>' + escape(probe) + '</span>'
    else:
        monkeypatch.setitem(ATTRIBUTE_RULES, channel, lambda value, m: value)
        html = f'<div {channel}="{escape(probe, quote=True)}"></div>'
    with pytest.raises(ValueError, match=channel_error(channel)):
        clean(html, Maps.fixed())
    monkeypatch.setitem(sanitize.FIXED_VALUES, channel, sanitize.FIXED_VALUES[channel] | {probe})
    assert clean(html, Maps.fixed()) == html


@pytest.mark.parametrize('channel,probe', BOUNDARY_PROBES.items())
def test_content_boundary(channel, probe):
    from html import escape
    if channel.startswith('href['):
        i = int(channel[5])
        parts = ['OwnUser', 'status', '1900000000000000051', 'photo', '1']
        parts[i] = probe
        html = '<a href="/' + '/'.join(parts[:i + 1]) + '"></a>'
    elif channel in ('text', 'whitespace'):
        html = '<span>' + escape(probe) + '</span>'
    else:
        html = f'<div {channel}="{escape(probe, quote=True)}"></div>'
    with pytest.raises(ValueError, match=channel_error(channel)):
        clean(html, Maps.fixed())


@pytest.mark.parametrize('source', [
    '{"note":"private","note":"real"}',
    '{"articles":[{"html":"private","html":"text"}]}',
])
def test_duplicate_json_keys(source):
    with pytest.raises(ValueError, match='json: duplicate key'):
        json.loads(source, object_pairs_hook=unique_object)


@pytest.mark.parametrize('tail', ['<', '</', '<!-- janedoe', '<div class', '<span data-testid="tweetText" style="tra'])
def test_incomplete_markup(tail):
    from datetime import datetime
    with pytest.raises(ValueError, match='html: incomplete markup'):
        clean('<span>text</span>' + tail, Maps.fixed())
    assert clean('<span>a</span>' + tail, Maps(datetime.now(UTC), 'me')) == '<span>text</span>'


@pytest.mark.parametrize('position', range(1, 5))
@pytest.mark.parametrize('value', ['user40', '1900000000000000058', '4'])
def test_href_position_content(position, value):
    parts = ['OwnUser', 'status', '1900000000000000051', 'photo', '1']
    parts[position] = value
    with pytest.raises(ValueError, match=channel_error(f'href[{position}]')):
        map_href('/' + '/'.join(parts[:position + 1]), Maps.fixed())


def object_paths(value, path=()):
    if isinstance(value, dict):
        yield path
        for key, item in value.items():
            yield from object_paths(item, (*path, key))
    elif isinstance(value, list):
        for i, item in enumerate(value):
            yield from object_paths(item, (*path, i))


@pytest.mark.parametrize('path', list(object_paths(CAPTURE)))
def test_duplicate_object_key(path):
    value = CAPTURE
    for key in path:
        value = value[key]
    key = next(iter(value))
    source = '{' + json.dumps(key) + ':"private",' + json.dumps(value)[1:]
    with pytest.raises(ValueError, match='json: duplicate key'):
        json.loads(source, object_pairs_hook=unique_object)


@pytest.mark.parametrize('html,channel', [
    ('<a href="https://x.com/i/lists/user123"></a>', 'href[2]'),
    ('<a href="/i/photo/photo/photo"></a>', 'href[2]'),
])
def test_raw_href_position(html, channel):
    from datetime import datetime
    with pytest.raises(ValueError, match=channel_error(channel)):
        clean(html, Maps(datetime.now(UTC), 'me'))


@pytest.mark.parametrize('name', ['aria-describedby', 'aria-controls'])
def test_unused_attributes(name):
    assert name not in ATTRIBUTE_RULES
    assert clean(f'<span {name}="private">text</span>', Maps.fixed()) == '<span>text</span>'


@pytest.mark.parametrize('name,value', [
    ('id', 'id__5\t'), ('aria-labelledby', ' id__1017 '),
    ('aria-labelledby', 'id__1015  id__1016'),
])
def test_fixed_id_spacing(name, value):
    with pytest.raises(ValueError, match=name):
        clean(f'<div {name}="{value}"></div>', Maps.fixed())


def test_capture_loader_duplicate():
    with pytest.raises(ValueError, match='duplicate key'):
        load_capture('{"note":"a","note":"b"}')


@pytest.mark.parametrize('path', list(scalar_paths(SKELETON)))
def test_scalar_near_miss(path):
    import copy
    capture = copy.deepcopy(CAPTURE)
    parent = capture
    for key in path[:-1]:
        parent = parent[key]
    value = parent[path[-1]]
    if isinstance(value, str):
        changed = value + 'x'
    elif type(value) is int:
        changed = int(str(value) + '1')
    elif type(value) is bool:
        changed = not value
    else:
        changed = 'x'
    parent[path[-1]] = changed
    channel = 'capture' + ''.join(f'[{k}]' if isinstance(k, int) else '.' + k for k in path)
    with pytest.raises(ValueError, match=channel_error(channel)):
        check_capture(capture)


from datetime import datetime

from tests.fixtures import sanitize


def channel_error(channel):
    # Errors chain as 'outer: inner: value', so the channel must be one whole link of that chain.
    return '(?:^|: )' + re.escape(channel) + '(?::|\n|$)'


RAW_TIME = datetime(2026, 11, 17, tzinfo=UTC)
RAW_PROBES = [
    (f'<div {name}="jane@mail.example"></div>', name, None)
    for name, rule in ATTRIBUTE_RULES.items() if isinstance(rule, set)
]
for position in range(5):
    for value in ('janedoe', 'jane@mail.example', 'private words', 'unknown_route_word'):
        parts = ['me', 'status', '123456', 'photo', '1']
        parts[position] = value
        html = '<a href="/' + '/'.join(parts[:position + 1]) + '"></a>'
        expected = '<a href="/user01"></a>' if position == 0 and value == 'janedoe' else None
        RAW_PROBES.append((html, f'href[{position}]', expected))
for value in ('Jane Doe', 'jane@mail.example', '+1 555 123 4567', '42 views by Jane'):
    RAW_PROBES.extend([
        (f'<span>{value}</span>', 'text', '<span>text</span>'),
        (f'<div aria-label="{value}"></div>', 'aria-label', '<div aria-label="text"></div>'),
    ])
RAW_PROBES.extend([
    ('<span>@janedoe</span>', 'text', '<span>@user01</span>'),
    ('<span>42 views</span>', 'text', '<span>11 views</span>'),
    ('<span>42.3K</span>', 'text', '<span>11.1K</span>'),
    ('<div data-testid="private"></div>', 'data-testid', None),
    ('<div data-testid="UserAvatar-Container-janedoe"></div>', 'data-testid', '<div data-testid="UserAvatar-Container-user01"></div>'),
    *[(f'<div {name}="private"></div>', name, f'<div {name}="id__1"></div>') for name in ('id', 'aria-labelledby')],
    ('<time datetime="2026-11-16T00:00:00Z"></time>', 'datetime', '<time datetime="age:86400"></time>'),
    ('<time datetime="2026-11-16T00:00:00"></time>', 'datetime', None),
])


def assert_raw_probe(module, html, channel, expected, mapper=None):
    m = module.Maps(RAW_TIME, 'me')
    if mapper:
        name, value = mapper
        setattr(m, name, lambda _: value)
    if expected is None:
        with pytest.raises(ValueError, match=channel_error(channel)):
            module.clean(html, m)
    else:
        assert module.clean(html, m) == expected


@pytest.mark.parametrize('html,channel,expected', RAW_PROBES)
def test_raw_channels(html, channel, expected):
    assert_raw_probe(sanitize, html, channel, expected)


# Mapper output checks keep the output grammar independent of the mapping tables.
OUTPUT_PROBES = [
    ('HANDLE', '<div data-testid="UserAvatar-Container-jane"></div>', 'data-testid', ('handle', 'janedoe')),
    ('STATUS', '<a href="/me/status/123456"></a>', 'href[2]', ('sid', '5551234567')),
    ('id', '<div id="private"></div>', 'id', ('eid', 'id__123456')),
    ('aria-labelledby', '<div aria-labelledby="private"></div>', 'aria-labelledby', ('eid', 'id__123456')),
    ('datetime', '<time datetime="1900-01-01T00:00:00Z"></time>', 'datetime', None),
]


@pytest.mark.parametrize('rule,html,channel,mapper', OUTPUT_PROBES)
def test_raw_output_shapes(rule, html, channel, mapper):
    assert_raw_probe(sanitize, html, channel, None, mapper)


@pytest.mark.parametrize('table,key', [
    *[('ATTRIBUTE_RULES', k) for k, v in ATTRIBUTE_RULES.items() if isinstance(v, set)],
    *[('HREF_RULES', i) for i in range(5)],
    ('LABELS', None), ('TESTIDS', None), ('UI_TEXT', None),
])
def test_raw_closed_boundaries(table, key, monkeypatch):
    value = 'jane@mail.example'
    if table == 'ATTRIBUTE_RULES':
        html, channel = f'<div {key}="{value}"></div>', key
    elif table == 'HREF_RULES':
        parts = ['me', 'status', '123456', 'photo', '1']
        parts[key] = value
        html, channel = '<a href="/' + '/'.join(parts[:key + 1]) + '"></a>', f'href[{key}]'
    elif table == 'TESTIDS':
        html, channel = f'<div data-testid="{value}"></div>', 'data-testid'
    elif table == 'LABELS':
        value = 'Jobs'
        html, channel = '<div aria-label="Jobs"></div>', 'aria-label'
    else:
        html, channel = f'<span>{value}</span>', 'text'
    expected = '<span>text</span>' if table == 'UI_TEXT' else None
    assert_raw_probe(sanitize, html, channel, expected)
    target = getattr(sanitize, table)
    if key is None:
        monkeypatch.setattr(sanitize, table, target | {value})
        if table == 'UI_TEXT':
            monkeypatch.setitem(sanitize.TEXT_RULES, 'ui', target | {value})
    else:
        monkeypatch.setitem(target, key, target[key] | {value})
    with pytest.raises((AssertionError, pytest.fail.Exception)):
        assert_raw_probe(sanitize, html, channel, expected)
