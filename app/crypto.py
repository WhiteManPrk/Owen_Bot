"""Шифрование ключей OwenCloud в БД (Fernet: AES-128-CBC + HMAC-SHA256)."""
import hashlib

from cryptography.fernet import Fernet, InvalidToken


class TokenCrypto:
    def __init__(self, key: str):
        try:
            self._fernet = Fernet(key.encode())
        except ValueError as e:
            raise SystemExit(
                "ENCRYPTION_KEY некорректен. Сгенерируйте: "
                "python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
            ) from e

    def encrypt(self, token: str) -> str:
        return self._fernet.encrypt(token.encode()).decode()

    def decrypt(self, token_enc: str) -> str | None:
        """None — если зашифровано другим ключом (ENCRYPTION_KEY сменили)."""
        try:
            return self._fernet.decrypt(token_enc.encode()).decode()
        except InvalidToken:
            return None

    @staticmethod
    def fingerprint(token: str) -> str:
        """Хэш для поиска дублей без расшифровки."""
        return hashlib.sha256(token.encode()).hexdigest()
