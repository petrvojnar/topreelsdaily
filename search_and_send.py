import os
import json
import re
import glob
import datetime
import smtplib
import subprocess
import tempfile
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from googleapiclient.discovery import build
from youtube_transcript_api import YouTubeTranscriptApi
from deep_translator import GoogleTranslator
import google.generativeai as genai

YOUTUBE_API_KEY = os.environ["YOUTUBE_API_KEY"]
EMAIL_SENDER    = os.environ["EMAIL_SENDER"]
EMAIL_PASSWORD  = os.environ["EMAIL_PASSWORD"]
EMAIL_RECIPIENT = "petrvojnar@seznam.cz"
SEEN_FILE       = "seen_videos.json"

QUERIES = [
    "communication skills shorts",
    "presentation skills shorts",
    "public speaking tips shorts",
    "body language confidence shorts",
    "storytelling skills shorts",
    "komunikační dovednosti shorts",
    "prezentační dovednosti shorts",
    "jak mluvit přesvědčivě shorts",
    "řeč těla komunikace shorts",
    "jak zaujmout publikum shorts",
]

# ── helpers ───────────────────────────────────────────────────────────────

def load_seen() -> set:
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, encoding="utf-8") as f:
            return set(json.load(f))
    return set()

def save_seen(seen: set) -> None:
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(seen), f, ensure_ascii=False, indent=2)

# ── YouTube search & stats ────────────────────────────────────────────────

def search_videos(youtube, query: str, max_results: int = 50) -> list[str]:
    try:
        resp = youtube.search().list(
            part="snippet",
            q=query,
            type="video",
            videoDuration="short",
            order="relevance",
            maxResults=max_results,
        ).execute()
        return [item["id"]["videoId"] for item in resp.get("items", [])]
    except Exception as e:
        print(f"  Search error '{query}': {e}")
        return []

def get_stats(youtube, video_ids: list[str]) -> dict:
    result = {}
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i : i + 50]
        try:
            resp = youtube.videos().list(
                part="statistics,snippet",
                id=",".join(batch),
            ).execute()
            for item in resp.get("items", []):
                vid_id  = item["id"]
                stats   = item["statistics"]
                snippet = item["snippet"]
                views   = int(stats.get("viewCount", 0))
                likes   = int(stats.get("likeCount", 0))
                engagement = (likes / views * 100) if views > 0 else 0
                result[vid_id] = {
                    "title":       snippet["title"],
                    "channel":     snippet["channelTitle"],
                    "published":   snippet["publishedAt"][:10],
                    "description": snippet.get("description", "")[:400],
                    "views":       views,
                    "likes":       likes,
                    "engagement":  engagement,
                    "url":         f"https://www.youtube.com/watch?v={vid_id}",
                    "shorts_url":  f"https://www.youtube.com/shorts/{vid_id}",
                }
        except Exception as e:
            print(f"  Stats error: {e}")
    return result

# ── transcript extraction ─────────────────────────────────────────────────

def parse_vtt(content: str) -> str:
    """Extract clean text from WebVTT subtitle file."""
    texts: list[str] = []
    for line in content.split("\n"):
        line = line.strip()
        if (not line or "WEBVTT" in line or "-->" in line
                or re.match(r"^\d+$", line) or line.startswith("NOTE")):
            continue
        clean = re.sub(r"<[^>]+>", "", line).strip()
        if clean and (not texts or clean != texts[-1]):
            texts.append(clean)
    return " ".join(texts)

def get_captions_api(video_id: str) -> tuple[str | None, str | None]:
    """Try youtube-transcript-api."""
    try:
        tlist = YouTubeTranscriptApi.list_transcripts(video_id)
        # prefer manual → then generated, prefer cs/en/sk
        for prefer_generated in (False, True):
            for t in tlist:
                if t.is_generated != prefer_generated:
                    continue
                try:
                    items = t.fetch()
                    text = " ".join(i["text"] for i in items)
                    print(f"  [API] captions OK — lang={t.language_code} generated={t.is_generated}")
                    return text, t.language_code
                except Exception:
                    continue
    except Exception as e:
        print(f"  [API] captions failed: {e}")
    return None, None

def get_captions_ytdlp(video_id: str) -> tuple[str | None, str | None]:
    """Try yt-dlp subtitle download (no video)."""
    url = f"https://www.youtube.com/watch?v={video_id}"
    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            subprocess.run(
                [
                    "yt-dlp",
                    "--skip-download",
                    "--write-auto-sub",
                    "--write-sub",
                    "--sub-lang", "cs,en,sk",
                    "--sub-format", "vtt",
                    "--output", os.path.join(tmpdir, "%(id)s"),
                    "--quiet",
                    url,
                ],
                capture_output=True, text=True, timeout=60,
            )
            vtt_files = glob.glob(os.path.join(tmpdir, "*.vtt"))
            if not vtt_files:
                print(f"  [yt-dlp] no subtitle files found")
                return None, None
            vtt_path = vtt_files[0]
            lang = "cs" if ".cs." in vtt_path else "en"
            with open(vtt_path, encoding="utf-8") as f:
                text = parse_vtt(f.read())
            if text:
                print(f"  [yt-dlp] captions OK — lang={lang}, {len(text)} chars")
                return text, lang
        except Exception as e:
            print(f"  [yt-dlp] error: {e}")
    return None, None

def get_transcript(video_id: str) -> tuple[str | None, str | None]:
    """Try all transcript sources in order."""
    text, lang = get_captions_api(video_id)
    if text:
        return text, lang
    return get_captions_ytdlp(video_id)

# ── Gemini analysis ───────────────────────────────────────────────────────

REELS_STRUCTURE = """
### 2. UPRAVENÝ TEXT
Přepiš do čtivé, smysluplné češtiny bez přeřeknutí a opakování. Maximálně 200 slov.

---

### 3. INSTAGRAM REELS SKRIPT
Vytvoř virální skript s přesnou strukturou:

🔥 HOOK (1–3 sekundy):
[Jedna silná věta která okamžitě zastaví scrollování — překvapení, provokace nebo otázka]

💡 OBSAH:
[3–5 krátkých úderných bodů z videa — každý max 1–2 věty]

📲 VÝZVA K AKCI:
[Jedna věta — Sleduj / Ulož / Pošli příteli / Napiš do komentářů]

#️⃣ HASHTAGY:
[12–15 hashtagů — mix českých a anglických]
"""

def gemini_text(prompt: str) -> str | None:
    """Call Gemini with a text-only prompt."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("  No GEMINI_API_KEY.")
        return None
    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel("gemini-1.5-flash")
        resp = model.generate_content(prompt)
        print(f"  Gemini OK ({len(resp.text)} chars)")
        return resp.text.strip()
    except Exception as e:
        print(f"  Gemini error: {e}")
        return None

def analyze_from_transcript(raw_text: str, lang: str) -> str:
    """Translate + clean + create Reels script from existing transcript."""
    # Translate to Czech if needed
    if lang != "cs":
        try:
            cs_text = GoogleTranslator(source="auto", target="cs").translate(raw_text[:4500])
            print(f"  Translated from {lang} to cs.")
        except Exception as e:
            print(f"  Translation error: {e}")
            cs_text = raw_text[:4500]
    else:
        cs_text = raw_text[:4500]

    prompt = (
        f"Zde je přepis videa o komunikaci/prezentaci (v češtině):\n\n"
        f"{cs_text[:3500]}\n\n"
        f"Nejprve uveď přepis pod nadpisem '### 1. PŘEPIS DO ČEŠTINY' "
        f"(zkráceně, max 300 slov), pak pokračuj:\n"
        f"{REELS_STRUCTURE}\n\n"
        f"Piš výhradně v češtině. Buď konkrétní a přímý."
    )
    result = gemini_text(prompt)
    return result or cs_text[:2000]

def analyze_from_metadata(title: str, description: str) -> str:
    """Create Reels script from title + description only (guaranteed fallback)."""
    print("  Using metadata fallback (title + description).")
    prompt = (
        f"Název videa: {title}\n"
        f"Popis: {description[:400]}\n\n"
        f"Toto video je o komunikaci nebo prezentačních dovednostech. "
        f"Na základě názvu a popisu vytvoř:\n\n"
        f"### 1. PŘEPIS DO ČEŠTINY\n"
        f"Odhadni co video pravděpodobně obsahuje a napiš klíčové myšlenky (100–150 slov).\n\n"
        f"{REELS_STRUCTURE}\n\n"
        f"Piš výhradně v češtině. Buď konkrétní a přímý. "
        f"Poznámka: přepis je odhadnutý z názvu — skutečný přepis nebyl dostupný."
    )
    result = gemini_text(prompt)
    return result or f"⚠️ Přepis není k dispozici. Téma: {title}"

def process_video(video_id: str, title: str, description: str) -> str:
    """Return full 3-part analysis."""
    text, lang = get_transcript(video_id)
    if text:
        print(f"  Transcript found ({len(text)} chars), calling Gemini...")
        return analyze_from_transcript(text, lang)
    else:
        print(f"  No transcript — using metadata fallback.")
        return analyze_from_metadata(title, description)

# ── email ─────────────────────────────────────────────────────────────────

def format_analysis_html(raw: str) -> str:
    """Convert markdown sections to HTML."""
    lines = raw.split("\n")
    html = []
    for line in lines:
        line = line.strip()
        if not line:
            html.append("<br>")
        elif line.startswith("### ") or line.startswith("## "):
            html.append(f'<p style="font-weight:bold;color:#1d3557;margin:16px 0 4px;font-size:14px">'
                        f'{line.lstrip("# ").strip()}</p>')
        elif line == "---":
            html.append('<hr style="border:none;border-top:1px solid #dee2e6;margin:12px 0">')
        elif line[:2] in ("🔥", "💡", "📲", "#️"):
            html.append(f'<p style="font-weight:bold;margin:10px 0 2px;font-size:13px">{line}</p>')
        else:
            html.append(f'<p style="margin:3px 0;line-height:1.75;font-size:13px">{line}</p>')
    return "\n".join(html)

def build_html(videos: list[dict]) -> str:
    weekday_cs = {0:"pondělí",1:"úterý",2:"středa",3:"čtvrtek",4:"pátek",5:"sobota",6:"neděle"}
    today    = datetime.date.today()
    date_str = f"{weekday_cs[today.weekday()].capitalize()} {today.strftime('%-d. %-m. %Y')}"

    rows = ""
    for i, v in enumerate(videos, 1):
        analysis_block = format_analysis_html(v.get("analysis", ""))
        rows += f"""
        <div style="border:1px solid #dee2e6;border-radius:10px;padding:18px;margin-bottom:28px">
          <table width="100%"><tr>
            <td style="width:36px;vertical-align:top;padding-top:2px">
              <span style="font-size:26px;font-weight:bold;color:#e63946">{i}</span>
            </td>
            <td>
              <a href="{v['shorts_url']}" style="font-size:16px;font-weight:bold;
                color:#1d3557;text-decoration:none">{v['title']}</a>
              <div style="color:#6c757d;font-size:12px;margin-top:4px">
                📺 {v['channel']} &nbsp;·&nbsp; 📅 {v['published']}
              </div>
              <div style="background:#f1faee;padding:7px 12px;border-radius:6px;
                          font-size:12px;margin-top:8px">
                👁 {v['views']:,} zhlédnutí &nbsp;|&nbsp;
                👍 {v['likes']:,} líbí se &nbsp;|&nbsp;
                📊 {v['engagement']:.1f} % engagement
              </div>
              <div style="margin-top:14px;background:#f8f9fa;border-radius:8px;padding:16px">
                {analysis_block}
              </div>
            </td>
          </tr></table>
        </div>"""

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"></head><body
      style="font-family:Arial,sans-serif;max-width:720px;margin:0 auto;padding:20px;color:#212529">
      <h1 style="color:#e63946;margin-bottom:4px">🎯 Top 5 Shorts – Komunikace & Prezentace</h1>
      <p style="color:#6c757d;margin-top:0;font-size:14px">{date_str} · přepis + Instagram Reels skript</p>
      {rows}
      <p style="color:#adb5bd;font-size:11px;margin-top:30px;border-top:1px solid #dee2e6;padding-top:10px">
        Automaticky generováno · YouTube Shorts · Gemini 1.5 Flash
      </p>
    </body></html>"""

def send_email(html: str) -> None:
    today = datetime.date.today()
    weekday_cs = {0:"Po",1:"Út",2:"St",3:"Čt",4:"Pá",5:"So",6:"Ne"}
    subject = f"🎯 Top 5 Shorts o komunikaci – {weekday_cs[today.weekday()]} {today.strftime('%-d. %-m.')}"
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = EMAIL_SENDER
    msg["To"]      = EMAIL_RECIPIENT
    msg.attach(MIMEText(html, "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(EMAIL_SENDER, EMAIL_PASSWORD)
        s.sendmail(EMAIL_SENDER, EMAIL_RECIPIENT, msg.as_string())
    print("Email sent.")

# ── main ──────────────────────────────────────────────────────────────────

def main() -> None:
    youtube = build("youtube", "v3", developerKey=YOUTUBE_API_KEY)
    seen    = load_seen()
    print(f"Already seen: {len(seen)} videos")

    candidate_ids: list[str] = []
    for q in QUERIES:
        print(f"Searching: {q}")
        ids = search_videos(youtube, q)
        candidate_ids.extend(ids)
        time.sleep(0.3)

    unique_new = list(set(candidate_ids) - seen)
    print(f"New candidates: {len(unique_new)}")
    if not unique_new:
        print("No new videos — skipping.")
        return

    all_stats = get_stats(youtube, unique_new[:200])
    max_likes = max((v["likes"] for v in all_stats.values()), default=1) or 1
    ranked = sorted(
        all_stats.items(),
        key=lambda kv: (
            kv[1]["likes"] / max_likes * 60
            + min(kv[1]["engagement"], 20) / 20 * 40
        ),
        reverse=True,
    )
    top5 = ranked[:5]

    result_videos: list[dict] = []
    for vid_id, info in top5:
        print(f"\n=== {info['title'][:65]} ===")
        info["analysis"] = process_video(vid_id, info["title"], info["description"])
        result_videos.append(info)

    save_seen(seen | {vid_id for vid_id, _ in top5})
    html = build_html(result_videos)
    send_email(html)

if __name__ == "__main__":
    main()
