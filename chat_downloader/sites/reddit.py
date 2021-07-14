
from .common import (
    BaseChatDownloader,
    Chat,
    Remapper as r
)

from ..utils.core import (
    multi_get,
    int_or_none,
    attempts,
    try_parse_json,
    get_title_of_webpage
)

from ..errors import (
    SiteError,
    VideoNotFound,
    UnexpectedError,
    ChatDisabled
)

from ..debugging import log


from requests.exceptions import RequestException
from json.decoder import JSONDecodeError

import json
import websocket
import time
import re


class RedditError(SiteError):
    """Raised when an error occurs with a Reddit video."""
    pass


class RedditChatDownloader(BaseChatDownloader):

    _REDDIT_HOMEPAGE = 'https://www.reddit.com'
    _INITIAL_INFO_REGEX = r'(?:window\.___r)\s*=\s*({.+?})\s*;<\/script>'

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

        max_attempts = 10

        for attempt_number in attempts(max_attempts):
            initial_data = self._session_get(self._REDDIT_HOMEPAGE).text
            info = re.search(self._INITIAL_INFO_REGEX, initial_data)

            if info:
                info = try_parse_json(info.group(1))
                break
            else:
                title = get_title_of_webpage(initial_data)
                self.retry(attempt_number, max_attempts, text=title)
                continue

        bearer = multi_get(info, 'user', 'session', 'accessToken')
        if bearer:
            self.update_session_headers({
                'authorization': 'Bearer {}'.format(bearer)
            })

    _NAME = 'reddit.com'

    _SITE_DEFAULT_PARAMS = {
        'format': 'default',  # TODO create reddit format
    }

    _TESTS = []

    # Regex provided by youtube-dl

    _VALID_URLS = {
        '_get_chat_by_stream_id': r'(?:https?://(?:[^/]+\.))reddit\.com/rpan/r/[^/]+/(?P<id>[^/?#&]+)'
        # TODO add support for "top livestream of subreddit"
        # https://www.reddit.com/rpan/r/AnimalsOnReddit
    }

    _REMAPPING = {
        'author_icon_img': 'author_profile_img',
        'subreddit_id': 'subreddit_id',
        'author_flair_type': 'author_flair_type',

        'author': 'author_name',
        'author_id': 'author_id',
        'author_fullname': 'author_str_id',
        'body': 'message',
        'body_html': 'message_html',

        'created_utc': r('timestamp', lambda x: int_or_none(x, time.time()) * 1000000),

        'link_id': 'message_id',
        'name': 'message_name',

        'score': 'score',
        'context': r('url', lambda x: RedditChatDownloader._REDDIT_HOMEPAGE+x)

        # Unused
        # 'comment_type':'comment_type',
        # 'attribs':[],
        # 'author_flair_template_id':'None',
        # 'author_snoovatar_img':'',
        # 'rtjson':{},
        # 'collapsed':false,
        # 'subreddit_name_prefixed':'r/RedditSessions',
        # 'full_date':'2021-07-02T16:12:00+00:00',
        # 'collapsed_in_crowd_control':false,
        # 'flair_css_class':'flair-None',
        # 'author_is_default_icon':true,
        # 'author_flair_richtext':[],
        # 'author_is_nsfw_icon':false,
        # 'total_comment_count':249,
        # 'associated_award':'None',
        # 'subreddit':'RedditSessions',
        # 'flair_position':'right',
        # 'author_flair_text_color':'None',
        # '_id36':'h3tiznf',
        # 'author_flair_text':'None',
        # 'author_flair_background_color':'None',
        # 'distinguished':'None'
    }

    _KNOWN_MESSAGE_TYPES = ['new_comment', 'delete_comment',
                            'remove_comment', 'update_comment_score']

    @staticmethod
    def _parse_item(item):

        info = r.remap_dict(item, RedditChatDownloader._REMAPPING)

        BaseChatDownloader._move_to_dict(info, 'author')

        author_name = multi_get(info, 'author', 'name')
        if author_name:
            info['author_display_name'] = author_name

        return info

    def _get_chat_by_stream_id(self, match, params):
        match_id = match.group('id')
        return self.get_chat_by_stream_id(match_id, params)

    def _try_get_info(self, url, params):
        max_attempts = params.get('max_attempts')
        retry_timeout = params.get('retry_timeout')

        for attempt_number in attempts(max_attempts):
            try:
                return self._session_get_json(url)
            except (JSONDecodeError, RequestException) as e:
                self.retry(attempt_number, max_attempts, e, retry_timeout)

    _API_TEMPLATE = 'https://strapi.reddit.com/videos/t3_{}'
    _FALLBACK_API_TEMPLATE = 'https://gateway.reddit.com/desktopapi/v1/postcomments/t3_{}?limit=1'

    def get_chat_by_stream_id(self, stream_id, params, attempt_number=0):

        max_attempts = params.get('max_attempts')
        retry_timeout = params.get('retry_timeout')

        fallback = attempt_number % 2 == 1

        if not fallback:
            initial_info = self._try_get_info(
                self._API_TEMPLATE.format(stream_id), params)

            status = initial_info.get('status')
            status_message = initial_info.get('status_message')
            data = initial_info.get('data')

        else:  # fallback
            log('debug', 'Using fallback API')

            initial_info = self._try_get_info(
                self._FALLBACK_API_TEMPLATE.format(stream_id), params)

            data = multi_get(initial_info, 'posts', 't3_{}'.format(stream_id))
            status = 'success' if data else None

        if status == 'success':
            chat_disabled = data.get('chat_disabled')
            if chat_disabled:
                raise ChatDisabled('Chat is disabled for this stream.')

            post_info = data.get('post') or data
            stream_info = data.get('stream') or data

            title = post_info.get('title')

            state = stream_info.get('state')
            is_live = (state == 'IS_LIVE' or fallback)  # 'ENDED'
            start_time = (stream_info.get('publish_at')
                          or stream_info.get('created')) * 1000

            socket_url = post_info.get('liveCommentsWebsocket')

            if is_live and socket_url:

                if not socket_url.startswith('wss://'):
                    raise RedditError(
                        'Invalid websocket URL: {}'.format(socket_url))

                # Create chat object
                chat_item = Chat(title=title, is_live=is_live,
                                 start_time=start_time)
                chat_item.chat = self._get_chat_messages_by_socket(
                    socket_url, params)

                return chat_item
            else:
                raise RedditError(
                    'Video ended. Chat replay not implemented yet.')  # TODO

        elif status == 'failure':
            if 'wait' in data.lower():
                message = 'Response from Reddit: "{}"'.format(data)
                self.retry(attempt_number, max_attempts,
                           retry_timeout=retry_timeout, text=message)
                return self.get_chat_by_stream_id(stream_id, params, attempt_number + 1)

            raise RedditError(data)

        elif status == 'video not found':
            raise VideoNotFound(status_message)

        else:  # Unknown status
            raise UnexpectedError(initial_info)

    def _get_chat_messages_by_socket(self, socket_url, params):

        message_receive_timeout = params.get('message_receive_timeout')
        max_attempts = params.get('max_attempts')
        retry_timeout = params.get('retry_timeout')

        def create_connection():
            for attempt_number in attempts(max_attempts):
                try:
                    ws = websocket.create_connection(socket_url)

                    # timeout needed for polling (allow keyboard interrupts)
                    ws.settimeout(message_receive_timeout)
                    return ws
                except (ConnectionError, websocket.WebSocketException) as e:
                    self.retry(attempt_number, max_attempts, e, retry_timeout)

        ws = create_connection()

        try:
            while True:
                try:
                    opcode, raw_data = ws.recv_data()
                    data = json.loads(raw_data)

                    message_type = data.get('type')

                    if message_type in self._KNOWN_MESSAGE_TYPES:
                        payload = data.get('payload')

                        parsed = self._parse_item(payload)
                        parsed['message_type'] = message_type

                        yield parsed

                    else:
                        self._debug_log(
                            params, 'Unknown message type: {}'.format(message_type), data)

                except websocket.WebSocketTimeoutException:
                    pass

                except (ConnectionError, websocket.WebSocketException):
                    # Close old connection
                    ws.close()

                    # Create a new connection
                    ws = create_connection()

        finally:
            ws.close()

    _BROADCAST_API_URL = 'https://strapi.reddit.com/broadcasts'  # ?page_size=x

    def generate_urls(self, **kwargs):
        # TODO add sort-by-viewers option

        max_attempts = 30  # TODO make param

        for attempt_number in attempts(max_attempts):
            message = None
            error = None

            try:
                info = self._session_get_json(self._BROADCAST_API_URL)

                data = info.get('data')

                if isinstance(data, list):  # success
                    break

                message = 'Response from Reddit: "{}"'.format(data)

            except (JSONDecodeError, RequestException) as e:
                error = e

            self.retry(attempt_number, max_attempts, error, text=message)

        for stream in data:
            yield multi_get(stream, 'post', 'url')
