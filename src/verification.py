import base64
import hashlib
import logging

import olm
from nio import (
    AsyncClient,
    KeyVerificationCancel,
    KeyVerificationKey,
    KeyVerificationMac,
    KeyVerificationStart,
    MatrixRoom,
    RoomMessageUnknown,
    ToDeviceError,
    UnknownEvent,
)

from cross_signing import _canonical_json, cross_sign_user_msk, get_msk_public_key

log = logging.getLogger("mistral-bot")

USER_ID: str = ""
matrix: AsyncClient | None = None
_trust_all_devices = None

_room_verifications: dict[str, dict] = {}


def init(client: AsyncClient, user_id: str, trust_all_devices_fn=None) -> None:
    global matrix, USER_ID, _trust_all_devices
    matrix = client
    USER_ID = user_id
    _trust_all_devices = trust_all_devices_fn


# ── To-device verification handlers ──


async def handle_start(event: KeyVerificationStart) -> None:
    """Auto-accept incoming SAS verification requests."""
    log.info(
        "Verification request from %s (transaction %s)",
        event.sender,
        event.transaction_id,
    )
    if event.method != "m.sas.v1":
        log.warning("Unsupported verification method %s, ignoring", event.method)
        return

    resp = await matrix.accept_key_verification(event.transaction_id)
    if isinstance(resp, ToDeviceError):
        log.error("accept_key_verification failed: %s", resp)
        return

    sas = matrix.key_verifications.get(event.transaction_id)
    if sas:
        resp = await matrix.to_device(sas.share_key())
        if isinstance(resp, ToDeviceError):
            log.error("share_key failed: %s", resp)


async def handle_key(event: KeyVerificationKey) -> None:
    """Auto-confirm SAS verification (trust on first use)."""
    sas = matrix.key_verifications.get(event.transaction_id)
    if not sas:
        return

    emoji = sas.get_emoji()
    log.info(
        "SAS emoji for transaction %s: %s",
        event.transaction_id,
        " ".join(e[0] for e in emoji),
    )
    # Auto-confirm — the bot trusts all verification requests
    resp = await matrix.confirm_short_auth_string(event.transaction_id)
    if isinstance(resp, ToDeviceError):
        log.error("confirm_short_auth_string failed: %s", resp)


async def handle_mac(event: KeyVerificationMac) -> None:
    """Complete verification."""
    sas = matrix.key_verifications.get(event.transaction_id)
    if not sas:
        return
    try:
        sas.verify_devices()
    except Exception:
        log.exception("SAS MAC verification failed")
        return
    log.info(
        "Verification completed (transaction %s), verified devices: %s",
        event.transaction_id,
        sas.verified_devices,
    )


async def handle_cancel(event: KeyVerificationCancel) -> None:
    log.info(
        "Verification cancelled (transaction %s): %s",
        event.transaction_id,
        event.reason,
    )


# ── In-room verification (for Element Desktop cross-signing) ──


async def handle_room_msg_unknown(room: MatrixRoom, event: RoomMessageUnknown) -> None:
    """Catch m.key.verification.request sent as m.room.message msgtype."""
    content = event.source.get("content", {})
    if content.get("msgtype") != "m.key.verification.request":
        return
    if event.sender == USER_ID:
        return
    if "m.sas.v1" not in content.get("methods", []):
        return

    req_id = event.event_id
    log.info("In-room verification request from %s (event %s)", event.sender, req_id)

    _room_verifications[req_id] = {
        "room_id": room.room_id,
        "sender": event.sender,
        "sender_device": content.get("from_device", ""),
    }

    if _trust_all_devices:
        _trust_all_devices()
    await matrix.room_send(
        room.room_id,
        "m.key.verification.ready",
        {
            "from_device": matrix.device_id,
            "methods": ["m.sas.v1"],
            "m.relates_to": {"rel_type": "m.reference", "event_id": req_id},
        },
    )
    log.info("Sent verification.ready for %s", req_id)


async def _room_sas_start(room, req_id, v, content):
    if content.get("method") != "m.sas.v1":
        return

    sas = olm.Sas()
    v["sas"] = sas
    v["start_content"] = content

    kaps = content.get("key_agreement_protocols", [])
    macs = content.get("message_authentication_codes", [])
    if not kaps or not macs:
        log.error(
            "Empty key_agreement_protocols or message_authentication_codes in start"
        )
        return
    if "hkdf-hmac-sha256.v2" in macs:
        v["mac_method"] = "hkdf-hmac-sha256.v2"
    elif "hkdf-hmac-sha256" in macs:
        v["mac_method"] = "hkdf-hmac-sha256"
    else:
        v["mac_method"] = macs[0]
    if content.get("from_device"):
        v["sender_device"] = content["from_device"]

    # commitment = base64(SHA256(our_sas_pubkey || canonical_json(start_content)))
    commit_input = (sas.pubkey + _canonical_json(content)).encode("utf-8")
    commitment = (
        base64.b64encode(hashlib.sha256(commit_input).digest()).decode().rstrip("=")
    )

    await matrix.room_send(
        room.room_id,
        "m.key.verification.accept",
        {
            "key_agreement_protocol": "curve25519-hkdf-sha256"
            if "curve25519-hkdf-sha256" in kaps
            else kaps[0],
            "hash": "sha256",
            "message_authentication_code": v["mac_method"],
            "short_authentication_string": ["emoji", "decimal"],
            "commitment": commitment,
            "m.relates_to": {"rel_type": "m.reference", "event_id": req_id},
        },
    )
    v["state"] = "accepted"
    log.info("Sent verification.accept for %s", req_id)


async def _room_sas_key(room, req_id, v, content):
    their_sas_key = content.get("key")
    sas = v["sas"]
    sas.set_their_pubkey(their_sas_key)

    # Send our SAS public key
    await matrix.room_send(
        room.room_id,
        "m.key.verification.key",
        {
            "key": sas.pubkey,
            "m.relates_to": {"rel_type": "m.reference", "event_id": req_id},
        },
    )
    log.info("SAS keys exchanged for %s", req_id)

    # Compute SAS info for emoji (starter = them, accepter = us)
    sender = v["sender"]
    sender_device = v["sender_device"]
    try:
        sender_ed25519 = matrix.device_store[sender][sender_device].ed25519
    except (KeyError, AttributeError):
        log.error("Cannot find ed25519 key for %s %s", sender, sender_device)
        return

    our_ed25519 = matrix.olm.account.identity_keys["ed25519"]
    sas_info = (
        f"MATRIX_KEY_VERIFICATION_SAS"
        f"|{sender}|{sender_device}|{sender_ed25519}"
        f"|{USER_ID}|{matrix.device_id}|{our_ed25519}"
        f"|{req_id}"
    )
    sas.generate_bytes(sas_info, 6)

    # Auto-confirm: send our MAC immediately
    await _room_send_mac(room, req_id, v)


async def _room_send_mac(room, req_id, v):
    sas = v["sas"]
    sender = v["sender"]
    sender_device = v["sender_device"]

    mac_base = (
        f"MATRIX_KEY_VERIFICATION_MAC"
        f"{USER_ID}{matrix.device_id}"
        f"{sender}{sender_device}"
        f"{req_id}"
    )

    mac_method = v.get("mac_method", "hkdf-hmac-sha256.v2")

    # hkdf-hmac-sha256.v2 uses fixed base64 (32-byte HKDF key, correct encoding)
    # NOT calculate_mac_long_kdf which derives a 256-byte key (legacy compat only)
    if mac_method in ("hkdf-hmac-sha256.v2", "org.matrix.msc3783.hkdf-hmac-sha256"):
        calc_mac = sas.calculate_mac_fixed_base64
    else:
        calc_mac = sas.calculate_mac

    our_ed25519 = matrix.olm.account.identity_keys["ed25519"]
    device_key_id = f"ed25519:{matrix.device_id}"

    key_ids = [device_key_id]
    macs = {device_key_id: calc_mac(our_ed25519, mac_base + device_key_id)}

    # Include our MSK in the MAC so the other side can verify our cross-signing identity
    msk_pub = get_msk_public_key()
    if msk_pub:
        msk_key_id = f"ed25519:{msk_pub}"
        key_ids.append(msk_key_id)
        macs[msk_key_id] = calc_mac(msk_pub, mac_base + msk_key_id)

    keys_mac = calc_mac(",".join(sorted(key_ids)), mac_base + "KEY_IDS")

    await matrix.room_send(
        room.room_id,
        "m.key.verification.mac",
        {
            "mac": macs,
            "keys": keys_mac,
            "m.relates_to": {"rel_type": "m.reference", "event_id": req_id},
        },
    )
    log.info("Sent verification.mac for %s", req_id)


async def _room_sas_mac(room, req_id, v, content):
    """Receive their MAC, send done, and cross-sign their master key."""
    log.info("Received verification.mac for %s", req_id)

    await matrix.room_send(
        room.room_id,
        "m.key.verification.done",
        {
            "m.relates_to": {"rel_type": "m.reference", "event_id": req_id},
        },
    )
    log.info("Sent verification.done for %s", req_id)

    await cross_sign_user_msk(v["sender"])
    _room_verifications.pop(req_id, None)


async def _room_verification_cancel(_room, req_id, _v, content):
    log.info("In-room verification %s cancelled: %s", req_id, content.get("reason"))
    _room_verifications.pop(req_id, None)


async def _room_verification_done(_room, req_id, _v, _content):
    log.info("In-room verification %s done (remote)", req_id)
    _room_verifications.pop(req_id, None)


_ROOM_VERIFICATION_HANDLERS = {
    "m.key.verification.start": _room_sas_start,
    "m.key.verification.key": _room_sas_key,
    "m.key.verification.mac": _room_sas_mac,
    "m.key.verification.cancel": _room_verification_cancel,
    "m.key.verification.done": _room_verification_done,
}


async def handle_room_unknown_event(room: MatrixRoom, event: UnknownEvent) -> None:
    """Handle m.key.verification.* in-room events."""
    content = event.source.get("content", {})
    event_type = event.source.get("type", "")

    if event.source.get("sender") == USER_ID:
        return

    relates_to = content.get("m.relates_to", {})
    if relates_to.get("rel_type") != "m.reference":
        return
    req_id = relates_to.get("event_id")
    if not req_id or req_id not in _room_verifications:
        return

    handler = _ROOM_VERIFICATION_HANDLERS.get(event_type)
    if handler:
        await handler(room, req_id, _room_verifications[req_id], content)
