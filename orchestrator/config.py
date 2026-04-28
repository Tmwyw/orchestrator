import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_dotenv() -> None:
    env_path = Path(os.getenv("ORCHESTRATOR_ENV_FILE", PROJECT_ROOT / ".env"))
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class Config:
    api_key: str
    host: str
    port: int
    database_url: str
    jobs_root: Path
    node_request_timeout_sec: int
    start_port_min: int
    start_port_max: int
    worker_poll_interval_sec: int


def get_config() -> Config:
    _load_dotenv()
    return Config(
        api_key=os.getenv("ORCHESTRATOR_API_KEY", "").strip(),
        host=os.getenv("ORCHESTRATOR_HOST", "0.0.0.0").strip(),
        port=_int_env("ORCHESTRATOR_PORT", 8090),
        database_url=os.getenv("DATABASE_URL", "").strip(),
        jobs_root=Path(os.getenv("JOBS_ROOT", "/opt/netrun-orchestrator/jobs")).resolve(),
        node_request_timeout_sec=_int_env("NODE_REQUEST_TIMEOUT_SEC", 1200),
        start_port_min=_int_env("ORCHESTRATOR_START_PORT_MIN", 32000),
        start_port_max=_int_env("ORCHESTRATOR_START_PORT_MAX", 65000),
        worker_poll_interval_sec=_int_env("WORKER_POLL_INTERVAL_SEC", 2),
    )
