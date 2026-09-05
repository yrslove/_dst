from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from datetime import timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from sqlalchemy import select, update

from app.config import Settings
from app.db import Database
from app.models import AdminSession, AdminUser, utcnow
from app.services.security import ensure_utc, generate_token, hash_token, token_matches


class InvalidCredentials(RuntimeError):
    pass


class RateLimited(RuntimeError):
    pass


class AuthService:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings
        self.passwords = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)
        self._attempts: dict[str, deque[float]] = defaultdict(deque)
        self._attempt_lock = threading.Lock()

    def bootstrap_admin(self) -> None:
        with self.db.transaction(immediate=True) as session:
            user = session.scalar(
                select(AdminUser).where(
                    AdminUser.username == self.settings.admin_username
                )
            )
            if user is None:
                session.add(
                    AdminUser(
                        username=self.settings.admin_username,
                        password_hash=self.passwords.hash(self.settings.admin_password),
                    )
                )

    def _check_rate(self, client_key: str) -> None:
        now = time.monotonic()
        with self._attempt_lock:
            attempts = self._attempts[client_key]
            while attempts and attempts[0] < now - 60:
                attempts.popleft()
            if len(attempts) >= 10:
                raise RateLimited("too many login attempts")
            attempts.append(now)

    def login(
        self, username: str, password: str, client_key: str
    ) -> tuple[str, str, int]:
        self._check_rate(client_key)
        with self.db.transaction(immediate=True) as session:
            user = session.scalar(
                select(AdminUser).where(AdminUser.username == username)
            )
            valid = False
            if user is not None and user.enabled:
                try:
                    valid = self.passwords.verify(user.password_hash, password)
                except (VerifyMismatchError, InvalidHashError):
                    valid = False
            if not valid or user is None:
                raise InvalidCredentials("invalid username or password")
            if self.passwords.check_needs_rehash(user.password_hash):
                user.password_hash = self.passwords.hash(password)
            token = generate_token()
            csrf = generate_token()
            expires = utcnow() + timedelta(seconds=self.settings.session_ttl_seconds)
            session.add(
                AdminSession(
                    admin_user_id=user.id,
                    token_hash=hash_token(token),
                    csrf_hash=hash_token(csrf),
                    expires_at=expires,
                )
            )
            return token, csrf, self.settings.session_ttl_seconds

    def authenticate(self, token: str | None) -> tuple[AdminUser, AdminSession] | None:
        if not token:
            return None
        now = utcnow()
        with self.db.session() as session:
            admin_session = session.scalar(
                select(AdminSession).where(
                    AdminSession.token_hash == hash_token(token),
                    AdminSession.revoked_at.is_(None),
                )
            )
            if admin_session is None or ensure_utc(admin_session.expires_at) <= now:
                return None
            user = session.get(AdminUser, admin_session.admin_user_id)
            if user is None or not user.enabled:
                return None
            admin_session.last_seen_at = now
            session.flush()
            session.expunge(user)
            session.expunge(admin_session)
            return user, admin_session

    def rotate_csrf(self, token: str) -> str | None:
        csrf = generate_token()
        with self.db.transaction(immediate=True) as session:
            admin_session = session.scalar(
                select(AdminSession).where(
                    AdminSession.token_hash == hash_token(token),
                    AdminSession.revoked_at.is_(None),
                )
            )
            if (
                admin_session is None
                or ensure_utc(admin_session.expires_at) <= utcnow()
            ):
                return None
            admin_session.csrf_hash = hash_token(csrf)
            return csrf

    def csrf_valid(self, admin_session: AdminSession, csrf: str | None) -> bool:
        return bool(csrf and token_matches(csrf, admin_session.csrf_hash))

    def logout(self, token: str | None) -> None:
        if not token:
            return
        with self.db.session() as session:
            session.execute(
                update(AdminSession)
                .where(
                    AdminSession.token_hash == hash_token(token),
                    AdminSession.revoked_at.is_(None),
                )
                .values(revoked_at=utcnow())
            )
