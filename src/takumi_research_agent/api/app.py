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
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from takumi_research_agent.blogs.adapter import BlogApiAdapter, BlogCapabilities
from takumi_research_agent.blogs.contracts import BlogMapping, ContractError, OpenApiLoader, build_create_payload, infer_collection_path, infer_response_mapping, normalize_records
from takumi_research_agent.config import Settings
from takumi_research_agent.persistence.database import create_database
from takumi_research_agent.persistence.models import AdminSession, AdminUser, ArticlePairRecord, ArticleVersion, BlogProfile, GlobalSettings, ProviderProfile, Run
from takumi_research_agent.orchestration.scheduler import _snapshot
from takumi_research_agent.providers.anthropic_native import AnthropicProvider
from takumi_research_agent.providers.base import ModelMessage, ProviderConfig
from takumi_research_agent.providers.openai_compatible import OpenAICompatibleProvider
from takumi_research_agent.security import SecretCipher, hash_password, verify_password

from .schemas import AdapterReadTestRequest, BlogRequest, BootstrapRequest, GlobalControlRequest, LoginRequest, MappingPreviewRequest, ProviderRequest

SESSION_COOKIE = "takumi_session"
SESSION_TTL_HOURS = 12


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


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
                "created_at": item.created_at.isoformat(),
            }
            for item in db.scalars(select(Run).order_by(Run.created_at.desc()).limit(limit))
        ]

    @app.post("/api/v1/blogs/{blog_id}/runs", status_code=status.HTTP_202_ACCEPTED)
    def create_manual_run(
        blog_id: str,
        _: tuple[AdminUser, AdminSession] = Depends(require_csrf),
        db: Session = Depends(get_db),
    ):
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
        run = Run(
            blog_id=blog.id,
            editorial_date=editorial_date,
            quota_ordinal=latest_ordinal + 1,
            quota_slot_key=_hash(f"{blog.id}:manual:{secrets.token_urlsafe(24)}"),
            target_at=now + timedelta(hours=1),
            status="queued",
            stage="queued",
            execution_mode="dryrun",
            config_snapshot=_snapshot(blog, provider),
            pipeline_outcome="queued",
            deadline_result="pending",
            next_run_at=now,
        )
        db.add(run)
        db.commit()
        return {"run_id": run.id, "status": run.status, "execution_mode": run.execution_mode}

    @app.get("/api/v1/runs/{run_id}")
    def get_run_details(run_id: str, _: tuple[AdminUser, AdminSession] = Depends(get_auth), db: Session = Depends(get_db)):
        run = db.get(Run, run_id)
        if run is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
        pair = db.scalar(select(ArticlePairRecord).where(ArticlePairRecord.run_id == run_id))
        versions = []
        if pair is not None:
            for version in db.scalars(select(ArticleVersion).where(ArticleVersion.pair_id == pair.id).order_by(ArticleVersion.locale)):
                versions.append({
                    "locale": version.locale,
                    "title": version.title,
                    "summary": version.metadata_json.get("summary", ""),
                    "body": version.final_body,
                    "body_hash": version.body_hash,
                })
        return {
            "id": run.id,
            "blog_id": run.blog_id,
            "status": run.status,
            "stage": run.stage,
            "pipeline_outcome": run.pipeline_outcome,
            "editorial_decision": run.editorial_decision,
            "pair_completeness": run.pair_completeness,
            "deadline_result": run.deadline_result,
            "pair": None if pair is None else {
                "id": pair.id,
                "topic": pair.topic,
                "field": pair.field,
                "research_question": pair.research_question,
                "decision": pair.decision,
                "review": pair.review_result,
                "articles": versions,
            },
        }

    web_dist = Path(os.getenv("WEB_DIST_DIR", Path(__file__).resolve().parents[3] / "web" / "dist")).resolve()
    if web_dist.is_dir() and (web_dist / "index.html").is_file():
        app.mount("/", StaticFiles(directory=web_dist, html=True), name="dashboard")

    return app
