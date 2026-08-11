#!/bin/sh
# AI Ops — единая точка входа репозитория. Файл создаёт установщик (`ai-ops init` / `ai-ops update`);
# править руками не нужно, он только маршрутизирует вызов.
#
# ЗАЧЕМ ОН ЕСТЬ. Кит устанавливается в продуктовый репозиторий копированием, а не через pip, поэтому
# команды `ai-ops` в PATH нет. До 3.35.1 все подсказки кита печатали `ai-ops model .`, `ai-ops next`,
# `ai-ops update` — и владелец, скопировав первую же строку, получал `command not found`. Обещание
# «в каждом сообщении сказано, что делать дальше» ломалось на первой команде. Теперь подсказки
# печатают `./ai-ops …`, и это работает.
set -e
here=$(cd "$(dirname "$0")" && pwd)
managed="$here/.ai/managed"

if [ ! -d "$managed" ]; then
  echo "AI Ops в этом репозитории не установлен (нет .ai/managed)." >&2
  exit 2
fi

# Команды managed-слоя живут в установщике, который в поставку НЕ едет (он обновляет сам кит, и
# ставить его в дочку значило бы дать ей себя же обновлять). Ищем его там, где он может быть:
# переменная окружения -> типовое место клона. Если не нашли — говорим прямо, а не молчим.
find_installer() {
  for cand in "$AI_OPS_HOME/installer/ai_ops.py" "$HOME/ai-ops-kit/installer/ai_ops.py"; do
    [ -f "$cand" ] && { echo "$cand"; return 0; }
  done
  return 1
}

cmd=${1:-}
case "$cmd" in
  # `status` СОЗНАТЕЛЬНО не здесь: у владельца «status» — это «что идёт прямо сейчас», продуктовый
  # вопрос к движку. Состояние самого кита (версия, целостность managed) спрашивают реже и называют
  # отдельно — `kit-status`. Прежде обе команды звались одинаково, и владелец вместо ответа про
  # работу получал отчёт о дрейфе managed-слоя.
  kit-status)
    shift; set -- status "$@"
    if inst=$(find_installer); then exec python3 "$inst" "$@"; fi
    echo "Состояние кита показывает сам кит, а его исходник рядом не найден." >&2
    echo "Укажите: AI_OPS_HOME=/путь/к/ai-ops-kit ./ai-ops kit-status" >&2
    exit 2
    ;;
  init|update|diff|doctor|validate|migrate|verify-capabilities|selftest)
    if inst=$(find_installer); then
      exec python3 "$inst" "$@"
    fi
    echo "Команда '$cmd' обслуживает сам кит, а его исходник рядом не найден." >&2
    echo "Укажите, где он лежит: AI_OPS_HOME=/путь/к/ai-ops-kit ./ai-ops $cmd" >&2
    exit 2
    ;;
  ""|-h|--help|help)
    exec python3 "$managed/tools/ai_ops_cli.py"
    ;;
  *)
    # Интенты движка: next, model, plan, run, specify, review, status, health, new, discuss, …
    # Каталог репозитория подставляем сами, если человек его не назвал — незачем помнить лишний
    # аргумент, когда мы и так стоим в его репозитории.
    exec python3 "$managed/tools/ai_ops_cli.py" "$@" "$here"
    ;;
esac
