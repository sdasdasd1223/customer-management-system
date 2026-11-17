"""讀取本機 .env；系統環境變數優先，不執行其中內容、不展開 $。"""
from pathlib import Path
import os


KEYS = ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_TENANT_KEY", "FEISHU_REDIRECT_URI", "FEISHU_SESSION_HOURS", "APP_BASE_PATH")


def feishu_settings(root, *, load_file=True):
    values = {}
    path = Path(root) / ".env"
    if load_file and path.is_file():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if key not in KEYS:
                continue
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[key] = value
    defaults = {"FEISHU_REDIRECT_URI": "http://127.0.0.1:8000/auth/feishu/callback", "FEISHU_SESSION_HOURS": "8"}
    return {key: os.environ.get(key, values.get(key, defaults.get(key, ""))).strip() for key in KEYS}
