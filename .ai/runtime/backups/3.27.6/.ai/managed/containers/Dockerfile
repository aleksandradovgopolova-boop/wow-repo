# Эталонный образ: ИЗОЛИРОВАННЫЙ рантайм движка AI Ops (P0.2 — то, что брокер в процессе не может).
# Брокер (tool_broker) даёт enforceable-подмножество ВНУТРИ процесса (allowlist/block_push/скраб
# секретов). Настоящую изоляцию ФС/ресурсов/привилегий даёт КОНТЕЙНЕР: read-only root, writable
# только смонтированный worktree, лимиты CPU/RAM/pids, cap-drop, non-root. Запуск — containers/
# run-sandboxed.sh (там docker-флаги jail'а).
#
# Сеть НЕ air-gap: движку нужен egress к API модели и пакетным реестрам (npm/pip). Для жёсткого
# контроля ставьте egress-allowlist прокси перед контейнером — см. docs/container-isolation.md.
#
# BASE_IMAGE конфигурируем: если Docker Hub недоступен (VPN/фаервол/троттлинг), укажи зеркало,
# напр. docker build --build-arg BASE_IMAGE=public.ecr.aws/docker/library/node:22-slim ...
ARG BASE_IMAGE=node:22-slim
FROM ${BASE_IMAGE}

# Python (stdlib + pyyaml), git, OpenSpec CLI (для гейта specification).
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-pip git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && pip3 install --no-cache-dir --break-system-packages pyyaml \
    && npm i -g @fission-ai/openspec@^1.6.0 \
    && npm cache clean --force

# Кит (движок + валидаторы + данные). Child-репозиторий МОНТИРУЕТСЯ в /work на запуске, не в образ.
COPY . /opt/ai-ops-kit
ENV AI_OPS_KIT=/opt/ai-ops-kit \
    PYTHONDONTWRITEBYTECODE=1 \
    OPENSPEC_TELEMETRY=0

# Non-root: контейнер работает под непривилегированным пользователем; /work — единственная
# writable точка (bind-mount worktree), root-fs монтируется read-only флагом запуска.
RUN useradd -m -u 10001 runner
USER runner
WORKDIR /work

# По умолчанию — единый контроллер движка. Аргументы (run <task> /work --engine pipeline --sandbox …)
# передаёт run-sandboxed.sh.
ENTRYPOINT ["python3", "/opt/ai-ops-kit/tools/ai_ops_run.py"]
