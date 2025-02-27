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
import redis
import google_auth_httplib2  # New import
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

        # Async function to fetch full email content
        async def fetch_email(email_id):
            try:
                msg = service.users().messages().get(
                    userId='me',
                    id=email_id.decode('utf-8'),  # Redis returns bytes
                    format='full'  # Get full content
                ).execute()

                email_info = {
                    'id': msg['id'],
                    'snippet': msg.get('snippet', '')
                }

                # Extract headers
                headers = msg.get('payload', {}).get('headers', [])
                for header in headers:
                    if header['name'] == 'From':
                        email_info['from'] = header['value']
                    elif header['name'] == 'Subject':
                        email_info['subject'] = header['value']
                    elif header['name'] == 'Date':
                        email_info['date'] = header['value']

                # Extract full text content
                body = ""
                payload = msg.get('payload', {})
                if 'parts' in payload:  # Multipart email
                    for part in payload['parts']:
                        if part['mimeType'] == 'text/plain':
                            data = part['body'].get('data')
                            if data:
                                body = base64.urlsafe_b64decode(data).decode('utf-8')
                                break
                        elif part['mimeType'] == 'text/html' and not body:  # Fallback to HTML
                            data = part['body'].get('data')
                            if data:
                                body = base64.urlsafe_b64decode(data).decode('utf-8')
                else:  # Simple text email
                    data = payload.get('body', {}).get('data')
                    if data:
                        body = base64.urlsafe_b64decode(data).decode('utf-8')

                email_info['body'] = body

                llm = ChatOllama(
                    model="llama3.2:1b",
                    temperature=0,
                    # other params...
                )


                improved_prompt = """You are a highly accurate email classification assistant focused specifically on DIRECT job application communications. 
Your task is to analyze the given email content and categorize it into one of the predefined categories.

First, determine if the email is directly related to a specific job application process you've personally initiated. 
Look for personalized communications about YOUR specific applications, interviews, and offers.

IMPORTANT: Classify as "not_job_related" if the email is:
- A job alert (notifications about new job postings)
- A promotional email from job sites
- A newsletter from a company
- A generic recruitment message not tied to your specific application
- Any email that is not about a specific job you've applied for

If the email is NOT directly related to a specific job application YOU submitted, respond with:
not_job_related

If the email IS directly related to YOUR specific job application, categorize it into ONE of these categories:
- application_submitted: Confirmation that your job application was received or submitted
- application_rejected: Rejection at the application stage before interviews
- assignment_given: Request to complete a technical assessment, coding challenge, or assignment
- interview_scheduled: Invitation to an interview or confirmation of interview details
- interview_rejected: Rejection after an interview stage
- offer_letter_received: Job offer or notification that an offer letter is available
- offer_released: Notification that a job offer is no longer available or has expired

Return ONLY the category name without any additional text or explanation.

Examples:

Example 1:
Subject: Your application to Software Developer at TechCorp
Body: Thank you for submitting your application to TechCorp. We have received your resume and will review it shortly.
RETURN: application_submitted

Example 2:
Subject: New jobs that match your profile: 5 Software Developer roles
Body: We found 5 new job postings that match your preferences. Click here to view them.
RETURN: not_job_related

Example 2:
Subject: Your application was viewed by Wise AI
Body: Your application was viewed by Wise AI
RETURN: not_job_related

Example 3:
Subject: Next steps - Technical Assessment for Senior Engineer position
Body: We were impressed with your application and would like you to complete a coding challenge. Please find attached the requirements and submit within 5 days.
RETURN: assignment_given

Example 4:
Subject: Weekly job alerts from Indeed
Body: Here are this week's top job recommendations based on your profile and search history.
RETURN: not_job_related

Example 5:
Subject: Special offer: Upgrade to Premium for more job applications
Body: Upgrade your account to apply to unlimited jobs and get seen by more recruiters!
RETURN: not_job_related"""

                prompt_template = ChatPromptTemplate([
                    ("system", improved_prompt),
                    ("user", "Here is the email content:\n\nSubject: {subject}\nFrom: {from}\nBody: {body}")
                ])

                chain = prompt_template | llm
                ai_msg = chain.invoke({
                    "subject": email_info.get("subject", ""),
                    "from": email_info.get("from", ""),
                    "body": email_info.get("body", "")[:1000]  # Limit to first 1000 chars for efficiency
                })
                
                category = ai_msg.content.strip()
                print(f"email_content {email_info.get("body", "")[:1000]}")
                print(f"Classified as: {category}")

                # Update result counter if category is valid
                if category in result:
                    result[category] += 1
                else:
                    # Default to not_job_related if invalid category returned
                    print(f"Invalid category returned: {category}, defaulting to not_job_related")
                    result["not_job_related"] += 1
                    category = "not_job_related"
                
                # Add classification to email_info
                email_info["category"] = category
                
                # Only store job-related emails in Redis
                if category != "not_job_related":
                    redis_client.setex(msg['id'], 3600, json.dumps(email_info))
                
                return email_info
            except Exception as e:
                print(f"Error fetching {email_id}: {e}")
                return None

        # Run parallel fetches
        tasks = [fetch_email(email_id) for email_id in email_ids]
        email_list = await asyncio.gather(*tasks)
        email_list = [email for email in email_list if email is not None]  # Filter out failures
        
        # Filter to only include job-related emails in response
        job_related_emails = [email for email in email_list if email.get("category") != "not_job_related"]

        return {
            "total_job_related": len(job_related_emails),
            "total_processed": len(email_list),
            "period": "Last 3 months",
            "categories": result  # Include the category counts in the response
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

