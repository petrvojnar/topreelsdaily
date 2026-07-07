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
MAX_SEEN        = 400   # keep only last N seen IDs to avoid infinite growth

QUERIES = [
    "communication skills shorts",
    "presentation skills shorts",
    "public speaking tips shorts",
    "body language confidence shorts",
    "storytelling presentation skills shorts",
    "komunikační dovednosti shorts",
    "prezentační dovednosti shorts",
    "jak mluvit přesvědčivě shorts",
    "řeč těla komunikace shorts",
    "jak zaujmout publikum shorts",
]

# Minimum quality thresholds
MIN_VIEWS      = 5_000
MIN_ENGAGEMENT = 0.5   # percent likes/views


def load_seen() -> set:
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_seen(seen: set) -> None:
    # Keep only the most recent MAX_SEEN IDs (list is sorted, trim oldest)
    ids = sorted(seen)[-MAX_SEEN:]
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(ids, f, ensure_ascii=False, indent=2)


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
    now = datetime.datetime.now(datetime.timezone.utc)
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
                if views < MIN_VIEWS:
                    continue
                engagement = (likes / views * 100) if views > 0 else 0
                if engagement < MIN_ENGAGEMENT:
                    continue
                # Recency: days since published (prefer newer)
                published_str = snippet["publishedAt"]
                published = datetime.datetime.fromisoformat(published_str.replace("Z", "+00:00"))
                days_old  = (now - published).days
                result[vid_id] = {
                    "title":      snippet["title"],
                    "channel":    snippet["channelTitle"],
                    "published":  published_str[:10],
                    "days_old":   days_old,
                    "views":      views,
                    "likes":      likes,
                    "engagement": engagement,
                    "url":        f"https://www.youtube.com/shorts/{vid_id}",
                    "thumbnail":  f"https://img.youtube.com/vi/{vid_id}/mqdefault.jpg",
                }
        except Exception as e:
            print(f"  Stats error: {e}")
    return result


def score(video: dict, max_likes: int) -> float:
    """Composite score: popularity + engagement + recency."""
    pop_score     = video["likes"] / max_likes * 50
    eng_score     = min(video["engagement"], 20) / 20 * 30
    # Recency: full points for <30 days, half for 30-365, zero for >2 years
    days = video["days_old"]
    if days < 30:
        rec_score = 20
    elif days < 365:
        rec_score = 20 * (1 - (days - 30) / 335)
    else:
        rec_score = 0
    return pop_score + eng_score + rec_score


def build_html(videos: list[dict]) -> str:
    weekday_cs = {0:"pondělí",1:"úterý",2:"středa",3:"čtvrtek",4:"pátek",5:"sobota",6:"neděle"}
    today    = datetime.date.today()
    date_str = f"{weekday_cs[today.weekday()].capitalize()} {today.strftime('%-d. %-m. %Y')}"

    rows = ""
    for i, v in enumerate(videos, 1):
        age_label = f"{v['days_old']} dní" if v['days_old'] < 365 else f"{v['days_old']//365} r."
        rows += f"""
        <div style="border:1px solid #dee2e6;border-radius:12px;overflow:hidden;margin-bottom:20px">
          <a href="{v['url']}" style="display:block;position:relative;text-decoration:none">
            <img src="{v['thumbnail']}" width="100%" style="display:block;max-height:200px;object-fit:cover">
            <span style="position:absolute;top:10px;left:10px;background:#e63946;color:white;
              font-size:22px;font-weight:bold;width:36px;height:36px;border-radius:50%;
              display:flex;align-items:center;justify-content:center;line-height:36px;
              text-align:center;padding:0">{i}</span>
            <span style="position:absolute;bottom:8px;right:8px;background:rgba(0,0,0,0.75);
              color:white;font-size:11px;padding:3px 8px;border-radius:4px">▶ Přehrát Short</span>
          </a>
          <div style="padding:14px">
            <a href="{v['url']}" style="font-size:15px;font-weight:bold;color:#1d3557;text-decoration:none;
              display:block;margin-bottom:6px">{v['title']}</a>
            <div style="color:#6c757d;font-size:12px;margin-bottom:8px">
              📺 {v['channel']} &nbsp;·&nbsp; 📅 {v['published']} ({age_label})
            </div>
            <div style="background:#f1faee;padding:7px 12px;border-radius:6px;font-size:12px">
              👁 {v['views']:,} zhlédnutí &nbsp;|&nbsp;
              👍 {v['likes']:,} líbí se &nbsp;|&nbsp;
              📊 {v['engagement']:.1f} % engagement
            </div>
          </div>
        </div>"""

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
</head><body style="font-family:Arial,sans-serif;max-width:640px;margin:0 auto;padding:16px;color:#212529;background:#fff">
  <h1 style="color:#e63946;margin-bottom:4px;font-size:22px">🎯 Top 5 Shorts – Komunikace & Prezentace</h1>
  <p style="color:#6c757d;margin-top:0;font-size:13px;margin-bottom:20px">{date_str} · výběr podle popularity, engagementu a čerstvosti</p>
  {rows}
  <div style="background:#f8f9fa;border-radius:8px;padding:12px;margin-top:10px;font-size:12px;color:#6c757d">
    💡 <strong>Tip:</strong> Pokud tento email přistál ve spamu, přidej odesílatele do kontaktů — příště přijde do doručené pošty.
  </div>
  <p style="color:#adb5bd;font-size:11px;margin-top:20px;border-top:1px solid #dee2e6;padding-top:10px">
    Automaticky generováno · YouTube Shorts · Řazeno: popularita + engagement + čerstvost
  </p>
</body></html>"""


def build_text(videos: list[dict]) -> str:
    """Plain text fallback — helps email deliverability."""
    today    = datetime.date.today().strftime("%-d. %-m. %Y")
    lines    = [f"Top 5 Shorts – Komunikace & Prezentace | {today}", "=" * 50, ""]
    for i, v in enumerate(videos, 1):
        lines += [
            f"{i}. {v['title']}",
            f"   Kanál: {v['channel']} | {v['published']}",
            f"   👁 {v['views']:,}  👍 {v['likes']:,}  📊 {v['engagement']:.1f}%",
            f"   {v['url']}",
            "",
        ]
    lines.append("Automaticky generováno z YouTube Shorts.")
    return "\n".join(lines)


def send_email(html: str, text: str) -> None:
    today = datetime.date.today()
    weekday_cs = {0:"Po",1:"Út",2:"St",3:"Čt",4:"Pá",5:"So",6:"Ne"}
    subject = f"🎯 Top 5 Shorts o komunikaci – {weekday_cs[today.weekday()]} {today.strftime('%-d. %-m.')}"
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"Top Shorts Komunikace <{EMAIL_SENDER}>"
    msg["To"]      = EMAIL_RECIPIENT
    msg.attach(MIMEText(text, "plain",  "utf-8"))
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
    print(f"New candidates before quality filter: {len(unique_new)}")

    all_stats = get_stats(youtube, unique_new[:250])
    print(f"Passed quality filter: {len(all_stats)}")

    if len(all_stats) < 5:
        print("Not enough quality videos — skipping.")
        return

    max_likes = max((v["likes"] for v in all_stats.values()), default=1) or 1
    ranked = sorted(
        all_stats.items(),
        key=lambda kv: score(kv[1], max_likes),
        reverse=True,
    )
    top5 = ranked[:5]

    for vid_id, info in top5:
        print(f"  ✓ {info['title'][:55]} | {info['views']:,} views | {info['engagement']:.1f}% | {info['days_old']}d")

    save_seen(seen | {vid_id for vid_id, _ in top5})
    videos = [info for _, info in top5]
    send_email(build_html(videos), build_text(videos))


if __name__ == "__main__":
    main()
