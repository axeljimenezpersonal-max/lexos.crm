"""
Texto de documentos de Word 97-2003 (.doc), sin dependencias externas.

El Orden Jurídico Nacional publica la legislación estatal en .doc binario. Aquí se lee el contenedor OLE
(Compound File Binary) y la tabla de piezas del documento (Clx/PlcPcd) para sacar el texto plano.
Solo extrae texto: sin formato, sin tablas como tablas, sin imágenes.
"""
from __future__ import annotations

import re
import struct

FIRMA = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_FIN = 0xFFFFFFFE
_LIBRE = 0xFFFFFFFF


class ErrorDoc(ValueError):
    pass


class _CFB:
    def __init__(self, datos: bytes):
        if datos[:8] != FIRMA:
            raise ErrorDoc("no es un documento OLE")
        self.d = datos
        self.tam = 1 << struct.unpack_from("<H", datos, 0x1E)[0]
        self.tam_mini = 1 << struct.unpack_from("<H", datos, 0x20)[0]
        n_fat, dir_ini = struct.unpack_from("<II", datos, 0x2C)
        self.corte = struct.unpack_from("<I", datos, 0x38)[0]
        minifat_ini, n_minifat, difat_ini, n_difat = struct.unpack_from("<IIII", datos, 0x3C)
        difat = list(struct.unpack_from("<109I", datos, 0x4C))
        s, vistos = difat_ini, 0
        while s not in (_FIN, _LIBRE) and vistos < n_difat:
            por = self.tam // 4
            vals = struct.unpack_from(f"<{por}I", datos, self._pos(s))
            difat += vals[:-1]
            s, vistos = vals[-1], vistos + 1
        self.fat: list[int] = []
        for sec in difat[:n_fat]:
            if sec in (_FIN, _LIBRE):
                continue
            self.fat += struct.unpack_from(f"<{self.tam // 4}I", datos, self._pos(sec))
        self.minifat: list[int] = []
        if n_minifat and minifat_ini not in (_FIN, _LIBRE):
            crudo = self._cadena(minifat_ini)
            self.minifat = list(struct.unpack(f"<{len(crudo) // 4}I", crudo[: len(crudo) // 4 * 4]))
        self.entradas = {}
        crudo = self._cadena(dir_ini)
        raiz = None
        for i in range(len(crudo) // 128):
            e = crudo[i * 128:(i + 1) * 128]
            largo = struct.unpack_from("<H", e, 0x40)[0]
            tipo = e[0x42]
            if not tipo or largo < 2:
                continue
            nombre = e[:largo - 2].decode("utf-16-le", "replace")
            ini, tam = struct.unpack_from("<II", e, 0x74)
            if tipo == 5:
                raiz = (ini, tam)
            self.entradas[nombre] = (ini, tam)
        self.mini = self._cadena(raiz[0])[: raiz[1]] if raiz and raiz[0] not in (_FIN, _LIBRE) else b""

    def _pos(self, sec: int) -> int:
        return (sec + 1) * self.tam

    def _cadena(self, ini: int) -> bytes:
        partes, s, vueltas = [], ini, 0
        while s not in (_FIN, _LIBRE) and s < len(self.fat) and vueltas <= len(self.fat):
            partes.append(self.d[self._pos(s):self._pos(s) + self.tam])
            s, vueltas = self.fat[s], vueltas + 1
        return b"".join(partes)

    def flujo(self, nombre: str) -> bytes:
        if nombre not in self.entradas:
            raise ErrorDoc(f"falta el flujo {nombre}")
        ini, tam = self.entradas[nombre]
        if tam < self.corte:
            partes, s, vueltas = [], ini, 0
            while s not in (_FIN, _LIBRE) and s < len(self.minifat) and vueltas <= len(self.minifat):
                partes.append(self.mini[s * self.tam_mini:(s + 1) * self.tam_mini])
                s, vueltas = self.minifat[s], vueltas + 1
            return b"".join(partes)[:tam]
        return self._cadena(ini)[:tam]


def _limpiar_campos(texto: str) -> str:
    """Quita el código de los campos de Word (entre \\x13 y \\x14) y deja su resultado."""
    salida, pila = [], []   # pila: True mientras estamos en la instrucción del campo
    for ch in texto:
        if ch == "\x13":
            pila.append(True)
        elif ch == "\x14":
            if pila:
                pila[-1] = False
        elif ch == "\x15":
            if pila:
                pila.pop()
        elif not (pila and pila[-1]):
            salida.append(ch)
    return "".join(salida)


def texto(datos: bytes) -> str:
    cfb = _CFB(datos)
    wd = cfb.flujo("WordDocument")
    if struct.unpack_from("<H", wd, 0)[0] != 0xA5EC:
        raise ErrorDoc("no es un documento de Word")
    banderas = struct.unpack_from("<H", wd, 0x0A)[0]
    if banderas & 0x0100:
        raise ErrorDoc("documento cifrado")
    tabla = cfb.flujo("1Table" if banderas & 0x0200 else "0Table")
    pos = 32
    csw = struct.unpack_from("<H", wd, pos)[0]
    pos += 2 + csw * 2
    cslw = struct.unpack_from("<H", wd, pos)[0]
    pos += 2 + cslw * 4
    pos += 2   # cbRgFcLcb
    fc_clx, lcb_clx = struct.unpack_from("<II", wd, pos + 33 * 8)
    clx = tabla[fc_clx:fc_clx + lcb_clx]
    i = 0
    while i < len(clx) and clx[i] == 0x01:          # Prc: propiedades, se saltan
        i += 3 + struct.unpack_from("<H", clx, i + 1)[0]
    if i >= len(clx) or clx[i] != 0x02:
        raise ErrorDoc("sin tabla de piezas")
    lcb = struct.unpack_from("<I", clx, i + 1)[0]
    plc = clx[i + 5:i + 5 + lcb]
    n = (lcb - 4) // 12
    cps = struct.unpack_from(f"<{n + 1}I", plc, 0)
    partes = []
    for k in range(n):
        fc = struct.unpack_from("<I", plc, (n + 1) * 4 + k * 8 + 2)[0]
        largo = cps[k + 1] - cps[k]
        if largo <= 0:
            continue
        if fc & 0x40000000:
            ini = (fc & 0x3FFFFFFF) // 2
            partes.append(wd[ini:ini + largo].decode("cp1252", "replace"))
        else:
            partes.append(wd[fc:fc + 2 * largo].decode("utf-16-le", "replace"))
    crudo = _limpiar_campos("".join(partes))
    crudo = crudo.replace("\r", "\n").replace("\x0b", "\n").replace("\x0c", "\n").replace("\x07", " | ")
    crudo = re.sub(r"[\x00-\x08\x0e-\x1f]", "", crudo)
    lineas = (re.sub(r"[ \t\xa0]+", " ", l).strip(" |") for l in crudo.split("\n"))
    return "\n".join(l for l in lineas if l.strip())
