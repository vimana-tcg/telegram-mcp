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



# --- Live two-way conversation, bridged to an ElevenLabs conversational agent ---

_TALK_RATE = 48000  # agent is configured for pcm_48000 in and out, same as the call
_TALK_FRAME_BYTES = _TALK_RATE // 100 * 2  # 10 ms of mono s16le
_TALK_SILENCE = b"\x00" * _TALK_FRAME_BYTES


async def _elevenlabs_signed_url() -> str:
    import httpx

    api_key = os.environ.get("ELEVENLABS_API_KEY", "")
    agent_id = os.environ.get("ELEVENLABS_CALL_AGENT_ID", "")
    if not api_key or not agent_id:
        raise RuntimeError("ELEVENLABS_API_KEY / ELEVENLABS_CALL_AGENT_ID are not set")
    async with httpx.AsyncClient(timeout=20) as http:
        resp = await http.get(
            "https://api.elevenlabs.io/v1/convai/conversation/get-signed-url",
            params={"agent_id": agent_id},
            headers={"xi-api-key": api_key},
        )
        resp.raise_for_status()
        return resp.json()["signed_url"]


@mcp.tool(
    annotations=ToolAnnotations(title="Talk Call", openWorldHint=True, destructiveHint=True)
)
@with_account(readonly=False)
@validate_id("user_id")
async def talk_call(
    user_id: Union[int, str],
    goal: str,
    first_message: str = "Алло, привет!",
    ring_timeout: int = 45,
    max_seconds: int = 300,
    account: str = None,
) -> str:
    """
    Call a user and hold a live voice conversation via an ElevenLabs agent.

    The agent speaks in the configured cloned voice and follows `goal`
    (what to find out / say). Returns the conversation transcript.
    Only users listed in TELEGRAM_CALL_ALLOWED_USER_IDS can be called.

    Args:
        user_id: Target user id or username.
        goal: Instructions for this call (who is called, what to find out).
        first_message: First phrase the agent says when the user answers.
        ring_timeout: Seconds to wait for the user to answer (10-90).
        max_seconds: Hard limit on conversation length (30-900).
    """
    try:
        import base64
        import json as _json

        import websockets
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

        if not 10 <= ring_timeout <= 90:
            return "ring_timeout must be between 10 and 90 seconds."
        if not 30 <= max_seconds <= 900:
            return "max_seconds must be between 30 and 900 seconds."

        cl = get_client(account)
        await ensure_connected(cl)
        user = await resolve_entity(user_id, cl)
        if not isinstance(user, types.User) or user.bot:
            return "Calls are only possible to real users."
        if user.id not in _CALL_ALLOWED_USER_IDS:
            return f"User {user.id} is not in TELEGRAM_CALL_ALLOWED_USER_IDS; call refused."

        signed_url = await _elevenlabs_signed_url()

        if _call_lock.locked():
            return "Another call is in progress; try again later."
        async with _call_lock:
            engine = await _get_call_engine(account or "default", cl)
            params = AudioParameters(_TALK_RATE, 1)
            hung_up = asyncio.Event()
            incoming: asyncio.Queue = asyncio.Queue(maxsize=500)
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

            try:
                await engine.play(
                    user.id,
                    MediaStream(ExternalMedia.AUDIO, audio_parameters=params),
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

            started = time.monotonic()
            try:
                await engine.record(user.id, RecordStream(audio=True, audio_parameters=params))
                async with websockets.connect(signed_url, max_size=None) as ws:
                    await ws.send(
                        _json.dumps(
                            {
                                "type": "conversation_initiation_client_data",
                                "conversation_config_override": {
                                    "agent": {
                                        "first_message": first_message,
                                        "prompt": {"prompt": _talk_prompt(goal)},
                                    }
                                },
                            }
                        )
                    )

                    async def _uplink():
                        # Telegram -> agent, batched to ~100 ms
                        buf = bytearray()
                        while True:
                            buf += await incoming.get()
                            if len(buf) >= _TALK_FRAME_BYTES * 10:
                                await ws.send(
                                    _json.dumps(
                                        {"user_audio_chunk": base64.b64encode(bytes(buf)).decode()}
                                    )
                                )
                                buf.clear()

                    async def _downlink():
                        # agent -> buffer; transcripts and control events
                        async for raw in ws:
                            msg = _json.loads(raw)
                            kind = msg.get("type")
                            if kind == "audio":
                                outgoing.extend(
                                    base64.b64decode(msg["audio_event"]["audio_base_64"])
                                )
                            elif kind == "interruption":
                                outgoing.clear()
                            elif kind == "user_transcript":
                                text = msg["user_transcription_event"]["user_transcript"]
                                transcript.append(f"Собеседник: {text}")
                            elif kind == "agent_response":
                                text = msg["agent_response_event"]["agent_response"]
                                transcript.append(f"Агент: {text}")
                            elif kind == "ping":
                                await ws.send(
                                    _json.dumps(
                                        {"type": "pong", "event_id": msg["ping_event"]["event_id"]}
                                    )
                                )
                        end_reason["value"] = "agent ended the conversation"

                    async def _speaker():
                        # buffer -> Telegram, paced in real time, 10 ms frames
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

                    async def _wait_hangup():
                        await hung_up.wait()

                    tasks = [
                        asyncio.create_task(t())
                        for t in (_uplink, _downlink, _speaker, _wait_hangup)
                    ]
                    done, pending = await asyncio.wait(
                        tasks, timeout=max_seconds, return_when=asyncio.FIRST_COMPLETED
                    )
                    downlink_task = tasks[1]
                    if downlink_task in done and not hung_up.is_set():
                        # let the goodbye phrase finish playing before hanging up
                        deadline = time.monotonic() + 8
                        while len(outgoing) >= _TALK_FRAME_BYTES and time.monotonic() < deadline:
                            await asyncio.sleep(0.1)
                    for t in tasks:
                        t.cancel()
                    for t in done:
                        if t.exception() and not isinstance(t.exception(), websockets.ConnectionClosed):
                            end_reason["value"] = f"error: {t.exception()!r}"
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
            lines = "\n".join(transcript) or "(no speech recognised)"
            return f"Call to {user.id} ended ({end_reason['value']}, ~{talked}s).\n\n{lines}"
    except Exception as e:
        return log_and_format_error("talk_call", e, user_id=user_id)


def _talk_prompt(goal: str) -> str:
    return (
        "Ты — голосовой ИИ-помощник Михаила Корогодского и звонишь от его имени по Telegram. "
        "Говори по-русски, коротко и живо, как в обычном телефонном разговоре: одна-две фразы за раз. "
        "Если спросят, кто ты, честно скажи, что ты ИИ-помощник Михаила. "
        "Ничего не обещай от имени Михаила (деньги, сроки, решения) — скажи, что передашь ему. "
        "Когда цель звонка достигнута или собеседник хочет закончить, вежливо попрощайся и заверши звонок.\n\n"
        f"Цель этого звонка: {goal}"
    )


__all__ = ["make_call", "talk_call"]
