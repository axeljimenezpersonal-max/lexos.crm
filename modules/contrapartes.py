"""
LexOS — modules/contrapartes.py
Gestión de contrapartes: listado con panel de inteligencia estratégica,
creación rápida y vinculación con asuntos.
"""

from flask import Blueprint, g, jsonify, render_template, request

from modules.auth import login_required, tenant_required

contrapartes_bp = Blueprint("contrapartes", __name__)


@contrapartes_bp.route("/")
@login_required
@tenant_required
def index():
    tdb = g.tenant_db
    q = request.args.get("q", "").strip()
    cp_id = request.args.get("cp", "").strip()

    contrapartes = tdb.execute(
        """
        SELECT c.*,
          COUNT(DISTINCT ca.asunto_id) as total_casos,
          SUM(CASE WHEN ca.resultado='favorable'    THEN 1 ELSE 0 END) as favorables,
          SUM(CASE WHEN ca.resultado='desfavorable' THEN 1 ELSE 0 END) as desfavorables,
          SUM(CASE WHEN ca.resultado='en_proceso'   THEN 1 ELSE 0 END) as en_proceso
        FROM contrapartes c
        LEFT JOIN contraparte_asunto ca ON ca.contraparte_id = c.id
        WHERE c.activo = 1
          AND (? = '' OR c.nombre LIKE ? OR c.despacho LIKE ? OR c.abogado_nombre LIKE ?)
        GROUP BY c.id
        ORDER BY total_casos DESC, c.nombre ASC
        """,
        (q, f"%{q}%", f"%{q}%", f"%{q}%"),
    ).fetchall()

    seleccionada = None
    asuntos_cp = []
    if cp_id:
        seleccionada = tdb.execute(
            "SELECT * FROM contrapartes WHERE id=? AND activo=1", (cp_id,)
        ).fetchone()
        if seleccionada:
            asuntos_cp = tdb.execute(
                """
                SELECT a.nombre, a.tipo_asunto, a.estado,
                       ca.resultado, a.id as asunto_id,
                       a.fecha_inicio, a.fecha_limite
                FROM contraparte_asunto ca
                JOIN asuntos a ON a.id = ca.asunto_id
                WHERE ca.contraparte_id = ?
                ORDER BY a.fecha_inicio DESC
                """,
                (cp_id,),
            ).fetchall()

    return render_template(
        "contrapartes/index.html",
        contrapartes=contrapartes,
        seleccionada=seleccionada,
        asuntos_cp=asuntos_cp,
        q=q,
        cp_id=cp_id,
        total=len(contrapartes),
    )


@contrapartes_bp.route("/crear", methods=["POST"])
@login_required
@tenant_required
def crear():
    d = request.get_json() or {}
    nombre = (d.get("nombre") or "").strip()
    if not nombre:
        return jsonify(ok=False, error="Nombre requerido"), 400

    cur = g.tenant_db.execute(
        """
        INSERT INTO contrapartes
          (nombre, tipo, rfc, email, telefono,
           abogado_nombre, abogado_email, despacho, ciudad, notas)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (
            nombre,
            d.get("tipo", "persona_fisica"),
            (d.get("rfc") or "").upper().strip() or None,
            (d.get("email") or "").strip() or None,
            (d.get("telefono") or "").strip() or None,
            (d.get("abogado_nombre") or "").strip() or None,
            (d.get("abogado_email") or "").strip() or None,
            (d.get("despacho") or "").strip() or None,
            (d.get("ciudad") or "").strip() or None,
            (d.get("notas") or "").strip() or None,
        ),
    )
    g.tenant_db.commit()
    return jsonify(ok=True, id=cur.lastrowid)


@contrapartes_bp.route("/<int:id>/vincular", methods=["POST"])
@login_required
@tenant_required
def vincular_asunto(id):
    d = request.get_json() or {}
    asunto_id = d.get("asunto_id")
    if not asunto_id:
        return jsonify(ok=False, error="asunto_id requerido"), 400

    g.tenant_db.execute(
        """
        INSERT OR REPLACE INTO contraparte_asunto
          (contraparte_id, asunto_id, resultado, notas)
        VALUES (?,?,?,?)
        """,
        (id, asunto_id, d.get("resultado", "en_proceso"), d.get("notas", "")),
    )
    g.tenant_db.commit()
    return jsonify(ok=True)
