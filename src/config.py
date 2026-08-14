"""全局配置加载。

密钥注入优先级（高 → 低）：
    1. 真实系统环境变量（os.environ，如容器/CI 注入）
    2. 项目根目录的 .env 文件（本地开发用，须加入 .gitignore）
    3. config/config.yaml 中的占位值

这样密钥可完全脱离配置文件注入，config.yaml 不再承载明文密钥。
"""
import os
from pathlib import Path
import yaml

_PROJECT_ROOT = Path(__file__).parent.parent
_DEFAULT_CONFIG_PATH = _PROJECT_ROOT / "config" / "config.yaml"
_DEFAULT_ENV_PATH = _PROJECT_ROOT / ".env"


def _load_dotenv(path: Path = None) -> None:
    """极简 .env 解析：把 KEY=VALUE 写入 os.environ（setdefault，
    已存在的真实环境变量优先，不覆盖），无需 python-dotenv 依赖。
    支持两端空格、引号包裹的值与 # 注释行。
    """
    p = path or _DEFAULT_ENV_PATH
    if not p.exists():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, val)


def _apply_env_overrides(cfg: dict) -> dict:
    """用环境变量覆盖配置中的密钥/端点字段。"""
    if "llm" in cfg:
        if os.getenv("CLAUDE_API_KEY"):
            cfg["llm"]["claude_api_key"] = os.getenv("CLAUDE_API_KEY")
        if os.getenv("OPENAI_API_KEY"):
            cfg["llm"]["openai_api_key"] = os.getenv("OPENAI_API_KEY")
        if os.getenv("OPENAI_BASE_URL"):
            cfg["llm"]["openai_base_url"] = os.getenv("OPENAI_BASE_URL")
    if os.getenv("TUSHARE_TOKEN"):
        cfg.setdefault("data_sources", {}).setdefault("tushare", {})["token"] = os.getenv("TUSHARE_TOKEN")
    return cfg


def load_config(path: str = None) -> dict:
    p = Path(path) if path else _DEFAULT_CONFIG_PATH
    with open(p, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    _load_dotenv()
    _apply_env_overrides(cfg)
    return cfg


_config = None


def get_config() -> dict:
    global _config
    if _config is None:
        _config = load_config()
    return _config


def reload_config() -> dict:
    """清除缓存并重新加载配置（含 .env 与环境变量覆盖），返回新配置。

    UI 设置页保存配置后调用，避免「改配置必须重启进程」。
    注意：``.env`` 加载是 setdefault 语义——已存在于 ``os.environ`` 的键
    不会被新值覆盖；若改了 .env 中已注入过进程的密钥，仍需重启进程。
    """
    global _config
    _config = None
    return get_config()
