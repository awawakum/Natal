"""Тарифы, платёжные счета и начисление запросов в SQLite."""
from __future__ import annotations

import base64
import hashlib
import hmac
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiohttp

from config import settings


@dataclass(frozen=True)
class Package:
    id: str
    title: str
    requests: int
    rubles: int
    stars: int
    crypto_usdt: str


PACKAGES = {
    "trial": Package("trial", "Пробный", 5, 149, 150, "2.00"),
    "standard": Package("standard", "Стандарт", 20, 399, 400, "5.00"),
    "maximum": Package("maximum", "Максимальный", 60, 899, 900, "11.00"),
}

def _connect() -> sqlite3.Connection:
    database_path = Path(settings.payments_db_path)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path, timeout=10)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS balances (
            user_id INTEGER PRIMARY KEY,
            requests INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS payments (
            external_id TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            package_id TEXT NOT NULL,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            credited INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS daily_usage (
            user_id INTEGER NOT NULL,
            usage_date TEXT NOT NULL,
            free_requests INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, usage_date)
        );
        CREATE TABLE IF NOT EXISTS invalid_media_attempts (
            user_id INTEGER PRIMARY KEY,
            attempts INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS user_accounts (
            user_id INTEGER PRIMARY KEY,
            welcome_requests INTEGER NOT NULL,
            blocked_until TEXT
        );
        CREATE TABLE IF NOT EXISTS referral_rewards (
            invited_user_id INTEGER PRIMARY KEY,
            inviter_user_id INTEGER NOT NULL,
            requests INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        INSERT OR IGNORE INTO user_accounts(user_id, welcome_requests)
        SELECT user_id, 0 FROM daily_usage;
        INSERT OR IGNORE INTO user_accounts(user_id, welcome_requests)
        SELECT user_id, 0 FROM balances;
        INSERT OR IGNORE INTO user_accounts(user_id, welcome_requests)
        SELECT user_id, 0 FROM payments;
        """
    )
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(payments)")
    }
    for column in ("username", "first_name", "last_name"):
        if column not in columns:
            connection.execute(f"ALTER TABLE payments ADD COLUMN {column} TEXT")
    return connection


def get_package(package_id: str) -> Package | None:
    return PACKAGES.get(package_id)


def add_requests(user_id: int, package_id: str) -> int:
    package = PACKAGES[package_id]
    with _connect() as connection:
        connection.execute(
            "INSERT INTO balances(user_id, requests) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET requests = requests + excluded.requests",
            (user_id, package.requests),
        )
        row = connection.execute(
            "SELECT requests FROM balances WHERE user_id = ?", (user_id,)
        ).fetchone()
    return int(row[0])


def paid_requests_left(user_id: int) -> int:
    with _connect() as connection:
        row = connection.execute(
            "SELECT requests FROM balances WHERE user_id = ?", (user_id,)
        ).fetchone()
    return int(row[0]) if row else 0


def consume_paid_request(user_id: int) -> bool:
    with _connect() as connection:
        cursor = connection.execute(
            "UPDATE balances SET requests = requests - 1 "
            "WHERE user_id = ? AND requests > 0",
            (user_id,),
        )
    return cursor.rowcount == 1


def register_invalid_media_attempt(user_id: int) -> int:
    with _connect() as connection:
        connection.execute(
            "INSERT INTO invalid_media_attempts(user_id, attempts) VALUES (?, 1) "
            "ON CONFLICT(user_id) DO UPDATE SET attempts = attempts + 1, updated_at = CURRENT_TIMESTAMP",
            (user_id,),
        )
        row = connection.execute(
            "SELECT attempts FROM invalid_media_attempts WHERE user_id = ?", (user_id,)
        ).fetchone()
    return int(row[0])


def clear_invalid_media_attempts(user_id: int) -> None:
    with _connect() as connection:
        connection.execute(
            "DELETE FROM invalid_media_attempts WHERE user_id = ?", (user_id,)
        )


def consume_request(
    user_id: int,
    daily_limit: int,
    welcome_limit: int = settings.new_user_requests,
) -> str | None:
    with _connect() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO user_accounts(user_id, welcome_requests) VALUES (?, ?)",
            (user_id, welcome_limit),
        )
        cursor = connection.execute(
            "UPDATE user_accounts SET welcome_requests = welcome_requests - 1 "
            "WHERE user_id = ? AND welcome_requests > 0",
            (user_id,),
        )
    if cursor.rowcount == 1:
        return "welcome"
    if consume_free_request(user_id, daily_limit):
        return "free"
    if consume_paid_request(user_id):
        return "paid"
    return None


def free_requests_used_today(user_id: int) -> int:
    with _connect() as connection:
        row = connection.execute(
            "SELECT free_requests FROM daily_usage WHERE user_id = ? AND usage_date = ?",
            (user_id, date.today().isoformat()),
        ).fetchone()
    return int(row[0]) if row else 0


def welcome_requests_left(user_id: int) -> int:
    with _connect() as connection:
        row = connection.execute(
            "SELECT welcome_requests FROM user_accounts WHERE user_id = ?", (user_id,)
        ).fetchone()
    return int(row[0]) if row else settings.new_user_requests


def available_requests_left(
    user_id: int,
    daily_limit: int,
    welcome_limit: int = settings.new_user_requests,
) -> int:
    """Return all requests currently available to the user."""
    return (
        welcome_requests_left(user_id)
        + max(0, daily_limit - free_requests_used_today(user_id))
        + paid_requests_left(user_id)
    )


def block_user(user_id: int, hours: int) -> None:
    blocked_until = datetime.now(timezone.utc) + timedelta(hours=hours)
    with _connect() as connection:
        connection.execute(
            "INSERT INTO user_accounts(user_id, welcome_requests, blocked_until) VALUES (?, 0, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET blocked_until = excluded.blocked_until",
            (user_id, blocked_until.isoformat()),
        )


def blocked_until(user_id: int) -> datetime | None:
    with _connect() as connection:
        row = connection.execute(
            "SELECT blocked_until FROM user_accounts WHERE user_id = ?", (user_id,)
        ).fetchone()
    if not row or not row[0]:
        return None
    blocked_time = datetime.fromisoformat(row[0])
    if blocked_time <= datetime.now(timezone.utc):
        with _connect() as connection:
            connection.execute(
                "UPDATE user_accounts SET blocked_until = NULL WHERE user_id = ?",
                (user_id,),
            )
        return None
    return blocked_time


def consume_free_request(user_id: int, daily_limit: int) -> bool:
    today = date.today().isoformat()
    with _connect() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO daily_usage(user_id, usage_date) VALUES (?, ?)",
            (user_id, today),
        )
        cursor = connection.execute(
            "UPDATE daily_usage SET free_requests = free_requests + 1 "
            "WHERE user_id = ? AND usage_date = ? AND free_requests < ?",
            (user_id, today, daily_limit),
        )
    return cursor.rowcount == 1


def remember_payment(
    provider: str,
    user_id: int,
    package_id: str,
    external_id: str,
    username: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
) -> None:
    with _connect() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO payments("
            "external_id, provider, user_id, package_id, username, first_name, last_name) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (external_id, provider, user_id, package_id, username, first_name, last_name),
        )


def credit_payment(external_id: str) -> bool:
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        payment = connection.execute(
            "SELECT user_id, package_id FROM payments "
            "WHERE external_id = ? AND credited = 0",
            (external_id,),
        ).fetchone()
        if payment is None:
            return False
        if payment[1] not in PACKAGES:
            return False
        package = PACKAGES[payment[1]]
        connection.execute(
            "INSERT INTO balances(user_id, requests) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET requests = requests + excluded.requests",
            (payment[0], package.requests),
        )
        connection.execute(
            "UPDATE payments SET credited = 1 WHERE external_id = ? AND credited = 0",
            (external_id,),
        )
    return True


def payment_recipient(external_id: str) -> tuple[int, str] | None:
    with _connect() as connection:
        payment = connection.execute(
            "SELECT user_id, package_id FROM payments WHERE external_id = ?",
            (external_id,),
        ).fetchone()
    if payment is None:
        return None
    return int(payment[0]), str(payment[1])


def payment_details(external_id: str) -> tuple[str, int, str, str | None, str | None, str | None] | None:
    with _connect() as connection:
        payment = connection.execute(
            "SELECT provider, user_id, package_id, username, first_name, last_name FROM payments "
            "WHERE external_id = ? AND credited = 1",
            (external_id,),
        ).fetchone()
    if payment is None:
        return None
    return (
        str(payment[0]),
        int(payment[1]),
        str(payment[2]),
        payment[3],
        payment[4],
        payment[5],
    )


def topup_summary() -> dict[str, tuple[int, int]]:
    with _connect() as connection:
        rows = connection.execute(
            "SELECT created_at, package_id FROM payments WHERE credited = 1"
        ).fetchall()
    summary = {
        "all": [0, 0],
        "week": [0, 0],
        "month": [0, 0],
    }
    current_month = date.today().replace(day=1).isoformat()
    week_start = (date.today() - timedelta(days=7)).isoformat()
    for created_at, package_id in rows:
        package = PACKAGES.get(package_id)
        if package is None:
            continue
        summary["all"][0] += 1
        summary["all"][1] += package.requests
        created_date = str(created_at)[:10]
        if created_date >= week_start:
            summary["week"][0] += 1
            summary["week"][1] += package.requests
        if created_date >= current_month:
            summary["month"][0] += 1
            summary["month"][1] += package.requests
    return {key: (values[0], values[1]) for key, values in summary.items()}


def credit_stars_payment(
    user_id: int,
    package_id: str,
    telegram_charge_id: str,
    username: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
) -> bool:
    """Register and credit one confirmed Telegram Stars payment."""
    with _connect() as connection:
        cursor = connection.execute(
            "INSERT OR IGNORE INTO payments"
            "(external_id, provider, user_id, package_id, username, first_name, last_name) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                telegram_charge_id,
                "stars",
                user_id,
                package_id,
                username,
                first_name,
                last_name,
            ),
        )
        if cursor.rowcount != 1:
            return False
        package = PACKAGES[package_id]
        connection.execute(
            "INSERT INTO balances(user_id, requests) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET requests = requests + excluded.requests",
            (user_id, package.requests),
        )
        connection.execute(
            "UPDATE payments SET credited = 1 WHERE external_id = ?",
            (telegram_charge_id,),
        )
    return True


def restore_request(user_id: int, source: str) -> None:
    """Return a request when generation failed after quota reservation."""
    if source == "welcome":
        with _connect() as connection:
            connection.execute(
                "UPDATE user_accounts SET welcome_requests = welcome_requests + 1 "
                "WHERE user_id = ?",
                (user_id,),
            )
        return
    if source == "paid":
        add_balance_requests(user_id, 1)
        return
    if source == "free":
        today = date.today().isoformat()
        with _connect() as connection:
            connection.execute(
                "UPDATE daily_usage SET free_requests = free_requests - 1 "
                "WHERE user_id = ? AND usage_date = ? AND free_requests > 0",
                (user_id, today),
            )


def add_balance_requests(user_id: int, amount: int) -> int:
    with _connect() as connection:
        connection.execute(
            "INSERT INTO balances(user_id, requests) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET requests = requests + excluded.requests",
            (user_id, amount),
        )
        row = connection.execute(
            "SELECT requests FROM balances WHERE user_id = ?", (user_id,)
        ).fetchone()
    return int(row[0])


def claim_referral_reward(inviter_user_id: int, invited_user_id: int) -> bool:
    if inviter_user_id == invited_user_id:
        return False
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            "INSERT OR IGNORE INTO referral_rewards "
            "(invited_user_id, inviter_user_id, requests) VALUES (?, ?, 3)",
            (invited_user_id, inviter_user_id),
        )
        if cursor.rowcount != 1:
            return False
        connection.execute(
            "INSERT INTO balances(user_id, requests) VALUES (?, 3) "
            "ON CONFLICT(user_id) DO UPDATE SET requests = requests + 3",
            (inviter_user_id,),
        )
    return True


def replace_payment_id(old_id: str, new_id: str) -> None:
    with _connect() as connection:
        connection.execute(
            "UPDATE payments SET external_id = ? WHERE external_id = ?",
            (new_id, old_id),
        )


def payment_menu_text() -> str:
    return "\n".join(
        f"{package.title}: {package.requests} запросов — {package.rubles} ₽ / {package.stars} ⭐"
        for package in PACKAGES.values()
    )


async def create_yookassa_payment(
    user_id: int,
    package: Package,
    shop_id: str,
    secret_key: str,
    return_url: str,
    username: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
) -> str:
    external_id = str(uuid.uuid4())
    remember_payment(
        "yookassa", user_id, package.id, external_id, username, first_name, last_name
    )
    auth = base64.b64encode(f"{shop_id}:{secret_key}".encode()).decode()
    payload = {
        "amount": {"value": f"{package.rubles:.2f}", "currency": "RUB"},
        "capture": True,
        "confirmation": {"type": "redirect", "return_url": return_url},
        "description": f"{package.title}: {package.requests} запросов",
        "metadata": {"user_id": str(user_id), "package_id": package.id},
    }
    headers = {
        "Authorization": f"Basic {auth}",
        "Idempotence-Key": external_id,
        "Content-Type": "application/json",
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://api.yookassa.ru/v3/payments", json=payload, headers=headers
        ) as response:
            data = await response.json()
            if response.status >= 400:
                raise RuntimeError(data.get("description", "ЮKassa не создала платёж"))
    replace_payment_id(external_id, data["id"])
    return data["confirmation"]["confirmation_url"]


async def create_xrocket_invoice(
    user_id: int,
    package: Package,
    api_token: str,
    webhook_url: str,
    api_base_url: str,
    username: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
) -> str:
    client_invoice_id = str(uuid.uuid4())
    remember_payment(
        "xrocket", user_id, package.id, client_invoice_id, username, first_name, last_name
    )
    payload = {
        "priceAmount": package.crypto_usdt,
        "priceCurrency": "USDT",
        "payoutCurrency": "USDT",
        "payCurrencies": ["USDT"],
        "clientInvoiceId": client_invoice_id,
        "description": f"{package.title}: {package.requests} запросов",
        "expiresIn": 3600,
    }
    if webhook_url:
        payload["callback"] = {
            "callbackUrl": webhook_url,
            "payload": {"userId": str(user_id), "packageId": package.id},
        }
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{api_base_url.rstrip('/')}/api/v1/invoices",
            json=payload,
            headers=headers,
        ) as response:
            data = await response.json()
            if response.status >= 400:
                detail = data.get("detail", data.get("title", "xRocket не создал счёт"))
                raise RuntimeError(f"{detail} (HTTP {response.status})")
    return data["links"]["telegramBotLink"]


def verify_xrocket_signature(
    body: bytes,
    signature: str,
    version: str,
    timestamp: str,
    webhook_secret: str,
    tolerance_ms: int = 300_000,
) -> bool:
    try:
        timestamp_ms = int(timestamp)
    except (TypeError, ValueError):
        return False
    if version != "v1" or not signature or not webhook_secret:
        return False
    if abs(int(datetime.now(timezone.utc).timestamp() * 1000) - timestamp_ms) > tolerance_ms:
        return False
    signed_string = f"{timestamp}.{body.decode('utf-8')}"
    expected = hmac.new(
        webhook_secret.encode(), signed_string.encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


async def yookassa_payment_succeeded(payment_id: str, shop_id: str, secret_key: str) -> bool:
    auth = base64.b64encode(f"{shop_id}:{secret_key}".encode()).decode()
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"https://api.yookassa.ru/v3/payments/{payment_id}",
            headers={"Authorization": f"Basic {auth}"},
        ) as response:
            if response.status >= 400:
                return False
            data = await response.json()
    return data.get("status") == "succeeded" and data.get("paid") is True
