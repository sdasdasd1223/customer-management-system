"""飛書 OAuth 授權登入；登入狀態由本系統後端保存，飛書令牌不落地。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from flask import g, jsonify, redirect, render_template, request, url_for
from access import AccessControl


AUTHORIZE_URL = "https://accounts.feishu.cn/open-apis/authen/v1/authorize"
# 2026-10-29：取得授權碼文件仍註明 PKCE 搭配 v2；實際 v3 回覆 20049。
# 保留 S256 PKCE，使用此授權入口目前支援的 v2 組合，不自動降級安全檢查。
TOKEN_URL = "https://open.feishu.cn/open-apis/authen/v2/oauth/token"
V3_TOKEN_URL = "https://accounts.feishu.cn/oauth/v3/token"
USER_INFO_URL = "https://open.feishu.cn/open-apis/authen/v1/user_info"
SESSION_COOKIE = "signing_session"
FLOW_COOKIE = "signing_oauth_browser"
SCHEMA = """
CREATE TABLE IF NOT EXISTS auth_users (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 app_id TEXT NOT NULL, tenant_key TEXT NOT NULL, open_id TEXT NOT NULL,
 name TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
 created_at INTEGER NOT NULL, last_login_at INTEGER NOT NULL,
 UNIQUE(app_id,tenant_key,open_id)
);
CREATE TABLE IF NOT EXISTS auth_sessions (
 token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES auth_users(id),
 client_id TEXT NOT NULL, csrf_token TEXT NOT NULL, created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS auth_flows (
 state_hash TEXT PRIMARY KEY, browser_hash TEXT NOT NULL, verifier TEXT NOT NULL,
 next_path TEXT NOT NULL, created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_auth_sessions_expiry ON auth_sessions(expires_at);
CREATE INDEX IF NOT EXISTS ix_auth_flows_expiry ON auth_flows(expires_at);
"""


MESSAGES = {
    "not_configured": "管理員尚未完成飛書登入設定，請先填寫設定檔後重新啟動系統。",
    "wrong_origin": "目前開啟的網址與飛書返回網址不同。請從下方的系統網址開啟後再登入。",
    "invalid_state": "這次登入已逾時、已使用，或不是由這個瀏覽器發起。請重新點選飛書登入。",
    "denied": "你已取消飛書授權，尚未登入系統。可以再次點選登入。",
    "provider_unavailable": "暫時無法連線至飛書，請稍後重新登入。",
    "invalid_code": "飛書授權碼無效、已使用或已逾時，請重新登入。",
    "invalid_pkce": "飛書的授權安全校驗未通過（20049），請重新登入；若持續發生，請聯絡系統維護人員。",
    "redirect_mismatch": "飛書返回網址不一致（20071），請管理員核對系統設定與飛書後台的完整返回網址。",
    "invalid_secret": "飛書應用憑證不正確，請管理員核對 App ID 與 App Secret。",
    "app_unavailable": "飛書應用尚未發布、啟用或安裝，請管理員確認應用狀態。",
    "no_access": "你的飛書帳號沒有此應用的使用權限，請管理員確認應用可用範圍。",
    "invalid_user": "飛書帳號已停用、離職或無法取得有效身分，請聯絡管理員。",
    "provider_rejected": "飛書未接受本次登入，請管理員核對應用憑證、返回網址及可用範圍。",
    "invalid_identity": "飛書未回傳完整的企業與使用者身分，請重新登入或聯絡管理員。",
    "wrong_tenant": "這個飛書帳號所屬的企業不在本系統允許範圍內。請切換至公司帳號再登入。",
    "tenant_setup": "飛書已確認你的身分，但系統尚未指定允許登入的企業。請管理員核對下方識別碼並填入設定；目前尚未開放業務資料。",
    "inactive": "這個系統帳號已停用，請聯絡管理員。",
    "expired": "登入狀態已失效，請重新使用飛書登入。",
    "logged_out": "已登出簽單台。你在飛書本身的登入狀態不受影響。",
}


class LoginError(Exception):
    def __init__(self, key):
        self.key = key
        super().__init__(MESSAGES.get(key, MESSAGES["provider_rejected"]))


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def safe_next(value):
    # 本系統僅需要返回首頁或前端的 #/ 路由，不接受任意網址。
    if isinstance(value, str) and len(value) <= 1500 and not any(ord(c) < 32 for c in value) and "\\" not in value:
        if value == "/" or value.startswith("/#/"):
            return value
    return "/"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def provider_json(url, *, form=None, bearer=None, as_json=False):
    """只呼叫固定官方 HTTPS 端點；錯誤不帶入令牌、密鑰或回應原文。"""
    if url not in {TOKEN_URL, V3_TOKEN_URL, USER_INFO_URL}:
        raise LoginError("provider_rejected")
    headers = {"Accept": "application/json"}
    payload = None
    if form is not None:
        if as_json:
            payload = json.dumps(form).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        else:
            payload = urlencode(form).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
    if bearer:
        headers["Authorization"] = "Bearer " + bearer
    req = Request(url, data=payload, headers=headers)
    try:
        response = build_opener(NoRedirect()).open(req, timeout=10)
    except HTTPError as error:
        if error.code not in {400, 401, 403}:
            error.close()
            raise LoginError("provider_unavailable") from None
        response = error
    except (URLError, OSError, ValueError):
        raise LoginError("provider_unavailable") from None
    try:
        with response:
            content = response.read(262145)
        if len(content) > 262144:
            raise ValueError
        result = json.loads(content)
        if not isinstance(result, dict):
            raise ValueError
    except (ValueError, OSError):
        raise LoginError("provider_unavailable") from None
    return result


def provider_ok(result):
    if not isinstance(result, dict):
        raise LoginError("provider_rejected")
    code = result.get("code")
    if type(code) is int and code == 0 and not result.get("error"):
        return
    if type(code) is not int:
        raise LoginError("provider_rejected")
    mapped = {20002: "invalid_secret", 20003: "invalid_code", 20004: "invalid_code", 20065: "invalid_code",
              20049: "invalid_pkce", 20071: "redirect_mismatch",
              20009: "app_unavailable", 20048: "app_unavailable", 20069: "app_unavailable", 20010: "no_access",
              20008: "invalid_user", 20021: "invalid_user", 20022: "invalid_user", 20023: "invalid_user", 20066: "invalid_user"}
    raise LoginError(mapped.get(code, "provider_rejected"))


def provider_diagnostics(result):
    """只保留回應結構與固定格式錯誤資訊，不記錄任何憑證值。"""
    if not isinstance(result, dict):
        return {"response_type": type(result).__name__}
    code = result.get("code")
    error = result.get("error")
    nested = result.get("data")
    return {"code": code if type(code) is int else None,
            "code_type": type(code).__name__,
            "oauth_error": error if isinstance(error, str) and re.fullmatch(r"[a-z_]{1,80}", error) else None,
            "token_present": bool(result.get("access_token")),
            "expires_type": type(result.get("expires_in")).__name__,
            "data_type": type(nested).__name__,
            "nested_token_present": isinstance(nested, dict) and bool(nested.get("access_token"))}


class FeishuAuth:
    def __init__(self, app, db, transaction, record):
        self.app, self.db, self.transaction, self.record = app, db, transaction, record
        if app.config.get("AUTH_TEST_BYPASS") and not app.testing:
            raise RuntimeError("登入略過選項僅能用於自動測試")
        with app.app_context():
            db().executescript(SCHEMA)
            self.access = AccessControl(app, db, transaction, record)
        app.extensions["feishu_auth"] = self
        app.before_request(self.guard)
        app.add_url_rule("/login", "login", self.login_page)
        app.add_url_rule("/waiting", "waiting", self.waiting_page)
        app.add_url_rule("/auth/feishu/start", "feishu_start", self.start)
        app.add_url_rule("/auth/feishu/callback", "feishu_callback", self.callback)
        app.add_url_rule("/api/v1/auth/me", "auth_me", self.me)
        app.add_url_rule("/api/v1/auth/logout", "auth_logout", self.logout, methods=["POST"])

    @property
    def config(self):
        return self.app.config

    def path(self, relative):
        return self.config["APP_BASE_PATH"] + relative

    def setup(self):
        missing = [key for key in ("FEISHU_APP_ID", "FEISHU_APP_SECRET") if not self.config.get(key)]
        if not re.fullmatch(r"cli_[a-zA-Z0-9]+", self.config.get("FEISHU_APP_ID", "")):
            if "FEISHU_APP_ID" not in missing:
                missing.append("FEISHU_APP_ID")
        tenant = self.config.get("FEISHU_TENANT_KEY", "")
        if tenant and not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", tenant):
            missing.append("FEISHU_TENANT_KEY")
        uri = self.config.get("FEISHU_REDIRECT_URI", "")
        origin = ""
        try:
            parsed = urlsplit(uri)
            valid = (parsed.scheme == "https" or (parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}))
            valid = valid and bool(parsed.hostname) and not parsed.username and not parsed.password
            valid = valid and parsed.path == self.path("/auth/feishu/callback") and not parsed.query and not parsed.fragment
            _ = parsed.port
            if not valid:
                raise ValueError
            origin = f"{parsed.scheme}://{parsed.netloc}"
        except (ValueError, TypeError):
            missing.append("FEISHU_REDIRECT_URI")
        try:
            hours = int(self.config.get("FEISHU_SESSION_HOURS", "8"))
            if not 1 <= hours <= 24:
                raise ValueError
        except (TypeError, ValueError):
            hours = 8
            missing.append("FEISHU_SESSION_HOURS")
        return {"ready": not missing, "missing": missing, "origin": origin, "secure": origin.startswith("https://"), "session_seconds": hours * 3600}

    def cookie(self, response, name, value, max_age):
        response.set_cookie(name, value, max_age=max_age, httponly=True, secure=self.setup()["secure"], samesite="Lax", path=self.config["APP_BASE_PATH"] or "/")

    def delete_cookie(self, response, name):
        response.delete_cookie(name, path=self.config["APP_BASE_PATH"] or "/", secure=self.setup()["secure"], httponly=True, samesite="Lax")

    def current(self):
        token = request.cookies.get(SESSION_COOKIE, "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            return None
        row = self.db().execute("""SELECT s.token_hash,s.csrf_token,s.expires_at,s.client_id,
            u.id,u.app_id,u.tenant_key,u.open_id,u.name,u.active,u.role,u.role_version,u.is_sales FROM auth_sessions s
            JOIN auth_users u ON u.id=s.user_id WHERE s.token_hash=?""", (digest(token),)).fetchone()
        if row is None:
            return None
        if (row["expires_at"] <= int(time.time()) or not row["active"] or
                not self.config.get("FEISHU_TENANT_KEY") or row["tenant_key"] != self.config["FEISHU_TENANT_KEY"] or
                row["client_id"] != self.config.get("FEISHU_APP_ID") or not self.setup()["ready"]):
            self.db().execute("DELETE FROM auth_sessions WHERE token_hash=?", (digest(token),))
            return None
        return dict(row)

    def guard(self):
        g.auth_user = None
        g.auth_session = None
        if self.app.testing and self.config.get("AUTH_TEST_BYPASS"):
            g.auth_user = {"id": 0, "name": "自動測試", "role": "admin"}
            return
        public = {"login", "feishu_start", "feishu_callback", "health"}
        if request.endpoint in public or request.path in {"/static/styles.css", "/static/waiting.js"}:
            # 已登入時前往 /login 可直接回系統。
            if request.endpoint == "login":
                g.auth_session = self.current()
                g.auth_user = g.auth_session
            return
        g.auth_session = self.current()
        g.auth_user = g.auth_session
        if g.auth_user is None:
            if request.path.startswith("/api/"):
                return jsonify(error=MESSAGES["expired"], login_url=self.path("/login"), authenticated=False), 401
            return redirect(self.path("/login"))
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            csrf = request.headers.get("X-CSRF-Token", "")
            if not csrf or not hmac.compare_digest(csrf, g.auth_session["csrf_token"]):
                return jsonify(error="頁面驗證已失效，請重新整理後再操作"), 403
        if g.auth_user["role"] == "pending" and request.endpoint not in {"waiting", "auth_me", "auth_logout"}:
            if request.path.startswith("/api/"):
                return jsonify(error="請等待管理員分配身分後再使用系統", approval_required=True,
                               waiting_url=self.path("/waiting")), 403
            return redirect(self.path("/waiting"))

    def login_response(self, key=None, *, status=200, tenant_candidate=None, next_path="/"):
        setup = self.setup()
        message = MESSAGES.get(key, "")
        if not setup["ready"] and not message:
            message = MESSAGES["not_configured"]
        return render_template("login.html", ready=setup["ready"], message=message,
                               tenant_candidate=tenant_candidate, next_path=safe_next(next_path),
                               system_url=(setup["origin"] + self.path("/")) if setup["origin"] else None), status

    def login_page(self):
        next_path = safe_next(request.args.get("next"))
        if g.auth_user:
            if g.auth_user["role"] == "pending":
                return redirect(url_for("waiting", next=next_path))
            return redirect(self.path(next_path))
        key = request.args.get("reason")
        return self.login_response(key if key in {"expired", "logged_out"} else None, next_path=next_path)

    def waiting_page(self):
        next_path = safe_next(request.args.get("next"))
        if g.auth_user["role"] != "pending":
            return redirect(self.path(next_path))
        return render_template("waiting.html", user=g.auth_user, next_url=self.path(next_path),
                               csrf_token=g.auth_session["csrf_token"])

    def start(self):
        setup = self.setup()
        if not setup["ready"]:
            return self.login_response("not_configured", status=503)
        if request.host_url.rstrip("/") != setup["origin"]:
            return self.login_response("wrong_origin", status=400)
        stamp = int(time.time())
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(48)
        browser = request.cookies.get(FLOW_COOKIE, "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", browser):
            browser = secrets.token_urlsafe(32)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")
        with self.transaction():
            self.db().execute("DELETE FROM auth_flows WHERE expires_at<=?", (stamp,))
            self.db().execute("DELETE FROM auth_sessions WHERE expires_at<=?", (stamp,))
            self.db().execute("INSERT INTO auth_flows(state_hash,browser_hash,verifier,next_path,created_at,expires_at) VALUES(?,?,?,?,?,?)",
                              (digest(state), digest(browser), verifier, safe_next(request.args.get("next")), stamp, stamp + 300))
        params = {"client_id": self.config["FEISHU_APP_ID"], "response_type": "code", "redirect_uri": self.config["FEISHU_REDIRECT_URI"],
                  "state": state, "code_challenge": challenge, "code_challenge_method": "S256"}
        # 單純登入使用基本身分，無需通訊錄、手機、郵箱或 offline_access 權限。
        response = redirect(AUTHORIZE_URL + "?" + urlencode(params))
        self.cookie(response, FLOW_COOKIE, browser, 600)
        return response

    def consume_flow(self):
        state = request.args.get("state", "")
        browser = request.cookies.get(FLOW_COOKIE, "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", state) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", browser):
            raise LoginError("invalid_state")
        with self.transaction():
            row = self.db().execute("SELECT * FROM auth_flows WHERE state_hash=?", (digest(state),)).fetchone()
            if row is None or row["expires_at"] <= int(time.time()) or not hmac.compare_digest(row["browser_hash"], digest(browser)):
                raise LoginError("invalid_state")
            self.db().execute("DELETE FROM auth_flows WHERE state_hash=?", (digest(state),))
        return dict(row)

    def callback(self):
        if not self.setup()["ready"]:
            return self.login_response("not_configured", status=503)
        stage = "validate_callback"
        diagnostics = {}
        try:
            if request.host_url.rstrip("/") != self.setup()["origin"]:
                raise LoginError("wrong_origin")
            if any(len(request.args.getlist(k)) != 1 for k in ("state",)) or len(request.args.getlist("code")) > 1:
                raise LoginError("invalid_state")
            flow = self.consume_flow()
            if request.args.get("error"):
                raise LoginError("denied")
            code = request.args.get("code", "")
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,512}", code):
                raise LoginError("invalid_code")
            stage = "exchange_token"
            token = provider_json(TOKEN_URL, form={"grant_type": "authorization_code", "client_id": self.config["FEISHU_APP_ID"],
                                  "client_secret": self.config["FEISHU_APP_SECRET"], "code": code,
                                  "redirect_uri": self.config["FEISHU_REDIRECT_URI"], "code_verifier": flow["verifier"]}, as_json=True)
            diagnostics = provider_diagnostics(token)
            provider_ok(token)
            access_token = token.get("access_token")
            expires = token.get("expires_in")
            if (not isinstance(access_token, str) or not access_token or len(access_token) > 16384 or
                    not isinstance(expires, int) or isinstance(expires, bool) or expires <= 0):
                raise LoginError("provider_rejected")
            stage = "load_identity"
            diagnostics = {}
            identity = provider_json(USER_INFO_URL, bearer=access_token)
            diagnostics = provider_diagnostics(identity)
            provider_ok(identity)
            identity = identity.get("data")
            if not isinstance(identity, dict):
                raise LoginError("invalid_identity")
            for field in ("tenant_key", "open_id", "name"):
                if not isinstance(identity.get(field), str) or not identity[field].strip() or len(identity[field]) > 256:
                    raise LoginError("invalid_identity")
            if any(not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", identity[field]) for field in ("tenant_key", "open_id")):
                raise LoginError("invalid_identity")
            stage = "check_company"
            tenant = self.config.get("FEISHU_TENANT_KEY", "")
            if not tenant:
                # 僅顯示飛書已驗證的企業識別碼供管理員核對，不自動信任第一位登入者。
                response, status = self.login_response("tenant_setup", status=403, tenant_candidate=identity["tenant_key"])
                response = self.app.make_response((response, status))
                self.delete_cookie(response, FLOW_COOKIE)
                return response
            if not hmac.compare_digest(tenant, identity["tenant_key"]):
                raise LoginError("wrong_tenant")
            stage = "create_session"
            raw_session = secrets.token_urlsafe(32)
            csrf_token = secrets.token_urlsafe(32)
            stamp = int(time.time())
            ttl = min(expires, self.setup()["session_seconds"])
            with self.transaction():
                self.db().execute("""INSERT INTO auth_users(app_id,tenant_key,open_id,name,created_at,last_login_at) VALUES(?,?,?,?,?,?)
                    ON CONFLICT(app_id,tenant_key,open_id) DO UPDATE SET name=excluded.name,last_login_at=excluded.last_login_at""",
                                  (self.config["FEISHU_APP_ID"], tenant, identity["open_id"], identity["name"], stamp, stamp))
                user = self.db().execute("SELECT * FROM auth_users WHERE app_id=? AND tenant_key=? AND open_id=?",
                                         (self.config["FEISHU_APP_ID"], tenant, identity["open_id"])).fetchone()
                if not user["active"]:
                    raise LoginError("inactive")
                self.access.claim_initial_admin(user)
                self.db().execute("UPDATE reps SET name=? WHERE auth_user_id=?", (identity["name"], user["id"]))
                user = self.db().execute("SELECT * FROM auth_users WHERE id=?", (user["id"],)).fetchone()
                # 重新登入時撤銷同一瀏覽器舊的登入憑證，避免殘留。
                old = request.cookies.get(SESSION_COOKIE)
                if old:
                    self.db().execute("DELETE FROM auth_sessions WHERE token_hash=?", (digest(old),))
                self.db().execute("INSERT INTO auth_sessions(token_hash,user_id,client_id,csrf_token,created_at,expires_at) VALUES(?,?,?,?,?,?)",
                                  (digest(raw_session), user["id"], self.config["FEISHU_APP_ID"], csrf_token, stamp, stamp + ttl))
                g.auth_user = dict(user)
                self.record("login", "user", user["id"])
            destination = url_for("waiting", next=flow["next_path"]) if user["role"] == "pending" else self.path(flow["next_path"])
            response = redirect(destination, code=303)
            self.cookie(response, SESSION_COOKIE, raw_session, ttl)
            self.delete_cookie(response, FLOW_COOKIE)
            return response
        except LoginError as error:
            # 僅記錄固定錯誤代碼，不記錄 code、state、密鑰、token 或個資。
            self.app.logger.warning("Feishu login failed: stage=%s reason=%s response=%s", stage, error.key, json.dumps(diagnostics))
            return self.login_response(error.key, status=403 if error.key in {"wrong_tenant", "no_access", "inactive"} else 400)

    def me(self):
        user = g.auth_user
        auth_session = g.auth_session
        return jsonify(authenticated=True, auth_required=not bool(self.config.get("AUTH_TEST_BYPASS")),
                       user=self.access.summary(user), approved=user["role"] != "pending",
                       csrf_token=auth_session["csrf_token"] if auth_session else "",
                       expires_at=auth_session["expires_at"] if auth_session else None)

    def logout(self):
        with self.transaction():
            if g.auth_session:
                self.db().execute("DELETE FROM auth_sessions WHERE token_hash=?", (g.auth_session["token_hash"],))
            self.record("logout", "user", g.auth_user["id"])
        response = jsonify(ok=True, login_url=self.path("/login?reason=logged_out"))
        self.delete_cookie(response, SESSION_COOKIE)
        self.delete_cookie(response, FLOW_COOKIE)
        return response
