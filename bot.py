import os
import sys
import logging
import json
import re
import time
from datetime import datetime
from typing import Optional

# patch eventlet before anything else
try:
    import eventlet
    eventlet.monkey_patch()
except Exception:
    eventlet = None

from dotenv import load_dotenv
from telebot import TeleBot, types
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from utils import (
    config, console, get_bot,
    get_shipment_list, get_shipment_details,
    save_shipment, update_shipment, invalidate_cache,
    sanitize_tracking_number, validate_email,
    is_admin, generate_unique_id, estimate_distance,
    DHL_CONFIG, spawn_simulation, can_start_simulation
)

load_dotenv()

# Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('telegram_bot')

# ========== BOT INSTANCE ==========
bot = get_bot()

# ========== HELPERS ==========
def safe_edit_text(chat_id, message_id, text, reply_markup=None, parse_mode='Markdown'):
    try:
        bot.edit_message_text(text, chat_id, message_id, reply_markup=reply_markup, parse_mode=parse_mode)
    except Exception as e:
        logger.warning(f"Edit failed: {e}")

def send_message(chat_id, text, reply_markup=None, parse_mode='Markdown'):
    try:
        bot.send_message(chat_id, text, reply_markup=reply_markup, parse_mode=parse_mode)
    except Exception as e:
        logger.error(f"Send failed: {e}")

def _build_shipment_summary(tn: str) -> str:
    shipment = get_shipment_details(tn)
    if not shipment:
        return "Shipment not found."
    checkpoints = shipment.get('checkpoints', [])
    last_cp = checkpoints[-1] if checkpoints else "No updates"
    return (f"📦 *Tracking:* `{tn}`\n"
            f"📌 *Status:* {shipment.get('status', 'Unknown')}\n"
            f"📍 *From:* {shipment.get('origin_location', 'N/A')}\n"
            f"📍 *To:* {shipment.get('delivery_location', 'N/A')}\n"
            f"🔄 *Last Checkpoint:* {last_cp}\n"
            f"📅 *Updated:* {shipment.get('last_updated', 'N/A')}")

def _build_main_menu():
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("📦 List Shipments", callback_data="menu_page_1"),
        InlineKeyboardButton("➕ Add Shipment", callback_data="add"),
        InlineKeyboardButton("🔍 Search", callback_data="search_menu"),
        InlineKeyboardButton("📊 Stats", callback_data="stats"),
        InlineKeyboardButton("🆕 Generate ID", callback_data="generate_id"),
        InlineKeyboardButton("❓ Help", callback_data="help")
    )
    return markup

def _paginated_menu(page=1, prefix='menu', extra_buttons=None):
    shipments, total = get_shipment_list(page=page)
    markup = InlineKeyboardMarkup(row_width=2)
    for tn in shipments:
        details = get_shipment_details(tn)
        label = f"{tn} [{details.get('status', '?')}]"
        markup.add(InlineKeyboardButton(label, callback_data=f"view_{tn}"))
    nav_buttons = []
    if page > 1:
        nav_buttons.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"{prefix}_page_{page-1}"))
    if page * 10 < total:
        nav_buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"{prefix}_page_{page+1}"))
    if nav_buttons:
        markup.add(*nav_buttons)
    if extra_buttons:
        markup.add(*extra_buttons)
    markup.add(InlineKeyboardButton("🏠 Menu", callback_data="menu"))
    return markup, total

# ========== HANDLERS ==========
@bot.message_handler(commands=['start'])
def start_command(message):
    if not is_admin(message.from_user.id):
        send_message(message.chat.id, "⛔ You are not authorized to use this bot.")
        return
    send_message(message.chat.id,
                 "👋 *DHL Admin Bot*\nSelect an action:",
                 reply_markup=_build_main_menu())

@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "⛔ Unauthorized", show_alert=True)
        return

    data = call.data
    chat_id = call.message.chat.id
    msg_id = call.message.message_id

    # ----- MENU -----
    if data == "menu":
        safe_edit_text(chat_id, msg_id, "👋 *DHL Admin Bot*\nSelect an action:",
                       reply_markup=_build_main_menu())
        bot.answer_callback_query(call.id)

    # ----- LIST SHIPMENTS (pagination) -----
    elif data.startswith("menu_page_"):
        page = int(data.split("_")[-1])
        markup, total = _paginated_menu(page, prefix='menu')
        safe_edit_text(chat_id, msg_id,
                       f"📦 *Shipments* (Page {page}/{ (total-1)//10 + 1 if total else 1})\nTotal: {total}",
                       reply_markup=markup)
        bot.answer_callback_query(call.id)

    # ----- VIEW SHIPMENT -----
    elif data.startswith("view_"):
        tn = data.split("_")[1]
        details = get_shipment_details(tn)
        if not details:
            safe_edit_text(chat_id, msg_id, "❌ Shipment not found.")
            bot.answer_callback_query(call.id)
            return
        text = _build_shipment_summary(tn)
        markup = InlineKeyboardMarkup()
        markup.add(
            InlineKeyboardButton("🔙 Back", callback_data=f"menu_page_1"),
            InlineKeyboardButton("🔄 Refresh", callback_data=f"view_{tn}")
        )
        safe_edit_text(chat_id, msg_id, text, reply_markup=markup)
        bot.answer_callback_query(call.id)

    # ----- ADD SHIPMENT (step 1) -----
    elif data == "add":
        msg = bot.send_message(chat_id, "Please enter the *origin* (e.g., Lagos, NG):", parse_mode='Markdown')
        bot.register_next_step_handler(msg, add_origin_step)
        bot.answer_callback_query(call.id)

    # ----- SEARCH -----
    elif data == "search_menu":
        msg = bot.send_message(chat_id, "Enter tracking number or location to search:", parse_mode='Markdown')
        bot.register_next_step_handler(msg, search_step)
        bot.answer_callback_query(call.id)

    # ----- STATS -----
    elif data == "stats":
        shipments, total = get_shipment_list(page=1, per_page=9999)  # get all
        statuses = {}
        for tn in shipments:
            s = get_shipment_details(tn)
            if s:
                st = s.get('status', 'Unknown')
                statuses[st] = statuses.get(st, 0) + 1
        stats_text = f"📊 *Statistics*\nTotal shipments: `{total}`\n"
        for st, count in statuses.items():
            stats_text += f"  • {st}: {count}\n"
        safe_edit_text(chat_id, msg_id, stats_text, reply_markup=InlineKeyboardMarkup().add(
            InlineKeyboardButton("🔙 Menu", callback_data="menu")
        ))
        bot.answer_callback_query(call.id)

    # ----- GENERATE ID -----
    elif data == "generate_id":
        new_id = generate_unique_id()
        safe_edit_text(chat_id, msg_id,
                       f"🆕 New tracking number: `{new_id}`\nUse 'Add Shipment' to create a shipment with this ID.",
                       reply_markup=InlineKeyboardMarkup().add(
                           InlineKeyboardButton("➕ Add with this ID", callback_data="add")
                       ))
        bot.answer_callback_query(call.id)

    # ----- HELP -----
    elif data == "help":
        help_text = (
            "🤖 *Help*\n"
            "• List Shipments – view all shipments (paginated)\n"
            "• Add Shipment – create a new shipment (origin, destination, email)\n"
            "• Search – find by tracking number or location\n"
            "• Stats – view shipment status breakdown\n"
            "• Generate ID – create a random DHL tracking number\n"
            "• View Shipment – tap any shipment to see details"
        )
        safe_edit_text(chat_id, msg_id, help_text, reply_markup=InlineKeyboardMarkup().add(
            InlineKeyboardButton("🔙 Menu", callback_data="menu")
        ))
        bot.answer_callback_query(call.id)

    # ----- PAGINATION for other menus (if needed) -----
    elif data.startswith("menu_page_"):
        page = int(data.split("_")[-1])
        markup, total = _paginated_menu(page, prefix='menu')
        safe_edit_text(chat_id, msg_id,
                       f"📦 *Shipments* (Page {page}/{ (total-1)//10 + 1 if total else 1})\nTotal: {total}",
                       reply_markup=markup)
        bot.answer_callback_query(call.id)

    else:
        bot.answer_callback_query(call.id, "Unknown action", show_alert=False)


# ========== STEP HANDLERS ==========
def add_origin_step(message):
    chat_id = message.chat.id
    origin = message.text.strip()
    if not origin:
        bot.send_message(chat_id, "Origin cannot be empty. Please enter origin:")
        bot.register_next_step_handler(message, add_origin_step)
        return
    msg = bot.send_message(chat_id, "Now enter the *destination* (e.g., London, UK):", parse_mode='Markdown')
    bot.register_next_step_handler(msg, add_destination_step, origin)

def add_destination_step(message, origin):
    chat_id = message.chat.id
    destination = message.text.strip()
    if not destination:
        bot.send_message(chat_id, "Destination cannot be empty. Please enter destination:")
        bot.register_next_step_handler(message, add_destination_step, origin)
        return
    msg = bot.send_message(chat_id, "Enter recipient email (optional, press /skip to skip):", parse_mode='Markdown')
    bot.register_next_step_handler(msg, add_email_step, origin, destination)

def add_email_step(message, origin, destination):
    chat_id = message.chat.id
    email = message.text.strip()
    if email.lower() == '/skip':
        email = None
    elif email and not validate_email(email):
        bot.send_message(chat_id, "Invalid email format. Please enter a valid email or /skip:")
        bot.register_next_step_handler(message, add_email_step, origin, destination)
        return
    # Create shipment
    try:
        tn = generate_unique_id()
        success = save_shipment(
            tracking_number=tn,
            status='Pending',
            checkpoints=f"{datetime.now().strftime('%Y-%m-%d %H:%M')} - {origin} - Shipment information received",
            delivery_location=destination,
            recipient_email=email,
            origin_location=origin,
            carrier='DHL',
            sender_location=origin,
            receiver_address=destination
        )
        if success:
            try:
                if can_start_simulation():
                    spawn_simulation(tn)
                else:
                    bot.send_message(chat_id, "⚠️ Simulator throttle active – simulation may start later.")
            except Exception as e:
                logger.error(f"Simulation start error: {e}")
            bot.send_message(chat_id,
                             f"✅ Shipment created!\nTracking: `{tn}`\nOrigin: {origin}\nDestination: {destination}\nEmail: {email or 'none'}",
                             parse_mode='Markdown',
                             reply_markup=InlineKeyboardMarkup().add(
                                 InlineKeyboardButton("📦 View Shipment", callback_data=f"view_{tn}"),
                                 InlineKeyboardButton("🏠 Menu", callback_data="menu")
                             ))
        else:
            bot.send_message(chat_id, "❌ Failed to create shipment. Please try again.")
    except Exception as e:
        logger.error(f"Add shipment error: {e}")
        bot.send_message(chat_id, f"❌ Error: {e}")

def search_step(message):
    chat_id = message.chat.id
    query = message.text.strip()
    if not query:
        bot.send_message(chat_id, "Please enter a search term.")
        return
    from utils import search_shipments
    results, total = search_shipments(query, page=1, per_page=20)
    if not results:
        bot.send_message(chat_id, "No shipments found.", reply_markup=InlineKeyboardMarkup().add(
            InlineKeyboardButton("🔙 Menu", callback_data="menu")
        ))
        return
    text = f"🔍 *Search results for '{query}'* ({total} found)\n"
    for tn in results[:20]:
        details = get_shipment_details(tn)
        status = details.get('status', '?')
        text += f"• `{tn}` – {status}\n"
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("🔙 Menu", callback_data="menu"))
    bot.send_message(chat_id, text, parse_mode='Markdown', reply_markup=markup)


# ========== POLLING WITH WEBHOOK CLEANUP ==========
def main():
    logger.info("Starting Telegram bot in polling mode...")
    console.print("[info]Telegram bot started (polling)[/info]")

    # Remove any existing webhook to avoid 409 conflict
    try:
        bot.remove_webhook()
        logger.info("Removed existing webhook (if any)")
        console.print("[info]Removed existing webhook[/info]")
        time.sleep(1)  # Allow Telegram to process the removal
    except Exception as e:
        logger.warning(f"Failed to remove webhook: {e}")

    # Poll with automatic retry on 409 conflict
    max_retries = 3
    for attempt in range(max_retries):
        try:
            bot.infinity_polling()
            break  # success
        except Exception as e:
            if "409" in str(e) and attempt < max_retries - 1:
                logger.warning(f"Conflict (409) on attempt {attempt+1}, retrying after webhook removal...")
                try:
                    bot.remove_webhook()
                    time.sleep(2)
                except Exception:
                    pass
                continue
            else:
                logger.error(f"Bot polling failed: {e}")
                raise

if __name__ == "__main__":
    main()
