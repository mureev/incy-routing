# incy-routing — the everyday commands.
#
# Delivery is not in here: a green push to master is tested and published by CI and deployed by the server
# itself (DEPLOY.md).
#
#   make check      lint + unit, the same as CI's first job (no network)
#   make routing    build from the committed snapshot and ask real Xray (downloads pinned Xray + geo files)
#   make canary     the same against today's upstream and the latest geo files
#   make build      one build into ./data from today's upstream, as the service does it
#   make run        the image on :8080 with ./data, against today's upstream (Docker)
#   make status URL=https://<host>/<path>/status    what a deployed instance reports
#   make lock       rewrite requirements-dev.lock (needs uv); UPGRADE=--upgrade to move versions

IMAGE ?= incy-routing:local
PORT  ?= 8080

.PHONY: check lint test routing canary build image run status lock clean

check: lint test

lint:
	ruff check .

test:
	pytest -q -p no:randomly --strict-markers -rs

routing:
	tools/routing_check.sh

canary:
	LIVE=1 tools/routing_check.sh

build:
	python3 -m incy_routing.build --data-dir ./data

image:
	docker build -t $(IMAGE) --build-arg APP_BUILD=$$(git rev-parse HEAD 2>/dev/null || echo dev) .

run: image
	mkdir -p data
	docker run --rm -p 127.0.0.1:$(PORT):8080 -v "$$PWD/data:/data" --read-only --tmpfs /tmp --cap-drop ALL $(IMAGE)

status:
	@test -n "$(URL)" || { echo "usage: make status URL=https://<host>/<path>/status"; exit 2; }
	curl -fsS "$(URL)"

lock:
	uv pip compile requirements-dev.txt --generate-hashes --python-version 3.12 --universal -o requirements-dev.lock -q $(UPGRADE)

clean:
	rm -rf .pytest_cache .ruff_cache .cache
	find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
