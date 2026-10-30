"""
Habeas Corpus — catálogo único de tipos de documento («¿Qué es?»).

Un solo lugar para saber qué es cada documento del despacho: lo usan la Bóveda (filtros y subida),
el expediente (subir documento) y el lector (sugerencia al leer el texto). Cada tipo tiene:
  clave    — lo que se guarda en la columna `tipo_documento`
  etiqueta — lo que ve el abogado
  grupo    — para los filtros por grupo
  palabras — frases (sin acentos, minúsculas) con las que se sugiere el tipo
  acto     — la clase de acto de Dictum (modules/mapa.CLASES_ACTO) con la que se alinea

Donde la clave coincide con CLASES_ACTO se usa la misma clave (demanda, contestacion, promocion, acuerdo,
auto, notificacion, emplazamiento, audiencia, sentencia, contrato, convenio, escritura, requerimiento).

Cada fila guarda además `tipo_estado`: 'confirmado' (lo eligió una persona), 'sugerido' (lo propuso el
sistema por el nombre o el texto; nunca se marca como confirmado sin que alguien lo diga) o 'sin_pista'
(se intentó y no hubo pistas: «Sin clasificar»). NULL = todavía no se ha revisado.
"""
from __future__ import annotations

import re
import sqlite3
import unicodedata

GRUPOS: list[tuple[str, str]] = [
    ("escritos", "Escritos de las partes"),
    ("juzgado", "Del juzgado o autoridad"),
    ("recursos", "Recursos y amparo"),
    ("pruebas", "Pruebas"),
    ("contratos", "Contratos y convenios"),
    ("notariales", "Notariales"),
    ("persona", "De la persona"),
    ("pagos", "Facturas y pagos"),
    ("correspondencia", "Correspondencia"),
    ("otro", "Otro"),
]

# (clave, etiqueta, grupo, acto de Dictum, palabras para sugerir)
_CATALOGO: list[tuple[str, str, str, str, tuple[str, ...]]] = [
    # Escritos de las partes
    ("demanda", "Demanda", "escritos", "demanda",
     ("demanda", "escrito inicial", "escrito de demanda", "se demanda", "vengo a demandar", "demandar")),
    ("contestacion", "Contestación", "escritos", "contestacion",
     ("contestacion", "contesta demanda", "contestacion de demanda", "vengo a contestar", "contestar la demanda")),
    ("reconvencion", "Reconvención", "escritos", "demanda",
     ("reconvencion", "contrademanda", "demanda reconvencional", "via reconvencional")),
    ("promocion", "Promoción o escrito", "escritos", "promocion",
     ("promocion", "escrito", "ocurso", "solicito", "vengo a solicitar")),
    ("alegatos", "Alegatos", "escritos", "promocion",
     ("alegatos", "alegato", "formulo alegatos")),
    # Del juzgado o autoridad
    ("acuerdo", "Acuerdo", "juzgado", "acuerdo",
     ("acuerdo", "proveido", "se acuerda", "visto el escrito", "tengase", "se tiene por")),
    ("auto", "Auto", "juzgado", "auto",
     ("auto", "auto admisorio", "auto de radicacion", "auto de inicio", "se admite", "auto de exequendo")),
    ("sentencia", "Sentencia", "juzgado", "sentencia",
     ("sentencia", "resolutivos", "se resuelve", "puntos resolutivos", "sentencia definitiva",
      "sentencia interlocutoria", "laudo")),
    ("resolucion", "Otra resolución", "juzgado", "sentencia",
     ("resolucion", "interlocutoria")),
    ("resolucion_administrativa", "Resolución administrativa", "juzgado", "otro",
     ("resolucion administrativa", "oficio de autoridad", "credito fiscal", "determinacion de credito",
      "clausura", "multa administrativa", "sat", "imss", "infonavit", "profeco")),
    ("notificacion", "Notificación", "juzgado", "notificacion",
     ("notificacion", "cedula de notificacion", "cedula", "se notifica", "notifiquese", "boletin judicial")),
    ("emplazamiento", "Emplazamiento", "juzgado", "emplazamiento",
     ("emplazamiento", "emplazar", "se emplaza", "cedula de emplazamiento")),
    ("audiencia", "Acta de audiencia", "juzgado", "audiencia",
     ("audiencia", "acta de audiencia", "audiencia preliminar", "audiencia de juicio", "comparecencia",
      "desahogo", "minuta de audiencia")),
    # Recursos y amparo
    ("apelacion", "Apelación", "recursos", "recurso",
     ("apelacion", "recurso de apelacion", "agravios", "toca", "segunda instancia")),
    ("revocacion", "Revocación", "recursos", "recurso",
     ("revocacion", "recurso de revocacion")),
    ("queja", "Queja", "recursos", "recurso",
     ("queja", "recurso de queja")),
    ("amparo_directo", "Amparo directo", "recursos", "amparo",
     ("amparo directo", "demanda de amparo directo", "tribunal colegiado", "conceptos de violacion")),
    ("amparo_indirecto", "Amparo indirecto", "recursos", "amparo",
     ("amparo indirecto", "amparo", "juicio de amparo", "demanda de amparo", "juzgado de distrito",
      "suspension provisional", "suspension definitiva", "acto reclamado", "informe justificado")),
    ("revision", "Recurso de revisión", "recursos", "recurso",
     ("recurso de revision", "revision", "amparo en revision")),
    # Pruebas
    ("prueba_documental", "Prueba documental", "pruebas", "prueba",
     ("prueba documental", "documental", "ofrecimiento de pruebas", "pruebas", "anexo")),
    ("prueba_pericial", "Dictamen pericial", "pruebas", "prueba",
     ("pericial", "peritaje", "dictamen pericial", "dictamen", "perito")),
    ("prueba_testimonial", "Prueba testimonial", "pruebas", "prueba",
     ("testimonial", "testigo", "testigos", "interrogatorio", "declaracion testimonial")),
    ("prueba_medios", "Fotografías, audio o video", "pruebas", "prueba",
     ("fotografia", "fotografias", "foto", "fotos", "video", "audio", "grabacion", "captura de pantalla",
      "whatsapp", "evidencia")),
    # Contratos y convenios
    ("contrato", "Contrato", "contratos", "contrato",
     ("contrato", "arrendamiento", "compraventa", "prestacion de servicios", "comodato", "mutuo",
      "clausulas", "las partes convienen")),
    ("convenio", "Convenio", "contratos", "convenio",
     ("convenio", "convenio de pago", "transaccion", "acuerdo de voluntades", "finiquito", "convenio judicial")),
    # Notariales
    ("escritura", "Escritura pública", "notariales", "escritura",
     ("escritura", "escritura publica", "instrumento publico", "protocolo", "notario publico", "notaria")),
    ("poder", "Poder notarial", "notariales", "escritura",
     ("poder", "poder notarial", "poder general", "pleitos y cobranzas", "actos de administracion", "mandato")),
    ("acta_notarial", "Acta notarial", "notariales", "escritura",
     ("acta notarial", "fe de hechos", "acta constitutiva", "asamblea", "protocolizacion")),
    ("testimonio", "Testimonio notarial", "notariales", "escritura",
     ("testimonio", "primer testimonio", "copia certificada")),
    # De la persona
    ("identificacion", "Identificación", "persona", "otro",
     ("identificacion", "ine", "ife", "credencial", "pasaporte", "cedula profesional", "licencia de conducir")),
    ("comprobante_domicilio", "Comprobante de domicilio", "persona", "otro",
     ("comprobante de domicilio", "domicilio", "recibo de luz", "recibo cfe", "cfe", "recibo de agua",
      "predial", "telmex", "recibo de telefono")),
    ("acta_civil", "Acta de nacimiento o matrimonio", "persona", "otro",
     ("acta de nacimiento", "acta de matrimonio", "acta de defuncion", "acta de divorcio", "registro civil",
      "nacimiento", "matrimonio")),
    ("rfc_curp", "RFC o CURP", "persona", "otro",
     ("rfc", "curp", "constancia de situacion fiscal", "cedula fiscal", "situacion fiscal")),
    ("estado_cuenta", "Estado de cuenta", "persona", "pago",
     ("estado de cuenta", "estados de cuenta", "movimientos bancarios", "banco", "bancario")),
    # Facturas y pagos
    ("factura", "Factura", "pagos", "pago",
     ("factura", "cfdi", "xml", "comprobante fiscal", "nota de credito")),
    ("comprobante_pago", "Comprobante de pago", "pagos", "pago",
     ("comprobante de pago", "pago", "transferencia", "spei", "deposito", "recibo de pago", "recibo",
      "ticket", "honorarios")),
    # Correspondencia
    ("correo", "Correo electrónico", "correspondencia", "otro",
     ("correo", "email", "e mail", "mensaje", "fwd")),
    ("carta", "Carta u oficio", "correspondencia", "otro",
     ("carta", "oficio", "memorandum", "memo", "comunicado", "aviso")),
    ("requerimiento", "Requerimiento", "correspondencia", "requerimiento",
     ("requerimiento", "requerimiento de pago", "carta de requerimiento", "intimacion", "se le requiere")),
    # Otro
    ("otro", "Otro", "otro", "otro", ()),
]

TIPOS: dict[str, dict] = {
    clave: {"clave": clave, "etiqueta": etiqueta, "grupo": grupo, "acto": acto, "palabras": palabras}
    for clave, etiqueta, grupo, acto, palabras in _CATALOGO
}
GRUPO_LABEL = dict(GRUPOS)
CONFIRMADO, SUGERIDO = "confirmado", "sugerido"

# Las tablas de documentos con archivo que llevan tipo (tabla → columna con el nombre visible)
TABLAS = {
    "documentos": "nombre",
    "asunto_sentencias": "titulo",
    "cliente_documentos": "nombre",
    "biblioteca_archivos": "nombre_original",
}
COLUMNAS = (("tipo_documento", "TEXT"), ("tipo_estado", "TEXT"), ("fecha_documento", "TEXT"))

# Valores que ya existían en la base y su equivalente en el catálogo
_EQUIVALENTES = {
    "escrito": "promocion", "resolucion": "resolucion", "sentencia": "sentencia", "auto": "auto",
    "acuerdo": "acuerdo", "contrato": "contrato", "convenio": "convenio", "demanda": "demanda",
    "amparo": "amparo_indirecto", "prueba": "prueba_documental", "pago": "comprobante_pago",
}


def por_grupo() -> list[dict]:
    """Grupos con sus tipos, en el orden del catálogo (para selects y filtros)."""
    salida = [{"clave": g, "etiqueta": l, "tipos": []} for g, l in GRUPOS]
    indice = {g["clave"]: g for g in salida}
    for t in TIPOS.values():
        indice[t["grupo"]]["tipos"].append(t)
    return salida


def valido(clave: str | None) -> str | None:
    clave = (clave or "").strip().lower()
    return clave if clave in TIPOS else None


def etiqueta(clave: str | None) -> str:
    return TIPOS[clave]["etiqueta"] if clave in TIPOS else ""


def grupo_de(clave: str | None) -> str | None:
    return TIPOS[clave]["grupo"] if clave in TIPOS else None


def acto_de(clave: str | None) -> str:
    """La clase de acto de Dictum (CLASES_ACTO) que corresponde a este tipo."""
    return TIPOS[clave]["acto"] if clave in TIPOS else "otro"


# ── Sugerencia ────────────────────────────────────────────────────────────────

def _plano(texto: str) -> str:
    t = unicodedata.normalize("NFKD", (texto or "").lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return " " + re.sub(r"[^a-z0-9]+", " ", t).strip() + " "


def _puntajes(texto_plano: str, peso: float = 1.0) -> dict[str, float]:
    puntos: dict[str, float] = {}
    for clave, t in TIPOS.items():
        for frase in t["palabras"]:
            n = texto_plano.count(f" {frase} ")
            if n:
                # Las frases largas pesan más («amparo directo» gana a «amparo»)
                puntos[clave] = puntos.get(clave, 0) + peso * min(n, 5) * (1 + len(frase.split()))
    return puntos


def sugerir(nombre: str | None = None, texto: str | None = None) -> str | None:
    """Tipo más probable a partir del nombre del archivo y, si ya se leyó, del inicio del texto.
    Devuelve None si no hay pistas suficientes (queda «Sin clasificar»)."""
    puntos: dict[str, float] = {}
    if nombre:
        base = re.sub(r"\.[a-z0-9]{2,5}$", "", nombre.strip(), flags=re.I)
        for k, v in _puntajes(_plano(base), 3.0).items():
            puntos[k] = puntos.get(k, 0) + v
    if texto:
        inicio = texto[:6000]
        for k, v in _puntajes(_plano(inicio[:500]), 2.0).items():   # el encabezado dice mucho
            puntos[k] = puntos.get(k, 0) + v
        for k, v in _puntajes(_plano(inicio), 1.0).items():
            puntos[k] = puntos.get(k, 0) + v
    if not puntos:
        return None
    mejor = max(puntos, key=lambda k: (puntos[k], -list(TIPOS).index(k)))
    return mejor if puntos[mejor] >= 2 else None


# ── Base de datos ─────────────────────────────────────────────────────────────

def asegurar(tdb) -> None:
    """Columnas de tipo en las tablas de documentos (también están en init_db._MIGRATE_COLS)."""
    for tabla in TABLAS:
        for col, tipo in COLUMNAS:
            try:
                tdb.execute(f"ALTER TABLE {tabla} ADD COLUMN {col} {tipo}")
            except sqlite3.OperationalError:
                pass   # ya existe, o el despacho aún no tiene la tabla


def guardar(tdb, tabla: str, fila_id: int, clave: str | None, confirmado: bool = True) -> bool:
    clave = valido(clave)
    if tabla not in TABLAS or not clave:
        return False
    tdb.execute(f"UPDATE {tabla} SET tipo_documento=?, tipo_estado=? WHERE id=?",
                (clave, CONFIRMADO if confirmado else SUGERIDO, fila_id))
    return True


def al_subir(tdb, tabla: str, fila_id: int, elegido: str | None, nombre: str | None,
             fecha: str | None = None) -> None:
    """Después de guardar un archivo: si la persona dijo qué es, queda confirmado;
    si no, se sugiere por el nombre (sugerido). La fecha del documento es opcional."""
    if tabla not in TABLAS:
        return
    clave = valido(elegido)
    if clave:
        guardar(tdb, tabla, fila_id, clave, confirmado=True)
    else:
        sugerido = sugerir(nombre)
        if sugerido:
            guardar(tdb, tabla, fila_id, sugerido, confirmado=False)
    f = fecha_valida(fecha)
    if f:
        tdb.execute(f"UPDATE {tabla} SET fecha_documento=? WHERE id=?", (f, fila_id))


def fecha_valida(v: str | None) -> str | None:
    v = (v or "").strip()[:10]
    return v if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v) else None


def sugerir_por_texto(conn, archivo_url: str, texto: str) -> None:
    """Lo llama el lector al terminar de leer: si nadie ha confirmado el tipo, lo sugiere por el texto."""
    for tabla, col in TABLAS.items():
        try:
            filas = conn.execute(f"SELECT id, {col} AS nombre FROM {tabla} WHERE archivo_url=? "
                                 "AND (tipo_estado IS NULL OR tipo_estado != 'confirmado')", (archivo_url,)).fetchall()
        except sqlite3.OperationalError:
            continue
        for f in filas:
            clave = sugerir(f[1], texto)
            if clave:
                conn.execute(f"UPDATE {tabla} SET tipo_documento=?, tipo_estado='sugerido' WHERE id=?", (clave, f[0]))
    conn.commit()


SIN_PISTA = "sin_pista"   # ya se intentó sugerir y no hubo pistas: queda «Sin clasificar»


def clasificar_pendientes(tdb, slug: str | None = None) -> int:
    """Una sola vez por documento: a los que nunca se han clasificado les sugiere un tipo (nunca «confirmado»).
    - Las resoluciones ya traían su tipo elegido por una persona (sentencia, auto, acuerdo…): se respeta.
    - Valores viejos de `tipo_documento` se pasan a su clave del catálogo.
    - Los demás se sugieren por el nombre y, si el lector ya los leyó, por el inicio del texto."""
    asegurar(tdb)
    n = 0
    try:
        for s in tdb.execute("SELECT id, tipo FROM asunto_sentencias WHERE tipo_estado IS NULL").fetchall():
            clave = {"sentencia": "sentencia", "resolucion": "resolucion", "auto": "auto",
                     "acuerdo": "acuerdo"}.get(s["tipo"] or "")
            if clave:
                tdb.execute("UPDATE asunto_sentencias SET tipo_documento=?, tipo_estado='confirmado' WHERE id=?",
                            (clave, s["id"]))
                n += 1
    except sqlite3.OperationalError:
        pass
    for tabla, col in TABLAS.items():
        try:
            filas = tdb.execute(f"SELECT id, {col} AS nombre, archivo_url, tipo_documento FROM {tabla} "
                                "WHERE tipo_estado IS NULL").fetchall()
        except sqlite3.OperationalError:
            continue
        for f in filas:
            viejo = (f["tipo_documento"] or "").strip().lower()
            if viejo and viejo not in TIPOS and viejo not in _EQUIVALENTES:
                viejo = ""
            clave = valido(viejo) or _EQUIVALENTES.get(viejo) or sugerir(f["nombre"], _texto_inicial(tdb, slug, f["archivo_url"]))
            if clave:
                tdb.execute(f"UPDATE {tabla} SET tipo_documento=?, tipo_estado='sugerido' WHERE id=?", (clave, f["id"]))
                n += 1
            else:
                tdb.execute(f"UPDATE {tabla} SET tipo_documento=NULL, tipo_estado=? WHERE id=?", (SIN_PISTA, f["id"]))
    tdb.commit()
    return n


def _texto_inicial(tdb, slug: str | None, url: str | None) -> str | None:
    """Los primeros fragmentos ya leídos de un documento (descifrados solo en memoria)."""
    if not slug or not url:
        return None
    try:
        from modules import boveda
        filas = tdb.execute(
            "SELECT f.texto_cifrado FROM lector_fragmentos f JOIN lector_documentos d ON d.id = f.documento_id "
            "WHERE d.archivo_url=? AND d.estado='listo' ORDER BY f.orden LIMIT 3", (url,)).fetchall()
        return " ".join(boveda.descifrar_texto(slug, r[0]) for r in filas) or None
    except Exception:
        return None
