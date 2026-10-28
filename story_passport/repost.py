"""Re-post the product stories from config.json (they expire after 24 h).

The responder recognises a story by its caption (products.<key>.match), so new
story ids need no config change. Usage: repost.sh [product key ...] (default: all).
"""
import asyncio, json, os, random, sys
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.stories import SendStoryRequest
from telethon.tl.types import DocumentAttributeVideo, InputMediaUploadedDocument, InputPrivacyValueAllowAll

CFG = os.environ.get("SP_CONFIG", "/app/config.json")
MEDIA = os.environ.get("SP_MEDIA", "/media")


async def main():
    cfg = json.load(open(CFG))
    keys = sys.argv[1:] or [k for k, p in cfg["products"].items() if "story" in p]
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
        print(f"posted {k}: story {ids[0] if ids else '?'}")
    await c.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
