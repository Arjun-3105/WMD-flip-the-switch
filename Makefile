PY ?= .venv/bin/python
SWITCH = docker compose -p mandate-switch --env-file switch/.env -f switch/docker-compose.yml --profile collab --profile gateway

.PHONY: install demo verify tamper ui test switch-env switch-up switch-down switch-reset setup live play

install:            ## create the virtualenv and install Mandate
	python3 -m venv .venv && $(PY) -m pip install -q --upgrade pip && $(PY) -m pip install -q -e ".[dev]"

demo:               ## the whole deal offline: no server, no API key
	$(PY) -m mandate demo --out out

verify:             ## check the demo certificate, with both private annexes
	$(PY) -m mandate verify out/D-1-certificate.json --annex out/D-1-annex-acme.json --annex out/D-1-annex-nimbus.json

tamper:             ## change one number in the certificate and watch verify fail
	$(PY) -m mandate tamper out/D-1-certificate.json out/tampered.json && $(PY) -m mandate verify out/tampered.json --annex out/D-1-annex-acme.json || true

ui:                 ## ledger page for the offline run at http://127.0.0.1:8787
	$(PY) -m mandate ui out

test:
	$(PY) -m pytest -q

switch-env:         ## write switch/.env with fresh secrets (ports 8110/3110/8065)
	@test -f switch/.env || (cp switch/.env.example switch/.env && \
	  sed -i 's/^SWITCH_VERSION=.*/SWITCH_VERSION=0.31.0/; s/^GATEWAY_HOST_PORT=.*/GATEWAY_HOST_PORT=3110/; s/^API_HOST_PORT=.*/API_HOST_PORT=8110/; s/^POSTGRES_HOST_PORT=.*/POSTGRES_HOST_PORT=5442/' switch/.env && \
	  bash switch/generate-env-secrets.sh switch/.env mandate-local && echo "wrote switch/.env")

switch-up: switch-env   ## start Switch 0.31 + Mattermost locally (Docker)
	$(SWITCH) up -d
	@echo "Waiting for Switch setup to finish..."; \
	for i in $$(seq 1 80); do docker inspect -f '{{.State.Status}}' mandate-switch-setup-1 2>/dev/null | grep -q exited && break; sleep 3; done; \
	curl -sf localhost:8110/health && echo " Switch is up. Mattermost: http://localhost:8065"

switch-down:        ## stop Switch, keep its data
	$(SWITCH) down

switch-reset:       ## stop Switch and delete its data (then delete .mandate/ by hand)
	$(SWITCH) down -v

setup:              ## provision users, agents, rooms and access rules on the local Switch
	$(PY) -m mandate setup

live:               ## run the referee and both agents against Switch (ARGS=--claude for Claude)
	$(PY) -m mandate live $(ARGS)

play:               ## post Priya's and Sam's lines into Mattermost (while `make live` runs)
	$(PY) -m mandate play $(ARGS)
