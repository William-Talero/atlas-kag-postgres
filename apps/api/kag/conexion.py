"""Conexion a PostgreSQL Flexible con AGE.

Dos decisiones que importan:

1. Cursor estandar, no ClientCursor. AGE rechaza una constante en el tercer
   argumento de `cypher()` con "third argument of cypher function must be a
   parameter": exige un parametro real del protocolo extendido. Eso tambien lo
   hace la opcion segura, porque el valor nunca se concatena al texto.
2. Token de Entra ID. En Azure no hay contrasena almacenada: la contrasena es un
   token que vence, asi que el pool se reconstruye antes del vencimiento.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator

import psycopg
from psycopg_pool import ConnectionPool

from settings import load_settings

_ALCANCE = "https://ossrdbms-aad.database.windows.net/.default"
_SIN_VENCIMIENTO = float("inf")

_lock = threading.Lock()
_token: tuple[str, float] = ("", 0.0)
_pool: ConnectionPool | None = None
_pool_vence: float = 0.0


def _token_entra() -> str:
    global _token
    valor, vence = _token
    if valor and time.time() < vence - 300:
        return valor
    from azure.identity import DefaultAzureCredential
    # Nunca interactivo: esto corre en un servidor y un prompt de navegador
    # dejaria la peticion colgada hasta el timeout del cliente.
    cred = DefaultAzureCredential(
        exclude_interactive_browser_credential=True,
        exclude_developer_cli_credential=False,
    )
    t = cred.get_token(_ALCANCE)
    _token = (t.token, float(t.expires_on))
    return t.token


def cadena_conexion() -> str:
    s = load_settings()
    clave = _token_entra() if s["PG_AUTH"] == "entra" else s["PG_PASSWORD"]
    return psycopg.conninfo.make_conninfo(
        host=s["PG_HOST"], port=s["PG_PORT"], dbname=s["PG_DB"],
        user=s["PG_USER"], password=clave, sslmode=s["PG_SSLMODE"],
        connect_timeout=15, application_name="atlas-kag",
    )


def _configurar(con: psycopg.Connection) -> None:
    with con.cursor() as cur:
        cur.execute('SET search_path = ag_catalog, "$user", public')
        try:
            # Mas vecinos recorridos en el indice HNSW: con un corpus de este
            # tamano el resultado es exacto en la practica. El GUC no existe si
            # todavia no se instalo pgvector, y eso pasa al aprovisionar.
            cur.execute("SET hnsw.ef_search = 200")
        except psycopg.errors.UndefinedObject:
            con.rollback()
    con.commit()


def _vencimiento() -> float:
    if load_settings()["PG_AUTH"] != "entra":
        return _SIN_VENCIMIENTO
    return _token[1] - 300 if _token[0] else 0.0


def obtener_pool() -> ConnectionPool:
    global _pool, _pool_vence
    anterior: ConnectionPool | None = None
    with _lock:
        if _pool is None or time.time() >= _pool_vence:
            anterior = _pool
            # open=False + open(wait=False): construir el pool no debe bloquear
            # mientras se sostiene el lock, o una renovación de token congela
            # todas las peticiones en vuelo.
            nuevo = ConnectionPool(
                cadena_conexion(), min_size=1, max_size=8, timeout=30,
                configure=_configurar, open=False,
            )
            nuevo.open(wait=False)
            _pool, _pool_vence = nuevo, _vencimiento()
        actual = _pool
    if anterior is not None:
        anterior.close()
    return actual


@contextmanager
def conexion() -> Iterator[psycopg.Connection]:
    with obtener_pool().connection() as con:
        yield con


def conectar() -> psycopg.Connection:
    """Conexion directa sin pool. Se usa en scripts de carga y verificacion."""
    con = psycopg.connect(cadena_conexion(), autocommit=False)
    _configurar(con)
    return con


def consultar(sql: str, params: tuple | list | None = None) -> list[tuple]:
    with conexion() as con, con.cursor() as cur:
        cur.execute(sql, params or ())
        return cur.fetchall() if cur.description else []


def ejecutar(sql: str, params: tuple | list | None = None) -> None:
    with conexion() as con, con.cursor() as cur:
        cur.execute(sql, params or ())


def cerrar() -> None:
    global _pool
    with _lock:
        if _pool is not None:
            _pool.close()
            _pool = None


def ping() -> dict[str, Any]:
    s = load_settings()
    try:
        db, ver, age, vec = consultar(
            "SELECT current_database(), version(), "
            "(SELECT extversion FROM pg_extension WHERE extname='age'), "
            "(SELECT extversion FROM pg_extension WHERE extname='vector')"
        )[0]
        return {
            "ok": True, "base": db,
            "postgres": str(ver).split(",")[0].replace("PostgreSQL ", "PG "),
            "age": age or "no instalada",
            "pgvector": vec or "no instalada",
            "auth": s["PG_AUTH"],
        }
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "auth": s["PG_AUTH"]}
