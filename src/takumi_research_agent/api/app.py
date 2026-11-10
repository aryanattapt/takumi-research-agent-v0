from __future__ import annotations

import hashlib
import ipaddress
import os
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from takumi_research_agent.blogs.adapter import BlogApiAdapter, BlogCapabilities, DeliveryOutcomeUnknown
from takumi_research_agent.blogs.contracts import BlogMapping, ContractError, OpenApiLoader, Operation, build_create_payload, get_path, infer_collection_path, infer_response_mapping, normalize_records
from takumi_research_agent.config import Settings
from takumi_research_agent.delivery.service import DeliveryBlocked, DeliveryService, delivery_reconciliation_ready, pending_delivery_attempt
from takumi_research_agent.persistence.database import create_database
from takumi_research_agent.persistence.models import AdminSession, AdminUser, ArticlePairRecord, ArticleVersion, BlogProfile, ContentLedger, DeliveryAttempt, DeliveryIntent, EvidenceSpanRecord, GlobalSettings, ProviderProfile, Run, RunEvent, SourceRecord as SourceRecordRow
from takumi_research_agent.orchestration.capacity import (
    DEFAULT_CONCURRENCY,
    DEFAULT_RUN_MINUTES,
    MAX_CONCURRENCY,
    MAX_RUN_MINUTES,
    MIN_RUN_MINUTES,
    apply_admission_control,
    capacity_report,
)
from takumi_research_agent.orchestration.scheduler import _snapshot
from takumi_research_agent.operations.backup import artifact_integrity_report, last_restore_report
from takumi_research_agent.providers.anthropic_native import AnthropicProvider
from takumi_research_agent.providers.base import ModelMessage, ProviderConfig
from takumi_research_agent.providers.openai_compatible import OpenAICompatibleProvider
from takumi_research_agent.security import SecretCipher, hash_password, verify_password

from .schemas import AdapterCreateOperationRequest, AdapterCreateTestRequest, AdapterReadTestRequest, BlogRequest, BootstrapRequest, DeliveryDispatchRequest, DeliveryReconcileRequest, GlobalControlRequest, LoginRequest, ManualRunRequest, MappingPreviewRequest, ProviderRequest

SESSION_COOKIE = "takumi_session"
SESSION_TTL_HOURS = 12


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _capacity_limits() -> tuple[int, int]:
    """Worker lane limits; the API and worker must read the same environment keys."""
    for value in (os.getenv("WORKER_CONCURRENCY"), os.getenv("CAPACITY_RUN_MINUTES")):
        if value is not None and not value.strip().isdigit():
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Capacity environment values must be integers")
    concurrency = min(max(int(os.getenv("WORKER_CONCURRENCY", str(DEFAULT_CONCURRENCY))), 1), MAX_CONCURRENCY)
    run_minutes = min(max(int(os.getenv("CAPACITY_RUN_MINUTES", str(DEFAULT_RUN_MINUTES))), MIN_RUN_MINUTES), MAX_RUN_MINUTES)
    return concurrency, run_minutes


def _normalize_expiry(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _validate_endpoint(url: str, allowed_private_hosts: tuple[str, ...]) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Endpoint must be an absolute HTTP(S) URL without embedded credentials")
    if parsed.query or parsed.fragment:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Endpoint must not contain query parameters or fragments")
    host = parsed.hostname.lower()
    if parsed.scheme == "https":
        if host in allowed_private_hosts or host in {"localhost", "127.0.0.1", "::1"}:
            return
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return
        if not address.is_global:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Private or reserved IP endpoints must be explicitly allowlisted")
        return
    if host in {"localhost", "127.0.0.1", "::1"} or host in allowed_private_hosts:
        return
    raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "HTTP is allowed only for localhost or an explicitly allowlisted private host")


def _provider_view(item: ProviderProfile) -> dict:
    return {
        "id": item.id,
        "name": item.name,
        "provider_type": item.provider_type,
        "base_url": item.base_url,
        "model": item.model,
        "secret_configured": bool(item.encrypted_api_key),
        "timeout_seconds": item.timeout_seconds,
        "max_output_tokens": item.max_output_tokens,
        "capability_result": item.capability_result,
        "revision": item.revision,
        "active": item.active,
    }


def _blog_view(item: BlogProfile) -> dict:
    return {
        "id": item.id,
        "name": item.name,
        "base_url": item.base_url,
        "api_spec_source": item.api_spec_source,
        "auth_header": item.auth_header,
        "auth_scheme": item.auth_scheme,
        "secret_configured": bool(item.encrypted_api_token),
        "provider_id": item.provider_id,
        "adapter_mapping": item.adapter_mapping,
        "capabilities": item.capabilities,
        "capability_result": item.capability_result,
        "topic_settings": item.topic_settings,
        "editorial_settings": item.editorial_settings,
        "schedule_enabled": item.schedule_enabled,
        "schedule_timezone": item.schedule_timezone,
        "publish_time": item.publish_time,
        "lead_minutes": item.lead_minutes,
        "revision": item.revision,
        "destination_epoch": item.destination_epoch,
        "active": item.active,
    }


def create_app(settings: Settings | None = None, *, engine=None, sessions: sessionmaker | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    if engine is None or sessions is None:
        engine, sessions = create_database(settings.database_url)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        engine.dispose()

    app = FastAPI(title="Takumi Research Agent", version="0.2.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine
    app.state.sessions = sessions

    def get_db():
        with sessions() as db:
            yield db

    def get_auth(request: Request, db: Session = Depends(get_db)) -> tuple[AdminUser, AdminSession]:
        raw = request.cookies.get(SESSION_COOKIE)
        if not raw:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Authentication required")
        row = db.get(AdminSession, _hash(raw))
        if row is None or _normalize_expiry(row.expires_at) <= datetime.now(timezone.utc):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired")
        user = db.get(AdminUser, row.user_id)
        if user is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session invalid")
        return user, row

    def require_csrf(
        auth: tuple[AdminUser, AdminSession] = Depends(get_auth),
        token: str | None = Header(default=None, alias="X-CSRF-Token"),
    ) -> tuple[AdminUser, AdminSession]:
        if not token or not secrets.compare_digest(auth[1].csrf_hash, _hash(token)):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "CSRF validation failed")
        return auth

    def cipher() -> SecretCipher:
        if not settings.master_key:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "TAKUMI_MASTER_KEY is not configured")
        try:
            return SecretCipher(settings.master_key)
        except ValueError as exc:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc

    @app.get("/api/health/live")
    def live():
        return {"status": "ok"}

    @app.get("/api/health/ready")
    def ready(db: Session = Depends(get_db)):
        try:
            db.execute(text("select 1"))
        except Exception as exc:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Database unavailable") from exc
        return {"status": "ready"}

    @app.get("/api/v1/setup/status")
    def setup_status(db: Session = Depends(get_db)):
        return {"needs_admin_setup": db.scalar(select(func.count()).select_from(AdminUser)) == 0, "master_key_configured": bool(settings.master_key)}

    @app.post("/api/v1/setup/admin", status_code=status.HTTP_201_CREATED)
    def bootstrap_admin(body: BootstrapRequest, db: Session = Depends(get_db)):
        if not settings.master_key:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Configure TAKUMI_MASTER_KEY before creating an administrator")
        if db.scalar(select(func.count()).select_from(AdminUser)):
            raise HTTPException(status.HTTP_409_CONFLICT, "Administrator setup is already complete")
        user = AdminUser(username=body.username, password_hash=hash_password(body.password))
        db.add(user)
        db.merge(GlobalSettings(id=1, recovery_mode=False, content_mutations_paused=True, revision=1))
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise HTTPException(status.HTTP_409_CONFLICT, "Administrator setup was completed by another request") from exc
        return {"created": True, "username": user.username}

    @app.post("/api/v1/auth/login")
    def login(body: LoginRequest, response: Response, db: Session = Depends(get_db)):
        user = db.scalar(select(AdminUser).where(AdminUser.username == body.username))
        if user is None or not verify_password(body.password, user.password_hash):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid username or password")
        raw_session = secrets.token_urlsafe(48)
        csrf = secrets.token_urlsafe(32)
        session = AdminSession(
            id_hash=_hash(raw_session),
            user_id=user.id,
            csrf_hash=_hash(csrf),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=SESSION_TTL_HOURS),
        )
        db.add(session)
        db.commit()
        response.set_cookie(
            SESSION_COOKIE,
            raw_session,
            max_age=SESSION_TTL_HOURS * 3600,
            httponly=True,
            secure=settings.session_cookie_secure,
            samesite="strict",
            path="/",
        )
        return {"username": user.username, "csrf_token": csrf, "expires_in_seconds": SESSION_TTL_HOURS * 3600}

    @app.get("/api/v1/auth/session")
    def current_session(auth: tuple[AdminUser, AdminSession] = Depends(get_auth), db: Session = Depends(get_db)):
        csrf = secrets.token_urlsafe(32)
        auth[1].csrf_hash = _hash(csrf)
        db.commit()
        return {"username": auth[0].username, "authenticated": True, "csrf_token": csrf}

    @app.post("/api/v1/auth/logout")
    def logout(response: Response, auth: tuple[AdminUser, AdminSession] = Depends(require_csrf), db: Session = Depends(get_db)):
        db.delete(auth[1])
        db.commit()
        response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, secure=settings.session_cookie_secure, samesite="strict")
        return {"logged_out": True}

    @app.get("/api/v1/providers")
    def list_providers(_: tuple[AdminUser, AdminSession] = Depends(get_auth), db: Session = Depends(get_db)):
        return [_provider_view(item) for item in db.scalars(select(ProviderProfile).order_by(ProviderProfile.created_at))]

    @app.post("/api/v1/providers", status_code=status.HTTP_201_CREATED)
    def create_provider(body: ProviderRequest, _: tuple[AdminUser, AdminSession] = Depends(require_csrf), db: Session = Depends(get_db)):
        _validate_endpoint(body.base_url, settings.allowed_private_hosts)
        if body.provider_type == "anthropic" and not body.api_key:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Anthropic requires an API key")
        encrypted = cipher().encrypt(body.api_key) if body.api_key else ""
        item = ProviderProfile(
            name=body.name,
            provider_type=body.provider_type,
            base_url=body.base_url.rstrip("/"),
            model=body.model,
            encrypted_api_key=encrypted,
            timeout_seconds=body.timeout_seconds,
            max_output_tokens=body.max_output_tokens,
        )
        db.add(item)
        db.commit()
        db.refresh(item)
        return _provider_view(item)

    @app.put("/api/v1/providers/{provider_id}")
    def update_provider(provider_id: str, body: ProviderRequest, _: tuple[AdminUser, AdminSession] = Depends(require_csrf), db: Session = Depends(get_db)):
        item = db.get(ProviderProfile, provider_id)
        if item is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Provider not found")
        if body.expected_revision is None or item.revision != body.expected_revision:
            raise HTTPException(status.HTTP_409_CONFLICT, "Provider configuration changed in another session; reload before saving")
        _validate_endpoint(body.base_url, settings.allowed_private_hosts)
        if body.provider_type == "anthropic" and (
            body.api_key == "" or (body.api_key is None and not item.encrypted_api_key)
        ):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Anthropic requires an API key")
        _ = cipher()
        item.name = body.name
        item.provider_type = body.provider_type
        item.base_url = body.base_url.rstrip("/")
        item.model = body.model
        item.timeout_seconds = body.timeout_seconds
        item.max_output_tokens = body.max_output_tokens
        if body.api_key is not None:
            item.encrypted_api_key = cipher().encrypt(body.api_key) if body.api_key else ""
        item.revision += 1
        item.capability_result = {}
        db.commit()
        db.refresh(item)
        return _provider_view(item)

    @app.delete("/api/v1/providers/{provider_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_provider(provider_id: str, expected_revision: int, _: tuple[AdminUser, AdminSession] = Depends(require_csrf), db: Session = Depends(get_db)):
        item = db.get(ProviderProfile, provider_id)
        if item is None:
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        if item.revision != expected_revision:
            raise HTTPException(status.HTTP_409_CONFLICT, "Provider changed; reload before deleting")
        linked = db.scalar(select(func.count()).select_from(BlogProfile).where(BlogProfile.provider_id == provider_id))
        if linked:
            raise HTTPException(status.HTTP_409_CONFLICT, "Provider is still assigned to a blog profile")
        db.delete(item)
        db.commit()
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post("/api/v1/providers/{provider_id}/probe")
    async def probe_provider(provider_id: str, _: tuple[AdminUser, AdminSession] = Depends(require_csrf)):
        # Release the database connection before a potentially slow external model request.
        with sessions() as db:
            item = db.get(ProviderProfile, provider_id)
            if item is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Provider not found")
            revision = item.revision
            try:
                api_key = SecretCipher(settings.master_key).decrypt(item.encrypted_api_key) if item.encrypted_api_key else ""
            except ValueError as exc:
                raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
            provider_type, base_url, model, timeout = item.provider_type, item.base_url, item.model, item.timeout_seconds
        provider_config = ProviderConfig(model=model, api_key=api_key, base_url=base_url, timeout_seconds=timeout)
        provider = AnthropicProvider(provider_config) if provider_type == "anthropic" else OpenAICompatibleProvider(provider_config)
        started = time.monotonic()
        result: dict = {}
        error: str | None = None
        try:
            reply = await provider.complete(
                [ModelMessage("system", "Reply with the exact token requested; do not call tools."), ModelMessage("user", "Reply exactly: TAKUMI_PROBE_OK")],
                [],
                max_output_tokens=64,
                temperature=0,
            )
            result = {
                "status": "reachable" if reply.stop_reason == "complete" and "TAKUMI_PROBE_OK" in reply.content else reply.stop_reason,
                "tested_at": datetime.now(timezone.utc).isoformat(),
                "latency_ms": round((time.monotonic() - started) * 1000),
                "usage_input_tokens": reply.usage_input_tokens,
                "usage_output_tokens": reply.usage_output_tokens,
                "reported_stop_reason": reply.stop_reason,
                "tools_tested": False,
                "json_schema_tested": False,
            }
            if result["status"] != "reachable":
                error = f"Provider probe ended with {reply.stop_reason}"
        except Exception as exc:
            result = {"status": "failed", "tested_at": datetime.now(timezone.utc).isoformat(), "latency_ms": round((time.monotonic() - started) * 1000), "error_category": type(exc).__name__}
            error = type(exc).__name__
        finally:
            await provider.close()
        with sessions.begin() as db:
            item = db.get(ProviderProfile, provider_id, with_for_update=True)
            if item is None or item.revision != revision:
                raise HTTPException(status.HTTP_409_CONFLICT, "Provider changed during the probe; reload before using its result")
            item.capability_result = result
        if error:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Provider request failed: {error}. The request may have used tokens.")
        return {**result, "note": "This checks one text response only; tool calling, JSON behavior, context capacity, and sustained reliability are not proven."}

    @app.get("/api/v1/blogs")
    def list_blogs(_: tuple[AdminUser, AdminSession] = Depends(get_auth), db: Session = Depends(get_db)):
        return [_blog_view(item) for item in db.scalars(select(BlogProfile).order_by(BlogProfile.created_at))]

    @app.post("/api/v1/blogs", status_code=status.HTTP_201_CREATED)
    def create_blog(body: BlogRequest, _: tuple[AdminUser, AdminSession] = Depends(require_csrf), db: Session = Depends(get_db)):
        _validate_endpoint(body.base_url, settings.allowed_private_hosts)
        guard = db.get(GlobalSettings, 1, with_for_update=True)
        if guard is None:
            guard = GlobalSettings(id=1, content_mutations_paused=True)
            db.add(guard)
            db.flush()
        count = db.scalar(select(func.count()).select_from(BlogProfile)) or 0
        if count >= 10:
            raise HTTPException(status.HTTP_409_CONFLICT, "A maximum of 10 blog profiles is supported")
        if body.provider_id and db.get(ProviderProfile, body.provider_id) is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Selected provider profile does not exist")
        if body.active:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "New blog profiles start inactive; validate the adapter before activation")
        encrypted_token = cipher().encrypt(body.api_token) if body.api_token else ""
        item = BlogProfile(
            name=body.name,
            base_url=body.base_url.rstrip("/"),
            api_spec_source=body.api_spec_source,
            auth_header=body.auth_header,
            auth_scheme=body.auth_scheme,
            encrypted_api_token=encrypted_token,
            provider_id=body.provider_id,
            adapter_mapping=body.adapter_mapping,
            capabilities=body.capabilities,
            topic_settings=body.topic_settings,
            editorial_settings=body.editorial_settings,
            schedule_enabled=body.schedule_enabled,
            schedule_timezone=body.schedule_timezone,
            publish_time=body.publish_time,
            lead_minutes=body.lead_minutes,
            active=False,
        )
        db.add(item)
        db.commit()
        db.refresh(item)
        return _blog_view(item)

    @app.put("/api/v1/blogs/{blog_id}")
    def update_blog(blog_id: str, body: BlogRequest, _: tuple[AdminUser, AdminSession] = Depends(require_csrf), db: Session = Depends(get_db)):
        item = db.get(BlogProfile, blog_id, with_for_update=True)
        if item is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Blog profile not found")
        if body.expected_revision is None or item.revision != body.expected_revision:
            raise HTTPException(status.HTTP_409_CONFLICT, "Blog configuration changed in another session; reload before saving")
        _validate_endpoint(body.base_url, settings.allowed_private_hosts)
        if body.provider_id and db.get(ProviderProfile, body.provider_id) is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Selected provider profile does not exist")
        destination_changed = item.base_url.rstrip("/") != body.base_url.rstrip("/")
        previous_adapter_config = (item.base_url, item.api_spec_source, item.auth_header, item.auth_scheme, item.adapter_mapping, item.capabilities)
        new_adapter_config = (body.base_url.rstrip("/"), body.api_spec_source, body.auth_header, body.auth_scheme, body.adapter_mapping, body.capabilities)
        adapter_changed = previous_adapter_config != new_adapter_config or body.api_token is not None
        if body.active and adapter_changed:
            raise HTTPException(status.HTTP_409_CONFLICT, "Save the adapter first, run its read-only validation, then activate it")
        if body.active and not item.capability_result.get("list_posts"):
            raise HTTPException(status.HTTP_409_CONFLICT, "Validate the history read capability before activating the profile")
        if body.active:
            provider = db.get(ProviderProfile, body.provider_id) if body.provider_id else None
            if provider is None or not provider.active or provider.capability_result.get("status") != "reachable":
                raise HTTPException(status.HTTP_409_CONFLICT, "Select and probe a model provider before activating the profile")
        item.name = body.name
        item.base_url = body.base_url.rstrip("/")
        item.api_spec_source = body.api_spec_source
        item.auth_header = body.auth_header
        item.auth_scheme = body.auth_scheme
        if body.api_token is not None:
            item.encrypted_api_token = cipher().encrypt(body.api_token) if body.api_token else ""
        item.provider_id = body.provider_id
        item.adapter_mapping = body.adapter_mapping
        item.capabilities = body.capabilities
        item.topic_settings = body.topic_settings
        item.editorial_settings = body.editorial_settings
        item.schedule_enabled = body.schedule_enabled
        item.schedule_timezone = body.schedule_timezone
        item.publish_time = body.publish_time
        item.lead_minutes = body.lead_minutes
        item.active = body.active and not adapter_changed
        item.revision += 1
        if destination_changed:
            item.destination_epoch += 1
            item.active = False
        if adapter_changed:
            item.validated_at = None
            item.validated_revision = None
            item.capability_result = {}
            item.active = False
        elif item.validated_at is not None:
            item.validated_revision = item.revision
        db.commit()
        db.refresh(item)
        return _blog_view(item)

    @app.delete("/api/v1/blogs/{blog_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_blog(blog_id: str, expected_revision: int, _: tuple[AdminUser, AdminSession] = Depends(require_csrf), db: Session = Depends(get_db)):
        item = db.get(BlogProfile, blog_id)
        if item is None:
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        if item.revision != expected_revision:
            raise HTTPException(status.HTTP_409_CONFLICT, "Blog configuration changed; reload before deleting")
        if item.active:
            raise HTTPException(status.HTTP_409_CONFLICT, "Pause the profile and reconcile pending operations before deletion")
        db.delete(item)
        db.commit()
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get("/api/v1/blogs/{blog_id}/adapter/contract")
    async def get_blog_contract(blog_id: str, _: tuple[AdminUser, AdminSession] = Depends(get_auth), db: Session = Depends(get_db)):
        item = db.get(BlogProfile, blog_id)
        if item is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Blog profile not found")
        if not item.api_spec_source:
            raise HTTPException(status.HTTP_409_CONFLICT, "Configure an OpenAPI URL or local contract file first")
        loader = OpenApiLoader(allowed_hosts=set(settings.allowed_private_hosts))
        try:
            contract = await loader.load(item.api_spec_source, base_url_override=item.base_url)
        except (ContractError, ValueError, OSError) as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Could not load contract: {type(exc).__name__}") from exc
        finally:
            await loader.close()
        return {"title": contract.title, "base_url": item.base_url, "operations": [operation.model_dump() for operation in contract.operations]}

    @app.post("/api/v1/blogs/{blog_id}/adapter/test-read")
    async def test_blog_read(blog_id: str, body: AdapterReadTestRequest, _: tuple[AdminUser, AdminSession] = Depends(require_csrf), db: Session = Depends(get_db)):
        item = db.get(BlogProfile, blog_id, with_for_update=True)
        if item is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Blog profile not found")
        if not item.adapter_mapping:
            raise HTTPException(status.HTTP_409_CONFLICT, "Configure and preview a valid response mapping first")
        if not item.api_spec_source:
            raise HTTPException(status.HTTP_409_CONFLICT, "Configure an OpenAPI contract before testing the API")
        loader = OpenApiLoader(allowed_hosts=set(settings.allowed_private_hosts))
        try:
            contract = await loader.load(item.api_spec_source, base_url_override=item.base_url)
        except Exception as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"OpenAPI contract load failed: {type(exc).__name__}: {str(exc)[:300]}") from exc
        finally:
            await loader.close()
        operation = next((op for op in contract.operations if op.operation_id == body.list_operation_id), None)
        if operation is None or operation.method != "get":
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Selected operation is missing or is not a GET")
        if not settings.master_key:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Credential master key is not configured")
        try:
            mapping = BlogMapping.model_validate(item.adapter_mapping)
            token = SecretCipher(settings.master_key).decrypt(item.encrypted_api_token) if item.encrypted_api_token else ""
        except (ValueError, ContractError) as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
        host = urlsplit(item.base_url).hostname or ""
        adapter = BlogApiAdapter(
            base_url=item.base_url,
            token=token,
            mapping=mapping,
            list_operation=operation,
            auth_header=item.auth_header,
            auth_scheme=item.auth_scheme,
            capabilities=BlogCapabilities(list_posts=True),
            allow_insecure_private=host.lower() in settings.allowed_private_hosts,
        )
        try:
            posts = await adapter.list_posts()
        except Exception as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Read capability failed: {type(exc).__name__}: {str(exc)[:300]}") from exc
        finally:
            await adapter.close()
        item.capabilities = {**item.capabilities, "list_operation": operation.model_dump()}
        item.capability_result = {"list_posts": True, "history_complete": True, "verified_at": datetime.now(timezone.utc).isoformat(), "records_read": len(posts)}
        item.validated_at = datetime.now(timezone.utc)
        item.revision += 1
        item.validated_revision = item.revision
        item.active = False
        db.commit()
        return {"validated": True, "records_read": len(posts), "sample": posts[:3], "note": "Read-only capability passed. Create, draft, publish, lookup, and remote schedule capabilities remain unverified."}

    @app.post("/api/v1/blogs/{blog_id}/adapter/create-operation")
    async def set_create_operation(
        blog_id: str,
        body: AdapterCreateOperationRequest,
        _: tuple[AdminUser, AdminSession] = Depends(require_csrf),
        db: Session = Depends(get_db),
    ):
        item = db.get(BlogProfile, blog_id, with_for_update=True)
        if item is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Blog profile not found")
        if not item.api_spec_source:
            raise HTTPException(status.HTTP_409_CONFLICT, "Configure an OpenAPI contract before selecting a create operation")
        loader = OpenApiLoader(allowed_hosts=set(settings.allowed_private_hosts))
        try:
            contract = await loader.load(item.api_spec_source, base_url_override=item.base_url)
        except Exception as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"OpenAPI contract load failed: {type(exc).__name__}: {str(exc)[:300]}") from exc
        finally:
            await loader.close()
        operation = next((op for op in contract.operations if op.operation_id == body.create_operation_id), None)
        if operation is None or operation.method not in {"post", "put"}:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Selected operation is missing or is not a POST/PUT")
        previous_operation = (item.capabilities or {}).get("create_operation")
        if previous_operation == operation.model_dump():
            return {"create_operation": operation.model_dump(), "revision": item.revision, "note": "Create operation unchanged."}
        item.capabilities = {**item.capabilities, "create_operation": operation.model_dump()}
        item.revision += 1
        item.capability_result = {
            key: value
            for key, value in item.capability_result.items()
            if key not in {"create_draft", "sandbox_create", "create_tested_at", "create_http_status", "create_remote_id", "create_error", "create_operation_id"}
        }
        item.capability_result = {**item.capability_result, "create_operation_id": operation.operation_id}
        if item.validated_at is not None:
            item.validated_revision = item.revision
        db.commit()
        db.refresh(item)
        return {"create_operation": operation.model_dump(), "revision": item.revision, "note": "Create capability is still unproven until a sandbox create succeeds."}

    @app.post("/api/v1/blogs/{blog_id}/adapter/test-create")
    async def test_blog_create(
        blog_id: str,
        body: AdapterCreateTestRequest,
        _: tuple[AdminUser, AdminSession] = Depends(require_csrf),
        db: Session = Depends(get_db),
    ):
        """Prove draft-create capability with one explicit, operator-acknowledged sandbox write."""
        if not body.acknowledge_remote_write:
            raise HTTPException(status.HTTP_409_CONFLICT, "Explicit acknowledgment is required before a remote write")
        item = db.get(BlogProfile, blog_id)
        if item is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Blog profile not found")
        control = db.get(GlobalSettings, 1)
        if control is None or control.recovery_mode:
            raise HTTPException(status.HTTP_409_CONFLICT, "Recovery mode blocks capability tests")
        if control.content_mutations_paused:
            raise HTTPException(status.HTTP_409_CONFLICT, "Open the global content pause only for an explicit sandbox test")
        if not item.capability_result.get("history_complete"):
            raise HTTPException(status.HTTP_409_CONFLICT, "Validate complete history before testing a create")
        if not settings.master_key:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Credential master key is not configured")
        try:
            mapping = BlogMapping.model_validate(item.adapter_mapping)
            raw_create = (item.capabilities or {}).get("create_operation")
            raw_list = (item.capabilities or {}).get("list_operation")
            if not isinstance(raw_create, dict) or not isinstance(raw_list, dict):
                raise ValueError("Create and list operations must be selected before testing create")
            create_operation = Operation.model_validate(raw_create)
            list_operation = Operation.model_validate(raw_list)
            token = SecretCipher(settings.master_key).decrypt(item.encrypted_api_token) if item.encrypted_api_token else ""
        except (ValueError, ContractError) as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)[:300]) from exc
        host = (urlsplit(item.base_url).hostname or "").lower()
        adapter = BlogApiAdapter(
            base_url=item.base_url,
            token=token,
            mapping=mapping,
            list_operation=list_operation,
            create_operation=create_operation,
            auth_header=item.auth_header,
            auth_scheme=item.auth_scheme,
            capabilities=BlogCapabilities(list_posts=True, create_draft=True),
            allow_insecure_private=host in settings.allowed_private_hosts,
        )
        marker = f"Takumi capability check {datetime.now(timezone.utc).isoformat(timespec='seconds')}"
        remote_id: str | None = None
        try:
            canonical = {"title": marker[:180], "content": "Sandbox capability check. This draft is safe to delete.", "summary": "Sandbox capability check.", "locale": "en", "language": "en", "status": "draft"}
            payload = build_create_payload(canonical, mapping)
            result = await adapter.send_payload(payload, intent_key=f"takumi-capability-{secrets.token_urlsafe(18)}", publish=False)
        except DeliveryOutcomeUnknown as exc:
            item.capability_result = {**item.capability_result, "create_draft": False, "sandbox_create": False, "create_tested_at": datetime.now(timezone.utc).isoformat(), "create_error": str(exc)[:200]}
            db.commit()
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Create outcome is unknown; reconcile on the blog before retrying") from exc
        except Exception as exc:
            item.capability_result = {**item.capability_result, "create_draft": False, "sandbox_create": False, "create_tested_at": datetime.now(timezone.utc).isoformat(), "create_error": type(exc).__name__}
            db.commit()
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Create capability test failed: {type(exc).__name__}") from exc
        finally:
            await adapter.close()
        id_path = mapping.response_fields.get("id")
        if id_path:
            value = get_path(result.get("body"), id_path)
            remote_id = str(value)[:300] if value not in (None, "") else None
        item.capability_result = {
            **item.capability_result,
            "create_draft": True,
            "sandbox_create": True,
            "create_tested_at": datetime.now(timezone.utc).isoformat(),
            "create_http_status": result.get("http_status"),
            "create_remote_id": remote_id,
        }
        db.commit()
        return {
            "create_draft": True,
            "http_status": result.get("http_status"),
            "remote_id": remote_id,
            "request_payload": payload,
            "note": "One remote draft was created. This proves a single create only; publish, idempotency, lookup, and sustained reliability remain unproven.",
        }

    @app.post("/api/v1/blogs/{blog_id}/adapter/preview")
    def preview_mapping(blog_id: str, body: MappingPreviewRequest, _: tuple[AdminUser, AdminSession] = Depends(require_csrf), db: Session = Depends(get_db)):
        item = db.get(BlogProfile, blog_id)
        if item is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Blog profile not found")
        if not item.adapter_mapping:
            return {"suggested_list_path": infer_collection_path(body.sample_response), "suggested_response_fields": infer_response_mapping(body.sample_response), "normalized_sample": None, "payload_preview": None, "safe_to_mutate": False}
        try:
            mapping = BlogMapping.model_validate(item.adapter_mapping)
            records = normalize_records(body.sample_response, mapping)
            payload = build_create_payload(body.canonical_post, mapping) if body.canonical_post else None
        except (ContractError, ValueError) as exc:
            return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, content={"safe_to_mutate": False, "error": str(exc)})
        return {"suggested_list_path": infer_collection_path(body.sample_response), "suggested_response_fields": infer_response_mapping(body.sample_response), "normalized_sample": records[:5], "payload_preview": payload, "safe_to_mutate": False, "note": "Preview only; capability test and explicit activation are still required."}

    @app.get("/api/v1/control")
    def get_control(_: tuple[AdminUser, AdminSession] = Depends(get_auth), db: Session = Depends(get_db)):
        item = db.get(GlobalSettings, 1)
        return {"content_mutations_paused": True if item is None else item.content_mutations_paused, "recovery_mode": True if item is None else item.recovery_mode, "revision": 1 if item is None else item.revision}

    @app.put("/api/v1/control")
    def set_control(body: GlobalControlRequest, auth: tuple[AdminUser, AdminSession] = Depends(require_csrf), db: Session = Depends(get_db)):
        item = db.get(GlobalSettings, 1, with_for_update=True)
        if item is None:
            item = GlobalSettings(id=1, content_mutations_paused=True, recovery_mode=True)
            db.add(item)
            db.flush()
        if item.revision != body.expected_revision:
            raise HTTPException(status.HTTP_409_CONFLICT, "Global control changed in another session")
        if item.recovery_mode and not body.content_mutations_paused:
            raise HTTPException(status.HTTP_409_CONFLICT, "Recovery mode must be explicitly cleared by the recovery procedure")
        item.content_mutations_paused = body.content_mutations_paused
        item.revision += 1
        db.commit()
        return {"content_mutations_paused": item.content_mutations_paused, "recovery_mode": item.recovery_mode, "revision": item.revision}

    @app.get("/api/v1/runs")
    def list_runs(limit: int = 50, _: tuple[AdminUser, AdminSession] = Depends(get_auth), db: Session = Depends(get_db)):
        limit = min(max(limit, 1), 200)
        return [
            {
                "id": item.id,
                "blog_id": item.blog_id,
                "editorial_date": item.editorial_date.isoformat(),
                "target_at": item.target_at.isoformat(),
                "status": item.status,
                "stage": item.stage,
                "editorial_decision": item.editorial_decision,
                "pair_completeness": item.pair_completeness,
                "deadline_result": item.deadline_result,
                "execution_mode": item.execution_mode,
                "capacity_state": item.capacity_state,
                "start_by_at": None if item.start_by_at is None else item.start_by_at.isoformat(),
                "schedule_version": item.schedule_version,
                "created_at": item.created_at.isoformat(),
            }
            for item in db.scalars(select(Run).order_by(Run.created_at.desc()).limit(limit))
        ]

    @app.get("/api/v1/recovery")
    def get_recovery(
        verify_artifacts: bool = False,
        _: tuple[AdminUser, AdminSession] = Depends(get_auth),
        db: Session = Depends(get_db),
    ):
        control = db.get(GlobalSettings, 1)
        recovery_mode = True if control is None else control.recovery_mode
        # Hashing every artifact is I/O heavy, so the dashboard polls the cheap view and asks
        # for a full check on demand (or always during recovery mode).
        if recovery_mode or verify_artifacts:
            artifacts = artifact_integrity_report(db, artifacts_root=settings.data_dir / "artifacts")
        else:
            total = db.scalar(select(func.count()).select_from(EvidenceSpanRecord)) or 0
            artifacts = {"checked": False, "total": total, "verified": None, "missing": [], "corrupt": [], "truncated": False}
        return {
            "recovery_mode": recovery_mode,
            "content_mutations_paused": True if control is None else control.content_mutations_paused,
            "artifacts": artifacts,
            "last_restore": last_restore_report(db),
        }

    @app.get("/api/v1/capacity")
    def get_capacity(
        concurrency: int | None = None,
        run_minutes: int | None = None,
        _: tuple[AdminUser, AdminSession] = Depends(get_auth),
        db: Session = Depends(get_db),
    ):
        default_concurrency, default_run_minutes = _capacity_limits()
        return capacity_report(
            db,
            concurrency=default_concurrency if concurrency is None else concurrency,
            run_minutes=default_run_minutes if run_minutes is None else run_minutes,
        )

    @app.post("/api/v1/blogs/{blog_id}/runs", status_code=status.HTTP_202_ACCEPTED)
    def create_manual_run(
        blog_id: str,
        body: ManualRunRequest | None = None,
        _: tuple[AdminUser, AdminSession] = Depends(require_csrf),
        db: Session = Depends(get_db),
    ):
        execution_mode = body.execution_mode if body is not None else "dryrun"
        blog = db.get(BlogProfile, blog_id)
        if blog is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Blog profile not found")
        provider = db.get(ProviderProfile, blog.provider_id) if blog.provider_id else None
        if not blog.active or blog.validated_revision != blog.revision or not blog.capability_result.get("history_complete"):
            raise HTTPException(status.HTTP_409_CONFLICT, "Activate the blog profile after complete history validation")
        if provider is None or not provider.active or provider.capability_result.get("status") != "reachable":
            raise HTTPException(status.HTTP_409_CONFLICT, "Select and probe a model provider before starting a run")
        control = db.get(GlobalSettings, 1)
        if control is None or control.recovery_mode:
            raise HTTPException(status.HTTP_409_CONFLICT, "System recovery mode blocks new runs")
        try:
            local_now = datetime.now(ZoneInfo(blog.schedule_timezone))
        except Exception as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Blog timezone is invalid") from exc
        editorial_date = local_now.date()
        latest_ordinal = db.scalar(
            select(func.max(Run.quota_ordinal)).where(Run.blog_id == blog.id, Run.editorial_date == editorial_date)
        ) or 0
        now = datetime.now(timezone.utc)
        concurrency, run_minutes = _capacity_limits()
        target_at = now + timedelta(hours=1)
        run = Run(
            blog_id=blog.id,
            editorial_date=editorial_date,
            quota_ordinal=latest_ordinal + 1,
            quota_slot_key=_hash(f"{blog.id}:manual:{secrets.token_urlsafe(24)}"),
            target_at=target_at,
            status="queued",
            stage="queued",
            execution_mode=execution_mode,
            config_snapshot=_snapshot(blog, provider),
            pipeline_outcome="queued",
            deadline_result="pending",
            schedule_version=blog.revision,
            start_by_at=target_at - timedelta(minutes=run_minutes),
            next_run_at=now,
        )
        db.add(run)
        db.flush()
        # A manual run still competes for the same lanes: plan it with the queue, not ahead of it.
        apply_admission_control(db, now=now, concurrency=concurrency, run_minutes=run_minutes)
        db.commit()
        return {"run_id": run.id, "status": run.status, "execution_mode": run.execution_mode, "capacity_state": run.capacity_state}

    @app.post("/api/v1/runs/{run_id}/delivery")
    async def dispatch_delivery(
        run_id: str,
        body: DeliveryDispatchRequest,
        _: tuple[AdminUser, AdminSession] = Depends(require_csrf),
        db: Session = Depends(get_db),
    ):
        run = db.get(Run, run_id)
        if run is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
        if run.execution_mode != body.execution_mode:
            raise HTTPException(status.HTTP_409_CONFLICT, "Run execution mode does not match the requested delivery mode")
        blog = db.get(BlogProfile, run.blog_id)
        if blog is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "Blog profile not found")
        if not settings.master_key:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Credential master key is not configured")
        try:
            mapping = BlogMapping.model_validate(blog.adapter_mapping)
            raw_create = (blog.capabilities or {}).get("create_operation")
            raw_list = (blog.capabilities or {}).get("list_operation")
            if not isinstance(raw_create, dict) or not isinstance(raw_list, dict):
                raise ValueError("Create and list operations must be selected before delivery")
            create_operation = Operation.model_validate(raw_create)
            list_operation = Operation.model_validate(raw_list)
            token = SecretCipher(settings.master_key).decrypt(blog.encrypted_api_token) if blog.encrypted_api_token else ""
        except (ValueError, ContractError) as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)[:300]) from exc
        host = (urlsplit(blog.base_url).hostname or "").lower()
        adapter = BlogApiAdapter(
            base_url=blog.base_url,
            token=token,
            mapping=mapping,
            list_operation=list_operation,
            create_operation=create_operation,
            auth_header=blog.auth_header,
            auth_scheme=blog.auth_scheme,
            capabilities=BlogCapabilities(
                list_posts=True,
                create_draft=bool(blog.capability_result.get("create_draft")),
                publish=bool(blog.capability_result.get("publish")),
                idempotency_key=bool(blog.capability_result.get("idempotency_key")),
            ),
            allow_insecure_private=host in settings.allowed_private_hosts,
        )
        try:
            summary = await DeliveryService().dispatch(db, run_id=run_id, adapter=adapter)
        except DeliveryBlocked as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, f"{exc.code}: {exc.detail}") from exc
        finally:
            await adapter.close()
        return {
            "run_id": run_id,
            "execution_mode": run.execution_mode,
            **summary,
            "note": "A successful request means the blog accepted the payload; remote status is not verified until a read capability check confirms it.",
        }

    @app.post("/api/v1/runs/{run_id}/delivery/{intent_id}/reconcile")
    def reconcile_delivery(
        run_id: str,
        intent_id: str,
        body: DeliveryReconcileRequest,
        _: tuple[AdminUser, AdminSession] = Depends(require_csrf),
        db: Session = Depends(get_db),
    ):
        """Resolve an operation whose remote outcome is unknown; never creates a new intent."""
        intent = db.get(DeliveryIntent, intent_id)
        if intent is None or intent.run_id != run_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Delivery intent not found")
        if intent.state not in {"unknown", "dispatching"}:
            raise HTTPException(status.HTTP_409_CONFLICT, "Only unknown or stale dispatching intents can be reconciled")
        now = datetime.now(timezone.utc)
        if intent.state == "dispatching" and not delivery_reconciliation_ready(db, intent, now=now):
            raise HTTPException(status.HTTP_409_CONFLICT, "Delivery request may still be in flight; wait before reconciling")
        next_state = "succeeded" if body.outcome == "succeeded" else "failed"
        claimed = db.execute(
            update(DeliveryIntent)
            .where(
                DeliveryIntent.id == intent.id,
                DeliveryIntent.run_id == run_id,
                DeliveryIntent.state == intent.state,
            )
            .values(state=next_state)
            .execution_options(synchronize_session=False)
        ).rowcount
        if claimed != 1:
            raise HTTPException(status.HTTP_409_CONFLICT, "Delivery intent was already reconciled")
        intent.state = next_state
        attempt = pending_delivery_attempt(db, intent.id)
        if body.remote_id:
            intent.remote_id = body.remote_id[:300]
        intent.response_summary = {
            "reconciled": True,
            "outcome": body.outcome,
            "remote_id": intent.remote_id,
            "note": (body.note or "")[:500],
        }
        if attempt is not None:
            attempt.finished_at = now
            attempt.outcome = body.outcome
            attempt.response_summary = {
                **(attempt.response_summary or {}),
                "reconciled": True,
                "outcome": body.outcome,
                "remote_id": intent.remote_id,
                "note": (body.note or "")[:300],
            }
        ledger = db.scalar(
            select(ContentLedger).where(
                ContentLedger.blog_id == intent.blog_id,
                ContentLedger.pair_id == intent.pair_id,
                ContentLedger.unit_key == intent.unit_key,
            )
        )
        if ledger is not None:
            if body.outcome == "succeeded":
                ledger.local_state = "delivered"
                ledger.remote_status = "accepted_unverified"
                ledger.remote_id = intent.remote_id
                ledger.verified_at = now
            else:
                ledger.local_state = "delivery_failed"
        db.add(
            RunEvent(
                run_id=run_id,
                event_type="delivery_reconciled",
                details={"intent_id": intent.id, "unit_key": intent.unit_key, "outcome": body.outcome, "remote_id": intent.remote_id, "note": (body.note or "")[:300]},
            )
        )
        db.commit()
        return {"intent_id": intent.id, "state": intent.state, "remote_id": intent.remote_id, "outcome": body.outcome}

    @app.get("/api/v1/runs/{run_id}")
    def get_run_details(run_id: str, _: tuple[AdminUser, AdminSession] = Depends(get_auth), db: Session = Depends(get_db)):
        run = db.get(Run, run_id)
        if run is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
        pair = db.scalar(select(ArticlePairRecord).where(ArticlePairRecord.run_id == run_id))
        coverage_event = db.scalar(
            select(RunEvent)
            .where(RunEvent.run_id == run_id, RunEvent.event_type == "discovery_coverage")
            .order_by(RunEvent.created_at.desc())
            .limit(1)
        )
        review_data = pair.review_result or {} if pair is not None else {}
        source_refs = review_data.get("sources") or []
        evidence_refs = review_data.get("evidence") or []
        source_ids = [item.get("source_id") for item in source_refs if item.get("source_id")]
        evidence_ids = [item.get("evidence_id") for item in evidence_refs if item.get("evidence_id")]
        source_rows = {
            item.id: item
            for item in db.scalars(select(SourceRecordRow).where(SourceRecordRow.id.in_(source_ids))).all()
        } if source_ids else {}
        evidence_rows = {
            item.id: item
            for item in db.scalars(select(EvidenceSpanRecord).where(EvidenceSpanRecord.id.in_(evidence_ids))).all()
        } if evidence_ids else {}
        sources = []
        for reference in source_refs:
            source = source_rows.get(reference.get("source_id"))
            metadata = source.metadata_json if source is not None else {}
            sources.append({
                "source_id": reference.get("source_id"),
                "title": source.title if source is not None else "",
                "url": source.url if source is not None else "",
                "source_type": source.source_type if source is not None else "unknown",
                "content_hash": reference.get("content_hash"),
                "read_status": reference.get("read_status", metadata.get("read_status", "unknown")),
                "peer_reviewed": metadata.get("peer_reviewed"),
                "peer_review_basis": metadata.get("peer_review_basis", "unknown"),
                "retracted": reference.get("retracted", metadata.get("retracted", False)),
                "available": source is not None,
            })
        evidence = []
        for reference in evidence_refs:
            item = evidence_rows.get(reference.get("evidence_id"))
            evidence.append({
                "evidence_id": reference.get("evidence_id"),
                "source_id": reference.get("source_id"),
                "content_hash": reference.get("content_hash"),
                "quote": item.quote if item is not None else None,
                "locator": item.locator if item is not None else None,
                "available": item is not None,
            })
        versions = []
        if pair is not None:
            for version in db.scalars(select(ArticleVersion).where(ArticleVersion.pair_id == pair.id).order_by(ArticleVersion.locale)):
                versions.append({
                    "locale": version.locale,
                    "title": version.title,
                    "summary": version.metadata_json.get("summary", ""),
                    "body": version.final_body,
                    "body_hash": version.body_hash,
                    "cited_evidence_ids": version.metadata_json.get("cited_evidence_ids", []),
                })
        deliveries = []
        for intent in db.scalars(select(DeliveryIntent).where(DeliveryIntent.run_id == run_id).order_by(DeliveryIntent.unit_key)):
            ledger = db.scalar(
                select(ContentLedger).where(
                    ContentLedger.blog_id == intent.blog_id,
                    ContentLedger.pair_id == intent.pair_id,
                    ContentLedger.unit_key == intent.unit_key,
                )
            )
            deliveries.append({
                "id": intent.id,
                "unit_key": intent.unit_key,
                "action": intent.action,
                "state": intent.state,
                "remote_id": intent.remote_id,
                "remote_status": ledger.remote_status if ledger else None,
                "local_state": ledger.local_state if ledger else None,
                "operation_id": intent.operation_id,
                "attempts": db.scalar(select(func.count()).select_from(DeliveryAttempt).where(DeliveryAttempt.intent_id == intent.id)) or 0,
                "reconciliation_ready": delivery_reconciliation_ready(db, intent),
                "response": intent.response_summary,
            })
        return {
            "id": run.id,
            "blog_id": run.blog_id,
            "status": run.status,
            "stage": run.stage,
            "execution_mode": run.execution_mode,
            "pipeline_outcome": run.pipeline_outcome,
            "editorial_decision": run.editorial_decision,
            "pair_completeness": run.pair_completeness,
            "deadline_result": run.deadline_result,
            "capacity_state": run.capacity_state,
            "start_by_at": None if run.start_by_at is None else run.start_by_at.isoformat(),
            "schedule_version": run.schedule_version,
            "discovery_coverage": None if coverage_event is None else coverage_event.details.get("coverage"),
            "deliveries": deliveries,
            "pair": None if pair is None else {
                "id": pair.id,
                "topic": pair.topic,
                "field": pair.field,
                "research_question": pair.research_question,
                "decision": pair.decision,
                "review": {
                    key: value
                    for key, value in review_data.items()
                    if key not in {"agent", "sources", "evidence", "claims"}
                },
                "agent": review_data.get("agent", {}),
                "sources": sources,
                "evidence": evidence,
                "claims": review_data.get("claims", []),
                "articles": versions,
            },
        }

    web_dist = Path(os.getenv("WEB_DIST_DIR", Path(__file__).resolve().parents[3] / "web" / "dist")).resolve()
    if web_dist.is_dir() and (web_dist / "index.html").is_file():
        app.mount("/", StaticFiles(directory=web_dist, html=True), name="dashboard")

    return app
