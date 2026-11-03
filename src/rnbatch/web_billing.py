"""Prepaid usage accounting for the authenticated rnbatch Web App.

This module deliberately does NOT charge cards or accept browser payment success.
A trusted operator may confirm an externally verified payment via the CLI.
All prices are illustrative and checkout is disabled until a real provider,
merchant account, privacy/security review and refund policy are configured.
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

FREE_FILES_PER_MONTH = 20

# Snapshot order economics at creation; never accept amount/credits from HTTP.
PACKAGES = {
    "pack100": {"title": "100 文件额度包", "price_cents": 990, "credits": 100},
    "pack500": {"title": "500 文件额度包", "price_cents": 3900, "credits": 500},
}
PROPOSAL = {"title": "专业版（月订阅，规划中）", "price_cents": 2900,
            "credits": 1000, "enabled": False}


class QuotaExceeded(ValueError):
    """There are fewer available processing credits than renamed files."""


class BillingStore:
    def __init__(self, database: str | Path):
        self.path = Path(database).expanduser()
        if not self.path.is_file():
            raise ValueError("请先初始化 Web App 用户数据库")
        self._lock = threading.RLock()
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS billing_wallets (
                    user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                    purchased_balance INTEGER NOT NULL DEFAULT 0
                        CHECK(purchased_balance >= 0)
                );
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
                CREATE TABLE IF NOT EXISTS billing_orders (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    sku TEXT NOT NULL,
                    price_cents INTEGER NOT NULL CHECK(price_cents > 0),
                    credits INTEGER NOT NULL CHECK(credits > 0),
                    status TEXT NOT NULL CHECK(status IN ('pending','fulfilled')),
                    payment_reference TEXT UNIQUE,
                    created_at INTEGER NOT NULL,
                    fulfilled_at INTEGER
                );
                CREATE INDEX IF NOT EXISTS idx_billing_orders_user
                    ON billing_orders(user_id, created_at);
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
            "packages": [
                {"sku": sku, **values} for sku, values in PACKAGES.items()
            ],
            "proposed_subscription": PROPOSAL,
            "notice": "仅为价格方案；未接入支付，无法在线付款。",
        }

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

    def status(self, user_id: str) -> dict:
        period = self._period()
        with self._connect() as db:
            self._assert_user(db, user_id)
            free_used = self._usage(db, user_id, period)
            wallet = db.execute(
                "SELECT purchased_balance FROM billing_wallets WHERE user_id=?",
                (user_id,)
            ).fetchone()
        paid = int(wallet[0]) if wallet else 0
        free_remaining = max(0, FREE_FILES_PER_MONTH - free_used)
        return {"ok": True, "period": period, "free_limit": FREE_FILES_PER_MONTH,
                "free_used": free_used, "free_remaining": free_remaining,
                "paid_balance": paid, "available": free_remaining + paid,
                "currency": "CNY", "checkout_enabled": False}

    def reserve(self, user_id: str, workspace_id: str, count: int) -> dict:
        """Atomic and durable reservation BEFORE a filesystem rename.

        Pending reservations consume quota, so parallel requests cannot
        overspend. release() restores paid credits on a failed rename.
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
            free_remaining = max(0, FREE_FILES_PER_MONTH - self._usage(db, user_id, period))
            free_units = min(free_remaining, count)
            paid_units = count - free_units
            row = db.execute(
                "SELECT purchased_balance FROM billing_wallets WHERE user_id=?",
                (user_id,)
            ).fetchone()
            paid_balance = int(row[0]) if row else 0
            if paid_units > paid_balance:
                raise QuotaExceeded("额度不足：请减少本次改名文件数或等待下月免费额度刷新")
            if paid_units:
                db.execute("""UPDATE billing_wallets SET purchased_balance =
                              purchased_balance - ? WHERE user_id=?""",
                           (paid_units, user_id))
            db.execute("""INSERT INTO billing_usage
                       (workspace_id,user_id,period,free_units,paid_units,state,created_at)
                       VALUES (?,?,?,?,?,'reserved',?)""",
                       (workspace_id, user_id, period, free_units, paid_units, int(time.time())))
        return {"free_units": free_units, "paid_units": paid_units, "renamed": count}

    def settle(self, user_id: str, workspace_id: str) -> None:
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            cursor = db.execute("""UPDATE billing_usage SET state='settled'
                    WHERE user_id=? AND workspace_id=? AND state='reserved'""",
                    (user_id, workspace_id))
            if cursor.rowcount != 1:
                raise ValueError("未找到待结算的额度预留")

    def release(self, user_id: str, workspace_id: str) -> None:
        """Refund ONLY an uncommitted reservation. Never refund settled use."""
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""SELECT paid_units FROM billing_usage
                    WHERE user_id=? AND workspace_id=? AND state='reserved'""",
                    (user_id, workspace_id)).fetchone()
            if row is None:
                return
            db.execute("DELETE FROM billing_usage WHERE user_id=? AND workspace_id=? AND state='reserved'",
                       (user_id, workspace_id))
            if row[0]:
                db.execute("""INSERT INTO billing_wallets(user_id,purchased_balance)
                    VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET
                    purchased_balance=purchased_balance+excluded.purchased_balance""",
                    (user_id, int(row[0])))

    def create_order(self, user_id: str, sku: str) -> dict:
        """Create an unpayable draft order; the browser cannot fulfill it."""
        if not isinstance(sku, str) or sku not in PACKAGES:
            raise ValueError("不存在该额度套餐")
        pack = PACKAGES[sku]
        ident = uuid.uuid4().hex
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._assert_user(db, user_id)
            pending = db.execute("""SELECT COUNT(*) FROM billing_orders
                       WHERE user_id=? AND status='pending'""", (user_id,)).fetchone()[0]
            if pending >= 5:
                raise ValueError("待处理订单过多，请联系管理员")
            db.execute("""INSERT INTO billing_orders
                (id,user_id,sku,price_cents,credits,status,created_at)
                VALUES (?,?,?,?,?,'pending',?)""",
                (ident, user_id, sku, pack["price_cents"], pack["credits"], int(time.time())))
        return {"ok": True, "id": ident, "sku": sku,
                "price_cents": pack["price_cents"], "credits": pack["credits"],
                "status": "pending", "checkout_url": None,
                "notice": "尚未接入支付；订单仅供内部商业化测试，不应汇款。"}

    def orders(self, user_id: str, limit: int = 20) -> list[dict]:
        with self._connect() as db:
            self._assert_user(db, user_id)
            rows = db.execute("""SELECT id,sku,price_cents,credits,status,created_at,fulfilled_at
                FROM billing_orders WHERE user_id=?
                ORDER BY created_at DESC,id DESC LIMIT ?""", (user_id, limit)).fetchall()
        return [dict(row) for row in rows]

    def confirm_offline(self, order_id: str, payment_reference: str) -> dict:
        """ADMIN CLI ONLY. Call AFTER independently verifying cleared payment.

        This operation is intentionally not reachable through any HTTP route.
        """
        if not isinstance(order_id, str) or not re.fullmatch(r"[a-f0-9]{32}", order_id):
            raise ValueError("订单编号不正确")
        if not isinstance(payment_reference, str) or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}", payment_reference):
            raise ValueError("外部支付凭证编号格式不正确")
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""SELECT user_id,credits,status,payment_reference
                FROM billing_orders WHERE id=?""", (order_id,)).fetchone()
            if row is None:
                raise ValueError("订单不存在")
            if row["status"] == "fulfilled":
                if row["payment_reference"] != payment_reference:
                    raise ValueError("订单已通过不同支付凭证核销")
                return {"ok": True, "already_fulfilled": True, "credits": int(row["credits"])}
            try:
                cursor = db.execute("""UPDATE billing_orders SET status='fulfilled',
                    payment_reference=?,fulfilled_at=? WHERE id=? AND status='pending'""",
                    (payment_reference, int(time.time()), order_id))
            except sqlite3.IntegrityError as exc:
                raise ValueError("该外部支付凭证已核销其他订单") from exc
            if cursor.rowcount != 1:
                raise ValueError("订单状态已变化")
            db.execute("""INSERT INTO billing_wallets(user_id,purchased_balance)
                VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET
                purchased_balance=purchased_balance+excluded.purchased_balance""",
                (row["user_id"], int(row["credits"])))
        return {"ok": True, "already_fulfilled": False, "credits": int(row["credits"])}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="rnbatch billing: manual verification only")
    parser.add_argument("--db", required=True, help="已有的 users.sqlite3 路径")
    sub = parser.add_subparsers(dest="cmd", required=True)
    confirm = sub.add_parser("confirm-offline",
                             help="仅在人工独立核验真实到账后执行")
    confirm.add_argument("--order", required=True)
    confirm.add_argument("--payment-reference", required=True)
    args = parser.parse_args(argv)
    if args.cmd == "confirm-offline":
        result = BillingStore(args.db).confirm_offline(
            args.order, args.payment_reference)
        print("已处理订单；增加额度:", result["credits"],
              " (已处理过)" if result["already_fulfilled"] else "")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
