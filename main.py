import asyncio
import base64
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from pydantic import BaseModel
import os
import datetime
from dateutil import relativedelta
from googleapiclient.http import BatchHttpRequest
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
REDIRECT_URI = "http://localhost:8000/callback"
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

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

        # Reduce maxResults to something reasonable (e.g., 100)
        results = service.users().messages().list(
            userId='me',
            q=query,
            maxResults=100  # Adjust based on your needs
        ).execute()

        messages = results.get('messages', [])
        if not messages:
            return {"emails": [], "total": 0, "period": "Last 3 months"}

        email_list = []
        
        # Batch request setup
        batch = BatchHttpRequest()
        email_dict = {}  # To store results by message ID

        def callback(request_id, response, exception):
            if exception:
                print(f"Error for {request_id}: {exception}")
                return
            
            msg = response
            email_info = {
                'id': request_id,
                'snippet': msg.get('snippet', '')
            }

            # Headers
            headers = msg['payload'].get('headers', [])
            for header in headers:
                if header['name'] == 'From':
                    email_info['from'] = header['value']
                elif header['name'] == 'Subject':
                    email_info['subject'] = header['value']
                elif header['name'] == 'Date':
                    email_info['date'] = header['value']

            # Get body (simplified to first text/plain part)
            body = ""
            parts = msg['payload'].get('parts', [])
            if parts:
                for part in parts:
                    if part['mimeType'] == 'text/plain':
                        data = part['body'].get('data')
                        if data:
                            body = base64.urlsafe_b64decode(data).decode('utf-8')
                            break
            else:
                data = msg['payload']['body'].get('data')
                if data:
                    body = base64.urlsafe_b64decode(data).decode('utf-8')

            email_info['body'] = body
            email_dict[request_id] = email_info

        # Add requests to batch
        for message in messages:
            batch.add(
                service.users().messages().get(
                    userId='me',
                    id=message['id'],
                    format='full'
                ),
                callback=callback,
                request_id=message['id']
            )

        # Execute batch (one API call instead of one per email)
        await asyncio.get_event_loop().run_in_executor(None, batch.execute)

        # Collect results
        email_list = list(email_dict.values())

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