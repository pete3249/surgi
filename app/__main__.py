"""`python -m app` starts the server, for local development."""
import uvicorn

from .config import get_settings


def main() -> None:
    settings = get_settings()
    print(f"Open http://localhost:{settings.port}   (health check: /api/health, API docs: /docs)")
    uvicorn.run("app.main:app", host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
