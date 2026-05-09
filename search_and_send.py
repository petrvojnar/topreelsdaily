import os
import json
import datetime
import smtplib
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from googleapiclient.discovery import build
from youtube_transcript_api import YouTubeTranscriptApi
from deep_translator import GoogleTranslator
from google import genai
from google.genai import types

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

REEL_PROMPT = """Jsi expert na komunikaci, virální obsah a Instagram Reels.
Zpracuj obsah tohoto videa ve třech krocích:

---
### 1. PŘEPIS DO ČEŠTINY
Přepiš vše co je v videu řečeno. Pokud je video v angličtině nebo jiném jazyce, přelož ho do češtiny. Zachovej vše co je řečeno.

---
### 2. UPRAVENÝ TEXT
Přepiš přepis do čtivé, smysluplné češtiny. Odstraň přeřeknutí, opakování a výplňová slova. Zachovej všechny klíčové myšlenky. Maximálně 200 slov.

---
### 3. INSTAGRAM REELS SKRIPT
Přepiš obsah jako virální skript pro Instagram Reels. Musí mít přesně tuto strukturu:

🔥 HOOK (1–3 sekundy):
[Jedna silná věta která okamžitě zastaví scrollování — překvapení, provokace nebo silná otázka]

💡 OBSAH:
[3–5 krátkých úderných bodů z videa — každý max 1–2 věty, bez omáčky]

📲 VÝZVA K AKCI:
[Jedna věta — Sleduj / Ulož / Pošli příteli / Napiš do komentářů]

#️⃣ HASHTAGY:
[12–15 hashtagů — mix českých a anglických relevantních k tématu]

---
Piš výhradně v češtině. Buď konkrétní, přímý, bez zbytečného úvodu."""


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
                    "title":      snippet["title"],
                    "channel":    snippet["channelTitle"],
                    "published":  snippet["publishedAt"][:10],
                    "views":      views,
                    "likes":      likes,
                    "engagement": engagement,
                    "url":        f"https://www.youtube.com/watch?v={vid_id}",
                    "shorts_url": f"https://www.youtube.com/shorts/{vid_id}",
                }
        except Exception as e:
            print(f"  Stats error: {e}")
    return result


# ── transcript & Gemini ───────────────────────────────────────────────────

def get_captions(video_id: str) -> tuple[str | None, str | None]:
    """Try YouTube captions in order of preference."""
    try:
        tlist = YouTubeTranscriptApi.list_transcripts(video_id)
        # manual Czech → manual EN/SK → any auto-generated
        for prefer_manual in (True, False):
            for t in tlist:
                if t.is_generated == prefer_manual:
                    continue
                try:
                    items = t.fetch()
                    text = " ".join(i["text"] for i in items)
                    print(f"  Captions found: {t.language} (generated={t.is_generated})")
                    return text, t.language_code
                except Exception:
                    continue
    except Exception as e:
        print(f"  Caption list error: {e}")
    return None, None


def gemini_from_video(video_id: str) -> str | None:
    """Ask Gemini to watch the YouTube video directly."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("  No GEMINI_API_KEY set.")
        return None

    client = genai.Client(api_key=api_key)

    for url in [
        f"https://www.youtube.com/watch?v={video_id}",
        f"https://www.youtube.com/shorts/{video_id}",
        f"https://youtu.be/{video_id}",
    ]:
        try:
            print(f"  Gemini trying: {url}")
            response = client.models.generate_content(
                model="gemini-1.5-flash",
                contents=[
                    types.Content(
                        role="user",
                        parts=[
                            types.Part(
                                file_data=types.FileData(file_uri=url)
                            ),
                            types.Part(text=REEL_PROMPT),
                        ],
                    )
                ],
            )
            print(f"  Gemini OK ({url})")
            return response.text.strip()
        except Exception as e:
            print(f"  Gemini error ({url}): {e}")

    return None


def gemini_from_text(text: str, lang: str) -> str:
    """Translate captions (if needed) then ask Gemini to create Reels script."""
    # Translate to Czech first
    if lang != "cs":
        try:
            translator = GoogleTranslator(source="auto", target="cs")
            text = translator.translate(text[:4500])
            print("  Translated to Czech.")
        except Exception as e:
            print(f"  Translation error: {e}")
            text = text[:4500]

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return text[:2000]

    try:
        client = genai.Client(api_key=api_key)
        prompt = (
            f"Zde je přepis videa o komunikaci/prezentaci (již v češtině):\n\n"
            f"{text[:3500]}\n\n"
            f"Zpracuj ho takto:\n\n"
            f"### 2. UPRAVENÝ TEXT\n"
            f"Přepiš do čtivé smysluplné češtiny, max 200 slov.\n\n"
            f"### 3. INSTAGRAM REELS SKRIPT\n"
            f"Vytvoř virální skript s přesnou strukturou:\n\n"
            f"🔥 HOOK (1–3 sekundy):\n[Silná věta která zastaví scrollování]\n\n"
            f"💡 OBSAH:\n[3–5 krátkých úderných bodů]\n\n"
            f"📲 VÝZVA K AKCI:\n[Jedna věta]\n\n"
            f"#️⃣ HASHTAGY:\n[12–15 hashtagů]\n\n"
            f"Piš výhradně v češtině."
        )
        response = client.models.generate_content(
            model="gemini-1.5-flash",
            contents=prompt,
        )
        print("  Gemini text structuring OK.")
        # Prepend the original transcript as section 1
        return f"### 1. PŘEPIS DO ČEŠTINY\n{text[:1500]}\n\n---\n\n" + response.text.strip()
    except Exception as e:
        print(f"  Gemini text error: {e}")
        return text[:2000]


def process_video(video_id: str) -> str | None:
    """Return full 3-part analysis or None."""
    # Try captions first (fast, free)
    text, lang = get_captions(video_id)
    if text:
        return gemini_from_text(text, lang)
    # Fallback: let Gemini watch the video directly
    return gemini_from_video(video_id)


# ── email ─────────────────────────────────────────────────────────────────

def format_analysis_html(raw: str) -> str:
    """Convert markdown-ish analysis to HTML."""
    if not raw:
        return ""
    lines = raw.split("\n")
    html_lines = []
    for line in lines:
        line = line.strip()
        if not line:
            html_lines.append("<br>")
        elif line.startswith("### ") or line.startswith("## "):
            heading = line.lstrip("# ").strip()
            html_lines.append(f'<p style="font-weight:bold;color:#1d3557;margin:14px 0 4px">{heading}</p>')
        elif line.startswith("---"):
            html_lines.append('<hr style="border:none;border-top:1px solid #dee2e6;margin:10px 0">')
        elif line.startswith("🔥") or line.startswith("💡") or line.startswith("📲") or line.startswith("#️⃣"):
            html_lines.append(f'<p style="font-weight:bold;margin:10px 0 2px">{line}</p>')
        else:
            html_lines.append(f'<p style="margin:3px 0;line-height:1.7">{line}</p>')
    return "\n".join(html_lines)


def build_html(videos: list[dict]) -> str:
    weekday_cs = {0:"pondělí",1:"úterý",2:"středa",3:"čtvrtek",4:"pátek",5:"sobota",6:"neděle"}
    today    = datetime.date.today()
    date_str = f"{weekday_cs[today.weekday()].capitalize()} {today.strftime('%-d. %-m. %Y')}"

    rows = ""
    for i, v in enumerate(videos, 1):
        analysis_html = ""
        if v.get("analysis"):
            analysis_html = f"""
            <div style="margin-top:16px;background:#f8f9fa;border-radius:8px;padding:16px;font-size:13px;color:#212529">
              {format_analysis_html(v['analysis'])}
            </div>"""
        else:
            analysis_html = """
            <div style="margin-top:12px;background:#fff3cd;border-radius:6px;padding:10px;font-size:12px;color:#856404">
              ⚠️ Přepis se nepodařilo získat (video může mít zakázané přehrávání třetí stranou).
            </div>"""

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
              {analysis_html}
            </td>
          </tr></table>
        </div>"""

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"></head><body
      style="font-family:Arial,sans-serif;max-width:720px;margin:0 auto;padding:20px;color:#212529">
      <h1 style="color:#e63946;margin-bottom:4px">🎯 Top 5 Shorts – Komunikace & Prezentace</h1>
      <p style="color:#6c757d;margin-top:0;font-size:14px">{date_str} · přepis + Instagram Reels skript</p>
      {rows}
      <p style="color:#adb5bd;font-size:11px;margin-top:30px;border-top:1px solid #dee2e6;padding-top:10px">
        Automaticky generováno · Zdroj: YouTube Shorts · AI: Gemini 1.5 Flash
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
        print(f"\n--- Processing: {info['title'][:70]} ---")
        info["analysis"] = process_video(vid_id)
        if info["analysis"]:
            print(f"  Analysis length: {len(info['analysis'])} chars")
        else:
            print(f"  No analysis obtained.")
        result_videos.append(info)

    save_seen(seen | {vid_id for vid_id, _ in top5})
    html = build_html(result_videos)
    send_email(html)


if __name__ == "__main__":
    main()
