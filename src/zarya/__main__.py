import uvicorn

from zarya.app import create_app
from zarya.config import Config


def main() -> None:
    config = Config.from_env()
    uvicorn.run(
        create_app(config),
        host=config.host,
        port=config.port,
        workers=1,
        proxy_headers=False,
        access_log=False,
    )


if __name__ == "__main__":
    main()
