"""
Конфигурация бота.
Все секреты берутся из переменных окружения (.env) — см. .env.example
"""
import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass
class Settings:
    # Telegram
    bot_token: str = os.getenv("BOT_TOKEN", "")

    # Кастомный OpenAI-совместимый провайдер (routercheap)
    llm_base_url: str = os.getenv("LLM_BASE_URL", "https://api.routercheap.example/v1")
    llm_api_key: str = os.getenv("LLM_API_KEY", "")
    llm_model: str = os.getenv("LLM_MODEL", "gpt-4o-mini")  # укажи модель, доступную в routercheap
    llm_vision_model: str = os.getenv("LLM_VISION_MODEL", "")

    # Redis для FSM; при пустом значении используется MemoryStorage.
    redis_url: str = os.getenv("REDIS_URL", "")
    redis_key_prefix: str = os.getenv("REDIS_KEY_PREFIX", "astro_bot:fsm")
    redis_state_ttl_seconds: int = int(os.getenv("REDIS_STATE_TTL_SECONDS", "86400"))
    redis_data_ttl_seconds: int = int(os.getenv("REDIS_DATA_TTL_SECONDS", "86400"))

    # Postgres (для профилей/истории в проде; демо работает без БД, в памяти)
    database_url: str = os.getenv("DATABASE_URL", "")
    payments_db_path: str = os.getenv("PAYMENTS_DB_PATH", "data/payments.sqlite3")

    # Лимиты бесплатных запросов в день (демо: просто число, без обхода/скрытых урезаний)
    new_user_requests: int = int(os.getenv("NEW_USER_REQUESTS", "2"))
    free_requests_per_day: int = int(os.getenv("FREE_REQUESTS_PER_DAY", "1"))
    temporary_block_hours: int = int(os.getenv("TEMPORARY_BLOCK_HOURS", "3"))
    admin_user_ids: tuple[int, ...] = tuple(
        int(value.strip())
        for value in os.getenv("ADMIN_USER_IDS", "").split(",")
        if value.strip()
    )

    # Платежи. Секреты задаются только через .env.
    yookassa_shop_id: str = os.getenv("YOOKASSA_SHOP_ID", "")
    yookassa_secret_key: str = os.getenv("YOOKASSA_SECRET_KEY", "")
    yookassa_return_url: str = os.getenv("YOOKASSA_RETURN_URL", "https://t.me")
    xrocket_api_token: str = os.getenv("XROCKET_API_TOKEN", "")
    xrocket_webhook_secret: str = os.getenv("XROCKET_WEBHOOK_SECRET", "")
    xrocket_api_base_url: str = os.getenv(
        "XROCKET_API_BASE_URL", "https://pay.api.xrocket.exchange"
    )
    payment_webhook_url: str = os.getenv("PAYMENT_WEBHOOK_URL", "")
    payment_webhook_host: str = os.getenv("PAYMENT_WEBHOOK_HOST", "127.0.0.1")
    payment_webhook_port: int = int(os.getenv("PAYMENT_WEBHOOK_PORT", "8080"))
    privacy_policy_url: str = os.getenv("PRIVACY_POLICY_URL", "")
    public_offer_url: str = os.getenv("PUBLIC_OFFER_URL", "")


settings = Settings()
