"""Descarga y limpieza de las fuentes oficiales: leyes federales (Cámara de Diputados) y DOF (SIDOF)."""
from __future__ import annotations

import collections
import html
import io
import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date
from html.parser import HTMLParser

AGENTE = "HabeasCorpus/1.0 (sistema legal para despachos; actualizacion diaria de legislacion publica)"
PAUSA = 1.0  # segundos entre peticiones: los servidores de gobierno son frágiles
DIPUTADOS = "https://www.diputados.gob.mx/LeyesBiblio/"
SIDOF = "https://sidof.segob.gob.mx/dof/sidof/"
_ultima_peticion = 0.0


def _get(url: str, encabezados: dict | None = None, timeout: int = 90) -> tuple[int, bytes, dict]:
    global _ultima_peticion
    espera = PAUSA - (time.monotonic() - _ultima_peticion)
    if espera > 0:
        time.sleep(espera)
    req = urllib.request.Request(url, headers={"User-Agent": AGENTE, **(encabezados or {})})
    for intento in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                _ultima_peticion = time.monotonic()
                return resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as e:
            _ultima_peticion = time.monotonic()
            if e.code == 304:
                return 304, b"", dict(e.headers)
            if e.code < 500 or intento == 2:
                raise
        except (urllib.error.URLError, TimeoutError):
            if intento == 2:
                raise
        time.sleep(5 * (intento + 1))
    raise RuntimeError("inalcanzable")


# ── Leyes federales ────────────────────────────────────────────────────────────

@dataclass
class Ley:
    clave: str
    titulo: str
    publicacion: str | None
    ultima_reforma: str | None

    @property
    def url_pdf(self) -> str:
        return f"{DIPUTADOS}pdf/{self.clave}.pdf"


def _fecha_dof(texto: str) -> str | None:
    m = re.search(r"(\d{2})/(\d{2})/(\d{4})", texto)
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else None


def _sin_etiquetas(fragmento_html: str) -> str:
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", fragmento_html))).strip()


def listar_leyes() -> list[Ley]:
    _, datos, _ = _get(DIPUTADOS + "index.htm")
    pagina = datos.decode("iso-8859-1")
    leyes, vistas = [], set()
    for fila in re.findall(r"<tr[^>]*>(.*?)</tr>", pagina, flags=re.S | re.I):
        pdf = re.search(r'href="pdf/([A-Za-z0-9_]+)\.pdf"', fila)
        celdas = re.findall(r"<td[^>]*>(.*?)</td>", fila, flags=re.S | re.I)
        if not pdf or len(celdas) < 4 or pdf.group(1) in vistas:
            continue
        nombre = re.search(r'<a href="ref/[^"]+">(.*?)</a>', celdas[1], flags=re.S | re.I)
        titulo = _sin_etiquetas(nombre.group(1) if nombre else celdas[1])
        titulo = re.sub(r"\s*DOF \d{2}/\d{2}/\d{4}.*$", "", titulo)
        vistas.add(pdf.group(1))
        leyes.append(Ley(pdf.group(1), titulo, _fecha_dof(celdas[1]), _fecha_dof(celdas[2])))
    return leyes


def descargar_ley(ley: Ley, etag: str | None = None) -> tuple[bytes | None, str | None, str | None]:
    """Devuelve (pdf, etag, last_modified); pdf es None si no cambió desde el etag dado."""
    estado, datos, enc = _get(ley.url_pdf, {"If-None-Match": etag} if etag else None, timeout=180)
    if estado == 304:
        return None, etag, enc.get("Last-Modified")
    return datos, enc.get("ETag"), enc.get("Last-Modified")


_ARTICULO = re.compile(
    r"(?m)^\s*(?:ART[ÍI]CULO|Art[íi]culo)\s+"
    r"(\d+(?:\s*[oº°])?(?:\s+(?:Bis|Ter|Qu[áa]ter|Quinquies|Sexies|Septies|Octies)(?:\s*\d+)?)?)"
    r"\s*[\.\-–:]"
)
_TRANSITORIOS = re.compile(r"(?m)^\s*(?:ART[ÍI]CULOS?\s+)?TRANSITORIOS?\s*$")


def texto_ley(pdf: bytes) -> str:
    from pypdf import PdfReader
    paginas = [(p.extract_text() or "") for p in PdfReader(io.BytesIO(pdf)).pages]
    conteo = collections.Counter(l.strip() for p in paginas for l in set(p.splitlines()) if l.strip())
    repetidas = {l for l, n in conteo.items() if len(paginas) > 3 and n > len(paginas) * 0.3}
    limpias = []
    for p in paginas:
        lineas = [l for l in p.splitlines()
                  if l.strip() and l.strip() not in repetidas and not re.fullmatch(r"\s*\d+\s+de\s+\d+\s*", l)]
        limpias.append("\n".join(lineas))
    return "\n".join(limpias)


@dataclass
class Seccion:
    referencia: str
    texto: str


def articulos(texto: str) -> list[Seccion]:
    """Parte una ley en artículos; lo que va después del primer TRANSITORIOS se agrupa aparte."""
    corte = _TRANSITORIOS.search(texto)
    cuerpo, transitorios = (texto[:corte.start()], texto[corte.start():]) if corte else (texto, "")
    secciones: list[Seccion] = []
    marcas = list(_ARTICULO.finditer(cuerpo))
    if marcas and marcas[0].start() > 0 and cuerpo[:marcas[0].start()].strip():
        secciones.append(Seccion("Encabezado", cuerpo[:marcas[0].start()].strip()))
    for i, m in enumerate(marcas):
        fin = marcas[i + 1].start() if i + 1 < len(marcas) else len(cuerpo)
        numero = re.sub(r"\s+", " ", m.group(1)).strip()
        secciones.append(Seccion(f"Artículo {numero}", cuerpo[m.start():fin].strip()))
    if not marcas and cuerpo.strip():
        secciones.append(Seccion("Texto", cuerpo.strip()))
    if transitorios.strip():
        secciones.append(Seccion("Transitorios", transitorios.strip()))
    return secciones


# ── Diario Oficial de la Federación ───────────────────────────────────────────

@dataclass
class NotaDOF:
    codigo: int
    titulo: str
    fecha: str                 # AAAA-MM-DD
    edicion: str               # Matutina, Vespertina, Extraordinaria
    organismo: str
    texto: str = ""

    @property
    def url(self) -> str:
        a, m, d = self.fecha.split("-")
        return f"https://dof.gob.mx/nota_detalle.php?codigo={self.codigo}&fecha={d}/{m}/{a}"


class _Texto(HTMLParser):
    def __init__(self):
        super().__init__()
        self.partes: list[str] = []
        self._ignorar = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._ignorar += 1
        if tag in ("p", "br", "div", "tr", "li", "h1", "h2", "h3", "h4"):
            self.partes.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._ignorar:
            self._ignorar -= 1
        if tag in ("td", "th"):
            self.partes.append(" | ")

    def handle_data(self, data):
        if not self._ignorar:
            self.partes.append(data)


def html_a_texto(contenido: str) -> str:
    p = _Texto()
    p.feed(contenido or "")
    texto = "".join(p.partes)
    return "\n".join(re.sub(r"[ \t\xa0]+", " ", l).strip() for l in texto.splitlines() if l.strip())


def notas_del_dia(dia: date) -> list[NotaDOF]:
    _, datos, _ = _get(f"{SIDOF}notas/{dia:%d-%m-%Y}")
    j = json.loads(datos)
    notas = []
    for clave, edicion in (("NotasMatutinas", "Matutina"), ("NotasVespertinas", "Vespertina"),
                           ("NotasExtraordinarias", "Extraordinaria")):
        for n in j.get(clave) or []:
            titulo = (n.get("titulo") or "").strip()
            if not titulo or n.get("existeHtml") != "S":
                continue
            notas.append(NotaDOF(int(n["codNota"]), titulo, dia.isoformat(), edicion,
                                 (n.get("codOrgaDos") or n.get("nombreCodOrgaUno") or "").strip()))
    return notas


def contenido_nota(nota: NotaDOF, maximo: int = 400_000) -> str:
    _, datos, _ = _get(f"{SIDOF}notas/nota/{nota.codigo}", timeout=180)
    j = json.loads(datos)
    return html_a_texto((j.get("Nota") or {}).get("cadenaContenido") or "")[:maximo]
