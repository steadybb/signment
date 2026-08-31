# utils.py

import os
from dotenv import load_dotenv
import re
import json
import logging
import socket
import time
from datetime import datetime, timedelta
from telebot import TeleBot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from functools import wraps
from collections import deque
from rich.console import Console
from flask_sqlalchemy import SQLAlchemy
from flask import Flask
from math import radians, sin, cos, sqrt, atan2
from typing import Optional, List, Tuple, Dict, Any
from urllib.parse import quote_plus, urlparse
from sqlalchemy import create_engine, text
import requests

load_dotenv()

LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO').upper()
LOG_FORMAT = os.getenv('LOG_FORMAT', '%(asctime)s %(levelname)s [%(name)s] %(message)s')

def configure_logging() -> None:
    level = getattr(logging, LOG_LEVEL, logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        root.addHandler(handler)
    else:
        for handler in root.handlers:
            if not handler.formatter:
                handler.setFormatter(logging.Formatter(LOG_FORMAT))
    logging.getLogger('werkzeug').setLevel(level)
    sa_level_name = os.getenv('SQLALCHEMY_LOG_LEVEL', 'WARNING').upper()
    sa_level = getattr(logging, sa_level_name, logging.WARNING)
    logging.getLogger('sqlalchemy.engine').setLevel(sa_level)

configure_logging()

bot_logger = logging.getLogger('telegram_bot')
console = Console()

class RedisClientProxy:
    def __init__(self):
        self._client = None

    def set_client(self, client):
        self._client = client

    def get_client(self):
        return self._client

    def __bool__(self):
        return self._client is not None

    def __getattr__(self, name):
        def _missing(*args, **kwargs):
            client = self._client
            if client is None:
                raise RuntimeError(f"Redis client is not initialized: {name}")
            return getattr(client, name)(*args, **kwargs)
        return _missing


redis_client = RedisClientProxy()
redis_url = os.getenv("REDIS_URL", "").strip()
redis_token = os.getenv("REDIS_TOKEN", "")
redis_user = os.getenv("REDISUSER", os.getenv("REDIS_USER", "default"))
redis_password = os.getenv("REDIS_PASSWORD", os.getenv("REDISPASSWORD", os.getenv("REDIS_PASS", "")))
redis_host = os.getenv("REDISHOST", os.getenv("REDIS_HOST", "")).strip()
redis_port = os.getenv("REDISPORT", os.getenv("REDIS_PORT", "6379")).strip()

redis_disable_count = 0
last_redis_disable = None
REDIS_RECONNECT_BASE = int(os.getenv('REDIS_RECONNECT_BASE_SECONDS', '5'))
REDIS_RECONNECT_MAX = int(os.getenv('REDIS_RECONNECT_MAX_SECONDS', '60'))

email_throttle_cache = {}
email_digest_cache = {}

EMAIL_TEST_MODE = os.getenv('EMAIL_TEST_MODE', 'false').lower() == 'true'
EMAIL_ENABLED = os.getenv('EMAIL_ENABLED', 'true').lower() == 'true'
AUTO_EMAIL_ENABLED = os.getenv('AUTO_EMAIL_ENABLED', 'true').lower() == 'true'
EMAIL_THROTTLE_MINUTES = int(os.getenv('EMAIL_THROTTLE_MINUTES', '60'))

recent_socket_events = deque(maxlen=200)
recent_client_errors = deque(maxlen=200)

SIMULATOR_THREAD_LIMIT = int(os.getenv('SIMULATOR_THREAD_LIMIT', '8'))
_simulator_thread_count = 0
_simulator_thread_lock = None

def _get_simulator_thread_lock():
    global _simulator_thread_lock
    if _simulator_thread_lock is None:
        import threading
        _simulator_thread_lock = threading.Lock()
    return _simulator_thread_lock

def can_start_simulation() -> bool:
    global _simulator_thread_count
    lock = _get_simulator_thread_lock()
    with lock:
        return _simulator_thread_count < SIMULATOR_THREAD_LIMIT

def register_simulation_start() -> bool:
    global _simulator_thread_count
    lock = _get_simulator_thread_lock()
    with lock:
        if _simulator_thread_count >= SIMULATOR_THREAD_LIMIT:
            return False
        _simulator_thread_count += 1
        return True

def register_simulation_stop() -> None:
    global _simulator_thread_count
    lock = _get_simulator_thread_lock()
    with lock:
        _simulator_thread_count = max(0, _simulator_thread_count - 1)

def add_socket_event(details: dict):
    try:
        recent_socket_events.append({**details, 'timestamp': datetime.utcnow().isoformat()})
    except Exception:
        pass

def add_client_error(payload: dict):
    try:
        recent_client_errors.append({**payload, 'timestamp': datetime.utcnow().isoformat()})
    except Exception:
        pass

# Stub: spawn_simulation is overridden by app.py
def spawn_simulation(tracking_number: str):
    bot_logger.warning(
        f"spawn_simulation called from utils for {tracking_number} — "
        "this should only run from app.py. No simulation started."
    )
    return None

def _resolve_template_url(url: str) -> str:
    if not url:
        return url
    resolved = url.strip()
    placeholders = {
        "${{REDISUSER}}": redis_user,
        "${{REDIS_PASSWORD}}": redis_password,
        "${{REDISPASSWORD}}": redis_password,
        "${{REDISHOST}}": redis_host,
        "${{REDISPORT}}": redis_port,
        "${REDISUSER}": redis_user,
        "${REDIS_PASSWORD}": redis_password,
        "${REDISPASSWORD}": redis_password,
        "${REDISHOST}": redis_host,
        "${REDISPORT}": redis_port,
        "{{REDISUSER}}": redis_user,
        "{{REDIS_PASSWORD}}": redis_password,
        "{{REDISPASSWORD}}": redis_password,
        "{{REDISHOST}}": redis_host,
        "{{REDISPORT}}": redis_port,
    }
    for placeholder, value in placeholders.items():
        resolved = resolved.replace(placeholder, value)
    if "{{" in resolved or "}}" in resolved:
        return ""
    return resolved

def _build_redis_url(user: str, password: str, host: str, port: str) -> str:
    if not host:
        return ""
    auth = ""
    if user:
        auth = quote_plus(user)
        if password:
            auth = f"{auth}:{quote_plus(password)}"
    return f"redis://{auth + '@' if auth else ''}{host}:{port}"

def _is_hostname_resolvable(hostname: str) -> bool:
    if not hostname:
        return False
    try:
        socket.getaddrinfo(hostname, None)
        return True
    except Exception as e:
        console.print(f"[yellow]Redis hostname not resolvable: {hostname} ({e})[/yellow]")
        return False

def _initialize_redis_client() -> None:
    if not redis_url:
        redis_client.set_client(None)
        return
    redis_url_local = redis_url.strip()
    parsed_url = urlparse(redis_url_local)
    scheme = parsed_url.scheme.lower()
    hostname = parsed_url.hostname
    if hostname and not _is_hostname_resolvable(hostname):
        console.print(f"[yellow]Redis hostname lookup warning: {hostname}[/yellow]")
    try:
        if scheme in ("https", "http") or "upstash" in redis_url_local.lower() or scheme.startswith("upstash"):
            from upstash_redis import Redis as UpstashRedis
            client = UpstashRedis(url=redis_url_local, token=redis_token)
            client.set("health_check", "ok", ex=10)
            client.delete("health_check")
            redis_client.set_client(client)
            console.print("[green]Upstash Redis connected[/green]")
        elif scheme in ("redis", "rediss"):
            try:
                from redis import Redis as RedisClient, ConnectionPool
                max_conn = int(os.getenv('REDIS_MAX_CONNECTIONS', '4'))
                pool = ConnectionPool.from_url(redis_url_local, decode_responses=True, max_connections=max_conn, socket_timeout=3, socket_connect_timeout=3)
                client = RedisClient(connection_pool=pool, decode_responses=True)
                client.ping()
                redis_client.set_client(client)
                console.print(f"[green]Redis connected (max_connections={max_conn})[/green]")
            except Exception as e:
                console.print(f"[yellow]Redis connect failed: {e}[/yellow]")
                redis_client.set_client(None)
        else:
            console.print(f"[yellow]Redis unavailable: unsupported Redis scheme '{scheme}'[/yellow]")
            redis_client.set_client(None)
    except Exception as e:
        if scheme in ("redis", "rediss") or redis_url_local.lower().startswith("redis://"):
            try:
                from redis import Redis as RedisClient
                client = RedisClient.from_url(redis_url_local, decode_responses=True)
                client.ping()
                redis_client.set_client(client)
                console.print("[green]Redis connected via redis-py fallback[/green]")
            except Exception as fallback_error:
                console.print(f"[yellow]Redis unavailable: {fallback_error}[/yellow]")
                redis_client.set_client(None)
        else:
            console.print(f"[yellow]Redis unavailable: {e}[/yellow]")
            redis_client.set_client(None)

def get_redis_client():
    if not redis_client:
        _initialize_redis_client()
    return redis_client.get_client()

if redis_url:
    redis_url = _resolve_template_url(redis_url)

if not redis_url and redis_host:
    redis_url = _build_redis_url(redis_user, redis_password, redis_host, redis_port)

_initialize_redis_client()

def _redis_reconnect_loop():
    import threading
    backoff = REDIS_RECONNECT_BASE
    while True:
        try:
            client = redis_client.get_client()
            if client:
                try:
                    client.ping()
                    backoff = REDIS_RECONNECT_BASE
                except Exception:
                    try:
                        redis_client.set_client(None)
                    except Exception:
                        pass
                    time.sleep(min(backoff, REDIS_RECONNECT_MAX))
                    backoff = min(backoff * 2, REDIS_RECONNECT_MAX)
                    continue
            else:
                _initialize_redis_client()
                if not redis_client.get_client():
                    time.sleep(min(backoff, REDIS_RECONNECT_MAX))
                    backoff = min(backoff * 2, REDIS_RECONNECT_MAX)
                    continue
                backoff = REDIS_RECONNECT_BASE
        except Exception:
            pass
        time.sleep(REDIS_RECONNECT_BASE)

try:
    import threading
    t = threading.Thread(target=_redis_reconnect_loop, daemon=True)
    t.start()
except Exception:
    pass

def get_redis_metrics() -> Dict[str, Any]:
    return {
        'connected': bool(redis_client.get_client()),
        'disable_count': globals().get('redis_disable_count', 0),
        'last_disable': globals().get('last_redis_disable')
    }

in_memory_sim = {}
in_memory_clients = {}

def rget(field: str, tn: str, default=None):
    global redis_client
    try:
        if not redis_client:
            return in_memory_sim.get(tn, {}).get(field, default)
        val = redis_client.hget(field, tn)
        if val is None:
            return in_memory_sim.get(tn, {}).get(field, default)
        if isinstance(val, bytes):
            return val.decode('utf-8')
        return val
    except Exception:
        return in_memory_sim.get(tn, {}).get(field, default)

def rset(field: str, tn: str, value):
    global redis_client
    if not redis_client:
        try:
            in_memory_sim.setdefault(tn, {})[field] = value
        except Exception:
            pass
        return
    try:
        redis_client.hset(field, tn, value)
    except Exception:
        try:
            in_memory_sim.setdefault(tn, {})[field] = value
        except Exception:
            pass

def rkeys(pattern: str):
    try:
        if not redis_client:
            return []
        keys = redis_client.keys(pattern) or []
        return [k.decode() if isinstance(k, bytes) else k for k in keys]
    except Exception as e:
        bot_logger.warning(f"Redis keys failed for pattern {pattern}: {e}")
        return []

def rhgetall(key: str):
    try:
        if not redis_client:
            return {}
        d = redis_client.hgetall(key) or {}
        return { (k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v) for k, v in d.items() }
    except Exception as e:
        bot_logger.warning(f"Redis hgetall failed for {key}: {e}")
        return {}

def rlist_lpop(key: str):
    try:
        if not redis_client:
            return None
        v = redis_client.lpop(key)
        if v is None:
            return None
        return v.decode() if isinstance(v, bytes) else v
    except Exception as e:
        bot_logger.warning(f"Redis lpop failed for {key}: {e}")
        return None

def rexists(key: str) -> bool:
    try:
        if not redis_client:
            return False
        return bool(redis_client.exists(key))
    except Exception as e:
        bot_logger.warning(f"Redis exists check failed for {key}: {e}")
        return False

def rhlen(key: str) -> int:
    try:
        if not redis_client:
            return 0
        return redis_client.hlen(key)
    except Exception as e:
        bot_logger.warning(f"Redis hlen failed for {key}: {e}")
        return 0

class BotConfig:
    def __init__(
        self,
        telegram_bot_token=None,
        redis_url=None,
        redis_token=None,
        webhook_url=None,
        websocket_server=None,
        allowed_admins=None,
        valid_statuses=None,
        route_templates=None,
        smtp_host=None,
        smtp_port=None,
        smtp_user=None,
        smtp_pass=None,
        smtp_from=None,
    ):
        self.telegram_bot_token = (
            telegram_bot_token
            if telegram_bot_token is not None
            else os.getenv("TELEGRAM_BOT_TOKEN", os.getenv("TELEGRAM_TOKEN", ""))
        )
        self.redis_url = redis_url if redis_url is not None else os.getenv("REDIS_URL")
        self.redis_token = redis_token if redis_token is not None else os.getenv("REDIS_TOKEN", "")
        self.webhook_url = webhook_url if webhook_url is not None else os.getenv("WEBHOOK_URL", "https://signment-9a96.onrender.com/telegram/webhook")
        self.websocket_server = websocket_server if websocket_server is not None else os.getenv("WEBSOCKET_SERVER", "https://signment-9a96.onrender.com")
        self.allowed_admins = (
            allowed_admins
            if allowed_admins is not None
            else [int(uid) for uid in os.getenv("ALLOWED_ADMINS", "").split(",") if uid.strip()]
        )
        self.valid_statuses = (
            valid_statuses
            if valid_statuses is not None
            else os.getenv("VALID_STATUSES", "Pending,In_Transit,Out_for_Delivery,Delivered,Returned,Delayed,On_Hold").split(",")
        )
        self.route_templates = (
            route_templates
            if route_templates is not None
            else json.loads(os.getenv("ROUTE_TEMPLATES", '{"Lagos, NG": ["Lagos, NG"]}'))
        )
        self.smtp_host = smtp_host if smtp_host is not None else os.getenv("SMTP_HOST", "smtp.gmail.com")
        self.smtp_port = smtp_port if smtp_port is not None else int(os.getenv("SMTP_PORT", 587))
        self.smtp_user = smtp_user if smtp_user is not None else os.getenv("SMTP_USER", "")
        self.smtp_pass = smtp_pass if smtp_pass is not None else os.getenv("SMTP_PASS", "")
        self.smtp_from = smtp_from if smtp_from is not None else os.getenv("SMTP_FROM", "no-reply@example.com")

try:
    config = BotConfig()
except Exception as e:
    bot_logger.error(f"Config init failed: {e}")
    raise

DHL_CONFIG = {
    "name": "DHL Express",
    "primary_color": "#D40511",
    "secondary_color": "#FFCC00",
    "logo_url": "https://www.dhl.com/etc.clientlibs/dhl/clientlibs/clientlib-site/resources/images/dhl-logo.svg",
    "tracking_prefix": "JD",
    "tracking_format": r"^JD\d{10}$",
    "status_flow": {
        "Pending": {"next": ["In_Transit"], "delay": [60, 180]},
        "On_Hold": {"next": ["In_Transit", "Delayed"], "delay": [3600, 86400]},
        "In_Transit": {"next": ["Out_for_Delivery", "Delayed"], "delay": [120, 600], "probabilities": [0.92, 0.08]},
        "Out_for_Delivery": {"next": ["Delivered"], "delay": [60, 240]},
        "Delayed": {"next": ["Out_for_Delivery"], "delay": [300, 900]},
        "Delivered": {"next": [], "delay": [0, 0]},
        "Returned": {"next": [], "delay": [0, 0]}
    },
    "events": {
        "In_Transit": ["Shipment picked up", "Departed origin facility", "Arrived at sort facility", "Processed at hub"],
        "Out_for_Delivery": ["Out for delivery", "With delivery courier"],
        "Delayed": ["Held at customs", "Weather delay", "Routing delay"],
        "On_Hold": ["Held for customs clearance", "Awaiting clearance documentation"]
    }
}

# ============================================================
# Database URI fallback function (kept only here)
# ============================================================
def get_working_database_uri(configured_uri):
    """
    Test the configured database URI. If it works, return it.
    Otherwise, fall back to SQLite.
    """
    if configured_uri and configured_uri.startswith('sqlite'):
        logging.info(f"Using SQLite database: {configured_uri}")
        return configured_uri
    if not configured_uri:
        logging.warning("No DATABASE_URI configured. Using SQLite fallback.")
        return 'sqlite:///app_fallback.db'
    try:
        connect_args = {}
        if 'postgresql' in configured_uri:
            connect_args = {'connect_timeout': 5}
        engine = create_engine(configured_uri, connect_args=connect_args)
        with engine.connect() as conn:
            conn.execute(text('SELECT 1'))
        logging.info(f"Successfully connected to primary database: {configured_uri}")
        return configured_uri
    except Exception as e:
        logging.warning(f"Failed to connect to primary database: {e}")
        logging.warning("Falling back to SQLite.")
        return 'sqlite:///app_fallback.db'
# ============================================================

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'your-secret-key')
_uri = os.getenv('SQLALCHEMY_DATABASE_URI', 'sqlite:///shipments.db')
app.config['SQLALCHEMY_DATABASE_URI'] = get_working_database_uri(_uri)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['RATELIMIT_STORAGE_URI'] = os.getenv('RATELIMIT_STORAGE_URI', 'memory://')
app.config['RATELIMIT_DEFAULTS'] = os.getenv('RATELIMIT_DEFAULTS', '200 per day,50 per hour').split(',')
app.config['WEBSOCKET_SERVER'] = os.getenv('WEBSOCKET_SERVER', 'https://signment-9a96.onrender.com')
app.config['GEOCODING_API_KEY'] = os.getenv('GEOCODING_API_KEY', '')
app.config['SMTP_HOST'] = os.getenv('SMTP_HOST', 'smtp.gmail.com')
app.config['SMTP_PORT'] = int(os.getenv('SMTP_PORT', 587))
app.config['SMTP_USER'] = os.getenv('SMTP_USER', '')
app.config['SMTP_PASS'] = os.getenv('SMTP_PASS', '')
app.config['SMTP_FROM'] = os.getenv('SMTP_FROM', 'no-reply@example.com')
app.config['RECAPTCHA_SITE_KEY'] = os.getenv('RECAPTCHA_SITE_KEY', 'your-site-key')
app.config['RECAPTCHA_SECRET_KEY'] = os.getenv('RECAPTCHA_SECRET_KEY', 'your-secret-key')
app.config['RECAPTCHA_VERIFY_URL'] = os.getenv('RECAPTCHA_VERIFY_URL', 'https://www.google.com/recaptcha/api/siteverify')
app.config['TAWK_PROPERTY_ID'] = os.getenv('TAWK_PROPERTY_ID', 'your-tawk-property-id')
app.config['TAWK_WIDGET_ID'] = os.getenv('TAWK_WIDGET_ID', 'your-tawk-widget-id')
app.config['ADMIN_PASSWORD'] = os.getenv('ADMIN_PASSWORD', 'admin')

db_uri = app.config.get('SQLALCHEMY_DATABASE_URI', '')
if 'sqlite' in db_uri:
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
        'pool_pre_ping': True,
        'connect_args': {'check_same_thread': False}
    }
else:
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
        'pool_size': int(os.getenv('SQLALCHEMY_POOL_SIZE', '20')),
        'max_overflow': int(os.getenv('SQLALCHEMY_MAX_OVERFLOW', '40')),
        'pool_timeout': int(os.getenv('SQLALCHEMY_POOL_TIMEOUT', '60')),
        'pool_pre_ping': True,
    }

db = SQLAlchemy(app)

class Shipment(db.Model):
    __tablename__ = 'shipments'
    id = db.Column(db.Integer, primary_key=True)
    tracking_number = db.Column(db.String(50), unique=True, nullable=False)
    status = db.Column(db.String(50), nullable=False)
    checkpoints = db.Column(db.Text)
    delivery_location = db.Column(db.String(100), nullable=False)
    last_updated = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    recipient_email = db.Column(db.String(120), nullable=True)
    origin_location = db.Column(db.String(100), nullable=True)
    origin_lat = db.Column(db.Float, nullable=True)
    origin_lon = db.Column(db.Float, nullable=True)
    delivery_lat = db.Column(db.Float, nullable=True)
    delivery_lon = db.Column(db.Float, nullable=True)
    webhook_url = db.Column(db.Text, nullable=True)
    email_notifications = db.Column(db.Boolean, default=True)
    carrier = db.Column(db.String(20), default="DHL")
    photo_url = db.Column(db.String(255), nullable=True)

    sender_name = db.Column(db.String(100), nullable=True)
    sender_location = db.Column(db.String(200), nullable=True)
    receiver_name = db.Column(db.String(100), nullable=True)
    receiver_address = db.Column(db.String(200), nullable=True)
    receiver_phone = db.Column(db.String(30), nullable=True)
    receiver_email = db.Column(db.String(120), nullable=True)
    weight_kg = db.Column(db.Float, nullable=True)
    shipment_date = db.Column(db.DateTime, nullable=True)

    invoice_amount = db.Column(db.Float, nullable=True)
    payment_method = db.Column(db.String(50), nullable=True)
    payment_status = db.Column(db.String(20), default='unpaid')
    payment_reason = db.Column(db.Text, nullable=True)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': self.id,
            'tracking_number': self.tracking_number,
            'status': self.status,
            'delivery_location': self.delivery_location,
            'last_updated': self.last_updated.isoformat(),
            'created_at': self.created_at.isoformat(),
            'recipient_email': self.recipient_email,
            'origin_location': self.origin_location,
            'origin_lat': self.origin_lat,
            'origin_lon': self.origin_lon,
            'delivery_lat': self.delivery_lat,
            'delivery_lon': self.delivery_lon,
            'webhook_url': self.webhook_url,
            'email_notifications': self.email_notifications,
            'carrier': self.carrier,
            'checkpoints': (self.checkpoints or "").split(";") if self.checkpoints else [],
            'photo_url': self.photo_url,
            'sender_name': self.sender_name,
            'sender_location': self.sender_location,
            'receiver_name': self.receiver_name,
            'receiver_address': self.receiver_address,
            'receiver_phone': self.receiver_phone,
            'receiver_email': self.receiver_email,
            'weight_kg': self.weight_kg,
            'shipment_date': self.shipment_date.isoformat() if self.shipment_date else None,
            'invoice_amount': self.invoice_amount,
            'payment_method': self.payment_method,
            'payment_status': self.payment_status,
            'payment_reason': self.payment_reason
        }

def safe_redis_operation(func, *args, **kwargs):
    client = get_redis_client()
    if not client:
        return None
    try:
        return func(*args, **kwargs)
    except Exception as e:
        msg = str(e).lower()
        bot_logger.error(f"Redis error: {e}")
        global redis_disable_count, last_redis_disable
        if "max number of clients" in msg or "too many connections" in msg or "max clients" in msg:
            try:
                redis_client.set_client(None)
                redis_disable_count += 1
                last_redis_disable = datetime.utcnow()
                bot_logger.warning("Redis client disabled due to server-side max-connections error")
            except Exception:
                pass
        return None

class DummyBot:
    def message_handler(self, *args, **kwargs):
        def decorator(func):
            return func
        return decorator

    def callback_query_handler(self, *args, **kwargs):
        def decorator(func):
            return func
        return decorator

    def reply_to(self, message, text, **kwargs):
        bot_logger.info(f"DummyBot reply_to: {text}")

    def send_message(self, chat_id, text, **kwargs):
        bot_logger.info(f"DummyBot send_message to {chat_id}: {text}")

    def edit_message_text(self, text, chat_id, message_id, **kwargs):
        bot_logger.info(f"DummyBot edit_message_text: {text}")

    def answer_callback_query(self, callback_query_id, text=None, show_alert=False, **kwargs):
        bot_logger.info(f"DummyBot answer_callback_query: {text}")

    def remove_webhook(self):
        bot_logger.info("DummyBot remove_webhook called")

    def set_webhook(self, url=None):
        bot_logger.info(f"DummyBot set_webhook called with url={url}")

    def get_webhook_info(self):
        class Info:
            url = None
        return Info()

_bot_instance = None

def get_bot() -> TeleBot:
    global _bot_instance
    if _bot_instance is None:
        token = config.telegram_bot_token
        if not token or ':' not in token:
            bot_logger.warning("Invalid or missing Telegram token; using DummyBot")
            _bot_instance = DummyBot()
        else:
            _bot_instance = TeleBot(token)
    return _bot_instance

def is_admin(user_id: int) -> bool:
    return user_id in config.allowed_admins

def sanitize_tracking_number(tn: str) -> Optional[str]:
    if not tn:
        return None
    tn = re.sub(r'\W+', '', tn.upper())
    return tn if re.match(DHL_CONFIG['tracking_format'], tn) else None

def generate_unique_id() -> str:
    import secrets
    return f"JD{secrets.randbelow(10**10):010d}"

def validate_email(email: str) -> bool:
    return bool(email and re.match(r'^[\w\.-]+@[\w\.-]+\.\w+$', email))

def validate_location(location: str) -> bool:
    return bool(location and isinstance(location, str) and len(location) <= 100)

def validate_webhook_url(url: str) -> bool:
    return bool(url and re.match(r'^https?://[^\s/$.?#].[^\s]*$', url))

def should_send_email(tn: str, status: str, checkpoints):
    if not AUTO_EMAIL_ENABLED:
        return False
    shipment = Shipment.query.filter_by(tracking_number=tn).first()
    if not shipment or not shipment.recipient_email or not shipment.email_notifications:
        return False
    important_statuses = {"Pending", "In_Transit", "Out_for_Delivery", "Delivered", "Exception", "Delayed", "On_Hold"}
    final_statuses = {"Delivered", "Exception"}
    if status not in important_statuses:
        return False
    if isinstance(checkpoints, str):
        checkpoints_list = [c for c in checkpoints.split(";") if c]
    else:
        checkpoints_list = [c for c in checkpoints if c]
    if not checkpoints_list:
        return False
    latest_checkpoint = checkpoints_list[-1].strip()
    digest = f"{status}:{latest_checkpoint}"

    client = get_redis_client()
    last_email_key = f"last_email:{tn}"
    digest_key = f"last_email_digest:{tn}"
    now = datetime.now()

    if client:
        try:
            current_digest = client.get(digest_key)
            if current_digest:
                current_digest = current_digest.decode() if isinstance(current_digest, bytes) else current_digest
                if current_digest == digest:
                    return False
        except Exception:
            pass
        if status not in final_statuses:
            try:
                last_sent = client.get(last_email_key)
                if last_sent:
                    last_time = datetime.fromisoformat(last_sent.decode() if isinstance(last_sent, bytes) else last_sent)
                    if now - last_time < timedelta(minutes=EMAIL_THROTTLE_MINUTES):
                        return False
            except Exception:
                pass
    else:
        current_digest_entry = email_digest_cache.get(tn)
        if current_digest_entry:
            current_digest, expiry = current_digest_entry
            if expiry and now < expiry and current_digest == digest:
                return False
        last_sent = email_throttle_cache.get(tn)
        if status not in final_statuses and last_sent and now - last_sent < timedelta(minutes=EMAIL_THROTTLE_MINUTES):
            return False

    if client:
        try:
            ttl = 86400 if status in final_statuses else EMAIL_THROTTLE_MINUTES * 60
            client.set(last_email_key, now.isoformat(), ex=ttl)
            client.set(digest_key, digest, ex=6 * 3600)
        except Exception:
            pass
    else:
        email_throttle_cache[tn] = now
        email_digest_cache[tn] = (digest, now + timedelta(hours=6))

    return True

# ============================================================
# ENHANCED estimate_distance with full city list (removed duplicate)
# ============================================================
def estimate_distance(origin: str, dest: str) -> float:
    """
    Return approximate distance in km between two cities using a pre‑defined
    coordinate dictionary. If a city is not found, return 1000.0.
    """
    city_coords = {
        "Lagos, NG": (6.5244, 3.3792), "Abuja, NG": (9.0579, 7.4951), "Port Harcourt, NG": (4.8156, 7.0498),
        "Kano, NG": (12.0001, 8.5167), "Ibadan, NG": (7.3775, 3.9470), "Enugu, NG": (6.4584, 7.5170),
        "New York, NY": (40.7128, -74.0060), "Los Angeles, CA": (34.0522, -118.2437), "London, UK": (51.5074, -0.1278),
        "Dubai, UAE": (25.2048, 55.2708), "Tokyo, JP": (35.6762, 139.6503), "Sydney, AU": (-33.8688, 151.2093),
        "Paris, FR": (48.8566, 2.3522), "Berlin, DE": (52.5200, 13.4050), "Mumbai, IN": (19.0760, 72.8777),
        "Singapore, SG": (1.3521, 103.8198), "Hong Kong, HK": (22.3193, 114.1694), "São Paulo, BR": (-23.5505, -46.6333),
        "Johannesburg, ZA": (-26.2041, 28.0473), "Cairo, EG": (30.0444, 31.2357), "Moscow, RU": (55.7558, 37.6173),
        "Toronto, CA": (43.6532, -79.3832), "Mexico City, MX": (19.4326, -99.1332), "Seoul, KR": (37.5665, 126.9780),
        "Bangkok, TH": (13.7563, 100.5018), "Jakarta, ID": (-6.2088, 106.8456), "Delhi, IN": (28.7041, 77.1025),
        "Beijing, CN": (39.9042, 116.4074), "Shanghai, CN": (31.2304, 121.4737), "Istanbul, TR": (41.0082, 28.9784),
        "Karachi, PK": (24.8607, 67.0011), "Buenos Aires, AR": (-34.6037, -58.3816), "Rio de Janeiro, BR": (-22.9068, -43.1729),
        "Lima, PE": (-12.0464, -77.0428), "Bogotá, CO": (4.7110, -74.0721), "Santiago, CL": (-33.4489, -70.6693),
        "Cape Town, ZA": (-33.9249, 18.4241), "Nairobi, KE": (-1.2921, 36.8219), "Accra, GH": (5.6037, -0.1870),
        "Addis Ababa, ET": (8.9806, 38.7578), "Kuala Lumpur, MY": (3.1390, 101.6869), "Hanoi, VN": (21.0285, 105.8342),
        "Manila, PH": (14.5995, 120.9842), "Taipei, TW": (25.0330, 121.5654), "Riyadh, SA": (24.7136, 46.6753),
        "Tel Aviv, IL": (32.0853, 34.7818), "Athens, GR": (37.9838, 23.7275), "Lisbon, PT": (38.7223, -9.1393),
        "Stockholm, SE": (59.3293, 18.0686), "Oslo, NO": (59.9139, 10.7522), "Helsinki, FI": (60.1699, 24.9384),
        "Warsaw, PL": (52.2297, 21.0122), "Prague, CZ": (50.0755, 14.4378), "Budapest, HU": (47.4979, 19.0402),
        "Vienna, AT": (48.2082, 16.3738), "Zurich, CH": (47.3769, 8.5417), "Amsterdam, NL": (52.3676, 4.9041),
        "Brussels, BE": (50.8476, 4.3572), "Dublin, IE": (53.3498, -6.2603), "Madrid, ES": (40.4168, -3.7038),
        "Rome, IT": (41.9028, 12.4964), "Milan, IT": (45.4642, 9.1900), "Barcelona, ES": (41.3851, 2.1734)
    }
    origin_lower = origin.lower()
    dest_lower = dest.lower()
    origin_key = next((k for k in city_coords if origin_lower in k.lower() or k.lower().startswith(origin_lower)), None)
    dest_key = next((k for k in city_coords if dest_lower in k.lower() or k.lower().startswith(dest_lower)), None)
    if not origin_key or not dest_key:
        return 1000.0
    lat1, lon1 = map(radians, city_coords[origin_key])
    lat2, lon2 = map(radians, city_coords[dest_key])
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
    c = 2 * atan2(sqrt(a), sqrt(1-a))
    return round(6371 * c, 1)
# ============================================================

def get_shipment_list(page: int = 1, per_page: int = 10) -> Tuple[List[str], int]:
    try:
        offset = (page - 1) * per_page
        shipments = Shipment.query.order_by(Shipment.created_at.desc()).offset(offset).limit(per_page).all()
        total = Shipment.query.count()
        return [s.tracking_number for s in shipments], total
    except Exception as e:
        bot_logger.error(f"List error: {e}")
        return [], 0

def get_shipment_details(tracking_number: str) -> Optional[Dict[str, Any]]:
    try:
        shipment = Shipment.query.filter_by(tracking_number=tracking_number).first()
        return shipment.to_dict() if shipment else None
    except Exception as e:
        bot_logger.error(f"Fetch error {tracking_number}: {e}")
        return None

# ============================================================
# Batch fetch and status aggregation for the bot
# ============================================================
def get_shipment_list_with_statuses(page: int = 1, per_page: int = 10) -> Tuple[List[Tuple[str, str]], int]:
    """Return list of (tracking_number, status) and total count."""
    try:
        offset = (page - 1) * per_page
        query = Shipment.query.with_entities(
            Shipment.tracking_number, Shipment.status
        ).order_by(Shipment.created_at.desc())
        total = query.count()
        rows = query.offset(offset).limit(per_page).all()
        return [(tn, status) for tn, status in rows], total
    except Exception as e:
        bot_logger.error(f"List with statuses error: {e}")
        return [], 0

def get_status_counts() -> Dict[str, int]:
    """Return dict of status -> count."""
    try:
        rows = Shipment.query.with_entities(
            Shipment.status, db.func.count()
        ).group_by(Shipment.status).all()
        return {status: count for status, count in rows}
    except Exception as e:
        bot_logger.error(f"Status counts error: {e}")
        return {}
# ============================================================

def save_shipment(tracking_number: str, status: str, checkpoints: str = '', delivery_location: Optional[str] = None,
                  recipient_email: Optional[str] = None, origin_location: Optional[str] = None,
                  webhook_url: Optional[str] = None, carrier: str = "DHL",
                  sender_name: Optional[str] = None, sender_location: Optional[str] = None,
                  receiver_name: Optional[str] = None, receiver_address: Optional[str] = None,
                  receiver_phone: Optional[str] = None, receiver_email: Optional[str] = None,
                  weight_kg: Optional[float] = None, shipment_date: Optional[datetime] = None,
                  invoice_amount: Optional[float] = None, payment_method: Optional[str] = None,
                  payment_status: str = 'unpaid', payment_reason: Optional[str] = None) -> bool:
    try:
        shipment = Shipment(
            tracking_number=tracking_number,
            status=status,
            checkpoints=checkpoints,
            delivery_location=delivery_location or "Lagos, NG",
            recipient_email=recipient_email,
            origin_location=origin_location or "Lagos, NG",
            webhook_url=webhook_url,
            email_notifications=True,
            carrier=carrier,
            sender_name=sender_name,
            sender_location=sender_location,
            receiver_name=receiver_name,
            receiver_address=receiver_address,
            receiver_phone=receiver_phone,
            receiver_email=receiver_email,
            weight_kg=weight_kg,
            shipment_date=shipment_date,
            invoice_amount=invoice_amount,
            payment_method=payment_method,
            payment_status=payment_status,
            payment_reason=payment_reason
        )
        db.session.add(shipment)
        db.session.commit()
        try:
            db.session.remove()
        except Exception:
            pass
        invalidate_cache(tracking_number)
        bot_logger.info(f"Saved {tracking_number}")
        return True
    except Exception as e:
        try:
            db.session.rollback()
        except Exception:
            pass
        try:
            db.session.remove()
        except Exception:
            pass
        bot_logger.error(f"Save failed: {e}")
        return False

def update_shipment(tracking_number: str, status: Optional[str] = None, delivery_location: Optional[str] = None,
                    recipient_email: Optional[str] = None, origin_location: Optional[str] = None,
                    webhook_url: Optional[str] = None, carrier: Optional[str] = None,
                    sender_name: Optional[str] = None, sender_location: Optional[str] = None,
                    receiver_name: Optional[str] = None, receiver_address: Optional[str] = None,
                    receiver_phone: Optional[str] = None, receiver_email: Optional[str] = None,
                    weight_kg: Optional[float] = None, shipment_date: Optional[datetime] = None,
                    invoice_amount: Optional[float] = None, payment_method: Optional[str] = None,
                    payment_status: Optional[str] = None, payment_reason: Optional[str] = None) -> bool:
    try:
        shipment = Shipment.query.filter_by(tracking_number=tracking_number).first()
        if not shipment:
            return False
        if status and status in config.valid_statuses:
            shipment.status = status
        if delivery_location:
            shipment.delivery_location = delivery_location
        if recipient_email is not None:
            shipment.recipient_email = recipient_email
        if origin_location is not None:
            shipment.origin_location = origin_location
        if webhook_url is not None:
            shipment.webhook_url = webhook_url
        if carrier:
            shipment.carrier = carrier
        if sender_name is not None:
            shipment.sender_name = sender_name
        if sender_location is not None:
            shipment.sender_location = sender_location
        if receiver_name is not None:
            shipment.receiver_name = receiver_name
        if receiver_address is not None:
            shipment.receiver_address = receiver_address
        if receiver_phone is not None:
            shipment.receiver_phone = receiver_phone
        if receiver_email is not None:
            shipment.receiver_email = receiver_email
        if weight_kg is not None:
            shipment.weight_kg = weight_kg
        if shipment_date is not None:
            shipment.shipment_date = shipment_date
        if invoice_amount is not None:
            shipment.invoice_amount = invoice_amount
        if payment_method is not None:
            shipment.payment_method = payment_method
        if payment_status is not None:
            shipment.payment_status = payment_status
        if payment_reason is not None:
            shipment.payment_reason = payment_reason
        shipment.last_updated = datetime.utcnow()
        db.session.commit()
        try:
            db.session.remove()
        except Exception:
            pass
        invalidate_cache(tracking_number)
        bot_logger.info(f"Updated {tracking_number}")
        return True
    except Exception as e:
        try:
            db.session.rollback()
        except Exception:
            pass
        try:
            db.session.remove()
        except Exception:
            pass
        bot_logger.error(f"Update failed: {e}")
        return False

def search_shipments(query: str, page: int = 1, per_page: int = 10) -> Tuple[List[str], int]:
    try:
        query = f"%{query}%"
        offset = (page - 1) * per_page
        shipments = Shipment.query.filter(
            db.or_(
                Shipment.tracking_number.ilike(query),
                Shipment.delivery_location.ilike(query),
                Shipment.origin_location.ilike(query),
                Shipment.recipient_email.ilike(query)
            )
        ).order_by(Shipment.created_at.desc()).offset(offset).limit(per_page).all()
        total = Shipment.query.filter(
            db.or_(
                Shipment.tracking_number.ilike(query),
                Shipment.delivery_location.ilike(query),
                Shipment.origin_location.ilike(query),
                Shipment.recipient_email.ilike(query)
            )
        ).count()
        return [s.tracking_number for s in shipments], total
    except Exception as e:
        bot_logger.error(f"Search error: {e}")
        return [], 0

def invalidate_cache(tracking_number: str):
    if redis_client:
        try:
            safe_redis_operation(redis_client.delete, f"shipment:{tracking_number}")
        except:
            pass

def enqueue_notification(data: Dict[str, Any]) -> bool:
    if not redis_client:
        bot_logger.warning(f"Redis client unavailable – notification for {data.get('tracking_number', 'unknown')} dropped")
        return False
    try:
        redis_client.rpush("notifications", json.dumps(data))
        bot_logger.info(f"Notification enqueued: {data.get('type')} for {data.get('tracking_number', 'unknown')}")
        return True
    except Exception as e:
        bot_logger.error(f"Queue failed: {e}")
        return False

def get_cached_route_templates() -> Dict[str, List[str]]:
    return {
        "Lagos, NG": ["Lagos, NG"],
        "Abuja, NG": ["Abuja, NG"],
        "Port Harcourt, NG": ["Port Harcourt, NG"]
    }

def cache_route_templates() -> bool:
    return True

RATE_LIMIT_WINDOW = 60
RATE_LIMIT_MAX = 20

def rate_limit(func):
    @wraps(func)
    def wrapper(message):
        user_id = str(message.from_user.id)
        key = f"rate_limit:{user_id}"
        count = safe_redis_operation(redis_client.incr, key) if redis_client else 0
        if count is None:
            count = 0
        if count == 1:
            safe_redis_operation(redis_client.expire, key, RATE_LIMIT_WINDOW)
        if count > RATE_LIMIT_MAX:
            get_bot().reply_to(message, "Rate limit exceeded. Try again later.")
            return
        return func(message)
    return wrapper

def send_dynamic_menu(chat_id: int, message_id: Optional[int] = None, page: int = 1):
    shipments, total = get_shipment_list(page=page)
    markup = InlineKeyboardMarkup(row_width=2)
    for tn in shipments:
        s = get_shipment_details(tn)
        label = f"{tn} [DHL]" if s.get('carrier') == 'DHL' else f"{tn} [{s['status']}]"
        markup.add(InlineKeyboardButton(label, callback_data=f"view_{tn}"))
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("Prev", callback_data=f"menu_page_{page-1}"))
    if page * 10 < total:
        nav.append(InlineKeyboardButton("Next", callback_data=f"menu_page_{page+1}"))
    if nav:
        markup.add(*nav)
    markup.add(
        InlineKeyboardButton("Generate ID", callback_data="generate_id"),
        InlineKeyboardButton("Add Shipment", callback_data="add"),
        InlineKeyboardButton("Search", callback_data="search_menu"),
        InlineKeyboardButton("Bulk Actions", callback_data="bulk_action"),
        InlineKeyboardButton("Stats", callback_data="stats"),
        InlineKeyboardButton("Help", callback_data="help")
    )
    text = f"*Admin Panel* (Page {page})\nTotal: `{total}` shipments"
    bot = get_bot()
    if message_id:
        bot.edit_message_text(text, chat_id, message_id, parse_mode='Markdown', reply_markup=markup)
    else:
        bot.send_message(chat_id, text, parse_mode='Markdown', reply_markup=markup)

def export_shipments() -> Optional[str]:
    try:
        shipments = Shipment.query.all()
        return json.dumps([s.to_dict() for s in shipments], indent=2, default=str)
    except Exception as e:
        bot_logger.error(f"Export error: {e}")
        return None

def get_recent_logs(limit: int = 5) -> List[str]:
    return [f"{datetime.utcnow().isoformat()} - INFO - Sample log {i}" for i in range(1, limit + 1)]

def show_shipment_menu(call, page: int, prefix: str, prompt: str, extra_buttons=None):
    shipments, total = get_shipment_list(page=page)
    if not shipments:
        get_bot().edit_message_text("No shipments.", call.message.chat.id, call.message.message_id)
        return
    markup = InlineKeyboardMarkup(row_width=1)
    for tn in shipments:
        s = get_shipment_details(tn)
        label = f"{tn} [DHL]" if s.get('carrier') == 'DHL' else tn
        markup.add(InlineKeyboardButton(label, callback_data=f"{prefix}_{tn}_{page}"))
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("Prev", callback_data=f"{prefix}_menu_{page-1}"))
    if page * 10 < total:
        nav.append(InlineKeyboardButton("Next", callback_data=f"{prefix}_menu_{page+1}"))
    if nav:
        markup.add(*nav)
    if extra_buttons:
        markup.add(*extra_buttons)
    get_bot().edit_message_text(f"*{prompt}* (Page {page}):", call.message.chat.id, call.message.message_id,
                               parse_mode='Markdown', reply_markup=markup)

def set_webhook():
    try:
        bot = get_bot()
        bot.remove_webhook()
        time.sleep(1)
        bot.set_webhook(url=config.webhook_url)
        bot_logger.info(f"Webhook set: {config.webhook_url}")
    except Exception as e:
        bot_logger.error(f"Webhook failed: {e}")

def keep_alive():
    bot_logger.info("Keep-alive loop started")
    console.print("[info]Keep-alive loop started[/info]")
    while True:
        try:
            bot = get_bot()
            info = bot.get_webhook_info()
            if info.url != config.webhook_url:
                bot_logger.warning("Webhook mismatch, resetting...")
                set_webhook()
            time.sleep(300)
        except Exception as e:
            bot_logger.error(f"Keep-alive error: {e}")
            time.sleep(60)

# ============================================================
# EMAIL MICROSERVICE SUPPORT (Resend removed)
# ============================================================

def send_email_via_service(recipient, subject, html_body=None, plain_body=None):
    """Send email via external microservice if configured."""
    service_url = os.getenv('EMAIL_SERVICE_URL')
    if not service_url:
        return None  # not configured – caller should fallback to SMTP
    try:
        payload = {
            'recipient': recipient,
            'subject': subject,
            'html_body': html_body,
            'plain_body': plain_body
        }
        # Timeout increased to 180 seconds to accommodate slow SMTP
        resp = requests.post(service_url, json=payload, timeout=180)
        if resp.status_code == 200:
            data = resp.json()
            return data.get('success', False)
        else:
            bot_logger.warning(f"Email service returned {resp.status_code}: {resp.text}")
            return False
    except Exception as e:
        bot_logger.error(f"Email service call failed: {e}")
        return False

def _store_email_history(tracking_number, email_type, recipient, subject, message):
    if not tracking_number:
        return
    try:
        history_key = f"email_history:{tracking_number}"
        entry = {
            'timestamp': datetime.utcnow().isoformat(),
            'type': email_type or 'status_update',
            'recipient': recipient,
            'subject': subject,
            'message': message
        }
        if redis_client:
            redis_client.lpush(history_key, json.dumps(entry))
            redis_client.ltrim(history_key, 0, 99)
    except Exception as e:
        bot_logger.warning(f'Failed to store email history for {tracking_number}: {e}')

def send_email_via_smtp(recipient, subject, html_body=None, plain_body=None):
    smtp_host = app.config.get('SMTP_HOST')
    smtp_user = app.config.get('SMTP_USER')
    smtp_pass = app.config.get('SMTP_PASS')
    if not all([smtp_host, smtp_user, smtp_pass]):
        bot_logger.warning("SMTP not fully configured, skipping")
        return False
    if not _is_smtp_host_resolvable(smtp_host):
        bot_logger.error(f"SMTP host '{smtp_host}' cannot be resolved - check your SMTP_HOST setting")
        return False

    # Import smtplib and email locally to avoid circular imports
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    msg = MIMEMultipart("alternative")
    msg['From'] = app.config['SMTP_FROM']
    msg['To'] = recipient
    msg['Subject'] = subject
    if plain_body:
        msg.attach(MIMEText(plain_body, "plain"))
    if html_body:
        msg.attach(MIMEText(html_body, "html"))
    try:
        port = int(app.config['SMTP_PORT'])
        # Timeout increased to 180 seconds
        if port == 465:
            server = smtplib.SMTP_SSL(smtp_host, port, timeout=180)
        else:
            server = smtplib.SMTP(smtp_host, port, timeout=180)
            server.starttls()
        server.login(smtp_user, smtp_pass)
        server.send_message(msg)
        server.quit()
        return True
    except Exception as e:
        bot_logger.error(f"SMTP send failed: {e}")
        return False

def _is_smtp_host_resolvable(hostname):
    if not hostname:
        return False
    try:
        socket.getaddrinfo(hostname, None)
        return True
    except socket.gaierror:
        return False

def send_email_notification(recipient, subject, html_body=None, plain_body=None,
                            tracking_number=None, email_type=None, message=None):
    if EMAIL_TEST_MODE:
        bot_logger.info(f"📧 TEST MODE - Email would be sent to: {recipient}")
        bot_logger.info(f"   Subject: {subject}")
        bot_logger.info(f"   Tracking: {tracking_number}")
        return True

    if not EMAIL_ENABLED:
        bot_logger.info(f"📧 Email disabled - would send to {recipient}: {subject}")
        return True

    # ---------- TRY EXTERNAL MICROSERVICE FIRST ----------
    service_result = send_email_via_service(recipient, subject, html_body, plain_body)
    if service_result is True:
        bot_logger.info(f"✅ Email sent via external service to {recipient}")
        _store_email_history(tracking_number, email_type, recipient, subject, message)
        return True
    elif service_result is False:
        bot_logger.warning(f"⚠️ External service failed for {recipient}, falling back to local SMTP")
    # if service_result is None (service not configured), we also fall through

    # ---------- FALLBACK: LOCAL SMTP (Resend removed) ----------
    smtp_configured = all([app.config.get('SMTP_HOST'),
                           app.config.get('SMTP_USER'),
                           app.config.get('SMTP_PASS')])
    if smtp_configured and not _is_smtp_host_resolvable(app.config.get('SMTP_HOST')):
        bot_logger.error(f"SMTP host '{app.config.get('SMTP_HOST')}' not resolvable – disabling SMTP")
        smtp_configured = False

    if smtp_configured:
        try:
            success = send_email_via_smtp(recipient, subject, html_body, plain_body)
            if success:
                bot_logger.info(f"✅ Email sent via local SMTP to {recipient}")
                _store_email_history(tracking_number, email_type, recipient, subject, message)
                return True
        except Exception as e:
            bot_logger.error(f"❌ Local SMTP failed: {e}")

    # ---------- ALL FAILED ----------
    bot_logger.error(f"❌ Failed to send email to {recipient} via any provider")
    # Optionally enqueue for later (if you have a worker)
    enqueue_notification({
        "tracking_number": tracking_number,
        "type": "email",
        "data": {
            "recipient_email": recipient,
            "subject": subject,
            "html_body": html_body,
            "plain_body": plain_body
        }
    })
    return False

# ============================================================

if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    set_webhook()
    console.print("[green]utils.py ready — Upstash Redis + BotConfig exported[/green]")
    keep_alive()
