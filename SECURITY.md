# Security

## Runtime bootstrap secrets

The runtime token has a hash for authentication and encrypted-at-rest storage
solely for bootstrap injection into a mode-0600 runtime environment file. It is
not returned through heartbeat, diagnostics, events, or normal account APIs.

## Control-plane auth

Admin password хэшируется Argon2id. Login выдаёт opaque random session token в HttpOnly, SameSite=Strict cookie; production cookie имеет Secure flag. В DB хранится SHA-256 token hash. Session имеет expiry/revocation. Все admin mutations требуют X-CSRF-Token.

Login имеет in-process per-client rate limit; nginx example добавляет edge rate limit. Для нескольких replicas edge rate limit остаётся обязательным.

## Agent auth

Node и runtime имеют отдельные random bearer credentials. API rotation показывает plaintext token один раз; DB хранит только hash. Runtime A token не может записывать heartbeat Runtime B. Protocol mismatch отклоняется до mutation. Runtime heartbeat допускает только фиксированный набор supervisor-фаз; diagnostics ограничены по размеру и редактируют поля с credential-like именами.

Production boundary: TLS + private network; mTLS рекомендуется как следующий hardening layer.

## Account secrets

Steam/email passwords шифруются Fernet. В production DST_FARM_SECRET_KEY обязателен и должен жить вне repository/DB backup. Development-only fallback .data/dev_master.key запрещён production validation.

List/detail API никогда не загружают AccountSecret и не возвращают ciphertext/plaintext. Structured logging redacts keys, содержащие password, secret, token, cookie или authorization.

Не хранить Steam Guard seeds, recovery codes, payment data и cookies без отдельной threat model.

## Startup guards

ENVIRONMENT=production fail-fast при SQLite, mock runtime provider, missing key, weak/default admin password, debug=true или plaintext public URL.

## Network/TLS

FastAPI слушает loopback за nginx. HTTP перенаправляется на HTTPS; HSTS включён. Agent routes ограничены RFC1918 networks в example config. Disabled VIEW provider не публикует VNC. Реальный VIEW transport обязан проверять short-lived capability.

## Repository hygiene

.env, databases, dev keys, caches и virtual environments игнорируются. Перед release выполняются secret scan и dependency sanity. Example values REPLACE_ME не являются credentials.

## Worker and VIEW boundaries

The worker subprocess receives no Steam/email password, database connection, Incus
authority, runtime bearer, Node bearer, or admin cookie. Its local multiprocessing
queues are inherited OS handles, not a public listener. It does not hide automation or
implement fingerprint/IP spoofing, enforcement bypass, injection, CAPTCHA, Steam
Guard, registration, inventory trade, market, transfer, or cashout behavior.

Runtime Agent reads its bearer into parent memory and scrubs credential-like variables
from `os.environ` before creating Display/Steam/DST/worker children. Subprocess
isolation is a crash and lifecycle boundary, not a sandbox against malicious local code.

Remote VIEW stores only a token hash. Plaintext is returned once, exchanged in an
authenticated CSRF-protected request, then held in an HttpOnly path-scoped cookie for
the short TTL. It never appears in the transport URL. xpra has no public listener: both
container and host exposure are loopback-only behind the authenticated adapter.
