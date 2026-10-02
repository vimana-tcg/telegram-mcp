"""Private (1-to-1) voice call MCP tools, backed by py-tgcalls."""

from telegram_mcp.runtime import *

# Outgoing calls are opt-in per user: only ids listed here can be called.
# Empty/unset -> the tool refuses every call.
_CALL_ALLOWED_USER_IDS = {
    int(x)
    for x in os.environ.get("TELEGRAM_CALL_ALLOWED_USER_IDS", "").replace(" ", "").split(",")
    if x.lstrip("-").isdigit()
}

_call_engines: dict = {}
_call_lock = asyncio.Lock()


async def _get_call_engine(label: str, cl):
    """One started PyTgCalls instance per Telegram account, created lazily."""
    engine = _call_engines.get(label)
    if engine is None:
        from pytgcalls import PyTgCalls

        engine = PyTgCalls(cl)
        await engine.start()
        _call_engines[label] = engine
    return engine


@mcp.tool(
    annotations=ToolAnnotations(title="Make Call", openWorldHint=True, destructiveHint=True)
)
@with_account(readonly=False)
@validate_id("user_id")
async def make_call(
    user_id: Union[int, str],
    file_path: str,
    ring_timeout: int = 45,
    max_seconds: int = 120,
    ctx: Optional[Context] = None,
    account: str = None,
) -> str:
    """
    Place a private voice call to a user and play an audio file once they answer.

    The call ends when the audio finishes, the user hangs up, or max_seconds passes.
    Only users listed in TELEGRAM_CALL_ALLOWED_USER_IDS can be called.

    Args:
        user_id: Target user id or username.
        file_path: Audio file (.mp3/.ogg/.opus/.wav/.m4a) under allowed roots.
        ring_timeout: Seconds to wait for the user to answer (10-90).
        max_seconds: Hard limit on call length after answer (5-600).
    """
    try:
        from pytgcalls import filters as call_filters
        from pytgcalls.exceptions import CallBusy, CallDeclined, CallDiscarded, TimedOutAnswer
        from pytgcalls.types import CallConfig, ChatUpdate, MediaStream

        if not 10 <= ring_timeout <= 90:
            return "ring_timeout must be between 10 and 90 seconds."
        if not 5 <= max_seconds <= 600:
            return "max_seconds must be between 5 and 600 seconds."

        cl = get_client(account)
        await ensure_connected(cl)
        user = await resolve_entity(user_id, cl)
        if not isinstance(user, types.User) or user.bot:
            return "Calls are only possible to real users."
        if user.id not in _CALL_ALLOWED_USER_IDS:
            return (
                f"User {user.id} is not in TELEGRAM_CALL_ALLOWED_USER_IDS; call refused."
            )

        safe_path, path_error = await _resolve_readable_file_path(
            raw_path=file_path,
            ctx=ctx,
            tool_name="make_call",
        )
        if path_error:
            return path_error

        if _call_lock.locked():
            return "Another call is in progress; try again later."
        async with _call_lock:
            engine = await _get_call_engine(account or "default", cl)
            finished = asyncio.Event()
            reason = {"value": "max_seconds reached"}

            async def _on_end(_, update):
                if getattr(update, "chat_id", None) != user.id:
                    return
                if isinstance(update, ChatUpdate):
                    reason["value"] = "user hung up"
                else:
                    reason["value"] = "audio finished"
                finished.set()

            handler = engine.on_update(
                call_filters.stream_end()
                | call_filters.chat_update(ChatUpdate.Status.DISCARDED_CALL)
            )(_on_end)

            started = time.monotonic()
            try:
                await engine.play(
                    user.id,
                    MediaStream(str(safe_path), video_flags=MediaStream.Flags.IGNORE),
                    CallConfig(timeout=ring_timeout),
                )
            except TimedOutAnswer:
                return f"No answer from {user.id} within {ring_timeout}s."
            except CallDeclined:
                return f"User {user.id} declined the call."
            except CallBusy:
                return f"User {user.id} is busy on another call."
            except CallDiscarded:
                return f"Call to {user.id} was discarded before it connected."

            try:
                await asyncio.wait_for(finished.wait(), timeout=max_seconds)
            except asyncio.TimeoutError:
                pass
            finally:
                try:
                    await engine.leave_call(user.id)
                except Exception:
                    pass
                remove = getattr(engine, "remove_handler", None)
                if callable(remove):
                    try:
                        remove(handler)
                    except Exception:
                        pass
            talked = int(time.monotonic() - started)
            return f"Call to {user.id} answered and ended ({reason['value']}, ~{talked}s)."
    except Exception as e:
        return log_and_format_error("make_call", e, user_id=user_id, file_path=file_path)



# --- Live two-way conversation: ElevenLabs ears and mouth, Claude (Claude Code subscription) brain ---

_TALK_RATE = 48000  # call audio: 48 kHz mono s16le
_TALK_FRAME_BYTES = _TALK_RATE // 100 * 2  # 10 ms
_TALK_SILENCE = b"\x00" * _TALK_FRAME_BYTES
_TALK_END_MARK = "[КОНЕЦ]"
_TALK_MODEL = os.environ.get("TALK_CALL_LLM", "sonnet")
_TALK_ROBOT_VOICE = os.environ.get("ELEVENLABS_ROBOT_VOICE_ID", "pNInz6obpgDQGcFmaJgB")


def _rms(pcm: bytes) -> float:
    from array import array

    samples = array("h", pcm)
    if not samples:
        return 0.0
    return (sum(x * x for x in samples) / len(samples)) ** 0.5


def _upsample_2x(pcm24: bytes) -> bytes:
    """24 kHz -> 48 kHz by sample doubling (fine for speech)."""
    from array import array

    src = array("h", pcm24[: len(pcm24) // 2 * 2])
    out = array("h", bytes(len(src) * 4))
    out[0::2] = src
    out[1::2] = src
    return out.tobytes()


def _wav_bytes(pcm: bytes) -> bytes:
    import io
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(_TALK_RATE)
        w.writeframes(pcm)
    return buf.getvalue()


def _talk_prompt(goal: str) -> str:
    return (
        "Ты — голосовой ИИ-помощник Михаила Корогодского и сейчас говоришь по телефону (Telegram-звонок) от его имени. "
        "Твои ответы озвучиваются голосом, поэтому: только разговорная речь, без списков, эмодзи и markdown; "
        "одна-две короткие фразы за раз, как живой человек по телефону. Говори по-русски. "
        "Если спросят, кто ты, честно скажи, что ты ИИ-помощник Михаила. "
        "Ничего не обещай от имени Михаила (деньги, сроки, решения) — скажи, что передашь ему. "
        "Реплики собеседника приходят из распознавания речи и могут быть с ошибками — понимай по смыслу. "
        "Самое первое сообщение — техническая проверка до звонка: ответь на него одним словом «готов». "
        f"Когда цель достигнута или собеседник прощается, попрощайся и добавь в самом конце {_TALK_END_MARK}.\n\n"
        f"Цель этого звонка: {goal}"
    )


async def _transcribe(http, pcm: bytes) -> str:
    resp = await http.post(
        "https://api.elevenlabs.io/v1/speech-to-text",
        headers={"xi-api-key": os.environ["ELEVENLABS_API_KEY"]},
        data={"model_id": "scribe_v1", "language_code": "rus", "tag_audio_events": "false"},
        files={"file": ("speech.wav", _wav_bytes(pcm), "audio/wav")},
    )
    resp.raise_for_status()
    return (resp.json().get("text") or "").strip()


class _ClaudeBrain:
    """One headless Claude Code session per call; runs on the Claude subscription."""

    def __init__(self, system: str):
        self.system = system
        self.proc = None
        self.ready = None

    async def start(self) -> None:
        self.proc = await asyncio.create_subprocess_exec(
            "claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json",
            "--verbose", "--model", _TALK_MODEL, "--tools", "", "--strict-mcp-config",
            "--setting-sources", "", "--no-session-persistence", "--system-prompt", self.system,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, cwd="/tmp",
            env={**os.environ, "DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"},
        )
        # warm up while the phone rings: the first turn pays the startup cost
        self.ready = asyncio.create_task(self._turn("(техническая проверка связи перед звонком, ответь одним словом: готов)"))

    async def _turn(self, text: str) -> str:
        import json

        msg = {"type": "user", "message": {"role": "user", "content": text}}
        self.proc.stdin.write((json.dumps(msg, ensure_ascii=False) + "\n").encode())
        await self.proc.stdin.drain()
        while True:
            line = await self.proc.stdout.readline()
            if not line:
                raise RuntimeError("claude process exited")
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") == "result":
                if event.get("is_error"):
                    raise RuntimeError(f"claude error: {event.get('result')}")
                return (event.get("result") or "").strip()

    async def ask(self, text: str) -> str:
        await self.ready
        return await self._turn(text)

    async def close(self) -> None:
        if self.proc and self.proc.returncode is None:
            self.proc.kill()
            await self.proc.wait()


async def _speak(http, text: str, voice: str, sink, interrupted: asyncio.Event) -> None:
    """Stream TTS (24 kHz PCM) into `sink` as 48 kHz, stopping on barge-in."""
    voice_id = _TALK_ROBOT_VOICE if voice == "robot" else os.environ["ELEVENLABS_VOICE_ID"]
    req = http.stream(
        "POST",
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream",
        params={"output_format": "pcm_24000"},
        headers={"xi-api-key": os.environ["ELEVENLABS_API_KEY"]},
        json={
            "text": text,
            "model_id": "eleven_flash_v2_5",
            "voice_settings": {"stability": 0.72, "similarity_boost": 0.8, "style": 0.15},
        },
    )
    tail = b""
    async with req as resp:
        resp.raise_for_status()
        async for chunk in resp.aiter_bytes():
            if interrupted.is_set():
                return
            chunk = tail + chunk
            cut = len(chunk) // 2 * 2
            tail = chunk[cut:]
            sink.extend(_upsample_2x(chunk[:cut]))


@mcp.tool(
    annotations=ToolAnnotations(title="Talk Call", openWorldHint=True, destructiveHint=True)
)
@with_account(readonly=False)
@validate_id("user_id")
async def talk_call(
    user_id: Union[int, str],
    goal: str,
    first_message: str = "Алло, привет!",
    voice: str = "robot",
    ring_timeout: int = 45,
    max_seconds: int = 300,
    account: str = None,
) -> str:
    """
    Call a user and hold a live voice conversation about `goal`.

    Speech recognition: ElevenLabs Scribe. Replies: Claude via the Claude Code
    CLI (subscription token in CLAUDE_CODE_OAUTH_TOKEN). Voice: the cloned
    ElevenLabs voice ("clone") or a stock ElevenLabs voice ("robot").
    Returns the transcript.
    Only users listed in TELEGRAM_CALL_ALLOWED_USER_IDS can be called.

    Args:
        user_id: Target user id or username.
        goal: Instructions for this call (who is called, what to find out).
        first_message: First phrase said when the user answers.
        voice: "robot" (stock voice) or "clone" (founder's cloned voice, paid plan).
        ring_timeout: Seconds to wait for the user to answer (10-90).
        max_seconds: Hard limit on conversation length (30-900).
    """
    try:
        import httpx
        from pytgcalls import filters as call_filters
        from pytgcalls.exceptions import CallBusy, CallDeclined, CallDiscarded, TimedOutAnswer
        from pytgcalls.types import (
            CallConfig,
            ChatUpdate,
            Device,
            Direction,
            ExternalMedia,
            MediaStream,
            RecordStream,
            StreamFrames,
        )
        from pytgcalls.types.raw import AudioParameters

        if voice not in ("clone", "robot"):
            return 'voice must be "clone" or "robot".'
        if not 10 <= ring_timeout <= 90:
            return "ring_timeout must be between 10 and 90 seconds."
        if not 30 <= max_seconds <= 900:
            return "max_seconds must be between 30 and 900 seconds."
        needed = ["ELEVENLABS_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"]
        if voice == "clone":
            needed.append("ELEVENLABS_VOICE_ID")
        missing = [k for k in needed if not os.environ.get(k)]
        if missing:
            return f"Missing env: {', '.join(missing)}"

        cl = get_client(account)
        await ensure_connected(cl)
        user = await resolve_entity(user_id, cl)
        if not isinstance(user, types.User) or user.bot:
            return "Calls are only possible to real users."
        if user.id not in _CALL_ALLOWED_USER_IDS:
            return f"User {user.id} is not in TELEGRAM_CALL_ALLOWED_USER_IDS; call refused."

        if _call_lock.locked():
            return "Another call is in progress; try again later."
        async with _call_lock:
            engine = await _get_call_engine(account or "default", cl)
            params = AudioParameters(_TALK_RATE, 1)
            hung_up = asyncio.Event()
            incoming: asyncio.Queue = asyncio.Queue(maxsize=3000)
            outgoing = bytearray()
            transcript: list = []
            end_reason = {"value": "max_seconds reached"}

            async def _on_update(_, update):
                if getattr(update, "chat_id", None) != user.id:
                    return
                if isinstance(update, StreamFrames):
                    for fr in update.frames:
                        if incoming.full():
                            incoming.get_nowait()
                        incoming.put_nowait(fr.frame)
                elif isinstance(update, ChatUpdate):
                    end_reason["value"] = "user hung up"
                    hung_up.set()

            handler = engine.on_update(
                call_filters.stream_frame(Direction.INCOMING)
                | call_filters.chat_update(ChatUpdate.Status.DISCARDED_CALL)
            )(_on_update)

            brain = _ClaudeBrain(_talk_prompt(goal))
            await brain.start()
            try:
                await engine.play(
                    user.id,
                    MediaStream(ExternalMedia.AUDIO, audio_parameters=params),
                    CallConfig(timeout=ring_timeout),
                )
            except BaseException as e:
                await brain.close()
                if isinstance(e, TimedOutAnswer):
                    return f"No answer from {user.id} within {ring_timeout}s."
                if isinstance(e, CallDeclined):
                    return f"User {user.id} declined the call."
                if isinstance(e, CallBusy):
                    return f"User {user.id} is busy on another call."
                if isinstance(e, CallDiscarded):
                    return f"Call to {user.id} was discarded before it connected."
                raise

            started = time.monotonic()
            utterances: asyncio.Queue = asyncio.Queue()
            interrupted = asyncio.Event()
            finished = asyncio.Event()
            pending = [f"(собеседник взял трубку, ты уже сказал: «{first_message}»)"]

            async def _speaker():
                # outgoing buffer -> call, paced in real time, 10 ms frames
                tick = time.monotonic()
                while True:
                    if len(outgoing) >= _TALK_FRAME_BYTES:
                        chunk = bytes(outgoing[:_TALK_FRAME_BYTES])
                        del outgoing[:_TALK_FRAME_BYTES]
                    else:
                        chunk = _TALK_SILENCE
                    await engine.send_frame(user.id, Device.MICROPHONE, chunk)
                    tick += 0.01
                    await asyncio.sleep(max(0.0, tick - time.monotonic()))

            async def _listener():
                # energy VAD: split incoming audio into utterances
                buf = bytearray()
                speech, pre, silence_ms, voiced_ms = bytearray(), [], 0, 0
                noise = 300.0
                in_speech = False
                while True:
                    buf += await incoming.get()
                    while len(buf) >= _TALK_FRAME_BYTES:
                        frame = bytes(buf[:_TALK_FRAME_BYTES])
                        del buf[:_TALK_FRAME_BYTES]
                        level = _rms(frame)
                        loud = level > max(500.0, noise * 3)
                        if not in_speech and not loud:
                            noise = noise * 0.98 + level * 0.02
                        if not in_speech:
                            pre.append(frame)
                            pre = pre[-25:]  # 250 ms pre-roll
                            voiced_ms = voiced_ms + 10 if loud else 0
                            if voiced_ms >= 60:
                                in_speech, silence_ms = True, 0
                                speech = bytearray(b"".join(pre))
                                if len(outgoing) > _TALK_FRAME_BYTES * 30:
                                    interrupted.set()  # barge-in: stop talking
                                    outgoing.clear()
                        else:
                            speech += frame
                            silence_ms = 0 if loud else silence_ms + 10
                            if silence_ms >= 700 or len(speech) > _TALK_FRAME_BYTES * 2000:
                                in_speech, voiced_ms = False, 0
                                if len(speech) >= _TALK_FRAME_BYTES * 40:
                                    utterances.put_nowait(bytes(speech))
                                pre = []

            async def _brain():
                async with httpx.AsyncClient(timeout=30) as http:
                    interrupted.clear()
                    await _speak(http, first_message, voice, outgoing, interrupted)
                    transcript.append(f"Агент: {first_message}")
                    while True:
                        pcm = await utterances.get()
                        text = await _transcribe(http, pcm)
                        if not text:
                            continue
                        transcript.append(f"Собеседник: {text}")
                        pending.append(text)
                        if not utterances.empty():
                            continue  # they kept talking; answer the whole thing
                        reply = await brain.ask(" ".join(pending))
                        pending.clear()
                        done = _TALK_END_MARK in reply
                        reply = reply.replace(_TALK_END_MARK, "").strip()
                        if reply:
                            transcript.append(f"Агент: {reply}")
                            interrupted.clear()
                            await _speak(http, reply, voice, outgoing, interrupted)
                        if done:
                            end_reason["value"] = "agent finished the conversation"
                            finished.set()
                            return

            tasks = [asyncio.create_task(c()) for c in (_speaker, _listener, _brain)]
            waiters = [asyncio.create_task(hung_up.wait()), asyncio.create_task(finished.wait())]
            try:
                await engine.record(user.id, RecordStream(audio=True, audio_parameters=params))
                done_set, _ = await asyncio.wait(
                    tasks + waiters, timeout=max_seconds, return_when=asyncio.FIRST_COMPLETED
                )
                if finished.is_set():
                    # let the goodbye finish playing
                    deadline = time.monotonic() + 10
                    while len(outgoing) >= _TALK_FRAME_BYTES and time.monotonic() < deadline:
                        await asyncio.sleep(0.1)
                for t in done_set:
                    if t in tasks and t.exception():
                        end_reason["value"] = f"error: {t.exception()!r}"
            finally:
                for t in tasks + waiters:
                    t.cancel()
                await brain.close()
                try:
                    await engine.leave_call(user.id)
                except Exception:
                    pass
                remove = getattr(engine, "remove_handler", None)
                if callable(remove):
                    try:
                        remove(handler)
                    except Exception:
                        pass
            talked = int(time.monotonic() - started)
            lines = "\n".join(transcript) or "(no speech recognised)"
            return f"Call to {user.id} ended ({end_reason['value']}, ~{talked}s).\n\n{lines}"
    except Exception as e:
        return log_and_format_error("talk_call", e, user_id=user_id)


__all__ = ["make_call", "talk_call"]
