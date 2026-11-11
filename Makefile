# agentvm — reproducible agent VM on Apple Silicon
#
# Run `make help` for the full list. Every step is idempotent.

SHELL := /bin/bash
export PATH := $(HOME)/.local/bin:$(PATH)

.DEFAULT_GOAL := help
.PHONY: help check install-tart fetch serve create-vm provision configure autostart \
        status logs stop clean purge-image lint test

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
#  3. APT_MIRROR 必须转义后再拼。原来用 %s 原样进客人机脚本：
#     make provision APT_MIRROR='http://x; rm -rf /; #' 会在客人机里执行任意
#     命令。改用 %q（Makefile 的 SHELL 是 bash，支持），产出可被 shell 安全再解析
#     一次的串——和 50-configure.sh 处理 LLM_BASE_URL 的做法一致。
provision: ## Inside the guest: fast apt mirror + build toolchain
	@{ if [ -n "$(APT_MIRROR)" ]; then printf 'AGENTVM_APT_MIRROR=%q\n' "$(APT_MIRROR)"; fi; \
	   cat scripts/40-provision-guest.sh; } \
		| $(TART_BIN) exec $(VM_NAME) bash -s

configure: ## Push LLM/Feishu config into the guest (secrets via files)
	@test -n "$(LLM_KEY_FILE)" || { \
	  echo "错误：请设置 LLM_KEY_FILE=/path/to/key（密钥不走命令行，只走文件）"; exit 1; }
	@test -n "$(LLM_BASE_URL)" || { \
	  echo "错误：请设置 LLM_BASE_URL=https://..."; exit 1; }
	@test -n "$(LLM_MODEL)" || { \
	  echo "错误：请设置 LLM_MODEL=..."; exit 1; }
	@test -n "$(FEISHU_APP_ID)" || { \
	  echo "错误：请设置 FEISHU_APP_ID=cli_..."; exit 1; }
	@test -n "$(FEISHU_APP_SECRET_FILE)" || { \
	  echo "错误：请设置 FEISHU_APP_SECRET_FILE=/path/to/secret"; exit 1; }
	@scripts/50-configure.sh --vm "$(VM_NAME)" \
		--llm-key-file "$(LLM_KEY_FILE)" \
		--llm-base-url "$(LLM_BASE_URL)" \
		--llm-model "$(LLM_MODEL)" \
		--feishu-app-id "$(FEISHU_APP_ID)" \
		--feishu-app-secret-file "$(FEISHU_APP_SECRET_FILE)"

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
#  - app/Sources/*.swift 里除了 MacVMEngine.swift 的那几个从来没被检查过。
#    ContentView / AppState / NewVMWizard 全是改安装流程的主战场，而
#    「invalid redeclaration」这类错只有整份一起 parse 才报得出来
#    （2026-11-10 实测：同一个 confirmDelete 写了两遍，make lint 全绿，
#    直到 app/build.sh 才炸）。-parse-as-library 是必须的：那批文件里有
#    AgentVMApp.swift 的 @main，不加它会报「顶层不允许有代码」。
#  - bash -n 查不出「全角标点紧跟变量名」。那不是语法错，是 bash 的变量名
#    解析把高位字节并进了名字：`"$X，y"` 里的名字是 X<U+FF0C>，set -u 下
#    直接 unbound variable。见 docs/PITFALLS.md M20。
# 五套离线测试，全不起虚拟机、不连网、不碰 ~/.agentvm：
#   test-autoinstall      无人值守安装驱动器（含画面判据、熔断、帧剪枝）
#   test-vmkit-audit      vmkit 快照/上传下载那批
#   test-vmkit-commands   vmkit / winarm 命令层
#   test-agentvm-fetch    镜像下载的并发、token 文件、HTTP 码捕获
#                          （本地 HTTPS 改 http 的 curl shim + 假 registry）
#   test-tart-backend     tart 后端的退避等待、日志留存、clone 进度
test: ## Unit-test every layer (offline, no VM, no network)
	@python3 -I scripts/test-autoinstall.py
	@python3 -I scripts/test-vmkit-audit.py
	@python3 -I scripts/test-vmkit-commands.py
	@python3 -I scripts/test-agentvm-fetch.py
	@python3 -I scripts/test-tart-backend.py

lint: ## Syntax-check every script
	@bash scripts/lint-wide-punct.sh
	@for f in scripts/*.sh scripts/lib/*.sh scripts/agentvm-fetch scripts/agentvm-serve \
		install.sh app/build.sh; do \
		bash -n "$$f" && echo "  ok $$f" || exit 1; done
	@for f in agentvm.py backends/*.py scripts/*.py scripts/lib/*.py; do \
		python3 -I -c "import ast,sys;ast.parse(open(sys.argv[1],encoding='utf-8').read(),sys.argv[1])" "$$f" \
			&& echo "  ok $$f" || exit 1; done
	@if command -v swiftc >/dev/null 2>&1; then \
		swiftc -parse -sdk "$$(xcrun --sdk macosx --show-sdk-path)" \
			-target arm64-apple-macos14.0 \
			app/Sources/MacVMEngine.swift scripts/vmz/main.swift \
			&& echo "  ok swift (MacVMEngine + vmz)"; \
	 else echo "  skip swift (没有 swiftc)"; fi
	@if command -v swiftc >/dev/null 2>&1; then \
		swiftc -parse -parse-as-library -sdk "$$(xcrun --sdk macosx --show-sdk-path)" \
			-target arm64-apple-macos14.0 \
			app/Sources/*.swift \
			&& echo "  ok swift (app/Sources 全部)"; \
	 else echo "  skip swift (没有 swiftc)"; fi
	# vzrun.swift 必须单独 parse：它和 vmz/main.swift 一样是带顶层可执行代码的
	# CLI 入口，两个入口放进同一次 swiftc -parse 会互相报
	# "statements are not allowed at the top level"——那不是代码错，是编译单元
	# 里出现了两个 main。分开各查一次才查得出各自的真错。
	@if command -v swiftc >/dev/null 2>&1; then \
		swiftc -parse -sdk "$$(xcrun --sdk macosx --show-sdk-path)" \
			-target arm64-apple-macos14.0 \
			scripts/vzrun.swift \
			&& echo "  ok swift (vzrun)"; \
	 else echo "  skip swift (没有 swiftc)"; fi
	@if ls config/launchd/*.plist >/dev/null 2>&1; then \
		for p in config/launchd/*.plist; do plutil -lint "$$p" || exit 1; done; \
	 else echo "  skip plutil (config/launchd/ 下没有 plist，未检查)"; fi

# clean 原来直接 rm -rf ~/.agentvm/image。删掉的正是 agentvm-fetch 那份按 digest
# 命名、可断点续传的 blob 池：镜像现在在外接盘上，写入实测只有 14.5 MB/s，一次
# make clean 意味着几小时的重下，而脚本一句确认都不问，目标名还叫 clean。
# 现在拆开：
#   clean       只清构建产物和缓存（安全，随时能再生成）
#   purge-image 删镜像池（贵，交互确认，或 AGENTVM_PURGE_IMAGE=1 显式放行）
clean: ## Remove build artifacts and caches (safe; keeps the image pool)
	@rm -rf __pycache__ scripts/__pycache__ scripts/lib/__pycache__ backends/__pycache__
	@find "$(HOME)/.agentvm/vms" -maxdepth 2 -name '*.log' -delete 2>/dev/null || true
	@echo "cleaned build artifacts（镜像池没动）"

purge-image: ## Delete the resumable image pool (costly: hours to re-download)
	@test -n "$$HOME" || { echo "HOME 未设置，拒绝 rm -rf 一个根路径"; exit 1; }; \
	 if [ ! -d "$$HOME/.agentvm/image" ]; then echo "没有镜像池可删（$$HOME/.agentvm/image 不存在）"; exit 0; fi; \
	 if [ "$${AGENTVM_PURGE_IMAGE:-0}" != "1" ]; then \
	   if [ -t 0 ]; then \
	     printf '这会删掉 %s（%s）。镜像在外接盘上，重下要按小时计。确定？[y/N] ' \
	       "$$HOME/.agentvm/image" "$$(du -sh "$$HOME/.agentvm/image" 2>/dev/null | cut -f1)"; \
	     read -r ans; \
	     case "$$ans" in y|Y|yes) ;; *) echo "已取消"; exit 1 ;; esac; \
	   else \
	     echo "非交互环境，拒绝删镜像池。要删就设 AGENTVM_PURGE_IMAGE=1。"; exit 1; \
	   fi; \
	 fi; \
	 rm -rf "$$HOME/.agentvm/image"; \
	 echo "removed $$HOME/.agentvm/image"
