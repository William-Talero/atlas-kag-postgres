"""Motor de grafo sobre Apache AGE.

Regla de la casa: **el texto Cypher es siempre estatico**. Todo valor dinamico
viaja en el objeto de parametros agtype, que AGE recibe como tercer argumento y
exige que sea un parametro del protocolo, nunca una constante interpolada.
Lo unico que se compone en el texto son etiquetas y profundidades, y ambas pasan
por una lista blanca derivada de la ontologia.
"""

from __future__ import annotations

import json
import re
import time
from functools import lru_cache
from typing import Any

from kag import agtype
from kag.conexion import conexion
from kag.ontologia import (DIMENSIONES, ETIQUETAS, GRAFO, RELACIONES,
                           dimension_de_relacion, fuente_de, nombre_visible)

_TAG = "$atlas$"
_CLAVE_VALIDA = re.compile(r"^[a-z]{3,12}:[A-Za-z0-9_\-]{1,48}$")


class ConsultaInvalida(ValueError):
    pass


def validar_clave(clave: str) -> str:
    if not _CLAVE_VALIDA.match(clave or ""):
        raise ConsultaInvalida(f"Clave de nodo no valida: {clave!r}")
    return clave


def validar_etiqueta(etiqueta: str) -> str:
    if etiqueta not in ETIQUETAS:
        raise ConsultaInvalida(f"Etiqueta desconocida: {etiqueta!r}")
    return etiqueta


def validar_relaciones(relaciones: list[str] | None) -> list[str]:
    if not relaciones:
        return []
    malas = [r for r in relaciones if r not in RELACIONES]
    if malas:
        raise ConsultaInvalida(f"Relaciones desconocidas: {malas}")
    return relaciones


def _sql(texto: str, columnas: list[str]) -> str:
    cols = ", ".join(f"{c} agtype" for c in columnas)
    return f"SELECT * FROM cypher('{GRAFO}', {_TAG}{texto}{_TAG}, %s) AS ({cols})"


def _texto_sql(valor: str) -> str:
    """Literal de texto para componer un UNION ALL sobre nombres del catalogo."""
    return "'" + valor.replace("'", "''") + "'"


class Resultado(dict):
    """Filas, el Cypher exacto que se ejecuto y la latencia medida."""


def ejecutar(texto: str, params: dict[str, Any] | None = None,
             columnas: list[str] | None = None) -> Resultado:
    columnas = columnas or ["resultado"]
    if _TAG in texto:
        raise ConsultaInvalida("El texto Cypher no puede contener el delimitador")
    t0 = time.perf_counter()
    with conexion() as con, con.cursor() as cur:
        cur.execute(_sql(texto, columnas), (json.dumps(params or {}, ensure_ascii=False),))
        crudas = cur.fetchall()
    filas = [dict(zip(columnas, agtype.fila(f))) for f in crudas]
    return Resultado(
        cypher=texto.strip(), params=params or {}, filas=filas, total=len(filas),
        ms=round((time.perf_counter() - t0) * 1000, 2), motor="postgresql-age",
    )


# --------------------------------------------------------------------- esquema

def esquema() -> dict[str, Any]:
    """Conteos reales leidos del catalogo de AGE, no estimados."""
    t0 = time.perf_counter()
    with conexion() as con, con.cursor() as cur:
        cur.execute(
            "SELECT l.name, l.kind, l.relation::regclass::text "
            "FROM ag_catalog.ag_label l JOIN ag_catalog.ag_graph g ON g.graphid = l.graph "
            "WHERE g.name = %s AND l.name NOT IN ('_ag_label_vertex','_ag_label_edge')",
            (GRAFO,),
        )
        etiquetas = cur.fetchall()
        conteos: dict[str, int] = {}
        if etiquetas:
            # Un UNION ALL en vez de un count por tabla: son ~39 tablas y cada
            # viaje a Central US cuesta mas que la propia cuenta.
            union = " UNION ALL ".join(
                f"SELECT {_texto_sql(nombre)} AS etiqueta, count(*) AS n FROM {relacion}"
                for nombre, _kind, relacion in etiquetas)
            cur.execute(union)
            conteos = {f[0]: int(f[1]) for f in cur.fetchall()}

    nodos, aristas = [], []
    for nombre, kind, _rel in etiquetas:
        if kind == "v":
            dim = ETIQUETAS.get(nombre, ("entidad", ""))[0]
            nodos.append({"etiqueta": nombre, "dimension": dim,
                          "conteo": conteos.get(nombre, 0)})
        else:
            aristas.append({"relacion": nombre,
                            "dimension": dimension_de_relacion(nombre),
                            "fuente": fuente_de(nombre),
                            "conteo": conteos.get(nombre, 0)})

    nodos.sort(key=lambda n: -n["conteo"])
    aristas.sort(key=lambda a: -a["conteo"])
    por_dimension = []
    for clave, meta in DIMENSIONES.items():
        por_dimension.append({
            "dimension": clave, **meta,
            "nodos": sum(n["conteo"] for n in nodos if n["dimension"] == clave),
            "aristas": sum(a["conteo"] for a in aristas if a["dimension"] == clave),
            "etiquetas": [n["etiqueta"] for n in nodos if n["dimension"] == clave],
        })

    return {
        "nodos": nodos, "aristas": aristas, "dimensiones": por_dimension,
        "totales": {
            "vertices": sum(n["conteo"] for n in nodos),
            "aristas": sum(a["conteo"] for a in aristas),
            "etiquetas": len(nodos), "relaciones": len(aristas),
            "dimensiones": len(DIMENSIONES),
        },
        "ms": round((time.perf_counter() - t0) * 1000, 2),
    }


# ---------------------------------------------------------------------- lectura

def nodo(clave: str) -> dict[str, Any] | None:
    validar_clave(clave)
    res = ejecutar(
        "MATCH (n {clave: $clave}) RETURN n LIMIT 1",
        {"clave": clave}, ["n"],
    )
    if not res["filas"]:
        return None
    v = agtype.nodo_plano(res["filas"][0]["n"])
    v["nombre"] = nombre_visible(v)
    v["dimension"] = ETIQUETAS.get(v["label"], ("entidad", ""))[0]
    return v


def buscar(termino: str, limite: int = 15) -> Resultado:
    """Busqueda lexica sobre los campos identificatorios de cualquier etiqueta."""
    limite = max(1, min(int(limite), 100))
    res = ejecutar(
        "MATCH (n) "
        "WHERE toLower(n.nombre) CONTAINS $t OR toLower(n.titulo) CONTAINS $t "
        "   OR toLower(n.documento) CONTAINS $t OR toLower(n.nit) CONTAINS $t "
        "   OR toLower(n.referencia) CONTAINS $t OR toLower(n.tipo) CONTAINS $t "
        "   OR toLower(n.etiqueta) CONTAINS $t OR toLower(n.clave) CONTAINS $t "
        f"RETURN n LIMIT {limite}",
        {"t": (termino or "").strip().lower()}, ["n"],
    )
    filas = []
    for f in res["filas"]:
        v = agtype.nodo_plano(f["n"])
        if not v:
            continue
        filas.append({
            "clave": v["clave"], "label": v["label"],
            "nombre": nombre_visible(v),
            "dimension": ETIQUETAS.get(v["label"], ("entidad", ""))[0],
            "props": v["props"],
        })
    res["filas"] = filas
    res["total"] = len(filas)
    return res


def expandir(clave: str, profundidad: int = 1,
             relaciones: list[str] | None = None, limite: int = 220) -> Resultado:
    """Vecindario del nodo hasta N saltos, con la procedencia de cada arista."""
    validar_clave(clave)
    profundidad = max(1, min(int(profundidad), 4))
    limite = max(1, min(int(limite), 1500))
    filtro = validar_relaciones(relaciones)
    tipos = (":" + "|".join(filtro)) if filtro else ""
    res = ejecutar(
        f"MATCH p = (a {{clave: $clave}})-[{tipos}*1..{profundidad}]-(b) "
        f"RETURN p LIMIT {limite}",
        {"clave": clave}, ["p"],
    )
    return _caminos_a_subgrafo(res, clave)


def camino(desde: str, hasta: str, max_saltos: int = 5) -> Resultado:
    """El camino mas corto. Es la explicacion de por que dos nodos se tocan."""
    validar_clave(desde)
    validar_clave(hasta)
    max_saltos = max(1, min(int(max_saltos), 8))
    res = ejecutar(
        f"MATCH p = (a {{clave: $desde}})-[*1..{max_saltos}]-(b {{clave: $hasta}}) "
        "RETURN p LIMIT 1",
        {"desde": desde, "hasta": hasta}, ["p"],
    )
    return _caminos_a_subgrafo(res, desde)


def subgrafo(limite: int = 900) -> Resultado:
    limite = max(10, min(int(limite), 6000))
    res = ejecutar(
        f"MATCH (a)-[r]->(b) RETURN a, r, b LIMIT {limite}", {}, ["a", "r", "b"])
    nodos: dict[str, dict] = {}
    aristas: list[dict] = []
    por_id: dict[str, str] = {}
    _acumular(res["filas"], nodos, aristas, por_id)
    res["filas"] = []
    res["nodos"] = list(nodos.values())
    res["aristas"] = [a for a in aristas if a["desde"] and a["hasta"]]
    res["total"] = len(res["nodos"])
    return res


def _acumular(filas: list[dict], nodos: dict[str, dict], aristas: list[dict],
              por_id: dict[str, str]) -> None:
    for f in filas:
        for extremo in ("a", "b"):
            v = agtype.nodo_plano(f.get(extremo))
            if v:
                por_id[v["_id"]] = v["clave"]
                nodos.setdefault(v["clave"], _resumen(v))
        r = f.get("r")
        if isinstance(r, dict) and r.get("_tipo") == "edge":
            aristas.append(_arista(r, por_id))


@lru_cache(maxsize=4)
def _muestra(por_relacion: int) -> tuple:
    """Muestra con TODAS las relaciones representadas.

    Un `LIMIT n` sobre el conjunto de aristas devuelve una sola relacion —la
    primera que encuentra el planificador, aqui TITULAR_DE— y da la impresion de
    un grafo plano de tres etiquetas. Muestreando por tipo se ve el modelo: las
    catorce etiquetas y las seis dimensiones aparecen aunque una relacion tenga
    cuatro aristas en todo el grafo.

    Va como un solo UNION ALL y no como veinticinco consultas: cada viaje a la
    region cuesta mas que la propia lectura, y en secuencia tardaba quince
    segundos.
    """
    t0 = time.perf_counter()
    bloques = [
        f"SELECT * FROM cypher('{GRAFO}', {_TAG} MATCH (a)-[r:{relacion}]->(b) "
        f"RETURN a, r, b LIMIT {por_relacion} {_TAG}) AS (a agtype, r agtype, b agtype)"
        for relacion in RELACIONES
    ]
    with conexion() as con, con.cursor() as cur:
        cur.execute(" UNION ALL ".join(bloques))
        crudas = cur.fetchall()

    filas = [dict(zip(("a", "r", "b"), agtype.fila(f))) for f in crudas]
    nodos: dict[str, dict] = {}
    aristas: list[dict] = []
    por_id: dict[str, str] = {}
    _acumular(filas, nodos, aristas, por_id)
    presentes = len({a["relacion"] for a in aristas})

    return (
        list(nodos.values()),
        [a for a in aristas if a["desde"] and a["hasta"]],
        presentes,
        round((time.perf_counter() - t0) * 1000, 2),
    )


def muestra(por_relacion: int = 10) -> Resultado:
    por_relacion = max(1, min(int(por_relacion), 60))
    nodos, aristas, presentes, ms = _muestra(por_relacion)
    return Resultado(
        cypher=(f"MATCH (a)-[r:<cada relación>]->(b)\n"
                f"RETURN a, r, b LIMIT {por_relacion}"),
        params={"por_relacion": por_relacion}, filas=[],
        nodos=list(nodos), aristas=list(aristas),
        relaciones_representadas=presentes, total=len(nodos),
        ms=ms, motor="postgresql-age",
    )


# ------------------------------------------------------------------ auxiliares

def _resumen(v: dict) -> dict[str, Any]:
    return {
        "clave": v["clave"], "label": v["label"], "nombre": nombre_visible(v),
        "dimension": ETIQUETAS.get(v["label"], ("entidad", ""))[0],
        "props": v["props"],
    }


def _arista(r: dict, por_id: dict[str, str]) -> dict[str, Any]:
    rel = r.get("label", "")
    return {
        "desde": por_id.get(r.get("_desde", ""), ""),
        "hasta": por_id.get(r.get("_hasta", ""), ""),
        "relacion": rel,
        "dimension": dimension_de_relacion(rel),
        "fuente": (r.get("props") or {}).get("fuente") or fuente_de(rel),
    }


def _caminos_a_subgrafo(res: Resultado, raiz: str) -> Resultado:
    """Aplana la lista de caminos en nodos + aristas, conservando los saltos."""
    nodos: dict[str, dict] = {}
    por_id: dict[str, str] = {}
    aristas: dict[tuple, dict] = {}
    saltos: list[dict] = []

    for f in res["filas"]:
        camino_ = f.get("p") or []
        if not isinstance(camino_, list):
            continue
        for elem in camino_:
            if isinstance(elem, dict) and elem.get("_tipo") == "vertex":
                v = agtype.nodo_plano(elem)
                por_id[v["_id"]] = v["clave"]
                nodos.setdefault(v["clave"], _resumen(v))
        for elem in camino_:
            if isinstance(elem, dict) and elem.get("_tipo") == "edge":
                a = _arista(elem, por_id)
                if a["desde"] and a["hasta"]:
                    aristas[(a["desde"], a["relacion"], a["hasta"])] = a

    for a in aristas.values():
        o, d = nodos.get(a["desde"], {}), nodos.get(a["hasta"], {})
        saltos.append({
            "origen": a["desde"], "origen_label": o.get("label", ""),
            "origen_nombre": o.get("nombre", ""),
            "relacion": a["relacion"], "dimension": a["dimension"],
            "destino": a["hasta"], "destino_label": d.get("label", ""),
            "destino_nombre": d.get("nombre", ""),
            "fuente": a["fuente"],
        })

    res["filas"] = []
    res["raiz"] = raiz
    res["nodos"] = list(nodos.values())
    res["aristas"] = list(aristas.values())
    res["saltos"] = saltos
    res["total"] = len(nodos)
    return res
