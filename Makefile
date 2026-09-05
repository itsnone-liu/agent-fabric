PY ?= .venv/bin/python

central:
	$(PY) -m fabric.central.app

node:
	$(PY) -m fabric.node.daemon

test:
	$(PY) -m pytest -q

db-up:
	docker compose -f deploy/docker-compose.yml up -d

db-down:
	docker compose -f deploy/docker-compose.yml down
