#!/usr/bin/env python3
"""Telegram bot that finds zero-priced products on routesandroads.fr."""

from __future__ import annotations

import html
import json
import logging
import os
import socket
import time
from dataclasses import dataclass
from datetime import datetime, time as clock_time
from decimal import Decimal, InvalidOperation
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


LOGGER = logging.getLogger("routesroads_price_bot")
DEFAULT_SITE_URL = "https://www.routesandroads.fr"
TELEGRAM_MESSAGE_LIMIT = 4096
SHOPIFY_PAGE_SIZE = 250
MAX_CATALOG_PAGES = 100


class BotError(RuntimeError):
    """An expected error that can be shown without exposing internals."""


@dataclass(frozen=True)
class ZeroPriceProduct:
    title: str
    url: str
    variants: tuple[str, ...]


@dataclass(frozen=True)
class ScanResult:
    checked_products: int
    zero_price_products: tuple[ZeroPriceProduct, ...]


def fetch_json(url: str, *, timeout: int = 35, attempts: int = 3) -> dict[str, Any]:
    """Download JSON with bounded retries for temporary network errors."""
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        request = Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "RoutesRoadsPriceBot/1.0 (+https://www.routesandroads.fr)",
            },
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                payload = json.load(response)
            if not isinstance(payload, dict):
                raise BotError("Сайт вернул данные в неожиданном формате.")
            return payload
        except HTTPError as exc:
            last_error = exc
            if exc.code == 429:
                retry_after = exc.headers.get("Retry-After", "2")
                try:
                    delay = min(max(float(retry_after), 1.0), 30.0)
                except ValueError:
                    delay = 2.0
            elif 500 <= exc.code < 600:
                delay = float(2 ** (attempt - 1))
            else:
                raise BotError(f"Сайт вернул ошибку HTTP {exc.code}.") from exc
        except (URLError, TimeoutError, socket.timeout, json.JSONDecodeError) as exc:
            last_error = exc
            delay = float(2 ** (attempt - 1))

        if attempt < attempts:
            time.sleep(delay)

    raise BotError("Не удалось получить каталог сайта после нескольких попыток.") from last_error


def scan_zero_price_products(
    site_url: str = DEFAULT_SITE_URL,
    *,
    fetcher: Callable[[str], dict[str, Any]] = fetch_json,
    page_size: int = SHOPIFY_PAGE_SIZE,
    max_pages: int = MAX_CATALOG_PAGES,
) -> ScanResult:
    """Scan every public Shopify product and report products with any zero variant."""
    base_url = site_url.rstrip("/")
    checked_products = 0
    found: dict[str, dict[str, Any]] = {}

    for page in range(1, max_pages + 1):
        endpoint = f"{base_url}/products.json?{urlencode({'limit': page_size, 'page': page})}"
        payload = fetcher(endpoint)
        products = payload.get("products")
        if not isinstance(products, list):
            raise BotError("В каталоге сайта отсутствует список товаров.")
        if not products:
            break

        checked_products += len(products)
        for product in products:
            if not isinstance(product, dict):
                continue
            handle = product.get("handle")
            title = product.get("title")
            variants = product.get("variants", [])
            if not isinstance(handle, str) or not handle or not isinstance(title, str):
                continue
            if not isinstance(variants, list):
                continue

            has_zero_variant = False
            zero_variants: list[str] = []
            for variant in variants:
                if not isinstance(variant, dict):
                    continue
                if _is_zero_price(variant.get("price")):
                    has_zero_variant = True
                    variant_title = variant.get("title")
                    if isinstance(variant_title, str) and variant_title != "Default Title":
                        zero_variants.append(variant_title)

            if has_zero_variant:
                product_url = f"{base_url}/products/{quote(handle, safe='-_')}"
                existing = found.setdefault(
                    product_url,
                    {"title": title, "variants": []},
                )
                existing["variants"].extend(zero_variants)

        if len(products) < page_size:
            break
    else:
        raise BotError("Каталог оказался больше установленного безопасного лимита страниц.")

    results = tuple(
        ZeroPriceProduct(
            title=data["title"],
            url=url,
            variants=tuple(dict.fromkeys(data["variants"])),
        )
        for url, data in found.items()
    )
    return ScanResult(checked_products=checked_products, zero_price_products=results)


def _is_zero_price(value: Any) -> bool:
    try:
        return Decimal(str(value)) == Decimal("0")
    except (InvalidOperation, ValueError):
        return False


def format_scan_result(result: ScanResult) -> list[str]:
    """Format a scan result as Telegram-safe HTML messages."""
    count = len(result.zero_price_products)
    if count == 0:
        return [
            "✅ <b>Проверка завершена.</b>\n\n"
            "Товаров с ценой 0 не найдено.\n"
            f"Проверено товаров: {result.checked_products}."
        ]

    heading = (
        "⚠️ <b>Найдены товары с ценой 0:</b> "
        f"{count}\nПроверено товаров: {result.checked_products}.\n\n"
    )
    items: list[str] = []
    for index, product in enumerate(result.zero_price_products, start=1):
        item = (
            f'{index}. <a href="{html.escape(product.url, quote=True)}">'
            f"{html.escape(product.title)}</a>"
        )
        if product.variants:
            variants = ", ".join(html.escape(name) for name in product.variants)
            item += f"\nВарианты: {variants}"
        items.append(item)

    return _chunk_messages(heading, items)


def _chunk_messages(heading: str, items: list[str]) -> list[str]:
    max_length = TELEGRAM_MESSAGE_LIMIT - 100
    chunks: list[str] = []
    current = heading
    for item in items:
        addition = item if current.endswith("\n\n") else f"\n\n{item}"
        if len(current) + len(addition) > max_length and current != heading:
            chunks.append(current)
            current = "<b>Продолжение списка:</b>\n\n" + item
        else:
            current += addition
    chunks.append(current)
    return chunks


class TelegramBot:
    def __init__(
        self,
        token: str,
        site_url: str,
        allowed_chat_ids: set[int] | None,
        *,
        target_chat_id: int | str | None = None,
        target_message_thread_id: int | None = None,
        schedule_time: clock_time | None = None,
        timezone: ZoneInfo | None = None,
    ) -> None:
        self.api_url = f"https://api.telegram.org/bot{token}"
        self.site_url = site_url
        self.allowed_chat_ids = allowed_chat_ids
        self.target_chat_id = target_chat_id
        self.target_message_thread_id = target_message_thread_id
        self.schedule_time = schedule_time
        self.timezone = timezone or ZoneInfo("Europe/Moscow")
        self.last_scheduled_date: str | None = None
        self.offset = 0

    def api_call(self, method: str, data: dict[str, Any], *, timeout: int = 45) -> Any:
        body = urlencode(data).encode("utf-8")
        request = Request(f"{self.api_url}/{method}", data=body)
        try:
            with urlopen(request, timeout=timeout) as response:
                payload = json.load(response)
        except (HTTPError, URLError, TimeoutError, socket.timeout, json.JSONDecodeError) as exc:
            raise BotError("Ошибка связи с Telegram.") from exc
        if not payload.get("ok"):
            raise BotError(f"Telegram отклонил запрос: {payload.get('description', 'неизвестная ошибка')}")
        return payload.get("result")

    def send_message(
        self,
        chat_id: int | str,
        text: str,
        *,
        message_thread_id: int | None = None,
    ) -> None:
        data: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }
        if message_thread_id is not None:
            data["message_thread_id"] = message_thread_id
        self.api_call("sendMessage", data)

    def report_destination(
        self, source_chat_id: int, source_message_thread_id: int | None
    ) -> tuple[int | str, int | None]:
        if self.target_chat_id is not None:
            return self.target_chat_id, self.target_message_thread_id
        return source_chat_id, source_message_thread_id

    def run_check(
        self,
        chat_id: int | str,
        message_thread_id: int | None,
        *,
        intro: str,
    ) -> None:
        self.send_message(chat_id, intro, message_thread_id=message_thread_id)
        try:
            result = scan_zero_price_products(self.site_url)
            for chunk in format_scan_result(result):
                self.send_message(chat_id, chunk, message_thread_id=message_thread_id)
        except BotError as exc:
            LOGGER.exception("Catalog scan failed")
            self.send_message(
                chat_id,
                f"❌ Проверка не завершена. {html.escape(str(exc))}",
                message_thread_id=message_thread_id,
            )

    def send_location_help(self, chat_id: int, message_thread_id: int | None) -> None:
        lines = [f"TARGET_CHAT_ID={chat_id}"]
        if message_thread_id is not None:
            lines.append(f"TARGET_MESSAGE_THREAD_ID={message_thread_id}")
        else:
            lines.append("# TARGET_MESSAGE_THREAD_ID не нужен: это не тема форума.")
        self.send_message(
            chat_id,
            "<b>Настройки для этого места:</b>\n<pre>"
            + html.escape("\n".join(lines))
            + "</pre>",
            message_thread_id=message_thread_id,
        )

    def handle_message(self, message: dict[str, Any]) -> None:
        chat = message.get("chat", {})
        chat_id = chat.get("id")
        text = message.get("text", "")
        if not isinstance(chat_id, int) or not isinstance(text, str):
            return

        command_parts = text.strip().split(maxsplit=1)
        if not command_parts:
            return
        command = command_parts[0].lower().split("@", maxsplit=1)[0]
        message_thread_id = message.get("message_thread_id")
        if not isinstance(message_thread_id, int):
            message_thread_id = None
        if command not in {"/start", "/check", "/where"}:
            return
        if self.allowed_chat_ids is not None and chat_id not in self.allowed_chat_ids:
            self.send_message(chat_id, "⛔ У вас нет доступа к запуску этой проверки.")
            return

        if command == "/where":
            self.send_location_help(chat_id, message_thread_id)
            return

        destination_chat_id, destination_thread_id = self.report_destination(
            chat_id, message_thread_id
        )
        if destination_chat_id != chat_id or destination_thread_id != message_thread_id:
            self.send_message(chat_id, "🔎 Запускаю проверку. Результат будет отправлен в настроенный чат.")
        self.run_check(
            destination_chat_id,
            destination_thread_id,
            intro="🔎 Начинаю проверку каталога routesandroads.fr…",
        )

    def run_scheduled_check_if_due(self, now: datetime | None = None) -> None:
        if self.target_chat_id is None or self.schedule_time is None:
            return
        now = now or datetime.now(self.timezone)
        date_marker = now.date().isoformat()
        if now.time().replace(tzinfo=None) < self.schedule_time:
            return
        if self.last_scheduled_date == date_marker:
            return

        self.last_scheduled_date = date_marker
        LOGGER.info("Starting scheduled catalog scan for %s", self.target_chat_id)
        self.run_check(
            self.target_chat_id,
            self.target_message_thread_id,
            intro="🕘 <b>Ежедневная проверка каталога — 09:00 МСК</b>",
        )

    def poll_timeout(self) -> int:
        if self.target_chat_id is None or self.schedule_time is None:
            return 30
        now = datetime.now(self.timezone)
        scheduled_today = datetime.combine(now.date(), self.schedule_time, tzinfo=self.timezone)
        seconds_until_schedule = (scheduled_today - now).total_seconds()
        if 0 < seconds_until_schedule < 30:
            return max(1, int(seconds_until_schedule))
        return 30

    def run(self) -> None:
        me = self.api_call("getMe", {})
        LOGGER.info("Bot @%s started", me.get("username", "unknown"))
        while True:
            try:
                self.run_scheduled_check_if_due()
                timeout = self.poll_timeout()
                updates = self.api_call(
                    "getUpdates",
                    {
                        "offset": self.offset,
                        "timeout": timeout,
                        "allowed_updates": json.dumps(["message"]),
                    },
                    timeout=timeout + 10,
                )
                for update in updates or []:
                    update_id = update.get("update_id")
                    if isinstance(update_id, int):
                        self.offset = max(self.offset, update_id + 1)
                    message = update.get("message")
                    if isinstance(message, dict):
                        self.handle_message(message)
            except BotError:
                LOGGER.exception("Polling failed; retrying")
                time.sleep(3)


def parse_allowed_chat_ids(raw_value: str | None) -> set[int] | None:
    if not raw_value:
        return None
    try:
        return {int(value.strip()) for value in raw_value.split(",") if value.strip()}
    except ValueError as exc:
        raise SystemExit("ALLOWED_CHAT_IDS должен содержать только числа через запятую.") from exc


def parse_target_chat_id(raw_value: str | None) -> int | str | None:
    if not raw_value:
        return None
    value = raw_value.strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        if value.startswith("@"):
            return value
        raise SystemExit("TARGET_CHAT_ID должен быть числовым ID или @username канала.")


def parse_optional_positive_int(raw_value: str | None, name: str) -> int | None:
    if not raw_value:
        return None
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise SystemExit(f"{name} должен быть целым числом.") from exc
    if value <= 0:
        raise SystemExit(f"{name} должен быть положительным числом.")
    return value


def parse_schedule_time(raw_value: str | None) -> clock_time | None:
    if not raw_value:
        return None
    try:
        return datetime.strptime(raw_value, "%H:%M").time()
    except ValueError as exc:
        raise SystemExit("SCHEDULE_TIME должен быть в формате ЧЧ:ММ, например 09:00.") from exc


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("Не задана переменная TELEGRAM_BOT_TOKEN.")
    site_url = os.getenv("SITE_URL", DEFAULT_SITE_URL)
    allowed_chat_ids = parse_allowed_chat_ids(os.getenv("ALLOWED_CHAT_IDS"))
    try:
        timezone = ZoneInfo(os.getenv("SCHEDULE_TIMEZONE", "Europe/Moscow"))
    except ZoneInfoNotFoundError as exc:
        raise SystemExit("Не найден часовой пояс SCHEDULE_TIMEZONE.") from exc
    TelegramBot(
        token,
        site_url,
        allowed_chat_ids,
        target_chat_id=parse_target_chat_id(os.getenv("TARGET_CHAT_ID")),
        target_message_thread_id=parse_optional_positive_int(
            os.getenv("TARGET_MESSAGE_THREAD_ID"), "TARGET_MESSAGE_THREAD_ID"
        ),
        schedule_time=parse_schedule_time(os.getenv("SCHEDULE_TIME", "09:00")),
        timezone=timezone,
    ).run()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        LOGGER.info("Bot stopped")
