from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api import audit, auth, departments_admin, maintenance, roles, system, users
from app.catalog.router import router as catalog_router
from app.core.errors import DomainError
from app.database import close_connection, init_db
from app.matching.router import router as matching_router
from app.matching.service import MatchingService
from app.pilots.router import router as pilot_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    del app
    init_db()
    # 值班人员重启服务后，先按可控时间规则释放已到期预留，再对外开放
    MatchingService().sweep_expired()
    yield
    close_connection()


app = FastAPI(title="全球健康创新试点运营服务", version="3.0.0", lifespan=lifespan)


@app.exception_handler(DomainError)
async def handle_domain_error(request: Request, exc: DomainError) -> JSONResponse:
    del request
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message, "context": exc.context}},
    )


app.include_router(auth.router)
app.include_router(users.router)
app.include_router(roles.router)
app.include_router(audit.router)
app.include_router(system.router)
app.include_router(departments_admin.router)
app.include_router(maintenance.router)
app.include_router(catalog_router)
app.include_router(pilot_router)
app.include_router(matching_router)


@app.get("/")
def root() -> dict:
    return {"service": "全球健康创新试点运营服务", "version": "3.0.0"}

