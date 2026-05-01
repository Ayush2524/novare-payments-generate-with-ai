# Novare Talent — Backend Services

A dual microservice system powering **automated hiring workflows** for the Novare Talent platform. Handles subscription-based payments and AI-driven candidate evaluation.

---

## Architecture Overview

```
┌──────────────────────┐        ┌──────────────────────────┐
│   novare-payments    │        │     novare-backend        │
│  (FastAPI + Uvicorn) │        │  (FastAPI + Gunicorn)     │
│                      │        │                           │
│  - Payment links     │        │  - Form generation (GPT)  │
│  - Cashfree webhooks │        │  - Candidate evaluation   │
│  - Subscriptions     │        │  - JWT auth + RBAC        │
└──────────┬───────────┘        └────────────┬──────────────┘
           │                                 │
           └──────────────┬──────────────────┘
                          │
                 ┌────────▼────────┐
                 │    Supabase     │
                 │  (PostgreSQL)   │
                 └─────────────────┘
```

Both services share a **Supabase database** — no direct service-to-service communication.

---

## Services

### 1. `novare-payments` — Payment Microservice

Handles subscription purchases via Cashfree payment gateway.

**Endpoints:**

| Method | Endpoint | Description | Auth |
|--------|----------|-------------|------|
| `POST` | `/start-payment/{profile_id}?jobs=N` | Create a Cashfree payment link (1–10 jobs) | None |
| `POST` | `/webhook/cashfree` | Receive payment confirmation from Cashfree | HMAC Signature |
| `GET` | `/` | Health check | None |
| `GET` | `/health` | Detailed health status | None |

**Payment Flow:**
1. Client calls `/start-payment/{profile_id}?jobs=2`
2. Service creates a Cashfree payment link (expires in 2 hours)
3. User completes payment on Cashfree-hosted page
4. Cashfree sends webhook to `/webhook/cashfree`
5. Service verifies payment directly with Cashfree API
6. On success, creates a subscription record in Supabase

**Security:**
- HMAC-SHA256 webhook signature verification
- Timestamp validation (10-minute window, replay attack prevention)
- Rate limiting: 10 req/min (API), 20 req/5min (webhooks)
- Idempotency tracking (1-hour dedup window)
- Direct Cashfree API verification — never trusts webhook payload alone
- Amount validation against expected price

**Environment Variables:**
```env
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_KEY=your_supabase_anon_key
CASHFREE_URL=https://api.cashfree.com/pg/links
CASHFREE_CLIENT_ID=your_cashfree_client_id
CASHFREE_CLIENT_SECRET=your_cashfree_client_secret
AMOUNT=5000
```

---

### 2. `novare-backend` — Evaluation Microservice

Generates AI-powered screening forms from job descriptions and evaluates candidates.

**Endpoints:**

| Method | Endpoint | Description | Auth |
|--------|----------|-------------|------|
| `POST` | `/generate_form/{job_id}` | Generate screening form from JD PDF | JWT (admin/client) |
| `POST` | `/evaluate/{job_id}` | Score all candidates for a job | JWT (admin/client) |

**Form Generation Flow:**
1. Fetch JD PDF from `jobs` table
2. Extract text (pdfplumber + OCR fallback via Tesseract)
3. GPT-4o-mini generates 10–18 screening questions (TEXT + RADIO types)
4. Store form schema (JSONB) in `forms` table

**Evaluation Flow:**
1. Fetch all form responses for the job
2. Download candidate resumes concurrently (max 15 parallel)
3. Extract resume text (PDF → OCR fallback, cached per candidate)
4. GPT-4o-mini scores each candidate on 5 metrics:
   - `skills_match` (1–10)
   - `experience_relevance` (1–10)
   - `communication_clarity` (1–10)
   - `overall_fit` (1–10)
   - `final_score` (0–10) + `justification`
5. Store all results in `evaluations` table

**Authentication:**
- JWT bearer token (Supabase-issued)
- Role-based access control — only `admin` or `client` roles

**Environment Variables:**
```env
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_KEY=your_supabase_anon_key
SUPABASE_JWT_SECRET=your_jwt_secret
OPENAI_API_KEY=your_openai_api_key
```

---

## Database Schema (Supabase / PostgreSQL)

```
profiles
  id (UUID PK), first_name, last_name, email, phone, role, created_at

subscriptions
  id (UUID PK), profile_id (FK), status, jobs_remaining, evaluations_remaining, created_at

jobs
  job_id (UUID PK), JD_pdf (URL), ...metadata

forms
  form_id (UUID PK), job_id (FK), form (JSONB: {title, questions[]})

responses
  form_id (FK), profile_id (FK), answers (JSONB)

evaluations
  job_id (FK), results (JSONB array: [{profile_id, results: {scores, justification}}])
```

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Framework | FastAPI |
| Server | Gunicorn + Uvicorn workers |
| Database | Supabase (PostgreSQL) |
| Auth | JWT (python-jose) |
| Payment Gateway | Cashfree (API v2025-01-01) |
| AI | OpenAI GPT-4o-mini |
| PDF Processing | pdfplumber, PyPDF2, pdf2image, Tesseract OCR |
| Runtime | Python 3.11 (backend), Python 3.12 (payments) |
| Container | Docker |

---

## Running Locally

### novare-payments
```bash
cd all-code/novare-payments
python -m venv .venv && .venv\Scripts\activate   # Windows
pip install -r requirements.txt
cp .env.example .env   # fill in env vars
uvicorn main:app --reload --port 8001
```

### novare-backend
```bash
cd all-code/novare-backend
python -m venv .venv && .venv\Scripts\activate   # Windows
pip install -r requirements.txt
cp .env.example .env   # fill in env vars
uvicorn main:app --reload --port 8000
```

> **Note:** The backend requires **Tesseract** and **Poppler** installed system-wide for PDF OCR.
> - Windows: [Tesseract installer](https://github.com/UB-Mannheim/tesseract/wiki), [Poppler for Windows](https://github.com/oschwartz10612/poppler-windows)

---

## Docker Deployment

### novare-payments
```bash
docker build -t novare-payments:latest ./all-code/novare-payments
docker run -p 8001:8000 \
  -e SUPABASE_URL=... \
  -e SUPABASE_KEY=... \
  -e CASHFREE_CLIENT_ID=... \
  -e CASHFREE_CLIENT_SECRET=... \
  -e CASHFREE_URL=https://api.cashfree.com/pg/links \
  -e AMOUNT=5000 \
  novare-payments:latest
```

### novare-backend
```bash
docker build -t novare-backend:latest ./all-code/novare-backend
docker run -p 8000:8000 \
  -e SUPABASE_URL=... \
  -e SUPABASE_KEY=... \
  -e SUPABASE_JWT_SECRET=... \
  -e OPENAI_API_KEY=... \
  novare-backend:latest
```

---

## Business Model

- Users purchase job evaluation subscriptions (1–10 jobs per transaction)
- Price per job: configurable via `AMOUNT` env var (default ₹5000)
- Each subscription tracks `jobs_remaining` and `evaluations_remaining`
- Payment links expire in 2 hours; SMS/email reminders enabled via Cashfree

---

## Notes

- Both services write logs to stdout and a local log file (`payment_system.log` for payments)
- The backend caps concurrent evaluations at **15 parallel tasks** (semaphore-controlled)
- Resume text is **cached per candidate** to avoid redundant PDF re-processing
- The payment service currently runs with relaxed signature validation (testing mode) — tighten before full production use
