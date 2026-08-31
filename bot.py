# bot.py
import os
import sys
import logging
import re
import time
import csv
import json
from io import StringIO
from datetime import datetime
from typing import Optional

# patch eventlet before anything else (safe – if eventlet not available, ignore)
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
    is_admin, generate_unique_id,
    DHL_CONFIG,
    # Batch functions
    get_shipment_list_with_statuses,
    get_status_counts,
    # Flask app for context (only used if we run polling)
    app as flask_app,
    search_shipments,
    # Redis helpers for export
    redis_client
)

load_dotenv()

# Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('telegram_bot')


# ========== HELPER FUNCTIONS (shared) ==========
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
        InlineKeyboardButton("📥 Export All", callback_data="export"),
        InlineKeyboardButton("❓ Help", callback_data="help")
    )
    return markup

def _paginated_menu(page=1, prefix='menu', extra_buttons=None):
    items, total = get_shipment_list_with_statuses(page=page)
    markup = InlineKeyboardMarkup(row_width=2)
    for tn, status in items:
        label = f"{tn} [{status or '?'}]"
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


# ========== EXPORT FUNCTION ==========
def _export_shipments_csv() -> Optional[str]:
    """Generate CSV string of all shipments."""
    try:
        from utils import Shipment, db
        shipments = Shipment.query.all()
        if not shipments:
            return None
        output = StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "Tracking", "Status", "Origin", "Destination", "Recipient Email",
            "Carrier", "Created At", "Last Updated", "Payment Status", "Invoice Amount"
        ])
        for s in shipments:
            writer.writerow([
                s.tracking_number,
                s.status,
                s.origin_location or "",
                s.delivery_location,
                s.recipient_email or "",
                s.carrier or "DHL",
                s.created_at.isoformat() if s.created_at else "",
                s.last_updated.isoformat() if s.last_updated else "",
                s.payment_status or "unpaid",
                s.invoice_amount or ""
            ])
        return output.getvalue()
    except Exception as e:
        logger.error(f"Export error: {e}")
        return None


# ========== HANDLER REGISTRATION ==========
def register_handlers(bot: TeleBot):
    """
    Register all message and callback handlers with the given bot instance.
    Call this once from app.py after the bot is created.
    """

    # ----- Message handlers -----
    @bot.message_handler(commands=['start'])
    def start_command(message):
        if not is_admin(message.from_user.id):
            bot.send_message(message.chat.id, "⛔ You are not authorized to use this bot.")
            return
        bot.send_message(message.chat.id,
                         "👋 *DHL Admin Bot*\nSelect an action:",
                         reply_markup=_build_main_menu())

    @bot.message_handler(commands=['export'])
    def export_command(message):
        if not is_admin(message.from_user.id):
            bot.send_message(message.chat.id, "⛔ Unauthorized.")
            return
        csv_data = _export_shipments_csv()
        if csv_data is None:
            bot.send_message(message.chat.id, "❌ No shipments to export or error occurred.")
            return
        try:
            bot.send_document(message.chat.id, document=('shipments.csv', csv_data),
                              caption="📥 All shipments exported.")
        except Exception as e:
            logger.error(f"Export send failed: {e}")
            bot.send_message(message.chat.id, f"❌ Failed to send export: {e}")

    # ----- Callback query handler -----
    @bot.callback_query_handler(func=lambda call: True)
    def callback_handler(call):
        if not is_admin(call.from_user.id):
            bot.answer_callback_query(call.id, "⛔ Unauthorized", show_alert=True)
            return

        data = call.data
        chat_id = call.message.chat.id
        msg_id = call.message.message_id

        # Helper to edit message safely
        def safe_edit(text, reply_markup=None, parse_mode='Markdown'):
            try:
                bot.edit_message_text(text, chat_id, msg_id, reply_markup=reply_markup, parse_mode=parse_mode)
            except Exception as e:
                logger.warning(f"Edit failed: {e}")

        if data == "menu":
            safe_edit("👋 *DHL Admin Bot*\nSelect an action:", reply_markup=_build_main_menu())
            bot.answer_callback_query(call.id)

        elif data.startswith("menu_page_"):
            page = int(data.split("_")[-1])
            markup, total = _paginated_menu(page, prefix='menu')
            safe_edit(f"📦 *Shipments* (Page {page}/{ (total-1)//10 + 1 if total else 1})\nTotal: {total}",
                      reply_markup=markup)
            bot.answer_callback_query(call.id)

        elif data.startswith("view_"):
            tn = data.split("_")[1]
            details = get_shipment_details(tn)
            if not details:
                safe_edit("❌ Shipment not found.")
                bot.answer_callback_query(call.id)
                return
            text = _build_shipment_summary(tn)
            markup = InlineKeyboardMarkup()
            markup.add(
                InlineKeyboardButton("🔙 Back", callback_data=f"menu_page_1"),
                InlineKeyboardButton("🔄 Refresh", callback_data=f"view_{tn}")
            )
            safe_edit(text, reply_markup=markup)
            bot.answer_callback_query(call.id)

        elif data == "add":
            msg = bot.send_message(chat_id, "Please enter the *origin* (e.g., Lagos, NG):", parse_mode='Markdown')
            bot.register_next_step_handler(msg, add_origin_step)
            bot.answer_callback_query(call.id)

        elif data == "search_menu":
            msg = bot.send_message(chat_id, "Enter tracking number or location to search:", parse_mode='Markdown')
            bot.register_next_step_handler(msg, search_step)
            bot.answer_callback_query(call.id)

        elif data == "stats":
            counts = get_status_counts()
            total = sum(counts.values())
            stats_text = f"📊 *Statistics*\nTotal shipments: `{total}`\n"
            for st, count in counts.items():
                stats_text += f"  • {st}: {count}\n"
            safe_edit(stats_text, reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("🔙 Menu", callback_data="menu")
            ))
            bot.answer_callback_query(call.id)

        elif data == "generate_id":
            new_id = generate_unique_id()
            safe_edit(f"🆕 New tracking number: `{new_id}`\nUse 'Add Shipment' to create a shipment with this ID.",
                      reply_markup=InlineKeyboardMarkup().add(
                          InlineKeyboardButton("➕ Add with this ID", callback_data="add")
                      ))
            bot.answer_callback_query(call.id)

        elif data == "export":
            # Generate and send CSV
            csv_data = _export_shipments_csv()
            if csv_data is None:
                safe_edit("❌ No shipments to export or error occurred.")
                bot.answer_callback_query(call.id)
                return
            # We cannot send document inline, so we send a new message
            try:
                bot.send_document(chat_id, document=('shipments.csv', csv_data),
                                  caption="📥 All shipments exported.")
                safe_edit("📥 Export sent as a file above.", reply_markup=InlineKeyboardMarkup().add(
                    InlineKeyboardButton("🔙 Menu", callback_data="menu")
                ))
            except Exception as e:
                logger.error(f"Export send failed: {e}")
                safe_edit(f"❌ Failed to send export: {e}")
            bot.answer_callback_query(call.id)

        elif data == "help":
            help_text = (
                "🤖 *Help*\n"
                "• List Shipments – view all shipments (paginated)\n"
                "• Add Shipment – create a new shipment (origin, destination, email)\n"
                "• Search – find by tracking number or location\n"
                "• Stats – view shipment status breakdown\n"
                "• Generate ID – create a random DHL tracking number\n"
                "• Export All – download all shipments as CSV\n"
                "• View Shipment – tap any shipment to see details"
            )
            safe_edit(help_text, reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("🔙 Menu", callback_data="menu")
            ))
            bot.answer_callback_query(call.id)

        else:
            bot.answer_callback_query(call.id, "Unknown action", show_alert=False)


    # ========== STEP HANDLERS (nested to capture `bot`) ==========

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
                bot.send_message(chat_id,
                                 f"✅ Shipment created!\nTracking: `{tn}`\nOrigin: {origin}\nDestination: {destination}\nEmail: {email or 'none'}\n\nSimulation will start when the tracking page is opened.",
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


# ========== POLLING ENTRY (for local testing) ==========
def _run_polling():
    """Start polling – only for local development."""
    logger.info("Starting Telegram bot in polling mode (local test)...")
    console.print("[info]Telegram bot started (polling)[/info]")

    # Get a fresh bot instance
    bot = get_bot()
    # Register handlers on this bot
    register_handlers(bot)

    # Force delete webhook for polling
    for _ in range(3):
        try:
            bot.delete_webhook(drop_pending_updates=True)
            logger.info("Deleted webhook (with drop_pending_updates)")
            time.sleep(2)
            info = bot.get_webhook_info()
            if not info.url:
                break
        except Exception as e:
            logger.warning(f"Webhook cleanup failed: {e}")
            time.sleep(2)

    # Start polling
    max_retries = 10
    for attempt in range(1, max_retries + 1):
        try:
            logger.info(f"Polling attempt {attempt}/{max_retries}")
            bot.infinity_polling(skip_pending=True, timeout=10)
            break
        except Exception as e:
            if "409" in str(e) and attempt < max_retries:
                wait_time = min(2 ** attempt, 60)
                logger.warning(f"Conflict (409) on attempt {attempt}, retrying after {wait_time}s...")
                try:
                    bot.delete_webhook(drop_pending_updates=True)
                except Exception:
                    pass
                time.sleep(wait_time)
                continue
            else:
                logger.error(f"Bot polling failed: {e}")
                time.sleep(60)
                raise


if __name__ == "__main__":
    # When run directly, start polling (for local testing).
    # In production (webhook), this script is not run – only import register_handlers.
    with flask_app.app_context():
        _run_polling()
