// static/js/modules/tariffs.js
import { api } from '../core/api.js';
import { setLoading, toast } from '../core/dom.js';

export const TariffsModule = {
    isInitialized: false,
    tariffsList: [], // Массив для хранения всех загруженных профилей

    // Карта соответствия: ключ_в_БД -> id_в_HTML для числовых значений
    MAPPING: {
        maintenance_repair: 't_main',
        social_rent: 't_rent',
        waste_disposal: 't_waste',
        heating: 't_heat',
        water_heating: 't_w_heat',
        water_supply: 't_w_sup',
        sewage: 't_sew',
        electricity_rate: 't_el_rate',
        electricity_per_sqm: 't_el_sqm'
    },

    init() {
        this.cacheDOM();

        // События вешаем только один раз
        if (!this.isInitialized) {
            this.bindEvents();
            this.isInitialized = true;
        }

        // Данные загружаем каждый раз при открытии вкладки
        this.load();
        this.loadSchedule(); // Загружаем график
    },

    cacheDOM() {
        this.dom = {
            form: document.getElementById('tariffsForm'),
            selector: document.getElementById('tariffSelector'),
            btnCreate: document.getElementById('btnCreateNewTariff'),
            btnDelete: document.getElementById('btnDeleteTariff'),
            inputId: document.getElementById('t_id'),
            inputName: document.getElementById('t_name')
        };
    },

    bindEvents() {
        if (this.dom.form) {
            this.dom.form.addEventListener('submit', (e) => this.handleSubmit(e));
        }
        if (this.dom.selector) {
            this.dom.selector.addEventListener('change', (e) => this.handleSelectChange(e));
        }
        if (this.dom.btnCreate) {
            this.dom.btnCreate.addEventListener('click', (e) => {
                e.preventDefault();
                this.clearFormForNew();
            });
        }
        if (this.dom.btnDelete) {
            this.dom.btnDelete.addEventListener('click', (e) => {
                e.preventDefault();
                this.handleDelete();
            });
        }

        // Событие для кнопки сохранения графика
        const btnSaveSchedule = document.getElementById('btnSaveSchedule');
        if (btnSaveSchedule) {
            btnSaveSchedule.addEventListener('click', () => this.saveSchedule());
        }
    },

    async loadSchedule() {
        try {
            const data = await api.get('/settings/submission-period');
            document.getElementById('setStartDay').value = data.start_day;
            document.getElementById('setEndDay').value = data.end_day;
        } catch (e) {
            console.warn('Failed to load submission schedule settings', e);
            toast('Не удалось загрузить график', 'error');
        }
    },

    async saveSchedule() {
        const btn = document.getElementById('btnSaveSchedule');
        const start = parseInt(document.getElementById('setStartDay').value);
        const end = parseInt(document.getElementById('setEndDay').value);

        if (isNaN(start) || isNaN(end)) {
            toast('Введите корректные числа', 'error');
            return;
        }

        setLoading(btn, true, 'Сохранение...');
        try {
            await api.post('/settings/submission-period', { start_day: start, end_day: end });
            toast('График успешно обновлен', 'success');
        } catch (e) {
            toast(e.message, 'error');
        } finally {
            setLoading(btn, false, 'Сохранить график');
        }
    },

    async load(selectedId = null) {
        try {
            // Теперь бекенд отдает массив тарифов
            this.tariffsList = await api.get('/tariffs');
            this.populateSelector();

            if (this.tariffsList.length > 0) {
                // Если не передан конкретный ID для выбора, берем первый (базовый)
                const targetId = selectedId || this.tariffsList[0].id;
                this.dom.selector.value = targetId;
                this.fillForm(targetId);
            } else {
                this.clearFormForNew();
            }
        } catch (error) {
            toast('Не удалось загрузить тарифы: ' + error.message, 'error');
        }
    },

    populateSelector() {
        if (!this.dom.selector) return;
        this.dom.selector.innerHTML = '';

        this.tariffsList.forEach(t => {
            const opt = document.createElement('option');
            opt.value = t.id;
            opt.textContent = t.name;
            this.dom.selector.appendChild(opt);
        });
    },

    handleSelectChange(e) {
        const id = parseInt(e.target.value);
        if (!isNaN(id)) {
            this.fillForm(id);
        }
    },

    fillForm(id) {
        const tariff = this.tariffsList.find(t => t.id === id);
        if (!tariff) return;

        // Заполняем системные поля (ID и Название)
        if (this.dom.inputId) this.dom.inputId.value = tariff.id;
        if (this.dom.inputName) this.dom.inputName.value = tariff.name;

        // Проходим по нашей карте и заполняем инпуты с ценами
        for (const [dbKey, htmlId] of Object.entries(this.MAPPING)) {
            const input = document.getElementById(htmlId);
            if (input && tariff[dbKey] !== undefined) {
                input.value = Number(tariff[dbKey]).toFixed(2);
            }
        }

        // Базовый тариф (id=1) удалять нельзя, прячем кнопку
        if (this.dom.btnDelete) {
            this.dom.btnDelete.style.display = (tariff.id === 1) ? 'none' : 'block';
        }
    },

    clearFormForNew() {
        if (this.dom.selector) this.dom.selector.value = ""; // Сбрасываем выбор
        if (this.dom.inputId) this.dom.inputId.value = "";
        if (this.dom.inputName) this.dom.inputName.value = "Новый профиль";

        for (const htmlId of Object.values(this.MAPPING)) {
            const input = document.getElementById(htmlId);
            if (input) input.value = "0.00";
        }

        // Кнопку удаления при создании нового профиля прячем
        if (this.dom.btnDelete) this.dom.btnDelete.style.display = 'none';
    },

    async handleSubmit(e) {
        e.preventDefault();
        const btnSubmit = this.dom.form.querySelector('button[type="submit"]');

        // Собираем название
        const data = {
            name: this.dom.inputName.value.trim()
        };

        if (!data.name) {
            toast('Введите название тарифа', 'error');
            return;
        }

        // Если есть ID (редактирование) — добавляем его
        const idVal = this.dom.inputId.value;
        if (idVal) {
            data.id = parseInt(idVal);
        }

        // Собираем данные цен из формы обратно в объект по карте
        for (const [dbKey, htmlId] of Object.entries(this.MAPPING)) {
            const input = document.getElementById(htmlId);
            if (input) {
                data[dbKey] = parseFloat(input.value) || 0;
            }
        }

        setLoading(btnSubmit, true, 'Сохранение...');

        try {
            const savedTariff = await api.post('/tariffs', data);
            toast('Тарифный профиль успешно сохранен!', 'success');

            // ВАЖНО: Очищаем кэш тарифов, чтобы во вкладке "Жильцы" обновились данные
            sessionStorage.removeItem('tariffs_cache');

            // Перезагружаем список и выделяем только что сохраненный тариф
            this.load(savedTariff.id);
        } catch (error) {
            toast('Ошибка сохранения: ' + error.message, 'error');
        } finally {
            setLoading(btnSubmit, false, 'Сохранить изменения профиля');
        }
    },

    async handleDelete() {
        const idVal = this.dom.inputId.value;
        if (!idVal) return;

        const id = parseInt(idVal);
        if (id === 1) {
            toast('Базовый тариф удалить нельзя', 'error');
            return;
        }

        if (!confirm('Вы действительно хотите удалить этот тарифный профиль?')) return;

        const originalText = this.dom.btnDelete.innerText;
        this.dom.btnDelete.innerText = 'Удаление...';
        this.dom.btnDelete.disabled = true;

        try {
            await api.delete(`/tariffs/${id}`);
            toast('Тарифный профиль удален', 'success');

            // ВАЖНО: Очищаем кэш тарифов
            sessionStorage.removeItem('tariffs_cache');

            // Перезагружаем и переключаемся на базовый тариф (id=1)
            this.load(1);
        } catch (error) {
            toast('Ошибка удаления: ' + error.message, 'error');
        } finally {
            this.dom.btnDelete.innerText = originalText;
            this.dom.btnDelete.disabled = false;
        }
    }
};