from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from supabase import create_client, Client
from PyPDF2 import PdfReader
from openai import OpenAI
from jose import jwt, JWTError
from dotenv import load_dotenv
import requests, os, tempfile, json
import uuid


# ============================================================
# FastAPI Setup
# ============================================================
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================================
# Supabase Setup
# ============================================================
load_dotenv()
SUPABASE_URL = os.getenv("SUPABASE_URL", "https://mfkmwkyatoczkrqawffk.supabase.co")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

security = HTTPBearer()
JWT_SECRET = os.getenv("SUPABASE_JWT_SECRET")
JWT_AUDIENCE = "authenticated"

def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    token = credentials.credentials
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"], audience=JWT_AUDIENCE)
        return payload
    except JWTError as e:
        raise HTTPException(status_code=401, detail=f"Invalid or expired token: {e}")

def role_required(allowed_roles: list):
    def wrapper(payload=Depends(verify_token)):
        user_id = payload.get("sub")
        role = get_user_role(user_id)
        if role not in allowed_roles:
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        return payload
    return wrapper


# ============================================================
# Utilities
# ============================================================
def get_user_role(user_id: str):
    response = supabase.table("profiles").select("role").eq("id", user_id).single().execute()
    if not response.data:
        raise HTTPException(status_code=403, detail="Profile not found or user not authorized")
    return response.data["role"]

def extract_text_from_pdf_url(pdf_url: str) -> str:
    response = requests.get(pdf_url)
    if response.status_code != 200:
        raise HTTPException(status_code=404, detail="JD PDF not accessible.")
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        tmp.write(response.content)
        pdf_path = tmp.name
    reader = PdfReader(pdf_path)
    text = "\n".join(page.extract_text() for page in reader.pages if page.extract_text())
    os.remove(pdf_path)
    return text


# ============================================================
# Form Generation
# ============================================================
def generate_form_questions_with_gemini(jd_text: str):
    system_prompt = """
    You are "Founder's Chief Hiring Strategist v2.0" — an expert in designing lean, signal-rich hiring forms for high-impact roles.

    Your mission: create a **Google Form JSON** that screens for conviction, motivation, availability, and skill–role alignment, **not generic data**.

    ### Core Rules
    - Output **pure JSON only** — no markdown, no text outside JSON.
    - The output must match this schema:
      {
        "title": "Form title",
        "questions": [
          {
            "type": "TEXT" or "RADIO",
            "title": "Question title",
            "options": ["Option1", "Option2"]  // only for RADIO
          }
        ]
      }
    - Use `"TEXT"` for open-ended inputs and `"RADIO"` for multiple-choice.
    - Every `"RADIO"` question must have **2+ options**.
    - **Do NOT** include name, email, LinkedIn, CV, GitHub, or personal info fields — they are already fetched from user profiles.
    - Prioritize high-signal questions that:
      - Test technical or domain-specific understanding (based on JD).
      - Gauge motivation, ownership, and mindset (why this role, why now).
      - Assess availability and role fit.
    - Be concise, specific, and founder-style direct (no fluff, no HR phrasing).

    ### Output Style
    - Tone: Direct, founder-level clarity.
    - Focus: Signal > Noise. Insight > Politeness.
    - Number of questions: 10–18, depending on role complexity.

    ### Example Output Format
    {
      "title": "Founding Engineer – AI x Manufacturing",
      "questions": [
        {
          "type": "RADIO",
          "title": "Are you comfortable working full-time from Mumbai?",
          "options": ["Yes", "Can relocate", "Remote only"]
        },
        {
          "type": "TEXT",
          "title": "Describe one project where you solved a complex technical problem end-to-end."
        },
        ...
      ]
    }
    """

    user_prompt = f"""
    Generate the Google Form JSON based on this Job Description (JD):

    {jd_text}
    """

    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        response_format={"type": "json_object"}
    )
    return json.loads(response.choices[0].message.content)


# ============================================================
# Routes
# ============================================================

@app.post("/generate_form/{job_id}")
def generate_form(job_id: str, user=Depends(role_required(["admin", "client"]))):
    result = supabase.table("jobs").select("JD_pdf").eq("job_id", job_id).single().execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Job ID not found.")
    jd_pdf_url = result.data["JD_pdf"]
    if not jd_pdf_url:
        raise HTTPException(status_code=400, detail="No JD_pdf found for this job.")
    jd_text = extract_text_from_pdf_url(jd_pdf_url)
    form_data = generate_form_questions_with_gemini(jd_text)

    new_form_id = str(uuid.uuid4())

    data = {
        "form_id": new_form_id,
        "job_id": job_id,
        "form": form_data
    }

    response = supabase.table("forms").insert(data).execute()
    return {"status": "success", "form_id": new_form_id, "form": response.data}
