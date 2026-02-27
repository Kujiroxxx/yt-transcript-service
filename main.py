from fastapi import FastAPI, Query, HTTPException
from pydantic import BaseModel
from typing import List, Optional
import re

from youtube_transcript_api import YouTubeTranscriptApi, TranscriptsDisabled, NoTranscriptFound

app = FastAPI(title="YT Transcript Service", version="1.0.0")

class Segment(BaseModel):
    start: float
    duration: float
    text: str

class TranscriptResponse(BaseModel):
    video_id: str
    language: Optional[str] = None
    text: str
    segments: List[Segment]

def extract_video_id(url: str) -> str:
    patterns = [
        r"v=([a-zA-Z0-9_-]{6,})",
        r"youtu\.be/([a-zA-Z0-9_-]{6,})",
        r"youtube\.com/shorts/([a-zA-Z0-9_-]{6,})",
    ]
    for p in patterns:
        m = re.search(p, url)
        if m:
            return m.group(1)
    raise ValueError("Cannot extract video_id from url")

@app.get("/transcript", response_model=TranscriptResponse)
def get_transcript(
    url: str = Query(..., description="YouTube URL"),
    lang: Optional[str] = Query(None, description="Preferred language code, e.g. en, ru, de")
):
    try:
        video_id = extract_video_id(url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    try:
        if lang:
            transcript = YouTubeTranscriptApi.get_transcript(video_id, languages=[lang])
            language = lang
        else:
            transcript = YouTubeTranscriptApi.get_transcript(video_id)
            language = None

        segments = [Segment(start=s["start"], duration=s["duration"], text=s["text"]) for s in transcript]
        full_text = " ".join([s.text.strip() for s in segments]).strip()

        return TranscriptResponse(
            video_id=video_id,
            language=language,
            text=full_text,
            segments=segments
        )

    except (TranscriptsDisabled, NoTranscriptFound):
        raise HTTPException(status_code=404, detail="Transcript not available (disabled, missing, or restricted).")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {e}")
