
import json
import os
import urllib.parse
import urllib.request
from pathlib import Path

CHANNEL = "didacticjam"
CHANNEL_URL = f"https://www.twitch.tv/{CHANNEL}"
STATE_FILE = Path("twitch_state.json")

CLIENT_ID = os.environ["TWITCH_CLIENT_ID"]
CLIENT_SECRET = os.environ["TWITCH_CLIENT_SECRET"]
WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]


def api_request(url, data=None, headers=None):
    req = urllib.request.Request(
        url,
        data=data,
        headers=headers or {},
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def get_access_token():
    form = urllib.parse.urlencode({
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "client_credentials",
    }).encode("utf-8")

    result = api_request(
        "https://id.twitch.tv/oauth2/token",
        data=form,
        headers={
            "Content-Type":
            "application/x-www-form-urlencoded"
        },
    )
    return result["access_token"]


def get_live_stream(token):
    url = (
        "https://api.twitch.tv/helix/streams?"
        + urllib.parse.urlencode({"user_login": CHANNEL})
    )

    result = api_request(url, headers={
        "Client-Id": CLIENT_ID,
        "Authorization": f"Bearer {token}",
    })

    streams = result.get("data", [])
    return streams[0] if streams else None


def send_discord_alert(stream):
    title = stream.get("title") or "Come hang out!"
    game = stream.get("game_name") or "Just Chatting"
    thumbnail = stream.get("thumbnail_url", "")

    if thumbnail:
        thumbnail = thumbnail.replace(
            "{width}", "640"
        ).replace("{height}", "360")

    # Prevent unexpected mentions in user-controlled titles.
    title = title[:250].replace("@", "@\u200b")
    game = game[:100].replace("@", "@\u200b")

    embed = {
        "title": title,
        "url": CHANNEL_URL,
        "color": 9520895,
        "description": f"🎮 **Playing:** {game}",
        "footer": {"text": "DidacticJam on Twitch"},
    }

    if thumbnail.startswith("https://"):
        embed["image"] = {"url": thumbnail}

    payload = {
        "username": "DidacticJam Live",
        "content": (
            "🔴 **DIDACTICJAM IS LIVE!**\n"
            f"Come hang out!\n{CHANNEL_URL}"
        ),
        "embeds": [embed],
        "allowed_mentions": {"parse": []},
    }

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        WEBHOOK_URL,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=30) as response:
        if response.status not in (200, 204):
            raise RuntimeError(
                f"Discord returned {response.status}"
            )


def save_state(last_stream_id):
    STATE_FILE.write_text(
        json.dumps(
            {"last_stream_id": last_stream_id},
            indent=2
        ) + "\n",
        encoding="utf-8",
    )


def main():
    token = get_access_token()
    stream = get_live_stream(token)
    current_id = stream["id"] if stream else None

    # First run: establish a baseline.
    # Never announce a stream that was already live
    # before the automation was initialized.
    if not STATE_FILE.exists():
        save_state(current_id)
        print("Initialized stream state.")
        print("Existing live stream will not be announced.")
        return

    state = json.loads(
        STATE_FILE.read_text(encoding="utf-8")
    )
    previous_id = state.get("last_stream_id")

    if not stream:
        print("Channel is offline. No alert needed.")
        # Preserve the last stream ID so an API blip
        # doesn't cause a repeat announcement.
        return

    if current_id == previous_id:
        print("Already announced or initialized this stream.")
        return

    print("New stream detected!")
    send_discord_alert(stream)
    save_state(current_id)
    print("Discord notification sent successfully.")


if __name__ == "__main__":
    main()
