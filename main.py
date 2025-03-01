import asyncio
import base64
import json
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
import httplib2
from pydantic import BaseModel
import os
import datetime
from dateutil import relativedelta
from googleapiclient.http import BatchHttpRequest
from langchain.output_parsers.json import SimpleJsonOutputParser
import redis
import html2text
import re
from langchain_ollama import ChatOllama
from langchain_core.prompts import ChatPromptTemplate

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Replace with your actual credentials
CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
REFRESH_TOKEN = os.getenv("REFRESH_TOKEN")
REDIS_URI= os.getenv("REDIS_URI")
REDIRECT_URI = "http://localhost:8000/callback"
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
redis_client = redis.from_url(REDIS_URI)

class AuthCode(BaseModel):
    code: str

class TokenInput(BaseModel):
    token: str

@app.get("/auth-url")
async def get_auth_url():
    try:
        flow = InstalledAppFlow.from_client_config(
            {
                "web": {
                    "client_id": CLIENT_ID,
                    "client_secret": CLIENT_SECRET,
                    "redirect_uris": [REDIRECT_URI],
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                }
            },
            scopes=SCOPES
        )

        flow.redirect_uri = REDIRECT_URI
        auth_url, _ = flow.authorization_url(
            prompt="consent",
            access_type="offline",
            include_granted_scopes="false",
        )
        return {"auth_url": auth_url}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/callback")
async def get_gmail_token(code: str = Query(...)):
    try:
        flow = InstalledAppFlow.from_client_config(
            {
                "web": {
                    "client_id": CLIENT_ID,
                    "client_secret": CLIENT_SECRET,
                    "redirect_uris": [REDIRECT_URI],
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                }
            },
            scopes=SCOPES
        )

        flow.redirect_uri = REDIRECT_URI
        flow.fetch_token(code=code)
        credentials = flow.credentials

        return {
            "access_token": credentials.token,
            "refresh_token": credentials.refresh_token,
            "expires_in": credentials.expiry
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/emails")
async def get_emails():
    try:
        credentials = Credentials(
            token=ACCESS_TOKEN,
            refresh_token=REFRESH_TOKEN,
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            token_uri="https://oauth2.googleapis.com/token",
            scopes=SCOPES
        )

        service = build('gmail', 'v1', credentials=credentials)
        
        three_months_ago = datetime.datetime.now() - relativedelta.relativedelta(months=3)
        query = "after:2024/11/01 before:2025/02/01"

        # Fetch message IDs with pagination
        email_ids = []
        page_token = None
        max_emails = 1000  # Total limit you want

        while len(email_ids) < max_emails:
            results = service.users().messages().list(
                userId='me',
                q=query,
                maxResults=min(500, max_emails - len(email_ids)),  # Max 500 per call
                pageToken=page_token,
                fields='messages(id),nextPageToken'  # Only fetch IDs and token
            ).execute()

            messages = results.get('messages', [])
            if not messages:
                break

            # Collect IDs
            email_ids.extend([msg['id'] for msg in messages])
            page_token = results.get('nextPageToken')
            if not page_token:  # No more pages
                break

        if not email_ids:
            return {"emails": [], "total": 0, "period": "Last 3 months"}

        # Store IDs in Redis as a single set (key: "user:emails:3months")
        redis_key = "user:emails:3months"
        redis_client.delete(redis_key)  # Clear old data
        redis_client.sadd(redis_key, *email_ids)  # Bulk store IDs
        redis_client.expire(redis_key, 3600)  # TTL of 1 hour

        return {
            "emails": [],  # No details yet, just IDs stored
            "total": len(email_ids),
            "period": "Last 3 months",
            "message": f"Stored {len(email_ids)} email IDs in Redis"
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# Endpoint to fetch full email later
@app.get("/emails/details")
async def get_email_details(limit: int = 100):
    try:
        credentials = Credentials(
            token=ACCESS_TOKEN,
            refresh_token=REFRESH_TOKEN,
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            token_uri="https://oauth2.googleapis.com/token",
            scopes=SCOPES
        )

        service = build('gmail', 'v1', credentials=credentials)
        redis_key = "user:emails:3months"
        
        # Fetch IDs from Redis
        email_ids = redis_client.smembers(redis_key)
        if not email_ids:
            return {"emails": [], "total": 0, "message": "No IDs stored"}

        email_ids = list(email_ids) # Limit to requested number

        result = {
            "application_submitted": 0,
            "application_rejected": 0,
            "application_viewed": 0,
            "assignment_given": 0,
            "interview_scheduled": 0,
            "interview_rejected": 0,
            "offer_letter_received": 0,
            "offer_released": 0,
            "not_job_related": 0
        }

        def extract_text_from_payload(payload):
            body = ""
            if 'parts' in payload:  # Multipart email
                for part in payload['parts']:
                    mime_type = part.get('mimeType', '')
                    if mime_type == 'text/plain':
                        data = part['body'].get('data')
                        if data:
                            body = base64.urlsafe_b64decode(data).decode('utf-8', errors='ignore')
                            break
                    elif mime_type == 'text/html' and not body:
                        data = part['body'].get('data')
                        if data:
                            html_content = base64.urlsafe_b64decode(data).decode('utf-8', errors='ignore')
                            body = html2text.html2text(html_content)  # Convert HTML to plain text
                    elif mime_type.startswith('multipart'):
                        nested_body = extract_text_from_payload(part)
                        if nested_body:
                            body = nested_body
                            break
            else:  # Simple email (no parts)
                data = payload.get('body', {}).get('data')
                if data:
                    body = base64.urlsafe_b64decode(data).decode('utf-8', errors='ignore')
            
            # Clean the text
            if body:
                body = re.sub(r'\s+', ' ', body).strip()  # Normalize whitespace
            return body

        # Async function to fetch full email content
        async def fetch_email(email_id):
            try:
                msg = service.users().messages().get(
                    userId='me',
                    id=email_id.decode('utf-8'),  # Redis returns bytes
                    format='full'  # Get full content
                ).execute()

                # Extract subject from headers
                subject = ""
                headers = msg.get('payload', {}).get('headers', [])
                for header in headers:
                    if header['name'] == 'Subject':
                        subject = header['value']
                        break

                # Extract body
                payload = msg.get('payload', {})
                body = extract_text_from_payload(payload)

                # Combine subject and body into content
                content = f"Subject: {subject}\nBody: {body}" if subject and body else body or subject

                llm = ChatOllama(
                    model="phi3",
                    temperature=0,
                )
                json_parser = SimpleJsonOutputParser()

                system_prompt = """
        Analyze the provided email content and return a JSON object with a `category` key that best matches the email. 

        ### **Categories & Descriptions**  
        - **`application_submitted`** → The candidate has successfully applied for a job.  
        *Example: "Thank you for your application to [Company Name]. We have received your resume and will review it soon."*  
        - **`application_viewed`** → The employer has reviewed the application but has not responded yet.  
        *Example: "Your application for [Job Title] has been viewed by the hiring manager."*  
        - **`application_rejected`** → The job application was rejected before any interview.  
        *Example: "We appreciate your interest, but we have decided to move forward with other candidates at this time."*  
        - **`assignment_given`** → The candidate has been given a task or test.  
        *Example: "Please complete the attached coding challenge and submit it within the next 48 hours."*  
        - **`interview_scheduled`** → The candidate has been invited for an interview.  
        *Example: "We would like to schedule an interview with you for the [Job Title] position on [Date]."*  
        - **`interview_rejected`** → The candidate was rejected after an interview.  
        *Example: "Thank you for interviewing with us. Unfortunately, we have decided to move forward with another candidate."*  
        - **`offer_letter_received`** → The candidate has received a job offer.  
        *Example: "We are pleased to offer you the position of [Job Title] at [Company Name]. Please find the offer letter attached."*  
        - **`offer_released`** → The offer has been finalized and the candidate is officially hired.  
        *Example: "Your employment contract has been signed, and we look forward to your joining date on [Date]."*  
        - **`not_job_related`** → The email does not belong to any of the above categories.  
        *Example: Spam emails, newsletters, promotional messages, or personal emails.*  

        ### **Instructions**  
        - Return only a JSON object in this format: `{{"category": "<selected_category>"}}`  
        - Do **not** add explanations or extra text.  
        - Select the most relevant category based on the email content.  

        ### **Email Content:**  
        {content}
        """

                prompt_template = ChatPromptTemplate([
                    ("system", system_prompt)
                ])

                chain = prompt_template | llm | json_parser
                ai_msg = chain.invoke({"content": content[:1000]})  # ai_msg is now a dict like {"category": "..."}
                
                # Directly access the 'category' key from the parsed dictionary
                category = ai_msg.get("category", "not_job_related")  # Default to "not_job_related" if key is missing

                print(f"email_content: {content[:1000]}")
                print(f"Classified as: {category}")

                # Update result counter
                if category in result:
                    result[category] += 1
                else:
                    print(f"Invalid category returned: {category}, defaulting to not_job_related")
                    result["not_job_related"] += 1
                    category = "not_job_related"

                # Store job-related emails in Redis with content
                if category != "not_job_related":
                    redis_client.setex(msg['id'], 3600, json.dumps({"content": content, "category": category}))

                return content

            except Exception as e:
                print(f"Error fetching {email_id}: {e}")
                return None

        # Run parallel fetches
        tasks = [fetch_email(email_id) for email_id in email_ids]
        content_list = await asyncio.gather(*tasks)
        content_list = [content for content in content_list if content is not None]  # Filter out failures

        # Filter job-related content (based on what was stored in Redis or result)
        job_related_content = [content for content in content_list if "not_job_related" not in result or result["not_job_related"] < len(content_list)]

        return {
            "content_list": job_related_content,  # List of "Subject: ... Body: ..." strings
            "total_job_related": len(job_related_content),
            "total_processed": len(content_list),
            "period": "Last 3 months",
            "categories": result
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/agent")
async def testagent():
    try:
        llm = ChatOllama(
                model="llama3.2:1b",
                temperature=0,
            )

        prompt = ChatPromptTemplate([
            ("system", "you are a agent ")
        ])

        print("s")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

