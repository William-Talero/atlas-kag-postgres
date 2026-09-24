# ATLAS · KAG multidimensional

Grafo de conocimiento sobre **Azure Database for PostgreSQL Flexible Server** con
**Apache AGE** para el grafo y **pgvector** para el texto, en la misma base.

La diferencia entre RAG y KAG cabe en una cláusula:

```sql
-- RAG: recupera por parecido y espera que el parecido implique pertinencia
ORDER BY embedding <=> consulta LIMIT k

-- KAG: el grafo demuestra primero la conexión y acota el universo
WITH candidatos AS MATERIALIZED (
  SELECT … FROM kag_fragmento WHERE entidades && claves_del_subgrafo
)
SELECT …, embedding <=> consulta AS distancia FROM candidatos ORDER BY distancia LIMIT k
```

Los dos motores viven en la misma base, así que el filtro estructural y la
similitud coseno se resuelven en una sola consulta. No hay que traer candidatos
al cliente para cruzarlos con otro sistema.

```
apps/web     Next.js 15 · React 19 · Tailwind v4 · Cytoscape + fcose
apps/api     FastAPI · psycopg 3 · Apache AGE (openCypher) · pgvector
infra/       provision.sh y schema.sql
scripts/     generación del grafo, carga y verificación
```

## Arranque

```bash
az login
make provision   # grupo de recursos + PostgreSQL Flexible con AGE y pgvector
make install     # venv de la API + npm de la web
make seed        # 9.368 nodos · 44.218 aristas · 1.063 fragmentos
make reload      # carga el grafo y el corpus en la base
make verify      # corre la secuencia completa y falla si algo no se alcanza
make dev         # API en :8000 y web en :3000
```

`make provision` no genera ni guarda contraseñas. El servidor queda con
`--password-auth Disabled` y la aplicación se autentica con un token de
Microsoft Entra ID que renueva sola. El firewall se abre solo para tu IP.

Para trabajar sin Azure, `make local` levanta `apache/age` en docker: es el mismo
motor y el mismo Cypher, así que no hay un backend de mentira que se comporte
distinto.

## Las seis dimensiones

El grafo no es una sola red: son seis dimensiones que comparten los mismos nodos.
Una persona es a la vez un titular, un residente, el sujeto de un evento, una
instancia de un concepto y algo mencionado en un documento. Cada consulta elige
por cuáles ejes se mueve.

| Dimensión | Pregunta | Etiquetas |
|---|---|---|
| **Entidad** | ¿Quién? | Persona · Empresa · Filial |
| **Operación** | ¿Qué se contrató? | Producto · Obligacion · Garantia |
| **Geográfica** | ¿Dónde? | Ciudad · Region |
| **Temporal** | ¿Cuándo? | Periodo · Evento |
| **Semántica** | ¿Qué significa? | Concepto · Norma |
| **Documental** | ¿Dónde consta? | Documento · Fragmento |

Catorce etiquetas, veinticinco relaciones. Cada relación declara **de qué sistema
de registro proviene**, y eso es lo que permite que una respuesta cite la
procedencia salto a salto y no solo el documento final.

```
Persona --TITULAR_DE--> Producto --EMITIDO_POR--> Filial      core de cada filial
                        Producto --GENERA-------> Obligacion  sistema de cartera
Persona --CODEUDOR_DE-> Obligacion                            expediente de crédito
Empresa --MATRIZ_DE---> Empresa                               Supersociedades
Fragmento --MENCIONA--> Persona|Empresa|Concepto|Norma        extractor de entidades
```

## El caso

El **Grupo Financiero Meridiano** tiene nueve filiales. Cada una guarda su parte
del cliente en su propio core y en su propio expediente. Ninguna ve al cliente
completo.

> Carlos Andrés Mejía Vargas tiene nueve productos en seis filiales. La última
> filial le aprobó una tarjeta calculando con lo que ella tenía. Sumando el grupo
> pasa de **39,8 %** a **48,6 %** de endeudamiento, con un tope de política del
> 40 %. Nadie se equivocó. Ese número no existe en ningún sistema del grupo.

`make verify` lo comprueba: si el caso deja de cruzar el tope exactamente con el
último producto, falla.

## Cómo responde

La interfaz tiene cuatro vistas. La principal es **KAG**: un agente al que se le
escribe en lenguaje natural y que contesta con el recorrido a la vista.

1. **Anclaje** — la pregunta se resuelve a nodos concretos. Primero por bigramas
   ("mejía vargas" identifica a una persona; "vargas" a veinte), luego por término
   suelto, y si nada coincide, por las entidades que mencionan los fragmentos más
   parecidos — ese último salto es el que permite responder preguntas normativas,
   que no nombran a ninguna entidad.
2. **Expansión** — recorrido acotado desde el ancla, con los saltos y su
   procedencia.
3. **Recuperación restringida** — similitud coseno *dentro* de lo que el grafo ya
   demostró conexo.
4. **Fusión** — `0,55 · similitud + 0,30 · proximidad + 0,15 · densidad de evidencia`,
   sobre un grupo de candidatos ocho veces mayor que `k`. Si solo se reordenara el
   top‑k por parecido, la proximidad nunca podría rescatar al fragmento que está a
   cero saltos del ancla y ocupa el puesto 40 por similitud.

El agente no redacta libremente: cada frase de su respuesta sale de una cifra del
recorrido — cuántos nodos tocó, cuántos sistemas de origen citó, cuánto redujo el
corpus, cuánta evidencia menciona el ancla directamente. La interfaz muestra
siempre el Cypher y el SQL exactos que se ejecutaron, con la latencia medida.

Cuando ninguna palabra de la pregunta está en el vocabulario del corpus, lo dice.
El embebedor es TF‑IDF con vocabulario cerrado, así que esa situación existe y es
preferible declararla a devolver una similitud vacía sin explicación.

## El lienzo muestra la explicación, no el vecindario

Un vecindario de dos saltos está dominado por **concentradores**. En la pregunta
de Carlos, 66 de los 123 nodos recuperados eran personas cuya única relación con
él es vivir en Medellín: no explican nada y tapan lo que sí.

El grafo se poda con una regla sola: **no se atraviesa un concentrador**. Un nodo
cuyo grado dentro del subgrafo supera un umbral se muestra —residir en Medellín
es un hecho sobre Carlos— pero no se expande. Quedan 48 nodos y las seis
dimensiones intactas.

La poda es **presentación**: la recuperación vectorial y la evidencia se calculan
sobre el subgrafo completo, de modo que enfocar el lienzo no cambia ni una cifra
de la respuesta. El botón `ver todo` lo demuestra en caliente.

Al pasar el ratón por un nodo aparece su ficha —etiqueta, clave y propiedades con
los importes formateados— y por una arista, la relación y **el sistema de registro
del que proviene**, que es lo que hace citable cada salto.

## El contraejemplo

La última pregunta del catálogo está marcada como contraejemplo a propósito:
*¿qué dice la política sobre el límite de cuota-ingreso?*. Esa respuesta **está
redactada** en un documento, la búsqueda vectorial la encuentra sola y el
recorrido no agrega nada. Se incluye para marcar el límite: si una consulta SQL o
una búsqueda semántica responden bien, la pregunta no necesita un grafo.

## Tres trampas de AGE que condicionan el código

1. **El tercer argumento de `cypher()` debe ser un parámetro del protocolo**, no
   una constante. Con `ClientCursor` de psycopg —que interpola en el cliente— AGE
   responde `third argument of cypher function must be a parameter`. Se usa el
   cursor estándar, que además es la opción segura: el valor nunca se concatena al
   texto. El texto Cypher es siempre estático; lo único que se compone son
   etiquetas y profundidades, validadas contra la ontología.
2. **AGE cachea grafos y etiquetas por sesión.** Después de `drop_graph`, o de
   `create_vlabel`, la misma conexión no puede seguir usándolos: el backend la
   termina con `protocol synchronization was lost`. Por eso el aprovisionamiento
   va en fases y cada fase abre su propia conexión.
3. **`ORDER BY` no resuelve un alias del propio `RETURN`.** Hay que introducirlo
   con `WITH`.

Y una de pgvector, que no es exclusiva de este proyecto: un `WHERE` combinado con
`ORDER BY embedding <=> q` sobre un índice HNSW hace **post-filtrado**. El índice
recorre solo `hnsw.ef_search` vecinos y el filtro descarta después, así que
devuelve menos de `k` filas y pierde los vecinos más cercanos. Con el filtro del
grafo aplicado primero en un CTE `MATERIALIZED`, el conjunto es pequeño y la
búsqueda exacta sale más barata que la aproximada.

## Por qué PostgreSQL y no una base de grafos dedicada

Porque el grafo no es el único índice que hace falta. Una respuesta KAG necesita
recorrido **y** similitud **y** agregación relacional, y aquí las tres son la
misma transacción, con un solo motor que operar, respaldar y auditar. A eso se
suma que Entra ID sustituye a la contraseña: no hay un secreto que rotar.

El precio es real y conviene decirlo: AGE no tiene el planificador de un motor
nativo, los recorridos profundos cuestan más, la carga masiva es cuatro veces más
lenta y las tres trampas de arriba son fricción que no existe en Neo4j.

La versión equivalente sobre Neo4j está en **`../atlas-kag-neo4j`**: mismo modelo,
mismos datos, misma batería de verificación y los mismos resultados hasta el
cuarto decimal. Levanta en los puertos 8001 y 3001, así que las dos pueden estar
corriendo a la vez. Allí está la tabla comparativa con las cifras medidas.
