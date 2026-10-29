"""
Habeas Corpus — modules/contrapartes.py
Contrapartes: las que se registran a mano y las que ya aparecen en los expedientes
(campo «contraparte»), juntas, con el historial del despacho contra cada una y sus abogados.
"""

from flask import Blueprint, g, jsonify, render_template, request, session, url_for

from modules.asuntos import ABIERTOS, TIPOS_DERECHO, _FILTRO_PROPIOS, _solo_lo_suyo
from modules.auth import login_required, tenant_required
from modules.buscador import normalizar
from modules.insights.casos import RESULTADOS

contrapartes_bp = Blueprint("contrapartes", __name__)

_COLORES = ("#60718D", "#8A6A2F", "#4A5A73", "#6E5A3A", "#34496B", "#9A7536")


def _clave(nombre: str) -> str:
    """Mismo nombre aunque cambien acentos, comas o «S.A. de C.V.»."""
    n = normalizar(nombre).replace(",", " ").replace(".", " ")
    return " ".join(n.split())


def _iniciales(nombre: str) -> str:
    partes = [p for p in nombre.replace(",", " ").split() if p[:1].isalpha()]
    return ((partes[0][0] + partes[1][0]) if len(partes) > 1 else nombre[:2]).upper()


def _contrapartes(tdb) -> list[dict]:
    fichas: dict[str, dict] = {}

    def ficha(nombre: str) -> dict:
        k = _clave(nombre)
        if k not in fichas:
            fichas[k] = {"nombre": nombre.strip(), "id": None, "tipo": "", "abogados": set(), "despachos": set(),
                         "asuntos": [], "ciudad": "", "telefono": "", "email": "", "notas": "", "rfc": ""}
        return fichas[k]

    for r in tdb.execute("SELECT * FROM contrapartes WHERE activo=1").fetchall():
        f = ficha(r["nombre"])
        f.update({"id": r["id"], "tipo": r["tipo"] or "", "ciudad": r["ciudad"] or "", "telefono": r["telefono"] or "",
                  "email": r["email"] or "", "notas": r["notas"] or "", "rfc": r["rfc"] or ""})
        if r["abogado_nombre"]:
            f["abogados"].add(r["abogado_nombre"].strip())
        if r["despacho"]:
            f["despachos"].add(r["despacho"].strip())

    alc, params = "1", []
    if _solo_lo_suyo():
        uid = session.get("user_id")
        alc, params = _FILTRO_PROPIOS, [uid, uid, uid]
    for a in tdb.execute(
        f"""SELECT a.id, a.titulo, a.numero_expediente, a.estado, a.resultado, a.tipo_asunto,
                   a.contrario_nombre, a.contrario_abogado, a.contrario_despacho, a.fecha_alta
            FROM asuntos a WHERE COALESCE(a.contrario_nombre, '') != '' AND {alc}
            ORDER BY a.fecha_alta DESC""", params).fetchall():
        f = ficha(a["contrario_nombre"])
        if a["contrario_abogado"]:
            f["abogados"].add(a["contrario_abogado"].strip())
        if a["contrario_despacho"]:
            f["despachos"].add(a["contrario_despacho"].strip())
        f["asuntos"].append({
            "id": a["id"], "titulo": a["titulo"], "numero": a["numero_expediente"] or f"#{a['id']}",
            "abierto": a["estado"] in ABIERTOS, "resultado": RESULTADOS.get(a["resultado"] or ""),
            "materia": TIPOS_DERECHO.get(a["tipo_asunto"] or "otro", TIPOS_DERECHO["otro"])["label"],
            "url": url_for("asuntos.detalle", id=a["id"]),
        })

    salida = []
    for i, f in enumerate(sorted(fichas.values(), key=lambda x: (-len(x["asuntos"]), normalizar(x["nombre"])))):
        cerrados = [a for a in f["asuntos"] if not a["abierto"] and a["resultado"]]
        ganados = sum(1 for a in cerrados if a["resultado"]["label"] in ("Ganado", "Convenio"))
        f["en_curso"] = sum(1 for a in f["asuntos"] if a["abierto"])
        f["historial"] = {"total": len(cerrados), "ganados": ganados}
        f["abogados"], f["despachos"] = sorted(f["abogados"]), sorted(f["despachos"])
        f["iniciales"], f["color"] = _iniciales(f["nombre"]), _COLORES[i % len(_COLORES)]
        f["empresa"] = f["tipo"] == "persona_moral" or any(s in f["nombre"].upper() for s in ("S.A.", "S.C.", "S. DE R.L", "SAPI", "A.C."))
        f["_pajar"] = normalizar(" ".join([f["nombre"], *f["abogados"], *f["despachos"], f["ciudad"],
                                           *[a["titulo"] + " " + a["numero"] for a in f["asuntos"]]]))
        salida.append(f)
    return salida


@contrapartes_bp.route("/")
@login_required
@tenant_required
def index():
    tdb = g.tenant_db
    q = request.args.get("q", "").strip()[:100]
    todas = _contrapartes(tdb)
    lista = todas
    if q:
        tokens = [t for t in normalizar(q).split() if t]
        lista = [c for c in todas if all(t in c["_pajar"] for t in tokens)]

    # Despachos contrarios: con quién nos enfrentamos más
    despachos: dict[str, dict] = {}
    for c in todas:
        for d in c["despachos"]:
            x = despachos.setdefault(_clave(d), {"nombre": d, "asuntos": 0, "abogados": set()})
            x["asuntos"] += len(c["asuntos"]) or 1
            x["abogados"].update(c["abogados"])
    despachos_l = sorted(despachos.values(), key=lambda d: -d["asuntos"])[:6]
    for d in despachos_l:
        d["abogados"] = sorted(d["abogados"])

    return render_template(
        "contrapartes/index.html",
        contrapartes=lista, total=len(todas), q=q, despachos=despachos_l,
        en_curso=sum(1 for c in todas if c["en_curso"]),
    )


@contrapartes_bp.route("/crear", methods=["POST"])
@login_required
@tenant_required
def crear():
    d = request.get_json() or {}
    nombre = (d.get("nombre") or "").strip()
    if not nombre:
        return jsonify(ok=False, error="Escribe el nombre de la contraparte"), 400

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
