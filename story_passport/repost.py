"""Re-post the product stories from config.json (they expire after 24 h).

Each new story id is written to /state/reposted.json, so the responder knows the
product even if the first reply comes after the story expired.
Usage: repost.sh [product key ...] (default: all with a "story" block).
"""
import asyncio, json, os, random, sys
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.stories import SendStoryRequest
from telethon.tl.types import DocumentAttributeVideo, InputMediaUploadedDocument, InputPrivacyValueAllowAll

CFG = os.environ.get("SP_CONFIG", "/app/config.json")
MEDIA = os.environ.get("SP_MEDIA", "/media")
REPOSTED = os.environ.get("SP_REPOSTED", "/state/reposted.json")


def remember(story_id, key):
    try:
        seen = json.load(open(REPOSTED))
    except (FileNotFoundError, ValueError):
        seen = {}
    seen[str(story_id)] = key
    tmp = REPOSTED + ".tmp"
    json.dump(seen, open(tmp, "w"), indent=1)
    os.replace(tmp, REPOSTED)


async def main():
    cfg = json.load(open(CFG))
    keys = sys.argv[1:] or [k for k, p in cfg["products"].items() if "story" in p]
    # validate everything before the first post, so a typo never leaves half the stories posted
    for k in keys:
        p = cfg["products"].get(k)
        if not p or "story" not in p:
            sys.exit(f"unknown product or no story block: {k}")
        if not os.path.isfile(os.path.join(MEDIA, p["story"]["video"])):
            sys.exit(f"{k}: video not found: {p['story']['video']}")
        if p.get("match") and p["match"] not in p["story"]["text"]:
            sys.exit(f"{k}: match '{p['match']}' is not in the story text")
    c = TelegramClient(StringSession(os.environ["TELEGRAM_SESSION_STRING"]),
                       int(os.environ["TELEGRAM_API_ID"]), os.environ["TELEGRAM_API_HASH"])
    await c.start()
    for k in keys:
        s = cfg["products"][k]["story"]
        f = await c.upload_file(os.path.join(MEDIA, s["video"]))
        med = InputMediaUploadedDocument(file=f, mime_type="video/mp4", attributes=[
            DocumentAttributeVideo(duration=s["duration"], w=720, h=1280, supports_streaming=True)])
        r = await c(SendStoryRequest(peer="me", media=med, privacy_rules=[InputPrivacyValueAllowAll()],
                                     caption=s["text"], period=86400, random_id=random.randint(1, 2**62)))
        ids = [u.id for u in getattr(r, "updates", []) if type(u).__name__ == "UpdateStoryID"]
        if ids:
            remember(ids[0], k)
        print(f"posted {k}: story {ids[0] if ids else '?'}")
    await c.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
