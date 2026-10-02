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


__all__ = ["make_call"]
