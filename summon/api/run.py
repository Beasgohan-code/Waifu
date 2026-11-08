import os

import uvicorn


def main() -> None:
    try:
        port = int(os.environ.get("PORT", "8000"))
    except ValueError:
        raise ValueError("PORT must be an integer between 1 and 65535") from None
    if not 1 <= port <= 65535:
        raise ValueError("PORT must be an integer between 1 and 65535")
    # The application logs route templates; Uvicorn's default access log would
    # also emit raw paths/query strings containing member and warning IDs.
    uvicorn.run(
        "summon.api.app:app", host="0.0.0.0", port=port, proxy_headers=True, access_log=False
    )
