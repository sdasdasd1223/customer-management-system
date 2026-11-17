"""簽單台第一版：Flask + SQLite，資料與附件皆放在 DATA_DIR。"""
from __future__ import annotations

import calendar
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import uuid
import zipfile
from contextlib import closing, contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from flask import Flask, g, jsonify, request, send_file, render_template_string, url_for, redirect
from werkzeug.exceptions import HTTPException, NotFound
from werkzeug.middleware.dispatcher import DispatcherMiddleware
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.serving import WSGIRequestHandler

from auth import FeishuAuth
from access import AccessError
from settings import feishu_settings


ROOT = Path(__file__).resolve().parent
CN_TZ = timezone(timedelta(hours=8))
STAGES = ["線索", "案前接觸", "報價整理", "報價確認", "合同簽約", "簽約完畢"]
FILE_TYPES = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg",
              ".jpeg": "image/jpeg", ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
              ".doc": "application/msword"}
KINDS = {"signed": "已簽合同", "draft": "合同草案", "quote": "報價單", "other": "其他附件"}
MONEY_SCALE = Decimal(1000000)  # 畫面單位為萬元，資料庫以人民幣分存整數。
SCHEMA = """
CREATE TABLE IF NOT EXISTS reps (
 id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS customers (
 id INTEGER PRIMARY KEY AUTOINCREMENT, company TEXT NOT NULL, contact TEXT NOT NULL DEFAULT '',
 phone TEXT NOT NULL DEFAULT '', region TEXT NOT NULL DEFAULT '', industry TEXT NOT NULL DEFAULT '',
 source TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '', estimate_cents INTEGER NOT NULL DEFAULT 0 CHECK(estimate_cents>=0),
 rep_id INTEGER REFERENCES reps(id), stage INTEGER NOT NULL DEFAULT 0 CHECK(stage BETWEEN 0 AND 5),
 created_at TEXT NOT NULL, assigned_at TEXT, next_followup TEXT NOT NULL DEFAULT '',
 version INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS followups (
 id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL REFERENCES customers(id),
 kind TEXT NOT NULL, note TEXT NOT NULL, occurred_on TEXT NOT NULL, created_at TEXT NOT NULL,
 rep_name TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS contracts (
 id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id INTEGER NOT NULL REFERENCES customers(id),
 number TEXT NOT NULL UNIQUE, title TEXT NOT NULL, amount_cents INTEGER NOT NULL CHECK(amount_cents>0),
 rep_id INTEGER NOT NULL REFERENCES reps(id), status TEXT NOT NULL DEFAULT 'signing' CHECK(status IN ('signing','signed')),
 signed_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
 CHECK((status='signing' AND signed_at IS NULL) OR (status='signed' AND signed_at IS NOT NULL))
);
CREATE TABLE IF NOT EXISTS attachments (
 id TEXT PRIMARY KEY, contract_id INTEGER NOT NULL REFERENCES contracts(id) ON DELETE CASCADE,
 name TEXT NOT NULL, stored_name TEXT NOT NULL UNIQUE, mime TEXT NOT NULL, size INTEGER NOT NULL,
 kind TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS targets (
 year INTEGER PRIMARY KEY, amount_cents INTEGER NOT NULL CHECK(amount_cents>=0), updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
 id INTEGER PRIMARY KEY, action TEXT NOT NULL, entity TEXT NOT NULL, entity_id TEXT NOT NULL,
 detail TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_contracts_customer ON contracts(customer_id);
CREATE INDEX IF NOT EXISTS ix_contracts_signed ON contracts(status,signed_at);
CREATE INDEX IF NOT EXISTS ix_followups_customer ON followups(customer_id,occurred_on);
CREATE INDEX IF NOT EXISTS ix_attachments_contract ON attachments(contract_id);
"""


class AuthRedactedRequestHandler(WSGIRequestHandler):
    """本機開發伺服器不將一次性授權碼及 state 寫入存取紀錄。"""
    def log_request(self, code="-", size="-"):
        original = getattr(self, "path", "")
        callback_path = original.partition("?")[0]
        if not callback_path.endswith("/auth/feishu/callback"):
            return super().log_request(code, size)
        try:
            self.path = callback_path + "?[redacted]" if "?" in original else original
            return super().log_request(code, size)
        finally:
            self.path = original


class BusinessError(Exception):
    def __init__(self, message, status=400):
        self.message, self.status = message, status


def now():
    return datetime.now(CN_TZ).isoformat(timespec="seconds")


def today():
    return datetime.now(CN_TZ).date()


def money(value, *, positive=False):
    try:
        if isinstance(value, (bool, dict, list)) or value is None:
            raise ValueError
        amount = Decimal(str(value))
        cents = amount * MONEY_SCALE
        if not amount.is_finite() or cents != cents.to_integral_value() or not 0 <= cents <= 999999999999999:
            raise ValueError
        if positive and cents <= 0:
            raise ValueError
        return int(cents)
    except (InvalidOperation, ValueError, TypeError, OverflowError):
        raise BusinessError("請輸入有效金額（單位：萬元，最多六位小數）" + ("，合同金額必須大於 0" if positive else ""))


def wan(cents):
    return format(Decimal(cents) / MONEY_SCALE, "f")


def text_field(data, key, *, required=False, limit=200):
    value = data.get(key, "")
    if not isinstance(value, str):
        raise BusinessError(f"{key} 必須是文字")
    value = value.strip()
    if required and not value:
        raise BusinessError("必填資料尚未填寫")
    if len(value) > limit:
        raise BusinessError(f"輸入內容過長，最多 {limit} 字")
    return value


def iso_date(value, *, required=True, not_future=False):
    if not value and not required:
        return ""
    try:
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError
        parsed = date.fromisoformat(value)
    except (ValueError, TypeError):
        raise BusinessError("日期格式不正確")
    if not_future and parsed > today():
        raise BusinessError("實際發生日期不能晚於今天")
    return parsed.isoformat()


def create_app(config=None):
    app = Flask(__name__, static_folder="static")
    app.json.ensure_ascii = False
    app.config.update(DATA_DIR=os.environ.get("DATA_DIR", str(ROOT / "data")), MAX_CONTENT_LENGTH=21 * 1024 * 1024)
    app.config.update(feishu_settings(ROOT, load_file=(config or {}).get("LOAD_ENV_FILE", True)))
    if config:
        app.config.update(config)
    base_path = app.config.get("APP_BASE_PATH", "")
    if not isinstance(base_path, str):
        raise ValueError("APP_BASE_PATH 必須是字串")
    base_path = base_path.rstrip("/")
    if not re.fullmatch(r"(?:/[A-Za-z0-9_-]+)*", base_path):
        raise ValueError("APP_BASE_PATH 必須是空字串或 /money 這類網址路徑")
    app.config.update(APP_BASE_PATH=base_path, APPLICATION_ROOT=base_path or "/")
    if base_path:
        # 保留請求的完整子路徑，由掛載層設定 SCRIPT_NAME；不信任外部前綴標頭。
        app.wsgi_app = DispatcherMiddleware(NotFound(), {base_path: app.wsgi_app})
    if app.config.get("TRUST_PROXY", os.environ.get("TRUST_PROXY") == "1"):
        # 僅在已由本機反向代理隔離的部署環境啟用。
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    data_dir = Path(app.config["DATA_DIR"]).resolve()
    uploads = data_dir / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    database = data_dir / "database.sqlite3"
    with closing(sqlite3.connect(database, timeout=15)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(SCHEMA)
        # 加欄即可升級舊資料庫，保留原編號、外鍵及所有附件。
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            for table in ("customers", "contracts"):
                columns = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
                for name, definition in {"lost_at": "TEXT", "loss_reason": "TEXT NOT NULL DEFAULT ''"}.items():
                    if name not in columns:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    def db():
        if "db" not in g:
            g.db = sqlite3.connect(database, timeout=15, isolation_level=None)
            g.db.row_factory = sqlite3.Row
            g.db.execute("PRAGMA foreign_keys=ON")
        return g.db

    @app.teardown_appcontext
    def close_db(_error):
        conn = g.pop("db", None)
        if conn is not None:
            conn.close()

    @contextmanager
    def transaction():
        conn = db()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def body():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise BusinessError("請提交有效的資料")
        return data

    def record(action, entity, entity_id, detail=None):
        detail = dict(detail or {})
        operator = g.get("auth_user")
        if operator:
            detail["operator"] = {"id": operator["id"], "name": operator["name"]}
        db().execute("INSERT INTO audit_log(action,entity,entity_id,detail,created_at) VALUES(?,?,?,?,?)",
                     (action, entity, str(entity_id), json.dumps(detail, ensure_ascii=False), now()))

    def is_admin():
        return g.auth_user["role"] == "admin"

    def own_rep():
        if "own_rep_id" not in g:
            row=db().execute("SELECT id FROM reps WHERE auth_user_id=?", (g.auth_user["id"],)).fetchone()
            g.own_rep_id=row["id"] if row else None
        return g.own_rep_id

    def customer_scope(alias="c"):
        if is_admin():
            return "1", ()
        return f"(({alias}.stage=0 AND {alias}.lost_at IS NULL AND {alias}.registered_by=?) OR ({alias}.stage>0 AND {alias}.rep_id=?))", (g.auth_user["id"],own_rep())

    def get_row(table, entity_id):
        # table 僅由以下程式中的固定字串傳入，不接受外部表名。
        row = db().execute(f"SELECT * FROM {table} WHERE id=?", (entity_id,)).fetchone()
        if row is None:
            raise BusinessError("找不到這筆資料", 404)
        if not is_admin():
            if table == "customers":
                visible = row["lost_at"] is None and row["registered_by"] == g.auth_user["id"] if row["stage"]==0 else own_rep() is not None and row["rep_id"]==own_rep()
                if not visible:
                    raise BusinessError("找不到這筆資料",404)
            elif table == "contracts":
                get_row("customers",row["customer_id"])
            elif table == "attachments":
                get_row("contracts",row["contract_id"])
        return row

    def check_version(row, data):
        if data.get("version") != row["version"]:
            raise BusinessError("這筆資料已被其他人更新，請重新整理後再操作", 409)

    def require_live_contract(row):
        if row["lost_at"]:
            raise BusinessError("已流失合同僅供查閱，請在客戶重新分配後建立新合同", 409)

    def rep_id(value, current_id=None):
        if isinstance(value, bool) or not isinstance(value, int):
            raise BusinessError("請選擇負責業務員")
        get_row("reps", value)
        if not is_admin() and value not in {own_rep(),current_id}:
            raise AccessError("業務員只能以自己的身分負責案件；轉交他人請由管理員分配")
        if value != current_id:
            eligible = db().execute("""SELECT 1 FROM reps r JOIN auth_users u ON u.id=r.auth_user_id
                WHERE r.id=? AND (u.role='sales' OR (u.role='admin' AND u.is_sales=1)) AND u.active=1 AND u.app_id=? AND u.tenant_key=?""",
                                     (value, app.config["FEISHU_APP_ID"], app.config["FEISHU_TENANT_KEY"])).fetchone()
            if not eligible:
                raise BusinessError("請選擇已綁定飛書帳號且通過審核的業務員")
        return value

    def followup(customer_id, kind, note, occurred_on=None, rep_name=None):
        if rep_name is None:
            row = db().execute("SELECT r.name FROM customers c LEFT JOIN reps r ON r.id=c.rep_id WHERE c.id=?", (customer_id,)).fetchone()
            rep_name = row["name"] or ""
        db().execute("INSERT INTO followups(customer_id,kind,note,occurred_on,created_at,rep_name) VALUES(?,?,?,?,?,?)",
                     (customer_id, kind, note, occurred_on or today().isoformat(), now(), rep_name))

    def sync_stage(customer_id):
        counts = db().execute("SELECT COUNT(*) AS n,SUM(status='signing') AS signing FROM contracts WHERE customer_id=? AND lost_at IS NULL", (customer_id,)).fetchone()
        if counts["n"]:
            stage = 4 if counts["signing"] else 5
            db().execute("UPDATE customers SET stage=?,version=version+1 WHERE id=?", (stage, customer_id))

    def attachment_info(row):
        return {"id": row["id"], "contract_id": row["contract_id"], "name": row["name"], "mime": row["mime"],
                "size": row["size"], "kind": row["kind"], "kind_label": KINDS[row["kind"]],
                "created_at": row["created_at"], "url": url_for("get_attachment", attachment_id=row["id"])}

    def all_contracts():
        scope,params=customer_scope()
        attachments = {}
        for row in db().execute(f"""SELECT a.* FROM attachments a JOIN contracts ct ON ct.id=a.contract_id
            JOIN customers c ON c.id=ct.customer_id WHERE {scope} ORDER BY a.created_at DESC,a.id DESC""",params):
            attachments.setdefault(row["contract_id"], []).append(attachment_info(row))
        rows = db().execute(f"""SELECT ct.*,c.company,r.name AS rep_name FROM contracts ct
            JOIN customers c ON c.id=ct.customer_id JOIN reps r ON r.id=ct.rep_id WHERE {scope} ORDER BY ct.id DESC""",params).fetchall()
        return [{"id": row["id"], "customer_id": row["customer_id"], "company": row["company"],
                 "number": row["number"], "title": row["title"], "amount": wan(row["amount_cents"]),
                 "rep_id": row["rep_id"], "rep_name": row["rep_name"], "status": "lost" if row["lost_at"] else row["status"],
                 "lost_at": row["lost_at"], "loss_reason": row["loss_reason"],
                 "signed_at": row["signed_at"], "created_at": row["created_at"], "version": row["version"],
                 "attachments": attachments.get(row["id"], [])} for row in rows]

    def all_customers():
        scope,params=customer_scope()
        rows = db().execute(f"""SELECT c.*,r.name AS rep_name,u.name AS registered_by_name FROM customers c
            LEFT JOIN reps r ON r.id=c.rep_id LEFT JOIN auth_users u ON u.id=c.registered_by WHERE {scope} ORDER BY c.id DESC""",params).fetchall()
        counts = {row["customer_id"]: row["n"] for row in db().execute("SELECT customer_id,COUNT(*) AS n FROM followups WHERE kind='面聊' GROUP BY customer_id")}
        return [{"id": row["id"], "company": row["company"], "contact": row["contact"], "phone": row["phone"],
                 "region": row["region"], "industry": row["industry"], "source": row["source"], "description": row["description"],
                 "estimate": wan(row["estimate_cents"]), "rep_id": row["rep_id"], "rep_name": row["rep_name"] or "未分配",
                 "stage": row["stage"], "stage_name": "客戶流失" if row["stage"]==0 and row["lost_at"] else STAGES[row["stage"]], "created_at": row["created_at"],
                 "lost_at": row["lost_at"], "loss_reason": row["loss_reason"],
                 "assigned_at": row["assigned_at"], "next_followup": row["next_followup"],
                 "meetings": counts.get(row["id"], 0), "version": row["version"],
                 "registered_by": row["registered_by"], "registered_by_name": row["registered_by_name"] or "歷史線索",
                 "can_delete": row["stage"] == 0 and not row["lost_at"] and (g.auth_user["role"] == "admin" or row["registered_by"] == g.auth_user["id"])} for row in rows]

    def target_stats(year, current_date=None):
        current_date = current_date or today()
        target = db().execute("SELECT amount_cents FROM targets WHERE year=?", (year,)).fetchone() if is_admin() else None
        target_cents = target[0] if target else 0
        scope,params=customer_scope()
        rows = db().execute(f"""SELECT ct.signed_at,ct.amount_cents FROM contracts ct JOIN customers c ON c.id=ct.customer_id
            WHERE ct.status='signed' AND ct.signed_at>=? AND ct.signed_at<? AND {scope}""",
                            (f"{year:04d}-01-01", f"{year+1:04d}-01-01",*params)).fetchall()
        amounts = [0] * 12
        for row in rows:
            amounts[int(row["signed_at"][5:7]) - 1] += row["amount_cents"]
        progress = 1.0 if year < current_date.year else 0.0
        if year == current_date.year:
            progress = (current_date.month - 1 + current_date.day / calendar.monthrange(year, current_date.month)[1]) / 12
        total = sum(amounts)
        plan = Decimal(target_cents) / MONEY_SCALE / 12
        expected = Decimal(target_cents) / MONEY_SCALE * Decimal(str(progress))
        return {"year": year, "annual_target": wan(target_cents), "signed_amount": wan(total),
                "monthly_plan": format(plan, "f"), "expected_amount": format(expected, "f"),
                "gap": format(Decimal(total) / MONEY_SCALE - expected, "f"), "time_progress": progress,
                "done_progress": total / target_cents if target_cents else None,
                "months": [{"month": index + 1, "actual": wan(amount), "plan": format(plan, "f"),
                            "difference": format(Decimal(amount) / MONEY_SCALE - plan, "f")} for index, amount in enumerate(amounts)]}

    def selected_year():
        try:
            year = int(request.args.get("year", today().year))
        except (ValueError, TypeError):
            raise BusinessError("年度格式不正確")
        if not 2000 <= year <= 9998:
            raise BusinessError("年度必須介於 2000 至 9998")
        return year

    def snapshot():
        # 所有查詢共用讀取交易，避免多人操作時同一畫面前後數字不一致。
        conn = db()
        conn.execute("BEGIN")
        try:
            customers, contracts = all_customers(), all_contracts()
            year = selected_year()
            totals = {c["id"]: {"count": 0, "pending": 0, "signed": 0} for c in customers}
            for row in conn.execute("SELECT customer_id,status,amount_cents FROM contracts WHERE lost_at IS NULL"):
                if row["customer_id"] not in totals:
                    continue
                item = totals[row["customer_id"]]
                item["count"] += 1
                item["signed" if row["status"] == "signed" else "pending"] += row["amount_cents"]
            pending_total = signed_total = 0
            for c in customers:
                item = totals[c["id"]]
                pending = item["pending"] if item["count"] else (money(c["estimate"]) if c["stage"] else 0)
                c.update(pending_amount=wan(pending), signed_amount=wan(item["signed"]), contract_count=item["count"])
                pending_total += pending
                signed_total += item["signed"]
            reps = [dict(row) for row in conn.execute("""SELECT r.id,r.name,r.auth_user_id,
                CASE WHEN (u.role='sales' OR (u.role='admin' AND u.is_sales=1)) AND u.active=1 AND u.app_id=? AND u.tenant_key=? THEN 1 ELSE 0 END AS selectable
                FROM reps r LEFT JOIN auth_users u ON u.id=r.auth_user_id ORDER BY r.id""",
                (app.config["FEISHU_APP_ID"], app.config["FEISHU_TENANT_KEY"]))]
            if not is_admin():
                reps=[r for r in reps if r["auth_user_id"]==g.auth_user["id"]]
            performance = []
            for rep in reps:
                # 預估額歸客戶負責人；正式合同歸各自合同負責人。
                p = sum(money(c["estimate"]) for c in customers if c["rep_id"] == rep["id"] and not c["contract_count"])
                signed_year = 0
                signed_all = 0
                for ct in contracts:
                    if ct["rep_id"] != rep["id"]:
                        continue
                    if ct["status"] == "signing":
                        p += money(ct["amount"])
                    elif ct["status"] == "signed":
                        signed_all += money(ct["amount"])
                        if ct["signed_at"].startswith(str(year) + "-"):
                            signed_year += money(ct["amount"])
                performance.append({**rep, "pending": wan(p), "signed": wan(signed_all), "signed_year": wan(signed_year)})
            scope,params=customer_scope()
            recent = [dict(row) for row in conn.execute(f"""SELECT f.*,c.company FROM followups f JOIN customers c ON c.id=f.customer_id
                WHERE {scope} ORDER BY f.occurred_on DESC,f.created_at DESC,f.id DESC LIMIT 8""",params)]
            open_leads = [c for c in customers if c["stage"] == 0 and not c["lost_at"]]
            return {"today": today().isoformat(), "stages": STAGES, "customers": customers, "contracts": contracts,
                    "reps": reps, "targets": target_stats(year), "performance": performance, "recent": recent,
                    "summary": {"pending_amount": wan(pending_total), "signed_amount": wan(signed_total),
                                "open_leads": len(open_leads),
                                "open_leads_amount": wan(sum(money(c["estimate"]) for c in open_leads)),
                                "customers": sum(c["stage"] > 0 for c in customers)}}
        finally:
            conn.rollback()

    auth = FeishuAuth(app, db, transaction, record)

    @app.before_request
    def same_origin_mutations():
        if request.path == "/static/index.html":
            return redirect(url_for("index"))
        if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
            return
        origin = request.headers.get("Origin")
        if origin and origin.rstrip("/") != request.host_url.rstrip("/"):
            raise BusinessError("請從系統頁面內提交資料", 403)

    @app.after_request
    def response_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        if request.path.startswith(("/api/", "/auth/")) or request.path in {"/", "/login", "/waiting", "/static/index.html"}:
            response.headers["Cache-Control"] = "no-store"
        if request.path.startswith("/auth/") or request.path == "/login":
            response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.errorhandler(BusinessError)
    def business_error(error):
        return jsonify(error=error.message), error.status

    @app.errorhandler(AccessError)
    def access_error(error):
        return jsonify(error=error.message), error.status

    @app.errorhandler(HTTPException)
    def http_error(error):
        messages = {400: "提交資料格式不正確", 404: "找不到這個頁面或檔案", 405: "不支援這個操作", 413: "檔案太大，單一附件上限為 20 MB"}
        return jsonify(error=messages.get(error.code, "請求無法完成")), error.code

    @app.errorhandler(sqlite3.OperationalError)
    def database_error(error):
        app.logger.exception("資料庫操作失敗")
        return jsonify(error="資料暫時無法儲存，請稍後重試"), 503

    @app.errorhandler(Exception)
    def unexpected_error(error):
        app.logger.exception("未預期錯誤")
        return jsonify(error="操作未完成，請稍後重試；若持續發生請查看伺服器紀錄"), 500

    @app.get("/")
    def index():
        return render_template_string((ROOT / "static" / "index.html").read_text(encoding="utf-8"))

    @app.get("/api/v1/health")
    def health():
        db().execute("SELECT 1").fetchone()
        return jsonify(ok=True)

    @app.get("/api/v1/state")
    def state():
        return jsonify(snapshot())

    @app.post("/api/v1/reps")
    def add_rep():
        auth.access.require_admin()
        raise BusinessError("請到人員與身分，將已登入的飛書帳號分配為業務員；不再手動新增姓名", 410)

    def customer_fields(data):
        return {"company": text_field(data, "company", required=True), "contact": text_field(data, "contact"),
                "phone": text_field(data, "phone", limit=80), "region": text_field(data, "region", limit=100),
                "industry": text_field(data, "industry", limit=100), "source": text_field(data, "source", limit=100),
                "description": text_field(data, "description", limit=10000), "estimate_cents": money(data.get("estimate", "0")),
                "next_followup": iso_date(data.get("next_followup", ""), required=False)}

    @app.post("/api/v1/leads")
    def add_lead():
        values = customer_fields(body())
        values["created_at"] = now()
        values["registered_by"] = g.auth_user["id"] if g.auth_user["id"] else None
        with transaction():
            columns = list(values)
            entity_id = db().execute(f"INSERT INTO customers({','.join(columns)}) VALUES({','.join('?' for _ in columns)})", list(values.values())).lastrowid
            record("create", "lead", entity_id)
        return jsonify(id=entity_id), 201

    @app.patch("/api/v1/customers/<int:entity_id>")
    def edit_customer(entity_id):
        data = body()
        with transaction():
            row = get_row("customers", entity_id)
            check_version(row, data)
            existing = {key: row[key] for key in ("company", "contact", "phone", "region", "industry", "source", "description", "next_followup")}
            existing["estimate"] = wan(row["estimate_cents"])
            values = customer_fields({**existing, **data})
            if row["stage"] > 0 and "rep_id" in data:
                values["rep_id"] = rep_id(data["rep_id"], row["rep_id"])
            db().execute(f"UPDATE customers SET {','.join(k+'=?' for k in values)},version=version+1 WHERE id=?", [*values.values(), entity_id])
            record("update", "customer", entity_id)
        return jsonify(ok=True)

    @app.post("/api/v1/leads/<int:entity_id>/assign")
    def assign_lead(entity_id):
        data = body()
        with transaction():
            row = get_row("customers", entity_id)
            owner = rep_id(data.get("rep_id"))
            result = db().execute("UPDATE customers SET rep_id=?,stage=1,assigned_at=?,version=version+1 WHERE id=? AND stage=0 AND rep_id IS NULL", (owner, now(), entity_id))
            if result.rowcount != 1:
                raise BusinessError("這條線索已經分配，請重新整理查看", 409)
            followup(entity_id, "線索分配", "已分配負責業務員，進入案前接觸。")
            record("assign", "lead", row["id"], {"rep_id": owner})
        return jsonify(ok=True)

    @app.post("/api/v1/customers/<int:entity_id>/lose")
    def lose_customer(entity_id):
        data = body()
        reason = text_field(data, "reason", required=True, limit=2000)
        with transaction():
            row = get_row("customers", entity_id)
            check_version(row, data)
            if row["stage"] == 0:
                raise BusinessError("這筆資料已在流失池或線索池", 409)
            if db().execute("SELECT 1 FROM contracts WHERE customer_id=? AND status='signed' LIMIT 1", (entity_id,)).fetchone():
                raise BusinessError("已有簽約完畢合同的客戶不能整體移入流失池", 409)
            stamp = now()
            # 先記原負責人，再解除分配；整個操作同一交易，不刪除任何歷史。
            followup(entity_id, "客戶流失", "移入流失池，由管理員管理及重新分配。原因：" + reason)
            db().execute("UPDATE contracts SET lost_at=?,loss_reason=?,updated_at=?,version=version+1 WHERE customer_id=? AND lost_at IS NULL AND status='signing'",
                         (stamp, reason, stamp, entity_id))
            db().execute("UPDATE customers SET stage=0,rep_id=NULL,assigned_at=NULL,next_followup='',lost_at=?,loss_reason=?,version=version+1 WHERE id=?",
                         (stamp, reason, entity_id))
            record("lose", "customer", entity_id, {"reason": reason, "previous_stage": row["stage"], "previous_rep_id": row["rep_id"]})
        return jsonify(ok=True)

    @app.delete("/api/v1/leads/<int:entity_id>")
    def delete_lead(entity_id):
        with transaction():
            row = get_row("customers", entity_id)
            check_version(row, body())
            if row["stage"] != 0:
                raise BusinessError("只能刪除尚未分配的線索")
            if row["lost_at"]:
                raise BusinessError("流失退回的客戶需保留歷史紀錄，不能刪除", 409)
            if g.auth_user["role"] != "admin" and row["registered_by"] != g.auth_user["id"]:
                raise AccessError("業務員只能刪除自己帳號登記的未分配線索")
            if db().execute("SELECT 1 FROM contracts WHERE customer_id=? LIMIT 1", (entity_id,)).fetchone():
                raise BusinessError("已有合同的資料不能刪除", 409)
            db().execute("DELETE FROM customers WHERE id=?", (entity_id,))
            record("delete", "lead", entity_id, {"company": row["company"]})
        return jsonify(ok=True)

    @app.get("/api/v1/customers/<int:entity_id>/followups")
    def get_followups(entity_id):
        get_row("customers", entity_id)
        return jsonify([dict(row) for row in db().execute("SELECT * FROM followups WHERE customer_id=? ORDER BY occurred_on DESC,created_at DESC,id DESC", (entity_id,))])

    @app.post("/api/v1/customers/<int:entity_id>/followups")
    def add_followup(entity_id):
        data = body()
        kind = text_field(data, "kind", required=True, limit=40)
        if kind not in {"面聊", "電話／微信溝通", "報價溝通", "合同溝通", "其他"}:
            raise BusinessError("請選擇有效的跟進類型")
        note = text_field(data, "note", required=True, limit=10000)
        occurred_on = iso_date(data.get("occurred_on"), not_future=True)
        with transaction():
            row = get_row("customers", entity_id)
            if not row["rep_id"]:
                raise BusinessError("請先分配負責業務員")
            followup(entity_id, kind, note, occurred_on)
            record("followup", "customer", entity_id, {"kind": kind})
        return jsonify(ok=True), 201

    @app.post("/api/v1/customers/<int:entity_id>/advance")
    def advance_customer(entity_id):
        data = body()
        with transaction():
            row = get_row("customers", entity_id)
            check_version(row, data)
            if not 1 <= row["stage"] <= 3:
                raise BusinessError("合同階段請透過合同上傳附件並完成簽約")
            stage = row["stage"] + 1
            db().execute("UPDATE customers SET stage=?,version=version+1 WHERE id=?", (stage, entity_id))
            followup(entity_id, "階段推進", "進入「" + STAGES[stage] + "」。")
            record("advance", "customer", entity_id, {"stage": stage})
        return jsonify(ok=True)

    @app.post("/api/v1/contracts")
    def add_contract():
        data = body()
        title = text_field(data, "title", required=True)
        amount = money(data.get("amount"), positive=True)
        customer_id = data.get("customer_id")
        if isinstance(customer_id, bool) or not isinstance(customer_id, int):
            raise BusinessError("請選擇客戶")
        with transaction():
            c = get_row("customers", customer_id)
            if not c["rep_id"]:
                raise BusinessError("請先分配這位客戶的負責業務員")
            owner = rep_id(data.get("rep_id", c["rep_id"]))
            timestamp = now()
            # 先取得永久編號，再產生可讀合同號，避免並行建立時重複。
            row_id = db().execute("INSERT INTO contracts(customer_id,number,title,amount_cents,rep_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                                  (customer_id, "new-" + uuid.uuid4().hex, title, amount, owner, timestamp, timestamp)).lastrowid
            number = f"HT-{today().year}-{row_id:06d}"
            db().execute("UPDATE contracts SET number=? WHERE id=?", (number, row_id))
            sync_stage(customer_id)
            followup(customer_id, "新增合同", f"{number}：{title}，金額 {wan(amount)} 萬元，簽約中。")
            record("create", "contract", row_id)
        return jsonify(id=row_id, number=number), 201

    @app.patch("/api/v1/contracts/<int:entity_id>")
    def edit_contract(entity_id):
        data = body()
        title = text_field(data, "title", required=True)
        amount = money(data.get("amount"), positive=True)
        with transaction():
            row = get_row("contracts", entity_id)
            check_version(row, data)
            require_live_contract(row)
            owner = rep_id(data.get("rep_id", row["rep_id"]), row["rep_id"])
            signed_at = iso_date(data.get("signed_at"), not_future=True) if row["status"] == "signed" else None
            db().execute("UPDATE contracts SET title=?,amount_cents=?,rep_id=?,signed_at=?,updated_at=?,version=version+1 WHERE id=?",
                         (title, amount, owner, signed_at, now(), entity_id))
            record("update", "contract", entity_id, {"old_amount": wan(row["amount_cents"]), "new_amount": wan(amount), "old_signed_at": row["signed_at"], "new_signed_at": signed_at})
        return jsonify(ok=True)

    @app.post("/api/v1/contracts/<int:entity_id>/complete")
    def complete_contract(entity_id):
        data = body()
        signed_at = iso_date(data.get("signed_at"), not_future=True)
        with transaction():
            row = get_row("contracts", entity_id)
            check_version(row, data)
            require_live_contract(row)
            if row["status"] == "signed":
                raise BusinessError("這份合同已完成簽約，無須再次操作", 409)
            originals = db().execute("SELECT * FROM attachments WHERE contract_id=? AND kind='signed'", (entity_id,)).fetchall()
            if not any((uploads / item["stored_name"]).is_file() and (uploads / item["stored_name"]).stat().st_size == item["size"] for item in originals):
                raise BusinessError("請先上傳類型為「已簽合同」的附件，才能完成簽約")
            db().execute("UPDATE contracts SET status='signed',signed_at=?,updated_at=?,version=version+1 WHERE id=?", (signed_at, now(), entity_id))
            sync_stage(row["customer_id"])
            followup(row["customer_id"], "簽約完畢", f"{row['number']} 已完成簽約，計入業績 {wan(row['amount_cents'])} 萬元。", signed_at)
            record("complete", "contract", entity_id, {"signed_at": signed_at})
        return jsonify(ok=True)

    @app.delete("/api/v1/contracts/<int:entity_id>")
    def delete_contract(entity_id):
        data = body()
        with transaction():
            row = get_row("contracts", entity_id)
            check_version(row, data)
            require_live_contract(row)
            if row["status"] == "signed":
                raise BusinessError("已簽合同不能直接刪除；資料有誤請使用修改功能")
            db().execute("DELETE FROM contracts WHERE id=?", (entity_id,))
            sync_stage(row["customer_id"])
            followup(row["customer_id"], "刪除合同", f"已刪除簽約中的合同 {row['number']}。")
            record("delete", "contract", entity_id, {"number": row["number"]})
        return jsonify(ok=True)

    def validate_file(filename, content):
        suffix = Path(filename).suffix.lower()
        if suffix not in FILE_TYPES:
            raise BusinessError("支援 PDF、JPG、PNG、DOC、DOCX 檔案")
        valid = ((suffix == ".pdf" and content.startswith(b"%PDF-")) or
                 (suffix == ".png" and content.startswith(b"\x89PNG\r\n\x1a\n")) or
                 (suffix in {".jpg", ".jpeg"} and content.startswith(b"\xff\xd8\xff")) or
                 (suffix == ".doc" and content.startswith(bytes.fromhex("D0CF11E0A1B11AE1"))))
        if suffix == ".docx":
            try:
                with zipfile.ZipFile(io.BytesIO(content)) as archive:
                    valid = "word/document.xml" in archive.namelist() and "[Content_Types].xml" in archive.namelist()
            except zipfile.BadZipFile:
                valid = False
        if not valid:
            raise BusinessError("檔案內容與格式不符，請選擇有效文件")
        return suffix, FILE_TYPES[suffix]

    @app.post("/api/v1/contracts/<int:entity_id>/attachments")
    def upload_attachment(entity_id):
        file = request.files.get("file")
        kind = request.form.get("kind", "")
        if kind not in KINDS:
            raise BusinessError("請選擇附件類型")
        if file is None or not file.filename:
            raise BusinessError("請選擇要上傳的檔案")
        name = file.filename.replace("\\", "/").rsplit("/", 1)[-1].strip()
        if not name or len(name) > 180 or any(ord(ch) < 32 for ch in name):
            raise BusinessError("檔案名稱無效或過長")
        content = file.read(20 * 1024 * 1024 + 1)
        if not content or len(content) > 20 * 1024 * 1024:
            raise BusinessError("請選擇非空白且不超過 20 MB 的檔案")
        suffix, mime = validate_file(name, content)
        attachment_id = uuid.uuid4().hex
        stored_name = attachment_id + suffix
        path = uploads / stored_name
        try:
            with transaction():
                require_live_contract(get_row("contracts", entity_id))
                path.write_bytes(content)
                db().execute("INSERT INTO attachments(id,contract_id,name,stored_name,mime,size,kind,created_at) VALUES(?,?,?,?,?,?,?,?)",
                             (attachment_id, entity_id, name, stored_name, mime, len(content), kind, now()))
                record("upload", "contract", entity_id, {"attachment_id": attachment_id, "kind": kind, "name": name})
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return jsonify(id=attachment_id), 201

    @app.get("/api/v1/attachments/<attachment_id>/file")
    def get_attachment(attachment_id):
        row = get_row("attachments", attachment_id)
        path = uploads / row["stored_name"]
        if not path.is_file():
            raise BusinessError("附件檔案遺失，請從備份還原或重新上傳", 404)
        preview = row["mime"] in {"application/pdf", "image/png", "image/jpeg"}
        return send_file(path, mimetype=row["mime"], download_name=row["name"], as_attachment=request.args.get("download") == "1" or not preview, conditional=True)

    @app.delete("/api/v1/attachments/<attachment_id>")
    def delete_attachment(attachment_id):
        with transaction():
            row = get_row("attachments", attachment_id)
            contract = get_row("contracts", row["contract_id"])
            require_live_contract(contract)
            if row["kind"] == "signed" and contract["status"] == "signed":
                remaining = db().execute("SELECT stored_name FROM attachments WHERE contract_id=? AND kind='signed' AND id<>?", (contract["id"], attachment_id)).fetchall()
                if not any((uploads / r["stored_name"]).is_file() for r in remaining):
                    raise BusinessError("已簽合同至少需要保留一份已簽附件；請先上傳替代文件")
            db().execute("DELETE FROM attachments WHERE id=?", (attachment_id,))
            record("delete_attachment", "contract", contract["id"], {"attachment_id": attachment_id})
        # 檔案暫留磁碟，讓同時製作中的備份仍可取得其快照所引用的附件。
        return jsonify(ok=True)

    @app.put("/api/v1/targets/<int:year>")
    def save_target(year):
        auth.access.require_admin()
        if not 2000 <= year <= 9998:
            raise BusinessError("年度必須介於 2000 至 9998")
        amount = money(body().get("amount"))
        with transaction():
            db().execute("INSERT INTO targets(year,amount_cents,updated_at) VALUES(?,?,?) ON CONFLICT(year) DO UPDATE SET amount_cents=excluded.amount_cents,updated_at=excluded.updated_at", (year, amount, now()))
            record("set_target", "target", year, {"amount": wan(amount)})
        return jsonify(ok=True)

    @app.get("/api/v1/backup")
    def download_backup():
        auth.access.require_admin()
        from backup import build_backup
        output = build_backup(data_dir)
        return send_file(output, mimetype="application/zip", as_attachment=True, download_name="簽單台備份-" + datetime.now(CN_TZ).strftime("%Y%m%d-%H%M%S") + ".zip")

    @app.get("/api/v1/contracts")
    def contract_list():
        return jsonify(all_contracts())

    @app.get("/api/v1/targets")
    def targets():
        return jsonify(target_stats(selected_year()))

    return app


if __name__ == "__main__":
    create_app().run(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8000")), debug=False,
                     request_handler=AuthRedactedRequestHandler)
