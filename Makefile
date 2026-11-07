# agentvm — reproducible agent VM on Apple Silicon
#
# Run `make help` for the full list. Every step is idempotent.

SHELL := /bin/bash
export PATH := $(HOME)/.local/bin:$(PATH)

.DEFAULT_GOAL := help
.PHONY: help check install-tart fetch serve create-vm provision configure autostart \
        status logs stop clean lint

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

check: ## Verify the host can run an Apple-Silicon VM
	@scripts/00-check.sh

install-tart: ## Install Tart from its release tarball (no Homebrew)
	@scripts/10-install-tart.sh

fetch: ## Download every blob of the OCI image (resumable, verifiable)
	@scripts/agentvm-fetch --verify

serve: ## Serve the local image over HTTP (foreground; Ctrl+C to stop)
	@scripts/agentvm-serve

create-vm: ## Clone the local image into a VM and size it
	@scripts/30-create-vm.sh

provision: ## Inside the guest: fast apt mirror + build toolchain
	@$(HOME)/.local/bin/tart exec $(VM_NAME) sh -s < scripts/40-provision-guest.sh

configure: ## Push LLM/Feishu config into the guest (secrets via files)
	@scripts/50-configure.sh --vm $(VM_NAME) \
		--llm-key-file $(LLM_KEY_FILE) \
		--llm-base-url $(LLM_BASE_URL) \
		--llm-model $(LLM_MODEL) \
		--feishu-app-id $(FEISHU_APP_ID) \
		--feishu-app-secret-file $(FEISHU_APP_SECRET_FILE)

autostart: ## launchd (host) + systemd (guest) so a reboot brings it all back
	@scripts/60-autostart.sh

status: ## VM, guest service and agent status
	@$(HOME)/.local/bin/tart list
	@$(HOME)/.local/bin/tart exec $(VM_NAME) sh -c \
		'systemctl is-active hermes-gateway; free -m | head -2; df -h / | tail -1'

logs: ## Tail the guest agent gateway logs
	@$(HOME)/.local/bin/tart exec $(VM_NAME) sudo journalctl -u hermes-gateway -f

stop: ## Stop the VM
	@$(HOME)/.local/bin/tart stop $(VM_NAME)

lint: ## Syntax-check every script
	@for f in scripts/*.sh scripts/lib/*.sh; do bash -n "$$f" && echo "  ok $$f"; done
	@python3 -m py_compile scripts/lib/*.py && echo "  ok python"
	@plutil -lint config/launchd/*.plist 2>/dev/null || true

clean: ## Remove local state (keeps scripts and config)
	@rm -rf $(HOME)/.agentvm/image
	@echo "removed $$HOME/.agentvm/image"
