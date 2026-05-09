import os
import json
import datetime
import smtplib
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from googleapiclient.discovery import build

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

def load_seen() -> set:
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, encoding="utf-8") as f:
            return set(json.load(f))
    return set()

def save_seen(seen: set) -> None:
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(seen), f, ensure_ascii=False, indent=2)

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
                    "url":        f"https://www.youtube.com/shorts/{vid_id}",
                }
        except Exception as e:
            print(f"  Stats error: {e}")
    return result

def build_html(videos: list[dict]) -> str:
    weekday_cs = {0:"pondělí",1:"úterý",2:"středa",3:"čtvrtek",4:"pátek",5:"sobota",6:"neděle"}
    today    = datetime.date.today()
    date_str = f"{weekday_cs[today.weekday()].capitalize()} {today.strftime('%-d. %-m. %Y')}"

    rows = ""
    for i, v in enumerate(videos, 1):
        rows += f"""
        <div style="border:1px solid #dee2e6;border-radius:10px;padding:18px;margin-bottom:20px">
          <table width="100%"><tr>
            <td style="width:36px;vertical-align:top;padding-top:4px">
              <span style="font-size:26px;font-weight:bold;color:#e63946">{i}</span>
            </td>
            <td>
              <a href="{v['url']}" style="font-size:16px;font-weight:bold;
                color:#1d3557;text-decoration:none">{v['title']}</a>
              <div style="color:#6c757d;font-size:12px;margin-top:5px">
                📺 {v['channel']} &nbsp;·&nbsp; 📅 {v['published']}
              </div>
              <div style="background:#f1faee;padding:8px 12px;border-radius:6px;
                          font-size:12px;margin-top:8px">
                👁 {v['views']:,} zhlédnutí &nbsp;|&nbsp;
                👍 {v['likes']:,} líbí se &nbsp;|&nbsp;
                📊 {v['engagement']:.1f} % engagement
              </div>
              <div style="margin-top:8px">
                <a href="{v['url']}" style="background:#e63946;color:white;padding:6px 14px;
                  border-radius:6px;text-decoration:none;font-size:13px;font-weight:bold">
                  ▶ Přehrát Short
                </a>
              </div>
            </td>
          </tr></table>
        </div>"""

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"></head><body
      style="font-family:Arial,sans-serif;max-width:680px;margin:0 auto;padding:20px;color:#212529">
      <h1 style="color:#e63946;margin-bottom:4px">🎯 Top 5 Shorts – Komunikace & Prezentace</h1>
      <p style="color:#6c757d;margin-top:0;font-size:14px">{date_str}</p>
      {rows}
      <p style="color:#adb5bd;font-size:11px;margin-top:30px;border-top:1px solid #dee2e6;padding-top:10px">
        Automaticky generováno · YouTube Shorts · Řazeno podle engagement (likes/views)
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

    for vid_id, info in top5:
        print(f"  Selected: {info['title'][:60]} ({info['views']:,} views, {info['engagement']:.1f}%)")

    save_seen(seen | {vid_id for vid_id, _ in top5})
    html = build_html([info for _, info in top5])
    send_email(html)

if __name__ == "__main__":
    main()
