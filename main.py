from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials
from pydantic import BaseModel
import os
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", ""],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Replace with your actual credentials
CLIENT_ID =  os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
REDIRECT_URI = os.getenv("REDIRECT_URI")
SCOPES = ["https://mail.google.com/",
				"https://www.googleapis.com/auth/gmail.compose",
				"https://www.googleapis.com/auth/gmail.send",
				"https://www.googleapis.com/auth/gmail.readonly",]

class AuthCode(BaseModel):
    code: str

@app.get("/auth-url")
async def get_auth_url():
    try:
        flow = InstalledAppFlow.from_client_config(
            {
                "installed": {
                    "client_id": CLIENT_ID,
                    "client_secret": CLIENT_SECRET,
                    "redirect_uris": [REDIRECT_URI],
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                }
            },
            scopes=SCOPES
        )
        auth_url, _ = flow.authorization_url(prompt="consent")
        return {"auth_url": auth_url}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/callback")
async def get_gmail_token(auth: AuthCode):
    try:
        flow = InstalledAppFlow.from_client_config(
            {
                "installed": {
                    "client_id": CLIENT_ID,
                    "client_secret": CLIENT_SECRET,
                    "redirect_uris": [REDIRECT_URI],
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                }
            },
            scopes=SCOPES
        )
        flow.fetch_token(code=auth.code)
        credentials = flow.credentials
        return {
            "access_token": credentials.token,
            "refresh_token": credentials.refresh_token,
            "expires_in": credentials.expiry
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/job")
async def getgmailtoken():
    try:
        print("console log")
        return {"message": "Job endpoint hit"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))