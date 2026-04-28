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


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


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
    refill_interval_sec: int
    refill_default_priority: int
    refill_max_skus_per_cycle: int
    proxy_allow_degraded_nodes: bool


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
        refill_interval_sec=_int_env("PROXY_REFILL_INTERVAL_SEC", 30),
        refill_default_priority=_int_env("REFILL_DEFAULT_PRIORITY", 10),
        refill_max_skus_per_cycle=_int_env("REFILL_MAX_SKUS_PER_CYCLE", 100),
        proxy_allow_degraded_nodes=_bool_env("PROXY_ALLOW_DEGRADED_NODES", False),
    )
