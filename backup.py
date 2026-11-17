"""製作包含資料庫與已引用附件的完整備份；不修改正式資料。"""
from __future__ import annotations

import argparse
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import zipfile
from contextlib import closing
from datetime import datetime, timezone


def build_backup(data_dir):
    data_dir = Path(data_dir).resolve()
    database = data_dir / "database.sqlite3"
    if not database.is_file():
        raise FileNotFoundError("找不到資料庫，請先啟動系統")
    output = io.BytesIO()
    with tempfile.TemporaryDirectory(prefix="signing-backup-") as tmp:
        snapshot = Path(tmp) / "database.sqlite3"
        with closing(sqlite3.connect(database, timeout=30)) as source, closing(sqlite3.connect(snapshot)) as destination:
            source.backup(destination)
            files = destination.execute("SELECT stored_name,size FROM attachments").fetchall()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.write(snapshot, "database.sqlite3")
            for name, expected_size in files:
                path = data_dir / "uploads" / name
                if not path.is_file() or path.stat().st_size != expected_size:
                    raise RuntimeError("附件遺失或損壞，備份未完成：" + name)
                archive.write(path, "uploads/" + name)
            archive.writestr("manifest.json", json.dumps({"version": 1, "created_at": datetime.now(timezone.utc).isoformat(), "attachments": len(files)}, ensure_ascii=False))
    output.seek(0)
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="備份簽單台資料庫與附件")
    parser.add_argument("--data-dir", default=os.environ.get("DATA_DIR", str(Path(__file__).resolve().parent / "data")))
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    path = Path(args.output).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        parser.error("輸出檔案已存在，請換一個檔名")
    path.write_bytes(build_backup(args.data_dir).getvalue())
    print("備份完成：", path)
