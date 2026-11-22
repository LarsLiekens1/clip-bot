"""
TikTok -> Discord webhook.

- Haalt elk uur je profiel en laatste video's op (yt-dlp + profielpagina).
- Slaat metingen op in data/state.json.
- Stuurt direct een melding bij een nieuwe volger of een views-mijlpaal.
- Stuurt eenmaal per dag een analyse met grafiek.
"""

import io
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import requests  # noqa: E402
import yt_dlp  # noqa: E402

try:
    from yt_dlp.networking.impersonate import ImpersonateTarget
except Exception:  # noqa: BLE001
    ImpersonateTarget = None

try:
    from curl_cffi import requests as cffi_requests
except Exception:  # noqa: BLE001
    cffi_requests = None

USERNAME = os.environ["TIKTOK_USERNAME"].lstrip("@")
WEBHOOK = os.environ["DISCORD_WEBHOOK_URL"]
PING_USER = os.environ.get("DISCORD_PING_USER_ID", "").strip()
TZ = ZoneInfo(os.environ.get("TZ_NAME", "Europe/Brussels"))
REPORT_HOUR = int(os.environ.get("REPORT_HOUR", "20"))
MAX_VIDEOS = int(os.environ.get("MAX_VIDEOS", "30"))
KEEP_DAYS = 14
MILESTONES = [1_000, 5_000, 10_000, 50_000, 100_000, 500_000, 1_000_000]
STATE_FILE = Path("data/state.json")
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


# ---------- helpers ----------

def fmt(n):
    if n is None:
        return "onbekend"
    return f"{int(n):,}".replace(",", ".")


def signed(n):
    return f"{'+' if n >= 0 else '-'}{fmt(abs(n))}"


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"history": [], "milestones": {}, "last_report_date": "", "last_followers": None}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


# ---------- data ophalen ----------

def http_get(url):
    """GET met browser-impersonation als curl_cffi beschikbaar is."""
    if cffi_requests is not None:
        return cffi_requests.get(
            url,
            headers={"Accept-Language": "en-US,en;q=0.9"},
            impersonate="chrome",
            timeout=30,
        )
    return requests.get(
        url,
        headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"},
        timeout=30,
    )


def fetch_followers():
    """Leest het volgersaantal uit de JSON op de profielpagina. Geeft None bij falen."""
    try:
        r = http_get(f"https://www.tiktok.com/@{USERNAME}")
        m = re.search(
            r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>',
            r.text,
            re.S,
        )
        if not m:
            print("Profielpagina bevat geen data (mogelijk geblokkeerd).")
            return None
        data = json.loads(m.group(1))
        stats = data["__DEFAULT_SCOPE__"]["webapp.user-detail"]["userInfo"]["stats"]
        return int(stats["followerCount"])
    except Exception as exc:  # noqa: BLE001
        print(f"Volgers ophalen mislukt: {exc}")
        return None


def fetch_videos():
    """Haalt de laatste video's op met views, likes, comments en reposts."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "ignoreerrors": True,
        "playlistend": MAX_VIDEOS,
    }
    if ImpersonateTarget is not None:
        opts["impersonate"] = ImpersonateTarget.from_str("chrome")
    videos = []
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"https://www.tiktok.com/@{USERNAME}", download=False)
    except Exception as exc:  # noqa: BLE001
        print(f"Video's ophalen mislukt: {exc}")
        return videos
    for e in (info or {}).get("entries") or []:
        if not e:
            continue
        videos.append(
            {
                "id": str(e.get("id")),
                "views": e.get("view_count") or 0,
                "likes": e.get("like_count") or 0,
                "comments": e.get("comment_count") or 0,
                "reposts": e.get("repost_count") or 0,
                "ts": e.get("timestamp"),
                "desc": (e.get("description") or e.get("title") or "").replace("\n", " ")[:80],
                "url": e.get("webpage_url") or f"https://www.tiktok.com/@{USERNAME}/video/{e.get('id')}",
            }
        )
    return videos


# ---------- Discord ----------

def post(embed, chart=None, ping=False):
    payload = {
        "content": f"<@{PING_USER}>" if (ping and PING_USER) else "",
        "embeds": [embed],
        "allowed_mentions": {"users": [PING_USER]} if (ping and PING_USER) else {"parse": []},
    }
    if chart:
        embed["image"] = {"url": "attachment://chart.png"}
        r = requests.post(
            WEBHOOK,
            data={"payload_json": json.dumps(payload)},
            files={"file": ("chart.png", chart, "image/png")},
            timeout=30,
        )
    else:
        r = requests.post(WEBHOOK, json=payload, timeout=30)
    r.raise_for_status()


# ---------- analyse ----------

def make_chart(history):
    if len(history) < 2:
        return None
    xs = [datetime.fromisoformat(h["ts"]).astimezone(TZ) for h in history]
    followers = [h["followers"] if h.get("followers") is not None else float("nan") for h in history]
    views = [h["total_views"] for h in history]

    fig, axes = plt.subplots(2, 1, figsize=(8, 6), sharex=True)
    axes[0].plot(xs, followers, marker="o", markersize=3)
    axes[0].set_title("Volgers")
    axes[0].grid(alpha=0.3)
    axes[1].plot(xs, views, marker="o", markersize=3, color="tab:orange")
    axes[1].set_title(f"Totaal aantal views (laatste {MAX_VIDEOS} video's)")
    axes[1].grid(alpha=0.3)
    fig.autofmt_xdate()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


def pick_baseline(history, now):
    if not history:
        return None
    target = now - timedelta(hours=24)
    older = [h for h in history if datetime.fromisoformat(h["ts"]) <= target]
    return older[-1] if older else history[0]


def build_report(snapshot, videos, baseline, now):
    hours = max(1, round((now - datetime.fromisoformat(baseline["ts"])).total_seconds() / 3600))
    lines = []

    if snapshot["followers"] is not None and baseline.get("followers") is not None:
        d = snapshot["followers"] - baseline["followers"]
        lines.append(f"**Volgers:** {fmt(snapshot['followers'])} ({signed(d)} in {hours}u)")
    elif snapshot["followers"] is not None:
        lines.append(f"**Volgers:** {fmt(snapshot['followers'])}")

    dv = snapshot["total_views"] - baseline["total_views"]
    lines.append(f"**Views (laatste {len(videos)} video's):** {fmt(snapshot['total_views'])} ({signed(dv)} in {hours}u)")

    # Beste video van de periode (grootste groei in views)
    base_views = baseline.get("videos", {})
    gains = sorted(
        ((v["views"] - base_views.get(v["id"], 0), v) for v in videos),
        key=lambda x: x[0],
        reverse=True,
    )
    if gains and gains[0][0] > 0:
        g, v = gains[0]
        lines.append(f"**Beste video nu:** {signed(g)} views - [{v['desc'] or 'zonder tekst'}]({v['url']})")

    new = [v for v in videos if v["id"] not in base_views]
    lines.append(f"**Nieuwe video's:** {len(new)}")

    # Engagement
    total_views = sum(v["views"] for v in videos)
    if total_views:
        eng = sum(v["likes"] + v["comments"] + v["reposts"] for v in videos) / total_views * 100
        avg = total_views / len(videos)
        lines.append(f"**Gemiddeld:** {fmt(avg)} views per video, engagement {eng:.1f}%")

    # Beste posttijd
    by_hour = {}
    for v in videos:
        if v["ts"]:
            h = datetime.fromtimestamp(v["ts"], tz=timezone.utc).astimezone(TZ).hour
            by_hour.setdefault(h, []).append(v["views"])
    if len(videos) >= 8 and by_hour:
        best = max(by_hour.items(), key=lambda kv: sum(kv[1]) / len(kv[1]))
        lines.append(
            f"**Beste posttijd:** rond {best[0]:02d}:00 "
            f"(gem. {fmt(sum(best[1]) / len(best[1]))} views, {len(best[1])} video's)"
        )

    # Top 3
    top = sorted(videos, key=lambda v: v["views"], reverse=True)[:3]
    if top:
        lines.append("**Top 3:**")
        for i, v in enumerate(top, 1):
            lines.append(f"{i}. [{v['desc'] or 'zonder tekst'}]({v['url']}) - {fmt(v['views'])} views")

    return {
        "title": f"Dagelijkse analyse @{USERNAME}",
        "description": "\n".join(lines)[:4000],
        "color": 0x25F4EE,
        "timestamp": now.isoformat(),
    }


# ---------- main ----------

def main():
    state = load_state()
    first_run = not state["history"]
    now = datetime.now(timezone.utc)

    videos = fetch_videos()
    followers = fetch_followers()
    if not videos and followers is None:
        print("Niets opgehaald. TikTok blokkeert waarschijnlijk deze server.")
        sys.exit(1)

    snapshot = {
        "ts": now.isoformat(),
        "followers": followers,
        "total_views": sum(v["views"] for v in videos),
        "total_likes": sum(v["likes"] for v in videos),
        "videos": {v["id"]: v["views"] for v in videos},
    }

    # Directe meldingen
    alerts = []
    last_f = state.get("last_followers")
    if followers is not None and last_f is not None and followers > last_f:
        alerts.append(f"Nieuwe volgers: **{signed(followers - last_f)}** (nu {fmt(followers)})")

    for v in videos:
        reached = max((m for m in MILESTONES if v["views"] >= m), default=0)
        done = state["milestones"].get(v["id"], 0)
        if reached > done:
            if not first_run:
                alerts.append(f"[{v['desc'] or 'Video'}]({v['url']}) heeft **{fmt(reached)} views** bereikt")
            state["milestones"][v["id"]] = reached

    if alerts:
        post(
            {
                "title": f"Update @{USERNAME}",
                "description": "\n".join(alerts),
                "color": 0x57F287,
                "timestamp": now.isoformat(),
            },
            ping=True,
        )

    # Dagelijkse analyse
    local = now.astimezone(TZ)
    today = local.date().isoformat()
    baseline = pick_baseline(state["history"], now)
    if baseline and local.hour >= REPORT_HOUR and state.get("last_report_date") != today:
        history_for_chart = state["history"] + [snapshot]
        post(build_report(snapshot, videos, baseline, now), chart=make_chart(history_for_chart))
        state["last_report_date"] = today

    # Opslaan
    state["history"].append(snapshot)
    cutoff = now - timedelta(days=KEEP_DAYS)
    state["history"] = [h for h in state["history"] if datetime.fromisoformat(h["ts"]) >= cutoff]
    if followers is not None:
        state["last_followers"] = followers
    save_state(state)
    print(f"OK: {len(videos)} video's, volgers: {followers}")


if __name__ == "__main__":
    main()