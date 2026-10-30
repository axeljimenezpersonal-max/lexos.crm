"""
Legislación de las 32 entidades federativas, del Orden Jurídico Nacional (Secretaría de Gobernación).

ordenjuridico.gob.mx es la compilación oficial de normas federales, estatales y municipales que mantiene la
SEGOB con lo que le envían los gobiernos de los estados. Es la única fuente que reúne a las 32 entidades con el
mismo formato: por cada estado lista sus ordenamientos por tipo, con fecha de publicación y de última reforma,
y cada uno se descarga en Word (.doc).

Se baja por tandas: primero las constituciones, luego los códigos (civil, familiar, penal, procedimientos…) y
al final las leyes. Un documento solo se vuelve a descargar si su fecha de última reforma cambió.
"""
from __future__ import annotations

import io
import re
import urllib.parse
from dataclasses import dataclass

from modules.corpus import fuentes, word97

OJN = "http://www.ordenjuridico.gob.mx/"

# Claves del OJN (orden del INEGI) → nombre con el que se cita
ENTIDADES = {
    1: "Aguascalientes", 2: "Baja California", 3: "Baja California Sur", 4: "Campeche", 5: "Coahuila",
    6: "Colima", 7: "Chiapas", 8: "Chihuahua", 9: "Ciudad de México", 10: "Durango", 11: "Guanajuato",
    12: "Guerrero", 13: "Hidalgo", 14: "Jalisco", 15: "Estado de México", 16: "Michoacán", 17: "Morelos",
    18: "Nayarit", 19: "Nuevo León", 20: "Oaxaca", 21: "Puebla", 22: "Querétaro", 23: "Quintana Roo",
    24: "San Luis Potosí", 25: "Sinaloa", 26: "Sonora", 27: "Tabasco", 28: "Tamaulipas", 29: "Tlaxcala",
    30: "Veracruz", 31: "Yucatán", 32: "Zacatecas",
}
# Tipos de ordenamiento del OJN que se descargan, en orden de valor para un despacho
TIPOS = {"constitucion": 2, "codigo": 5, "ley": 4}
NOMBRE_TIPO = {2: "Constitución", 5: "Código", 4: "Ley"}

# Formas en que un texto puede nombrar a cada entidad (para reconocerla en el juzgado o en la pregunta)
_ALIAS = {
    "Ciudad de México": ("ciudad de mexico", "cdmx", "distrito federal"),
    "Estado de México": ("estado de mexico", "edomex", "edo. de mexico", "edo. mex"),
    "Coahuila": ("coahuila",), "Michoacán": ("michoacan",), "Veracruz": ("veracruz",),
    "Querétaro": ("queretaro",), "Nuevo León": ("nuevo leon",), "San Luis Potosí": ("san luis potosi",),
    "Yucatán": ("yucatan",),
}


def _sin_acentos(texto: str) -> str:
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", texto.lower()) if unicodedata.category(c) != "Mn")


def entidad_en(texto: str | None) -> str | None:
    """La entidad federativa que menciona un texto ("Juzgado Tercero de lo Familiar de la CDMX" → Ciudad de México)."""
    if not texto:
        return None
    t = " " + re.sub(r"[^a-z0-9.]+", " ", _sin_acentos(texto)) + " "
    # El estado suele ir al final ("… de Tepatitlán de Morelos, Jalisco"): gana la mención más a la derecha.
    hallazgos = []
    for nombre in ENTIDADES.values():
        for alias in _ALIAS.get(nombre, (_sin_acentos(nombre),)):
            pos = t.rfind(f" {alias} ")
            if pos >= 0:
                hallazgos.append((pos + len(alias), len(alias), nombre))
    if hallazgos:
        return max(hallazgos)[2]
    # "Juzgado Civil de Toluca, México": en un juzgado, "México" a secas es el Estado de México.
    if " mexico " in t and re.search(r"juzgado|tribunal|sala|poder judicial", t):
        return "Estado de México"
    return None


@dataclass
class Ordenamiento:
    id_archivo: int
    entidad: str
    tipo: int
    titulo: str
    publicacion: str | None
    ultima_reforma: str | None

    @property
    def clave(self) -> str:
        return f"ojn-{self.id_archivo}"

    @property
    def url_ficha(self) -> str:
        return f"{OJN}fichaOrdenamiento.php?idArchivo={self.id_archivo}&ambito=estatal"


def _fecha(texto: str) -> str | None:
    m = re.search(r"(\d{2})-(\d{2})-(\d{4})", texto or "")
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else None


def listar(edo: int, tipo: int) -> list[Ordenamiento]:
    _, datos, _ = fuentes._get(f"{OJN}despliegaedo2.php?ordenar=&edo={edo}&idi=&catTipo={tipo}")
    pagina = datos.decode("iso-8859-1")
    nombre = re.search(r'<font size="\+1"><b>([^<]+)</b>', pagina)
    if nombre and _sin_acentos(fuentes._sin_etiquetas(nombre.group(1)).split()[0]) not in _sin_acentos(ENTIDADES[edo]):
        raise ValueError(f"el OJN devolvió otra entidad para edo={edo}: {nombre.group(1)}")
    salida, vistos = [], set()
    for m in re.finditer(r"idArchivo=(\d+)&ambito=estatal[^>]*>(.*?)</a>\s*</td>\s*<td[^>]*>([^<]*)</td>\s*"
                         r"<td[^>]*>([^<]*)</td>", pagina, flags=re.S):
        ida = int(m.group(1))
        titulo = re.sub(r"\s+", " ", fuentes._sin_etiquetas(m.group(2))).strip()
        if ida in vistos or not titulo:
            continue
        vistos.add(ida)
        # Algunos títulos traen una nota larga ("Entrará en vigor…"): se corta en el primer punto.
        if len(titulo) > 180:
            titulo = titulo.split(". ")[0]
        salida.append(Ordenamiento(ida, ENTIDADES[edo], tipo, titulo, _fecha(m.group(3)), _fecha(m.group(4))))
    return salida


def es_aplicable(o: Ordenamiento) -> bool:
    """Deja fuera lo que no es legislación del estado: los códigos nacionales (ya están en las leyes federales) y
    los códigos de ética o conducta internos."""
    t = _sin_acentos(o.titulo)
    if "codigo nacional" in t or t.startswith(("codigo de etica", "codigo de conducta")):
        return False
    return True


def url_documento(o: Ordenamiento) -> str:
    _, datos, _ = fuentes._get(o.url_ficha)
    pagina = datos.decode("iso-8859-1")
    m = re.search(r'href="(https?://[^"]*?/Documentos/[^"]+)"', pagina, flags=re.I)
    if not m:
        raise ValueError("la ficha no tiene documento")
    url = m.group(1).replace("/./", "/")
    partes = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit(partes._replace(path=urllib.parse.quote(urllib.parse.unquote(partes.path))))


def descargar(url: str, etag: str | None = None) -> tuple[bytes | None, str | None]:
    estado, datos, enc = fuentes._get(url, {"If-None-Match": etag} if etag else None, timeout=180)
    if estado == 304:
        return None, etag
    return datos, enc.get("ETag")


def texto_documento(datos: bytes, url: str) -> str:
    """Texto plano de lo que publique el OJN: .doc (lo normal), .docx, PDF o HTML."""
    if datos[:8] == word97.FIRMA:
        return word97.texto(datos)
    if datos[:4] == b"%PDF":
        return fuentes.texto_ley(datos)
    if datos[:2] == b"PK":
        import docx
        return "\n".join(p.text for p in docx.Document(io.BytesIO(datos)).paragraphs if p.text.strip())
    if datos[:5] == b"{\\rtf":
        raise ValueError("documento RTF no soportado")
    return fuentes.html_a_texto(datos.decode("utf-8", "replace") if b"utf-8" in datos[:2000].lower()
                                else datos.decode("iso-8859-1", "replace"))


def secciones(texto: str) -> list[tuple[str, str]]:
    return [(s.referencia, s.texto) for s in fuentes.articulos(texto, abreviado=True)]


def pares(tipos: list[str] | None = None, entidades: list[str] | None = None) -> list[tuple[int, int]]:
    """Orden de descarga: todas las constituciones, luego todos los códigos, luego todas las leyes."""
    claves = [TIPOS[t] for t in (tipos or list(TIPOS))]
    edos = [e for e, n in ENTIDADES.items() if not entidades or n in entidades]
    return [(e, t) for t in claves for e in edos]



# ── Sincronización ─────────────────────────────────────────────────────────────

_NO_LEGISLACION = re.compile(
    r"^(ley de ingresos|ley de egresos|presupuesto de egresos|ley que aprueba|tabla de valores|"
    r"ley de hacienda (del|para el) municipio)", re.I)


def _vale_la_pena(o: Ordenamiento) -> bool:
    """Leyes de ingresos anuales y por municipio son cientos y no sirven para litigar: se omiten."""
    return es_aplicable(o) and not _NO_LEGISLACION.match(_sin_acentos(o.titulo))


def sincronizar(config, entidades: list[str] | None = None, tipos: list[str] | None = None,
                limite: int | None = None, forzar: bool = False, avisar=print, hasta: float | None = None,
                pasada: bool = False) -> dict:
    """
    Recorre (entidad, tipo) en orden y guarda lo nuevo o reformado.
    Con `pasada=True` (el programador) guarda en qué par va (corpus_avance 'estados.cursor') para seguir en la
    siguiente vuelta si se le acaba el tiempo (`hasta`, en time.monotonic()). Sin ella (pruebas, línea de
    comandos) recorre lo pedido desde el principio y no toca ese avance.
    """
    import hashlib
    import time
    from datetime import datetime, timezone

    from modules.corpus import indice

    conn = indice.conectar(config)
    sid = indice._bitacora(conn, "ley_estatal")
    cuenta = {"nuevos": 0, "actualizados": 0, "sin_cambios": 0, "errores": 0}
    lista = pares(tipos, entidades)
    completa = pasada and not limite
    inicio = int(indice.avance(conn, "estados.cursor") or 0) if completa else 0
    terminada = True
    for k in range(inicio, len(lista)):
        edo, tipo = lista[k]
        nombre = ENTIDADES[edo]
        if hasta and time.monotonic() > hasta:
            terminada = False
            avisar(f"Leyes estatales: se acabó el tiempo de esta vuelta; sigue en {nombre} ({NOMBRE_TIPO[tipo]}).")
            break
        try:
            ordenamientos = [o for o in listar(edo, tipo) if _vale_la_pena(o)]
        except Exception as exc:
            cuenta["errores"] += 1
            avisar(f"{nombre} ({NOMBRE_TIPO[tipo]}): ERROR al listar: {type(exc).__name__}")
            if completa:
                indice.avance(conn, "estados.cursor", str(k + 1))
            continue
        ordenamientos = ordenamientos[:limite] if limite else ordenamientos
        cortado = False
        for i, o in enumerate(ordenamientos, start=1):
            if hasta and time.monotonic() > hasta:
                cortado = True
                break
            previa = conn.execute("SELECT id, ultima_reforma, etag, huella FROM corpus_documentos "
                                  "WHERE fuente='ley_estatal' AND clave=?", (o.clave,)).fetchone()
            if previa and not forzar and (previa["ultima_reforma"] or "") == (o.ultima_reforma or ""):
                cuenta["sin_cambios"] += 1
                continue
            try:
                url = url_documento(o)
                datos, etag = descargar(url, previa["etag"] if previa and not forzar else None)
                huella = hashlib.sha256(datos).hexdigest() if datos else None
                if datos is None or (previa and huella == previa["huella"] and not forzar):
                    # El archivo no cambió aunque la ficha diga otra fecha: solo se anota la fecha.
                    conn.execute("UPDATE corpus_documentos SET ultima_reforma=? WHERE id=?",
                                 (o.ultima_reforma, previa["id"]))
                    conn.commit()
                    cuenta["sin_cambios"] += 1
                    continue
                texto = texto_documento(datos, url)
                if len(texto) < 200:
                    raise ValueError("documento sin texto")
                secs = secciones(texto)
                indice._guardar(conn, config, "ley_estatal", o.clave, o.titulo, secs,
                                detalle=NOMBRE_TIPO[tipo], fecha_publicacion=o.publicacion,
                                ultima_reforma=o.ultima_reforma, url_oficial=o.url_ficha, etag=etag,
                                huella=huella, entidad=nombre)
                cuenta["actualizados" if previa else "nuevos"] += 1
                avisar(f"[{i}/{len(ordenamientos)}] {nombre} · {o.titulo}: {len(secs)} secciones")
            except Exception as exc:
                cuenta["errores"] += 1
                avisar(f"[{i}/{len(ordenamientos)}] {nombre} · ERROR {o.clave}: {type(exc).__name__}: {exc}")
        if cortado:
            terminada = False
            avisar(f"Leyes estatales: se acabó el tiempo de esta vuelta; sigue en {nombre} ({NOMBRE_TIPO[tipo]}).")
            break
        if not limite and ordenamientos:
            _quitar_abrogados(conn, nombre, tipo, {o.clave for o in ordenamientos}, avisar)
        if completa:
            indice.avance(conn, "estados.cursor", str(k + 1))
    if completa and terminada:
        indice.avance(conn, "estados.cursor", "0")
        indice.avance(conn, "estados.ultima_pasada", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    indice._cerrar(conn, sid, **cuenta, nota=None if terminada else "pasada a medias; sigue en la próxima vuelta")
    conn.close()
    return {**cuenta, "terminada": terminada}


def _quitar_abrogados(conn, entidad: str, tipo: int, vigentes: set[str], avisar) -> None:
    """Lo que ya no aparece en la lista del OJN se abrogó: se quita (con tope, por si la lista vino incompleta)."""
    from modules.corpus import indice
    guardados = conn.execute("SELECT id, clave, titulo FROM corpus_documentos WHERE fuente='ley_estatal' "
                             "AND entidad=? AND detalle=?", (entidad, NOMBRE_TIPO[tipo])).fetchall()
    sobran = [r for r in guardados if r["clave"] not in vigentes]
    if not sobran or len(sobran) > max(3, len(guardados) // 5):
        return
    for r in sobran:
        indice._borrar_fragmentos(conn, r["id"])
        conn.execute("DELETE FROM corpus_documentos WHERE id=?", (r["id"],))
        avisar(f"{entidad}: se quitó «{r['titulo']}» (ya no está vigente en el Orden Jurídico Nacional)")
    conn.commit()
    indice._cache.pop(entidad, None)
