import os
from datetime import timedelta
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = os.path.abspath(os.path.dirname(__file__))


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-key-change-in-prod")
    DATABASE_DIR = os.path.join(BASE_DIR, os.environ.get("DATABASE_DIR", "data"))
    GLOBAL_DB = os.path.join(DATABASE_DIR, "lexos_global.db")
    UPLOAD_FOLDER = os.path.join(BASE_DIR, os.environ.get("UPLOAD_FOLDER", "static/uploads"))
    MAX_CONTENT_LENGTH = int(os.environ.get("MAX_CONTENT_LENGTH", 16 * 1024 * 1024))
    BASE_DOMAIN = os.environ.get("BASE_DOMAIN", "lexos.mx")
    DEFAULT_TENANT_SLUG = os.environ.get("DEFAULT_TENANT_SLUG", "")
    ALLOWED_EXTENSIONS = {"pdf", "doc", "docx", "xls", "xlsx", "png", "jpg", "jpeg", "gif", "txt"}

    # ─── Bóveda de documentos ────────────────────────────────────────────────
    # Carpeta privada (fuera de /static): nunca se sirve directo, solo por /archivos/ con sesión.
    BOVEDA_DIR = os.path.join(BASE_DIR, os.environ.get("BOVEDA_DIR", "storage"))
    # Llave maestra (base64 urlsafe, 32 bytes). En producción es obligatoria y debe respaldarse:
    # si se pierde, los documentos cifrados no se pueden recuperar.
    BOVEDA_MASTER_KEY = os.environ.get("BOVEDA_MASTER_KEY", "")

    # ─── Asistente jurídico ─────────────────────────────────────────────────
    ASISTENTE_NOMBRE = os.environ.get("ASISTENTE_NOMBRE", "Tribuno")
    ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
    ASISTENTE_MODELO = os.environ.get("ASISTENTE_MODELO", "claude-sonnet-5-5")
    ASISTENTE_MODELO_RAPIDO = os.environ.get("ASISTENTE_MODELO_RAPIDO", "claude-haiku-5-5")
    WTF_CSRF_ENABLED = True

    # ─── Sesión ───────────────────────────────────────────────────────────────
    PERMANENT_SESSION_LIFETIME = timedelta(days=30)
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"

    # ─── Flask-Mail ───────────────────────────────────────────────────────────
    MAIL_SERVER   = os.environ.get("MAIL_SERVER", "smtp.gmail.com")
    MAIL_PORT     = int(os.environ.get("MAIL_PORT", 587))
    MAIL_USE_TLS  = os.environ.get("MAIL_USE_TLS", "true").lower() == "true"
    MAIL_USERNAME = os.environ.get("MAIL_USERNAME", "")
    MAIL_PASSWORD = os.environ.get("MAIL_PASSWORD", "")
    MAIL_DEFAULT_SENDER = os.environ.get(
        "MAIL_DEFAULT_SENDER", "LexOS <noreply@lexos.mx>"
    )
    CONTACT_EMAIL = os.environ.get("CONTACT_EMAIL", "")

    # ─── Modo revisión (botón "Comentar" para socios y probadores) ───────────
    REVISION_ACTIVA = os.environ.get("REVISION_ACTIVA", "").lower() in ("1", "true", "si", "sí")
    REVISION_CLAVE = os.environ.get("REVISION_CLAVE", "")

    # ─── Google Calendar OAuth ──────────────────────────────────────────────────
    GOOGLE_CLIENT_ID     = os.environ.get("GOOGLE_CLIENT_ID", "")
    GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")

    # ─── Dominio para reset links ─────────────────────────────────────────────
    RESET_TOKEN_TTL_HOURS = 24
    SERVER_NAME_DEV = os.environ.get("SERVER_NAME_DEV", "localhost:5001")

    # ─── Catálogos ────────────────────────────────────────────────────────────
    ROLES = ["super_admin", "admin", "abogado", "asistente", "cliente"]
    TIPOS_ASUNTO = [
        "civil", "penal", "mercantil", "laboral", "familiar",
        "administrativo", "fiscal", "amparo", "constitucional", "otro"
    ]
    TIPOS_DESPACHO = [
        "Mercantil", "Laboral", "Civil", "Penal", "Familiar",
        "Administrativo", "Fiscal", "Amparo", "Corporativo", "Inmobiliario",
    ]
    PLANES = [
        {"id": "starter",    "nombre": "Starter",    "precio": 0},
        {"id": "pro",        "nombre": "Pro",         "precio": 999},
        {"id": "enterprise", "nombre": "Enterprise",  "precio": 2499},
    ]

    @staticmethod
    def tenant_db(slug: str) -> str:
        return os.path.join(Config.DATABASE_DIR, f"tenant_{slug}.db")


class DevelopmentConfig(Config):
    DEBUG = True
    TESTING = False
    # Allow OAuth over HTTP for local dev
    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"


class ProductionConfig(Config):
    DEBUG = False
    TESTING = False
    SESSION_COOKIE_SECURE = True


class TestingConfig(Config):
    TESTING = True
    WTF_CSRF_ENABLED = False
    DATABASE_DIR = "/tmp/lexos_test"
    GLOBAL_DB = "/tmp/lexos_test/lexos_global.db"


config = {
    "development": DevelopmentConfig,
    "production": ProductionConfig,
    "testing": TestingConfig,
    "default": DevelopmentConfig,
}
