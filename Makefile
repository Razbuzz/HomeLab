.PHONY: help up down up-beta down-beta restart logs pull clean setup permissions backup upgrade rollback lint validate test system-test qa check changelog release-notes

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

permissions: ## Fix ownership of app volumes (CouchDB, Maintainerr, Seerr)
	bash scripts/fix-permissions.sh

setup: ## Run initial setup scripts (directories & system service)
	bash scripts/install_dependencies.sh
	bash scripts/setup.sh
	bash scripts/system-service/create_system_service.sh

backup: ## Create verified backup of .env and app data (BACKUP=<dir>)
	@bash scripts/backup.sh $(if $(BACKUP),--backup-dir "$(BACKUP)")

upgrade: ## Back up, merge .env, upgrade to TARGET=<ref> (default: latest tag)
	@bash scripts/upgrade.sh $(if $(TARGET),--target "$(TARGET)") $(if $(BACKUP),--backup-dir "$(BACKUP)")

rollback: ## Restore HomeLab from backup BACKUP=<dir> (default: latest)
	@bash scripts/rollback.sh $(if $(BACKUP),--backup "$(BACKUP)")

lint: ## Run yamllint and shellcheck (and hadolint if installed)
	yamllint -c .yamllint.yml docker-compose.yml tests/docker-compose.ci.yml .yamllint.yml .github
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

system-test: ## Run full-stack system test (make up-beta + QA checks + make down-beta)
	bash scripts/system-test.sh

qa: ## Run full-stack System QA test (all default + beta services, web UI, auth, links)
	bash scripts/system-test.sh

check: lint validate test ## Run all local quality checks (lint, validate, test)

changelog: ## Generate CHANGELOG.md using git-cliff
	@if command -v git-cliff >/dev/null 2>&1; then \
		git-cliff --config cliff.toml -o CHANGELOG.md; \
	else \
		docker run --rm -v "$$PWD:/app" -w /app orhunp/git-cliff:2.14.2 --config cliff.toml -o CHANGELOG.md; \
	fi

release-notes: ## Preview unreleased release notes via git-cliff
	@if command -v git-cliff >/dev/null 2>&1; then \
		git-cliff --config cliff.toml --bump --unreleased; \
	else \
		docker run --rm -v "$$PWD:/app" -w /app orhunp/git-cliff:2.14.2 --config cliff.toml --bump --unreleased; \
	fi

# ==========================================
# ℹ️ HELP
# ==========================================

help: ## Show this help message
	@echo "Usage: make [command]"
	@echo ""
	@echo "Available commands:"
	@grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'
