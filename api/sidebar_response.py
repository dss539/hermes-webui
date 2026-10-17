"""Conditional sidebar responses; validators cover the actual runtime overlay."""

import hashlib
import json

from api.helpers import j, flush_pending_auth_cookies, _security_headers


def session_list_etag(response, scope):
    # Wall-clock response time is intentionally not part of the projection.
    # Include runtime flags, redacted values, timezone and request scope, not
    # just the database stamp: those can change independently of a DB commit.
    stable = {key: value for key, value in response.items() if key != 'server_time'}
    encoded = json.dumps((scope, stable), sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
    return 'W/"' + hashlib.sha256(encoded).hexdigest() + '"'


def send_session_list(handler, response, scope):
    etag = session_list_etag(response, scope)
    extra_headers = {'ETag': etag, 'X-Sidebar-Server-Time': str(response.get('server_time', ''))}
    supplied = getattr(handler, 'headers', {}).get('If-None-Match', '')
    if any(token.strip() == '*' or token.strip().removeprefix('W/') == etag.removeprefix('W/')
           for token in supplied.split(',')):
        handler.send_response(304)
        for key, value in extra_headers.items():
            handler.send_header(key, value)
        handler.send_header('Cache-Control', 'no-store')
        _security_headers(handler)
        flush_pending_auth_cookies(handler)
        handler.end_headers()
        return
    return j(handler, response, extra_headers=extra_headers, pretty=False)
