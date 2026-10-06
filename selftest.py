"""可控交错自测：验证参与页状态读取的快照语义与事务/连接释放。

不依赖运行中的服务：直接以 Flask 测试客户端驱动 app，
数据库使用独立临时文件（导入 app 前通过 DB_PATH 指定），不影响正式数据。

运行：python selftest.py
"""

import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest

os.environ["DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="review-selftest-"), "app.db"
)

import app as app_module  # noqa: E402  必须在设置 DB_PATH 之后导入
from flask import g  # noqa: E402


# ---------------- 测试工具 ----------------


class ConnWrapper:
    """包装 sqlite3 连接：逐语句钩子（用于暂停/注入故障），并记录关闭时的事务状态。

    hook 在语句执行完成后调用；pre_hook 在执行前调用（可截住因锁竞争而阻塞、
    尚未返回的 BEGIN IMMEDIATE）。
    """

    def __init__(self, real):
        self._real = real
        self.hook = None
        self.pre_hook = None
        self.paused = False
        self.in_transaction_at_close = None

    def execute(self, sql, parameters=()):
        if self.pre_hook is not None:
            self.pre_hook(self, sql)
        cur = self._real.execute(sql, parameters)
        if self.hook is not None:
            self.hook(self, sql)
        return cur

    def commit(self):
        return self._real.commit()

    def rollback(self):
        return self._real.rollback()

    def close(self):
        self.in_transaction_at_close = self._real.in_transaction
        return self._real.close()


class use_wrapped_db:
    """临时把 app.get_db 换成返回 ConnWrapper 的版本（连接参数与 g 缓存保持一致）。"""

    def __init__(self, hook=None, pre_hook=None):
        self.hook = hook
        self.pre_hook = pre_hook
        self.wrappers = []
        self._orig = None

    def _get_db(self):
        if "db" not in g:
            conn = sqlite3.connect(app_module.DB_PATH, timeout=10)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            wrapper = ConnWrapper(conn)
            wrapper.hook = self.hook
            wrapper.pre_hook = self.pre_hook
            self.wrappers.append(wrapper)
            g.db = wrapper
        return g.db

    def __enter__(self):
        self._orig = app_module.get_db
        app_module.get_db = self._get_db
        return self

    def __exit__(self, *exc):
        app_module.get_db = self._orig
        return False


def new_client():
    return app_module.app.test_client()


def create_session(client):
    resp = client.post(
        "/api/sessions",
        json={
            "people": [
                {"name": "甲", "manuscript": "文本一"},
                {"name": "乙", "manuscript": "文本二"},
                {"name": "丙", "manuscript": "文本三"},
            ]
        },
    )
    assert resp.status_code == 200, resp.get_json()
    data = resp.get_json()
    host_token = data["host_url"].rsplit("/", 1)[-1]
    tokens = [p["url"].rsplit("/", 1)[-1] for p in data["people"]]
    return host_token, tokens


def create_named_session(client, names):
    """按给定姓名（原录入顺序）创建会话，返回 (host_token, {姓名: token})。"""
    resp = client.post(
        "/api/sessions",
        json={"people": [{"name": n, "manuscript": "稿" + n} for n in names]},
    )
    assert resp.status_code == 200, resp.get_json()
    data = resp.get_json()
    host_token = data["host_url"].rsplit("/", 1)[-1]
    tokens = {p["name"]: p["url"].rsplit("/", 1)[-1] for p in data["people"]}
    return host_token, tokens


def state(client, token):
    resp = client.get(f"/api/p/{token}")
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()


def remove_member(client, host_token, name):
    return client.post(f"/api/h/{host_token}/remove", json={"name": name})


def submit_review(client, token, content, version, credential=None):
    payload = {"content": content, "version": version}
    if credential is not None:
        payload["credential"] = credential
    return client.post(f"/api/p/{token}/review", json=payload)


def ask(client, token, question):
    return client.post(
        f"/api/p/{token}/question", json={"question": question}
    )


def answer(client, token, reply):
    return client.post(
        f"/api/p/{token}/answer", json={"answer": reply}
    )


def ack(client, token):
    return client.post(f"/api/p/{token}/ack")


def report_review(client, token, reason):
    return client.post(
        f"/api/p/{token}/report", json={"reason": reason}
    )


def decide_report(client, host_token, report_id, decision):
    return client.post(
        f"/api/h/{host_token}/reports/{report_id}/{decision}"
    )


def close_session(client, host_token):
    return client.post(f"/api/h/{host_token}/close")


def set_consent(client, token, consent):
    return client.post(
        f"/api/p/{token}/anthology-consent", json={"consent": consent}
    )


def publish_anthology(client, host_token, names):
    return client.post(f"/api/h/{host_token}/anthology", json={"names": names})


def host_state(client, host_token):
    resp = client.get(f"/api/h/{host_token}")
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()


def publish_with_reviews(client, host_token, tokens, reviews=("甲评乙", "乙评丙", "丙评甲")):
    """三人首轮意见全部提交并一次公布，返回各 token。"""
    for tok, content in zip(tokens, reviews):
        resp = submit_review(client, tok, content, 1)
        assert resp.status_code == 200, resp.get_json()
    resp = client.post(f"/api/h/{host_token}/publish")
    assert resp.status_code == 200, resp.get_json()


# ---------------- 用例 ----------------


class SnapshotReadTest(unittest.TestCase):
    """核心场景：读取暂停期间另一连接完成 改稿→提交新版意见→公布。"""

    def setUp(self):
        self.client = new_client()

    def test_read_snapshot_consistent_under_concurrent_writes(self):
        host_token, tokens = create_session(self.client)
        # 首轮：三人都对 v1 稿件提交了有效意见
        for tok, content in zip(tokens, ["甲评乙", "乙评丙", "丙评甲"]):
            resp = submit_review(self.client, tok, content, 1)
            self.assertEqual(resp.status_code, 200, resp.get_json())

        read_started = threading.Event()
        writes_done = threading.Event()
        armed = {"on": True}

        def pause_hook(wrapper, sql):
            s = sql.lstrip().upper()
            # 读出本人稿件（第一条 SELECT）后暂停，模拟"读出旧稿后"的交错点
            if armed["on"] and s.startswith("SELECT") and "FROM PARTICIPANTS" in s:
                armed["on"] = False
                wrapper.paused = True
                read_started.set()
                self.assertTrue(writes_done.wait(10), "写入侧未及时完成")

        result = {}

        def reader():
            result["resp"] = new_client().get(f"/api/p/{tokens[0]}")

        with use_wrapped_db(pause_hook) as db:
            t = threading.Thread(target=reader)
            t.start()
            self.assertTrue(read_started.wait(10), "读取未在第一条查询后暂停")

            # 读取暂停期间，另一连接依次完成合法写入（WAL 下不被读事务阻塞）
            resp = self.client.put(
                f"/api/p/{tokens[0]}/manuscript", json={"manuscript": "文本一（改）"}
            )
            self.assertEqual(resp.status_code, 200, resp.get_json())
            self.assertEqual(resp.get_json()["version"], 2)
            resp = submit_review(self.client, tokens[2], "丙评甲（新版）", 2)
            self.assertEqual(resp.status_code, 200, resp.get_json())
            resp = self.client.post(f"/api/h/{host_token}/publish")
            self.assertEqual(resp.status_code, 200, resp.get_json())

            writes_done.set()
            t.join(10)
            self.assertFalse(t.is_alive(), "读取线程未结束")

        reader_conn = next(w for w in db.wrappers if w.paused)

        resp = result["resp"]
        self.assertEqual(resp.status_code, 200, resp.get_json())
        data = resp.get_json()
        # 关键断言：允许完整返回公布前状态；不得拼出
        # published=true + 旧 version + received=null 的混合快照
        self.assertEqual(
            (data["published"], data["version"], data["received"]),
            (False, 1, None),
        )
        self.assertEqual(data["manuscript"], "文本一")
        self.assertEqual(data["assignment"]["manuscript"], "文本二")
        self.assertEqual(data["assignment"]["version"], 1)
        self.assertEqual(data["my_review"]["content"], "甲评乙")
        self.assertTrue(data["my_review"]["valid"])
        # 读取事务已结束、连接已释放
        self.assertIs(reader_conn.in_transaction_at_close, False)

        # 交错结束后再读：完整落在公布后快照，received 为当前稿件的有效意见
        data = self.client.get(f"/api/p/{tokens[0]}").get_json()
        self.assertEqual((data["published"], data["version"]), (True, 2))
        self.assertEqual(data["manuscript"], "文本一（改）")
        self.assertEqual(data["received"], "丙评甲（新版）")
        self.assertTrue(data["my_review"]["valid"])

    def test_journal_mode_is_wal(self):
        conn = sqlite3.connect(app_module.DB_PATH)
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(mode.lower(), "wal")

    def test_success_ends_transaction(self):
        _, tokens = create_session(self.client)
        with use_wrapped_db() as db:
            resp = self.client.get(f"/api/p/{tokens[0]}")
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertEqual(len(db.wrappers), 1)
        self.assertIs(db.wrappers[0].in_transaction_at_close, False)

    def test_invalid_token_ends_transaction(self):
        with use_wrapped_db() as db:
            resp = self.client.get("/api/p/不存在的链接")
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json(), {"error": "个人链接无效"})
        self.assertEqual(len(db.wrappers), 1)
        self.assertIs(db.wrappers[0].in_transaction_at_close, False)

    def test_read_error_ends_transaction(self):
        _, tokens = create_session(self.client)

        def boom(_wrapper, sql):
            if "FROM SESSIONS" in sql.lstrip().upper():
                raise RuntimeError("注入的读取故障")

        logger = logging.getLogger(app_module.app.name)
        old_level = logger.level
        logger.setLevel(logging.CRITICAL)  # 预期的 500，静默堆栈
        try:
            with use_wrapped_db(boom) as db:
                resp = self.client.get(f"/api/p/{tokens[0]}")
        finally:
            logger.setLevel(old_level)
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.get_json(), {"error": "服务器内部错误"})
        self.assertEqual(len(db.wrappers), 1)
        self.assertIs(db.wrappers[0].in_transaction_at_close, False)


class ExistingRulesTest(unittest.TestCase):
    """回归：匿名隔离、改稿失效、过期拒绝、公布门槛与公布冻结等既有规则不变。"""

    def setUp(self):
        self.client = new_client()

    def test_rules_unchanged(self):
        host_token, tokens = create_session(self.client)
        t_jia, t_yi, t_bing = tokens

        # 匿名隔离：响应不含他人姓名与任何链接字段；公布前不含收到的意见
        resp = self.client.get(f"/api/p/{t_jia}")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertNotIn("乙", body)
        self.assertNotIn("丙", body)
        self.assertNotIn("token", body)
        self.assertNotIn("url", body)
        self.assertIsNone(resp.get_json()["received"])

        # 改稿失效：乙改稿后，甲针对 v1 的意见失效
        self.assertEqual(submit_review(self.client, t_jia, "甲评乙", 1).status_code, 200)
        resp = self.client.put(
            f"/api/p/{t_yi}/manuscript", json={"manuscript": "文本二（改）"}
        )
        self.assertEqual(resp.status_code, 200)

        # 过期拒绝：绑定旧版本提交被 409 拒绝，且不覆盖已有意见
        resp = submit_review(self.client, t_jia, "甲评乙（过期）", 1)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json()["current_version"], 2)
        data = self.client.get(f"/api/p/{t_jia}").get_json()
        self.assertEqual(data["my_review"]["content"], "甲评乙")
        self.assertFalse(data["my_review"]["valid"])
        # 绑定新版本后提交成功
        self.assertEqual(
            submit_review(self.client, t_jia, "甲评乙（新版）", 2).status_code, 200
        )

        # 公布门槛：仍有稿件缺有效意见时 409
        resp = self.client.post(f"/api/h/{host_token}/publish")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(set(resp.get_json()["missing"]), {"甲", "丙"})
        self.assertEqual(submit_review(self.client, t_yi, "乙评丙", 1).status_code, 200)
        self.assertEqual(submit_review(self.client, t_bing, "丙评甲", 1).status_code, 200)
        resp = self.client.post(f"/api/h/{host_token}/publish")
        self.assertEqual(resp.status_code, 200)

        # 公布冻结：重复公布、改稿、提交意见均 409
        self.assertEqual(
            self.client.post(f"/api/h/{host_token}/publish").status_code, 409
        )
        self.assertEqual(
            self.client.put(
                f"/api/p/{t_jia}/manuscript", json={"manuscript": "冻结后改稿"}
            ).status_code,
            409,
        )
        self.assertEqual(submit_review(self.client, t_jia, "冻结后提交", 2).status_code, 409)

        # 公布后：作者看到针对当前版本的有效意见
        data = self.client.get(f"/api/p/{t_jia}").get_json()
        self.assertTrue(data["published"])
        self.assertEqual(data["received"], "丙评甲")

        # 无效链接 404
        self.assertEqual(self.client.get("/api/p/无效").status_code, 404)
        self.assertEqual(self.client.get("/api/h/无效").status_code, 404)


class ClarificationTest(unittest.TestCase):
    """公布后一问一答：409 规则、身份隔离、文本限制、快照读取。"""

    def setUp(self):
        self.client = new_client()
        self.host_token, self.tokens = create_session(self.client)
        self.t_jia, self.t_yi, self.t_bing = self.tokens
        publish_with_reviews(self.client, self.host_token, self.tokens)

    def test_409_rules_and_full_flow(self):
        # 公布前不能提问/答复（在另一个新会话上验证，本会话已公布）
        c2 = new_client()
        _, t2 = create_session(c2)
        self.assertEqual(ask(c2, t2[0], "公布前提问").status_code, 409)
        self.assertEqual(answer(c2, t2[0], "公布前答复").status_code, 409)

        # 甲收到的是丙的意见；尚无问题时答复 409
        self.assertEqual(answer(self.client, self.t_bing, "抢答").status_code, 409)

        # 甲提问成功
        resp = ask(self.client, self.t_jia, "请澄清Q1")
        self.assertEqual(resp.status_code, 200, resp.get_json())

        # 重复提问 409，问题不变
        resp = ask(self.client, self.t_jia, "再问一次")
        self.assertEqual(resp.status_code, 409)
        data = self.client.get(f"/api/p/{self.t_jia}").get_json()
        self.assertEqual(data["received_clarification"]["question"], "请澄清Q1")
        self.assertFalse(data["received_clarification"]["answered"])
        self.assertIsNone(data["received_clarification"]["answer"])

        # 身份隔离：乙不是该意见的评审者（评审者是丙），不能答复甲的问题；
        # 乙就"自己收到的"意见另开一问是合法的，不影响甲这条一问一答
        self.assertEqual(ask(self.client, self.t_yi, "乙对自己收到的意见提问").status_code, 200)
        self.assertEqual(answer(self.client, self.t_yi, "乙来抢答甲的问题").status_code, 409)

        # 评审者丙答复成功
        resp = answer(self.client, self.t_bing, "答复A1")
        self.assertEqual(resp.status_code, 200, resp.get_json())

        # 重复答复 409，答复不变
        resp = answer(self.client, self.t_bing, "再答一次")
        self.assertEqual(resp.status_code, 409)

        # 双方各自参与页读到同一条问答
        da = self.client.get(f"/api/p/{self.t_jia}").get_json()
        db = self.client.get(f"/api/p/{self.t_bing}").get_json()
        self.assertEqual(
            da["received_clarification"],
            {"question": "请澄清Q1", "answer": "答复A1", "answered": True},
        )
        self.assertEqual(
            db["my_review_clarification"],
            {"question": "请澄清Q1", "answer": "答复A1", "answered": True},
        )

        # 回答后作者侧仍不能再次提问
        self.assertEqual(ask(self.client, self.t_jia, "第二个问题").status_code, 409)

    def test_visibility_isolation(self):
        # 提问前：所有参与页两个问答字段为 None；主持台完全不含问答
        for tok in self.tokens:
            data = self.client.get(f"/api/p/{tok}").get_json()
            self.assertIsNone(data["received_clarification"])
            self.assertIsNone(data["my_review_clarification"])
        host_body = self.client.get(f"/api/h/{self.host_token}").get_data(as_text=True)
        self.assertNotIn("clarification", host_body)
        self.assertNotIn("question", host_body)
        self.assertNotIn("answer", host_body)

        ask(self.client, self.t_jia, "甲的问题正文XYZ")
        answer(self.client, self.t_bing, "丙的答复正文UVW")

        # 甲（作者）：收到问答；自己没提问过别人（甲评乙，乙未问），评审视角为 None
        da = self.client.get(f"/api/p/{self.t_jia}").get_json()
        self.assertIsNone(da["my_review_clarification"])
        self.assertEqual(da["received_clarification"]["question"], "甲的问题正文XYZ")

        # 丙（评审者）：评审视角有问答；丙收到的是乙的意见，乙未提问，作者视角为 None
        db = self.client.get(f"/api/p/{self.t_bing}").get_json()
        self.assertIsNone(db["received_clarification"])
        self.assertEqual(db["my_review_clarification"]["answer"], "丙的答复正文UVW")

        # 乙是无关第三方：两侧都为 None，且响应体不含问答正文
        body = self.client.get(f"/api/p/{self.t_yi}").get_data(as_text=True)
        self.assertNotIn("正文XYZ", body)
        self.assertNotIn("正文UVW", body)

        # 主持状态始终不含问答内容
        host_body = self.client.get(f"/api/h/{self.host_token}").get_data(as_text=True)
        self.assertNotIn("正文XYZ", host_body)
        self.assertNotIn("正文UVW", host_body)

        # 匿名：响应不含对方姓名与任何私密链接
        self.assertNotIn(self.t_bing, self.client.get(f"/api/p/{self.t_jia}").get_data(as_text=True))
        self.assertNotIn("甲", self.client.get(f"/api/p/{self.t_bing}").get_data(as_text=True))

    def test_text_limits(self):
        # 空白与超长均 400（沿用意见的非空及长度限制）
        self.assertEqual(ask(self.client, self.t_jia, "   ").status_code, 400)
        self.assertEqual(ask(self.client, self.t_jia, "x" * 100001).status_code, 400)
        ask(self.client, self.t_jia, "Q")
        self.assertEqual(answer(self.client, self.t_bing, "").status_code, 400)
        self.assertEqual(answer(self.client, self.t_bing, "y" * 100001).status_code, 400)
        # 被拒绝后正常长度仍可走通（失败不改变数据）
        self.assertEqual(answer(self.client, self.t_bing, "A").status_code, 200)

    def test_failed_attempts_change_nothing(self):
        # 各种针对甲这条意见的非法尝试后：该意见无问答行产生，原稿与原意见冻结不变
        answer(self.client, self.t_bing, "提前答复")        # 尚无问题
        answer(self.client, self.t_yi, "冒名答复")          # 乙不是该意见评审者
        ask(self.client, self.t_jia, " ")                  # 空白文本
        conn = sqlite3.connect(app_module.DB_PATH)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM clarifications c"
                " JOIN reviews r ON c.review_id = r.id"
                " JOIN participants p ON r.target_id = p.id WHERE p.token=?",
                (self.t_jia,),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 0)
        data = self.client.get(f"/api/p/{self.t_jia}").get_json()
        self.assertEqual(data["manuscript"], "文本一")
        self.assertEqual(data["received"], "丙评甲")
        self.assertEqual(data["version"], 1)

    def test_concurrent_question_only_one_wins(self):
        results = []

        def worker():
            c = new_client()
            results.append(ask(c, self.t_jia, "并发问题").status_code)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
            self.assertFalse(t.is_alive())
        self.assertEqual(sorted(results), [200, 409, 409, 409, 409])
        conn = sqlite3.connect(app_module.DB_PATH)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM clarifications c"
                " JOIN reviews r ON c.review_id = r.id"
                " JOIN participants p ON r.target_id = p.id WHERE p.token=?",
                (self.t_jia,),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 1)

    def test_concurrent_answer_only_one_wins(self):
        self.assertEqual(ask(self.client, self.t_jia, "问题").status_code, 200)
        results = []

        def worker():
            c = new_client()
            results.append(answer(c, self.t_bing, "并发答复").status_code)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
            self.assertFalse(t.is_alive())
        self.assertEqual(sorted(results), [200, 409, 409, 409, 409])
        conn = sqlite3.connect(app_module.DB_PATH)
        try:
            row = conn.execute(
                "SELECT c.question, c.answer FROM clarifications c"
                " JOIN reviews r ON c.review_id = r.id"
                " JOIN participants p ON r.target_id = p.id WHERE p.token=?",
                (self.t_jia,),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row, ("问题", "并发答复"))

    def test_invalid_token_404(self):
        self.assertEqual(ask(new_client(), "无效链接", "Q").status_code, 404)
        self.assertEqual(answer(new_client(), "无效链接", "A").status_code, 404)

    def test_qa_in_same_snapshot(self):
        ask(self.client, self.t_jia, "快照问题")
        answer(self.client, self.t_bing, "快照答复")
        # 问答随参与状态在同一响应中读取，且与公布状态、稿件版本一致
        data = self.client.get(f"/api/p/{self.t_jia}").get_json()
        self.assertTrue(data["published"])
        self.assertEqual(data["version"], 1)
        self.assertEqual(data["received"], "丙评甲")
        self.assertEqual(data["received_clarification"]["question"], "快照问题")
        self.assertEqual(data["received_clarification"]["answer"], "快照答复")
        # 原稿与原意见仍冻结：公布后写操作继续 409
        self.assertEqual(
            self.client.put(
                f"/api/p/{self.t_jia}/manuscript", json={"manuscript": "x"}
            ).status_code,
            409,
        )
        self.assertEqual(submit_review(self.client, self.t_jia, "x", 1).status_code, 409)


class ReceiptTest(unittest.TestCase):
    """公布后已读回执：404/409 规则、首次时间不改、身份隔离、换发保留、快照与持久化。"""

    def setUp(self):
        self.client = new_client()
        self.host_token, self.tokens = create_session(self.client)
        self.t_jia, self.t_yi, self.t_bing = self.tokens

    def receipts_count(self):
        """当前会话（甲所在会话）的回执总数。"""
        conn = sqlite3.connect(app_module.DB_PATH)
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM receipts WHERE review_id IN"
                " (SELECT id FROM reviews WHERE session_id="
                "   (SELECT session_id FROM participants WHERE token=?))",
                (self.t_jia,),
            ).fetchone()[0]
        finally:
            conn.close()

    def host_reviewers(self):
        resp = self.client.get(f"/api/h/{self.host_token}")
        assert resp.status_code == 200, resp.get_json()
        return resp.get_json()["reviewers"]

    def test_pre_publish_409_invalid_link_404_and_no_writes(self):
        # 公布前确认 409；无效链接 404；均不写数据
        self.assertEqual(ack(self.client, self.t_jia).status_code, 409)
        self.assertEqual(ack(new_client(), "无效链接").status_code, 404)
        self.assertEqual(self.receipts_count(), 0)
        # 公布前参与状态不含回执；主持台回执字段为待确认
        self.assertIsNone(state(self.client, self.t_jia)["receipt"])
        for r in self.host_reviewers():
            self.assertFalse(r["acknowledged"])
            self.assertIsNone(r["acknowledged_at"])

        # 公布前换发链接后：旧链接确认 404，新链接仍 409（尚未公布）
        resp = self.client.post(
            f"/api/h/{self.host_token}/rotate-link",
            json={"name": "甲", "link": f"/p/{self.t_jia}"},
        )
        self.assertEqual(resp.status_code, 200, resp.get_json())
        new_jia = resp.get_json()["url"].rsplit("/", 1)[-1]
        self.assertEqual(ack(self.client, self.t_jia).status_code, 404)
        self.assertEqual(ack(self.client, new_jia).status_code, 409)
        self.assertEqual(self.receipts_count(), 0)

    def test_first_confirm_time_kept_and_host_order(self):
        publish_with_reviews(self.client, self.host_token, self.tokens)

        # 初始均为待确认；打开页面/轮询/提问都不自动确认
        for tok in self.tokens:
            self.assertEqual(
                state(self.client, tok)["receipt"],
                {"acknowledged": False, "acknowledged_at": None},
            )
        state(self.client, self.t_jia)
        state(self.client, self.t_jia)
        self.assertEqual(ask(self.client, self.t_jia, "确认前的问题").status_code, 200)
        self.assertFalse(state(self.client, self.t_jia)["receipt"]["acknowledged"])
        self.assertEqual(self.receipts_count(), 0)

        # 主持台：按留存名单原顺序显示待确认，首次确认时间为空
        reviewers = self.host_reviewers()
        self.assertEqual([r["name"] for r in reviewers], ["甲", "乙", "丙"])
        for r in reviewers:
            self.assertFalse(r["acknowledged"])
            self.assertIsNone(r["acknowledged_at"])

        # 首次确认成功，响应携带首次确认时间
        resp = ack(self.client, self.t_jia)
        self.assertEqual(resp.status_code, 200, resp.get_json())
        first_at = resp.get_json()["acknowledged_at"]
        self.assertTrue(first_at)
        self.assertEqual(
            state(self.client, self.t_jia)["receipt"],
            {"acknowledged": True, "acknowledged_at": first_at},
        )

        # 把首次时间改到一个可识别的旧值：重复确认仍成功且不改首次时间
        conn = sqlite3.connect(app_module.DB_PATH)
        try:
            conn.execute("UPDATE receipts SET acknowledged_at='2000-01-01 00:00:00'")
            conn.commit()
        finally:
            conn.close()
        resp = ack(self.client, self.t_jia)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["acknowledged_at"], "2000-01-01 00:00:00")
        self.assertEqual(
            state(self.client, self.t_jia)["receipt"]["acknowledged_at"],
            "2000-01-01 00:00:00",
        )

        # 主持台：甲已确认（首次时间），乙、丙仍待确认；顺序不变，且不含意见或问答内容
        reviewers = self.host_reviewers()
        self.assertEqual([r["name"] for r in reviewers], ["甲", "乙", "丙"])
        self.assertTrue(reviewers[0]["acknowledged"])
        self.assertEqual(reviewers[0]["acknowledged_at"], "2000-01-01 00:00:00")
        self.assertFalse(reviewers[1]["acknowledged"])
        self.assertFalse(reviewers[2]["acknowledged"])
        body = self.client.get(f"/api/h/{self.host_token}").get_data(as_text=True)
        self.assertNotIn("甲评乙", body)
        self.assertNotIn("确认前的问题", body)

        # 确认不改变问答，也不影响答复权限：评审者仍可答复
        self.assertEqual(
            answer(self.client, self.t_bing, "确认后的答复").status_code, 200
        )
        s = state(self.client, self.t_jia)
        self.assertEqual(s["received_clarification"]["answer"], "确认后的答复")
        self.assertTrue(s["receipt"]["acknowledged"])

    def test_receipt_isolation_between_participants(self):
        publish_with_reviews(self.client, self.host_token, self.tokens)
        jia_at = ack(self.client, self.t_jia).get_json()["acknowledged_at"]

        # 乙只能拿到自己的回执（仍待确认），响应中不含甲的确认时间
        self.assertEqual(
            state(self.client, self.t_yi)["receipt"],
            {"acknowledged": False, "acknowledged_at": None},
        )
        body = self.client.get(f"/api/p/{self.t_yi}").get_data(as_text=True)
        self.assertNotIn(jia_at, body)

        # 乙确认的是自己的回执；甲的首次时间不受影响
        self.assertEqual(ack(self.client, self.t_yi).status_code, 200)
        self.assertEqual(
            state(self.client, self.t_jia)["receipt"]["acknowledged_at"], jia_at
        )
        self.assertEqual(
            [r["acknowledged"] for r in self.host_reviewers()],
            [True, True, False],
        )

    def test_ack_keeps_freeze_and_data_unchanged(self):
        publish_with_reviews(self.client, self.host_token, self.tokens)
        before = state(self.client, self.t_jia)
        self.assertEqual(ack(self.client, self.t_jia).status_code, 200)
        after = state(self.client, self.t_jia)
        # 除回执外，状态字段逐项不变：确认不改变稿件、意见及问答
        for key in after:
            if key == "receipt":
                continue
            self.assertEqual(after[key], before[key], key)
        # 公布冻结不变：改稿/提意见仍 409；提问/答复权限不受确认影响
        self.assertEqual(
            self.client.put(
                f"/api/p/{self.t_jia}/manuscript", json={"manuscript": "x"}
            ).status_code,
            409,
        )
        self.assertEqual(submit_review(self.client, self.t_jia, "x", 1).status_code, 409)
        self.assertEqual(ask(self.client, self.t_jia, "确认后仍可提问").status_code, 200)
        self.assertEqual(
            answer(self.client, self.t_bing, "确认后仍可答复").status_code, 200
        )

    def test_rotate_link_preserves_receipt(self):
        publish_with_reviews(self.client, self.host_token, self.tokens)
        first_at = ack(self.client, self.t_jia).get_json()["acknowledged_at"]

        # 换发链接：回执随原身份保留；旧链接确认 404，新链接沿用同一回执
        resp = self.client.post(
            f"/api/h/{self.host_token}/rotate-link",
            json={"name": "甲", "link": f"/p/{self.t_jia}"},
        )
        self.assertEqual(resp.status_code, 200, resp.get_json())
        new_jia = resp.get_json()["url"].rsplit("/", 1)[-1]
        self.assertEqual(ack(self.client, self.t_jia).status_code, 404)
        self.assertEqual(
            state(self.client, new_jia)["receipt"],
            {"acknowledged": True, "acknowledged_at": first_at},
        )
        # 新链接重复确认仍成功且不改首次时间
        resp = ack(self.client, new_jia)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["acknowledged_at"], first_at)
        # 主持台看到甲已确认，其他人不受影响
        self.assertEqual(
            [r["acknowledged"] for r in self.host_reviewers()],
            [True, False, False],
        )

    def test_removed_member_link_404(self):
        host, tokens = create_named_session(new_client(), ["甲", "乙", "丙", "丁"])
        client = new_client()
        for n in ["甲", "乙", "丙", "丁"]:
            s = state(client, tokens[n])
            self.assertEqual(
                submit_review(
                    client, tokens[n], n + "评", s["assignment"]["version"]
                ).status_code,
                200,
            )
        # 移除乙：其链接确认 404；甲改评丙，持新凭据重交后才能公布
        self.assertEqual(remove_member(client, host, "乙").status_code, 200)
        self.assertEqual(ack(client, tokens["乙"]).status_code, 404)
        cred = state(client, tokens["甲"])["assignment"]["credential"]
        self.assertEqual(
            submit_review(client, tokens["甲"], "甲评丙", 1, credential=cred).status_code,
            200,
        )
        self.assertEqual(client.post(f"/api/h/{host}/publish").status_code, 200)
        # 公布后退出者链接仍 404；主持台留存名单按原顺序且不含乙
        self.assertEqual(ack(client, tokens["乙"]).status_code, 404)
        resp = client.get(f"/api/h/{host}")
        self.assertEqual(
            [r["name"] for r in resp.get_json()["reviewers"]], ["甲", "丙", "丁"]
        )
        # 留存者确认正常
        self.assertEqual(ack(client, tokens["丙"]).status_code, 200)
        self.assertEqual(ack(client, tokens["乙"]).status_code, 404)

    def test_concurrent_ack_all_succeed_single_row(self):
        publish_with_reviews(self.client, self.host_token, self.tokens)
        results = []

        def worker():
            results.append(ack(new_client(), self.t_jia).status_code)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
            self.assertFalse(t.is_alive())
        # 并发/重复确认都成功，且只有一条回执
        self.assertEqual(sorted(results), [200] * 5)
        self.assertEqual(self.receipts_count(), 1)
        self.assertTrue(state(self.client, self.t_jia)["receipt"]["acknowledged"])

    def test_same_snapshot_and_restart_persistence(self):
        publish_with_reviews(self.client, self.host_token, self.tokens)
        first_at = ack(self.client, self.t_jia).get_json()["acknowledged_at"]
        # 回执与原状态字段同一快照返回
        s = state(self.client, self.t_jia)
        self.assertTrue(s["published"])
        self.assertEqual(s["received"], "丙评甲")
        self.assertEqual(s["receipt"]["acknowledged_at"], first_at)
        # 模拟重启：回执保留，未确认者仍待确认
        app_module.init_db()
        self.assertEqual(
            state(self.client, self.t_jia)["receipt"],
            {"acknowledged": True, "acknowledged_at": first_at},
        )
        self.assertFalse(state(self.client, self.t_yi)["receipt"]["acknowledged"])
        self.assertEqual(self.host_reviewers()[0]["acknowledged_at"], first_at)


class RotateLinkTest(unittest.TestCase):
    """个人链接换发：404/409/400 规则、旧链接全接口失效、数据沿用、并发与提交顺序、持久化。"""

    def setUp(self):
        self.client = new_client()
        self.host_token, self.tokens = create_session(self.client)
        self.t_jia, self.t_yi, self.t_bing = self.tokens

    def rotate(self, client, name, link, host_token=None):
        return client.post(
            f"/api/h/{host_token or self.host_token}/rotate-link",
            json={"name": name, "link": link},
        )

    def host_links(self, client=None):
        resp = (client or self.client).get(f"/api/h/{self.host_token}")
        assert resp.status_code == 200, resp.get_json()
        return {l["name"]: l["url"].rsplit("/", 1)[-1] for l in resp.get_json()["links"]}

    def test_rotate_before_publish_keeps_everything_and_old_link_dies(self):
        # 公布前：先有一轮意见，乙再改稿，甲改投新版意见
        self.assertEqual(submit_review(self.client, self.t_jia, "甲评乙", 1).status_code, 200)
        self.assertEqual(
            self.client.put(
                f"/api/p/{self.t_yi}/manuscript", json={"manuscript": "文本二（改）"}
            ).status_code,
            200,
        )
        self.assertEqual(
            submit_review(self.client, self.t_jia, "甲评乙（新版）", 2).status_code, 200
        )
        self.assertEqual(submit_review(self.client, self.t_yi, "乙评丙", 1).status_code, 200)
        self.assertEqual(submit_review(self.client, self.t_bing, "丙评甲", 1).status_code, 200)

        old_jia = self.t_jia
        resp = self.rotate(self.client, "甲", f"/p/{old_jia}")
        self.assertEqual(resp.status_code, 200, resp.get_json())
        new_jia = resp.get_json()["url"].rsplit("/", 1)[-1]
        self.assertNotEqual(new_jia, old_jia)
        # 成功响应只含姓名与新链接
        self.assertEqual(set(resp.get_json().keys()), {"name", "url"})

        # 主持台立即展示新链接；其他人链接不变
        links = self.host_links()
        self.assertEqual(links["甲"], new_jia)
        self.assertEqual(links["乙"], self.t_yi)
        self.assertEqual(links["丙"], self.t_bing)

        # 旧链接：状态读取与各类写入全部 404，响应不透露新链接
        old_resp = self.client.get(f"/api/p/{old_jia}")
        self.assertEqual(old_resp.status_code, 404)
        self.assertNotIn(new_jia, old_resp.get_data(as_text=True))
        self.assertEqual(
            self.client.put(
                f"/api/p/{old_jia}/manuscript", json={"manuscript": "偷改"}
            ).status_code,
            404,
        )
        self.assertEqual(
            submit_review(self.client, old_jia, "旧链接意见", 2).status_code, 404
        )
        # 公布前提问/答复本是 409，但链接失效时一律 404
        self.assertEqual(ask(self.client, old_jia, "旧链接提问").status_code, 404)
        self.assertEqual(answer(self.client, old_jia, "旧链接答复").status_code, 404)

        # 新链接沿用同一参与者：姓名、稿件版本、分配、意见均原样
        data = self.client.get(f"/api/p/{new_jia}").get_json()
        self.assertEqual(data["name"], "甲")
        self.assertFalse(data["published"])
        self.assertEqual(data["manuscript"], "文本一")
        self.assertEqual(data["version"], 1)
        self.assertEqual(data["assignment"]["manuscript"], "文本二（改）")
        self.assertEqual(data["assignment"]["version"], 2)
        self.assertEqual(data["my_review"]["content"], "甲评乙（新版）")
        self.assertTrue(data["my_review"]["valid"])

        # 参与者行未重建：id 不变，意见未失效；新链接可正常继续业务并完成公布
        self.assertEqual(
            self.client.put(
                f"/api/p/{new_jia}/manuscript", json={"manuscript": "文本一（甲再改）"}
            ).status_code,
            200,
        )
        # 丙针对甲的意见因甲改稿失效，丙用新链接读到的是乙稿……此处由丙改投甲 v2
        self.assertEqual(
            submit_review(self.client, self.t_bing, "丙评甲（新版）", 2).status_code, 200
        )
        self.assertEqual(
            self.client.post(f"/api/h/{self.host_token}/publish").status_code, 200
        )
        data = self.client.get(f"/api/p/{new_jia}").get_json()
        self.assertTrue(data["published"])
        self.assertEqual(data["manuscript"], "文本一（甲再改）")
        self.assertEqual(data["version"], 2)
        self.assertEqual(data["received"], "丙评甲（新版）")
        # 旧链接在公布后依旧 404
        self.assertEqual(self.client.get(f"/api/p/{old_jia}").status_code, 404)

    def test_rotate_after_publish_keeps_reviews_and_qa(self):
        publish_with_reviews(self.client, self.host_token, self.tokens)
        ask(self.client, self.t_jia, "甲的澄清")
        answer(self.client, self.t_bing, "丙的答复")

        old_jia = self.t_jia
        resp = self.rotate(self.client, "甲", f"http://localhost:8000/p/{old_jia}")
        self.assertEqual(resp.status_code, 200, resp.get_json())
        new_jia = resp.get_json()["url"].rsplit("/", 1)[-1]

        # 新链接：公布状态、收到的匿名意见、一问一答全部沿用
        data = self.client.get(f"/api/p/{new_jia}").get_json()
        self.assertTrue(data["published"])
        self.assertEqual(data["received"], "丙评甲")
        self.assertEqual(
            data["received_clarification"],
            {"question": "甲的澄清", "answer": "丙的答复", "answered": True},
        )
        # 评审者丙的视角问答不随甲换链接而变化
        data = self.client.get(f"/api/p/{self.t_bing}").get_json()
        self.assertEqual(data["my_review_clarification"]["question"], "甲的澄清")

        # 公布后冻结规则保留：新链接改稿/提意见仍 409；一问一答已提交不可再改
        self.assertEqual(
            self.client.put(
                f"/api/p/{new_jia}/manuscript", json={"manuscript": "x"}
            ).status_code,
            409,
        )
        self.assertEqual(submit_review(self.client, new_jia, "x", 1).status_code, 409)
        self.assertEqual(ask(self.client, new_jia, "再问").status_code, 409)
        # 旧链接仍是 404（而不是公布冻结的 409）
        self.assertEqual(
            self.client.put(
                f"/api/p/{old_jia}/manuscript", json={"manuscript": "x"}
            ).status_code,
            404,
        )

    def test_404_invalid_host_or_unknown_name_changes_nothing(self):
        # 无效主持链接：404
        resp = self.rotate(self.client, "甲", f"/p/{self.t_jia}", host_token="假主持")
        self.assertEqual(resp.status_code, 404)
        # 姓名不属于本会话：404，且不透露名单
        resp = self.rotate(self.client, "不存在的人", f"/p/{self.t_jia}")
        self.assertEqual(resp.status_code, 404)
        self.assertNotIn("url", resp.get_json())
        # 失败不改数据：链接仍是旧链接
        self.assertEqual(self.host_links()["甲"], self.t_jia)
        data = self.client.get(f"/api/p/{self.t_jia}").get_json()
        self.assertEqual(data["name"], "甲")

    def test_409_expired_or_mismatched_seen_link(self):
        old_jia = self.t_jia
        resp = self.rotate(self.client, "甲", f"/p/{old_jia}")
        self.assertEqual(resp.status_code, 200)
        new_jia = resp.get_json()["url"].rsplit("/", 1)[-1]

        # 再拿已过期的旧所见链接换发：409，不返回新链接，新链接不变
        resp = self.rotate(self.client, "甲", f"/p/{old_jia}")
        self.assertEqual(resp.status_code, 409)
        self.assertNotIn("url", resp.get_json())
        self.assertNotIn(new_jia, resp.get_data(as_text=True))
        self.assertEqual(self.host_links()["甲"], new_jia)

        # 伪造链接 / 他人当前链接：均 409
        self.assertEqual(self.rotate(self.client, "甲", "/p/根本不存在").status_code, 409)
        self.assertEqual(
            self.rotate(self.client, "甲", f"/p/{self.t_yi}").status_code, 409
        )
        self.assertEqual(self.host_links()["甲"], new_jia)

        # 用当前链接可再次换发成功（连环换发）；上一次的新链接也进入作废历史
        resp = self.rotate(self.client, "甲", f"/p/{new_jia}")
        self.assertEqual(resp.status_code, 200)
        newest = resp.get_json()["url"].rsplit("/", 1)[-1]
        self.assertEqual(self.host_links()["甲"], newest)
        self.assertEqual(self.client.get(f"/api/p/{new_jia}").status_code, 404)
        self.assertEqual(self.client.get(f"/api/p/{old_jia}").status_code, 404)
        self.assertEqual(self.client.get(f"/api/p/{newest}").status_code, 200)

    def test_400_malformed_request(self):
        for payload in [
            {},
            {"name": "甲"},
            {"link": f"/p/{self.t_jia}"},
            {"name": "  ", "link": f"/p/{self.t_jia}"},
            {"name": "甲", "link": "不是链接"},
            {"name": "甲", "link": "/h/xxx"},
            {"name": "甲", "link": "http://localhost:8000/api/p/xxx"},
        ]:
            resp = self.client.post(
                f"/api/h/{self.host_token}/rotate-link", json=payload
            )
            self.assertEqual(resp.status_code, 400, payload)
        # 全部失败后链接不变
        self.assertEqual(self.host_links()["甲"], self.t_jia)

    def test_concurrent_rotate_same_old_link_only_one_wins(self):
        old_jia = self.t_jia
        results = []

        def worker():
            c = new_client()
            results.append(self.rotate(c, "甲", f"/p/{old_jia}").status_code)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
            self.assertFalse(t.is_alive())
        self.assertEqual(sorted(results), [200, 409, 409, 409, 409])
        # 只有一个当前链接，旧链接失效
        cur = self.host_links()["甲"]
        self.assertNotEqual(cur, old_jia)
        self.assertEqual(self.client.get(f"/api/p/{old_jia}").status_code, 404)
        self.assertEqual(self.client.get(f"/api/p/{cur}").status_code, 200)
        conn = sqlite3.connect(app_module.DB_PATH)
        try:
            history = conn.execute(
                "SELECT COUNT(*) FROM token_history WHERE token=?", (old_jia,)
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(history, 1)

    def test_rotate_first_old_credential_write_rejected(self):
        """换发先拿到写锁：旧凭据写入在换发提交后才继续，必须整体 404，数据不动。"""
        old_jia = self.t_jia
        rotate_entered = threading.Event()
        release_rotate = threading.Event()
        writer_begin = threading.Event()
        tls = threading.local()
        armed = {"rotate": True}

        def hook(_wrapper, sql):
            s = sql.lstrip().upper()
            if armed["rotate"] and "UPDATE PARTICIPANTS SET TOKEN" in s:
                armed["rotate"] = False
                rotate_entered.set()
                self.assertTrue(release_rotate.wait(10), "换发未被释放")

        def pre_hook(_wrapper, sql):
            if getattr(tls, "writer", False) and sql.lstrip().upper().startswith("BEGIN IMMEDIATE"):
                # 旧凭据写入即将在写锁上排队；此时换发仍未提交
                self.assertFalse(release_rotate.is_set())
                writer_begin.set()

        result = {}

        def rotator():
            tls.rotator = True
            with use_wrapped_db(hook, pre_hook=pre_hook):
                result["rotate"] = self.rotate(new_client(), "甲", f"/p/{old_jia}").status_code

        t = threading.Thread(target=rotator)
        t.start()
        self.assertTrue(rotate_entered.wait(10), "换发未进入更新阶段")

        def writer():
            tls.writer = True
            with use_wrapped_db(hook, pre_hook=pre_hook):
                result["write"] = new_client().put(
                    f"/api/p/{old_jia}/manuscript", json={"manuscript": "偷改内容"}
                ).status_code

        w = threading.Thread(target=writer)
        w.start()
        # 旧凭据写入的 BEGIN IMMEDIATE 已发起（即在写锁上排队）后，再提交换发
        self.assertTrue(writer_begin.wait(10), "旧凭据写入未发起写事务")
        time.sleep(0.2)
        self.assertNotIn("write", result, "旧凭据写入不应在换发提交前完成")
        release_rotate.set()
        t.join(10)
        w.join(10)
        self.assertFalse(t.is_alive() or w.is_alive())
        self.assertEqual(result["rotate"], 200)
        self.assertEqual(result["write"], 404)
        # 新链接读到的稿件未被偷改
        new_jia = self.host_links()["甲"]
        data = self.client.get(f"/api/p/{new_jia}").get_json()
        self.assertEqual(data["manuscript"], "文本一")
        self.assertEqual(data["version"], 1)

    def test_old_credential_write_first_is_kept(self):
        """旧凭据写入先拿到写锁并提交：换发等待后照常成功，新链接看到已提交的新版本。"""
        old_jia = self.t_jia
        writer_past_check = threading.Event()
        release_writer = threading.Event()
        rotate_begin = threading.Event()
        tls = threading.local()
        armed = {"writer": True}

        def hook(_wrapper, sql):
            if armed["writer"] and sql.lstrip().upper().startswith("UPDATE PARTICIPANTS"):
                armed["writer"] = False
                writer_past_check.set()
                self.assertTrue(release_writer.wait(10), "写入未被释放")

        def pre_hook(_wrapper, sql):
            if getattr(tls, "rotator", False) and sql.lstrip().upper().startswith("BEGIN IMMEDIATE"):
                # 换发已在写锁上排队，而旧凭据写入尚未提交
                self.assertFalse(release_writer.is_set())
                rotate_begin.set()

        result = {}

        def writer():
            tls.writer = True
            with use_wrapped_db(hook, pre_hook=pre_hook):
                result["write"] = new_client().put(
                    f"/api/p/{old_jia}/manuscript", json={"manuscript": "甲的新版本稿"}
                ).status_code

        w = threading.Thread(target=writer)
        w.start()
        self.assertTrue(writer_past_check.wait(10), "旧凭据写入未进入更新阶段")

        def rotator():
            tls.rotator = True
            with use_wrapped_db(hook, pre_hook=pre_hook):
                result["rotate"] = self.rotate(
                    new_client(), "甲", f"/p/{old_jia}"
                ).status_code

        t = threading.Thread(target=rotator)
        t.start()
        self.assertTrue(rotate_begin.wait(10), "换发未发起写事务")
        time.sleep(0.2)
        self.assertNotIn("rotate", result, "换发不应在旧凭据写入提交前完成")
        release_writer.set()
        w.join(10)
        t.join(10)
        self.assertFalse(w.is_alive() or t.is_alive())
        self.assertEqual(result["write"], 200)
        self.assertEqual(result["rotate"], 200)
        # 已提交写入保留；新链接直接看到新版本
        new_jia = self.host_links()["甲"]
        data = self.client.get(f"/api/p/{new_jia}").get_json()
        self.assertEqual(data["manuscript"], "甲的新版本稿")
        self.assertEqual(data["version"], 2)
        self.assertEqual(self.client.get(f"/api/p/{old_jia}").status_code, 404)

    def test_persistence_across_restart(self):
        old_jia = self.t_jia
        resp = self.rotate(self.client, "甲", f"/p/{old_jia}")
        new_jia = resp.get_json()["url"].rsplit("/", 1)[-1]
        # 模拟重启：幂等建表，数据在同一 SQLite 文件中
        app_module.init_db()
        self.assertEqual(self.client.get(f"/api/p/{new_jia}").status_code, 200)
        self.assertEqual(self.client.get(f"/api/p/{new_jia}").get_json()["name"], "甲")
        self.assertEqual(self.client.get(f"/api/p/{old_jia}").status_code, 404)
        # 重启后旧链接仍不能拿去换发（409），主持链接照常有效
        self.assertEqual(self.rotate(self.client, "甲", f"/p/{old_jia}").status_code, 409)
        self.assertEqual(self.host_links()["甲"], new_jia)


class RemoveMemberTest(unittest.TestCase):
    """公布前移除成员：环闭合、旧链接失效、凭据校验、404/409/400、并发顺序、持久化。"""

    def submit_all_v1(self):
        """四人各对当前被分配稿件（v1）提交意见，返回 {姓名: 意见文本}。"""
        reviews = {}
        for name in self.names:
            s = state(self.client, self.tokens[name])
            content = name + "评"
            resp = submit_review(
                self.client, self.tokens[name], content, s["assignment"]["version"]
            )
            self.assertEqual(resp.status_code, 200, resp.get_json())
            reviews[name] = content
        return reviews

    def setUp(self):
        self.client = new_client()
        self.names = ["甲", "乙", "丙", "丁"]
        self.host_token, self.tokens = create_named_session(self.client, self.names)

    def test_remove_closes_ring_and_invalidates_only_predecessor_review(self):
        # 四人首轮意见全部有效：甲评乙、乙评丙、丙评丁、丁评甲
        self.submit_all_v1()

        # 移除乙：仅"原来评乙"的甲改评下一位留存者丙
        self.assertEqual(remove_member(self.client, self.host_token, "乙").status_code, 200)

        s = state(self.client, self.tokens["甲"])
        self.assertEqual(s["assignment"]["manuscript"], "稿丙")
        # 甲的旧意见失效，且标注为分配改变所致
        self.assertFalse(s["my_review"]["valid"])
        self.assertTrue(s["my_review"]["target_changed"])
        self.assertEqual(s["my_review"]["content"], "甲评")

        # 乙→丙：乙不再评丙，丙改由……丙仍评丁；丁仍评甲：其他人意见保持有效
        s_bing = state(self.client, self.tokens["丙"])
        self.assertEqual(s_bing["assignment"]["manuscript"], "稿丁")
        self.assertTrue(s_bing["my_review"]["valid"])
        s_ding = state(self.client, self.tokens["丁"])
        self.assertEqual(s_ding["assignment"]["manuscript"], "稿甲")
        self.assertTrue(s_ding["my_review"]["valid"])

        # 乙的稿件、版本不出现在任何人的分配中；主持台名单仅剩甲丙丁
        host = self.client.get(f"/api/h/{self.host_token}").get_json()
        self.assertEqual([r["name"] for r in host["reviewers"]], ["甲", "丙", "丁"])
        self.assertEqual([l["name"] for l in host["links"]], ["甲", "丙", "丁"])

        # 乙发出/收到的意见不计入公布条件：现在只差"丙收到的意见"（原评审者是乙）
        # 与甲需对丙重交意见 —— 主持台显示甲待交；公布被 409 拒绝
        pending = {r["name"] for r in host["reviewers"] if not r["submitted"]}
        self.assertEqual(pending, {"甲"})
        resp = self.client.post(f"/api/h/{self.host_token}/publish")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(set(resp.get_json()["missing"]), {"丙"})

        # 甲确认重读后用新凭据对丙 v1 重交；丙评丁、丁评甲意见原样有效，随后可公布
        cred = s["assignment"]["credential"]
        self.assertEqual(
            submit_review(
                self.client, self.tokens["甲"], "甲评丙", 1, credential=cred
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.post(f"/api/h/{self.host_token}/publish").status_code, 200
        )
        # 公布后丙收到的是甲的匿名意见（乙的旧意见不出现），其他意见保留
        self.assertEqual(state(self.client, self.tokens["丙"])["received"], "甲评丙")

    def test_received_reviews_after_ring_close(self):
        # 环闭合后各人收到的意见：甲←丁（保留）、丙←甲（重交）、丁←丙（保留）；
        # 乙收到的甲旧意见与乙发出给丙的意见都不计入
        self.submit_all_v1()
        remove_member(self.client, self.host_token, "乙")
        cred_jia = state(self.client, self.tokens["甲"])["assignment"]["credential"]
        submit_review(self.client, self.tokens["甲"], "甲评丙新", 1, credential=cred_jia)
        self.assertEqual(
            self.client.post(f"/api/h/{self.host_token}/publish").status_code, 200
        )
        self.assertEqual(state(self.client, self.tokens["甲"])["received"], "丁评")
        self.assertEqual(state(self.client, self.tokens["丙"])["received"], "甲评丙新")
        self.assertEqual(state(self.client, self.tokens["丁"])["received"], "丙评")

    def test_removed_link_dead_on_all_endpoints(self):
        old_yi = self.tokens["乙"]
        self.assertEqual(remove_member(self.client, self.host_token, "乙").status_code, 200)
        # 状态读取与一切合法写入均 404
        self.assertEqual(self.client.get(f"/api/p/{old_yi}").status_code, 404)
        self.assertEqual(
            self.client.put(
                f"/api/p/{old_yi}/manuscript", json={"manuscript": "偷改"}
            ).status_code,
            404,
        )
        self.assertEqual(
            submit_review(self.client, old_yi, "x", 1).status_code, 404
        )
        self.assertEqual(ask(self.client, old_yi, "q").status_code, 404)
        self.assertEqual(answer(self.client, old_yi, "a").status_code, 404)
        # 退出者不能再被换发链接（按姓名查不到）
        resp = self.client.post(
            f"/api/h/{self.host_token}/rotate-link",
            json={"name": "乙", "link": f"/p/{old_yi}"},
        )
        self.assertEqual(resp.status_code, 404)

    def test_remove_first_and_last_positions(self):
        # 五人：移除首位甲——其原评审者是录入顺序末位的戊，戊改评乙
        five = ["甲", "乙", "丙", "丁", "戊"]
        host, tokens = create_named_session(new_client(), five)
        self.assertEqual(remove_member(self.client, host, "甲").status_code, 200)
        self.assertEqual(state(new_client(), tokens["戊"])["assignment"]["manuscript"], "稿乙")
        self.assertEqual(state(new_client(), tokens["乙"])["assignment"]["manuscript"], "稿丙")
        self.assertEqual(new_client().get(f"/api/p/{tokens['甲']}").status_code, 404)
        # 现存 乙丙丁戊；移除末位（现存末位）丁：丙改评戊，戊仍评乙
        self.assertEqual(remove_member(new_client(), host, "丁").status_code, 200)
        self.assertEqual(state(new_client(), tokens["丙"])["assignment"]["manuscript"], "稿戊")
        self.assertEqual(state(new_client(), tokens["戊"])["assignment"]["manuscript"], "稿乙")

    def test_remove_first_member_reassigns_last_position_only(self):
        """移除首位（当前录入顺序最前者）：仅末位（原来评首位的人）改评并换发凭据。

        回归：曾把"退出者后面第一位"（退出者原来评的人）误当作改评者，导致乙的意见
        被撤销、凭据被换发，而真正改评的丁凭据未换、可不经重读用旧格式提交。
        """
        # 四人稿件版本相同（均 v1）且均已评审：甲评乙、乙评丙、丙评丁、丁评甲
        reviews = self.submit_all_v1()
        before = {n: state(self.client, self.tokens[n]) for n in ["乙", "丙", "丁"]}

        self.assertEqual(remove_member(self.client, self.host_token, "甲").status_code, 200)

        # 仅丁（末位，原来评甲）改评乙：旧意见失效并标注分配改变，凭据换发
        s_ding = state(self.client, self.tokens["丁"])
        self.assertEqual(s_ding["assignment"]["manuscript"], "稿乙")
        self.assertEqual(s_ding["assignment"]["version"], 1)
        self.assertFalse(s_ding["my_review"]["valid"])
        self.assertTrue(s_ding["my_review"]["target_changed"])
        self.assertEqual(s_ding["my_review"]["content"], reviews["丁"])
        cred_ding_old = before["丁"]["assignment"]["credential"]
        cred_ding_new = s_ding["assignment"]["credential"]
        self.assertNotEqual(cred_ding_new, cred_ding_old)

        # 乙、丙的稿件、版本、意见、分配与分配凭据全部不变（已开页面无需重读）
        for n in ["乙", "丙"]:
            s = state(self.client, self.tokens[n])
            self.assertEqual((s["manuscript"], s["version"]), ("稿" + n, 1))
            self.assertTrue(s["my_review"]["valid"])
            self.assertEqual(s["my_review"]["content"], reviews[n])
            self.assertEqual(s["assignment"], before[n]["assignment"])
        # 乙、丙分配从未变过：不带凭据的原格式提交（修改自己的意见）仍被接受
        self.assertEqual(
            submit_review(self.client, self.tokens["乙"], "乙评丙（改）", 1).status_code, 200
        )
        # 主持台：仅丁待交
        host = self.client.get(f"/api/h/{self.host_token}").get_json()
        self.assertEqual(
            {r["name"] for r in host["reviewers"] if not r["submitted"]}, {"丁"}
        )

        # 丁缺少凭据 -> 409 不写入；沿用旧凭据 -> 409 不写入（即使两篇稿版本号都是 1）
        resp = submit_review(self.client, self.tokens["丁"], "丁未重读", 1)
        self.assertEqual(resp.status_code, 409)
        self.assertTrue(resp.get_json()["assignment_changed"])
        resp = submit_review(
            self.client, self.tokens["丁"], "丁旧凭据", 1, credential=cred_ding_old
        )
        self.assertEqual(resp.status_code, 409)
        s = state(self.client, self.tokens["丁"])
        self.assertEqual(s["my_review"]["content"], reviews["丁"])  # 失败不写入
        self.assertFalse(s["my_review"]["valid"])

        # 丁补齐有效意见前公布 409（缺"乙收到的意见"）；持新凭据+当前版本提交后才可公布
        resp = self.client.post(f"/api/h/{self.host_token}/publish")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json()["missing"], ["乙"])
        self.assertEqual(
            submit_review(
                self.client, self.tokens["丁"], "丁评乙", 1, credential=cred_ding_new
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.post(f"/api/h/{self.host_token}/publish").status_code, 200
        )
        # 公布后：乙收到丁的新意见，丙仍收到乙（改后）的意见，丁仍收到丙的意见
        self.assertEqual(state(self.client, self.tokens["乙"])["received"], "丁评乙")
        self.assertEqual(state(self.client, self.tokens["丙"])["received"], "乙评丙（改）")
        self.assertEqual(state(self.client, self.tokens["丁"])["received"], "丙评")

    def test_consecutive_first_removals_reassign_last_each_time(self):
        """五人连续移除当前首位：每次都是当时末位改评新首位，凭据逐次换发。"""
        names = ["甲", "乙", "丙", "丁", "戊"]
        host, tokens = create_named_session(new_client(), names)
        client = new_client()
        for n in names:
            s = state(client, tokens[n])
            self.assertEqual(
                submit_review(client, tokens[n], n + "评", s["assignment"]["version"]).status_code,
                200,
            )
        cred_wu_0 = state(client, tokens["戊"])["assignment"]["credential"]

        # 移除首位甲：末位戊（原评甲）改评乙，戊旧意见失效、凭据换发；乙丙丁一切不变
        self.assertEqual(remove_member(client, host, "甲").status_code, 200)
        s_wu = state(client, tokens["戊"])
        cred_wu_1 = s_wu["assignment"]["credential"]
        self.assertEqual(s_wu["assignment"]["manuscript"], "稿乙")
        self.assertFalse(s_wu["my_review"]["valid"])
        self.assertTrue(s_wu["my_review"]["target_changed"])
        self.assertNotEqual(cred_wu_1, cred_wu_0)
        for n in ["乙", "丙", "丁"]:
            s = state(client, tokens[n])
            self.assertTrue(s["my_review"]["valid"], n)
            self.assertEqual((s["manuscript"], s["version"]), ("稿" + n, 1))
        self.assertEqual(client.get(f"/api/p/{tokens['甲']}").status_code, 404)

        # 戊持新凭据对乙提交后，再移除新首位乙：戊（仍末位，原评乙）改评丙，凭据再次换发
        self.assertEqual(
            submit_review(client, tokens["戊"], "戊评乙", 1, credential=cred_wu_1).status_code,
            200,
        )
        self.assertEqual(remove_member(client, host, "乙").status_code, 200)
        s_wu = state(client, tokens["戊"])
        cred_wu_2 = s_wu["assignment"]["credential"]
        self.assertEqual(s_wu["assignment"]["manuscript"], "稿丙")
        self.assertFalse(s_wu["my_review"]["valid"])  # 刚交的"戊评乙"随之失效
        self.assertTrue(s_wu["my_review"]["target_changed"])
        self.assertNotEqual(cred_wu_2, cred_wu_1)
        self.assertNotEqual(cred_wu_2, cred_wu_0)
        for n in ["丙", "丁"]:
            self.assertTrue(state(client, tokens[n])["my_review"]["valid"], n)
        self.assertEqual(client.get(f"/api/p/{tokens['乙']}").status_code, 404)

        # 戊沿用任一代旧凭据均 409 且不写入；持当前凭据可提交，补齐后公布
        for stale in (cred_wu_0, cred_wu_1):
            resp = submit_review(client, tokens["戊"], "戊旧凭据", 1, credential=stale)
            self.assertEqual(resp.status_code, 409)
        self.assertFalse(state(client, tokens["戊"])["my_review"]["valid"])
        resp = client.post(f"/api/h/{host}/publish")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json()["missing"], ["丙"])
        self.assertEqual(
            submit_review(client, tokens["戊"], "戊评丙", 1, credential=cred_wu_2).status_code,
            200,
        )
        self.assertEqual(client.post(f"/api/h/{host}/publish").status_code, 200)
        self.assertEqual(state(client, tokens["丙"])["received"], "戊评丙")

    def test_404_host_or_member(self):
        self.assertEqual(remove_member(self.client, "假主持", "乙").status_code, 404)
        self.assertEqual(remove_member(self.client, self.host_token, "某人").status_code, 404)
        # 失败不留变更：分配仍是原环（甲评乙）
        self.assertEqual(
            state(self.client, self.tokens["甲"])["assignment"]["manuscript"], "稿乙"
        )
        # 已移除者再次操作（公布前）也是 404：不支持恢复
        self.assertEqual(remove_member(self.client, self.host_token, "乙").status_code, 200)
        self.assertEqual(remove_member(self.client, self.host_token, "乙").status_code, 404)
        # 第二次（对已退出者）失败不留变更：环仍是甲丙丁，甲评丙
        self.assertEqual(
            state(self.client, self.tokens["甲"])["assignment"]["manuscript"], "稿丙"
        )
        self.assertEqual(
            self.client.get(f"/api/p/{self.tokens['乙']}").status_code, 404
        )

    def test_409_when_fewer_than_three_and_after_publish(self):
        # 四人移除一人后只剩三人，再次移除即 409
        self.assertEqual(remove_member(self.client, self.host_token, "乙").status_code, 200)
        resp = remove_member(self.client, self.host_token, "丙")
        self.assertEqual(resp.status_code, 409)
        # 409 不留变更：丙仍在
        self.assertEqual(
            self.client.get(f"/api/p/{self.tokens['丙']}").status_code, 200
        )
        # 三人会话直接移除即 409
        host3, tokens3 = create_session(new_client())
        self.assertEqual(remove_member(new_client(), host3, "甲").status_code, 409)
        # 公布后移除 409
        publish_with_reviews(new_client(), host3, tokens3)
        self.assertEqual(remove_member(new_client(), host3, "甲").status_code, 409)

    def test_400_malformed_request(self):
        for payload in [{}, {"name": "  "}, {"name": "x" * (app_module.MAX_NAME_LEN + 1)}, []]:
            resp = self.client.post(
                f"/api/h/{self.host_token}/remove", json=payload
            )
            self.assertEqual(resp.status_code, 400, payload)

    def test_credential_required_only_after_assignment_change(self):
        # 分配从未变过：兼容不带 credential 的原提交格式
        self.assertEqual(
            submit_review(self.client, self.tokens["甲"], "甲评乙", 1).status_code, 200
        )
        # 乙改稿（分配不变）后，无凭据旧格式仍然接受
        self.assertEqual(
            self.client.put(
                f"/api/p/{self.tokens['乙']}/manuscript",
                json={"manuscript": "稿乙v2"},
            ).status_code,
            200,
        )
        self.assertEqual(
            submit_review(self.client, self.tokens["甲"], "甲评乙v2", 2).status_code, 200
        )

        # 移除丁：仅丙的分配改变（丁→甲）；甲、乙的凭据不变
        self.assertEqual(remove_member(self.client, self.host_token, "丁").status_code, 200)
        cred_bing = state(self.client, self.tokens["丙"])["assignment"]["credential"]
        # 甲的分配未变：旧格式继续可用
        self.assertEqual(
            submit_review(self.client, self.tokens["甲"], "甲改意见", 2).status_code, 200
        )
        # 丙缺少凭据 -> 409；甲、丁均为 v1，即使版本号相同也拒绝
        resp = submit_review(self.client, self.tokens["丙"], "丙无凭据", 1)
        self.assertEqual(resp.status_code, 409)
        self.assertTrue(resp.get_json()["assignment_changed"])
        # 丙沿用旧（错误）凭据 -> 409，即使版本相同
        resp = submit_review(
            self.client, self.tokens["丙"], "丙旧凭据", 1, credential="旧的随便什么"
        )
        self.assertEqual(resp.status_code, 409)
        # 失败不覆盖：丙此前未提交过，移除后仍没有任何意见行
        self.assertIsNone(
            state(self.client, self.tokens["丙"])["my_review"]
        )
        # 新凭据 + 正确版本提交成功
        self.assertEqual(
            submit_review(
                self.client, self.tokens["丙"], "丙评甲", 1, credential=cred_bing
            ).status_code,
            200,
        )
        # credential 类型错误 -> 400
        resp = self.client.post(
            f"/api/p/{self.tokens['丙']}/review",
            json={"content": "x", "version": 1, "credential": 123},
        )
        self.assertEqual(resp.status_code, 400)

    def test_others_manuscripts_versions_links_preserved(self):
        # 乙改稿到 v2 并持有甲的有效意见；丁有一轮意见；移除乙
        self.client.put(
            f"/api/p/{self.tokens['乙']}/manuscript", json={"manuscript": "稿乙二改"}
        )
        submit_review(self.client, self.tokens["甲"], "甲评乙v2", 2)
        submit_review(self.client, self.tokens["丁"], "丁评甲", 1)
        bing_old_link = self.tokens["丙"]
        remove_member(self.client, self.host_token, "乙")

        # 甲自己的稿件与版本不变；丙、丁的稿件/版本/链接不变
        self.assertEqual(
            state(self.client, self.tokens["甲"])["manuscript"], "稿甲"
        )
        s = state(self.client, bing_old_link)
        self.assertEqual((s["manuscript"], s["version"]), ("稿丙", 1))
        self.assertEqual(
            state(self.client, self.tokens["丁"])["assignment"]["manuscript"], "稿甲"
        )
        # 丁对甲的意见仍有效；甲的意见因改评而失效，乙的稿件不影响他人
        self.assertTrue(
            state(self.client, self.tokens["丁"])["my_review"]["valid"]
        )

    def test_submit_first_then_remove_invalidates_and_reopens(self):
        # 甲已对乙 v1 提交有效意见；移除乙：名单/分配/意见一起生效，甲的意见随即失效，
        # 公布条件不再计入乙，且甲需对新分配重交
        submit_review(self.client, self.tokens["甲"], "甲评乙", 1)
        submit_review(self.client, self.tokens["丙"], "丙评丁", 1)
        remove_member(self.client, self.host_token, "乙")
        s = state(self.client, self.tokens["甲"])
        self.assertFalse(s["my_review"]["valid"])
        self.assertTrue(s["my_review"]["target_changed"])
        resp = self.client.post(f"/api/h/{self.host_token}/publish")
        self.assertEqual(resp.status_code, 409)

    def test_concurrent_remove_first_submit_rejected_with_old_credential(self):
        """移除先拿到写锁：甲（原评乙）的提交在移除提交后才继续，凭据已换代 → 409。"""
        cred_jia = state(self.client, self.tokens["甲"])["assignment"]["credential"]
        remove_entered = threading.Event()
        release_remove = threading.Event()
        submit_begin = threading.Event()
        tls = threading.local()
        armed = {"remove": True}

        def hook(_wrapper, sql):
            if armed["remove"] and "UPDATE PARTICIPANTS SET ASSIGNMENT_NONCE" in sql.upper():
                armed["remove"] = False
                remove_entered.set()
                self.assertTrue(release_remove.wait(10), "移除未被释放")

        def pre_hook(_wrapper, sql):
            if getattr(tls, "submitter", False) and sql.lstrip().upper().startswith("BEGIN IMMEDIATE"):
                self.assertFalse(release_remove.is_set())
                submit_begin.set()

        result = {}

        def remover():
            tls.remover = True
            with use_wrapped_db(hook, pre_hook=pre_hook):
                result["remove"] = remove_member(
                    new_client(), self.host_token, "乙"
                ).status_code

        t = threading.Thread(target=remover)
        t.start()
        self.assertTrue(remove_entered.wait(10), "移除未进入凭据换发阶段")

        def submitter():
            tls.submitter = True
            with use_wrapped_db(hook, pre_hook=pre_hook):
                result["submit"] = submit_review(
                    new_client(), self.tokens["甲"], "甲卡在旧凭据", 1,
                    credential=cred_jia,
                ).status_code

        w = threading.Thread(target=submitter)
        w.start()
        self.assertTrue(submit_begin.wait(10), "提交未发起写事务")
        time.sleep(0.2)
        self.assertNotIn("submit", result)
        release_remove.set()
        t.join(10)
        w.join(10)
        self.assertFalse(t.is_alive() or w.is_alive())
        self.assertEqual(result["remove"], 200)
        self.assertEqual(result["submit"], 409)
        # 甲的旧意见不存在（此前未提交）；新分配为丙，需用新凭据
        s = state(self.client, self.tokens["甲"])
        self.assertEqual(s["assignment"]["manuscript"], "稿丙")
        self.assertIsNone(s["my_review"])

    def test_persistence_across_restart(self):
        self.submit_all_v1()
        cred_before = state(self.client, self.tokens["甲"])["assignment"]["credential"]
        remove_member(self.client, self.host_token, "乙")
        new_cred = state(self.client, self.tokens["甲"])["assignment"]["credential"]
        self.assertNotEqual(cred_before, new_cred)

        # 模拟重启：幂等建表/补列，数据在同一 SQLite 文件中
        app_module.init_db()
        # 乙仍永久失效；甲仍改评丙、凭据不变；甲的旧意见仍失效
        self.assertEqual(self.client.get(f"/api/p/{self.tokens['乙']}").status_code, 404)
        s = state(self.client, self.tokens["甲"])
        self.assertEqual(s["assignment"]["manuscript"], "稿丙")
        self.assertEqual(s["assignment"]["credential"], new_cred)
        self.assertFalse(s["my_review"]["valid"])
        # 旧凭据重启后仍 409；新凭据可提交并完成公布
        self.assertEqual(
            submit_review(
                self.client, self.tokens["甲"], "甲评丙", 1, credential=cred_before
            ).status_code,
            409,
        )
        submit_review(self.client, self.tokens["甲"], "甲评丙", 1, credential=new_cred)
        self.assertEqual(
            self.client.post(f"/api/h/{self.host_token}/publish").status_code, 200
        )
        # 丙/丁稿件版本链接保留；乙链接重启后仍 404
        self.assertEqual(
            self.client.get(f"/api/p/{self.tokens['乙']}").status_code, 404
        )


class ReportTest(unittest.TestCase):
    """公布后的意见举报：作者提交、主持台裁定、屏蔽可见性与并发。"""

    def setUp(self):
        self.client = new_client()
        self.host_token, self.tokens = create_session(self.client)
        publish_with_reviews(self.client, self.host_token, self.tokens)

    def host_status(self):
        resp = self.client.get(f"/api/h/{self.host_token}")
        self.assertEqual(resp.status_code, 200, resp.get_json())
        return resp.get_json()

    def test_report_validation_identity_and_visibility(self):
        # 公布前另设新会话：举报返回 409，且不生成记录
        pre_client = new_client()
        host2, tokens2 = create_session(pre_client)
        self.assertEqual(report_review(pre_client, tokens2[0], "原因").status_code, 409)
        self.assertEqual(pre_client.get(f"/api/h/{host2}").get_json()["reports"], [])

        # 原因不能为空、不能全空白，长度不能超过 1000 字
        self.assertEqual(report_review(self.client, self.tokens[0], "").status_code, 400)
        self.assertEqual(report_review(self.client, self.tokens[0], "   ").status_code, 400)
        self.assertEqual(
            report_review(self.client, self.tokens[0], "x" * 1001).status_code, 400
        )
        # 失败不留举报记录
        self.assertIsNone(state(self.client, self.tokens[0])["report"])

        resp = report_review(self.client, self.tokens[0], "甲的举报原因")
        self.assertEqual(resp.status_code, 200, resp.get_json())
        report_id = resp.get_json()["id"]
        self.assertEqual(resp.get_json()["status"], "pending")

        # 作者可查看自己的举报状态；每条意见只可举报一次
        self.assertEqual(
            state(self.client, self.tokens[0])["report"],
            {"id": report_id, "reason": "甲的举报原因", "status": "pending"},
        )
        self.assertEqual(
            report_review(self.client, self.tokens[0], "再次举报").status_code, 409
        )
        self.assertEqual(
            self.client.post("/api/p/not-a-token/report", json={"reason": "x"}).status_code,
            404,
        )

        # 主持列表只展示编号、原因、意见原文和处理状态；不含姓名、链接、问答字段
        reports = self.host_status()["reports"]
        self.assertEqual(len(reports), 1)
        self.assertEqual(set(reports[0]), {"id", "reason", "review", "status"})
        self.assertEqual(
            reports[0],
            {
                "id": report_id,
                "reason": "甲的举报原因",
                "review": "丙评甲",
                "status": "pending",
            },
        )
        # 其他参与者（这里是对应评审者）状态里没有作者的举报信息，自己的意见原文不变
        reviewer_state = state(self.client, self.tokens[2])
        self.assertIsNone(reviewer_state["report"])
        self.assertEqual(reviewer_state["my_review"]["content"], "丙评甲")

    def test_keep_and_block_decisions_and_immutability(self):
        # 甲的举报裁定保留：意见仍可读，状态变为 kept
        r0 = report_review(self.client, self.tokens[0], "请求保留测试")
        self.assertEqual(r0.status_code, 200, r0.get_json())
        id0 = r0.get_json()["id"]
        resp = decide_report(self.client, self.host_token, id0, "keep")
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertEqual(resp.get_json()["status"], "kept")
        self.assertEqual(state(self.client, self.tokens[0])["received"], "丙评甲")
        self.assertEqual(
            state(self.client, self.tokens[0])["report"]["status"], "kept"
        )

        # 乙在被屏蔽前已有问答和回执
        ask(self.client, self.tokens[1], "乙的问题")
        answer(self.client, self.tokens[0], "甲的答复")
        ack(self.client, self.tokens[1])

        r1 = report_review(self.client, self.tokens[1], "请求屏蔽测试")
        self.assertEqual(r1.status_code, 200, r1.get_json())
        id1 = r1.get_json()["id"]
        resp = decide_report(self.client, self.host_token, id1, "block")
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertEqual(resp.get_json()["status"], "blocked")

        author_state = state(self.client, self.tokens[1])
        self.assertIsNone(author_state["received"])
        self.assertTrue(author_state["received_blocked"])
        self.assertEqual(author_state["report"]["status"], "blocked")
        # 问答与回执不变
        self.assertEqual(
            author_state["received_clarification"],
            {"question": "乙的问题", "answer": "甲的答复", "answered": True},
        )
        self.assertTrue(author_state["receipt"]["acknowledged"])

        # 评审者自己的意见及其问答不变
        reviewer_state = state(self.client, self.tokens[0])
        self.assertEqual(reviewer_state["my_review"]["content"], "甲评乙")
        self.assertEqual(
            reviewer_state["my_review_clarification"]["question"], "乙的问题"
        )

        # 丙的意见被屏蔽后，仍可对原意见发起一问一答；评审者乙也仍可答复
        r2 = report_review(self.client, self.tokens[2], "丙请求屏蔽测试")
        self.assertEqual(r2.status_code, 200, r2.get_json())
        id2 = r2.get_json()["id"]
        self.assertEqual(
            decide_report(self.client, self.host_token, id2, "block").status_code, 200
        )
        self.assertEqual(ask(self.client, self.tokens[2], "丙屏蔽后的问题").status_code, 200)
        self.assertEqual(answer(self.client, self.tokens[1], "乙屏蔽后的答复").status_code, 200)
        blocked_qa = state(self.client, self.tokens[2])
        self.assertIsNone(blocked_qa["received"])
        self.assertTrue(blocked_qa["received_blocked"])
        self.assertEqual(
            blocked_qa["received_clarification"],
            {"question": "丙屏蔽后的问题", "answer": "乙屏蔽后的答复", "answered": True},
        )

        # 主持列表仍保留意见原文，且只含允许字段
        reports = {r["id"]: r for r in self.host_status()["reports"]}
        self.assertEqual(reports[id1]["review"], "甲评乙")
        self.assertEqual(reports[id1]["status"], "blocked")

        # 处理后不可改：重复保留/屏蔽均 409；作者也不能重复举报
        self.assertEqual(
            decide_report(self.client, self.host_token, id1, "keep").status_code, 409
        )
        self.assertEqual(
            decide_report(self.client, self.host_token, id1, "block").status_code, 409
        )
        self.assertEqual(
            report_review(self.client, self.tokens[1], "再举报").status_code, 409
        )
        self.assertEqual(state(self.client, self.tokens[1])["received"], None)

        # 无效主持链接 404；有效主持链接处理跨会话/不存在编号也 404
        self.assertEqual(decide_report(self.client, "bad-host", id1, "keep").status_code, 404)
        self.assertEqual(
            decide_report(self.client, self.host_token, 999999, "keep").status_code, 404
        )
        other = new_client()
        other_host, other_tokens = create_session(other)
        publish_with_reviews(other, other_host, other_tokens)
        other_id = report_review(other, other_tokens[0], "其他会话举报").get_json()["id"]
        self.assertEqual(
            decide_report(self.client, self.host_token, other_id, "keep").status_code,
            404,
        )
        # 跨会话探测未改变原举报
        self.assertEqual(
            other.get(f"/api/h/{other_host}").get_json()["reports"][0]["status"],
            "pending",
        )

    def test_concurrent_decision_succeeds_at_most_once(self):
        resp = report_review(self.client, self.tokens[2], "并发处理测试")
        self.assertEqual(resp.status_code, 200, resp.get_json())
        report_id = resp.get_json()["id"]

        start = threading.Barrier(2)
        results = {}

        def choose(decision):
            client = new_client()
            start.wait()
            results[decision] = decide_report(
                client, self.host_token, report_id, decision
            ).status_code

        t1 = threading.Thread(target=choose, args=("keep",))
        t2 = threading.Thread(target=choose, args=("block",))
        t1.start(); t2.start()
        t1.join(10); t2.join(10)
        self.assertFalse(t1.is_alive() or t2.is_alive())
        self.assertEqual(sorted(results.values()), [200, 409])
        self.assertEqual(
            self.host_status()["reports"][0]["status"] in {"kept", "blocked"},
            True,
        )

    def test_blocked_state_persists_across_restart(self):
        resp = report_review(self.client, self.tokens[0], "重启保留测试")
        report_id = resp.get_json()["id"]
        self.assertEqual(
            decide_report(self.client, self.host_token, report_id, "block").status_code,
            200,
        )
        app_module.init_db()
        s = state(self.client, self.tokens[0])
        self.assertIsNone(s["received"])
        self.assertTrue(s["received_blocked"])
        self.assertEqual(s["report"]["status"], "blocked")
        self.assertEqual(
            self.host_status()["reports"][0]["id"], report_id
        )


class CloseSessionTest(unittest.TestCase):
    """永久封存：404/409 门槛、封存后一切写入 409、提交顺序、数据/链接保留、快照与持久化。"""

    def setUp(self):
        self.client = new_client()
        self.host_token, self.tokens = create_session(self.client)
        self.t_jia, self.t_yi, self.t_bing = self.tokens

    def published(self):
        publish_with_reviews(self.client, self.host_token, self.tokens)

    def assertClosed(self, expected=True):
        self.assertIs(host_state(self.client, self.host_token)["closed"], expected)
        for t in self.tokens:
            self.assertIs(state(self.client, t)["closed"], expected)

    def test_404_invalid_host_changes_nothing(self):
        self.published()
        resp = close_session(self.client, "假主持链接")
        self.assertEqual(resp.status_code, 404)
        # 失败不改数据：仍未封存，写入照常
        self.assertClosed(False)
        self.assertEqual(ack(self.client, self.t_jia).status_code, 200)

    def test_409_close_before_publish(self):
        # 仅公布前：即使没有任何未解决事项也不能封存，数据不动
        resp = close_session(self.client, self.host_token)
        self.assertEqual(resp.status_code, 409)
        hs = host_state(self.client, self.host_token)
        self.assertFalse(hs["published"])
        self.assertFalse(hs["closed"])
        # 未公布会话的旧行为不变：仍可改稿、提交、公布
        self.assertEqual(
            self.client.put(
                f"/api/p/{self.t_jia}/manuscript", json={"manuscript": "甲改稿"}
            ).status_code,
            200,
        )
        self.assertEqual(
            submit_review(self.client, self.t_bing, "丙评甲新版", 2).status_code, 200
        )

    def test_409_unanswered_clarification_blocks_close(self):
        self.published()
        self.assertEqual(ask(self.client, self.t_jia, "甲的问题").status_code, 200)
        # 已问未答：不能封存；问题保持未答
        resp = close_session(self.client, self.host_token)
        self.assertEqual(resp.status_code, 409)
        self.assertClosed(False)
        c = state(self.client, self.t_jia)["received_clarification"]
        self.assertEqual(c["question"], "甲的问题")
        self.assertFalse(c["answered"])
        # 答复后（一问一答已闭环）即可封存
        self.assertEqual(answer(self.client, self.t_bing, "丙的答复").status_code, 200)
        self.assertEqual(close_session(self.client, self.host_token).status_code, 200)
        self.assertClosed(True)

    def test_409_pending_report_blocks_close_kept_and_blocked(self):
        self.published()
        rid = report_review(self.client, self.t_jia, "甲举报").get_json()["id"]
        # 有待处理举报：不能封存
        self.assertEqual(close_session(self.client, self.host_token).status_code, 409)
        self.assertClosed(False)
        # 举报本身不受失败的封存影响，仍可裁定；裁定保留后可封存
        self.assertEqual(
            decide_report(self.client, self.host_token, rid, "keep").status_code, 200
        )
        self.assertEqual(close_session(self.client, self.host_token).status_code, 200)
        self.assertClosed(True)

        # 另一会话：屏蔽裁定后同样允许封存，屏蔽语义随封存保留
        client2 = new_client()
        ht2, toks2 = create_session(client2)
        publish_with_reviews(client2, ht2, toks2)
        rid2 = report_review(client2, toks2[0], "乙会话举报").get_json()["id"]
        self.assertEqual(close_session(client2, ht2).status_code, 409)
        self.assertEqual(decide_report(client2, ht2, rid2, "block").status_code, 200)
        self.assertEqual(close_session(client2, ht2).status_code, 200)
        s = state(client2, toks2[0])
        self.assertTrue(s["closed"])
        self.assertIsNone(s["received"])
        self.assertTrue(s["received_blocked"])
        self.assertEqual(s["report"]["status"], "blocked")

    def test_close_success_then_all_writes_409_and_duplicate_close(self):
        self.published()
        # 甲先提问并获答复、乙确认已读：制造已闭环事项
        ask(self.client, self.t_jia, "问题一")
        answer(self.client, self.t_bing, "答复一")
        self.assertEqual(ack(self.client, self.t_yi).status_code, 200)
        resp = close_session(self.client, self.host_token)
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertEqual(resp.get_json(), {"ok": True, "closed": True})
        self.assertClosed(True)

        # 重复封存 409
        self.assertEqual(close_session(self.client, self.host_token).status_code, 409)

        # 封存后该会话一切合法写请求一律 409（含重复确认已读、换发链接、举报裁定）
        self.assertEqual(ack(self.client, self.t_yi).status_code, 409)   # 重复确认
        self.assertEqual(ack(self.client, self.t_jia).status_code, 409)  # 首次确认
        self.assertEqual(ask(self.client, self.t_yi, "乙再问").status_code, 409)
        self.assertEqual(answer(self.client, self.t_bing, "丙再答").status_code, 409)
        self.assertEqual(report_review(self.client, self.t_bing, "丙举报").status_code, 409)
        self.assertEqual(
            self.client.put(
                f"/api/p/{self.t_jia}/manuscript", json={"manuscript": "x"}
            ).status_code,
            409,
        )
        self.assertEqual(
            submit_review(self.client, self.t_jia, "x", 1).status_code, 409
        )
        self.assertEqual(
            self.client.post(f"/api/h/{self.host_token}/publish").status_code, 409
        )
        self.assertEqual(
            self.client.post(
                f"/api/h/{self.host_token}/rotate-link",
                json={"name": "甲", "link": f"/p/{self.t_jia}"},
            ).status_code,
            409,
        )
        self.assertEqual(
            self.client.post(
                f"/api/h/{self.host_token}/remove", json={"name": "甲"}
            ).status_code,
            409,
        )
        # 无效链接在封存后仍一律 404（而非封存的 409）
        self.assertEqual(self.client.get("/api/p/不存在的token").status_code, 404)
        self.assertEqual(ack(self.client, "不存在的token").status_code, 404)
        self.assertEqual(ask(self.client, "不存在的token", "x").status_code, 404)
        self.assertEqual(
            self.client.post("/api/h/假主持/publish").status_code, 404
        )

    def test_failed_and_successful_close_change_nothing_but_flag(self):
        self.published()
        ask(self.client, self.t_jia, "闭环前的问题")
        self.assertEqual(close_session(self.client, self.host_token).status_code, 409)
        # 封存失败不改数据：问答/回执/举报/链接全部原样；失败期间乙仍可确认已读
        self.assertEqual(ack(self.client, self.t_yi).status_code, 200)
        hs_before = host_state(self.client, self.host_token)
        answer(self.client, self.t_bing, "闭环答复")
        self.assertEqual(close_session(self.client, self.host_token).status_code, 200)

        # 封存成功后只读数据完整保留：稿件、意见、问答、回执、举报状态、链接均不变
        s_after = state(self.client, self.t_jia)
        hs_after = host_state(self.client, self.host_token)
        self.assertEqual(s_after["name"], "甲")
        self.assertEqual(s_after["manuscript"], "文本一")
        self.assertEqual(s_after["version"], 1)
        self.assertEqual(s_after["received"], "丙评甲")
        self.assertEqual(
            s_after["received_clarification"],
            {"question": "闭环前的问题", "answer": "闭环答复", "answered": True},
        )
        # 乙此前已确认的回执保留
        sy = state(self.client, self.t_yi)
        self.assertTrue(sy["receipt"]["acknowledged"])
        # 主持台链接原样保留（封存不撤销、不换发链接）
        self.assertEqual(
            [l["url"] for l in hs_after["links"]],
            [l["url"] for l in hs_before["links"]],
        )
        self.assertEqual(
            [l["url"].rsplit("/", 1)[-1] for l in hs_after["links"]], self.tokens
        )
        # 评审者视角的我提交意见与问答也保留
        sb = state(self.client, self.t_bing)
        self.assertEqual(sb["my_review"]["content"], "丙评甲")
        self.assertEqual(sb["my_review_clarification"]["question"], "闭环前的问题")
        # 匿名隔离不变：参与状态中仍无他人姓名/链接字段
        self.assertNotIn("links", s_after)
        # 公布后冻结语义不回退：稿件/意见字段仍是公布时的值
        self.assertTrue(s_after["published"])

    def test_close_after_ack_and_report_kept(self):
        """已读回执与已裁定（保留）举报都不是未解决事项：封存后回执与举报状态保留。"""
        self.published()
        first_at = ack(self.client, self.t_jia).get_json()["acknowledged_at"]
        rid = report_review(self.client, self.t_yi, "乙举报").get_json()["id"]
        decide_report(self.client, self.host_token, rid, "keep")
        self.assertEqual(close_session(self.client, self.host_token).status_code, 200)
        sj = state(self.client, self.t_jia)
        self.assertEqual(
            sj["receipt"], {"acknowledged": True, "acknowledged_at": first_at}
        )
        sy = state(self.client, self.t_yi)
        self.assertEqual(sy["report"]["status"], "kept")
        self.assertEqual(sy["received"], "甲评乙")  # 保留：意见原文仍可读
        # 主持台仍按原顺序展示回执与已裁定举报（只读）
        hs = host_state(self.client, self.host_token)
        self.assertTrue(hs["reviewers"][0]["acknowledged"])
        self.assertEqual(hs["reports"][0]["status"], "kept")

    def test_concurrent_close_only_one_wins(self):
        self.published()
        results = []

        def worker():
            c = new_client()
            results.append(close_session(c, self.host_token).status_code)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
            self.assertFalse(t.is_alive())
        self.assertEqual(sorted(results), [200, 409, 409, 409, 409])
        self.assertClosed(True)

    def test_close_first_pending_question_and_report_rejected(self):
        """封存先拿到写锁并提交：之后排队的提问/举报必须按封存后状态整体 409，不写入。"""
        self.published()
        close_entered = threading.Event()
        release_close = threading.Event()
        writer_begin = threading.Event()
        tls = threading.local()
        armed = {"close": True}

        def hook(_wrapper, sql):
            if armed["close"] and sql.lstrip().upper().startswith("UPDATE SESSIONS SET CLOSED"):
                armed["close"] = False
                close_entered.set()
                self.assertTrue(release_close.wait(10), "封存未被释放")

        def pre_hook(_wrapper, sql):
            if getattr(tls, "writer", False) and sql.lstrip().upper().startswith("BEGIN IMMEDIATE"):
                writer_begin.set()

        result = {}

        def closer():
            tls.closer = True
            with use_wrapped_db(hook, pre_hook=pre_hook):
                result["close"] = close_session(new_client(), self.host_token).status_code

        t = threading.Thread(target=closer)
        t.start()
        self.assertTrue(close_entered.wait(10), "封存未进入更新阶段")

        def writer(token, path, payload, key):
            tls.writer = True
            with use_wrapped_db(hook, pre_hook=pre_hook):
                c = new_client()
                if path == "/question":
                    r = ask(c, token, payload)
                else:
                    r = report_review(c, token, payload)
                result[key] = r.status_code

        w1 = threading.Thread(
            target=writer, args=(self.t_yi, "/question", "封存排队提问", "question")
        )
        w2 = threading.Thread(
            target=writer, args=(self.t_bing, "/report", "封存排队举报", "report")
        )
        w1.start()
        w2.start()
        self.assertTrue(writer_begin.wait(10), "排队写入未发起写事务")
        time.sleep(0.2)
        self.assertNotIn("question", result)
        self.assertNotIn("report", result)
        release_close.set()
        t.join(10)
        w1.join(10)
        w2.join(10)
        self.assertFalse(t.is_alive() or w1.is_alive() or w2.is_alive())
        self.assertEqual(result["close"], 200)
        self.assertEqual(result["question"], 409)
        self.assertEqual(result["report"], 409)
        # 封存成功后没有写入任何新问题/举报
        self.assertClosed(True)
        self.assertIsNone(state(self.client, self.t_yi)["received_clarification"])
        self.assertIsNone(state(self.client, self.t_bing)["report"])
        self.assertEqual(host_state(self.client, self.host_token)["reports"], [])
        # 评审者丙侧也不应出现新问答
        self.assertIsNone(state(self.client, self.t_bing)["my_review_clarification"])

    def test_question_first_then_close_rejected_until_answered(self):
        """提问先提交：封存检查到未答问题整体 409，问题保留；答复后封存才成功。"""
        self.published()
        question_done = threading.Event()
        release_question = threading.Event()
        close_begin = threading.Event()
        tls = threading.local()
        armed = {"question": True}

        def hook(_wrapper, sql):
            if armed["question"] and sql.lstrip().upper().startswith("INSERT INTO CLARIFICATIONS"):
                armed["question"] = False
                question_done.set()
                self.assertTrue(release_question.wait(10), "提问未被释放")

        def pre_hook(_wrapper, sql):
            if getattr(tls, "closer", False) and sql.lstrip().upper().startswith("BEGIN IMMEDIATE"):
                close_begin.set()

        result = {}

        def asker():
            tls.asker = True
            with use_wrapped_db(hook, pre_hook=pre_hook):
                result["question"] = ask(new_client(), self.t_jia, "抢先提问").status_code

        w = threading.Thread(target=asker)
        w.start()
        self.assertTrue(question_done.wait(10), "提问未进入写入阶段")

        def closer():
            tls.closer = True
            with use_wrapped_db(hook, pre_hook=pre_hook):
                result["close"] = close_session(new_client(), self.host_token).status_code

        t = threading.Thread(target=closer)
        t.start()
        self.assertTrue(close_begin.wait(10), "封存未发起写事务")
        time.sleep(0.2)
        self.assertNotIn("close", result, "封存不应在提问提交前完成")
        release_question.set()
        w.join(10)
        t.join(10)
        self.assertFalse(w.is_alive() or t.is_alive())
        self.assertEqual(result["question"], 200)
        # 提问已提交且无人答复：封存被整体拒绝，未解决事项保留
        self.assertEqual(result["close"], 409)
        self.assertClosed(False)
        c = state(self.client, self.t_jia)["received_clarification"]
        self.assertEqual(c["question"], "抢先提问")
        self.assertFalse(c["answered"])
        # 答复闭环后，再次封存成功
        self.assertEqual(answer(self.client, self.t_bing, "答复").status_code, 200)
        self.assertEqual(close_session(self.client, self.host_token).status_code, 200)
        self.assertClosed(True)

    def test_report_first_pending_blocks_close_decide_then_ok(self):
        """举报先提交：封存看到 pending 举报整体 409；主持人裁定后封存成功，屏蔽语义保留。"""
        self.published()
        self.assertEqual(report_review(self.client, self.t_jia, "先举报").status_code, 200)
        self.assertEqual(close_session(self.client, self.host_token).status_code, 409)
        self.assertClosed(False)
        rid = state(self.client, self.t_jia)["report"]["id"]
        self.assertEqual(
            decide_report(self.client, self.host_token, rid, "block").status_code, 200
        )
        self.assertEqual(close_session(self.client, self.host_token).status_code, 200)
        s = state(self.client, self.t_jia)
        self.assertTrue(s["closed"])
        self.assertIsNone(s["received"])
        self.assertTrue(s["received_blocked"])
        # 封存后不能再改裁定
        self.assertEqual(
            decide_report(self.client, self.host_token, rid, "keep").status_code, 409
        )
        self.assertEqual(state(self.client, self.t_jia)["report"]["status"], "blocked")

    def test_persistence_across_restart(self):
        self.published()
        ask(self.client, self.t_jia, "持久化问题")
        answer(self.client, self.t_bing, "持久化答复")
        self.assertEqual(ack(self.client, self.t_yi).status_code, 200)
        self.assertEqual(close_session(self.client, self.host_token).status_code, 200)
        # 模拟重启：init_db 幂等，closed 随原 SQLite 文件保留
        app_module.init_db()
        self.assertClosed(True)
        self.assertEqual(
            state(self.client, self.t_jia)["received_clarification"]["answer"], "持久化答复"
        )
        # 重启后封存仍生效：写入 409，链接未撤销、只读 200
        self.assertEqual(ack(self.client, self.t_jia).status_code, 409)
        self.assertEqual(
            self.client.post(
                f"/api/h/{self.host_token}/rotate-link",
                json={"name": "甲", "link": f"/p/{self.t_jia}"},
            ).status_code,
            409,
        )
        self.assertEqual(self.client.get(f"/api/p/{self.t_jia}").status_code, 200)
        self.assertEqual(close_session(self.client, self.host_token).status_code, 409)

    def test_unclosed_session_behavior_unchanged(self):
        """未封存会话不出现 closed 引起的行为变化：公布前后旧接口全部照旧。"""
        # 公布前：closed=false 且可写
        s = state(self.client, self.t_jia)
        self.assertFalse(s["closed"])
        self.assertEqual(
            self.client.put(
                f"/api/p/{self.t_jia}/manuscript", json={"manuscript": "文本一v2"}
            ).status_code,
            200,
        )
        # 公布（丙需重投甲 v2）后：closed=false，一问一答/回执/举报/换发全部可用
        self.assertEqual(submit_review(self.client, self.t_jia, "甲评乙", 1).status_code, 200)
        self.assertEqual(submit_review(self.client, self.t_yi, "乙评丙", 1).status_code, 200)
        self.assertEqual(submit_review(self.client, self.t_bing, "丙评甲v2", 2).status_code, 200)
        self.assertEqual(self.client.post(f"/api/h/{self.host_token}/publish").status_code, 200)
        self.assertClosed(False)
        self.assertEqual(ack(self.client, self.t_jia).status_code, 200)
        self.assertEqual(ack(self.client, self.t_jia).status_code, 200)  # 重复确认仍成功
        self.assertEqual(ask(self.client, self.t_yi, "乙问").status_code, 200)
        self.assertEqual(answer(self.client, self.t_jia, "甲答乙的问题").status_code, 200)
        rid = report_review(self.client, self.t_bing, "丙举报").get_json()["id"]
        self.assertEqual(
            decide_report(self.client, self.host_token, rid, "keep").status_code, 200
        )
        resp = self.client.post(
            f"/api/h/{self.host_token}/rotate-link",
            json={"name": "甲", "link": f"/p/{self.t_jia}"},
        )
        self.assertEqual(resp.status_code, 200)


class AnthologyTest(unittest.TestCase):
    """匿名合辑：授权/撤回、发布门槛、提交顺序、公开只读、冻结、封存共存与持久化。"""

    def setUp(self):
        self.client = new_client()
        self.names = ["甲", "乙", "丙"]
        self.host_token, self.tokens = create_named_session(self.client, self.names)
        publish_with_reviews(
            self.client, self.host_token, [self.tokens[n] for n in self.names]
        )

    def host_reviewers(self):
        return host_state(self.client, self.host_token)["reviewers"]

    def consents(self):
        return [r["anthology_consent"] for r in self.host_reviewers()]

    def test_consent_flow_and_status_visibility(self):
        # 初始均未授权：参与状态（本人）与主持状态（各人）同快照返回
        for n in self.names:
            self.assertIs(state(self.client, self.tokens[n])["anthology_consent"], False)
        hs = host_state(self.client, self.host_token)
        self.assertEqual([r["anthology_consent"] for r in hs["reviewers"]], [False] * 3)
        self.assertIsNone(hs["anthology_url"])

        # 甲授权：只有甲的授权变化，主持台按留存名单原顺序展示各人授权
        resp = set_consent(self.client, self.tokens["甲"], True)
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertEqual(resp.get_json(), {"ok": True, "consent": True})
        self.assertIs(state(self.client, self.tokens["甲"])["anthology_consent"], True)
        self.assertIs(state(self.client, self.tokens["乙"])["anthology_consent"], False)
        self.assertEqual(self.consents(), [True, False, False])

        # 参与状态只返回本人授权：响应中只有一个授权字段，且不含公开链接字段
        body = self.client.get(f"/api/p/{self.tokens['乙']}").get_data(as_text=True)
        self.assertEqual(body.count("anthology_consent"), 1)
        self.assertNotIn("anthology_url", body)

        # 撤回：甲回到未授权
        resp = set_consent(self.client, self.tokens["甲"], False)
        self.assertEqual(resp.get_json(), {"ok": True, "consent": False})
        self.assertEqual(self.consents(), [False, False, False])

    def test_consent_request_rules(self):
        # 请求体非法 400：缺 consent、非布尔
        for payload in [{}, {"consent": "true"}, {"consent": 1}, {"consent": None}]:
            resp = self.client.post(
                f"/api/p/{self.tokens['甲']}/anthology-consent", json=payload
            )
            self.assertEqual(resp.status_code, 400, payload)
        # 无效个人链接 404
        self.assertEqual(set_consent(new_client(), "无效链接", True).status_code, 404)
        # 失败均不留变更
        self.assertIs(state(self.client, self.tokens["甲"])["anthology_consent"], False)

        # 公布前（另一会话）：授权 409、发布 409，且不留合辑
        client2 = new_client()
        host2, toks2 = create_session(client2)
        self.assertEqual(set_consent(client2, toks2[0], True).status_code, 409)
        self.assertEqual(publish_anthology(client2, host2, ["甲"]).status_code, 409)
        self.assertIsNone(host_state(client2, host2)["anthology_url"])
        self.assertIs(state(client2, toks2[0])["anthology_consent"], False)

    def test_publish_request_rules(self):
        # 非法名单 400：缺字段、非列表、空名单、重复、空白/非字符串/超长姓名
        for payload in [
            {},
            {"names": None},
            {"names": "甲乙丙"},
            {"names": []},
            {"names": ["甲", "甲"]},
            {"names": ["甲", "  "]},
            {"names": ["甲", 1]},
            {"names": ["甲", "x" * (app_module.MAX_NAME_LEN + 1)]},
        ]:
            resp = self.client.post(f"/api/h/{self.host_token}/anthology", json=payload)
            self.assertEqual(resp.status_code, 400, payload)
        # 无效主持链接 404；姓名不属于本会话留存成员 404
        self.assertEqual(publish_anthology(new_client(), "假主持", ["甲"]).status_code, 404)
        self.assertEqual(
            publish_anthology(self.client, self.host_token, ["甲", "某人"]).status_code,
            404,
        )
        # 失败不留合辑
        self.assertIsNone(host_state(self.client, self.host_token)["anthology_url"])

    def test_unconsented_selection_fails_and_leaves_nothing(self):
        set_consent(self.client, self.tokens["甲"], True)
        set_consent(self.client, self.tokens["乙"], True)
        resp = publish_anthology(self.client, self.host_token, ["甲", "乙", "丙"])
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json()["missing_consent"], ["丙"])
        # 整次失败不留合辑，授权状态不变
        self.assertIsNone(host_state(self.client, self.host_token)["anthology_url"])
        conn = sqlite3.connect(app_module.DB_PATH)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM anthologies WHERE session_id="
                "(SELECT id FROM sessions WHERE host_token=?)",
                (self.host_token,),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 0)
        self.assertEqual(self.consents(), [True, True, False])
        # 丙补授权后重发成功：失败不冻结任何状态
        set_consent(self.client, self.tokens["丙"], True)
        resp = publish_anthology(self.client, self.host_token, ["甲", "乙", "丙"])
        self.assertEqual(resp.status_code, 200, resp.get_json())

    def test_public_page_contains_only_number_and_manuscript(self):
        client = new_client()
        resp = client.post(
            "/api/sessions",
            json={
                "people": [
                    {"name": "甲", "manuscript": "春眠不觉晓"},
                    {"name": "乙", "manuscript": "处处闻啼鸟"},
                    {"name": "丙", "manuscript": "夜来风雨声"},
                ]
            },
        )
        data = resp.get_json()
        host = data["host_url"].rsplit("/", 1)[-1]
        toks = {p["name"]: p["url"].rsplit("/", 1)[-1] for p in data["people"]}
        for n in ["甲", "乙", "丙"]:
            self.assertEqual(
                submit_review(client, toks[n], "评审意见机密" + n, 1).status_code, 200
            )
        self.assertEqual(client.post(f"/api/h/{host}/publish").status_code, 200)
        # 一问一答与举报：其内容都不应出现在公开合辑中
        self.assertEqual(ask(client, toks["甲"], "澄清问题机密").status_code, 200)
        self.assertEqual(answer(client, toks["丙"], "澄清答复机密").status_code, 200)
        self.assertEqual(report_review(client, toks["乙"], "举报原因机密").status_code, 200)
        # 全部授权后乱序选择 [丙, 甲] 发布：编号按原名单顺序（甲=1、丙=2），稿文固化
        for n in ["甲", "乙", "丙"]:
            self.assertEqual(set_consent(client, toks[n], True).status_code, 200)
        resp = publish_anthology(client, host, ["丙", "甲"])
        self.assertEqual(resp.status_code, 200, resp.get_json())
        url = resp.get_json()["url"]
        pub = url.rsplit("/", 1)[-1]

        resp = client.get(f"/api/a/{pub}")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(set(data.keys()), {"items"})
        self.assertEqual(
            data["items"],
            [
                {"number": 1, "manuscript": "春眠不觉晓"},
                {"number": 2, "manuscript": "夜来风雨声"},
            ],
        )
        for it in data["items"]:
            self.assertEqual(set(it.keys()), {"number", "manuscript"})
        body = resp.get_data(as_text=True)
        # 不含姓名、私密链接、意见、问答、举报或授权记录；未入选的稿件不出现
        for leaked in (
            "甲", "乙", "丙",
            "评审意见机密", "澄清问题机密", "澄清答复机密", "举报原因机密",
            "consent", "token", host, toks["甲"], toks["乙"], toks["丙"],
            "处处闻啼鸟",
        ):
            self.assertNotIn(leaked, body)
        # 公开页面可打开；无效公开链接 404
        resp = client.get(f"/a/{pub}")
        self.assertEqual(resp.status_code, 200)
        resp.close()  # 关闭静态文件句柄
        self.assertEqual(client.get("/api/a/不存在的公开链接").status_code, 404)
        # 主持状态可见公开链接；参与状态不含公开链接
        self.assertEqual(host_state(client, host)["anthology_url"], url)
        self.assertNotIn("anthology_url", state(client, toks["甲"]))

    def test_freeze_after_publish(self):
        for n in self.names:
            set_consent(self.client, self.tokens[n], True)
        resp = publish_anthology(self.client, self.host_token, ["甲", "丙"])
        self.assertEqual(resp.status_code, 200)
        pub = resp.get_json()["url"].rsplit("/", 1)[-1]
        # 全部授权冻结：入选者与未入选者的变更一律 409
        self.assertEqual(set_consent(self.client, self.tokens["甲"], False).status_code, 409)
        self.assertEqual(set_consent(self.client, self.tokens["乙"], True).status_code, 409)
        # 重复发布（相同或不同名单）一律 409
        self.assertEqual(
            publish_anthology(self.client, self.host_token, ["甲", "丙"]).status_code, 409
        )
        self.assertEqual(
            publish_anthology(self.client, self.host_token, ["乙"]).status_code, 409
        )
        # 合辑内容与授权状态均保持发布时状态
        self.assertEqual(
            self.client.get(f"/api/a/{pub}").get_json()["items"],
            [
                {"number": 1, "manuscript": "稿甲"},
                {"number": 2, "manuscript": "稿丙"},
            ],
        )
        self.assertEqual(self.consents(), [True, True, True])

    def test_rotate_link_keeps_consent(self):
        set_consent(self.client, self.tokens["甲"], True)
        old = self.tokens["甲"]
        resp = self.client.post(
            f"/api/h/{self.host_token}/rotate-link",
            json={"name": "甲", "link": f"/p/{old}"},
        )
        self.assertEqual(resp.status_code, 200, resp.get_json())
        new = resp.get_json()["url"].rsplit("/", 1)[-1]
        # 授权随原身份保留；旧链接的授权操作 404
        self.assertIs(state(self.client, new)["anthology_consent"], True)
        self.assertEqual(set_consent(self.client, old, False).status_code, 404)
        # 新链接可正常撤回/再授权
        self.assertEqual(set_consent(self.client, new, False).status_code, 200)
        self.assertIs(state(self.client, new)["anthology_consent"], False)
        self.assertEqual(set_consent(self.client, new, True).status_code, 200)
        # 换发不影响发布：甲以随身份保留的授权入选
        resp = publish_anthology(self.client, self.host_token, ["甲"])
        self.assertEqual(resp.status_code, 200, resp.get_json())
        # 发布后再换发链接（会话未封存仍允许）：授权仍冻结且随身份保留
        resp = self.client.post(
            f"/api/h/{self.host_token}/rotate-link",
            json={"name": "甲", "link": f"/p/{new}"},
        )
        self.assertEqual(resp.status_code, 200)
        newest = resp.get_json()["url"].rsplit("/", 1)[-1]
        self.assertIs(state(self.client, newest)["anthology_consent"], True)
        self.assertEqual(set_consent(self.client, newest, False).status_code, 409)

    def test_removed_member_cannot_be_selected(self):
        client = new_client()
        names = ["甲", "乙", "丙", "丁"]
        host, toks = create_named_session(client, names)
        for n in names:
            s = state(client, toks[n])
            self.assertEqual(
                submit_review(client, toks[n], n + "评", s["assignment"]["version"]).status_code,
                200,
            )
        self.assertEqual(remove_member(client, host, "乙").status_code, 200)
        cred = state(client, toks["甲"])["assignment"]["credential"]
        self.assertEqual(
            submit_review(client, toks["甲"], "甲评丙", 1, credential=cred).status_code, 200
        )
        self.assertEqual(client.post(f"/api/h/{host}/publish").status_code, 200)
        # 已移除成员按不存在处理 404，失败不留合辑
        self.assertEqual(publish_anthology(client, host, ["甲", "乙"]).status_code, 404)
        self.assertIsNone(host_state(client, host)["anthology_url"])
        # 留存成员可正常发布，编号按原名单顺序
        for n in ["甲", "丙", "丁"]:
            set_consent(client, toks[n], True)
        resp = publish_anthology(client, host, ["丁", "甲", "丙"])
        self.assertEqual(resp.status_code, 200, resp.get_json())
        pub = resp.get_json()["url"].rsplit("/", 1)[-1]
        self.assertEqual(
            [it["manuscript"] for it in client.get(f"/api/a/{pub}").get_json()["items"]],
            ["稿甲", "稿丙", "稿丁"],
        )

    def test_close_interaction(self):
        # 先发布合辑再封存：合辑公开页仍可只读，授权变更与再发布 409
        for n in self.names:
            set_consent(self.client, self.tokens[n], True)
        resp = publish_anthology(self.client, self.host_token, ["甲", "乙"])
        self.assertEqual(resp.status_code, 200)
        pub = resp.get_json()["url"].rsplit("/", 1)[-1]
        self.assertEqual(close_session(self.client, self.host_token).status_code, 200)
        resp = self.client.get(f"/api/a/{pub}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.get_json()["items"]), 2)
        self.assertEqual(set_consent(self.client, self.tokens["甲"], False).status_code, 409)
        self.assertEqual(
            publish_anthology(self.client, self.host_token, ["甲"]).status_code, 409
        )
        # 封存后主持状态仍可见公开链接（只读）
        hs = host_state(self.client, self.host_token)
        self.assertTrue(hs["closed"])
        self.assertEqual(hs["anthology_url"], f"/a/{pub}")

        # 先封存（未发合辑）：授权与发布均 409
        client2 = new_client()
        host2, toks2 = create_session(client2)
        publish_with_reviews(client2, host2, toks2)
        self.assertEqual(close_session(client2, host2).status_code, 200)
        self.assertEqual(set_consent(client2, toks2[0], True).status_code, 409)
        self.assertEqual(publish_anthology(client2, host2, ["甲"]).status_code, 409)
        self.assertIsNone(host_state(client2, host2)["anthology_url"])

    def test_withdraw_first_then_publish_fails(self):
        """入选作者先撤回（先提交）：发布在校验时看到撤回，整次 409，不留合辑。"""
        for n in self.names:
            set_consent(self.client, self.tokens[n], True)
        withdraw_entered = threading.Event()
        release_withdraw = threading.Event()
        publish_begin = threading.Event()
        tls = threading.local()
        armed = {"withdraw": True}

        def hook(_wrapper, sql):
            if armed["withdraw"] and sql.lstrip().upper().startswith(
                "UPDATE PARTICIPANTS SET ANTHOLOGY_CONSENT"
            ):
                armed["withdraw"] = False
                withdraw_entered.set()
                self.assertTrue(release_withdraw.wait(10), "撤回未被释放")

        def pre_hook(_wrapper, sql):
            if getattr(tls, "publisher", False) and sql.lstrip().upper().startswith(
                "BEGIN IMMEDIATE"
            ):
                publish_begin.set()

        result = {}

        def withdrawer():
            with use_wrapped_db(hook, pre_hook=pre_hook):
                result["withdraw"] = set_consent(
                    new_client(), self.tokens["甲"], False
                ).status_code

        w = threading.Thread(target=withdrawer)
        w.start()
        self.assertTrue(withdraw_entered.wait(10), "撤回未进入更新阶段")

        def publisher():
            tls.publisher = True
            with use_wrapped_db(hook, pre_hook=pre_hook):
                result["publish"] = publish_anthology(
                    new_client(), self.host_token, ["甲", "乙", "丙"]
                ).status_code

        t = threading.Thread(target=publisher)
        t.start()
        self.assertTrue(publish_begin.wait(10), "发布未发起写事务")
        time.sleep(0.2)
        self.assertNotIn("publish", result, "发布不应在撤回提交前完成")
        release_withdraw.set()
        w.join(10)
        t.join(10)
        self.assertFalse(w.is_alive() or t.is_alive())
        self.assertEqual(result["withdraw"], 200)
        self.assertEqual(result["publish"], 409)
        # 不留合辑：甲已撤回，乙、丙授权不变
        self.assertIsNone(host_state(self.client, self.host_token)["anthology_url"])
        self.assertEqual(self.consents(), [False, True, True])

    def test_publish_first_then_consent_frozen_and_repeat_409(self):
        """发布先提交：后排队的授权变更看到合辑已存在而 409（全部授权冻结），重复发布 409。"""
        for n in self.names:
            set_consent(self.client, self.tokens[n], True)
        publish_entered = threading.Event()
        release_publish = threading.Event()
        consent_begin = threading.Event()
        tls = threading.local()
        armed = {"publish": True}

        def hook(_wrapper, sql):
            if armed["publish"] and sql.lstrip().upper().startswith(
                "INSERT INTO ANTHOLOGIES"
            ):
                armed["publish"] = False
                publish_entered.set()
                self.assertTrue(release_publish.wait(10), "发布未被释放")

        def pre_hook(_wrapper, sql):
            if getattr(tls, "consenter", False) and sql.lstrip().upper().startswith(
                "BEGIN IMMEDIATE"
            ):
                consent_begin.set()

        result = {}

        def publisher():
            with use_wrapped_db(hook, pre_hook=pre_hook):
                result["publish"] = publish_anthology(
                    new_client(), self.host_token, ["甲", "乙", "丙"]
                ).status_code

        t = threading.Thread(target=publisher)
        t.start()
        self.assertTrue(publish_entered.wait(10), "发布未进入写入阶段")

        def consenter():
            tls.consenter = True
            with use_wrapped_db(hook, pre_hook=pre_hook):
                result["consent"] = set_consent(
                    new_client(), self.tokens["甲"], False
                ).status_code

        w = threading.Thread(target=consenter)
        w.start()
        self.assertTrue(consent_begin.wait(10), "授权变更未发起写事务")
        time.sleep(0.2)
        self.assertNotIn("consent", result, "授权变更不应在发布提交前完成")
        release_publish.set()
        t.join(10)
        w.join(10)
        self.assertFalse(t.is_alive() or w.is_alive())
        self.assertEqual(result["publish"], 200)
        self.assertEqual(result["consent"], 409)
        # 授权冻结：甲仍保持已授权；重复发布 409
        self.assertEqual(self.consents(), [True, True, True])
        self.assertEqual(
            publish_anthology(self.client, self.host_token, ["甲"]).status_code, 409
        )

    def test_persistence_across_restart(self):
        set_consent(self.client, self.tokens["甲"], True)
        set_consent(self.client, self.tokens["丙"], True)
        resp = publish_anthology(self.client, self.host_token, ["丙", "甲"])
        self.assertEqual(resp.status_code, 200)
        url = resp.get_json()["url"]
        pub = url.rsplit("/", 1)[-1]
        # 模拟重启：init_db 幂等，合辑、授权与公开链接随原库保留
        app_module.init_db()
        self.assertEqual(
            self.client.get(f"/api/a/{pub}").get_json()["items"],
            [
                {"number": 1, "manuscript": "稿甲"},
                {"number": 2, "manuscript": "稿丙"},
            ],
        )
        self.assertEqual(host_state(self.client, self.host_token)["anthology_url"], url)
        self.assertEqual(self.consents(), [True, False, True])
        # 冻结状态保留：变更与重复发布仍 409
        self.assertEqual(set_consent(self.client, self.tokens["乙"], True).status_code, 409)
        self.assertEqual(
            publish_anthology(self.client, self.host_token, ["乙"]).status_code, 409
        )


class HostAnthologySelectionFlowTest(unittest.TestCase):
    """主持页合辑勾选流程的 API 级回归：重复轮询只读且持续反映授权变化、
    撤回授权后整次发布失败可重试、封存后只读（页面据此禁用选择与发布）。

    前端"勾选结果随 3 秒轮询保留、按页面名单顺序提交"的回归见 selftest_host.js。
    """

    def setUp(self):
        self.client = new_client()
        self.names = ["甲", "乙", "丙"]
        self.host_token, self.tokens = create_named_session(self.client, self.names)
        publish_with_reviews(
            self.client, self.host_token, [self.tokens[n] for n in self.names]
        )

    def poll(self):
        """主持页一次 3 秒轮询对应的状态读取。"""
        return host_state(self.client, self.host_token)

    def test_repeated_polling_is_read_only_and_reflects_consent_updates(self):
        set_consent(self.client, self.tokens["甲"], True)
        set_consent(self.client, self.tokens["丙"], True)
        first = self.poll()
        self.assertIsNone(first["anthology_url"])
        # 模拟主持页反复轮询：读取只读，不产生任何变更，响应逐次一致
        for _ in range(5):
            self.assertEqual(self.poll(), first)
        self.assertEqual(
            [r["anthology_consent"] for r in first["reviewers"]],
            [True, False, True],
        )
        # 轮询持续反映各人授权变化：乙授权后下一次轮询即可见
        set_consent(self.client, self.tokens["乙"], True)
        self.assertEqual(
            [r["anthology_consent"] for r in self.poll()["reviewers"]],
            [True, True, True],
        )

    def test_withdraw_then_failed_publish_keeps_state_and_retry_succeeds(self):
        for n in self.names:
            set_consent(self.client, self.tokens[n], True)
        # 入选成员在发布前撤回授权：主持端下一次轮询看到撤回，但页面保留按姓名
        # 勾选的名单（见 selftest_host.js），发布时仍会提交该姓名
        set_consent(self.client, self.tokens["丙"], False)
        self.assertEqual(
            [r["anthology_consent"] for r in self.poll()["reviewers"]],
            [True, True, False],
        )
        # 服务端整次拒绝并给出未授权名单；不留合辑、授权不变，可调整重试
        resp = publish_anthology(self.client, self.host_token, ["甲", "乙", "丙"])
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json()["missing_consent"], ["丙"])
        self.assertIsNone(self.poll()["anthology_url"])
        self.assertEqual(
            [r["anthology_consent"] for r in self.poll()["reviewers"]],
            [True, True, False],
        )
        # 重新授权后按原名单重试成功：按页面名单（录入）顺序编号固化
        set_consent(self.client, self.tokens["丙"], True)
        resp = publish_anthology(self.client, self.host_token, ["甲", "乙", "丙"])
        self.assertEqual(resp.status_code, 200, resp.get_json())
        pub = resp.get_json()["url"].rsplit("/", 1)[-1]
        items = self.client.get(f"/api/a/{pub}").get_json()["items"]
        self.assertEqual(
            [(it["number"], it["manuscript"]) for it in items],
            [(1, "稿甲"), (2, "稿乙"), (3, "稿丙")],
        )
        # 发布后主持状态返回公开链接：页面据此停止编辑、只显示链接
        self.assertEqual(self.poll()["anthology_url"], f"/a/{pub}")

    def test_closed_disables_selection_and_publish(self):
        set_consent(self.client, self.tokens["甲"], True)
        self.assertEqual(close_session(self.client, self.host_token).status_code, 200)
        hs = self.poll()
        self.assertTrue(hs["closed"])
        # 封存后授权变更与发布均 409：页面据 closed=true 禁用勾选与发布
        self.assertEqual(
            set_consent(self.client, self.tokens["乙"], True).status_code, 409
        )
        self.assertEqual(
            publish_anthology(self.client, self.host_token, ["甲"]).status_code, 409
        )
        # 状态不变：仍无合辑，各人授权保持封存时快照
        hs = self.poll()
        self.assertIsNone(hs["anthology_url"])
        self.assertEqual(
            [r["anthology_consent"] for r in hs["reviewers"]],
            [True, False, False],
        )


class OldSchemaMigrationTest(unittest.TestCase):
    """旧库启动时自动支持问答、换发、移除、回执、举报与封存存储：补表/补列/回填凭据，旧数据保留。"""

    OLD_SCHEMA = """
    CREATE TABLE sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        host_token TEXT NOT NULL UNIQUE,
        published INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE participants (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER NOT NULL REFERENCES sessions(id),
        token TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        position INTEGER NOT NULL,
        manuscript TEXT NOT NULL,
        version INTEGER NOT NULL DEFAULT 1,
        UNIQUE (session_id, position)
    );
    CREATE TABLE reviews (
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
    """

    def test_old_db_auto_upgraded_and_persists(self):
        tmp_dir = tempfile.mkdtemp(prefix="review-olddb-")
        old_path = os.path.join(tmp_dir, "old.db")
        conn = sqlite3.connect(old_path)
        conn.executescript(self.OLD_SCHEMA)
        conn.execute("INSERT INTO sessions (id, host_token, published) VALUES (1,'ht',1)")
        conn.execute(
            "INSERT INTO participants (id, session_id, token, name, position, manuscript, version)"
            " VALUES (1,1,'tk','旧库作者',0,'旧稿',1)"
        )
        conn.execute(
            "INSERT INTO reviews (id, session_id, reviewer_id, target_id, target_version, content, valid)"
            " VALUES (1,1,1,1,1,'旧意见',1)"
        )
        conn.commit()
        conn.close()

        original = app_module.DB_PATH
        app_module.DB_PATH = old_path
        try:
            # 模拟新版本启动：旧库自动补建问答表
            app_module.init_db()

            c = sqlite3.connect(old_path)
            try:
                tables = {
                    r[0]
                    for r in c.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
            finally:
                c.close()
            self.assertIn("clarifications", tables)
            self.assertIn("token_history", tables)
            self.assertIn("receipts", tables)
            self.assertIn("reports", tables)

            # 旧数据原样保留，且新表可直接读写
            client = new_client()
            data = client.get("/api/p/tk").get_json()
            self.assertEqual(data["name"], "旧库作者")
            self.assertEqual(data["received"], "旧意见")
            self.assertEqual(ask(client, "tk", "旧库追问").status_code, 200)

            # 旧库上同样可以换发链接：旧链接作废，新链接沿用稿件与意见、问答
            resp = client.post(
                "/api/h/ht/rotate-link", json={"name": "旧库作者", "link": "/p/tk"}
            )
            self.assertEqual(resp.status_code, 200, resp.get_json())
            new_tk = resp.get_json()["url"].rsplit("/", 1)[-1]
            self.assertNotEqual(new_tk, "tk")
            self.assertEqual(client.get("/api/p/tk").status_code, 404)
            data = client.get(f"/api/p/{new_tk}").get_json()
            self.assertEqual(data["name"], "旧库作者")
            self.assertEqual(data["received"], "旧意见")
            self.assertEqual(data["received_clarification"]["question"], "旧库追问")

            # 模拟重启：再次 init_db 幂等，问答内容保留，新链接仍有效、旧链接仍失效
            app_module.init_db()
            data = client.get(f"/api/p/{new_tk}").get_json()
            self.assertEqual(data["received_clarification"]["question"], "旧库追问")
            self.assertEqual(client.get("/api/p/tk").status_code, 404)
        finally:
            app_module.DB_PATH = original

    def test_old_db_supports_remove_after_auto_upgrade(self):
        """旧库补列并回填凭据后：可移除成员，环闭合、凭据换代；重启后状态保留。"""
        tmp_dir = tempfile.mkdtemp(prefix="review-olddb-remove-")
        old_path = os.path.join(tmp_dir, "old.db")
        conn = sqlite3.connect(old_path)
        conn.executescript(self.OLD_SCHEMA)
        conn.execute("INSERT INTO sessions (id, host_token, published) VALUES (1,'ht',0)")
        for i, n in enumerate(["甲", "乙", "丙", "丁"]):
            conn.execute(
                "INSERT INTO participants (id, session_id, token, name, position, manuscript, version)"
                " VALUES (?,?,?,?,?,?,1)",
                (i + 1, 1, "t" + n, n, i, "稿" + n),
            )
        conn.commit()
        conn.close()

        original = app_module.DB_PATH
        app_module.DB_PATH = old_path
        try:
            app_module.init_db()
            # 补列齐全且为既有成员回填了非空凭据
            c = sqlite3.connect(old_path)
            try:
                cols = {r[1] for r in c.execute("PRAGMA table_info(participants)")}
                empty_nonce = c.execute(
                    "SELECT COUNT(*) FROM participants WHERE assignment_nonce=''"
                ).fetchone()[0]
                gen_values = {r[0] for r in c.execute("SELECT assignment_gen FROM participants")}
            finally:
                c.close()
            self.assertTrue({"active", "assignment_nonce", "assignment_gen"} <= cols)
            self.assertEqual(empty_nonce, 0)
            self.assertEqual(gen_values, {0})

            client = new_client()
            # 旧库回填 gen=0：分配从未变过，不带凭据的旧提交格式兼容
            self.assertEqual(
                submit_review(client, "t甲", "甲评乙", 1).status_code, 200
            )
            # 旧库上直接支持移除：乙退出，甲改评丙并换代凭据
            self.assertEqual(remove_member(client, "ht", "乙").status_code, 200)
            self.assertEqual(client.get("/api/p/t乙").status_code, 404)
            s = client.get("/api/p/t甲").get_json()
            self.assertEqual(s["assignment"]["manuscript"], "稿丙")
            self.assertFalse(s["my_review"]["valid"])
            self.assertTrue(s["my_review"]["target_changed"])
            # 换代后无凭据/旧格式被 409
            self.assertEqual(submit_review(client, "t甲", "旧格式", 1).status_code, 409)
            self.assertEqual(
                submit_review(
                    client, "t甲", "甲评丙", 1, credential=s["assignment"]["credential"]
                ).status_code,
                200,
            )

            # 重启：补列幂等，乙仍 404、甲仍评丙且凭据不变，其他人链接保留
            app_module.init_db()
            self.assertEqual(client.get("/api/p/t乙").status_code, 404)
            s2 = client.get("/api/p/t甲").get_json()
            self.assertEqual(s2["assignment"]["manuscript"], "稿丙")
            self.assertEqual(s2["assignment"]["credential"], s["assignment"]["credential"])
            self.assertEqual(client.get("/api/p/t丙").status_code, 200)
            self.assertEqual(client.get("/api/p/t丁").status_code, 200)
        finally:
            app_module.DB_PATH = original


    def test_old_db_receipts_auto_supported(self):
        """旧库启动时自动补建回执表：既有意见初始为待确认，确认后重启保留。"""
        tmp_dir = tempfile.mkdtemp(prefix="review-olddb-receipt-")
        old_path = os.path.join(tmp_dir, "old.db")
        conn = sqlite3.connect(old_path)
        conn.executescript(self.OLD_SCHEMA)
        conn.execute("INSERT INTO sessions (id, host_token, published) VALUES (1,'ht',1)")
        conn.execute(
            "INSERT INTO participants (id, session_id, token, name, position, manuscript, version)"
            " VALUES (1,1,'tk','旧库作者',0,'旧稿',1)"
        )
        conn.execute(
            "INSERT INTO reviews (id, session_id, reviewer_id, target_id, target_version, content, valid)"
            " VALUES (1,1,1,1,1,'旧意见',1)"
        )
        conn.commit()
        conn.close()

        original = app_module.DB_PATH
        app_module.DB_PATH = old_path
        try:
            app_module.init_db()
            c = sqlite3.connect(old_path)
            try:
                tables = {
                    r[0]
                    for r in c.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
            finally:
                c.close()
            self.assertIn("receipts", tables)
            self.assertIn("reports", tables)

            client = new_client()
            # 旧库既有意见初始为未举报，且可直接举报、处理、屏蔽
            self.assertIsNone(client.get("/api/p/tk").get_json()["report"])
            report_resp = report_review(client, "tk", "旧库举报")
            self.assertEqual(report_resp.status_code, 200, report_resp.get_json())
            report_id = report_resp.get_json()["id"]
            self.assertEqual(
                decide_report(client, "ht", report_id, "block").status_code, 200
            )
            blocked_state = client.get("/api/p/tk").get_json()
            self.assertIsNone(blocked_state["received"])
            self.assertTrue(blocked_state["received_blocked"])
            self.assertEqual(blocked_state["report"]["status"], "blocked")

            # 既有意见初始为待确认
            data = client.get("/api/p/tk").get_json()
            self.assertEqual(
                data["receipt"], {"acknowledged": False, "acknowledged_at": None}
            )
            # 旧库上可直接确认，记录首次确认时间
            resp = ack(client, "tk")
            self.assertEqual(resp.status_code, 200, resp.get_json())
            first_at = resp.get_json()["acknowledged_at"]
            self.assertTrue(first_at)
            # 模拟重启：举报裁定与回执均保留、首次时间不变；重复确认仍成功且不改首次时间
            app_module.init_db()
            self.assertEqual(
                client.get("/api/p/tk").get_json()["report"]["status"], "blocked"
            )
            self.assertEqual(
                client.get("/api/p/tk").get_json()["receipt"],
                {"acknowledged": True, "acknowledged_at": first_at},
            )
            self.assertEqual(ack(client, "tk").get_json()["acknowledged_at"], first_at)
        finally:
            app_module.DB_PATH = original


    def test_old_db_closed_column_auto_added_unclosed_by_default(self):
        """旧库 sessions 缺 closed 列：启动自动补列，既有会话默认未封存；封存后重启保留。"""
        tmp_dir = tempfile.mkdtemp(prefix="review-olddb-close-")
        old_path = os.path.join(tmp_dir, "old.db")
        conn = sqlite3.connect(old_path)
        conn.executescript(self.OLD_SCHEMA)
        conn.execute("INSERT INTO sessions (id, host_token, published) VALUES (1,'ht',1)")
        conn.execute(
            "INSERT INTO participants (id, session_id, token, name, position, manuscript, version)"
            " VALUES (1,1,'tk','旧库作者',0,'旧稿',1)"
        )
        conn.execute(
            "INSERT INTO reviews (id, session_id, reviewer_id, target_id, target_version, content, valid)"
            " VALUES (1,1,1,1,1,'旧意见',1)"
        )
        conn.commit()
        conn.close()

        original = app_module.DB_PATH
        app_module.DB_PATH = old_path
        try:
            app_module.init_db()
            c = sqlite3.connect(old_path)
            try:
                cols = {r[1] for r in c.execute("PRAGMA table_info(sessions)")}
                closed_values = {r[0] for r in c.execute("SELECT closed FROM sessions")}
            finally:
                c.close()
            self.assertIn("closed", cols)
            self.assertEqual(closed_values, {0})  # 既有会话一律未封存

            client = new_client()
            self.assertFalse(client.get("/api/h/ht").get_json()["closed"])
            self.assertFalse(client.get("/api/p/tk").get_json()["closed"])
            # 未封存：旧库既有公布会话可直接封存
            self.assertEqual(close_session(client, "ht").status_code, 200)
            self.assertTrue(client.get("/api/h/ht").get_json()["closed"])
            # 重启：补列幂等，封存状态保留，仍只读
            app_module.init_db()
            self.assertTrue(client.get("/api/p/tk").get_json()["closed"])
            self.assertEqual(ack(client, "tk").status_code, 409)
            self.assertEqual(close_session(client, "ht").status_code, 409)
        finally:
            app_module.DB_PATH = original

    def test_old_db_anthology_auto_supported(self):
        """旧库启动时自动补建合辑表与授权列：既有成员默认未授权，可授权/发布，重启保留。"""
        tmp_dir = tempfile.mkdtemp(prefix="review-olddb-anthology-")
        old_path = os.path.join(tmp_dir, "old.db")
        conn = sqlite3.connect(old_path)
        conn.executescript(self.OLD_SCHEMA)
        conn.execute("INSERT INTO sessions (id, host_token, published) VALUES (1,'ht',1)")
        conn.execute(
            "INSERT INTO participants (id, session_id, token, name, position, manuscript, version)"
            " VALUES (1,1,'tk','旧库作者',0,'旧稿',1)"
        )
        conn.execute(
            "INSERT INTO reviews (id, session_id, reviewer_id, target_id, target_version, content, valid)"
            " VALUES (1,1,1,1,1,'旧意见',1)"
        )
        conn.commit()
        conn.close()

        original = app_module.DB_PATH
        app_module.DB_PATH = old_path
        try:
            app_module.init_db()
            c = sqlite3.connect(old_path)
            try:
                tables = {
                    r[0]
                    for r in c.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                cols = {r[1] for r in c.execute("PRAGMA table_info(participants)")}
                consent_values = {
                    r[0] for r in c.execute("SELECT anthology_consent FROM participants")
                }
            finally:
                c.close()
            self.assertIn("anthologies", tables)
            self.assertIn("anthology_items", tables)
            self.assertIn("anthology_consent", cols)
            self.assertEqual(consent_values, {0})  # 既有成员一律默认未授权

            client = new_client()
            # 旧库上直接授权/发布：参与状态（本人）与主持状态（各人）同快照返回授权
            self.assertIs(client.get("/api/p/tk").get_json()["anthology_consent"], False)
            self.assertEqual(set_consent(client, "tk", True).status_code, 200)
            resp = publish_anthology(client, "ht", ["旧库作者"])
            self.assertEqual(resp.status_code, 200, resp.get_json())
            pub = resp.get_json()["url"].rsplit("/", 1)[-1]
            self.assertEqual(
                client.get(f"/api/a/{pub}").get_json(),
                {"items": [{"number": 1, "manuscript": "旧稿"}]},
            )
            # 模拟重启：合辑、授权与公开链接均保留，冻结状态不变
            app_module.init_db()
            self.assertEqual(
                client.get(f"/api/a/{pub}").get_json()["items"][0]["manuscript"], "旧稿"
            )
            self.assertIs(client.get("/api/p/tk").get_json()["anthology_consent"], True)
            self.assertEqual(
                client.get("/api/h/ht").get_json()["anthology_url"], f"/a/{pub}"
            )
            self.assertEqual(set_consent(client, "tk", False).status_code, 409)
            self.assertEqual(publish_anthology(client, "ht", ["旧库作者"]).status_code, 409)
        finally:
            app_module.DB_PATH = original


if __name__ == "__main__":
    prog = unittest.main(verbosity=2, exit=False)
    ok = prog.result.wasSuccessful()
    # 主持页前端回归（勾选随轮询保留）：本机有 node 时一并执行，
    # 也可单独运行 node selftest_host.js
    node = shutil.which("node")
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "selftest_host.js")
    if node:
        print("\n--- 主持页前端回归：node selftest_host.js ---", flush=True)
        ok = subprocess.run([node, script]).returncode == 0 and ok
    else:
        print("\n未找到 node，跳过主持页前端回归（可单独运行：node selftest_host.js）")
    sys.exit(0 if ok else 1)
