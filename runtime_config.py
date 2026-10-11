"""运行时安全配置。

开发环境允许使用默认值以便快速启动；生产环境必须显式提供强随机密钥。
"""

import os


PRODUCTION_ENV_VALUES = {"prod", "production"}
MIN_SECRET_LENGTH = 32
WEAK_SECRET_PREFIXES = (
    "change-this",
    "change-me",
    "ecommerce-cs",
    "ecommerce-agent",
    "your-",
)
REQUIRED_PRODUCTION_SECRETS = (
    "SECRET_KEY",
    "JWT_SECRET",
    "AGENT_JWT_SECRET",
)


def is_production():
    value = (
        os.environ.get("APP_ENV")
        or os.environ.get("FLASK_ENV")
        or ""
    ).strip().lower()
    return value in PRODUCTION_ENV_VALUES


def env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def debug_enabled():
    """生产环境永远关闭 Flask Debug。"""
    return env_bool("FLASK_DEBUG", False) and not is_production()


def get_secret(name, development_default):
    value = os.environ.get(name, "").strip()
    if not is_production():
        return value or development_default

    if not value:
        raise RuntimeError(f"生产环境缺少必需密钥: {name}")
    if len(value) < MIN_SECRET_LENGTH:
        raise RuntimeError(
            f"生产环境密钥 {name} 长度不足 {MIN_SECRET_LENGTH} 个字符"
        )
    if value.lower().startswith(WEAK_SECRET_PREFIXES):
        raise RuntimeError(f"生产环境密钥 {name} 仍在使用示例值")
    return value


def validate_production_config():
    if not is_production():
        return

    secrets = {
        name: get_secret(name, "") for name in REQUIRED_PRODUCTION_SECRETS
    }
    if len(set(secrets.values())) != len(secrets):
        raise RuntimeError(
            "SECRET_KEY、JWT_SECRET、AGENT_JWT_SECRET 必须使用不同密钥"
        )
