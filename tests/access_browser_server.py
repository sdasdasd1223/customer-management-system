"""隔離的本機身分驗收站；只使用 .test-data，模擬飛書而不碰正式帳號或資料。"""
import argparse
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
from unittest.mock import patch
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app import create_app
from auth import TOKEN_URL, USER_INFO_URL, LoginError
from flask import request, render_template_string

PEOPLE = {'mock_admin':('測試管理員','ou_test_admin'), 'mock_sales_a':('測試業務甲','ou_test_sales_a'),
          'mock_sales_b':('測試業務乙','ou_test_sales_b')}


def provider(url, *, form=None, bearer=None, as_json=False):
    if url == TOKEN_URL and form and form.get('code') in PEOPLE:
        return {'code':0,'access_token':form['code'],'expires_in':7200}
    if url == USER_INFO_URL and bearer in PEOPLE:
        name, account = PEOPLE[bearer]
        return {'code':0,'data':{'name':name,'open_id':account,'tenant_key':'test_company'}}
    raise LoginError('invalid_code')


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--data-dir',required=True)
    parser.add_argument('--port',type=int,default=8016)
    args=parser.parse_args()
    target=Path(args.data_dir).resolve()
    if not target.is_relative_to(ROOT/'.test-data'):
        parser.error('測試資料只能放在 .test-data 內')
    app=create_app(dict(TESTING=True,LOAD_ENV_FILE=False,DATA_DIR=str(target),
                        INITIAL_ADMINS={'測試管理員':('測試管理員',)},
                        FEISHU_APP_ID='cli_browserAccess',FEISHU_APP_SECRET='test-only-secret',
                        FEISHU_TENANT_KEY='test_company',FEISHU_REDIRECT_URI=f'http://localhost:{args.port}/auth/feishu/callback'))
    with closing(sqlite3.connect(target/'database.sqlite3')) as db,db:
        if db.execute('SELECT COUNT(*) FROM customers').fetchone()[0]==0:
            db.execute("INSERT INTO reps(id,name,created_at) VALUES(100,'測試舊業務員','2026-10-28')")
            db.execute("INSERT INTO customers(company,rep_id,stage,created_at,estimate_cents) VALUES('測試既有客戶',100,1,'2026-10-28',50000000)")
            db.execute("INSERT INTO customers(company,created_at) VALUES('測試歷史線索（他人登記）','2026-10-28')")

    # 僅此隔離驗收站：提供假的飛書授權頁，仍走正常 state、Cookie 與 callback 驗證。
    @app.before_request
    def mock_authorize_page():
        if request.path!='/test/authorize': return
        state=request.args.get('state','')
        links=[(name,'/auth/feishu/callback?'+urlencode({'state':state,'code':code})) for code,(name,_) in PEOPLE.items()]
        return render_template_string('''<!doctype html><html lang="zh-Hant"><meta charset="utf-8"><title>本機模擬飛書</title>
          <h1>本機身分驗收 · 模擬飛書</h1><p>只操作獨立測試資料，不連接真實飛書。</p>
          {% for name,url in links %}<p><a href="{{ url }}">使用{{ name }}登入</a></p>{% endfor %}</html>''',links=links)

    # 授權頁需在正式 guard 前處理；此模擬路由不會出現在正式程式或部署包。
    handlers=app.before_request_funcs[None]
    handlers.insert(0,handlers.pop())
    with patch('auth.provider_json',provider), patch('auth.AUTHORIZE_URL',f'http://localhost:{args.port}/test/authorize'):
        app.run(host='127.0.0.1',port=args.port,debug=False)
