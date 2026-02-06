// static/js/core/store.js

export const store = {
    state: {
        // Данные текущей страницы
        readings: [],       // Список показаний
        users: [],          // Список пользователей
        tariffs: {},        // Текущие тарифы
        summary: {},        // Сводка для бухгалтера

        // Состояние интерфейса
        activePeriod: null, // Имя активного периода (или null)
        pagination: {
            page: 1,
            limit: 50
        }
    },

    // --- Действия (Actions) для изменения данных ---

    setReadings(readings) {
        this.state.readings = readings;
    },

    setUsers(users) {
        this.state.users = users;
    },

    setTariffs(tariffs) {
        this.state.tariffs = tariffs;
    },

    setSummary(data) {
        this.state.summary = data;
    },

    setPage(page) {
        this.state.pagination.page = page;
    },

    // Получить одну запись показания по ID (нужно для модального окна)
    getReadingById(id) {
        return this.state.readings.find(r => r.id === id);
    }
};