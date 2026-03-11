// static/js/core/store.js
/**
 * Глобальное хранилище состояния приложения (в оперативной памяти).
 * Используется для легковесных данных, которые не нужно хранить между сессиями браузера.
 */
export const store = {
    state: {
        activePeriod: null, // Имя текущего активного периода
        isLoadingGlobal: false
    },

    setActivePeriod(periodName) {
        this.state.activePeriod = periodName;
    },

    setGlobalLoading(status) {
        this.state.isLoadingGlobal = status;
        if (status) {
            document.body.style.cursor = 'wait';
        } else {
            document.body.style.cursor = 'default';
        }
    }
};