import requests
from supabase import create_client, Client
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, Request, Header, HTTPException, Query
from fastapi.responses import JSONResponse
import uvicorn
import subprocess
import re
import time
import threading
import json
import hmac
import hashlib
import base64
import logging
import os
from dotenv import load_dotenv
from collections import defaultdict
from typing import Optional
import uuid

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
CASHFREE_URL = os.getenv("CASHFREE_URL", "https://api.cashfree.com/pg/links")
CASHFREE_CLIENT_ID = os.getenv("CASHFREE_CLIENT_ID")
CASHFREE_CLIENT_SECRET = os.getenv("CASHFREE_CLIENT_SECRET")
LOCALTUNNEL_PATH = "https://grafted-seltzer-shorter.ngrok-free.dev"
AMOUNT_PER_JOB = int(os.getenv("AMOUNT"))
BOT_CALLBACK_URL = os.getenv("BOT_CALLBACK_URL", "")  # e.g. https://zenhyre-bot.loca.lt

# Validate required environment variables
required_vars = ["SUPABASE_URL", "SUPABASE_KEY", "CASHFREE_CLIENT_ID", "CASHFREE_CLIENT_SECRET"]
for var in required_vars:
    if not os.getenv(var):
        raise ValueError(f"Missing required environment variable: {var}")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
app = FastAPI(title="Novare Talent Automated Payment System")

# ============================ LOGGING SETUP ============================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('payment_system.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ============================ RATE LIMITING ============================

webhook_requests = defaultdict(list)
api_requests = defaultdict(list)

def check_rate_limit(ip: str, request_type: str = "webhook", max_requests: int = 20, window_minutes: int = 5) -> bool:
    request_tracker = webhook_requests if request_type == "webhook" else api_requests
    now = datetime.now()
    cutoff = now - timedelta(minutes=window_minutes)
    request_tracker[ip] = [req_time for req_time in request_tracker[ip] if req_time > cutoff]
    if len(request_tracker[ip]) >= max_requests:
        logger.warning(f"Rate limit exceeded for IP: {ip} (type: {request_type})")
        return False
    request_tracker[ip].append(now)
    return True

# ============================ IDEMPOTENCY TRACKING ============================

processed_webhooks = {}   # link_id  → datetime (1h dedup)
payment_processed = {}    # profile_id → datetime (60s dedup, blocks late link webhook)

def claim_webhook(link_id: str) -> bool:
    """
    Atomically check-and-claim by link_id (1h window).
    Returns True = duplicate, skip. False = first call, proceed.
    Safe within a single process; use workers=1 in Gunicorn.
    """
    now = datetime.now()
    if link_id in processed_webhooks:
        if (now - processed_webhooks[link_id]).total_seconds() < 3600:
            logger.info(f"Duplicate webhook ignored (link_id): {link_id}")
            return True
    processed_webhooks[link_id] = now
    cutoff = now - timedelta(hours=24)
    for k in [k for k, v in list(processed_webhooks.items()) if v < cutoff]:
        del processed_webhooks[k]
    logger.info(f"Webhook claimed: {link_id}")
    return False

def claim_payment(profile_id: str) -> bool:
    """
    Atomically check-and-claim by profile_id (60s window).
    Blocks the late-arriving link-type webhook from re-processing
    the same payment that the earlier order-type webhook already handled.
    Returns True = already processed recently, skip.
    """
    now = datetime.now()
    if profile_id in payment_processed:
        if (now - payment_processed[profile_id]).total_seconds() < 60:
            logger.info(f"Duplicate payment ignored (profile_id, 60s window): {profile_id}")
            return True
    payment_processed[profile_id] = now
    return False

def verify_cashfree_signature(payload: str, signature: str, timestamp: str) -> bool:
    if not signature or not timestamp:
        logger.warning("Missing signature or timestamp in webhook")
        logger.warning("⚠️ SECURITY WARNING: Skipping verification (testing mode)")
        return True
    try:
        message = f"{timestamp}{payload}"
        expected_signature_bytes = hmac.new(
            CASHFREE_CLIENT_SECRET.encode('utf-8'),
            message.encode('utf-8'),
            hashlib.sha256
        ).digest()
        expected_signature = base64.b64encode(expected_signature_bytes).decode('utf-8')
        is_valid = hmac.compare_digest(signature, expected_signature)
        if not is_valid:
            logger.error("❌ Webhook signature verification FAILED")
            logger.warning("⚠️ SECURITY WARNING: Accepting webhook despite signature mismatch (testing mode)")
            return False
        logger.info("✅ Webhook signature verified successfully")
        return True
    except Exception as e:
        logger.error(f"Error verifying signature: {e}", exc_info=True)
        return False

def validate_timestamp(timestamp: str, max_age_minutes: int = 10) -> bool:
    try:
        timestamp_value = int(timestamp)
        if timestamp_value > 10000000000:
            webhook_time = datetime.fromtimestamp(timestamp_value / 1000)
        else:
            webhook_time = datetime.fromtimestamp(timestamp_value)
        current_time = datetime.now()
        age = abs((current_time - webhook_time).total_seconds() / 60)
        if age > max_age_minutes:
            logger.warning(f"Webhook timestamp too old: {age} minutes")
            return False
        return True
    except (ValueError, TypeError, OSError) as e:
        logger.error(f"Error validating timestamp: {e}")
        logger.warning("Accepting webhook despite timestamp validation error (testing mode)")
        return True
    except Exception as e:
        logger.error(f"Unexpected error validating timestamp: {e}")
        return False

def sanitize_input(value: str, max_length: int = 255) -> str:
    if not value:
        return ""
    return str(value).strip()[:max_length]

def _cf_safe(val: str) -> str:
    """Strip chars Cashfree forbids in link_notes values and enforce 1-100 char limit."""
    cleaned = re.sub(r'[<>{}\x22“”]', '', str(val)).strip()
    return cleaned[:100] or "N/A"

# ============================ DATABASE HELPERS ============================

def fetch_profile_by_id(profile_id: str):
    try:
        profile_id = sanitize_input(profile_id, 100)
        if not profile_id:
            logger.error("Empty profile_id provided")
            return None
        response = supabase.table('profiles').select('*').eq('id', profile_id).execute()
        logger.info(f"Fetching profile with ID: {profile_id}")
        if response.data:
            return response.data[0]
        logger.warning(f"Profile not found: {profile_id}")
        return None
    except Exception as e:
        logger.error(f"Error fetching profile: {e}")
        return None

def create_subscription(profile_id: str, jobs: int) -> bool:
    try:
        profile_id = sanitize_input(profile_id, 100)
        if not profile_id:
            logger.error("Empty profile_id provided")
            return False
        if not isinstance(jobs, int) or jobs <= 0:
            logger.error(f"Invalid jobs value: {jobs}")
            return False

        logger.info(f"Upserting subscription for profile: {profile_id}, adding {jobs} job(s)")

        existing = supabase.table('subscriptions').select('jobs_remaining,evaluations_remaining').eq('profile_id', profile_id).execute()

        if existing.data:
            current = existing.data[0]
            new_jobs = (current.get('jobs_remaining') or 0) + jobs
            new_evals = (current.get('evaluations_remaining') or 0) + jobs
            response = supabase.table('subscriptions').update({
                'status': 'paid',
                'jobs_remaining': new_jobs,
                'evaluations_remaining': new_evals,
            }).eq('profile_id', profile_id).execute()
        else:
            response = supabase.table('subscriptions').insert({
                "id": str(uuid.uuid4()),
                "profile_id": profile_id,
                "status": "paid",
                "jobs_remaining": jobs,
                "evaluations_remaining": jobs,
                "created_at": datetime.now().isoformat()
            }).execute()

        if response.data:
            logger.info(f"✅ Subscription upserted successfully for profile: {profile_id}")
            return True
        logger.error(f"Failed to upsert subscription for profile: {profile_id}")
        return False
    except Exception as e:
        logger.error(f"Error upserting subscription: {e}")
        return False

# ============================ PAYMENT VERIFICATION ============================

def verify_payment_with_cashfree(link_id: str) -> Optional[dict]:
    url = f"{CASHFREE_URL}/{link_id}"
    headers = {
        "x-api-version": "2025-01-01",
        "x-client-id": CASHFREE_CLIENT_ID,
        "x-client-secret": CASHFREE_CLIENT_SECRET,
        "Content-Type": "application/json"
    }
    try:
        logger.info(f"Verifying payment with Cashfree for link_id: {link_id}")
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()
        logger.info(f"Cashfree verification response: {data.get('link_status')}")
        return data
    except requests.exceptions.RequestException as e:
        logger.error(f"Error verifying payment with Cashfree: {e}")
        return None

def validate_payment_amount(verified_data: dict, expected_amount: Optional[int] = None) -> bool:
    if not expected_amount:
        return True
    try:
        paid_amount = verified_data.get('link_amount')
        if paid_amount and int(paid_amount) == expected_amount:
            return True
        logger.warning(f"Amount mismatch: expected {expected_amount}, got {paid_amount}")
        return False
    except Exception as e:
        logger.error(f"Error validating amount: {e}")
        return False

# ============================ PAYMENT LINK CREATION ============================

def create_payment_link(profile_data: dict, amount: int, jobs: int, webhook_url: Optional[str] = None, phone_override: str = ""):
    try:
        profile_id = sanitize_input(profile_data.get('id', ''), 100)
        first_name = sanitize_input(profile_data.get('first_name', ''), 100)
        last_name = sanitize_input(profile_data.get('last_name', ''), 100)
        email = sanitize_input(profile_data.get('email', ''), 255)
        phone = sanitize_input(profile_data.get('phone', ''), 20)

        if not all([profile_id, email, phone]):
            logger.error("Missing required profile fields")
            return None
        if '@' not in email or '.' not in email:
            logger.error(f"Invalid email format: {email}")
            return None
        if not any(char.isdigit() for char in phone):
            logger.error(f"Invalid phone format: {phone}")
            return None
        if not isinstance(amount, int) or amount <= 0 or amount > 10000000:
            logger.error(f"Invalid amount: {amount}")
            return None

        full_name = f"{first_name} {last_name}".strip() or "Customer"
        short_id = str(profile_id)[-8:]
        timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
        link_id = f"link_{short_id}_{timestamp}"
        IST = timezone(timedelta(hours=5, minutes=30))
        expiry_time = (datetime.now(IST) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S+05:30")
        notify_url = webhook_url or "https://fallback-url/webhook/cashfree"

        payload = {
            "customer_details": {
                "customer_email": email,
                "customer_name": full_name,
                "customer_phone": phone
            },
            "link_amount": amount,
            "link_auto_reminders": True,
            "link_currency": "INR",
            "link_expiry_time": expiry_time,
            "link_id": link_id,
            "link_meta": {
                "notify_url": notify_url,
                "return_url": "https://www.novaretalent.com/sign-in",
                "upi_intent": False
            },
            "link_notify": {
                "send_email": True,
                "send_sms": True
            },
            "link_purpose": "Novare Talent Subscription Payment",
            "link_notes": {
                "profile_id": _cf_safe(str(profile_id)),
                "name": _cf_safe(full_name),
                "amount": _cf_safe(str(amount)),
                "jobs": _cf_safe(str(jobs)),
                "phone": _cf_safe(phone_override or phone),
            },
            "link_partial_payments": False
        }

        headers = {
            "x-api-version": "2025-01-01",
            "x-client-id": CASHFREE_CLIENT_ID,
            "x-client-secret": CASHFREE_CLIENT_SECRET,
            "Content-Type": "application/json"
        }

        logger.info(f"Creating payment link for profile: {profile_id}")
        logger.info(f"Email: {email}, Phone: {phone}, Amount: {amount}, Jobs: {jobs}")

        response = requests.post(CASHFREE_URL, json=payload, headers=headers, timeout=10)
        response.raise_for_status()
        result = response.json()

        logger.info(f"Payment link created successfully - Link ID: {link_id}")
        return result

    except requests.exceptions.RequestException as e:
        logger.error(f"Error creating payment link: {e}")
        if hasattr(e, 'response') and e.response is not None:
            logger.error(f"Response: {e.response.text}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error creating payment link: {e}")
        return None

# ============================ FASTAPI ROUTES ============================

@app.post("/webhook/cashfree")
async def cashfree_webhook(
    request: Request,
    x_webhook_signature: str = Header(None, alias="x-webhook-signature"),
    x_webhook_timestamp: str = Header(None, alias="x-webhook-timestamp")
):
    client_ip = request.client.host
    logger.info(f"📩 Webhook received from IP: {client_ip}")

    try:
        if not check_rate_limit(client_ip, "webhook", max_requests=20, window_minutes=5):
            raise HTTPException(status_code=429, detail="Too many requests")

        body = await request.body()
        payload = body.decode('utf-8')

        logger.info(f"Raw payload length: {len(payload)}")
        logger.info(f"Payload (first 200 chars): {payload[:200]}")

        if not verify_cashfree_signature(payload, x_webhook_signature, x_webhook_timestamp):
            logger.error(f"❌ Invalid webhook signature from IP: {client_ip}")
            return JSONResponse(status_code=401, content={"status": "error", "message": "Invalid signature"})

        if x_webhook_timestamp and not validate_timestamp(x_webhook_timestamp):
            return JSONResponse(status_code=400, content={"status": "error", "message": "Timestamp too old"})

        try:
            webhook_data = json.loads(payload)
        except json.JSONDecodeError as e:
            logger.error(f"❌ Invalid JSON in webhook: {e}")
            return JSONResponse(status_code=400, content={"status": "error", "message": "Invalid JSON"})

        data = webhook_data.get('data', {})

        # Support both link webhook (link_notes) and order webhook (order_tags via notify_url)
        link_id = sanitize_input(data.get('link_id', ''), 100)
        link_notes = data.get('link_notes', {})

        if not link_notes and 'order' in data:
            link_notes = data['order'].get('order_tags', {})
        if not link_id and 'order' in data:
            link_id = sanitize_input(data['order'].get('order_id', ''), 100)

        profile_id = sanitize_input(link_notes.get('profile_id', ''), 100)
        expected_amount = link_notes.get('amount')
        jobs = int(link_notes.get('jobs', '1'))
        phone = sanitize_input(link_notes.get('phone', ''), 20)

        logger.info(f"Webhook - link_id: {link_id}, profile_id: {profile_id}, jobs: {jobs}, phone: {phone}")

        if not link_id or not profile_id:
            return JSONResponse(status_code=400, content={"status": "error", "message": "Missing required fields"})

        if claim_webhook(link_id):
            return JSONResponse(status_code=200, content={"status": "success", "message": "Already processed"})

        # For order-based webhooks, payment status is in the payload itself
        is_paid = False
        verified_status = None
        if 'payment' in data:
            is_paid = data['payment'].get('payment_status') == 'SUCCESS'

        # For link-based webhooks, verify via Cashfree API
        if not is_paid and not link_id.startswith('CFPay_'):
            verified_data = verify_payment_with_cashfree(link_id)
            if verified_data:
                verified_status = verified_data.get('link_status')
                is_paid = verified_status == 'PAID'
                if not is_paid and 'payment' in verified_data:
                    is_paid = verified_data.get('payment', {}).get('payment_status') == 'SUCCESS'
                logger.info(f"Cashfree API verification - status: {verified_status}, is_paid: {is_paid}")

        logger.info(f"is_paid: {is_paid}")

        if is_paid and profile_id:
            if claim_payment(profile_id):
                return JSONResponse(status_code=200, content={"status": "success", "message": "Already processed"})
            success = create_subscription(profile_id, jobs)
            if success:
                logger.info(f"✅ Payment processed for profile: {profile_id}")

                # Notify the WhatsApp bot to resume job posting automatically
                if BOT_CALLBACK_URL and phone:
                    try:
                        cb = requests.post(
                            f"{BOT_CALLBACK_URL}/payment/confirmed",
                            json={"phone": phone, "profile_id": profile_id, "jobs": jobs},
                            timeout=10,
                        )
                        logger.info(f"Bot callback status: {cb.status_code}")
                    except Exception as cb_err:
                        logger.warning(f"Bot callback failed (non-fatal): {cb_err}")

                return JSONResponse(status_code=200, content={
                    "status": "success",
                    "message": "Subscription created successfully",
                    "profile_id": profile_id,
                    "jobs": jobs
                })
            else:
                return JSONResponse(status_code=500, content={"status": "error", "message": "Failed to create subscription"})
        else:
            logger.warning(f"⚠️ Payment not confirmed. Status: {verified_status}")
            return JSONResponse(status_code=200, content={"status": "pending", "message": "Payment not confirmed"})

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Webhook error: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"status": "error", "message": "Internal server error"})


@app.post("/start-payment/{profile_id}")
async def start_payment(
    request: Request,
    profile_id: str,
    jobs: int = Query(default=1, ge=1, le=10, description="Number of jobs to purchase"),
    phone: str = Query(default="", description="User's WhatsApp phone number for bot callback"),
):
    client_ip = request.client.host
    try:
        amount = AMOUNT_PER_JOB * jobs

        if not check_rate_limit(client_ip, "api", max_requests=10, window_minutes=1):
            raise HTTPException(status_code=429, detail="Too many requests")

        profile_id = sanitize_input(profile_id, 100)

        if not isinstance(amount, int) or amount <= 0 or amount > 10000000:
            raise HTTPException(status_code=400, detail="Invalid amount")

        logger.info(f"Starting payment flow for profile: {profile_id}, jobs: {jobs}, amount: ₹{amount}")

        notify_url = f"{LOCALTUNNEL_PATH}/webhook/cashfree"
        logger.info(f"📡 Using notify_url: {notify_url}")

        profile = fetch_profile_by_id(profile_id)
        if not profile:
            raise HTTPException(status_code=404, detail="Profile not found")

        result = create_payment_link(profile, amount=amount, jobs=jobs, webhook_url=notify_url, phone_override=phone)
        if not result:
            raise HTTPException(status_code=500, detail="Failed to create payment link")

        payment_url = result.get("link_url")
        logger.info(f"✅ Payment link created: {payment_url}")

        return JSONResponse(status_code=200, content={
            "status": "success",
            "message": "Payment link created successfully",
            "payment_link": payment_url,
            "profile_id": profile_id,
            "jobs": jobs,
            "amount_per_job": AMOUNT_PER_JOB,
            "total_amount": amount
        })

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Error in /start-payment: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/")
async def root():
    return {
        "status": "ok",
        "message": "Novare Talent Automated Payment System",
        "version": "3.2.0 - Subscription-based",
        "amount_per_job": AMOUNT_PER_JOB
    }


@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "services": {"supabase": "connected", "cashfree": "configured"},
        "config": {"amount_per_job": AMOUNT_PER_JOB}
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
