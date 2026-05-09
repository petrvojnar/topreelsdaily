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

# ── helpers ────────────────────────────────────────────────────────────────

def load_seen() -> set:
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_seen(seen: set) -> None:
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(seen), f, ensure_ascii=False, indent=2)


# ── YouTube ────────────────────────────────────────────────────────────────

def search_videos(youtube, query: str, max_results: int = 50) -> list[str]:
    try:
        resp = youtube.search().list(
            part="snippet",
            q=query,
            type="video",
            videoDuration="short",   # pod 4 minuty — pokryje Shorts i krátká videa
            order="relevance",
            maxResults=max_results,
        ).execute()
        return [item["id"]["videoId"] for item in resp.get("items", [])]
    except Exception as e:
        print(f"  Search error for '{query}': {e}")
        return []


def get_stats(youtube, video_ids: list[str]) -> dict:
    result = {}
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i : i + 50]
        try:
            resp = youtube.videos().list(
                part="statistics,snippet,contentDetails",
                id=",".join(batch),
            ).execute()
            for item in resp.get("items", []):
                vid_id   = item["id"]
                stats    = item["statistics"]
                snippet  = item["snippet"]
                duration = item["contentDetails"]["duration"]  # ISO 8601
                views    = int(stats.get("viewCount", 0))
                likes    = int(stats.get("likeCount", 0))
                engagement = (likes / views * 100) if views > 0 else 0
                result[vid_id] = {
                    "title":      snippet["title"],
                    "channel":    snippet["channelTitle"],
                    "published":  snippet["publishedAt"][:10],
                    "duration":   duration,
                    "views":      views,
                    "likes":      likes,
                    "engagement": engagement,
                    "url":        f"https://www.youtube.com/watch?v={vid_id}",
                    "shorts_url": f"https://www.youtube.com/shorts/{vid_id}",
                }
        except Exception as e:
            print(f"  Stats error: {e}")
    return result


# ── transcripts ────────────────────────────────────────────────────────────

def get_transcript_text(video_id: str) -> tuple[str | None, str | None]:
    """Try YouTube captions first — fast and free."""
    try:
        tlist = YouTubeTranscriptApi.list_transcripts(video_id)

        # 1. manual Czech
        try:
            t = tlist.find_manually_created_transcript(["cs"])
            items = t.fetch()
            return " ".join(i["text"] for i in items), "cs"
        except Exception:
            pass

        # 2. manual English / Slovak
        try:
            t = tlist.find_manually_created_transcript(["en", "sk"])
            items = t.fetch()
            return " ".join(i["text"] for i in items), t.language_code
        except Exception:
            pass

        # 3. any auto-generated
        for t in tlist:
            try:
                items = t.fetch()
                return " ".join(i["text"] for i in items), t.language_code
            except Exception:
                continue

    except Exception as e:
        print(f"  Caption error: {e}")

    return None, None


def transcribe_and_contextualize_gemini(video_id: str, title: str) -> str | None:
    """Use Gemini to transcribe + rewrite for communication theme and viral potential."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None
    try:
        client = genai.Client(api_key=api_key)
        video_url = f"https://www.youtube.com/watch?v={video_id}"
        prompt = (
            "Toto video pojednává o komunikačních nebo prezentačních dovednostech. "
            "Udělej prosím toto:\n"
            "1. Přepiš vše co je v videu řečeno do češtiny.\n"
            "2. Pod nadpisem 'Klíčové myšlenky' vypiš 3–5 hlavních poznatků z videa.\n"
            "3. Pod nadpisem 'Virální potenciál' napiš 2–3 věty proč toto video může "
            "rezonovat s publikem a co z něj dělá sdílený obsah.\n"
            "Odpověz pouze česky."
        )
        response = client.models.generate_content(
            model="gemini-1.5-flash",
            contents=[
                types.Content(parts=[
                    types.Part(
                        file_data=types.FileData(file_uri=video_url)
                    ),
                    types.Part(text=prompt),
                ])
            ],
        )
        return response.text.strip()
    except Exception as e:
        print(f"  Gemini error: {e}")
        return None


def to_czech_and_contextualize(text: str, lang: str) -> str:
    """Translate captions to Czech, then ask Gemini to structure and contextualize."""
    # Translate if needed
    if lang != "cs":
        try:
            translator = GoogleTranslator(source="auto", target="cs")
            text = translator.translate(text[:4000])
        except Exception as e:
            print(f"  Translation error: {e}")
            text = text[:4000]

    # Ask Gemini to structure the raw transcript
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return text[:2000]
    try:
        client = genai.Client(api_key=api_key)
        prompt = (
            f"Zde je přepis videa o komunikačních/prezentačních dovednostech:\n\n"
            f"{text[:3000]}\n\n"
            "Udělej prosím toto:\n"
            "1. Uprav a zkrať přepis do čtivé podoby (max 300 slov).\n"
            "2. Pod nadpisem 'Klíčové myšlenky' vypiš 3–5 hlavních poznatků.\n"
            "3. Pod nadpisem 'Virální potenciál' napiš 2–3 věty proč toto video může "
            "rezonovat s publikem a co z něj dělá sdílený obsah.\n"
            "Odpověz pouze česky."
        )
        response = client.models.generate_content(
            model="gemini-1.5-flash",
            contents=prompt,
        )
        return response.text.strip()
    except Exception as e:
        print(f"  Gemini structuring error: {e}")
        return text[:2000]


# ── email ──────────────────────────────────────────────────────────────────

def build_html(videos: list[dict]) -> str:
    weekday_cs = {
        0: "pondělí", 1: "úterý", 2: "středa",
        3: "čtvrtek", 4: "pátek", 5: "sobota", 6: "neděle"
    }
    today = datetime.date.today()
    date_str = f"{weekday_cs[today.weekday()].capitalize()} {today.strftime('%-d. %-m. %Y')}"

    rows = ""
    for i, v in enumerate(videos, 1):
        transcript_block = ""
        if v.get("analysis"):
            # Format the analysis with nice HTML
            analysis_html = v["analysis"].replace("\n\n", "</p><p>").replace("\n", "<br>")
            transcript_block = f"""
            <div style="margin-top:14px;border-left:3px solid #e63946;padding-left:12px">
              <p style="font-size:13px;line-height:1.8;color:#333;margin:0">{analysis_html}</p>
            </div>"""

        rows += f"""
        <div style="border:1px solid #dee2e6;border-radius:10px;padding:18px;margin-bottom:24px">
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
              {transcript_block}
            </td>
          </tr></table>
        </div>"""

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"></head><body
      style="font-family:Arial,sans-serif;max-width:720px;margin:0 auto;padding:20px;color:#212529">
      <h1 style="color:#e63946;margin-bottom:4px">🎯 Top 5 Shorts – Komunikace & Prezentace</h1>
      <p style="color:#6c757d;margin-top:0;font-size:14px">{date_str} · s přepisem a analýzou</p>
      {rows}
      <p style="color:#adb5bd;font-size:11px;margin-top:30px;border-top:1px solid #dee2e6;padding-top:10px">
        Automaticky generováno · Zdroj: YouTube Shorts · Přepis & analýza: Gemini AI
      </p>
    </body></html>"""


def send_email(html: str) -> None:
    today = datetime.date.today()
    weekday_cs = {0: "Po", 1: "Út", 2: "St", 3: "Čt", 4: "Pá", 5: "So", 6: "Ne"}
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


# ── main ───────────────────────────────────────────────────────────────────

def main() -> None:
    youtube = build("youtube", "v3", developerKey=YOUTUBE_API_KEY)
    seen    = load_seen()
    print(f"Already seen: {len(seen)} videos")

    # 1. collect candidates
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

    # 2. get stats (max 200 candidates)
    all_stats = get_stats(youtube, unique_new[:200])

    # 3. rank by engagement
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

    # 4. transcribe + analyze each video
    result_videos: list[dict] = []
    for vid_id, info in top5:
        print(f"Processing: {info['title'][:60]}")
        text, lang = get_transcript_text(vid_id)

        if text:
            print(f"  Captions found ({lang}), contextualizing via Gemini...")
            info["analysis"] = to_czech_and_contextualize(text, lang)
        else:
            print(f"  No captions — transcribing via Gemini...")
            info["analysis"] = transcribe_and_contextualize_gemini(vid_id, info["title"])

        result_videos.append(info)

    # 5. mark as seen
    save_seen(seen | {vid_id for vid_id, _ in top5})

    # 6. send
    html = build_html(result_videos)
    send_email(html)


if __name__ == "__main__":
    main()
