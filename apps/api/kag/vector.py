"""Recuperacion vectorial sobre pgvector.

Aqui esta la diferencia entre RAG y KAG, y es una sola clausula WHERE:

    RAG  ->  ORDER BY embedding <=> consulta LIMIT k
    KAG  ->  WHERE entidades && (claves del subgrafo)
             ORDER BY embedding <=> consulta LIMIT k

El grafo no reemplaza al vector: le acota el universo. Los dos motores viven en
la misma base, asi que el filtro estructural y la similitud se resuelven en una
sola consulta y no hay que traer candidatos al cliente para cruzarlos.
"""

from __future__ import annotations

import time
from functools import lru_cache
from pathlib import Path
from typing import Any

from kag.conexion import conexion
from kag.embeddings import Embebedor, a_literal, es_nulo
from settings import load_settings

TABLA = "public.kag_fragmento"

SIN_VOCABULARIO = ("Ningún término de la consulta está en el vocabulario del "
                   "corpus: la búsqueda vectorial no puede opinar.")


@lru_cache(maxsize=1)
def embebedor() -> Embebedor:
    return Embebedor.cargar(load_settings()["DATA"] / "embeddings.npz")


def _filas(cur) -> list[dict[str, Any]]:
    return [
        {
            "clave": f[0], "documento": f[1], "titulo": f[2],
            "extracto": _extracto(f[3]), "dimension": f[4],
            "entidades": list(f[5] or []),
            "similitud": round(1.0 - float(f[6]), 4),
        }
        for f in cur.fetchall()
    ]


def _extracto(texto: str, limite: int = 320) -> str:
    t = (texto or "").strip()
    return t if len(t) <= limite else t[:limite].rsplit(" ", 1)[0] + "…"


_COLUMNAS = ("clave, documento, titulo, texto, dimension, entidades, "
             f"embedding <=> %s::vector AS distancia")


def buscar(consulta: str, k: int = 8) -> dict[str, Any]:
    """Linea base RAG: similitud pura sobre todo el corpus."""
    k = max(1, min(int(k), 50))
    crudo = embebedor().vector(consulta)
    if es_nulo(crudo):
        return {"modo": "rag", "consulta": consulta, "resultados": [],
                "degenerado": True, "nota": SIN_VOCABULARIO, "ms": 0.0, "sql": ""}
    v = a_literal(crudo)
    t0 = time.perf_counter()
    with conexion() as con, con.cursor() as cur:
        cur.execute(
            f"SELECT {_COLUMNAS} FROM {TABLA} ORDER BY embedding <=> %s::vector LIMIT {k}",
            (v, v),
        )
        filas = _filas(cur)
    return {
        "modo": "rag", "consulta": consulta, "resultados": filas,
        "ms": round((time.perf_counter() - t0) * 1000, 2),
        "sql": (f"SELECT … FROM {TABLA}\n"
                "ORDER BY embedding <=> $consulta::vector\n"
                f"LIMIT {k}"),
    }


def buscar_en_subgrafo(consulta: str, claves: list[str], k: int = 8) -> dict[str, Any]:
    """KAG: la misma similitud, restringida a lo que el grafo ya demostro conexo.

    El CTE va MATERIALIZED a proposito. Si se deja que el planificador empuje el
    ORDER BY al indice HNSW, la busqueda aproximada recorre solo `ef_search`
    vecinos y el filtro descarta despues: se pierden fragmentos que estaban
    entre los mas parecidos. Con el filtro del grafo aplicado primero el
    conjunto es pequeno y la busqueda exacta sale mas barata que la aproximada.
    """
    k = max(1, min(int(k), 60))
    crudo = embebedor().vector(consulta)
    if es_nulo(crudo):
        return {"modo": "kag", "consulta": consulta, "resultados": [],
                "degenerado": True, "nota": SIN_VOCABULARIO,
                "anclas": len(claves), "ms": 0.0, "sql": ""}
    v = a_literal(crudo)
    t0 = time.perf_counter()
    with conexion() as con, con.cursor() as cur:
        cur.execute(
            "WITH candidatos AS MATERIALIZED ("
            "  SELECT clave, documento, titulo, texto, dimension, entidades, embedding "
            f"  FROM {TABLA} WHERE entidades && %s::text[]"
            ") "
            "SELECT clave, documento, titulo, texto, dimension, entidades, "
            "       embedding <=> %s::vector AS distancia "
            f"FROM candidatos ORDER BY distancia LIMIT {k}",
            (list(claves), v),
        )
        filas = _filas(cur)
    return {
        "modo": "kag", "consulta": consulta, "resultados": filas,
        "anclas": len(claves),
        "ms": round((time.perf_counter() - t0) * 1000, 2),
        "sql": ("WITH candidatos AS MATERIALIZED (\n"
                f"  SELECT … FROM {TABLA}\n"
                "  WHERE entidades && $claves_del_subgrafo::text[]\n"
                ")\n"
                "SELECT …, embedding <=> $consulta::vector AS distancia\n"
                f"FROM candidatos ORDER BY distancia LIMIT {k}"),
    }


def cobertura(claves: list[str]) -> dict[str, Any]:
    """Cuantos fragmentos del corpus tocan el subgrafo. Mide cuanto acota el grafo."""
    with conexion() as con, con.cursor() as cur:
        cur.execute(
            f"SELECT count(*), count(*) FILTER (WHERE entidades && %s::text[]) "
            f"FROM {TABLA}", (list(claves),))
        total, alcanzados = (int(v) for v in cur.fetchone())
    return {
        "corpus": total, "alcanzados": alcanzados,
        "reduccion": round((1 - alcanzados / total) * 100, 1) if total else 0.0,
    }


def documentos_que_relacionan(claves: list[str]) -> list[str]:
    """Fragmentos que mencionan a la vez TODAS las claves dadas.

    Es la prueba honesta de por que el vector solo no alcanza: cuando devuelve
    cero, la relacion existe en el grafo y no fue redactada en ningun documento.
    """
    if len(claves) < 2:
        return []
    with conexion() as con, con.cursor() as cur:
        cur.execute(
            f"SELECT documento FROM {TABLA} WHERE entidades @> %s::text[] LIMIT 20",
            (list(claves),),
        )
        return sorted({f[0] for f in cur.fetchall()})


def estado() -> dict[str, Any]:
    ruta: Path = load_settings()["DATA"] / "embeddings.npz"
    try:
        e = embebedor()
        dims, vocab = e.dimensiones, len(e.vocabulario)
    except Exception:
        dims, vocab = 0, 0
    try:
        with conexion() as con, con.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM {TABLA}")
            total = int(cur.fetchone()[0])
    except Exception:
        total = 0
    return {"fragmentos": total, "dimensiones": dims, "vocabulario": vocab,
            "indice": "hnsw · vector_cosine_ops", "modelo": ruta.name}
