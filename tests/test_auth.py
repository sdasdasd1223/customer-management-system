import base64
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from urllib.error import URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from unittest.mock import patch

from app import create_app, AuthRedactedRequestHandler
from auth import AUTHORIZE_URL, TOKEN_URL, V3_TOKEN_URL, USER_INFO_URL, FLOW_COOKIE, SESSION_COOKIE, LoginError, digest, provider_json, safe_next, provider_diagnostics
from settings import feishu_settings


TOKEN = {'code':0,'access_token':'only-server-access-token','expires_in':7200}
IDENTITY = {'code':0,'data':{'name':'林小姐','tenant_key':'company_tenant','open_id':'ou_employee'}}


class AuthTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='signing-auth-')
        self.config = {'TESTING':True,'LOAD_ENV_FILE':False,'DATA_DIR':self.tmp.name,
                       'INITIAL_ADMINS': {'林小姐': ('林小姐',)},
                       'FEISHU_APP_ID':'cli_testing','FEISHU_APP_SECRET':'never-send-this-to-browser',
                       'FEISHU_TENANT_KEY':'company_tenant','FEISHU_SESSION_HOURS':'8',
                       'FEISHU_REDIRECT_URI':'http://localhost/auth/feishu/callback'}
        self.app = create_app(self.config)
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def query(self, sql, params=()):
        conn=sqlite3.connect(Path(self.tmp.name)/'database.sqlite3')
        conn.row_factory=sqlite3.Row
        try:
            result=[dict(row) for row in conn.execute(sql,params)]
            conn.commit()
            return result
        finally:
            conn.close()

    def start(self, next_path='/'):
        response=self.client.get('/auth/feishu/start',query_string={'next':next_path})
        self.assertEqual(response.status_code,302)
        return parse_qs(urlsplit(response.location).query)

    def callback(self, state, responses=None, **args):
        with patch('auth.provider_json',side_effect=responses or [TOKEN,IDENTITY]) as provider:
            response=self.client.get('/auth/feishu/callback',query_string={'state':state,'code':'valid_auth_code',**args})
        return response,provider

    def login(self, next_path='/'):
        params=self.start(next_path)
        response,provider=self.callback(params['state'][0])
        self.assertEqual(response.status_code,303,response.get_data(as_text=True))
        me=self.client.get('/api/v1/auth/me').get_json()
        self.assertTrue(me['authenticated'])
        return me,response,provider

    def test_every_business_entry_is_protected(self):
        for path in ['/api/v1/state','/api/v1/contracts','/api/v1/backup','/api/v1/targets','/api/v1/customers/1/followups','/api/v1/attachments/fake/file']:
            response=self.client.get(path)
            self.assertEqual(response.status_code,401,path)
            self.assertEqual(response.headers['Cache-Control'],'no-store')
        self.assertEqual(self.client.post('/api/v1/leads',json={'company':'不應建立'}).status_code,401)
        self.assertEqual(self.query('SELECT * FROM customers'),[])
        for path in ['/','/static/index.html','/static/app.js']:
            self.assertEqual(self.client.get(path).location,'/login')
        response=self.client.get('/static/styles.css')
        self.assertEqual(response.status_code,200)
        response.close()
        self.assertEqual(self.client.get('/api/v1/health').status_code,200)

    def test_unconfigured_application_stays_closed(self):
        self.app.config['FEISHU_APP_SECRET']=''
        response=self.client.get('/login')
        self.assertEqual(response.status_code,200)
        self.assertIn('等待管理員完成設定',response.get_data(as_text=True))
        self.assertNotIn('data-testid="feishu-login"',response.get_data(as_text=True))
        self.assertEqual(self.client.get('/auth/feishu/start').status_code,503)
        self.assertEqual(self.client.get('/api/v1/state').status_code,401)

    def test_authorize_uses_random_state_and_pkce_without_extra_scopes(self):
        params=self.start('/#/contracts')
        self.assertEqual(params['response_type'],['code'])
        self.assertEqual(params['client_id'],['cli_testing'])
        self.assertEqual(params['redirect_uri'],[self.config['FEISHU_REDIRECT_URI']])
        self.assertEqual(params['code_challenge_method'],['S256'])
        self.assertNotIn('client_secret',params)
        self.assertNotIn('scope',params)
        flow=self.query('SELECT * FROM auth_flows')[0]
        self.assertNotEqual(flow['state_hash'],params['state'][0])
        expected=base64.urlsafe_b64encode(hashlib.sha256(flow['verifier'].encode()).digest()).decode().rstrip('=')
        self.assertEqual(params['code_challenge'][0],expected)
        self.assertEqual(flow['next_path'],'/#/contracts')
        cookie=self.client.get_cookie(FLOW_COOKIE)
        self.assertTrue(cookie.http_only)
        self.assertEqual(cookie.same_site,'Lax')
        self.assertEqual(flow['browser_hash'],digest(cookie.value))
        self.assertNotEqual(self.start()['state'],params['state'])

    def test_login_uses_pkce_compatible_endpoint_without_persisting_provider_tokens(self):
        me,response,provider=self.login('/#/contracts')
        self.assertEqual(response.location,'/#/contracts')
        self.assertEqual(me['user']['name'],'林小姐')
        self.assertNotIn('access_token',me)
        self.assertEqual(provider.call_args_list[0].args[0],TOKEN_URL)
        self.assertEqual(TOKEN_URL,'https://open.feishu.cn/open-apis/authen/v2/oauth/token')
        self.assertTrue(provider.call_args_list[0].kwargs['as_json'])
        form=provider.call_args_list[0].kwargs['form']
        self.assertEqual(form['client_secret'],self.config['FEISHU_APP_SECRET'])
        self.assertIn('code_verifier',form)
        self.assertEqual(provider.call_args_list[1].args[0],USER_INFO_URL)
        self.assertEqual(provider.call_args_list[1].kwargs['bearer'],TOKEN['access_token'])
        cookie=self.client.get_cookie(SESSION_COOKIE)
        self.assertTrue(cookie.http_only)
        self.assertFalse(cookie.secure)
        session=self.query('SELECT * FROM auth_sessions')[0]
        self.assertEqual(session['token_hash'],digest(cookie.value))
        self.assertNotEqual(session['token_hash'],cookie.value)
        self.assertEqual(session['expires_at']-session['created_at'],7200)
        self.assertIsNone(self.client.get_cookie(FLOW_COOKIE))
        all_records=json.dumps(self.query('SELECT * FROM auth_sessions')+self.query('SELECT * FROM auth_users')+self.query('SELECT * FROM audit_log'))
        for secret in [TOKEN['access_token'],self.config['FEISHU_APP_SECRET'],cookie.value]:
            self.assertNotIn(secret,all_records)
            self.assertNotIn(secret,response.get_data(as_text=True))

    def test_wrong_state_or_browser_never_calls_feishu(self):
        params=self.start()
        response,provider=self.callback('z'*43)
        self.assertEqual(response.status_code,400)
        provider.assert_not_called()
        other=self.app.test_client()
        with patch('auth.provider_json') as provider:
            response=other.get('/auth/feishu/callback',query_string={'state':params['state'][0],'code':'code'})
        self.assertEqual(response.status_code,400)
        provider.assert_not_called()
        # 無關瀏覽器的嘗試不會消耗真正瀏覽器的流程。
        response,_=self.callback(params['state'][0])
        self.assertEqual(response.status_code,303)

    def test_callback_is_single_use(self):
        params=self.start()
        state=params['state'][0]
        self.assertEqual(self.callback(state)[0].status_code,303)
        response,provider=self.callback(state)
        self.assertEqual(response.status_code,400)
        provider.assert_not_called()

    def test_expired_flow_and_duplicate_state_are_rejected(self):
        params=self.start()
        self.query('UPDATE auth_flows SET expires_at=0')
        response,provider=self.callback(params['state'][0])
        self.assertEqual(response.status_code,400)
        provider.assert_not_called()
        with patch('auth.provider_json') as provider:
            response=self.client.get('/auth/feishu/callback?state=a&state=b&code=c')
        self.assertEqual(response.status_code,400)
        provider.assert_not_called()

    def test_user_cancel_is_understandable_and_no_login(self):
        params=self.start()
        response,provider=self.callback(params['state'][0],error='access_denied')
        self.assertIn('取消飛書授權',response.get_data(as_text=True))
        provider.assert_not_called()
        self.assertEqual(self.client.get('/api/v1/state').status_code,401)

    def test_other_enterprise_cannot_login(self):
        params=self.start()
        identity={'code':0,'data':{**IDENTITY['data'],'tenant_key':'another_company'}}
        response,_=self.callback(params['state'][0],[TOKEN,identity])
        self.assertEqual(response.status_code,403)
        self.assertIn('不在本系統允許範圍',response.get_data(as_text=True))
        self.assertEqual(self.query('SELECT * FROM auth_sessions'),[])

    def test_unknown_enterprise_is_shown_for_confirmation_but_not_automatically_trusted(self):
        self.app.config['FEISHU_TENANT_KEY']=''
        params=self.start()
        response,_=self.callback(params['state'][0])
        self.assertEqual(response.status_code,403)
        self.assertIn('company_tenant',response.get_data(as_text=True))
        self.assertIn('FEISHU_TENANT_KEY',response.get_data(as_text=True))
        self.assertEqual(self.query('SELECT * FROM auth_users'),[])
        self.assertEqual(self.client.get('/api/v1/backup').status_code,401)

    def test_provider_failures_never_reveal_secrets(self):
        for code in [20002,20003,20009,20010,20021,20069]:
            with self.subTest(code=code):
                params=self.start()
                response,_=self.callback(params['state'][0],[{'code':code,'error':'invalid','error_description':self.config['FEISHU_APP_SECRET']}])
                self.assertIn(response.status_code,[400,403])
                self.assertNotIn(self.config['FEISHU_APP_SECRET'],response.get_data(as_text=True))
                self.assertEqual(self.client.get('/api/v1/state').status_code,401)

    def test_network_failure_can_retry_from_new_authorization(self):
        params=self.start()
        response,_=self.callback(params['state'][0],[LoginError('provider_unavailable')])
        self.assertIn('暫時無法連線',response.get_data(as_text=True))
        self.login()

    def test_csrf_protects_json_upload_and_logout(self):
        me,_,_=self.login()
        self.assertEqual(self.client.post('/api/v1/leads',json={'company':'不應寫入'}).status_code,403)
        self.assertEqual(self.client.post('/api/v1/contracts/1/attachments',data={'kind':'signed','file':(io.BytesIO(b'%PDF-1.4'),'a.pdf')}).status_code,403)
        self.assertEqual(self.client.post('/api/v1/auth/logout',json={}).status_code,403)
        headers={'X-CSRF-Token':me['csrf_token']}
        response=self.client.post('/api/v1/leads',json={'company':'已驗證公司'},headers=headers)
        self.assertEqual(response.status_code,201)
        record=json.loads(self.query("SELECT detail FROM audit_log WHERE entity='lead'")[0]['detail'])
        self.assertEqual(record['operator']['name'],'林小姐')
        self.assertEqual(self.client.post('/api/v1/leads',json={'company':'跨站'},headers={**headers,'Origin':'https://other.example'}).status_code,403)

    def test_logout_revokes_copied_cookie_and_cannot_use_get(self):
        me,_,_=self.login()
        copied=self.client.get_cookie(SESSION_COOKIE).value
        self.assertEqual(self.client.get('/api/v1/auth/logout').status_code,405)
        response=self.client.post('/api/v1/auth/logout',json={},headers={'X-CSRF-Token':me['csrf_token']})
        self.assertEqual(response.status_code,200)
        self.assertEqual(self.query('SELECT * FROM auth_sessions'),[])
        self.client.set_cookie(SESSION_COOKIE,copied)
        self.assertEqual(self.client.get('/api/v1/state').status_code,401)

    def test_expired_and_disabled_user_sessions_are_rejected(self):
        self.login()
        self.query('UPDATE auth_sessions SET expires_at=0')
        self.assertEqual(self.client.get('/api/v1/state').status_code,401)
        self.login()
        self.query('UPDATE auth_users SET active=0')
        self.assertEqual(self.client.get('/api/v1/state').status_code,401)
        params=self.start()
        self.assertEqual(self.callback(params['state'][0])[0].status_code,403)
        self.assertEqual(self.query('SELECT active FROM auth_users')[0]['active'],0)

    def test_config_change_revokes_existing_sessions(self):
        self.login()
        self.app.config['FEISHU_TENANT_KEY']='another_company'
        self.assertEqual(self.client.get('/api/v1/state').status_code,401)

    def test_session_survives_restart(self):
        self.login()
        cookie=self.client.get_cookie(SESSION_COOKIE).value
        restarted=create_app(self.config).test_client()
        restarted.set_cookie(SESSION_COOKIE,cookie)
        self.assertEqual(restarted.get('/api/v1/state').status_code,200)

    def test_https_proxy_session_cookie_and_mutation(self):
        app=create_app({**self.config,'TRUST_PROXY':True,'FEISHU_REDIRECT_URI':'https://signing.example/auth/feishu/callback'})
        client=app.test_client()
        # 連到本機代理的 HTTP，由已受信任代理轉送外部的 HTTPS 主機資訊。
        headers={'X-Forwarded-Host':'signing.example','X-Forwarded-Proto':'https'}
        response=client.get('/auth/feishu/start',base_url='http://signing.example',headers=headers)
        state=parse_qs(urlsplit(response.location).query)['state'][0]
        with patch('auth.provider_json',side_effect=[TOKEN,IDENTITY]):
            response=client.get('/auth/feishu/callback',base_url='http://signing.example',headers=headers,query_string={'state':state,'code':'code'})
        self.assertEqual(response.status_code,303)
        self.assertIn('Secure',response.headers['Set-Cookie'])
        me=client.get('/api/v1/auth/me',base_url='http://signing.example',headers=headers).get_json()
        response=client.post('/api/v1/leads',base_url='http://signing.example',json={'company':'HTTPS代理測試'},headers={**headers,'Origin':'https://signing.example','X-CSRF-Token':me['csrf_token']})
        self.assertEqual(response.status_code,201,response.get_json())

    def test_malformed_provider_error_is_handled_without_crashing(self):
        params=self.start()
        response,_=self.callback(params['state'][0],[{'code':{},'error_description':'bad response'}])
        self.assertEqual(response.status_code,400)

    def test_repeated_login_maps_by_id_not_name(self):
        self.login()
        identity={'code':0,'data':{**IDENTITY['data'],'name':'新的顯示姓名'}}
        params=self.start()
        self.callback(params['state'][0],[TOKEN,identity])
        self.assertEqual(len(self.query('SELECT * FROM auth_users')),1)
        self.assertEqual(self.client.get('/api/v1/auth/me').get_json()['user']['name'],'新的顯示姓名')
        self.assertEqual(len(self.query('SELECT * FROM auth_sessions')),1)
        identity={'code':0,'data':{**IDENTITY['data'],'open_id':'ou_different_employee','name':'新的顯示姓名'}}
        self.callback(self.start()['state'][0],[TOKEN,identity])
        self.assertEqual(len(self.query('SELECT * FROM auth_users')),2)
        self.assertEqual(self.query('SELECT * FROM reps'),[])

    def test_only_safe_internal_return_routes(self):
        for value in ['https://evil.example','//evil.example','/\\evil.example','/%2f%2fevil.example','/#/\nhttps://evil.example']:
            self.assertEqual(safe_next(value),'/')
        self.assertEqual(safe_next('/#/customer/5'),'/#/customer/5')
        params=self.start('//evil.example')
        response,_=self.callback(params['state'][0])
        self.assertEqual(response.location,'/')

    def test_secure_cookie_with_https_proxy_and_origin_check(self):
        self.app.config.update(FEISHU_REDIRECT_URI='https://signing.example/auth/feishu/callback')
        # 錯誤入口不建立流程，避免 localhost / 127.0.0.1 混用。
        self.assertEqual(self.client.get('/auth/feishu/start').status_code,400)
        response=self.client.get('/auth/feishu/start',base_url='https://signing.example')
        self.assertEqual(response.status_code,302)
        self.assertIn('Secure',response.headers['Set-Cookie'])
        self.assertIn('HttpOnly',response.headers['Set-Cookie'])
        self.assertEqual(response.headers['Referrer-Policy'],'no-referrer')

    def test_invalid_identity_and_token_lifetimes_fail_closed(self):
        for field,value in [('open_id',''),('tenant_key',''),('name',None)]:
            params=self.start()
            identity={'code':0,'data':{**IDENTITY['data'],field:value}}
            self.assertEqual(self.callback(params['state'][0],[TOKEN,identity])[0].status_code,400)
        for expires in [0,-1,'7200',None,True]:
            params=self.start()
            response,_=self.callback(params['state'][0],[{**TOKEN,'expires_in':expires}])
            self.assertEqual(response.status_code,400)
        self.assertEqual(self.query('SELECT * FROM auth_sessions'),[])

    def test_bypass_is_forbidden_outside_tests(self):
        with self.assertRaises(RuntimeError):
            create_app({**self.config,'TESTING':False,'AUTH_TEST_BYPASS':True})

    def test_official_transport_is_form_encoded_and_does_not_follow_redirects(self):
        fake=io.BytesIO(json.dumps(TOKEN).encode())
        with patch('auth.build_opener') as opener:
            opener.return_value.open.return_value=fake
            result=provider_json(V3_TOKEN_URL,form={'client_id':'cli_test','client_secret':'a+b&c','code':'test'})
        self.assertEqual(result,TOKEN)
        req=opener.return_value.open.call_args.args[0]
        self.assertEqual(req.full_url,'https://accounts.feishu.cn/oauth/v3/token')
        self.assertEqual(req.get_header('Content-type'),'application/x-www-form-urlencoded')
        self.assertEqual(parse_qs(req.data.decode())['client_secret'],['a+b&c'])
        with patch('auth.build_opener') as opener:
            opener.return_value.open.side_effect=URLError('network failed')
            with self.assertRaises(LoginError): provider_json(TOKEN_URL,form={})
        with self.assertRaises(LoginError): provider_json('https://evil.example',bearer='secret')

    def test_pkce_token_transport_uses_official_json_format(self):
        with patch('auth.build_opener') as opener:
            opener.return_value.open.return_value=io.BytesIO(json.dumps(TOKEN).encode())
            form={'client_id':'cli_test','client_secret':'not-for-log','code':'auth_code','code_verifier':'correct_verifier'}
            result=provider_json(TOKEN_URL,form=form,as_json=True)
        req=opener.return_value.open.call_args.args[0]
        self.assertEqual(req.get_header('Content-type'),'application/json; charset=utf-8')
        self.assertEqual(json.loads(req.data),form)
        self.assertEqual(result,TOKEN)

    def test_diagnostics_identify_failed_stage_without_leaking_provider_values(self):
        response={'code':20049,'error':'invalid_grant','access_token':'token-must-not-be-logged',
                  'error_description':'secret-must-not-be-logged','data':{'access_token':'nested-secret'}}
        metadata=json.dumps(provider_diagnostics(response))
        for secret in ['token-must-not-be-logged','secret-must-not-be-logged','nested-secret']:
            self.assertNotIn(secret,metadata)
        params=self.start()
        with self.assertLogs(self.app.logger,level='WARNING') as logs:
            result,_=self.callback(params['state'][0],[response])
        self.assertEqual(result.status_code,400)
        output='\n'.join(logs.output)
        self.assertIn('exchange_token',output)
        self.assertIn('20049',output)
        self.assertIn('20049',result.get_data(as_text=True))
        self.assertNotIn('token-must-not-be-logged',output)
        self.assertNotIn('secret-must-not-be-logged',output)

    def test_local_access_log_redacts_oauth_query(self):
        handler=object.__new__(AuthRedactedRequestHandler)
        handler.path='/auth/feishu/callback?code=private-code&state=private-state'
        original=handler.path
        handler.command='GET'
        handler.request_version='HTTP/1.1'
        messages=[]
        handler.log=lambda level,fmt,*args: messages.append(fmt % args)
        handler.log_request(400)
        output='\n'.join(messages)
        self.assertIn('[redacted]',output)
        self.assertNotIn('private-code',output)
        self.assertNotIn('private-state',output)
        self.assertEqual(handler.path,original)

    def test_dotenv_handles_bom_and_literal_secret_without_execution(self):
        (Path(self.tmp.name)/'.env').write_text('\ufeffFEISHU_APP_ID=cli_file\nFEISHU_APP_SECRET="a=b#c$dont-expand"\nIGNORED=value\n',encoding='utf-8')
        with patch.dict('os.environ',{},clear=True):
            values=feishu_settings(self.tmp.name)
            self.assertEqual(values['FEISHU_APP_SECRET'],'a=b#c$dont-expand')
            self.assertNotIn('IGNORED',values)
        with patch.dict('os.environ',{'FEISHU_APP_ID':'cli_environment'}):
            self.assertEqual(feishu_settings(self.tmp.name)['FEISHU_APP_ID'],'cli_environment')


if __name__=='__main__':
    unittest.main()
