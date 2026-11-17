"""以明確清單打包部署程式；排除正式設定、資料庫、附件及登入憑證。"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import tarfile
from datetime import datetime, timezone


ROOT = Path(__file__).resolve().parents[1]
FILES = (
    "app.py", "auth.py", "access.py", "settings.py", "backup.py", "wsgi.py", "requirements.txt",
    "Dockerfile", "compose.yaml", ".dockerignore", ".env.example", "README.md", "FEISHU_LOGIN.md",
    "deploy/nginx.conf.example", "deploy/nginx.money.locations.example", "deploy/signing-money.service.example", "deploy/DEPLOYMENT.md",
)
ASSET_TYPES = {".html", ".css", ".js", ".svg", ".png", ".jpg", ".jpeg", ".ico", ".woff", ".woff2"}


def build_release(output):
    destination = Path(output).resolve()
    if destination.exists():
        raise FileExistsError("部署包已存在，請換一個檔名")
    paths = [ROOT / name for name in FILES]
    for folder in ("static", "templates"):
        paths.extend(p for p in (ROOT / folder).rglob("*") if p.is_file() and p.suffix.lower() in ASSET_TYPES)
    manifest = {"created_at": datetime.now(timezone.utc).isoformat(), "files": []}
    payloads = []
    for path in sorted(paths):
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(ROOT):
            raise ValueError("部署檔案不存在或不在專案內：" + str(path))
        relative = path.relative_to(ROOT).as_posix()
        if relative.startswith(("data/", ".codex-review/", ".test-data/", "backups/")) or relative == ".env":
            raise ValueError("部署清單包含禁止打包的檔案")
        content = path.read_bytes()
        manifest["files"].append({"path": relative, "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()})
        payloads.append((relative, content))
    # 只檢查本機設定中已填寫的密鑰，不把值寫入清單、訊息或部署包。
    import sys
    sys.path.insert(0, str(ROOT))
    from settings import feishu_settings
    secret = feishu_settings(ROOT).get("FEISHU_APP_SECRET", "")
    if secret and any(secret.encode("utf-8") in content for _, content in payloads):
        raise ValueError("程式檔案中含有實際應用密鑰，已停止打包")
    payloads.append(("release-manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")))
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("xb") as stream:
        with tarfile.open(fileobj=stream, mode="w:gz") as archive:
            for relative, content in payloads:
                info = tarfile.TarInfo(relative)
                info.size = len(content)
                info.mode = 0o644
                archive.addfile(info, io.BytesIO(content))
    return {"path": str(destination), "file_count": len(payloads),
            "sha256": hashlib.sha256(destination.read_bytes()).hexdigest()}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="打包簽單台部署程式，不含實際密鑰和業務資料")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        result = build_release(args.output)
    except (OSError, ValueError) as error:
        parser.exit(1, str(error) + "\n")
    print(json.dumps(result, ensure_ascii=True))
