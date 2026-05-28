// static/js/core/format-address.js
//
// Единая фабрика форматирования адреса помещения. После рефакторинга
// Жилфонда (housing_001 / E1) у Room есть два варианта схемы:
//   - place_type='dormitory' → dormitory_name + room_number
//   - place_type='house'     → street + house_number + apartment_number
// До E2-A фронт собирал адрес руками в десятке мест — теперь все они
// зовут эти helpers, чтобы добавление нового типа в будущем не размазывалось
// по UI.
//
// Контракт: на вход — объект с любым набором перечисленных полей (room
// сам по себе, или сериализованный API-ответ типа `{dormitory_name,
// room_number, place_type, …}`). На выход — строка.

const HAS = (v) => v !== null && v !== undefined && v !== '';

/**
 * Полный канонический адрес — для квитанций, отчётов, заголовков.
 *  dormitory → "<dorm>, ком. <N>"
 *  house     → "ул. X, д. Y, кв. Z"
 *  Пустой room → "—".
 */
export function formatRoomAddress(room) {
    if (!room) return '—';
    const type = room.place_type || 'dormitory';
    if (type === 'house') {
        const parts = [];
        if (HAS(room.street))           parts.push(`ул. ${room.street}`);
        if (HAS(room.house_number))     parts.push(`д. ${room.house_number}`);
        if (HAS(room.apartment_number)) parts.push(`кв. ${room.apartment_number}`);
        return parts.length ? parts.join(', ') : '—';
    }
    // dormitory (default)
    const dorm = HAS(room.dormitory_name) ? room.dormitory_name : '—';
    const num  = HAS(room.room_number)    ? room.room_number    : '—';
    return `${dorm}, ком. ${num}`;
}

/**
 * Короткий адрес — для компактных списков, где контекст здания уже выведен
 * отдельно (или не важен).
 *  dormitory → "ком. <N>"
 *  house     → "кв. <N>"
 */
export function formatRoomShort(room) {
    if (!room) return '—';
    const type = room.place_type || 'dormitory';
    if (type === 'house') {
        return HAS(room.apartment_number) ? `кв. ${room.apartment_number}` : '—';
    }
    return HAS(room.room_number) ? `ком. ${room.room_number}` : '—';
}

/**
 * Иконка типа помещения — для бейджей и заголовков карточек.
 */
export function placeTypeIcon(room) {
    const type = (room && room.place_type) || 'dormitory';
    return type === 'house' ? '🏠' : '🏢';
}
