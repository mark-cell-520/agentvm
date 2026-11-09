# agentvm — reproducible agent VM on Apple Silicon
#
# Run `make help` for the full list. Every step is idempotent.

SHELL := /bin/bash
export PATH := $(HOME)/.local/bin:$(PATH)

.DEFAULT_GOAL := help
.PHONY: help check install-tart fetch serve create-vm provision configure autostart \
        status logs stop clean lint

# 这些变量原来一个都没定义，而 provision/status/logs/stop/configure 五个目标
# 都在用。展开出来是 `tart stop `（VM 名是空的）、`--llm-key-file  --llm-base-url`
# （空串让参数错位，--llm-base-url 把下一个参数当成自己的值，报出的
# `unknown argument: https://...` 和真实原因毫无关系）。默认值和
# scripts/lib/common.sh 保持一致，也能被环境变量或 .agentvmrc 覆盖。
VM_NAME    ?= agent
VM_CPU     ?= 2
TART_BIN   ?= $(HOME)/.local/bin/tart
APT_MIRROR ?=

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
	@scripts/30-create-vm.sh --name $(VM_NAME) --cpu $(VM_CPU)

# 两个改动：
#  1. sh -s 改 bash -s。客人机是 Debian，/bin/sh 是 dash，而脚本第 18 行是
#     `set -euo pipefail`——dash 不支持 pipefail，第一行就退，换 apt 源和装
#     编译器一样都没做，要到后面某个包编译失败才间接暴露。
#  2. 宿主机环境变量不会透传进 tart exec。原来读 ${AGENTVM_APT_MIRROR:-...}
#     永远拿到默认值，用户设了镜像也看不出来。现在显式拼一行 export 送进去。
provision: ## Inside the guest: fast apt mirror + build toolchain
	@{ if [ -n "$(APT_MIRROR)" ]; then printf 'AGENTVM_APT_MIRROR=%s\n' "$(APT_MIRROR)"; fi; \
	   cat scripts/40-provision-guest.sh; } \
		| $(TART_BIN) exec $(VM_NAME) bash -s

configure: ## Push LLM/Feishu config into the guest (secrets via files)
	@test -n "$(LLM_KEY_FILE)" || { \
	  echo "错误：请设置 LLM_KEY_FILE=/path/to/key（密钥不走命令行，只走文件）"; exit 1; }
	@test -n "$(LLM_BASE_URL)" || { \
	  echo "错误：请设置 LLM_BASE_URL=https://..."; exit 1; }
	@test -n "$(LLM_MODEL)" || { \
	  echo "错误：请设置 LLM_MODEL=..."; exit 1; }
	@scripts/50-configure.sh --vm $(VM_NAME) \
		--llm-key-file $(LLM_KEY_FILE) \
		--llm-base-url $(LLM_BASE_URL) \
		--llm-model $(LLM_MODEL) \
		--feishu-app-id $(FEISHU_APP_ID) \
		--feishu-app-secret-file $(FEISHU_APP_SECRET_FILE)

autostart: ## launchd (host) + systemd (guest) so a reboot brings it all back
	@scripts/60-autostart.sh

status: ## VM, guest service and agent status
	@$(TART_BIN) list
	@$(TART_BIN) exec $(VM_NAME) sh -c \
		'systemctl is-active hermes-gateway; free -m | head -2; df -h / | tail -1'

logs: ## Tail the guest agent gateway logs
	@$(TART_BIN) exec $(VM_NAME) sudo journalctl -u hermes-gateway -f

stop: ## Stop the VM (autostart 装过的话重启后会再起来，见 autostart 目标)
	@$(TART_BIN) stop $(VM_NAME)

# lint 原来有三处漏检/假绿：
#  - scripts/*.sh 匹配不到没有 .sh 后缀的 agentvm-fetch 和 agentvm-serve
#    （两个最大的 bash 文件从来没被语法检查过）
#  - plutil -lint config/launchd/*.plist 的通配符匹配不到任何文件（config/ 下
#    只有 agentvmrc.example 和 env.template），2>/dev/null || true 把错误整个吞掉
#  - python3 -m py_compile 会在仓库里留下 __pycache__——app/build.sh 的注释里
#    特意改用 ast.parse 要避免的正是这件事
lint: ## Syntax-check every script
	@for f in scripts/*.sh scripts/lib/*.sh scripts/agentvm-fetch scripts/agentvm-serve \
		install.sh app/build.sh; do \
		bash -n "$$f" && echo "  ok $$f" || exit 1; done
	@for f in agentvm.py backends/*.py scripts/*.py scripts/lib/*.py; do \
		python3 -I -c "import ast,sys;ast.parse(open(sys.argv[1],encoding='utf-8').read(),sys.argv[1])" "$$f" \
			&& echo "  ok $$f" || exit 1; done
	@if ls config/launchd/*.plist >/dev/null 2>&1; then \
		for p in config/launchd/*.plist; do plutil -lint "$$p" || exit 1; done; \
	 else echo "  ok plutil (config/launchd/ 下没有 plist，跳过)"; fi

clean: ## Remove local state (keeps scripts and config)
	@rm -rf $(HOME)/.agentvm/image
	@echo "removed $$HOME/.agentvm/image"
