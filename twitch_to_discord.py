
import json
import os
import random
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# ============================================================
# DIDACTICJAM - TWITCH TO DISCORD
# Fan art + gameplay screenshots
# ============================================================

CHANNEL = "didacticjam"
CHANNEL_URL = f"https://www.twitch.tv/{CHANNEL}"
STATE_FILE = Path("twitch_state.json")

TWITCH_CLIENT_ID = os.environ["TWITCH_CLIENT_ID"]
TWITCH_CLIENT_SECRET = os.environ["TWITCH_CLIENT_SECRET"]
DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]

FANART_WEIGHT = 55
SCREENSHOT_WEIGHT = 45

STRICT_GAME_MATCHING = True

BLOCKED_DOMAINS = (
    "fandom.com",
    "wikia.com",
    "wikipedia.org",
    "wikimedia.org",
)

ALLOWED_LICENSES = {"by", "by-sa", "cc0"}


# ============================================================
# HTTP REQUEST HELPERS
# ============================================================

def request_json(url, data=None, headers=None):
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "User-Agent": "DidacticJamArtBot/1.0",
            "Accept": "application/json",
            **(headers or {}),
        },
    )

    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


# ============================================================
# TWITCH AUTHENTICATION AND STREAM DETECTION
# ============================================================

def get_twitch_token():
    body = urllib.parse.urlencode({
        "client_id": TWITCH_CLIENT_ID,
        "client_secret": TWITCH_CLIENT_SECRET,
        "grant_type": "client_credentials",
    }).encode("utf-8")

    result = request_json(
        "https://id.twitch.tv/oauth2/token",
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )

    return result["access_token"]


def twitch_headers(token):
    return {
        "Client-Id": TWITCH_CLIENT_ID,
        "Authorization": f"Bearer {token}",
    }


def get_stream(token):
    query = urllib.parse.urlencode({
        "user_login": CHANNEL,
    })

    result = request_json(
        f"https://api.twitch.tv/helix/streams?{query}",
        headers=twitch_headers(token),
    )

    streams = result.get("data", [])
    return streams[0] if streams else None


def get_igdb_id(game_id, token):
    if not game_id:
        return None

    query = urllib.parse.urlencode({"id": game_id})

    result = request_json(
        f"https://api.twitch.tv/helix/games?{query}",
        headers=twitch_headers(token),
    )

    games = result.get("data", [])
    return games[0].get("igdb_id") if games else None


# ============================================================
# IMAGE FILTERING
# ============================================================

def forbidden_url(url):
    try:
        hostname = (
            urllib.parse.urlsplit(url).hostname or ""
        ).lower()
    except (ValueError, TypeError):
        return True

    return any(
        hostname == domain
        or hostname.endswith("." + domain)
        for domain in BLOCKED_DOMAINS
    )


def safe_url(url):
    return (
        isinstance(url, str)
        and url.startswith("https://")
        and not forbidden_url(url)
    )


def normalize(text):
    return re.sub(
        r"[^a-z0-9]+",
        " ",
        str(text).lower(),
    ).strip()


def matching_game(game_name, item):
    title = normalize(item.get("title", ""))
    game = normalize(game_name)

    if not game or not title:
        return False

    if STRICT_GAME_MATCHING:
        return game in title

    return all(
        word in title.split()
        for word in game.split()
        if len(word) > 2
    )


# ============================================================
# OPENVERSE AUTHENTICATION
# ============================================================

def get_openverse_token():
    client_id = os.environ["OPENVERSE_CLIENT_ID"]
    client_secret = os.environ["OPENVERSE_CLIENT_SECRET"]

    body = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    }).encode("utf-8")

    result = request_json(
        "https://api.openverse.org/v1/auth_tokens/token/",
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )

    print("Openverse authentication successful")
    return result["access_token"]


# ============================================================
# OPENVERSE FAN ART DISCOVERY
# ============================================================

def get_fanart(game_name):
    if not game_name:
        return []

    query = urllib.parse.urlencode({
        "q": f'"{game_name}" fan art',
        "mature": "false",
        "license": "by,by-sa,cc0",
        "page_size": 20,
        "filter_dead": "true",
    })

    url = (
        "https://api.openverse.org/v1/images/"
        f"?{query}"
    )

    try:
        openverse_token = get_openverse_token()

        result = request_json(
            url,
            headers={
                "Authorization": f"Bearer {openverse_token}",
                "Accept": "application/json",
            },
        )

        images = []

        for item in result.get("results", []):
            image_url = item.get("url", "")
            source_url = item.get(
                "foreign_landing_url", ""
            )

            creator = item.get("creator")
            license_name = (
                item.get("license") or ""
            ).lower()

            if item.get("mature") is True:
                continue

            if license_name not in ALLOWED_LICENSES:
                continue

            if not creator or not source_url:
                continue

            if not safe_url(image_url):
                continue

            if not safe_url(source_url):
                continue

            if not matching_game(game_name, item):
                continue

            width = item.get("width") or 0
            height = item.get("height") or 0

            if width < 600 or height < 300:
                continue

            if width < height:
                continue

            if not item.get("id"):
                continue

            images.append({
                "id": "openverse-" + str(item["id"]),
                "type": "fanart",
                "url": image_url,
                "source_url": source_url,
                "creator": creator,
                "license": license_name.upper(),
                "license_url": item.get("license_url"),
            })

        print(f"Found {len(images)} fan-art candidates")
        return images

    except urllib.error.HTTPError as error:
        print(
            f"Fan-art lookup failed: HTTP {error.code}"
        )
        return []

    except Exception as error:
        print(f"Fan-art lookup failed: {error}")
        return []


# ============================================================
# IGDB GAMEPLAY SCREENSHOTS
# ============================================================

def get_screenshots(igdb_id, token):
    if not igdb_id:
        return []

    body = (
        "fields image_id,width,height; "
        f"where game = {int(igdb_id)}; "
        "limit 100;"
    ).encode("utf-8")

    try:
        screenshots = request_json(
            "https://api.igdb.com/v4/screenshots",
            data=body,
            headers={
                **twitch_headers(token),
                "Content-Type": "text/plain",
            },
        )

        results = []

        for shot in screenshots:
            image_id = shot.get("image_id")
            width = shot.get("width") or 0
            height = shot.get("height") or 0

            if not image_id:
                continue

            if width <= height:
                continue

            image_url = (
                "https://images.igdb.com/igdb/"
                f"image/upload/t_1080p/{image_id}.jpg"
            )

            results.append({
                "id": "igdb-" + str(image_id),
                "type": "screenshot",
                "url": image_url,
                "source_url": "https://www.igdb.com/",
                "creator": "IGDB",
                "license": None,
                "license_url": None,
            })

        print(
            f"Found {len(results)} gameplay screenshots"
        )
        return results

    except Exception as error:
        print(f"Screenshot lookup failed: {error}")
        return []


# ============================================================
# IMAGE ROTATION
# ============================================================

def choose_image(game_id, fanart, screenshots, state):
    history = state.setdefault(
        "image_history", {}
    ).get(str(game_id), [])

    all_images = fanart + screenshots

    if not all_images:
        return None

    # Use images not previously selected.
    available = [
        img for img in all_images
        if img["id"] not in history
    ]

    if not available:
        last_id = history[-1] if history else None

        available = [
            img for img in all_images
            if img["id"] != last_id
        ] or all_images

    fan_pool = [
        img for img in available
        if img["type"] == "fanart"
    ]

    screenshot_pool = [
        img for img in available
        if img["type"] == "screenshot"
    ]

    if fan_pool and screenshot_pool:
        category = random.choices(
            ["fanart", "screenshot"],
            weights=[
                FANART_WEIGHT,
                SCREENSHOT_WEIGHT,
            ],
            k=1,
        )[0]

        pool = (
            fan_pool
            if category == "fanart"
            else screenshot_pool
        )
    else:
        pool = fan_pool or screenshot_pool

    return random.choice(pool)


# ============================================================
# DISCORD WEBHOOK
# ============================================================

def send_discord(stream, image):
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
            "Come hang out!"
        ),
        "footer": {
            "text": "DidacticJam • Twitch"
        },
    }

    if image:
        embed["image"] = {
            "url": image["url"],
        }

        creator = str(image["creator"]).replace(
            "@", "@\u200b"
        )[:180]

        if image["type"] == "fanart":
            credit = (
                f"By **{creator}**\n"
                f"[Original artwork]({image['source_url']})"
            )

            if image.get("license"):
                credit += (
                    f"\nLicense: {image['license']}"
                )

                if safe_url(image.get("license_url")):
                    credit += (
                        f" · [Terms]({image['license_url']})"
                    )
        else:
            credit = (
                "Gameplay screenshot via IGDB\n"
                "[Image source](https://www.igdb.com/)"
            )

        embed["fields"] = [{
            "name": "🖼️ Image source",
            "value": credit[:1024],
            "inline": False,
        }]

    payload = {
        "username": "DidacticJam Live",
        "content": (
            "🔴 **DIDACTICJAM IS LIVE!**\n"
            f"{CHANNEL_URL}"
        ),
        "embeds": [embed],
        "allowed_mentions": {"parse": []},
    }

    req = urllib.request.Request(
        DISCORD_WEBHOOK_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "DidacticJam-LiveAlerts/1.0",
            "Accept": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(
            req, timeout=30
        ) as response:
            print(
                f"Discord webhook status: {response.status}"
            )

    except urllib.error.HTTPError as error:
        details = error.read().decode(
            "utf-8", errors="replace"
        )

        print(
            f"Discord webhook failed: HTTP {error.code}"
        )
        print(f"Discord response: {details[:1000]}")
        raise


# ============================================================
# SAVE AND RESTORE HISTORY
# ============================================================

def save_state(state):
    STATE_FILE.write_text(
        json.dumps(state, indent=2) + "\n",
        encoding="utf-8",
    )


# ============================================================
# MAIN
# ============================================================

def main():
    token = get_twitch_token()
    stream = get_stream(token)

    current_id = stream["id"] if stream else None

    # First run: initialize without posting an old stream.
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
        print("DidacticJam is offline.")
        return

    if current_id == state.get("last_stream_id"):
        print("Already announced this stream.")
        return

    game_id = stream.get("game_id", "")
    game_name = stream.get("game_name", "")

    print(
        f"Game: {game_name} | Twitch ID: {game_id}"
    )

    # Find fan art.
    fanart = get_fanart(game_name)

    # Find game-specific screenshots.
    igdb_id = get_igdb_id(game_id, token)

    screenshots = get_screenshots(
        igdb_id, token
    )

    # Rotate images.
    image = choose_image(
        game_id,
        fanart,
        screenshots,
        state,
    )

    if image:
        print(
            f"Selected {image['type']}: {image['id']}"
        )
    else:
        print(
            "No suitable image. Posting text only."
        )

    # Announce before saving stream state.
    send_discord(stream, image)

    state["last_stream_id"] = current_id

    if image:
        history = state["image_history"].setdefault(
            str(game_id), []
        )

        history.append(image["id"])

        state["image_history"][str(game_id)] = (
            history[-100:]
        )

    save_state(state)

    print("Live announcement posted!")


if __name__ == "__main__":
    main()
