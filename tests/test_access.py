import io
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
import zipfile

from app import create_app, SCHEMA as BUSINESS_SCHEMA
from auth import SCHEMA as AUTH_SCHEMA
from backup import build_backup


class AccessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='signing-access-')
        self.config = dict(TESTING=True, LOAD_ENV_FILE=False, DATA_DIR=self.tmp.name,
                           FEISHU_APP_ID='cli_access', FEISHU_APP_SECRET='only-test-secret',
                           FEISHU_TENANT_KEY='test_company', FEISHU_REDIRECT_URI='http://localhost/auth/feishu/callback')
        self.app = create_app(self.config)

    def tearDown(self):
        self.tmp.cleanup()

    def query(self, sql, params=()):
        with closing(sqlite3.connect(Path(self.tmp.name)/'database.sqlite3')) as db, db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute(sql, params)]

    def login(self, name, account, client=None):
        client = client or self.app.test_client()
        start = client.get('/auth/feishu/start')
        state = parse_qs(urlsplit(start.location).query)['state'][0]
        with patch('auth.provider_json', side_effect=[
            {'code':0,'access_token':'test-token','expires_in':7200},
            {'code':0,'data':{'name':name,'open_id':account,'tenant_key':'test_company'}}
        ]):
            response = client.get('/auth/feishu/callback', query_string={'code':'code','state':state})
        self.assertEqual(response.status_code, 303, response.get_data(as_text=True))
        me = client.get('/api/v1/auth/me').get_json()
        return client, me, response

    def approve(self, admin, admin_me, user_id, role='sales', **extra):
        users = admin.get('/api/v1/admin/users').get_json()['users']
        user = next(u for u in users if u['id'] == user_id)
        response = admin.post(f'/api/v1/admin/users/{user_id}/role',
                              json={'role':role,'version':user['role_version'],**extra},
                              headers={'X-CSRF-Token':admin_me['csrf_token']})
        self.assertEqual(response.status_code, 200, response.get_json())

    def team(self):
        admin, admin_me, _ = self.login('林育丰', 'ou_admin')
        a, a_me, _ = self.login('業務甲', 'ou_sales_a')
        b, b_me, _ = self.login('業務乙', 'ou_sales_b')
        for me in [a_me, b_me]: self.approve(admin, admin_me, me['user']['id'])
        return (admin, admin_me), (a, a_me), (b, b_me)

    def post(self, client, me, path, values):
        return client.post('/api/v1'+path, json=values, headers={'X-CSRF-Token':me['csrf_token']})

    def lead(self, client, me, **extra):
        response = self.post(client, me, '/leads', {'company':'權限測試公司','estimate':'10',**extra})
        self.assertEqual(response.status_code, 201, response.get_json())
        return response.get_json()['id']

    def delete(self, client, me, lead, version=1):
        return client.delete(f'/api/v1/leads/{lead}',json={'version':version},headers={'X-CSRF-Token':me['csrf_token']})

    def test_new_user_waits_and_cannot_read_business_files_or_write(self):
        client, me, response = self.login('新同事', 'ou_pending')
        self.assertTrue(response.location.startswith('/waiting'))
        self.assertFalse(me['approved'])
        self.assertEqual(me['user']['role'], 'pending')
        self.assertEqual(client.get('/').location, '/waiting')
        self.assertIn('等待管理員分配身分',client.get('/waiting').get_data(as_text=True))
        for path in ['/state','/contracts','/backup','/targets','/attachments/no-file/file','/admin/users']:
            r = client.get('/api/v1'+path)
            self.assertEqual(r.status_code, 403, path)
            self.assertTrue(r.get_json()['approval_required'])
        self.assertEqual(self.post(client, me, '/leads', {'company':'不能建立'}).status_code,403)
        self.assertEqual(self.query('SELECT * FROM customers'), [])
        self.assertEqual(self.post(client, me, '/auth/logout', {}).status_code,200)
        self.assertEqual(client.get('/api/v1/auth/me').status_code,401)

    def test_initial_admins_bind_once_and_same_name_does_not_grant_again(self):
        for name, account in [('林育丰','ou_first'),('涂是淦','ou_second')]:
            _, me, _ = self.login(name, account)
            self.assertEqual(me['user']['role'],'admin')
        _, impersonator, _ = self.login('林育丰','ou_other_person')
        self.assertEqual(impersonator['user']['role'],'pending')
        _, renamed, _ = self.login('改名後的管理員','ou_first')
        self.assertEqual(renamed['user']['role'],'admin')

    def test_approval_takes_effect_without_login_and_manual_rep_creation_is_gone(self):
        admin, admin_me, _ = self.login('林育丰','ou_admin')
        sales, sales_me, _ = self.login('業務甲','ou_sales')
        self.approve(admin,admin_me,sales_me['user']['id'])
        me = sales.get('/api/v1/auth/me').get_json()
        self.assertTrue(me['approved'])
        state = sales.get('/api/v1/state').get_json()
        self.assertEqual(state['reps'][0]['name'],'業務甲')
        self.assertEqual(state['reps'][0]['auth_user_id'],sales_me['user']['id'])
        self.assertTrue(state['reps'][0]['selectable'])
        self.assertEqual(sales.get('/api/v1/admin/users').status_code,403)
        self.assertEqual(self.post(sales,sales_me,'/reps',{'name':'手動姓名'}).status_code,403)
        self.assertEqual(self.post(admin,admin_me,'/reps',{'name':'手動姓名'}).status_code,410)

    def test_same_name_accounts_are_separate_and_renaming_keeps_rep_id(self):
        admin, admin_me, _ = self.login('林育丰','ou_admin')
        a, a_me, _ = self.login('同名業務','ou_a')
        b, b_me, _ = self.login('同名業務','ou_b')
        for me in [a_me,b_me]: self.approve(admin,admin_me,me['user']['id'])
        reps = self.query('SELECT * FROM reps ORDER BY id')
        self.assertEqual([r['name'] for r in reps],['同名業務','同名業務'])
        self.assertNotEqual(reps[0]['auth_user_id'],reps[1]['auth_user_id'])
        self.login('新姓名','ou_a',a)
        self.assertEqual(self.query('SELECT id,name FROM reps WHERE auth_user_id=?',(a_me['user']['id'],)),[{'id':reps[0]['id'],'name':'新姓名'}])
        self.assertEqual(self.query('SELECT COUNT(*) n FROM reps')[0]['n'],2)

    def test_role_change_conflict_last_admin_and_immediate_revocation(self):
        admin, admin_me, _ = self.login('林育丰','ou_admin')
        own = admin.get('/api/v1/admin/users').get_json()['users'][0]
        response = self.post(admin,admin_me,f"/admin/users/{own['id']}/role",{'role':'pending','version':own['role_version']})
        self.assertEqual(response.status_code,409)
        sales, me, _ = self.login('業務甲','ou_sales')
        original_version=me['user']['role_version']
        self.approve(admin,admin_me,me['user']['id'])
        rep_id=sales.get('/api/v1/auth/me').get_json()['user']['rep_id']
        stale=self.post(admin,admin_me,f"/admin/users/{me['user']['id']}/role",{'role':'admin','version':original_version})
        self.assertEqual(stale.status_code,409)
        self.approve(admin,admin_me,me['user']['id'],'pending')
        self.assertEqual(sales.get('/api/v1/state').status_code,403)
        self.assertFalse(admin.get('/api/v1/state').get_json()['reps'][0]['selectable'])
        self.approve(admin,admin_me,me['user']['id'])
        self.assertEqual(sales.get('/api/v1/auth/me').get_json()['user']['rep_id'],rep_id)
        self.assertEqual(self.query('SELECT COUNT(*) n FROM reps')[0]['n'],1)

    def test_legacy_rep_can_be_claimed_explicitly_without_changing_customer_id(self):
        admin, admin_me, _ = self.login('林育丰','ou_admin')
        self.query("INSERT INTO reps(id,name,created_at) VALUES(88,'原手動姓名','2026-10-28')")
        self.query("INSERT INTO customers(id,company,rep_id,stage,created_at) VALUES(66,'原客戶',88,1,'2026-10-28')")
        sales, me, _ = self.login('真實飛書姓名','ou_sales')
        self.approve(admin,admin_me,me['user']['id'],legacy_rep_id=88)
        state=sales.get('/api/v1/state').get_json()
        self.assertEqual(state['reps'][0]['id'],88)
        self.assertEqual(state['reps'][0]['name'],'真實飛書姓名')
        self.assertEqual(state['customers'][0]['id'],66)
        self.assertEqual(state['customers'][0]['rep_id'],88)
        other, other_me, _ = self.login('另一人','ou_other')
        user=other_me['user']
        response=self.post(admin,admin_me,f"/admin/users/{user['id']}/role",{'role':'sales','version':user['role_version'],'legacy_rep_id':88})
        self.assertEqual(response.status_code,409)
        self.assertEqual(other.get('/api/v1/auth/me').get_json()['user']['role'],'pending')

    def test_sales_deletes_only_own_unassigned_leads_and_cannot_spoof_creator(self):
        (admin,am),(a,ma),(b,mb)=self.team()
        lead=self.lead(a,ma,registered_by=mb['user']['id'])
        ca=next(c for c in a.get('/api/v1/state').get_json()['customers'] if c['id']==lead)
        cb=b.get('/api/v1/state').get_json()['customers']
        self.assertEqual(ca['registered_by'],ma['user']['id'])
        self.assertTrue(ca['can_delete']); self.assertNotIn(lead,[c['id'] for c in cb])
        self.assertEqual(self.delete(b,mb,lead).status_code,404)
        self.assertEqual(self.delete(a,ma,lead,version=999).status_code,409)
        self.assertEqual(self.delete(a,ma,lead).status_code,200)
        another=self.lead(b,mb)
        self.assertEqual(self.delete(admin,am,another).status_code,200)

    def test_assigned_customers_and_contracts_are_not_deletable_even_by_admin(self):
        (admin,am),(a,ma),_=self.team()
        lead=self.lead(a,ma)
        rep=a.get('/api/v1/auth/me').get_json()['user']['rep_id']
        self.assertEqual(self.post(admin,am,f'/leads/{lead}/assign',{'rep_id':rep}).status_code,200)
        for client, me in [(a,ma),(admin,am)]:
            self.assertEqual(self.delete(client,me,lead,version=2).status_code,400)
            self.assertEqual(client.delete(f'/api/v1/customers/{lead}',headers={'X-CSRF-Token':me['csrf_token']}).status_code,405)
        self.assertEqual(self.post(a,ma,'/contracts',{'customer_id':lead,'rep_id':rep,'title':'應保留','amount':'1'}).status_code,201)
        state=admin.get('/api/v1/state').get_json()
        self.assertFalse(state['customers'][0]['can_delete'])
        self.assertEqual(len(state['contracts']),1)

    def test_unknown_historical_creator_is_deletable_only_by_admin(self):
        (admin,am),(a,ma),_=self.team()
        self.query("INSERT INTO customers(id,company,created_at) VALUES(88,'舊線索','2026-10-28')")
        self.assertEqual(self.delete(a,ma,88).status_code,404)
        self.assertEqual(self.delete(admin,am,88).status_code,200)

    def test_role_assignment_checks_admin_csrf_and_input_before_changing_access(self):
        (admin,am),(a,ma),(b,mb)=self.team()
        target=b.get('/api/v1/auth/me').get_json()['user']
        endpoint=f"/admin/users/{target['id']}/role"
        payload={'role':'admin','version':target['role_version']}
        self.assertEqual(self.post(a,ma,endpoint,payload).status_code,403)
        self.assertEqual(admin.post('/api/v1'+endpoint,json=payload).status_code,403)
        for invalid in [[],{},None,True,'owner']:
            self.assertEqual(self.post(admin,am,endpoint,{**payload,'role':invalid}).status_code,400)
        self.assertEqual(b.get('/api/v1/auth/me').get_json()['user']['role'],'sales')
        self.approve(admin,am,target['id'],'admin')
        self.assertEqual(b.get('/api/v1/admin/users').status_code,200)
        self.assertFalse(next(r for r in admin.get('/api/v1/state').get_json()['reps'] if r['auth_user_id']==target['id'])['selectable'])

    def test_initial_admin_bindings_are_not_regranted_after_explicit_role_change(self):
        admin,am,_=self.login('林育丰','ou_admin')
        self.login('涂是淦','ou_second_admin')
        self.approve(admin,am,am['user']['id'],'pending')
        self.app=create_app(self.config)
        user=self.query("SELECT role FROM auth_users WHERE open_id='ou_admin'")[0]
        self.assertEqual(user['role'],'pending')

    def test_initial_sales_override_binds_real_account_once_and_survives_restart(self):
        # 另行明確指定的首次身分仍可支援；目前李海兵的預設流程是等待審核。
        from access import INITIAL_ADMINS
        self.config['INITIAL_ADMINS']={**INITIAL_ADMINS,'李海兵':('李海兵',)}
        self.app=create_app(self.config)
        self.query("UPDATE initial_admin_bindings SET initial_role='sales' WHERE name='李海兵'")
        client,me,response=self.login('李海兵','ou_li_sales')
        self.assertEqual(response.location,'/')
        self.assertEqual(me['user']['role'],'sales')
        self.assertIsNotNone(me['user']['rep_id'])
        self.assertEqual(client.get('/api/v1/admin/users').status_code,403)
        rep_id=me['user']['rep_id']
        self.app=create_app(self.config)
        client,again,_=self.login('李海兵','ou_li_sales')
        self.assertEqual(again['user']['role'],'sales')
        self.assertEqual(again['user']['rep_id'],rep_id)
        self.assertEqual(self.query('SELECT COUNT(*) n FROM reps')[0]['n'],1)
        _,other,_=self.login('李海兵','ou_other_li')
        self.assertEqual(other['user']['role'],'pending')

    def test_li_haibing_waits_for_assignment_then_becomes_sales(self):
        admin,am,_=self.login('林育丰','ou_admin')
        client,me,response=self.login('李海兵','ou_li_waiting')
        self.assertTrue(response.location.startswith('/waiting'))
        self.assertEqual(me['user']['role'],'pending')
        self.assertIsNone(me['user']['rep_id'])
        self.assertEqual(client.get('/api/v1/state').status_code,403)
        self.approve(admin,am,me['user']['id'],'sales')
        approved=client.get('/api/v1/auth/me').get_json()
        self.assertEqual(approved['user']['role'],'sales')
        self.assertIsNotNone(approved['user']['rep_id'])
        self.assertEqual(client.get('/api/v1/state').status_code,200)
        self.assertEqual(client.get('/api/v1/admin/users').status_code,403)
        self.app=create_app(self.config)
        _,again,_=self.login('李海兵','ou_li_waiting')
        self.assertEqual(again['user']['role'],'sales')
        self.assertEqual(again['user']['rep_id'],approved['user']['rep_id'])

    def test_old_database_migrates_preserving_foreign_keys_contracts_and_files(self):
        old_dir=Path(self.tmp.name)/'legacy'; old_dir.mkdir(); (old_dir/'uploads').mkdir()
        with closing(sqlite3.connect(old_dir/'database.sqlite3')) as db, db:
            db.executescript(BUSINESS_SCHEMA); db.executescript(AUTH_SCHEMA)
            db.execute("INSERT INTO reps(id,name,created_at) VALUES(7,'小王','2026-10-28')")
            db.execute("INSERT INTO customers(id,company,rep_id,stage,created_at) VALUES(9,'歷史客戶',7,5,'2026-10-28')")
            db.execute("""INSERT INTO contracts(id,customer_id,number,title,amount_cents,rep_id,status,signed_at,created_at,updated_at)
                VALUES(11,9,'HT-OLD','舊合同',100,7,'signed','2026-10-28','2026-10-28','2026-10-28')""")
            db.execute("INSERT INTO attachments VALUES('file-id',11,'old.pdf','old.pdf','application/pdf',4,'signed','2026-10-28')")
            db.execute("INSERT INTO auth_users(app_id,tenant_key,open_id,name,created_at,last_login_at) VALUES('cli_access','test_company','ou_old','林育丰',1,1)")
        (old_dir/'uploads/old.pdf').write_bytes(b'%PDF')
        migrated=create_app({**self.config,'DATA_DIR':str(old_dir)})
        with migrated.app_context():
            access=migrated.extensions['access_control']; db=access.db()
            self.assertEqual(db.execute('PRAGMA foreign_keys').fetchone()[0],1)
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])
            self.assertEqual(db.execute('SELECT rep_id FROM customers WHERE id=9').fetchone()[0],7)
            self.assertEqual(db.execute('SELECT customer_id,rep_id FROM contracts WHERE id=11').fetchone()[:],(9,7))
            self.assertEqual(db.execute('SELECT role FROM auth_users').fetchone()[0],'admin')
        create_app({**self.config,'DATA_DIR':str(old_dir)})
        with zipfile.ZipFile(build_backup(old_dir)) as archive:
            self.assertEqual(archive.read('uploads/old.pdf'),b'%PDF')

    def test_multiple_roles_bind_sales_and_preserve_administrator_access(self):
        admin,am,_=self.login('林育丰','ou_admin')
        target=admin.get('/api/v1/auth/me').get_json()['user']
        response=self.post(admin,am,f"/admin/users/{target['id']}/role",{'roles':['admin','sales'],'version':target['role_version']})
        self.assertEqual(response.status_code,200,response.get_json())
        me=admin.get('/api/v1/auth/me').get_json()['user']
        self.assertEqual(me['roles'],['admin','sales'])
        self.assertEqual(me['role_label'],'管理員兼業務員')
        self.assertIsNotNone(me['rep_id'])
        self.assertEqual(admin.get('/api/v1/admin/users').status_code,200)
        self.assertTrue(next(r for r in admin.get('/api/v1/state').get_json()['reps'] if r['id']==me['rep_id'])['selectable'])
        self.app=create_app(self.config)
        _,again,_=self.login('林育丰','ou_admin')
        self.assertEqual(again['user']['roles'],['admin','sales'])
        self.assertEqual(again['user']['rep_id'],me['rep_id'])

    def test_sales_scope_moves_with_assignment_and_protects_all_related_endpoints(self):
        (admin,am),(a,ma),(b,mb)=self.team()
        a_lead=self.lead(a,ma,company='A自己未分配')
        transferred=self.lead(a,ma,company='A登記轉交B')
        foreign=self.lead(b,mb,company='B自己未分配')
        rep_b=b.get('/api/v1/auth/me').get_json()['user']['rep_id']
        self.assertEqual(self.post(admin,am,f'/leads/{transferred}/assign',{'rep_id':rep_b}).status_code,200)
        self.assertEqual([c['id'] for c in a.get('/api/v1/state').get_json()['customers']],[a_lead])
        self.assertEqual({c['id'] for c in b.get('/api/v1/state').get_json()['customers']},{transferred,foreign})
        created=self.post(b,mb,'/contracts',{'customer_id':transferred,'rep_id':rep_b,'title':'B專用合同','amount':'50'})
        ct=created.get_json()['id']
        pdf=b'%PDF-1.4\n%%EOF'
        upload=b.post(f'/api/v1/contracts/{ct}/attachments',data={'kind':'signed','file':(io.BytesIO(pdf),'test.pdf')},headers={'X-CSRF-Token':mb['csrf_token']})
        self.assertEqual(upload.status_code,201,upload.get_json())
        state_b=b.get('/api/v1/state').get_json()
        contract=state_b['contracts'][0];attachment=contract['attachments'][0]
        self.assertEqual(a.get(attachment['url']).status_code,404)
        self.assertEqual(a.get(f'/api/v1/customers/{transferred}/followups').status_code,404)
        self.assertEqual(a.patch(f'/api/v1/customers/{transferred}',json={'company':'不可修改','version':2},headers={'X-CSRF-Token':ma['csrf_token']}).status_code,404)
        self.assertEqual(self.post(a,ma,f'/contracts/{ct}/complete',{'signed_at':'2026-10-29','version':contract['version']}).status_code,404)
        forbidden_upload=a.post(f'/api/v1/contracts/{ct}/attachments',data={'kind':'signed','file':(io.BytesIO(pdf),'forbidden.pdf')},headers={'X-CSRF-Token':ma['csrf_token']})
        self.assertEqual(forbidden_upload.status_code,404)
        self.assertEqual(self.post(b,mb,f'/contracts/{ct}/complete',{'signed_at':'2026-10-29','version':contract['version']}).status_code,200)
        scope_a=a.get('/api/v1/state?year=2026').get_json()
        self.assertEqual(scope_a['contracts'],[])
        self.assertEqual(scope_a['recent'],[])
        self.assertEqual(float(scope_a['summary']['signed_amount']),0)
        self.assertEqual(float(scope_a['targets']['signed_amount']),0)
        self.assertTrue(all(r['auth_user_id']==ma['user']['id'] for r in scope_a['reps']))
        self.assertEqual(a.get('/api/v1/backup').status_code,403)
        self.assertEqual(a.put('/api/v1/targets/2026',json={'amount':'999'},headers={'X-CSRF-Token':ma['csrf_token']}).status_code,403)
        self.assertEqual(float(b.get('/api/v1/targets?year=2026').get_json()['signed_amount']),50)
        with admin.get('/api/v1/backup') as backup:self.assertEqual(backup.status_code,200)

    def test_role_checkbox_validation_and_last_admin_protection(self):
        admin,am,_=self.login('林育丰','ou_admin')
        user=admin.get('/api/v1/auth/me').get_json()['user']
        endpoint=f"/admin/users/{user['id']}/role"
        for invalid in [['admin','admin'],['unknown'],{},'sales',[True]]:
            self.assertEqual(self.post(admin,am,endpoint,{'roles':invalid,'version':user['role_version']}).status_code,400)
        self.assertEqual(self.post(admin,am,endpoint,{'roles':[],'version':user['role_version']}).status_code,409)


    def test_returned_customer_waits_for_admin_and_access_moves_to_new_owner(self):
        (admin,am),(a,ma),(b,mb)=self.team()
        lead=self.lead(a,ma)
        rep_a=a.get('/api/v1/auth/me').get_json()['user']['rep_id']
        rep_b=b.get('/api/v1/auth/me').get_json()['user']['rep_id']
        self.post(admin,am,f'/leads/{lead}/assign',{'rep_id':rep_b})
        ct=self.post(b,mb,'/contracts',{'customer_id':lead,'rep_id':rep_b,'title':'歷史合同','amount':'50'}).get_json()['id']
        c=b.get('/api/v1/state').get_json()['customers'][0]
        values={'version':c['version'],'reason':'客戶需求中止'}
        self.assertEqual(self.post(a,ma,f'/customers/{lead}/lose',values).status_code,404)
        self.assertEqual(self.post(b,mb,f'/customers/{lead}/lose',values).status_code,200)
        for client,me in [(a,ma),(b,mb)]:
            self.assertEqual(client.get('/api/v1/state').get_json()['customers'],[])
            self.assertEqual(client.get('/api/v1/contracts').get_json(),[])
            self.assertEqual(client.get(f'/api/v1/customers/{lead}/followups').status_code,404)
            self.assertEqual(self.post(client,me,f'/leads/{lead}/assign',{'rep_id':rep_a}).status_code,404)
        self.assertEqual(admin.get('/api/v1/state').get_json()['customers'][0]['stage'],0)
        self.assertEqual(self.post(admin,am,f'/leads/{lead}/assign',{'rep_id':rep_a}).status_code,200)
        self.assertEqual(a.get('/api/v1/state').get_json()['contracts'][0]['status'],'lost')
        self.assertEqual(a.get(f'/api/v1/customers/{lead}/followups').status_code,200)
        self.assertEqual(b.get(f'/api/v1/customers/{lead}/followups').status_code,404)


if __name__=='__main__': unittest.main()
