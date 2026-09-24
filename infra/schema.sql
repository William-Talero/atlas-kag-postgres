-- Esquema de ATLAS KAG sobre PostgreSQL Flexible.
--
-- Dos motores, una sola base: AGE guarda el grafo y pgvector guarda los
-- fragmentos. Que convivan es el punto: el filtro estructural y la similitud
-- coseno se resuelven en la misma consulta, sin traer candidatos al cliente.
--
-- {dim} lo sustituye el cargador con EMBED_DIM.

CREATE EXTENSION IF NOT EXISTS age CASCADE;
CREATE EXTENSION IF NOT EXISTS vector SCHEMA public;

SET search_path = ag_catalog, "$user", public;

-- create_graph() falla si el grafo ya existe; el catalogo decide.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM ag_catalog.ag_graph WHERE name = 'atlas') THEN
        PERFORM ag_catalog.create_graph('atlas');
    END IF;
END $$;

-- Dimension documental. `entidades` son las claves de negocio de los nodos que
-- el fragmento menciona: es la llave de union con el grafo.
-- Va calificada en public: ag_catalog encabeza el search_path y no es escribible.
CREATE TABLE IF NOT EXISTS public.kag_fragmento (
    clave      text PRIMARY KEY,
    documento  text NOT NULL,
    titulo     text NOT NULL,
    texto      text NOT NULL,
    dimension  text NOT NULL,
    entidades  text[] NOT NULL DEFAULT '{{}}',
    embedding  public.vector({dim}) NOT NULL
);

-- GIN sobre el arreglo: resuelve `entidades && claves_del_subgrafo` sin escanear.
CREATE INDEX IF NOT EXISTS ix_frag_entidades
    ON public.kag_fragmento USING gin (entidades);

CREATE INDEX IF NOT EXISTS ix_frag_documento
    ON public.kag_fragmento (documento);

-- HNSW con distancia coseno: los vectores ya vienen unitarios del embebedor.
CREATE INDEX IF NOT EXISTS ix_frag_embedding
    ON public.kag_fragmento USING hnsw (embedding public.vector_cosine_ops);
