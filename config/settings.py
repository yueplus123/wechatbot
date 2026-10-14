from pathlib import Path
import os

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / "config" / ".env")
load_dotenv(BASE_DIR / ".env")

SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "dev-only-insecure-key")
DEBUG = os.getenv("DJANGO_DEBUG", "False").lower() in {"1", "true", "yes"}
ALLOWED_HOSTS = [
    h.strip()
    for h in os.getenv("DJANGO_ALLOWED_HOSTS", "127.0.0.1,localhost").split(",")
    if h.strip()
]

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "apps.accounts.apps.AccountsConfig",
    "apps.chatrooms.apps.ChatroomsConfig",
    "apps.rules.apps.RulesConfig",
    "apps.ledger.apps.LedgerConfig",
    "apps.gateway_api.apps.GatewayApiConfig",
    "apps.audit.apps.AuditConfig",
    "apps.integrations.apps.IntegrationsConfig",
    "apps.panel.apps.PanelConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# PoC 阶段可用 SQLite；正式版按文档切换 MySQL 8.4
USE_MYSQL = os.getenv("USE_MYSQL", "0").lower() in {"1", "true", "yes"}
if USE_MYSQL:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.mysql",
            "NAME": os.getenv("MYSQL_DATABASE", "xinhuo_bookkeeper"),
            "USER": os.getenv("MYSQL_USER", "bookkeeper"),
            "PASSWORD": os.getenv("MYSQL_PASSWORD", ""),
            "HOST": os.getenv("MYSQL_HOST", "127.0.0.1"),
            "PORT": os.getenv("MYSQL_PORT", "3306"),
            "OPTIONS": {"charset": "utf8mb4"},
        }
    }
else:
    _sqlite_dir = BASE_DIR / "runtime"
    _sqlite_dir.mkdir(parents=True, exist_ok=True)
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": os.getenv("SQLITE_PATH") or _sqlite_dir / "poc.sqlite3",
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "zh-hans"
TIME_ZONE = os.getenv("DJANGO_TIME_ZONE", "Asia/Shanghai")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "panel:login"
LOGIN_REDIRECT_URL = "panel:home"
LOGOUT_REDIRECT_URL = "panel:login"

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": [],
    "UNAUTHENTICATED_USER": None,
}

# 业务配置
GATEWAY_ID = os.getenv("GATEWAY_ID", "wx-gateway-01")
GATEWAY_HMAC_SECRET = os.getenv("GATEWAY_HMAC_SECRET", "")
DEFAULT_CURRENCY = os.getenv("DEFAULT_CURRENCY", "CNY")
TRIGGER_PREFIX = os.getenv("TRIGGER_PREFIX", "记账")
REVERSE_WINDOW_MINUTES = int(os.getenv("REVERSE_WINDOW_MINUTES", "10"))
RULE_VERSION = os.getenv("RULE_VERSION", "1")
DEDUP_WINDOW_SECONDS = int(os.getenv("DEDUP_WINDOW_SECONDS", "60"))

# 薪火电商统一对接（xinhuo-bridge）：优先只配 API_BASE + API_KEY
# 能力路径固定：products / wechat-bot/push / ledger/push / bridge/health
# 未写 config/.env 时仍推到线上薪火（压缩包不含 .env，避免记单成功但后台没有）
XINHUO_API_BASE = (
    os.getenv("XINHUO_API_BASE") or "http://192.144.236.133"
).strip()
XINHUO_BOT_PUSH_API_KEY = (
    os.getenv("XINHUO_BOT_PUSH_API_KEY") or ""
).strip()
XINHUO_BOT_PUSH_TIMEOUT = float(os.getenv("XINHUO_BOT_PUSH_TIMEOUT", "8") or 8)
XINHUO_BRIDGE_MAX_RETRIES = int(os.getenv("XINHUO_BRIDGE_MAX_RETRIES", "2") or 2)
# 可选覆盖（一般不需要；未设时由 API_BASE + 能力表拼接）
XINHUO_LEDGER_PUSH_URL = (os.getenv("XINHUO_LEDGER_PUSH_URL") or "").strip()
XINHUO_BOT_PUSH_URL = (os.getenv("XINHUO_BOT_PUSH_URL") or "").strip()
XINHUO_PRODUCT_QUERY_URL = (os.getenv("XINHUO_PRODUCT_QUERY_URL") or "").strip()
# 旧薪火无 action=pull 时，用 CEO 账号拉记账列表对账
XINHUO_API_USERNAME = (os.getenv("XINHUO_API_USERNAME") or "").strip()
XINHUO_API_PASSWORD = (os.getenv("XINHUO_API_PASSWORD") or "").strip()

from django.contrib.messages import constants as message_constants

MESSAGE_TAGS = {
    message_constants.DEBUG: "secondary",
    message_constants.INFO: "info",
    message_constants.SUCCESS: "success",
    message_constants.WARNING: "warning",
    message_constants.ERROR: "danger",
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "runtime": {
            "format": "%(asctime)s [%(name)s] %(levelname)s %(message)s",
            "datefmt": "%Y-%m-%d %H:%M:%S",
        },
    },
    "handlers": {
        "runtime_file": {
            "class": "config.safe_logging.SafeRotatingFileHandler",
            "filename": str(BASE_DIR / "data" / "runtime.log"),
            "maxBytes": 2_000_000,
            "backupCount": 3,
            "encoding": "utf-8",
            "formatter": "runtime",
            "delay": True,
        },
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "runtime",
        },
    },
    "root": {
        "handlers": ["runtime_file", "console"],
        "level": "INFO",
    },
    "loggers": {
        "django.request": {"level": "WARNING", "propagate": True},
        "apps": {"level": "INFO", "propagate": True},
        "gateway": {"level": "INFO", "propagate": True},
    },
}
