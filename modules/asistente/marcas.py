"""
Marcas: antes de que un texto del despacho salga al modelo externo, los nombres y datos personales
se cambian por marcas ([CLIENTE], [CONTRAPARTE], [PERSONA_2]…) y en la respuesta se vuelven a poner
los reales. El modelo razona sobre el caso sin saber de quién es.

Las marcas viven solo durante una lectura o una pregunta: no se guardan ni se comparten entre casos.
Tres capas encuentran los datos: lo que ya está registrado, patrones (RFC, CURP, cuentas, domicilios…)
y un detector de nombres que corre en este servidor. Si el detector no está, nada debe salir.
"""
from __future__ import annotations

import logging
import re
import sqlite3
import threading
import unicodedata

log = logging.getLogger(__name__)

MODELO_NER = "xx_ent_wiki_sm"
BLOQUE_NER = 100_000
RESERVADO = "[RESERVADO]"
PATRON_MARCA = re.compile(r"\[[A-ZÑ]+(?:_[A-ZÑ]+)*(?:_\d+)?\]")

INSTRUCCION = (
    "Los nombres y datos personales del despacho vienen cambiados por marcas entre corchetes, como [CLIENTE], "
    "[CONTRAPARTE] o [PERSONA_2]. Trátalas como si fueran los nombres: escríbelas exactamente igual, sin explicarlas, "
    "y no intentes deducir a quién corresponden. [RESERVADO] es un dato de un caso de otro equipo: no lo menciones."
)

_MAY = "A-ZÁÉÍÓÚÜÑ"
_MIN = "a-záéíóúüñ"
_UNICAS = {"CLIENTE", "CONTRAPARTE", "ABOGADO_CONTRARIO", "DESPACHO_CONTRARIO", "EXPEDIENTE"}
_CONECTORES = {"de", "del", "la", "las", "los", "y", "e"}
_TIPO_SOCIEDAD = {"sa", "cv", "sc", "srl", "rl", "sapi", "sab", "sas", "spr", "scp", "ac", "ap", "iap", "sociedad",
                  "anonima", "capital", "variable", "responsabilidad", "limitada", "civil", "asociacion"}
# Palabras que nunca forman parte de un nombre detectado: evita marcar leyes, autoridades o encabezados.
_NO_ES_NOMBRE = set("""
ley leyes codigo constitucion politica articulo articulos juzgado juzgados juez jueza magistrado magistrada tribunal
suprema corte justicia nacion sala pleno amparo demanda escrito inicial sentencia acuerdo audiencia expediente juicio
notaria registro civil penal familiar mercantil laboral federal estatal estado estados unidos mexicanos mexico republica
ciudad distrito municipio alcaldia gobierno secretaria secretario presidencia presidente ministerio publico fiscalia
procuraduria instituto comision banco usted presente atentamente protesta decir verdad primera segunda tercera tercero
primero segundo cuarto quinto sat imss infonavit empresa sociedad moral persona denominada la el los las mtro mtra lic
licda dra sr sra srta don dona ciudadano ciudadana testigo perito notario actor actora demandado demandada
""".split())

_ANTES_DE_NOMBRE = (
    r"(?i:\b(?:c\.|ciudadan[oa]|sr\.|sra\.|srta\.|señora?|señorita|don|doña|lic\.|licda\.|licenciad[oa]s?|mtro\.|mtra\.|"
    r"maestr[oa]|dr\.|dra\.|ing\.|arq\.|notari[oa](?:\s+p[uú]blic[oa])?|testigos?|perit[oa]s?|apoderad[oa](?:\s+legal)?|"
    r"representante\s+legal|actuari[oa]|secretari[oa](?:\s+de\s+acuerdos)?|compareci[oó]|menor|demandad[oa]|actora?|"
    r"quejos[oa]|imputad[oa]|v[ií]ctima|ofendid[oa]))"
)
_NOMBRE = rf"[{_MAY}][{_MIN}]+(?:\s+(?:(?i:de|del|de\s+la|de\s+las|de\s+los)\s+)?[{_MAY}][{_MIN}]+){{0,5}}"
_CON_TITULO = re.compile(rf"{_ANTES_DE_NOMBRE}(?:\s+(?i:el|la)\s+(?i:señora?|licenciad[oa]))?[\s,]+({_NOMBRE})")
_SOCIEDAD = re.compile(
    rf"((?:[{_MAY}][\w&.-]*\s+(?:(?i:de|del|la|las|los|y|e)\s+)*){{1,8}}?[{_MAY}][\w&.-]*),?\s+"
    r"(S\.?\s?A\.?(?:\s?P\.?\s?I\.?)?(?:\s+(?i:de)\s+C\.?\s?V\.?)?|S\.?\s?(?i:de)\s?R\.?\s?L\.?(?:\s+(?i:de)\s+C\.?\s?V\.?)?"
    r"|S\.?\s?C\.?|A\.?\s?C\.?|S\.?\s?A\.?\s?S\.?)(?![\w])"
)
_PATRONES = (
    (re.compile(r"\b[A-Z]{4}\d{6}[HM][A-Z]{5}[A-Z0-9]\d\b"), "CURP", 0),
    (re.compile(r"\b[A-ZÑ&]{3,4}\d{6}[A-Z0-9]{3}\b"), "RFC", 0),
    (re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b"), "CORREO", 0),
    (re.compile(r"\b\d{18}\b"), "CUENTA", 0),
    (re.compile(r"\b\d{4}[ -]\d{4}[ -]\d{4}[ -]\d{4}\b|\b\d{16}\b"), "CUENTA", 0),
    (re.compile(r"(?i:cuenta(?:\s+bancaria)?(?:\s+(?:n[uú]mero|n[uú]m\.|no\.))?)\s*:?\s*(\d{8,})"), "CUENTA", 1),
    (re.compile(r"(?<![\d.,/])(?:\+?52[ -]?)?(?:\(\d{2,3}\)[ -]?|\d{2,3}[ -])\d{3,4}[ -]\d{4}(?![\d/])|\b\d{10}\b"), "TELEFONO", 0),
    (re.compile(r"(?i:clave\s+de\s+elector|credencial\s+para\s+votar|pasaporte|c[eé]dula\s+profesional|nss|"
                r"seguridad\s+social)[^\n\d]{0,30}?([A-Z0-9]{6,18})\b"), "IDENTIFICACION", 1),
    (re.compile(r"(?i:expediente|exp\.|juicio|amparo(?:\s+(?:directo|indirecto|en\s+revisi[oó]n))?|toca|causa\s+penal|"
                r"carpeta\s+de\s+investigaci[oó]n)(?:\s+(?:n[uú]mero|n[uú]m\.|no\.))?\s*:?\s*([A-Z0-9-]*\d+/\d+(?:[-/][A-Z0-9]+)*)"),
     "EXPEDIENTE", 1),
    (re.compile(r"(?:(?i:calle|avenida|av\.|boulevard|blvd\.|bulevar|callej[oó]n|andador)|Privada|Calzada|Prolongaci[oó]n)\s+"
                r"[^\n;]{2,60}?(?:(?i:n[uú]mero|n[uú]m\.|no\.|\#)\s*)?\d+[A-Za-z]?"
                r"(?:[^\n.;]{0,20}?(?i:interior|int\.)\s*\w+)?"
                r"(?:,?\s*(?i:colonia|col\.|fraccionamiento|fracc\.)\s+[^\n,.;]{2,40})?"
                r"(?:,?\s*(?i:c\.\s?p\.|c[oó]digo\s+postal)\s*\d{5})?"), "DOMICILIO", 0),
)

_nlp = None
_sin_detector = False
_candado = threading.Lock()


def _detector():
    """El detector de nombres, cargado una vez por proceso y solo cuando se usa (ocupa memoria)."""
    global _nlp, _sin_detector
    if _nlp is None and not _sin_detector:
        with _candado:
            if _nlp is None and not _sin_detector:
                try:
                    import spacy
                    _nlp = spacy.load(MODELO_NER)
                except Exception as exc:
                    _sin_detector = True
                    log.error("Marcas: no se pudo cargar el detector de nombres (%s)", type(exc).__name__)
    return _nlp


def disponible() -> bool:
    return _detector() is not None


def _plano(texto: str) -> str:
    t = unicodedata.normalize("NFKD", str(texto or "").lower())
    return " ".join("".join(c for c in t if not unicodedata.combining(c)).split())


def _palabras(texto: str) -> list[str]:
    return re.findall(r"\w+", _plano(texto))


_VARIANTES = {"a": "aá", "e": "eé", "i": "ií", "o": "oó", "u": "uúü", "n": "nñ"}


def _forma(valor: str) -> str:
    """Patrón que encuentra el valor aunque cambien mayúsculas, acentos o espacios."""
    salida = []
    for c in _plano(valor):
        if c == " ":
            salida.append(r"\s+")
        elif c in _VARIANTES:
            salida.append(f"[{_VARIANTES[c]}]")
        else:
            salida.append(re.escape(c))
    return "".join(salida)


def _sin_mayusculas(texto: str) -> str:
    """Los escritos ponen los nombres en MAYÚSCULAS y el detector no los reconoce. Conserva la longitud."""
    def nombre_propio(m):
        return re.sub(r"\S+", lambda w: w.group(0).capitalize(), m.group(0))
    return re.sub(rf"\b[{_MAY}]{{2,}}(?:\s+[{_MAY}]{{2,}})+\b", nombre_propio, texto)


def _limpiar_nombre(nombre: str) -> str:
    nombre = " ".join(nombre.split()).strip(" ,.;:")
    return re.sub(r"(?i)^(?:c|sr|sra|srta|lic|licda|mtro|mtra|dr|dra|ing|arq)\.\s+|^(?:don|doña)\s+", "", nombre)


def _es_nombre(nombre: str, minimo: int) -> bool:
    if "[" in nombre or re.search(r"\d", nombre):
        return False
    palabras = [p for p in _palabras(nombre) if p not in _CONECTORES]
    return len(palabras) >= minimo and not any(p in _NO_ES_NOMBRE for p in palabras)


def _empresa_sin_tipo(valor: str) -> str | None:
    """«Constructora del Bajío, S.A. de C.V.» también aparece como «Constructora del Bajío»."""
    palabras = valor.replace(",", " ").split()
    while palabras and (_plano(palabras[-1]).replace(".", "") in _TIPO_SOCIEDAD | _CONECTORES
                        or len(_plano(palabras[-1]).replace(".", "")) <= 2):
        palabras.pop()
    resto = " ".join(palabras)
    return resto if resto and resto != valor else None


class Marcas:
    """Cambia datos personales por marcas y los devuelve. Con `reservado` no hay vuelta atrás: todo queda
    como [RESERVADO], para lo que quien pregunta no puede ver."""

    def __init__(self, reservado: bool = False):
        self.reservado = reservado
        self._original: dict[str, str] = {}       # marca → valor real
        self._vistas: dict[str, set[str]] = {}    # marca → como apareció en los textos
        self._marca_de: dict[str, str] = {}       # valor en plano → marca
        self._por_inicio: dict[str, list[str]] = {}  # primera palabra → valores en plano
        self._cuenta: dict[str, int] = {}
        self._personas: dict[str, set[str]] = {}  # marca → palabras del nombre

    def _nueva(self, tipo: str, valor: str) -> str:
        if self.reservado:
            return RESERVADO
        n = self._cuenta[tipo] = self._cuenta.get(tipo, 0) + 1
        marca = f"[{tipo}]" if tipo in _UNICAS and n == 1 else f"[{tipo}_{n}]"
        self._original[marca] = valor
        return marca

    def _registrar(self, valor: str, marca: str) -> None:
        plano = _plano(valor)
        if len(plano) < 3 or plano in self._marca_de:
            return
        self._marca_de[plano] = marca
        palabras = _palabras(plano)
        if palabras:
            self._por_inicio.setdefault(palabras[0], []).append(plano)

    def conocer(self, valor, tipo: str = "PERSONA", clase: str = "persona") -> None:
        """Registra un dato. `clase`: persona (también busca nombre y apellidos sueltos), empresa o dato."""
        valor = " ".join(str(valor or "").split()).strip(" ,;")
        if len(_plano(valor)) < 3:
            return
        marca = self._marca_de.get(_plano(valor)) or self._nueva(tipo, valor)
        self._registrar(valor, marca)
        if clase == "empresa":
            resto = _empresa_sin_tipo(valor)
            if resto and (len(_palabras(resto)) >= 2 or tipo in _UNICAS):
                self._registrar(resto, marca)
        elif clase == "persona":
            todas = valor.split()
            reales = [p for p in todas if _plano(p) not in _CONECTORES]
            self._personas.setdefault(marca, set()).update(_palabras(valor))
            if len(reales) >= 3:
                nombre, apellidos = reales[0], " ".join(reales[-2:])
                for variante in (f"{nombre} {reales[-2]}", apellidos, f"{apellidos} {nombre}", " ".join(todas[:-1])):
                    self._registrar(variante, marca)

    def _sustituir_conocidos(self, texto: str) -> str:
        presentes = set(re.findall(r"\w+", _plano(texto)))
        valores = [v for p in presentes for v in self._por_inicio.get(p, ())]
        if not valores:
            return texto
        patron = "|".join(_forma(v) for v in sorted(valores, key=len, reverse=True))

        def cambiar(m):
            marca = self._marca_de.get(_plano(m.group(0)))
            if not marca:
                return m.group(0)
            self._vistas.setdefault(marca, set()).add(m.group(0))
            return marca
        return re.sub(rf"(?<![\w\[])(?:{patron})(?![\w\]])", cambiar, texto, flags=re.I)

    def _sustituir_patrones(self, texto: str) -> str:
        for patron, tipo, grupo in _PATRONES:
            def cambiar(m, tipo=tipo, grupo=grupo):
                valor = m.group(grupo)
                if "[" in valor:
                    return m.group(0)
                plano = _plano(valor)
                marca = self._marca_de.get(plano)
                if not marca:
                    marca = self._nueva(tipo, valor)
                    self._registrar(valor, marca)
                self._vistas.setdefault(marca, set()).add(valor)
                return m.group(0).replace(valor, marca)
            texto = patron.sub(cambiar, texto)
        return texto

    def _detectar(self, texto: str) -> None:
        """Aprende los nombres que no estaban registrados. Los apellidos sueltos se unen a su persona."""
        copia = _sin_mayusculas(texto)
        for m in _SOCIEDAD.finditer(copia):
            palabras = texto[m.start(1):m.end()].split()
            while palabras and _plano(palabras[0]).strip(".,") in _NO_ES_NOMBRE | _CONECTORES:
                palabras.pop(0)
            if len(palabras) >= 2:
                self.conocer(" ".join(palabras), "EMPRESA", "empresa")
        sueltos = []
        for m in _CON_TITULO.finditer(copia):
            nombre = _limpiar_nombre(texto[m.start(1):m.end(1)])
            if _es_nombre(nombre, 2):
                self.conocer(nombre)
            elif _es_nombre(nombre, 1):
                sueltos.append(nombre)
        nlp = _detector()
        if nlp is None:
            return
        for inicio in range(0, len(copia), BLOQUE_NER):
            for e in nlp(copia[inicio:inicio + BLOQUE_NER]).ents:
                if e.label_ == "PER":
                    nombre = _limpiar_nombre(texto[inicio + e.start_char:inicio + e.end_char])
                    if _es_nombre(nombre, 2):
                        self.conocer(nombre)
        for nombre in sueltos:
            palabra = _plano(nombre)
            marca = next((k for k, v in self._personas.items() if palabra in v), None)
            if marca:
                self._registrar(nombre, marca)
            else:
                self.conocer(nombre)

    def poner(self, texto) -> str:
        """El texto con los datos personales cambiados por marcas."""
        if not texto:
            return texto or ""
        texto = self._sustituir_patrones(self._sustituir_conocidos(str(texto)))
        self._detectar(texto)
        return self._sustituir_conocidos(texto)

    def quitar(self, texto) -> str:
        """El texto con los valores reales en lugar de las marcas."""
        if not texto:
            return texto or ""
        return PATRON_MARCA.sub(lambda m: self._original.get(m.group(0), m.group(0)), str(texto))

    def literal(self, cita: str, original: str) -> str:
        """Una cita con marcas, tal como está escrita en el documento original."""
        piezas = []
        for i, trozo in enumerate(re.split(f"({PATRON_MARCA.pattern})", cita)):
            if i % 2:
                formas = self._vistas.get(trozo) or {self._original.get(trozo, trozo)}
                piezas.append("(?:" + "|".join(r"\s+".join(map(re.escape, f.split())) for f in formas) + ")")
            else:
                piezas.append(r"\s+".join(map(re.escape, trozo.split())) + (r"\s*" if trozo[-1:].isspace() else ""))
        encontrada = re.search(r"\s*".join(p for p in piezas if p), original, re.I)
        return " ".join(encontrada.group(0).split()) if encontrada else " ".join(self.quitar(cita).split())


class Restaurador:
    """Devuelve los valores reales en una respuesta que llega a trozos: una marca puede venir partida."""

    def __init__(self, marcas: Marcas):
        self._marcas = marcas
        self._pendiente = ""

    def trozo(self, texto: str) -> str:
        texto = self._pendiente + texto
        self._pendiente = ""
        i = texto.rfind("[")
        if i != -1 and "]" not in texto[i:] and len(texto) - i < 40:
            texto, self._pendiente = texto[:i], texto[i:]
        return self._marcas.quitar(texto)

    def fin(self) -> str:
        texto, self._pendiente = self._pendiente, ""
        return self._marcas.quitar(texto)


# ── Lo que el despacho ya tiene registrado ───────────────────────────────────

def _filas(conn, sql: str, parametros: tuple = ()) -> list:
    try:
        return conn.execute(sql, parametros).fetchall()
    except sqlite3.OperationalError:
        return []


def _nombre_cliente(c) -> tuple[str, str]:
    if c["razon_social"]:
        return c["razon_social"], "empresa"
    return " ".join(p for p in (c["nombre"], c["apellidos"]) if p), "persona"


def conocer_despacho(conn, marcas: Marcas) -> None:
    """Clientes y contrapartes de todo el despacho: un documento puede mencionar a alguien de otro asunto."""
    for c in _filas(conn, "SELECT nombre, apellidos, razon_social FROM clientes"):
        nombre, clase = _nombre_cliente(c)
        if len(_palabras(nombre)) >= 2:
            marcas.conocer(nombre, "EMPRESA" if clase == "empresa" else "PERSONA", clase)
    for c in _filas(conn, "SELECT nombre FROM contrapartes"):
        if len(_palabras(c["nombre"])) >= 2:
            marcas.conocer(c["nombre"], "PERSONA", "dato" if _empresa_sin_tipo(c["nombre"]) else "persona")


def conocer_asunto(conn, marcas: Marcas, asunto_id: int, principal: bool = True) -> None:
    """Las partes de un asunto. En el asunto principal llevan su papel: [CLIENTE], [CONTRAPARTE]…"""
    a = next(iter(_filas(conn, "SELECT * FROM asuntos WHERE id=?", (asunto_id,))), None)
    if a is None:
        return
    papel = (lambda rol, otro="PERSONA": rol) if principal else (lambda rol, otro="PERSONA": otro)
    cliente = next(iter(_filas(conn, "SELECT * FROM clientes WHERE id=?", (a["cliente_id"],))), None)
    if cliente is not None:
        nombre, clase = _nombre_cliente(cliente)
        marcas.conocer(nombre, papel("CLIENTE", "EMPRESA" if clase == "empresa" else "PERSONA"), clase)
        marcas.conocer(cliente["representante_legal"], "PERSONA")
        marcas.conocer(cliente["contacto_principal"], "PERSONA")
        for campo, tipo in (("rfc", "RFC"), ("curp", "CURP"), ("email", "CORREO"), ("telefono", "TELEFONO"),
                            ("celular", "TELEFONO")):
            marcas.conocer(cliente[campo], tipo, "dato")
        calle = " ".join(p for p in (cliente["direccion"], cliente["num_ext"]) if p)
        marcas.conocer(calle, "DOMICILIO", "dato")
    marcas.conocer(a["contrario_nombre"], papel("CONTRAPARTE"))
    marcas.conocer(a["contrario_abogado"], papel("ABOGADO_CONTRARIO"))
    marcas.conocer(a["contrario_despacho"], papel("DESPACHO_CONTRARIO", "EMPRESA"), "empresa")
    marcas.conocer(a["numero_expediente"], "EXPEDIENTE", "dato")
    marcas.conocer(a["numero_juicio"], "EXPEDIENTE", "dato")
    for c in _filas(conn, "SELECT c.* FROM contrapartes c JOIN contraparte_asunto ca ON ca.contraparte_id=c.id "
                          "WHERE ca.asunto_id=?", (asunto_id,)):
        marcas.conocer(c["nombre"], papel("CONTRAPARTE"))
        marcas.conocer(c["abogado_nombre"], papel("ABOGADO_CONTRARIO"))
        marcas.conocer(c["despacho"], papel("DESPACHO_CONTRARIO", "EMPRESA"), "empresa")
        for campo, tipo in (("rfc", "RFC"), ("email", "CORREO"), ("telefono", "TELEFONO")):
            marcas.conocer(c[campo], tipo, "dato")
