"""Story → passport auto-responder for @godskyiflow.

Someone replies to one of our Telegram stories with the code word (e.g. "250")
→ they get the matching PDF passport + one qualifying question, and the founder
gets a note in Saved Messages. Each user gets each product once.

Polls recent private dialogs instead of relying on pushed updates, because the
same session is also used by several telegram-mcp containers at once.
"""
import asyncio, json, os, re, time
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.stories import GetStoriesByIDRequest
from telethon.tl.types import MessageReplyStoryHeader, User

CFG = os.environ.get("SP_CONFIG", "/app/config.json")
STATE = os.environ.get("SP_STATE", "/state/state.json")
PASSPORTS = os.environ.get("SP_PASSPORTS", "/passports")
POLL = int(os.environ.get("SP_POLL", "30"))
DRY = os.environ.get("SP_DRY") == "1"


def load(path, default):
    try:
        return json.load(open(path))
    except FileNotFoundError:
        return default


def save(state):
    tmp = STATE + ".tmp"
    json.dump(state, open(tmp, "w"), ensure_ascii=False, indent=1)
    os.replace(tmp, STATE)


async def story_key(c, sid, cfg, state):
    """Product for one of our stories: config ids first, else match by the story caption."""
    known = state.setdefault("story_map", {})
    key = cfg["stories"].get(str(sid)) or known.get(str(sid))
    if key:
        return key
    r = await c(GetStoriesByIDRequest(peer="me", id=[sid]))
    cap = next((getattr(s, "caption", None) or "" for s in r.stories), "")
    key = next((k for k, p in cfg["products"].items() if p.get("match") and p["match"] in cap), None)
    if key:
        known[str(sid)] = key
    return key


def has_code(text, code):
    return re.search(r"(?<!\d)" + re.escape(code) + r"(?!\d)", text or "") is not None


async def tick(c, me_id, cfg, state):
    since = state.get("since", time.time() - 3600)
    newest = since
    async for d in c.iter_dialogs(limit=cfg.get("dialogs", 40)):
        if not isinstance(d.entity, User) or d.entity.bot or d.entity.is_self:
            continue
        if d.date and d.date.timestamp() <= since:
            break  # dialogs are sorted by last activity
        async for msg in c.iter_messages(d, limit=10):
            ts = msg.date.timestamp()
            if ts <= since:
                break
            newest = max(newest, ts)
            if msg.out:
                continue
            r = msg.reply_to
            if not isinstance(r, MessageReplyStoryHeader):
                continue
            if getattr(r.peer, "user_id", me_id) != me_id:
                continue
            key = await story_key(c, r.story_id, cfg, state)
            if not key:
                continue
            p = cfg["products"][key]
            if not has_code(msg.message, p["code"]):
                continue
            uid = str(d.entity.id)
            done = state.setdefault("sent", {}).setdefault(uid, [])
            name = " ".join(x for x in (d.entity.first_name, d.entity.last_name) if x) or uid
            who = name + (f" (@{d.entity.username})" if d.entity.username else "")
            if key in done:
                await notify(c, f"🔁 {who} ще раз попросив паспорт {p['title']} — вже надсилали, відповідай сам")
                continue
            if DRY:
                print("DRY would send", key, "to", uid)
            else:
                await c.send_file(d.entity, os.path.join(PASSPORTS, p["file"]), caption=p["caption"], reply_to=msg.id)
            done.append(key)
            save(state)
            await notify(c, f"📄 {who} відповів «{p['code']}» на сторіс → надіслано паспорт {p['title']}")
    state["since"] = newest
    save(state)


async def notify(c, text):
    print(text)
    if not DRY:
        await c.send_message("me", text)


async def main():
    cfg = load(CFG, None)
    state = load(STATE, {})
    for p in cfg["products"].values():
        assert os.path.isfile(os.path.join(PASSPORTS, p["file"])), p["file"]
    c = TelegramClient(StringSession(os.environ["TELEGRAM_SESSION_STRING"]),
                       int(os.environ["TELEGRAM_API_ID"]), os.environ["TELEGRAM_API_HASH"])
    await c.start()
    me = await c.get_me()
    print("story-passport up, stories:", cfg["stories"], "dry" if DRY else "")
    while True:
        try:
            await tick(c, me.id, cfg, state)
        except Exception as e:  # keep running; systemd restarts on crash anyway
            print("tick error:", type(e).__name__, e)
        await asyncio.sleep(POLL)


if __name__ == "__main__":
    asyncio.run(main())
