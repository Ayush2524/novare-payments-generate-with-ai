# novare-backend — Form Generation Service

FastAPI service that generates AI-powered application forms from job description PDFs.
Deployed on AWS EC2 (Mumbai) and accessed via `/api/form-proxy/[...path]` on the Next.js layer.

## Endpoint

`POST /generate_form/{job_id}`

Requires a valid Supabase JWT (Bearer token) with role `admin` or `client`.
Fetches the job's JD PDF from Supabase Storage, extracts text via PyPDF2, calls OpenAI gpt-4o-mini with the Founder's Chief Hiring Strategist prompt, and inserts the generated form into the `forms` table.

## Setup

1. Create and activate a virtual environment:
   ```
   uv venv
   .venv\Scripts\activate
   ```

2. Install dependencies:
   ```
   uv pip install -r requirements.txt
   ```

3. Create a `.env` file in this directory:
   ```
   SUPABASE_URL=https://<your-project>.supabase.co
   SUPABASE_KEY=<service-role-key>
   SUPABASE_JWT_SECRET=<jwt-secret>
   OPENAI_API_KEY=<openai-key>
   ```

4. Run the server:
   ```
   uvicorn main:app --reload --port 8000
   ```

## Production

Served via Gunicorn with Uvicorn workers:
```
gunicorn main:app -w 4 -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:8000
```
