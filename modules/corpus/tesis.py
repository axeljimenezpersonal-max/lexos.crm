"""
Tesis y jurisprudencia del Semanario Judicial de la Federación (SCJN, sjf2.scjn.gob.mx).

Cada tesis es un documento: registro digital (clave), rubro (título), texto, época, instancia, tipo
(jurisprudencia o aislada), clave de la tesis, materias y fecha de publicación.

Cómo se descarga:
  - El SJF expone el detalle de cada tesis por su registro digital en su servicio público
    (services/sjftesismicroservice/api/public/tesis/{registro}). Desde 2012 los registros son consecutivos
    (2000000 en adelante = Décima, Undécima y Duodécima Épocas), así que:
      · lo nuevo se busca avanzando desde el registro más alto que ya tenemos;
      · el histórico se llena hacia atrás por tandas, hasta el 2000000, guardando el avance.
  - Hoy el SJF está detrás de una protección anti-robots (Incapsula) que responde con una página de
    verificación a cualquier programa. No se intenta saltarla: si aparece, la vuelta se detiene, se anota
    y se reintenta al día siguiente. Para cargar tesis mientras tanto existe `importar()` (JSON o JSONL
    con los campos de la SCJN, p. ej. una exportación oficial o la entregada por transparencia).
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
from datetime import datetime, timezone

from modules.corpus import fuentes

SJF = "https://sjf2.scjn.gob.mx/"
API = SJF + "services/sjftesismicroservice/api/public/tesis/"
PRIMER_REGISTRO = 2_000_000       # inicio de la numeración consecutiva (Décima Época, 2012)
REGISTRO_SEMILLA = 2_031_000      # punto de partida si la base no tiene ninguna tesis (oct. 2026, aprox.)
HUECO_MAXIMO = 300                # registros seguidos sin tesis antes de dar por terminado lo nuevo


class Bloqueado(RuntimeError):
    """El SJF respondió con su página anti-robots en vez de datos."""


def url_tesis(registro: int | str) -> str:
    return f"{SJF}detalle/tesis/{registro}"


def _campo(d: dict, *nombres, defecto=None):
    for n in nombres:
        v = d.get(n)
        if v not in (None, "", []):
            return v
    return defecto


def _fecha(valor) -> str | None:
    if not valor:
        return None
    if isinstance(valor, (int, float)):           # milisegundos desde 1970
        return datetime.fromtimestamp(valor / 1000, tz=timezone.utc).date().isoformat()
    texto = str(valor)
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", texto)
    if m:
        return m.group(0)
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", texto)
    if m:
        return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    meses = {"enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
             "septiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12}
    m = re.search(r"(\d{1,2}) de (\w+) de (\d{4})", texto.lower())
    if m and m.group(2) in meses:
        return f"{m.group(3)}-{meses[m.group(2)]:02d}-{int(m.group(1)):02d}"
    return None


def normalizar(d: dict) -> dict | None:
    """De un registro del SJF (nombres de campo de la API o de una exportación) a los campos de la base."""
    registro = _campo(d, "registroDigital", "registro_digital", "ius", "idTesis", "id", "registro")
    rubro = _campo(d, "rubro", "titulo", "title")
    texto = _campo(d, "texto", "text", "contenido")
    if not registro or not rubro or not texto:
        return None
    tipo = str(_campo(d, "tipoTesis", "tipo_tesis", "tipo", defecto="")).lower()
    clave = str(_campo(d, "tesis", "claveTesis", "clave_tesis", "numeroTesis", defecto="")).strip()
    if "jurisprudencia" in tipo or "/J." in clave or "J/" in clave:
        tipo = "jurisprudencia"
    elif tipo:
        tipo = "aislada"
    materias = _campo(d, "materias", "materia", defecto="")
    if isinstance(materias, list):
        materias = ", ".join(str(m) for m in materias)
    instancia = _campo(d, "instancia", "organoJuris", "organo", defecto="")
    if isinstance(instancia, dict):
        instancia = instancia.get("nombre") or instancia.get("descripcion") or ""
    epoca = _campo(d, "epoca", "época", defecto="")
    if isinstance(epoca, dict):
        epoca = epoca.get("nombre") or epoca.get("descripcion") or ""
    return {
        "registro": str(registro).strip(),
        "rubro": fuentes.html_a_texto(str(rubro)).strip(),
        "texto": fuentes.html_a_texto(str(texto)).strip(),
        "precedentes": fuentes.html_a_texto(str(_campo(d, "precedentes", defecto=""))).strip(),
        "tipo": tipo or None,
        "clave": clave or None,
        "epoca": str(epoca).strip() or None,
        "instancia": str(instancia).strip() or None,
        "materias": str(materias).strip() or None,
        "fecha": _fecha(_campo(d, "fechaPublicacion", "fecha_publicacion", "fecha", "publicacion")),
        "fuente_publicacion": _campo(d, "fuente", "localizacion", defecto=None),
    }


def guardar(conn, config, t: dict) -> int:
    from modules.corpus import indice
    secciones = [(t["clave"] or f"Registro digital {t['registro']}", t["texto"])]
    if t.get("precedentes"):
        secciones.append(("Precedentes", t["precedentes"][:4000]))
    detalle = " · ".join(p for p in (
        "Jurisprudencia" if t["tipo"] == "jurisprudencia" else ("Tesis aislada" if t["tipo"] else None),
        t["instancia"], t["epoca"]) if p)
    return indice._guardar(conn, config, "tesis", t["registro"], t["rubro"], secciones,
                           detalle=detalle or None, fecha_publicacion=t["fecha"], url_oficial=url_tesis(t["registro"]),
                           tipo_tesis=t["tipo"], epoca=t["epoca"], instancia=t["instancia"],
                           clave_tesis=t["clave"], materias=t["materias"])


def detalle(registro: int) -> dict | None:
    """Una tesis por registro digital; None si ese registro no es una tesis."""
    try:
        _, datos, enc = fuentes._get(f"{API}{registro}", {"Accept": "application/json"}, timeout=60)
    except urllib.error.HTTPError as e:
        if e.code in (404, 400, 204):
            return None
        raise
    tipo = (enc.get("Content-Type") or enc.get("content-type") or "").lower()
    if b"Incapsula" in datos[:3000] or b"_Incapsula_Resource" in datos[:3000]:
        raise Bloqueado("el Semanario Judicial respondió con su verificación anti-robots")
    if "json" not in tipo and not datos.strip().startswith((b"{", b"[")):
        return None
    if not datos.strip():
        return None
    j = json.loads(datos)
    if isinstance(j, list):
        j = j[0] if j else {}
    return normalizar(j)


def sincronizar(config, limite: int | None = None, avisar=print, hasta: float | None = None,
                desde: int | None = None) -> dict:
    """
    1) Lo nuevo: avanza desde el registro más alto guardado hasta encontrar HUECO_MAXIMO registros vacíos seguidos.
    2) El histórico: retrocede desde 'tesis.cursor_atras' hacia PRIMER_REGISTRO, hasta `limite` registros
       revisados o hasta `hasta` (time.monotonic()).
    """
    from modules.corpus import indice
    conn = indice.conectar(config)
    sid = indice._bitacora(conn, "tesis")
    cuenta = {"nuevos": 0, "actualizados": 0, "sin_cambios": 0, "errores": 0}
    revisados = 0
    nota = None

    def agotado() -> bool:
        return (limite is not None and revisados >= limite) or (hasta is not None and time.monotonic() > hasta)

    def traer(registro: int) -> bool:
        nonlocal revisados
        revisados += 1
        if conn.execute("SELECT 1 FROM corpus_documentos WHERE fuente='tesis' AND clave=?", (str(registro),)).fetchone():
            cuenta["sin_cambios"] += 1
            return True
        t = detalle(registro)
        if not t:
            return False
        guardar(conn, config, t)
        cuenta["nuevos"] += 1
        if cuenta["nuevos"] % 10 == 1:
            avisar(f"Tesis {registro}: {t['rubro'][:90]}")
        return True

    try:
        maximo = conn.execute("SELECT MAX(CAST(clave AS INTEGER)) FROM corpus_documentos WHERE fuente='tesis' "
                              "AND CAST(clave AS INTEGER) >= ?", (PRIMER_REGISTRO,)).fetchone()[0]
        inicio = desde or (maximo + 1 if maximo else REGISTRO_SEMILLA)
        # 1) Lo nuevo
        hueco, r = 0, inicio
        while hueco < HUECO_MAXIMO and not agotado():
            try:
                hueco = 0 if traer(r) else hueco + 1
            except Bloqueado:
                raise
            except Exception as exc:
                cuenta["errores"] += 1
                avisar(f"Tesis {r}: ERROR {type(exc).__name__}")
                if cuenta["errores"] >= 20:
                    break
            r += 1
        # 2) El histórico, hacia atrás
        atras = int(indice.avance(conn, "tesis.cursor_atras") or (inicio - 1))
        while atras >= PRIMER_REGISTRO and not agotado():
            try:
                traer(atras)
            except Bloqueado:
                raise
            except Exception as exc:
                cuenta["errores"] += 1
                avisar(f"Tesis {atras}: ERROR {type(exc).__name__}")
                if cuenta["errores"] >= 20:
                    break
            atras -= 1
            if revisados % 25 == 0:
                indice.avance(conn, "tesis.cursor_atras", str(atras))
        indice.avance(conn, "tesis.cursor_atras", str(atras))
        indice.avance(conn, "tesis.bloqueado", "")
    except Bloqueado as exc:
        nota = f"bloqueado: {exc}"
        indice.avance(conn, "tesis.bloqueado", datetime.now(timezone.utc).isoformat(timespec="seconds"))
        avisar("Tesis: el Semanario Judicial no permite descargas automáticas en este momento "
               "(protección anti-robots). Se reintenta mañana.")
    indice._cerrar(conn, sid, **cuenta, nota=nota)
    conn.close()
    return {**cuenta, "revisados": revisados, "bloqueado": bool(nota)}


def importar(config, ruta: str, limite: int | None = None, avisar=print) -> dict:
    """Carga tesis de un archivo JSON (lista) o JSONL (una por línea). Las que ya están se saltan."""
    from modules.corpus import indice
    conn = indice.conectar(config)
    sid = indice._bitacora(conn, "tesis")
    cuenta = {"nuevos": 0, "actualizados": 0, "sin_cambios": 0, "errores": 0}
    with open(ruta, encoding="utf-8") as fh:
        crudo = fh.read()
    registros = json.loads(crudo) if crudo.lstrip().startswith("[") else [json.loads(l) for l in crudo.splitlines() if l.strip()]
    for i, d in enumerate(registros[:limite] if limite else registros, start=1):
        t = normalizar(d)
        if not t:
            cuenta["errores"] += 1
            continue
        previa = conn.execute("SELECT 1 FROM corpus_documentos WHERE fuente='tesis' AND clave=?", (t["registro"],)).fetchone()
        guardar(conn, config, t)
        cuenta["actualizados" if previa else "nuevos"] += 1
        if i % 25 == 0:
            avisar(f"[{i}/{len(registros)}] tesis importadas")
    indice._cerrar(conn, sid, **cuenta, nota=f"importadas de {ruta.rsplit('/', 1)[-1]}")
    conn.close()
    return cuenta
