# OpenShift mirror planner. `make help` lists the targets.

IMAGE       ?= localhost/openshift-mirror-planner:latest
OCP_CHANNEL ?= stable-4.22
# Folder mounted into the container
MIRROR_DIR  ?= $(CURDIR)/mirror
PULL_SECRET ?= $(firstword $(wildcard $(CURDIR)/pull-secret.json $(CURDIR)/dl/*pull-secret*.json))
PORT        ?= 8088
NAME        ?= mirror-planner
PODMAN      ?= podman

VENV := .venv
MP   := $(VENV)/bin/mirror-planner

# keep-id plus --user: the container runs as you, so files in the mounted folders are yours
RUN_ARGS = --userns=keep-id --user $(shell id -u):$(shell id -g) \
	-v $(MIRROR_DIR):/data/mirror:z \
	-v $(PULL_SECRET):/run/secrets/pull-secret.json:ro,z

.PHONY: help venv test serve image scan run start stop logs imageset vendor-patternfly check-secret

help:
	@echo "Container"
	@echo "  make image                    build $(IMAGE) from freshly pulled, fully updated bases"
	@echo "  make scan                     list the image's vulnerabilities (Trivy, run as a container)"
	@echo "  make run | start | stop | logs  UI on http://127.0.0.1:$(PORT)/ (foreground | background)"
	@echo "  make imageset                 regenerate imageset-config.yaml from plan.yaml"
	@echo "Folder: MIRROR_DIR=$(MIRROR_DIR)  Pull secret: PULL_SECRET=$(PULL_SECRET)"
	@echo "Development: make venv | test | serve | vendor-patternfly"

# ---------------------------------------------------------------- development
venv: $(MP)
$(MP): pyproject.toml
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install -q -e '.[dev]'
	@touch $(MP)

test: venv
	$(VENV)/bin/pytest -q

# Run the UI from the checkout.
serve: venv
	MIRROR_DIR=$(MIRROR_DIR) PULL_SECRET_FILE=$(PULL_SECRET) $(MP) serve --port $(PORT)

vendor-patternfly:
	scripts/vendor-patternfly.sh

# ---------------------------------------------------------------- container
image:
	$(PODMAN) build --pull=always --build-arg OCP_CHANNEL=$(OCP_CHANNEL) -t $(IMAGE) .

# Vulnerability report for the built image; TRIVY_ARGS="--severity HIGH,CRITICAL" narrows it.
TRIVY_IMAGE ?= docker.io/aquasec/trivy:latest
scan:
	@mkdir -p $(CURDIR)/.scan
	$(PODMAN) save --format docker-archive -o $(CURDIR)/.scan/image.tar $(IMAGE)
	$(PODMAN) run --rm -v $(CURDIR)/.scan:/scan:z $(TRIVY_IMAGE) image --quiet --cache-dir /scan/cache \
	    --input /scan/image.tar --scanners vuln $(TRIVY_ARGS)
	@rm -f $(CURDIR)/.scan/image.tar

check-secret:
	@test -n "$(PULL_SECRET)" -a -f "$(PULL_SECRET)" || { echo "set PULL_SECRET=/path/to/pull-secret.json"; exit 1; }
	@mkdir -p $(MIRROR_DIR)

run: check-secret
	$(PODMAN) run --rm -it --name $(NAME) -p 127.0.0.1:$(PORT):8088 $(RUN_ARGS) $(IMAGE)

start: check-secret
	$(PODMAN) run -d --replace --name $(NAME) -p 127.0.0.1:$(PORT):8088 $(RUN_ARGS) $(IMAGE)
	@echo "OpenShift mirror planner on http://127.0.0.1:$(PORT)/"

stop:
	-$(PODMAN) stop $(NAME)

logs:
	$(PODMAN) logs -f $(NAME)

imageset: check-secret
	$(PODMAN) run --rm $(RUN_ARGS) $(IMAGE) imageset
