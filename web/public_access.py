import asyncio
import hashlib
import hmac
import json
import re
import secrets
import ssl
import time
from collections import deque
from contextlib import nullcontext
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse

COOKIE = '__Host-p2v-session'
SESSION_SECONDS = 8 * 60 * 60


def normalize_origin(value):
    if not isinstance(value, str) or not value or any(character.isspace() for character in value):
        raise ValueError('public_origins：必须是完整的HTTPS地址')
    parsed = urlsplit(value)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.path not in ('', '/') or parsed.query or parsed.fragment:
        raise ValueError('public_origins：只允许HTTPS来源，不允许账号、路径、查询或片段')
    if not re.fullmatch(r'[a-zA-Z0-9.-]+', parsed.hostname) or '..' in parsed.hostname:
        raise ValueError('public_origins：主机名不合法，不支持通配符')
    port = parsed.port
    if port is not None and not 1 <= port <= 65535:
        raise ValueError('public_origins：端口必须在1至65535之间')
    authority = parsed.hostname.lower() + (f':{port}' if port and port != 443 else '')
    return f'https://{authority}'


def password_digest(password, salt):
    return hashlib.scrypt(password.encode('utf-8'), salt=bytes.fromhex(salt), n=32768, r=8, p=1, dklen=32, maxmem=67108864).hex()


class PublicAccess:
    def __init__(self, origins, username, salt, digest):
        if not isinstance(origins, list) or not 1 <= len(origins) <= 8:
            raise ValueError('public_origins：必须提供1至8个明确来源')
        self.origins = tuple(normalize_origin(value) for value in origins)
        if not isinstance(username, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', username):
            raise ValueError('username：只能使用1至64个字母、数字、下划线或连字符')
        if not isinstance(salt, str) or not re.fullmatch(r'[a-f0-9]{32}', salt):
            raise ValueError('password_salt：必须是16字节的十六进制盐')
        if not isinstance(digest, str) or not re.fullmatch(r'[a-f0-9]{64}', digest):
            raise ValueError('password_digest：必须是32字节的scrypt摘要')
        self.username, self.salt, self.digest = username, salt, digest
        self.sessions = {}
        self.attempts = deque()

    @classmethod
    def from_file(cls, filename):
        path = Path(filename).resolve()
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            access = cls(data['public_origins'], data['username'], data['password_salt'], data['password_digest'])
            cert = (path.parent / data['tls_cert']).resolve()
            key = (path.parent / data['tls_key']).resolve()
            ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(str(cert), str(key))
        except (OSError, ValueError, KeyError, TypeError, ssl.SSLError) as exc:
            raise ValueError(f'公网配置或TLS证书无效：{type(exc).__name__}；请检查配置文件，不输出其敏感内容') from None
        access.cert_file, access.key_file = str(cert), str(key)
        return access

    def origin(self, request, trusted_address):
        try:
            origin = normalize_origin(f'https://{request.url.netloc}')
        except ValueError:
            return None
        if origin in self.origins:
            return origin
        hostname = request.url.hostname
        if hostname == 'localhost' or trusted_address(hostname or ''):
            return origin
        return None

    def authenticated(self, request):
        now = time.monotonic()
        self.sessions = {token: expires for token, expires in self.sessions.items() if expires > now}
        cookie = request.cookies.get(COOKIE, '')
        if not cookie or len(cookie) > 128:
            return False
        return hashlib.sha256(cookie.encode('utf-8')).hexdigest() in self.sessions

    async def login(self, request):
        now = time.monotonic()
        while self.attempts and self.attempts[0] <= now - 60:
            self.attempts.popleft()
        if len(self.attempts) >= 10:
            return JSONResponse({'detail': '登录尝试过于频繁，请一分钟后重试'}, 429, headers={'Retry-After': '60'})
        self.attempts.append(now)
        if request.headers.get('content-type', '').split(';')[0].strip() != 'application/json':
            return JSONResponse({'detail': '登录请求必须使用application/json'}, 415)
        try:
            length = int(request.headers.get('content-length', '0'))
        except ValueError:
            return JSONResponse({'detail': 'Content-Length：请求长度不合法'}, 400)
        if length < 0:
            return JSONResponse({'detail': 'Content-Length：请求长度不合法'}, 400)
        if length > 4096:
            return JSONResponse({'detail': '登录请求最多4096字节'}, 413)
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > 4096:
                return JSONResponse({'detail': '登录请求最多4096字节'}, 413)
            body.extend(chunk)
        try:
            data = json.loads(body)
            username, password = data.get('username'), data.get('password')
        except (ValueError, AttributeError, UnicodeError):
            return JSONResponse({'detail': 'username/password：登录请求格式不合法'}, 422)
        if not isinstance(username, str) or not isinstance(password, str) or not 1 <= len(username) <= 64 or not 1 <= len(password) <= 256:
            return JSONResponse({'detail': 'username/password：账号或密码格式不合法'}, 422)
        digest = await asyncio.to_thread(password_digest, password, self.salt)
        if not (hmac.compare_digest(digest, self.digest) & hmac.compare_digest(username.encode('utf-8'), self.username.encode('utf-8'))):
            return JSONResponse({'detail': '账号或密码不正确'}, 401)
        if len(self.sessions) >= 128:
            return JSONResponse({'detail': '登录会话已达上限，请稍后重试或重启服务'}, 503)
        old_cookie = request.cookies.get(COOKIE, '')
        self.sessions.pop(hashlib.sha256(old_cookie.encode('utf-8')).hexdigest(), None)
        token = secrets.token_urlsafe(32)
        self.sessions[hashlib.sha256(token.encode('utf-8')).hexdigest()] = time.monotonic() + SESSION_SECONDS
        response = JSONResponse({'authenticated': True})
        response.set_cookie(COOKIE, token, max_age=SESSION_SECONDS, secure=True, httponly=True, samesite='strict', path='/')
        return response

    async def guard(self, request, call_next, trusted_address, static_directory):
        origin = self.origin(request, trusted_address)
        if not origin:
            return JSONResponse({'detail': '访问地址不在公网来源或可信本地地址白名单内'}, 403)
        if request.url.scheme != 'https':
            return JSONResponse({'detail': '公网访问模式要求HTTPS，不接受明文请求'}, 403)
        if request.method not in ('GET', 'HEAD', 'OPTIONS') and request.headers.get('origin') != origin:
            return JSONResponse({'detail': '拒绝缺少Origin或跨站写入'}, 403)
        authenticated = self.authenticated(request)
        request.state.public_authenticated = authenticated
        path = request.url.path
        if path == '/api/auth/status' and request.method == 'GET':
            return JSONResponse({'enabled': True, 'authenticated': authenticated, 'username': self.username if authenticated else None})
        if path == '/api/auth/login' and request.method == 'POST':
            return await self.login(request)
        if path == '/api/auth/logout' and request.method == 'POST':
            token = request.cookies.get(COOKIE, '')
            self.sessions.pop(hashlib.sha256(token.encode('utf-8')).hexdigest(), None)
            response = JSONResponse({'authenticated': False})
            response.delete_cookie(COOKIE, secure=True, httponly=True, samesite='strict', path='/')
            return response
        if path == '/login' and request.method in ('GET', 'HEAD'):
            if not Path(static_directory, 'index.html').is_file():
                return JSONResponse({'detail': '登录页尚未构建，请先构建前端'}, 503)
            return FileResponse(Path(static_directory, 'index.html'))
        if path.startswith('/assets/') and request.method in ('GET', 'HEAD'):
            return await call_next(request)
        if not authenticated:
            if request.method in ('GET', 'HEAD') and 'text/html' in request.headers.get('accept', '') and not path.startswith('/api/'):
                return RedirectResponse('/login', 303)
            return JSONResponse({'detail': '请先登录，或登录会话已过期'}, 401)
        response = await call_next(request)
        if response.headers.get('content-type', '').startswith('text/event-stream'):
            original = response.body_iterator

            async def checked_stream():
                try:
                    async for chunk in original:
                        if not self.authenticated(request):
                            break
                        yield chunk
                finally:
                    await original.aclose()

            response.body_iterator = checked_stream()
        return response


def install(app, static_directory, trusted_address):
    app.state.public_access = None

    @app.get('/api/auth/status')
    async def status():
        return {'enabled': False, 'authenticated': True}

    @app.middleware('http')
    async def guard(request, call_next):
        access = app.state.public_access
        if not access:
            return await call_next(request)
        response = await access.guard(request, call_next, trusted_address, static_directory)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Content-Security-Policy'] = "frame-ancestors 'none'; form-action 'self'; base-uri 'self'; object-src 'none'"
        return response


def redirect_app(access, trusted_address, https_port):
    async def redirect(scope, receive, send):
        if scope['type'] != 'http':
            return
        request = Request(scope, receive)
        origin = access.origin(request, trusted_address)
        if not origin:
            response = JSONResponse({'detail': '访问地址不在白名单内'}, 403)
        elif request.method not in ('GET', 'HEAD'):
            response = JSONResponse({'detail': '请通过HTTPS提交请求，HTTP入口仅提供跳转'}, 403)
        else:
            if origin not in access.origins:
                origin = f'https://{request.url.hostname}:{https_port}'
            target = origin + request.url.path + (f'?{request.url.query}' if request.url.query else '')
            response = RedirectResponse(target, 307, headers={'Cache-Control': 'no-store'})
        await response(scope, receive, send)
    return redirect


async def serve(app, access, host, port, https_port, trusted_address):
    import uvicorn

    secure = uvicorn.Server(uvicorn.Config(app, host=host, port=https_port, log_level='info', proxy_headers=False,
                           ssl_certfile=access.cert_file, ssl_keyfile=access.key_file))
    redirect = uvicorn.Server(uvicorn.Config(redirect_app(access, trusted_address, https_port), host=host, port=port,
                             log_level='info', proxy_headers=False, lifespan='off'))
    redirect.capture_signals = nullcontext
    tasks = [asyncio.create_task(secure.serve()), asyncio.create_task(redirect.serve())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        secure.should_exit = redirect.should_exit = True
        await asyncio.gather(*tasks)
