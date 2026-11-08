"""Optional same-origin hosting for the already-built Mini App."""

from fastapi import HTTPException
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response
from starlette.types import Scope


class PublicWebFiles(StaticFiles):
    async def get_response(self, path: str, scope: Scope) -> Response:
        # Missing API/health routes must never turn into static files, even if a
        # future build accidentally contains files with those names.
        if any(path == root or path.startswith(f"{root}/") for root in ("api", "health")):
            raise HTTPException(status_code=404, detail="Not Found")
        return await super().get_response(path, scope)
