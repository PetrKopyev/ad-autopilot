// jsdom разбирает свою таблицу стилей по умолчанию при первом getComputedStyle: это десятки миллисекунд, а под нагрузкой
// параллельных прогонов — больше секунды. Первым его вызывает поиск по роли (виден ли элемент), и разбор съедал секунду
// ожидания findByRole у первого такого теста в файле. Разбираем её до тестов.
if (typeof document !== 'undefined') getComputedStyle(document.documentElement);

// jsdom не умеет модальные диалоги: открываем и закрываем их, как браузер.
if (typeof HTMLDialogElement !== 'undefined') {
  HTMLDialogElement.prototype.showModal = function showModal(this: HTMLDialogElement) { this.open = true; };
  HTMLDialogElement.prototype.close = function close(this: HTMLDialogElement) { this.open = false; };
}
