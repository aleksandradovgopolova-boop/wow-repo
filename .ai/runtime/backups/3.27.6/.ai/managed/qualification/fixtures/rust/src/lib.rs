//! Стек-фикстура квалификации (rust). НЕ продукт.
pub fn add(a: i32, b: i32) -> i32 { a + b }
pub fn sub(a: i32, b: i32) -> i32 { a - b }

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn test_add() { assert_eq!(add(2, 3), 5); }
    #[test]
    fn test_sub() {
        // Заведомо падающий baseline-тест: харнесс снимает РЕАЛЬНЫЙ вывод `cargo test` и
        // проверяет, что движок извлекает имя упавшего теста (thread 'tests::test_sub' panicked).
        assert_eq!(sub(5, 2), 999);
    }
}
