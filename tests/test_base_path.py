"""共用網站的子路徑部署：保護既有根路徑並驗證完整登入、附件與備份。"""
import io
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from app import create_app
from auth import FLOW_COOKIE, SESSION_COOKIE
from fixtures import seed_sales


class BasePathTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='signing-prefix-')
        self.config = dict(TESTING=True, LOAD_ENV_FILE=False, DATA_DIR=self.tmp.name,
                           INITIAL_ADMINS={'測試人員': ('測試人員',)},
                           APP_BASE_PATH='/money', FEISHU_APP_ID='cli_testing',
                           FEISHU_APP_SECRET='test-secret', FEISHU_TENANT_KEY='company_tenant',
                           FEISHU_REDIRECT_URI='https://localhost/money/auth/feishu/callback')
        self.app = create_app(self.config)
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def get(self, path, **kwargs):
        return self.client.get(path, base_url='https://localhost/', **kwargs)

    def post(self, path, **kwargs):
        return self.client.post(path, base_url='https://localhost/', **kwargs)

    def login(self):
        start = self.get('/money/auth/feishu/start?next=/%23/contracts')
        self.assertEqual(start.status_code, 302)
        params = parse_qs(urlsplit(start.location).query)
        self.assertEqual(params['redirect_uri'], [self.config['FEISHU_REDIRECT_URI']])
        self.assertEqual(self.client.get_cookie(FLOW_COOKIE, path='/money').path, '/money')
        with patch('auth.provider_json', side_effect=[
            {'code': 0, 'access_token': 'test-token', 'expires_in': 7200},
            {'code': 0, 'data': {'name': '測試人員', 'tenant_key': 'company_tenant', 'open_id': 'ou_test'}}
        ]):
            response = self.get('/money/auth/feishu/callback', query_string={'state': params['state'][0], 'code': 'test_code'})
        self.assertEqual(response.status_code, 303, response.get_data(as_text=True))
        self.assertEqual(response.location, '/money/#/contracts')
        cookie = self.client.get_cookie(SESSION_COOKIE, path='/money')
        self.assertEqual(cookie.path, '/money')
        self.assertTrue(cookie.secure)
        self.csrf = self.get('/money/api/v1/auth/me').get_json()['csrf_token']

    def test_outside_paths_are_not_claimed_and_login_is_prefixed(self):
        for path in ['/', '/api/v1/health', '/login', '/static/styles.css', '/money-other/login']:
            self.assertEqual(self.get(path).status_code, 404, path)
        self.assertEqual(self.get('/money/api/v1/health').status_code, 200)
        self.assertEqual(self.get('/money/').location, '/money/login')
        self.assertEqual(self.get('/money/api/v1/state').get_json()['login_url'], '/money/login')
        html = self.get('/money/login').get_data(as_text=True)
        self.assertIn('href="/money/static/styles.css"', html)
        self.assertIn('href="/money/auth/feishu/start', html)
        self.assertIn('href="https://localhost/money/"', html)
        with self.get('/money/static/styles.css') as stylesheet:
            self.assertEqual(stylesheet.status_code, 200)

    def test_login_assets_csrf_and_logout_stay_under_prefix(self):
        self.login()
        html = self.get('/money/').get_data(as_text=True)
        self.assertIn('name="app-base-path" content="/money"', html)
        self.assertIn('src="/money/static/app.js"', html)
        self.assertNotIn('{{', html)
        self.assertEqual(self.get('/money/static/index.html').location, '/money/')
        self.assertEqual(self.get('/money/login?next=/%23/dash').location, '/money/#/dash')
        self.assertEqual(self.post('/money/api/v1/leads', json={'company': '不可建立'}).status_code, 403)
        headers = {'X-CSRF-Token': self.csrf, 'Origin': 'https://localhost'}
        self.assertEqual(self.post('/money/api/v1/leads', headers=headers, json={'company': '路徑測試', 'estimate': '1'}).status_code, 201)
        logout = self.post('/money/api/v1/auth/logout', headers=headers)
        self.assertEqual(logout.get_json()['login_url'], '/money/login?reason=logged_out')
        self.assertIsNone(self.client.get_cookie(SESSION_COOKIE, path='/money'))
        self.assertEqual(self.get('/money/api/v1/state').status_code, 401)

    def test_attachments_and_backup_are_reachable_under_prefix(self):
        self.login()
        headers = {'X-CSRF-Token': self.csrf}
        def create(path, data):
            response = self.post('/money/api/v1' + path, json=data, headers=headers)
            self.assertIn(response.status_code, [200, 201], response.get_data(as_text=True))
            return response.get_json()
        rep = seed_sales(self.app, '測試業務')
        lead = create('/leads', {'company': '測試客戶', 'estimate': '5'})['id']
        create(f'/leads/{lead}/assign', {'rep_id': rep})
        contract = create('/contracts', {'customer_id': lead, 'rep_id': rep, 'title': '測試合同', 'amount': '5'})['id']
        content = b'%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF'
        response = self.post(f'/money/api/v1/contracts/{contract}/attachments', headers=headers,
                             data={'kind': 'signed', 'file': (io.BytesIO(content), 'contract.pdf')})
        self.assertEqual(response.status_code, 201, response.get_json())
        attachment = self.get('/money/api/v1/state').get_json()['contracts'][0]['attachments'][0]
        self.assertTrue(attachment['url'].startswith('/money/api/v1/attachments/'))
        download = self.get(attachment['url'])
        self.assertEqual(download.data, content)
        download.close()
        backup = self.get('/money/api/v1/backup')
        self.assertEqual(backup.status_code, 200)
        self.assertTrue(backup.data.startswith(b'PK'))
        backup.close()

    def test_root_callback_is_not_accepted_for_prefix_deployment(self):
        self.app.config['FEISHU_REDIRECT_URI'] = 'https://localhost/auth/feishu/callback'
        self.assertEqual(self.get('/money/auth/feishu/start').status_code, 503)

    def test_pending_identity_and_waiting_assets_stay_under_prefix(self):
        start = self.get('/money/auth/feishu/start')
        state = parse_qs(urlsplit(start.location).query)['state'][0]
        with patch('auth.provider_json', side_effect=[
            {'code':0,'access_token':'test-token','expires_in':7200},
            {'code':0,'data':{'name':'新同事','tenant_key':'company_tenant','open_id':'ou_pending'}}
        ]):
            response = self.get('/money/auth/feishu/callback',query_string={'state':state,'code':'test_code'})
        self.assertTrue(response.location.startswith('/money/waiting'))
        denied = self.get('/money/api/v1/state')
        self.assertEqual(denied.status_code,403)
        self.assertEqual(denied.get_json()['waiting_url'],'/money/waiting')
        html=self.get('/money/waiting').get_data(as_text=True)
        self.assertIn('src="/money/static/waiting.js"',html)
        with self.get('/money/static/waiting.js') as asset:
            self.assertEqual(asset.status_code,200)

    def test_invalid_prefix_is_rejected(self):
        for prefix in ['//external.test', '/money?x=1', '/a/../money', '/money%2f', None]:
            with self.subTest(prefix=prefix), self.assertRaises(ValueError):
                create_app({**self.config, 'APP_BASE_PATH': prefix})
