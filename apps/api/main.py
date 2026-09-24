"""ATLAS · KAG sobre PostgreSQL Flexible + Apache AGE."""

from __future__ import annotations

from contextlib import asynccontextmanager
from threading import Thread
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from kag import grafo as motor_grafo
from kag.conexion import cerrar, ping
from kag.ontologia import DIMENSIONES, ETIQUETAS, GRAFO, RELACIONES
from routers import dimensiones, grafo, kag
from settings import host_enmascarado, load_settings


def _precalentar() -> None:
    """La muestra del grafo cuesta segundos y no cambia: se deja lista antes de
    que el navegador la pida."""
    try:
        motor_grafo.muestra()
    except Exception:
        pass


@asynccontextmanager
async def ciclo(_: FastAPI):
    Thread(target=_precalentar, daemon=True).start()
    yield
    cerrar()


app = FastAPI(title="ATLAS · KAG multidimensional", version="1.0.0", lifespan=ciclo)

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(grafo.router)
app.include_router(kag.router)
app.include_router(dimensiones.router)


def _motor() -> dict[str, Any]:
    s = load_settings()
    estado = ping()
    return {
        "motor": "PostgreSQL Flexible · Apache AGE",
        "host": host_enmascarado(s["PG_HOST"]),
        "base": s["PG_DB"],
        "grafo": GRAFO,
        "autenticacion": "Microsoft Entra ID" if s["PG_AUTH"] == "entra" else "contraseña",
        "modelo": {
            "dimensiones": len(DIMENSIONES),
            "etiquetas": len(ETIQUETAS),
            "relaciones": len(RELACIONES),
        },
        "componentes": [
            {"etiqueta": "PostgreSQL", "valor": str(estado.get("postgres", "")).replace("PG ", "").split(" ")[0]},
            {"etiqueta": "AGE", "valor": estado.get("age", "—")},
            {"etiqueta": "pgvector", "valor": estado.get("pgvector", "—")},
        ],
        "conectado": estado.get("ok", False),
        "error": estado.get("error", ""),
    }


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"estado": "ok", "motor": _motor()}


@app.get("/motor")
async def motor() -> dict[str, Any]:
    return _motor()
