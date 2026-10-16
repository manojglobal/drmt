"""
credentials/axisdirect.py — Axis Direct Authentication & Cryptographic Handshake Module for DRMT V2.
Handles:
1. RSA Public Key Handshake via https://welcome-api.axisdirect.in/handshake
2. Hybrid RSA-OAEP + AES-GCM encryption/decryption for Axis Direct RAPID API
3. Multi-header authentication (Authorization, x-api-client-id, x-subAccountID, x-authtoken, x-api-encryption-key)
4. Session validation via positions / order-book probe
5. In-flight token renewal via /v1/handshake/get-auth-token
"""

import base64
import json
import logging
import os
from datetime import datetime
from typing import Dict, Any, Optional, Tuple

from Crypto.Cipher import AES, PKCS1_OAEP
from Crypto.PublicKey import RSA
from Crypto.Hash import SHA256

from models.schemas import Credential
from credentials.httpclient import proxy_request
from core.engine import core_engine
from credentials.dedup import save_working_credential
from credentials.crypto import totp_now
from logger.logger import logger_auth, sanitize_log_message

logger = logging.getLogger("DRMT.SCM")

AXIS_WELCOME_BASE_URL = "https://welcome-api.axisdirect.in"
AXIS_INVEST_BASE_URL = "https://invest-api.axisdirect.in"

# In-memory public key cache: api_client_id -> public_key
_axis_public_keys: Dict[str, str] = {}


def encrypt_aes(payload: str, secret_key: str) -> str:
    """Encrypts a string payload using AES-128/256 GCM with a hex secret key."""
    secret_key_bytes = secret_key.encode("utf-8")
    cipher = AES.new(secret_key_bytes, AES.MODE_GCM)
    ciphertext, tag = cipher.encrypt_and_digest(payload.encode("utf-8"))
    encoded_nonce = base64.b64encode(cipher.nonce).decode("utf-8")
    encoded_tag = base64.b64encode(tag).decode("utf-8")
    encoded_ciphertext = base64.b64encode(ciphertext).decode("utf-8")
    return f"{encoded_nonce}.{encoded_tag}.{encoded_ciphertext}"


def encrypt_rsa(secret_key: str, public_key: str) -> str:
    """Encrypts the session secret_key with Axis Direct's RSA public key using RSA-OAEP SHA256."""
    clean_key = public_key.strip()
    if not clean_key.startswith("-----BEGIN"):
        pem_string = f"-----BEGIN PUBLIC KEY-----\n{clean_key}\n-----END PUBLIC KEY-----"
    else:
        pem_string = clean_key
    key = RSA.import_key(pem_string)
    cipher = PKCS1_OAEP.new(key, hashAlgo=SHA256)
    encrypted_payload = cipher.encrypt(secret_key.encode("utf-8"))
    return base64.b64encode(encrypted_payload).decode("utf-8")


def decrypt_data(encoded_payload: str, secret_key: str) -> str:
    """Decrypts an AES-GCM encrypted payload string formatted as nonce.tag.ciphertext."""
    secret_key_bytes = secret_key.encode("utf-8")
    encoded_nonce, encoded_tag, encoded_ciphertext = encoded_payload.split(".")
    nonce = base64.b64decode(encoded_nonce)
    tag = base64.b64decode(encoded_tag)
    ciphertext = base64.b64decode(encoded_ciphertext)
    cipher = AES.new(secret_key_bytes, AES.MODE_GCM, nonce=nonce)
    return cipher.decrypt_and_verify(ciphertext, tag).decode("utf-8")


async def get_axis_public_key(api_client_id: str, authorization_key: str, static_ip: str, force_refresh: bool = False) -> str:
    """
    Fetches the RSA Public Key from Axis Direct via POST /handshake.
    Caches the public key in memory to eliminate repeated network handshakes.
    """
    cid = str(api_client_id or "").strip()
    if not force_refresh and cid in _axis_public_keys and _axis_public_keys[cid]:
        return _axis_public_keys[cid]

    handshake_url = f"{AXIS_WELCOME_BASE_URL.rstrip('/')}/handshake"
    auth_key = str(authorization_key or "").strip()
    headers = {
        "Authorization": auth_key,
        "x-api-client-id": cid,
        "X-API-Client-Id": cid,
        "api_client_id": cid,
        "authorization_key": auth_key,
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (DRMT Axis Direct Handshake)",
    }

    try:
        if static_ip:
            resp = await proxy_request("POST", handshake_url, str(static_ip), headers=headers, json_data={})
        else:
            session = core_engine.http_session
            if not session:
                raise RuntimeError("Core Engine HTTP session not initialized")
            async with session.post(handshake_url, headers=headers, json={}) as r:
                resp = await r.json(content_type=None)

        if isinstance(resp, str):
            try:
                resp = json.loads(resp)
            except Exception:
                resp = {}

        if isinstance(resp, dict):
            data = resp.get("data") if isinstance(resp.get("data"), dict) else resp
            pub_key = (
                data.get("publicKey")
                or data.get("public_key")
                or data.get("key")
                or resp.get("publicKey")
                or resp.get("public_key")
                or ""
            )
            if pub_key:
                pub_key = str(pub_key).strip()
                _axis_public_keys[cid] = pub_key
                logger.info(f"🔑 Successfully acquired Axis Direct public key for {cid}")
                return pub_key

        logger.warning(f"Axis Direct handshake returned unexpected format: {resp}")
        return ""
    except Exception as e:
        logger.error(f"❌ Failed to fetch Axis Direct public key for {cid}: {e}")
        return ""


def get_axis_headers(
    api_client_id: str,
    authorization_key: str,
    sub_account_id: str = "",
    access_token: str = "",
    api_encryption_key: str = "",
) -> Dict[str, str]:
    """
    Constructs the standard authentication headers required by Axis Direct RAPID API:
    - Authorization (primary API gateway key)
    - x-api-client-id / X-API-Client-Id
    - x-subAccountID / X-SubAccountID
    - x-authtoken (access token)
    - x-api-encryption-key (RSA-OAEP encrypted ephemeral AES secret key)
    """
    auth_key = str(authorization_key or "").strip()
    client_id_val = str(api_client_id or "").strip()
    sub_acc = str(sub_account_id or "").strip()
    token_val = str(access_token or "").strip()
    enc_key = str(api_encryption_key or "").strip()

    headers = {
        "Content-Type": "application/json",
        "Authorization": auth_key,
        "x-api-client-id": client_id_val,
        "X-API-Client-Id": client_id_val,
        "api_client_id": client_id_val,
        "authorization_key": auth_key,
        "User-Agent": "Mozilla/5.0 (DRMT Axis Direct Adapter)",
    }
    if enc_key:
        headers["x-api-encryption-key"] = enc_key
    if sub_acc:
        headers["x-subAccountID"] = sub_acc
        headers["X-SubAccountID"] = sub_acc
        headers["x-subAccountId"] = sub_acc
        headers["sub_account_id"] = sub_acc
    if token_val:
        headers["x-authtoken"] = token_val
        headers["access_token"] = token_val

    return headers


async def send_axis_api_request(
    method: str,
    url: str,
    static_ip: str,
    authorization_key: str,
    api_client_id: str,
    sub_account_id: str,
    access_token: str,
    public_key: str,
    body: Optional[Dict[str, Any]] = None,
    _retry_count: int = 0,
) -> Tuple[int, Any]:
    """
    Dispatches an HTTP request to the Axis Direct gateway via the DRMT proxy or direct HTTP session.
    Encrypted using hybrid RSA-OAEP + AES-GCM as required by Axis Direct RAPID API:
    - Encrypts ephemeral secret_key using public_key into x-api-encryption-key header.
    - Encrypts body using AES-GCM into {"payload": ...}.
    - Decrypts response payload if present.
    Automatically handles public key rotation (error 1200) by refreshing public key and retrying.
    Returns (status_code, response_data).
    """
    cid = str(api_client_id or "").strip()
    secret_key = os.urandom(16).hex()
    api_encryption_key = ""
    if public_key:
        try:
            api_encryption_key = encrypt_rsa(secret_key, public_key)
        except Exception as e:
            logger.error(f"❌ Axis Direct RSA encryption error for {cid}: {e}")

    headers = get_axis_headers(
        api_client_id=cid,
        authorization_key=authorization_key,
        sub_account_id=sub_account_id,
        access_token=access_token,
        api_encryption_key=api_encryption_key,
    )

    outgoing_payload: Dict[str, Any] = {}
    if body is not None:
        try:
            enc_body = encrypt_aes(json.dumps(body), secret_key)
            outgoing_payload = {"payload": enc_body}
        except Exception as e:
            logger.error(f"❌ Axis Direct AES encryption error: {e}")
            outgoing_payload = body

    try:
        if static_ip:
            resp = await proxy_request(method, url, str(static_ip), headers=headers, json_data=outgoing_payload)
            if isinstance(resp, str):
                try:
                    resp = json.loads(resp)
                except Exception:
                    pass
            sc = 200
        else:
            session = core_engine.http_session
            if not session:
                raise RuntimeError("Core Engine HTTP session not initialized")
            async with session.request(method, url, headers=headers, json=outgoing_payload) as r:
                sc = r.status
                try:
                    resp = await r.json(content_type=None)
                except Exception:
                    resp = await r.text()

        # Decrypt response payload if returned by Axis Direct
        if isinstance(resp, dict):
            err_dict = resp.get("error", {}) if isinstance(resp.get("error"), dict) else {}
            err_code = str(err_dict.get("code") or "")
            if _retry_count == 0 and (err_code == "1200" or "failed to decrypt" in str(resp).lower()):
                logger.warning(f"🔄 Axis Direct public key rotation (1200) detected for {cid}. Refreshing public key and retrying...")
                new_pk = ""
                if isinstance(err_dict.get("data"), dict):
                    new_pk = err_dict["data"].get("publicKey") or ""
                if not new_pk:
                    new_pk = await get_axis_public_key(cid, authorization_key, static_ip, force_refresh=True)
                if new_pk:
                    _axis_public_keys[cid] = new_pk
                    return await send_axis_api_request(
                        method=method,
                        url=url,
                        static_ip=static_ip,
                        authorization_key=authorization_key,
                        api_client_id=cid,
                        sub_account_id=sub_account_id,
                        access_token=access_token,
                        public_key=new_pk,
                        body=body,
                        _retry_count=_retry_count + 1,
                    )

            payload_str = ""
            if "data" in resp and isinstance(resp["data"], dict) and "payload" in resp["data"]:
                payload_str = resp["data"]["payload"]
            elif "payload" in resp and isinstance(resp["payload"], str):
                payload_str = resp["payload"]

            if payload_str:
                try:
                    decrypted_raw = decrypt_data(payload_str, secret_key)
                    try:
                        decrypted_obj = json.loads(decrypted_raw)
                        return sc, decrypted_obj
                    except Exception:
                        return sc, decrypted_raw
                except Exception as e:
                    logger.warning(f"Failed to decrypt Axis response payload: {e}")

        return sc, resp
    except PermissionError as pe:
        # Proxy identified 401 or 403
        return 401, {"error": {"code": "401", "message": str(pe)}}
    except Exception as e:
        err_s = str(e)
        import re
        sc_match = re.search(r"Broker HTTP (\d+) Error:\s*(.*)", err_s, re.DOTALL)
        if sc_match:
            parsed_sc = int(sc_match.group(1))
            raw_body = sc_match.group(2).strip()
            parsed_body = None
            try:
                parsed_body = json.loads(raw_body)
            except Exception:
                parsed_body = {"error": {"code": str(parsed_sc), "message": raw_body}}

            if _retry_count == 0 and ("1200" in raw_body or "failed to decrypt" in raw_body.lower()):
                logger.warning(f"🔄 Axis Direct public key rotation (1200) detected in HTTP error for {cid}. Refreshing public key and retrying...")
                m = re.search(r"['\"]publicKey['\"]\s*:\s*['\"]([^'\"]+)['\"]", raw_body)
                new_pk = m.group(1).strip() if m else ""
                if not new_pk:
                    new_pk = await get_axis_public_key(cid, authorization_key, static_ip, force_refresh=True)
                if new_pk:
                    _axis_public_keys[cid] = new_pk
                    return await send_axis_api_request(
                        method=method,
                        url=url,
                        static_ip=static_ip,
                        authorization_key=authorization_key,
                        api_client_id=cid,
                        sub_account_id=sub_account_id,
                        access_token=access_token,
                        public_key=new_pk,
                        body=body,
                        _retry_count=_retry_count + 1,
                    )
            return parsed_sc, parsed_body

        if _retry_count == 0 and ("1200" in err_s or "failed to decrypt encryption key" in err_s):
            logger.warning(f"🔄 Axis Direct public key rotation (1200) detected in error for {cid}. Refreshing public key and retrying...")
            m = re.search(r"['\"]publicKey['\"]\s*:\s*['\"]([^'\"]+)['\"]", err_s)
            new_pk = m.group(1).strip() if m else ""
            if not new_pk:
                new_pk = await get_axis_public_key(cid, authorization_key, static_ip, force_refresh=True)
            if new_pk:
                _axis_public_keys[cid] = new_pk
                return await send_axis_api_request(
                    method=method,
                    url=url,
                    static_ip=static_ip,
                    authorization_key=authorization_key,
                    api_client_id=cid,
                    sub_account_id=sub_account_id,
                    access_token=access_token,
                    public_key=new_pk,
                    body=body,
                    _retry_count=_retry_count + 1,
                )
        if any(code in err_s for code in ("401", "403", "1081", "1082", "1200", "1002", "1003", "invalid session", "token expired", "unauthorized")):
            return 401, {"error": {"code": "401", "message": err_s}}
        return 500, {"error": {"message": err_s}}


async def refresh_axis_auth_token(
    api_client_id: str,
    authorization_key: str,
    sub_account_id: str,
    refresh_token: str,
    public_key: str,
    static_ip: str,
) -> Dict[str, Any]:
    """
    Exchanges an active daily refreshToken for a fresh 15-minute authToken.
    Endpoint: POST https://welcome-api.axisdirect.in/v1/handshake/get-auth-token
    """
    cid = str(api_client_id or "").strip()
    url = f"{AXIS_WELCOME_BASE_URL.rstrip('/')}/v1/handshake/get-auth-token"
    body = {"refreshToken": refresh_token}
    status_code, resp_data = await send_axis_api_request(
        method="POST",
        url=url,
        static_ip=static_ip,
        authorization_key=authorization_key,
        api_client_id=cid,
        sub_account_id=sub_account_id,
        access_token="",
        public_key=public_key,
        body=body,
    )
    if isinstance(resp_data, str):
        try:
            resp_data = json.loads(resp_data)
        except Exception:
            resp_data = {}

    data = resp_data.get("data", {}) if isinstance(resp_data, dict) and isinstance(resp_data.get("data"), (dict, list)) else (resp_data if isinstance(resp_data, dict) else {})

    token = None
    if isinstance(data, dict):
        auth_token_obj = data.get("authToken") or data.get("auth_token")
        if isinstance(auth_token_obj, dict):
            token = auth_token_obj.get("token") or auth_token_obj.get("accessToken") or auth_token_obj.get("access_token")
        elif isinstance(auth_token_obj, str) and auth_token_obj:
            token = auth_token_obj
        if not token:
            token = data.get("token") or data.get("access_token") or data.get("accessToken")

    if not token and isinstance(resp_data, dict):
        root_auth = resp_data.get("authToken") or resp_data.get("auth_token")
        if isinstance(root_auth, dict):
            token = root_auth.get("token") or root_auth.get("accessToken") or root_auth.get("access_token")
        elif isinstance(root_auth, str) and root_auth:
            token = root_auth
        if not token:
            token = resp_data.get("token") or resp_data.get("access_token") or resp_data.get("accessToken")

    new_refresh = None
    if isinstance(data, dict):
        refresh_obj = data.get("refreshToken") or data.get("refresh_token")
        if isinstance(refresh_obj, dict):
            new_refresh = refresh_obj.get("token") or refresh_obj.get("refreshToken")
        elif isinstance(refresh_obj, str) and refresh_obj:
            new_refresh = refresh_obj
        if not new_refresh:
            new_refresh = data.get("refreshToken") or data.get("refresh_token")

    if not new_refresh and isinstance(resp_data, dict):
        root_ref = resp_data.get("refreshToken") or resp_data.get("refresh_token")
        if isinstance(root_ref, dict):
            new_refresh = root_ref.get("token") or root_ref.get("refreshToken")
        elif isinstance(root_ref, str) and root_ref:
            new_refresh = root_ref

    if token:
        masked_tok = f"{str(token)[:8]}...{str(token)[-8:]}" if len(str(token)) > 16 else str(token)
        logger.info(f"🔑 [AXIS REFRESH] Successfully exchanged refreshToken for fresh authToken for {cid}: {masked_tok}")
        return {
            "status": True,
            "token": str(token).strip(),
            "refresh_token": str(new_refresh).strip() if new_refresh else refresh_token,
            "expire_at": data.get("expireAt") if isinstance(data, dict) else None,
        }
    return {"status": False, "msg": f"Refresh failed ({status_code}): {resp_data}"}


async def headless_axis_direct_login(
    api_client_id: str,
    authorization_key: str,
    mobile: str,
    password: str,
    totp_secret: str,
    public_key: str = "",
    static_ip: str = "",
    sub_account_id: str = "",
) -> Dict[str, Any]:
    """
    Executes automated 3-step direct headless TOTP login for Axis Direct:
    Step 1: POST /auth/can-login -> verifies mobile number eligibility
    Step 2: POST /auth/login/access-code/generate -> verifies password, returns refId
    Step 3: POST /auth/login/totp/validate -> validates TOTP code, returns fresh authToken + daily refreshToken
    """
    cid = str(api_client_id or "").strip()
    if not public_key:
        public_key = await get_axis_public_key(cid, authorization_key, static_ip)
    if not public_key:
        return {"status": False, "msg": "Failed to acquire Axis Direct public key for headless login"}

    clean_mobile = str(mobile or "").replace("+91", "").replace(" ", "").replace("-", "").strip()
    if len(clean_mobile) > 10 and clean_mobile.startswith("91"):
        clean_mobile = clean_mobile[2:]
    if len(clean_mobile) > 10 and clean_mobile.startswith("0"):
        clean_mobile = clean_mobile[1:]

    # Step 1: Can-Login Check
    can_login_url = f"{AXIS_WELCOME_BASE_URL.rstrip('/')}/auth/can-login"
    sc1, resp1 = await send_axis_api_request(
        method="POST",
        url=can_login_url,
        static_ip=static_ip,
        authorization_key=authorization_key,
        api_client_id=cid,
        sub_account_id=sub_account_id,
        access_token="",
        public_key=public_key,
        body={"mobile": clean_mobile, "phone_number": clean_mobile},
    )
    if sc1 >= 400:
        logger.warning(f"⚠️  Axis Direct Step 1 (Can-Login) returned {sc1} for {cid}: {resp1}")
        if sc1 in (401, 403) or (isinstance(resp1, dict) and resp1.get("error")):
            return {"status": False, "msg": f"Can-Login check failed ({sc1}): {resp1}"}
    else:
        logger.info(f"✅ Axis Direct Step 1 (Can-Login) passed for {cid}")

    # Step 2: Generate Access Code (Password verification)
    access_code_url = f"{AXIS_WELCOME_BASE_URL.rstrip('/')}/auth/login/access-code/generate"
    sc2, resp2 = await send_axis_api_request(
        method="POST",
        url=access_code_url,
        static_ip=static_ip,
        authorization_key=authorization_key,
        api_client_id=cid,
        sub_account_id=sub_account_id,
        access_token="",
        public_key=public_key,
        body={"identifierType": "Mobile", "identifier": clean_mobile, "password": str(password).strip()},
    )
    gen_data = resp2.get("data") if isinstance(resp2, dict) and isinstance(resp2.get("data"), dict) else (resp2 if isinstance(resp2, dict) else {})
    ref_id = None
    if isinstance(gen_data, dict):
        ref_id = (
            gen_data.get("refId")
            or gen_data.get("ref_id")
            or gen_data.get("referenceId")
            or gen_data.get("reference_id")
            or gen_data.get("id")
        )
    if not ref_id:
        logger.error(f"❌ Axis Direct Step 2 (Access Code) failed ({sc2}) for {cid}: {resp2}")
        return {"status": False, "msg": f"Access code generation failed ({sc2}): {resp2}"}
    logger.info(f"✅ Axis Direct Step 2 (Access Code) succeeded for {cid} (refId={ref_id[:8]}...)")

    # Step 3: TOTP Validation with complete device schema required by Axis Direct
    totp_code = totp_now(totp_secret)
    device_info = {
        "deviceId": f"drmt-{sub_account_id or cid}",
        "DeviceId": f"drmt-{sub_account_id or cid}",
        "deviceType": "WEB",
        "os": "Linux",
        "osVersion": "1.0.0",
        "OsVersion": "1.0.0",
        "browser": "Chrome",
        "ip": static_ip or "127.0.0.1",
        "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }
    totp_url = f"{AXIS_WELCOME_BASE_URL.rstrip('/')}/auth/login/totp/validate"
    sc3, resp3 = await send_axis_api_request(
        method="POST",
        url=totp_url,
        static_ip=static_ip,
        authorization_key=authorization_key,
        api_client_id=cid,
        sub_account_id=sub_account_id,
        access_token="",
        public_key=public_key,
        body={"totpCode": str(totp_code).strip(), "device": device_info, "refId": ref_id},
    )
    totp_data = resp3.get("data") if isinstance(resp3, dict) and isinstance(resp3.get("data"), dict) else (resp3 if isinstance(resp3, dict) else {})

    auth_token = None
    if isinstance(totp_data, dict):
        auth_token_obj = totp_data.get("authToken") or totp_data.get("auth_token")
        if isinstance(auth_token_obj, dict):
            auth_token = auth_token_obj.get("token") or auth_token_obj.get("accessToken")
        elif isinstance(auth_token_obj, str) and auth_token_obj:
            auth_token = auth_token_obj
        if not auth_token:
            auth_token = totp_data.get("token") or totp_data.get("access_token") or totp_data.get("accessToken")
    if not auth_token and isinstance(resp3, dict):
        root_auth = resp3.get("authToken") or resp3.get("auth_token")
        if isinstance(root_auth, dict):
            auth_token = root_auth.get("token") or root_auth.get("accessToken")
        elif isinstance(root_auth, str) and root_auth:
            auth_token = root_auth
        if not auth_token:
            auth_token = resp3.get("token") or resp3.get("accessToken") or resp3.get("access_token")

    new_refresh = None
    if isinstance(totp_data, dict):
        refresh_token_obj = totp_data.get("refreshToken") or totp_data.get("refresh_token")
        if isinstance(refresh_token_obj, dict):
            new_refresh = refresh_token_obj.get("token") or refresh_token_obj.get("refreshToken")
        elif isinstance(refresh_token_obj, str) and refresh_token_obj:
            new_refresh = refresh_token_obj
        if not new_refresh:
            new_refresh = totp_data.get("refresh_token") or totp_data.get("refreshToken")
    if not new_refresh and isinstance(resp3, dict):
        root_ref = resp3.get("refreshToken") or resp3.get("refresh_token")
        if isinstance(root_ref, dict):
            new_refresh = root_ref.get("token") or root_ref.get("refreshToken")
        elif isinstance(root_ref, str) and root_ref:
            new_refresh = root_ref

    ret_sub_acc = None
    if isinstance(totp_data, dict):
        ret_sub_acc = (
            totp_data.get("subAccountId")
            or totp_data.get("sub_account_id")
            or totp_data.get("subAccountID")
            or totp_data.get("accountId")
        )
    if not ret_sub_acc and isinstance(resp3, dict):
        ret_sub_acc = resp3.get("subAccountId") or resp3.get("sub_account_id")
    if not ret_sub_acc:
        ret_sub_acc = sub_account_id

    if not auth_token:
        logger.error(f"❌ Axis Direct Step 3 (TOTP Validate) failed ({sc3}) for {cid}: {resp3}")
        return {"status": False, "msg": f"TOTP validation failed ({sc3}): {resp3}"}
    logger.info(f"✅ Axis Direct Step 3 (TOTP Validate) succeeded for {cid}")

    current_pk = _axis_public_keys.get(cid, public_key)

    return {
        "status": True,
        "token": str(auth_token).strip(),
        "refresh_token": str(new_refresh).strip() if new_refresh else "",
        "sub_account_id": str(ret_sub_acc).strip() if ret_sub_acc else sub_account_id,
        "public_key": current_pk,
    }


async def generate_user_axis_direct_access_token(
    api_client_id: str,
    authorization_key: str,
    sub_account_id: str = "",
    refresh_token: str = "",
    access_token: str = "",
    public_key: str = "",
    static_ip: str = "",
    phone_number: str = "",
    password: str = "",
    totp_secret: str = "",
) -> Dict[str, Any]:
    """
    Unified access token generator for Axis Direct:
    1. Fetches/verifies RSA public key.
    2. Tries silent refresh using active daily refreshToken.
    3. If refreshToken fails (overnight expiry / error 1200 / 400), falls back to automated headless login.
    """
    cid = str(api_client_id or "").strip()
    fresh_pk = await get_axis_public_key(cid, authorization_key, static_ip)
    if fresh_pk:
        public_key = fresh_pk

    # 1. Try silent refresh if refresh_token is present
    if refresh_token:
        ref_res = await refresh_axis_auth_token(
            api_client_id=cid,
            authorization_key=authorization_key,
            sub_account_id=sub_account_id,
            refresh_token=refresh_token,
            public_key=public_key,
            static_ip=static_ip,
        )
        if ref_res.get("status"):
            current_pk = _axis_public_keys.get(cid, public_key)
            return {
                "status": True,
                "token": ref_res.get("token"),
                "refresh_token": ref_res.get("refresh_token") or refresh_token,
                "sub_account_id": sub_account_id,
                "public_key": current_pk,
            }
        logger.info(f"🔄 Silent refreshToken refresh not accepted for {cid} ({ref_res.get('msg')}) — falling back to Headless TOTP Login...")

    # 2. Fallback to headless TOTP login
    mobile = phone_number or ""
    clean_sub = str(sub_account_id or cid).replace("RAPID_", "").strip()
    if not mobile or not password or not totp_secret:
        from credentials.dynamic_loader import get_dynamic_credentials
        dyn = get_dynamic_credentials(clean_sub) or get_dynamic_credentials(sub_account_id) or get_dynamic_credentials(cid) or {}
        if not mobile:
            mobile = dyn.get("mobile_number") or dyn.get("phone_number") or ""
        if not password:
            password = dyn.get("password") or ""
        if not totp_secret:
            totp_secret = dyn.get("totp_secret") or ""

    if mobile and password and totp_secret:
        logger.info(f"🔄 Executing automated Axis Direct headless login for {cid} ({mobile})...")
        login_res = await headless_axis_direct_login(
            api_client_id=cid,
            authorization_key=authorization_key,
            mobile=mobile,
            password=password,
            totp_secret=totp_secret,
            public_key=public_key,
            static_ip=static_ip,
            sub_account_id=clean_sub or sub_account_id,
        )
        if login_res.get("status"):
            return login_res
        logger.warning(f"❌ Axis Direct headless login failed for {cid}: {login_res.get('msg')}")
        return login_res

    return {
        "status": False,
        "msg": f"Could not refresh Axis Direct token for {cid}: missing mobile/password/totp_secret for headless fallback.",
    }


async def refresh_user_axis_direct_access_token(cred: Credential) -> bool:
    """
    Renews Axis Direct access token:
    1. Tries silent refresh with refreshToken via /v1/handshake/get-auth-token.
    2. Falls back to headless login if refreshToken expired.
    3. Persists new token and refresh_token to SQLite and memory.
    """
    from credentials.dynamic_loader import get_dynamic_credentials, update_dynamic_credential_token
    clean_cid = str(cred.client_id).replace("RAPID_", "").strip()
    dyn = get_dynamic_credentials(cred.client_id) or get_dynamic_credentials(clean_cid) or {}

    phone = getattr(cred, "mobile_number", "") or ""
    if not phone:
        phone = dyn.get("mobile_number") or dyn.get("phone_number") or ""
        if phone:
            cred.mobile_number = phone

    ref_tok = getattr(cred, "refresh_token", "") or ""
    if not ref_tok:
        ref_tok = dyn.get("refresh_token") or ""
        if ref_tok:
            cred.refresh_token = ref_tok

    pwd = cred.password or dyn.get("password") or ""
    totp = cred.totp_secret or dyn.get("totp_secret") or ""
    sub_acc = getattr(cred, "sub_account_id", "") or clean_cid or cred.client_id or ""

    res = await generate_user_axis_direct_access_token(
        api_client_id=cred.api_key,
        authorization_key=cred.api_secret,
        sub_account_id=getattr(cred, "sub_account_id", "") or cred.client_id or "",
        refresh_token=ref_tok,
        access_token=cred.token or "",
        public_key=getattr(cred, "public_key", "") or "",
        static_ip=str(cred.static_ip or ""),
        phone_number=phone,
        password=pwd,
        totp_secret=totp,
    )

    if res.get("status") and res.get("token"):
        cred.token = str(res["token"]).strip()
        if res.get("refresh_token"):
            cred.refresh_token = str(res["refresh_token"]).strip()
        if res.get("public_key"):
            cred.public_key = str(res["public_key"]).strip()
        if res.get("sub_account_id"):
            cred.sub_account_id = str(res["sub_account_id"]).strip()
        cred.token_updated_at = datetime.now()

        update_dynamic_credential_token(cred.client_id, cred.token)
        if clean_cid != cred.client_id:
            update_dynamic_credential_token(clean_cid, cred.token)
        if dyn:
            dyn["token"] = cred.token
            if cred.refresh_token:
                dyn["refresh_token"] = cred.refresh_token
            if cred.public_key:
                dyn["public_key"] = cred.public_key
            if cred.sub_account_id:
                dyn["sub_account_id"] = cred.sub_account_id

        await save_working_credential(cred)
        logger.info(f"🔑 Successfully renewed Axis Direct access token for {cred.client_id}")
        return True

    logger.warning(f"Axis Direct token renewal failed for {cred.client_id}: {res.get('msg')}")
    return False


async def login_axisdirect(cred: Credential) -> str:
    """
    Axis Direct Session Validation & Verification.
    Validates credentials against Axis Direct invest gateway.
    1. Ensures Public Key is present (fetches via handshake if missing).
    2. Probes GET /trading/portfolio/get-positions (or /trading/orders/get-order-book fallback).
    3. If session expired or invalid, attempts automatic in-flight token renewal.
    4. If still invalid, raises ValueError so account is safely placed in Failed Auth.
    """
    api_client_id = cred.api_key or ""
    authorization_key = cred.api_secret or ""
    sub_account_id = getattr(cred, "sub_account_id", "") or cred.client_id or ""
    access_token = cred.token or ""
    public_key = getattr(cred, "public_key", "") or ""

    if not api_client_id or not authorization_key:
        raise ValueError(f"Axis Direct credentials missing for {cred.client_id}: api_client_id or authorization_key not configured.")

    if not public_key:
        public_key = await get_axis_public_key(api_client_id, authorization_key, str(cred.static_ip or ""))
        if public_key:
            cred.public_key = public_key

    probe_url = f"{AXIS_INVEST_BASE_URL.rstrip('/')}/trading/portfolio/get-positions"
    probe_body = {"segment": "FO", "interop": True, "type": "open"}

    needs_renewal = not access_token or not public_key
    if not needs_renewal:
        status_code, resp = await send_axis_api_request(
            method="POST",
            url=probe_url,
            static_ip=str(cred.static_ip or ""),
            authorization_key=authorization_key,
            api_client_id=api_client_id,
            sub_account_id=sub_account_id,
            access_token=access_token,
            public_key=public_key,
            body=probe_body,
        )

        if status_code == 404:
            probe_url = f"{AXIS_INVEST_BASE_URL.rstrip('/')}/trading/orders/get-order-book"
            probe_body = {"segment": "FO", "omsOrderId": "", "orderStatus": "-1"}
            status_code, resp = await send_axis_api_request(
                method="POST",
                url=probe_url,
                static_ip=str(cred.static_ip or ""),
                authorization_key=authorization_key,
                api_client_id=api_client_id,
                sub_account_id=sub_account_id,
                access_token=access_token,
                public_key=public_key,
                body=probe_body,
            )

        err_dict = resp.get("error", {}) if isinstance(resp, dict) else {}
        err_code = str(err_dict.get("code") or "")
        if status_code in (401, 403) or err_code in ("1081", "1082", "1200", "1002", "1003"):
            needs_renewal = True

    if needs_renewal:
        logger.info(f"🔄 Renewing Axis Direct session token for {cred.client_id}...")
        renewed = await refresh_user_axis_direct_access_token(cred)
        if not renewed:
            raise ValueError(f"Axis Direct Auth Error for {cred.client_id}: Token renewal failed.")

        access_token = cred.token
        public_key = cred.public_key or public_key

        # Re-probe with refreshed token
        status_code, resp = await send_axis_api_request(
            method="POST",
            url=probe_url,
            static_ip=str(cred.static_ip or ""),
            authorization_key=authorization_key,
            api_client_id=api_client_id,
            sub_account_id=sub_account_id,
            access_token=access_token,
            public_key=public_key,
            body=probe_body,
        )
        err_dict = resp.get("error", {}) if isinstance(resp, dict) else {}
        err_code = str(err_dict.get("code") or "")
        if status_code in (401, 403) or err_code in ("1081", "1082", "1200", "1002", "1003"):
            msg = err_dict.get("message") or f"Session error code {err_code or status_code}"
            raise ValueError(f"Axis Direct Auth Error for {cred.client_id}: {msg}")

    # Success: persist working credential
    cred.token_updated_at = datetime.now()
    await save_working_credential(cred)
    masked = f"{cred.token[:8]}...{cred.token[-8:]}" if len(cred.token) > 16 else cred.token
    logger.info(f"🔑 Validated Axis Direct session token for {cred.client_id}: {masked}")
    return cred.token
