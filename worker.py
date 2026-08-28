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
import socket
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

# Maximum number of retries for a failed notification
MAX_RETRIES = 3

# ============================================================
# FIXED: send_email with Resend → SMTP fallback
# ============================================================
def send_email(tracking_number: str, status: str, checkpoints: str, delivery_location: str,
               recipient_email: str, subject: str = None, html_body: str = None,
               plain_body: str = None) -> tuple[bool, bool]:
    """
    Send an email notification.
    Returns (success, permanent_failure)
      - success: True if email was sent successfully
      - permanent_failure: True if the error is permanent and should not be retried
    """
    if not recipient_email:
        logger.warning(f"No recipient email for {tracking_number}")
        return False, True  # Missing email is permanent

    # Prepare checkpoints as a list for template/fallback
    checkpoints_list = checkpoints.split(';') if checkpoints else []

    # Determine subject
    msg_subject = subject or f"DHL Shipment Update: {tracking_number}"

    # Build HTML and plain bodies if not provided
    if not html_body:
        with app.app_context():
            try:
                html_body = render_template('email_notification.html',
                                            tracking_number=tracking_number,
                                            status=status,
                                            checkpoints=checkpoints_list,
                                            delivery_location=delivery_location)
            except Exception:
                html_body = f"<html><body><h2>DHL Shipment Update</h2><p>Tracking: {tracking_number}</p><p>Status: {status}</p></body></html>"

    if not plain_body:
        location = delivery_location or 'Unknown'
        plain_body = f"DHL Shipment Update\n\nTracking Number: {tracking_number}\nStatus: {status}\nDestination: {location}\n\nRecent Updates:\n{chr(10).join(['- ' + c for c in checkpoints_list[-3:]]) if checkpoints_list else 'No updates yet'}\n\nTrack online: {config.websocket_server}/track/{tracking_number}"

    # Create MIME message (for SMTP)
    msg = MIMEMultipart('alternative')
    msg['Subject'] = msg_subject
    msg['From'] = config.smtp_from
    msg['To'] = recipient_email
    msg.attach(MIMEText(plain_body, 'plain'))
    msg.attach(MIMEText(html_body, 'html'))

    resend_key = config.resend_api_key
    smtp_configured = all([config.smtp_host, config.smtp_user, config.smtp_pass])

    # ---------- Try Resend first (if key exists) ----------
    if resend_key:
        try:
            response = requests.post(
                'https://api.resend.com/emails',
                headers={'Authorization': f'Bearer {resend_key}', 'Content-Type': 'application/json'},
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
            logger.info(f"Sent email via Resend for {tracking_number} to {recipient_email}")
            console.print(f"[info]Sent email via Resend for {tracking_number} to {recipient_email}[/info]")
            return True, False
        except requests.exceptions.HTTPError as e:
            status_code = e.response.status_code if e.response else None
            logger.warning(f"Resend HTTP {status_code} error for {tracking_number}: {e}")
            # If SMTP is not configured, treat 4xx (except 429) as permanent, else fallback
            if not smtp_configured:
                if status_code and 400 <= status_code < 500 and status_code != 429:
                    return False, True
                # Otherwise transient (will retry)
                return False, False
            # If SMTP is configured, we will fallback; do not return yet
        except Exception as e:
            logger.warning(f"Resend failed for {tracking_number}: {e}")
            if not smtp_configured:
                # Without SMTP, treat all Resend errors as transient (retry)
                return False, False
            # else fallback to SMTP

    # ---------- Fallback to SMTP ----------
    if smtp_configured:
        try:
            smtp_class = smtplib.SMTP_SSL if int(config.smtp_port) == 465 else smtplib.SMTP
            with smtp_class(config.smtp_host, config.smtp_port, timeout=20) as server:
                if int(config.smtp_port) != 465:
                    server.starttls()
                server.login(config.smtp_user, config.smtp_pass)
                server.send_message(msg)
            logger.info(f"Sent email via SMTP for {tracking_number} to {recipient_email}")
            console.print(f"[info]Sent email via SMTP for {tracking_number} to {recipient_email}[/info]")
            return True, False
        except smtplib.SMTPAuthenticationError:
            logger.error(f"SMTP authentication error for {tracking_number}")
            return False, True  # permanent
        except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError, socket.timeout, ConnectionError):
            logger.warning(f"SMTP connection error for {tracking_number}, will retry")
            return False, False  # transient
        except Exception as e:
            logger.error(f"SMTP error for {tracking_number}: {e}")
            # Most SMTP errors are transient; we'll retry
            return False, False
    else:
        # No SMTP configured and Resend already failed
        logger.error(f"No email provider available for {tracking_number}")
        return False, True  # permanent


def send_webhook(tracking_number: str, status: str, checkpoints: list, delivery_location: str, webhook_url: str) -> tuple[bool, bool]:
    """
    Send a webhook notification.
    Returns (success, permanent_failure)
    """
    try:
        if not webhook_url:
            logger.warning(f"No webhook URL for {tracking_number}")
            return False, True  # Missing URL is permanent

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
                return True, False
            except requests.exceptions.HTTPError as e:
                # 4xx client errors (except 429) are permanent
                if hasattr(e, 'response') and e.response is not None:
                    status_code = e.response.status_code
                    if 400 <= status_code < 500 and status_code != 429:
                        logger.error(f"Permanent webhook error (HTTP {status_code}) for {tracking_number}: {e}")
                        return False, True
                # Otherwise treat as transient
                if attempt == max_retries - 1:
                    raise
                time.sleep(2 ** attempt)
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
                if attempt == max_retries - 1:
                    raise
                time.sleep(2 ** attempt)
            except Exception as e:
                if attempt == max_retries - 1:
                    raise
                time.sleep(2 ** attempt)
        
        return False, False
    except requests.RequestException as e:
        logger.error(f"Failed to send webhook notification for {tracking_number}: {e}")
        console.print(Panel(f"[error]Failed to send webhook notification for {tracking_number}: {e}[/error]", title="Webhook Error", border_style="red"))
        return False, False


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
            retry_count = notification.get('retry_count', 0)

            # Check if we've exceeded retry limit
            if retry_count >= MAX_RETRIES:
                logger.warning(f"Discarding notification for {tracking_number} after {MAX_RETRIES} retries")
                console.print(f"[yellow]Discarded notification for {tracking_number} (type: {notification_type})[/yellow]")
                continue

            logger.info(f"Processing {notification_type} notification for {tracking_number} (attempt {retry_count + 1}/{MAX_RETRIES})")
            console.print(f"[info]Processing {notification_type} notification for {tracking_number} (attempt {retry_count + 1})[/info]")

            success = False
            permanent = False

            if notification_type == "email":
                success, permanent = send_email(
                    tracking_number=tracking_number,
                    status=data.get('status', 'Unknown'),
                    checkpoints=data.get('checkpoints', ''),
                    delivery_location=data.get('delivery_location', 'Unknown'),
                    recipient_email=data.get('recipient_email', ''),
                    subject=data.get('subject'),
                    html_body=data.get('html_body'),
                    plain_body=data.get('plain_body')
                )
            elif notification_type == "webhook":
                checkpoints = data.get('checkpoints', [])
                if isinstance(checkpoints, str):
                    checkpoints = checkpoints.split(';') if checkpoints else []
                success, permanent = send_webhook(
                    tracking_number=tracking_number,
                    status=data.get('status', 'Unknown'),
                    checkpoints=checkpoints,
                    delivery_location=data.get('delivery_location', 'Unknown'),
                    webhook_url=data.get('webhook_url', config.websocket_server)
                )
            else:
                logger.warning(f"Unknown notification type: {notification_type}")
                continue

            if success:
                logger.info(f"Successfully processed {notification_type} for {tracking_number}")
                continue

            # If permanent failure, discard
            if permanent:
                logger.error(f"Permanent failure for {tracking_number} ({notification_type}) – discarding")
                console.print(f"[red]Permanent failure for {tracking_number} ({notification_type}) – discarded[/red]")
                continue

            # Transient failure – requeue with incremented retry count
            notification['retry_count'] = retry_count + 1
            requeue_data = json.dumps(notification)
            safe_redis_operation(redis_client.lpush, "notifications", requeue_data)
            logger.warning(f"Requeued {notification_type} for {tracking_number} (attempt {retry_count + 1}/{MAX_RETRIES})")
            console.print(f"[yellow]Requeued {notification_type} for {tracking_number} (attempt {retry_count + 1})[/yellow]")

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
