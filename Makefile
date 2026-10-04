# OpenShift mirror planner. `make help` lists the targets.

IMAGE       ?= localhost/openshift-mirror-planner:latest
OCP_CHANNEL ?= stable-4.22
# Folders mounted into the container
MIRROR_DIR  ?= $(CURDIR)/mirror
CACHE_DIR   ?= $(CURDIR)/oc-mirror-cache
PULL_SECRET ?= $(firstword $(wildcard $(CURDIR)/pull-secret.json $(CURDIR)/dl/*pull-secret*.json))
PORT        ?= 8088
NAME        ?= mirror-planner
PODMAN      ?= podman

VENV := .venv
MP   := $(VENV)/bin/mirror-planner

# keep-id plus --user: the container runs as you, so files in the mounted folders are yours
RUN_ARGS = --userns=keep-id --user $(shell id -u):$(shell id -g) \
	-v $(MIRROR_DIR):/data/mirror:z \
	-v $(CACHE_DIR):/data/cache:z \
	-v $(PULL_SECRET):/run/secrets/pull-secret.json:ro,z

.PHONY: help venv test serve clients image run start stop logs mirror dry-run imageset vendor-patternfly check-secret

help:
	@echo "Container (self-contained: oc and oc-mirror are inside the image)"
	@echo "  make image                    build $(IMAGE) (OCP_CHANNEL=$(OCP_CHANNEL) picks the oc version)"
	@echo "  make run | start | stop | logs  UI on http://127.0.0.1:$(PORT)/ (foreground | background)"
	@echo "  make mirror                   oc-mirror --v2 to disk for the plan, in the container"
	@echo "  make dry-run                  same, resolving images without downloading them"
	@echo "  make imageset                 regenerate imageset-config.yaml from plan.yaml"
	@echo "Folders: MIRROR_DIR=$(MIRROR_DIR) CACHE_DIR=$(CACHE_DIR) PULL_SECRET=$(PULL_SECRET)"
	@echo "Development: make venv | test | serve | clients | vendor-patternfly"

# ---------------------------------------------------------------- development
venv: $(MP)
$(MP): pyproject.toml
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install -q -e '.[dev]'
	@touch $(MP)

test: venv
	$(VENV)/bin/pytest -q

# Run the UI from the checkout; needs oc on PATH (or `make clients` and OC=dl/...) for catalog scans.
serve: venv
	MIRROR_DIR=$(MIRROR_DIR) PULL_SECRET_FILE=$(PULL_SECRET) $(MP) serve --port $(PORT)

clients:
	DL=$(CURDIR)/dl OCP_CHANNEL=$(OCP_CHANNEL) scripts/fetch-clients.sh

vendor-patternfly:
	scripts/vendor-patternfly.sh

# ---------------------------------------------------------------- container
image:
	$(PODMAN) build --build-arg OCP_CHANNEL=$(OCP_CHANNEL) -t $(IMAGE) .

check-secret:
	@test -n "$(PULL_SECRET)" -a -f "$(PULL_SECRET)" || { echo "set PULL_SECRET=/path/to/pull-secret.json"; exit 1; }
	@mkdir -p $(MIRROR_DIR) $(CACHE_DIR)

run: check-secret
	$(PODMAN) run --rm -it --name $(NAME) -p 127.0.0.1:$(PORT):8088 $(RUN_ARGS) $(IMAGE)

start: check-secret
	$(PODMAN) run -d --replace --name $(NAME) -p 127.0.0.1:$(PORT):8088 $(RUN_ARGS) $(IMAGE)
	@echo "OpenShift mirror planner on http://127.0.0.1:$(PORT)/"

stop:
	-$(PODMAN) stop $(NAME)

logs:
	$(PODMAN) logs -f $(NAME)

mirror: check-secret
	$(PODMAN) run --rm $(RUN_ARGS) $(IMAGE) mirror

dry-run: check-secret
	$(PODMAN) run --rm $(RUN_ARGS) $(IMAGE) mirror --dry-run

imageset: check-secret
	$(PODMAN) run --rm $(RUN_ARGS) $(IMAGE) imageset
