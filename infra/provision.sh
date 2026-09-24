#!/usr/bin/env bash
# Aprovisiona ATLAS KAG en Azure: grupo de recursos, PostgreSQL Flexible con
# Apache AGE y pgvector, y autenticacion solo con Microsoft Entra ID.
#
# No crea ni guarda contrasenas: el servidor queda con `--password-auth Disabled`
# y la aplicacion autentica con un token que renueva sola.
#
#   ./infra/provision.sh [region]
#
set -euo pipefail

RG="${RG:-rg-atlas-kag}"
LOC="${1:-${LOC:-centralus}}"
SKU="${SKU:-Standard_B2s}"
VERSION="${VERSION:-16}"
DB="${DB:-atlas}"
RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

paso() { printf '\n\033[1m· %s\033[0m\n' "$*"; }

paso "Comprobando sesión de Azure"
az account show --query '{suscripcion:name, id:id}' -o tsv >/dev/null || {
  echo "Ejecuta 'az login' primero." >&2; exit 1
}

paso "Registrando el proveedor Microsoft.DBforPostgreSQL"
estado=$(az provider show -n Microsoft.DBforPostgreSQL --query registrationState -o tsv 2>/dev/null || echo NotRegistered)
if [[ "$estado" != "Registered" ]]; then
  az provider register --namespace Microsoft.DBforPostgreSQL
  until [[ "$(az provider show -n Microsoft.DBforPostgreSQL --query registrationState -o tsv)" == "Registered" ]]; do
    sleep 10
  done
fi
echo "  proveedor registrado"

paso "Verificando que la región admita aprovisionamiento"
# Muchas suscripciones tienen regiones restringidas; el campo `reason` lo dice.
motivo=$(az postgres flexible-server list-skus -l "$LOC" -o json 2>/dev/null \
  | python3 -c 'import json,sys; print((json.load(sys.stdin)[0].get("reason") or "").strip())')
if [[ -n "$motivo" ]]; then
  echo "  $LOC está restringida: $motivo" >&2
  echo "  Regiones alternativas a probar: westus3, northcentralus, canadacentral, westeurope." >&2
  exit 1
fi
echo "  $LOC disponible"

paso "Creando el grupo de recursos $RG"
az group create -n "$RG" -l "$LOC" \
  --tags proyecto=atlas-kag modelo=KAG motor=postgres-age -o none

PGNAME="${PGNAME:-pg-atlas-kag-$(openssl rand -hex 3)}"
MYIP="$(curl -fsS https://api.ipify.org)"
UPN="$(az ad signed-in-user show --query userPrincipalName -o tsv)"
OBJID="$(az ad signed-in-user show --query id -o tsv)"

paso "Creando el servidor $PGNAME (PG $VERSION · $SKU)"
az postgres flexible-server create \
  --resource-group "$RG" --name "$PGNAME" --location "$LOC" \
  --tier Burstable --sku-name "$SKU" --version "$VERSION" \
  --storage-size 32 --storage-auto-grow Enabled \
  --public-access "$MYIP" \
  --active-directory-auth Enabled --password-auth Disabled \
  --database-name "$DB" \
  --tags proyecto=atlas-kag motor=postgres-age \
  --yes -o none

paso "Habilitando AGE y pgvector"
az postgres flexible-server parameter set -g "$RG" -s "$PGNAME" \
  --name shared_preload_libraries --value "age,pg_cron,pg_stat_statements" -o none
az postgres flexible-server parameter set -g "$RG" -s "$PGNAME" \
  --name azure.extensions --value "AGE,VECTOR,PG_TRGM" -o none

paso "Registrando al usuario actual como administrador Entra ID"
az postgres flexible-server ad-admin create -g "$RG" -s "$PGNAME" \
  -u "$UPN" -i "$OBJID" -t User -o none

paso "Reiniciando para cargar la librería AGE"
az postgres flexible-server restart -g "$RG" -n "$PGNAME" -o none

paso "Escribiendo .env"
cat > "$RAIZ/.env" <<ENV
# Generado por infra/provision.sh el $(date -u +%Y-%m-%dT%H:%M:%SZ)
# No hay contraseña: PG_AUTH=entra usa un token de Microsoft Entra ID.
PG_HOST=$PGNAME.postgres.database.azure.com
PG_PORT=5432
PG_DB=$DB
PG_USER=$UPN
PG_AUTH=entra
PG_SSLMODE=require

GRAFO=atlas
EMBED_DIM=256
DEMO_MODE=live
ENV

cat <<FIN

Listo.

  servidor   $PGNAME.postgres.database.azure.com
  base       $DB
  región     $LOC
  acceso     Entra ID ($UPN), firewall abierto solo para $MYIP

Siguiente:

  make install
  make seed
  make load
  make verify

FIN
