"""Fail-closed access and resource limits for a single-operator starter."""
import asyncio
import base64
import os
import secrets
import time
from collections import defaultdict, deque
from urllib.parse import urlparse
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse, RedirectResponse
import db

class StarterSecurity:
    def __init__(self, app):
        self.app = app
        self.hits = defaultdict(deque)
        self.active = 0

    async def __call__(self, scope, receive, send):
        if scope['type'] not in ('http', 'websocket'):
            return await self.app(scope, receive, send)
        request = HTTPConnection(scope)
        path = scope['path']
        ws = scope['type'] == 'websocket'
        async def reject(status, message):
            if ws:
                await send({'type': 'websocket.close', 'code': 1008})
            else:
                await JSONResponse({'error': message}, status_code=status)(scope, receive, send)
        # Disable unsupported ingress, open signup, old unbounded turn APIs and web scraping.
        if path in ('/signup', '/api/v1/voice/turn', '/api/v1/voice/reset') or (
            path.startswith('/api/v1/telephony/') and path != '/api/v1/telephony/exotel/stream'):
            return await reject(404, 'Outside this starter release')
        if path == '/health' and not ws:
            return await JSONResponse({'status': 'ok', 'edition': 'experimental-starter'})(scope, receive, send)
        # Limit attempts per remote peer; use one worker. Never trust forwarded IP headers here.
        peer = str(scope.get('client', ('unknown',))[0])
        key = (peer, 'login' if path == '/login' else 'app')
        now = time.monotonic()
        # Bound the number of limiter entries too.
        if len(self.hits) > 4096:
            for old in list(self.hits):
                if not self.hits[old] or self.hits[old][-1] < now - 60:
                    del self.hits[old]
            if len(self.hits) > 4096:
                return await reject(429, 'Server busy')
        hits = self.hits[key]
        while hits and hits[0] < now - 60: hits.popleft()
        limit = 8 if path == '/login' and scope.get('method') == 'POST' else 120
        if len(hits) >= limit: return await reject(429, 'Rate limit reached; retry later')
        hits.append(now)
        expected_origin = os.getenv('PUBLIC_ORIGIN', 'http://localhost:8000').rstrip('/')
        origin = request.headers.get('origin')
        if (ws and path == '/api/v1/voice/live') or (not ws and scope.get('method') not in ('GET','HEAD','OPTIONS') and path != '/api/v1/trigger'):
            # All browser forms and WebSockets must come from the configured origin.
            if origin != expected_origin:
                return await reject(403, 'Origin rejected')
        operator_id = int(os.getenv('VOICE_TENANT_ID', '1'))
        user = db.session_user(request.cookies.get('vsa_session', ''))
        if user and user['tenant_id'] != operator_id: user = None
        scope.setdefault('state', {})['user'] = user
        if path == '/api/v1/telephony/exotel/stream':
            username = os.getenv('EXOTEL_STREAM_USER', '')
            password = os.getenv('EXOTEL_STREAM_PASSWORD', '')
            expected = base64.b64encode(f'{username}:{password}'.encode()).decode()
            if not username or len(password) < 32 or not secrets.compare_digest(request.headers.get('authorization', ''), 'Basic ' + expected):
                return await reject(401, 'Exotel stream authentication required')
            if request.query_params.get('sample-rate', '8000') not in ('8000','16000','24000'):
                return await reject(400, 'Unsupported sample rate')
            if not ws or request.query_params.get('engine', 'gemini') != 'gemini':
                return await reject(400, 'Exotel starter supports Gemini media only')
        elif path == '/api/v1/trigger':
            tenant = db.tenant_by_api_key(request.headers.get('x-api-key', ''))
            if not tenant or tenant['id'] != operator_id:
                return await reject(401, 'Operator API key required')
        elif path != '/login' and not user:
            if not ws and not path.startswith('/api/'):
                return await RedirectResponse('/login', status_code=303)(scope, receive, send)
            return await reject(401, 'Sign in required')
        # Bound HTTP bodies before passing them to Starlette's form/JSON parsers.
        if not ws:
            body = bytearray()
            while True:
                message = await receive()
                if message['type'] == 'http.disconnect': return
                body.extend(message.get('body', b''))
                if len(body) > 65536: return await reject(413, 'Request body too large')
                if not message.get('more_body'): break
            delivered = False
            async def replay():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {'type':'http.request', 'body':bytes(body), 'more_body':False}
                return await receive()
            async def secure_send(message):
                if message['type'] == 'http.response.start':
                    message.setdefault('headers', []).extend([(b'cache-control',b'no-store'),(b'x-content-type-options',b'nosniff'),(b'x-frame-options',b'DENY'),(b'referrer-policy',b'same-origin')])
                await send(message)
            return await self.app(scope, replay, secure_send)
        if self.active >= 2: return await reject(429, 'Audio session capacity reached')
        self.active += 1
        received_bytes = 0
        frame_times = deque()
        async def bounded_receive():
            nonlocal received_bytes
            message = await receive()
            size = len(message.get('bytes') or b'') + len((message.get('text') or '').encode())
            received_bytes += size
            now = time.monotonic()
            while frame_times and frame_times[0] < now - 1: frame_times.popleft()
            frame_times.append(now)
            if size > 262144 or received_bytes > 64 * 1024 * 1024 or len(frame_times) > 100:
                raise ValueError('Audio frame too large')
            return message
        try:
            await asyncio.wait_for(self.app(scope, bounded_receive, send), timeout=min(600, max(1, int(os.getenv('MAX_CALL_SECONDS', '180')))))
        except (TimeoutError, ValueError):
            try: await send({'type':'websocket.close','code':1008})
            except Exception: pass
        finally:
            self.active -= 1


def configure_private_logging():
    """Do not print transcripts, numbers, provider URLs or keys by default."""
    import logging
    if os.getenv('PRIVATE_DEBUG_LOGS', '0') == '1':
        return
    class PrivateFilter(logging.Filter):
        def filter(self, record):
            if record.name not in ('uvicorn.error', 'uvicorn.access'):
                record.msg = 'Application event (details hidden; enable PRIVATE_DEBUG_LOGS only locally)'
                record.args = ()
                record.exc_info = None
                record.exc_text = None
            return True
    for handler in logging.getLogger().handlers:
        handler.addFilter(PrivateFilter())
