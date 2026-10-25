PY ?= python3
PORT ?= 8080
URL ?= http://127.0.0.1:$(PORT)
KEY ?= $(MEMORY_SYSTEM_KEY)

.PHONY: help init run test check contract replay doctor smoke image docker-up clean

help:
	@echo "make init      生成 service/.env（随机 Memory System Key）"
	@echo "make run       本地启动服务（默认 hash 模式，读取 .env）"
	@echo "make test      单元 + 端到端测试（52 项）"
	@echo "make contract  契约自检 27 项（服务需已启动）"
	@echo "make replay    幂等 + 确定性检查（服务需已启动）"
	@echo "make check     起临时服务并跑 contract + replay（无需手动启动）"
	@echo "make doctor    配置体检（含 DashScope 连通性）"
	@echo "make smoke     URL=... KEY=... 对线上服务做 smoke"
	@echo "make image     构建 Docker 镜像"
	@echo "make docker-up docker compose up -d"

init:
	$(PY) tools/doctor.py --init

run:
	$(PY) service/server.py

test:
	$(PY) -m unittest discover -s tests -v

contract:
	$(PY) tools/contract_check.py $(URL) --key "$(KEY)"

replay:
	$(PY) tools/replay.py $(URL) 5 --key "$(KEY)"

check:
	bash scripts/verify.sh

doctor:
	$(PY) tools/doctor.py

smoke:
	$(PY) tools/smoke.py $(URL) --key "$(KEY)"

image:
	docker build -t mem-pro:local .

docker-up:
	docker compose up -d

clean:
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	rm -f service/mem.db service/mem.db-wal service/mem.db-shm
