import re
from datetime import datetime
from html import escape
from html.parser import HTMLParser

UI_TEXT = {
    'Delete', 'Pin to your profile', 'Highlight on your profile', 'Add/remove from Lists',
    'View post activity', 'Embed post', 'View post analytics', 'Request Community Note',
    'You reposted', 'Repost', 'Undo repost', 'Quote', 'Pinned', 'Posts', 'Replies', 'Reposts',
    'Media', 'Profile', 'Home', 'Explore', 'Notifications', 'Follow', 'Chat', 'Calls', 'Grok',
    'More', 'Post', 'Show more', 'Retry', 'Premium', 'Lists', 'Bookmarks', 'Communities',
    'Messages', 'Jobs', 'Money', 'History', 'Creator Studio', 'Edit profile', 'Show this thread',
    'Replying to', 'Verified account', 'Bookmark', 'Share post', 'Image', 'Grok actions',
    'Account menu', 'Cancel', 'Edited', 'Ad', 'Translate post', '·', '@', 'Followers',
    'Following', 'Subscriptions', 'Search', 'Primary', 'Back', 'Joined', 'Highlights', 'Articles',
    'Likes', 'Embedded video', 'Play', 'Pause', 'GIF', 'Show', 'Hide', 'Follow back',
}
HANDLE = r'OwnUser|user[0-9]{2,3}'
STATUS = r'1900000000000000[0-9]{3}'
COUNT_SHAPES = (
    r'{n} (?:Reply|Replies)\. Reply', r'{n} (?:repost|reposts)\. Repost',
    r'{n} (?:Like|Likes)\. Like', r'{n} views?\. View post analytics',
    r'{n} (?:reply|replies|repost|reposts|like|likes|bookmark|bookmarks|view|views)(?:, {n} (?:reply|replies|repost|reposts|like|likes|bookmark|bookmarks|view|views))*',
)


def counts(value, fixed):
    return any(re.fullmatch(p.format(n='1+' if fixed else '[0-9]+'), value) for p in COUNT_SHAPES)


def require(value, rule, channel):
    if not (value in rule if isinstance(rule, set) else re.fullmatch(rule, value)):
        raise ValueError(f'{channel}: {value!r}')
    return value


VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'source', 'track', 'wbr'}
DROP = {'script', 'style', 'path', 'g', 'circle', 'rect', 'defs', 'linearGradient', 'stop', 'clipPath'}


TAGS = {'div', 'header', 'a', 'section', 'video', 'h2', 'button', 'svg', 'img', 'span', 'time', 'h1', 'nav', 'article'}


class Maps:
    def __init__(self, captured, own_handle):
        self.is_fixed = False
        self.captured = captured
        self.handles = {own_handle.lower(): 'OwnUser'}
        self.ids = {}
        self.eids = {}
        self.seen_handles = set()
        self.seen_ids = set()

    @classmethod
    def fixed(cls):
        m = cls(None, 'OwnUser')
        m.is_fixed = True
        return m

    def checked(self, value, pattern):
        if not re.fullmatch(pattern, value):
            raise ValueError(value)
        return value

    def handle(self, h):
        if self.is_fixed:
            return self.checked(h, HANDLE)
        self.seen_handles.add(h)
        low = h.lower()
        if low not in self.handles:
            self.handles[low] = f'user{len(self.handles):02d}'
        return self.handles[low]

    def sid(self, s):
        if self.is_fixed:
            return self.checked(s, STATUS)
        self.seen_ids.add(s)
        if s not in self.ids:
            self.ids[s] = str(1900000000000000000 + len(self.ids) + 1)
        return self.ids[s]

    def eid(self, s):
        if self.is_fixed:
            return self.checked(s, r'id__[0-9]{1,5}')
        if s not in self.eids:
            self.eids[s] = f'id__{len(self.eids) + 1}'
        return self.eids[s]


LABELS = {'Home', 'Image', 'History', 'Grok actions', 'Share post', 'Calls', 'Embedded video', 'Money', 'Primary', 'Creator Studio', 'Follow', 'Bookmark', 'More', 'Timeline: text', 'Grok', 'Account menu', 'text', 'Post', 'Verified account', 'Profile', 'Premium', 'Search', 'Back', 'Notifications'}
TESTIDS = {'UserJoinDate', 'AppTabBar_Explore_Link', 'ScrollSnap-prevButtonWrapper', 'videoPlayer', 'pillLabel', 'app-bar-back', 'caret', 'retweet', 'premium-signup-tab', 'highlightUpsell', 'ScrollSnap-List', 'UserDescription', 'socialContext', 'UserProfileHeader_Items', 'tweet', 'app-text-transition-container', 'analytics', 'testCondensedMedia', 'ScrollSnap-nextButtonWrapper', 'Dropdown', 'AppTabBar_Home_Link', 'unretweet', 'tweetText', 'AppTabBar_Notifications_Link', 'placementTracking', 'primaryColumn', 'reply', 'tweet-text-show-more-link', 'icon-birdwatch-fill', 'AppTabBar_Profile_Link', 'AppTabBar_Follow_Link', 'unlike', 'User-Name', 'card.wrapper', 'card.layoutLarge.media', 'UserName', 'birdwatch-pivot', 'UserUrl', 'AppTabBar_Calls_Link', 'AppTabBar_DirectMessage_Link', 'editProfileButton', 'unretweetConfirm', 'like', 'pin', 'bookmark', 'Tweet-User-Avatar', 'AppTabBar_More_Menu', 'tweetPhoto', 'cellInnerDiv', 'tweetEngagements', 'SideNav_NewTweet_Button', 'videoComponent', 'icon-verified', 'ownProfilePaymentsButton', 'ScrollSnap-SwipeableList', 'SideNav_AccountSwitcher_Button'}
HREF_RULES = {0: {'compose', 'explore', 'hashtag', 'home', 'i', 'notifications', 'settings'},
              1: {'about', 'calls', 'chat', 'communitynotes', 'connect_people', 'following', 'grok', 'header_photo', 'history', 'jf', 'lists', 'media', 'money', 'photo', 'post', 'premium_sign_up', 'profile', 'reposts', 'status', 'tag', 'verified-choose', 'verified_followers', 'with_replies'},
              2: {'add_member', 'creators', 'noterequest'},
              3: {'analytics', 'quotes', 'studio', 'photo'}, 4: set()}


def map_href(value, m):
    if value == 'https://example.com/link':
        return value
    if not m.is_fixed:
        value = re.sub(r'^https?://(?:x|twitter)\.com', '', value)
        if re.match(r'^https?://', value):
            return 'https://example.com/link'
        value = value.split('?')[0].split('#')[0]
    if not value.startswith('/') or any(c in value for c in '?#'):
        raise ValueError(f'href: {value!r}')
    parts = value[1:].split('/') if value != '/' else []
    for i, part in enumerate(parts):
        channel = f'href[{i}]'
        if i not in HREF_RULES:
            raise ValueError(f'{channel}: {part!r}')
        if not m.is_fixed:
            if i == 0 and part not in HREF_RULES[0] and re.fullmatch(r'[A-Za-z0-9_]{1,15}', part):
                part = m.handle(part)
            elif re.fullmatch(r'[0-9]{6,}', part):
                part = m.sid(part)
            elif i == 1 and parts[0] == 'hashtag':
                part = 'tag'
        if part not in HREF_RULES[i] and not re.fullmatch(HANDLE if i == 0 else f'(?:{HANDLE}|{STATUS}|[1-4])', part):
            raise ValueError(f'{channel}: {part!r}')
        if m.is_fixed:
            fixed_value(part, channel)
        elif i > 0 and part not in HREF_RULES[i] and not re.fullmatch(f'(?:{STATUS}|[1-4])', part):
            raise ValueError(f'{channel}: {part!r}')
        parts[i] = part
    result = '/' + '/'.join(parts)
    return fixed_value(result, 'href') if m.is_fixed else result


def map_testid(value, m):
    prefix = 'UserAvatar-Container-'
    if value.startswith(prefix):
        try:
            return prefix + require(m.handle(value[len(prefix):]), HANDLE, 'data-testid')
        except ValueError as e:
            raise ValueError(f'data-testid: {value!r}') from e
    return require(value, TESTIDS, 'data-testid')


def ui(text):
    t = ' '.join(text.split())
    return t in UI_TEXT or counts(t, False) or bool(re.fullmatch(r'[0-9]+(?:\.[0-9]+)?[KM]?', t))


TEXT_RULES = {
    'ui': UI_TEXT,
    'placeholder': {'Jan 1', 'text', 'Timeline: text'},
    'handle': rf'@(?:{HANDLE})',
    'short_count': r'1+(?:\.1)?[KM]?',
}


def text_valid(t, label=False):
    rules = [LABELS, TEXT_RULES['placeholder']] if label else list(TEXT_RULES.values())
    return counts(t, True) or any(
        t in rule if isinstance(rule, set) else re.fullmatch(rule, t) for rule in rules
    )


def map_text(text, m):
    if m.is_fixed:
        channel = 'text' if text.strip() else 'whitespace'
        if channel == 'text' and not text_valid(' '.join(text.split())):
            raise ValueError(f'text: {text!r}')
        return fixed_value(text, channel)
    t = ' '.join(text.split())
    if not t:
        return text
    if re.fullmatch(r'@[A-Za-z0-9_]{1,15}', t):
        result = '@' + m.handle(t[1:])
    elif t.startswith('Timeline: '):
        result = 'Timeline: text'
    elif ui(t):
        result = re.sub('[0-9]', '1', t)
    elif re.fullmatch(r'[A-Z][a-z]{2} [0-9]{1,2}(?:, [0-9]{4})?|[0-9]+[smhd]', t):
        result = 'Jan 1'
    else:
        result = 'text'
    if not text_valid(result):
        raise ValueError(f'text: {result!r}')
    return result


def map_label(value, m):
    result = value if m.is_fixed else map_text(value, m)
    if not text_valid(result, True):
        raise ValueError(f'aria-label: {value!r}')
    return result


def map_ids(value, m):
    return ' '.join(m.eid(v) for v in value.split())


def map_date(value, m):
    if m.is_fixed:
        return value
    date = datetime.fromisoformat(value.replace('Z', '+00:00'))  # noqa: FURB162
    if date.tzinfo is None:
        raise ValueError(f'datetime: {value!r}')
    age = (m.captured - date).total_seconds()
    return f'age:{round(age)}'


ATTRIBUTE_RULES = {
    'role': {'menuitem', 'button', 'navigation', 'heading', 'none', 'region', 'banner', 'presentation', 'article', 'status', 'tab', 'tablist', 'menu', 'img', 'link', 'group'},
    'tabindex': {'0', '-1'},
    'aria-hidden': {'true'},
    'dir': {'ltr', 'auto'},
    'type': {'button'},
    'aria-expanded': {'false'},
    'aria-haspopup': {'menu'},
    'aria-live': {'polite'},
    'aria-disabled': {'true'},
    'disabled': {''},
    'aria-selected': {'true', 'false'},
    'href': map_href, 'aria-label': map_label, 'data-testid': map_testid,
    'datetime': (map_date, r'age:[0-9]{1,9}'),
    **{k: (map_ids, r'id__[0-9]{1,5}(?: id__[0-9]{1,5})*')
       for k in ('id', 'aria-labelledby')},
}

FIXED_VALUES = {'aria-disabled': {'true'},
 'aria-expanded': {'false'},
 'aria-haspopup': {'menu'},
 'aria-hidden': {'true'},
 'aria-label': {'1 Like. Like',
                '1 Likes. Like',
                '1 Replies. Reply',
                '1 Reply. Reply',
                '1 like, 111 views',
                '1 like, 1111 views',
                '1 likes, 11 views',
                '1 likes, 111 views',
                '1 replies, 1 likes, 1 bookmark, 1111 views',
                '1 replies, 1 likes, 1111 views',
                '1 replies, 1 repost, 11 likes, 1 bookmarks, 1111 views',
                '1 replies, 1 repost, 111 likes, 1 bookmarks, 11111 views',
                '1 replies, 111 likes, 1 bookmarks, 11111 views',
                '1 reply, 1 like, 11 views',
                '1 reply, 1 likes, 1 bookmark, 111 views',
                '1 reply, 1 likes, 11 views',
                '1 reply, 1 likes, 111 views',
                '1 reply, 11 views',
                '1 reply, 111 views',
                '1 repost, 1 like, 111 views',
                '1 repost, 11 likes, 111 views',
                '1 repost. Repost',
                '1 reposts. Repost',
                '11 Likes. Like',
                '11 Replies. Reply',
                '11 likes, 1 bookmark, 111 views',
                '11 likes, 1111 views',
                '11 replies, 1 likes, 111 views',
                '11 replies, 1 reposts, 111 likes, 1 bookmarks, 11111 views',
                '11 replies, 11 reposts, 111 likes, 11 bookmarks, 1111 views',
                '11 replies, 11 reposts, 111 likes, 111 bookmarks, 11111 views',
                '11 replies, 111 reposts, 1111 likes, 111 bookmarks, 11111 views',
                '11 reposts. Repost',
                '11 views',
                '11 views. View post analytics',
                '111 Likes. Like',
                '111 Replies. Reply',
                '111 replies, 11 reposts, 111 likes, 111 bookmarks, 111111 views',
                '111 replies, 11 reposts, 1111 likes, 111 bookmarks, 111111 views',
                '111 reposts. Repost',
                '111 views',
                '111 views. View post analytics',
                '1111 Likes. Like',
                '1111 Replies. Reply',
                '1111 views. View post analytics',
                '11111 views. View post analytics',
                '111111 views. View post analytics',
                '1111111 views. View post analytics',
                'Account menu',
                'Back',
                'Bookmark',
                'Calls',
                'Creator Studio',
                'Embedded video',
                'Follow',
                'Grok',
                'Grok actions',
                'History',
                'Home',
                'Image',
                'Jan 1',
                'Money',
                'More',
                'Notifications',
                'Post',
                'Premium',
                'Primary',
                'Profile',
                'Search',
                'Share post',
                'Timeline: text',
                'Verified account',
                'text'},
 'aria-labelledby': {('id__1 id__2 id__3 id__4 id__5 id__6 id__7 id__8 id__9 id__10 id__11 id__12 id__13 '
                     'id__14 id__15 id__16 id__17 id__18 id__19 id__20'),
                     'id__1015 id__1016',
                     'id__1017',
                     ('id__1018 id__1019 id__1020 id__1021 id__1022 id__1023 id__1024 id__1025 id__1026 '
                     'id__1027 id__1028 id__1029 id__1030 id__1031 id__1032 id__1033 id__1034 id__1035 '
                     'id__1036 id__1037'),
                     'id__1038 id__1039',
                     ('id__1040 id__1041 id__1042 id__1043 id__1044 id__1045 id__1046 id__1047 id__1048 '
                     'id__1049 id__1050 id__1051 id__1052 id__1053 id__1054 id__1055 id__1056 id__1057 '
                     'id__1058 id__1059'),
                     'id__1060',
                     'id__109 id__110',
                     ('id__111 id__112 id__113 id__114 id__115 id__116 id__117 id__118 id__119 id__120 '
                     'id__121 id__122 id__123 id__124 id__125 id__126 id__127 id__128 id__129 id__130'),
                     ('id__131 id__132 id__133 id__134 id__135 id__136 id__137 id__138 id__139 id__140 '
                     'id__141 id__142 id__143 id__144 id__145 id__146 id__147 id__148 id__149'),
                     ('id__151 id__152 id__153 id__154 id__155 id__156 id__157 id__158 id__159 id__160 '
                     'id__161 id__162 id__163 id__164 id__165 id__166 id__167 id__168 id__169 id__170'),
                     'id__171 id__172',
                     ('id__173 id__174 id__175 id__176 id__177 id__178 id__179 id__180 id__181 id__182 '
                     'id__183 id__184 id__185 id__186 id__187 id__188 id__189 id__190 id__191 id__192'),
                     'id__193 id__194',
                     ('id__195 id__196 id__197 id__198 id__199 id__200 id__201 id__202 id__203 id__204 '
                     'id__205 id__206 id__207 id__208 id__209 id__210 id__211 id__212 id__213 id__214'),
                     'id__21 id__22',
                     ('id__215 id__216 id__217 id__218 id__219 id__220 id__221 id__222 id__223 id__224 '
                     'id__225 id__226 id__227 id__228 id__229 id__230 id__231 id__232 id__233'),
                     ('id__23 id__24 id__25 id__26 id__27 id__28 id__29 id__30 id__31 id__32 id__33 id__34 '
                     'id__35 id__36 id__37 id__38 id__39 id__40 id__41 id__42'),
                     ('id__235 id__236 id__237 id__238 id__239 id__240 id__241 id__242 id__243 id__244 '
                     'id__245 id__246 id__247 id__248 id__249 id__250 id__251 id__252 id__253 id__254'),
                     'id__255 id__256',
                     ('id__257 id__258 id__259 id__260 id__261 id__262 id__263 id__264 id__265 id__266 '
                     'id__267 id__268 id__269 id__270 id__271 id__272 id__273 id__274 id__275 id__276'),
                     ('id__277 id__278 id__279 id__280 id__281 id__282 id__283 id__284 id__285 id__286 '
                     'id__287 id__288 id__289 id__290 id__291 id__292 id__293 id__294 id__295 id__296'),
                     ('id__297 id__298 id__299 id__300 id__301 id__302 id__303 id__304 id__305 id__306 '
                     'id__307 id__308 id__309 id__310 id__311 id__312 id__313 id__314 id__315 id__316'),
                     ('id__317 id__318 id__319 id__320 id__321 id__322 id__323 id__324 id__325 id__326 '
                     'id__327 id__328 id__329 id__330 id__331 id__332 id__333 id__334 id__335'),
                     ('id__337 id__338 id__339 id__340 id__341 id__342 id__343 id__344 id__345 id__346 '
                     'id__347 id__348 id__349 id__350 id__351 id__352 id__353 id__354 id__355 id__356'),
                     ('id__357 id__358 id__359 id__360 id__361 id__362 id__363 id__364 id__365 id__366 '
                     'id__367 id__368 id__369 id__370 id__371 id__372 id__373 id__374 id__375 id__376'),
                     'id__377 id__378',
                     ('id__379 id__380 id__381 id__382 id__383 id__384 id__385 id__386 id__387 id__388 '
                     'id__389 id__390 id__391 id__392 id__393 id__394 id__395 id__396 id__397 id__398'),
                     ('id__399 id__400 id__401 id__402 id__403 id__404 id__405 id__406 id__407 id__408 '
                     'id__409 id__410 id__411 id__412 id__413 id__414 id__415 id__416 id__417 id__418'),
                     ('id__419 id__420 id__421 id__422 id__423 id__424 id__425 id__426 id__427 id__428 '
                     'id__429 id__430 id__431 id__432 id__433 id__434 id__435 id__436 id__437 id__438'),
                     'id__43 id__44',
                     ('id__439 id__440 id__441 id__442 id__443 id__444 id__445 id__446 id__447 id__448 '
                     'id__449 id__450 id__451 id__452 id__453 id__454 id__455 id__456 id__457'),
                     ('id__45 id__46 id__47 id__48 id__49 id__50 id__51 id__52 id__53 id__54 id__55 id__56 '
                     'id__57 id__58 id__59 id__60 id__61 id__62 id__63 id__64'),
                     ('id__459 id__460 id__461 id__462 id__463 id__464 id__465 id__466 id__467 id__468 '
                     'id__469 id__470 id__471 id__472 id__473 id__474 id__475 id__476 id__477 id__478'),
                     'id__479 id__480',
                     ('id__481 id__482 id__483 id__484 id__485 id__486 id__487 id__488 id__489 id__490 '
                     'id__491 id__492 id__493 id__494 id__495 id__496 id__497 id__498 id__499'),
                     ('id__501 id__502 id__503 id__504 id__505 id__506 id__507 id__508 id__509 id__510 '
                     'id__511 id__512 id__513 id__514 id__515 id__516 id__517 id__518 id__519 id__520'),
                     ('id__521 id__522 id__523 id__524 id__525 id__526 id__527 id__528 id__529 id__530 '
                     'id__531 id__532 id__533 id__534 id__535 id__536 id__537 id__538 id__539 id__540'),
                     ('id__541 id__542 id__543 id__544 id__545 id__546 id__547 id__548 id__549 id__550 '
                     'id__551 id__552 id__553 id__554 id__555 id__556 id__557 id__558 id__559 id__560'),
                     ('id__561 id__562 id__563 id__564 id__565 id__566 id__567 id__568 id__569 id__570 '
                     'id__571 id__572 id__573 id__574 id__575 id__576 id__577 id__578 id__579 id__580'),
                     ('id__581 id__582 id__583 id__584 id__585 id__586 id__587 id__588 id__589 id__590 '
                     'id__591 id__592 id__593 id__594 id__595 id__596 id__597 id__598 id__599 id__600'),
                     'id__601 id__602',
                     ('id__603 id__604 id__605 id__606 id__607 id__608 id__609 id__610 id__611 id__612 '
                     'id__613 id__614 id__615 id__616 id__617 id__618 id__619 id__620 id__621 id__622'),
                     ('id__623 id__624 id__625 id__626 id__627 id__628 id__629 id__630 id__631 id__632 '
                     'id__633 id__634 id__635 id__636 id__637 id__638 id__639 id__640 id__641 id__642'),
                     'id__643 id__644',
                     ('id__645 id__646 id__647 id__648 id__649 id__650 id__651 id__652 id__653 id__654 '
                     'id__655 id__656 id__657 id__658 id__659 id__660 id__661 id__662 id__663'),
                     'id__65 id__66',
                     ('id__665 id__666 id__667 id__668 id__669 id__670 id__671 id__672 id__673 id__674 '
                     'id__675 id__676 id__677 id__678 id__679 id__680 id__681 id__682 id__683 id__684'),
                     ('id__67 id__68 id__69 id__70 id__71 id__72 id__73 id__74 id__75 id__76 id__77 id__78 '
                     'id__79 id__80 id__81 id__82 id__83 id__84 id__85 id__86'),
                     ('id__685 id__686 id__687 id__688 id__689 id__690 id__691 id__692 id__693 id__694 '
                     'id__695 id__696 id__697 id__698 id__699 id__700 id__701 id__702 id__703 id__704'),
                     ('id__705 id__706 id__707 id__708 id__709 id__710 id__711 id__712 id__713 id__714 '
                     'id__715 id__716 id__717 id__718 id__719 id__720 id__721 id__722 id__723 id__724'),
                     ('id__725 id__726 id__727 id__728 id__729 id__730 id__731 id__732 id__733 id__734 '
                     'id__735 id__736 id__737 id__738 id__739 id__740 id__741 id__742 id__743'),
                     ('id__745 id__746 id__747 id__748 id__749 id__750 id__751 id__752 id__753 id__754 '
                     'id__755 id__756 id__757 id__758 id__759 id__760 id__761 id__762 id__763 id__764'),
                     'id__765 id__766',
                     'id__767 id__768',
                     ('id__769 id__770 id__771 id__772 id__773 id__774 id__775 id__776 id__777 id__778 '
                     'id__779 id__780 id__781 id__782 id__783 id__784 id__785 id__786 id__787 id__788'),
                     ('id__789 id__790 id__791 id__792 id__793 id__794 id__795 id__796 id__797 id__798 '
                     'id__799 id__800 id__801 id__802 id__803 id__804 id__805 id__806 id__807 id__808'),
                     ('id__809 id__810 id__811 id__812 id__813 id__814 id__815 id__816 id__817 id__818 '
                     'id__819 id__820 id__821 id__822 id__823 id__824 id__825 id__826 id__827 id__828'),
                     'id__829 id__830',
                     ('id__831 id__832 id__833 id__834 id__835 id__836 id__837 id__838 id__839 id__840 '
                     'id__841 id__842 id__843 id__844 id__845 id__846 id__847 id__848 id__849 id__850'),
                     ('id__851 id__852 id__853 id__854 id__855 id__856 id__857 id__858 id__859 id__860 '
                     'id__861 id__862 id__863 id__864 id__865 id__866 id__867 id__868 id__869 id__870'),
                     'id__87 id__88',
                     ('id__871 id__872 id__873 id__874 id__875 id__876 id__877 id__878 id__879 id__880 '
                     'id__881 id__882 id__883 id__884 id__885 id__886 id__887 id__888 id__889 id__890'),
                     ('id__89 id__90 id__91 id__92 id__93 id__94 id__95 id__96 id__97 id__98 id__99 id__100 '
                     'id__101 id__102 id__103 id__104 id__105 id__106 id__107 id__108'),
                     ('id__891 id__892 id__893 id__894 id__895 id__896 id__897 id__898 id__899 id__900 '
                     'id__901 id__902 id__903 id__904 id__905 id__906 id__907 id__908 id__909 id__910'),
                     'id__911 id__912',
                     ('id__913 id__914 id__915 id__916 id__917 id__918 id__919 id__920 id__921 id__922 '
                     'id__923 id__924 id__925 id__926 id__927 id__928 id__929 id__930 id__931'),
                     ('id__933 id__934 id__935 id__936 id__937 id__938 id__939 id__940 id__941 id__942 '
                     'id__943 id__944 id__945 id__946 id__947 id__948 id__949 id__950 id__951 id__952'),
                     ('id__953 id__954 id__955 id__956 id__957 id__958 id__959 id__960 id__961 id__962 '
                     'id__963 id__964 id__965 id__966 id__967 id__968 id__969 id__970 id__971 id__972'),
                     ('id__973 id__974 id__975 id__976 id__977 id__978 id__979 id__980 id__981 id__982 '
                     'id__983 id__984 id__985 id__986 id__987 id__988 id__989 id__990 id__991 id__992'),
                     'id__993 id__994',
                     ('id__995 id__996 id__997 id__998 id__999 id__1000 id__1001 id__1002 id__1003 id__1004 '
                     'id__1005 id__1006 id__1007 id__1008 id__1009 id__1010 id__1011 id__1012 id__1013 '
                     'id__1014')},
 'aria-live': {'polite'},
 'aria-selected': {'true', 'false'},
 'data-testid': {'AppTabBar_Calls_Link',
                 'AppTabBar_DirectMessage_Link',
                 'AppTabBar_Explore_Link',
                 'AppTabBar_Follow_Link',
                 'AppTabBar_Home_Link',
                 'AppTabBar_More_Menu',
                 'AppTabBar_Notifications_Link',
                 'AppTabBar_Profile_Link',
                 'Dropdown',
                 'ScrollSnap-List',
                 'ScrollSnap-SwipeableList',
                 'ScrollSnap-nextButtonWrapper',
                 'ScrollSnap-prevButtonWrapper',
                 'SideNav_AccountSwitcher_Button',
                 'SideNav_NewTweet_Button',
                 'Tweet-User-Avatar',
                 'User-Name',
                 'UserAvatar-Container-OwnUser',
                 'UserAvatar-Container-user01',
                 'UserAvatar-Container-user03',
                 'UserAvatar-Container-user04',
                 'UserAvatar-Container-user06',
                 'UserAvatar-Container-user07',
                 'UserAvatar-Container-user08',
                 'UserAvatar-Container-user09',
                 'UserAvatar-Container-user11',
                 'UserAvatar-Container-user12',
                 'UserAvatar-Container-user13',
                 'UserAvatar-Container-user14',
                 'UserAvatar-Container-user16',
                 'UserAvatar-Container-user17',
                 'UserAvatar-Container-user18',
                 'UserAvatar-Container-user19',
                 'UserAvatar-Container-user20',
                 'UserAvatar-Container-user21',
                 'UserAvatar-Container-user22',
                 'UserAvatar-Container-user23',
                 'UserAvatar-Container-user24',
                 'UserAvatar-Container-user25',
                 'UserAvatar-Container-user26',
                 'UserAvatar-Container-user27',
                 'UserAvatar-Container-user28',
                 'UserAvatar-Container-user29',
                 'UserAvatar-Container-user30',
                 'UserAvatar-Container-user31',
                 'UserAvatar-Container-user32',
                 'UserAvatar-Container-user33',
                 'UserAvatar-Container-user34',
                 'UserAvatar-Container-user35',
                 'UserAvatar-Container-user36',
                 'UserAvatar-Container-user37',
                 'UserAvatar-Container-user38',
                 'UserAvatar-Container-user39',
                 'UserDescription',
                 'UserJoinDate',
                 'UserName',
                 'UserProfileHeader_Items',
                 'UserUrl',
                 'analytics',
                 'app-bar-back',
                 'app-text-transition-container',
                 'birdwatch-pivot',
                 'bookmark',
                 'card.layoutLarge.media',
                 'card.wrapper',
                 'caret',
                 'cellInnerDiv',
                 'editProfileButton',
                 'highlightUpsell',
                 'icon-birdwatch-fill',
                 'icon-verified',
                 'like',
                 'ownProfilePaymentsButton',
                 'pillLabel',
                 'pin',
                 'placementTracking',
                 'premium-signup-tab',
                 'primaryColumn',
                 'reply',
                 'retweet',
                 'socialContext',
                 'testCondensedMedia',
                 'tweet',
                 'tweet-text-show-more-link',
                 'tweetEngagements',
                 'tweetPhoto',
                 'tweetText',
                 'unlike',
                 'unretweet',
                 'unretweetConfirm',
                 'videoComponent',
                 'videoPlayer'},
 'datetime': {'age:10633986',
              'age:10699021',
              'age:111245',
              'age:1136422',
              'age:1197560',
              'age:1217509',
              'age:1217596',
              'age:1220045',
              'age:1242578',
              'age:1252720',
              'age:1286923',
              'age:1320841',
              'age:1338008',
              'age:1418239',
              'age:1498563',
              'age:151923',
              'age:1586758',
              'age:158878',
              'age:160010',
              'age:160750',
              'age:162147',
              'age:163362',
              'age:165324',
              'age:192224',
              'age:196515',
              'age:199675',
              'age:216129',
              'age:241774',
              'age:241946',
              'age:263433',
              'age:30933356',
              'age:31337632',
              'age:31754859',
              'age:320980',
              'age:362357',
              'age:3652618',
              'age:365437',
              'age:3663948',
              'age:3714759',
              'age:3726089',
              'age:373206',
              'age:378774',
              'age:384161',
              'age:408868',
              'age:419472',
              'age:434601',
              'age:441432',
              'age:448390',
              'age:453169',
              'age:459417',
              'age:460739',
              'age:477309',
              'age:496970',
              'age:503102',
              'age:512873',
              'age:514103',
              'age:588721',
              'age:597159',
              'age:6130975',
              'age:6159312',
              'age:6193116',
              'age:632291',
              'age:654610',
              'age:730154',
              'age:770171'},
 'dir': {'ltr', 'auto'},
 'disabled': {''},
 'href': {'/OwnUser/status/1900000000000000051', '/user34/status/1900000000000000052', '/OwnUser',
          '/OwnUser/about',
          '/OwnUser/following',
          '/OwnUser/header_photo',
          '/OwnUser/media',
          '/OwnUser/photo',
          '/OwnUser/reposts',
          '/OwnUser/status/1900000000000000001',
          '/OwnUser/status/1900000000000000001/analytics',
          '/OwnUser/status/1900000000000000002',
          '/OwnUser/status/1900000000000000002/analytics',
          '/OwnUser/status/1900000000000000004',
          '/OwnUser/status/1900000000000000004/analytics',
          '/OwnUser/status/1900000000000000005',
          '/OwnUser/status/1900000000000000005/analytics',
          '/OwnUser/status/1900000000000000005/photo/1',
          '/OwnUser/status/1900000000000000006',
          '/OwnUser/status/1900000000000000006/analytics',
          '/OwnUser/status/1900000000000000006/photo/1',
          '/OwnUser/status/1900000000000000009',
          '/OwnUser/status/1900000000000000009/analytics',
          '/OwnUser/status/1900000000000000012',
          '/OwnUser/status/1900000000000000012/analytics',
          '/OwnUser/status/1900000000000000012/photo/1',
          '/OwnUser/status/1900000000000000014',
          '/OwnUser/status/1900000000000000014/analytics',
          '/OwnUser/status/1900000000000000017',
          '/OwnUser/status/1900000000000000017/analytics',
          '/OwnUser/status/1900000000000000019',
          '/OwnUser/status/1900000000000000019/analytics',
          '/OwnUser/status/1900000000000000020',
          '/OwnUser/status/1900000000000000020/analytics',
          '/OwnUser/status/1900000000000000022',
          '/OwnUser/status/1900000000000000022/analytics',
          '/OwnUser/status/1900000000000000022/photo/1',
          '/OwnUser/status/1900000000000000024',
          '/OwnUser/status/1900000000000000024/analytics',
          '/OwnUser/status/1900000000000000026',
          '/OwnUser/status/1900000000000000026/analytics',
          '/OwnUser/status/1900000000000000028',
          '/OwnUser/status/1900000000000000028/analytics',
          '/OwnUser/status/1900000000000000030',
          '/OwnUser/status/1900000000000000030/analytics',
          '/OwnUser/status/1900000000000000032',
          '/OwnUser/status/1900000000000000032/analytics',
          '/OwnUser/status/1900000000000000035',
          '/OwnUser/status/1900000000000000035/analytics',
          '/OwnUser/status/1900000000000000037',
          '/OwnUser/status/1900000000000000037/analytics',
          '/OwnUser/status/1900000000000000039',
          '/OwnUser/status/1900000000000000039/analytics',
          '/OwnUser/status/1900000000000000041',
          '/OwnUser/status/1900000000000000041/analytics',
          '/OwnUser/status/1900000000000000043',
          '/OwnUser/status/1900000000000000043/analytics',
          '/OwnUser/status/1900000000000000044',
          '/OwnUser/status/1900000000000000044/analytics',
          '/OwnUser/status/1900000000000000046',
          '/OwnUser/status/1900000000000000046/analytics',
          '/OwnUser/status/1900000000000000048',
          '/OwnUser/status/1900000000000000048/analytics',
          '/OwnUser/status/1900000000000000050',
          '/OwnUser/status/1900000000000000050/analytics',
          '/OwnUser/status/1900000000000000051/analytics',
          '/OwnUser/status/1900000000000000051/quotes',
          '/OwnUser/verified_followers',
          '/OwnUser/with_replies',
          '/compose/post',
          '/explore',
          '/hashtag/tag',
          '/home',
          '/i/calls',
          '/i/chat',
          '/i/communitynotes/noterequest/1900000000000000051',
          '/i/connect_people',
          '/i/grok',
          '/i/history',
          '/i/jf/creators/studio',
          '/i/lists/add_member',
          '/i/money',
          '/i/premium_sign_up',
          '/i/verified-choose',
          '/notifications',
          '/settings/profile',
          '/user03/status/1900000000000000003/photo/1',
          '/user06/status/1900000000000000007/photo/1',
          '/user07',
          '/user07/status/1900000000000000008',
          '/user07/status/1900000000000000008/analytics',
          '/user08',
          '/user08/status/1900000000000000010',
          '/user08/status/1900000000000000010/analytics',
          '/user09/status/1900000000000000011/photo/1',
          '/user11',
          '/user11/status/1900000000000000013',
          '/user11/status/1900000000000000013/analytics',
          '/user12',
          '/user12/status/1900000000000000015',
          '/user12/status/1900000000000000015/analytics',
          '/user13/status/1900000000000000016/photo/1',
          '/user14',
          '/user14/status/1900000000000000018',
          '/user14/status/1900000000000000018/analytics',
          '/user15',
          '/user16',
          '/user16/status/1900000000000000021',
          '/user16/status/1900000000000000021/analytics',
          '/user17',
          '/user17/status/1900000000000000023',
          '/user17/status/1900000000000000023/analytics',
          '/user18',
          '/user18/status/1900000000000000025',
          '/user18/status/1900000000000000025/analytics',
          '/user19',
          '/user19/status/1900000000000000027',
          '/user19/status/1900000000000000027/analytics',
          '/user21',
          '/user21/status/1900000000000000029',
          '/user21/status/1900000000000000029/analytics',
          '/user22',
          '/user22/status/1900000000000000031',
          '/user22/status/1900000000000000031/analytics',
          '/user23',
          '/user23/status/1900000000000000033',
          '/user23/status/1900000000000000033/analytics',
          '/user24/status/1900000000000000034/photo/1',
          '/user25',
          '/user25/status/1900000000000000036',
          '/user25/status/1900000000000000036/analytics',
          '/user27',
          '/user27/status/1900000000000000038',
          '/user27/status/1900000000000000038/analytics',
          '/user28',
          '/user28/status/1900000000000000040',
          '/user28/status/1900000000000000040/analytics',
          '/user28/status/1900000000000000042',
          '/user28/status/1900000000000000042/analytics',
          '/user29',
          '/user29/status/1900000000000000045',
          '/user29/status/1900000000000000045/analytics',
          '/user29/status/1900000000000000045/photo/1',
          '/user31',
          '/user31/status/1900000000000000047',
          '/user31/status/1900000000000000047/analytics',
          '/user32',
          '/user32/status/1900000000000000049',
          '/user32/status/1900000000000000049/analytics',
          '/user34',
          '/user34/status/1900000000000000052/analytics',
          '/user35',
          '/user35/status/1900000000000000053',
          '/user35/status/1900000000000000053/analytics',
          '/user36',
          '/user36/status/1900000000000000054',
          '/user36/status/1900000000000000054/analytics',
          '/user37/status/1900000000000000055/photo/1',
          '/user38',
          '/user38/status/1900000000000000056',
          '/user38/status/1900000000000000056/analytics',
          '/user38/status/1900000000000000056/photo/1',
          '/user38/status/1900000000000000056/photo/2',
          '/user38/status/1900000000000000056/photo/3',
          '/user39/status/1900000000000000057/photo/1',
          'https://example.com/link'},
 'href[0]': {'OwnUser',
             'compose',
             'explore',
             'hashtag',
             'home',
             'i',
             'notifications',
             'settings',
             'user03',
             'user06',
             'user07',
             'user08',
             'user09',
             'user11',
             'user12',
             'user13',
             'user14',
             'user15',
             'user16',
             'user17',
             'user18',
             'user19',
             'user21',
             'user22',
             'user23',
             'user24',
             'user25',
             'user27',
             'user28',
             'user29',
             'user31',
             'user32',
             'user34',
             'user35',
             'user36',
             'user37',
             'user38',
             'user39'},
 'href[1]': {'about',
             'calls',
             'chat',
             'communitynotes',
             'connect_people',
             'following',
             'grok',
             'header_photo',
             'history',
             'jf',
             'lists',
             'media',
             'money',
             'photo',
             'post',
             'premium_sign_up',
             'profile',
             'reposts',
             'status',
             'tag',
             'verified-choose',
             'verified_followers',
             'with_replies'},
 'href[2]': {'1900000000000000001',
             '1900000000000000002',
             '1900000000000000003',
             '1900000000000000004',
             '1900000000000000005',
             '1900000000000000006',
             '1900000000000000007',
             '1900000000000000008',
             '1900000000000000009',
             '1900000000000000010',
             '1900000000000000011',
             '1900000000000000012',
             '1900000000000000013',
             '1900000000000000014',
             '1900000000000000015',
             '1900000000000000016',
             '1900000000000000017',
             '1900000000000000018',
             '1900000000000000019',
             '1900000000000000020',
             '1900000000000000021',
             '1900000000000000022',
             '1900000000000000023',
             '1900000000000000024',
             '1900000000000000025',
             '1900000000000000026',
             '1900000000000000027',
             '1900000000000000028',
             '1900000000000000029',
             '1900000000000000030',
             '1900000000000000031',
             '1900000000000000032',
             '1900000000000000033',
             '1900000000000000034',
             '1900000000000000035',
             '1900000000000000036',
             '1900000000000000037',
             '1900000000000000038',
             '1900000000000000039',
             '1900000000000000040',
             '1900000000000000041',
             '1900000000000000042',
             '1900000000000000043',
             '1900000000000000044',
             '1900000000000000045',
             '1900000000000000046',
             '1900000000000000047',
             '1900000000000000048',
             '1900000000000000049',
             '1900000000000000050',
             '1900000000000000051',
             '1900000000000000052',
             '1900000000000000053',
             '1900000000000000054',
             '1900000000000000055',
             '1900000000000000056',
             '1900000000000000057',
             'add_member',
             'creators',
             'noterequest'},
 'href[3]': {'studio', '1900000000000000051', 'quotes', 'photo', 'analytics'},
 'href[4]': {'3', '1', '2'},
 'id': {'id__1007',
        'id__101',
        'id__1014',
        'id__1015',
        'id__1017',
        'id__1022',
        'id__1028',
        'id__1030',
        'id__1037',
        'id__1038',
        'id__1044',
        'id__1060',
        'id__108',
        'id__109',
        'id__11',
        'id__115',
        'id__121',
        'id__13',
        'id__130',
        'id__135',
        'id__141',
        'id__150',
        'id__155',
        'id__161',
        'id__163',
        'id__170',
        'id__171',
        'id__177',
        'id__183',
        'id__185',
        'id__192',
        'id__199',
        'id__20',
        'id__205',
        'id__21',
        'id__214',
        'id__219',
        'id__225',
        'id__234',
        'id__239',
        'id__245',
        'id__247',
        'id__254',
        'id__255',
        'id__261',
        'id__267',
        'id__27',
        'id__276',
        'id__281',
        'id__287',
        'id__296',
        'id__301',
        'id__307',
        'id__316',
        'id__321',
        'id__326',
        'id__327',
        'id__33',
        'id__336',
        'id__341',
        'id__347',
        'id__35',
        'id__356',
        'id__361',
        'id__367',
        'id__369',
        'id__376',
        'id__383',
        'id__389',
        'id__398',
        'id__403',
        'id__409',
        'id__418',
        'id__42',
        'id__423',
        'id__429',
        'id__43',
        'id__438',
        'id__443',
        'id__449',
        'id__458',
        'id__463',
        'id__469',
        'id__471',
        'id__478',
        'id__479',
        'id__485',
        'id__49',
        'id__491',
        'id__5',
        'id__500',
        'id__505',
        'id__511',
        'id__520',
        'id__525',
        'id__531',
        'id__540',
        'id__545',
        'id__55',
        'id__551',
        'id__560',
        'id__565',
        'id__57',
        'id__571',
        'id__580',
        'id__585',
        'id__591',
        'id__593',
        'id__600',
        'id__601',
        'id__607',
        'id__613',
        'id__622',
        'id__627',
        'id__633',
        'id__635',
        'id__64',
        'id__642',
        'id__643',
        'id__649',
        'id__65',
        'id__655',
        'id__664',
        'id__669',
        'id__675',
        'id__684',
        'id__689',
        'id__695',
        'id__704',
        'id__709',
        'id__71',
        'id__715',
        'id__724',
        'id__729',
        'id__735',
        'id__744',
        'id__749',
        'id__755',
        'id__757',
        'id__764',
        'id__766',
        'id__767',
        'id__768',
        'id__77',
        'id__773',
        'id__779',
        'id__788',
        'id__79',
        'id__793',
        'id__799',
        'id__808',
        'id__813',
        'id__819',
        'id__821',
        'id__828',
        'id__829',
        'id__835',
        'id__841',
        'id__850',
        'id__855',
        'id__86',
        'id__861',
        'id__870',
        'id__875',
        'id__881',
        'id__890',
        'id__895',
        'id__901',
        'id__903',
        'id__910',
        'id__911',
        'id__917',
        'id__923',
        'id__93',
        'id__932',
        'id__936',
        'id__937',
        'id__943',
        'id__952',
        'id__956',
        'id__957',
        'id__963',
        'id__972',
        'id__976',
        'id__977',
        'id__983',
        'id__985',
        'id__99',
        'id__992',
        'id__993',
        'id__998',
        'id__999'},
 'role': {'article',
          'banner',
          'button',
          'group',
          'heading',
          'img',
          'link',
          'menu',
          'menuitem',
          'navigation',
          'none',
          'presentation',
          'region',
          'status',
          'tab',
          'tablist'},
 'tabindex': {'0', '-1'},
 'text': {'1',
          '1.1K',
          '1.1M',
          '11',
          '111',
          '111K',
          '11K',
          '1K',
          '@OwnUser',
          '@user01',
          '@user02',
          '@user03',
          '@user04',
          '@user05',
          '@user06',
          '@user07',
          '@user08',
          '@user09',
          '@user10',
          '@user11',
          '@user12',
          '@user13',
          '@user14',
          '@user15',
          '@user16',
          '@user17',
          '@user18',
          '@user19',
          '@user20',
          '@user21',
          '@user22',
          '@user23',
          '@user24',
          '@user25',
          '@user26',
          '@user27',
          '@user28',
          '@user29',
          '@user30',
          '@user31',
          '@user32',
          '@user33',
          '@user34',
          '@user35',
          '@user36',
          '@user37',
          '@user38',
          '@user39',
          'Add/remove from Lists',
          'Calls',
          'Chat',
          'Creator Studio',
          'Delete',
          'Edit profile',
          'Embed post',
          'Explore',
          'Follow',
          'Followers',
          'Following',
          'Grok',
          'Highlight on your profile',
          'History',
          'Home',
          'Jan 1',
          'Media',
          'Money',
          'More',
          'Notifications',
          'Pin to your profile',
          'Post',
          'Posts',
          'Premium',
          'Profile',
          'Quote',
          'Replies',
          'Replying to',
          'Reposts',
          'Request Community Note',
          'Show more',
          'Undo repost',
          'View post activity',
          'View post analytics',
          'You reposted',
          'text',
          '·'},
 'type': {'button'},
 'whitespace': {' \n', '\n', ' '}}


def fixed_value(value, channel):
    return require(value, FIXED_VALUES[channel], channel)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"json: duplicate key {key!r}")
        result[key] = value
    return result

KEEP = ATTRIBUTE_RULES.keys()


def attribute(name, value, m):
    rule = ATTRIBUTE_RULES[name]
    try:
        if callable(rule):
            value = rule(value, m)
            return fixed_value(value, name) if m.is_fixed else value
        if isinstance(rule, tuple):
            if m.is_fixed:
                require(value, rule[1], name)
                fixed_value(value, name)
            value = rule[0](value, m)
            rule = rule[1]
        value = require(value, rule, name)
        return fixed_value(value, name) if m.is_fixed else value
    except ValueError as e:
        raise ValueError(f'{name}: {e}') from e


class Sanitizer(HTMLParser):
    def __init__(self, m):
        super().__init__(convert_charrefs=True)
        self.m = m
        self.out = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if self.skip or tag in DROP or tag not in TAGS:
            if tag not in VOID:
                self.skip += 1
            return
        kept = []
        names = [name for name, _ in attrs]
        if len(names) != len(set(names)):
            raise ValueError('duplicate attribute')
        for name, value in attrs:
            if name not in KEEP:
                continue
            value = attribute(name, '' if value is None else value, self.m)
            kept.append(f' {name}="{escape(value, quote=True)}"')
        self.out.append(f'<{tag}{"".join(kept)}>')

    def handle_startendtag(self, tag, attrs):
        if self.skip or tag in DROP or tag not in TAGS:
            return
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.out.append(f'</{tag}>')

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        if self.skip:
            self.skip -= 1
            return
        if tag in TAGS:
            self.out.append(f'</{tag}>')

    def handle_data(self, data):
        if not self.skip:
            self.out.append(escape(map_text(data, self.m), quote=False))


def clean(html, m):
    s = Sanitizer(m)
    s.feed(html)
    # Fixed mode rejects unparsed input; raw mode drops the entire unparsed tail.
    if m.is_fixed and s.rawdata:
        raise ValueError('html: incomplete markup')
    if not m.is_fixed:
        s.rawdata = ''
    s.close()
    return ''.join(s.out)

