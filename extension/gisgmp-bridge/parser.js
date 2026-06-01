// extension/gisgmp-bridge/parser.js
//
// Извлечение начислений из HTML страницы «Начисления» реестра ГИС ГМП
// (gisgmp.cgu.mchs.ru/charge/). В MV3 service worker нет DOM API
// (DOMParser отсутствует), поэтому парсим регулярками. Структура таблицы
// стабильная (серверный рендер, как у Drupal): каждая строка начисления —
// <tr> с 15 ячейками <td> в фиксированном порядке.
//
// Колонки (0-based), как в таблице реестра:
//   0  УИН                 (в <div class="no-print">)
//   1  Размер платежа      (сумма)
//   2  Дата начисления
//   3  Дата актуализации
//   4  КБК
//   5  ИНН получателя
//   6  ОГРН получателя
//   7  КПП получателя
//   8  УИП / лицевой счёт   (в <div class="no-print">)
//   9  Плательщик (ФИО)
//   10 Назначение
//   11 Статус квитирования  (в <div class="no-print">)
//   12 Статус изменения
//   13 Источник
//   14 Действия (ссылка /charge/{uuid})

const ENTITY_MAP = {
    "&amp;":  "&",
    "&lt;":   "<",
    "&gt;":   ">",
    "&quot;": "\"",
    "&#039;": "'",
    "&apos;": "'",
    "&nbsp;": " ",
};

export function decodeEntities(s) {
    if (!s) return "";
    return s.replace(/&(?:amp|lt|gt|quot|#039|apos|nbsp);/g, m => ENTITY_MAP[m] || m)
            .replace(/&#(\d+);/g, (_, n) => String.fromCharCode(parseInt(n, 10)))
            .replace(/&#x([0-9a-fA-F]+);/g, (_, n) => String.fromCharCode(parseInt(n, 16)));
}

function stripTags(s) {
    return s ? s.replace(/<[^>]+>/g, "").replace(/\s+/g, " ").trim() : "";
}

// Видимое значение ячейки: приоритетно содержимое <div class="no-print">…</div>
// (в обычном, не print-режиме показывается именно оно), иначе — весь текст ячейки.
function cellText(cellHtml) {
    if (!cellHtml) return "";
    const np = cellHtml.match(/<div class="no-print">([\s\S]*?)<\/div>/i);
    const raw = np ? np[1] : cellHtml;
    return decodeEntities(stripTags(raw));
}

// Сумма приходит в разном виде: «1 862. 50», «7 326. 09», «889,82».
// Чистим до канонического «1862.50».
function normalizeAmount(s) {
    if (!s) return "0";
    return s.replace(/[\s ]/g, "").replace(",", ".");
}

/**
 * Признак «залогинен в реестр»: на странице под сессией всегда есть ссылка
 * выхода href="/logout". Если её нет — пользователь не авторизован (ЭЦП).
 */
export function isLoggedIn(html) {
    return !!html && /href="\/logout"/i.test(html);
}

/**
 * Парсит все строки начислений из HTML страницы /charge/.
 * Возвращает массив объектов в формате, который ждёт бэкенд ЖКХ
 * (POST /api/financier/gisgmp/sync).
 */
export function extractCharges(html) {
    if (!html) return [];

    // Берём содержимое <tbody>…</tbody> таблицы начислений, чтобы не цеплять
    // строки из шапки/фильтров. Если tbody не найден — парсим весь html
    // (в худшем случае строки-нечисления отсеются проверкой ниже).
    const tbodyMatch = html.match(/<tbody>([\s\S]*?)<\/tbody>/i);
    const body = tbodyMatch ? tbodyMatch[1] : html;

    const charges = [];
    const rowRe = /<tr[^>]*>([\s\S]*?)<\/tr>/gi;
    let rm;
    while ((rm = rowRe.exec(body)) !== null) {
        const rowHtml = rm[1];

        // Все ячейки строки по порядку.
        const cells = [];
        const cellRe = /<td[^>]*>([\s\S]*?)<\/td>/gi;
        let cm;
        while ((cm = cellRe.exec(rowHtml)) !== null) cells.push(cm[1]);
        if (cells.length < 15) continue;   // не строка начисления

        const uin = cellText(cells[0]);
        const payer = cellText(cells[9]);
        // Защита: УИН — длинная цифровая строка, плательщик — непустой.
        if (!/^\d{15,25}$/.test(uin) || !payer) continue;

        // UUID карточки из ссылки «Просмотр» (/charge/{uuid}).
        const uuidMatch = rowHtml.match(/href="\/charge\/([0-9a-fA-F-]{36})"/);

        charges.push({
            uin,
            amount:         normalizeAmount(cellText(cells[1])),
            bill_date:      cellText(cells[2]),
            actualize_date: cellText(cells[3]),
            account:        cellText(cells[8]),   // лицевой счёт (УИП)
            payer_name:     payer,
            purpose:        cellText(cells[10]),
            ack_status:     cellText(cells[11]),  // квитирование
            change_status:  cellText(cells[12]),  // эталонное/аннулирование/…
            source:         cellText(cells[13]),
            charge_uuid:    uuidMatch ? uuidMatch[1] : null,
        });
    }
    return charges;
}
