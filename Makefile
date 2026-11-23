.PHONY: help up down up-beta down-beta restart logs pull clean setup permissions lint validate test check

# ==========================================
# 🛠️ COMMANDS
# ==========================================

up: permissions ## Start the stack (pulls latest images & builds dashboard)
	docker compose up -d --pull always --build --remove-orphans

down: ## Stop the stack and remove containers
	docker compose down

up-beta: permissions ## Start the stack including beta services (e.g. Hermes Agent)
	docker compose --profile beta up -d --pull always --build --remove-orphans

down-beta: ## Stop the stack including beta services
	docker compose --profile beta down

restart: down up ## Restart the stack (runs down then up)

logs: ## View logs for all services (Ctrl+C to exit)
	docker compose logs -f

pull: ## Just pull images (useful for pre-loading updates)
	docker compose pull

clean: ## Clean up dangling images to free space
	docker image prune -f

permissions: ## Fix ownership of app volumes (CouchDB, Mattermost, Seerr)
	bash scripts/fix-permissions.sh

setup: ## Run initial setup scripts (directories & system service)
	bash scripts/install_dependencies.sh
	bash scripts/setup.sh
	bash scripts/system-service/create_system_service.sh

lint: ## Run yamllint and shellcheck (and hadolint if installed)
	yamllint -c .yamllint.yml docker-compose.yml .yamllint.yml .github
	find scripts -type f -name '*.sh' -exec shellcheck -S warning {} +
	@if command -v hadolint >/dev/null 2>&1; then \
		hadolint apps/home-server-dashboard/Dockerfile; \
	else \
		echo "hadolint not installed; skipping Dockerfile lint"; \
	fi

validate: ## Validate docker-compose.yml with temporary placeholders when needed
	@storage_mount="$$(grep -E '^DEFAULT_STORAGE_MOUNT=' .env | cut -d= -f2-)"; \
	lan_ip="$$(grep -E '^DEFAULT_LAN_IP=' .env | cut -d= -f2-)"; \
	if [ -z "$$storage_mount" ] || [ "$${storage_mount#CHANGE_TO_}" != "$$storage_mount" ]; then \
		storage_mount="/tmp/homelab-storage"; \
	fi; \
	if [ -z "$$lan_ip" ] || [ "$${lan_ip#CHANGE_TO_}" != "$$lan_ip" ]; then \
		lan_ip="127.0.0.1"; \
	fi; \
	DEFAULT_STORAGE_MOUNT="$$storage_mount" DEFAULT_LAN_IP="$$lan_ip" docker compose --profile '*' config --quiet

test: ## Run pytest validation suite
	python3 -m pytest -q tests

check: lint validate test ## Run all local quality checks (lint, validate, test)

# ==========================================
# ℹ️ HELP
# ==========================================

help: ## Show this help message
	@echo "Usage: make [command]"
	@echo ""
	@echo "Available commands:"
	@grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'
