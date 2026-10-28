"""Modelo de búsqueda semántica local: el texto nunca sale del servidor para indexarse."""
from __future__ import annotations

import os
import threading

import numpy as np

MODELO = os.environ.get("LECTOR_MODELO", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
_modelo = None
_candado = threading.Lock()


def _cargar(carpeta: str):
    global _modelo
    with _candado:
        if _modelo is None:
            from fastembed import TextEmbedding
            os.makedirs(carpeta, exist_ok=True)
            _modelo = TextEmbedding(model_name=MODELO, cache_dir=carpeta)
    return _modelo


def vectorizar(textos: list[str], carpeta: str) -> np.ndarray:
    """Vectores normalizados (float32) para calcular similitud con producto punto."""
    if not textos:
        return np.zeros((0, 0), dtype=np.float32)
    vecs = np.array(list(_cargar(carpeta).embed(textos, batch_size=64)), dtype=np.float32)
    normas = np.linalg.norm(vecs, axis=1, keepdims=True)
    normas[normas == 0] = 1.0
    return vecs / normas
