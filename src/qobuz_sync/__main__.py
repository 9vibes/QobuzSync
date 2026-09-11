from __future__ import annotations

import os

import uvicorn


def main() -> None:
    # The Umbrel app proxy reaches the service inside its container network;
    # binding all interfaces here is intentional for container operation.
    host = os.environ.get("WEB_HOST", "0.0.0.0")
    port = int(os.environ.get("WEB_PORT", "23809"))
    uvicorn.run("qobuz_sync.web:app", host=host, port=port)


if __name__ == "__main__":
    main()
