"""飛書帳號的待審核身分、初始管理員與業務員固定綁定。"""
from datetime import datetime, timezone
import time

from flask import g, jsonify, request


INITIAL_ADMINS = {"林育丰": ("林育丰",), "塗是淦": ("塗是淦", "涂是淦")}
ROLE_LABELS = {"pending": "待分配身分", "sales": "業務員", "admin": "管理員"}


class AccessError(Exception):
    def __init__(self, message, status=403):
        self.message, self.status = message, status


class AccessControl:
    def __init__(self, app, db, transaction, record):
        self.app, self.db, self.transaction, self.record = app, db, transaction, record
        self.initial_admins = app.config.get("INITIAL_ADMINS", INITIAL_ADMINS)
        self.migrate()
        app.extensions["access_control"] = self
        app.add_url_rule("/api/v1/admin/users", "admin_users", self.users)
        app.add_url_rule("/api/v1/admin/users/<int:user_id>/role", "admin_user_role", self.set_role, methods=["POST"])

    def migrate(self):
        conn = self.db()
        # SQLite 重建業務員表以容許同名的不同帳號；所有原有 id 與外鍵保留。
        conn.execute("PRAGMA foreign_keys=OFF")
        try:
            with self.transaction():
                columns = {row["name"] for row in conn.execute("PRAGMA table_info(auth_users)")}
                for name, definition in {
                    "role": "TEXT NOT NULL DEFAULT 'pending' CHECK(role IN ('pending','sales','admin'))",
                    "role_version": "INTEGER NOT NULL DEFAULT 1",
                    "approved_at": "INTEGER",
                    "approved_by": "INTEGER REFERENCES auth_users(id)",
                    "is_sales": "INTEGER NOT NULL DEFAULT 0 CHECK(is_sales IN (0,1))",
                }.items():
                    if name not in columns:
                        conn.execute(f"ALTER TABLE auth_users ADD COLUMN {name} {definition}")
                conn.execute("UPDATE auth_users SET is_sales=1 WHERE role='sales' AND is_sales=0")
                rep_columns = {row["name"] for row in conn.execute("PRAGMA table_info(reps)")}
                if "auth_user_id" not in rep_columns:
                    conn.execute("""CREATE TABLE reps_with_accounts (
                        id INTEGER PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL,
                        auth_user_id INTEGER UNIQUE REFERENCES auth_users(id))""")
                    conn.execute("INSERT INTO reps_with_accounts(id,name,created_at) SELECT id,name,created_at FROM reps")
                    conn.execute("DROP TABLE reps")
                    conn.execute("ALTER TABLE reps_with_accounts RENAME TO reps")
                customer_columns = {row["name"] for row in conn.execute("PRAGMA table_info(customers)")}
                if "registered_by" not in customer_columns:
                    conn.execute("ALTER TABLE customers ADD COLUMN registered_by INTEGER REFERENCES auth_users(id)")
                conn.execute("""CREATE TABLE IF NOT EXISTS initial_admin_bindings (
                    name TEXT PRIMARY KEY, user_id INTEGER UNIQUE REFERENCES auth_users(id), claimed_at INTEGER)""")
                initial_columns = {row["name"] for row in conn.execute("PRAGMA table_info(initial_admin_bindings)")}
                if "initial_role" not in initial_columns:
                    conn.execute("ALTER TABLE initial_admin_bindings ADD COLUMN initial_role TEXT NOT NULL DEFAULT 'admin' CHECK(initial_role IN ('admin','sales'))")
                for name in self.initial_admins:
                    conn.execute("INSERT OR IGNORE INTO initial_admin_bindings(name) VALUES(?)", (name,))
                for row in conn.execute("SELECT * FROM auth_users ORDER BY id").fetchall():
                    self.claim_initial_admin(row)
                if conn.execute("PRAGMA foreign_key_check").fetchone():
                    raise RuntimeError("身分資料移轉的外鍵檢查未通過，已停止修改")
                conn.execute("PRAGMA user_version=2")
        finally:
            conn.execute("PRAGMA foreign_keys=ON")

    def claim_initial_admin(self, user):
        config = self.app.config
        if not user["active"] or user["app_id"] != config.get("FEISHU_APP_ID") or user["tenant_key"] != config.get("FEISHU_TENANT_KEY"):
            return
        for slot, aliases in self.initial_admins.items():
            if user["name"] not in aliases:
                continue
            binding = self.db().execute("SELECT user_id,initial_role FROM initial_admin_bindings WHERE name=?", (slot,)).fetchone()
            if not binding or binding["user_id"] is not None:
                continue
            placeholders = ",".join("?" for _ in aliases)
            matches = self.db().execute(f"SELECT id FROM auth_users WHERE app_id=? AND tenant_key=? AND name IN ({placeholders})",
                                        (user["app_id"], user["tenant_key"], *aliases)).fetchall()
            # 同名帳號不自動合併或猜測，由已登入管理員處理。
            if len(matches) != 1:
                continue
            stamp = int(time.time())
            claimed = self.db().execute("UPDATE initial_admin_bindings SET user_id=?,claimed_at=? WHERE name=? AND user_id IS NULL",
                                         (user["id"], stamp, slot))
            if claimed.rowcount:
                role = binding["initial_role"]
                self.db().execute("UPDATE auth_users SET role=?,is_sales=?,approved_at=?,role_version=role_version+1 WHERE id=?", (role, int(role=='sales'), stamp, user["id"]))
                if role == "sales":
                    self.db().execute("INSERT OR IGNORE INTO reps(name,created_at,auth_user_id) VALUES(?,?,?)",
                                      (user["name"], datetime.now(timezone.utc).isoformat(timespec="seconds"), user["id"]))
                self.record("initial_role", "user", user["id"], {"initial_name": slot, "role": role})

    def require_admin(self):
        if not g.get("auth_user") or g.auth_user.get("role") != "admin":
            raise AccessError("只有管理員可以執行這個操作")

    def summary(self, user):
        rep = self.db().execute("SELECT id FROM reps WHERE auth_user_id=?", (user["id"],)).fetchone()
        sales = bool(user.get("is_sales")) or user["role"] == "sales"
        roles = (["admin"] if user["role"] == "admin" else []) + (["sales"] if sales else [])
        return {"id": user["id"], "name": user["name"], "role": user["role"],
                "roles": roles, "is_sales": sales,
                "role_label": "管理員兼業務員" if len(roles)==2 else ROLE_LABELS[user["role"]],
                "role_version": user.get("role_version", 1), "rep_id": rep["id"] if rep else None,
                "approved": user["role"] in {"admin", "sales"}, "active": bool(user.get("active", 1))}

    def users(self):
        self.require_admin()
        rows = self.db().execute("SELECT * FROM auth_users WHERE app_id=? AND tenant_key=? ORDER BY role='pending' DESC,id",
                                  (self.app.config["FEISHU_APP_ID"], self.app.config["FEISHU_TENANT_KEY"])).fetchall()
        legacy = self.db().execute("""SELECT r.id,r.name,
            (SELECT COUNT(*) FROM customers c WHERE c.rep_id=r.id) AS customers,
            (SELECT COUNT(*) FROM contracts c WHERE c.rep_id=r.id) AS contracts
            FROM reps r WHERE r.auth_user_id IS NULL ORDER BY r.id""").fetchall()
        presets = self.db().execute("SELECT name,initial_role AS role,user_id FROM initial_admin_bindings ORDER BY rowid").fetchall()
        return jsonify(users=[self.summary(dict(row)) for row in rows], legacy_reps=[dict(row) for row in legacy],
                       initial_roles=[{**dict(row), "role_label": ROLE_LABELS[row["role"]]} for row in presets])

    def set_role(self, user_id):
        self.require_admin()
        values = request.get_json(silent=True)
        if not isinstance(values, dict):
            raise AccessError("請提交有效的身分設定", 400)
        if "roles" in values:
            roles=values["roles"]
            if ("role" in values or not isinstance(roles,list) or any(not isinstance(r,str) or r not in {"admin","sales"} for r in roles)
                    or len(roles)!=len(set(roles))):
                raise AccessError("請勾選管理員或業務員；全部取消代表待分配", 400)
            role="admin" if "admin" in roles else "sales" if "sales" in roles else "pending"
        else:
            role=values.get("role")
            if not isinstance(role,str) or role not in ROLE_LABELS:
                raise AccessError("請選擇管理員、業務員或待分配身分",400)
            roles=[] if role=="pending" else [role]
        sales="sales" in roles
        legacy_id=values.get("legacy_rep_id")
        if legacy_id is not None and (type(legacy_id) is not int or not sales):
            raise AccessError("請選擇正確的舊業務員資料", 400)
        with self.transaction():
            row = self.db().execute("SELECT * FROM auth_users WHERE id=? AND app_id=? AND tenant_key=?",
                                    (user_id, self.app.config["FEISHU_APP_ID"], self.app.config["FEISHU_TENANT_KEY"])).fetchone()
            if row is None:
                raise AccessError("找不到這個使用者", 404)
            if not row["active"]:
                raise AccessError("這個帳號已停用，無法分配身分", 409)
            if type(values.get("version")) is not int or values["version"] != row["role_version"]:
                raise AccessError("身分已被其他管理員修改，請重新整理", 409)
            if row["role"] == "admin" and role != "admin":
                count = self.db().execute("SELECT COUNT(*) FROM auth_users WHERE role='admin' AND active=1 AND app_id=? AND tenant_key=?",
                                          (row["app_id"], row["tenant_key"])).fetchone()[0]
                if count <= 1:
                    raise AccessError("至少需要保留一位可登入的管理員", 409)
            rep = self.db().execute("SELECT * FROM reps WHERE auth_user_id=?", (user_id,)).fetchone()
            if sales:
                if legacy_id is not None:
                    if rep:
                        raise AccessError("此帳號已綁定業務員資料，不可重複綁定", 409)
                    updated = self.db().execute("UPDATE reps SET auth_user_id=?,name=? WHERE id=? AND auth_user_id IS NULL", (user_id, row["name"], legacy_id))
                    if updated.rowcount != 1:
                        raise AccessError("這份舊業務員資料已綁定或不存在，請重新整理", 409)
                elif not rep:
                    self.db().execute("INSERT INTO reps(name,created_at,auth_user_id) VALUES(?,?,?)",
                                      (row["name"], datetime.now(timezone.utc).isoformat(timespec="seconds"), user_id))
                else:
                    self.db().execute("UPDATE reps SET name=? WHERE auth_user_id=?", (row["name"], user_id))
            self.db().execute("UPDATE auth_users SET role=?,is_sales=?,approved_at=?,approved_by=?,role_version=role_version+1 WHERE id=?",
                              (role, int(sales), int(time.time()) if role != "pending" else None, g.auth_user["id"], user_id))
            self.record("assign_role", "user", user_id, {"from": row["role"], "to": role, "roles": roles, "legacy_rep_id": legacy_id})
        return jsonify(ok=True)
