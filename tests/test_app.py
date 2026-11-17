import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
import zipfile
from datetime import date
from unittest.mock import patch

from app import create_app, money
from backup import build_backup
from fixtures import seed_sales


PDF = b'%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF'


class SystemTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='signing-test-')
        self.path = Path(self.tmp.name)
        self.app = create_app({'TESTING': True, 'AUTH_TEST_BYPASS': True, 'LOAD_ENV_FILE': False, 'DATA_DIR': self.tmp.name})
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def state(self, year=None):
        return self.client.get('/api/v1/state' + (f'?year={year}' if year else '')).get_json()

    def post(self, url, data, status=200):
        result = self.client.post('/api/v1' + url, json=data)
        self.assertEqual(result.status_code, status, result.get_json())
        return result.get_json()

    def new_rep(self, name='測試業務員'):
        return seed_sales(self.app, name)

    def new_lead(self, company='測試公司', estimate='100', **extra):
        return self.post('/leads', {'company': company, 'estimate': estimate, 'contact': '陳先生',
                                   'phone': '0912345678', 'region': '華東', 'industry': '製造',
                                   'source': '轉介紹', 'description': '客戶實際需求', **extra}, 201)['id']

    def assigned(self, estimate='100'):
        rep = self.new_rep()
        lead = self.new_lead(estimate=estimate)
        self.post(f'/leads/{lead}/assign', {'rep_id': rep})
        return lead, rep

    def new_contract(self, customer, rep, amount='60', title='系統建置'):
        return self.post('/contracts', {'customer_id': customer, 'rep_id': rep, 'title': title, 'amount': amount}, 201)['id']

    def contract(self, contract_id):
        return next(c for c in self.state()['contracts'] if c['id'] == contract_id)

    def upload(self, contract_id, kind='signed', filename='已簽合同.pdf', content=PDF, status=201):
        response = self.client.post(f'/api/v1/contracts/{contract_id}/attachments',
                                    data={'kind': kind, 'file': (io.BytesIO(content), filename)},
                                    content_type='multipart/form-data')
        self.assertEqual(response.status_code, status, response.get_json())
        return response.get_json()

    def complete(self, contract_id, signed_at='2026-09-15', status=200):
        return self.post(f'/contracts/{contract_id}/complete',
                         {'signed_at': signed_at, 'version': self.contract(contract_id)['version']}, status)

    def test_initial_database_is_empty(self):
        s = self.state()
        self.assertEqual(s['customers'], [])
        self.assertEqual(s['contracts'], [])
        self.assertEqual(s['reps'], [])
        self.assertEqual(money(s['summary']['pending_amount']), 0)
        self.assertEqual(money(s['targets']['signed_amount']), 0)
        self.assertEqual(len(s['targets']['months']), 12)
        self.assertIsNone(s['targets']['done_progress'])
        self.assertTrue(all(money(m['actual']) == 0 for m in s['targets']['months']))

    def test_lead_assignment_preserves_fields_and_estimate(self):
        lead, rep = self.assigned('100.25')
        s = self.state()
        c = s['customers'][0]
        self.assertEqual((c['id'], c['rep_id'], c['stage']), (lead, rep, 1))
        self.assertEqual(c['phone'], '0912345678')
        self.assertEqual(c['region'], '華東')
        self.assertEqual(c['industry'], '製造')
        self.assertEqual(money(s['summary']['pending_amount']), money('100.25'))
        self.assertEqual(s['summary']['open_leads'], 0)
        followups = self.client.get(f'/api/v1/customers/{lead}/followups').get_json()
        self.assertEqual(followups[0]['kind'], '線索分配')

    def test_unassigned_lead_is_not_pending_business(self):
        self.new_lead(estimate='120')
        s = self.state()
        self.assertEqual(s['summary']['open_leads'], 1)
        self.assertEqual(money(s['summary']['pending_amount']), 0)
        self.assertEqual(money(s['summary']['open_leads_amount']), money('120'))

    def test_assignment_is_atomic_for_two_people(self):
        lead = self.new_lead()
        reps = [self.new_rep('業務甲'), self.new_rep('業務乙')]
        barrier = threading.Barrier(2)
        results = []
        def assign(rep):
            with self.app.test_client() as client:
                barrier.wait()
                results.append(client.post(f'/api/v1/leads/{lead}/assign', json={'rep_id': rep}).status_code)
        threads = [threading.Thread(target=assign, args=(rep,)) for rep in reps]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=20)
        self.assertEqual(sorted(results), [200, 409])
        self.assertEqual(len(self.client.get(f'/api/v1/customers/{lead}/followups').get_json()), 1)

    def test_multi_contract_amounts_no_double_counting(self):
        lead, rep = self.assigned('100')
        first = self.new_contract(lead, rep, '60')
        second = self.new_contract(lead, rep, '40')
        self.upload(first)
        self.complete(first)
        s = self.state(2026)
        self.assertEqual(len(s['customers']), 1)
        self.assertEqual(len(s['contracts']), 2)
        self.assertEqual(money(s['summary']['pending_amount']), money('40'))
        self.assertEqual(money(s['summary']['signed_amount']), money('60'))
        self.assertEqual(s['customers'][0]['stage'], 4)
        self.upload(second)
        self.complete(second)
        s = self.state()
        self.assertEqual(money(s['summary']['pending_amount']), 0)
        self.assertEqual(money(s['summary']['signed_amount']), money('100'))
        self.assertEqual(s['customers'][0]['stage'], 5)
        third = self.new_contract(lead, rep, '25')
        s = self.state()
        self.assertEqual(s['customers'][0]['stage'], 4)
        self.assertEqual(money(s['summary']['signed_amount']), money('100'))
        self.assertEqual(money(s['summary']['pending_amount']), money('25'))
        self.assertNotEqual(third, second)

    def test_first_contract_replaces_full_estimate(self):
        lead, rep = self.assigned('100')
        self.new_contract(lead, rep, '60')
        self.assertEqual(money(self.state()['summary']['pending_amount']), money('60'))

    def test_completion_requires_real_signed_attachment(self):
        lead, rep = self.assigned()
        ct = self.new_contract(lead, rep)
        self.complete(ct, status=400)
        self.upload(ct, kind='draft')
        self.complete(ct, status=400)
        self.upload(ct, kind='signed')
        self.assertEqual(self.contract(ct)['status'], 'signing')
        self.complete(ct)
        self.assertEqual(self.contract(ct)['status'], 'signed')

    def test_missing_file_cannot_complete_contract(self):
        lead, rep = self.assigned()
        ct = self.new_contract(lead, rep)
        self.upload(ct)
        next((self.path/'uploads').iterdir()).unlink()
        self.complete(ct, status=400)
        self.assertEqual(self.contract(ct)['status'], 'signing')

    def test_month_and_year_use_actual_signing_date(self):
        with patch('app.today', return_value=date(2026,10,28)):
            lead, rep = self.assigned()
            ct = self.new_contract(lead, rep, '60.123456')
            self.upload(ct)
            self.complete(ct, '2025-09-15')
            self.assertEqual(money(self.state(2026)['targets']['signed_amount']), 0)
            target = self.state(2025)['targets']
            self.assertEqual(money(target['signed_amount']), money('60.123456'))
            self.assertEqual(money(target['months'][8]['actual']), money('60.123456'))
            self.assertEqual(target['time_progress'], 1)

    def test_target_formula_and_persistence(self):
        with patch('app.today', return_value=date(2026,10,28)):
            response = self.client.put('/api/v1/targets/2026', json={'amount': '2400'})
            self.assertEqual(response.status_code, 200)
            target = self.state(2026)['targets']
            self.assertEqual(money(target['monthly_plan']), money('200'))
            self.assertAlmostEqual(target['time_progress'], (9+28/31)/12)
            self.assertEqual(target['done_progress'], 0)
            self.assertEqual(money(self.state(2027)['targets']['annual_target']), 0)
            restarted = create_app({'TESTING': True,'AUTH_TEST_BYPASS':True,'LOAD_ENV_FILE':False,'DATA_DIR': self.tmp.name}).test_client()
            self.assertEqual(money(restarted.get('/api/v1/targets?year=2026').get_json()['annual_target']), money('2400'))

    def test_followup_and_stage_do_not_change_contract_signing_date(self):
        lead, rep = self.assigned()
        ct = self.new_contract(lead, rep)
        self.upload(ct)
        self.complete(ct)
        self.post(f'/customers/{lead}/followups', {'kind':'面聊', 'note':'後續服務', 'occurred_on':'2026-10-20'},201)
        self.assertEqual(self.contract(ct)['signed_at'], '2026-09-15')
        self.assertEqual(self.state()['customers'][0]['meetings'], 1)
        self.post(f'/customers/{lead}/advance',{'version':self.state()['customers'][0]['version']},400)

    def test_only_signed_original_cannot_be_removed(self):
        lead, rep = self.assigned()
        ct = self.new_contract(lead, rep)
        first = self.upload(ct)['id']
        self.complete(ct)
        response = self.client.delete(f'/api/v1/attachments/{first}')
        self.assertEqual(response.status_code, 400)
        second = self.upload(ct, filename='更清晰的掃描件.pdf')['id']
        self.assertEqual(self.client.delete(f'/api/v1/attachments/{first}').status_code, 200)
        self.assertEqual(self.client.delete(f'/api/v1/attachments/{second}').status_code, 400)

    def test_attachment_download_content_and_traversal(self):
        lead, rep = self.assigned()
        ct = self.new_contract(lead, rep)
        uploaded = self.upload(ct, filename='../中文 合同.pdf')['id']
        response = self.client.get(f'/api/v1/attachments/{uploaded}/file?download=1')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, PDF)
        self.assertIn('attachment;',response.headers['Content-Disposition'])
        self.assertEqual(self.contract(ct)['attachments'][0]['name'], '中文 合同.pdf')
        response.close()
        self.assertEqual(self.client.get('/api/v1/attachments/../../app.py/file').status_code,404)

    def test_reject_invalid_file(self):
        lead, rep = self.assigned()
        ct = self.new_contract(lead, rep)
        self.upload(ct,content=b'<script>alert(1)</script>',status=400)
        self.upload(ct,filename='code.html',status=400)
        self.upload(ct,content=b'',status=400)
        self.assertFalse(self.contract(ct)['attachments'])

    def test_backup_contains_consistent_database_and_files(self):
        lead, rep = self.assigned()
        ct = self.new_contract(lead, rep)
        self.upload(ct)
        self.complete(ct)
        payload = build_backup(self.path)
        restore_path = self.path / 'restored'
        with zipfile.ZipFile(payload) as archive:
            names = archive.namelist()
            self.assertIn('database.sqlite3',names)
            self.assertIn('manifest.json',names)
            self.assertEqual(len([x for x in names if x.startswith('uploads/')]),1)
            archive.extractall(restore_path)
        restored = create_app({'TESTING':True,'AUTH_TEST_BYPASS':True,'LOAD_ENV_FILE':False,'DATA_DIR':str(restore_path)}).test_client()
        state = restored.get('/api/v1/state').get_json()
        self.assertEqual(state['contracts'][0]['status'],'signed')
        self.assertEqual(money(state['summary']['signed_amount']),money('60'))
        response = self.client.get('/api/v1/backup')
        self.assertEqual(response.status_code,200)
        self.assertTrue(zipfile.is_zipfile(io.BytesIO(response.data)))
        response.close()

    def test_concurrent_edits_are_rejected(self):
        lead, rep = self.assigned()
        c = self.state()['customers'][0]
        values = {'company':'新公司名','version':c['version'],'estimate':'100'}
        response = self.client.patch(f'/api/v1/customers/{lead}',json=values)
        self.assertEqual(response.status_code,200)
        response = self.client.patch(f'/api/v1/customers/{lead}',json=values)
        self.assertEqual(response.status_code,409)
        self.assertEqual(self.state()['customers'][0]['company'],'新公司名')

    def test_signed_corrections_recalculate_performance(self):
        lead, rep = self.assigned()
        ct = self.new_contract(lead, rep)
        self.upload(ct)
        self.complete(ct)
        response = self.client.patch(f'/api/v1/contracts/{ct}',json={
            'version':self.contract(ct)['version'],'title':'修正後合同','amount':'80.5',
            'rep_id':rep,'signed_at':'2025-12-31'})
        self.assertEqual(response.status_code,200)
        self.assertEqual(money(self.state(2026)['targets']['signed_amount']),0)
        self.assertEqual(money(self.state(2025)['targets']['signed_amount']),money('80.5'))
        self.assertEqual(money(self.state()['summary']['signed_amount']),money('80.5'))

    def test_validation_and_cross_origin_mutation(self):
        self.post('/leads',{'company':''},400)
        for invalid in ['-1','NaN','Infinity','0.0000001',True,{},'1e1000']:
            self.post('/leads',{'company':'測試','estimate':invalid},400)
        response = self.client.post('/api/v1/leads',json={'company':'測試'},headers={'Origin':'https://other.example'})
        self.assertEqual(response.status_code,403)
        self.assertEqual(self.client.get('/api/v1/state?year=not-a-year').status_code,400)

    def test_https_proxy_allows_same_origin_and_rejects_others(self):
        client = create_app({'TESTING': True,'AUTH_TEST_BYPASS':True,'LOAD_ENV_FILE':False,'DATA_DIR':self.tmp.name,'TRUST_PROXY':True}).test_client()
        headers = {'Origin':'https://signing.example.com','X-Forwarded-Host':'signing.example.com','X-Forwarded-Proto':'https'}
        response = client.post('/api/v1/leads',json={'company':'代理測試'},headers=headers)
        self.assertEqual(response.status_code,201)
        headers['Origin']='https://other.example.com'
        self.assertEqual(client.post('/api/v1/leads',json={'company':'拒絕'},headers=headers).status_code,403)

    def test_partial_customer_edit_preserves_contact_details(self):
        lead, rep = self.assigned()
        response=self.client.patch(f'/api/v1/customers/{lead}',json={'company':'修正名稱','version':self.state()['customers'][0]['version']})
        self.assertEqual(response.status_code,200)
        c=self.state()['customers'][0]
        self.assertEqual(c['phone'],'0912345678')
        self.assertEqual(c['region'],'華東')
        self.assertEqual(money(c['estimate']),money('100'))

    def test_unique_contract_number_after_deletion(self):
        lead, rep = self.assigned()
        first = self.new_contract(lead,rep)
        first_number = self.contract(first)['number']
        response = self.client.delete(f'/api/v1/contracts/{first}',json={'version':self.contract(first)['version']})
        self.assertEqual(response.status_code,200)
        second = self.new_contract(lead,rep)
        self.assertNotEqual(first_number,self.contract(second)['number'])

    def test_static_page_and_no_external_scripts(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code,200)
        html = response.get_data(as_text=True)
        self.assertIn('lang="zh-Hant"',html)
        self.assertNotIn('unpkg.com',html)
        response.close()
        self.assertEqual(self.client.get('/api/v1/health').get_json(),{'ok':True})


    def test_loss_archives_contracts_preserves_files_and_can_reassign(self):
        lead, rep = self.assigned()
        ct = self.new_contract(lead, rep)
        attachment = self.upload(ct, kind='draft')
        before = self.state()
        c = next(c for c in before['customers'] if c['id'] == lead)
        self.post(f'/customers/{lead}/lose', {'version':c['version'], 'reason':'客戶暫停專案'})
        state = self.state()
        c = state['customers'][0]
        self.assertEqual((c['stage'],c['rep_id'],c['assigned_at'],c['next_followup']), (0,None,None,''))
        self.assertIn('客戶流失', c['stage_name'])
        self.assertFalse(c['can_delete'])
        self.assertEqual(state['contracts'][0]['status'],'lost')
        self.assertEqual(money(state['summary']['pending_amount']),0)
        self.assertEqual(state['summary']['open_leads'],0)
        self.assertEqual(money(state['summary']['open_leads_amount']),0)
        self.new_lead(company='一般待分配線索', estimate='15')
        summary = self.state()['summary']
        self.assertEqual(summary['open_leads'],1)
        self.assertEqual(money(summary['open_leads_amount']),money('15'))
        self.assertTrue(all(money(r['pending']) == 0 for r in state['performance']))
        url = state['contracts'][0]['attachments'][0]['url']
        self.assertEqual(self.client.get(url).data, PDF)
        follows = self.client.get(f'/api/v1/customers/{lead}/followups').get_json()
        self.assertTrue(any(f['kind']=='客戶流失' and '客戶暫停專案' in f['note'] for f in follows))
        self.assertEqual(self.client.delete(f'/api/v1/leads/{lead}',json={'version':c['version']}).status_code,409)
        new_rep = self.new_rep('接手業務')
        self.post(f'/leads/{lead}/assign', {'rep_id':new_rep})
        self.assertEqual(next(c for c in self.state()['customers'] if c['id']==lead)['stage'],1)
        self.assertEqual(money(self.state()['summary']['pending_amount']),money('100'))
        new_ct = self.new_contract(lead,new_rep,amount='25')
        self.assertEqual(next(c for c in self.state()['customers'] if c['id']==lead)['stage'],4)
        self.assertEqual(money(self.state()['summary']['pending_amount']),money('25'))
        self.upload(new_ct)
        self.complete(new_ct)
        state=self.state()
        self.assertEqual(next(c for c in state['customers'] if c['id']==lead)['stage'],5)
        self.assertEqual(money(state['summary']['signed_amount']),money('25'))
        self.assertEqual(money(state['summary']['pending_amount']),0)
        self.assertEqual(self.contract(ct)['status'],'lost')

    def test_lost_contract_is_read_only_including_attachment_mutations(self):
        lead, rep = self.assigned()
        ct=self.new_contract(lead,rep)
        self.upload(ct)
        c=self.state()['customers'][0]
        self.post(f'/customers/{lead}/lose',{'version':c['version'],'reason':'停止合作'})
        row=self.contract(ct)
        self.post(f'/contracts/{ct}/complete',{'version':row['version'],'signed_at':'2026-10-29'},409)
        self.assertEqual(self.client.patch(f'/api/v1/contracts/{ct}',json={'version':row['version'],'title':'不能改','amount':'20'}).status_code,409)
        self.assertEqual(self.client.delete(f'/api/v1/contracts/{ct}',json={'version':row['version']}).status_code,409)
        self.upload(ct,status=409)
        self.assertEqual(self.client.delete('/api/v1/attachments/'+row['attachments'][0]['id']).status_code,409)
        self.assertEqual(self.client.get(row['attachments'][0]['url']).data,PDF)

    def test_loss_blocks_signed_customer_and_rejects_stale_or_empty_reason(self):
        lead,rep=self.assigned()
        c=self.state()['customers'][0]
        for reason in ['', '   ', 'a'*2001]:
            self.post(f'/customers/{lead}/lose',{'version':c['version'],'reason':reason},400)
        self.post(f'/customers/{lead}/lose',{'version':c['version']-1,'reason':'重複操作'},409)
        ct=self.new_contract(lead,rep)
        self.upload(ct)
        self.complete(ct)
        self.new_contract(lead,rep,title='另一份未簽合同')
        before=self.state()
        self.post(f'/customers/{lead}/lose',{'version':before['customers'][0]['version'],'reason':'客戶流失'},409)
        self.assertEqual(self.state(),before)

    def test_loss_without_contract_and_repeated_returns_preserve_history(self):
        lead,rep=self.assigned()
        for i in range(2):
            c=self.state()['customers'][0]
            self.post(f'/customers/{lead}/lose',{'version':c['version'],'reason':f'第{i+1}次流失'})
            c=self.state()['customers'][0]
            self.post(f'/customers/{lead}/lose',{'version':c['version'],'reason':'重複點擊'},409)
            self.post(f'/leads/{lead}/assign',{'rep_id':rep})
        follows=self.client.get(f'/api/v1/customers/{lead}/followups').get_json()
        self.assertEqual(sum(f['kind']=='客戶流失' for f in follows),2)
        self.assertEqual(sum(f['kind']=='線索分配' for f in follows),3)
        self.app=create_app({'TESTING':True,'AUTH_TEST_BYPASS':True,'LOAD_ENV_FILE':False,'DATA_DIR':self.tmp.name})
        self.client=self.app.test_client()
        self.assertEqual(self.state()['customers'][0]['stage'],1)


if __name__ == '__main__':
    unittest.main()
