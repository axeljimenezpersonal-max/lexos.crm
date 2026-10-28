"""
Proveedor del modelo de lenguaje. Hoy: API de Anthropic. El resto del asistente solo usa
`reformular()` y `responder()`, así que cambiar a un modelo propio no toca nada más.
"""
from __future__ import annotations

import json
import re
from typing import Iterator

PROMPT_REFORMULAR = """Eres un clasificador para un asistente jurídico mexicano. Recibes la pregunta de un abogado
(y quizá contexto previo). Devuelve SOLO un objeto JSON, sin texto alrededor, con esta forma:
{"es_legal": true|false, "consulta": "...", "terminos": ["...", "..."], "materia": "..."}

- es_legal: true si la pregunta es de derecho, práctica jurídica, trámites, plazos, contratos, litigio o la gestión
  de un asunto o documento del despacho. false si no tiene relación con lo legal.
- consulta: la pregunta reformulada en lenguaje técnico-jurídico mexicano, como la buscaría un abogado en la ley
  (incluye la figura jurídica y, si lo sabes con seguridad, el ordenamiento y artículo probables).
- terminos: 3 a 6 palabras o frases clave jurídicas para buscar.
- materia: civil, familiar, mercantil, penal, laboral, amparo, administrativo, fiscal, constitucional u otra."""


class ProveedorAnthropic:
    def __init__(self, api_key: str, modelo: str, modelo_rapido: str):
        import anthropic
        self._cliente = anthropic.Anthropic(api_key=api_key)
        self.modelo = modelo
        self.modelo_rapido = modelo_rapido

    def reformular(self, pregunta: str, contexto: str = "") -> dict:
        contenido = (f"Contexto previo de la conversación:\n{contexto}\n\n" if contexto else "") + f"Pregunta: {pregunta}"
        r = self._cliente.messages.create(
            model=self.modelo_rapido, max_tokens=400, system=PROMPT_REFORMULAR,
            messages=[{"role": "user", "content": contenido}],
        )
        texto = "".join(b.text for b in r.content if getattr(b, "type", "") == "text")
        m = re.search(r"\{.*\}", texto, re.S)
        try:
            datos = json.loads(m.group(0)) if m else {}
        except json.JSONDecodeError:
            datos = {}
        return {
            "es_legal": bool(datos.get("es_legal", True)),
            "consulta": str(datos.get("consulta") or pregunta)[:500],
            "terminos": [str(t)[:80] for t in (datos.get("terminos") or [])][:6],
            "materia": str(datos.get("materia") or "")[:40],
        }

    def responder(self, sistema: str, mensajes: list[dict], max_tokens: int = 1800) -> Iterator[str]:
        with self._cliente.messages.stream(
            model=self.modelo, max_tokens=max_tokens,
            system=[{"type": "text", "text": sistema, "cache_control": {"type": "ephemeral"}}],
            messages=mensajes,
        ) as flujo:
            for texto in flujo.text_stream:
                yield texto


def obtener(config) -> ProveedorAnthropic | None:
    clave = config.get("ANTHROPIC_API_KEY")
    if not clave:
        return None
    return ProveedorAnthropic(clave, config.get("ASISTENTE_MODELO"), config.get("ASISTENTE_MODELO_RAPIDO"))
