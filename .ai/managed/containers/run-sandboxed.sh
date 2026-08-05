#!/usr/bin/env bash
# run-sandboxed.sh — запустить движок AI Ops в ИЗОЛИРОВАННОМ контейнере (P0.2 runtime jail).
#
# worktree-only изоляция (v2.93): в контейнер монтируется НЕ основной child-репозиторий, а его
# ОДНОРАЗОВЫЙ клон (disposable checkout). Модель физически не может тронуть основной checkout — он
# не смонтирован. После прогона доверенный host-слой ЗАБИРАЕТ ветку ai-ops/* из клона обратно в
# основной репозиторий (git fetch) — доставка вне контейнера. Клон удаляется (кроме случая ошибки
# доставки — тогда путь печатается, чтобы работа не пропала).
#
# Что enforce'ит контейнер (чего брокер в процессе НЕ может):
#   --read-only            root-fs только для чтения; писать некуда, кроме bind'а и tmpfs
#   bind <disposable-clone> -> /work   ЕДИНСТВЕННАЯ writable точка; основной child НЕ смонтирован
#   --tmpfs /tmp,/home     writable временные каталоги (npm/pip кэш) без записи на root-fs
#   --memory/--cpus/--pids-limit   лимиты ресурсов (модель/сборка не съедят машину)
#   --cap-drop=ALL + --security-opt=no-new-privileges   без привилегий/эскалации
#   --user 10001 (non-root, зашит в образ)
#
# ЧЕСТНО: сеть НЕ отключена — движку нужен egress к API модели и реестрам (npm/pip). Для жёсткого
# контроля подставьте свой egress-allowlist прокси (см. docs/container-isolation.md) или задайте
# AI_OPS_NETWORK=none, если модель/зависимости уже локальны.
#
# Использование:
#   containers/run-sandboxed.sh <child_repo> "<task>" [доп. флаги ai-ops run]
# Пример:
#   OPENAI_COMPATIBLE_BASE_URL=... OPENAI_COMPATIBLE_API_KEY=... \
#     containers/run-sandboxed.sh ~/ii-sreda "почини тест даты" \
#       --engine pipeline --provider openai-compatible --model deepseek-chat \
#       --execute --sandbox --baseline-diff
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

CHILD="${1:?укажи путь к child-репозиторию}"; shift
TASK="${1:?укажи текст задачи}"; shift
IMAGE="${AI_OPS_IMAGE:-ai-ops-engine:latest}"
MEM="${AI_OPS_MEM:-2g}"
CPUS="${AI_OPS_CPUS:-2}"
NET="${AI_OPS_NETWORK:-bridge}"   # bridge (нужен egress к модели/реестрам) | none (всё локально)

CHILD_ABS="$(cd "$CHILD" && pwd)"

if [ ! -d "$CHILD_ABS/.git" ]; then
  echo "ОШИБКА: $CHILD_ABS — не git-репозиторий; worktree-only изоляция требует git." >&2
  exit 2
fi

# 1. Одноразовый клон child (без хардлинков -> полностью независимые объекты; основной репо не
#    делится инодами с контейнером). Монтируем в /work ТОЛЬКО его. AI_OPS_WORKDIR — переопределить
#    базу под клоны (по умолчанию системный tmp).
WORKBASE="${AI_OPS_WORKDIR:-${TMPDIR:-/tmp}}"
DISPOSABLE="$(mktemp -d "${WORKBASE%/}/ai-ops-run.XXXXXX")"
CLONE="$DISPOSABLE/repo"
echo "worktree-only: клонирую child в одноразовый checkout $CLONE (основной репо не монтируется)…" >&2
git clone --quiet --no-hardlinks --local "$CHILD_ABS" "$CLONE"

# v2.113 Container delivery scope: снимок ai-ops/* веток клона ДО прогона. После прогона доставляем
# ТОЛЬКО ветки, которые ЭТОТ прогон создал или изменил (новые ИЛИ с другим SHA) — не все ai-ops/*.
# Так параллельные ai-ops/* ветки в основном репо не будут перезаписаны устаревшей версией из клона.
SNAP_BEFORE="$DISPOSABLE/ai-ops-refs.before"
git -C "$CLONE" for-each-ref --format='%(objectname) %(refname:short)' 'refs/heads/ai-ops/*' \
  > "$SNAP_BEFORE" 2>/dev/null || : > "$SNAP_BEFORE"

cleanup() { rm -rf "$DISPOSABLE" 2>/dev/null || true; }

# Пробрасываем ТОЛЬКО имена секрет-переменных (значения берутся из окружения, не в образ).
ENVFLAGS=()
for v in OPENAI_COMPATIBLE_BASE_URL OPENAI_COMPATIBLE_API_KEY ANTHROPIC_API_KEY \
         OPENAI_API_KEY GITHUB_TOKEN GH_TOKEN; do
  [ -n "${!v:-}" ] && ENVFLAGS+=(-e "$v")
done

# 2. Запуск движка в jail'е над ОДНОРАЗОВЫМ клоном (не над основным репо).
set +e
docker run --rm \
  --read-only \
  --network "$NET" \
  --mount "type=bind,src=${CLONE},dst=/work" \
  --tmpfs /tmp:rw,size=1g \
  --tmpfs /home/runner:rw,size=1g \
  --workdir /work \
  --memory "$MEM" --cpus "$CPUS" --pids-limit 512 \
  --cap-drop ALL --security-opt no-new-privileges \
  "${ENVFLAGS[@]}" \
  "$IMAGE" \
  run "$TASK" /work "$@"
RC=$?
set -e

# 3. Доставка доверенным host-слоем: ТОЛЬКО ветки, которые создал/изменил ЭТОТ прогон (диф снимка
#    ai-ops/* до/после), а не все ai-ops/*. Логика — в отдельном deliver-run-branches.sh (тестируется
#    валидатором без docker). Модель этого сделать НЕ могла — была заперта в клоне.
if "$SCRIPT_DIR/deliver-run-branches.sh" "$CLONE" "$CHILD_ABS" "$SNAP_BEFORE" >&2; then
  :
else
  echo "ВНИМАНИЕ: доставка веток прогона не удалась; работа сохранена в $CLONE (не удаляю)." >&2
  trap - EXIT; exit "$RC"
fi

cleanup
exit "$RC"
