from __future__ import annotations

from app.config import ConfigurationError
from app.db import Database
from app.domain.errors import ControlPlaneError
from app.models import ErrorCode, RuntimeImage, utcnow


class RuntimeImageNotVerified(ControlPlaneError):
    code = ErrorCode.RUNTIME_IMAGE_NOT_VERIFIED


class RuntimeImageService:
    def __init__(self, db: Database):
        self.db = db

    def ensure_configured(
        self, *, version: str, provider: str, source_ref: str, verified: bool
    ) -> None:
        """Register only configuration metadata; it is not evidence of real-node validation."""
        with self.db.transaction(immediate=True) as session:
            image = session.get(RuntimeImage, version)
            if image is None:
                session.add(
                    RuntimeImage(
                        version=version,
                        provider=provider,
                        source_ref=source_ref,
                        verified=verified,
                        verified_at=utcnow() if verified else None,
                        metadata_json={
                            "validation": "CONFIGURED_AS_VERIFIED"
                            if verified
                            else "UNVALIDATED_ON_REAL_NODE"
                        },
                    )
                )
            else:
                if image.provider != provider or image.source_ref != source_ref:
                    raise ConfigurationError(
                        "runtime image version is already bound to a different source"
                    )
                if verified and not image.verified:
                    image.verified = True
                    image.verified_at = utcnow()
                    image.metadata_json = {"validation": "CONFIGURED_AS_VERIFIED"}

    def require_verified(self, version: str, provider: str) -> RuntimeImage:
        with self.db.session() as session:
            image = session.get(RuntimeImage, version)
            if image is None or image.provider != provider or not image.verified:
                raise RuntimeImageNotVerified(
                    "selected runtime image is not verified for this provider"
                )
            return image
