.PHONY: help up down restart logs pull clean setup

# ==========================================
# 🛠️ COMMANDS
# ==========================================

up: ## Start the stack (pulls latest images & builds dashboard)
	docker compose up -d --pull always --build --remove-orphans

down: ## Stop the stack and remove containers
	docker compose down

restart: down up ## Restart the stack (runs down then up)

logs: ## View logs for all services (Ctrl+C to exit)
	docker compose logs -f

pull: ## Just pull images (useful for pre-loading updates)
	docker compose pull

clean: ## Clean up dangling images to free space
	docker image prune -f

setup: ## Run initial setup scripts (directories & system service)
	sh scripts/install_dependencies.sh
	sh scripts/setup.sh
	sh scripts/system-service/create_system_service.sh

# ==========================================
# ℹ️ HELP
# ==========================================

help: ## Show this help message
	@echo "Usage: make [command]"
	@echo ""
	@echo "Available commands:"
	@grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'