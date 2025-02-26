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
        query = f"after:{three_months_ago.strftime('%Y/%m/%d')}"

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

        email_ids = list(email_ids)[:limit]  # Limit to requested number

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

                # Store in Redis
                redis_client.setex(msg['id'], 3600, json.dumps(email_info))
                return email_info
            except Exception as e:
                print(f"Error fetching {email_id}: {e}")
                return None

        # Run parallel fetches
        tasks = [fetch_email(email_id) for email_id in email_ids]
        email_list = await asyncio.gather(*tasks)
        email_list = [email for email in email_list if email is not None]  # Filter out failures

        return {
            "emails": email_list,
            "total": len(email_list),
            "period": "Last 3 months"
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)