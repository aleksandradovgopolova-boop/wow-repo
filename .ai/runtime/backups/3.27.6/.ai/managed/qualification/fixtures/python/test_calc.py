from calc import add, sub


def test_add():
    assert add(2, 3) == 5


def test_sub():
    # Заведомо падающий baseline-тест: харнесс снимает РЕАЛЬНЫЙ вывод pytest и проверяет,
    # что движок извлекает стабильный node-id (test_calc.py::test_sub).
    assert sub(5, 2) == 999
