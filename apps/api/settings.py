"""Configuracion tipada. Dos modos de conexion: Entra ID en Azure, contrasena en local."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal, TypedDict

from dotenv import load_dotenv

RAIZ = Path(__file__).resolve().parents[2]
load_dotenv(RAIZ / ".env")


class Settings(TypedDict):
    PG_HOST: str
    PG_PORT: int
    PG_DB: str
    PG_USER: str
    PG_PASSWORD: str
    PG_SSLMODE: str
    PG_AUTH: Literal["entra", "password"]
    GRAFO: str
    EMBED_DIM: int
    DEMO_MODE: Literal["live", "offline"]
    PROJECT_ENDPOINT: str
    MODEL_DEPLOYMENT: str
    RAIZ: Path
    DATA: Path


@lru_cache(maxsize=1)
def load_settings() -> Settings:
    host = os.getenv("PG_HOST", "localhost").strip()
    auth = os.getenv("PG_AUTH", "").strip().lower()
    if auth not in ("entra", "password"):
        # Un host de Azure sin contrasena solo puede autenticar con token.
        auth = "entra" if host.endswith(".postgres.database.azure.com") else "password"
    local = host in ("localhost", "127.0.0.1", "postgres")
    modo = os.getenv("DEMO_MODE", "offline").strip().lower()
    return Settings(
        PG_HOST=host,
        PG_PORT=int(os.getenv("PG_PORT", "5432")),
        PG_DB=os.getenv("PG_DB", "atlas"),
        PG_USER=os.getenv("PG_USER", "atlas"),
        PG_PASSWORD=os.getenv("PG_PASSWORD", ""),
        PG_SSLMODE=os.getenv("PG_SSLMODE", "prefer" if local else "require"),
        PG_AUTH=auth,  # type: ignore[arg-type]
        GRAFO=os.getenv("GRAFO", "atlas"),
        EMBED_DIM=int(os.getenv("EMBED_DIM", "256")),
        DEMO_MODE=modo if modo in ("live", "offline") else "offline",  # type: ignore[arg-type]
        PROJECT_ENDPOINT=os.getenv("PROJECT_ENDPOINT", ""),
        MODEL_DEPLOYMENT=os.getenv("MODEL_DEPLOYMENT", "gpt-4o-mini"),
        RAIZ=RAIZ,
        DATA=RAIZ / "data",
    )


def host_enmascarado(host: str) -> str:
    if not host:
        return ""
    partes = host.split(".")
    if partes and len(partes[0]) > 4:
        partes[0] = partes[0][:3] + "•" * (len(partes[0]) - 3)
    return ".".join(partes)
