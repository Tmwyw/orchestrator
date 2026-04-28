import uvicorn

from orchestrator.config import get_config


def main() -> None:
    cfg = get_config()
    uvicorn.run("orchestrator.main:app", host=cfg.host, port=cfg.port, log_level="info")


if __name__ == "__main__":
    main()
