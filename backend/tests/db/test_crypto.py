import pytest
from cryptography.fernet import Fernet, InvalidToken
from pydantic import SecretStr
from sqlalchemy.dialects.postgresql import dialect

from app.db.crypto import EncryptedText
from app.db.models import MarkingProfile, PlatformAccount


@pytest.mark.parametrize("value", ["", "test-access-token", "Тестовые данные +7 (000) 000-00-00"])
def test_encryption_round_trip(test_settings, value: str) -> None:
    field = EncryptedText()
    encrypted = field.process_bind_param(value, dialect())

    assert encrypted != value
    assert Fernet(test_settings.token_encryption_key.get_secret_value()).decrypt(encrypted.encode()).decode() == value
    assert field.process_result_value(encrypted, dialect()) == value
    assert field.process_bind_param(value, dialect()) != encrypted


def test_null_does_not_require_key(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected_key_access():
        pytest.fail("NULL must not access encryption settings")

    monkeypatch.setattr(EncryptedText, "_cipher", unexpected_key_access)
    assert EncryptedText().process_bind_param(None, dialect()) is None
    assert EncryptedText().process_result_value(None, dialect()) is None


def test_wrong_key_and_tampering_are_rejected(test_settings, monkeypatch: pytest.MonkeyPatch) -> None:
    field = EncryptedText()
    encrypted = field.process_bind_param("test-value", dialect())
    with pytest.raises(InvalidToken):
        field.process_result_value(encrypted[:-5] + "AAAAA", dialect())
    monkeypatch.setattr(test_settings, "token_encryption_key", SecretStr(Fernet.generate_key().decode()))
    with pytest.raises(InvalidToken):
        field.process_result_value(encrypted, dialect())


@pytest.mark.parametrize(
    "model,column",
    [
        (PlatformAccount, "access_token_enc"),
        (PlatformAccount, "refresh_token_enc"),
        (MarkingProfile, "inn_enc"),
        (MarkingProfile, "phone_enc"),
    ],
)
def test_encrypted_columns_use_bind_and_result_processors(test_settings, model, column: str) -> None:
    field = model.__table__.c[column].type
    assert isinstance(field, EncryptedText)
    bind = field.bind_processor(dialect())
    result = field.result_processor(dialect(), None)
    stored = bind("test-value")
    assert stored != "test-value"
    assert result(stored) == "test-value"
