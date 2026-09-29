from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field

class User(BaseModel):
    id: int
    username: str
    password_hash: str = Field(exclude=True)
    created_at: datetime

class Session(BaseModel):
    id: int
    user_id: int
    token: str
    expires_at: datetime

class Credential(BaseModel):
    id: Optional[int] = None
    broker: str
    client_id: str
    api_key: str = ""
    api_secret: str = Field(default="", exclude=True)
    totp_secret: str = Field(default="", exclude=True)
    password: str = Field(default="", exclude=True)
    static_ip: str = ""
    fingerprint: str
    status: str
    token: str = ""
    token_updated_at: Optional[datetime] = None

class ExitHistory(BaseModel):
    id: Optional[int] = None
    broker: str
    symbol: str
    status: str
    timestamp: Optional[datetime] = None

class Position(BaseModel):
    id: str
    broker: str
    client_id: str
    username: str = ""
    symbol: str
    token: str
    exchange: str
    product_type: str
    order_type: str
    transaction_type: str
    quantity: int
    avg_price: float
    ltp: float
    pnl: float

class ExitedPosition(Position):
    exit_price: float
    exited_at: datetime

class SystemLog(BaseModel):
    id: int
    timestamp: str
    level: str
    message: str
    module: str

class AuthAuditLog(BaseModel):
    id: Optional[int] = None
    user_id: Optional[int] = None
    username: str
    action: str
    status: str
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None
    details: Optional[str] = None
    timestamp: Optional[str] = None

