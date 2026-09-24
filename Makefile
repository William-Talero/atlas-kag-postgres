SHELL := /bin/bash
API := apps/api
WEB := apps/web
VENV := $(API)/.venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

.PHONY: install venv provision seed load reload dev api web verify local local-down clean

## Instala dependencias de API y Web
install: venv
	$(PIP) install -q -r $(API)/requirements.txt
	cd $(WEB) && npm install

venv:
	@test -d $(VENV) || python3 -m venv $(VENV)
	@$(PIP) install -q --upgrade pip

## Crea el grupo de recursos y el PostgreSQL Flexible con AGE y pgvector
provision:
	./infra/provision.sh

## Genera el grafo multidimensional, el corpus y el embebedor
seed: venv
	$(PY) scripts/seed.py

## Carga el grafo y el corpus en la base
load: venv
	$(PY) scripts/load_age.py

## Borra el grafo y lo reconstruye desde cero
reload: venv
	$(PY) scripts/load_age.py --recrear

## Levanta API (8000) y Web (3000) en paralelo
dev:
	@$(MAKE) -j2 api web

api:
	cd $(API) && ../../$(VENV)/bin/uvicorn main:app --reload --port 8000

web:
	cd $(WEB) && npm run dev

## Corre la verificación de extremo a extremo y falla si algo no se alcanza
verify: venv
	$(PY) scripts/verify_kag.py

## PostgreSQL local con AGE y pgvector en docker, sin nada de Azure
local:
	docker compose up -d postgres
	@echo "Esperando a que el motor acepte conexiones…"
	@until docker compose exec -T postgres pg_isready -U atlas -d atlas >/dev/null 2>&1; do sleep 1; done
	@echo "Listo. Usa la sección local del .env.example."

local-down:
	docker compose down -v

clean:
	rm -rf $(VENV) $(WEB)/node_modules $(WEB)/.next data/*.json data/*.npz data/*.vocab.json
