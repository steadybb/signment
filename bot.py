import os
import re
import json
import logging
import socket
import time
import threading
import requests
from datetime import datetime
from telebot import TeleBot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from functools import wraps
from rich.console import Console
from urllib.parse import quote_plus, urlparse
from sqlalchemy import text

# Import app first, then get bot from utils
from app import app, db, Shipment
from utils import (
    BotConfig, config, get_bot, is_admin, send_dynamic_menu, get_shipment_details,
    generate_unique_id, search_shipments, RATE_LIMIT_WINDOW, RATE_LIMIT_MAX,
    safe_redis_operation, sanitize_tracking_number, enqueue_notification,
    save_shipment, update_shipment, get_shipment_list, export_shipments, get_recent_logs,
    show_shipment_menu, cache_route_templates, keep_alive, estimate_distance, DHL_CONFIG,
    redis_client
)

# Logging setup
bot_logger = logging.getLogger('telegram_bot')
console = Console()

# Initialize bot - MUST be done before decorators
bot = get_bot()
if bot is None:
    bot_logger.warning("Telegram bot token not configured; bot disabled. Set TELEGRAM_BOT_TOKEN to enable.")
    bot = None

# === RATE LIMIT DECORATOR ===
def rate_limit(func):
    @wraps(func)
    def wrapper(message):
        if bot is None:
            return
        user_id = str(message.from_user.id)
        key = f"rate_limit:{user_id}"
        count = safe_redis_operation(redis_client.incr, key) if redis_client else 0
        if count == 1:
            safe_redis_operation(redis_client.expire, key, RATE_LIMIT_WINDOW)
        if count > RATE_LIMIT_MAX:
            bot.reply_to(message, "Rate limit exceeded. Please try again later.")
            return
        return func(message)
    return wrapper

# === DATABASE HELPERS WITH CONTEXT ===
def get_shipment_with_context(tracking_number):
    with app.app_context():
        shipment = Shipment.query.filter_by(tracking_number=tracking_number).first()
        return shipment.to_dict() if shipment else None

def get_all_shipments_with_context(page=1, per_page=10):
    with app.app_context():
        shipments_query = Shipment.query.order_by(
            Shipment.created_at.desc()
        ).offset((page - 1) * per_page).limit(per_page).all()
        total = Shipment.query.count()
        return [s.to_dict() for s in shipments_query], total

def update_shipment_with_context(tracking_number, **kwargs):
    with app.app_context():
        shipment = Shipment.query.filter_by(tracking_number=tracking_number).first()
        if not shipment:
            return False
        for key, value in kwargs.items():
            if hasattr(shipment, key):
                setattr(shipment, key, value)
        db.session.commit()
        return True

def save_shipment_with_context(tracking_number, status, checkpoints, delivery_location,
                               recipient_email=None, origin_location=None,
                               webhook_url=None, carrier="DHL"):
    with app.app_context():
        try:
            shipment = Shipment(
                tracking_number=tracking_number,
                status=status,
                checkpoints=checkpoints,
                delivery_location=delivery_location,
                last_updated=datetime.now(),
                recipient_email=recipient_email or '',
                created_at=datetime.now(),
                origin_location=origin_location or 'Lagos, NG',
                webhook_url=webhook_url,
                email_notifications=bool(recipient_email),
                carrier=carrier or 'DHL'
            )
            db.session.add(shipment)
            db.session.commit()
            return True
        except Exception as e:
            db.session.rollback()
            bot_logger.error(f"Error saving shipment: {e}")
            return False

def search_shipments_with_context(query, page=1):
    with app.app_context():
        search = f"%{query}%"
        results = Shipment.query.filter(
            db.or_(
                Shipment.tracking_number.like(search),
                Shipment.delivery_location.like(search),
                Shipment.origin_location.like(search)
            )
        ).order_by(Shipment.created_at.desc()).offset((page - 1) * 10).limit(10).all()
        total = Shipment.query.filter(
            db.or_(
                Shipment.tracking_number.like(search),
                Shipment.delivery_location.like(search),
                Shipment.origin_location.like(search)
            )
        ).count()
        return [s.tracking_number for s in results], total

def export_shipments_with_context():
    with app.app_context():
        shipments = Shipment.query.all()
        return json.dumps([s.to_dict() for s in shipments])

def get_recent_logs_with_context(limit=10):
    try:
        log_file = 'flask_app.log'
        if os.path.exists(log_file):
            with open(log_file, 'r') as f:
                lines = f.readlines()[-limit:]
                return [line.strip() for line in lines]
        return []
    except Exception as e:
        bot_logger.error(f"Error reading logs: {e}")
        return []

# === COMMAND HANDLERS ===
if bot is not None:
    @bot.message_handler(commands=['myid'])
    @rate_limit
    def get_my_id(message):
        bot.reply_to(message, f"Your Telegram user ID: `{message.from_user.id}`", parse_mode='Markdown')
        bot_logger.info(f"User requested their ID")

    @bot.message_handler(commands=['start', 'menu'])
    @rate_limit
    def send_menu(message):
        if not is_admin(message.from_user.id):
            bot.reply_to(message, "Access denied.")
            bot_logger.warning(f"Access denied for user {message.from_user.id}")
            return
        send_dynamic_menu(message.chat.id, page=1)
        bot_logger.info(f"Menu sent to admin {message.from_user.id}")

    @bot.message_handler(commands=['track'])
    @rate_limit
    def track_shipment(message):
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            bot.reply_to(message, "Usage: /track <tracking_number>\nExample: /track JD1234567890")
            return
        tracking_number = sanitize_tracking_number(parts[1].strip())
        if not tracking_number:
            bot.reply_to(message, "Invalid tracking number. Must be DHL format: JDxxxxxxxxxx")
            return
        try:
            shipment = get_shipment_with_context(tracking_number)
            if not shipment:
                bot.reply_to(message, f"Shipment `{tracking_number}` not found.", parse_mode='Markdown')
                return
            paused = safe_redis_operation(redis_client.hget, "paused_simulations", tracking_number) == "true" if redis_client else False
            speed = float(safe_redis_operation(redis_client.hget, "sim_speed_multipliers", tracking_number) or 1.0) if redis_client else 1.0
            distance = estimate_distance(shipment.get('origin_location') or "Lagos, NG", shipment.get('delivery_location', ''))
            service_level = safe_redis_operation(redis_client.hget, "service_level", tracking_number) if redis_client else None
            if service_level and isinstance(service_level, bytes):
                service_level = service_level.decode('utf-8')
            delivery_window = safe_redis_operation(redis_client.hget, "delivery_window", tracking_number) if redis_client else None
            if delivery_window and isinstance(delivery_window, bytes):
                delivery_window = delivery_window.decode('utf-8')
            response = (
                f"*Shipment*: `{tracking_number}`\n"
                f"*Carrier*: `{shipment.get('carrier', 'DHL')}`\n"
                f"*Status*: `{shipment['status']}`\n"
                f"*Service*: `{service_level or 'DHL Express'}`\n"
                f"*Delivery Window*: `{delivery_window or 'Calculating...'}`\n"
                f"*Paused*: `{paused}`\n"
                f"*Speed*: `{speed}x`\n"
                f"*Distance*: `{distance} km`\n"
                f"*Origin*: `{shipment.get('origin_location', 'N/A')}`\n"
                f"*Destination*: `{shipment['delivery_location']}`\n"
                f"*Checkpoints*: `{len(shipment.get('checkpoints', []))}`"
            )
            markup = InlineKeyboardMarkup(row_width=2)
            if shipment['status'] not in ['Delivered', 'Returned']:
                markup.add(
                    InlineKeyboardButton("⏸ Pause" if not paused else "▶️ Resume",
                                       callback_data=f"{'pause' if not paused else 'resume'}_{tracking_number}_1"),
                    InlineKeyboardButton("⚡ Speed", callback_data=f"setspeed_{tracking_number}")
                )
            markup.add(
                InlineKeyboardButton("📧 Notify", callback_data=f"notify_{tracking_number}"),
                InlineKeyboardButton("🔗 Webhook", callback_data=f"set_webhook_{tracking_number}"),
                InlineKeyboardButton("🧪 Test Webhook", callback_data=f"test_webhook_{tracking_number}"),
                InlineKeyboardButton("🏠 Home", callback_data="menu_page_1")
            )
            bot.reply_to(message, response, parse_mode='Markdown', reply_markup=markup)
        except Exception as e:
            bot.reply_to(message, f"Error: {e}")
            bot_logger.error(f"Error in track command: {e}")

    @bot.message_handler(commands=['stats'])
    @rate_limit
    def system_stats(message):
        if not is_admin(message.from_user.id):
            bot.reply_to(message, "Access denied.")
            return
        try:
            with app.app_context():
                total = Shipment.query.count()
                active = Shipment.query.filter(
                    Shipment.status.in_(['Pending', 'In_Transit', 'Out_for_Delivery', 'Delayed', 'On_Hold'])
                ).count()
            paused_count = len(safe_redis_operation(redis_client.hgetall, "paused_simulations") or {}) if redis_client else 0
            response = (
                f"*📊 System Statistics*\n\n"
                f"*Total Shipments*: `{total}`\n"
                f"*Active Shipments*: `{active}`\n"
                f"*Paused Simulations*: `{paused_count}`\n"
                f"*Redis*: `{'✅ Connected' if redis_client else '❌ Disconnected'}`"
            )
            markup = InlineKeyboardMarkup(row_width=2)
            markup.add(
                InlineKeyboardButton("📋 All Shipments", callback_data="list_1"),
                InlineKeyboardButton("🏠 Home", callback_data="menu_page_1")
            )
            bot.reply_to(message, response, parse_mode='Markdown', reply_markup=markup)
        except Exception as e:
            bot.reply_to(message, f"Error: {e}")
            bot_logger.error(f"Error in stats command: {e}")

    @bot.message_handler(commands=['notify'])
    @rate_limit
    def manual_notification(message):
        if not is_admin(message.from_user.id):
            bot.reply_to(message, "Access denied.")
            return
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            bot.reply_to(message, "Usage: /notify <tracking_number>\nExample: /notify JD1234567890")
            return
        tracking_number = sanitize_tracking_number(parts[1].strip())
        if not tracking_number:
            bot.reply_to(message, "Invalid tracking number.")
            return
        try:
            shipment = get_shipment_with_context(tracking_number)
            if not shipment:
                bot.reply_to(message, f"Shipment `{tracking_number}` not found.", parse_mode='Markdown')
                return
            markup = InlineKeyboardMarkup(row_width=2)
            if shipment.get('recipient_email') and shipment.get('email_notifications'):
                markup.add(InlineKeyboardButton("📧 Send Email", callback_data=f"send_email_{tracking_number}"))
            if shipment.get('webhook_url'):
                markup.add(InlineKeyboardButton("🔗 Send Webhook", callback_data=f"send_webhook_{tracking_number}"))
            markup.add(InlineKeyboardButton("🏠 Home", callback_data="menu_page_1"))
            bot.reply_to(message, f"Select notification type for `{tracking_number}`:", parse_mode='Markdown', reply_markup=markup)
        except Exception as e:
            bot.reply_to(message, f"Error: {e}")
            bot_logger.error(f"Error in notify command: {e}")

    @bot.message_handler(commands=['search'])
    @rate_limit
    def search_command(message):
        if not is_admin(message.from_user.id):
            bot.reply_to(message, "Access denied.")
            return
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            bot.reply_to(message, "Usage: /search <query>\nExample: /search Lagos")
            return
        query = parts[1].strip()
        try:
            shipments, total = search_shipments_with_context(query, page=1)
            if not shipments:
                bot.reply_to(message, f"No shipments found for query: `{query}`", parse_mode='Markdown')
                return
            markup = InlineKeyboardMarkup(row_width=1)
            for tn in shipments:
                markup.add(InlineKeyboardButton(tn, callback_data=f"view_{tn}"))
            if total > 10:
                markup.add(InlineKeyboardButton("Next", callback_data=f"search_page_{query}_2"))
            markup.add(InlineKeyboardButton("🏠 Home", callback_data="menu_page_1"))
            bot.reply_to(message, f"*Search Results for '{query}'* (Page 1, {total} total):", parse_mode='Markdown', reply_markup=markup)
        except Exception as e:
            bot.reply_to(message, f"Error: {e}")
            bot_logger.error(f"Error in search command: {e}")

    @bot.message_handler(commands=['bulk_action'])
    @rate_limit
    def bulk_action_command(message):
        if not is_admin(message.from_user.id):
            bot.reply_to(message, "Access denied.")
            return
        markup = InlineKeyboardMarkup(row_width=2)
        markup.add(
            InlineKeyboardButton("⏸ Bulk Pause", callback_data="bulk_pause_menu_1"),
            InlineKeyboardButton("▶️ Bulk Resume", callback_data="bulk_resume_menu_1"),
            InlineKeyboardButton("🗑 Bulk Delete", callback_data="batch_delete_menu_1"),
            InlineKeyboardButton("🏠 Home", callback_data="menu_page_1")
        )
        bot.reply_to(message, "*Select bulk action*:", parse_mode='Markdown', reply_markup=markup)

    @bot.message_handler(commands=['stop'])
    @rate_limit
    def stop_simulation(message):
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            bot.reply_to(message, "Usage: /stop <tracking_number>\nExample: /stop JD1234567890")
            return
        tracking_number = sanitize_tracking_number(parts[1].strip())
        if not tracking_number:
            bot.reply_to(message, "Invalid tracking number.")
            return
        try:
            shipment = get_shipment_with_context(tracking_number)
            if not shipment:
                bot.reply_to(message, f"Shipment `{tracking_number}` not found.", parse_mode='Markdown')
                return
            if shipment['status'] in ['Delivered', 'Returned']:
                bot.reply_to(message, f"Shipment `{tracking_number}` is already completed.", parse_mode='Markdown')
                return
            if redis_client and safe_redis_operation(redis_client.hget, "paused_simulations", tracking_number) == "true":
                bot.reply_to(message, f"Simulation for `{tracking_number}` is already paused.", parse_mode='Markdown')
                return
            if redis_client:
                safe_redis_operation(redis_client.hset, "paused_simulations", tracking_number, "true")
            bot.reply_to(message, f"⏸ Simulation paused for `{tracking_number}`.", parse_mode='Markdown')
        except Exception as e:
            bot.reply_to(message, f"Error: {e}")
            bot_logger.error(f"Error in stop command: {e}")

    @bot.message_handler(commands=['continue'])
    @rate_limit
    def continue_simulation(message):
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            bot.reply_to(message, "Usage: /continue <tracking_number>\nExample: /continue JD1234567890")
            return
        tracking_number = sanitize_tracking_number(parts[1].strip())
        if not tracking_number:
            bot.reply_to(message, "Invalid tracking number.")
            return
        try:
            shipment = get_shipment_with_context(tracking_number)
            if not shipment:
                bot.reply_to(message, f"Shipment `{tracking_number}` not found.", parse_mode='Markdown')
                return
            if shipment['status'] in ['Delivered', 'Returned']:
                bot.reply_to(message, f"Shipment `{tracking_number}` is already completed.", parse_mode='Markdown')
                return
            if redis_client and safe_redis_operation(redis_client.hget, "paused_simulations", tracking_number) != "true":
                bot.reply_to(message, f"Simulation for `{tracking_number}` is not paused.", parse_mode='Markdown')
                return
            if redis_client:
                safe_redis_operation(redis_client.hdel, "paused_simulations", tracking_number)
            bot.reply_to(message, f"▶️ Simulation resumed for `{tracking_number}`.", parse_mode='Markdown')
        except Exception as e:
            bot.reply_to(message, f"Error: {e}")
            bot_logger.error(f"Error in continue command: {e}")

    @bot.message_handler(commands=['setspeed'])
    @rate_limit
    def set_simulation_speed(message):
        parts = message.text.split(maxsplit=2)
        if len(parts) < 3:
            bot.reply_to(message, "Usage: /setspeed <tracking_number> <speed>\nExample: /setspeed JD1234567890 2.0")
            return
        tracking_number = sanitize_tracking_number(parts[1].strip())
        if not tracking_number:
            bot.reply_to(message, "Invalid tracking number.")
            return
        try:
            speed = float(parts[2].strip())
            if speed < 0.1 or speed > 10:
                bot.reply_to(message, "Speed must be between 0.1 and 10.0.")
                return
            shipment = get_shipment_with_context(tracking_number)
            if not shipment:
                bot.reply_to(message, f"Shipment `{tracking_number}` not found.", parse_mode='Markdown')
                return
            if redis_client:
                safe_redis_operation(redis_client.hset, "sim_speed_multipliers", tracking_number, str(speed))
            bot.reply_to(message, f"⚡ Simulation speed set to `{speed}x` for `{tracking_number}`.", parse_mode='Markdown')
        except Exception as e:
            bot.reply_to(message, f"Error: {e}")
            bot_logger.error(f"Error in setspeed command: {e}")

    @bot.message_handler(commands=['generate'])
    @rate_limit
    def handle_generate(message):
        tracking_id = generate_unique_id()
        bot.reply_to(message, f"Generated Tracking ID: `{tracking_id}`", parse_mode='Markdown')

    @bot.message_handler(commands=['list'])
    @rate_limit
    def list_shipments(message):
        if not is_admin(message.from_user.id):
            bot.reply_to(message, "Access denied.")
            return
        try:
            shipments, total = get_all_shipments_with_context(page=1)
            if not shipments:
                bot.reply_to(message, "No shipments available.", parse_mode='Markdown')
                return
            markup = InlineKeyboardMarkup(row_width=1)
            for s in shipments:
                tn = s['tracking_number']
                label = f"{tn} [{s['status']}]"
                markup.add(InlineKeyboardButton(label, callback_data=f"view_{tn}"))
            if total > 10:
                markup.add(InlineKeyboardButton("Next", callback_data="list_2"))
            markup.add(InlineKeyboardButton("🏠 Home", callback_data="menu_page_1"))
            bot.reply_to(message, f"*📋 Shipment List* (Page 1, {total} total):", parse_mode='Markdown', reply_markup=markup)
        except Exception as e:
            bot.reply_to(message, f"Error: {e}")
            bot_logger.error(f"Error in list command: {e}")

    @bot.message_handler(commands=['add'])
    @rate_limit
    def add_shipment(message):
        if not is_admin(message.from_user.id):
            bot.reply_to(message, "Access denied.")
            return
        parts = message.text.strip().split(maxsplit=3)
        if len(parts) < 4:
            bot.reply_to(message, "Usage: /add <tracking_number> <status> <delivery_location> [origin_location] [recipient_email] [webhook_url]")
            return
        tracking_number = sanitize_tracking_number(parts[1].strip())
        status = parts[2].strip()
        delivery_location = parts[3].strip()
        args = message.text.split(maxsplit=6)[4:] if len(message.text.split()) > 4 else []
        origin_location = args[0] if len(args) > 0 else "Lagos, NG"
        recipient_email = args[1] if len(args) > 1 else None
        webhook_url = args[2] if len(args) > 2 else None
        try:
            if not tracking_number:
                bot.reply_to(message, "Invalid tracking number. Must be JDxxxxxxxxxx")
                return
            if status not in config.valid_statuses:
                bot.reply_to(message, f"Invalid status. Must be one of: {', '.join(config.valid_statuses)}")
                return
            if save_shipment_with_context(tracking_number, status, '', delivery_location,
                                         recipient_email, origin_location, webhook_url, "DHL"):
                bot.reply_to(message, f"✅ Shipment `{tracking_number}` added.", parse_mode='Markdown')
            else:
                bot.reply_to(message, "❌ Failed to add shipment.")
        except Exception as e:
            bot.reply_to(message, f"Error: {e}")
            bot_logger.error(f"Error in add command: {e}")

    @bot.message_handler(commands=['export'])
    @rate_limit
    def export_shipments_command(message):
        if not is_admin(message.from_user.id):
            bot.reply_to(message, "Access denied.")
            return
        try:
            export_data = export_shipments_with_context()
            if not export_data:
                bot.reply_to(message, "No shipments to export.", parse_mode='Markdown')
                return
            max_length = 4096
            if len(export_data) <= max_length:
                bot.reply_to(message, f"```json\n{export_data}\n```", parse_mode='Markdown')
            else:
                parts = [export_data[i:i+max_length] for i in range(0, len(export_data), max_length)]
                for i, part in enumerate(parts, 1):
                    bot.reply_to(message, f"```json\nPart {i}/{len(parts)}:\n{part}\n```", parse_mode='Markdown')
        except Exception as e:
            bot.reply_to(message, f"Error: {e}")
            bot_logger.error(f"Error in export command: {e}")

    @bot.message_handler(commands=['logs'])
    @rate_limit
    def get_logs_command(message):
        if not is_admin(message.from_user.id):
            bot.reply_to(message, "Access denied.")
            return
        try:
            logs = get_recent_logs_with_context(limit=5)
            if not logs:
                bot.reply_to(message, "No logs available.", parse_mode='Markdown')
                return
            response = "*📋 Recent Logs*:\n" + "\n".join([f"`{log}`" for log in logs])
            bot.reply_to(message, response, parse_mode='Markdown')
        except Exception as e:
            bot.reply_to(message, f"Error: {e}")
            bot_logger.error(f"Error in logs command: {e}")

    # === CALLBACK HANDLERS ===
    @bot.callback_query_handler(func=lambda call: True)
    def handle_callback(call):
        data = call.data
        try:
            if data.startswith("menu_page_"):
                page = int(data.split("_")[-1])
                send_dynamic_menu(call.message.chat.id, call.message.message_id, page)
            elif data.startswith("view_"):
                tracking_number = data.split("_", 1)[1]
                shipment = get_shipment_with_context(tracking_number)
                if not shipment:
                    bot.answer_callback_query(call.id, f"Shipment `{tracking_number}` not found.", show_alert=True)
                    return
                paused = safe_redis_operation(redis_client.hget, "paused_simulations", tracking_number) == "true" if redis_client else False
                speed = float(safe_redis_operation(redis_client.hget, "sim_speed_multipliers", tracking_number) or 1.0) if redis_client else 1.0
                distance = estimate_distance(shipment.get('origin_location') or "Lagos, NG", shipment.get('delivery_location', ''))
                service_level = safe_redis_operation(redis_client.hget, "service_level", tracking_number) if redis_client else None
                if service_level and isinstance(service_level, bytes):
                    service_level = service_level.decode('utf-8')
                delivery_window = safe_redis_operation(redis_client.hget, "delivery_window", tracking_number) if redis_client else None
                if delivery_window and isinstance(delivery_window, bytes):
                    delivery_window = delivery_window.decode('utf-8')
                response = (
                    f"*Shipment*: `{tracking_number}`\n"
                    f"*Carrier*: `{shipment.get('carrier', 'DHL')}`\n"
                    f"*Status*: `{shipment['status']}`\n"
                    f"*Service*: `{service_level or 'DHL Express'}`\n"
                    f"*Delivery Window*: `{delivery_window or 'Calculating...'}`\n"
                    f"*Paused*: `{paused}`\n"
                    f"*Speed*: `{speed}x`\n"
                    f"*Distance*: `{distance} km`\n"
                    f"*Origin*: `{shipment.get('origin_location', 'N/A')}`\n"
                    f"*Destination*: `{shipment['delivery_location']}`\n"
                    f"*Checkpoints*: `{len(shipment.get('checkpoints', []))}`"
                )
                markup = InlineKeyboardMarkup(row_width=2)
                if shipment['status'] not in ['Delivered', 'Returned']:
                    markup.add(
                        InlineKeyboardButton("⏸ Pause" if not paused else "▶️ Resume",
                                           callback_data=f"{'pause' if not paused else 'resume'}_{tracking_number}_1"),
                        InlineKeyboardButton("⚡ Speed", callback_data=f"setspeed_{tracking_number}")
                    )
                markup.add(
                    InlineKeyboardButton("📧 Notify", callback_data=f"notify_{tracking_number}"),
                    InlineKeyboardButton("🔗 Webhook", callback_data=f"set_webhook_{tracking_number}"),
                    InlineKeyboardButton("🧪 Test Webhook", callback_data=f"test_webhook_{tracking_number}"),
                    InlineKeyboardButton("🏠 Home", callback_data="menu_page_1")
                )
                bot.edit_message_text(response, chat_id=call.message.chat.id, message_id=call.message.message_id,
                                     parse_mode='Markdown', reply_markup=markup)
            elif data.startswith("pause_"):
                tracking_number, page = data.split("_")[1], int(data.split("_")[2])
                if redis_client:
                    safe_redis_operation(redis_client.hset, "paused_simulations", tracking_number, "true")
                bot.answer_callback_query(call.id, f"⏸ Simulation paused for `{tracking_number}`.")
                show_shipment_menu(call, page, "view", "Select shipment to view")
            elif data.startswith("resume_"):
                tracking_number, page = data.split("_")[1], int(data.split("_")[2])
                if redis_client:
                    safe_redis_operation(redis_client.hdel, "paused_simulations", tracking_number)
                bot.answer_callback_query(call.id, f"▶️ Simulation resumed for `{tracking_number}`.")
                show_shipment_menu(call, page, "view", "Select shipment to view")
            elif data.startswith("setspeed_"):
                tracking_number = data.split("_", 1)[1]
                bot.answer_callback_query(call.id, f"Enter speed for `{tracking_number}` (0.1 to 10.0):", show_alert=True)
                bot.register_next_step_handler(call.message, lambda msg: handle_set_speed(msg, tracking_number))
            elif data.startswith("notify_"):
                tracking_number = data.split("_", 1)[1]
                shipment = get_shipment_with_context(tracking_number)
                if not shipment:
                    bot.answer_callback_query(call.id, f"Shipment `{tracking_number}` not found.", show_alert=True)
                    return
                markup = InlineKeyboardMarkup(row_width=2)
                if shipment.get('recipient_email') and shipment.get('email_notifications'):
                    markup.add(InlineKeyboardButton("📧 Send Email", callback_data=f"send_email_{tracking_number}"))
                if shipment.get('webhook_url'):
                    markup.add(InlineKeyboardButton("🔗 Send Webhook", callback_data=f"send_webhook_{tracking_number}"))
                markup.add(InlineKeyboardButton("🏠 Home", callback_data="menu_page_1"))
                bot.edit_message_text(f"Select notification type for `{tracking_number}`:", chat_id=call.message.chat.id,
                                     message_id=call.message.message_id, parse_mode='Markdown', reply_markup=markup)
            elif data.startswith("send_email_"):
                tracking_number = data.split("_", 2)[2]
                shipment = get_shipment_with_context(tracking_number)
                if not shipment or not shipment.get('recipient_email'):
                    bot.answer_callback_query(call.id, "No email configured.", show_alert=True)
                    return
                notification_data = {
                    "tracking_number": tracking_number,
                    "type": "email",
                    "data": {
                        "recipient_email": shipment['recipient_email'],
                        "status": shipment['status'],
                        "checkpoints": "; ".join(shipment.get('checkpoints', [])),
                        "delivery_location": shipment['delivery_location']
                    }
                }
                if enqueue_notification(notification_data):
                    bot.answer_callback_query(call.id, f"📧 Email enqueued for `{tracking_number}`.")
                else:
                    bot.answer_callback_query(call.id, "Failed to enqueue email.", show_alert=True)
            elif data.startswith("send_webhook_"):
                tracking_number = data.split("_", 2)[2]
                shipment = get_shipment_with_context(tracking_number)
                if not shipment or not shipment.get('webhook_url'):
                    bot.answer_callback_query(call.id, "No webhook URL set.", show_alert=True)
                    return
                notification_data = {
                    "tracking_number": tracking_number,
                    "type": "webhook",
                    "data": {
                        "status": shipment['status'],
                        "checkpoints": "; ".join(shipment.get('checkpoints', [])),
                        "delivery_location": shipment['delivery_location'],
                        "webhook_url": shipment['webhook_url']
                    }
                }
                if enqueue_notification(notification_data):
                    bot.answer_callback_query(call.id, f"🔗 Webhook enqueued for `{tracking_number}`.")
                else:
                    bot.answer_callback_query(call.id, "Failed to enqueue webhook.", show_alert=True)
            elif data.startswith("set_webhook_"):
                tracking_number = data.split("_", 2)[2]
                bot.answer_callback_query(call.id, f"Enter webhook URL for `{tracking_number}`:", show_alert=True)
                bot.register_next_step_handler(call.message, lambda msg: handle_set_webhook(msg, tracking_number))
            elif data.startswith("test_webhook_"):
                tracking_number = data.split("_", 2)[2]
                send_manual_webhook(call, tracking_number)
            elif data.startswith("list_"):
                page = int(data.split("_")[-1])
                show_shipment_menu(call, page, "view", "Select shipment to view")
            elif data == "generate_id":
                new_id = generate_unique_id()
                bot.answer_callback_query(call.id, f"Generated ID: `{new_id}`", show_alert=True)
            elif data == "add":
                bot.answer_callback_query(call.id, "Enter: tracking_number status delivery_location [origin] [email] [webhook]", show_alert=True)
                bot.register_next_step_handler(call.message, handle_add_shipment)
            elif data == "help":
                help_text = (
                    "*📚 Help Menu*\n\n"
                    "/start - Show menu\n"
                    "/track JD... - Track shipment\n"
                    "/notify JD... - Send notification\n"
                    "/stop JD... - Pause simulation\n"
                    "/continue JD... - Resume simulation\n"
                    "/setspeed JD... 2.0 - Set speed\n"
                    "/generate - New ID\n"
                    "/add - Create shipment\n"
                    "/list - View all shipments\n"
                    "/export - Download JSON\n"
                    "/logs - View logs\n"
                    "/stats - System statistics\n"
                    "/search - Search shipments\n"
                    "/bulk_action - Bulk operations\n"
                    "/myid - Get your user ID"
                )
                bot.edit_message_text(help_text, call.message.chat.id, call.message.message_id,
                                     parse_mode='Markdown', reply_markup=InlineKeyboardMarkup().add(
                                         InlineKeyboardButton("🏠 Home", callback_data="menu_page_1")))
            bot.answer_callback_query(call.id)
        except Exception as e:
            bot.answer_callback_query(call.id, f"Error: {e}", show_alert=True)
            bot_logger.error(f"Callback error: {e}")

    # === STEP HANDLERS ===
    def handle_set_speed(message, tracking_number):
        try:
            speed = float(message.text.strip())
            if speed < 0.1 or speed > 10:
                bot.reply_to(message, "Speed must be 0.1 to 10.0")
                return
            if redis_client:
                safe_redis_operation(redis_client.hset, "sim_speed_multipliers", tracking_number, str(speed))
            bot.reply_to(message, f"⚡ Speed set to `{speed}x` for `{tracking_number}`.", parse_mode='Markdown')
        except:
            bot.reply_to(message, "❌ Invalid speed.")

    def handle_set_webhook(message, tracking_number):
        webhook_url = message.text.strip()
        if not re.match(r'^https?://[^\s/$.?#].[^\s]*$', webhook_url):
            bot.reply_to(message, "❌ Invalid URL.")
            return
        if update_shipment_with_context(tracking_number, webhook_url=webhook_url):
            bot.reply_to(message, f"🔗 Webhook set for `{tracking_number}`.", parse_mode='Markdown')
        else:
            bot.reply_to(message, "❌ Failed to set webhook.")

    def handle_add_shipment(message):
        parts = message.text.strip().split()
        if len(parts) < 3:
            bot.reply_to(message, "Usage: tracking_number status delivery_location [origin] [email] [webhook]")
            return
        tn, status, dest = parts[:3]
        origin = parts[3] if len(parts) > 3 else "Lagos, NG"
        email = parts[4] if len(parts) > 4 else None
        webhook = parts[5] if len(parts) > 5 else None
        tn = sanitize_tracking_number(tn)
        if not tn or status not in config.valid_statuses:
            bot.reply_to(message, "Invalid tracking number or status.")
            return
        if save_shipment_with_context(tn, status, '', dest, email, origin, webhook, "DHL"):
            bot.reply_to(message, f"✅ Shipment `{tn}` added.", parse_mode='Markdown')
        else:
            bot.reply_to(message, "❌ Failed to add shipment.")

    def get_startup_welcome_text() -> str:
        return (
            "🚀 *DHL Shipment Bot is online!*\n\n"
            "📦 *Commands:*\n"
            "/menu - Open admin panel\n"
            "/track <tracking_number> - View shipment details\n"
            "/notify <tracking_number> - Trigger notifications\n"
            "/stop <tracking_number> - Pause simulation\n"
            "/continue <tracking_number> - Resume simulation\n"
            "/setspeed <tracking_number> <speed> - Set simulation speed\n"
            "/list - List all shipments\n"
            "/search <query> - Search shipments\n"
            "/stats - System statistics\n"
            "/export - Export shipments as JSON\n"
            "/logs - View recent logs\n"
            "/myid - Get your Telegram user ID\n\n"
            "💡 *Tip:* You can also just send me a tracking number directly!"
        )

    def notify_admins_on_startup() -> None:
        if not config.allowed_admins:
            bot_logger.warning("No allowed admins configured for startup notifications.")
            return

        if redis_client:
            already_notified = safe_redis_operation(redis_client.exists, "bot:startup_notified")
            if already_notified:
                bot_logger.info("Startup welcome already sent recently; skipping.")
                return

        welcome_text = get_startup_welcome_text()
        for admin_id in config.allowed_admins:
            try:
                bot.send_message(admin_id, welcome_text, disable_web_page_preview=True)
                bot_logger.info(f"Sent startup welcome to admin {admin_id}")
            except Exception as e:
                bot_logger.error(f"Failed to send startup welcome to {admin_id}: {e}")

        if redis_client:
            safe_redis_operation(redis_client.set, "bot:startup_notified", "1", ex=86400)

    def is_valid_webhook_url(url: str) -> bool:
        if not url:
            return False
        try:
            parsed = urlparse(url)
            return parsed.scheme in ("http", "https") and bool(parsed.netloc)
        except Exception:
            return False

    def is_hostname_resolvable(hostname: str) -> bool:
        if not hostname:
            return False
        try:
            socket.getaddrinfo(hostname, None)
            return True
        except socket.gaierror as e:
            bot_logger.error(f"Webhook hostname resolution failed: {hostname} -> {e}")
            return False

    # === CUSTOM POLLING USING REQUESTS (AVOIDS RECURSION) ===
    _polling_thread = None
    _polling_active = False

    def start_polling_fallback():
        global _polling_thread, _polling_active
        if bot is None:
            return

        if _polling_thread and _polling_thread.is_alive():
            bot_logger.info("Polling already running; skipping start")
            return

        if _polling_active:
            bot_logger.info("Polling already active; skipping start")
            return

        def poll():
            global _polling_active
            bot_logger.info("Starting manual polling loop using requests...")
            offset = 0
            _polling_active = True
            token = config.telegram_bot_token
            if not token:
                bot_logger.error("No bot token for polling")
                return
            while True:
                try:
                    url = f"https://api.telegram.org/bot{token}/getUpdates"
                    params = {"offset": offset, "timeout": 30}
                    resp = requests.get(url, params=params, timeout=35)
                    if resp.status_code != 200:
                        bot_logger.error(f"Polling HTTP error: {resp.status_code}")
                        time.sleep(5)
                        continue
                    data = resp.json()
                    if data.get("ok"):
                        updates = data.get("result", [])
                        if updates:
                            offset = updates[-1]["update_id"] + 1
                            # Convert dicts to Update objects
                            from telebot import types
                            update_objects = [types.Update.de_json(u) for u in updates]
                            try:
                                bot.process_new_updates(update_objects)
                            except RecursionError as re:
                                bot_logger.error(f"Recursion error processing updates: {re}")
                                # Continue, don't break
                        else:
                            # no updates, continue
                            pass
                        time.sleep(1)
                    else:
                        bot_logger.error(f"Polling API error: {data}")
                        time.sleep(5)
                except RecursionError as re:
                    bot_logger.error(f"Recursion error in polling: {re}")
                    time.sleep(10)
                except Exception as e:
                    bot_logger.error(f"Polling error: {e}")
                    time.sleep(5)

        _polling_thread = threading.Thread(target=poll, daemon=True)
        _polling_thread.start()
        bot_logger.info("Manual polling thread started")

    def start_bot_service() -> None:
        try:
            with app.app_context():
                cache_route_templates()
            bot_logger.info("Route templates cached")
        except Exception as e:
            bot_logger.error(f"Failed to cache route templates: {e}")

        bot_logger.info("Using polling mode (webhook disabled to avoid recursion)")
        start_polling_fallback()

        try:
            notify_admins_on_startup()
        except Exception as e:
            bot_logger.error(f"Failed to notify admins: {e}")

    def send_manual_webhook(call, tracking_number):
        shipment = get_shipment_with_context(tracking_number)
        if not shipment or not shipment.get('webhook_url'):
            bot.answer_callback_query(call.id, "No webhook URL.", show_alert=True)
            return
        data = {
            "tracking_number": tracking_number,
            "type": "webhook",
            "data": {
                "status": shipment['status'],
                "checkpoints": "; ".join(shipment.get('checkpoints', [])),
                "delivery_location": shipment['delivery_location'],
                "webhook_url": shipment['webhook_url']
            }
        }
        if enqueue_notification(data):
            bot.answer_callback_query(call.id, "🔗 Webhook enqueued.")
        else:
            bot.answer_callback_query(call.id, "❌ Failed.", show_alert=True)

    def keep_alive_loop():
        while True:
            try:
                if redis_client:
                    redis_client.ping()
                with app.app_context():
                    db.session.execute(text('SELECT 1'))
                bot_logger.debug("Health check passed")
            except Exception as e:
                bot_logger.error(f"Health check failed: {e}")
            time.sleep(300)

    def main():
        try:
            start_bot_service()
            bot_logger.info("Bot service started")
            console.print("[green]✅ bot.py started — DHL + All Features Live[/green]")

            keep_alive_thread = threading.Thread(target=keep_alive_loop, daemon=True)
            keep_alive_thread.start()
            bot_logger.info("Keep-alive loop started")

            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            console.print("[yellow]Bot shutting down...[/yellow]")
            bot_logger.info("Bot shutting down")
        except Exception as e:
            console.print(f"[red]Fatal error: {e}[/red]")
            bot_logger.error(f"Fatal error: {e}")
            raise

    if __name__ == "__main__":
        main()
else:
    bot_logger.warning("Telegram bot not configured. Set TELEGRAM_BOT_TOKEN to enable.")
    console.print("[yellow]⚠️ Telegram bot not configured. Set TELEGRAM_BOT_TOKEN to enable.[/yellow]")

    def main():
        console.print("[yellow]Bot is disabled. Set TELEGRAM_BOT_TOKEN to enable.[/yellow]")
        while True:
            time.sleep(60)

    if __name__ == "__main__":
        main()
