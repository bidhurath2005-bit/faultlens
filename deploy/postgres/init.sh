#!/bin/sh
# Creates two isolated databases with least-privilege owners:
#   faultlens -> FaultLens platform (schema managed by Alembic)
#   shop      -> demo microservices' business data
set -eu
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres <<-EOSQL
  CREATE ROLE faultlens LOGIN PASSWORD '${FAULTLENS_DB_PASSWORD}';
  CREATE DATABASE faultlens OWNER faultlens;
  CREATE DATABASE faultlens_test OWNER faultlens;
  CREATE ROLE shop LOGIN PASSWORD '${SHOP_DB_PASSWORD}';
  CREATE DATABASE shop OWNER shop;
  REVOKE ALL ON DATABASE faultlens FROM PUBLIC;
  REVOKE ALL ON DATABASE shop FROM PUBLIC;
EOSQL
