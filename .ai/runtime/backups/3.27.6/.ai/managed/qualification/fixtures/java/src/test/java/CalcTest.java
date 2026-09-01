import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.assertEquals;

public class CalcTest {
    @Test void testAdd() { assertEquals(5, Calc.add(2, 3)); }
    // Заведомо падающий baseline-тест: харнесс проверяет, что движок извлекает Class.method
    // упавшего теста из surefire-вывода (CalcTest.testSub), а не пустое множество.
    @Test void testSub() { assertEquals(999, Calc.sub(5, 2)); }
}
