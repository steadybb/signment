import os
import json
import time
import logging

# patch eventlet before importing Flask/Werkzeug or networking modules
try:
    import eventlet
    eventlet.monkey_patch()
except Exception:
    eventlet = None

from dotenv import load_dotenv
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from flask import Flask, render_template
import requests
from rich.console import Console
from rich.panel import Panel
from utils import BotConfig, safe_redis_operation, get_redis_client

load_dotenv()

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('worker')
console = Console()

# Use shared Redis client from utils.py
redis_client = get_redis_client()
if redis_client is None:
    logger.warning("Redis client unavailable in worker process")
    console.print(Panel("[warning]Redis client unavailable in worker process[/warning]", title="Redis Warning", border_style="yellow"))
else:
    logger.info("Worker Redis client initialized")
    console.print("[info]Worker Redis client initialized[/info]")

# Load configuration
try:
    config = BotConfig(
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", os.getenv("TELEGRAM_TOKEN", "")),
        redis_url=os.getenv("REDIS_URL"),
        redis_token=os.getenv("REDIS_TOKEN", ""),
        webhook_url=os.getenv("WEBHOOK_URL", "https://signment-9a96.onrender.com/telegram/webhook"),
        websocket_server=os.getenv("WEBSOCKET_SERVER", "https://signment-9a96.onrender.com"),
        allowed_admins=[int(uid) for uid in os.getenv("ALLOWED_ADMINS", "").split(",") if uid],
        valid_statuses=os.getenv("VALID_STATUSES", "Pending,On_Hold,In_Transit,Out_for_Delivery,Delivered,Returned,Delayed").split(","),
        route_templates=json.loads(os.getenv("ROUTE_TEMPLATES", '{"Lagos, NG": ["Lagos, NG"]}')),
        smtp_host=os.getenv("SMTP_HOST", "smtp.gmail.com"),
        smtp_port=int(os.getenv("SMTP_PORT", 587)),
        smtp_user=os.getenv("SMTP_USER", ""),
        smtp_pass=os.getenv("SMTP_PASS", ""),
        smtp_from=os.getenv("SMTP_FROM", "no-reply@example.com"),
        email_provider=os.getenv("EMAIL_PROVIDER", "resend" if os.getenv("RESEND_API_KEY", "").strip() else "smtp"),
        resend_api_key=os.getenv("RESEND_API_KEY", "")
    )
except Exception as e:
    logger.error(f"Configuration validation failed: {e}")
    console.print(Panel(f"[error]Configuration validation failed: {e}[/error]", title="Config Error", border_style="red"))
    raise

# Initialize Flask app for template rendering
app = Flask(__name__)
app.config['TEMPLATES_AUTO_RELOAD'] = True

def send_email(tracking_number: str, status: str, checkpoints: str, delivery_location: str, recipient_email: str, subject: str = None, html_body: str = None, plain_body: str = None) -> bool:
    """Send an email notification. Uses provided HTML/plain bodies if present, otherwise falls back to rendering template and building plain text."""
    try:
        if not recipient_email:
            logger.warning(f"No recipient email for {tracking_number}")
            return False

        # Prepare checkpoints as a list for template/fallback
        checkpoints_list = checkpoints.split(';') if checkpoints else []

        # Determine subject
        msg_subject = subject or f"DHL Shipment Update: {tracking_number}"

        # If no html_body provided, render legacy template (keeps backward compatibility)
        if not html_body:
            with app.app_context():
                try:
                    html_body = render_template('email_notification.html',
                                                tracking_number=tracking_number,
                                                status=status,
                                                checkpoints=checkpoints_list,
                                                delivery_location=delivery_location)
                except Exception:
                    # If template missing, fall back to simple HTML
                    html_body = f"<html><body><h2>DHL Shipment Update</h2><p>Tracking: {tracking_number}</p><p>Status: {status}</p></body></html>"

        # If no plain_body provided, build a simple fallback
        if not plain_body:
            location = delivery_location or 'Unknown'
            plain_body = f"DHL Shipment Update\n\nTracking Number: {tracking_number}\nStatus: {status}\nDestination: {location}\n\nRecent Updates:\n{chr(10).join(['- ' + c for c in checkpoints_list[-3:]]) if checkpoints_list else 'No updates yet'}\n\nTrack online: {config.websocket_server}/track/{tracking_number}"

        # Create MIME message
        msg = MIMEMultipart('alternative')
        msg['Subject'] = msg_subject
        msg['From'] = config.smtp_from
        msg['To'] = recipient_email

        # Attach plain and HTML parts
        msg.attach(MIMEText(plain_body, 'plain'))
        msg.attach(MIMEText(html_body, 'html'))

        # Send email with retry
        max_retries = 3
        for attempt in range(max_retries):
            try:
                email_provider = 'resend' if config.resend_api_key else config.email_provider
                if email_provider == 'resend':
                    response = requests.post(
                        'https://api.resend.com/emails',
                        headers={'Authorization': f'Bearer {config.resend_api_key}', 'Content-Type': 'application/json'},
                        json={
                            'from': config.smtp_from,
                            'to': [recipient_email],
                            'subject': msg_subject,
                            'html': html_body or f'<p>{plain_body or ""}</p>',
                            'text': plain_body or ''
                        },
                        timeout=20
                    )
                    response.raise_for_status()
                else:
                    smtp_class = smtplib.SMTP_SSL if int(config.smtp_port) == 465 else smtplib.SMTP
                    with smtp_class(config.smtp_host, config.smtp_port, timeout=20) as server:
                        if int(config.smtp_port) != 465:
                            server.starttls()
                        server.login(config.smtp_user, config.smtp_pass)
                        server.send_message(msg)
                logger.info(f"Sent HTML email notification for {tracking_number} to {recipient_email}")
                console.print(f"[info]Sent HTML email notification for {tracking_number} to {recipient_email}[/info]")
                return True
            except smtplib.SMTPServerDisconnected:
                logger.warning(f"SMTP connection lost, retrying... ({attempt + 1}/{max_retries})")
                time.sleep(2 ** attempt)
            except Exception as e:
                if attempt == max_retries - 1:
                    raise
                time.sleep(2 ** attempt)

        return False
    except smtplib.SMTPException as e:
        logger.error(f"Failed to send HTML email notification for {tracking_number}: {e}")
        console.print(Panel(f"[error]Failed to send HTML email notification for {tracking_number}: {e}[/error]", title="Email Error", border_style="red"))
        return False
    except Exception as e:
        logger.error(f"Unexpected error sending HTML email for {tracking_number}: {e}")
        console.print(Panel(f"[error]Unexpected error sending HTML email for {tracking_number}: {e}[/error]", title="Email Error", border_style="red"))
        return False

def send_webhook(tracking_number: str, status: str, checkpoints: list, delivery_location: str, webhook_url: str) -> bool:
    """Send a webhook notification."""
    try:
        if not webhook_url:
            logger.warning(f"No webhook URL for {tracking_number}")
            return False

        payload = {
            "tracking_number": tracking_number,
            "status": status,
            "checkpoints": checkpoints if isinstance(checkpoints, list) else [],
            "delivery_location": delivery_location,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
        }
        
        # Send with timeout and retry
        max_retries = 3
        for attempt in range(max_retries):
            try:
                response = requests.post(webhook_url, json=payload, timeout=5)
                response.raise_for_status()
                logger.info(f"Sent webhook notification for {tracking_number} to {webhook_url}")
                console.print(f"[info]Sent webhook notification for {tracking_number} to {webhook_url}[/info]")
                return True
            except requests.RequestException as e:
                if attempt == max_retries - 1:
                    raise
                logger.warning(f"Webhook attempt {attempt + 1} failed: {e}")
                time.sleep(2 ** attempt)
        
        return False
    except requests.RequestException as e:
        logger.error(f"Failed to send webhook notification for {tracking_number}: {e}")
        console.print(Panel(f"[error]Failed to send webhook notification for {tracking_number}: {e}[/error]", title="Webhook Error", border_style="red"))
        return False

def process_notifications():
    """Process notifications from the Redis queue."""
    consecutive_errors = 0
    max_consecutive_errors = 10
    
    while True:
        redis_client = get_redis_client()
        if not redis_client:
            consecutive_errors += 1
            logger.warning(f"Redis client unavailable, retrying in 5 seconds (error {consecutive_errors})")
            console.print(Panel(f"[warning]Redis client unavailable, retrying... ({consecutive_errors})[/warning]", title="Worker Warning", border_style="yellow"))
            if consecutive_errors >= max_consecutive_errors:
                logger.error("Max consecutive Redis errors reached, waiting longer...")
                time.sleep(30)
            else:
                time.sleep(5)
            continue
        
        # Reset error counter on successful connection
        consecutive_errors = 0

        try:
            # Use non-blocking lpop instead of blpop
            notification_data = safe_redis_operation(redis_client.lpop, "notifications")
            if not notification_data:
                time.sleep(1)  # Avoid tight loop
                continue

            notification = json.loads(notification_data)
            tracking_number = notification.get('tracking_number')
            notification_type = notification.get('type')
            data = notification.get('data', {})

            logger.info(f"Processing {notification_type} notification for {tracking_number}")
            console.print(f"[info]Processing {notification_type} notification for {tracking_number}[/info]")

            if notification_type == "email":
                success = send_email(
                    tracking_number=tracking_number,
                    status=data.get('status', 'Unknown'),
                    checkpoints=data.get('checkpoints', ''),
                    delivery_location=data.get('delivery_location', 'Unknown'),
                    recipient_email=data.get('recipient_email', ''),
                    subject=data.get('subject'),
                    html_body=data.get('html_body'),
                    plain_body=data.get('plain_body')
                )
                if not success:
                    logger.warning(f"Requeueing failed email notification for {tracking_number}")
                    safe_redis_operation(redis_client.lpush, "notifications", notification_data)

            elif notification_type == "webhook":
                checkpoints = data.get('checkpoints', [])
                if isinstance(checkpoints, str):
                    checkpoints = checkpoints.split(';') if checkpoints else []
                success = send_webhook(
                    tracking_number=tracking_number,
                    status=data.get('status', 'Unknown'),
                    checkpoints=checkpoints,
                    delivery_location=data.get('delivery_location', 'Unknown'),
                    webhook_url=data.get('webhook_url', config.websocket_server)
                )
                if not success:
                    logger.warning(f"Requeueing failed webhook notification for {tracking_number}")
                    safe_redis_operation(redis_client.lpush, "notifications", notification_data)

        except json.JSONDecodeError as e:
            logger.error(f"Invalid notification format: {e}")
            console.print(Panel(f"[error]Invalid notification format: {e}[/error]", title="Worker Error", border_style="red"))
        except Exception as e:
            logger.error(f"Unexpected error processing notification: {e}")
            console.print(Panel(f"[error]Unexpected error processing notification: {e}[/error]", title="Worker Error", border_style="red"))
            time.sleep(5)  # Prevent tight loop on persistent errors

def start_worker():
    """Start the worker process."""
    logger.info("Starting notification worker")
    console.print("[info]Starting notification worker[/info]")
    
    try:
        process_notifications()
    except KeyboardInterrupt:
        logger.info("Shutting down notification worker")
        console.print("[info]Shutting down notification worker[/info]")
    except Exception as e:
        logger.critical(f"Worker crashed: {e}")
        console.print(Panel(f"[critical]Worker crashed: {e}[/critical]", title="Worker Error", border_style="red"))
        raise

if __name__ == "__main__":
    start_worker()
