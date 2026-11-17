"""僅供本機瀏覽器自動測試：替代飛書回應，不提供任何略過登入的路由。"""
import argparse
from contextlib import closing
import sqlite3
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import create_app
from auth import LoginError, TOKEN_URL, USER_INFO_URL
from fixtures import seed_sales


def fake_feishu(url, *, form=None, bearer=None, as_json=False):
    if url == TOKEN_URL and form and form.get("code") == "browser_mock_code":
        return {"code": 0, "access_token": "browser-test-token", "expires_in": 7200}
    if url == USER_INFO_URL and bearer == "browser-test-token":
        return {"code": 0, "data": {"name": "瀏覽器測試人員", "tenant_key": "test_company", "open_id": "ou_test_person"}}
    raise LoginError("invalid_code")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--port", type=int, default=8015)
    args = parser.parse_args()
    target = Path(args.data_dir).resolve()
    if not target.is_relative_to(ROOT / ".test-data"):
        parser.error("測試資料只能放在本專案 .test-data 目錄")
    app = create_app({"TESTING": True, "LOAD_ENV_FILE": False, "DATA_DIR": str(target),
                      "INITIAL_ADMINS": {"瀏覽器測試人員": ("瀏覽器測試人員",)},
                      "FEISHU_APP_ID": "cli_browserTest", "FEISHU_APP_SECRET": "fake-test-secret",
                      "FEISHU_TENANT_KEY": "test_company", "FEISHU_SESSION_HOURS": "8",
                      "FEISHU_REDIRECT_URI": f"http://127.0.0.1:{args.port}/auth/feishu/callback"})
    with closing(sqlite3.connect(target/'database.sqlite3')) as conn:
        has_rep=conn.execute("SELECT 1 FROM reps WHERE auth_user_id IS NOT NULL AND name='測試業務員'").fetchone()
    if not has_rep:
        seed_sales(app,'測試業務員')
    with patch("auth.provider_json", fake_feishu):
        app.run(host="127.0.0.1", port=args.port, debug=False)
