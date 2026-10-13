"""
Демо-бот: "гадание" по Таро и натальной карте через Telegram.

Архитектура:
  Сообщение пользователя
      -> если юзер НЕ в середине сценария: спрашиваем LLM с tool-схемами,
         модель либо отвечает текстом (уточняющий вопрос), либо вызывает
         одну из функций (start_tarot_reading / start_natal_chart)
      -> если юзер УЖЕ в сценарии (FSM state не пуст): обрабатываем
         сообщение как очередной шаг этого сценария, без похода в LLM
         для роутинга (роутинг уже не нужен — сценарий известен)
      -> сам расчёт (карты/знак) — детерминированный код
      -> LLM вызывается снова ТОЛЬКО для генерации финального текста
         на основе готовых фактов

Запуск:
    pip install -r requirements.txt
    cp .env.example .env  # заполнить BOT_TOKEN, LLM_BASE_URL, LLM_API_KEY
    python bot.py
"""
import asyncio
from datetime import datetime, timedelta, timezone
from io import BytesIO
import html
import json
import logging
import re
import uuid
from datetime import date
from zoneinfo import ZoneInfo

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import DefaultKeyBuilder
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import (
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)

from config import settings
from llm_client import generate_reply, route_message
from payments import (
    PACKAGES,
    claim_referral_reward,
    consume_request,
    create_xrocket_invoice,
    create_yookassa_payment,
    credit_payment,
    credit_stars_payment,
    add_balance_requests,
    block_user,
    blocked_until,
    clear_invalid_media_attempts,
    paid_requests_left,
    available_requests_left,
    payment_menu_text,
    free_requests_used_today,
    payment_recipient,
    payment_details,
    topup_summary,
    welcome_requests_left,
    register_invalid_media_attempt,
    restore_request,
    verify_xrocket_signature,
    yookassa_payment_succeeded,
)
from scenarios import coffee, tarot, natal
from moderation import should_temporarily_block
from sessions import (
    accept_consent,
    close_current_session,
    base_dialog_responses_today,
    consume_base_dialog_response,
    consent_accepted,
    delete_user_data,
    get_referral_source,
    get_referral_inviter,
    get_birth_date,
    prepare_context,
    referral_statistics,
    record_message_with_summary,
    reset_base_dialog_usage_today,
    save_referral_source,
    save_birth_date,
)
from states import FollowUpFlow, ImageFlow, TarotFlow, NatalFlow
from tools import SCENARIO_TOOLS

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
MOSCOW = ZoneInfo("Europe/Moscow")

bot = Bot(token=settings.bot_token)


def _build_fsm_storage():
    if settings.redis_url:
        logger.info("FSM storage: Redis (%s)", settings.redis_url.split("@")[-1])
        return RedisStorage.from_url(
            settings.redis_url,
            key_builder=DefaultKeyBuilder(prefix=settings.redis_key_prefix),
            state_ttl=settings.redis_state_ttl_seconds,
            data_ttl=settings.redis_data_ttl_seconds,
        )
    logger.warning("FSM storage: MemoryStorage; REDIS_URL is empty")
    return MemoryStorage()


dp = Dispatcher(storage=_build_fsm_storage())


def _telegram_html(text: str) -> str:
    escaped = html.escape(text)
    escaped = re.sub(r"^#{1,3}\s+(.+)$", r"<b>\1</b>", escaped, flags=re.MULTILINE)
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped, flags=re.DOTALL)


def _reply_context(message: Message) -> str:
    replied = message.reply_to_message
    if replied is None:
        return ""
    replied_text = (replied.text or replied.caption or "").strip()
    if not replied_text:
        return "Пользователь отвечает на сообщение бота с вложением без текста."
    return f"Пользователь отвечает на это сообщение бота:\n«{replied_text[:4000]}»"


def _message_with_reply_context(message: Message) -> str:
    text = (message.text or message.caption or "").strip()
    reply_context = _reply_context(message)
    if not reply_context:
        return text
    return f"{reply_context}\n\nНовое сообщение пользователя:\n{text}"


def _has_tarot_question(text: str) -> bool:
    normalized = " ".join(text.lower().split())
    if "?" in normalized:
        return True
    question_words = (
        "как ", "что ", "когда ", "почему ", "стоит ли ",
        "получу ли ", "найду ли ", "будет ли ", "есть ли ",
    )
    if any(word in normalized for word in question_words):
        return True
    stripped = re.sub(
        r"\b(таро|карты|картах|расклад|погадай|погадать|посмотри|сделай|дай|мне|пожалуйста)\b",
        " ",
        normalized,
    )
    return len(" ".join(stripped.split())) >= 18


async def _answer_formatted(message: Message, text: str) -> None:
    await message.answer(_telegram_html(text), parse_mode=ParseMode.HTML)


async def _run_with_wait_animation(message: Message, operation):
    waiting = await message.answer("🔮 Готовлю ответ  ⠋")
    frames = (
        "🔮 Готовлю ответ  ⠋",
        "🔮 Готовлю ответ  ⠙",
        "🔮 Готовлю ответ  ⠹",
        "🔮 Готовлю ответ  ⠸",
        "🔮 Готовлю ответ  ⠼",
        "🔮 Готовлю ответ  ⠴",
        "🔮 Готовлю ответ  ⠦",
        "🔮 Готовлю ответ  ⠧",
        "🔮 Готовлю ответ  ⠇",
        "🔮 Готовлю ответ  ⠏",
    )
    task = asyncio.create_task(operation)
    frame_index = 0
    try:
        while not task.done():
            await asyncio.sleep(0.8)
            if task.done():
                break
            frame_index = (frame_index + 1) % len(frames)
            await waiting.edit_text(frames[frame_index])
        return await task
    finally:
        try:
            await waiting.delete()
        except Exception:
            logger.debug("Could not delete waiting message", exc_info=True)

def _check_and_consume_limit(user_id: int) -> str | None:
    return consume_request(
        user_id,
        settings.free_requests_per_day,
    )


async def _quota_exhausted_message(message: Message) -> None:
    await message.answer(
        "Похоже, доступные запросы закончились 🌙\n\n"
        "Проверь остаток: /limits\n"
        "Продолжить можно с новым пакетом: /buy"
    )


def _looks_like_service_follow_up(
    text: str,
    service_context: str,
    is_reply_to_bot: bool = False,
) -> bool:
    normalized = " ".join(text.lower().split())
    if normalized in {"привет", "здравствуй", "здравствуйте", "как дела", "спасибо", "ок", "понятно"}:
        return False
    if is_reply_to_bot:
        return True
    service_markers = {
        "tarot": (
            "расклад", "таро", "карт", "аркан", "выпал", "выпали", "позици",
            "совет", "отношен", "ситуац", "работ", "карьер", "деньг",
        ),
        "natal": (
            "наталь", "карт", "планет", "аспект", "знак", "восход",
            "луна", "солнц", "венер", "марс", "отношен", "работ", "карьер",
        ),
        "coffee": (
            "гущ", "чаш", "блюд", "символ", "кофе", "знак", "отношен", "ситуац",
        ),
    }
    contextual_markers = (
        "по нему", "по ней", "по этому", "по этой", "из этого", "из этого",
        "что значит", "как это", "почему так", "что дальше", "продолж",
        "подробнее", "а если", "какой совет", "что поможет",
    )
    return any(marker in normalized for marker in service_markers.get(service_context, ())) or any(
        marker in normalized for marker in contextual_markers
    )


BASE_DIALOG_LIMIT = 10


def _base_dialog_state(state_data: dict) -> tuple[int, str]:
    today = date.today().isoformat()
    if state_data.get("base_dialog_date") != today:
        return 0, today
    return int(state_data.get("base_dialog_count", 0)), today


def _suggest_service(text: str) -> str:
    normalized = text.lower()
    if any(marker in normalized for marker in ("отношен", "любов", "партнер", "расстав", "чувств")):
        return "расклад Таро на отношения"
    if any(marker in normalized for marker in ("работ", "карьер", "деньг", "выбор", "решен")):
        return "расклад Таро на ситуацию и ближайшие шаги"
    if any(marker in normalized for marker in ("дата рождения", "наталь", "планет", "знак зодиака")):
        return "натальный разбор"
    return "расклад Таро по конкретному вопросу"


def _base_dialog_offer(response_number: int, user_text: str) -> str:
    service = _suggest_service(user_text)
    if response_number < 4:
        return ""
    if response_number < 6:
        return (
            f"\n\nЕсли захочешь посмотреть на эту тему глубже, я могу сделать "
            f"{service} ✨"
        )
    if response_number < 8:
        return (
            f"\n\nПо тому, что ты описываешь, здесь особенно уместен {service}. "
            "Он поможет разобрать именно твою ситуацию, а не оставаться в общих рассуждениях 🔮"
        )
    if response_number < BASE_DIALOG_LIMIT:
        return (
            f"\n\nМы уже близко подошли к теме. Я бы предложила продолжить через "
            f"{service}, чтобы разобрать детали и возможные шаги ✨"
        )
    return (
        f"\n\nЭто последний ответ в базовом диалоге. Если захочешь продолжить, "
        f"лучше перейти к услуге: {service} 🔮"
    )


async def _warn_if_two_requests_left(message: Message) -> None:
    remaining = available_requests_left(
        message.from_user.id,
        settings.free_requests_per_day,
    )
    if remaining == 2:
        await message.answer(
            "Осталось 2 запроса. Если захочешь продолжить глубже, "
            "можно сделать персональный расклад или натальный разбор ✨"
        )


async def _reject_blocked(message: Message, state: FSMContext | None = None) -> bool:
    blocked = blocked_until(message.from_user.id)
    if blocked is None:
        return False
    if state is not None:
        await state.clear()
    await message.answer(
        "Я поставила общение на паузу на 3 часа. "
        "Вернись позже, и мы продолжим расклад спокойно."
    )
    return True


async def _moderate_message(message: Message, state: FSMContext | None = None) -> bool:
    if await _reject_blocked(message, state):
        return True
    if should_temporarily_block(message.text or ""):
        block_user(message.from_user.id, settings.temporary_block_hours)
        if state is not None:
            await state.clear()
        await message.answer(
            "Я не обсуждаю внутреннюю механику и проверки бота. "
            "Вернись через 3 часа, если захочешь сделать настоящий расклад."
        )
        return True
    return False


def _consent_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Согласен(на)", callback_data="consent:accept")],
    ])


async def _summarize_dialog(transcript: str) -> str:
    return await generate_reply(
        system_prompt=(
            "Сожми разговор в 2-4 предложения для будущей сессии. "
            "Сохрани только тему, эмоциональный контекст и незакрытый вопрос. "
            "Не сохраняй конкретные карты, дословные цитаты, секреты и лишние "
            "подробности личной жизни. Не делай предсказаний."
        ),
        user_prompt=transcript,
    )


async def _require_consent(message: Message) -> bool:
    if consent_accepted(message.from_user.id):
        return True
    links = []
    if settings.privacy_policy_url:
        links.append(f"Политика: {settings.privacy_policy_url}")
    else:
        links.append("Политику нужно указать в PRIVACY_POLICY_URL")
    if settings.public_offer_url:
        links.append(f"Оферта: {settings.public_offer_url}")
    else:
        links.append("Оферту нужно указать в PUBLIC_OFFER_URL")
    await message.answer(
        "Перед началом я сохраню короткую память диалога: последние реплики, "
        "краткие итоги сессий и данные, которые ты сам(а) сообщишь. "
        "Это нужно, чтобы не переспрашивать одно и то же. Удалить всё можно "
        "командой /forget.\n\n" + "\n".join(links),
        reply_markup=_consent_keyboard(),
    )
    return False


async def _answer_follow_up(message: Message, state: FSMContext) -> None:
    if await _moderate_message(message, state):
        return
    if not await _require_consent(message):
        return
    user_text = _message_with_reply_context(message)
    if not user_text:
        await message.answer("Напиши пару слов, и я продолжу мысль расклада.")
        return

    state_data = await state.get_data()
    service_context = state_data.get("service_context")
    current_text = (message.text or message.caption or "").strip()
    is_reply_to_bot = bool(
        message.reply_to_message
        and message.reply_to_message.from_user
        and message.reply_to_message.from_user.is_bot
    )
    if service_context and not _looks_like_service_follow_up(
        current_text, service_context, is_reply_to_bot
    ):
        await state.clear()
        await free_dialog(message, state)
        return
    follow_up_count = int(state_data.get("follow_up_count", 0))
    if follow_up_count >= 5:
        await state.clear()
        await message.answer(
            "Мы уже подробно разобрали этот вопрос. Если хочешь посмотреть "
            "другую сторону ситуации, можно перейти к новому раскладу или "
            "натальному разбору ✨"
        )
        return
    quota_source = _check_and_consume_limit(message.from_user.id)
    if quota_source is None:
        await state.clear()
        await _quota_exhausted_message(message)
        return

    await record_message_with_summary(
        message.from_user.id, "user", user_text, _summarize_dialog
    )
    history = await prepare_context(message.from_user.id, _summarize_dialog)
    service_names = {
        "tarot": "расклада Таро",
        "natal": "натального разбора",
        "coffee": "чтения кофейной гущи",
    }
    service_name = service_names.get(service_context, "оказанной услуги")
    prompt = (
        f"Пользователь задаёт дополнительный вопрос в контексте {service_name}. "
        "Продолжи разговор естественно и по-человечески, "
        f"опираясь на контекст {service_name}. Не превращай ответ в консультацию "
        "по поиску работы, программированию, резюме или технологиям. "
        "Не начинай новый расклад и не проси повторить уже сказанное. "
        "Свяжи ответ с символикой карт, внутренним состоянием и выбором "
        "пользователя. Ответь тепло и содержательно в 5-8 предложениях, "
        "добавь один практичный следующий шаг и задай один мягкий вопрос "
        "для продолжения. Если естественно подходит более глубокая услуга, "
        "предложи конкретный формат по теме пользователя, без упоминания "
        "команд и покупки.\n\n"
        f"Контекст ответа пользователя: {user_text}"
    )
    try:
        reply_text = await _run_with_wait_animation(
            message,
            generate_reply(
                system_prompt=(
                    "Ты — внимательная ИИ-гадалка. Оставайся в образной, "
                    "бережной атмосфере уже оказанной услуги. Говори о чувствах, "
                    "внутренних опорах, препятствиях и возможных шагах, но "
                    "не давай технических карьерных советов и не упоминай "
                    "Git, стек, код, резюме или вакансии. Не выдавай "
                    "интерпретацию за неизбежное предсказание."
                ),
                user_prompt="\n".join(
                    f"{item['role']}: {item['content']}" for item in history
                ) + "\n\n" + prompt,
            ),
        )
    except Exception:
        restore_request(message.from_user.id, quota_source)
        await state.clear()
        logger.exception("Follow-up reply generation failed")
        await message.answer("Не удалось продолжить разговор. Попробуй написать ещё раз позже.")
        return
    await record_message_with_summary(
        message.from_user.id, "assistant", reply_text, _summarize_dialog
    )
    reset_base_dialog_usage_today(message.from_user.id)
    await state.update_data(follow_up_count=follow_up_count + 1)
    await state.set_state(FollowUpFlow.awaiting_reply)
    await _answer_formatted(message, reply_text)


async def _photo_bytes(message: Message) -> tuple[bytes, str] | None:
    file_id = None
    if message.photo:
        file_id = message.photo[-1].file_id
    elif message.document and message.document.mime_type and message.document.mime_type.startswith("image/"):
        file_id = message.document.file_id
    if not file_id:
        return None
    telegram_file = await bot.get_file(file_id)
    buffer = BytesIO()
    await bot.download(telegram_file, destination=buffer)
    mime_type = "image/jpeg"
    if message.document and message.document.mime_type:
        mime_type = message.document.mime_type
    return buffer.getvalue(), mime_type


async def _handle_image(message: Message, state: FSMContext) -> None:
    if await _moderate_message(message, state):
        return
    if not await _require_consent(message):
        return
    image_data = await _photo_bytes(message)
    if not image_data:
        await message.answer("Пришли изображение как фото или файл JPG/PNG.")
        return
    image_bytes, image_mime_type = image_data
    try:
        raw_result = await _run_with_wait_animation(
            message,
            coffee.read_photo(
                image_bytes,
                _message_with_reply_context(message),
                image_mime_type,
            ),
        )
    except Exception:
        logger.exception("Image analysis failed")
        await message.answer(
            "Не получилось прочитать изображение. Пришли чёткое фото чашки "
            "с гущей или скрин натальной карты."
        )
        return

    kind, reply_text = coffee.classify_result(raw_result)
    if kind == "invalid":
        attempts = register_invalid_media_attempt(message.from_user.id)
        if attempts >= 3:
            block_user(message.from_user.id, settings.temporary_block_hours)
            await state.clear()
            await message.answer(
                "Похоже, ты несколько раз отправил(а) неподходящее изображение. "
                "Общение приостановлено на 3 часа."
            )
            return
        remaining = 3 - attempts
        await message.answer(
            "Я не смогла определить фото. Пришли либо чёткую чашку/блюдце "
            "с кофейной гущей, либо читаемый скрин натальной карты.\n"
            f"Осталось попыток для подходящего изображения: {remaining}."
        )
        return

    clear_invalid_media_attempts(message.from_user.id)
    if kind == "natal":
        chart_data = coffee.extract_chart_data(raw_result)
        known_birth_date = get_birth_date(message.from_user.id)
        if known_birth_date and chart_data["date"] == "unknown":
            chart_data["date"] = known_birth_date
        if chart_data["date"] == "unknown":
            await state.update_data(
                chart_data=chart_data,
                chart_observation=reply_text,
            )
            await state.set_state(NatalFlow.awaiting_birth_date)
            await message.answer(
                "Я вижу карту и могу начать разбор уже сейчас. Если хочешь проверить "
                "её точность, напиши дату рождения в формате ДД.ММ.ГГГГ; если дату "
                "уже присылал(а), просто ответь «продолжай» 🌌"
            )
            return
        known_date = natal.parse_birth_date(known_birth_date or "")
        chart_date = natal.parse_birth_date(chart_data["date"])
        if known_date and chart_date and known_date != chart_date:
            await message.answer(
                f"⚠️ Я нашла расхождение: в твоём профиле дата "
                f"{known_date.strftime('%d.%m.%Y')}, а на карте указана "
                f"{chart_date.strftime('%d.%m.%Y')}. Проверь, та ли это карта. "
                "Я не буду делать вид, что данные совпадают."
            )
            await state.clear()
            return
        await state.update_data(chart_data=chart_data, chart_observation=reply_text)
        if known_date:
            await state.set_state(NatalFlow.awaiting_birth_date)
            await natal_date_given(message, state)
            return
        await state.set_state(NatalFlow.awaiting_birth_date)
        await message.answer(
            f"Вижу на карте дату {chart_data['date']}. Если это твоя дата, напиши "
            "«продолжай», и я начну разбор. Если нет — пришли правильную дату 🌌"
        )
        return
    quota_source = _check_and_consume_limit(message.from_user.id)
    if quota_source is None:
        await _quota_exhausted_message(message)
        return
    await record_message_with_summary(
        message.from_user.id,
        "user",
        f"Пользователь прислал фото для сценария {kind}. "
        f"{_message_with_reply_context(message)}",
        _summarize_dialog,
    )
    await record_message_with_summary(
        message.from_user.id, "assistant", reply_text, _summarize_dialog
    )
    await _warn_if_two_requests_left(message)
    reset_base_dialog_usage_today(message.from_user.id)
    await state.update_data(service_context="coffee", follow_up_count=0, base_dialog_count=0)
    await state.set_state(FollowUpFlow.awaiting_reply)
    await _answer_formatted(message, reply_text)


def _package_keyboard(package) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text=f"⭐ {package.stars} Stars",
            callback_data=f"buy:stars:{package.id}",
        ),
        InlineKeyboardButton(
            text=f"💳 {package.rubles} ₽",
            callback_data=f"buy:yookassa:{package.id}",
        ),
        InlineKeyboardButton(
                text=f"🚀 {package.crypto_usdt} USDT",
                callback_data=f"buy:xrocket:{package.id}",
        ),
    ]])


@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    if await _moderate_message(message, state):
        return
    start_parts = (message.text or "").split(maxsplit=1)
    referral_source = start_parts[1].strip() if len(start_parts) == 2 else ""
    if referral_source and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", referral_source):
        referral_match = re.fullmatch(r"ref_(\d+)", referral_source)
        inviter_id = int(referral_match.group(1)) if referral_match else None
        if inviter_id == message.from_user.id:
            inviter_id = None
        save_referral_source(message.from_user.id, referral_source, inviter_id)
    if not await _require_consent(message):
        return
    await state.clear()
    await message.answer(
        "Привет! Я помогу посмотреть на важную ситуацию с неожиданной стороны 🌙\n\n"
        "Что я умею:\n"
        "🔮 расклад Таро по твоему вопросу;\n"
        "☕ чтение символов на фото кофейной гущи;\n"
        "🌌 натальная карта по дате рождения или фото.\n\n"
        "Тебе доступны 2 подарочных запроса при первом старте и 1 бесплатный "
        "запрос в сутки. После результата можно задать дополнительные вопросы "
        "в его контексте.\n\n"
        "Для начала напиши вопрос, например: «Что поможет мне улучшить отношения?»\n"
        "Пригласить друга и получить 3 запроса можно через /referral."
    )


@dp.callback_query(F.data == "consent:accept")
async def consent_accept(callback, state: FSMContext):
    is_new_user = accept_consent(callback.from_user.id)
    if is_new_user:
        await _notify_admins_about_new_user(callback.from_user)
        inviter_id = get_referral_inviter(callback.from_user.id)
        if inviter_id is not None and claim_referral_reward(inviter_id, callback.from_user.id):
            try:
                await bot.send_message(
                    inviter_id,
                    "🎁 Твой друг присоединился по твоей ссылке! "
                    "Тебе начислено 3 запроса. Спасибо, что рекомендуешь нас ✨",
                )
            except Exception:
                logger.exception("Could not notify inviter %s about referral reward", inviter_id)
    await callback.message.edit_text(
        "✅ Согласие принято. Можно начинать.\n\n"
        "Я помогу посмотреть на важную ситуацию с неожиданной стороны 🌙\n\n"
        "Что можно сделать:\n"
        "🔮 получить расклад Таро по своему вопросу;\n"
        "☕ прислать фото чашки с кофейной гущей;\n"
        "🌌 разобрать натальную карту по дате рождения или фото.\n\n"
        "У тебя есть 2 подарочных запроса при первом старте и 1 бесплатный "
        "запрос в сутки. После результата можно задавать дополнительные вопросы "
        "по теме разбора.\n\n"
        "Начни с вопроса, например: «Что поможет мне улучшить отношения?»\n"
        "За приглашённого друга начисляется 3 запроса. Твоя ссылка: /referral",
    )
    await callback.answer("Готово")


async def _notify_admins_about_new_user(user) -> None:
    username = user.username
    display_name = " ".join(
        part for part in (user.first_name, user.last_name) if part
    ).strip()
    profile_label = f"@{username}" if username else (display_name or str(user.id))
    profile_url = f"https://t.me/{username}" if username else f"tg://user?id={user.id}"
    referral_source = get_referral_source(user.id) or "direct"
    text = (
        "🌟 Новый пользователь\n\n"
        f"👤 <a href=\"{profile_url}\">{html.escape(profile_label)}</a>\n"
        f"🆔 ID: <code>{user.id}</code>\n"
        f"📣 Источник: <code>{html.escape(referral_source)}</code>\n"
        f"🎁 Стартовый доступ: {settings.new_user_requests} запросов\n"
        "✅ Условия приняты, можно начинать"
    )
    for admin_id in settings.admin_user_ids:
        try:
            await bot.send_message(admin_id, text, parse_mode=ParseMode.HTML)
        except Exception:
            logger.exception("Could not notify admin %s about new user", admin_id)


@dp.message(F.text == "/new")
async def cmd_new(message: Message, state: FSMContext):
    if await _moderate_message(message, state):
        return
    if not await _require_consent(message):
        return
    await close_current_session(message.from_user.id)
    await state.clear()
    await message.answer("Текущий контекст закрыт. Начинаем с чистого листа ✨")


@dp.message(F.text == "/forget")
@dp.message(F.text == "/delete_data")
async def cmd_forget(message: Message, state: FSMContext):
    delete_user_data(message.from_user.id)
    await state.clear()
    await message.answer(
        "Профиль, историю сессий и сохранённые итоги удалила 🕯️\n"
        "Лимиты, оплаченные запросы и записи платежей сохранены, чтобы удаление "
        "данных нельзя было использовать для обхода ограничений."
    )


@dp.message(F.text == "/limits")
async def cmd_limits(message: Message):
    if await _moderate_message(message):
        return
    if not await _require_consent(message):
        return
    used = free_requests_used_today(message.from_user.id)
    daily_left = max(0, settings.free_requests_per_day - used)
    welcome_left = welcome_requests_left(message.from_user.id)
    lines = []
    if welcome_left > 0:
        lines.append(f"Стартовых запросов осталось: {welcome_left} 🎁")
    lines.extend(
        [
            f"Запросов на сегодня: {daily_left} ⏳",
            f"Всего доступно запросов: {welcome_left + daily_left + paid_requests_left(message.from_user.id)}",
            f"Из них платных: {paid_requests_left(message.from_user.id)}",
            "",
            "Купить пакет: /buy",
        ]
    )
    await message.answer("\n".join(lines))


@dp.message(F.text == "/referral")
async def cmd_referral(message: Message):
    if await _moderate_message(message):
        return
    if not await _require_consent(message):
        return
    me = await bot.get_me()
    await message.answer(
        "Пригласи друга и получи 3 запроса 🎁\n\n"
        f"Твоя ссылка:\nhttps://t.me/{me.username}?start=ref_{message.from_user.id}\n\n"
        "Бонус начислится один раз, когда друг впервые откроет ссылку и примет условия."
    )


@dp.message(F.text == "/buy")
async def cmd_buy(message: Message):
    if await _moderate_message(message):
        return
    if not await _require_consent(message):
        return
    await message.answer(
        "Выбери пакет. Оплата доступна через Telegram Stars, банковскую карту "
        "или xRocket USDT."
    )
    for package in PACKAGES.values():
        await message.answer(
            f"<b>{html.escape(package.title)}</b>\n{package.requests} запросов",
            parse_mode=ParseMode.HTML,
            reply_markup=_package_keyboard(package),
        )


@dp.message(F.photo)
async def photo_message(message: Message, state: FSMContext):
    await _handle_image(message, state)


@dp.message(F.document)
async def image_document_message(message: Message, state: FSMContext):
    if not message.document or not (message.document.mime_type or "").startswith("image/"):
        await message.answer(
            "Я читаю только фото чашки с кофейной гущей или натальной карты. "
            "Пришли подходящее изображение."
        )
        return
    await _handle_image(message, state)


@dp.message(ImageFlow.awaiting_image)
async def image_flow_text(message: Message, state: FSMContext):
    if await _moderate_message(message, state):
        return
    parsed = natal.parse_birth_date(message.text or "")
    if parsed:
        await state.set_state(NatalFlow.awaiting_birth_date)
        await natal_date_given(message, state)
        return
    await message.answer(
        "Для этого сценария пришли именно изображение: чёткое фото чашки "
        "с гущей или читаемый скрин натальной карты."
    )


@dp.message(F.text.startswith("/grant "))
async def cmd_grant(message: Message):
    if message.from_user.id not in settings.admin_user_ids:
        return
    parts = message.text.split()
    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
        await message.answer("Формат: /grant USER_ID КОЛИЧЕСТВО")
        return
    user_id, amount = int(parts[1]), int(parts[2])
    balance = add_balance_requests(user_id, amount)
    await message.answer(f"Начислено {amount}. Баланс пользователя: {balance}")


@dp.message(F.text == "/refstats")
async def cmd_refstats(message: Message):
    if message.from_user.id not in settings.admin_user_ids:
        return
    rows = referral_statistics()
    total = sum(count for _, count in rows)
    details = "\n".join(f"• {source}: {count}" for source, count in rows)
    await message.answer(
        f"📣 Источники пользователей\n\nВсего: {total}\n{details or 'Данных пока нет'}"
    )


@dp.callback_query(F.data.startswith("buy:"))
async def buy_package(callback):
    _, provider, package_id = callback.data.split(":", 2)
    package = PACKAGES.get(package_id)
    if package is None:
        await callback.answer("Пакет не найден", show_alert=True)
        return

    user_id = callback.from_user.id
    if provider == "stars":
        payload = f"stars:{user_id}:{package.id}:{uuid.uuid4().hex}"
        await callback.message.answer_invoice(
            title=package.title,
            description=f"{package.requests} запросов для Таро и натальной карты",
            payload=payload,
            currency="XTR",
            prices=[LabeledPrice(label=f"{package.requests} запросов", amount=package.stars)],
        )
        await callback.answer()
        return

    try:
        if provider == "yookassa":
            if not all((settings.yookassa_shop_id, settings.yookassa_secret_key, settings.yookassa_return_url)):
                raise RuntimeError("ЮKassa ещё не настроена в .env")
            url = await create_yookassa_payment(
                user_id,
                package,
                settings.yookassa_shop_id,
                settings.yookassa_secret_key,
                settings.yookassa_return_url,
                callback.from_user.username,
                callback.from_user.first_name,
                callback.from_user.last_name,
            )
        elif provider == "xrocket":
            if not settings.xrocket_api_token:
                raise RuntimeError("В .env не указан XROCKET_API_TOKEN")
            url = await create_xrocket_invoice(
                user_id,
                package,
                settings.xrocket_api_token,
                (
                    settings.payment_webhook_url.rstrip("/") + "/payments/xrocket"
                    if settings.payment_webhook_url
                    else ""
                ),
                settings.xrocket_api_base_url,
                callback.from_user.username,
                callback.from_user.first_name,
                callback.from_user.last_name,
            )
        else:
            raise RuntimeError("Неизвестный способ оплаты")
    except Exception as error:
        logger.exception("Payment invoice creation failed: provider=%s package=%s", provider, package_id)
        await callback.answer(str(error), show_alert=True)
        return

    await callback.message.answer(f"Счёт «{package.title}»: {url}")
    if provider == "xrocket" and not settings.payment_webhook_url:
        await callback.message.answer(
            "Оплата открыта. Чтобы запросы начислялись автоматически после "
            "оплаты, настрой PAYMENT_WEBHOOK_URL и XROCKET_WEBHOOK_SECRET."
        )
    await callback.answer()


@dp.pre_checkout_query()
async def process_pre_checkout_query(query: PreCheckoutQuery):
    await query.answer(ok=True)


@dp.message(F.successful_payment)
async def successful_payment(message: Message):
    payment = message.successful_payment
    payload_parts = payment.invoice_payload.split(":")
    if len(payload_parts) != 4 or payload_parts[0] != "stars":
        await message.answer("Не удалось проверить платёж. Обратись в поддержку.")
        return
    _, payload_user_id, package_id, _ = payload_parts
    if (
        str(message.from_user.id) != payload_user_id
        or package_id not in PACKAGES
    ):
        await message.answer("Не удалось проверить платёж. Обратись в поддержку.")
        return
    if credit_stars_payment(
        message.from_user.id,
        package_id,
        payment.telegram_payment_charge_id,
        message.from_user.username,
        message.from_user.first_name,
        message.from_user.last_name,
    ):
        await message.answer(
            f"Оплата получена. Начислено {PACKAGES[package_id].requests} запросов ✅\n"
            f"Баланс: {paid_requests_left(message.from_user.id)}"
        )
        await _notify_admins_about_payment(payment.telegram_payment_charge_id)
    else:
        await message.answer("Этот платёж уже обработан или не найден.")


async def _notify_admins_about_payment(external_id: str) -> None:
    details = payment_details(external_id)
    if details is None:
        return
    provider, user_id, package_id, username, first_name, last_name = details
    package = PACKAGES.get(package_id)
    if package is None:
        return
    summary = topup_summary()
    display_name = " ".join(part for part in (first_name, last_name) if part).strip()
    profile_label = f"@{username}" if username else (display_name or str(user_id))
    profile_url = f"https://t.me/{username}" if username else f"tg://user?id={user_id}"
    message = (
        "💰 Успешное пополнение\n"
        f"👤 Пользователь: <a href=\"{profile_url}\">{html.escape(profile_label)}</a> "
        f"(<code>{user_id}</code>)\n"
        f"📦 Пакет: {package.title}\n"
        f"✅ Начислено: {package.requests} запросов\n"
        f"📊 Всего: {summary['all'][0]} оплат / {summary['all'][1]} запросов\n"
        f"📅 За 7 дней: {summary['week'][0]} / {summary['week'][1]}\n"
        f"🗓 За месяц: {summary['month'][0]} / {summary['month'][1]}"
    )
    for admin_id in settings.admin_user_ids:
        try:
            await bot.send_message(admin_id, message, parse_mode=ParseMode.HTML)
        except Exception:
            logger.exception("Could not notify admin %s about payment", admin_id)


async def _topup_summary_scheduler() -> None:
    while True:
        now = datetime.now(MOSCOW)
        next_hour = min((2, 8, 14, 20), key=lambda hour: (hour - now.hour) % 24)
        next_run = now.replace(hour=next_hour, minute=0, second=0, microsecond=0)
        if next_run <= now:
            next_run += timedelta(days=1)
        await asyncio.sleep((next_run - now).total_seconds())
        summary = topup_summary()
        text = (
            "📊 Сводка пополнений\n"
            f"💰 Всего: {summary['all'][0]} оплат / {summary['all'][1]} запросов\n"
            f"📅 За 7 дней: {summary['week'][0]} оплат / {summary['week'][1]} запросов\n"
            f"🗓 За месяц: {summary['month'][0]} оплат / {summary['month'][1]} запросов"
        )
        for admin_id in settings.admin_user_ids:
            try:
                await bot.send_message(admin_id, text)
            except Exception:
                logger.exception("Could not send scheduled top-up summary to %s", admin_id)


async def yookassa_webhook(request: web.Request) -> web.Response:
    data = await request.json()
    event = data.get("event")
    payment = data.get("object", {})
    payment_id = payment.get("id", "")
    verified = (
        event == "payment.succeeded"
        and bool(settings.yookassa_shop_id and settings.yookassa_secret_key)
        and await yookassa_payment_succeeded(
            payment_id, settings.yookassa_shop_id, settings.yookassa_secret_key
        )
    )
    if verified and credit_payment(payment_id):
        recipient = payment_recipient(payment_id)
        if recipient is not None:
            user_id, package_id = recipient
            package = PACKAGES.get(package_id)
            if package is not None:
                try:
                    await bot.send_message(
                        user_id,
                        f"Оплата получена. Начислено {package.requests} запросов ✅\n"
                        f"Баланс: {paid_requests_left(user_id)}",
                    )
                except Exception:
                    logger.exception(
                        "Could not notify user about ЮKassa payment: %s", payment_id
                    )
        await _notify_admins_about_payment(payment_id)
        logger.info("ЮKassa payment credited: %s", payment.get("id"))
    return web.json_response({"ok": verified}, status=200 if verified else 401)


async def xrocket_webhook(request: web.Request) -> web.Response:
    body = await request.read()
    if not verify_xrocket_signature(
        body,
        request.headers.get("Signature", ""),
        request.headers.get("Signature-Version", ""),
        request.headers.get("Signature-Timestamp", ""),
        settings.xrocket_webhook_secret,
    ):
        return web.json_response({"ok": False}, status=401)
    data = json.loads(body)
    invoice_event = data.get("data", {})
    invoice = invoice_event.get("invoice", {})
    invoice_id = str(invoice.get("clientInvoiceId", ""))
    paid = (
        data.get("type") == "invoice"
        and invoice_event.get("event") in {"payment_status_changed", "invoice_status_changed"}
        and invoice.get("status") == "paid"
    )
    if paid and credit_payment(invoice_id):
        recipient = payment_recipient(invoice_id)
        if recipient is not None:
            user_id, package_id = recipient
            package = PACKAGES.get(package_id)
            if package is not None:
                try:
                    await bot.send_message(
                        user_id,
                        f"Оплата получена. Начислено {package.requests} запросов ✅\n"
                        f"Баланс: {paid_requests_left(user_id)}",
                    )
                except Exception:
                    logger.exception(
                        "Could not notify user about xRocket payment: %s", invoice_id
                    )
        await _notify_admins_about_payment(invoice_id)
        logger.info("xRocket invoice credited: %s", invoice_id)
    return web.json_response({"ok": True})


async def healthcheck(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def payment_success(request: web.Request) -> web.Response:
    return web.Response(
        text=(
            "<!doctype html><html lang='ru'><meta charset='utf-8'>"
            "<title>Оплата</title><body><h1>Спасибо за оплату</h1>"
            "<p>Вернись в Telegram. Запросы будут начислены после подтверждения платежа.</p>"
            "</body></html>"
        ),
        content_type="text/html",
    )


async def start_payment_webhook_server() -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/health", healthcheck)
    app.router.add_get("/payment-success", payment_success)
    app.router.add_post("/payments/yookassa", yookassa_webhook)
    app.router.add_post("/payments/xrocket", xrocket_webhook)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, settings.payment_webhook_host, settings.payment_webhook_port)
    await site.start()
    logger.info(
        "Платёжные webhook-и слушают %s:%s",
        settings.payment_webhook_host,
        settings.payment_webhook_port,
    )
    return runner


# ---------- Обработка шагов уже запущенных сценариев ----------

@dp.message(TarotFlow.choosing_topic)
async def tarot_topic_chosen(message: Message, state: FSMContext):
    if await _moderate_message(message, state):
        return
    text = message.text.lower()
    if "любов" in text or "отношен" in text:
        topic = "love"
    elif "работ" in text or "карьер" in text:
        topic = "career"
    else:
        topic = "general"

    await state.update_data(topic=topic)
    await state.set_state(TarotFlow.awaiting_question)
    await message.answer(
        "Сформулируй один конкретный вопрос для расклада. "
        "Например: «Что поможет мне улучшить отношения?»",
    )


@dp.message(FollowUpFlow.awaiting_reply)
async def follow_up_reply(message: Message, state: FSMContext):
    await _answer_follow_up(message, state)


@dp.callback_query(F.data.startswith("tarot_topic:"))
async def tarot_topic_button(callback, state: FSMContext):
    topic = callback.data.split(":", 1)[1]
    await state.update_data(topic=topic)
    await state.set_state(TarotFlow.awaiting_question)
    await callback.message.answer(
        "Хорошо. Теперь задай один конкретный вопрос, который тебя волнует. "
        "Я посмотрю на него через карты 🔮"
    )
    await callback.answer()


async def start_tarot_from_router(
    message: Message,
    state: FSMContext,
    topic: str,
) -> None:
    """Start a reading using the user's original message as the question."""
    question = _message_with_reply_context(message)
    if not question:
        await state.update_data(topic=topic)
        await state.set_state(TarotFlow.awaiting_question)
        await message.answer(
            "О чём хочешь спросить карты? Напиши один вопрос, который сейчас "
            "действительно важен 🔮"
        )
        return
    if not _has_tarot_question(question):
        await state.update_data(topic=topic if topic != "general" else None)
        await state.set_state(
            TarotFlow.awaiting_question if topic != "general" else TarotFlow.choosing_topic
        )
        if topic == "general":
            await message.answer(
                "Конечно 🔮 На какую тему посмотрим: отношения, работа, "
                "деньги или другая ситуация? Напиши своими словами."
            )
        else:
            await message.answer(
                "Хорошо, посмотрим эту тему. Какой конкретный вопрос хочешь "
                "задать картам?"
            )
        return
    await state.update_data(topic=topic)
    await state.set_state(TarotFlow.awaiting_question)
    await tarot_question_given(message, state)


@dp.message(TarotFlow.awaiting_question)
async def tarot_question_given(message: Message, state: FSMContext):
    if await _moderate_message(message, state):
        return
    question = _message_with_reply_context(message)
    if not question:
        await message.answer("Напиши вопрос текстом, чтобы я мог сделать расклад.")
        return

    quota_source = _check_and_consume_limit(message.from_user.id)
    if quota_source is None:
        await state.clear()
        await _quota_exhausted_message(message)
        return
    await record_message_with_summary(message.from_user.id, "user", question, _summarize_dialog)
    data = await state.get_data()
    topic = data.get("topic", "general")
    cards = tarot.draw_cards(3)
    await message.answer(
        "Вопрос принят. Делаю расклад из трёх карт: ситуация, совет и итог 🔮"
    )
    for position, (card_name, meaning) in zip(tarot.CARD_POSITIONS, cards):
        image_path = tarot.get_card_image_path(card_name)
        caption = f"{position}: {card_name}"
        if image_path:
            await message.answer_photo(FSInputFile(image_path), caption=caption)
        else:
            await message.answer(f"{caption}\n{meaning}")

    try:
        reply_text = await _run_with_wait_animation(
            message, tarot.build_tarot_reply(topic, question, cards)
        )
    except Exception:
        restore_request(message.from_user.id, quota_source)
        await state.clear()
        logger.exception("Tarot reply generation failed")
        await message.answer("Не удалось завершить расклад. Запрос возвращён на баланс, попробуй ещё раз.")
        return
    await _warn_if_two_requests_left(message)
    await record_message_with_summary(message.from_user.id, "assistant", reply_text, _summarize_dialog)
    reset_base_dialog_usage_today(message.from_user.id)
    await state.update_data(service_context="tarot", follow_up_count=0, base_dialog_count=0)
    await state.set_state(FollowUpFlow.awaiting_reply)
    await _answer_formatted(message, reply_text)


@dp.message(NatalFlow.awaiting_birth_date)
async def natal_date_given(message: Message, state: FSMContext):
    if await _moderate_message(message, state):
        return
    state_data = await state.get_data()
    chart_data = state_data.get("chart_data")
    parsed = natal.parse_birth_date(message.text or "")
    if chart_data and parsed is None:
        parsed = natal.parse_birth_date(get_birth_date(message.from_user.id) or "")
    if parsed is None:
        await message.answer(
            "Не понял дату 🤔 Напиши в формате ДД.ММ.ГГГГ, например 15.03.1995"
        )
        return

    if chart_data:
        chart_date = natal.parse_birth_date(chart_data.get("date", ""))
        warning = ""
        if chart_date and chart_date != parsed:
            warning = (
                f"⚠️ Внимание: на карте указана дата {chart_date.strftime('%d.%m.%Y')}, "
                f"а ты указал(а) {parsed.strftime('%d.%m.%Y')}. Вероятно, карта "
                "построена для других данных или содержит ошибку.\n\n"
            )
        if warning:
            await message.answer(warning + "Я не буду делать точный разбор по этой карте без подтверждения. Проверь данные и пришли карту заново.")
            await state.clear()
            return

    quota_source = _check_and_consume_limit(message.from_user.id)
    if quota_source is None:
        await state.clear()
        await _quota_exhausted_message(message)
        return
    save_birth_date(message.from_user.id, parsed.isoformat())
    user_message_text = message.text or message.caption or parsed.strftime("%d.%m.%Y")
    await record_message_with_summary(
        message.from_user.id,
        "user",
        _message_with_reply_context(message) or user_message_text,
        _summarize_dialog,
    )
    try:
        if chart_data:
            reply_text = await _run_with_wait_animation(
                message,
                natal.build_chart_photo_reply(
                    parsed,
                    chart_data,
                    state_data.get("chart_observation", ""),
                ),
            )
        else:
            reply_text = await _run_with_wait_animation(
                message, natal.build_natal_reply(parsed)
            )
    except Exception:
        restore_request(message.from_user.id, quota_source)
        await state.clear()
        logger.exception("Natal reply generation failed")
        await message.answer("Не удалось завершить разбор. Запрос возвращён на баланс, попробуй ещё раз.")
        return
    await _warn_if_two_requests_left(message)
    await record_message_with_summary(message.from_user.id, "assistant", reply_text, _summarize_dialog)
    reset_base_dialog_usage_today(message.from_user.id)
    await state.update_data(service_context="natal", follow_up_count=0, base_dialog_count=0)
    await state.set_state(FollowUpFlow.awaiting_reply)
    await _answer_formatted(message, reply_text)


# ---------- Точка входа: пользователь не в сценарии ----------

@dp.message()
async def free_dialog(message: Message, state: FSMContext):
    if await _moderate_message(message, state):
        return
    if not await _require_consent(message):
        return
    current_state = await state.get_state()
    if current_state is not None:
        # Теоретически сюда не должны попасть — все активные состояния
        # обработаны выше. Оставлено как страховка.
        return

    state_data = await state.get_data()
    base_dialog_count = base_dialog_responses_today(message.from_user.id)
    if base_dialog_count >= BASE_DIALOG_LIMIT:
        await message.answer(
            "Базовый диалог завершён: мы уже обсудили тему в общем формате. "
            "Чтобы продолжить, выбери персональный разбор: Таро, натальную "
            "карту или чтение кофейной гущи 🔮"
        )
        return

    history = await prepare_context(message.from_user.id, _summarize_dialog)
    user_text = _message_with_reply_context(message)
    await record_message_with_summary(
        message.from_user.id,
        "user",
        user_text,
        _summarize_dialog,
    )
    routed = await _run_with_wait_animation(
        message,
        route_message(
            user_text=user_text,
            history=history,
            tools=SCENARIO_TOOLS,
        ),
    )

    if routed["type"] == "text":
        response_number = consume_base_dialog_response(
            message.from_user.id, BASE_DIALOG_LIMIT
        )
        if response_number is None:
            await message.answer(
                "Базовый диалог завершён. Для продолжения выбери персональный "
                "разбор: Таро, натальную карту или кофейную гущу 🔮"
            )
            return
        await record_message_with_summary(message.from_user.id, "assistant", routed["text"], _summarize_dialog)
        await _answer_formatted(
            message,
            routed["text"] + _base_dialog_offer(response_number, user_text),
        )
        return

    name = routed["name"]
    args = routed["arguments"]

    if name == "start_tarot_reading":
        topic = args.get("topic")
        if topic:
            await start_tarot_from_router(message, state, topic)
        else:
            await start_tarot_from_router(message, state, "general")

    elif name == "start_natal_chart":
        birth_date_raw = args.get("birth_date")
        parsed = natal.parse_birth_date(birth_date_raw) if birth_date_raw else None
        if parsed:
            quota_source = _check_and_consume_limit(message.from_user.id)
            if quota_source is None:
                await _quota_exhausted_message(message)
                return
            save_birth_date(message.from_user.id, parsed.isoformat())
            await record_message_with_summary(
                message.from_user.id,
                "user",
                _message_with_reply_context(message),
                _summarize_dialog,
            )
            try:
                reply_text = await _run_with_wait_animation(
                    message, natal.build_natal_reply(parsed)
                )
            except Exception:
                restore_request(message.from_user.id, quota_source)
                logger.exception("Natal reply generation failed")
                await message.answer("Не удалось завершить разбор. Запрос возвращён на баланс, попробуй ещё раз.")
                return
            await _warn_if_two_requests_left(message)
            await record_message_with_summary(message.from_user.id, "assistant", reply_text, _summarize_dialog)
            reset_base_dialog_usage_today(message.from_user.id)
            await state.update_data(service_context="natal", follow_up_count=0, base_dialog_count=0)
            await state.set_state(FollowUpFlow.awaiting_reply)
            await _answer_formatted(message, reply_text)
        else:
            await state.set_state(NatalFlow.awaiting_birth_date)
            await message.answer(
                "Для разбора нужна дата рождения — когда ты родился? "
                "(формат ДД.ММ.ГГГГ)"
            )


async def main():
    logger.info("Запуск бота...")
    webhook_runner = await start_payment_webhook_server()
    summary_task = asyncio.create_task(_topup_summary_scheduler())
    try:
        await dp.start_polling(bot)
    finally:
        summary_task.cancel()
        await asyncio.gather(summary_task, return_exceptions=True)
        await webhook_runner.cleanup()
        await dp.storage.close()


if __name__ == "__main__":
    asyncio.run(main())
