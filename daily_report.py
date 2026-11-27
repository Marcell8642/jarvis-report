"""
Jarvis daily report: every day at 14:00 (Madrid time) sends a Telegram voice message (Jarvis voice)
and a text message about the latest 3 videos of the YouTube channel. Runs on GitHub Actions,
so the PC can be switched off.

Settings come from GitHub secrets / the workflow file:
  TELEGRAM_TOKEN, TELEGRAM_CHAT_ID  (required)
  GROQ_API_KEY                       (optional - nicer, natural wording)
  YT_CHANNEL                         ("@name", channel link or UC... id)
  SEND_NOW                           ("true" = send immediately, used by the "Run workflow" button)
"""

import os
import re
import sys
import json
import time
import datetime
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

TZ = ZoneInfo("Europe/Madrid")
SEND_HOUR = 14
VOICE = "bm_george"
STATE = Path("state.json")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0 Safari/537.36", "Cookie": "SOCS=CAI; CONSENT=YES+cb", "Accept-Language": "en"}

TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
CHAT_ID = re.sub(r"[^0-9-]", "", os.environ.get("TELEGRAM_CHAT_ID", ""))   # drop quotes/spaces/text
GROQ_KEY = os.environ.get("GROQ_API_KEY", "").strip()
CHANNEL = os.environ.get("YT_CHANNEL", "").strip()
SEND_NOW = os.environ.get("SEND_NOW", "").lower() == "true"


# ---------------------------------------------------------------- state
def load_state():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state):
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------------------------------------------------------------- timing
def wait_for_send_time(state):
    """Returns True if we should send now (waits until 14:00 if we are a bit early)."""
    if SEND_NOW:
        return True
    now = datetime.datetime.now(TZ)
    today = now.date().isoformat()
    if state.get("last_sent") == today:
        print("Already sent today - nothing to do.")
        return False
    target = now.replace(hour=SEND_HOUR, minute=0, second=0, microsecond=0)
    early = (target - now).total_seconds()
    if early > 40 * 60:
        print(f"Too early ({now:%H:%M} Madrid) - the other scheduled run will send it.")
        return False
    if early > 0:
        print(f"Waiting {early / 60:.0f} minutes until {SEND_HOUR}:00 Madrid time...")
        time.sleep(early)
    elif early < -6 * 3600:
        print("Way past the send time - skipping.")
        return False
    return True


# ---------------------------------------------------------------- YouTube (public feed, no login)
def http_get(url):
    r = requests.get(url, headers=UA, timeout=20)
    r.raise_for_status()
    return r.text


def channel_id():
    m = re.search(r"(UC[\w-]{22})", CHANNEL)
    if m:
        return m.group(1)
    url = CHANNEL if CHANNEL.startswith("http") else "https://www.youtube.com/" + (
        CHANNEL if CHANNEL.startswith("@") else "@" + CHANNEL)
    html = http_get(url)
    m = (re.search(r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[\w-]{22})"', html) or
         re.search(r'"externalId":"(UC[\w-]{22})"', html) or re.search(r'"channelId":"(UC[\w-]{22})"', html))
    if not m:
        raise RuntimeError("Could not find the channel id - put the UC... id into YT_CHANNEL")
    return m.group(1)


def latest_videos(cid, n=3):
    xml = http_get("https://www.youtube.com/feeds/videos.xml?channel_id=" + cid)
    ns = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015",
          "media": "http://search.yahoo.com/mrss/"}
    root = ET.fromstring(xml)
    vids = []
    for e in root.findall("a:entry", ns)[:n]:
        st = e.find("media:group/media:community/media:statistics", ns)
        rt = e.find("media:group/media:community/media:starRating", ns)
        vid = e.findtext("yt:videoId", "", ns)
        vids.append({"id": vid, "title": e.findtext("a:title", "", ns),
                     "published": e.findtext("a:published", "", ns)[:10],
                     "views": int(st.get("views", 0)) if st is not None else 0,
                     "likes": int(rt.get("count", 0)) if rt is not None else 0,
                     "url": f"https://youtu.be/{vid}"})
    return root.findtext("a:title", "your channel", ns), vids


# ---------------------------------------------------------------- wording
def days_ago(date_str):
    d = (datetime.datetime.now(TZ).date() - datetime.date.fromisoformat(date_str)).days
    return "today" if d <= 0 else "yesterday" if d == 1 else f"{d} days ago"


def facts(channel, vids, prev):
    lines = []
    for i, v in enumerate(vids, 1):
        old = prev.get(v["id"])
        growth = f"+{v['views'] - old:,} views since yesterday" if old is not None else "first report for this video"
        lines.append(f"{i}. '{v['title']}' - uploaded {days_ago(v['published'])}: {v['views']:,} views "
                     f"({growth}), {v['likes']:,} likes")
    return f"Channel: {channel}\n" + "\n".join(lines)


def voice_script(fact_text, vids, prev):
    """Natural spoken summary, ~35 seconds. Groq if available, otherwise a plain template."""
    if GROQ_KEY:
        try:
            r = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_KEY}", "User-Agent": "Jarvis/1.0"},
                json={"model": "openai/gpt-oss-120b", "temperature": 0.7, "max_tokens": 500,
                      "reasoning_effort": "low",
                      "messages": [
                          {"role": "system", "content":
                              "You are J.A.R.V.I.S., a calm, quietly witty British AI butler. Address the user as "
                              "'sir'. Write a spoken daily briefing about their YouTube videos: 70-110 words, "
                              "natural speech, round the numbers the way a person says them, mention which video "
                              "is doing best and one short useful remark or suggestion. No lists, no markdown, "
                              "no emojis, no web addresses. Start with a short greeting for the afternoon."},
                          {"role": "user", "content": fact_text}]},
                timeout=40)
            r.raise_for_status()
            text = r.json()["choices"][0]["message"]["content"].strip()
            if text:
                return text
        except Exception as e:
            print("Groq failed, using the template:", e)
    return template_script(vids, prev)


def template_script(vids, prev):
    """Spoken summary without Groq."""
    names = ["Your latest video", "The one before it", "And the third"]
    parts = ["Good afternoon, sir. Here is your daily update."]
    for i, v in enumerate(vids):
        old = prev.get(v["id"])
        grew = f", {v['views'] - old:,} more than yesterday" if old is not None else ""
        parts.append(f"{names[i]}, {v['title']}, uploaded {days_ago(v['published'])}, "
                     f"has {v['views']:,} views{grew}, and {v['likes']:,} likes.")
    best = max(vids, key=lambda v: v["views"] - prev.get(v["id"], v["views"]))
    if best["id"] in prev and best["views"] > prev[best["id"]]:
        parts.append(f"{best['title']} is growing fastest at the moment.")
    parts.append("That is all for today, sir.")
    return " ".join(parts)


def text_report(channel, vids, prev):
    today = datetime.datetime.now(TZ).strftime("%A, %d %B")
    out = [f"📊 <b>Daily YouTube report</b> – {today}", f"<i>{esc(channel)}</i>", ""]
    for i, v in enumerate(vids, 1):
        old = prev.get(v["id"])
        delta = f" (<b>+{v['views'] - old:,}</b> since yesterday)" if old is not None else ""
        out += [f"{i}. <b>{esc(v['title'])}</b>",
                f"   👁 {v['views']:,}{delta}   👍 {v['likes']:,}",
                f"   uploaded {days_ago(v['published'])} · <a href=\"{v['url']}\">open</a>", ""]
    return "\n".join(out).strip()


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------------------------------------------------------------- voice
def make_voice(text, out="report.ogg"):
    import numpy as np
    import soundfile as sf
    from kokoro import KPipeline
    pipe = KPipeline(lang_code=VOICE[0], repo_id="hexgrad/Kokoro-82M")
    fixed = re.sub(r"\b([Ss])ir\b", lambda m: f"[{m.group(1)}ir](/sˈɜːɹ/)", text)   # "sir" with an r
    try:
        chunks = [a for _, _, a in pipe(fixed, voice=VOICE)]
    except Exception:
        chunks = [a for _, _, a in pipe(text, voice=VOICE)]
    audio = np.concatenate([c.numpy() if hasattr(c, "numpy") else c for c in chunks])
    sf.write("report.wav", audio, 24000)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", "report.wav", "-c:a", "libopus", "-b:a", "48k",
                    "-application", "voip", out], check=True)     # real Telegram voice message format
    return out, int(len(audio) / 24000)


# ---------------------------------------------------------------- telegram
def tg(method, **kw):
    r = requests.post(f"https://api.telegram.org/bot{TOKEN}/{method}", timeout=60, **kw)
    if not r.ok:
        raise RuntimeError(f"Telegram {method} failed: {r.status_code} {r.text[:200]}")
    return r.json()


def find_chat():
    """Check the chat ID before the slow part; if Telegram doesn't know it, find the right one."""
    global CHAT_ID
    me = requests.get(f"https://api.telegram.org/bot{TOKEN}/getMe", timeout=30).json()
    if not me.get("ok"):
        sys.exit(f"The TELEGRAM_TOKEN secret is wrong or revoked: {me}")
    bot = "@" + me["result"]["username"]
    print("Bot:", bot)
    if CHAT_ID:
        r = requests.get(f"https://api.telegram.org/bot{TOKEN}/getChat", params={"chat_id": CHAT_ID}, timeout=30).json()
        if r.get("ok"):
            return
        print(f"TELEGRAM_CHAT_ID ({CHAT_ID}) is not a chat of {bot}: {r.get('description')}")
    ups = requests.get(f"https://api.telegram.org/bot{TOKEN}/getUpdates", timeout=30).json().get("result", [])
    chats = {}
    for u in ups:
        m = u.get("message") or u.get("edited_message") or {}
        c = m.get("chat") or {}
        if c.get("type") == "private":
            chats[str(c["id"])] = c.get("first_name", "")
    if len(chats) == 1:
        CHAT_ID = next(iter(chats))
        print(f"Using the chat found in recent messages: {CHAT_ID}  <- put this into the TELEGRAM_CHAT_ID secret")
        return
    sys.exit(f"Can't find your chat. Open Telegram, write 'hi' to {bot} (press Start first), then run again. "
             f"Chats seen: {chats or 'none'}")


def main():
    if not TOKEN:
        sys.exit("TELEGRAM_TOKEN secret is missing (see the guide).")
    find_chat()
    state = load_state()
    if not wait_for_send_time(state):
        return
    channel, vids = latest_videos(channel_id())
    if not vids:
        sys.exit("No videos found on the channel feed.")
    prev = state.get("views", {})
    fact_text = facts(channel, vids, prev)
    print(fact_text)

    script = voice_script(fact_text, vids, prev)
    script = re.sub(r"[^\w\s.,!?;:'\"()%&+/-]", "", script)      # emoji can't be spoken
    script = re.sub(r"\s+([.,!?])", r"\1", re.sub(r"\.+", ".", script))
    script = re.sub(r"([.!?]),", r"\1", script).strip()
    print("Voice script:", script)
    ogg, secs = make_voice(script)
    with open(ogg, "rb") as f:
        tg("sendVoice", data={"chat_id": CHAT_ID, "duration": secs}, files={"voice": ("jarvis.ogg", f, "audio/ogg")})
    tg("sendMessage", data={"chat_id": CHAT_ID, "text": text_report(channel, vids, prev), "parse_mode": "HTML",
                            "disable_web_page_preview": "true"})

    if not SEND_NOW:          # a test run must not change "since yesterday"
        views = dict(prev)
        views.update({v["id"]: v["views"] for v in vids})
        state["views"] = views
        state["last_sent"] = datetime.datetime.now(TZ).date().isoformat()
        save_state(state)
    print("Report sent.")


if __name__ == "__main__":
    main()
