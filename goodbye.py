#!/usr/bin/env python3
"""Send farewell message and birthday backup to all bot users."""

import logging
import os
import sys
import time

import telebot
from dotenv import load_dotenv
from telebot.apihelper import ApiTelegramException

import db
import i18n
import utils

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s [%(filename)s:%(lineno)d]",
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler(),
    ],
)

load_dotenv()

GOODBYE_MESSAGE_RU = """Добрый день. Этот бот перестанет работать, так как Роскомнадзор отключил Telegram API для России
Возможности хостить его на заграничных серверах у меня пока нет
На время этот бот перестает работать, а в следующем сообщении присылаю все ваши данные, чтобы они не потерялись

Если можете помочь - пишите мне, @pchelka_zh

Спасибо за доверие боту"""

GOODBYE_MESSAGE_EN = """Good day. This bot will stop working because Roskomnadzor has blocked Telegram API access in Russia.
I currently don't have the ability to host it on servers outside Russia.
For now, this bot is shutting down, and in the next message I'm sending all your data so you won't lose it.

If you can help — please write to me at @pchelka_zh

Thank you for trusting the bot"""

SEND_DELAY_SECONDS = 0.05


def _load_token() -> str:
    prestable = os.getenv("PRESTABLE_MODE", "false").lower() == "true"
    if prestable:
        token = os.getenv("PRESTABLE_TELEGRAM_BOT_TOKEN")
        if not token:
            raise ValueError("PRESTABLE_TELEGRAM_BOT_TOKEN is not set in the .env file!")
    else:
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        if not token:
            raise ValueError("TELEGRAM_BOT_TOKEN is not set in the .env file!")
    return token


def _try_send(
    bot: telebot.TeleBot, chat_id: int, text: str, **kwargs
) -> str | None:
    """Send a message. Returns None on success, or an error label ('blocked', 'failed')."""
    while True:
        try:
            bot.send_message(chat_id, text, **kwargs)
            return None
        except ApiTelegramException as e:
            if e.error_code == 429:
                retry_after = getattr(e, "result_json", {}).get("parameters", {}).get(
                    "retry_after", 30
                )
                logging.warning(f"Rate limited, sleeping {retry_after}s")
                time.sleep(retry_after)
                continue
            if e.error_code == 403:
                logging.warning(f"User {chat_id} blocked the bot")
                return "blocked"
            if e.error_code == 400 and kwargs.get("parse_mode"):
                logging.warning(
                    f"Markdown send failed for {chat_id}, retrying as plain text"
                )
                return _try_send(bot, chat_id, text)
            logging.warning(f"Telegram API error {e.error_code} for {chat_id}: {e}")
            return "failed"
        except Exception as e:
            logging.warning(f"Failed to send to {chat_id}: {e}")
            return "failed"


def _notify_user(
    bot: telebot.TeleBot, chat_id: int, get_all_birthdays_formatted
) -> str:
    """Notify one user. Returns 'sent', 'blocked', or 'failed'. Never raises."""
    try:
        err = _try_send(bot, chat_id, GOODBYE_MESSAGE_RU)
        if err:
            return err
        time.sleep(SEND_DELAY_SECONDS)

        err = _try_send(bot, chat_id, GOODBYE_MESSAGE_EN)
        if err:
            return err
        time.sleep(SEND_DELAY_SECONDS)

        backup_text = get_all_birthdays_formatted(chat_id)
        backup_header = i18n.get_message("latest_backup", chat_id)
        full_backup = f"{backup_header}\n{backup_text}"

        for part in utils.split_message(full_backup):
            err = _try_send(bot, chat_id, part, parse_mode="Markdown")
            if err:
                return err
            time.sleep(SEND_DELAY_SECONDS)

        return "sent"
    except Exception as e:
        logging.warning(f"Unexpected error notifying user {chat_id}: {e}")
        utils.log_exception(e)
        return "failed"


def send_goodbye_to_all() -> None:
    from bot import get_all_birthdays_formatted

    bot = telebot.TeleBot(_load_token())
    chat_ids = db.get_all_distinct_chat_ids()

    print(f"Found {len(chat_ids)} users to notify.")
    confirm = input("Type YES to send goodbye messages: ")
    if confirm != "YES":
        print("Aborted.")
        return

    sent = 0
    failed = 0
    blocked = 0

    for chat_id in chat_ids:
        result = _notify_user(bot, chat_id, get_all_birthdays_formatted)
        if result == "sent":
            sent += 1
            if sent % 50 == 0:
                logging.info(f"Progress: {sent}/{len(chat_ids)} users notified")
        elif result == "blocked":
            blocked += 1
        else:
            failed += 1

    logging.info(
        f"Goodbye complete: sent={sent}, blocked={blocked}, failed={failed}, total={len(chat_ids)}"
    )


if __name__ == "__main__":
    db.init_db()
    try:
        send_goodbye_to_all()
    except ValueError as e:
        logging.critical(str(e))
        sys.exit(1)
