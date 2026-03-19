import base64
import json
import logging
import os

import aiohttp
from nio import AsyncClient

log = logging.getLogger("mistral-bot")

USER_ID: str = ""
HOMESERVER: str = ""
STORE_PATH: str = ""
MATRIX_PASSWORD: str = ""
matrix: AsyncClient | None = None


def init(
    client: AsyncClient,
    user_id: str,
    homeserver: str,
    store_path: str,
    password: str,
) -> None:
    global matrix, USER_ID, HOMESERVER, STORE_PATH, MATRIX_PASSWORD
    matrix = client
    USER_ID = user_id
    HOMESERVER = homeserver
    STORE_PATH = store_path
    MATRIX_PASSWORD = password


def _canonical_json(obj: dict) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _matrix_api_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {matrix.access_token}",
        "Content-Type": "application/json",
    }


def _sign_json(signing, obj: dict) -> str:
    to_sign = {k: v for k, v in obj.items() if k not in ("signatures", "unsigned")}
    return signing.sign(_canonical_json(to_sign))


def _load_or_generate_seeds(seeds_file: str) -> tuple[bytes, bytes, bytes]:
    from olm.pk import PkSigning

    if os.path.exists(seeds_file):
        with open(seeds_file) as f:
            data = json.load(f)
        return (
            base64.b64decode(data["master"]),
            base64.b64decode(data["self_signing"]),
            base64.b64decode(data["user_signing"]),
        )

    msk_seed = PkSigning.generate_seed()
    ssk_seed = PkSigning.generate_seed()
    usk_seed = PkSigning.generate_seed()
    with open(seeds_file, "w") as f:
        json.dump(
            {
                "master": base64.b64encode(msk_seed).decode(),
                "self_signing": base64.b64encode(ssk_seed).decode(),
                "user_signing": base64.b64encode(usk_seed).decode(),
            },
            f,
        )
    return msk_seed, ssk_seed, usk_seed


async def _upload_cross_signing_keys(
    session, headers, master, self_signing, user_signing
) -> bool:
    """Upload MSK/SSK/USK to the server. Returns True on success."""
    msk_pub = master.public_key
    ssk_pub = self_signing.public_key
    usk_pub = user_signing.public_key

    msk_obj = {
        "user_id": USER_ID,
        "usage": ["master"],
        "keys": {f"ed25519:{msk_pub}": msk_pub},
    }
    msk_obj["signatures"] = {
        USER_ID: {f"ed25519:{msk_pub}": _sign_json(master, msk_obj)}
    }
    ssk_obj = {
        "user_id": USER_ID,
        "usage": ["self_signing"],
        "keys": {f"ed25519:{ssk_pub}": ssk_pub},
    }
    ssk_obj["signatures"] = {
        USER_ID: {f"ed25519:{msk_pub}": _sign_json(master, ssk_obj)}
    }
    usk_obj = {
        "user_id": USER_ID,
        "usage": ["user_signing"],
        "keys": {f"ed25519:{usk_pub}": usk_pub},
    }
    usk_obj["signatures"] = {
        USER_ID: {f"ed25519:{msk_pub}": _sign_json(master, usk_obj)}
    }

    upload_body = {
        "master_key": msk_obj,
        "self_signing_key": ssk_obj,
        "user_signing_key": usk_obj,
    }

    async with session.post(
        f"{HOMESERVER}/_matrix/client/v3/keys/device_signing/upload",
        headers=headers,
        json=upload_body,
    ) as resp:
        if resp.status == 401:
            data = await resp.json()
            upload_body["auth"] = {
                "type": "m.login.password",
                "identifier": {"type": "m.id.user", "user": USER_ID},
                "password": MATRIX_PASSWORD,
                "session": data.get("session", ""),
            }
            async with session.post(
                f"{HOMESERVER}/_matrix/client/v3/keys/device_signing/upload",
                headers=headers,
                json=upload_body,
            ) as resp2:
                if resp2.status != 200:
                    log.error("Cross-signing upload failed: %s", await resp2.text())
                    return False
        elif resp.status != 200:
            log.error("Cross-signing upload failed: %s", await resp.text())
            return False

    log.info("Uploaded cross-signing keys")
    return True


async def _sign_own_device(session, headers, self_signing) -> bool:
    """Sign the bot's device key with the self-signing key. Returns True on success."""
    ssk_pub = self_signing.public_key
    device_id = matrix.device_id
    ik = matrix.olm.account.identity_keys

    device_obj = {
        "user_id": USER_ID,
        "device_id": device_id,
        "algorithms": ["m.olm.v1.curve25519-aes-sha2", "m.megolm.v1.aes-sha2"],
        "keys": {
            f"curve25519:{device_id}": ik["curve25519"],
            f"ed25519:{device_id}": ik["ed25519"],
        },
    }

    sig_body = {
        USER_ID: {
            device_id: {
                **device_obj,
                "signatures": {
                    USER_ID: {
                        f"ed25519:{ssk_pub}": _sign_json(self_signing, device_obj)
                    },
                },
            }
        }
    }

    log.info("Signing device %s with SSK %s", device_id, ssk_pub)

    async with session.post(
        f"{HOMESERVER}/_matrix/client/v3/keys/signatures/upload",
        headers=headers,
        json=sig_body,
    ) as resp:
        resp_body = await resp.text()
        if resp.status != 200:
            log.error("Device signature upload failed: %s", resp_body)
            return False
        log.info("Device signature upload response: %s", resp_body)

    log.info("Cross-signing bootstrap complete, device %s signed", device_id)
    return True


async def bootstrap() -> None:
    """Set up cross-signing keys so Element can verify the bot."""
    from olm.pk import PkSigning

    seeds_file = os.path.join(STORE_PATH, "cross_signing_seeds.json")
    headers = _matrix_api_headers()

    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{HOMESERVER}/_matrix/client/v3/keys/query",
            headers=headers,
            json={"device_keys": {USER_ID: []}},
        ) as resp:
            keys_data = await resp.json()
            has_cross_signing = USER_ID in keys_data.get("master_keys", {})

        if has_cross_signing and not os.path.exists(seeds_file):
            log.info(
                "Cross-signing exists on server but no local seeds — re-bootstrapping"
            )

        msk_seed, ssk_seed, usk_seed = _load_or_generate_seeds(seeds_file)
        master = PkSigning(msk_seed)
        self_signing = PkSigning(ssk_seed)
        _user_signing = PkSigning(usk_seed)

        # Check if server already has matching MSK — skip upload to avoid identity reset
        server_msk = keys_data.get("master_keys", {}).get(USER_ID, {})
        server_msk_pub = None
        for key in server_msk.get("keys", {}).values():
            server_msk_pub = key
            break

        if server_msk_pub == master.public_key:
            log.info("Cross-signing keys already match server, skipping upload")
        else:
            if not await _upload_cross_signing_keys(
                session, headers, master, self_signing, _user_signing
            ):
                return

        await _sign_own_device(session, headers, self_signing)


def get_msk_public_key() -> str | None:
    """Return the local MSK public key, or None if seeds don't exist."""
    from olm.pk import PkSigning

    seeds_file = os.path.join(STORE_PATH, "cross_signing_seeds.json")
    if not os.path.exists(seeds_file):
        return None
    with open(seeds_file) as f:
        seeds = json.load(f)
    msk = PkSigning(base64.b64decode(seeds["master"]))
    return msk.public_key


async def cross_sign_user_msk(user_id: str) -> None:
    """Sign another user's MSK with our USK after successful verification."""
    from olm.pk import PkSigning

    seeds_file = os.path.join(STORE_PATH, "cross_signing_seeds.json")
    if not os.path.exists(seeds_file):
        return

    with open(seeds_file) as f:
        seeds = json.load(f)
    usk = PkSigning(base64.b64decode(seeds["user_signing"]))
    headers = _matrix_api_headers()

    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{HOMESERVER}/_matrix/client/v3/keys/query",
            headers=headers,
            json={"device_keys": {user_id: []}},
        ) as resp:
            data = await resp.json()

        msk = data.get("master_keys", {}).get(user_id)
        if not msk:
            log.warning("No master key found for %s", user_id)
            return

        to_sign = {k: v for k, v in msk.items() if k not in ("signatures", "unsigned")}
        sig = usk.sign(_canonical_json(to_sign))
        usk_pub = usk.public_key
        msk_pub = msk["keys"][next(iter(msk["keys"]))]

        signed_key = dict(msk)
        signed_key.setdefault("signatures", {}).setdefault(USER_ID, {})[
            f"ed25519:{usk_pub}"
        ] = sig

        async with session.post(
            f"{HOMESERVER}/_matrix/client/v3/keys/signatures/upload",
            headers=headers,
            json={user_id: {msk_pub: signed_key}},
        ) as resp:
            if resp.status != 200:
                log.error("Cross-sign of %s failed: %s", user_id, await resp.text())
                return

        log.info("Cross-signed %s's master key with our USK", user_id)
