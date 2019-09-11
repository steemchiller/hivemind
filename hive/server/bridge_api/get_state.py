"""Routes then builds a get_state response object"""

#pylint: disable=line-too-long,too-many-lines
import logging
from collections import OrderedDict
import ujson as json

from hive.server.common.mutes import Mutes

from hive.server.hive_api.community import if_tag_community, list_all_subscriptions
from hive.server.hive_api.common import get_account_id

from hive.server.bridge_api.objects import (
    load_accounts,
    load_posts,
    load_posts_keyed,
    load_posts_reblogs)
from hive.server.common.helpers import (
    ApiError,
    return_error_info,
    valid_account,
    valid_permlink,
    valid_sort,
    valid_tag)
from hive.server.bridge_api.tags import (
    get_top_trending_tags_summary)

import hive.server.bridge_api.cursor as cursor

log = logging.getLogger(__name__)

# steemd account 'tabs' - specific post list queries
ACCOUNT_TAB_KEYS = {
    'null': None,
    'blog': 'blog',
    'feed': 'feed',
    'comments': 'comments',
    'recent-replies': 'recent_replies'}

# post list sorts
POST_LIST_SORTS = [
    'trending',
    'promoted',
    'hot',
    'created',
    'payout',
    'payout_comments',
]

@return_error_info
async def get_state(context, path, observer=None):
    """`get_state` reimplementation.

    See: https://github.com/steemit/steem/blob/06e67bd4aea73391123eca99e1a22a8612b0c47e/libraries/app/database_api.cpp#L1937
    """
    # pylint: disable=too-many-branches,too-many-locals,too-many-statements
    (path, part) = _normalize_path(path)
    parts = len(part)

    db = context['db']
    observer_id = await get_account_id(db, observer) if observer else None

    state = {
        'feed_price': await _get_feed_price(db),
        'props': await _get_props_lite(db),
        'tags': {},
        'accounts': {},
        'content': {},
        'tag_idx': {'trending': []},
        'discussion_idx': {"": {}}, # {tag: sort: [keys]}
        'community': {}}

    # account - `/@account/tab` (feed, blog, comments, replies)
    if parts == 2 and part[0][0] == '@' and part[1] in ACCOUNT_TAB_KEYS:
        account = valid_account(part[0][1:])
        state['accounts'][account] = await _load_account(db, account, observer_id)

        key = ACCOUNT_TAB_KEYS[part[1]]
        if key:
            posts = await _get_account_discussion_by_key(db, account, key)
            state['content'] = _keyed_posts(posts)
            state['accounts'][account][key] = list(state['content'].keys())
            state['tag_idx']['trending'] = await get_top_trending_tags_summary(context, 10)

    # discussion - `/category/@account/permlink`
    elif parts == 3 and part[1][0] == '@':
        tag = part[0]
        author = valid_account(part[1][1:])
        permlink = valid_permlink(part[2])

        state['content'] = await _load_discussion(db, author, permlink)
        state['accounts'] = await _load_content_accounts(db, state['content'], observer_id)

        community = await if_tag_community(context, tag, observer)
        if community:
            ref = author + '/' + permlink
            assert state['content'][ref]['category'] == tag, 'invalid comm url'
            state['community'] = {tag: community}

    # ranked posts - `/sort/category`
    elif parts <= 2 and part[0] in POST_LIST_SORTS:
        sort = valid_sort(part[0])
        tag = valid_tag(part[1]) if parts == 2 else ''

        community = await if_tag_community(context, tag, observer)
        if community: state['community'] = {tag: community}

        pids = await cursor.pids_by_ranked(db, sort, '', '', 20, tag, observer_id)
        state['content'] = _keyed_posts(await load_posts(db, pids))
        state['discussion_idx'] = {tag: {sort: list(state['content'].keys())}}
        state['tag_idx']['trending'] = await get_top_trending_tags_summary(context, 10)

    else:
        raise ApiError("invalid path /%s" % path)

    # ensure all comms objs are available
    for tag in state['tag_idx']['trending']:
        if tag in state['community']: continue
        obj = await if_tag_community(context, tag, observer)
        if obj: state['community'][tag] = obj

    # if observer, add all subs
    if observer:
        comms = await list_all_subscriptions(context, observer)
        for comm in comms:
            tag = comm['name']
            state['community'][tag] = comm
            if tag not in state['tag_idx']['trending']:
                state['tag_idx']['trending'].insert(0, tag)

    return state

async def _get_account_discussion_by_key(db, account, key):
    assert account, 'account must be specified'
    assert key, 'discussion key must be specified'

    if key == 'recent_replies':
        pids = await cursor.pids_by_replies(db, account, '', 20)
        posts = await load_posts(db, pids)
    elif key == 'comments':
        pids = await cursor.pids_by_comments(db, account, '', 20)
        posts = await load_posts(db, pids)
    elif key == 'blog':
        pids = await cursor.pids_by_blog(db, account, '', '', 20)
        posts = await load_posts(db, pids)
    elif key == 'feed':
        res = await cursor.pids_by_feed_with_reblog(db, account, '', '', 20)
        posts = await load_posts_reblogs(db, res)
    else:
        raise ApiError("unknown account discussion key %s" % key)

    return posts

def _normalize_path(path):
    assert path, 'path cannot be blank'
    assert path[0] != '/', 'path cannot start with forward slash'
    assert path[-1] != '/', 'path cannot end with forward slash'
    assert '#' not in path, 'path contains hash mark (#)'
    assert '?' not in path, 'path contains query string: `%s`' % path
    assert path.count('/') < 3, 'too many parts in path: `%s`' % path
    return (path, path.split('/'))

def _keyed_posts(posts):
    out = OrderedDict()
    for post in posts:
        out[_ref(post)] = post
    return out

def _ref(post):
    return post['author'] + '/' + post['permlink']

async def _load_content_accounts(db, content, observer_id):
    if not content:
        return {}
    posts = content.values()
    names = set(map(lambda p: p['author'], posts))
    accounts = await load_accounts(db, names, observer_id)
    return {a['name']: a for a in accounts}

async def _load_account(db, name, observer_id):
    ret = await load_accounts(db, [name], observer_id)
    assert ret, 'account not found: `%s`' % name
    account = ret[0]
    for key in ACCOUNT_TAB_KEYS.values():
        account[key] = []
    return account

async def _child_ids(db, parent_ids):
    """Load child ids for multuple parent ids."""
    sql = """
             SELECT parent_id, array_agg(id)
               FROM hive_posts
              WHERE parent_id IN :ids
                AND is_deleted = '0'
           GROUP BY parent_id
    """
    rows = await db.query_all(sql, ids=tuple(parent_ids))
    return [[row[0], row[1]] for row in rows]

async def _load_discussion(db, author, permlink):
    """Load a full discussion thread."""
    root_id = await cursor.get_post_id(db, author, permlink)
    if not root_id:
        return {}

    # build `ids` list and `tree` map
    ids = []
    tree = {}
    todo = [root_id]
    while todo:
        ids.extend(todo)
        rows = await _child_ids(db, todo)
        todo = []
        for pid, cids in rows:
            tree[pid] = cids
            todo.extend(cids)

    # load all post objects, build ref-map
    posts = await load_posts_keyed(db, ids)

    # remove posts/comments from muted accounts
    muted_accounts = Mutes.all()
    rem_pids = []
    for pid, post in posts.items():
        if post['author'] in muted_accounts:
            rem_pids.append(pid)
    for pid in rem_pids:
        if pid in posts:
            del posts[pid]
        if pid in tree:
            rem_pids.extend(tree[pid])

    refs = {pid: _ref(post) for pid, post in posts.items()}

    # add child refs to parent posts
    for pid, post in posts.items():
        if pid in tree:
            post['replies'] = [refs[cid] for cid in tree[pid]
                               if cid in refs]

    # return all nodes keyed by ref
    return {refs[pid]: post for pid, post in posts.items()}

async def _get_feed_price(db):
    """Get a steemd-style ratio object representing feed price."""
    price = await db.query_one("SELECT usd_per_steem FROM hive_state")
    return {"base": "%.3f SBD" % price, "quote": "1.000 STEEM"}

async def _get_props_lite(db):
    """Return a minimal version of get_dynamic_global_properties data."""
    raw = json.loads(await db.query_one("SELECT dgpo FROM hive_state"))
    return {'sbd_print_rate': raw['sbd_print_rate']}
