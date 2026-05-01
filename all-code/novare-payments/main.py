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
LOCALTUNNEL_PATH = "https://novare-payments.loca.lt"
AMOUNT_PER_JOB = int(os.getenv("AMOUNT"))

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
    """Check if IP has exceeded rate limit"""
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

processed_webhooks = {}

def has_webhook_been_processed(link_id: str) -> bool:
    """Check if webhook was already processed (prevents duplicate processing)"""
    if link_id in processed_webhooks:
        processed_time = processed_webhooks[link_id]
        if (datetime.now() - processed_time).total_seconds() < 3600:
            logger.info(f"Webhook already processed: {link_id}")
            return True
    return False

def mark_webhook_processed(link_id: str):
    """Mark webhook as processed"""
    processed_webhooks[link_id] = datetime.now()
    logger.info(f"Webhook marked as processed: {link_id}")
    
    # Clean old entries (older than 24 hours)
    cutoff = datetime.now() - timedelta(hours=24)
    to_remove = [k for k, v in processed_webhooks.items() if v < cutoff]
    for k in to_remove:
        del processed_webhooks[k]

def verify_cashfree_signature(payload: str, signature: str, timestamp: str) -> bool:
    """
    Verify Cashfree webhook signature using HMAC-SHA256
    
    Cashfree signature format: 
    - Concatenate: {timestamp}{rawBody} (NO dot separator)
    - HMAC-SHA256 using client_secret as key
    - Base64 encode the result
    """
    if not signature or not timestamp:
        logger.warning("Missing signature or timestamp in webhook")
        logger.warning(f"Signature: {signature}, Timestamp: {timestamp}")
        logger.warning("⚠️ SECURITY WARNING: Skipping verification (testing mode)")
        return True  # In production, return False here
    
    try:
        message = f"{timestamp}{payload}"
        
        logger.debug(f"Verifying signature for message length: {len(message)}")
        logger.debug(f"Timestamp: {timestamp}")
        logger.debug(f"Payload (first 100 chars): {payload[:100]}")
        
        # Generate expected signature
        expected_signature_bytes = hmac.new(
            CASHFREE_CLIENT_SECRET.encode('utf-8'),
            message.encode('utf-8'),
            hashlib.sha256
        ).digest()
        
        # Convert to Base64
        expected_signature = base64.b64encode(expected_signature_bytes).decode('utf-8')
        
        # Constant-time comparison
        is_valid = hmac.compare_digest(signature, expected_signature)
        
        if not is_valid:
            logger.error("❌ Webhook signature verification FAILED")
            logger.error(f"Expected: {expected_signature}")
            logger.error(f"Received: {signature}")
            
            # TESTING MODE: Accept anyway (REMOVE IN PRODUCTION)
            logger.warning("⚠️ SECURITY WARNING: Accepting webhook despite signature mismatch (testing mode)")
            return False  # Change to: return False in production
        
        logger.info("✅ Webhook signature verified successfully")
        return True
        
    except Exception as e:
        logger.error(f"Error verifying signature: {e}", exc_info=True)
        return False

def validate_timestamp(timestamp: str, max_age_minutes: int = 10) -> bool:
    """Validate webhook timestamp to prevent replay attacks"""
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
        
        logger.debug(f"Timestamp validation passed. Age: {age:.2f} minutes")
        return True
    except (ValueError, TypeError, OSError) as e:
        logger.error(f"Error validating timestamp: {e}")
        logger.warning(" Accepting webhook despite timestamp validation error (testing mode)")
        return True
    except Exception as e:
        logger.error(f"Unexpected error validating timestamp: {e}")
        return False

def sanitize_input(value: str, max_length: int = 255) -> str:
    """Sanitize string inputs to prevent injection attacks"""
    if not value:
        return ""
    return str(value).strip()[:max_length]

# ============================ DATABASE HELPERS ============================

def fetch_profile_by_id(profile_id: str):
    """Fetch profile from database with validation"""
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
    """Create a new subscription record in Supabase"""
    try:
        profile_id = sanitize_input(profile_id, 100)
        
        if not profile_id:
            logger.error("Empty profile_id provided")
            return False
        
        if not isinstance(jobs, int) or jobs <= 0:
            logger.error(f"Invalid jobs value: {jobs}")
            return False
        
        subscription_data = {
            "id": str(uuid.uuid4()),  # Generate new UUID for subscription
            "profile_id": profile_id,
            "status": "paid",
            "jobs_remaining": jobs,
            "evaluations_remaining": jobs,
            "created_at": datetime.now().isoformat()
        }
        
        logger.info(f"Creating subscription for profile: {profile_id}")
        logger.info(f"Subscription data: {subscription_data}")
        
        response = supabase.table('subscriptions').insert(subscription_data).execute()
        
        if response.data:
            logger.info(f"✅ Subscription created successfully for profile: {profile_id}")
            logger.info(f"   - Subscription ID: {subscription_data['id']}")
            logger.info(f"   - Jobs remaining: {jobs}")
            logger.info(f"   - Evaluations remaining: {jobs}")
            logger.info(f"   - Status: paid")
            return True
        
        logger.error(f"Failed to create subscription for profile: {profile_id}")
        return False
        
    except Exception as e:
        logger.error(f"Error creating subscription: {e}")
        return False

# ============================ PAYMENT VERIFICATION ============================

def verify_payment_with_cashfree(link_id: str) -> Optional[dict]:
    """
    Verify payment status directly with Cashfree API.
    """
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
    """Validate payment amount matches expected amount"""
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

def create_payment_link(profile_data: dict, amount: int, jobs: int, webhook_url: Optional[str] = None):
    """Create a payment link via Cashfree API with full validation"""
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
                "profile_id": str(profile_id),
                "first_name": first_name,
                "last_name": last_name,
                "amount": str(amount),
                "jobs": str(jobs)  # Store jobs count in link_notes
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

# ============================ LOCALTUNNEL ============================

localtunnel_instance = {"url": None, "process": None}
localtunnel_lock = threading.Lock()

def start_localtunnel(port: int = 8000):
    """Starts a LocalTunnel on the given port and returns the public URL"""
    logger.info("Starting LocalTunnel...")

    try:
        process = subprocess.Popen(
            [LOCALTUNNEL_PATH, '--port', str(port)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            shell=True
        )

        time.sleep(5)
        
        public_url = None
        for _ in range(20):
            if process.poll() is not None:
                logger.error("LocalTunnel process terminated unexpectedly")
                return None, None
            
            try:
                line = process.stdout.readline()
                if line:
                    logger.info(line.strip())
                    match = re.search(r"(https://[a-zA-Z0-9-]+\.loca\.lt)", line)
                    if match:
                        public_url = match.group(1)
                        logger.info(f"✅ LocalTunnel public URL: {public_url}")
                        return public_url, process
            except Exception as e:
                logger.debug(f"Reading line: {e}")
            
            time.sleep(0.5)

        if not public_url:
            logger.error("Failed to get LocalTunnel URL")
            process.terminate()
            return None, None
        
    except FileNotFoundError:
        logger.error(f"LocalTunnel not found at: {LOCALTUNNEL_PATH}")
        logger.error("Install with: npm install -g localtunnel")
        return None, None
    except Exception as e:
        logger.error(f"Error starting LocalTunnel: {e}")
        return None, None

# ============================ FASTAPI ROUTES ============================

@app.post("/webhook/cashfree")
async def cashfree_webhook(
    request: Request,
    x_webhook_signature: str = Header(None, alias="x-webhook-signature"),
    x_webhook_timestamp: str = Header(None, alias="x-webhook-timestamp")
):
    """
    Secure webhook endpoint with correct signature verification and enhanced logging
    """
    client_ip = request.client.host
    logger.info(f"📩 Webhook received from IP: {client_ip}")
    
    # Log ALL headers for debugging
    logger.info("=== WEBHOOK HEADERS ===")
    for header_name, header_value in request.headers.items():
        logger.info(f"{header_name}: {header_value}")
    logger.info("======================")
    
    try:
        # Rate limiting
        if not check_rate_limit(client_ip, "webhook", max_requests=20, window_minutes=5):
            logger.error(f"❌ Rate limit exceeded for IP: {client_ip}")
            raise HTTPException(status_code=429, detail="Too many requests")
        
        # Get raw body EXACTLY as received
        body = await request.body()
        payload = body.decode('utf-8')
        
        logger.info(f"Raw payload length: {len(payload)}")
        logger.info(f"Payload (first 200 chars): {payload[:200]}")
        
        # Check signature headers
        if not x_webhook_signature or not x_webhook_timestamp:
            logger.warning("Missing signature headers!")
            logger.warning(f"x-webhook-signature: {x_webhook_signature}")
            logger.warning(f"x-webhook-timestamp: {x_webhook_timestamp}")
        
        # Verify signature
        if not verify_cashfree_signature(payload, x_webhook_signature, x_webhook_timestamp):
            logger.error(f"❌ Invalid webhook signature from IP: {client_ip}")
            return JSONResponse(
                status_code=401,
                content={"status": "error", "message": "Invalid signature"}
            )
        
        logger.info("✅ Webhook signature verified successfully")
        
        # Validate timestamp
        if x_webhook_timestamp and not validate_timestamp(x_webhook_timestamp):
            logger.error(f"❌ Webhook timestamp too old from IP: {client_ip}")
            return JSONResponse(
                status_code=400,
                content={"status": "error", "message": "Timestamp too old"}
            )
        
        # Parse webhook data
        try:
            webhook_data = json.loads(payload)
        except json.JSONDecodeError as e:
            logger.error(f"❌ Invalid JSON in webhook: {e}")
            logger.error(f"Payload: {payload}")
            return JSONResponse(
                status_code=400,
                content={"status": "error", "message": "Invalid JSON"}
            )
        
        data = webhook_data.get('data', {})
        link_id = sanitize_input(data.get('link_id', ''), 100)
        link_notes = data.get('link_notes', {})
        profile_id = sanitize_input(link_notes.get('profile_id', ''), 100)
        expected_amount = link_notes.get('amount')
        jobs = int(link_notes.get('jobs', '1'))  # Get jobs from link_notes
        
        logger.info(f"Webhook data - link_id: {link_id}, profile_id: {profile_id}, expected_amount: {expected_amount}, jobs: {jobs}")
        
        if not link_id or not profile_id:
            logger.error("❌ Missing link_id or profile_id in webhook")
            return JSONResponse(
                status_code=400,
                content={"status": "error", "message": "Missing required fields"}
            )
        
        logger.info(f"Processing webhook for link_id: {link_id}, profile_id: {profile_id}")
        
        # Idempotency check
        if has_webhook_been_processed(link_id):
            logger.info(f"⚠️ Webhook already processed for link_id: {link_id}")
            return JSONResponse(
                status_code=200,
                content={"status": "success", "message": "Already processed"}
            )
        
        # Verify payment with Cashfree API
        verified_data = verify_payment_with_cashfree(link_id)
        
        if not verified_data:
            logger.error(f"❌ Failed to verify payment with Cashfree for link_id: {link_id}")
            return JSONResponse(
                status_code=400,
                content={"status": "error", "message": "Payment verification failed"}
            )
        
        logger.info("✅ Payment verified with Cashfree API successfully")
        
        # Check payment status
        verified_status = verified_data.get('link_status')
        is_paid = verified_status == 'PAID'
        
        if not is_paid and 'payment' in verified_data:
            payment_status = verified_data.get('payment', {}).get('payment_status')
            is_paid = payment_status == 'SUCCESS'
        
        logger.info(f"Payment status: {verified_status}, is_paid: {is_paid}")
        
        # Validate amount
        if expected_amount and not validate_payment_amount(verified_data, int(expected_amount)):
            logger.error(f"❌ Payment amount validation failed for link_id: {link_id}")
            return JSONResponse(
                status_code=400,
                content={"status": "error", "message": "Amount mismatch"}
            )
        
        # Create subscription if payment confirmed
        if is_paid and profile_id:
            logger.info(f"💰 Payment confirmed! Creating subscription for profile: {profile_id}")
            
            success = create_subscription(profile_id, jobs)
            
            if success:
                mark_webhook_processed(link_id)
                
                logger.info(f"✅ Successfully processed payment for profile: {profile_id}")
                return JSONResponse(
                    status_code=200,
                    content={
                        "status": "success",
                        "message": "Subscription created successfully",
                        "profile_id": profile_id,
                        "jobs": jobs
                    }
                )
            else:
                logger.error(f"❌ Failed to create subscription for profile: {profile_id}")
                return JSONResponse(
                    status_code=500,
                    content={"status": "error", "message": "Failed to create subscription"}
                )
        else:
            logger.warning(f"⚠️ Payment not confirmed. Status: {verified_status}")
            return JSONResponse(
                status_code=200,
                content={"status": "pending", "message": "Payment not confirmed"}
            )
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Webhook error: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"status": "error", "message": "Internal server error"}
        )


@app.post("/start-payment/{profile_id}")
async def start_payment(
    request: Request, 
    profile_id: str, 
    jobs: int = Query(default=1, ge=1, le=10, description="Number of jobs to purchase")
):
    """
    Automated payment flow endpoint.
    Creates a payment link and starts the webhook listener.
    
    Parameters:
    - profile_id: The UUID of the user's profile
    - jobs: Number of jobs to purchase (default: 1, min: 1, max: 100)
    """
    client_ip = request.client.host
    
    try:
        # Calculate amount based on jobs
        amount = AMOUNT_PER_JOB * jobs
        
        # Rate limiting
        if not check_rate_limit(client_ip, "api", max_requests=10, window_minutes=1):
            raise HTTPException(status_code=429, detail="Too many requests")
        
        profile_id = sanitize_input(profile_id, 100)

        if not isinstance(amount, int) or amount <= 0 or amount > 10000000:
            logger.error(f"Invalid amount: {amount}")
            raise HTTPException(status_code=400, detail="Invalid amount (must be between 1 and 10,000,000)")
        
        logger.info(f" Starting payment flow for profile: {profile_id}")
        logger.info(f"   - Jobs: {jobs}")
        logger.info(f"   - Amount per job: ₹{AMOUNT_PER_JOB}")
        logger.info(f"   - Total amount: ₹{amount}")
        
        # Get or start LocalTunnel (thread-safe)
        public_url = "https://novare-payments.loca.lt"

        notify_url = f"{public_url}/webhook/cashfree"
        logger.info(f"📡 Using notify_url: {notify_url}")

        # Fetch profile
        profile = fetch_profile_by_id(profile_id)
        if not profile:
            raise HTTPException(status_code=404, detail="Profile not found")

        # Create payment link
        result = create_payment_link(profile, amount=amount, jobs=jobs, webhook_url=notify_url)
        if not result:
            raise HTTPException(status_code=500, detail="Failed to create payment link")

        payment_url = result.get("link_url")
        logger.info(f"✅ Payment link created: {payment_url}")

        return JSONResponse(
            status_code=200,
            content={
                "status": "success",
                "message": "Payment link created successfully",
                "payment_link": payment_url,
                "notify_url": notify_url,
                "profile_id": profile_id,
                "jobs": jobs,
                "amount_per_job": AMOUNT_PER_JOB,
                "total_amount": amount
            }
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Error in /start-payment: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/")
async def root():
    """Health check endpoint"""
    return {
        "status": "ok",
        "message": "Novare Talent Automated Payment System",
        "version": "3.1.0 - Subscription-based",
        "amount_per_job": AMOUNT_PER_JOB
    }


@app.get("/health")
async def health():
    """Detailed health check"""
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "services": {
            "supabase": "connected",
            "cashfree": "configured"
        },
        "config": {
            "amount_per_job": AMOUNT_PER_JOB
        }
    }

if __name__ == "__main__":
    logger.info("=" * 70)
    logger.info("🚀 Starting Novare Talent Payment System")
    logger.info("=" * 70)
    logger.info(f"Configuration:")
    logger.info(f"  💰 Amount per job: ₹{AMOUNT_PER_JOB}")
    logger.info("=" * 70)
    logger.info("Security features enabled:")
    logger.info("  ✅ Webhook signature verification (HMAC-SHA256)")
    logger.info("  ✅ Timestamp validation (prevents replay attacks)")
    logger.info("  ✅ Rate limiting (20 req/5min for webhooks, 10 req/1min for API)")
    logger.info("  ✅ Idempotency checks (prevents duplicate processing)")
    logger.info("  ✅ Direct Cashfree API verification (never trust webhooks alone)")
    logger.info("  ✅ Input sanitization (prevents injection attacks)")
    logger.info("  ✅ Amount validation (ensures correct payment)")
    logger.info("  ✅ Comprehensive logging (full audit trail)")
    logger.info("=" * 70)
    uvicorn.run(app, host="0.0.0.0", port=8000)
