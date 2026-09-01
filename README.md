# Routes & Roads Price Bot

Telegram-бот для `@routesroads_price_bot`. При команде `/start` (или `/check`) он
проверяет весь публичный каталог `routesandroads.fr` и присылает ссылки на товары,
у которых хотя бы один вариант имеет цену `0`.

## Быстрый запуск

Требуется Python 3.9 или новее. Сторонние библиотеки не нужны.

1. Получите токен бота у [@BotFather](https://t.me/BotFather).
2. В терминале перейдите в папку проекта.
3. Запустите:

   ```bash
   export TELEGRAM_BOT_TOKEN='токен_от_BotFather'
   python3 bot.py
   ```

4. Откройте `@routesroads_price_bot` и отправьте `/start`.

Токен нельзя публиковать или добавлять в Git. Для постоянной работы бот должен
быть запущен на сервере или хостинге 24/7.

## Ограничение доступа

По умолчанию проверку может запустить любой пользователь бота. Чтобы ограничить
доступ, задайте один или несколько числовых Telegram chat ID:

```bash
export ALLOWED_CHAT_IDS='123456789,987654321'
```

## Docker

```bash
docker build -t routesroads-price-bot .
docker run --restart unless-stopped \
  -e TELEGRAM_BOT_TOKEN='токен_от_BotFather' \
  routesroads-price-bot
```

## Проверка кода

```bash
python3 -m unittest -v
```

Сканер использует публичный Shopify-адрес `/products.json`, запрашивает до 250
товаров на страницу и продолжает, пока не дойдёт до конца каталога. В Telegram
выводятся только опубликованные в интернет-магазине товары.
