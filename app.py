"""Vercel/local entrypoint for the full-stack Business to Development app."""

from pathlib import Path

from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from agentic_core.api.app import app


PUBLIC_DIR = Path(__file__).resolve().parent / "public"

if (PUBLIC_DIR / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=PUBLIC_DIR / "assets"), name="assets")


@app.get("/", include_in_schema=False)
async def frontend_index():
    index = PUBLIC_DIR / "index.html"
    if index.is_file():
        return FileResponse(index)
    return RedirectResponse("/docs")


@app.get("/login", include_in_schema=False)
@app.get("/auth/callback", include_in_schema=False)
async def frontend_login():
    login = PUBLIC_DIR / "login.html"
    if login.is_file():
        return FileResponse(login)
    return RedirectResponse("/docs")

