# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#     "matrix-nio[e2e]>=0.24,<1.0",
#     "mistralai>=2.0,<3.0",
#     "duckduckgo-search>=7.0,<8.0",
# ]
# ///

import asyncio
import logging
import os
import sys

from nio import (
    AsyncClient,
    ClientConfig,
    InviteMemberEvent,
    KeyVerificationCancel,
    KeyVerificationKey,
    KeyVerificationMac,
    KeyVerificationStart,
    MatrixRoom,
    MegolmEvent,
    RoomMessageText,
    RoomMessageUnknown,
    UnknownEvent,
)

import chat
import cross_signing
import search
import verification

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("mistral-bot")

HOMESERVER = os.environ["MATRIX_HOMESERVER"]
USER_ID = os.environ["MATRIX_USER_ID"]
MATRIX_PASSWORD = os.environ["MATRIX_PASSWORD"]
STORE_PATH = os.environ.get("STORE_PATH", "./crypto_store")
MISTRAL_API_KEY = os.environ["MISTRAL_API_KEY"]
MISTRAL_MODEL = os.environ.get("MISTRAL_MODEL", "mistral-large-latest")
MAX_CONTEXT_MESSAGES = int(os.environ.get("MAX_CONTEXT_MESSAGES", "20"))
MAX_TOOL_ROUNDS = int(os.environ.get("MAX_TOOL_ROUNDS", "3"))
SYSTEM_PROMPT_TEMPLATE = os.environ.get(
    "SYSTEM_PROMPT",
    "You are a helpful assistant in a Matrix chat room. "
    "Today's date is {date}. "
    "You have access to a web_search tool to find current information. "
    "Use it when you need up-to-date facts, news, or information you're unsure about. "
    "Always use today's actual date when searching for current news or events. "
    "Keep answers concise and informative.",
)

os.makedirs(STORE_PATH, exist_ok=True)
client_config = ClientConfig(store_sync_tokens=True)
matrix = AsyncClient(HOMESERVER, USER_ID, store_path=STORE_PATH, config=client_config)


def trust_all_devices() -> None:
    """Trust all devices of all users in all joined rooms."""
    for room_id in matrix.rooms:
        room = matrix.rooms[room_id]
        for user_id in room.users:
            for device in matrix.device_store.active_user_devices(user_id):
                if not matrix.olm.is_device_verified(device):
                    matrix.verify_device(device)
                    log.info("Trusted device %s of %s", device.device_id, user_id)


# Initialize modules
search.init(MISTRAL_API_KEY, MISTRAL_MODEL, MAX_TOOL_ROUNDS)
chat.init(
    matrix, USER_ID, SYSTEM_PROMPT_TEMPLATE, MAX_CONTEXT_MESSAGES, trust_all_devices
)
verification.init(matrix, USER_ID, trust_all_devices)
cross_signing.init(matrix, USER_ID, HOMESERVER, STORE_PATH, MATRIX_PASSWORD)


async def handle_megolm(room: MatrixRoom, event: MegolmEvent) -> None:
    log.warning(
        "Could not decrypt message in %s from %s (session: %s)",
        room.room_id,
        event.sender,
        event.session_id,
    )


async def handle_invite(room: MatrixRoom, event: InviteMemberEvent) -> None:
    if event.state_key != USER_ID:
        return
    log.info("Invited to %s by %s, joining...", room.room_id, event.sender)
    await matrix.join(room.room_id)


async def main() -> None:
    matrix.add_event_callback(handle_invite, InviteMemberEvent)
    matrix.add_event_callback(handle_megolm, MegolmEvent)

    matrix.add_to_device_callback(verification.handle_start, KeyVerificationStart)
    matrix.add_to_device_callback(verification.handle_key, KeyVerificationKey)
    matrix.add_to_device_callback(verification.handle_mac, KeyVerificationMac)
    matrix.add_to_device_callback(verification.handle_cancel, KeyVerificationCancel)

    log.info("Logging in as %s...", USER_ID)
    device_id_file = os.path.join(STORE_PATH, "device_id")
    saved_device_id = None
    if os.path.exists(device_id_file):
        with open(device_id_file) as f:
            saved_device_id = f.read().strip()
        log.info("Reusing device_id: %s", saved_device_id)
    resp = await matrix.login(
        MATRIX_PASSWORD, device_name="MistralBot", device_id=saved_device_id
    )
    if hasattr(resp, "access_token"):
        log.info("Logged in, device_id: %s", resp.device_id)
        with open(device_id_file, "w") as f:
            f.write(resp.device_id)
    else:
        log.error("Login failed: %s", resp)
        return

    log.info("Bootstrapping cross-signing...")
    await cross_signing.bootstrap()

    log.info("Performing initial sync...")
    await matrix.sync(timeout=30000, full_state=True)
    log.info("Initial sync done.")

    # Second sync to pick up device keys from key query responses
    log.info("Second sync to fetch device keys...")
    await matrix.sync(timeout=10000)

    log.info("Trusting all devices...")
    trust_all_devices()

    # Claim one-time keys for all users to establish Olm sessions
    log.info("Claiming keys for room members...")
    for room_id in matrix.rooms:
        room = matrix.rooms[room_id]
        if room.encrypted:
            users = [u for u in room.users if u != USER_ID]
            if users:
                log.info(
                    "Sharing group session for %s with %d users", room_id, len(users)
                )
                await matrix.share_group_session(room_id)

    log.info("Listening for messages...")
    matrix.add_event_callback(chat.handle_message, RoomMessageText)
    matrix.add_event_callback(verification.handle_room_msg_unknown, RoomMessageUnknown)
    matrix.add_event_callback(verification.handle_room_unknown_event, UnknownEvent)
    await matrix.sync_forever(timeout=30000)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Shutting down")
        sys.exit(0)
