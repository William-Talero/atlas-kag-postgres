"""Carga el grafo y el corpus a PostgreSQL: AGE para los nodos, pgvector para el texto.

Dos trampas de AGE que condicionan la estructura de este script:

1. AGE cachea grafos y etiquetas **por sesion**. Despues de `drop_graph`, o de
   `create_vlabel`, la misma conexion no puede seguir usandolos: el backend
   termina la conexion con "protocol synchronization was lost". Por eso el
   aprovisionamiento va en fases y cada fase abre su propia conexion.
2. `MATCH (a {clave: ...})` sin etiqueta recorre la tabla padre de todos los
   vertices. Por eso las aristas se agrupan por (relacion, etiqueta_origen,
   etiqueta_destino) y cada lote emite un MATCH etiquetado, que si usa indice.

    python scripts/load_age.py            carga incremental
    python scripts/load_age.py --recrear  borra el grafo y lo reconstruye
"""

from __future__ import annotations

import json
import sys
import time
from collections import defaultdict
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "apps" / "api"))

from kag.conexion import cerrar, conectar  # noqa: E402
from kag.embeddings import a_literal  # noqa: E402
from kag.ontologia import ETIQUETAS, GRAFO, RELACIONES  # noqa: E402
from settings import load_settings  # noqa: E402

LOTE = 400
TAG = "$atlas$"


def _log(msg: str) -> None:
    print(msg, flush=True)


# ------------------------------------------------------------------- esquema

def _fases() -> tuple[list[str], list[str]]:
    dim = int(load_settings()["EMBED_DIM"])
    sql = (RAIZ / "infra" / "schema.sql").read_text("utf-8").replace("{dim}", str(dim))
    # El marcador del arreglo vacio viene escapado para convivir con format().
    sql = sql.replace("'{{}}'", "'{}'")
    sentencias = _sentencias(sql)
    previas = [s for s in sentencias
               if s.upper().startswith(("CREATE EXTENSION", "SET "))]
    return previas, [s for s in sentencias if s not in previas]


def aplicar_esquema(recrear: bool) -> None:
    previas, resto = _fases()

    con = conectar()
    try:
        with con.cursor() as cur:
            for s in previas:
                cur.execute(s)
            if recrear:
                cur.execute("SELECT 1 FROM ag_catalog.ag_graph WHERE name = %s",
                            (GRAFO,))
                if cur.fetchone():
                    _log("· borrando grafo anterior")
                    cur.execute("SELECT ag_catalog.drop_graph(%s, true)", (GRAFO,))
                cur.execute("DROP TABLE IF EXISTS public.kag_fragmento")
            con.commit()
    finally:
        con.close()

    con = conectar()
    try:
        with con.cursor() as cur:
            for s in resto:
                cur.execute(s)
            crear_etiquetas(cur)
            con.commit()
    finally:
        con.close()
    _log(f"· esquema listo (vector({load_settings()['EMBED_DIM']}))")


def _sentencias(sql: str) -> list[str]:
    """Separa por ';' respetando los bloques $$ ... $$ de los DO."""
    partes, buffer, dentro = [], [], False
    for linea in sql.splitlines():
        if linea.strip().startswith("--"):
            continue
        if "$$" in linea:
            dentro = not dentro if linea.count("$$") % 2 else dentro
        buffer.append(linea)
        if not dentro and linea.rstrip().endswith(";"):
            texto = "\n".join(buffer).strip()
            if texto:
                partes.append(texto)
            buffer = []
    resto = "\n".join(buffer).strip()
    if resto:
        partes.append(resto)
    return partes


def crear_etiquetas(cur) -> None:
    existe = ("SELECT 1 FROM ag_catalog.ag_label l "
              "JOIN ag_catalog.ag_graph g ON g.graphid = l.graph "
              "WHERE g.name = %s AND l.name = %s")
    for etiqueta in ETIQUETAS:
        cur.execute(existe, (GRAFO, etiqueta))
        if not cur.fetchone():
            cur.execute("SELECT ag_catalog.create_vlabel(%s, %s)", (GRAFO, etiqueta))
    for relacion in RELACIONES:
        cur.execute(existe, (GRAFO, relacion))
        if not cur.fetchone():
            cur.execute("SELECT ag_catalog.create_elabel(%s, %s)", (GRAFO, relacion))
    _log(f"· {len(ETIQUETAS)} etiquetas y {len(RELACIONES)} relaciones declaradas")


def indexar(cur) -> None:
    """Indice por clave de negocio en cada tabla de etiqueta."""
    for etiqueta in ETIQUETAS:
        nombre = f"ix_{etiqueta.lower()}_clave"
        cur.execute(
            f'CREATE INDEX IF NOT EXISTS {nombre} ON {GRAFO}."{etiqueta}" '
            "USING btree (ag_catalog.agtype_access_operator("
            "VARIADIC ARRAY[properties, '\"clave\"'::agtype]))")
    _log(f"· {len(ETIQUETAS)} índices por clave")


# --------------------------------------------------------------------- nodos

def _literal_cypher(valor) -> str:
    """Los valores viajan por parametros; esto solo arma el mapa de claves."""
    return valor


def cargar_nodos(cur, nodos: list[dict]) -> int:
    por_etiqueta: dict[str, list[dict]] = defaultdict(list)
    for n in nodos:
        por_etiqueta[n["label"]].append(n)

    total = 0
    for etiqueta, lista in por_etiqueta.items():
        if etiqueta not in ETIQUETAS:
            raise SystemExit(f"Etiqueta fuera de la ontología: {etiqueta}")
        claves = sorted({k for n in lista for k in n["props"]} | {"clave"})
        malas = [k for k in claves if not k.isidentifier()]
        if malas:
            raise SystemExit(f"Propiedades con nombre inválido en {etiqueta}: {malas}")
        mapa = ", ".join(f"{k}: f.{k}" for k in claves)
        texto = f"UNWIND $filas AS f CREATE (n:{etiqueta} {{{mapa}}})"
        sql = (f"SELECT * FROM cypher('{GRAFO}', {TAG}{texto}{TAG}, %s) AS (v agtype)")

        for i in range(0, len(lista), LOTE):
            filas = [{"clave": n["clave"], **{k: n["props"].get(k) for k in claves
                                              if k != "clave"}}
                     for n in lista[i:i + LOTE]]
            cur.execute(sql, (json.dumps({"filas": filas}, ensure_ascii=False),))
        total += len(lista)
        _log(f"  · {etiqueta:<12} {len(lista):>6}")
    return total


# ------------------------------------------------------------------- aristas

def cargar_aristas(cur, aristas: list[dict], etiqueta_de: dict[str, str]) -> int:
    grupos: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for a in aristas:
        eo, ed = etiqueta_de.get(a["desde"]), etiqueta_de.get(a["hasta"])
        if not eo or not ed:
            raise SystemExit(f"Arista con extremo inexistente: {a}")
        meta = RELACIONES[a["relacion"]]
        if eo not in meta["desde"] or ed not in meta["hasta"]:
            raise SystemExit(
                f"{a['relacion']} no admite {eo} -> {ed} según la ontología")
        grupos[(a["relacion"], eo, ed)].append(a)

    total = 0
    for (relacion, eo, ed), lista in sorted(grupos.items()):
        texto = (
            "UNWIND $filas AS f "
            f"MATCH (a:{eo} {{clave: f.desde}}), (b:{ed} {{clave: f.hasta}}) "
            f"CREATE (a)-[r:{relacion} {{fuente: f.fuente, dimension: f.dimension}}]->(b)"
        )
        sql = f"SELECT * FROM cypher('{GRAFO}', {TAG}{texto}{TAG}, %s) AS (v agtype)"
        for i in range(0, len(lista), LOTE):
            filas = [{"desde": a["desde"], "hasta": a["hasta"],
                      "fuente": a["props"]["fuente"],
                      "dimension": a["props"]["dimension"]}
                     for a in lista[i:i + LOTE]]
            cur.execute(sql, (json.dumps({"filas": filas}, ensure_ascii=False),))
        total += len(lista)
        _log(f"  · {relacion:<16} {eo:>10} → {ed:<12} {len(lista):>6}")
    return total


# ---------------------------------------------------------------- fragmentos

def cargar_fragmentos(cur, documentos: list[dict], vectores: dict[str, list]) -> int:
    filas = []
    for d in documentos:
        for f in d["fragmentos"]:
            filas.append((f["clave"], d["clave"], f["titulo"], f["texto"],
                          f["dimension"], f["entidades"],
                          a_literal(vectores[f["clave"]])))
    cur.execute("TRUNCATE public.kag_fragmento")
    cur.executemany(
        "INSERT INTO public.kag_fragmento "
        "(clave, documento, titulo, texto, dimension, entidades, embedding) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s::public.vector)", filas)
    _log(f"  · kag_fragmento  {len(filas):>6}")
    return len(filas)


# ----------------------------------------------------------------------- main

def main() -> int:
    recrear = "--recrear" in sys.argv
    data = load_settings()["DATA"]
    grafo = json.loads((data / "grafo.json").read_text("utf-8"))
    documentos = json.loads((data / "documentos.json").read_text("utf-8"))
    vectores = json.loads((data / "vectores.json").read_text("utf-8"))

    t0 = time.perf_counter()
    aplicar_esquema(recrear)

    # Sesion nueva: AGE cachea las etiquetas por sesion y `cypher()` no ve las
    # que se acaban de crear en la conexion anterior.
    con = conectar()
    try:
        with con.cursor() as cur:
            _log("· nodos")
            etiqueta_de = {n["clave"]: n["label"] for n in grafo["nodos"]}
            n = cargar_nodos(cur, grafo["nodos"])
            con.commit()

            indexar(cur)
            con.commit()

            _log("· aristas")
            a = cargar_aristas(cur, grafo["aristas"], etiqueta_de)
            con.commit()

            _log("· dimensión documental")
            f = cargar_fragmentos(cur, documentos, vectores)
            con.commit()

            cur.execute(f'ANALYZE {GRAFO}."Persona"')
            cur.execute("ANALYZE public.kag_fragmento")
            con.commit()
    finally:
        con.close()
        cerrar()

    _log(f"\nCargado: {n} nodos · {a} aristas · {f} fragmentos "
         f"en {time.perf_counter() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
