import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.responses import StreamingResponse
from starlette.staticfiles import StaticFiles
from web.public_access import COOKIE, PublicAccess, install, normalize_origin, password_digest, redirect_app

ORIGIN = 'https://video.example.test:23741'
PASSWORD = 'offline-test-password-only'
SALT = '1234567890abcdef1234567890abcdef'
DIGEST = password_digest(PASSWORD, SALT)


class AccessChecks(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / 'jobs/_validation')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / 'index.html').write_text('<html>login shell</html>', encoding='utf-8')
        (self.root / 'assets').mkdir()
        (self.root / 'assets/app.js').write_text('login asset', encoding='utf-8')
        self.app = FastAPI()
        self.calls = []

        @self.app.get('/api/jobs')
        async def jobs():
            return {'private_jobs': True}

        @self.app.get('/api/jobs/example/files/video.mp4')
        async def files():
            return {'private_file': True}

        @self.app.get('/api/jobs/example/events')
        async def events():
            async def chunks():
                yield 'data: private-progress\n\n'
                self.access.sessions.clear()
                yield 'data: must-not-leak\n\n'
            return StreamingResponse(chunks(), media_type='text/event-stream')

        @self.app.post('/api/write')
        async def write():
            self.calls.append('write')
            return {'ok': True}

        self.trusted = lambda value: value in ('127.0.0.1', '192.168.31.128')
        install(self.app, self.root, self.trusted)
        self.access = PublicAccess([ORIGIN], 'admin', SALT, DIGEST)
        self.app.state.public_access = self.access
        self.app.mount('/', StaticFiles(directory=self.root, html=True))
        self.client = TestClient(self.app, base_url=ORIGIN, follow_redirects=False)

    def login(self, client=None):
        client = client or self.client
        return client.post('/api/auth/login', json={'username': 'admin', 'password': PASSWORD}, headers={'Origin': str(client.base_url).rstrip('/')})

    def test_origin_validation(self):
        self.assertEqual(normalize_origin('https://VIDEO.EXAMPLE.test:443/'), 'https://video.example.test')
        for value in ['http://example.test', 'https://user:pass@example.test', 'https://*.example.test', 'https://example.test/path', 'https://example.test?x=1', 'https://example.test#x', 'https://example.test:0', 'https://example.test:70000', 'https://example.test\n', None]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_origin(value)

    def test_invalid_admin_settings(self):
        for origins, username, salt, digest in [([], 'admin', SALT, DIGEST), ([ORIGIN], '', SALT, DIGEST), ([ORIGIN], 'admin', 'x', DIGEST), ([ORIGIN], 'admin', SALT, '')]:
            with self.assertRaises(ValueError):
                PublicAccess(origins, username, salt, digest)

    def test_private_routes_require_login(self):
        for path in ['/api/jobs', '/api/jobs/example/files/video.mp4', '/api/jobs/example/events', '/other-private-path']:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 401)
        self.assertEqual(self.client.post('/api/write', json={}, headers={'Origin': ORIGIN}).status_code, 401)
        self.assertEqual(self.calls, [])

    def test_login_shell_and_assets_public_but_page_redirects(self):
        self.assertEqual(self.client.get('/login').status_code, 200)
        self.assertEqual(self.client.get('/assets/app.js').status_code, 200)
        response = self.client.get('/', headers={'Accept': 'text/html'})
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers['location'], '/login')

    def test_login_cookie_and_authenticated_reads_writes(self):
        response = self.login()
        self.assertEqual(response.status_code, 200)
        cookie = response.headers['set-cookie']
        for value in [COOKIE, 'HttpOnly', 'Secure', 'SameSite=strict', 'Path=/', 'Max-Age=28800']:
            self.assertIn(value, cookie)
        self.assertNotIn('Domain=', cookie)
        self.assertEqual(self.client.get('/api/jobs').status_code, 200)
        self.assertEqual(self.client.get('/api/jobs/example/files/video.mp4').status_code, 200)
        self.assertEqual(self.client.post('/api/write', json={}, headers={'Origin': ORIGIN}).status_code, 200)
        self.assertEqual(self.client.get('/api/auth/status').json()['username'], 'admin')
        self.assertTrue(self.client.get('/api/auth/status').json()['authenticated'])

    def test_bad_login_never_echoes_password(self):
        response = self.client.post('/api/auth/login', json={'username': 'admin', 'password': 'wrong-secret'}, headers={'Origin': ORIGIN})
        self.assertEqual(response.status_code, 401)
        self.assertNotIn('wrong-secret', response.text)
        self.assertFalse(self.client.get('/api/auth/status').json()['authenticated'])

    def test_forged_cookie_is_rejected(self):
        self.assertEqual(self.client.get('/api/jobs', headers={'Cookie': f'{COOKIE}=forged'}).status_code, 401)

    def test_logout_revokes_session(self):
        self.login()
        token = self.client.cookies.get(COOKIE)
        self.assertEqual(self.client.post('/api/auth/logout', json={}, headers={'Origin': ORIGIN}).status_code, 200)
        self.assertEqual(self.client.get('/api/jobs', headers={'Cookie': f'{COOKIE}={token}'}).status_code, 401)

    def test_expired_session_and_restart_require_login(self):
        self.login()
        with patch('web.public_access.time.monotonic', return_value=time.monotonic() + 28801):
            self.assertEqual(self.client.get('/api/jobs').status_code, 401)
        self.login()
        self.app.state.public_access = PublicAccess([ORIGIN], 'admin', SALT, DIGEST)
        self.assertEqual(self.client.get('/api/jobs').status_code, 401)

    def test_host_and_forwarded_headers_cannot_bypass(self):
        self.assertEqual(self.client.get('/api/auth/status', headers={'Host': 'evil.example.test'}).status_code, 403)
        self.assertEqual(self.client.get('/api/jobs', headers={'Host': '127.0.0.1:8849'}).status_code, 401)
        self.assertEqual(self.client.get('/api/jobs', headers={'X-Forwarded-For': '127.0.0.1', 'X-Forwarded-Host': '127.0.0.1'}).status_code, 401)

    def test_missing_and_cross_origin_writes_rejected(self):
        for headers in [{}, {'Origin': 'https://evil.example.test'}, {'Origin': 'null'}]:
            self.assertEqual(self.client.post('/api/auth/login', json={'username': 'admin', 'password': PASSWORD}, headers=headers).status_code, 403)
        self.login()
        self.assertEqual(self.client.post('/api/write', json={}).status_code, 403)
        self.assertEqual(self.client.post('/api/auth/logout', json={}, headers={'Origin': 'https://evil.example.test'}).status_code, 403)
        self.assertEqual(self.calls, [])

    def test_login_limits_types_and_rate(self):
        headers = {'Origin': ORIGIN, 'Content-Type': 'application/json'}
        self.assertEqual(self.client.post('/api/auth/login', content='x' * 4097, headers=headers).status_code, 413)
        self.assertEqual(self.client.post('/api/auth/login', content='bad', headers=headers).status_code, 422)
        self.assertEqual(self.client.post('/api/auth/login', json=[], headers=headers).status_code, 422)
        self.assertEqual(self.client.post('/api/auth/login', json={'username': [], 'password': 'x'}, headers=headers).status_code, 422)
        self.assertEqual(self.client.post('/api/auth/login', content='x', headers={'Origin': ORIGIN}).status_code, 415)
        self.access.attempts.clear()
        for _ in range(10):
            self.assertEqual(self.client.post('/api/auth/login', json={}, headers=headers).status_code, 422)
        response = self.login()
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers['retry-after'], '60')

    def test_http_cannot_login_or_read_even_with_forwarded_proto(self):
        client = TestClient(self.app, base_url=ORIGIN.replace('https:', 'http:'))
        self.assertEqual(client.get('/api/jobs', headers={'X-Forwarded-Proto': 'https'}).status_code, 403)
        self.assertEqual(self.login(client).status_code, 403)

    def test_local_hosts_still_require_login_in_public_mode(self):
        client = TestClient(self.app, base_url='https://192.168.31.128:8849')
        self.assertEqual(client.get('/api/jobs').status_code, 401)
        self.assertEqual(self.login(client).status_code, 200)
        self.assertEqual(client.get('/api/jobs').status_code, 200)

    def test_redirect_listener_never_serves_data_or_accepts_writes(self):
        client = TestClient(redirect_app(self.access, self.trusted, 8849), base_url='http://192.168.31.128:8848', follow_redirects=False)
        response = client.get('/api/jobs?q=1')
        self.assertEqual(response.status_code, 307)
        self.assertEqual(response.headers['location'], 'https://192.168.31.128:8849/api/jobs?q=1')
        self.assertEqual(client.post('/api/auth/login', json={}).status_code, 403)
        self.assertEqual(client.get('/', headers={'Host': 'evil.example.test'}).status_code, 403)
        response = client.get('/', headers={'Host': 'video.example.test:23741'})
        self.assertEqual(response.headers['location'], ORIGIN + '/')

    def test_security_headers(self):
        response = self.client.get('/login')
        self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertEqual(response.headers['referrer-policy'], 'no-referrer')
        self.assertEqual(response.headers['x-content-type-options'], 'nosniff')
        self.assertIn("frame-ancestors 'none'", response.headers['content-security-policy'])

    def test_existing_stream_stops_after_revocation(self):
        self.login()
        response = self.client.get('/api/jobs/example/events')
        self.assertEqual(response.status_code, 200)
        self.assertIn('private-progress', response.text)
        self.assertNotIn('must-not-leak', response.text)

    def test_disabled_mode_compatible(self):
        self.app.state.public_access = None
        self.assertEqual(self.client.get('/api/auth/status').json(), {'enabled': False, 'authenticated': True})
        self.assertEqual(self.client.get('/api/jobs').status_code, 200)

    def test_real_app_middleware_applies_authentication_before_write_checks(self):
        from web import app as web
        with patch.object(web.app.state, 'public_access', self.access):
            client = TestClient(web.app, base_url=ORIGIN)
            self.assertEqual(client.get('/api/jobs').status_code, 401)
            self.assertEqual(self.login(client).status_code, 200)
            self.assertEqual(client.get('/api/jobs').status_code, 200)
            self.assertEqual(client.post('/api/model-config/validate', json={}, headers={'Origin': ORIGIN}).status_code, 422)

    def test_invalid_config_fails_closed(self):
        path = self.root / 'config.json'
        path.write_text(json.dumps({'public_origins': [ORIGIN], 'username': 'admin', 'password_salt': SALT, 'password_digest': DIGEST, 'tls_cert': 'missing.pem', 'tls_key': 'missing.key'}))
        with self.assertRaises(ValueError):
            PublicAccess.from_file(path)

    def test_missing_frontend_reports_unavailable_without_leaking_paths(self):
        (self.root / 'index.html').unlink()
        response = self.client.get('/login')
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(str(self.root), response.text)

    def test_login_content_length_is_checked_before_reading(self):
        headers = {'Origin': ORIGIN, 'Content-Type': 'application/json'}
        self.assertEqual(self.client.post('/api/auth/login', content=b'{}', headers={**headers, 'Content-Length': '4097'}).status_code, 413)
        self.assertEqual(self.client.post('/api/auth/login', content=b'{}', headers={**headers, 'Content-Length': '-1'}).status_code, 400)


if __name__ == '__main__':
    unittest.main()
