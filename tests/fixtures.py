"""只在臨時測試資料庫建立已審核的飛書帳號，不提供正式系統繞過入口。"""
import sqlite3
from contextlib import closing
import time
import uuid
from pathlib import Path


def seed_sales(app, name):
    with closing(sqlite3.connect(Path(app.config['DATA_DIR']) / 'database.sqlite3')) as conn, conn:
        stamp = int(time.time())
        user_id = conn.execute("""INSERT INTO auth_users(app_id,tenant_key,open_id,name,role,created_at,last_login_at)
            VALUES(?,?,?,?,'sales',?,?)""", (app.config['FEISHU_APP_ID'], app.config['FEISHU_TENANT_KEY'],
                                            'ou_fixture_' + uuid.uuid4().hex, name, stamp, stamp)).lastrowid
        return conn.execute("INSERT INTO reps(name,created_at,auth_user_id) VALUES(?,?,?)", (name, '2026-10-29', user_id)).lastrowid
