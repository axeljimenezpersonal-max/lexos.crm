"""
LexOS — init_db.py
Inicializa la base de datos global y las bases por tenant.
Incluye schema completo + seeds de prueba.

Uso:
    python init_db.py               # crea todo desde cero
    python init_db.py --reset       # borra y recrea
    python init_db.py --seed-only   # solo inserta seeds (bases existentes)
"""

import os
import sys
import sqlite3
import hashlib
import json
from datetime import date, datetime, timedelta
from config import Config

os.makedirs(Config.DATABASE_DIR, exist_ok=True)


# ─────────────────────────────────────────────────────────────────────────────
#  ESQUEMA GLOBAL
# ─────────────────────────────────────────────────────────────────────────────

GLOBAL_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS tenants (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    slug              TEXT    UNIQUE NOT NULL,
    nombre_despacho   TEXT    NOT NULL,
    logo_url          TEXT,
    plan              TEXT    DEFAULT 'starter'
                              CHECK(plan IN ('starter','pro','enterprise','basico','profesional')),
    tipos_despacho    TEXT    DEFAULT '[]',
    fecha_alta        DATETIME DEFAULT CURRENT_TIMESTAMP,
    activo            INTEGER  DEFAULT 1,
    email_contacto    TEXT,
    telefono          TEXT,
    direccion         TEXT,
    rfc               TEXT,
    color_primario    TEXT    DEFAULT '#00DFA2',
    config_json       TEXT    DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS tenant_users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id     INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    nombre        TEXT    NOT NULL,
    apellidos     TEXT    NOT NULL,
    email         TEXT    NOT NULL,
    password_hash TEXT    NOT NULL,
    rol           TEXT    NOT NULL
                          CHECK(rol IN ('super_admin','admin','abogado','asistente','cliente')),
    activo        INTEGER  DEFAULT 1,
    avatar_url    TEXT,
    telefono      TEXT,
    cargo         TEXT     DEFAULT 'asociado',
    especialidades TEXT    DEFAULT '[]',
    fecha_ingreso DATE,
    estatus       TEXT     DEFAULT 'activo',
    fecha_alta    DATETIME DEFAULT CURRENT_TIMESTAMP,
    ultimo_acceso DATETIME,
    UNIQUE(tenant_id, email)
);

CREATE INDEX IF NOT EXISTS idx_tenant_users_email ON tenant_users(tenant_id, email);
CREATE INDEX IF NOT EXISTS idx_tenant_users_rol   ON tenant_users(tenant_id, rol);

-- ── Password reset tokens ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS password_reset_tokens (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES tenant_users(id) ON DELETE CASCADE,
    tenant_id   INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    token       TEXT    UNIQUE NOT NULL,
    expires_at  DATETIME NOT NULL,
    used        INTEGER  DEFAULT 0,
    fecha_alta  DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_prt_token    ON password_reset_tokens(token);
CREATE INDEX IF NOT EXISTS idx_prt_user     ON password_reset_tokens(user_id);

-- ── Google Calendar Tokens ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS gcal_tokens (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL REFERENCES tenant_users(id) ON DELETE CASCADE,
    tenant_id     INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    access_token  TEXT,
    refresh_token TEXT,
    token_expiry  TEXT,
    sync_activo   INTEGER DEFAULT 0,
    last_sync     DATETIME,
    fecha_alta    DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, tenant_id)
);

CREATE INDEX IF NOT EXISTS idx_gcal_user ON gcal_tokens(user_id, tenant_id);

-- ── User Integrations (Gmail, etc.) ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS user_integrations (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id      INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id        INTEGER NOT NULL REFERENCES tenant_users(id) ON DELETE CASCADE,
    servicio       TEXT    NOT NULL DEFAULT 'gmail',
    access_token   TEXT,
    refresh_token  TEXT,
    token_expiry   DATETIME,
    email_cuenta   TEXT,
    nombre_cuenta  TEXT,
    activo         BOOLEAN DEFAULT 1,
    created_at     DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_user_integrations ON user_integrations(user_id, tenant_id, servicio);
"""

# ─────────────────────────────────────────────────────────────────────────────
#  ESQUEMA POR TENANT
# ─────────────────────────────────────────────────────────────────────────────

TENANT_SCHEMA = """
PRAGMA foreign_keys = ON;

-- ── Clientes ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS clientes (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    tipo                  TEXT    DEFAULT 'persona_fisica'
                                  CHECK(tipo IN ('persona_fisica','persona_moral')),
    nombre                TEXT    NOT NULL,
    apellidos             TEXT,
    razon_social          TEXT,
    rfc                   TEXT,
    curp                  TEXT,
    email                 TEXT,
    telefono              TEXT,
    celular               TEXT,
    -- Dirección extendida
    direccion             TEXT,
    num_ext               TEXT,
    num_int               TEXT,
    colonia               TEXT,
    municipio             TEXT,
    ciudad                TEXT,
    estado                TEXT,
    cp                    TEXT,
    -- CRM
    foto_url              TEXT,
    fecha_nacimiento      DATE,
    giro_comercial        TEXT,
    representante_legal   TEXT,
    contacto_principal    TEXT,
    origen_cliente        TEXT,
    potencial_facturacion REAL    DEFAULT 0,
    abogado_id            INTEGER,
    etiquetas             TEXT    DEFAULT '[]',
    invitar_portal        INTEGER DEFAULT 0,
    notas                 TEXT,
    activo                INTEGER DEFAULT 1,
    estatus               TEXT    DEFAULT 'activo'
                                  CHECK(estatus IN ('activo','inactivo','prospecto')),
    fecha_alta            DATETIME DEFAULT CURRENT_TIMESTAMP,
    creado_por            INTEGER
);

-- ── Notas de cliente ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS cliente_notas (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    cliente_id  INTEGER NOT NULL REFERENCES clientes(id) ON DELETE CASCADE,
    contenido   TEXT    NOT NULL,
    tipo        TEXT    DEFAULT 'nota'
                        CHECK(tipo IN ('nota','llamada','reunion','correo','whatsapp','otro')),
    creado_por  INTEGER,
    fecha       DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- ── Historial de comunicaciones ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS cliente_comunicaciones (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    cliente_id  INTEGER NOT NULL REFERENCES clientes(id) ON DELETE CASCADE,
    tipo        TEXT    DEFAULT 'llamada'
                        CHECK(tipo IN ('llamada','correo','reunion','whatsapp','otro')),
    resumen     TEXT    NOT NULL,
    creado_por  INTEGER,
    fecha       DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- ── Documentos del cliente ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS cliente_documentos (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    cliente_id     INTEGER NOT NULL REFERENCES clientes(id) ON DELETE CASCADE,
    nombre         TEXT    NOT NULL,
    descripcion    TEXT,
    archivo_url    TEXT,
    archivo_nombre TEXT,
    tamanio        INTEGER,
    mime_type      TEXT,
    fecha_subida   DATETIME DEFAULT CURRENT_TIMESTAMP,
    subido_por     INTEGER
);

-- ── Asuntos ───────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS asuntos (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    numero_expediente    TEXT,
    titulo               TEXT    NOT NULL,
    descripcion          TEXT,
    tipo_asunto          TEXT    DEFAULT 'civil',
    sub_tipo             TEXT,
    estado               TEXT    DEFAULT 'activo'
                                 CHECK(estado IN ('por_iniciar','activo','en_espera','completado','archivado','suspendido','cerrado')),
    prioridad            TEXT    DEFAULT 'media'
                                 CHECK(prioridad IN ('alta','media','baja')),
    cliente_id           INTEGER REFERENCES clientes(id),
    abogado_responsable  INTEGER,
    juzgado              TEXT,
    numero_juicio        TEXT,
    fecha_inicio         DATE,
    fecha_cierre         DATE,
    fecha_limite         DATE,
    monto_estimado       REAL,
    honorarios_pactados  REAL,
    forma_cobro          TEXT    DEFAULT 'fijo'
                                 CHECK(forma_cobro IN ('fijo','por_hora','por_evento')),
    contrario_nombre     TEXT,
    contrario_abogado    TEXT,
    contrario_despacho   TEXT,
    notas                TEXT,
    fecha_alta           DATETIME DEFAULT CURRENT_TIMESTAMP,
    creado_por           INTEGER
);

CREATE TABLE IF NOT EXISTS asunto_colaboradores (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    asunto_id  INTEGER NOT NULL REFERENCES asuntos(id) ON DELETE CASCADE,
    user_id    INTEGER NOT NULL,
    rol        TEXT    DEFAULT 'colaborador'
);

-- ── Actividades del asunto ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS asunto_actividades (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    asunto_id        INTEGER NOT NULL REFERENCES asuntos(id) ON DELETE CASCADE,
    tipo             TEXT    DEFAULT 'nota'
                             CHECK(tipo IN ('audiencia','acuerdo','diligencia','vencimiento','reunion','nota','otro')),
    titulo           TEXT    NOT NULL,
    descripcion      TEXT,
    fecha            DATETIME NOT NULL,
    recordatorio_dias INTEGER,
    realizado        INTEGER DEFAULT 0,
    creado_por       INTEGER,
    fecha_alta       DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- ── Sentencias / resoluciones ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS asunto_sentencias (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    asunto_id      INTEGER NOT NULL REFERENCES asuntos(id) ON DELETE CASCADE,
    titulo         TEXT    NOT NULL,
    tipo           TEXT    DEFAULT 'sentencia'
                           CHECK(tipo IN ('sentencia','resolucion','auto','acuerdo','otro')),
    contenido      TEXT,
    fecha_emision  DATE,
    archivo_url    TEXT,
    archivo_nombre TEXT,
    creado_por     INTEGER,
    fecha_alta     DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- ── Horas trabajadas ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS asunto_horas (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    asunto_id   INTEGER NOT NULL REFERENCES asuntos(id) ON DELETE CASCADE,
    user_id     INTEGER NOT NULL,
    horas       REAL    NOT NULL,
    descripcion TEXT,
    fecha       DATE    NOT NULL DEFAULT (date('now')),
    fecha_alta  DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- ── Notas internas del asunto ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS asunto_notas (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    asunto_id   INTEGER NOT NULL REFERENCES asuntos(id) ON DELETE CASCADE,
    contenido   TEXT    NOT NULL,
    menciones   TEXT    DEFAULT '[]',
    creado_por  INTEGER,
    fecha       DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- ── Documentos ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS documentos (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    asunto_id        INTEGER REFERENCES asuntos(id),
    nombre           TEXT    NOT NULL,
    descripcion      TEXT,
    tipo_documento   TEXT,
    archivo_url      TEXT,
    archivo_nombre   TEXT,
    tamanio          INTEGER,
    mime_type        TEXT,
    fecha_documento  DATE,
    fecha_subida     DATETIME DEFAULT CURRENT_TIMESTAMP,
    subido_por       INTEGER
);

-- ── Cobros / Facturación ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS cobros (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    asunto_id           INTEGER REFERENCES asuntos(id),
    cliente_id          INTEGER REFERENCES clientes(id),
    concepto            TEXT    NOT NULL,
    numero_factura      TEXT,
    monto               REAL    NOT NULL DEFAULT 0,
    moneda              TEXT    DEFAULT 'MXN',
    iva_pct             REAL    DEFAULT 16.0,
    iva_monto           REAL    DEFAULT 0,
    total               REAL    DEFAULT 0,
    descuento_tipo      TEXT    DEFAULT 'fijo',
    descuento_valor     REAL    DEFAULT 0,
    estado              TEXT    DEFAULT 'pendiente'
                                CHECK(estado IN ('pendiente','abono','pagado','vencido','cancelado')),
    fecha_emision       DATE,
    fecha_vencimiento   DATE,
    fecha_pago          DATE,
    metodo_pago         TEXT,
    referencia          TEXT,
    monto_abonado       REAL    DEFAULT 0,
    fecha_abono         DATE,
    abogado_id          INTEGER,
    archivo_factura_url TEXT,
    archivo_xml_url     TEXT,
    enviar_correo       INTEGER DEFAULT 0,
    notas               TEXT,
    fecha_alta          DATETIME DEFAULT CURRENT_TIMESTAMP,
    creado_por          INTEGER
);

CREATE TABLE IF NOT EXISTS cobro_pagos (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    cobro_id        INTEGER NOT NULL REFERENCES cobros(id) ON DELETE CASCADE,
    monto           REAL    NOT NULL,
    fecha           DATE    NOT NULL,
    metodo          TEXT,
    referencia      TEXT,
    comprobante_url TEXT,
    notas           TEXT,
    creado_por      INTEGER,
    fecha_alta      DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- ── Eventos de Calendario ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS eventos_calendario (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    titulo                TEXT    NOT NULL,
    descripcion           TEXT,
    tipo                  TEXT    DEFAULT 'audiencia'
                                  CHECK(tipo IN ('audiencia','reunion','plazo','tarea','otro',
                                                 'vencimiento','diligencia','fecha_limite','recordatorio')),
    color_custom          TEXT,
    asunto_id             INTEGER REFERENCES asuntos(id),
    cliente_id            INTEGER REFERENCES clientes(id),
    fecha_inicio          DATETIME NOT NULL,
    fecha_fin             DATETIME,
    todo_el_dia           INTEGER  DEFAULT 0,
    recurrencia           TEXT    DEFAULT 'none'
                                  CHECK(recurrencia IN ('none','diario','semanal','mensual','anual')),
    recurrencia_parent_id INTEGER REFERENCES eventos_calendario(id),
    lugar                 TEXT,
    url_meet              TEXT,
    recordatorio          INTEGER,
    gcal_event_id         TEXT,
    estado                TEXT    DEFAULT 'pendiente'
                                  CHECK(estado IN ('pendiente','completado','cancelado')),
    creado_por            INTEGER,
    fecha_alta            DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS evento_participantes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    evento_id  INTEGER NOT NULL REFERENCES eventos_calendario(id) ON DELETE CASCADE,
    user_id    INTEGER NOT NULL
);

-- ── Notas ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS notas (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    asunto_id  INTEGER REFERENCES asuntos(id),
    titulo     TEXT,
    contenido  TEXT    NOT NULL,
    tipo       TEXT    DEFAULT 'nota'
                       CHECK(tipo IN ('nota','memo','resumen','instruccion')),
    fecha_alta DATETIME DEFAULT CURRENT_TIMESTAMP,
    creado_por INTEGER
);

-- ── Tareas ────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS tareas (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    titulo           TEXT    NOT NULL,
    descripcion      TEXT,
    asunto_id        INTEGER REFERENCES asuntos(id),
    asignado_a       INTEGER,
    prioridad        TEXT    DEFAULT 'media'
                             CHECK(prioridad IN ('alta','media','baja')),
    estado           TEXT    DEFAULT 'pendiente'
                             CHECK(estado IN ('pendiente','en_proceso','completada','cancelada')),
    fecha_vencimiento DATE,
    fecha_completada  DATETIME,
    creado_por        INTEGER,
    fecha_alta        DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- ── Investigaciones ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS investigaciones (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    asunto_id       INTEGER REFERENCES asuntos(id),
    titulo          TEXT    NOT NULL,
    contenido       TEXT,
    fuente          TEXT,
    url_referencia  TEXT,
    tags            TEXT    DEFAULT '[]',
    fecha_alta      DATETIME DEFAULT CURRENT_TIMESTAMP,
    creado_por      INTEGER
);

CREATE TABLE IF NOT EXISTS inv_colecciones (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    nombre      TEXT    NOT NULL,
    asunto_id   INTEGER REFERENCES asuntos(id) ON DELETE SET NULL,
    creado_por  INTEGER,
    fecha_alta  DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS inv_coleccion_items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    coleccion_id    INTEGER NOT NULL REFERENCES inv_colecciones(id) ON DELETE CASCADE,
    tipo            TEXT    NOT NULL,
    entidad_id      INTEGER,
    titulo          TEXT    NOT NULL,
    url             TEXT,
    notas           TEXT,
    fecha_alta      DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS inv_organos (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    nombre      TEXT    NOT NULL,
    circuito    TEXT,
    estado_mex  TEXT,
    tipo_organo TEXT,
    materia     TEXT,
    es_custom   INTEGER DEFAULT 0,
    creado_por  INTEGER,
    fecha_alta  DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS inv_busqueda_historial (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    termino     TEXT    NOT NULL,
    resultados  INTEGER DEFAULT 0,
    fecha       DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- ── Biblioteca (legacy) ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS biblioteca_items (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    titulo        TEXT    NOT NULL,
    descripcion   TEXT,
    tipo          TEXT    DEFAULT 'ley'
                          CHECK(tipo IN ('ley','jurisprudencia','template','articulo','otro')),
    contenido     TEXT,
    archivo_url   TEXT,
    archivo_nombre TEXT,
    tags          TEXT    DEFAULT '[]',
    publico       INTEGER DEFAULT 0,
    fecha_alta    DATETIME DEFAULT CURRENT_TIMESTAMP,
    creado_por    INTEGER
);

-- ── Biblioteca — Carpetas del estudio ───────────────────────────────────────
CREATE TABLE IF NOT EXISTS biblioteca_carpetas (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    nombre      TEXT    NOT NULL,
    parent_id   INTEGER REFERENCES biblioteca_carpetas(id) ON DELETE CASCADE,
    creado_por  INTEGER,
    fecha_alta  DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- ── Biblioteca — Archivos del estudio ───────────────────────────────────────
CREATE TABLE IF NOT EXISTS biblioteca_archivos (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    carpeta_id      INTEGER REFERENCES biblioteca_carpetas(id) ON DELETE SET NULL,
    nombre          TEXT    NOT NULL,
    nombre_original TEXT,
    archivo_url     TEXT    NOT NULL,
    tamanio         INTEGER DEFAULT 0,
    mime_type       TEXT,
    extension       TEXT,
    asunto_id       INTEGER REFERENCES asuntos(id),
    cliente_id      INTEGER REFERENCES clientes(id),
    creado_por      INTEGER,
    fecha_alta      DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- ── Biblioteca — Plantillas (formatos) ──────────────────────────────────────
CREATE TABLE IF NOT EXISTS biblioteca_plantillas (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    nombre         TEXT    NOT NULL,
    categoria      TEXT    DEFAULT 'Otro',
    descripcion    TEXT,
    tipo_derecho   TEXT,
    archivo_url    TEXT,
    archivo_nombre TEXT,
    tamanio        INTEGER DEFAULT 0,
    extension      TEXT,
    es_custom      INTEGER DEFAULT 0,
    creado_por     INTEGER,
    fecha_alta     DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- ── Biblioteca — Documentos del editor ──────────────────────────────────────
CREATE TABLE IF NOT EXISTS biblioteca_documentos (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    titulo          TEXT    NOT NULL,
    contenido_html  TEXT,
    plantilla_id    INTEGER REFERENCES biblioteca_plantillas(id),
    asunto_id       INTEGER REFERENCES asuntos(id),
    cliente_id      INTEGER REFERENCES clientes(id),
    estado          TEXT    DEFAULT 'borrador'
                            CHECK(estado IN ('borrador','final','archivado')),
    share_token     TEXT,
    share_expires   DATETIME,
    creado_por      INTEGER,
    fecha_alta      DATETIME DEFAULT CURRENT_TIMESTAMP,
    fecha_mod       DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- ── Biblioteca — Versiones de documentos ────────────────────────────────────
CREATE TABLE IF NOT EXISTS biblioteca_doc_versiones (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    documento_id   INTEGER NOT NULL REFERENCES biblioteca_documentos(id) ON DELETE CASCADE,
    contenido_html TEXT,
    version        INTEGER DEFAULT 1,
    creado_por     INTEGER,
    fecha          DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- ── Biblioteca — Links compartidos ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS biblioteca_compartidos (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tipo        TEXT    NOT NULL,
    entidad_id  INTEGER NOT NULL,
    token       TEXT    UNIQUE NOT NULL,
    expires_at  DATETIME NOT NULL,
    accesos     INTEGER DEFAULT 0,
    creado_por  INTEGER,
    fecha_alta  DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- ── Biblioteca — Acuerdos y audiencias ──────────────────────────────────────
CREATE TABLE IF NOT EXISTS biblioteca_acuerdos (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    tipo            TEXT    DEFAULT 'acuerdo'
                            CHECK(tipo IN ('acuerdo','audiencia')),
    neun            TEXT,
    numero_asunto   TEXT,
    organo          TEXT,
    circuito        TEXT,
    autoridad       TEXT,
    fecha           DATE,
    descripcion     TEXT,
    asunto_id       INTEGER REFERENCES asuntos(id),
    creado_por      INTEGER,
    fecha_alta      DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- ── Actividad / Audit Log ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS actividad_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER,
    accion      TEXT    NOT NULL,
    entidad     TEXT,
    entidad_id  INTEGER,
    detalles    TEXT    DEFAULT '{}',
    ip          TEXT,
    fecha       DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- ── Portal Clientes ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS portal_clientes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    cliente_id      INTEGER NOT NULL REFERENCES clientes(id) ON DELETE CASCADE,
    email           TEXT    NOT NULL,
    password_hash   TEXT,
    magic_token     TEXT,
    magic_expires   DATETIME,
    activo          INTEGER DEFAULT 1,
    intentos_login  INTEGER DEFAULT 0,
    bloqueado_hasta DATETIME,
    ultimo_login    DATETIME,
    fecha_alta      DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(cliente_id)
);

CREATE TABLE IF NOT EXISTS portal_mensajes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    cliente_id  INTEGER NOT NULL REFERENCES clientes(id) ON DELETE CASCADE,
    asunto_id   INTEGER REFERENCES asuntos(id),
    remitente   TEXT    DEFAULT 'despacho'
                        CHECK(remitente IN ('cliente','despacho')),
    mensaje     TEXT    NOT NULL,
    leido       INTEGER DEFAULT 0,
    fecha       DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS portal_docs_compartidos (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    cliente_id      INTEGER NOT NULL REFERENCES clientes(id) ON DELETE CASCADE,
    titulo          TEXT    NOT NULL,
    descripcion     TEXT,
    archivo_url     TEXT,
    archivo_nombre  TEXT,
    tamanio         INTEGER DEFAULT 0,
    asunto_id       INTEGER REFERENCES asuntos(id),
    compartido_por  INTEGER,
    fecha           DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS portal_accesos (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    cliente_id      INTEGER REFERENCES clientes(id),
    token           TEXT    UNIQUE,
    activo          INTEGER DEFAULT 1,
    fecha_expiracion DATETIME,
    fecha_alta      DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- ── Onboarding ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS onboarding_progress (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER NOT NULL UNIQUE,
    fase_actual      INTEGER DEFAULT 0,
    paso_actual      INTEGER DEFAULT 0,
    fases_completadas TEXT   DEFAULT '[]',
    completado       INTEGER DEFAULT 0,
    fecha_inicio     DATETIME DEFAULT CURRENT_TIMESTAMP,
    fecha_completado DATETIME
);

-- ── Índices ───────────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_asuntos_cliente     ON asuntos(cliente_id);
CREATE INDEX IF NOT EXISTS idx_asuntos_estado      ON asuntos(estado);
CREATE INDEX IF NOT EXISTS idx_asuntos_fecha_lim   ON asuntos(fecha_limite);
CREATE INDEX IF NOT EXISTS idx_cobros_cliente      ON cobros(cliente_id);
CREATE INDEX IF NOT EXISTS idx_cobros_estado       ON cobros(estado);
CREATE INDEX IF NOT EXISTS idx_eventos_fecha       ON eventos_calendario(fecha_inicio);
CREATE INDEX IF NOT EXISTS idx_tareas_asignado     ON tareas(asignado_a);
CREATE INDEX IF NOT EXISTS idx_actividad_fecha     ON actividad_log(fecha);
CREATE INDEX IF NOT EXISTS idx_portal_cli_cliente  ON portal_clientes(cliente_id);
CREATE INDEX IF NOT EXISTS idx_portal_cli_email    ON portal_clientes(email);
CREATE INDEX IF NOT EXISTS idx_portal_msg_cliente  ON portal_mensajes(cliente_id);
CREATE INDEX IF NOT EXISTS idx_portal_docs_cliente ON portal_docs_compartidos(cliente_id);
CREATE INDEX IF NOT EXISTS idx_inv_col_items       ON inv_coleccion_items(coleccion_id);
CREATE INDEX IF NOT EXISTS idx_inv_organos_materia ON inv_organos(materia);
CREATE INDEX IF NOT EXISTS idx_inv_hist_user       ON inv_busqueda_historial(user_id);
CREATE INDEX IF NOT EXISTS idx_clientes_estatus    ON clientes(estatus);
CREATE INDEX IF NOT EXISTS idx_cli_notas_cliente   ON cliente_notas(cliente_id);
CREATE INDEX IF NOT EXISTS idx_cli_com_cliente     ON cliente_comunicaciones(cliente_id);
CREATE INDEX IF NOT EXISTS idx_cli_doc_cliente     ON cliente_documentos(cliente_id);
CREATE INDEX IF NOT EXISTS idx_act_asunto          ON asunto_actividades(asunto_id);
CREATE INDEX IF NOT EXISTS idx_horas_asunto        ON asunto_horas(asunto_id);
CREATE INDEX IF NOT EXISTS idx_notas_asunto        ON asunto_notas(asunto_id);
CREATE INDEX IF NOT EXISTS idx_bib_archivos_carpeta ON biblioteca_archivos(carpeta_id);
CREATE INDEX IF NOT EXISTS idx_bib_archivos_asunto  ON biblioteca_archivos(asunto_id);
CREATE INDEX IF NOT EXISTS idx_bib_docs_asunto      ON biblioteca_documentos(asunto_id);
CREATE INDEX IF NOT EXISTS idx_bib_versiones_doc    ON biblioteca_doc_versiones(documento_id);
CREATE INDEX IF NOT EXISTS idx_bib_compartidos_token ON biblioteca_compartidos(token);
CREATE INDEX IF NOT EXISTS idx_bib_acuerdos_tipo    ON biblioteca_acuerdos(tipo);

CREATE TABLE IF NOT EXISTS reportes_carpetas (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    nombre      TEXT    NOT NULL,
    creado_por  INTEGER,
    fecha_alta  DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS reportes_guardados (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    nombre          TEXT    NOT NULL,
    tipo            TEXT    NOT NULL,
    filtros_json    TEXT    DEFAULT '{}',
    carpeta_id      INTEGER REFERENCES reportes_carpetas(id) ON DELETE SET NULL,
    programado      INTEGER DEFAULT 0,
    cron_expresion  TEXT,
    ultimo_envio    DATETIME,
    creado_por      INTEGER,
    fecha_alta      DATETIME DEFAULT CURRENT_TIMESTAMP,
    fecha_mod       DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_rpt_guardados_carpeta ON reportes_guardados(carpeta_id);
"""

# ─────────────────────────────────────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def hash_password(pwd: str) -> str:
    return hashlib.sha256(pwd.encode()).hexdigest()


def get_global_conn():
    conn = sqlite3.connect(Config.GLOBAL_DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_tenant_conn(slug: str):
    path = Config.tenant_db(slug)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# ─────────────────────────────────────────────────────────────────────────────
#  INICIALIZACIÓN
# ─────────────────────────────────────────────────────────────────────────────

def init_global():
    print("  → Inicializando base de datos global...")
    conn = get_global_conn()
    conn.executescript(GLOBAL_SCHEMA)
    conn.commit()
    conn.close()
    print(f"     ✓ {Config.GLOBAL_DB}")


def init_tenant(slug: str):
    print(f"  → Inicializando DB del tenant '{slug}'...")
    conn = get_tenant_conn(slug)
    conn.executescript(TENANT_SCHEMA)
    conn.commit()
    conn.close()
    print(f"     ✓ {Config.tenant_db(slug)}")


# ─────────────────────────────────────────────────────────────────────────────
#  MIGRACIÓN (para bases existentes)
# ─────────────────────────────────────────────────────────────────────────────

_MIGRATE_COLS = [
    # (tabla, columna, definicion_sqlite)
    ("clientes", "num_ext",               "TEXT"),
    ("clientes", "num_int",               "TEXT"),
    ("clientes", "municipio",             "TEXT"),
    ("clientes", "estatus",               "TEXT DEFAULT 'activo'"),
    ("clientes", "foto_url",              "TEXT"),
    ("clientes", "fecha_nacimiento",      "DATE"),
    ("clientes", "giro_comercial",        "TEXT"),
    ("clientes", "representante_legal",   "TEXT"),
    ("clientes", "contacto_principal",    "TEXT"),
    ("clientes", "origen_cliente",        "TEXT"),
    ("clientes", "potencial_facturacion", "REAL DEFAULT 0"),
    ("clientes", "abogado_id",            "INTEGER"),
    ("clientes", "etiquetas",             "TEXT DEFAULT '[]'"),
    ("clientes", "invitar_portal",        "INTEGER DEFAULT 0"),
    # tenant_users — equipo
    ("tenant_users", "cargo",           "TEXT DEFAULT 'asociado'"),
    ("tenant_users", "especialidades",  "TEXT DEFAULT '[]'"),
    ("tenant_users", "fecha_ingreso",   "DATE"),
    ("tenant_users", "estatus",         "TEXT DEFAULT 'activo'"),
    ("asuntos", "sub_tipo",           "TEXT"),
    ("asuntos", "fecha_limite",       "DATE"),
    ("asuntos", "forma_cobro",        "TEXT DEFAULT 'fijo'"),
    ("asuntos", "contrario_nombre",   "TEXT"),
    ("asuntos", "contrario_abogado",  "TEXT"),
    ("asuntos", "contrario_despacho", "TEXT"),
    ("asuntos", "juzgado",            "TEXT"),
    ("asuntos", "numero_juicio",      "TEXT"),
    # ── Cobros — columnas nuevas ──────────────────────────────────────────────
    ("cobros", "numero_factura",      "TEXT"),
    ("cobros", "iva_pct",             "REAL DEFAULT 16.0"),
    ("cobros", "iva_monto",           "REAL DEFAULT 0"),
    ("cobros", "total",               "REAL DEFAULT 0"),
    ("cobros", "descuento_tipo",      "TEXT DEFAULT 'fijo'"),
    ("cobros", "descuento_valor",     "REAL DEFAULT 0"),
    ("cobros", "monto_abonado",       "REAL DEFAULT 0"),
    ("cobros", "fecha_abono",         "DATE"),
    ("cobros", "abogado_id",          "INTEGER"),
    ("cobros", "archivo_factura_url", "TEXT"),
    ("cobros", "archivo_xml_url",     "TEXT"),
    ("cobros", "enviar_correo",       "INTEGER DEFAULT 0"),
    # ── Eventos calendario — columnas nuevas ──────────────────────────────────
    ("eventos_calendario", "color_custom",          "TEXT"),
    ("eventos_calendario", "cliente_id",            "INTEGER"),
    ("eventos_calendario", "recurrencia",           "TEXT DEFAULT 'none'"),
    ("eventos_calendario", "recurrencia_parent_id", "INTEGER"),
    ("eventos_calendario", "gcal_event_id",         "TEXT"),
]

_MIGRATE_TABLES = """
CREATE TABLE IF NOT EXISTS cliente_notas (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    cliente_id  INTEGER NOT NULL REFERENCES clientes(id) ON DELETE CASCADE,
    contenido   TEXT    NOT NULL,
    tipo        TEXT    DEFAULT 'nota'
                        CHECK(tipo IN ('nota','llamada','reunion','correo','whatsapp','otro')),
    creado_por  INTEGER,
    fecha       DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS cliente_comunicaciones (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    cliente_id  INTEGER NOT NULL REFERENCES clientes(id) ON DELETE CASCADE,
    tipo        TEXT    DEFAULT 'llamada'
                        CHECK(tipo IN ('llamada','correo','reunion','whatsapp','otro')),
    resumen     TEXT    NOT NULL,
    creado_por  INTEGER,
    fecha       DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS cliente_documentos (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    cliente_id     INTEGER NOT NULL REFERENCES clientes(id) ON DELETE CASCADE,
    nombre         TEXT    NOT NULL,
    descripcion    TEXT,
    archivo_url    TEXT,
    archivo_nombre TEXT,
    tamanio        INTEGER,
    mime_type      TEXT,
    fecha_subida   DATETIME DEFAULT CURRENT_TIMESTAMP,
    subido_por     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_clientes_estatus  ON clientes(estatus);
CREATE INDEX IF NOT EXISTS idx_cli_notas_cliente ON cliente_notas(cliente_id);
CREATE INDEX IF NOT EXISTS idx_cli_com_cliente   ON cliente_comunicaciones(cliente_id);
CREATE INDEX IF NOT EXISTS idx_cli_doc_cliente   ON cliente_documentos(cliente_id);
CREATE TABLE IF NOT EXISTS asunto_actividades (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    asunto_id        INTEGER NOT NULL REFERENCES asuntos(id) ON DELETE CASCADE,
    tipo             TEXT    DEFAULT 'nota',
    titulo           TEXT    NOT NULL,
    descripcion      TEXT,
    fecha            DATETIME NOT NULL,
    recordatorio_dias INTEGER,
    realizado        INTEGER DEFAULT 0,
    creado_por       INTEGER,
    fecha_alta       DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS asunto_sentencias (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    asunto_id      INTEGER NOT NULL REFERENCES asuntos(id) ON DELETE CASCADE,
    titulo         TEXT    NOT NULL,
    tipo           TEXT    DEFAULT 'sentencia',
    contenido      TEXT,
    fecha_emision  DATE,
    archivo_url    TEXT,
    archivo_nombre TEXT,
    creado_por     INTEGER,
    fecha_alta     DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS asunto_horas (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    asunto_id   INTEGER NOT NULL REFERENCES asuntos(id) ON DELETE CASCADE,
    user_id     INTEGER NOT NULL,
    horas       REAL    NOT NULL,
    descripcion TEXT,
    fecha       DATE    NOT NULL DEFAULT (date('now')),
    fecha_alta  DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS asunto_notas (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    asunto_id   INTEGER NOT NULL REFERENCES asuntos(id) ON DELETE CASCADE,
    contenido   TEXT    NOT NULL,
    menciones   TEXT    DEFAULT '[]',
    creado_por  INTEGER,
    fecha       DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_asuntos_fecha_lim ON asuntos(fecha_limite);
CREATE INDEX IF NOT EXISTS idx_act_asunto        ON asunto_actividades(asunto_id);
CREATE INDEX IF NOT EXISTS idx_horas_asunto      ON asunto_horas(asunto_id);
CREATE INDEX IF NOT EXISTS idx_notas_asunto      ON asunto_notas(asunto_id);
CREATE TABLE IF NOT EXISTS cobro_pagos (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    cobro_id        INTEGER NOT NULL REFERENCES cobros(id) ON DELETE CASCADE,
    monto           REAL    NOT NULL,
    fecha           DATE    NOT NULL,
    metodo          TEXT,
    referencia      TEXT,
    comprobante_url TEXT,
    notas           TEXT,
    creado_por      INTEGER,
    fecha_alta      DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_cobro_pagos_cobro ON cobro_pagos(cobro_id);
CREATE INDEX IF NOT EXISTS idx_eventos_gcal      ON eventos_calendario(gcal_event_id);
CREATE TABLE IF NOT EXISTS biblioteca_carpetas (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    nombre      TEXT    NOT NULL,
    parent_id   INTEGER REFERENCES biblioteca_carpetas(id) ON DELETE CASCADE,
    creado_por  INTEGER,
    fecha_alta  DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS biblioteca_archivos (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    carpeta_id      INTEGER REFERENCES biblioteca_carpetas(id) ON DELETE SET NULL,
    nombre          TEXT    NOT NULL,
    nombre_original TEXT,
    archivo_url     TEXT    NOT NULL,
    tamanio         INTEGER DEFAULT 0,
    mime_type       TEXT,
    extension       TEXT,
    asunto_id       INTEGER REFERENCES asuntos(id),
    cliente_id      INTEGER REFERENCES clientes(id),
    creado_por      INTEGER,
    fecha_alta      DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS biblioteca_plantillas (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    nombre         TEXT    NOT NULL,
    categoria      TEXT    DEFAULT 'Otro',
    descripcion    TEXT,
    tipo_derecho   TEXT,
    archivo_url    TEXT,
    archivo_nombre TEXT,
    tamanio        INTEGER DEFAULT 0,
    extension      TEXT,
    es_custom      INTEGER DEFAULT 0,
    creado_por     INTEGER,
    fecha_alta     DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS biblioteca_documentos (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    titulo          TEXT    NOT NULL,
    contenido_html  TEXT,
    plantilla_id    INTEGER REFERENCES biblioteca_plantillas(id),
    asunto_id       INTEGER REFERENCES asuntos(id),
    cliente_id      INTEGER REFERENCES clientes(id),
    estado          TEXT    DEFAULT 'borrador',
    share_token     TEXT,
    share_expires   DATETIME,
    creado_por      INTEGER,
    fecha_alta      DATETIME DEFAULT CURRENT_TIMESTAMP,
    fecha_mod       DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS biblioteca_doc_versiones (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    documento_id   INTEGER NOT NULL REFERENCES biblioteca_documentos(id) ON DELETE CASCADE,
    contenido_html TEXT,
    version        INTEGER DEFAULT 1,
    creado_por     INTEGER,
    fecha          DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS biblioteca_compartidos (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tipo        TEXT    NOT NULL,
    entidad_id  INTEGER NOT NULL,
    token       TEXT    UNIQUE NOT NULL,
    expires_at  DATETIME NOT NULL,
    accesos     INTEGER DEFAULT 0,
    creado_por  INTEGER,
    fecha_alta  DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS biblioteca_acuerdos (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    tipo            TEXT    DEFAULT 'acuerdo',
    neun            TEXT,
    numero_asunto   TEXT,
    organo          TEXT,
    circuito        TEXT,
    autoridad       TEXT,
    fecha           DATE,
    descripcion     TEXT,
    asunto_id       INTEGER REFERENCES asuntos(id),
    creado_por      INTEGER,
    fecha_alta      DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_bib_archivos_carpeta ON biblioteca_archivos(carpeta_id);
CREATE INDEX IF NOT EXISTS idx_bib_archivos_asunto  ON biblioteca_archivos(asunto_id);
CREATE INDEX IF NOT EXISTS idx_bib_docs_asunto      ON biblioteca_documentos(asunto_id);
CREATE INDEX IF NOT EXISTS idx_bib_versiones_doc    ON biblioteca_doc_versiones(documento_id);
CREATE INDEX IF NOT EXISTS idx_bib_compartidos_token ON biblioteca_compartidos(token);
CREATE INDEX IF NOT EXISTS idx_bib_acuerdos_tipo    ON biblioteca_acuerdos(tipo);
CREATE TABLE IF NOT EXISTS reportes_carpetas (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    nombre      TEXT    NOT NULL,
    creado_por  INTEGER,
    fecha_alta  DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS reportes_guardados (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    nombre          TEXT    NOT NULL,
    tipo            TEXT    NOT NULL,
    filtros_json    TEXT    DEFAULT '{}',
    carpeta_id      INTEGER REFERENCES reportes_carpetas(id) ON DELETE SET NULL,
    programado      INTEGER DEFAULT 0,
    cron_expresion  TEXT,
    ultimo_envio    DATETIME,
    creado_por      INTEGER,
    fecha_alta      DATETIME DEFAULT CURRENT_TIMESTAMP,
    fecha_mod       DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_rpt_guardados_carpeta ON reportes_guardados(carpeta_id);
CREATE TABLE IF NOT EXISTS portal_clientes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    cliente_id      INTEGER NOT NULL REFERENCES clientes(id) ON DELETE CASCADE,
    email           TEXT    NOT NULL,
    password_hash   TEXT,
    magic_token     TEXT,
    magic_expires   DATETIME,
    activo          INTEGER DEFAULT 1,
    intentos_login  INTEGER DEFAULT 0,
    bloqueado_hasta DATETIME,
    ultimo_login    DATETIME,
    fecha_alta      DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(cliente_id)
);
CREATE TABLE IF NOT EXISTS portal_docs_compartidos (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    cliente_id      INTEGER NOT NULL REFERENCES clientes(id) ON DELETE CASCADE,
    titulo          TEXT    NOT NULL,
    descripcion     TEXT,
    archivo_url     TEXT,
    archivo_nombre  TEXT,
    tamanio         INTEGER DEFAULT 0,
    asunto_id       INTEGER REFERENCES asuntos(id),
    compartido_por  INTEGER,
    fecha           DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_portal_cli_cliente  ON portal_clientes(cliente_id);
CREATE INDEX IF NOT EXISTS idx_portal_cli_email    ON portal_clientes(email);
CREATE INDEX IF NOT EXISTS idx_portal_msg_cliente  ON portal_mensajes(cliente_id);
CREATE INDEX IF NOT EXISTS idx_portal_docs_cliente ON portal_docs_compartidos(cliente_id);
CREATE TABLE IF NOT EXISTS inv_colecciones (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    nombre      TEXT    NOT NULL,
    asunto_id   INTEGER REFERENCES asuntos(id) ON DELETE SET NULL,
    creado_por  INTEGER,
    fecha_alta  DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS inv_coleccion_items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    coleccion_id    INTEGER NOT NULL REFERENCES inv_colecciones(id) ON DELETE CASCADE,
    tipo            TEXT    NOT NULL,
    entidad_id      INTEGER,
    titulo          TEXT    NOT NULL,
    url             TEXT,
    notas           TEXT,
    fecha_alta      DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS inv_organos (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    nombre      TEXT    NOT NULL,
    circuito    TEXT,
    estado_mex  TEXT,
    tipo_organo TEXT,
    materia     TEXT,
    es_custom   INTEGER DEFAULT 0,
    creado_por  INTEGER,
    fecha_alta  DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS inv_busqueda_historial (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    termino     TEXT    NOT NULL,
    resultados  INTEGER DEFAULT 0,
    fecha       DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_inv_col_items       ON inv_coleccion_items(coleccion_id);
CREATE INDEX IF NOT EXISTS idx_inv_organos_materia ON inv_organos(materia);
CREATE INDEX IF NOT EXISTS idx_inv_hist_user       ON inv_busqueda_historial(user_id);

-- ── Etiquetas (Nota 4 — transversal) ───────────────────────────────────────
CREATE TABLE IF NOT EXISTS etiquetas (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    nombre      TEXT    NOT NULL,
    color       TEXT    DEFAULT '#6C5CE7',
    tipo        TEXT    DEFAULT 'custom'
                        CHECK(tipo IN ('estado','prioridad','accion','custom')),
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS asunto_etiquetas (
    asunto_id   INTEGER NOT NULL REFERENCES asuntos(id) ON DELETE CASCADE,
    etiqueta_id INTEGER NOT NULL REFERENCES etiquetas(id) ON DELETE CASCADE,
    PRIMARY KEY (asunto_id, etiqueta_id)
);
CREATE INDEX IF NOT EXISTS idx_asunto_etiquetas_asunto ON asunto_etiquetas(asunto_id);

-- ── Ficha Técnica Estratégica (Nota 9) ─────────────────────────────────────
CREATE TABLE IF NOT EXISTS ficha_tecnica (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    asunto_id           INTEGER NOT NULL UNIQUE REFERENCES asuntos(id) ON DELETE CASCADE,
    objetivo_cliente    TEXT,
    via_procesal        TEXT,
    justificacion_via   TEXT,
    argumentos_clave    TEXT    DEFAULT '[]',
    riesgos             TEXT    DEFAULT '[]',
    probabilidad_exito  TEXT    DEFAULT 'media'
                                CHECK(probabilidad_exito IN ('alta','media','baja','incierta')),
    insights            TEXT    DEFAULT '[]',
    updated_at          DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- ── Plazos unificados (Notas 2, 6, 8) ──────────────────────────────────────
CREATE TABLE IF NOT EXISTS plazos (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    asunto_id       INTEGER REFERENCES asuntos(id) ON DELETE CASCADE,
    titulo          TEXT    NOT NULL,
    descripcion     TEXT,
    fecha_limite    DATETIME NOT NULL,
    tipo            TEXT    DEFAULT 'interno'
                            CHECK(tipo IN ('fatal','procesal','interno','deseable')),
    responsable_id  INTEGER,
    estado          TEXT    DEFAULT 'pendiente'
                            CHECK(estado IN ('pendiente','completado','vencido')),
    entidad_tipo    TEXT,
    entidad_id      INTEGER,
    alertas_config  TEXT    DEFAULT '[{"horas_antes":72,"canal":"sistema"},{"horas_antes":24,"canal":"sistema"}]',
    completado_at   DATETIME,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_plazos_asunto ON plazos(asunto_id);
CREATE INDEX IF NOT EXISTS idx_plazos_fecha  ON plazos(fecha_limite);
CREATE INDEX IF NOT EXISTS idx_plazos_estado ON plazos(estado);

-- ── Machotes / Plantillas (Notas 5, 7) ─────────────────────────────────────
CREATE TABLE IF NOT EXISTS machotes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    nombre          TEXT    NOT NULL,
    materia         TEXT,
    tipo_escrito    TEXT,
    contenido       TEXT,
    variables       TEXT    DEFAULT '[]',
    es_del_sistema  INTEGER DEFAULT 0,
    creado_por      INTEGER,
    veces_usado     INTEGER DEFAULT 0,
    version         TEXT    DEFAULT '1.0',
    tags            TEXT    DEFAULT '[]',
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS contrapartes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    nombre          TEXT    NOT NULL,
    tipo            TEXT    DEFAULT 'persona_fisica',
    rfc             TEXT,
    email           TEXT,
    telefono        TEXT,
    abogado_nombre  TEXT,
    abogado_email   TEXT,
    despacho        TEXT,
    ciudad          TEXT,
    notas           TEXT,
    activo          INTEGER DEFAULT 1,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS contraparte_asunto (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    contraparte_id  INTEGER REFERENCES contrapartes(id),
    asunto_id       INTEGER REFERENCES asuntos(id),
    resultado       TEXT    DEFAULT 'en_proceso',
    notas           TEXT,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);
"""


def migrate_tenant_db(slug: str):
    """Aplica migraciones de esquema a una DB de tenant existente (idempotente)."""
    conn = get_tenant_conn(slug)
    for table, col, coldef in _MIGRATE_COLS:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coldef}")
        except Exception:
            pass  # columna ya existe
    conn.executescript(_MIGRATE_TABLES)
    conn.commit()
    conn.close()


# ─────────────────────────────────────────────────────────────────────────────
#  SEEDS
# ─────────────────────────────────────────────────────────────────────────────

TENANTS_SEED = [
    {
        "slug": "bufete-ramirez",
        "nombre_despacho": "Bufete Jurídico Ramírez & Asociados",
        "plan": "profesional",
        "email_contacto": "contacto@bufete-ramirez.lexos.mx",
        "telefono": "555-100-2000",
        "direccion": "Paseo de la Reforma 250, Piso 12, CDMX",
        "rfc": "BJR860412AB1",
        "color_primario": "#00DFA2",
        "users": [
            {"nombre": "Ricardo", "apellidos": "Ramírez Vega", "email": "r.ramirez@bufete-ramirez.lexos.mx", "password": "Admin123!", "rol": "super_admin"},
            {"nombre": "Sofía", "apellidos": "Mendoza Torres", "email": "s.mendoza@bufete-ramirez.lexos.mx", "password": "Admin123!", "rol": "admin"},
            {"nombre": "Luis", "apellidos": "Guerrero Solís", "email": "l.guerrero@bufete-ramirez.lexos.mx", "password": "Abogado1!", "rol": "abogado"},
            {"nombre": "Mariana", "apellidos": "Ríos Castillo", "email": "m.rios@bufete-ramirez.lexos.mx", "password": "Abogado1!", "rol": "abogado"},
            {"nombre": "Carmen", "apellidos": "López Fuentes", "email": "c.lopez@bufete-ramirez.lexos.mx", "password": "Asist001!", "rol": "asistente"},
        ],
    },
    {
        "slug": "silva-consultores",
        "nombre_despacho": "Silva & Consultores Legales S.C.",
        "plan": "basico",
        "email_contacto": "info@silva-consultores.lexos.mx",
        "telefono": "33-3800-9900",
        "direccion": "Av. Vallarta 3233, Guadalajara, Jalisco",
        "rfc": "SCL910815CD2",
        "color_primario": "#0EA5E9",
        "users": [
            {"nombre": "Alejandro", "apellidos": "Silva Morales", "email": "a.silva@silva-consultores.lexos.mx", "password": "Admin123!", "rol": "super_admin"},
            {"nombre": "Patricia", "apellidos": "Núñez Bravo", "email": "p.nunez@silva-consultores.lexos.mx", "password": "Abogado1!", "rol": "abogado"},
            {"nombre": "Ernesto", "apellidos": "Vargas Peña", "email": "e.vargas@silva-consultores.lexos.mx", "password": "Asist001!", "rol": "asistente"},
        ],
    },
]

def seed_global(conn, tenants_data: list) -> dict:
    """Inserta tenants y usuarios globales, retorna {slug: tenant_id}."""
    id_map = {}
    for t in tenants_data:
        cur = conn.execute(
            """INSERT OR IGNORE INTO tenants
               (slug, nombre_despacho, plan, email_contacto, telefono, direccion, rfc, color_primario)
               VALUES (?,?,?,?,?,?,?,?)""",
            (t["slug"], t["nombre_despacho"], t["plan"],
             t["email_contacto"], t["telefono"], t["direccion"],
             t["rfc"], t["color_primario"]),
        )
        tenant_id = cur.lastrowid or conn.execute(
            "SELECT id FROM tenants WHERE slug=?", (t["slug"],)
        ).fetchone()[0]
        id_map[t["slug"]] = tenant_id

        for u in t["users"]:
            conn.execute(
                """INSERT OR IGNORE INTO tenant_users
                   (tenant_id, nombre, apellidos, email, password_hash, rol)
                   VALUES (?,?,?,?,?,?)""",
                (tenant_id, u["nombre"], u["apellidos"], u["email"],
                 hash_password(u["password"]), u["rol"]),
            )
    conn.commit()
    return id_map


def seed_tenant(slug: str, tenant_global_id: int):
    """Inserta datos de prueba en la DB del tenant."""
    conn = get_tenant_conn(slug)
    gconn = get_global_conn()
    users = gconn.execute(
        "SELECT * FROM tenant_users WHERE tenant_id=?", (tenant_global_id,)
    ).fetchall()
    gconn.close()

    # Mapeo nombre→id para referencias cruzadas
    abogados = [u for u in users if u["rol"] in ("abogado", "admin", "super_admin")]
    abogado_id = abogados[0]["id"] if abogados else 1

    today = date.today()

    # ── Clientes ──────────────────────────────────────────────────────────────
    if slug == "bufete-ramirez":
        clientes_data = [
            ("persona_fisica", "Héctor", "Fuentes Alvarado", None, "FUAH750310MX1", "hector.fuentes@gmail.com", "5512345678", "Insurgentes Sur 1234, CDMX"),
            ("persona_fisica", "Gabriela", "Moreno Ruiz", None, "MORG880921MX2", "gmoreno.legal@outlook.com", "5523456789", "Polanco, CDMX"),
            ("persona_moral", "Constructora", "Omega S.A. de C.V.", "Constructora Omega S.A. de C.V.", None, "contacto@omega.com.mx", "5598765432", "Av. Insurgentes Norte 400, CDMX"),
            ("persona_fisica", "Roberto", "Salinas Espinoza", None, "SAER920615MX3", "r.salinas@empresa.mx", "5534567890", "Santa Fe, CDMX"),
            ("persona_moral", "Inversiones", "Del Pacífico S.C.", "Inversiones Del Pacífico S.C.", None, "legal@delpac.mx", "5545678901", "Lomas de Chapultepec, CDMX"),
        ]
    else:
        clientes_data = [
            ("persona_fisica", "Jorge", "Medina Castañeda", None, "MECJ840712MX1", "jmedina@hotmail.com", "3312345678", "Zapopan, Jalisco"),
            ("persona_fisica", "Ana", "Torres Villalobos", None, "TOVA910330MX2", "a.torres@gmail.com", "3323456789", "Guadalajara, Jalisco"),
            ("persona_moral", "Grupo", "Comercial Jalisco S.A.", "Grupo Comercial Jalisco S.A.", None, "legal@gcj.mx", "3398765432", "Tlaquepaque, Jalisco"),
            ("persona_fisica", "Miguel", "Hernández Olvera", None, "HEOM780520MX3", "m.hernandez@yahoo.mx", "3334567890", "Tonalá, Jalisco"),
            ("persona_moral", "Tech", "Solutions GDL S.C.", "Tech Solutions GDL S.C.", None, "admin@techgdl.mx", "3345678901", "Zona Industrial, Guadalajara"),
        ]

    cliente_ids = []
    for c in clientes_data:
        cur = conn.execute(
            """INSERT INTO clientes
               (tipo, nombre, apellidos, razon_social, curp, email, celular, direccion, creado_por)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (*c, abogado_id),
        )
        cliente_ids.append(cur.lastrowid)

    # ── Asuntos ───────────────────────────────────────────────────────────────
    if slug == "bufete-ramirez":
        asuntos_data = [
            ("EXP-2024-001", "Divorcio Contencioso Fuentes-Gómez", "Juicio de divorcio con disputa de bienes y custodia.",
             "familiar", "activo", "alta", cliente_ids[0], f"{today.year-1}-03-15", 185000.0),
            ("EXP-2024-002", "Demanda Mercantil Constructora Omega",
             "Reclamación por incumplimiento de contrato de obra.",
             "mercantil", "activo", "alta", cliente_ids[2], f"{today.year-1}-06-01", 650000.0),
            ("EXP-2025-001", "Amparo Administrativo — Moreno Ruiz",
             "Juicio de amparo contra resolución fiscal del SAT.",
             "amparo", "activo", "media", cliente_ids[1], f"{today.year}-01-10", 95000.0),
        ]
    else:
        asuntos_data = [
            ("EXP-2024-A01", "Rescisión Contrato Arrendamiento — Medina",
             "Rescisión de contrato de arrendamiento comercial por falta de pago.",
             "civil", "activo", "alta", cliente_ids[0], f"{today.year-1}-07-20", 120000.0),
            ("EXP-2024-A02", "Conflicto Laboral Grupo Comercial Jalisco",
             "Defensa ante demanda colectiva de 12 trabajadores.",
             "laboral", "activo", "alta", cliente_ids[2], f"{today.year-1}-09-01", 280000.0),
            ("EXP-2025-A01", "Constitución Sociedad — Tech Solutions GDL",
             "Asesoría y trámites para constitución de S.C.",
             "mercantil", "cerrado", "baja", cliente_ids[4], f"{today.year}-02-01", 45000.0),
        ]

    asunto_ids = []
    for a in asuntos_data:
        cur = conn.execute(
            """INSERT INTO asuntos
               (numero_expediente, titulo, descripcion, tipo_asunto, estado, prioridad,
                cliente_id, abogado_responsable, fecha_inicio, honorarios_pactados, creado_por)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (*a[:8], abogado_id, a[8], abogado_id),
        )
        asunto_ids.append(cur.lastrowid)

    # ── Cobros ────────────────────────────────────────────────────────────────
    cobros_data = []
    for i, aid in enumerate(asunto_ids):
        cid = asuntos_data[i][6]  # cliente_id del asunto
        cobros_data += [
            (aid, cid, "Anticipo de honorarios", 25000.0, "pagado",
             str(today - timedelta(days=90)), str(today - timedelta(days=75)),
             str(today - timedelta(days=80)), "transferencia"),
            (aid, cid, "Segunda exhibición de honorarios", 20000.0, "pendiente",
             str(today - timedelta(days=30)), str(today + timedelta(days=15)),
             None, None),
        ]

    for c in cobros_data:
        conn.execute(
            """INSERT INTO cobros
               (asunto_id, cliente_id, concepto, monto, estado,
                fecha_emision, fecha_vencimiento, fecha_pago, metodo_pago, creado_por)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (*c, abogado_id),
        )

    # ── Eventos de calendario ─────────────────────────────────────────────────
    eventos = [
        ("Audiencia inicial — " + asuntos_data[0][1], "audiencia", asunto_ids[0],
         str(today + timedelta(days=7)) + " 10:00:00"),
        ("Plazo para contestar demanda", "plazo", asunto_ids[1],
         str(today + timedelta(days=3)) + " 09:00:00"),
        ("Reunión con cliente — revisión estrategia", "reunion", asunto_ids[2],
         str(today + timedelta(days=2)) + " 16:00:00"),
    ]
    for e in eventos:
        conn.execute(
            """INSERT INTO eventos_calendario
               (titulo, tipo, asunto_id, fecha_inicio, creado_por)
               VALUES (?,?,?,?,?)""",
            (*e, abogado_id),
        )

    # ── Notas ─────────────────────────────────────────────────────────────────
    conn.execute(
        """INSERT INTO notas (asunto_id, titulo, contenido, tipo, creado_por)
           VALUES (?,?,?,?,?)""",
        (asunto_ids[0], "Estrategia inicial",
         "El cliente prefiere llegar a un acuerdo respecto a la custodia. Explorar mediación.",
         "memo", abogado_id),
    )

    # ── Tareas ────────────────────────────────────────────────────────────────
    conn.execute(
        """INSERT INTO tareas
           (titulo, asunto_id, asignado_a, prioridad, estado, fecha_vencimiento, creado_por)
           VALUES (?,?,?,?,?,?,?)""",
        ("Preparar escrito inicial de demanda", asunto_ids[0], abogado_id,
         "alta", "en_proceso", str(today + timedelta(days=5)), abogado_id),
    )

    conn.commit()
    conn.close()
    print(f"     ✓ Seeds insertados en tenant '{slug}'")


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]
    reset = "--reset" in args
    seed_only = "--seed-only" in args

    if reset:
        print("⚠  --reset: eliminando bases de datos existentes...")
        for f in os.listdir(Config.DATABASE_DIR):
            if f.endswith(".db"):
                os.remove(os.path.join(Config.DATABASE_DIR, f))
                print(f"   eliminado: {f}")

    if not seed_only:
        print("\n[1/3] Creando esquemas...")
        init_global()
        for t in TENANTS_SEED:
            init_tenant(t["slug"])

    print("\n[2/3] Insertando seeds globales...")
    gconn = get_global_conn()
    id_map = seed_global(gconn, TENANTS_SEED)
    gconn.close()
    print(f"     ✓ {len(TENANTS_SEED)} tenants")

    print("\n[3/3] Insertando seeds por tenant...")
    for t in TENANTS_SEED:
        seed_tenant(t["slug"], id_map[t["slug"]])

    print("\n✅  LexOS — base de datos lista.")
    print("\nCredenciales de prueba:")
    for t in TENANTS_SEED:
        print(f"\n  Tenant: {t['slug']}")
        for u in t["users"]:
            print(f"    {u['rol']:12}  {u['email']}  /  {u['password']}")


if __name__ == "__main__":
    main()
