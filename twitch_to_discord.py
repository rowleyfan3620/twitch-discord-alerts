
import json
import os
import random
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# ============================================================
# DIDACTICJAM - TWITCH TO DISCORD
# Automatically finds fan art and gameplay screenshots
# ============================================================

CHANNEL = "didacticjam"
CHANNEL_URL = f"https://www.twitch.tv/{CHANNEL}"
STATE_FILE = Path("twitch_state.json")

TWITCH_CLIENT_ID = os.environ["TWITCH_CLIENT_ID"]
TWITCH_CLIENT_SECRET = os.environ["TWITCH_CLIENT_SECRET"]
DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]

# Target artwork mix when both categories are available.
FANART_WEIGHT = 55
SCREENSHOT_WEIGHT = 45

# Openverse reliability settings.
OPENVERSE_TIMEOUT = 12
OPENVERSE_ATTEMPTS = 3
OPENVERSE_TOTAL_BUDGET = 45

# Strict game-name matching reduces unrelated results.
STRICT_GAME_MATCHING = True

# No wiki-sourced images.
BLOCKED_DOMAINS = (
    "fandom.com",
    "wikia.com",
    "wikipedia.org",
    "wikimedia.org",
)

# Openly licensed images only.
ALLOWED_LICENSES = {"by", "by-sa", "cc0"}


# ============================================================
# REQUEST HELPERS
# ============================================================

def request_json(url, data=None, headers=None, timeout=30):
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "User-Agent": "DidacticJamArtBot/1.0",
            "Accept": "application/json",
            **(headers or {}),
        },
    )

    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


def retry_openverse_request(url, data=None, headers=None,
                           deadline=None, label="Openverse"):
    """
    Retry transient Openverse errors while respecting a
    total time budget, so live alerts aren't delayed forever.
    """

    for attempt in range(1, OPENVERSE_ATTEMPTS + 1):
        remaining = (
            deadline - time.monotonic()
            if deadline is not None
            else OPENVERSE_TIMEOUT
        )

        if remaining <= 0:
            raise TimeoutError("Openverse time budget exceeded")

        timeout = min(OPENVERSE_TIMEOUT, remaining)

        try:
            print(
                f"{label}: attempt "
                f"{attempt}/{OPENVERSE_ATTEMPTS}",
                flush=True,
            )

            return request_json(
                url,
                data=data,
                headers=headers,
                timeout=timeout,
            )

        except urllib.error.HTTPError as error:
            # Retry only transient API failures.
            if error.code not in (429, 500, 502, 503, 504):
                raise

            print(
                f"{label}: HTTP {error.code}",
                flush=True,
            )

        except (
            TimeoutError,
            socket.timeout,
            urllib.error.URLError,
        ) as error:
            print(
                f"{label}: {error}",
                flush=True,
            )

        if attempt == OPENVERSE_ATTEMPTS:
            break

        delay = attempt * 2

        if deadline is not None:
            delay = min(
                delay,
                max(0, deadline - time.monotonic()),
            )

        if delay > 0:
            time.sleep(delay)

    raise TimeoutError(f"{label} failed after retries")


# ============================================================
# TWITCH AUTHENTICATION
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


# ============================================================
# TWITCH STREAM AND GAME DETECTION
# ============================================================

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

    query = urllib.parse.urlencode({
        "id": game_id,
    })

    result = request_json(
        f"https://api.twitch.tv/helix/games?{query}",
        headers=twitch_headers(token),
    )

    games = result.get("data", [])

    return games[0].get("igdb_id") if games else None


# ============================================================
# IMAGE SAFETY AND MATCHING
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
    if not isinstance(url, str):
        return False

    try:
        parsed = urllib.parse.urlsplit(url)

        return (
            parsed.scheme == "https"
            and bool(parsed.hostname)
            and not forbidden_url(url)
        )

    except ValueError:
        return False


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

    title_words = title.split()

    return all(
        word in title_words
        for word in game.split()
        if len(word) > 2
    )


# ============================================================
# OPENVERSE AUTHENTICATION
# ============================================================

def get_openverse_token(deadline):
    client_id = os.environ["OPENVERSE_CLIENT_ID"]
    client_secret = os.environ["OPENVERSE_CLIENT_SECRET"]

    body = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    }).encode("utf-8")

    result = retry_openverse_request(
        "https://api.openverse.org/v1/auth_tokens/token/",
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
        },
        deadline=deadline,
        label="Openverse authentication",
    )

    print(
        "Openverse authentication successful",
        flush=True,
    )

    return result["access_token"]


# ============================================================
# FAN ART DISCOVERY
# ============================================================

def get_fanart(game_name):
    if not game_name:
        return []

    # Bound the entire authentication + search operation.
    deadline = (
        time.monotonic() + OPENVERSE_TOTAL_BUDGET
    )

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
        openverse_token = get_openverse_token(deadline)

        result = retry_openverse_request(
            url,
            headers={
                "Authorization": f"Bearer {openverse_token}",
                "Accept": "application/json",
            },
            deadline=deadline,
            label="Openverse image search",
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

            # Exclude items explicitly flagged as mature.
            if item.get("mature") is not False:
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

            # Discord embeds work best with landscape art.
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
                "creator": str(creator),
                "license": license_name.upper(),
                "license_url": item.get("license_url"),
            })

        print(
            f"Found {len(images)} fan-art candidates",
            flush=True,
        )

        return images

    except urllib.error.HTTPError as error:
        print(
            f"Fan-art lookup failed: HTTP {error.code}",
            flush=True,
        )
        return []

    except Exception as error:
        print(
            f"Fan-art lookup failed: {error}",
            flush=True,
        )
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
            f"Found {len(results)} gameplay screenshots",
            flush=True,
        )

        return results

    except Exception as error:
        print(
            f"Screenshot lookup failed: {error}",
            flush=True,
        )
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

    # Avoid already-used images until the pool is exhausted.
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
                f"Discord webhook status: {response.status}",
                flush=True,
            )

    except urllib.error.HTTPError as error:
        details = error.read().decode(
            "utf-8",
            errors="replace",
        )

        print(
            f"Discord webhook failed: HTTP {error.code}",
            flush=True,
        )

        print(
            f"Discord response: {details[:1000]}",
            flush=True,
        )

        raise


# ============================================================
# PERSISTENT STREAM + IMAGE HISTORY
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

    # Don't post an already-live stream on first setup.
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
        f"Game: {game_name} | Twitch ID: {game_id}",
        flush=True,
    )

    # Fan-art search; failure doesn't stop the alert.
    fanart = get_fanart(game_name)

    # Known-working IGDB screenshot fallback.
    try:
        igdb_id = get_igdb_id(game_id, token)
        screenshots = get_screenshots(igdb_id, token)
    except Exception as error:
        print(f"IGDB lookup failed: {error}")
        screenshots = []

    image = choose_image(
        game_id,
        fanart,
        screenshots,
        state,
    )

    if image:
        print(
            f"Selected {image['type']}: {image['id']}",
            flush=True,
        )
    else:
        print("No suitable image. Posting text only.")

    # Post before marking the stream as announced.
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

    print(
        "Live announcement posted!",
        flush=True,
    )


if __name__ == "__main__":
    main()
