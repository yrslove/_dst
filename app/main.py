from __future__ import annotations

import asyncio
import logging
import re
import socket
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import (
    FastAPI,
    HTTPException,
    Request,
    Response,
    WebSocket,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select

from app.config import Settings
from app.db import Database, migrate, schema_revision
from app.domain.errors import ControlPlaneError
from app.domain.state import InvalidStateTransition
from app.logging_config import configure_logging, request_id_var
from app.models import (
    GameplayTask,
    Job,
    JobKind,
    JobStatus,
    RuntimeInstance,
    RuntimeState,
)
from app.providers.base import ProviderError
from app.providers.incus_cli import IncusCLIProvider
from app.providers.mock import MockProvider
from app.providers.view import (
    DisabledRuntimeViewProvider,
    MockRuntimeViewProvider,
    ViewUnavailable,
    XpraRuntimeViewProvider,
)
from app.runtime.display import DisplayEnvironment
from app.schemas import (
    AccountCreate,
    LoginRequest,
    MoveRuntimeRequest,
    NodeHeartbeatRequest,
    RebuildRuntimeRequest,
    RuntimeHeartbeatRequest,
    ViewAccessRequest,
    ViewSessionCreate,
    WorkerModeRequest,
)
from app.services.account_scheduler import AccountScheduler
from app.services.accounts import (
    AccountBusyError,
    AccountService,
    DuplicateAccountError,
)
from app.services.agents import AgentService
from app.services.auth import AuthService, InvalidCredentials, RateLimited
from app.services.executor import JobExecutor
from app.services.jobs import JobQueue, serialize_job
from app.services.leadership import SchedulerLeadershipService
from app.services.leases import LeaseService
from app.services.reconciler import Reconciler
from app.services.records import add_audit
from app.services.runtime_images import RuntimeImageService
from app.services.scheduler import Scheduler
from app.services.secrets import SecretsService
from app.services.views import ViewService, ViewSessionNotFound, ViewSessionNotReady
from app.services.watchdog import Watchdog
from app.services.workers import WorkerControlError, WorkerControlService

EXPECTED_SCHEMA_REVISION = "0012_schedule_pause"
logger = logging.getLogger("control_plane")


def _bearer(request: Request) -> str | None:
    value = request.headers.get("authorization", "")
    scheme, _, token = value.partition(" ")
    return token if scheme.lower() == "bearer" and token else None


def _error(
    code: str, message: str, status_code: int, request_id: str | None = None
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}, "request_id": request_id},
    )


def create_app(settings: Settings | None = None, *, provider=None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.validate()
    settings.ensure_local_dirs()
    if settings.auto_migrate:
        migrate(settings.database_url)
    configure_logging(logging.DEBUG if settings.debug else logging.INFO)

    db = Database(settings.database_url)
    secrets_service = SecretsService(settings)
    jobs = JobQueue(
        db,
        lease_seconds=settings.job_lease_seconds,
        retry_base_seconds=settings.retry_base_seconds,
        max_attempts=settings.retry_max_attempts,
    )
    leases = LeaseService(
        db,
        lease_seconds=settings.slot_lease_seconds,
        node_stale_seconds=settings.node_stale_seconds,
    )
    account_scheduler = AccountScheduler(db, jobs, leases)
    accounts = AccountService(db, secrets_service, jobs, settings)
    auth = AuthService(db, settings)
    selected_provider = provider or (
        IncusCLIProvider(settings)
        if settings.runtime_provider == "incus"
        else MockProvider()
    )
    agents = AgentService(db, leases, settings)
    if settings.runtime_view_provider == "mock":
        view_provider = MockRuntimeViewProvider()
    elif settings.runtime_view_provider == "xpra":
        view_provider = XpraRuntimeViewProvider(
            selected_provider, incus_remote=settings.incus_remote
        )
    else:
        view_provider = DisabledRuntimeViewProvider()
    worker_controls = WorkerControlService(db)
    views = ViewService(
        db,
        view_provider,
        ttl_seconds=settings.view_session_ttl_seconds,
        display=DisplayEnvironment(
            settings.runtime_display, xauthority=settings.runtime_xauthority
        ),
        workers=worker_controls,
    )
    leadership = SchedulerLeadershipService(
        db,
        f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}",
        lease_seconds=settings.scheduler_leader_lease_seconds,
    )
    scheduler = Scheduler(db, jobs, leadership, account_scheduler)
    reconciler = Reconciler(db, selected_provider, leadership)
    watchdog = Watchdog(db, leases, settings)
    executor = JobExecutor(
        db,
        jobs,
        leases,
        selected_provider,
        settings,
        worker_id=f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}",
    )
    auth.bootstrap_admin()
    RuntimeImageService(db).ensure_configured(
        version=settings.current_image_version,
        provider=settings.runtime_provider,
        source_ref=settings.incus_base_instance
        if settings.runtime_provider == "incus"
        else "mock",
        # Mock is a code-only provider, so it can verify its deterministic fixture;
        # Incus stays unverified unless explicitly configured after real-node validation.
        verified=settings.current_image_verified or settings.runtime_provider == "mock",
    )
    node_id = accounts.ensure_default_node()

    stop_event = asyncio.Event()

    async def periodic(name: str, interval: float, callback) -> None:
        while not stop_event.is_set():
            try:
                await asyncio.to_thread(callback)
            except Exception:
                logger.exception(
                    "background component failed", extra={"event": f"{name}_FAILED"}
                )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue

    async def execute_jobs() -> None:
        while not stop_event.is_set():
            try:
                worked = await asyncio.to_thread(executor.execute_next)
            except Exception:
                logger.exception("job loop failed", extra={"event": "JOB_LOOP_FAILED"})
                worked = False
            if worked:
                continue
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=settings.job_poll_interval_seconds
                )
            except asyncio.TimeoutError:
                continue

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        stop_event.clear()
        tasks: list[asyncio.Task] = []
        if settings.background_workers:
            tasks = [
                asyncio.create_task(execute_jobs(), name="job-executor"),
                asyncio.create_task(
                    periodic(
                        "scheduler", settings.scheduler_interval_seconds, scheduler.tick
                    ),
                    name="scheduler",
                ),
                asyncio.create_task(
                    periodic(
                        "reconciler",
                        settings.scheduler_interval_seconds,
                        reconciler.tick,
                    ),
                    name="reconciler",
                ),
                asyncio.create_task(
                    periodic(
                        "watchdog", settings.watchdog_interval_seconds, watchdog.tick
                    ),
                    name="watchdog",
                ),
                asyncio.create_task(
                    periodic(
                        "view-cleanup",
                        min(60, settings.view_session_ttl_seconds),
                        views.cleanup_expired_sessions,
                    ),
                    name="view-cleanup",
                ),
            ]
        try:
            yield
        finally:
            stop_event.set()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            try:
                await asyncio.to_thread(leadership.release)
            finally:
                db.dispose()

    app = FastAPI(
        title="DST Runtime Orchestrator",
        version=settings.app_version,
        debug=settings.debug,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.db = db
    app.state.accounts = accounts
    app.state.auth = auth
    app.state.jobs = jobs
    app.state.leases = leases
    app.state.provider = selected_provider
    app.state.agents = agents
    app.state.scheduler = scheduler
    app.state.account_scheduler = account_scheduler
    app.state.leadership = leadership
    app.state.reconciler = reconciler
    app.state.watchdog = watchdog
    app.state.executor = executor
    app.state.views = views
    app.state.node_id = node_id

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", request_id):
            request_id = uuid.uuid4().hex
        request.state.request_id = request_id
        token = request_id_var.set(request_id)
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        response.headers["X-Request-ID"] = request_id
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        # Pydantic's default response includes rejected input, possibly passwords.
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "VALIDATION_FAILED",
                    "message": "invalid request fields",
                },
                "fields": [".".join(map(str, item["loc"])) for item in exc.errors()],
                "request_id": request.state.request_id,
            },
        )

    @app.exception_handler(ControlPlaneError)
    async def control_plane_error(request: Request, exc: ControlPlaneError):
        return _error(
            str(exc.code), str(exc), exc.status_code, request.state.request_id
        )

    @app.exception_handler(InvalidStateTransition)
    async def invalid_transition(request: Request, exc: InvalidStateTransition):
        return _error(
            "INVALID_STATE_TRANSITION", str(exc), 409, request.state.request_id
        )

    @app.exception_handler(ProviderError)
    async def provider_error(request: Request, exc: ProviderError):
        return _error(
            str(exc.code),
            str(exc),
            503 if exc.retryable else 409,
            request.state.request_id,
        )

    def require_admin(request: Request, *, csrf: bool = False):
        token = request.cookies.get(settings.session_cookie_name)
        authenticated = auth.authenticate(token)
        if authenticated is None:
            raise HTTPException(
                status_code=401,
                detail={"code": "AUTH_REQUIRED", "message": "login required"},
            )
        user, admin_session = authenticated
        if csrf and not auth.csrf_valid(
            admin_session, request.headers.get("x-csrf-token")
        ):
            raise HTTPException(
                status_code=403,
                detail={"code": "CSRF_FAILED", "message": "invalid CSRF token"},
            )
        return user

    @app.get("/health/live")
    def live():
        return {"status": "ok"}

    @app.get("/health/ready")
    def ready():
        database = db.ping()
        revision = schema_revision(settings.database_url)
        ready_value = database and revision == EXPECTED_SCHEMA_REVISION
        return JSONResponse(
            status_code=200 if ready_value else 503,
            content={
                "status": "ready" if ready_value else "not_ready",
                "database": database,
                "schema_revision": revision,
                "expected_schema_revision": EXPECTED_SCHEMA_REVISION,
            },
        )

    @app.get("/health", include_in_schema=False)
    def health_compat():
        return {"status": "ok", "deprecated": True}

    @app.get("/api/v1/system/version")
    def version():
        return {
            "app_version": settings.app_version,
            "schema_version": schema_revision(settings.database_url),
            "agent_protocol_version": settings.agent_protocol_version,
        }

    @app.post("/api/v1/auth/login")
    def login(payload: LoginRequest, request: Request, response: Response):
        client = request.client.host if request.client else "unknown"
        try:
            token, csrf, max_age = auth.login(
                payload.username, payload.password, client
            )
        except InvalidCredentials:
            raise HTTPException(
                status_code=401,
                detail={
                    "code": "INVALID_CREDENTIALS",
                    "message": "invalid credentials",
                },
            )
        except RateLimited:
            raise HTTPException(
                status_code=429,
                detail={"code": "RATE_LIMITED", "message": "try again later"},
            )
        response.set_cookie(
            settings.session_cookie_name,
            token,
            max_age=max_age,
            httponly=True,
            secure=settings.is_production,
            samesite="strict",
            path="/",
        )
        return {"authenticated": True, "csrf_token": csrf, "expires_in": max_age}

    @app.get("/api/v1/auth/session")
    def session_info(request: Request):
        user = require_admin(request)
        token = request.cookies.get(settings.session_cookie_name)
        csrf = auth.rotate_csrf(token or "")
        return {"authenticated": True, "username": user.username, "csrf_token": csrf}

    @app.post("/api/v1/auth/logout")
    def logout(request: Request, response: Response):
        user = require_admin(request, csrf=True)
        auth.logout(request.cookies.get(settings.session_cookie_name))
        response.delete_cookie(settings.session_cookie_name, path="/")
        with db.session() as session:
            add_audit(
                session,
                actor=user.username,
                action="LOGOUT",
                entity_type="admin",
                entity_id=user.id,
                request_id=request.state.request_id,
            )
        return {"ok": True}

    @app.get("/api/v1/accounts")
    def list_accounts(request: Request):
        require_admin(request)
        return accounts.list()

    @app.get("/api/v1/accounts/{account_id}")
    def get_account(account_id: int, request: Request):
        require_admin(request)
        return accounts.get(account_id)

    @app.get("/api/v1/accounts/{account_id}/runtime-history")
    def runtime_history(account_id: int, request: Request):
        require_admin(request)
        return accounts.history(account_id)

    @app.get("/api/v1/accounts/{account_id}/runs")
    def account_runs(
        account_id: int, request: Request, limit: int = 100, offset: int = 0
    ):
        require_admin(request)
        return accounts.runs(
            account_id, limit=min(max(limit, 1), 100), offset=max(offset, 0)
        )

    @app.post("/api/v1/accounts", status_code=202)
    def create_account(payload: AccountCreate, request: Request):
        user = require_admin(request, csrf=True)
        try:
            account, job = accounts.create(
                payload,
                node_id=node_id,
                actor=user.username,
                request_id=request.state.request_id,
            )
        except DuplicateAccountError:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "DUPLICATE_ACCOUNT",
                    "message": "Steam username already exists",
                },
            )
        return {"account": account, "job": serialize_job(job)}

    def queue_action(
        account_id: int,
        kind: JobKind,
        request: Request,
        payload: dict | None = None,
    ):
        user = require_admin(request, csrf=True)
        try:
            job = accounts.command(
                account_id,
                kind,
                actor=user.username,
                request_id=request.state.request_id,
                idempotency_key=request.headers.get("idempotency-key"),
                payload=payload,
            )
        except AccountBusyError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "ACCOUNT_NOT_READY", "message": str(exc)},
            )
        content = {"job": serialize_job(job)}
        if kind in {JobKind.DAILY_GIFT_CLAIM, JobKind.LONG_SESSION}:
            content["gameplay_task_id"] = job.payload.get("gameplay_task_id")
        return JSONResponse(status_code=202, content=content)

    @app.post("/api/v1/accounts/{account_id}/start")
    def start_account(account_id: int, request: Request):
        return queue_action(
            account_id, JobKind.START_RUNTIME, request, {"reason": "manual"}
        )

    @app.post("/api/v1/accounts/{account_id}/gameplay/daily-gift", status_code=202)
    def request_daily_gift_claim(account_id: int, request: Request):
        return queue_action(account_id, JobKind.DAILY_GIFT_CLAIM, request)

    @app.post("/api/v1/accounts/{account_id}/gameplay/session", status_code=202)
    def request_long_session(account_id: int, request: Request, payload: dict):
        duration = payload.get("requested_duration")
        if (
            isinstance(duration, bool)
            or not isinstance(duration, int)
            or not 600 <= duration <= 5400
        ):
            raise HTTPException(
                status_code=422, detail="requested_duration must be 600..5400 seconds"
            )
        return queue_action(
            account_id, JobKind.LONG_SESSION, request, {"requested_duration": duration}
        )

    @app.get("/api/v1/accounts/{account_id}/gameplay/tasks")
    def gameplay_tasks(account_id: int, request: Request, limit: int = 50):
        require_admin(request)
        with db.session() as session:
            rows = list(
                session.scalars(
                    select(GameplayTask)
                    .where(GameplayTask.account_id == account_id)
                    .order_by(GameplayTask.id.desc())
                    .limit(min(max(limit, 1), 100))
                )
            )
            return [
                {
                    "id": task.id,
                    "account_id": task.account_id,
                    "runtime_id": task.runtime_id,
                    "worker_run_id": task.worker_run_id,
                    "kind": task.kind,
                    "status": task.status,
                    "started_at": task.started_at.isoformat(),
                    "updated_at": task.updated_at.isoformat(),
                    "completed_at": task.completed_at.isoformat()
                    if task.completed_at
                    else None,
                    "result": task.result_json,
                    "error_code": task.error_code,
                    "error_message": task.error_message,
                }
                for task in rows
            ]

    @app.post("/api/v1/accounts/{account_id}/setup")
    def setup_account(account_id: int, request: Request):
        return queue_action(
            account_id, JobKind.SETUP_RUNTIME, request, {"reason": "manual_login"}
        )

    @app.post("/api/v1/accounts/{account_id}/stop")
    def stop_account(account_id: int, request: Request):
        return queue_action(account_id, JobKind.STOP_RUNTIME, request)

    @app.post("/api/v1/accounts/{account_id}/restart")
    def restart_account(account_id: int, request: Request):
        return queue_action(
            account_id, JobKind.RESTART_RUNTIME, request, {"reason": "manual"}
        )

    @app.post("/api/v1/accounts/{account_id}/verify")
    def verify_account(account_id: int, request: Request):
        return queue_action(account_id, JobKind.VERIFY_RUNTIME, request)

    @app.post("/api/v1/accounts/{account_id}/rebuild")
    def rebuild_account(
        account_id: int,
        request: Request,
        payload: RebuildRuntimeRequest | None = None,
    ):
        data = payload.model_dump(exclude_none=True) if payload else {}
        return queue_action(account_id, JobKind.REBUILD_RUNTIME, request, data)

    @app.post("/api/v1/accounts/{account_id}/move")
    def move_account(account_id: int, payload: MoveRuntimeRequest, request: Request):
        return queue_action(
            account_id,
            JobKind.MOVE_RUNTIME,
            request,
            {"destination_node_id": payload.destination_node_id},
        )

    @app.post("/api/v1/accounts/{account_id}/enable")
    def enable_account(account_id: int, request: Request):
        user = require_admin(request, csrf=True)
        try:
            return accounts.set_enabled(
                account_id,
                True,
                actor=user.username,
                request_id=request.state.request_id,
            )
        except AccountBusyError as exc:
            raise HTTPException(
                status_code=409, detail={"code": "ACCOUNT_BUSY", "message": str(exc)}
            )

    @app.post("/api/v1/accounts/{account_id}/disable")
    def disable_account(account_id: int, request: Request):
        user = require_admin(request, csrf=True)
        try:
            return accounts.set_enabled(
                account_id,
                False,
                actor=user.username,
                request_id=request.state.request_id,
            )
        except AccountBusyError as exc:
            raise HTTPException(
                status_code=409, detail={"code": "ACCOUNT_BUSY", "message": str(exc)}
            )

    @app.get("/api/v1/jobs")
    def list_jobs(request: Request, limit: int = 100, offset: int = 0):
        require_admin(request)
        return jobs.list(min(max(limit, 1), 100), max(offset, 0))

    @app.post("/api/v1/accounts/{account_id}/schedule/pause")
    def pause_account_schedule(account_id: int, request: Request):
        require_admin(request, csrf=True)
        try:
            return account_scheduler.pause(account_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    @app.get("/api/v1/jobs/{job_id}")
    def get_job(job_id: int, request: Request):
        require_admin(request)
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "JOB_NOT_FOUND", "message": "job not found"},
            )
        return serialize_job(job)

    @app.get("/api/v1/nodes")
    def list_nodes(request: Request):
        require_admin(request)
        return accounts.nodes()

    @app.post("/api/v1/nodes/{selected_node_id}/{action}")
    def node_action(selected_node_id: int, action: str, request: Request):
        user = require_admin(request, csrf=True)
        try:
            return accounts.node_action(
                selected_node_id,
                action,
                actor=user.username,
                request_id=request.state.request_id,
            )
        except AccountBusyError as exc:
            raise HTTPException(
                status_code=409, detail={"code": "NODE_BUSY", "message": str(exc)}
            )

    @app.post("/api/v1/nodes/{selected_node_id}/token/rotate")
    def rotate_node_token(selected_node_id: int, request: Request):
        user = require_admin(request, csrf=True)
        token = accounts.rotate_node_token(
            selected_node_id,
            actor=user.username,
            request_id=request.state.request_id,
        )
        return {"node_id": selected_node_id, "token": token, "shown_once": True}

    @app.post("/api/v1/runtimes/{runtime_id}/token/rotate")
    def rotate_runtime_token(runtime_id: int, request: Request):
        user = require_admin(request, csrf=True)
        try:
            token = accounts.rotate_runtime_token(
                runtime_id,
                actor=user.username,
                request_id=request.state.request_id,
            )
        except AccountBusyError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "RUNTIME_BUSY", "message": str(exc)},
            )
        return {"runtime_id": runtime_id, "token": token, "shown_once": True}

    @app.post("/api/v1/runtimes/{runtime_id}/view-sessions", status_code=201)
    def create_view_session(
        runtime_id: int, request: Request, payload: ViewSessionCreate | None = None
    ):
        user = require_admin(request, csrf=True)
        payload = payload or ViewSessionCreate()
        try:
            return views.create(
                runtime_id,
                admin_user_id=user.id,
                actor=user.username,
                request_id=request.state.request_id,
                mode=payload.mode,
            )
        except ViewUnavailable as exc:
            raise HTTPException(
                status_code=503,
                detail={"code": str(exc.code), "message": str(exc)},
            )
        except ViewSessionNotFound:
            raise HTTPException(
                status_code=404,
                detail={"code": "RUNTIME_NOT_FOUND", "message": "runtime not found"},
            )

    @app.get("/api/v1/view-sessions/{view_session_id}")
    def view_session_status(view_session_id: int, request: Request):
        try:
            return views.status(view_session_id, _bearer(request))
        except ViewSessionNotFound:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "REMOTE_VIEW_SESSION_EXPIRED",
                    "message": "view session not found or expired",
                },
            )

    @app.delete("/api/v1/view-sessions/{view_session_id}")
    def close_view_session(view_session_id: int, request: Request):
        user = require_admin(request, csrf=True)
        try:
            views.close(
                view_session_id,
                actor=user.username,
                request_id=request.state.request_id,
            )
        except ViewSessionNotFound:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "VIEW_SESSION_NOT_FOUND",
                    "message": "view session not found",
                },
            )
        except ViewUnavailable as exc:
            raise HTTPException(
                status_code=503,
                detail={"code": str(exc.code), "message": str(exc)},
            )
        return {"ok": True}

    @app.post("/api/v1/view-sessions/{view_session_id}/access")
    def exchange_view_access(
        view_session_id: int,
        payload: ViewAccessRequest,
        request: Request,
        response: Response,
    ):
        require_admin(request, csrf=True)
        try:
            value = views.status(view_session_id, payload.token)
        except ViewSessionNotFound:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "REMOTE_VIEW_SESSION_EXPIRED",
                    "message": "view session not found or expired",
                },
            )
        response.set_cookie(
            "dst_view_access",
            payload.token,
            max_age=settings.view_session_ttl_seconds,
            httponly=True,
            secure=settings.is_production,
            samesite="strict",
            path=f"/api/v1/view-sessions/{view_session_id}/transport",
        )
        return value

    async def _proxy_view_transport(view_session_id: int, path: str, request: Request):
        token = request.cookies.get("dst_view_access")
        try:
            host, port, _remaining = await asyncio.to_thread(
                views.resolve_upstream, view_session_id, token
            )
        except (ViewSessionNotFound, ViewSessionNotReady, ViewUnavailable):
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "REMOTE_VIEW_SESSION_EXPIRED",
                    "message": "view transport unavailable",
                },
            )
        query = f"?{request.url.query}" if request.url.query else ""
        headers = {
            key: value
            for key, value in request.headers.items()
            if key.lower()
            in {"accept", "accept-language", "content-type", "user-agent"}
        }
        async with httpx.AsyncClient(follow_redirects=False) as client:
            upstream = await client.request(
                request.method,
                f"http://{host}:{port}/{path}{query}",
                headers=headers,
                content=await request.body(),
                timeout=15,
            )
        passthrough = {
            key: value
            for key, value in upstream.headers.items()
            if key.lower() in {"content-type", "cache-control", "etag", "last-modified"}
        }
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            headers=passthrough,
        )

    @app.get("/api/v1/view-sessions/{view_session_id}/transport/{path:path}")
    async def view_transport_get(view_session_id: int, path: str, request: Request):
        return await _proxy_view_transport(view_session_id, path, request)

    @app.post("/api/v1/view-sessions/{view_session_id}/transport/{path:path}")
    async def view_transport_post(view_session_id: int, path: str, request: Request):
        return await _proxy_view_transport(view_session_id, path, request)

    @app.websocket("/api/v1/view-sessions/{view_session_id}/transport/{path:path}")
    async def view_transport_websocket(
        websocket: WebSocket, view_session_id: int, path: str
    ):
        token = websocket.cookies.get("dst_view_access")
        try:
            host, port, remaining = await asyncio.to_thread(
                views.resolve_upstream, view_session_id, token
            )
        except (ViewSessionNotFound, ViewSessionNotReady, ViewUnavailable):
            await websocket.close(code=4404)
            return
        # Xpra's HTML5 client requires the `binary` WebSocket subprotocol.
        # Negotiate it on both legs of the relay; a bare upgrade is accepted
        # by ASGI but rejected by the browser and by xpra.
        if "binary" not in websocket.scope.get("subprotocols", []):
            await websocket.close(code=4406)
            return
        await websocket.accept(subprotocol="binary")
        try:
            from websockets.asyncio.client import connect

            query = f"?{websocket.url.query}" if websocket.url.query else ""
            async with connect(
                f"ws://{host}:{port}/{path}{query}",
                subprotocols=["binary"],
                max_size=2 * 1024 * 1024,
            ) as upstream:

                async def client_to_upstream():
                    while True:
                        message = await websocket.receive()
                        if message.get("bytes") is not None:
                            await upstream.send(message["bytes"])
                        elif message.get("text") is not None:
                            await upstream.send(message["text"])
                        else:
                            return

                async def upstream_to_client():
                    async for message in upstream:
                        if isinstance(message, bytes):
                            await websocket.send_bytes(message)
                        else:
                            await websocket.send_text(message)

                async with asyncio.timeout(remaining):
                    relays = {
                        asyncio.create_task(
                            client_to_upstream(), name="view-client-to-upstream"
                        ),
                        asyncio.create_task(
                            upstream_to_client(), name="view-upstream-to-client"
                        ),
                    }
                    try:
                        done, pending = await asyncio.wait(
                            relays, return_when=asyncio.FIRST_COMPLETED
                        )
                        for task in pending:
                            task.cancel()
                        await asyncio.gather(*pending, return_exceptions=True)
                        for task in done:
                            task.result()
                    finally:
                        for task in relays:
                            if not task.done():
                                task.cancel()
                        await asyncio.gather(*relays, return_exceptions=True)
        except Exception:
            logger.debug("view websocket relay ended", exc_info=True)
        finally:
            try:
                await websocket.close()
            except RuntimeError:
                pass

    @app.get("/api/v1/accounts/{account_id}/worker")
    def worker_status(account_id: int, request: Request):
        require_admin(request)
        try:
            return worker_controls.status(account_id)
        except WorkerControlError as exc:
            raise HTTPException(
                status_code=404,
                detail={"code": "RUNTIME_NOT_FOUND", "message": str(exc)},
            )

    def worker_command(
        account_id: int, command: str, request: Request, payload: dict | None = None
    ):
        user = require_admin(request, csrf=True)
        try:
            return worker_controls.command(
                account_id,
                command,
                actor=user.username,
                request_id=request.state.request_id,
                payload=payload,
            )
        except WorkerControlError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "WORKER_COMMAND_REJECTED", "message": str(exc)},
            )

    @app.post("/api/v1/accounts/{account_id}/worker/pause", status_code=202)
    def pause_worker(account_id: int, request: Request):
        return worker_command(account_id, "PAUSE", request)

    @app.post("/api/v1/accounts/{account_id}/worker/resume", status_code=202)
    def resume_worker(account_id: int, request: Request):
        return worker_command(account_id, "RESUME", request)

    @app.post("/api/v1/accounts/{account_id}/worker/stop", status_code=202)
    def stop_worker(account_id: int, request: Request):
        return worker_command(account_id, "STOP", request)

    @app.post("/api/v1/accounts/{account_id}/worker/mode", status_code=202)
    def set_worker_mode(account_id: int, payload: WorkerModeRequest, request: Request):
        values = {"mode": payload.mode}
        if payload.locomotion_profile:
            values.update(payload.model_dump(exclude={"mode"}))
        return worker_command(account_id, "SET_MODE", request, values)

    @app.get("/api/v1/accounts/{account_id}/worker/commands/{command_id}")
    def worker_command_status(account_id: int, command_id: int, request: Request):
        require_admin(request)
        try:
            return worker_controls.command_status(account_id, command_id)
        except WorkerControlError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/v1/events")
    def list_events(
        request: Request,
        limit: int = 100,
        offset: int = 0,
        level: str | None = None,
        entity: str | None = None,
    ):
        require_admin(request)
        return accounts.events(
            limit=min(max(limit, 1), 100),
            offset=max(offset, 0),
            level=level,
            entity=entity,
        )

    @app.get("/api/v1/audit-events")
    def list_audit_events(request: Request, limit: int = 100, offset: int = 0):
        require_admin(request)
        return accounts.audit_events(
            limit=min(max(limit, 1), 100),
            offset=max(offset, 0),
        )

    @app.post("/api/v1/node-agent/heartbeat")
    def node_heartbeat(payload: NodeHeartbeatRequest, request: Request):
        agents.node_heartbeat(payload, _bearer(request))
        return {"ok": True}

    @app.post("/api/v1/runtime-agent/heartbeat")
    def runtime_heartbeat(payload: RuntimeHeartbeatRequest, request: Request):
        return agents.runtime_heartbeat(payload, _bearer(request))

    @app.get("/metrics")
    def metrics(request: Request):
        if not settings.metrics_enabled:
            raise HTTPException(status_code=404, detail="metrics disabled")
        require_admin(request)
        with db.session() as session:
            running = (
                session.scalar(
                    select(func.count(RuntimeInstance.id)).where(
                        RuntimeInstance.active.is_(True),
                        RuntimeInstance.state.in_(
                            [RuntimeState.STARTING, RuntimeState.RUNNING]
                        ),
                    )
                )
                or 0
            )
            pending = (
                session.scalar(
                    select(func.count(Job.id)).where(
                        Job.status.in_(
                            [
                                JobStatus.PENDING,
                                JobStatus.LEASED,
                                JobStatus.RUNNING,
                                JobStatus.RETRY,
                            ]
                        )
                    )
                )
                or 0
            )
            failed = (
                session.scalar(
                    select(func.count(Job.id)).where(Job.status == JobStatus.FAILED)
                )
                or 0
            )
        body = (
            "# TYPE dst_active_runtimes gauge\n"
            f"dst_active_runtimes {running}\n"
            "# TYPE dst_jobs_pending gauge\n"
            f"dst_jobs_pending {pending}\n"
            "# TYPE dst_jobs_failed gauge\n"
            f"dst_jobs_failed {failed}\n"
        )
        return PlainTextResponse(body, media_type="text/plain; version=0.0.4")

    static_dir = Path(__file__).with_name("static")
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/")
    def index():
        return FileResponse(static_dir / "index.html")

    return app


app = create_app()
