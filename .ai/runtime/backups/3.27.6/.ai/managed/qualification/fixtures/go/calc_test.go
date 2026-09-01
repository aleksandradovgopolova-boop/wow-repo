package calcfixture

import "testing"

func TestAdd(t *testing.T) {
	if Add(2, 3) != 5 {
		t.Errorf("Add(2,3) = %d; want 5", Add(2, 3))
	}
}

func TestSub(t *testing.T) {
	// Заведомо падающий baseline-тест: харнесс снимает РЕАЛЬНЫЙ вывод `go test` и проверяет,
	// что движок извлекает имя упавшего теста (--- FAIL: TestSub), а не мусорный {'FAIL'}.
	if Sub(5, 2) != 999 {
		t.Errorf("Sub(5,2) = %d; want 999", Sub(5, 2))
	}
}
