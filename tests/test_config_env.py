"""配置注入单测 —— .env 解析与密钥优先级。

优先级（高→低）：真实环境变量 > .env 文件 > config.yaml 占位。
真实环境变量绝不被 .env 覆盖（setdefault 语义）。
"""
import os

import pytest

from src.config import _apply_env_overrides, _load_dotenv


@pytest.fixture
def clean_env():
    """记录进入前已存在的环境变量键，测试后删除本测试新增的键，避免污染。"""
    before = set(os.environ.keys())
    yield
    for k in list(os.environ):
        if k not in before:
            del os.environ[k]


# ── _load_dotenv 解析 ───────────────────────────────────────────
class TestLoadDotenv:
    @pytest.mark.unit
    def test_parses_key_value(self, clean_env, tmp_path):
        env = tmp_path / ".env"
        env.write_text("TESTENV_PLAIN=hello\n", encoding="utf-8")
        _load_dotenv(env)
        assert os.environ.get("TESTENV_PLAIN") == "hello"

    @pytest.mark.unit
    def test_strips_quotes_and_spaces(self, clean_env, tmp_path):
        env = tmp_path / ".env"
        env.write_text('TESTENV_QUOTED = "quoted value"\n', encoding="utf-8")
        _load_dotenv(env)
        assert os.environ.get("TESTENV_QUOTED") == "quoted value"

    @pytest.mark.unit
    def test_skips_comments_and_blank_and_no_equals(self, clean_env, tmp_path):
        env = tmp_path / ".env"
        env.write_text(
            "# a comment\n"
            "\n"
            "TESTENV_REAL=yes\n"
            "INVALIDLINE\n",  # 无 = → 跳过
            encoding="utf-8",
        )
        _load_dotenv(env)
        assert os.environ.get("TESTENV_REAL") == "yes"
        assert "INVALIDLINE" not in os.environ

    @pytest.mark.unit
    def test_missing_file_is_noop(self, clean_env, tmp_path):
        # 不存在的路径静默返回，不抛异常
        _load_dotenv(tmp_path / "absent.env")

    @pytest.mark.unit
    def test_real_env_wins_over_file(self, clean_env, tmp_path):
        # 真实环境变量优先：setdefault 不覆盖已存在值
        os.environ["TESTENV_PRECEDE"] = "from-real-env"
        env = tmp_path / ".env"
        env.write_text("TESTENV_PRECEDE=from-file\n", encoding="utf-8")
        _load_dotenv(env)
        assert os.environ["TESTENV_PRECEDE"] == "from-real-env"


# ── _apply_env_overrides 注入 ───────────────────────────────────
class TestApplyEnvOverrides:
    @pytest.mark.unit
    def test_openai_key_injected(self, clean_env, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
        cfg = {"llm": {"openai_api_key": ""}}
        _apply_env_overrides(cfg)
        assert cfg["llm"]["openai_api_key"] == "sk-from-env"

    @pytest.mark.unit
    def test_openai_base_url_injected(self, clean_env, monkeypatch):
        monkeypatch.setenv("OPENAI_BASE_URL", "https://api.example.com")
        cfg = {"llm": {}}
        _apply_env_overrides(cfg)
        assert cfg["llm"]["openai_base_url"] == "https://api.example.com"

    @pytest.mark.unit
    def test_claude_key_injected(self, clean_env, monkeypatch):
        monkeypatch.setenv("CLAUDE_API_KEY", "sk-ant-x")
        cfg = {"llm": {"claude_api_key": ""}}
        _apply_env_overrides(cfg)
        assert cfg["llm"]["claude_api_key"] == "sk-ant-x"

    @pytest.mark.unit
    def test_tushare_token_injected(self, clean_env, monkeypatch):
        monkeypatch.setenv("TUSHARE_TOKEN", "tok-123")
        cfg = {}  # 无 data_sources 键也能注入
        _apply_env_overrides(cfg)
        assert cfg["data_sources"]["tushare"]["token"] == "tok-123"

    @pytest.mark.unit
    def test_no_env_no_change(self, clean_env, monkeypatch):
        # 确保目标键不存在（即便真实会话可能注入过）
        for k in ("CLAUDE_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL", "TUSHARE_TOKEN"):
            monkeypatch.delenv(k, raising=False)
        cfg = {"llm": {"claude_api_key": "KEEP"}}
        _apply_env_overrides(cfg)
        assert cfg["llm"]["claude_api_key"] == "KEEP"  # 未被改

    @pytest.mark.unit
    def test_cfg_without_llm_is_safe(self, clean_env, monkeypatch):
        monkeypatch.setenv("CLAUDE_API_KEY", "sk-x")
        cfg = {}
        _apply_env_overrides(cfg)
        assert "llm" not in cfg  # 不主动创建 llm 键
