LANGFUSE_DIR := langfuse
DIFY_DIR     := dify
GROWI_DIR    := growi
GRAPHRAG_DIR := graphrag

.PHONY: up-langfuse down-langfuse logs-langfuse \
        up-dify down-dify logs-dify \
        up-growi down-growi logs-growi \
        up-graphrag down-graphrag logs-graphrag \
        up-all down-all status

# --- Langfuse ---
up-langfuse:
	docker compose -f $(LANGFUSE_DIR)/docker-compose.yml --env-file $(LANGFUSE_DIR)/.env up -d
	@echo "Langfuse: http://localhost:3100"

down-langfuse:
	docker compose -f $(LANGFUSE_DIR)/docker-compose.yml --env-file $(LANGFUSE_DIR)/.env down

logs-langfuse:
	docker compose -f $(LANGFUSE_DIR)/docker-compose.yml --env-file $(LANGFUSE_DIR)/.env logs -f

# --- Dify ---
up-dify:
	docker compose -f $(DIFY_DIR)/docker-compose.yaml --env-file $(DIFY_DIR)/.env up -d
	@echo "Dify: http://localhost:80"

down-dify:
	docker compose -f $(DIFY_DIR)/docker-compose.yaml --env-file $(DIFY_DIR)/.env down

logs-dify:
	docker compose -f $(DIFY_DIR)/docker-compose.yaml --env-file $(DIFY_DIR)/.env logs -f

# --- GROWI ---
up-growi:
	docker compose -f $(GROWI_DIR)/docker-compose.yml --env-file $(GROWI_DIR)/.env up -d
	@echo "GROWI: http://localhost:3300"

down-growi:
	docker compose -f $(GROWI_DIR)/docker-compose.yml --env-file $(GROWI_DIR)/.env down

logs-growi:
	docker compose -f $(GROWI_DIR)/docker-compose.yml --env-file $(GROWI_DIR)/.env logs -f

# --- GraphRAG ---
up-graphrag:
	docker compose -f $(GRAPHRAG_DIR)/docker-compose.yml --env-file $(GRAPHRAG_DIR)/.env up -d
	@echo "GraphRAG: http://localhost:8080"

down-graphrag:
	docker compose -f $(GRAPHRAG_DIR)/docker-compose.yml --env-file $(GRAPHRAG_DIR)/.env down

logs-graphrag:
	docker compose -f $(GRAPHRAG_DIR)/docker-compose.yml --env-file $(GRAPHRAG_DIR)/.env logs -f

# --- 一括操作 ---
up-all: up-growi up-langfuse up-dify up-graphrag

down-all:
	$(MAKE) down-graphrag || true
	$(MAKE) down-dify || true
	$(MAKE) down-langfuse || true
	$(MAKE) down-growi || true

status:
	@echo "=== GraphRAG ===" && docker compose -f $(GRAPHRAG_DIR)/docker-compose.yml --env-file $(GRAPHRAG_DIR)/.env ps 2>/dev/null || echo "(停止中)"
	@echo "=== Langfuse ===" && docker compose -f $(LANGFUSE_DIR)/docker-compose.yml ps 2>/dev/null || echo "(停止中)"
	@echo "=== Dify ===" && docker compose -f $(DIFY_DIR)/docker-compose.yaml ps 2>/dev/null || echo "(停止中)"
	@echo "=== GROWI ===" && docker compose -f $(GROWI_DIR)/docker-compose.yml ps 2>/dev/null || echo "(停止中)"
