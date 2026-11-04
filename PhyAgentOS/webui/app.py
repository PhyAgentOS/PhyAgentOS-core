"""FastAPI app factory for the single-page local settings UI."""

from __future__ import annotations

import copy
import ipaddress
import secrets
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from PhyAgentOS.config.loader import get_config_path
from PhyAgentOS.config.schema import Config
from PhyAgentOS.skill_runtime.registry import RegistryError
from PhyAgentOS.skill_runtime.service import SkillServicePaths
from PhyAgentOS.webui import settings as settings_service
from PhyAgentOS.webui.forge_runtime import forge_runtime_snapshot, registry_search
from PhyAgentOS.webui.skill_jobs import SkillJobBusyError, SkillJobError, SkillJobManager

STATIC_DIR = Path(__file__).with_name("static")
SAVE_MESSAGE = (
    "Saved. A running PhyAgentOS process keeps its loaded configuration; "
    "restart it for the new settings to take effect."
)

# The schema is immutable for a given release; render it once per process.
def _ui_schema() -> dict[str, Any]:
    schema = copy.deepcopy(Config.model_json_schema(by_alias=True))
    agent_defaults = schema.get("$defs", {}).get("AgentDefaults", {})
    properties = agent_defaults.get("properties", {})
    # memoryWindow is accepted only for old configs and is excluded from
    # model_dump; offering it in the form would make edits vanish on save.
    properties.pop("memoryWindow", None)
    required = agent_defaults.get("required")
    if isinstance(required, list) and "memoryWindow" in required:
        required.remove("memoryWindow")
    return schema


CONFIG_SCHEMA = _ui_schema()


class SettingsUpdate(BaseModel):
    """Full sanitized config plus the revision it was based on."""

    model_config = ConfigDict(populate_by_name=True)

    config: dict[str, Any]
    revision: str
    clear_secrets: list[str] = Field(default_factory=list, alias="clearSecrets")


class SkillActionRequest(BaseModel):
    """One Skill action requested from the settings page."""

    model_config = ConfigDict(populate_by_name=True)

    name: str
    version: str | None = None


def is_loopback_host(host: str) -> bool:
    """Return True for addresses that are only reachable from this machine."""
    if host in {"localhost", "localhost."}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _token_guard(token: str | None):
    def guard(request: Request) -> None:
        if token is None:
            return
        supplied = request.headers.get("x-paos-token", "")
        authorization = request.headers.get("authorization", "")
        if not supplied and authorization.lower().startswith("bearer "):
            supplied = authorization[7:].strip()
        if not supplied or not secrets.compare_digest(supplied, token):
            raise HTTPException(status_code=401, detail="Missing or invalid access token")

    return guard


def _safe_request_errors(exc: RequestValidationError) -> list[dict[str, Any]]:
    """Never echo the submitted body or its context back to the client."""
    return [
        {
            "loc": list(error.get("loc", ())),
            "msg": error.get("msg", "invalid value"),
            "type": error.get("type", "value_error"),
        }
        for error in exc.errors()
    ]


def _payload(config: Config, raw: bytes, *, restart_required: bool) -> dict[str, Any]:
    # GET and PUT are built from the same shape; the frontend relies on it.
    return {
        "config": settings_service.redact_config(config),
        "schema": CONFIG_SCHEMA,
        "revision": settings_service.revision_of(raw),
        "secret_fields": settings_service.secret_fields(config),
        "restart_required": restart_required,
    }


def create_app(
    config_path: Path | None = None,
    *,
    token: str | None = None,
    allow_skill_actions: bool = False,
) -> FastAPI:
    """Build the app. ``config_path`` is injected by tests and the CLI.

    ``token`` is a test seam only. The CLI serves loopback exclusively because
    the page has no token entry flow; there is no supported remote mode.
    ``allow_skill_actions`` gates the install/update/remove endpoints; the
    default keeps the settings server read-only regarding Skill bundles.
    """
    path = Path(config_path).expanduser() if config_path is not None else get_config_path()
    auth = _token_guard(token)
    data_dir = path.parent
    skill_job_manager = SkillJobManager(
        paths=SkillServicePaths(
            bundles=data_dir / "skills",
            state=data_dir / "run" / "skills",
            forge_runtime=data_dir / "forge_runtime",
            cache=data_dir / "cache",
        ),
        config_path=path,
    )
    app = FastAPI(
        title="PhyAgentOS Settings",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.exception_handler(RequestValidationError)
    async def _invalid_request(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": _safe_request_errors(exc)})

    @app.get("/api/health", dependencies=[Depends(auth)])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/settings", dependencies=[Depends(auth)])
    def get_settings() -> dict[str, Any]:
        try:
            config, raw = settings_service.snapshot(path)
        except settings_service.InvalidSettingsError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        return _payload(config, raw, restart_required=False)

    @app.get("/api/forge/runtime", dependencies=[Depends(auth)])
    def get_forge_runtime() -> dict[str, Any]:
        data = forge_runtime_snapshot(path.parent)
        data["actionsEnabled"] = allow_skill_actions
        return data

    @app.get("/api/forge/registry", dependencies=[Depends(auth)])
    def search_forge_registry(query: str = "") -> dict[str, Any]:
        try:
            return registry_search(path.parent, path, query.strip()[:200])
        except RegistryError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None

    @app.get("/api/forge/jobs/current", dependencies=[Depends(auth)])
    def get_current_job() -> dict[str, Any]:
        job = skill_job_manager.current()
        return {"job": job.snapshot() if job is not None else None}

    @app.get("/api/forge/jobs/{job_id}", dependencies=[Depends(auth)])
    def get_job(job_id: str) -> dict[str, Any]:
        job = skill_job_manager.snapshot_for(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="任务不存在。")
        return {"job": job.snapshot()}

    def _start_skill_job(kind: str, name: str, version: str | None) -> dict[str, Any]:
        if not allow_skill_actions:
            raise HTTPException(
                status_code=403,
                detail="页面内 Skill 操作未启用；请使用 paos web --allow-skill-actions 重新启动设置服务。",
            )
        try:
            job = skill_job_manager.start(kind, name, version)
        except SkillJobBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except SkillJobError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return {"job": job.snapshot()}

    @app.post("/api/forge/skills/install", dependencies=[Depends(auth)], status_code=202)
    def install_skill(update: SkillActionRequest) -> dict[str, Any]:
        return _start_skill_job("install", update.name, update.version)

    @app.post("/api/forge/skills/update", dependencies=[Depends(auth)], status_code=202)
    def update_skill(update: SkillActionRequest) -> dict[str, Any]:
        return _start_skill_job("update", update.name, update.version)

    @app.post("/api/forge/skills/remove", dependencies=[Depends(auth)], status_code=202)
    def remove_skill(update: SkillActionRequest) -> dict[str, Any]:
        return _start_skill_job("remove", update.name, None)

    @app.put("/api/settings", dependencies=[Depends(auth)])
    def put_settings(update: SettingsUpdate) -> dict[str, Any]:
        try:
            config, raw = settings_service.save_settings(
                path, update.config, update.revision, update.clear_secrets
            )
        except settings_service.RevisionMismatchError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except settings_service.InvalidSecretPathError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        except settings_service.InvalidSettingsError as exc:
            raise HTTPException(
                status_code=422,
                detail={"message": str(exc), "errors": exc.errors},
            ) from None
        payload = _payload(config, raw, restart_required=True)
        payload["saved"] = True
        payload["message"] = SAVE_MESSAGE
        return payload

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> Response:
        return Response(status_code=204)

    @app.get("/", include_in_schema=False, dependencies=[Depends(auth)])
    def index() -> FileResponse:
        index_path = STATIC_DIR / "index.html"
        if not index_path.is_file():
            raise HTTPException(status_code=404, detail="WebUI static page is not installed")
        return FileResponse(index_path, media_type="text/html")

    return app
