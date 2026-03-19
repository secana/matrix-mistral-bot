import logging
from datetime import date

from nio import AsyncClient, MatrixRoom, RoomMessageText

from search import call_mistral

log = logging.getLogger("mistral-bot")

USER_ID: str = ""
SYSTEM_PROMPT_TEMPLATE: str = ""
MAX_CONTEXT_MESSAGES: int = 20
matrix: AsyncClient | None = None
_trust_all_devices = None


def init(
    client: AsyncClient,
    user_id: str,
    system_prompt_template: str,
    max_context_messages: int,
    trust_all_devices_fn=None,
) -> None:
    global \
        matrix, \
        USER_ID, \
        SYSTEM_PROMPT_TEMPLATE, \
        MAX_CONTEXT_MESSAGES, \
        _trust_all_devices
    matrix = client
    USER_ID = user_id
    SYSTEM_PROMPT_TEMPLATE = system_prompt_template
    MAX_CONTEXT_MESSAGES = max_context_messages
    _trust_all_devices = trust_all_devices_fn


def is_mention(event: RoomMessageText) -> bool:
    formatted = event.source.get("content", {}).get("formatted_body", "")
    if USER_ID in formatted:
        return True
    body = event.body
    localpart = USER_ID.split(":")[0].lstrip("@")
    return USER_ID in body or f"@{localpart}" in body


def strip_mention(body: str) -> str:
    localpart = USER_ID.split(":")[0].lstrip("@")
    for mention in [USER_ID, f"@{localpart}"]:
        body = body.replace(mention, "")
    return body.lstrip(":, ").strip()


def get_thread_id(event: RoomMessageText) -> str | None:
    relates_to = event.source.get("content", {}).get("m.relates_to", {})
    if relates_to.get("rel_type") == "m.thread":
        return relates_to.get("event_id")
    return None


async def fetch_context(room: MatrixRoom, event: RoomMessageText) -> list[dict]:
    messages = []
    thread_id = get_thread_id(event)

    resp = await matrix.room_messages(
        room.room_id,
        start=None,
        limit=MAX_CONTEXT_MESSAGES,
    )

    if not hasattr(resp, "chunk"):
        return messages

    for evt in reversed(resp.chunk):
        if not isinstance(evt, RoomMessageText):
            continue

        if thread_id:
            evt_relates = evt.source.get("content", {}).get("m.relates_to", {})
            in_thread = (
                evt_relates.get("event_id") == thread_id or evt.event_id == thread_id
            )
            if not in_thread:
                continue

        if evt.event_id == event.event_id:
            continue

        role = "assistant" if evt.sender == USER_ID else "user"
        messages.append({"role": role, "content": evt.body})

    return messages


async def handle_message(room: MatrixRoom, event: RoomMessageText) -> None:
    if event.sender == USER_ID:
        return

    if not is_mention(event):
        return

    query = strip_mention(event.body)
    if not query:
        return

    log.info("Query from %s in %s: %s", event.sender, room.room_id, query[:100])

    await matrix.room_typing(room.room_id, typing_state=True)

    try:
        context = await fetch_context(room, event)
        system_prompt = SYSTEM_PROMPT_TEMPLATE.format(date=date.today().isoformat())
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(context)
        messages.append({"role": "user", "content": query})

        reply = await call_mistral(messages)

        if not reply:
            reply = "I couldn't generate a response. Please try again."

    except Exception:
        log.exception("Mistral API error")
        reply = "Sorry, I encountered an error processing your request."
    finally:
        await matrix.room_typing(room.room_id, typing_state=False)

    content = {
        "msgtype": "m.text",
        "body": reply,
    }
    thread_id = get_thread_id(event)
    if thread_id:
        content["m.relates_to"] = {
            "rel_type": "m.thread",
            "event_id": thread_id,
        }

    if _trust_all_devices:
        _trust_all_devices()
    if room.encrypted:
        await matrix.share_group_session(room.room_id)
    await matrix.room_send(room.room_id, "m.room.message", content)
    log.info("Replied in %s (%d chars)", room.room_id, len(reply))
