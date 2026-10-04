from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.routers import admin_billing, agents, auth, billing, calls, demo, health, hvac, usage
from app.settings import get_settings

app = FastAPI(title="Veyra")
settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for module in (health, auth, agents, calls, usage, billing, admin_billing, hvac, demo):
    app.include_router(module.router)


@app.exception_handler(HTTPException)
async def http_error(_request: Request, exc: HTTPException):
    if isinstance(exc.detail, dict) and "error" in exc.detail:
        return JSONResponse(exc.detail, status_code=exc.status_code)
    return JSONResponse({"error": str(exc.detail), "code": "error"}, status_code=exc.status_code)
