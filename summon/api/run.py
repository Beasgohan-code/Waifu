import uvicorn


def main() -> None:
    # The application logs route templates; Uvicorn's default access log would
    # also emit raw paths/query strings containing member and warning IDs.
    uvicorn.run(
        "summon.api.app:app", host="0.0.0.0", port=8000, proxy_headers=True, access_log=False
    )
