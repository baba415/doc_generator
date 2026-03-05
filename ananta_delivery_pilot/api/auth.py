"""API key authentication — §6.2 of pilot event contract."""
import os

from fastapi import HTTPException, Request


API_KEY = os.environ.get("ANANTA_API_KEY", "dev-key-change-me")


async def verify_api_key(request: Request) -> None:
    key = request.headers.get("X-API-Key")
    if key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
