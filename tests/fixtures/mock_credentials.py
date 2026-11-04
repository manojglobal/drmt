"""Mock credentials fixtures for unit tests."""

from datetime import datetime
import zoneinfo

_IST = zoneinfo.ZoneInfo("Asia/Kolkata")
TODAY_IST_STR = datetime.now(tz=_IST).strftime("%Y-%m-%d %H:%M:%S")

MOCK_FLATTRADE_CRED = {
    "broker": "FLATTRADE",
    "client_id": "FZ48062",
    "password": "Alg0Tr@d1ng4",
    "totp_secret": "2364AP5Z35V2R4A3AHDJ4K464357M2K6",
    "token": "mock_valid_token_1234567890abcdef",
    "static_ip": "172.31.8.142",
    "fingerprint": "FLATTRADE:FZ48062",
    "status": "working",
    "token_created_at": TODAY_IST_STR
}

MOCK_EXPIRED_FLATTRADE_CRED = {
    "broker": "FLATTRADE",
    "client_id": "FZ48062",
    "password": "Alg0Tr@d1ng4",
    "totp_secret": "2364AP5Z35V2R4A3AHDJ4K464357M2K6",
    "token": "mock_expired_token_1234567890",
    "static_ip": "172.31.8.142",
    "fingerprint": "FLATTRADE:FZ48062",
    "status": "working",
    "token_created_at": "2026-10-01 09:15:00"  # Expired prior date
}

MOCK_ALICEBLUE_CRED = {
    "broker": "ALICEBLUE",
    "client_id": "AB12345",
    "api_key": "mock_api_key",
    "api_secret": "mock_api_secret",
    "token": "mock_alice_token_bearer_123",
    "fingerprint": "ALICEBLUE:AB12345",
    "status": "working",
    "token_created_at": TODAY_IST_STR
}

MOCK_CSV_CONTENT = f"""id,user_id,first_name,last_name,broker_id,broker,broker_creds,static_ip
1,50,Manoj,Flattrade,5,FLATTRADE,"{{""client_id"": ""FZ48062"", ""password"": ""Alg0Tr@d1ng4"", ""totp_secret_key"": ""2364AP5Z35V2R4A3AHDJ4K464357M2K6"", ""access_token"": ""mock_token_fz"", ""access_token_created_at"": ""{TODAY_IST_STR}""}}",172.31.8.142
2,51,Test,AliceBlue,2,ALICEBLUE,"{{""client_id"": ""AB12345"", ""api_key"": ""mock_ab_key"", ""access_token"": ""mock_token_ab"", ""access_token_created_at"": ""{TODAY_IST_STR}""}}",
"""
