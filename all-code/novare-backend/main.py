from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from supabase import create_client, Client
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from PyPDF2 import PdfReader
import requests, os, tempfile, json, re, io
import pandas as pd
import pdfplumber
import pytesseract
from openai import AsyncOpenAI, OpenAI
from pdf2image import convert_from_path
from dotenv import load_dotenv
import uuid


import time
import asyncio
import functools
from pathlib import Path
from urllib.parse import unquote

from jose import jwt, JWTError
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi import Depends



# ============================================================
# FastAPI Setup
# ============================================================
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # adjust to frontend domain if needed
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
JWT_AUDIENCE = "authenticated"  # default in Supabase

def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    token = credentials.credentials
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"], audience=JWT_AUDIENCE)
        return payload  # contains user info like sub, email, role, etc.
    except JWTError as e:
        raise HTTPException(status_code=401, detail=f"Invalid or expired token: {e}")
    
def role_required(allowed_roles: list):
    def wrapper(payload=Depends(verify_token)):
        user_id = payload.get("sub")
        role = get_user_role(user_id)
        if role not in allowed_roles:
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        return payload  # So downstream routes still know the user info
    return wrapper

# ============================================================
# AI Setup
# ============================================================
openai_client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
# Constants for the pipeline
CONCURRENCY_LIMIT = 15  # Max concurrent async tasks
RESUMES_DIR = Path("resumes")
TEXT_CACHE_DIR = Path("resumes_text_cache")


# ============================================================
# Utilities
# ============================================================
def get_user_role(user_id: str):
    """Fetch the user's role from the profiles table."""
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

def extract_text_from_pdf(pdf_path: str):
    text = ""
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text += page_text
        if text.strip():
            return text.strip()
    except:
        pass
    try:
        images = convert_from_path(pdf_path)
        for image in images:
            page_text = pytesseract.image_to_string(image)
            text += page_text + "\n"
    except:
        pass
    return text.strip()


# --- 2. DATA FETCHING & HELPER FUNCTIONS ---

def fetch_form_data(form_id: str):
    """Fetch form schema and responses for a given form_id."""
    print(f"Fetching schema and responses for form_id: {form_id}")
    form_data = supabase.table("forms").select("form").eq("form_id", form_id).single().execute()
    form_schema = form_data.data["form"]
    responses_data = supabase.table("responses").select("answers, profile_id").eq("form_id", form_id).execute()
    return form_schema, responses_data.data

def responses_to_dataframe(form_schema: dict, responses: list):
    """Convert responses JSON into a pandas DataFrame aligned with form schema, 
    plus selected resume column."""
    
    # Extract question titles from form schema
    question_titles = [q["title"] for q in form_schema.get("questions", [])]
    
    rows = []
    for r in responses:
        row = {}
        answers = r.get("answers", {})
        
        # Fill answers for schema-defined questions
        for title in question_titles:
            row[title] = answers.get(title, None)
        
        # Add resume column if present
        row["selected_resume"] = answers.get("selected_resume") or answers.get("Link to your Resume/CV")
        
        # Add profile_id
        row["profile_id"] = r.get("profile_id")
        
        rows.append(row)
    
    return pd.DataFrame(rows)
def sanitize_filename(filename):
    """Sanitizes a string to be a valid filename."""
    filename = unquote(filename)
    filename = re.sub(r'[<>:"/\\|?*]', '_', filename)
    filename = re.sub(r'\s+', ' ', filename).strip()
    return filename

def download_resume(url: str, destination: Path):
    """Downloads a file from a URL using a synchronous request."""
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        response = requests.get(url, headers=headers, stream=True, timeout=30)
        response.raise_for_status()
        with open(destination, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        return True
    except requests.exceptions.RequestException as e:
        print(f" -> Failed to download from URL {url}: {e}")
        return False

def extract_resume_urls(resume_data):
    """Robustly extracts resume URLs from different data formats."""
    if not resume_data: return []
    if isinstance(resume_data, list): return resume_data
    if isinstance(resume_data, str):
        if resume_data.startswith('[') and resume_data.endswith(']'):
            try: return json.loads(resume_data)
            except: return []
        elif resume_data.startswith('http'):
            return [resume_data]
    return []

def format_candidate_text(candidate_row):
    """Formats a DataFrame row of form answers into a readable text string for the API."""
    exclude_cols = {'id', 'profile_id', 'resume_url', 'first_name', 'last_name', 'created_at', 'original_index'}
    text_parts = [f"{col}: {val}" for col, val in candidate_row.items()
                  if col not in exclude_cols and pd.notna(val)]
    return "\n".join(text_parts)


def build_comprehensive_prompt(job_description, candidate_text_response, candidate_resume_text):
    """Builds the JSON prompt for the Gemini API."""
    return json.dumps({
        "task": "You are an expert recruiter. Evaluate the candidate's suitability for the job based on the job description, their text responses, AND their resume provided.",
        "job_description": job_description.strip(),
        "candidate_sources": {"text_response": candidate_text_response.strip(), "resume_text": candidate_resume_text.strip() or "No resume provided."},
        "evaluation_criteria": {"skills_match": "Scale 1-10", "experience_relevance": "Scale 1-10", "communication_clarity": "Scale 1-10", "overall_fit": "Scale 1-10"},
        "output_instructions": {"final_score": "Provide a final weighted relevancy score (0-10).", "justification": "Provide a concise justification.", "format": "Return ONLY valid JSON."},
        "output_format": {"skills_match": "int", "experience_relevance": "int", "communication_clarity": "int", "overall_fit": "int", "final_score": "int", "justification": "string"}
    }, indent=2)

async def evaluate_candidate_async(job_description, candidate_text, resume_text):
    prompt = build_comprehensive_prompt(job_description, candidate_text, resume_text)
    try:
        response = await openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"}
        )
        return json.loads(response.choices[0].message.content)
    except Exception as e:
        return {"final_score": 0, "justification": f"API/Parsing Error: {e}"}
# --- 4. THE ASYNCHRONOUS PIPELINE WORKER ---

async def process_candidate_pipeline(candidate_row, job_description, semaphore):
    """Runs the full evaluation pipeline for a single candidate's data row asynchronously."""
    async with semaphore:
        loop = asyncio.get_running_loop()
        print(f"Processing candidate row index: {candidate_row}")
        supabase_id = candidate_row.get('profile_id', 'unknown_id')
        # candidate_name = f"{candidate_row.get('first_name', 'Unk')} {candidate_row.get('last_name', '')}".strip()
        original_index = candidate_row.name

        print(f"Starting pipeline for: (ID: {supabase_id})")
        
        # --- Resume Text Extraction (with caching) ---
        all_resumes_text = []
        resume_urls = extract_resume_urls(candidate_row.get('selected_resume'))

        for i, url in enumerate(resume_urls):
            safe_filename = sanitize_filename(f"{supabase_id}_{i}.pdf")
            text_cache_path = TEXT_CACHE_DIR / f"{Path(safe_filename).stem}.txt"

            if text_cache_path.exists():

                all_resumes_text.append(text_cache_path.read_text(encoding="utf-8"))
            else:
                destination = RESUMES_DIR / safe_filename
                if not destination.exists():
                    # Run the synchronous download in a thread to avoid blocking the event loop
                    await loop.run_in_executor(None, functools.partial(download_resume, url, destination))
                    # Run the CPU-bound PDF extraction in a thread
                    resume_text = await loop.run_in_executor(None, extract_text_from_pdf, destination)
                    # print(f" -> Extracted {len(resume_text)} chars from resume for {candidate_name}")
                    # text_cache_path.write_text(resume_text, encoding="utf-8")
                    all_resumes_text.append(resume_text)
        
        final_resume_text = "\n\n--- END OF RESUME ---\n\n".join(all_resumes_text)
        print(f" -> Extracted resume text for  ({len(final_resume_text)} chars)")

        # Delete the downloaded resume to save space
        os.remove(destination)

        # --- Gemini API Call (natively async) ---
        candidate_text_response = format_candidate_text(candidate_row)
        # print(f" -> Evaluating {candidate_name} via Gemini API...")
        scores = await evaluate_candidate_async(job_description, candidate_text_response, final_resume_text)
        
        print(f"✅ Finished ")
        scores['original_index'] = original_index
        return scores


# ---------- Generate Form ----------
# def generate_form_questions_with_gemini(jd_text: str):
#     prompt = f"""
#     You are a hiring assistant. Based on the following job description, 
#     create a Google Form questionnaire for applicants.

#     Output JSON with this structure:
#     {{
#       "title": "Form title",
#       "questions": [
#         {{
#           "title": "Question?",
#           "type": "TEXT" or "RADIO",
#           "options": ["Option1", "Option2"]
#         }}
#       ]
#     }}
#     Rules:
#     - JSON only (no markdown, no text before/after).
#     - TEXT = no options.
#     - RADIO must have 2+ options.
#     - Do not ask for a CV or resume.

#     JD:
#     {jd_text}
#     """
#     response = model.generate_content(prompt)
#     raw_text = response.candidates[0].content.parts[0].text if response.candidates else response.text
#     match = re.search(r"\{.*\}", raw_text, re.DOTALL)
#     if not match:
#         raise HTTPException(status_code=500, detail="Gemini did not return JSON.")
#     return json.loads(match.group(0))
def generate_form_questions_with_gemini(jd_text: str):
    system_prompt = """
    You are "Founder’s Chief Hiring Strategist v2.0" — an expert in designing lean, signal-rich hiring forms for high-impact roles.

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

def create_form_with_questions(service, creds, form_data):
    form = service.forms().create(body={
        "info": {
            "title": form_data.get("title", "Job Application Form"),
            "documentTitle": form_data.get("title", "Job Application Form")
        }
    }).execute()
    form_id = form["formId"]
    form_link = form["responderUri"]

    requests_list = []
    for i, q in enumerate(form_data["questions"]):
        item = {
            "title": q["title"],
            "questionItem": {"question": {"required": True}}
        }
        if q["type"] == "RADIO":
            item["questionItem"]["question"]["choiceQuestion"] = {
                "type": "RADIO",
                "options": [{"value": opt} for opt in q["options"]],
                "shuffle": False
            }
        else:
            item["questionItem"]["question"]["textQuestion"] = {}
        requests_list.append({"createItem": {"item": item, "location": {"index": i}}})

    service.forms().batchUpdate(formId=form_id, body={"requests": requests_list}).execute()
    return form_id, form_link

# ============================================================
# Routes
# ============================================================

@app.post("/generate_form/{job_id}")
def generate_form(job_id: str, user=Depends(role_required(["admin", "client"]))):
    # user contains decoded token payload (e.g., user id, email)

    result = supabase.table("jobs").select("JD_pdf").eq("job_id", job_id).single().execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Job ID not found.")
    jd_pdf_url = result.data["JD_pdf"]
    if not jd_pdf_url:
        raise HTTPException(status_code=400, detail="No JD_pdf found for this job.")
    jd_text = extract_text_from_pdf_url(jd_pdf_url)
    form_data = generate_form_questions_with_gemini(jd_text)
    # form_id, form_link= create_form_with_questions(forms_service, creds, form_data)

    new_form_id = str(uuid.uuid4())  # generate a new UUID for form_id

    data = {
        "form_id": new_form_id,
        "job_id": job_id,
        "form": form_data  # must be a Python list/dict, Supabase client will handle JSONB
    }

    response = supabase.table("forms").insert(data).execute()
    # return response.data
    # supabase.table("jobs").update({
    #     "form_id": form_id,
    #     "form_link": form_link,
    # }).eq("id", job_id).execute()
    return {"status": "success", "form_id": new_form_id, "form": response.data}




@app.post("/evaluate/{job_id}")
async def evaluate_job(job_id: str, user=Depends(role_required(["admin", "client"]))):
    try:
        # --- Step A: Fetch Job & JD ---
        job_result = supabase.table("jobs").select("*").eq("job_id", job_id).single().execute()
        # print(job_result)
        if not job_result.data:
            raise HTTPException(status_code=404, detail="Job not found")
        job = job_result.data

        jd_url = job.get("JD_pdf")
        if not jd_url:
            raise HTTPException(status_code=400, detail="No JD PDF found for this job.")

        # Download JD PDF and extract text
        r = requests.get(jd_url)
        if r.status_code != 200:
            raise HTTPException(status_code=400, detail="Failed to download JD PDF")
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            tmp.write(r.content)
            jd_path = tmp.name
        job_description = extract_text_from_pdf(jd_path)
        os.remove(jd_path)
        if not job_description:
            raise HTTPException(status_code=500, detail="Could not extract JD text")

        # --- Step B: Get form_id for this job ---
        form_result = supabase.table("forms").select("form_id").eq("job_id", job_id).single().execute()
        if not form_result.data:
            raise HTTPException(status_code=404, detail="Form not found for this job")
        form_id = form_result.data["form_id"]

        # --- Step C: Fetch responses & profiles ---
        form_schema, responses = fetch_form_data(form_id)

        responses_df = responses_to_dataframe(form_schema, responses)

        applied_profile_ids = responses_df["profile_id"].dropna().unique().tolist()

        # Fetch only those profiles
        profiles_response = supabase.table("profiles").select("*").in_("id", applied_profile_ids).execute()
        # print(f"Fetched {(profiles_response.data)} profiles from Supabase.")
        profiles_df = pd.DataFrame(profiles_response.data)
        # print(f"Fetched {(profiles_df)} profiles from Supabase.")

        # profiles_response = supabase.table("profiles").select("*").execute()

        # profiles_df = pd.DataFrame(profiles_response.data)

        # combined_df = pd.merge(profiles_df, responses_df, left_on="id", right_on="profile_id", how="inner")
        combined_df =responses_df
        print(f"Merged DataFrame shape: {combined_df}")

        # --- Step D: Run evaluation pipelines ---
        semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)
        tasks = [process_candidate_pipeline(row, job_description, semaphore) for _, row in combined_df.iterrows()]
        api_results = await asyncio.gather(*tasks, return_exceptions=True)

        successful_results = [r for r in api_results if isinstance(r, dict)]
        if not successful_results:
            raise HTTPException(status_code=500, detail="No successful evaluation results")

        results_df = pd.DataFrame(successful_results).set_index("original_index")
        print(f"Results DataFrame shape: {results_df}")
        final_df = combined_df.join(results_df)
        # --- Step E: Save to evaluations table ---
        # --- Step E: Save to evaluations table ---
        all_evaluations = []

        for _, row in final_df.iterrows():
            if pd.notna(row.get("final_score")):
                profile_eval = {
                    "profile_id": row.get("profile_id"),
                    "results": {
                        "skills_match": row.get("skills_match"),
                        "experience_relevance": row.get("experience_relevance"),
                        "communication_clarity": row.get("communication_clarity"),
                        "overall_fit": row.get("overall_fit"),
                        "final_score": row.get("final_score"),
                        "justification": row.get("justification"),
                    },
                }
                all_evaluations.append(profile_eval)

        # Save a single row with job_id and all evaluations
        if all_evaluations:
            record = {
                "job_id": job_id,
                "results": all_evaluations  
            }
            supabase.table("evaluations").insert(record).execute()

        return {"job_id": job_id, "results": all_evaluations}


    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

