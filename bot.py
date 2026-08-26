import enum
import logging
import os
import signal
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone

import telebot
from dotenv import load_dotenv
from requests.exceptions import ConnectionError, ReadTimeout
from telebot.apihelper import ApiTelegramException
from telebot.types import (InlineKeyboardButton, InlineKeyboardMarkup,
                           ReplyKeyboardRemove, ReplyKeyboardMarkup, KeyboardButton)

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

# Check if prestable mode is enabled
PRESTABLE_MODE = os.getenv("PRESTABLE_MODE", "false").lower() == "true"

if PRESTABLE_MODE:
    TOKEN = os.getenv("PRESTABLE_TELEGRAM_BOT_TOKEN")
    if not TOKEN:
        logging.critical("PRESTABLE_TELEGRAM_BOT_TOKEN is not set in the .env file!")
        raise ValueError("PRESTABLE_TELEGRAM_BOT_TOKEN is not set in the .env file!")
    logging.info("🧪 Running in PRESTABLE mode")
else:
    TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    if not TOKEN:
        logging.critical("TELEGRAM_BOT_TOKEN is not set in the .env file!")
        raise ValueError("TELEGRAM_BOT_TOKEN is not set in the .env file!")
    logging.info("🚀 Running in PRODUCTION mode")

bot = telebot.TeleBot(TOKEN)


def setup_bot_commands():
    """Register the bot's command menu (shown when users type '/' in Telegram).

    This must be called after polling has been (re)started, otherwise it can
    race with Telegram tearing down the previous getUpdates connection and
    raise a 409 Conflict error.
    """
    commands = [
        telebot.types.BotCommand("start", "Запустить бота"),
        telebot.types.BotCommand("backup", "Резервная копия дней рождения"),
        telebot.types.BotCommand("add", "Добавить день рождения"),
        telebot.types.BotCommand("remove", "Удалить день рождения"),
        telebot.types.BotCommand("stats", "Статистика дней рождения"),
        telebot.types.BotCommand("settings", "Настройки бота"),
        telebot.types.BotCommand("support", "Поддержать автора"),
        telebot.types.BotCommand("language", "Изменить язык"),
    ]
    try:
        bot.set_my_commands(commands)
        logging.info("Bot command menu registered successfully")
    except ApiTelegramException as e:
        if getattr(e, "error_code", None) == 409:
            logging.warning(
                f"409 Conflict while setting bot commands (another instance may "
                f"still be shutting down): {e}"
            )
        else:
            logging.error(f"Failed to set bot commands: {e}")
            utils.log_exception(e)
    except Exception as e:
        logging.error(f"Unexpected error while setting bot commands: {e}")
        utils.log_exception(e)


user_states = {}

# Global dictionary to track messages related to birthday registration
birthday_registration_messages = defaultdict(set)
# Global dictionary to track messages related to birthday deletion
birthday_deletion_messages = defaultdict(list)
# Global dictionary to track messages related to backup registration
register_backup_messages = defaultdict(list)

REMINDED_DAYS = [0, 1, 3, 7]


class TUserState(enum.Enum):
    Default = "default"
    AwaitingInterval = "awaiting_interval"
    AwaitingDeletion = "awaiting_deletion"
    AwaitingBirthday = "awaiting_birthday"


class TCommand(enum.Enum):
    Start = "start"
    Backup = "backup"
    RegisterBirthday = "register_birthday"
    RegisterBackup = "register_backup"
    UnregisterBackup = "unregister_backup"
    DeleteBirthday = "delete_birthday"
    Stats = "stats"
    Share = "share"
    Language = "language"
    Support = "support"
    Settings = "settings"


# Command mappings for text commands.
# Keys must be lowercase, without the leading slash and without the
# "@botusername" suffix -- see extract_command() below, which normalizes
# incoming text before looking it up here.
COMMAND_MAPPINGS = {
    "start": TCommand.Start,
    "help": TCommand.Start,
    "menu": TCommand.Start,
    "backup": TCommand.Backup,
    "register_birthday": TCommand.RegisterBirthday,
    "add": TCommand.RegisterBirthday,
    "register_backup": TCommand.RegisterBackup,
    "unregister_backup": TCommand.UnregisterBackup,
    "delete_birthday": TCommand.DeleteBirthday,
    "remove": TCommand.DeleteBirthday,
    "delete": TCommand.DeleteBirthday,
    "share": TCommand.Share,
    "stats": TCommand.Stats,
    "support": TCommand.Support,
    "settings": TCommand.Settings,
    "language": TCommand.Language,
}


def extract_command(text: str) -> str | None:
    """Normalize a slash command out of raw message text.

    Telegram appends "@botusername" to commands sent in group chats (and
    whenever a command is tapped from the command menu there), so a plain
    string comparison against "/start" silently fails. This strips that
    suffix, drops any arguments and lowercases the result.

    Returns the bare command name (no leading slash), or None if the text
    is not a command.

    Examples:
        "/start"                       -> "start"
        "/start@FalconZZZ_bdatebot"    -> "start"
        "/Add@SomeBot extra args"      -> "add"
        "hello"                        -> None
    """
    if not text:
        return None

    text = text.strip()
    if not text.startswith("/"):
        return None

    # Keep only the first token: "/remove 1, 2" -> "/remove"
    command = text.split(maxsplit=1)[0]
    # Drop the leading slash
    command = command[1:]
    # Drop the "@botusername" suffix that Telegram adds in group chats
    command = command.split("@", 1)[0]

    return command.lower() or None


def get_button_to_command_mapping(chat_id: int) -> dict:
    """Get button text to command mapping for specific user's language"""
    return {
        i18n.get_button_text("start", chat_id): TCommand.Start,
        i18n.get_button_text("backup", chat_id): TCommand.Backup,
        i18n.get_button_text("register_birthday", chat_id): TCommand.RegisterBirthday,
        i18n.get_button_text("register_backup", chat_id): TCommand.RegisterBackup,
        i18n.get_button_text("unregister_backup", chat_id): TCommand.UnregisterBackup,
        i18n.get_button_text("delete_birthday", chat_id): TCommand.DeleteBirthday,
        i18n.get_button_text("share", chat_id): TCommand.Share,
        i18n.get_button_text("stats", chat_id): TCommand.Stats,
        i18n.get_button_text("settings", chat_id): TCommand.Settings,
        i18n.get_button_text("support", chat_id): TCommand.Support,
    }


def get_command_descriptions(chat_id: int) -> dict:
    """Get command descriptions for specific user's language"""
    return {
        i18n.get_button_text("start", chat_id): i18n.get_button_description(
            "start", chat_id
        ),
        i18n.get_button_text("register_birthday", chat_id): i18n.get_button_description(
            "register_birthday", chat_id
        ),
        i18n.get_button_text("delete_birthday", chat_id): i18n.get_button_description(
            "delete_birthday", chat_id
        ),
        i18n.get_button_text("backup", chat_id): i18n.get_button_description(
            "backup", chat_id
        ),
        i18n.get_button_text("register_backup", chat_id): i18n.get_button_description(
            "register_backup", chat_id
        ),
        i18n.get_button_text("unregister_backup", chat_id): i18n.get_button_description(
            "unregister_backup", chat_id
        ),
        i18n.get_button_text("share", chat_id): i18n.get_button_description(
            "share", chat_id
        ),
        i18n.get_button_text("stats", chat_id): i18n.get_button_description(
            "stats", chat_id
        ),
        i18n.get_button_text("settings", chat_id): i18n.get_button_description(
            "settings", chat_id
        ),
        i18n.get_button_text("support", chat_id): i18n.get_button_description(
            "support", chat_id
        ),
    }


def get_all_birthdays(chat_id: int, need_id: bool = False) -> str:
    return "\n".join(db.get_all_birthdays(chat_id, need_id))


def is_group_chat(message) -> bool:
    return message.chat.type in ["group", "supergroup"]


def safe_delete_message(chat_id, message_id):
    """Safely delete a message, ignoring errors when the message
    is already deleted or otherwise inaccessible.

    Catches telebot.apihelper.ApiTelegramException and silently
    ignores it when the error code is 400 (Bad Request, e.g.
    "message can't be deleted" or "message to delete not found").
    Any other unexpected exception is re-raised.
    """
    try:
        bot.delete_message(chat_id, message_id)
    except telebot.apihelper.ApiTelegramException as e:
        if getattr(e, "error_code", None) == 400:
            logging.debug(
                f"Could not delete message {message_id} in chat {chat_id}: {e}"
            )
        else:
            logging.warning(
                f"Unexpected error deleting message {message_id} in chat {chat_id}: {e}"
            )
            raise


def remove_keyboard(message):
    delete_message = bot.send_message(
        message.chat.id,
        i18n.get_message("keyboard_removed", message.chat.id),
        reply_markup=ReplyKeyboardRemove(),
    )
    safe_delete_message(delete_message.chat.id, delete_message.message_id)


def get_persistent_menu(message) -> ReplyKeyboardMarkup:
    """Build the persistent menu shown under the text input field.

    Used in both private and group chats so the menu never disappears
    after /start.
    """
    chat_id = message.chat.id

    markup = ReplyKeyboardMarkup(row_width=2, resize_keyboard=True)
    buttons = [
        KeyboardButton(i18n.get_button_text("register_birthday", chat_id)),
        KeyboardButton(i18n.get_button_text("delete_birthday", chat_id)),
        KeyboardButton(i18n.get_button_text("backup", chat_id)),
        KeyboardButton(i18n.get_button_text("stats", chat_id)),
        KeyboardButton(i18n.get_button_text("share", chat_id)),
        KeyboardButton(i18n.get_button_text("settings", chat_id)),
    ]
    markup.add(*buttons)
    return markup


def get_reply_markup(message) -> InlineKeyboardMarkup:
    """Build the inline keyboard attached under the welcome message.

    Shown in both private and group chats.
    """
    chat_id = message.chat.id

    markup = InlineKeyboardMarkup()
    buttons = [
        InlineKeyboardButton(
            i18n.get_button_text("start", chat_id), callback_data="start"
        ),
        InlineKeyboardButton(
            i18n.get_button_text("backup", chat_id), callback_data="backup"
        ),
        InlineKeyboardButton(
            i18n.get_button_text("register_birthday", chat_id),
            callback_data="register_birthday",
        ),
        InlineKeyboardButton(
            i18n.get_button_text("register_backup", chat_id),
            callback_data="register_backup",
        ),
        InlineKeyboardButton(
            i18n.get_button_text("delete_birthday", chat_id),
            callback_data="delete_birthday",
        ),
        InlineKeyboardButton(
            i18n.get_button_text("unregister_backup", chat_id),
            callback_data="unregister_backup",
        ),
        InlineKeyboardButton(
            i18n.get_button_text("share", chat_id), callback_data="share"
        ),
        InlineKeyboardButton(
            i18n.get_button_text("stats", chat_id), callback_data="stats"
        ),
        InlineKeyboardButton(
            i18n.get_button_text("settings", chat_id), callback_data="settings"
        ),
        InlineKeyboardButton(
            i18n.get_button_text("support", chat_id), callback_data="support"
        ),
    ]
    for i in range(0, len(buttons), 2):
        markup.row(*buttons[i : (i + 2)])
    return markup


def get_language_keyboard() -> InlineKeyboardMarkup:
    """Create language selection keyboard"""
    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton("🇬🇧 English", callback_data="lang_en"),
        InlineKeyboardButton("🇷🇺 Русский", callback_data="lang_ru"),
    )
    return markup


def get_settings_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    """Create settings menu keyboard"""
    markup = InlineKeyboardMarkup()
    notification_hour = db.get_notification_hour(chat_id)
    markup.row(
        InlineKeyboardButton(
            i18n.get_message(
                "settings_notif_time", chat_id, hour=f"{notification_hour:02d}"
            ),
            callback_data="settings_notif_time",
        )
    )
    markup.row(
        InlineKeyboardButton(
            i18n.get_button_text("language", chat_id),
            callback_data="settings_language",
        )
    )
    markup.row(
        InlineKeyboardButton(
            i18n.get_message("settings_back", chat_id),
            callback_data="settings_back",
        )
    )
    return markup


def get_notification_time_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    """Create notification time picker keyboard (all 24 UTC hours)"""
    markup = InlineKeyboardMarkup()
    current_hour = db.get_notification_hour(chat_id)

    buttons = []
    for hour in range(24):
        label = f"{'✅ ' if hour == current_hour else ''}{hour:02d}:00"
        buttons.append(
            InlineKeyboardButton(label, callback_data=f"settings_hour_{hour}")
        )

    for i in range(0, len(buttons), 4):
        markup.row(*buttons[i : i + 4])

    markup.row(
        InlineKeyboardButton(
            i18n.get_message("settings_back", chat_id),
            callback_data="settings_back_main",
        )
    )
    return markup


def get_support_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    """Create support keyboard with star amounts"""
    markup = InlineKeyboardMarkup()
    star_amounts = [50, 100, 250, 500, 1000]
    buttons = []
    for amount in star_amounts:
        buttons.append(
            InlineKeyboardButton(
                i18n.get_message(f"stars_amount_{amount}", chat_id),
                callback_data=f"support_pay_{amount}",
            )
        )
    # Add buttons in rows of 2
    for i in range(0, len(buttons), 2):
        if i + 1 < len(buttons):
            markup.row(buttons[i], buttons[i + 1])
        else:
            markup.row(buttons[i])
    return markup


def get_reminder_settings_keyboard(chat_id) -> InlineKeyboardMarkup:
    current_settings = db.get_reminder_settings(chat_id) or []

    markup = InlineKeyboardMarkup()

    reminder_buttons = [
        InlineKeyboardButton(
            f"{'✅' if days in current_settings else '❌'} {days} {i18n.get_message('days', chat_id)}",
            callback_data=f"reminder_{days}",
        )
        for days in REMINDED_DAYS
    ]

    markup.row(reminder_buttons[0], reminder_buttons[1])
    markup.row(reminder_buttons[2], reminder_buttons[3])

    return markup


def handle_stats(message):
    chat_id = message.chat.id

    # Retrieve pre-formatted birthday strings used for display.
    local_birthdays = db.get_all_birthdays(chat_id)
    global_birthdays = db.get_all_birthdays_for_all_chats()

    total_birthdays_for_this_chat = len(local_birthdays)
    total_birthdays_for_all_chats = len(global_birthdays)
    total_users = len(set(chat_id for chat_id, in db.get_all_chat_ids()))

    # Calculate birthdays in the current month for the local chat.
    current_month = datetime.now().month
    birthdays_this_month = []
    for birthday in local_birthdays:
        date_str = birthday.split(", ")[0]
        try:
            date = datetime.strptime(date_str, "%d %B %Y")
        except ValueError:
            date = datetime.strptime(date_str, "%d %B")

        if date.month == current_month:
            birthdays_this_month.append(birthday)
    total_birthdays_this_month = len(birthdays_this_month)

    # Helper function to extract the month name from a birthday string.
    def extract_month(birthday_line: str) -> str:
        try:
            date_part = birthday_line.split(",")[0].strip()
            tokens = date_part.split(" ")
            if len(tokens) >= 2:
                return tokens[1]
            return ""
        except Exception:
            return ""

    # Calculate the most popular birthday month locally.
    local_month_counts = {}
    for birthday in local_birthdays:
        month = extract_month(birthday)
        if month:
            local_month_counts[month] = local_month_counts.get(month, 0) + 1

    if local_month_counts:
        local_most_popular_month, local_count = max(
            local_month_counts.items(), key=lambda x: x[1]
        )
    else:
        local_most_popular_month, local_count = "N/A", 0

    # Calculate the most popular birthday month globally.
    global_month_counts = {}
    for birthday in global_birthdays:
        month = extract_month(birthday)
        if month:
            global_month_counts[month] = global_month_counts.get(month, 0) + 1

    if global_month_counts:
        global_most_popular_month, global_count = max(
            global_month_counts.items(), key=lambda x: x[1]
        )
    else:
        global_most_popular_month, global_count = "N/A", 0

    # Compute Age Statistics using the already given birthday strings.
    # Only birthdays with a full_date (i.e. %d %B %Y format) are considered.
    (
        avg_age_local,
        min_age_local,
        max_age_local,
        median_age_local,
    ) = utils.compute_age_metrics(local_birthdays)
    (
        avg_age_global,
        min_age_global,
        max_age_global,
        median_age_global,
    ) = utils.compute_age_metrics(global_birthdays)

    # Find most popular dates (day + month)
    most_popular_date_local, popular_date_count_local = utils.find_most_popular_date(
        local_birthdays
    )
    most_popular_date_global, popular_date_count_global = utils.find_most_popular_date(
        global_birthdays
    )

    if avg_age_local is not None:
        local_age_stats = (
            f"• Age Statistics:\n"
            f"   - Average Age: {avg_age_local:.1f}\n"
            f"   - Median Age: {median_age_local:.1f}\n"
            f"   - Minimum Age: {min_age_local}\n"
            f"   - Maximum Age: {max_age_local}\n"
        )
    else:
        local_age_stats = "• Age Statistics: N/A (no birthdays with full date)\n"

    if avg_age_global is not None:
        global_age_stats = (
            f"• Age Statistics:\n"
            f"   - Average Age: {avg_age_global:.1f}\n"
            f"   - Median Age: {median_age_global:.1f}\n"
            f"   - Minimum Age: {min_age_global}\n"
            f"   - Maximum Age: {max_age_global}\n"
        )
    else:
        global_age_stats = "• Age Statistics: N/A (no birthdays with full date)\n"

    # Assemble the local statistics.
    local_popular_date_str = ""
    if most_popular_date_local:
        local_popular_date_str = f"• Most Popular Date: {most_popular_date_local} ({popular_date_count_local} birthdays)\n"
    else:
        local_popular_date_str = "• Most Popular Date: N/A\n"

    local_stats = (
        "📍 *Local Statistics:*\n\n"
        f"• Total Birthdays in this Chat: {total_birthdays_for_this_chat}\n"
        f"• Birthdays in this Month: {total_birthdays_this_month}\n"
        f"• Most Popular Birthday Month: {local_most_popular_month} ({local_count} birthdays)\n"
        f"{local_popular_date_str}"
        f"{local_age_stats}"
    )

    # Assemble the global statistics.
    global_popular_date_str = ""
    if most_popular_date_global:
        global_popular_date_str = f"• Most Popular Date: {most_popular_date_global} ({popular_date_count_global} birthdays)\n"
    else:
        global_popular_date_str = "• Most Popular Date: N/A\n"

    global_stats = (
        "🌐 *Global Statistics:*\n\n"
        f"• Total Birthdays in All Chats: {total_birthdays_for_all_chats}\n"
        f"• Total Users: {total_users}\n"
        f"• Most Popular Birthday Month: {global_most_popular_month} ({global_count} birthdays)\n"
        f"{global_popular_date_str}"
        f"{global_age_stats}"
    )

    stats_message = f"{local_stats}\n{global_stats}"

    bot.send_message(
        chat_id,
        stats_message,
        parse_mode="Markdown",
    )

    user_states[chat_id] = TUserState.Default


def handle_start(message):
    chat_id = message.chat.id
    user_states[chat_id] = TUserState.Default

    # remove /start command itself (safely ignore errors)
    safe_delete_message(chat_id, message.message_id)

    backup_ping_settings = db.select_from_backup_ping(chat_id)
    if backup_ping_settings.is_active:
        backup_ping_msg = (
            i18n.get_message(
                "backup_ping_active",
                chat_id,
                interval=backup_ping_settings.update_timedelta,
            )
            + "\n"
        )
    else:
        backup_ping_msg = i18n.get_message("backup_ping_inactive", chat_id) + "\n"

    commands_descriptions = get_command_descriptions(chat_id)
    commands_msg = "\n".join(
        [
            f"{command}: {description}"
            for command, description in commands_descriptions.items()
        ]
    )

    welcome_message = f"""
{i18n.get_message("welcome_title", chat_id)}

{i18n.get_message("welcome_subtitle", chat_id)}

{i18n.get_message("what_can_bot_do", chat_id)}
{i18n.get_message("bot_features", chat_id)}

{i18n.get_message("how_to_use", chat_id)}
{i18n.get_message("how_to_use_steps", chat_id)}

{i18n.get_message("available_commands", chat_id)}
{commands_msg}

{backup_ping_msg}

"""

    bot.send_message(
        chat_id,
        welcome_message,
        reply_markup=get_reply_markup(message),
        parse_mode="Markdown",
    )
    logging.debug(f"Sent welcome message to Chat ID {chat_id}")

    # Only show reminder settings in private chats
    if not is_group_chat(message):
        bot.send_message(
            chat_id,
            f"{i18n.get_message('configure_reminders', chat_id)}\n\n{i18n.get_message('reminder_example', chat_id)}",
            reply_markup=get_reminder_settings_keyboard(chat_id),
            parse_mode="Markdown",
        )

    # A message can carry only one reply_markup, so the persistent menu
    # (the buttons under the input field) is attached to a final short
    # message. Sent in both private and group chats.
    bot.send_message(
        chat_id,
        i18n.get_message("menu_ready", chat_id),
        reply_markup=get_persistent_menu(message),
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("reminder_"))
def handle_reminder_callback(call):
    days = int(call.data.split("_")[1])
    chat_id = call.message.chat.id

    current_settings = db.get_reminder_settings(chat_id) or []

    if days in current_settings:
        current_settings.remove(days)
    else:
        current_settings.append(days)

    db.update_reminder_settings(chat_id, current_settings)

    bot.edit_message_reply_markup(
        chat_id=chat_id,
        message_id=call.message.message_id,
        reply_markup=get_reminder_settings_keyboard(chat_id),
    )

    status = i18n.get_message(
        "reminder_enabled" if days in current_settings else "reminder_disabled", chat_id
    )
    bot.answer_callback_query(
        call.id,
        f"{status} {days}{i18n.get_message('reminder_days_suffix', chat_id)}",
    )

    user_states[call.message.chat.id] = TUserState.Default


@bot.callback_query_handler(func=lambda call: call.data.startswith("lang_"))
def handle_language_callback(call):
    language_code = call.data.split("_")[1]
    chat_id = call.message.chat.id

    if i18n.set_user_language(chat_id, language_code):
        # Send confirmation message
        bot.send_message(
            chat_id,
            i18n.get_message("language_changed", chat_id),
            parse_mode="Markdown",
        )

        # Refresh the main menu with new language
        handle_start(call.message)

    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("settings_"))
def handle_settings_callback(call):
    """Handle settings menu interactions"""
    chat_id = call.message.chat.id

    if call.data == "settings_notif_time":
        bot.edit_message_text(
            i18n.get_message("settings_notif_time_title", chat_id),
            chat_id=chat_id,
            message_id=call.message.message_id,
            reply_markup=get_notification_time_keyboard(chat_id),
            parse_mode="Markdown",
        )
        bot.answer_callback_query(call.id)

    elif call.data == "settings_language":
        bot.edit_message_reply_markup(
            chat_id=chat_id,
            message_id=call.message.message_id,
            reply_markup=get_language_keyboard(),
        )
        bot.answer_callback_query(call.id)

    elif call.data.startswith("settings_hour_"):
        hour = int(call.data.split("_")[2])
        db.set_notification_hour(chat_id, hour)
        bot.edit_message_reply_markup(
            chat_id=chat_id,
            message_id=call.message.message_id,
            reply_markup=get_notification_time_keyboard(chat_id),
        )
        bot.answer_callback_query(
            call.id,
            i18n.get_message("settings_notif_time_set", chat_id, hour=f"{hour:02d}"),
        )

    elif call.data == "settings_back_main":
        bot.edit_message_text(
            i18n.get_message("settings_title", chat_id),
            chat_id=chat_id,
            message_id=call.message.message_id,
            reply_markup=get_settings_keyboard(chat_id),
            parse_mode="Markdown",
        )
        bot.answer_callback_query(call.id)

    elif call.data == "settings_back":
        handle_start(call.message)
        bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("support_pay_"))
def handle_support_payment_callback(call):
    """Handle payment button clicks"""
    try:
        amount = int(call.data.split("_")[2])
        chat_id = call.message.chat.id

        # Send invoice for digital goods and services
        bot.send_invoice(
            chat_id=chat_id,
            title=i18n.get_message("payment_invoice_title", chat_id),
            description=i18n.get_message("payment_invoice_description", chat_id),
            invoice_payload=f"support_donation_{amount}",
            provider_token="",  # Empty for Telegram Stars
            currency="XTR",  # Telegram Stars currency
            prices=[telebot.types.LabeledPrice(label="Support", amount=amount)],
        )

        bot.answer_callback_query(call.id)
    except Exception as e:
        logging.error(f"Error sending invoice: {e}")
        utils.log_exception(e)
        bot.answer_callback_query(
            call.id, "❌ Error creating invoice. Please try again later."
        )


@bot.pre_checkout_query_handler(func=lambda query: True)
def handle_pre_checkout_query(pre_checkout_query):
    """Handle pre-checkout queries for Telegram Stars payments"""
    try:
        # Always approve the order for digital goods
        # You can add additional validation here if needed
        bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)
        logging.info(
            f"Pre-checkout approved for user {pre_checkout_query.from_user.id}, "
            f"amount: {pre_checkout_query.total_amount} {pre_checkout_query.currency}"
        )
    except Exception as e:
        logging.error(f"Error in pre-checkout: {e}")
        utils.log_exception(e)
        # In case of error, reject with a message
        bot.answer_pre_checkout_query(
            pre_checkout_query.id,
            ok=False,
            error_message="Sorry, we couldn't process your payment. Please try again later.",
        )


@bot.message_handler(content_types=["successful_payment"])
def handle_successful_payment(message):
    """Handle successful payment notifications"""
    try:
        chat_id = message.chat.id
        payment = message.successful_payment

        # Log the payment
        logging.info(
            f"Successful payment from user {chat_id}: "
            f"{payment.total_amount} {payment.currency}, "
            f"invoice_payload: {payment.invoice_payload}, "
            f"telegram_payment_charge_id: {payment.telegram_payment_charge_id}"
        )

        # Send thank you message
        bot.send_message(
            chat_id,
            i18n.get_message("payment_success", chat_id, amount=payment.total_amount),
            parse_mode="Markdown",
        )

        user_states[chat_id] = TUserState.Default

    except Exception as e:
        logging.error(f"Error handling successful payment: {e}")
        utils.log_exception(e)


def parse_display_date(date_str: str) -> tuple:
    """Split a date as rendered by TBirthday.__str__ ("09 мая 2018").

    Returns (day, month, year); year is None for entries stored without
    one.

    datetime.strptime("%d %B") cannot do this: %B only understands the
    process locale (English inside the container), so it began raising
    ValueError the moment months started rendering in Russian and took
    /backup down with it. Returning parts rather than a datetime also
    avoids constructing 29 February in the yearless case, where the
    placeholder year 1900 is not a leap year.
    """
    parts = date_str.split()
    if len(parts) not in (2, 3):
        raise ValueError(f"Unrecognized date string: {date_str!r}")

    month = i18n.parse_month_name(parts[1])
    if month is None:
        raise ValueError(f"Unknown month name in {date_str!r}")

    day = int(parts[0])
    year = int(parts[2]) if len(parts) == 3 else None
    return day, month, year


def get_all_birthdays_formatted(chat_id: int, need_id: bool = False) -> str:
    all_birthdays = get_all_birthdays(chat_id, need_id)

    if not all_birthdays:
        return i18n.get_message("no_birthdays", chat_id)

    birthdays_by_month = {}
    for line in all_birthdays.split("\n"):
        date_str = line.split(",")[0].strip()
        _, month_number, _ = parse_display_date(date_str)
        # Section headers take the nominative form ("Август"), unlike the
        # genitive used inside a date ("26 августа 1996").
        translated_month = i18n.get_month_name(
            i18n.MONTH_KEYS[month_number - 1], chat_id
        )
        if translated_month not in birthdays_by_month:
            birthdays_by_month[translated_month] = []
        birthdays_by_month[translated_month].append(line)

    markdown_message = f"{i18n.get_message('your_birthdays', chat_id)}\n\n"
    for month, birthdays in birthdays_by_month.items():
        markdown_message += f"*{month}*\n"
        for birthday in birthdays:
            markdown_message += f"- {birthday}\n"
        markdown_message += "\n"

    user_states[chat_id] = TUserState.Default

    return markdown_message


def get_all_birthdays_for_share(chat_id: int) -> str:
    all_birthdays = get_all_birthdays(chat_id)

    if not all_birthdays:
        return "Nothing found"

    formatted_birthdays = []
    for line in all_birthdays.split("\n"):
        date_str, name, *rest = line.split(", ")
        day, month, year = parse_display_date(date_str)
        if year is not None and year != utils.DEFAULT_BD_YEAR:
            formatted_date = f"{day:02d}.{month:02d}.{year}"
        else:
            formatted_date = f"{day:02d}.{month:02d}"
        formatted_birthdays.append(name)
        formatted_birthdays.append(formatted_date)

    return "\n".join(formatted_birthdays)


def send_backup(message):
    all_birthdays = get_all_birthdays_formatted(message.chat.id)
    birthdays_messages = utils.split_message(all_birthdays)

    for birthday_message in birthdays_messages:
        bot.send_message(
            message.chat.id,
            birthday_message,
            parse_mode="Markdown",
        )

    user_states[message.chat.id] = TUserState.Default


def process_birthday_pings():
    while True:
        minutes = 5
        time.sleep(minutes * 60)

        try:
            # Reset flags for birthdays that are far from current date
            db.reset_birthday_reminder_flags()

            current_hour_utc = datetime.now(timezone.utc).hour

            for days in REMINDED_DAYS:
                upcoming_birthdays = db.get_upcoming_birthdays(days)

                for id, chat_id, name, birthday_str, has_year in upcoming_birthdays:
                    try:
                        user_settings = db.get_reminder_settings(chat_id)
                        if not user_settings:
                            continue

                        notification_hour = db.get_notification_hour(chat_id)
                        if not utils.is_in_notification_window(
                            current_hour_utc, notification_hour
                        ):
                            continue

                        birthday = datetime.strptime(birthday_str, "%Y-%m-%d")
                        current_year = datetime.now().year
                        birthday_this_year = db._safe_replace_year(
                            birthday, current_year
                        )

                        today = datetime.now().replace(
                            hour=0, minute=0, second=0, microsecond=0
                        )
                        days_until = (birthday_this_year - today).days

                        if days_until not in user_settings:
                            continue

                        age_text = ""
                        if has_year:
                            age = current_year - birthday.year
                            age_text = i18n.get_message("age_suffix", chat_id, age=age)

                        if days_until == 0:
                            bot.send_message(chat_id, "🎂")
                            reminder_text = i18n.get_message(
                                "today_birthday",
                                chat_id,
                                name=name,
                                age_text=age_text,
                            )
                        else:
                            reminder_text = i18n.get_message(
                                "upcoming_birthday",
                                chat_id,
                                days=days_until,
                                name=name,
                                age_text=age_text,
                            )

                        bot.send_message(chat_id, reminder_text)
                        db.mark_birthday_reminder_sent(id, days_until)
                    except telebot.apihelper.ApiTelegramException as e:
                        if e.error_code == 429 or e.error_code >= 500:
                            logging.warning(
                                f"Temporary Telegram error {e.error_code} for "
                                f"user {chat_id}, will retry: {e}"
                            )
                        else:
                            logging.warning(
                                f"Permanent Telegram error {e.error_code} for "
                                f"user {chat_id}, marking reminder as sent: {e}"
                            )
                            db.mark_birthday_reminder_sent(id, days_until)
                    except Exception as e:
                        logging.error(
                            f"Error processing birthday {id} for "
                            f"chat {chat_id}: {e}"
                        )
                        utils.log_exception(e)

        except Exception as e:
            logging.error(f"Error during birthday ping processing: {e}")
            utils.log_exception(e)


def send_share_message(message):
    all_birthdays = get_all_birthdays_for_share(message.chat.id)
    birthdays_messages = utils.split_message(all_birthdays)

    for birthday_message in birthdays_messages:
        bot.send_message(message.chat.id, birthday_message, parse_mode="Markdown")


def register_birthday(message):
    chat_id = message.chat.id

    instruct_msg = bot.send_message(
        chat_id,
        i18n.get_message("register_birthday_instructions", chat_id),
        parse_mode="Markdown",
    )

    birthday_registration_messages[chat_id] = set([instruct_msg.message_id])
    user_states[chat_id] = TUserState.AwaitingBirthday


def process_backup_pings():
    while True:
        minutes = 5
        time.sleep(minutes * 60)
        try:
            due_pings = db.get_active_backup_pings_due()

            for chat_id, _ in due_pings:
                try:
                    all_birthdays = get_all_birthdays_formatted(chat_id)
                    bot.send_message(
                        chat_id,
                        f"{i18n.get_message('latest_backup', chat_id)}\n{all_birthdays}",
                        parse_mode="Markdown",
                    )
                    db.update_backup_ping(chat_id)
                except telebot.apihelper.ApiTelegramException as e:
                    if e.error_code == 429 or e.error_code >= 500:
                        logging.warning(
                            f"Temporary Telegram error {e.error_code} for "
                            f"user {chat_id} during backup ping, will retry: {e}"
                        )
                    else:
                        logging.warning(
                            f"Permanent Telegram error {e.error_code} for "
                            f"user {chat_id}, deactivating backup ping: {e}"
                        )
                        db.unregister_backup_ping(chat_id)
                except Exception as e:
                    logging.error(f"Error sending backup ping to user {chat_id}: {e}")

        except Exception as e:
            logging.error(f"Error during backup ping processing: {e}")


def register_backup(message):
    chat_id = message.chat.id

    msg = bot.send_message(
        chat_id,
        i18n.get_message("enter_backup_interval", chat_id),
        parse_mode="Markdown",
    )

    register_backup_messages[chat_id] = [msg.message_id]

    user_states[chat_id] = TUserState.AwaitingInterval


def unregister_backup(message):
    chat_id = message.chat.id
    db.unregister_backup_ping(chat_id)
    bot.send_message(
        chat_id,
        i18n.get_message("backup_unregistered", chat_id),
        parse_mode="Markdown",
    )

    user_states[chat_id] = TUserState.Default


def handle_deletion(message):
    chat_id = message.chat.id
    all_birthdays = get_all_birthdays_formatted(chat_id, need_id=True)
    birthdays_messages = utils.split_message(all_birthdays)

    instruct_msg = bot.send_message(
        chat_id,
        i18n.get_message("enter_delete_ids", chat_id),
        parse_mode="Markdown",
    )

    birthday_deletion_messages[chat_id] = [instruct_msg.message_id]

    for birthday_message in birthdays_messages:
        msg = bot.send_message(chat_id, birthday_message, parse_mode="Markdown")
        birthday_deletion_messages[chat_id].append(msg.message_id)

    user_states[chat_id] = TUserState.AwaitingDeletion


def handle_support(message):
    """Handle support command - show donation options"""
    chat_id = message.chat.id
    bot.send_message(
        chat_id,
        i18n.get_message("support_description", chat_id),
        reply_markup=get_support_keyboard(chat_id),
        parse_mode="Markdown",
    )
    user_states[chat_id] = TUserState.Default


@bot.callback_query_handler(func=lambda call: True)
def handle_callback_query(call):
    # Skip if already handled by specific handlers
    if (
        call.data.startswith("reminder_")
        or call.data.startswith("lang_")
        or call.data.startswith("support_pay_")
        or call.data.startswith("settings_")
    ):
        return

    message = call.message
    chat_id = message.chat.id

    # Map callback data to commands
    command_mapping = {
        "start": TCommand.Start,
        "backup": TCommand.Backup,
        "register_birthday": TCommand.RegisterBirthday,
        "register_backup": TCommand.RegisterBackup,
        "unregister_backup": TCommand.UnregisterBackup,
        "delete_birthday": TCommand.DeleteBirthday,
        "stats": TCommand.Stats,
        "share": TCommand.Share,
        "settings": TCommand.Settings,
        "language": TCommand.Language,
        "support": TCommand.Support,
    }

    command = command_mapping.get(call.data)
    if command is None:
        bot.answer_callback_query(call.id, i18n.get_message("invalid_action", chat_id))
        return

    if dispatch_command(command, message):
        # Stop the loading spinner on the button
        bot.answer_callback_query(call.id)
    else:
        bot.answer_callback_query(call.id, i18n.get_message("unknown_command", chat_id))


def dispatch_command(command: TCommand, message) -> bool:
    """Run the handler for a resolved command.

    Returns True if the command was handled, False if it is unknown.
    Shared by slash commands and localized keyboard button texts so the
    two paths can never drift apart.
    """
    chat_id = message.chat.id

    if command == TCommand.Start:
        handle_start(message)
    elif command == TCommand.Backup:
        send_backup(message)
    elif command == TCommand.RegisterBirthday:
        register_birthday(message)
    elif command == TCommand.RegisterBackup:
        register_backup(message)
    elif command == TCommand.UnregisterBackup:
        unregister_backup(message)
    elif command == TCommand.DeleteBirthday:
        handle_deletion(message)
    elif command == TCommand.Stats:
        handle_stats(message)
    elif command == TCommand.Share:
        send_share_message(message)
    elif command == TCommand.Settings:
        bot.send_message(
            chat_id,
            i18n.get_message("settings_title", chat_id),
            reply_markup=get_settings_keyboard(chat_id),
            parse_mode="Markdown",
        )
    elif command == TCommand.Language:
        bot.send_message(
            chat_id,
            i18n.get_message("settings_title", chat_id),
            reply_markup=get_language_keyboard(),
            parse_mode="Markdown",
        )
    elif command == TCommand.Support:
        handle_support(message)
    else:
        return False

    return True


@bot.message_handler(func=lambda message: True)
def handle_message(message):
    chat_id = message.chat.id
    user_message = message.text.strip()

    # Handle slash commands, tolerating the "@botusername" suffix that
    # Telegram adds in group chats.
    slash_command = extract_command(user_message)
    if slash_command is not None:
        if slash_command == "clear":
            # Secret command to clear keyboard
            safe_delete_message(chat_id, message.message_id)
            # Remove keyboard and clean up that message
            remove_keyboard(message)
            return

        command = COMMAND_MAPPINGS.get(slash_command)
        if command is not None and dispatch_command(command, message):
            return

    # Handle button texts in user's language
    button_mapping = get_button_to_command_mapping(chat_id)
    if user_message in button_mapping:
        if dispatch_command(button_mapping[user_message], message):
            return

    match user_states.get(chat_id):
        case TUserState.AwaitingInterval:
            try:
                if not utils.is_timestamp_valid(user_message):
                    raise ValueError(f"Invalid format, user_message: {user_message}")

                interval_in_minutes = utils.get_time(user_message)

                db.register_backup_ping(chat_id, interval_in_minutes)

                bot.send_message(
                    chat_id,
                    i18n.get_message(
                        "backup_registered", chat_id, interval=interval_in_minutes
                    ),
                    parse_mode="Markdown",
                )

                user_states[chat_id] = TUserState.Default

                safe_delete_message(chat_id, message.message_id)

                for old_message_id in register_backup_messages[chat_id]:
                    safe_delete_message(chat_id, old_message_id)

                if chat_id in register_backup_messages.keys():
                    del register_backup_messages[chat_id]

            except Exception:
                error_msg = bot.send_message(
                    chat_id,
                    i18n.get_message("invalid_interval_format", chat_id),
                    parse_mode="Markdown",
                )

                register_backup_messages[chat_id].append(error_msg.message_id)

        case TUserState.AwaitingDeletion:
            try:
                birthday_ids = [
                    int(id_str.strip()) for id_str in user_message.split(",")
                ]
                deleted_ids = []
                not_found_ids = []

                for birthday_id in birthday_ids:
                    deleted_rows = db.delete_birthday(chat_id, birthday_id)
                    if deleted_rows > 0:
                        deleted_ids.append(birthday_id)
                    else:
                        not_found_ids.append(birthday_id)

                if deleted_ids:
                    bot.send_message(
                        chat_id,
                        i18n.get_message(
                            "birthdays_deleted",
                            chat_id,
                            ids=", ".join(map(str, deleted_ids)),
                        ),
                        parse_mode="Markdown",
                    )

                if not_found_ids:
                    bot.send_message(
                        chat_id,
                        i18n.get_message(
                            "birthdays_not_found",
                            chat_id,
                            ids=", ".join(map(str, not_found_ids)),
                        ),
                        parse_mode="Markdown",
                    )
                    logging.warning(
                        f"Could not find birthdays for Chat ID {chat_id}: {not_found_ids}"
                    )

                user_states[chat_id] = TUserState.Default

                birthday_deletion_messages[chat_id].append(message.message_id)

                for old_message_id in birthday_deletion_messages[chat_id]:
                    safe_delete_message(chat_id, old_message_id)

                if chat_id in birthday_deletion_messages.keys():
                    del birthday_deletion_messages[chat_id]

            except ValueError:
                error_msg = bot.send_message(
                    chat_id,
                    i18n.get_message("invalid_ids_format", chat_id),
                    parse_mode="Markdown",
                )
                birthday_deletion_messages[chat_id].append(error_msg.message_id)
            except Exception as e:
                logging.error(f"Error deleting birthdays for Chat ID {chat_id}: {e}")

        case TUserState.AwaitingBirthday:
            try:
                success, error_message = utils.validate_birthday_input(
                    user_message, chat_id
                )
                if not success:
                    err_msg = bot.send_message(
                        chat_id,
                        error_message,
                        parse_mode="Markdown",
                    )
                    birthday_registration_messages[chat_id].add(err_msg.message_id)
                    return

                success, parsed_birthdays = utils.parse_dates(user_message)
                for name, parsed_date, has_year in parsed_birthdays:
                    db.register_birthday(chat_id, name, parsed_date, has_year)

                birthday_msg = ""
                for name, parsed_date, has_year in parsed_birthdays:
                    if has_year:
                        birthday_msg += (
                            f"- {name}: {parsed_date.strftime('%d %B %Y')}\n"
                        )
                    else:
                        birthday_msg += f"- {name}: {parsed_date.strftime('%d %B')}\n"

                bot.send_message(
                    chat_id,
                    f"{i18n.get_message('birthdays_registered', chat_id)}\n{birthday_msg}",
                    parse_mode="Markdown",
                )

                user_states[chat_id] = TUserState.Default

                safe_delete_message(chat_id, message.message_id)

                for old_message_id in birthday_registration_messages[chat_id]:
                    safe_delete_message(chat_id, old_message_id)

                if chat_id in birthday_registration_messages.keys():
                    del birthday_registration_messages[chat_id]

            except Exception:
                bot.send_message(
                    chat_id,
                    i18n.get_message("invalid_name_format", chat_id),
                    parse_mode="Markdown",
                )
        case _:
            pass


def log_cleaner():
    """Thread function to periodically clean up old log files."""
    while True:
        try:
            utils.cleanup_old_logs()
            time.sleep(24 * 60 * 60)
        except Exception as e:
            logging.error(f"Error in log cleaner thread: {e}")
            utils.log_exception(e)
            time.sleep(60 * 60)


def clear_previous_bot_state():
    """Clear any lingering webhook/getUpdates state from a previous bot instance.

    Telegram only allows a single consumer of getUpdates (polling) or a
    webhook at a time. When a new instance deploys while an old one is still
    shutting down, Telegram can respond to the new instance's polling
    request with a 409 Conflict. Explicitly deleting the webhook (which also
    drops any pending updates queue tied to the previous connection) before
    starting to poll helps make sure the old instance's connection is
    released first.
    """
    try:
        logging.info("Clearing any previous webhook/polling state...")
        bot.remove_webhook()
        logging.info("Webhook cleared successfully")
    except ApiTelegramException as e:
        if getattr(e, "error_code", None) == 409:
            logging.warning(
                f"409 Conflict while clearing webhook (previous instance may "
                f"still be shutting down): {e}"
            )
        else:
            logging.error(f"Failed to clear webhook: {e}")
            utils.log_exception(e)
    except Exception as e:
        logging.error(f"Unexpected error while clearing webhook: {e}")
        utils.log_exception(e)


def delayed_setup_bot_commands(delay_seconds: float = 3.0):
    """Register the command menu shortly after polling has started.

    Running this immediately after `bot.polling()` is invoked (rather than
    before) avoids racing with Telegram's cleanup of the previous instance's
    connection, which is a common source of 409 Conflict errors.
    """
    time.sleep(delay_seconds)
    setup_bot_commands()


def handle_shutdown_signal(signum, frame):
    """Gracefully stop polling and exit on SIGTERM/SIGINT.

    This helps prevent "ghost" getUpdates connections from lingering on
    Telegram's side, which can otherwise cause 409 Conflict errors the next
    time the bot starts up.
    """
    logging.info(f"Received signal {signum}, stopping bot gracefully...")
    try:
        bot.stop_polling()
    except Exception as e:
        logging.warning(f"Error while stopping polling: {e}")
    sys.exit(0)


if __name__ == "__main__":
    db.init_db()

    logging.info("Bot initialization starting...")

    signal.signal(signal.SIGTERM, handle_shutdown_signal)
    signal.signal(signal.SIGINT, handle_shutdown_signal)

    try:
        # Ensure no stale webhook/polling connection from a previous instance
        # is still holding Telegram's getUpdates lock before we start.
        clear_previous_bot_state()

        # Give Telegram a brief moment to fully release the previous
        # instance's connection before we start polling.
        logging.info("Waiting briefly for previous instance cleanup...")
        time.sleep(2)

        logging.info("Starting backup ping thread...")
        backup_thread = threading.Thread(target=process_backup_pings, daemon=True)
        backup_thread.start()

        logging.info("Starting birthday ping thread...")
        birthday_thread = threading.Thread(target=process_birthday_pings, daemon=True)
        birthday_thread.start()

        logging.info("Starting log cleaner thread...")
        log_cleaner_thread = threading.Thread(target=log_cleaner, daemon=True)
        log_cleaner_thread.start()

        logging.info("Bot is running...")

        while True:
            try:
                # Register the command menu only after polling is underway,
                # to avoid racing Telegram's teardown of the old connection.
                commands_thread = threading.Thread(
                    target=delayed_setup_bot_commands, daemon=True
                )
                commands_thread.start()

                bot.polling(none_stop=True, timeout=60, long_polling_timeout=60)
                break
            except ApiTelegramException as e:
                if getattr(e, "error_code", None) == 409:
                    logging.warning(
                        f"409 Conflict during polling (another instance is still "
                        f"active): {e}. Retrying in 10 seconds..."
                    )
                else:
                    logging.warning(
                        f"API error during polling: {e}. Reconnecting in 10 seconds..."
                    )
                time.sleep(10)
            except (ReadTimeout, ConnectionError) as e:
                logging.warning(
                    f"Network error during polling: {e}. Reconnecting in 10 seconds..."
                )
                time.sleep(10)
            except Exception as e:
                logging.error(
                    f"Unexpected error during polling: {e}. Reconnecting in 10 seconds..."
                )
                utils.log_exception(e)
                time.sleep(10)

    except KeyboardInterrupt:
        logging.info("Shutting down bot gracefully...")

        try:
            bot.stop_polling()
        except Exception as e:
            logging.warning(f"Error while stopping polling: {e}")

        backup_thread.join(timeout=2)
        birthday_thread.join(timeout=2)
        log_cleaner_thread.join(timeout=2)

    except Exception as e:
        logging.critical(f"Bot polling encountered an error: {e}")
        utils.log_exception(e)
