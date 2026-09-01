"""sys.path для модулей пакета: корень + tools/ (v3.34).

Здесь НЕ алиас на плоский `tools/_bootstrap.py`, хотя раньше был. Алиас делал `import _bootstrap`,
то есть искал плоский модуль в `tools/` — и работал только там, где `tools/` УЖЕ на пути, а это
происходит лишь при входе через плоский алиас. Из-за этого `import ai_ops_kit.gates.preflight` с
одним корнем на `sys.path` падал: пакет собирался в дистрибутив, но пакетом не был.

Прежний докстринг объяснял это циклом («чтобы импортировать пакет, нужен путь, который добавляет
модуль внутри этого пакета»). Цикла нет: модуль лежит ВНУТРИ пакета, значит к моменту его импорта
корень на пути по определению — иначе не нашёлся бы сам пакет. Корень ищется маркером `VERSION`,
как в `tools/_bootstrap.py` и `validation/_bootstrap.py`.

Тёзки безопасны: все три ничего не хранят и лишь идемпотентно правят `sys.path`.

Зачем `tools/` нужен и после перевода импортов на пакетные имена: плоские алиасы остаются точками
входа для скриптов и документации child-репозитория.

v3.34: `validation/` отсюда УБРАН. Валидаторы переехали в `ai_ops_kit/validation/`, у них появилось
пакетное имя, и все 12 импортов в коде пакета его называют. Путь, который больше не нужен, — это
пояс: он молча чинил бы чужой недоперевод, и следующий узнал бы о нём только в чистом окружении.
"""
from __future__ import annotations
import sys
from pathlib import Path

PKG = next((_p for _p in Path(__file__).resolve().parents if (_p / "VERSION").is_file()),
            Path(__file__).resolve().parents[2])
# R-39: в `.ai/managed` дочки байткода быть не должно (слой checksummed — свой же `.pyc` кит
# принимал за правку владельца). Свой файл записан ДО исполнения тела, поэтому убираем его явно.
if PKG.name == "managed" and PKG.parent.name == ".ai":
    sys.dont_write_bytecode = True
    _c = globals().get("__cached__")
    if _c:
        try:
            Path(_c).unlink(missing_ok=True)
            Path(_c).parent.rmdir()
        except OSError:
            pass

for _p in (str(PKG / "tools"), str(PKG)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
