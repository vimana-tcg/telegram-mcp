"""Story → passport auto-responder for @godskyiflow.

Someone replies to one of our Telegram stories with the code word (e.g. "250")
→ they get the matching PDF passport + one qualifying question, and the founder
gets a note in Saved Messages. Each user gets each product once.

Polls recent private dialogs instead of relying on pushed updates, because the
same session is also used by several telegram-mcp containers at once.
"""
import asyncio, json, os, re, time
from telethon import TelegramClient, errors
from telethon.sessions import StringSession
from telethon.tl.functions.stories import GetStoriesByIDRequest
from telethon.tl.types import MessageReplyStoryHeader, User

CFG = os.environ.get("SP_CONFIG", "/app/config.json")
STATE = os.environ.get("SP_STATE", "/state/state.json")
REPOSTED = os.environ.get("SP_REPOSTED", "/state/reposted.json")  # written by repost.py
PASSPORTS = os.environ.get("SP_PASSPORTS", "/passports")
POLL = int(os.environ.get("SP_POLL", "30"))
MAX_PER_HOUR = int(os.environ.get("SP_MAX_PER_HOUR", "20"))  # keeps the account clear of spam limits
DRY = os.environ.get("SP_DRY") == "1"  # dry run: no sends, state is never written


def load(path, default):
    try:
        return json.load(open(path))
    except FileNotFoundError:
        return default
    except ValueError:  # truncated / corrupt file: keep it for inspection, start clean
        if not DRY:
            os.replace(path, path + ".bad")
        print("corrupt", path, "→ ignored")
        return default


def save(state):
    if DRY:
        return
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, STATE)


def has_code(text, code):
    return re.search(r"(?<!\d)" + re.escape(code) + r"(?!\d)", text or "") is not None


def who(u):
    # names are attacker-controlled: no control chars, bounded length (notes go out with parse_mode=None)
    name = " ".join(x for x in (u.first_name, u.last_name) if x) or str(u.id)
    name = re.sub(r"[\x00-\x1f\x7f]", " ", name)[:64]
    return name + (f" (@{u.username})" if u.username else "")


async def story_key(c, sid, cfg, state):
    """Product for one of our stories: config ids, ids from repost.py, else match by the story caption."""
    known = state.setdefault("story_map", {})
    key = (cfg.get("stories", {}).get(str(sid)) or known.get(str(sid))
           or load(REPOSTED, {}).get(str(sid)))
    if key:
        return key
    r = await c(GetStoriesByIDRequest(peer="me", id=[sid]))
    cap = next((getattr(s, "caption", None) or "" for s in r.stories), "")
    key = next((k for k, p in cfg["products"].items() if p.get("match") and p["match"] in cap), None)
    if key:
        known[str(sid)] = key
    return key


def sends_last_hour(state):
    now = time.time()
    state["sends"] = [t for t in state.get("sends", []) if now - t < 3600]
    return len(state["sends"])


async def handle(c, me_id, cfg, state, user, msg):
    r = msg.reply_to
    if msg.out or not isinstance(r, MessageReplyStoryHeader):
        return
    if getattr(r.peer, "user_id", None) != me_id:
        return
    codes = {p["code"] for p in cfg["products"].values()}
    if not any(has_code(msg.message, code) for code in codes):
        return
    key = await story_key(c, r.story_id, cfg, state)
    if not key:
        flag = state.setdefault("unknown_noted", [])
        if r.story_id not in flag:
            flag.append(r.story_id)
            save(state)
            await notify(c, f"❓ {who(user)} відповів на сторіс №{r.story_id}, але я не впізнав товар — відповідай сам")
        return
    p = cfg["products"][key]
    if not has_code(msg.message, p["code"]):
        return
    uid = str(user.id)
    done = state.setdefault("sent", {}).setdefault(uid, [])
    if key in done:
        noted = state.setdefault("repeat_noted", {}).setdefault(uid, [])
        if key not in noted:
            noted.append(key)
            save(state)
            await notify(c, f"🔁 {who(user)} ще раз попросив паспорт {p['title']} — вже надсилали, відповідай сам")
        return
    if sends_last_hour(state) >= MAX_PER_HOUR:
        await notify(c, f"⏸ {who(user)} просить паспорт {p['title']}, але ліміт {MAX_PER_HOUR}/год вичерпано — надішли сам")
        return
    # mark before sending: a crash mid-send means at most one missed PDF, never a duplicate
    done.append(key)
    state["sends"].append(time.time())
    save(state)
    if DRY:
        print("DRY would send", key, "to", uid)
    else:
        try:
            await c.send_file(user, os.path.join(PASSPORTS, p["file"]), caption=p["caption"], reply_to=msg.id)
        except errors.FloodWaitError:
            done.remove(key)
            save(state)
            raise
        except Exception as e:
            done.remove(key)
            save(state)
            await notify(c, f"⚠️ Не вдалося надіслати паспорт {p['title']} для {who(user)}: {type(e).__name__} — відповідай сам")
            return
    await notify(c, f"📄 {who(user)} відповів «{p['code']}» на сторіс → надіслано паспорт {p['title']}")


async def tick(c, me_id, cfg, state):
    started = time.time()
    since = state.get("since", started - 3600)
    last = state.setdefault("last_id", {})  # per dialog: newest message already handled
    async for d in c.iter_dialogs(limit=cfg.get("dialogs", 40)):
        if not isinstance(d.entity, User) or d.entity.bot or d.entity.is_self:
            continue
        if d.date and d.date.timestamp() < since:
            continue  # not break: pinned dialogs come first regardless of date
        uid = str(d.entity.id)
        msgs = []
        async for m in c.iter_messages(d, limit=50, min_id=last.get(uid, 0)):
            if uid not in last and m.date.timestamp() < since:
                break
            msgs.append(m)
        for m in reversed(msgs):  # oldest first, so last_id only moves past handled messages
            try:
                await handle(c, me_id, cfg, state, d.entity, m)
            except errors.FloodWaitError:
                save(state)
                raise
            except Exception as e:  # one bad message must not block everyone behind it
                print("message error:", uid, m.id, type(e).__name__, e)
            last[uid] = max(last.get(uid, 0), m.id)
    state["since"] = started - 60
    save(state)


async def notify(c, text):
    print(text)
    if DRY:
        return
    try:
        await c.send_message("me", text, parse_mode=None)
    except Exception as e:
        print("notify error:", type(e).__name__, e)


def check_config(cfg):
    matches = [p["match"] for p in cfg["products"].values() if p.get("match")]
    assert len(matches) == len(set(matches)), "duplicate match"
    for k, p in cfg["products"].items():
        assert os.path.isfile(os.path.join(PASSPORTS, p["file"])), p["file"]
        if "story" in p and p.get("match"):
            assert p["match"] in p["story"]["text"], f"{k}: match not in story text"


async def main():
    cfg = load(CFG, None)
    check_config(cfg)
    state = load(STATE, {})
    c = TelegramClient(StringSession(os.environ["TELEGRAM_SESSION_STRING"]),
                       int(os.environ["TELEGRAM_API_ID"]), os.environ["TELEGRAM_API_HASH"])
    await c.start()
    me = await c.get_me()
    print("story-passport up, products:", list(cfg["products"]), "dry" if DRY else "")
    while True:
        try:
            await tick(c, me.id, cfg, state)
        except errors.FloodWaitError as e:
            print("flood wait", e.seconds)
            await asyncio.sleep(e.seconds + 5)
        except Exception as e:  # keep running; systemd restarts on crash anyway
            print("tick error:", type(e).__name__, e)
        await asyncio.sleep(POLL)


if __name__ == "__main__":
    asyncio.run(main())
