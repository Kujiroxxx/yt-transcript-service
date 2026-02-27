from fastapi import FastAPI, Query, HTTPException
from pydantic import BaseModel
from typing import List, Optional
import re
import os
import glob
import subprocess
from pathlib import Path

app = FastAPI(title="YT Transcript Service", version="2.0.0")

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

def vtt_to_text(vtt_content: str) -> str:
    lines = vtt_content.splitlines()
    out = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        # пропускаем заголовки и таймкоды
        if line.startswith("WEBVTT"):
            continue
        if "-->" in line:
            continue
        # пропускаем служебные теги/комментарии
        if line.startswith("NOTE") or line.startswith("STYLE") or line.startswith("REGION"):
            continue
        # убираем простые html-теги
        line = re.sub(r"<[^>]+>", "", line).strip()
        if not line:
            continue
        out.append(line)
    # убираем повторы соседних строк (иногда vtt дублирует)
    cleaned = []
    prev = None
    for t in out:
        if t != prev:
            cleaned.append(t)
        prev = t
    return " ".join(cleaned).strip()

def fetch_subtitles_with_ytdlp(url: str, lang: Optional[str]) -> tuple[str, Optional[str]]:
    """
    Возвращает (text, language_used) или кидает исключение.
    """
    video_id = extract_video_id(url)
    workdir = "/tmp/yt"
    os.makedirs(workdir, exist_ok=True)

    # шаблон имён файлов
    outtmpl = os.path.join(workdir, f"{video_id}.%(ext)s")

    # выбираем языки: если указан lang, пробуем его; иначе пробуем набор наиболее вероятных
    # можно расширить список под себя
    lang_list = [lang] if lang else ["ru", "en", "de", "uk"]

    last_err = None

    for l in lang_list:
        # Сначала пробуем обычные субтитры (--write-subs), потом авто (--write-auto-subs)
        for auto in [False, True]:
            # чистим старые файлы для этого video_id
            for f in glob.glob(os.path.join(workdir, f"{video_id}*")):
                try:
                    os.remove(f)
                except:
                    pass

                       cookies_path = os.getenv("YT_COOKIES_PATH", "/etc/secrets/cookies.txt")
            use_cookies = Path(cookies_path).exists()

            args = ["yt-dlp"]

            if use_cookies:
                args += ["--cookies", cookies_path]

            args += [
                "--skip-download",
                "--no-warnings",
                "--write-subs" if not auto else "--write-auto-subs",
                "--sub-langs", l,
                "--sub-format", "vtt",
                "--output", outtmpl,
                url,
            ]

            try:
                proc = subprocess.run(
                    args,
                    capture_output=True,
                    text=True,
                    timeout=60
                )
                if proc.returncode != 0:
                    last_err = (proc.stderr or proc.stdout or "").strip()
                    continue

                # yt-dlp обычно создаёт файл вида: <id>.<lang>.vtt или <id>.<lang>-orig.vtt и т.п.
                candidates = glob.glob(os.path.join(workdir, f"{video_id}*.vtt"))
                if not candidates:
                    last_err = "yt-dlp finished but .vtt not found"
                    continue

                # берём самый свежий/короткий путь
                path = sorted(candidates, key=lambda p: os.path.getmtime(p), reverse=True)[0]
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()

                text = vtt_to_text(content)
                if not text:
                    last_err = "Subtitles file exists but parsed text is empty"
                    continue

                return text, l

            except subprocess.TimeoutExpired:
                last_err = "yt-dlp timeout"
                continue
            except Exception as e:
                last_err = f"yt-dlp error: {e}"
                continue

    raise RuntimeError(last_err or "Unable to fetch subtitles via yt-dlp")

@app.get("/transcript", response_model=TranscriptResponse)
def get_transcript(
    url: str = Query(..., description="YouTube URL"),
    lang: Optional[str] = Query(None, description="Preferred language code, e.g. en, ru, de")
):
    try:
        video_id = extract_video_id(url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # 1) Основной путь: yt-dlp (устойчивее к 429)
    try:
        text, used_lang = fetch_subtitles_with_ytdlp(url, lang)
        # У нас нет точной сегментации по таймкодам на этом уровне — вернём один сегмент
        segments = [Segment(start=0.0, duration=0.0, text=text)]
        return TranscriptResponse(video_id=video_id, language=used_lang, text=text, segments=segments)
    except Exception as e:
        # 2) Если совсем не получилось — говорим честно, чтобы GPT попросил транскрипт у пользователя
        # 429 у YouTube часто всплывает тут тоже (реже, но бывает)
        msg = str(e)
        if "429" in msg or "Too Many Requests" in msg:
            raise HTTPException(status_code=429, detail="YouTube rate limited requests (429). Try again later.")
        raise HTTPException(status_code=404, detail=f"Transcript not available via yt-dlp. Details: {msg}")

@app.get("/health")
def health():
    cookies_path = os.getenv("YT_COOKIES_PATH", "/etc/secrets/cookies.txt")
    p = Path(cookies_path)
    return {
        "status": "ok",
        "cookies_path": cookies_path,
        "cookies_exists": p.exists(),
        "cookies_size": p.stat().st_size if p.exists() else 0,
        "cookies_readable": os.access(cookies_path, os.R_OK) if p.exists() else False,
    }
