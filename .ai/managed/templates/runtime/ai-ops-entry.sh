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

# ИНТЕРПРЕТАТОР ВЫБИРАЕТСЯ ПО СПОСОБНОСТИ РАБОТАТЬ, а не по имени (ревизия 2026-08-11).
#
# Прежде здесь стояло голое `python3`. На машине с несколькими интерпретаторами (brew обновил
# python3 до новой минорной версии, pyyaml остался в прежней; или кит ставили из venv) `python3`
# из PATH библиотеку не видит — и владелец на первой же команде получал не сообщение, а
# трейсбек `ModuleNotFoundError: No module named 'yaml'`. Хуже, что `doctor` при этом зелёный:
# он проверяет ТОТ интерпретатор, которым его запустили, а не тот, которым побежит эта обёртка.
# Ровно класс «зелёное ничего не значит», против которого стоит остальной кит.
#
# pyyaml — единственная рантайм-зависимость (requirements.txt), поэтому она и есть признак
# пригодности. AI_OPS_PYTHON — явное слово владельца, оно сильнее перебора.
find_python() {
  if [ -n "${AI_OPS_PYTHON:-}" ]; then
    if "$AI_OPS_PYTHON" -c 'import yaml' 2>/dev/null; then echo "$AI_OPS_PYTHON"; return 0; fi
    return 1
  fi
  for cand in python3 python3.13 python3.12 python3.11 python3.10 python3.9 python; do
    if command -v "$cand" >/dev/null 2>&1 && "$cand" -c 'import yaml' 2>/dev/null; then
      echo "$cand"; return 0
    fi
  done
  return 1
}

if py=$(find_python); then
  :
else
  if [ -n "${AI_OPS_PYTHON:-}" ]; then
    echo "AI Ops не может запуститься: указанный AI_OPS_PYTHON=$AI_OPS_PYTHON не видит pyyaml." >&2
  else
    echo "AI Ops не может запуститься: ни один python3 в PATH не видит библиотеку pyyaml." >&2
  fi
  echo "Это единственная зависимость кита — без неё не работает ни одна команда." >&2
  echo "Что сделать (одно из двух):" >&2
  echo "  python3 -m pip install --user pyyaml" >&2
  echo "  AI_OPS_PYTHON=/путь/к/python3 ./ai-ops ${*:-<команда>}" >&2
  exit 2
fi

# БАЙТКОД НЕ ПИШЕМ В ЧУЖОЙ РЕПОЗИТОРИЙ (ревизия 2026-08-11).
#
# Замер: после одной команды `./ai-ops status` в `.ai/managed/` появлялось 11 файлов `.pyc`
# в 6 каталогах `__pycache__` (~185 КиБ), а `.gitignore` установщик в дочку не пишет — то есть
# `git add -A` у владельца застейджил бы байткод кита как свой исходник. Собственный CI кита
# ставит эту же переменную и объясняет ровно так: «байткод в дереве ломает проверку целостности
# managed». В чужом дереве цена выше: там это не только шум, но и чужие файлы в коммите.
#
# export, а не префикс одной команды: движок сам запускает python-подпроцессы (валидаторы,
# гейты), и они пишут байткод точно так же.
export PYTHONDONTWRITEBYTECODE=1

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
    if inst=$(find_installer); then exec "$py" "$inst" "$@"; fi
    echo "Состояние кита показывает сам кит, а его исходник рядом не найден." >&2
    echo "Укажите: AI_OPS_HOME=/путь/к/ai-ops-kit ./ai-ops kit-status" >&2
    exit 2
    ;;
  init|update|diff|doctor|validate|migrate|verify-capabilities|selftest|delivery-proof)
    if inst=$(find_installer); then
      exec "$py" "$inst" "$@"
    fi
    echo "Команда '$cmd' обслуживает сам кит, а его исходник рядом не найден." >&2
    echo "Укажите, где он лежит: AI_OPS_HOME=/путь/к/ai-ops-kit ./ai-ops $cmd" >&2
    exit 2
    ;;
  ""|-h|--help|help)
    exec "$py" "$managed/tools/ai_ops_cli.py"
    ;;
  *)
    # Интенты движка: next, model, plan, run, specify, review, status, health, new, discuss, …
    # Каталог репозитория подставляем сами, если человек его не назвал — незачем помнить лишний
    # аргумент, когда мы и так стоим в его репозитории.
    exec "$py" "$managed/tools/ai_ops_cli.py" "$@" "$here"
    ;;
esac
