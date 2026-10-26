from cryptography.fernet import Fernet
from sqlalchemy import Text
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator


class EncryptedText(TypeDecorator[str]):
    impl = Text
    cache_ok = True

    @staticmethod
    def _cipher() -> Fernet:
        from app.config import settings

        return Fernet(settings.token_encryption_key.get_secret_value().encode("ascii"))

    def process_bind_param(self, value: str | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        return self._cipher().encrypt(value.encode("utf-8")).decode("ascii")

    def process_result_value(self, value: str | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        return self._cipher().decrypt(value.encode("ascii")).decode("utf-8")
