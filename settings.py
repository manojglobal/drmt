import os
from pydantic_settings import BaseSettings, SettingsConfigDict
from utils.paths import get_app_dir

_env_path = os.path.join(get_app_dir(), ".env")

class Settings(BaseSettings):
    PORT: int = 10000
    DB_PATH: str = "data/drmt.db"
    DRMT_PROXY_URL: str = "https://demoapi.quantivo.in/proxy"
    ENVIRONMENT: str = "LOCAL"
    DAILY_REFRESH_HOUR: int = 9
    DAILY_REFRESH_MINUTE: int = 10
    WORKER_POOL_SIZE: int = 50
    API_CONCURRENCY: int = 10
    DB_POOL_SIZE: int = 5
    USER_RATE_LIMIT: int = 10
    GLOBAL_RATE_LIMIT: int = 50
    REDIS_URL: str = "redis://localhost:6379"
    EMAIL_HOST: str = "smtp.office365.com"
    EMAIL_PORT: int = 587
    EMAIL_USER: str = ""
    EMAIL_PASS: str = ""
    EMAIL_SECRET_KEY: str = ""
    EMAIL_ALGORITHM: str = "HS256"
    EMAIL_TO: str = "sahil@quantivo.in"
    EMAIL_FROM: str = "noreply@drmt.app"
    
    CONFIG_ME_URL: str = "https://config.me/api/credentials"
    CONFIG_ME_API_KEY: str = ""
    CREDENTIALS_CSV_PATH: str = "static/plato_client_credentials.csv"
    CREDENTIALS_CSV_URL: str = ""
    USE_DROPBOX_CREDENTIALS: bool = False
    DROPBOX_CREDENTIALS_URL: str = ""
    ALICE_BLUE_FLOW: str = "WEB"  # "WEB" (OMK Web Execution) or "SDK" (Open-API SDK Flow)
    BLOCKED_RECHECK_INTERVAL_SEC: int = 90  # 1.5 minutes interval for blocked credentials recovery
    
    model_config = SettingsConfigDict(env_file=_env_path, env_file_encoding="utf-8", extra="ignore")

settings = Settings()
