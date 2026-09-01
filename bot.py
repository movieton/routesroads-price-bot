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
from decimal import Decimal, InvalidOperation
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


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
    def __init__(self, token: str, site_url: str, allowed_chat_ids: set[int] | None) -> None:
        self.api_url = f"https://api.telegram.org/bot{token}"
        self.site_url = site_url
        self.allowed_chat_ids = allowed_chat_ids
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

    def send_message(self, chat_id: int, text: str) -> None:
        self.api_call(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            },
        )

    def handle_message(self, message: dict[str, Any]) -> None:
        chat = message.get("chat", {})
        chat_id = chat.get("id")
        text = message.get("text", "")
        if not isinstance(chat_id, int) or not isinstance(text, str):
            return

        command = text.strip().split(maxsplit=1)[0].lower().split("@", maxsplit=1)[0]
        if command not in {"/start", "/check"}:
            return
        if self.allowed_chat_ids is not None and chat_id not in self.allowed_chat_ids:
            self.send_message(chat_id, "⛔ У вас нет доступа к запуску этой проверки.")
            return

        self.send_message(chat_id, "🔎 Начинаю проверку каталога routesandroads.fr…")
        try:
            result = scan_zero_price_products(self.site_url)
            for chunk in format_scan_result(result):
                self.send_message(chat_id, chunk)
        except BotError as exc:
            LOGGER.exception("Catalog scan failed")
            self.send_message(chat_id, f"❌ Проверка не завершена. {html.escape(str(exc))}")

    def run(self) -> None:
        me = self.api_call("getMe", {})
        LOGGER.info("Bot @%s started", me.get("username", "unknown"))
        while True:
            try:
                updates = self.api_call(
                    "getUpdates",
                    {
                        "offset": self.offset,
                        "timeout": 30,
                        "allowed_updates": json.dumps(["message"]),
                    },
                    timeout=40,
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
    TelegramBot(token, site_url, allowed_chat_ids).run()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        LOGGER.info("Bot stopped")
