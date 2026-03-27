"""
LexOS — modules/onboarding.py
Tour de onboarding de 4 fases con progreso persistente y badge PDF.

Rutas:
  GET  /onboarding/tour          — Página principal del tour
  POST /onboarding/progress      — AJAX: actualizar fase/paso
  POST /onboarding/complete      — Marcar tour como completado
  GET  /onboarding/badge.pdf     — Descargar badge en PDF
  POST /onboarding/skip          — Saltar el tour completo
"""

import io
import json
from datetime import date, datetime

from flask import (
    Blueprint, render_template, request, redirect,
    url_for, session, flash, g, jsonify, make_response
)
from modules.auth import login_required

onboarding_bp = Blueprint("onboarding", __name__)


# ─── Definición de las fases ──────────────────────────────────────────────────

FASES = [
    {
        "id": 0,
        "slug": "configuracion",
        "titulo": "Configuración Base",
        "icono": "settings",
        "color": "#00DFA2",
        "pasos": [
            {
                "id": 0,
                "titulo": "Identidad del despacho",
                "subtitulo": "Personaliza tu espacio de trabajo",
                "descripcion": (
                    "Sube el logo de tu despacho y configura tu información de contacto. "
                    "Una identidad visual sólida genera confianza en tus clientes."
                ),
                "consejo": "Los despachos con logo completo reciben un 40% más de confianza inicial.",
                "accion_label": "Subir logo y configurar",
                "accion_url": "/auth/perfil",
                "omitible": True,
                "ilustracion": "logo",
            },
            {
                "id": 1,
                "titulo": "Invita a tu equipo",
                "subtitulo": "Colaboración desde el primer día",
                "descripcion": (
                    "Agrega a tus socios, abogados y asistentes. Asigna roles precisos: "
                    "admin, abogado o asistente. Cada rol tiene permisos ajustados a sus responsabilidades."
                ),
                "consejo": "Define roles claros desde el inicio para proteger la información sensible.",
                "accion_label": "Agregar colaboradores",
                "accion_url": "/clientes/",
                "omitible": False,
                "ilustracion": "equipo",
            },
        ],
    },
    {
        "id": 1,
        "slug": "primer_cliente",
        "titulo": "Primer Cliente",
        "icono": "user",
        "color": "#10B981",
        "pasos": [
            {
                "id": 0,
                "titulo": "Registra tu primer cliente",
                "subtitulo": "El núcleo de tu práctica legal",
                "descripcion": (
                    "Agrega los datos completos de tu primer cliente. "
                    "Persona física o moral, con RFC, contacto y dirección. "
                    "Todos los asuntos y cobros quedan vinculados a este perfil."
                ),
                "consejo": "Un expediente bien documentado desde el inicio ahorra horas de búsqueda.",
                "accion_label": "Registrar cliente",
                "accion_url": "/clientes/nuevo",
                "omitible": False,
                "ilustracion": "cliente",
            },
            {
                "id": 1,
                "titulo": "Crea el primer asunto",
                "subtitulo": "Tu primer expediente digital",
                "descripcion": (
                    "Vincula un asunto al cliente que acabas de registrar. "
                    "Asigna tipo (civil, mercantil, penal…), juzgado, número de juicio "
                    "y los honorarios pactados."
                ),
                "consejo": "Usa expedientes separados aunque un cliente tenga múltiples casos.",
                "accion_label": "Crear asunto",
                "accion_url": "/asuntos/nuevo",
                "omitible": False,
                "ilustracion": "asunto",
            },
        ],
    },
    {
        "id": 2,
        "slug": "primer_cobro",
        "titulo": "Primer Cobro",
        "icono": "dollar",
        "color": "#F59E0B",
        "pasos": [
            {
                "id": 0,
                "titulo": "Registra el anticipo de honorarios",
                "subtitulo": "Controla tus finanzas desde el día uno",
                "descripcion": (
                    "Crea el primer cobro vinculado al asunto. "
                    "Establece concepto, monto, fecha de vencimiento y método de pago. "
                    "LexOS lleva el control automático de pendientes y vencidos."
                ),
                "consejo": "Registra anticipos aunque sean verbales — documenta todo.",
                "accion_label": "Registrar cobro",
                "accion_url": "/cobranzas/nuevo",
                "omitible": True,
                "ilustracion": "cobro",
            },
            {
                "id": 1,
                "titulo": "Notifica a tu cliente",
                "subtitulo": "Comunicación transparente y profesional",
                "descripcion": (
                    "El portal de clientes de LexOS permite que tu cliente vea "
                    "el estado de su asunto y sus cobros en tiempo real. "
                    "Genera su acceso con un clic."
                ),
                "consejo": "Los clientes informados generan menos llamadas de seguimiento.",
                "accion_label": "Ir al Portal Clientes",
                "accion_url": "/portal/",
                "omitible": True,
                "ilustracion": "portal",
            },
        ],
    },
    {
        "id": 3,
        "slug": "productividad",
        "titulo": "Productividad",
        "icono": "zap",
        "color": "#3B82F6",
        "pasos": [
            {
                "id": 0,
                "titulo": "Agenda tu primera audiencia",
                "subtitulo": "Nunca más pierdas un plazo",
                "descripcion": (
                    "El calendario de LexOS muestra audiencias, plazos y reuniones "
                    "con código de colores. Programa un evento y asígnalo al asunto."
                ),
                "consejo": "Activa recordatorios 24h antes para audiencias críticas.",
                "accion_label": "Ir al Calendario",
                "accion_url": "/calendario/",
                "omitible": True,
                "ilustracion": "calendario",
            },
            {
                "id": 1,
                "titulo": "Explora la Biblioteca Legal",
                "subtitulo": "Conocimiento a un clic",
                "descripcion": (
                    "Guarda leyes, tesis jurisprudenciales y templates de escritos "
                    "en tu biblioteca privada. Búsqueda instantánea cuando más lo necesitas."
                ),
                "consejo": "Crea templates de escritos frecuentes para reducir tiempos.",
                "accion_label": "Abrir Biblioteca",
                "accion_url": "/biblioteca/",
                "omitible": True,
                "ilustracion": "biblioteca",
            },
        ],
    },
]

TOTAL_PASOS = sum(len(f["pasos"]) for f in FASES)


def _tdb():
    return getattr(g, "tenant_db", None)


def _get_progress(user_id: int):
    tdb = _tdb()
    if not tdb:
        return None
    row = tdb.execute(
        "SELECT * FROM onboarding_progress WHERE user_id=?", (user_id,)
    ).fetchone()
    if not row:
        tdb.execute("INSERT INTO onboarding_progress (user_id) VALUES (?)", (user_id,))
        tdb.commit()
        row = tdb.execute(
            "SELECT * FROM onboarding_progress WHERE user_id=?", (user_id,)
        ).fetchone()
    return row


def _pasos_completados(fases_completadas_json: str) -> int:
    try:
        fases = json.loads(fases_completadas_json or "[]")
        return sum(len(FASES[f]["pasos"]) for f in fases if f < len(FASES))
    except Exception:
        return 0


# ─── Rutas ────────────────────────────────────────────────────────────────────

@onboarding_bp.route("/tour")
@login_required
def tour():
    user_id = session["user_id"]
    progress = _get_progress(user_id)

    if progress and progress["completado"]:
        flash("Ya completaste la configuración inicial. ¡Bienvenido!", "success")
        return redirect(url_for("dashboard.index"))

    fase_actual = progress["fase_actual"] if progress else 0
    paso_actual = progress["paso_actual"] if progress else 0
    fases_completadas = json.loads(progress["fases_completadas"] if progress else "[]")

    # Calcular porcentaje
    pasos_hechos = _pasos_completados(progress["fases_completadas"] if progress else "[]")
    pct = int((pasos_hechos / TOTAL_PASOS) * 100) if TOTAL_PASOS else 0

    fase = FASES[min(fase_actual, len(FASES) - 1)]
    paso = fase["pasos"][min(paso_actual, len(fase["pasos"]) - 1)]

    return render_template(
        "onboarding/tour.html",
        fases=FASES,
        fase=fase,
        paso=paso,
        fase_actual=fase_actual,
        paso_actual=paso_actual,
        fases_completadas=fases_completadas,
        porcentaje=pct,
        total_pasos=TOTAL_PASOS,
        pasos_hechos=pasos_hechos,
    )


@onboarding_bp.route("/progress", methods=["POST"])
@login_required
def save_progress():
    """AJAX: guarda el progreso del tour."""
    data = request.get_json(silent=True) or request.form
    user_id = session["user_id"]
    tdb = _tdb()
    if not tdb:
        return jsonify({"ok": False, "error": "No DB"}), 500

    fase_nueva = int(data.get("fase", 0))
    paso_nuevo  = int(data.get("paso", 0))

    progress = _get_progress(user_id)
    fases_completadas = json.loads(progress["fases_completadas"] if progress else "[]")

    # Marcar la fase anterior como completada si avanzamos
    fase_anterior = progress["fase_actual"] if progress else 0
    if fase_nueva > fase_anterior and fase_anterior not in fases_completadas:
        fases_completadas.append(fase_anterior)

    tdb.execute(
        """UPDATE onboarding_progress
           SET fase_actual=?, paso_actual=?, fases_completadas=?
           WHERE user_id=?""",
        (fase_nueva, paso_nuevo, json.dumps(fases_completadas), user_id),
    )
    tdb.commit()

    pasos_hechos = _pasos_completados(json.dumps(fases_completadas))
    pct = int((pasos_hechos / TOTAL_PASOS) * 100) if TOTAL_PASOS else 0

    return jsonify({
        "ok": True,
        "fase": fase_nueva,
        "paso": paso_nuevo,
        "porcentaje": pct,
        "fases_completadas": fases_completadas,
    })


@onboarding_bp.route("/complete", methods=["POST"])
@login_required
def complete():
    """Marca el onboarding como completado."""
    user_id = session["user_id"]
    tdb = _tdb()
    if not tdb:
        return jsonify({"ok": False}), 500

    fases_todas = list(range(len(FASES)))
    tdb.execute(
        """UPDATE onboarding_progress
           SET completado=1, fecha_completado=CURRENT_TIMESTAMP,
               fases_completadas=?
           WHERE user_id=?""",
        (json.dumps(fases_todas), user_id),
    )
    tdb.commit()

    if request.is_json:
        return jsonify({"ok": True, "redirect": url_for("onboarding.badge_pdf")})
    return redirect(url_for("onboarding.badge_pdf"))


@onboarding_bp.route("/skip", methods=["POST"])
@login_required
def skip():
    """Salta el tour completo."""
    user_id = session["user_id"]
    tdb = _tdb()
    if tdb:
        tdb.execute(
            "UPDATE onboarding_progress SET completado=1 WHERE user_id=?",
            (user_id,),
        )
        tdb.commit()
    flash("Puedes retomar el tour desde el menú en cualquier momento.", "info")
    return redirect(url_for("dashboard.index"))


@onboarding_bp.route("/badge.pdf")
@login_required
def badge_pdf():
    """Genera y descarga el badge 'Despacho Digital' en PDF."""
    tenant = getattr(g, "tenant", None)
    user = session.get("user", {})
    nombre_despacho = tenant["nombre_despacho"] if tenant else "Mi Despacho"
    nombre_usuario  = f"{user.get('nombre','')} {user.get('apellidos','')}".strip()
    hoy = date.today().strftime("%d de %B de %Y")

    try:
        from reportlab.lib.pagesizes import letter
        from reportlab.lib import colors
        from reportlab.lib.units import inch
        from reportlab.pdfgen import canvas as rl_canvas
        from reportlab.lib.colors import HexColor

        buf = io.BytesIO()
        w, h = letter
        c = rl_canvas.Canvas(buf, pagesize=letter)

        # Fondo oscuro
        c.setFillColor(HexColor("#0F1117"))
        c.rect(0, 0, w, h, fill=1, stroke=0)

        # Marco decorativo
        c.setStrokeColor(HexColor("#00DFA2"))
        c.setLineWidth(3)
        c.roundRect(0.5 * inch, 0.5 * inch, w - inch, h - inch, 12, fill=0, stroke=1)

        # Línea interna
        c.setLineWidth(1)
        c.setStrokeColor(HexColor("#2A2D3E"))
        c.roundRect(0.65 * inch, 0.65 * inch, w - 1.3 * inch, h - 1.3 * inch, 8, fill=0, stroke=1)

        # Logo LexOS
        c.setFillColor(HexColor("#00DFA2"))
        c.roundRect(w / 2 - 32, h - 2.2 * inch, 64, 64, 8, fill=1, stroke=0)
        c.setFillColor(HexColor("#FFFFFF"))
        c.setFont("Helvetica-Bold", 26)
        c.drawCentredString(w / 2, h - 2.2 * inch + 18, "Lx")

        # Título LexOS
        c.setFont("Helvetica-Bold", 28)
        c.setFillColor(HexColor("#F1F2F7"))
        c.drawCentredString(w / 2, h - 2.8 * inch, "LexOS")
        c.setFont("Helvetica", 11)
        c.setFillColor(HexColor("#6B7280"))
        c.drawCentredString(w / 2, h - 3.1 * inch, "Plataforma Legal Multi-tenant")

        # Badge
        c.setFillColor(HexColor("#00DFA2"))
        c.roundRect(w / 2 - 1.4 * inch, h - 4.2 * inch, 2.8 * inch, 0.55 * inch, 20, fill=1, stroke=0)
        c.setFillColor(HexColor("#FFFFFF"))
        c.setFont("Helvetica-Bold", 14)
        c.drawCentredString(w / 2, h - 3.95 * inch, "⭐  Despacho Digital")

        # Nombre despacho
        c.setFont("Helvetica-Bold", 22)
        c.setFillColor(HexColor("#F1F2F7"))
        c.drawCentredString(w / 2, h - 5.0 * inch, nombre_despacho)

        c.setFont("Helvetica", 13)
        c.setFillColor(HexColor("#9CA3AF"))
        c.drawCentredString(w / 2, h - 5.4 * inch,
                            f"Administrado por {nombre_usuario}")

        # Línea separadora
        c.setStrokeColor(HexColor("#2A2D3E"))
        c.setLineWidth(1)
        c.line(1.5 * inch, h - 5.8 * inch, w - 1.5 * inch, h - 5.8 * inch)

        # Texto de certificación
        c.setFont("Helvetica", 10)
        c.setFillColor(HexColor("#6B7280"))
        lines = [
            "Este despacho ha completado exitosamente el proceso de",
            "configuración inicial de LexOS y está listo para operar",
            "de forma 100% digital.",
        ]
        y_txt = h - 6.2 * inch
        for line in lines:
            c.drawCentredString(w / 2, y_txt, line)
            y_txt -= 0.22 * inch

        # Fecha
        c.setFont("Helvetica-Bold", 11)
        c.setFillColor(HexColor("#00DFA2"))
        c.drawCentredString(w / 2, h - 7.1 * inch, hoy)

        # Footer
        c.setFont("Helvetica", 9)
        c.setFillColor(HexColor("#374151"))
        c.drawCentredString(w / 2, 0.9 * inch, "lexos.mx — Tecnología legal para México")

        c.save()
        buf.seek(0)

        response = make_response(buf.read())
        response.headers["Content-Type"] = "application/pdf"
        response.headers["Content-Disposition"] = (
            f'attachment; filename="lexos-badge-{tenant["slug"] if tenant else "despacho"}.pdf"'
        )
        return response

    except ImportError:
        flash("ReportLab no está instalado. Ejecuta: pip install reportlab", "danger")
        return redirect(url_for("dashboard.index"))
