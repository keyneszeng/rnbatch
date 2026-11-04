"""Subscription accounting for the authenticated rnbatch Web App.

The free tier stays a monthly file allowance. Paid access is a subscription:
while it is active, renaming is not metered at all.

This module still does NOT charge cards or accept a browser's claim of payment.
`confirm_offline` is the only way to activate a subscription, and it is a CLI
operation for a trusted operator after independently verifying the money
arrived. Wiring a real payment provider (WeChat virtual payment, Stripe, …)
means calling `create_order` and then `fulfill_order` from that provider's
verified callback — never from the browser.
"""
from __future__ import annotations

import argparse
import re
import secrets
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

FREE_FILES_PER_MONTH = 20

# 价格快照在创建订单时写入，绝不接受 HTTP 传来的金额。
PLANS = {
    "member_monthly": {"title": "月度会员", "price_cents": 990, "days": 30},
    "member_yearly": {"title": "年度会员", "price_cents": 9900, "days": 365},
}

# 兑换码字母表：去掉 0/O、1/I/L、U 这些手抄容易看错的字符
CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"
CODE_LENGTH = 12
CODE_GROUP = 4


def format_code(code: str) -> str:
    """数据库里不带分隔符，展示成 XXXX-XXXX-XXXX，方便手抄和口述。"""
    return "-".join(code[i:i + CODE_GROUP] for i in range(0, len(code), CODE_GROUP))


def normalize_code(raw: str) -> str:
    """把用户输入的兑换码收敛成存储形态；非法字符直接报错，不静默丢弃。"""
    if not isinstance(raw, str):
        raise ValueError("兑换码格式不正确")
    cleaned = re.sub(r"[^0-9A-Za-z]", "", raw).upper()
    if len(cleaned) != CODE_LENGTH:
        raise ValueError("兑换码应为 %d 位" % CODE_LENGTH)
    if any(ch not in CODE_ALPHABET for ch in cleaned):
        raise ValueError("兑换码包含无效字符")
    return cleaned


class QuotaExceeded(ValueError):
    """免费额度不足以覆盖本次改名，且没有生效中的订阅。"""


class BillingStore:
    def __init__(self, database: str | Path):
        self.path = Path(database).expanduser()
        if not self.path.is_file():
            raise ValueError("请先初始化 Web App 用户数据库")
        self._lock = threading.RLock()
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS billing_usage (
                    workspace_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    period TEXT NOT NULL,
                    free_units INTEGER NOT NULL CHECK(free_units >= 0),
                    paid_units INTEGER NOT NULL CHECK(paid_units >= 0),
                    state TEXT NOT NULL CHECK(state IN ('reserved','settled')),
                    created_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_billing_usage_user_period
                    ON billing_usage(user_id, period);
                CREATE TABLE IF NOT EXISTS billing_subscriptions (
                    user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                    plan TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS billing_orders (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    sku TEXT NOT NULL,
                    price_cents INTEGER NOT NULL CHECK(price_cents > 0),
                    days INTEGER NOT NULL CHECK(days > 0),
                    status TEXT NOT NULL CHECK(status IN ('pending','fulfilled')),
                    payment_reference TEXT UNIQUE,
                    created_at INTEGER NOT NULL,
                    fulfilled_at INTEGER
                );
                CREATE INDEX IF NOT EXISTS idx_billing_orders_user
                    ON billing_orders(user_id, created_at);
                CREATE TABLE IF NOT EXISTS billing_gift_codes (
                    code TEXT PRIMARY KEY,
                    plan TEXT NOT NULL,
                    days INTEGER NOT NULL CHECK(days > 0),
                    note TEXT,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER,
                    redeemed_by TEXT REFERENCES users(id) ON DELETE SET NULL,
                    redeemed_at INTEGER
                );
                CREATE INDEX IF NOT EXISTS idx_gift_codes_redeemed
                    ON billing_gift_codes(redeemed_by);
            """)

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(str(self.path), timeout=15)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _period() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m")

    @staticmethod
    def catalog() -> dict:
        return {
            "currency": "CNY",
            "free_files_per_month": FREE_FILES_PER_MONTH,
            "checkout_enabled": False,
            "plans": [{"sku": sku, **values} for sku, values in PLANS.items()],
            "notice": "订阅期内不限改名次数；支付通道尚未接入，当前无法在线购买。",
        }

    # ------------------------------------------------------------ 内部
    @staticmethod
    def _assert_user(db, user_id: str):
        if not isinstance(user_id, str) or not db.execute(
            "SELECT 1 FROM users WHERE id=?", (user_id,)
        ).fetchone():
            raise ValueError("用户不存在")

    def _usage(self, db, user_id: str, period: str) -> int:
        return int(db.execute("""
            SELECT COALESCE(SUM(free_units),0) FROM billing_usage
            WHERE user_id=? AND period=?
        """, (user_id, period)).fetchone()[0])

    def _subscription(self, db, user_id: str) -> dict | None:
        row = db.execute(
            "SELECT plan, expires_at FROM billing_subscriptions WHERE user_id=?",
            (user_id,)
        ).fetchone()
        if row is None or int(row["expires_at"]) <= int(time.time()):
            return None
        return {"plan": row["plan"], "expires_at": int(row["expires_at"])}

    # ------------------------------------------------------------ 查询
    def status(self, user_id: str) -> dict:
        period = self._period()
        with self._connect() as db:
            self._assert_user(db, user_id)
            free_used = self._usage(db, user_id, period)
            subscription = self._subscription(db, user_id)
        free_remaining = max(0, FREE_FILES_PER_MONTH - free_used)
        active = subscription is not None
        return {
            "ok": True,
            "period": period,
            "free_limit": FREE_FILES_PER_MONTH,
            "free_used": free_used,
            "free_remaining": free_remaining,
            "member": active,
            "member_plan": subscription["plan"] if active else None,
            "member_until": subscription["expires_at"] if active else None,
            "unlimited": active,
            "available": None if active else free_remaining,
            "currency": "CNY",
            "checkout_enabled": False,
        }

    # ------------------------------------------------------------ 计量
    def reserve(self, user_id: str, workspace_id: str, count: int) -> dict:
        """改名落盘之前先占用额度；订阅生效时不计量。

        待结算的预留同样计入已用额度，所以并发请求不会超支。
        改名失败时由 release() 归还。
        """
        if not isinstance(workspace_id, str) or not workspace_id or len(workspace_id) > 128:
            raise ValueError("工作区 ID 无效")
        if type(count) is not int or count <= 0 or count > 200:
            raise ValueError("改名文件数量无效")
        period = self._period()
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._assert_user(db, user_id)
            if db.execute("SELECT 1 FROM billing_usage WHERE workspace_id=?",
                          (workspace_id,)).fetchone():
                raise ValueError("该工作区的执行额度已使用，请创建新工作区")
            if self._subscription(db, user_id) is not None:
                free_units = 0
            else:
                free_remaining = max(0, FREE_FILES_PER_MONTH - self._usage(db, user_id, period))
                if free_remaining < count:
                    raise QuotaExceeded(
                        "免费额度不足：本月剩余 %d 个，本次需要 %d 个。"
                        "可以等额度刷新，或开通会员后不限次数。"
                        % (free_remaining, count))
                free_units = count
            db.execute("""INSERT INTO billing_usage
                       (workspace_id,user_id,period,free_units,paid_units,state,created_at)
                       VALUES (?,?,?,?,0,'reserved',?)""",
                       (workspace_id, user_id, period, free_units, int(time.time())))
        return {"free_units": free_units, "paid_units": 0, "renamed": count}

    def settle(self, user_id: str, workspace_id: str) -> None:
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            cursor = db.execute("""UPDATE billing_usage SET state='settled'
                    WHERE user_id=? AND workspace_id=? AND state='reserved'""",
                    (user_id, workspace_id))
            if cursor.rowcount != 1:
                raise ValueError("未找到待结算的额度预留")

    def release(self, user_id: str, workspace_id: str) -> None:
        """只退还尚未结算的预留；已结算的使用不退。"""
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""DELETE FROM billing_usage
                    WHERE user_id=? AND workspace_id=? AND state='reserved'""",
                    (user_id, workspace_id))

    # ------------------------------------------------------------ 订单
    def create_order(self, user_id: str, sku: str) -> dict:
        """创建待支付订单；浏览器无法自行把它变成已支付。"""
        if not isinstance(sku, str) or sku not in PLANS:
            raise ValueError("不存在该订阅套餐")
        plan = PLANS[sku]
        ident = uuid.uuid4().hex
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._assert_user(db, user_id)
            pending = db.execute("""SELECT COUNT(*) FROM billing_orders
                       WHERE user_id=? AND status='pending'""", (user_id,)).fetchone()[0]
            if pending >= 5:
                raise ValueError("待处理订单过多，请稍后再试")
            db.execute("""INSERT INTO billing_orders
                (id,user_id,sku,price_cents,days,status,created_at)
                VALUES (?,?,?,?,?,'pending',?)""",
                (ident, user_id, sku, plan["price_cents"], plan["days"], int(time.time())))
        return {"ok": True, "id": ident, "sku": sku,
                "price_cents": plan["price_cents"], "days": plan["days"],
                "status": "pending", "checkout_url": None,
                "notice": "支付通道尚未接入，订单暂时无法支付。"}

    def orders(self, user_id: str, limit: int = 20) -> list[dict]:
        with self._connect() as db:
            self._assert_user(db, user_id)
            rows = db.execute("""SELECT id,sku,price_cents,days,status,created_at,fulfilled_at
                FROM billing_orders WHERE user_id=?
                ORDER BY created_at DESC,id DESC LIMIT ?""", (user_id, limit)).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------ 订阅延期
    @staticmethod
    def _extend_subscription(db, user_id: str, plan: str, days: int) -> int:
        """把订阅到期时间往后延，返回新的到期时间（UTC 秒）。

        续费从现有到期时间起算，不吞掉剩余天数；已过期则从当前时间起算。
        """
        now = int(time.time())
        current = db.execute(
            "SELECT expires_at FROM billing_subscriptions WHERE user_id=?",
            (user_id,)).fetchone()
        base = max(now, int(current[0])) if current else now
        expires = base + days * 86400
        db.execute("""INSERT INTO billing_subscriptions
            (user_id,plan,expires_at,updated_at) VALUES (?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
            plan=excluded.plan, expires_at=excluded.expires_at,
            updated_at=excluded.updated_at""",
            (user_id, plan, expires, now))
        return expires

    def fulfill_order(self, order_id: str, payment_reference: str) -> dict:
        """订单履约：把订阅到期时间往后延。

        只能由可信来源调用——现在走 CLI，将来接支付回调时，必须是
        provider 验签通过之后的那一步。
        """
        if not isinstance(order_id, str) or not re.fullmatch(r"[a-f0-9]{32}", order_id):
            raise ValueError("订单编号不正确")
        if not isinstance(payment_reference, str) or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}", payment_reference):
            raise ValueError("外部支付凭证编号格式不正确")
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""SELECT user_id,sku,days,status,payment_reference
                FROM billing_orders WHERE id=?""", (order_id,)).fetchone()
            if row is None:
                raise ValueError("订单不存在")
            if row["status"] == "fulfilled":
                if row["payment_reference"] != payment_reference:
                    raise ValueError("订单已通过不同支付凭证核销")
                return {"ok": True, "already_fulfilled": True,
                        "plan": row["sku"], "days": int(row["days"])}
            try:
                cursor = db.execute("""UPDATE billing_orders SET status='fulfilled',
                    payment_reference=?,fulfilled_at=? WHERE id=? AND status='pending'""",
                    (payment_reference, int(time.time()), order_id))
            except sqlite3.IntegrityError as exc:
                raise ValueError("该外部支付凭证已核销其他订单") from exc
            if cursor.rowcount != 1:
                raise ValueError("订单状态已变化")

            days = int(row["days"])
            expires = self._extend_subscription(db, row["user_id"], row["sku"], days)
        return {"ok": True, "already_fulfilled": False,
                "plan": row["sku"], "days": days, "expires_at": expires}

    # ------------------------------------------------------------ 赠送兑换码
    def create_gift_codes(self, plan: str, count: int = 1, note: str = "",
                          expires_days: int | None = None) -> list[dict]:
        """生成赠送用的兑换码。只有服务端能调用（CLI），没有对应 HTTP 路由。"""
        if plan not in PLANS:
            raise ValueError("不存在该订阅套餐")
        if type(count) is not int or not (1 <= count <= 100):
            raise ValueError("一次最多生成 100 个兑换码")
        if expires_days is not None and (type(expires_days) is not int or expires_days <= 0):
            raise ValueError("兑换码有效期必须是正整数天")
        days = PLANS[plan]["days"]
        now = int(time.time())
        expires_at = now + expires_days * 86400 if expires_days else None

        out: list[dict] = []
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            while len(out) < count:
                code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
                try:
                    db.execute("""INSERT INTO billing_gift_codes
                        (code,plan,days,note,created_at,expires_at)
                        VALUES (?,?,?,?,?,?)""",
                        (code, plan, days, note or None, now, expires_at))
                except sqlite3.IntegrityError:
                    continue          # 撞码，换一个
                out.append({"code": format_code(code), "plan": plan,
                            "days": days, "expires_at": expires_at})
        return out

    def redeem_gift(self, user_id: str, raw_code: str) -> dict:
        """兑换：核销一次性兑换码，并把订阅往后延。"""
        code = normalize_code(raw_code)
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._assert_user(db, user_id)
            row = db.execute("""SELECT plan,days,expires_at,redeemed_by
                FROM billing_gift_codes WHERE code=?""", (code,)).fetchone()
            if row is None:
                raise ValueError("兑换码不存在")
            if row["redeemed_by"]:
                raise ValueError("这个兑换码已经被用过了")
            if row["expires_at"] and int(row["expires_at"]) < int(time.time()):
                raise ValueError("兑换码已过期")
            cursor = db.execute("""UPDATE billing_gift_codes
                SET redeemed_by=?, redeemed_at=?
                WHERE code=? AND redeemed_by IS NULL""",
                (user_id, int(time.time()), code))
            if cursor.rowcount != 1:
                raise ValueError("这个兑换码已经被用过了")
            expires = self._extend_subscription(db, user_id, row["plan"], int(row["days"]))
        return {"ok": True, "plan": row["plan"],
                "days": int(row["days"]), "expires_at": expires}

    def list_gift_codes(self, limit: int = 50) -> list[dict]:
        with self._connect() as db:
            rows = db.execute("""SELECT code,plan,days,note,created_at,expires_at,
                       redeemed_by,redeemed_at
                FROM billing_gift_codes ORDER BY created_at DESC LIMIT ?""",
                (limit,)).fetchall()
        return [dict(row) for row in rows]

    # 兼容旧调用点与 CLI 措辞
    confirm_offline = fulfill_order


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="rnbatch billing: manual verification only")
    parser.add_argument("--db", required=True, help="已有的 users.sqlite3 路径")
    sub = parser.add_subparsers(dest="cmd", required=True)
    confirm = sub.add_parser("confirm-offline",
                             help="仅在人工独立核验真实到账后执行")
    confirm.add_argument("--order", required=True)
    confirm.add_argument("--payment-reference", required=True)

    gift = sub.add_parser("gift-create", help="生成赠送用的会员兑换码")
    gift.add_argument("--plan", required=True, choices=sorted(PLANS))
    gift.add_argument("--count", type=int, default=1, help="生成数量，默认 1，最多 100")
    gift.add_argument("--note", default="", help="备注，例如送给谁；仅内部可见")
    gift.add_argument("--expires-days", type=int, default=None,
                      help="兑换码自身的有效期（天），不填则一直有效")

    listing = sub.add_parser("gift-list", help="查看兑换码与核销状态")
    listing.add_argument("--limit", type=int, default=50)

    args = parser.parse_args(argv)
    store = BillingStore(args.db)

    if args.cmd == "confirm-offline":
        result = store.confirm_offline(args.order, args.payment_reference)
        print("已履约；套餐:", result["plan"], "天数:", result["days"],
              " (已处理过)" if result["already_fulfilled"] else "")
        return 0

    if args.cmd == "gift-create":
        codes = store.create_gift_codes(args.plan, args.count, args.note,
                                        args.expires_days)
        title = PLANS[args.plan]["title"]
        print("%s兑换码（%d 天）：" % (title, codes[0]["days"]))
        for item in codes:
            print("  " + item["code"])
        if args.expires_days:
            print("兑换码本身 %d 天内有效。" % args.expires_days)
        print("把码发给对方，让对方在小程序「会员」里点「兑换码」输入即可。")
        return 0

    if args.cmd == "gift-list":
        rows = store.list_gift_codes(args.limit)
        if not rows:
            print("还没有生成过兑换码。")
            return 0
        for row in rows:
            state = "已兑换" if row["redeemed_by"] else "未兑换"
            print("%s  %-14s %3d天  %s  %s" % (
                format_code(row["code"]), row["plan"], row["days"], state,
                row["note"] or ""))
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
