
import json
import os
import random
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
        url, data=data, headers=headers or {}
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def get_access_token():
    form = urllib.parse.urlencode({
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "client_credentials",
    }).encode()

    result = api_request(
        "https://id.twitch.tv/oauth2/token",
        data=form,
        headers={
            "Content-Type": "application/x-www-form-urlencoded"
        },
    )
    return result["access_token"]


def auth_headers(token):
    return {
        "Client-Id": CLIENT_ID,
        "Authorization": f"Bearer {token}",
    }


def get_live_stream(token):
    query = urllib.parse.urlencode({
        "user_login": CHANNEL
    })

    response = api_request(
        f"https://api.twitch.tv/helix/streams?{query}",
        headers=auth_headers(token),
    )
    streams = response.get("data", [])
    return streams[0] if streams else None


def get_game_artwork(stream, token, previous_image=None):
    game_id = stream.get("game_id")

    if not game_id:
        return None

    try:
        # Find the linked IGDB game ID.
        query = urllib.parse.urlencode({"id": game_id})
        result = api_request(
            f"https://api.twitch.tv/helix/games?{query}",
            headers=auth_headers(token),
        )
        games = result.get("data", [])

        if not games:
            return None

        igdb_id = games[0].get("igdb_id")
        if not igdb_id:
            return None

        # Retrieve official game artworks from IGDB.
        query_body = (
            "fields artworks.image_id; "
            f"where id = {int(igdb_id)}; "
            "limit 1;"
        ).encode("utf-8")

        result = api_request(
            "https://api.igdb.com/v4/games",
            data=query_body,
            headers={
                **auth_headers(token),
                "Content-Type": "text/plain",
            },
        )

        if not result:
            return None

        artworks = result[0].get("artworks", [])
        ids = [
            art["image_id"]
            for art in artworks
            if art.get("image_id")
        ]

        if not ids:
            return None

        # Avoid repeating the most recent image
        # for this game when alternatives exist.
        choices = [
            image_id for image_id in ids
            if image_id != previous_image
        ]

        selected = random.choice(choices or ids)

        url = (
            "https://images.igdb.com/igdb/"
            f"image/upload/t_1080p/{selected}.jpg"
        )

        print(
            f"Selected artwork for "
            f"{stream.get('game_name')}: {selected}"
        )

        return {"url": url, "id": selected}

    except Exception as exc:
        print(f"Artwork lookup failed: {exc}")
        return None


def get_stream_thumbnail(stream):
    thumbnail = stream.get("thumbnail_url", "")
    return (
        thumbnail.replace("{width}", "1280")
        .replace("{height}", "720")
    )


def send_discord_alert(stream, artwork):
    title = stream.get("title") or "Come hang out!"
    game = stream.get("game_name") or "Uncategorized"

    # Sanitize potentially user-controlled content.
    title = title[:256].replace("@", "@\u200b")
    game = game[:100].replace("@", "@\u200b")

    image = (
        artwork["url"] if artwork
        else get_stream_thumbnail(stream)
    )

    embed = {
        "title": title,
        "url": CHANNEL_URL,
        "color": 9520895,
        "description": (
            f"🎮 **Now Playing:** {game}\n\n"
            "Come hang out on Twitch!"
        ),
        "footer": {
            "text": "DidacticJam • Live on Twitch"
        },
    }

    if image.startswith("https://"):
        embed["image"] = {"url": image}

    payload = {
        "username": "DidacticJam Live",
        "content": (
            "🔴 **DIDACTICJAM IS LIVE!**\n"
            f"Watch here: {CHANNEL_URL}"
        ),
        "embeds": [embed],
        "allowed_mentions": {"parse": []},
    }

    request = urllib.request.Request(
        WEBHOOK_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    with urllib.request.urlopen(request, timeout=30) as r:
        if r.status not in (200, 204):
            raise RuntimeError(f"Discord HTTP {r.status}")


def save_state(state):
    STATE_FILE.write_text(
        json.dumps(state, indent=2) + "\n",
        encoding="utf-8",
    )


def main():
    token = get_access_token()
    stream = get_live_stream(token)
    current_id = stream["id"] if stream else None

    # Initialize without announcing an existing stream.
    if not STATE_FILE.exists():
        save_state({
            "last_stream_id": current_id,
            "last_artworks": {},
        })
        print("Initialized. Existing stream skipped.")
        return

    state = json.loads(STATE_FILE.read_text())
    state.setdefault("last_artworks", {})

    if not stream:
        print("Offline. No announcement needed.")
        return

    if current_id == state.get("last_stream_id"):
        print("This stream was already processed.")
        return

    game_id = stream.get("game_id", "")
    previous_image = state["last_artworks"].get(game_id)

    artwork = get_game_artwork(
        stream, token, previous_image
    )

    send_discord_alert(stream, artwork)

    state["last_stream_id"] = current_id

    if artwork and game_id:
        state["last_artworks"][game_id] = artwork["id"]

    save_state(state)

    print("New stream announced successfully!")


if __name__ == "__main__":
    main()
