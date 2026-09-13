from base64 import urlsafe_b64encode
from hashlib import sha256
from uuid import UUID

from cryptography.fernet import Fernet, InvalidToken
from sqlmodel import Session

from evalweave.core.config import AgentConfig, get_settings
from evalweave.db.models import EvaluationModel


def _cipher() -> Fernet:
    digest = sha256(get_settings().auth.jwt_secret.encode()).digest()
    return Fernet(urlsafe_b64encode(digest))


def encrypt_api_key(value: str) -> str:
    return _cipher().encrypt(value.encode()).decode()


def decrypt_api_key(value: str) -> str:
    try:
        return _cipher().decrypt(value.encode()).decode()
    except InvalidToken as error:
        raise ValueError("评测模型密钥无法解密，请由管理员重新填写") from error


def resolve_agent_config(session: Session, model_id: UUID | str | None) -> AgentConfig:
    default = get_settings().agent
    if not model_id:
        return default
    try:
        identifier = UUID(str(model_id))
    except ValueError as error:
        raise ValueError("评测模型标识无效") from error
    model = session.get(EvaluationModel, identifier)
    if model is None or not model.is_active:
        raise ValueError("所选评测模型不存在或已停用")
    return default.model_copy(
        update={
            "enabled": True,
            "api_mode": model.api_mode,
            "base_url": model.base_url,
            "api_key": decrypt_api_key(model.api_key_encrypted),
            "model": model.model_name,
        }
    )
