"""
credentials/aliceblue.py — Full 6-step AliceBlue login.
Exactly matches the Go implementation in internal/credentials/aliceblue.go.

Steps:
  1. Verify User
  2. Get Enc Key
  3. Validate Password (AES-CBC encrypted with CryptoJS-compatible key derivation)
  4. Verify TOTP  (Auth header: "Bearer <client_id> WEB <temp_token>")
  5. SSO Authorize
  6. Get User Details (checksum = sha256(client_id + auth_code + api_secret))
"""

import logging
from datetime import datetime
from urllib.parse import urlparse, parse_qs

from models.schemas import Credential
from credentials.crypto import sha256_hex, encrypt_password_alice, totp_now, md5_hex
from core.engine import core_engine
from credentials.dedup import save_working_credential

logger = logging.getLogger("DRMT.SCM")

ALICE_BASE             = "https://ant.aliceblueonline.com"
ALICE_VERIFY_USER      = ALICE_BASE + "/omk/auth/access/verify/user"
ALICE_GET_ENC_KEY      = ALICE_BASE + "/omk/auth/access/client/enckey"
ALICE_VALIDATE_PWD     = ALICE_BASE + "/omk/auth/access/v2/pwd/validate"
ALICE_VERIFY_TOTP      = ALICE_BASE + "/omk/auth/access/topt/verify"
ALICE_SSO_AUTHORIZE    = ALICE_BASE + "/omk/auth/sso/vendor/authorize/check"
ALICE_GET_USER_DETAILS = ALICE_BASE + "/open-api/od/v1/vendor/getUserDetails"


def _is_status_ok(resp: dict) -> bool:
    """Check if status or stat field indicates success."""
    return resp.get("status") == "Ok" or resp.get("stat") == "Ok"


def _extract_result_field(resp: dict, field: str) -> str:
    """Extract a field from the first element of result array."""
    result = resp.get("result", [])
    if isinstance(result, list) and result:
        return result[0].get(field, "")
    return ""


def _get_error_msg(resp: dict) -> str:
    # Extracts the exact error message from an API response.
    return resp.get("emsg") or resp.get("message") or "unknown error"


def _extract_auth_code(redirect_url: str) -> str:
    """Parse authCode or auth_code from a redirect URL query string."""
    try:
        parsed = urlparse(redirect_url)
        params = parse_qs(parsed.query)
        return (params.get("authCode") or params.get("auth_code") or [""])[0]
    except Exception:
        return ""


async def login_aliceblue(cred: Credential) -> str:
    """Full 6-step AliceBlue login. Returns userSession token."""
    logger.info(f"Logging in AliceBlue {cred.client_id}")

    session = core_engine.http_session
    if not session:
        raise RuntimeError("Core Engine HTTP session not initialized")

    common_headers = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}

    async def post_json(url: str, payload: dict, auth_header: str = "") -> dict:
        # Sends a secure JSON POST request to a remote server.
        headers = dict(common_headers)
        if auth_header:
            headers["Authorization"] = auth_header
        async with session.post(url, json=payload, headers=headers) as resp:
            return await resp.json(content_type=None)

    # ── Step 1: Verify User ──────────────────────────────────────────────────
    resp = await post_json(ALICE_VERIFY_USER, {"userId": cred.client_id})
    if not _is_status_ok(resp):
        raise ValueError(f"AliceBlue Step 1 (verifyUser) failed: {_get_error_msg(resp)}")

    # ── Step 2: Get Enc Key ──────────────────────────────────────────────────
    resp = await post_json(ALICE_GET_ENC_KEY, {"userId": cred.client_id})
    if not _is_status_ok(resp):
        raise ValueError(f"AliceBlue Step 2 (getEncKey) failed: {_get_error_msg(resp)}")
    enc_key = _extract_result_field(resp, "encKey")
    if not enc_key:
        raise ValueError("AliceBlue Step 2: encKey missing from response")

    # ── Step 3: Validate Password (AES-CBC) ──────────────────────────────────
    encrypted_pwd = encrypt_password_alice(cred.password, enc_key)
    resp = await post_json(ALICE_VALIDATE_PWD, {
        "userId": cred.client_id,
        "userData": encrypted_pwd,
        "source": "WEB",
    })
    if not _is_status_ok(resp):
        raise ValueError(f"AliceBlue Step 3 (validatePwd) failed: {_get_error_msg(resp)}")
    temp_token = _extract_result_field(resp, "token")
    if not temp_token:
        raise ValueError("AliceBlue Step 3: temp token missing from response")

    # ── Step 4: Verify TOTP (Auth: "Bearer <client_id> WEB <temp_token>") ───
    current_totp = totp_now(cred.totp_secret)
    device_id = md5_hex(cred.client_id)
    resp = await post_json(ALICE_VERIFY_TOTP, {
        "userId":       cred.client_id,
        "totp":         current_totp,
        "source":       "WEB",
        "deviceId":     device_id,
        "deviceNumber": device_id,
    }, auth_header=f"Bearer {cred.client_id} WEB {temp_token}")
    if not _is_status_ok(resp):
        raise ValueError(f"AliceBlue Step 4 (verifyTOTP) failed: {_get_error_msg(resp)}")
    access_token = _extract_result_field(resp, "accessToken")
    if not access_token:
        raise ValueError("AliceBlue Step 4: accessToken missing from response")

    # ── Step 5: SSO Authorize ────────────────────────────────────────────────
    resp = await post_json(ALICE_SSO_AUTHORIZE, {
        "userId": cred.client_id,
        "vendor": cred.api_key or "",
    }, auth_header=f"Bearer {access_token}")
    if not _is_status_ok(resp):
        raise ValueError(f"AliceBlue Step 5 (ssoAuthorize) failed: {_get_error_msg(resp)}")
    redirect_url = _extract_result_field(resp, "redirectUrl")
    auth_code = _extract_auth_code(redirect_url)
    if not auth_code:
        raise ValueError(f"AliceBlue Step 5: authCode missing from redirectUrl: {redirect_url}")

    # ── Step 6: Get User Details (checksum = sha256(client_id+auth_code+api_secret)) ──
    checksum_base = cred.client_id + auth_code + (cred.api_secret or "")
    checksum = sha256_hex(checksum_base)
    resp = await post_json(ALICE_GET_USER_DETAILS, {"checkSum": checksum})
    if resp.get("stat") != "Ok":
        raise ValueError(f"AliceBlue Step 6 (getUserDetails) failed: {_get_error_msg(resp)}")
    user_session = resp.get("userSession", "")
    if not user_session:
        raise ValueError("AliceBlue Step 6: userSession missing from response")

    cred.token = user_session
    cred.token_updated_at = datetime.now()
    await save_working_credential(cred)
    masked = f"{user_session[:10]}...{user_session[-10:]}" if len(user_session) > 20 else user_session
    logger.info(f"🔑 Loaded dynamic auth token: {masked}")
    return user_session
