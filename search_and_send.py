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
import google.generativeai as genai

YOUTUBE_API_KEY = os.environ["YOUTUBE_API_KEY"]
EMAIL_SENDER    = os.environ["EMAIL_SENDER"]
EMAIL_PASSWORD  = os.environ["EMAIL_PASSWORD"]
EMAIL_RECIPIENT = "petrvojnar@seznam.cz"
SEEN_FILE       = "seen_videos.json"

QUERIES = [
    "communication skills reels",
    "presentation skills tips",
    "public speaking body language",
    "how to speak confidently",
    "storytelling presentation",
    "komunikační dovednosti",
    "prezentační dovednosti",
    "jak mluvit na veřejnosti",
    "řeč těla komunikace",
    "vystupování na veřejnosti",
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
            videoDuration="medium",   # 4–20 minutes
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
                part="statistics,snippet",
                id=",".join(batch),
            ).execute()
            for item in resp.get("items", []):
                vid_id  = item["id"]
                stats   = item["statistics"]
                snippet = item["snippet"]
                views   = int(stats.get("viewCount",   0))
                likes   = int(stats.get("likeCount",   0))
                # engagement = like-to-view ratio (%) — best public proxy for saves/shares
                engagement = (likes / views * 100) if views > 0 else 0
                result[vid_id] = {
                    "title":       snippet["title"],
                    "channel":     snippet["channelTitle"],
                    "published":   snippet["publishedAt"][:10],
                    "views":       views,
                    "likes":       likes,
                    "engagement":  engagement,
                    "url":         f"https://www.youtube.com/watch?v={vid_id}",
                }
        except Exception as e:
            print(f"  Stats error: {e}")
    return result


# ── transcripts ────────────────────────────────────────────────────────────

def get_transcript_text(video_id: str) -> tuple[str | None, str | None]:
    """Returns (text, language_code) or (None, None)."""
    for lang in (["cs"], ["en"], ["sk"]):
        try:
            items = YouTubeTranscriptApi.get_transcript(video_id, languages=lang)
            return " ".join(t["text"] for t in items), lang[0]
        except Exception:
            pass
    # try any auto-generated
    try:
        tlist = YouTubeTranscriptApi.list_transcripts(video_id)
        for t in tlist:
            items = t.fetch()
            return " ".join(i["text"] for i in items), t.language_code
    except Exception:
        pass
    return None, None


def transcribe_with_gemini(video_id: str) -> str | None:
    """Transcribe a YouTube video using Gemini — fallback when captions unavailable."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None
    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel("gemini-1.5-flash")
        url   = f"https://www.youtube.com/watch?v={video_id}"
        response = model.generate_content([
            {"video_url": url},
            ("Přepiš vše, co je v tomto videu řečeno. Výstup pouze v češtině — "
             "pokud je video v jiném jazyce, přelož ho. Bez komentářů, jen přepis."),
        ])
        return response.text.strip()
    except Exception as e:
        print(f"  Gemini transcription error: {e}")
        return None


def to_czech(text: str) -> str:
    translator = GoogleTranslator(source="auto", target="cs")
    # translate first ~4 000 chars (≈ first minute of speech)
    chunk = text[:4000]
    try:
        return translator.translate(chunk)
    except Exception as e:
        print(f"  Translation error: {e}")
        return text[:4000]


# ── email ──────────────────────────────────────────────────────────────────

def build_html(videos: list[dict]) -> str:
    today = datetime.date.today().strftime("%-d. %-m. %Y")
    rows  = ""
    for i, v in enumerate(videos, 1):
        transcript_block = ""
        if v.get("transcript_cs"):
            preview = v["transcript_cs"][:700]
            if len(v["transcript_cs"]) > 700:
                preview += "…"
            transcript_block = f"""
            <details style="margin-top:10px">
              <summary style="cursor:pointer;color:#457b9d;font-size:13px">
                📝 Přepis (prvních ~60 vteřin)
              </summary>
              <p style="font-size:13px;line-height:1.7;color:#333;background:#f8f9fa;
                         padding:10px;border-radius:6px;margin-top:6px">{preview}</p>
            </details>"""

        rows += f"""
        <div style="border:1px solid #dee2e6;border-radius:10px;padding:18px;margin-bottom:20px">
          <table width="100%"><tr>
            <td style="width:40px;vertical-align:top">
              <span style="font-size:28px;font-weight:bold;color:#e63946">{i}</span>
            </td>
            <td>
              <a href="{v['url']}" style="font-size:16px;font-weight:bold;
                color:#1d3557;text-decoration:none">{v['title']}</a>
              <div style="color:#6c757d;font-size:13px;margin-top:4px">
                📺 {v['channel']} &nbsp;·&nbsp; 📅 {v['published']}
              </div>
              <div style="background:#f1faee;padding:8px 12px;border-radius:6px;
                          font-size:13px;margin-top:8px">
                👁 {v['views']:,} zhlédnutí &nbsp;|&nbsp;
                👍 {v['likes']:,} líbí se &nbsp;|&nbsp;
                📊 {v['engagement']:.1f} % engagement
              </div>
              {transcript_block}
            </td>
          </tr></table>
        </div>"""

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"></head><body
      style="font-family:Arial,sans-serif;max-width:780px;margin:0 auto;padding:20px;color:#212529">
      <h1 style="color:#e63946;margin-bottom:4px">🎯 Top 10 videí – Komunikace & Prezentace</h1>
      <p style="color:#6c757d;margin-top:0">Denní výběr · {today}</p>
      {rows}
      <p style="color:#adb5bd;font-size:11px;margin-top:30px;border-top:1px solid #dee2e6;padding-top:10px">
        Automaticky generováno · Zdroj: YouTube · Řazeno podle engagement skóre (likes/views)
      </p>
    </body></html>"""


def send_email(html: str) -> None:
    today = datetime.date.today().strftime("%-d. %-m. %Y")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🎯 Top 10 videí o komunikaci – {today}"
    msg["From"]    = EMAIL_SENDER
    msg["To"]      = EMAIL_RECIPIENT
    msg.attach(MIMEText(html, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(EMAIL_SENDER, EMAIL_PASSWORD)
        s.sendmail(EMAIL_SENDER, EMAIL_RECIPIENT, msg.as_string())
    print("Email sent.")


# ── main ───────────────────────────────────────────────────────────────────

def main() -> None:
    youtube    = build("youtube", "v3", developerKey=YOUTUBE_API_KEY)
    seen       = load_seen()
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

    # 2. get stats (max 200 candidates to stay within API quota)
    all_stats = get_stats(youtube, unique_new[:200])

    # 3. rank: 60 % weight on likes count (popularity) + 40 % on engagement ratio
    max_likes = max((v["likes"] for v in all_stats.values()), default=1) or 1
    ranked = sorted(
        all_stats.items(),
        key=lambda kv: (
            kv[1]["likes"] / max_likes * 60
            + min(kv[1]["engagement"], 20) / 20 * 40
        ),
        reverse=True,
    )
    top10 = ranked[:10]

    # 4. fetch + translate transcripts
    result_videos: list[dict] = []
    for vid_id, info in top10:
        print(f"Transcript: {info['title'][:60]}")
        text, lang = get_transcript_text(vid_id)
        if text and lang != "cs":
            info["transcript_cs"] = to_czech(text)
        elif text:
            info["transcript_cs"] = text[:4000]
        else:
            print(f"  No captions — trying Gemini...")
            info["transcript_cs"] = transcribe_with_gemini(vid_id)
        result_videos.append(info)

    # 5. mark as seen
    save_seen(seen | {vid_id for vid_id, _ in top10})

    # 6. send
    html = build_html(result_videos)
    send_email(html)


if __name__ == "__main__":
    main()
