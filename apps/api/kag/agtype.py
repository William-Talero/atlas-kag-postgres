"""Lectura de agtype, el tipo con el que AGE devuelve vertices, aristas y caminos.

AGE no devuelve JSON: devuelve texto con un sufijo de tipo por elemento, y los
anida. Un camino llega asi:

    [{...}::vertex, {...}::edge, {...}::vertex]::path

Por eso los sufijos se retiran en todo el texto antes de parsear, y el tipo de
cada elemento se deduce despues por su forma: una arista trae `start_id`.
"""

from __future__ import annotations

import json
import re
from typing import Any

# Solo se retira el sufijo cuando cierra el valor, para no tocar el interior de
# una cadena que por casualidad contenga la palabra.
_SUFIJO = re.compile(r"::(?:vertex|edge|path|numeric)(?=\s*[,\]\}]|\s*$)")

# Los identificadores de AGE son enteros de 64 bits: superan el rango seguro de
# JavaScript, asi que se convierten a cadena antes de salir hacia el front.
_ID_GRANDE = re.compile(r'("(?:id|start_id|end_id)"\s*:\s*)(\d{10,})')


def leer(valor: Any) -> Any:
    """Convierte un agtype crudo en dict/list/escalar de Python."""
    if valor is None or isinstance(valor, (dict, list, int, float, bool)):
        return valor
    texto = str(valor).strip()
    es_camino = texto.endswith("::path")
    limpio = _ID_GRANDE.sub(r'\1"\2"', _SUFIJO.sub("", texto))
    try:
        dato = json.loads(limpio)
    except json.JSONDecodeError:
        return texto.strip('"')
    if es_camino and isinstance(dato, list):
        return [_marcar(e) for e in dato]
    return _marcar(dato)


def _marcar(dato: Any) -> Any:
    if isinstance(dato, list):
        return [_marcar(e) for e in dato]
    if not isinstance(dato, dict) or "label" not in dato or "properties" not in dato:
        return dato
    arista = "start_id" in dato and "end_id" in dato
    salida: dict[str, Any] = {
        "_tipo": "edge" if arista else "vertex",
        "_id": str(dato.get("id", "")),
        "label": dato.get("label", ""),
        "props": dato.get("properties") or {},
    }
    if arista:
        salida["_desde"] = str(dato["start_id"])
        salida["_hasta"] = str(dato["end_id"])
    return salida


def fila(valores: tuple) -> list[Any]:
    return [leer(v) for v in valores]


def nodo_plano(v: Any) -> dict[str, Any]:
    """Vertice AGE -> dict estable para la API, con la clave de negocio al frente."""
    if not isinstance(v, dict) or v.get("_tipo") != "vertex":
        return {}
    props = dict(v.get("props") or {})
    return {
        "clave": props.get("clave", v.get("_id", "")),
        "label": v.get("label", ""),
        "props": props,
        "_id": v.get("_id", ""),
    }
