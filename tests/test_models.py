"""Unit tests for models/schemas.py Pydantic models."""

import unittest
from datetime import datetime
from pydantic import ValidationError

from models.schemas import (
    User,
    Session,
    Credential,
    Position,
    ExitedPosition,
    SystemLog,
    AuthAuditLog,
)


class TestModelsSchemas(unittest.TestCase):
    """Test suite for data models and schema validation."""

    def test_position_model_long_position(self):
        """Verify normal long position creation and field validation."""
        pos = Position(
            id="FLATTRADE:FZ48062:RELIANCE-EQ",
            broker="FLATTRADE",
            client_id="FZ48062",
            username="Manoj Flattrade",
            symbol="RELIANCE-EQ",
            token="2885",
            exchange="NSE",
            product_type="INTRADAY",
            order_type="MARKET",
            transaction_type="BUY",
            quantity=10,
            avg_price=2900.5,
            ltp=2910.0,
            pnl=95.0,
            cash=29005.0,
            cash_value=29005.0,
        )
        self.assertEqual(pos.id, "FLATTRADE:FZ48062:RELIANCE-EQ")
        self.assertEqual(pos.transaction_type, "BUY")
        self.assertEqual(pos.quantity, 10)
        self.assertEqual(pos.avg_price, 2900.5)

    def test_position_model_short_position_negative_quantity(self):
        """Verify short position creation preserves negative quantity."""
        pos = Position(
            id="FLATTRADE:FZ48062:WIPRO-EQ",
            broker="FLATTRADE",
            client_id="FZ48062",
            symbol="WIPRO-EQ",
            token="3787",
            exchange="NSE",
            product_type="INTRADAY",
            order_type="",
            transaction_type="SELL",
            quantity=-1,
            avg_price=193.73,
            ltp=193.30,
            pnl=0.43,
        )
        self.assertEqual(pos.quantity, -1)
        self.assertEqual(pos.transaction_type, "SELL")
        self.assertEqual(pos.cash, 0.0)  # default value

    def test_position_model_missing_mandatory_field(self):
        """Verify ValidationError is raised when mandatory fields are omitted."""
        with self.assertRaises(ValidationError):
            # Missing broker and symbol
            Position(
                id="TEST:1",
                client_id="C1",
                token="123",
                exchange="NSE",
                product_type="MIS",
                order_type="",
                transaction_type="BUY",
                quantity=1,
                avg_price=10.0,
                ltp=10.0,
                pnl=0.0,
            )

    def test_credential_model_secrets_exclusion(self):
        """Verify sensitive credentials are excluded from default serialization."""
        cred = Credential(
            broker="FLATTRADE",
            client_id="FZ48062",
            password="SecretPassword123",
            totp_secret="SECRET_TOTP",
            api_secret="API_SECRET",
            fingerprint="FLATTRADE:FZ48062",
            status="working",
            token="token_xyz",
        )
        dumped = cred.model_dump()
        self.assertNotIn("password", dumped)
        self.assertNotIn("totp_secret", dumped)
        self.assertNotIn("api_secret", dumped)
        self.assertIn("broker", dumped)
        self.assertIn("client_id", dumped)
        self.assertIn("token", dumped)

    def test_exited_position_inheritance(self):
        """Verify ExitedPosition inherits Position attributes plus exit_price and timestamp."""
        now = datetime.now()
        exited = ExitedPosition(
            id="FLATTRADE:FZ48062:WIPRO-EQ",
            broker="FLATTRADE",
            client_id="FZ48062",
            symbol="WIPRO-EQ",
            token="3787",
            exchange="NSE",
            product_type="INTRADAY",
            order_type="",
            transaction_type="SELL",
            quantity=-1,
            avg_price=193.73,
            ltp=193.30,
            pnl=0.43,
            exit_price=193.30,
            exited_at=now,
        )
        self.assertEqual(exited.exit_price, 193.30)
        self.assertEqual(exited.exited_at, now)
        self.assertEqual(exited.quantity, -1)

    def test_user_and_session_validation(self):
        """Verify User and Session model validation."""
        now = datetime.now()
        user = User(
            id=1,
            username="admin",
            password_hash="hashed_pw_123",
            created_at=now,
        )
        self.assertEqual(user.id, 1)
        self.assertEqual(user.username, "admin")
        self.assertNotIn("password_hash", user.model_dump())

        session = Session(
            id=10,
            user_id=1,
            token="session_token_abc",
            expires_at=now,
        )
        self.assertEqual(session.user_id, 1)
        self.assertEqual(session.token, "session_token_abc")


if __name__ == "__main__":
    unittest.main()
