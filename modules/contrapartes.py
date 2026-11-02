"""
Habeas Corpus — modules/contrapartes.py
Contrapartes: las que se registran a mano y las que ya aparecen en los expedientes
(campo «contraparte»), juntas, con el historial del despacho contra cada una y sus abogados.
"""

import re

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


def clave_persona(nombre: str) -> str:
    """Para comparar personas entre Clientes y Contrapartes: solo letras y números, sin acentos.
    «Constructora Omega, S.A. de C.V.» y «CONSTRUCTORA OMEGA SA DE CV» quedan iguales."""
    return "".join(ch for ch in normalizar(nombre) if ch.isalnum())


# Palabras que no distinguen a una persona: tipo de sociedad y artículos («de la», «S.A. de C.V.»)
_NO_DISTINGUEN = set("""de del la las el los y e sa cv sc srl rl sapi sab sas spr scp ac ap iap
sociedad anonima capital variable responsabilidad limitada civil asociacion""".split())


def palabras_persona(nombre: str) -> frozenset:
    """Las palabras que sí distinguen el nombre, sin acentos, sin iniciales sueltas y sin el tipo de sociedad."""
    n = "".join(ch if ch.isalnum() else " " for ch in normalizar(nombre or ""))
    return frozenset(p for p in n.split() if len(p) > 1 and p not in _NO_DISTINGUEN)


def parecido(a: str, b: str) -> str | None:
    """«mismo» si es el mismo nombre escrito distinto; «parecido» si uno contiene al otro
    con al menos dos palabras («Juan Pérez» y «Juan Pérez García», o «Pérez García Juan»)."""
    ka, kb = clave_persona(a or ""), clave_persona(b or "")
    if len(ka) < 5 or len(kb) < 5:
        return None
    if ka == kb:
        return "mismo"
    pa, pb = palabras_persona(a), palabras_persona(b)
    if pa and pa == pb and len("".join(pa)) >= 5:
        return "mismo"
    chico, grande = (pa, pb) if len(pa) <= len(pb) else (pb, pa)
    if len(chico) >= 2 and chico <= grande:
        return "parecido"
    return None


def coincidencias(tdb, nombre: str, rfc: str | None = None) -> list[dict]:
    """Dónde aparece esta persona (o alguien con nombre parecido) como contraparte del despacho:
    posible conflicto de interés. Revisa todos los expedientes, pero a un abogado no le muestra
    el título ni la contraparte de los que no lleva."""
    if len(clave_persona(nombre or "")) < 5:
        return []
    visibles = None
    if _solo_lo_suyo():
        uid = session.get("user_id")
        visibles = {r[0] for r in tdb.execute(f"SELECT a.id FROM asuntos a WHERE {_FILTRO_PROPIOS}", (uid, uid, uid))}
    salida = []
    for a in tdb.execute("SELECT id, titulo, numero_expediente, contrario_nombre FROM asuntos "
                         "WHERE COALESCE(contrario_nombre, '') != '' ORDER BY fecha_alta DESC").fetchall():
        como = parecido(nombre, a["contrario_nombre"])
        if not como:
            continue
        if visibles is not None and a["id"] not in visibles:
            texto = "un expediente que lleva otro abogado" + (", con un nombre parecido" if como == "parecido" else "")
            salida.append({"texto": texto, "url": None, "como": como})
        else:
            texto = f"«{a['titulo']}» ({a['numero_expediente'] or '#' + str(a['id'])})"
            if como == "parecido":
                texto += f", con el nombre «{a['contrario_nombre']}»"
            salida.append({"texto": texto, "url": url_for("asuntos.detalle", id=a["id"]), "como": como})
    if not salida:
        for r in tdb.execute("SELECT nombre, rfc FROM contrapartes WHERE activo=1").fetchall():
            como = "mismo" if rfc and (r["rfc"] or "").upper() == rfc.upper() else parecido(nombre, r["nombre"])
            if como:
                texto = "la lista de Contrapartes" + (f", con el nombre «{r['nombre']}»" if como == "parecido" else "")
                salida.append({"texto": texto, "url": url_for("contrapartes.index", q=r["nombre"]), "como": como})
                break
    # Primero lo que es el mismo nombre; luego lo parecido
    salida.sort(key=lambda x: x["como"] != "mismo")
    return salida[:4]


def clientes_con_nombre(tdb, nombre: str) -> list[dict]:
    """Clientes activos que se llaman igual (o parecido) que esta contraparte: posible conflicto de interés."""
    if len(clave_persona(nombre or "")) < 5:
        return []
    salida = []
    for c in tdb.execute("SELECT id, COALESCE(NULLIF(razon_social, ''), TRIM(nombre || ' ' || COALESCE(apellidos, ''))) AS n "
                         "FROM clientes WHERE activo=1").fetchall():
        como = parecido(nombre, c["n"])
        if como:
            salida.append({"id": c["id"], "nombre": c["n"], "parecido": como == "parecido",
                           "url": url_for("clientes.detalle", id=c["id"])})
    salida.sort(key=lambda x: x["parecido"])
    return salida[:3]


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

    clientes = [dict(c) for c in tdb.execute(
        "SELECT id, COALESCE(NULLIF(razon_social, ''), TRIM(nombre || ' ' || COALESCE(apellidos, ''))) AS n, rfc "
        "FROM clientes WHERE activo=1").fetchall()]

    def cliente_de(f):
        """El cliente que es (o se parece a) esta contraparte: el mismo RFC o nombre gana a uno parecido."""
        por_rfc = f["rfc"] and next((c for c in clientes if (c["rfc"] or "").upper() == f["rfc"].upper()), None)
        if por_rfc:
            return por_rfc, False
        hallados = [(c, parecido(f["nombre"], c["n"])) for c in clientes]
        hallados = sorted((h for h in hallados if h[1]), key=lambda h: h[1] != "mismo")
        return (hallados[0][0], hallados[0][1] == "parecido") if hallados else (None, False)

    salida = []
    for i, f in enumerate(sorted(fichas.values(), key=lambda x: (-len(x["asuntos"]), normalizar(x["nombre"])))):
        cerrados = [a for a in f["asuntos"] if not a["abierto"] and a["resultado"]]
        ganados = sum(1 for a in cerrados if a["resultado"]["label"] in ("Ganado", "Convenio"))
        f["en_curso"] = sum(1 for a in f["asuntos"] if a["abierto"])
        f["historial"] = {"total": len(cerrados), "ganados": ganados}
        f["abogados"], f["despachos"] = sorted(f["abogados"]), sorted(f["despachos"])
        f["iniciales"], f["color"] = _iniciales(f["nombre"]), _COLORES[i % len(_COLORES)]
        f["empresa"] = f["tipo"] == "persona_moral" or any(s in f["nombre"].upper() for s in ("S.A.", "S.C.", "S. DE R.L", "SAPI", "A.C."))
        cli, solo_parecido = cliente_de(f)
        f["cliente_id"], f["cliente_nombre"], f["cliente_parecido"] = (cli["id"], cli["n"], solo_parecido) if cli else (None, "", False)
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


TIPOS = ("persona_fisica", "persona_moral")
RE_RFC = re.compile(r"^[A-ZÑ&]{3,4}\d{6}[A-Z\d]{3}$")


def _leer(d: dict):
    """Valida el formulario de contraparte. Regresa (valores, error)."""
    texto = lambda k, n: ((d.get(k) or "").strip()[:n] or None)  # noqa: E731
    v = {"nombre": texto("nombre", 200), "tipo": d.get("tipo") if d.get("tipo") in TIPOS else "persona_fisica",
         "rfc": re.sub(r"[\s-]", "", (d.get("rfc") or "").upper()) or None, "email": (texto("email", 160) or "").lower() or None,
         "telefono": texto("telefono", 30), "abogado_nombre": texto("abogado_nombre", 200),
         "abogado_email": (texto("abogado_email", 160) or "").lower() or None, "despacho": texto("despacho", 200),
         "ciudad": texto("ciudad", 80), "notas": texto("notas", 2000)}
    if not v["nombre"]:
        return None, "Escribe el nombre de la contraparte"
    if v["rfc"] and not RE_RFC.match(v["rfc"]):
        return None, "El RFC no tiene un formato válido (12 caracteres para empresa, 13 para persona)"
    return v, None


def _repetida(tdb, nombre: str, excepto: int = 0):
    k = _clave(nombre)
    return next((r for r in tdb.execute("SELECT id, nombre FROM contrapartes WHERE activo=1 AND id != ?", (excepto,)).fetchall()
                 if _clave(r["nombre"]) == k), None)


@contrapartes_bp.route("/es-cliente", methods=["POST"])
@login_required
@tenant_required
def es_cliente():
    """Para avisar al escribir la contraparte de un expediente. El nombre va en el cuerpo, no en la dirección."""
    nombre = str((request.get_json(silent=True) or {}).get("nombre") or "")[:200]
    return jsonify(ok=True, clientes=clientes_con_nombre(g.tenant_db, nombre))


@contrapartes_bp.route("/crear", methods=["POST"])
@login_required
@tenant_required
def crear():
    tdb = g.tenant_db
    v, err = _leer(request.get_json(silent=True) or {})
    if err:
        return jsonify(ok=False, error=err), 400
    if _repetida(tdb, v["nombre"]):
        return jsonify(ok=False, error=f"«{v['nombre']}» ya está registrada en Contrapartes"), 409
    cur = tdb.execute(f"INSERT INTO contrapartes ({', '.join(v)}) VALUES ({', '.join('?' * len(v))})", tuple(v.values()))
    tdb.commit()
    return jsonify(ok=True, id=cur.lastrowid)


@contrapartes_bp.route("/<int:id>/datos")
@login_required
@tenant_required
def datos(id: int):
    r = g.tenant_db.execute("SELECT * FROM contrapartes WHERE id=? AND activo=1", (id,)).fetchone()
    if not r:
        return jsonify(ok=False, error="No encontrada"), 404
    return jsonify(ok=True, **{k: r[k] for k in ("nombre", "tipo", "rfc", "email", "telefono", "abogado_nombre",
                                                  "abogado_email", "despacho", "ciudad", "notas")})


@contrapartes_bp.route("/<int:id>/editar", methods=["POST"])
@login_required
@tenant_required
def editar(id: int):
    tdb = g.tenant_db
    if not tdb.execute("SELECT 1 FROM contrapartes WHERE id=? AND activo=1", (id,)).fetchone():
        return jsonify(ok=False, error="No encontrada"), 404
    v, err = _leer(request.get_json(silent=True) or {})
    if err:
        return jsonify(ok=False, error=err), 400
    if _repetida(tdb, v["nombre"], id):
        return jsonify(ok=False, error=f"Ya hay otra contraparte llamada «{v['nombre']}»"), 409
    tdb.execute(f"UPDATE contrapartes SET {', '.join(k + '=?' for k in v)}, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (*v.values(), id))
    tdb.commit()
    return jsonify(ok=True)


@contrapartes_bp.route("/<int:id>/quitar", methods=["POST"])
@login_required
@tenant_required
def quitar(id: int):
    """Quita el registro hecho a mano. Si la contraparte sale en expedientes, sigue apareciendo por ellos."""
    tdb = g.tenant_db
    cur = tdb.execute("UPDATE contrapartes SET activo=0, updated_at=CURRENT_TIMESTAMP WHERE id=? AND activo=1", (id,))
    tdb.commit()
    if not cur.rowcount:
        return jsonify(ok=False, error="No encontrada"), 404
    return jsonify(ok=True)
