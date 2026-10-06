"""稿件匿名互评：Flask + SQLite + 原生 JavaScript。

规则要点：
- 主持人录入 3~8 人姓名与纯文本稿件，名单固定，按录入顺序循环互评（每人评下一人，末位评首位）。
- 主持链接与个人链接均为随机私密链接，无需注册。
- 参与者只能改自己的稿、读被分配的稿（不返回他人姓名/链接）、提交意见。
- 改稿立即使针对它的已交意见失效；提交绑定所读稿件版本，过期提交拒绝且不覆盖有效意见。
- 每篇当前稿都有有效意见时才可一次公布；公布后作者可见匿名意见，稿件与意见锁定。
- 公布后作者可对收到的意见提一次澄清问题，对应评审者答复一次；每条意见一问一答，
  问答只随参与状态快照返回给该作者与评审者，主持台与其他参与者均不可见。
- 公布后作者可凭个人链接明确确认已读自己收到的意见（已读回执）：服务端确定作者及
  对应意见，首次成功记录确认时间，重复确认仍成功且不改首次时间；打开页面、轮询或
  提问都不自动确认。作者状态返回自己的回执（与其他字段同一快照），主持台在公布后
  按留存名单原顺序显示各成员已确认/待确认与首次确认时间（不含意见或问答内容），
  其他参与者不得获得别人的回执。确认不改变稿件、意见及问答，也不影响提问或答复
  权限；换发链接后回执随原身份保留。
- 公布后作者可举报自己收到的意见，每条意见一次，原因非空且不超过 1000 字。主持台
  只显示举报编号、原因、意见原文和处理状态，不显示作者姓名、个人链接或问答；主持人
  可将待处理举报裁定为保留或屏蔽，裁定不可修改。屏蔽后作者状态的 received 为 null，
  但意见原文、问答、回执及评审者自己的意见均保持不变。
- 个人链接泄露时，主持人可凭主持链接在主持台为指定成员换发链接（公布前后均可）：
  生成新随机链接更新主持台，旧链接立即作废——其之后发起的状态读取与一切写入均 404；
  新链接沿用同一参与者的稿件、版本、分配、意见与问答，不重建身份、不使意见失效。
- 公布前主持人可凭主持链接与姓名移除成员（不能补人、不能恢复）：留存成员按原录入顺序
  重新闭合互评环，移除后至少留 3 人。退出者的个人链接永久失效（一切读取与写入 404），
  其发出或收到的意见均不计入公布条件；仅"原来评退出者"的人改评下一位留存成员
  （退出者原为当前首位时，即留存者中录入顺序最末的一位——末位评首位），
  其旧意见失效并换发新的匿名分配凭据，其他人的稿件、版本、意见与链接全部保留。
- 主持人可在主持台将已公布会话永久封存（POST /api/h/<host_token>/close），供双方只读
  查阅：仅已公布、没有未答澄清问题且没有待处理举报时可封存，未公布或仍有未解决事项返回
  409，失败不改数据。封存与提问、答复、举报、换发等所有写入在 BEGIN IMMEDIATE 事务内
  按提交顺序串行生效——封存成功后不可能再写入新问题或举报，也封存不掉已有未解决事项的会话。
  封存后主持状态与参与状态同一快照新增 closed=true；该会话所有合法写请求（含重复确认已读、
  换发链接、举报裁定）及重复封存一律 409，无效链接仍 404。封存不删除任何数据、不撤销链接：
  原读取权限、匿名隔离与举报屏蔽语义全部保留；未封存会话的旧接口行为不变。
- 参与状态随快照返回不暴露作者身份的分配凭据；提交意见同时校验凭据与稿件版本，
  分配改变后缺少凭据或沿用旧凭据一律 409（即使两篇稿件版本号相同）；分配从未变过时
  兼容不带凭据的旧提交格式。移除、意见提交与公布在写事务内按提交顺序串行生效。
- 已公布会话可发布一次匿名合辑：参与者凭个人链接授权或撤回本人稿件（初始未授权，
  换发链接不改变授权）；主持台显示各人授权，按姓名选择非空、无重复的留存名单发布。
  仅已公布且未封存时可授权/撤回/发布，否则 409；无效链接或成员 404，非法名单 400；
  任一入选稿未授权则整次发布 409、不留合辑。授权变更与发布在写事务内按提交顺序生效：
  入选作者先撤回则发布失败；发布先提交则全部授权冻结，变更及重复发布 409。成功生成
  独立随机公开链接，入选稿文固化、按原名单顺序编号；公开只读页及响应只含编号与稿文
  （不含姓名、私密链接、意见、问答或授权记录），无效公开链接 404；封存后合辑仍可只读。
- 所有写操作在 BEGIN IMMEDIATE 事务内"先检查后变更"，拒绝即整体回滚，不留部分变更。
- 状态读取（参与页、主持台）在单个只读事务内完成：整个响应取自同一快照；
  WAL 下读取不阻塞其他连接的写入，写入也不阻塞读取。
"""

import os
import secrets
import sqlite3
from urllib.parse import urlparse

from flask import Flask, g, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "data", "app.db"))
PORT = int(os.environ.get("PORT", "8000"))

MIN_PEOPLE = 3
MAX_PEOPLE = 8
MAX_NAME_LEN = 50
MAX_TEXT_LEN = 100000
MAX_REPORT_REASON_LEN = 1000

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    host_token TEXT NOT NULL UNIQUE,
    published INTEGER NOT NULL DEFAULT 0,
    -- 封存：主持人可将已公布且无未解决事项的会话永久封存为双方只读；默认 0（未封存）
    closed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS participants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    token TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    position INTEGER NOT NULL,
    manuscript TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    active INTEGER NOT NULL DEFAULT 1,
    assignment_nonce TEXT NOT NULL DEFAULT '',
    assignment_gen INTEGER NOT NULL DEFAULT 0,
    -- 匿名合辑授权：本人是否授权稿件收入合辑；初始未授权（0），换发链接不改变，
    -- 合辑发布后全部冻结
    anthology_consent INTEGER NOT NULL DEFAULT 0,
    UNIQUE (session_id, position)
);
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    reviewer_id INTEGER NOT NULL REFERENCES participants(id),
    target_id INTEGER NOT NULL REFERENCES participants(id),
    target_version INTEGER NOT NULL,
    content TEXT NOT NULL,
    valid INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (session_id, reviewer_id)
);
-- 公布后的一问一答：每条意见至多一条澄清，问题/答案提交后不可修改
CREATE TABLE IF NOT EXISTS clarifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id INTEGER NOT NULL UNIQUE REFERENCES reviews(id),
    question TEXT NOT NULL,
    answer TEXT,
    asked_at TEXT NOT NULL DEFAULT (datetime('now')),
    answered_at TEXT
);
-- 公布后的已读回执：作者确认已读自己收到的那条意见；每条意见至多一条回执，
-- 首次确认时间写入后不再改变（重复确认不覆盖）
CREATE TABLE IF NOT EXISTS receipts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id INTEGER NOT NULL UNIQUE REFERENCES reviews(id),
    acknowledged_at TEXT NOT NULL DEFAULT (datetime('now'))
);
-- 公布后的意见举报：每条收到的有效意见至多一条举报；主持人裁定为保留/屏蔽。
-- 屏蔽只改变作者是否仍能读到意见原文，意见、问答与回执本身均不删除、不修改。
CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    review_id INTEGER NOT NULL UNIQUE REFERENCES reviews(id),
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'kept', 'blocked')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    decided_at TEXT
);
-- 换发链接时登记的历史（已作废）个人链接：仅用于换发时区分"所见链接已过期"(409)，
-- 作废链接在参与端的一切读取与写入均按不存在处理（404）
CREATE TABLE IF NOT EXISTS token_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    participant_id INTEGER NOT NULL REFERENCES participants(id),
    token TEXT NOT NULL UNIQUE
);
-- 匿名合辑：已公布会话由主持人按授权名单发布一次（session_id 唯一）；公开链接独立随机
CREATE TABLE IF NOT EXISTS anthologies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL UNIQUE REFERENCES sessions(id),
    public_token TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
-- 合辑条目：入选稿文发布时复制固化，按原名单（录入）顺序从 1 编号；
-- 只存编号与稿文，不含姓名、私密链接、意见、问答或授权记录
CREATE TABLE IF NOT EXISTS anthology_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    anthology_id INTEGER NOT NULL REFERENCES anthologies(id),
    number INTEGER NOT NULL,
    manuscript TEXT NOT NULL,
    UNIQUE (anthology_id, number)
);
"""

app = Flask(__name__)


def init_db():
    """建库建表（幂等），数据落盘在 DB_PATH，重启后保留。

    旧版数据库（无 clarifications / token_history / receipts / reports /
    anthologies / anthology_items 表，或 participants 缺 active / assignment_nonce /
    assignment_gen / anthology_consent 列，或 sessions 缺 closed 列）启动时自动补建
    问答表、历史链接表、回执表、举报表、合辑表与移除成员所需列，
    既有 sessions/participants/reviews 数据原样保留；
    补列时为每位既有成员回填一个分配凭据（视为其当前分配从未变过，gen=0），
    sessions.closed 补列默认 0——既有会话一律视为未封存，封存状态随原数据卷重启保留；
    participants.anthology_consent 补列默认 0——既有成员一律视为未授权合辑；
    旧库因此自动支持移除成员、已读回执、意见举报、会话封存与匿名合辑
    （既有意见初始为未举报，既有成员初始未授权），无需手动迁移。
    """
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    session_cols = {
        r[1] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()
    }
    if "closed" not in session_cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN closed INTEGER NOT NULL DEFAULT 0")
    cols = {
        r[1]
        for r in conn.execute("PRAGMA table_info(participants)").fetchall()
    }
    if "active" not in cols:
        conn.execute("ALTER TABLE participants ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
    if "assignment_nonce" not in cols:
        conn.execute("ALTER TABLE participants ADD COLUMN assignment_nonce TEXT NOT NULL DEFAULT ''")
    if "assignment_gen" not in cols:
        conn.execute("ALTER TABLE participants ADD COLUMN assignment_gen INTEGER NOT NULL DEFAULT 0")
    if "anthology_consent" not in cols:
        # 旧库既有成员一律视为未授权合辑（初始未授权）
        conn.execute("ALTER TABLE participants ADD COLUMN anthology_consent INTEGER NOT NULL DEFAULT 0")
    # 旧库既有成员：回填一个分配凭据，标记为"分配从未变过"（gen=0，兼容旧提交格式）
    rows = conn.execute(
        "SELECT id FROM participants WHERE assignment_nonce=''"
    ).fetchall()
    for (pid,) in rows:
        conn.execute(
            "UPDATE participants SET assignment_nonce=? WHERE id=?",
            (secrets.token_urlsafe(24), pid),
        )
    conn.commit()
    conn.close()


init_db()


def get_db():
    if "db" not in g:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        g.db = conn
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


@app.errorhandler(Exception)
def handle_exception(e):
    if isinstance(e, HTTPException):
        return e
    app.logger.exception("unhandled error")
    return jsonify({"error": "服务器内部错误"}), 500


def err(message, status=400, **extra):
    payload = {"error": message}
    payload.update(extra)
    return jsonify(payload), status


def get_target(conn, session_id, position):
    """循环分配（按原录入顺序，在留存成员间闭合）：position 之后第一位仍留存的人；
    其后无人时回到首位仍留存者。移除成员后环自然闭合，例如 [甲,乙,丙,丁] 移除乙后，
    甲评丙、丙评丁、丁评甲。
    """
    later = conn.execute(
        "SELECT * FROM participants WHERE session_id=? AND active=1 AND position>? "
        "ORDER BY position LIMIT 1",
        (session_id, position),
    ).fetchone()
    if later is not None:
        return later
    return conn.execute(
        "SELECT * FROM participants WHERE session_id=? AND active=1 "
        "ORDER BY position LIMIT 1",
        (session_id,),
    ).fetchone()


def find_session_by_host(conn, host_token):
    return conn.execute(
        "SELECT * FROM sessions WHERE host_token=?", (host_token,)
    ).fetchone()


def find_participant(conn, token):
    # 退出（移除）成员的个人链接永久失效：连同其历史链接一律按不存在处理（404）
    return conn.execute(
        "SELECT * FROM participants WHERE token=? AND active=1", (token,)
    ).fetchone()


def find_participant_by_name(conn, session_id, name):
    # 已移除者"成员不存在"：不能凭旧姓名再次操作（不支持恢复/补人）
    return conn.execute(
        "SELECT * FROM participants WHERE session_id=? AND name=? AND active=1",
        (session_id, name),
    ).fetchone()


def extract_personal_token(link):
    """从"页面所见个人链接"取出 token：接受 /p/<token> 或完整 URL（如 http://host/p/<token>）。

    只识别形如个人链接的输入；其余返回 None，由调用方按 400 处理。
    """
    if not isinstance(link, str):
        return None
    value = link.strip()
    if value.startswith("/"):
        path = value
    else:
        parsed = urlparse(value)
        if not parsed.netloc or not parsed.scheme:
            return None
        path = parsed.path
    parts = [seg for seg in path.split("/") if seg]
    if len(parts) == 2 and parts[0] == "p":
        return parts[1]
    return None


def new_participant_token(conn):
    """生成不与任何当前链接或历史（已作废）链接冲突的随机个人链接 token。"""
    while True:
        token = secrets.token_urlsafe(24)
        exists = conn.execute(
            "SELECT 1 FROM participants WHERE token=?"
            " UNION ALL SELECT 1 FROM token_history WHERE token=? LIMIT 1",
            (token, token),
        ).fetchone()
        if exists is None:
            return token


def new_assignment_nonce(conn):
    """生成不与任何当前分配凭据冲突的随机凭据（不暴露作者身份）。"""
    while True:
        nonce = secrets.token_urlsafe(24)
        exists = conn.execute(
            "SELECT 1 FROM participants WHERE assignment_nonce=? AND active=1 LIMIT 1",
            (nonce,),
        ).fetchone()
        if exists is None:
            return nonce


def find_anthology(conn, session_id):
    """本会话的匿名合辑（至多一份；未发布为 None）。"""
    return conn.execute(
        "SELECT * FROM anthologies WHERE session_id=?", (session_id,)
    ).fetchone()


def new_anthology_token(conn):
    """生成不与任何已发合辑冲突的独立随机公开链接 token。"""
    while True:
        token = secrets.token_urlsafe(24)
        exists = conn.execute(
            "SELECT 1 FROM anthologies WHERE public_token=? LIMIT 1", (token,)
        ).fetchone()
        if exists is None:
            return token


def find_review_by_reviewer(conn, session_id, reviewer_id):
    return conn.execute(
        "SELECT * FROM reviews WHERE session_id=? AND reviewer_id=?",
        (session_id, reviewer_id),
    ).fetchone()


def review_is_current(review_row, target_row):
    # target 必须仍是当前被分配对象：分配改变（移除成员导致改评下一位）后旧意见失效，
    # 即便两篇稿件的版本号恰好相同
    return (
        review_row is not None
        and review_row["valid"]
        and review_row["target_id"] == target_row["id"]
        and review_row["target_version"] == target_row["version"]
    )


def find_clarification(conn, review_id):
    return conn.execute(
        "SELECT * FROM clarifications WHERE review_id=?", (review_id,)
    ).fetchone()


def clarification_view(row):
    """问答对外结构：只含问/答文本，不含任何身份信息与私密链接。"""
    if row is None:
        return None
    return {
        "question": row["question"],
        "answer": row["answer"],
        "answered": row["answer"] is not None,
    }


def find_received_review(conn, me):
    """作者当前收到的那条有效意见（针对其当前稿件版本，评审者须仍留存）。

    公布后稿件与意见冻结，这条意见随之固定；一问一答与已读回执都绑定在它上面。
    """
    return conn.execute(
        "SELECT r.* FROM reviews r JOIN participants v ON r.reviewer_id=v.id"
        " WHERE r.target_id=? AND r.valid=1 AND r.target_version=? AND v.active=1",
        (me["id"], me["version"]),
    ).fetchone()


def find_receipt(conn, review_id):
    return conn.execute(
        "SELECT * FROM receipts WHERE review_id=?", (review_id,)
    ).fetchone()


def receipt_view(row):
    """回执对外结构：是否已确认与首次确认时间；不含任何身份信息与私密链接。"""
    if row is None:
        return {"acknowledged": False, "acknowledged_at": None}
    return {"acknowledged": True, "acknowledged_at": row["acknowledged_at"]}


def find_report_by_review(conn, review_id):
    return conn.execute(
        "SELECT * FROM reports WHERE review_id=?", (review_id,)
    ).fetchone()


def find_host_report(conn, session_id, report_id):
    return conn.execute(
        "SELECT * FROM reports WHERE id=? AND session_id=?",
        (report_id, session_id),
    ).fetchone()


def report_status_view(row):
    """作者查看自己举报时的状态；无举报时为 None。"""
    if row is None:
        return None
    return {"id": row["id"], "reason": row["reason"], "status": row["status"]}


def host_report_view(row):
    """主持台举报列表：只含编号、原因、意见原文和处理状态。

    刻意不含作者/评审者姓名、个人链接、问答或回执，避免主持页借举报获得匿名信息。
    """
    return {
        "id": row["id"],
        "reason": row["reason"],
        "review": row["review_content"],
        "status": row["status"],
    }


# ---------------- 页面 ----------------

@app.get("/")
def index_page():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/h/<token>")
def host_page(token):
    return send_from_directory(app.static_folder, "host.html")


@app.get("/p/<token>")
def participant_page(token):
    return send_from_directory(app.static_folder, "participant.html")


@app.get("/a/<token>")
def anthology_page(token):
    return send_from_directory(app.static_folder, "anthology.html")


# ---------------- 创建会话 ----------------

@app.post("/api/sessions")
def create_session():
    data = request.get_json(silent=True) or {}
    people = data.get("people")
    if not isinstance(people, list) or not (MIN_PEOPLE <= len(people) <= MAX_PEOPLE):
        return err(f"需要 {MIN_PEOPLE}～{MAX_PEOPLE} 人")

    cleaned, names = [], []
    for p in people:
        if not isinstance(p, dict):
            return err("数据格式不正确")
        name = p.get("name")
        text = p.get("manuscript")
        if not isinstance(name, str) or not isinstance(text, str):
            return err("姓名与稿件必须是字符串")
        name = name.strip()
        if not name or len(name) > MAX_NAME_LEN:
            return err(f"姓名不能为空且不超过 {MAX_NAME_LEN} 字")
        if not text.strip() or len(text) > MAX_TEXT_LEN:
            return err(f"「{name}」的稿件不能为空且不超过 {MAX_TEXT_LEN} 字")
        cleaned.append((name, text))
        names.append(name)
    if len(set(names)) != len(names):
        return err("姓名不能重复")

    host_token = secrets.token_urlsafe(24)
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute("INSERT INTO sessions (host_token) VALUES (?)", (host_token,))
        session_id = cur.lastrowid
        links = []
        for i, (name, text) in enumerate(cleaned):
            token = secrets.token_urlsafe(24)
            conn.execute(
                "INSERT INTO participants (session_id, token, name, position, manuscript, assignment_nonce)"
                " VALUES (?,?,?,?,?,?)",
                (session_id, token, name, i, text, secrets.token_urlsafe(24)),
            )
            links.append({"name": name, "url": f"/p/{token}"})
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return jsonify({"host_url": f"/h/{host_token}", "people": links})


# ---------------- 主持端 ----------------

@app.get("/api/h/<host_token>")
def host_status(host_token):
    conn = get_db()
    try:
        # 只读事务：整个响应取自同一快照；WAL 下不阻塞其他连接的写入
        conn.execute("BEGIN")
        session = find_session_by_host(conn, host_token)
        if session is None:
            conn.rollback()
            return err("主持链接无效", 404)
        parts = conn.execute(
            "SELECT * FROM participants WHERE session_id=? AND active=1 ORDER BY position",
            (session["id"],),
        ).fetchall()
        reviewers = []
        can_publish = not session["published"]
        for p in parts:
            target = get_target(conn, session["id"], p["position"])
            review = find_review_by_reviewer(conn, session["id"], p["id"])
            submitted = review_is_current(review, target)
            if not submitted:
                can_publish = False
            # 已读回执：按留存名单原顺序随 reviewers 返回；仅公布后有意义（公布前一律
            # 待确认，前端不展示）。只含确认状态与首次确认时间，不含意见或问答内容
            receipt = None
            if session["published"]:
                received = find_received_review(conn, p)
                if received is not None:
                    receipt = find_receipt(conn, received["id"])
            view = receipt_view(receipt)
            reviewers.append(
                {
                    "name": p["name"],
                    "submitted": submitted,
                    "acknowledged": view["acknowledged"],
                    "acknowledged_at": view["acknowledged_at"],
                    # 匿名合辑授权：主持台按留存名单原顺序显示各人授权（同一快照）
                    "anthology_consent": bool(p["anthology_consent"]),
                }
            )
        report_rows = conn.execute(
            "SELECT rp.id, rp.reason, rp.status, r.content AS review_content"
            " FROM reports rp JOIN reviews r ON rp.review_id=r.id"
            " WHERE rp.session_id=? ORDER BY rp.id",
            (session["id"],),
        ).fetchall()
        anthology = find_anthology(conn, session["id"])
        conn.commit()
        return jsonify(
            {
                "published": bool(session["published"]),
                "closed": bool(session["closed"]),
                "can_publish": can_publish,
                "reviewers": reviewers,
                "reports": [host_report_view(r) for r in report_rows],
                # 合辑公开链接：未发布为 None；公开链接本身可公开，不含任何私密信息
                "anthology_url": (
                    f"/a/{anthology['public_token']}" if anthology is not None else None
                ),
                "links": [
                    {"name": p["name"], "url": f"/p/{p['token']}"} for p in parts
                ],
            }
        )
    except Exception:
        conn.rollback()
        raise


@app.post("/api/h/<host_token>/publish")
def publish(host_token):
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        session = find_session_by_host(conn, host_token)
        if session is None:
            conn.rollback()
            return err("主持链接无效", 404)
        if session["closed"]:
            conn.rollback()
            return err("会话已封存，只读，不能再写入", 409)
        if session["published"]:
            conn.rollback()
            return err("已经公布过了", 409)
        parts = conn.execute(
            "SELECT * FROM participants WHERE session_id=? AND active=1 ORDER BY position",
            (session["id"],),
        ).fetchall()
        missing = []
        for p in parts:
            review = conn.execute(
                "SELECT 1 FROM reviews r JOIN participants v ON r.reviewer_id=v.id"
                " WHERE r.target_id=? AND r.valid=1 AND r.target_version=?"
                " AND v.active=1",
                (p["id"], p["version"]),
            ).fetchone()
            if review is None:
                missing.append(p["name"])
        if missing:
            conn.rollback()
            return err("仍有稿件缺少针对当前版本的有效意见，不能公布", 409, missing=missing)
        conn.execute("UPDATE sessions SET published=1 WHERE id=?", (session["id"],))
        conn.commit()
        return jsonify({"ok": True})
    except Exception:
        conn.rollback()
        raise


@app.post("/api/h/<host_token>/close")
def close_session(host_token):
    """主持人将已公布会话永久封存，供双方只读查阅。

    在 BEGIN IMMEDIATE 事务内"先检查后变更"，与提问、答复、举报、换发等一切写入按
    提交顺序串行生效：
    - 主持链接无效 → 404；
    - 未公布、存在未答澄清问题（已问未答）或待处理举报（pending）、或已封存 → 409，
      失败不改任何数据；
    - 条件满足则把 closed 置 1 提交。封存成功后，该会话的所有合法写请求（含重复确认
      已读、换发链接、举报裁定）以及重复封存都返回 409；封存不删除数据、不撤销链接，
      读取权限、匿名隔离与屏蔽语义不变。
    """
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        session = find_session_by_host(conn, host_token)
        if session is None:
            conn.rollback()
            return err("主持链接无效", 404)
        if session["closed"]:
            conn.rollback()
            return err("会话已经封存，不能重复封存", 409)
        if not session["published"]:
            conn.rollback()
            return err("尚未公布，不能封存", 409)
        # 未答澄清问题：一问一答中已提问但评审者尚未答复（封存后无法再答复）
        unanswered = conn.execute(
            "SELECT 1 FROM clarifications c JOIN reviews r ON c.review_id=r.id"
            " WHERE r.session_id=? AND c.answer IS NULL LIMIT 1",
            (session["id"],),
        ).fetchone()
        if unanswered is not None:
            conn.rollback()
            return err("仍有未答复的澄清问题，不能封存", 409)
        # 待处理举报：主持人尚未裁定保留/屏蔽（封存后无法再处理）
        pending_report = conn.execute(
            "SELECT 1 FROM reports WHERE session_id=? AND status='pending' LIMIT 1",
            (session["id"],),
        ).fetchone()
        if pending_report is not None:
            conn.rollback()
            return err("仍有待处理的举报，不能封存", 409)
        cur = conn.execute(
            "UPDATE sessions SET closed=1 WHERE id=? AND closed=0",
            (session["id"],),
        )
        # BEGIN IMMEDIATE 已串行化；条件更新再兜底，并发封存最多一次成功
        if cur.rowcount != 1:
            conn.rollback()
            return err("会话已经封存，不能重复封存", 409)
        conn.commit()
        return jsonify({"ok": True, "closed": True})
    except Exception:
        conn.rollback()
        raise


@app.post("/api/h/<host_token>/rotate-link")
def rotate_link(host_token):
    """主持人为本会话某人换发个人链接（公布前后均可）。

    请求携带主持链接（路径）、姓名与主持台页面所见的个人链接；仅有效主持链接可操作
    本会话人员：主持链接无效或姓名不存在返回 404；所见链接非当前链接（已过期作废）
    返回 409；失败不改变任何数据，且任何错误响应都不返回新链接。成功时生成新的随机
    个人链接并更新主持台，旧链接登记作废——换发提交后它发起的状态读取及一切合法写入
    （改稿、意见、问题、答复）一律 404。新链接沿用同一参与者行（稿件、版本、分配、
    意见、问答均不变），其他人的链接与公布状态不变。
    """
    data = request.get_json(silent=True) or {}
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        return err("请指定要换发链接的姓名")
    name = name.strip()
    if len(name) > MAX_NAME_LEN:
        return err(f"姓名不超过 {MAX_NAME_LEN} 字")
    seen_token = extract_personal_token(data.get("link"))
    if seen_token is None:
        return err("请提供主持台页面所见的个人链接")

    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        session = find_session_by_host(conn, host_token)
        if session is None:
            conn.rollback()
            return err("主持链接无效", 404)
        if session["closed"]:
            conn.rollback()
            return err("会话已封存，只读，不能再写入", 409)
        participant = find_participant_by_name(conn, session["id"], name)
        # 姓名不存在（不属于本会话）按 404，且不透露会话中的名单
        if participant is None:
            conn.rollback()
            return err("本会话中没有该成员", 404)
        # 所见链接必须与该成员当前链接一致：已作废的旧链接/他人链接/伪造链接均 409
        if participant["token"] != seen_token:
            conn.rollback()
            retired = conn.execute(
                "SELECT 1 FROM token_history WHERE token=?", (seen_token,)
            ).fetchone()
            if retired is not None:
                return err("所见的个人链接已过期，链接已被换发", 409)
            return err("所见的个人链接与该成员当前链接不一致", 409)
        old_token = participant["token"]
        new_token = new_participant_token(conn)
        # 同一事务内：旧链接登记作废 + 当前链接换为新随机链接；
        # token_history.token UNIQUE 下同一旧链接的并发换发最多一次成功
        conn.execute(
            "INSERT INTO token_history (participant_id, token) VALUES (?,?)",
            (participant["id"], old_token),
        )
        conn.execute(
            "UPDATE participants SET token=? WHERE id=?",
            (new_token, participant["id"]),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return jsonify({"name": name, "url": f"/p/{new_token}"})


@app.post("/api/h/<host_token>/remove")
def remove_participant(host_token):
    """主持人在公布前凭主持链接与姓名移除一名成员。

    规则（名单、分配与意见在同一写事务内一起生效，失败不留任何变更）：
    - 主持链接无效或成员（按姓名）不存在/已移除 → 404；已公布，或移除后留存成员
      不足 3 人 → 409；请求缺姓名或格式不对 → 400。
    - 退出者标记为不再留存（active=0）：其个人链接（以及换发前的历史链接）永久失效，
      之后的状态读取与一切合法写入（改稿、意见、问题、答复、换发）均 404。
    - 留存成员按原录入顺序重新闭合互评环；退出者发出或收到的意见一律不计入公布条件。
      仅"原来评退出者"的留存成员改评下一位留存成员：即录入顺序中退出者前面最近的
      一位留存者；退出者原为当前首位时，则是留存者中录入顺序最末的一位（末位评首位）。
      其旧意见失效（valid=0）并换发新的匿名分配凭据（nonce 更新、gen+1）；
      其他人的稿件、版本、意见与链接原样保留。不支持补人或恢复。
    """
    data = request.get_json(silent=True) or {}
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        return err("请指定要移除成员的姓名")
    name = name.strip()
    if len(name) > MAX_NAME_LEN:
        return err(f"姓名不超过 {MAX_NAME_LEN} 字")

    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        session = find_session_by_host(conn, host_token)
        if session is None:
            conn.rollback()
            return err("主持链接无效", 404)
        if session["closed"]:
            conn.rollback()
            return err("会话已封存，只读，不能再写入", 409)
        if session["published"]:
            conn.rollback()
            return err("已公布，不能再移除成员", 409)
        removed = find_participant_by_name(conn, session["id"], name)
        if removed is None:
            conn.rollback()
            return err("本会话中没有该成员", 404)

        active_count = conn.execute(
            "SELECT COUNT(*) AS c FROM participants"
            " WHERE session_id=? AND active=1",
            (session["id"],),
        ).fetchone()["c"]
        if active_count <= MIN_PEOPLE:
            conn.rollback()
            return err(f"移除后不足 {MIN_PEOPLE} 人，不能移除", 409)

        # 原来评退出者的人：原录入顺序中退出者前面最近的一位留存者，移除后改评下一位
        predecessor = conn.execute(
            "SELECT * FROM participants WHERE session_id=? AND active=1 AND position<?"
            " ORDER BY position DESC LIMIT 1",
            (session["id"], removed["position"]),
        ).fetchone()
        if predecessor is None:
            # 退出者前面没有留存者（退出者原为当前首位）：环闭合处"末位评首位"，
            # 原来评退出者的是当前留存者中录入顺序最末的一位（不是退出者后面第一位——
            # 那是退出者原来评的人）。此时退出者 position 最小，末位必非退出者本人
            predecessor = conn.execute(
                "SELECT * FROM participants WHERE session_id=? AND active=1"
                " ORDER BY position DESC LIMIT 1",
                (session["id"],),
            ).fetchone()

        # 退出者永久退出：active=0 后其 token 与全部历史链接在所有接口上按不存在处理
        conn.execute(
            "UPDATE participants SET active=0 WHERE id=?", (removed["id"],)
        )

        if predecessor is not None:
            new_target = get_target(conn, session["id"], predecessor["position"])
            # 仅 predecessor 的分配改变：旧意见失效 + 换发新的匿名分配凭据；
            # 其他人的稿件、版本、意见与链接全部保留
            conn.execute(
                "UPDATE reviews SET valid=0 WHERE reviewer_id=? AND valid=1",
                (predecessor["id"],),
            )
            conn.execute(
                "UPDATE participants SET assignment_nonce=?, assignment_gen=assignment_gen+1"
                " WHERE id=?",
                (new_assignment_nonce(conn), predecessor["id"]),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return jsonify({"ok": True})


# ---------------- 参与端 ----------------

@app.get("/api/p/<token>")
def participant_state(token):
    """参与页数据：同一快照读取；不含他人姓名与任何私密链接；公布前不含意见、问答与举报。"""
    conn = get_db()
    try:
        # 只读事务：本人稿件、分配稿件、版本、意见与公布状态取自同一快照；
        # WAL 下读取暂停期间其他连接仍可完成合法写入
        conn.execute("BEGIN")
        me = find_participant(conn, token)
        if me is None:
            conn.rollback()
            return err("个人链接无效", 404)
        session = conn.execute(
            "SELECT * FROM sessions WHERE id=?", (me["session_id"],)
        ).fetchone()
        published = bool(session["published"])
        closed = bool(session["closed"])
        target = get_target(conn, me["session_id"], me["position"])
        my_review = find_review_by_reviewer(conn, me["session_id"], me["id"])

        resp = {
            "name": me["name"],
            "manuscript": me["manuscript"],
            "version": me["version"],
            "published": published,
            # 封存：与 published 等字段同一快照返回；true 时页面只读、一切写请求 409
            "closed": closed,
            # 匿名合辑授权：只返回本人授权状态（初始未授权），与上述字段同一快照；
            # 他人授权、合辑名单与公开链接不在此返回
            "anthology_consent": bool(me["anthology_consent"]),
            # 匿名：不返回被评稿件作者的姓名；credential 是不暴露作者身份的分配凭据，
            # 提交意见时与稿件版本一并校验
            "assignment": {
                "manuscript": target["manuscript"],
                "version": target["version"],
                "credential": me["assignment_nonce"],
            },
            "my_review": None,
            "received": None,
            # 已读回执：公布前一律 None；公布后为作者自己的回执（不含他人回执）
            "receipt": None,
            # 一问一答：公布前一律 None；公布后只返回与本人相关的那条意见的问答
            "received_clarification": None,
            "my_review_clarification": None,
            # 作者对自己收到意见的举报状态；未举报为 None。其他参与者不会得到该对象
            "report": None,
            # 收到的意见被主持人屏蔽后，received 置空并以此明确标明"已屏蔽"
            "received_blocked": False,
        }
        if my_review is not None:
            resp["my_review"] = {
                "content": my_review["content"],
                "version": my_review["target_version"],
                "valid": review_is_current(my_review, target),
                # 失效原因之一：分配改变（原被评成员退出）改评他人，旧意见已作废
                "target_changed": my_review["target_id"] != target["id"],
            }
            # 评审者视角：我提交的那条意见收到的澄清（公布后才可见，且仅本人可见）
            if published:
                resp["my_review_clarification"] = clarification_view(
                    find_clarification(conn, my_review["id"])
                )
        if published:
            # 匿名：只返回意见内容，不返回评审人任何信息；评审者须仍留存
            row = find_received_review(conn, me)
            if row is not None:
                report = find_report_by_review(conn, row["id"])
                resp["report"] = report_status_view(report)
                # 回执不随屏蔽改变，仍在同一快照返回
                resp["receipt"] = receipt_view(find_receipt(conn, row["id"]))
                # 主持人屏蔽举报后：作者不再取得意见原文，但问答、回执仍绑定原意见且不变
                if report is not None and report["status"] == "blocked":
                    resp["received"] = None
                    resp["received_blocked"] = True
                else:
                    resp["received"] = row["content"]
                # 作者视角：就我收到的这条意见的一问一答（仅本人与对应评审者可见）；
                # 屏蔽只隐藏意见原文，不删除、不改变问答
                resp["received_clarification"] = clarification_view(
                    find_clarification(conn, row["id"])
                )
        conn.commit()
        return jsonify(resp)
    except Exception:
        conn.rollback()
        raise


@app.put("/api/p/<token>/manuscript")
def edit_manuscript(token):
    data = request.get_json(silent=True) or {}
    text = data.get("manuscript")
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_LEN:
        return err(f"稿件不能为空且不超过 {MAX_TEXT_LEN} 字")

    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        me = find_participant(conn, token)
        if me is None:
            conn.rollback()
            return err("个人链接无效", 404)
        session = conn.execute(
            "SELECT published, closed FROM sessions WHERE id=?", (me["session_id"],)
        ).fetchone()
        if session["closed"]:
            conn.rollback()
            return err("会话已封存，只读，不能再写入", 409)
        if session["published"]:
            conn.rollback()
            return err("已公布，稿件不可再修改", 409)
        # 同一事务内：改稿 + 版本号 +1 + 使针对该稿的已交意见失效（其他意见保留）
        conn.execute(
            "UPDATE participants SET manuscript=?, version=version+1 WHERE id=?",
            (text, me["id"]),
        )
        conn.execute(
            "UPDATE reviews SET valid=0 WHERE target_id=? AND valid=1", (me["id"],)
        )
        new_version = conn.execute(
            "SELECT version FROM participants WHERE id=?", (me["id"],)
        ).fetchone()["version"]
        conn.commit()
        return jsonify({"ok": True, "version": new_version})
    except Exception:
        conn.rollback()
        raise


@app.post("/api/p/<token>/review")
def submit_review(token):
    data = request.get_json(silent=True) or {}
    content = data.get("content")
    version = data.get("version")
    if not isinstance(content, str) or not content.strip() or len(content) > MAX_TEXT_LEN:
        return err(f"意见不能为空且不超过 {MAX_TEXT_LEN} 字")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        return err("缺少所读稿件的版本号")
    # credential 可选：分配从未变过（gen=0）时兼容旧客户端的原提交格式；
    # 分配改变后（gen>0）必须携带，且类型须为字符串
    credential = data.get("credential")
    if credential is not None and not isinstance(credential, str):
        return err("分配凭据格式不正确")

    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        me = find_participant(conn, token)
        if me is None:
            conn.rollback()
            return err("个人链接无效", 404)
        session = conn.execute(
            "SELECT published, closed FROM sessions WHERE id=?", (me["session_id"],)
        ).fetchone()
        if session["closed"]:
            conn.rollback()
            return err("会话已封存，只读，不能再写入", 409)
        if session["published"]:
            conn.rollback()
            return err("已公布，意见不可再提交或修改", 409)
        target = get_target(conn, me["session_id"], me["position"])
        # 先校验分配凭据：分配改变（移除成员导致改评下一位）后，缺少凭据或沿用旧凭据
        # 一律 409——即使两篇稿件的版本号恰好相同
        if me["assignment_gen"] > 0 and credential != me["assignment_nonce"]:
            conn.rollback()
            return err(
                "分配已改变，请重新阅读新分配的稿件后再提交",
                409,
                assignment_changed=True,
            )
        # 再绑定所读稿件版本：过期提交整体拒绝，不覆盖任何已有意见
        if target["version"] != version:
            conn.rollback()
            return err(
                "稿件已更新，请阅读最新版本后再提交",
                409,
                current_version=target["version"],
            )
        conn.execute(
            """
            INSERT INTO reviews (session_id, reviewer_id, target_id, target_version, content, valid, updated_at)
            VALUES (?,?,?,?,?,1,datetime('now'))
            ON CONFLICT(session_id, reviewer_id)
            DO UPDATE SET content=excluded.content,
                          target_id=excluded.target_id,
                          target_version=excluded.target_version,
                          valid=1,
                          updated_at=datetime('now')
            """,
            (me["session_id"], me["id"], target["id"], version, content),
        )
        conn.commit()
        return jsonify({"ok": True})
    except Exception:
        conn.rollback()
        raise


# ---------------- 公布后的一问一答 ----------------


@app.post("/api/p/<token>/question")
def ask_clarification(token):
    """作者就自己收到的意见提一次澄清问题：公布后开放，每条意见至多一问，提交不可修改。"""
    data = request.get_json(silent=True) or {}
    question = data.get("question")
    # 文本沿用现有意见的非空及长度限制
    if not isinstance(question, str) or not question.strip() or len(question) > MAX_TEXT_LEN:
        return err(f"问题不能为空且不超过 {MAX_TEXT_LEN} 字")

    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        me = find_participant(conn, token)
        if me is None:
            conn.rollback()
            return err("个人链接无效", 404)
        session = conn.execute(
            "SELECT * FROM sessions WHERE id=?", (me["session_id"],)
        ).fetchone()
        # 封存后只读：提问一律 409，且不改变任何数据
        if session["closed"]:
            conn.rollback()
            return err("会话已封存，只读，不能再写入", 409)
        # 公布前提问一律 409，且不改变任何数据
        if not session["published"]:
            conn.rollback()
            return err("尚未公布，不能提问", 409)
        # 服务端按个人链接确定身份与对应意见：只能问自己收到的那条有效意见
        review = find_received_review(conn, me)
        if review is None:
            conn.rollback()
            return err("没有可追问的意见", 409)
        # 每条意见只容纳一问；重复提交 409
        if find_clarification(conn, review["id"]) is not None:
            conn.rollback()
            return err("该意见已经提过问，不能重复提问或修改", 409)
        # review_id UNIQUE：并发提交同一问题最多一次成功，其余触发约束失败
        conn.execute(
            "INSERT INTO clarifications (review_id, question) VALUES (?,?)",
            (review["id"], question),
        )
        conn.commit()
        return jsonify({"ok": True})
    except Exception:
        conn.rollback()
        raise


@app.post("/api/p/<token>/answer")
def answer_clarification(token):
    """对应评审者就作者的澄清问题答复一次：尚无问题或已答复均 409，提交不可修改。"""
    data = request.get_json(silent=True) or {}
    answer = data.get("answer")
    # 文本沿用现有意见的非空及长度限制
    if not isinstance(answer, str) or not answer.strip() or len(answer) > MAX_TEXT_LEN:
        return err(f"答复不能为空且不超过 {MAX_TEXT_LEN} 字")

    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        me = find_participant(conn, token)
        if me is None:
            conn.rollback()
            return err("个人链接无效", 404)
        session = conn.execute(
            "SELECT * FROM sessions WHERE id=?", (me["session_id"],)
        ).fetchone()
        # 封存后只读：答复一律 409
        if session["closed"]:
            conn.rollback()
            return err("会话已封存，只读，不能再写入", 409)
        # 公布前答复一律 409
        if not session["published"]:
            conn.rollback()
            return err("尚未公布，不能答复", 409)
        # 服务端按个人链接确定身份：只能答复本人提交的那条意见上的问题
        review = find_review_by_reviewer(conn, me["session_id"], me["id"])
        if review is None:
            conn.rollback()
            return err("没有可答复的问题", 409)
        clarification = find_clarification(conn, review["id"])
        # 尚无问题时答复 409
        if clarification is None:
            conn.rollback()
            return err("作者尚未提问，暂不能答复", 409)
        # 每条意见只容纳一答；重复提交 409
        if clarification["answer"] is not None:
            conn.rollback()
            return err("该问题已经答复，不能重复答复或修改", 409)
        conn.execute(
            "UPDATE clarifications SET answer=?, answered_at=datetime('now') WHERE id=? AND answer IS NULL",
            (answer, clarification["id"]),
        )
        conn.commit()
        return jsonify({"ok": True})
    except Exception:
        conn.rollback()
        raise


# ---------------- 公布后的已读回执 ----------------


@app.post("/api/p/<token>/ack")
def acknowledge_receipt(token):
    """作者明确确认已读自己收到的匿名意见：公布后开放，无需请求体。

    服务端凭个人链接确定作者及对应意见；首次成功记录确认时间，重复确认仍成功
    且不改首次时间。打开页面、轮询或提问都不会自动确认，只能显式调用本接口。
    公布前一律 409；会话封存后确认（含重复确认）一律 409；无效、已退出或已换发的
    旧个人链接一律 404；失败不写任何数据。
    确认只写入回执本身：稿件、意见与一问一答均不变，提问/答复权限不受影响；
    换发链接后回执随原身份（同一参与者行）保留。
    """
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        me = find_participant(conn, token)
        if me is None:
            conn.rollback()
            return err("个人链接无效", 404)
        session = conn.execute(
            "SELECT published, closed FROM sessions WHERE id=?", (me["session_id"],)
        ).fetchone()
        # 封存后只读：即使此前从未确认，重复/首次确认也一律 409
        if session["closed"]:
            conn.rollback()
            return err("会话已封存，只读，不能再写入", 409)
        # 公布前确认一律 409，且不改变任何数据
        if not session["published"]:
            conn.rollback()
            return err("尚未公布，不能确认已读", 409)
        # 服务端按个人链接确定身份与对应意见：只能确认自己收到的那条有效意见
        review = find_received_review(conn, me)
        if review is None:
            conn.rollback()
            return err("没有可确认的意见", 409)
        receipt = find_receipt(conn, review["id"])
        if receipt is None:
            # 首次确认：记录确认时间；写事务串行化 + review_id UNIQUE 下并发确认只写一次
            conn.execute("INSERT INTO receipts (review_id) VALUES (?)", (review["id"],))
            receipt = find_receipt(conn, review["id"])
        # 重复确认仍成功：不回写，首次确认时间保持不变
        conn.commit()
        return jsonify({"ok": True, "acknowledged_at": receipt["acknowledged_at"]})
    except Exception:
        conn.rollback()
        raise


# ---------------- 公布后的意见举报 ----------------


@app.post("/api/p/<token>/report")
def report_received_review(token):
    """作者举报自己收到的意见：公布后开放，每条意见至多举报一次。"""
    data = request.get_json(silent=True) or {}
    reason = data.get("reason")
    if (
        not isinstance(reason, str)
        or not reason.strip()
        or len(reason) > MAX_REPORT_REASON_LEN
    ):
        return err(f"举报原因不能为空且不超过 {MAX_REPORT_REASON_LEN} 字")

    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        me = find_participant(conn, token)
        if me is None:
            conn.rollback()
            return err("个人链接无效", 404)
        session = conn.execute(
            "SELECT * FROM sessions WHERE id=?", (me["session_id"],)
        ).fetchone()
        # 封存后只读：举报一律 409，且不改变任何数据
        if session["closed"]:
            conn.rollback()
            return err("会话已封存，只读，不能再写入", 409)
        # 公布前举报一律 409，且不改变任何数据
        if not session["published"]:
            conn.rollback()
            return err("尚未公布，不能举报", 409)
        review = find_received_review(conn, me)
        if review is None:
            conn.rollback()
            return err("没有可举报的意见", 409)
        existing = find_report_by_review(conn, review["id"])
        if existing is not None:
            conn.rollback()
            return err("该意见已经举报，不能重复举报", 409)
        cur = conn.execute(
            "INSERT INTO reports (session_id, review_id, reason) VALUES (?,?,?)",
            (session["id"], review["id"], reason),
        )
        report_id = cur.lastrowid
        conn.commit()
        return jsonify({"ok": True, "id": report_id, "status": "pending"})
    except Exception:
        conn.rollback()
        raise


# ---------------- 主持人处理意见举报 ----------------


@app.post("/api/h/<host_token>/reports/<int:report_id>/<any(keep, block):decision>")
@app.post("/api/h/<host_token>/report/<int:report_id>/<any(keep, block):decision>")
def decide_report(host_token, report_id, decision):
    """主持人对本会话待处理举报选择保留或屏蔽，裁定一次且不可修改。"""
    new_status = "kept" if decision == "keep" else "blocked"
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        session = find_session_by_host(conn, host_token)
        if session is None:
            conn.rollback()
            return err("主持链接无效", 404)
        if session["closed"]:
            conn.rollback()
            return err("会话已封存，只读，不能再写入", 409)
        report = find_host_report(conn, session["id"], report_id)
        # 编号不存在或属于其他会话均按不存在处理，不借错误透露其他会话信息
        if report is None:
            conn.rollback()
            return err("举报不存在", 404)
        if report["status"] != "pending":
            conn.rollback()
            return err("该举报已经处理，不能重复处理或修改", 409)
        cur = conn.execute(
            "UPDATE reports SET status=?, decided_at=datetime('now')"
            " WHERE id=? AND status='pending'",
            (new_status, report_id),
        )
        # BEGIN IMMEDIATE 已串行化；条件更新再兜底，确保同一举报的并发裁定最多一次成功
        if cur.rowcount != 1:
            conn.rollback()
            return err("该举报已经处理，不能重复处理或修改", 409)
        conn.commit()
        return jsonify({"ok": True, "id": report_id, "status": new_status})
    except Exception:
        conn.rollback()
        raise


# ---------------- 匿名合辑 ----------------


@app.post("/api/p/<token>/anthology-consent")
def set_anthology_consent(token):
    """参与者授权或撤回本人稿件收入匿名合辑：公布后开放，初始未授权。

    服务端凭个人链接确定本人，请求体 {"consent": true|false}；授权绑定参与者身份，
    换发链接不改变。仅已公布且未封存时可变更，否则 409；合辑一旦发布，全部授权冻结，
    之后的变更一律 409。无效（含已退出、已换发作废的）个人链接 404；请求格式不对 400。
    授权变更与合辑发布在同一写事务机制内按提交顺序串行生效。
    """
    data = request.get_json(silent=True) or {}
    consent = data.get("consent")
    if not isinstance(consent, bool):
        return err("请明确授权（consent 为 true）或撤回（consent 为 false）")

    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        me = find_participant(conn, token)
        if me is None:
            conn.rollback()
            return err("个人链接无效", 404)
        session = conn.execute(
            "SELECT published, closed FROM sessions WHERE id=?", (me["session_id"],)
        ).fetchone()
        # 封存后只读：授权变更一律 409，且不改变任何数据
        if session["closed"]:
            conn.rollback()
            return err("会话已封存，只读，不能再写入", 409)
        # 仅已公布会话可授权/撤回：公布前一概 409
        if not session["published"]:
            conn.rollback()
            return err("尚未公布，不能授权或撤回", 409)
        # 合辑发布后全部授权冻结：发布先提交则此处必能看到，变更一律 409
        if find_anthology(conn, me["session_id"]) is not None:
            conn.rollback()
            return err("合辑已发布，授权已冻结，不能再变更", 409)
        conn.execute(
            "UPDATE participants SET anthology_consent=? WHERE id=?",
            (1 if consent else 0, me["id"]),
        )
        conn.commit()
        return jsonify({"ok": True, "consent": consent})
    except Exception:
        conn.rollback()
        raise


@app.post("/api/h/<host_token>/anthology")
def publish_anthology(host_token):
    """主持人按姓名选择本会话留存成员，发布一次匿名合辑。

    请求体 {"names": [...]}：非空、无重复，否则 400；仅已公布且未封存时可发布，
    否则 409；主持链接无效或姓名不属于本会话留存成员 404；任一入选稿未授权则整次
    发布 409、不留合辑。成功生成独立随机公开链接，入选稿文复制固化并按原名单
    （录入）顺序从 1 编号。授权变更与发布按提交顺序串行生效：入选作者先撤回则
    本发布在校验时看到撤回而失败；发布先提交则全部授权冻结，变更及重复发布 409。
    """
    data = request.get_json(silent=True) or {}
    names = data.get("names")
    if not isinstance(names, list) or not names:
        return err("请提供非空的入选名单")
    cleaned = []
    for n in names:
        if not isinstance(n, str) or not n.strip():
            return err("名单中的姓名必须是非空字符串")
        n = n.strip()
        if len(n) > MAX_NAME_LEN:
            return err(f"姓名不超过 {MAX_NAME_LEN} 字")
        cleaned.append(n)
    if len(set(cleaned)) != len(cleaned):
        return err("名单中的姓名不能重复")

    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        session = find_session_by_host(conn, host_token)
        if session is None:
            conn.rollback()
            return err("主持链接无效", 404)
        if session["closed"]:
            conn.rollback()
            return err("会话已封存，只读，不能再写入", 409)
        if not session["published"]:
            conn.rollback()
            return err("尚未公布，不能发布合辑", 409)
        # 发布一次：重复发布一律 409（与授权变更按提交顺序串行，彼此可见）
        if find_anthology(conn, session["id"]) is not None:
            conn.rollback()
            return err("已发布过合辑，不能重复发布", 409)
        selected = []
        for n in cleaned:
            p = find_participant_by_name(conn, session["id"], n)
            # 姓名不属于本会话留存成员（含已移除者）按不存在处理，不透露名单
            if p is None:
                conn.rollback()
                return err("本会话中没有该成员", 404)
            selected.append(p)
        # 任一入选稿未授权：整次发布失败，回滚不留合辑，授权状态不变
        not_consented = [p["name"] for p in selected if not p["anthology_consent"]]
        if not_consented:
            conn.rollback()
            return err(
                "有入选稿件未获授权，整次发布已取消",
                409,
                missing_consent=not_consented,
            )
        # 按原名单（录入）顺序编号；稿文复制固化，此后授权与稿件状态如何变化都与合辑无关
        selected.sort(key=lambda p: p["position"])
        public_token = new_anthology_token(conn)
        cur = conn.execute(
            "INSERT INTO anthologies (session_id, public_token) VALUES (?,?)",
            (session["id"], public_token),
        )
        anthology_id = cur.lastrowid
        for number, p in enumerate(selected, 1):
            conn.execute(
                "INSERT INTO anthology_items (anthology_id, number, manuscript)"
                " VALUES (?,?,?)",
                (anthology_id, number, p["manuscript"]),
            )
        conn.commit()
        return jsonify({"ok": True, "url": f"/a/{public_token}"})
    except Exception:
        conn.rollback()
        raise


@app.get("/api/a/<public_token>")
def anthology_public(public_token):
    """匿名合辑公开只读数据：只含编号与稿文。

    不含姓名、私密链接、意见、问答或授权记录；无效公开链接 404。
    合辑在发布时固化，会话封存后仍可只读；任何人持公开链接均可读取。
    """
    conn = get_db()
    try:
        # 只读事务：整个响应取自同一快照；WAL 下不阻塞其他连接的写入
        conn.execute("BEGIN")
        anthology = conn.execute(
            "SELECT * FROM anthologies WHERE public_token=?", (public_token,)
        ).fetchone()
        if anthology is None:
            conn.rollback()
            return err("公开链接无效", 404)
        items = conn.execute(
            "SELECT number, manuscript FROM anthology_items"
            " WHERE anthology_id=? ORDER BY number",
            (anthology["id"],),
        ).fetchall()
        conn.commit()
        return jsonify(
            {
                "items": [
                    {"number": r["number"], "manuscript": r["manuscript"]}
                    for r in items
                ]
            }
        )
    except Exception:
        conn.rollback()
        raise


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, threaded=True)
