
import json
import os
import random
import urllib.parse
import urllib.request
from pathlib import Path

CHANNEL = "didacticjam"
CHANNEL_URL = f"https://www.twitch.tv/{CHANNEL}"

LIBRARY_FILE = Path("art_library.json")
STATE_FILE = Path("twitch_state.json")

CLIENT_ID = os.environ["TWITCH_CLIENT_ID"]
CLIENT_SECRET = os.environ["TWITCH_CLIENT_SECRET"]
WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]

CATEGORIES = ("fanart", "concept", "screenshot", "other")
PERMITTED_REUSE = {
    "permission-granted",
    "licensed",
    "self-created",
}


def api_request(url, data=None, headers=None):
    req = urllib.request.Request(
        url, data=data, headers=headers or {}
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def get_access_token():
    body = urllib.parse.urlencode({
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "client_credentials",
    }).encode("utf-8")

    response = api_request(
        "https://id.twitch.tv/oauth2/token",
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded"
        },
    )
    return response["access_token"]


def get_live_stream(token):
    query = urllib.parse.urlencode({
        "user_login": CHANNEL
    })

    response = api_request(
        f"https://api.twitch.tv/helix/streams?{query}",
        headers={
            "Client-Id": CLIENT_ID,
            "Authorization": f"Bearer {token}",
        },
    )
    streams = response.get("data", [])
    return streams[0] if streams else None


def load_library():
    return json.loads(
        LIBRARY_FILE.read_text(encoding="utf-8")
    )


def is_eligible(image, game_id):
    required = (
        "id",
        "image_url",
        "source_url",
        "creator",
        "category",
    )

    if not all(image.get(key) for key in required):
        return False

    if str(image.get("twitch_game_id")) != str(game_id):
        return False

    if image.get("approved") is not True:
        return False

    if image.get("sfw") is not True:
        return False

    if image.get("reuse_permission") not in PERMITTED_REUSE:
        return False

    if image.get("category") not in CATEGORIES:
        return False

    if not image["image_url"].startswith("https://"):
        return False

    if not image["source_url"].startswith("https://"):
        return False

    return True


def choose_image(game_id, library, state):
    eligible = [
        image for image in library.get("images", [])
        if is_eligible(image, game_id)
    ]

    if not eligible:
        print(f"No approved images for game {game_id}")
        return None

    # Avoid recently used images for this game.
    recent = state.setdefault("image_history", {}).get(
        str(game_id), []
    )

    unused = [
        image for image in eligible
        if image["id"] not in recent
    ]

    # Reset the pool once all images have been used.
    if not unused:
        unused = [
            image for image in eligible
            if image["id"] != recent[-1]
        ] if recent else eligible

        if not unused:
            unused = eligible

    # Group available images by type.
    groups = {}
    for category in CATEGORIES:
        matches = [
            image for image in unused
            if image["category"] == category
        ]
        if matches:
            groups[category] = matches

    weights = library.get("weights", {})
    categories = list(groups)

    selected_category = random.choices(
        categories,
        weights=[
            max(0, float(weights.get(category, 0)))
            for category in categories
        ],
        k=1,
    )[0] if sum(
        max(0, float(weights.get(c, 0)))
        for c in categories
    ) > 0 else random.choice(categories)

    selected = random.choice(groups[selected_category])

    print(
        f"Selected {selected_category}: "
        f"{selected['id']} by {selected['creator']}"
    )

    return selected


def send_discord_alert(stream, image):
    title = (
        stream.get("title") or "Come hang out!"
    )[:256].replace("@", "@\u200b")

    game = (
        stream.get("game_name") or "Uncategorized"
    )[:100].replace("@", "@\u200b")

    embed = {
        "title": title,
        "url": CHANNEL_URL,
        "color": 9520895,
        "description": (
            f"🎮 **Now Playing:** {game}\n\n"
            "Come hang out on Twitch!"
        ),
        "footer": {
            "text": "DidacticJam • Twitch Live"
        },
    }

    if image:
        embed["image"] = {
            "url": image["image_url"]
        }

        creator = image["creator"].replace(
            "@", "@\u200b"
        )[:200]

        embed["fields"] = [
            {
                "name": "🎨 Artwork credit",
                "value": (
                    f"**{creator}** · "
                    f"{image['category']}\n"
                    f"[Original artwork]({image['source_url']})"
                )[:1024],
                "inline": False,
            }
        ]

    payload = {
        "username": "DidacticJam Live",
        "content": (
            "🔴 **DIDACTICJAM IS LIVE!**\n"
            f"{CHANNEL_URL}"
        ),
        "embeds": [embed],
        "allowed_mentions": {"parse": []},
    }

    data = json.dumps(payload).encode("utf-8")

    request = urllib.request.Request(
        WEBHOOK_URL,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    with urllib.request.urlopen(request, timeout=30) as r:
        if r.status not in (200, 204):
            raise RuntimeError(
                f"Discord HTTP error: {r.status}"
            )


def save_state(state):
    STATE_FILE.write_text(
        json.dumps(state, indent=2) + "\n",
        encoding="utf-8",
    )


def main():
    token = get_access_token()
    stream = get_live_stream(token)

    current_id = stream["id"] if stream else None

    # First run never announces an already-live stream.
    if not STATE_FILE.exists():
        save_state({
            "last_stream_id": current_id,
            "image_history": {},
        })
        print("Initialized. Existing stream skipped.")
        return

    state = json.loads(
        STATE_FILE.read_text(encoding="utf-8")
    )
    state.setdefault("image_history", {})

    if not stream:
        print("DidacticJam is currently offline.")
        return

    if current_id == state.get("last_stream_id"):
        print("This stream has already been processed.")
        return

    library = load_library()
    game_id = str(stream.get("game_id", ""))
    print(f"Twitch game: {stream.get('game_name')} | ID: {game_id}")

    image = choose_image(game_id, library, state)

    # Send notification before recording success.
    send_discord_alert(stream, image)

    state["last_stream_id"] = current_id

    if image:
        history = state["image_history"].setdefault(
            game_id, []
        )
        history.append(image["id"])

        # Keep enough history to rotate through the pool.
        state["image_history"][game_id] = history[-100:]

    save_state(state)

    print("Discord live announcement posted!")


if __name__ == "__main__":
    main()
