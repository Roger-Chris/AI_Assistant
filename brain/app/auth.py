from fastapi import Header, HTTPException

from .config import settings


async def require_token(authorization: str = Header(default="")) -> None:
    """Shared-secret auth for bridge / worker / app. Everything is behind Tailscale anyway;
    this stops a stray LAN device from feeding the brain."""
    if authorization != f"Bearer {settings.brain_token}":
        raise HTTPException(status_code=401, detail="bad token")
