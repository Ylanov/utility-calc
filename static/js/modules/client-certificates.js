// static/js/modules/client-certificates.js
//
// Логика клиентской вкладки «Справки» + блок «Персональные данные» на
// вкладке «Профиль». Жилец:
//   1) Заполняет ФИО + паспорт + дату регистрации (для заказа справок).
//   2) Добавляет семью (супруг(а), дети) — прикладывается к справкам.
//   3) Заказывает справку из каталога (сейчас один тип: ФЛС).
//   4) Видит историю заявок и скачивает готовый PDF.
//
// Принцип: данные грузятся ЛЕНИВО — только при клике на вкладку, или
// когда открывается модалка заказа. Не нагружаем /api/me/... на каждой
// загрузке страницы.

import { api } from '../core/api.js';
import { toast } from '../core/dom.js';

function esc(s) {
    if (s == null) return '';
    const d = document.createElement('div');
    d.textContent = String(s);
    return d.innerHTML;
}

function fmtDate(iso) {
    if (!iso) return '—';
    try { return new Date(iso).toLocaleDateString('ru-RU'); } catch { return iso; }
}

const ROLE_LABEL = {
    spouse: 'Супруг(а)',
    child: 'Ребёнок',
    parent: 'Родитель',
    other: 'Член семьи',
};

const STATUS_META = {
    pending:   { color: '#f59e0b', bg: '#fef3c7', label: 'В обработке' },
    generated: { color: '#10b981', bg: '#d1fae5', label: 'Готова' },
    delivered: { color: '#3b82f6', bg: '#dbeafe', label: 'Выдана' },
    rejected:  { color: '#ef4444', bg: '#fee2e2', label: 'Отклонена' },
};

export const ClientCertificates = {
    isInitialized: false,
    profile: null,
    family: [],
    contracts: [],
    certs: [],
    certsLoaded: false,

    init() {
        if (this.isInitialized) return;
        this.cacheDOM();
        if (!this.dom.certTab || !this.dom.profileForm) {
            // На портале может не быть нужной разметки (старая версия
            // index.html — тогда просто молчим, ничего не инициализируем).
            return;
        }
        this.bindEvents();
        this.isInitialized = true;

        // Загружаем профиль + семью сразу — они нужны и для вкладки Профиль,
        // и для pre-flight проверки при заказе справки.
        this.loadProfile();
        this.loadFamily();

        // Если при старте юзер оказался на вкладке «Справки» (через hash или
        // deep-link) — сразу подгружаем список заявок.
        if (document.getElementById('tab-certificates')?.classList.contains('active')) {
            this.loadCerts();
        }
    },

    cacheDOM() {
        this.dom = {
            // Табы
            certTab: document.getElementById('tab-certificates'),
            certTabBtn: document.querySelector('.tab-btn[data-tab="tab-certificates"]'),

            // Каталог и список заявок
            catalog: document.getElementById('certCatalog'),
            certsList: document.getElementById('certsList'),
            btnRefreshCerts: document.getElementById('btnRefreshCerts'),

            // Модалка заказа ФЛС
            flcModal: document.getElementById('certFlcModal'),
            flcForm: document.getElementById('certFlcForm'),
            flcPurpose: document.getElementById('certPurpose'),
            flcFrom: document.getElementById('certPeriodFrom'),
            flcTo: document.getElementById('certPeriodTo'),
            flcContractBlock: document.getElementById('certContractBlock'),
            flcContractInfo: document.getElementById('certContractInfo'),
            flcWarn: document.getElementById('certProfileWarn'),

            // Форма персональных данных
            profileForm: document.getElementById('profileDataForm'),
            profFullName: document.getElementById('profFullName'),
            profPosition: document.getElementById('profPosition'),
            profPassSeries: document.getElementById('profPassSeries'),
            profPassNumber: document.getElementById('profPassNumber'),
            profPassIssuedBy: document.getElementById('profPassIssuedBy'),
            profPassIssuedAt: document.getElementById('profPassIssuedAt'),
            profRegDate: document.getElementById('profRegDate'),

            // Семья
            familyList: document.getElementById('familyList'),
            btnAddFamily: document.getElementById('btnAddFamilyMember'),
            familyModal: document.getElementById('familyMemberModal'),
            familyForm: document.getElementById('familyMemberForm'),
            familyModalTitle: document.getElementById('familyModalTitle'),
            familyId: document.getElementById('familyMemberId'),
            familyRole: document.getElementById('familyRole'),
            familyFullName: document.getElementById('familyFullName'),
            familyBirthDate: document.getElementById('familyBirthDate'),
            familyPassSeries: document.getElementById('familyPassSeries'),
            familyPassNumber: document.getElementById('familyPassNumber'),
            familyRegDate: document.getElementById('familyRegDate'),
        };
    },

    bindEvents() {
        // Клик по табу «Справки» — первая ленивая загрузка списка заявок.
        this.dom.certTabBtn?.addEventListener('click', () => {
            if (!this.certsLoaded) this.loadCerts();
        });

        // Клик по карточке каталога — открыть модалку соответствующего типа.
        this.dom.catalog?.addEventListener('click', (e) => {
            const card = e.target.closest('.cert-card[data-cert-type]');
            if (!card) return;
            const type = card.dataset.certType;
            if (type === 'flc') this.openFlcModal();
        });

        // Закрытие модалки ФЛС
        this.dom.flcModal?.addEventListener('click', (e) => {
            if (e.target.closest('[data-cert-close]') || e.target === this.dom.flcModal) {
                this.dom.flcModal.classList.remove('open');
            }
        });
        this.dom.flcForm?.addEventListener('submit', (e) => this.submitFlcOrder(e));

        this.dom.btnRefreshCerts?.addEventListener('click', () => this.loadCerts());
        this.dom.certsList?.addEventListener('click', (e) => {
            const btn = e.target.closest('[data-cert-download]');
            if (btn) this.downloadCert(Number(btn.dataset.certDownload));
        });

        // Форма профиля
        this.dom.profileForm?.addEventListener('submit', (e) => this.submitProfile(e));

        // Семья
        this.dom.btnAddFamily?.addEventListener('click', () => this.openFamilyModal(null));
        this.dom.familyModal?.addEventListener('click', (e) => {
            if (e.target.closest('[data-family-close]') || e.target === this.dom.familyModal) {
                this.dom.familyModal.classList.remove('open');
            }
        });
        this.dom.familyForm?.addEventListener('submit', (e) => this.submitFamily(e));
        this.dom.familyList?.addEventListener('click', (e) => {
            const edit = e.target.closest('[data-family-edit]');
            const del = e.target.closest('[data-family-delete]');
            if (edit) {
                const m = this.family.find(f => f.id === Number(edit.dataset.familyEdit));
                if (m) this.openFamilyModal(m);
            }
            if (del) this.deleteFamily(Number(del.dataset.familyDelete));
        });
    },

    // =====================================================
    // PROFILE
    // =====================================================
    async loadProfile() {
        try {
            this.profile = await api.get('/me/profile');
            this.populateProfileForm();
        } catch (e) {
            toast('Не удалось загрузить профиль: ' + e.message, 'error');
        }
    },

    populateProfileForm() {
        const p = this.profile || {};
        const d = this.dom;
        if (d.profFullName)    d.profFullName.value    = p.full_name || '';
        if (d.profPosition)    d.profPosition.value    = p.position || '';
        if (d.profPassSeries)  d.profPassSeries.value  = p.passport_series || '';
        if (d.profPassNumber)  d.profPassNumber.value  = p.passport_number || '';
        if (d.profPassIssuedBy)d.profPassIssuedBy.value= p.passport_issued_by || '';
        if (d.profPassIssuedAt)d.profPassIssuedAt.value= p.passport_issued_at || '';
        if (d.profRegDate)     d.profRegDate.value     = p.registration_date || '';
    },

    async submitProfile(e) {
        e.preventDefault();
        const payload = {
            full_name: this.dom.profFullName.value.trim() || null,
            position: this.dom.profPosition.value.trim() || null,
            passport_series: this.dom.profPassSeries.value.trim() || null,
            passport_number: this.dom.profPassNumber.value.trim() || null,
            passport_issued_by: this.dom.profPassIssuedBy.value.trim() || null,
            passport_issued_at: this.dom.profPassIssuedAt.value || null,
            registration_date: this.dom.profRegDate.value || null,
        };
        const btn = this.dom.profileForm.querySelector('button[type="submit"]');
        const originalHtml = btn.innerHTML;
        btn.disabled = true;
        btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Сохранение…';
        try {
            this.profile = await api.put('/me/profile', payload);
            toast('Данные сохранены', 'success');
        } catch (err) {
            toast('Ошибка сохранения: ' + err.message, 'error');
        } finally {
            btn.disabled = false;
            btn.innerHTML = originalHtml;
        }
    },

    // =====================================================
    // FAMILY
    // =====================================================
    async loadFamily() {
        if (!this.dom.familyList) return;
        try {
            this.family = await api.get('/me/family');
            this.renderFamily();
        } catch (e) {
            this.dom.familyList.innerHTML =
                `<div style="padding:16px; color:var(--danger-color);">Ошибка: ${esc(e.message)}</div>`;
        }
    },

    renderFamily() {
        if (!this.family.length) {
            this.dom.familyList.innerHTML = `
                <div style="padding:20px; text-align:center; color:var(--text-secondary); font-size:13px;">
                    Пока нет записей. Добавьте супруга(у), детей или других членов семьи,
                    чтобы их данные попадали в справки.
                </div>`;
            return;
        }
        this.dom.familyList.innerHTML = `
            <div style="display:flex; flex-direction:column; gap:8px;">
                ${this.family.map(m => `
                    <div style="display:flex; align-items:center; gap:12px; padding:12px 14px;
                                background:var(--bg-page); border-radius:8px; border:1px solid var(--border-color);">
                        <div style="width:40px; height:40px; border-radius:50%; background:var(--primary-bg);
                                    display:flex; align-items:center; justify-content:center; flex-shrink:0;">
                            <i class="fa-solid fa-user" style="color:var(--primary-color);"></i>
                        </div>
                        <div style="flex:1; min-width:0;">
                            <div style="font-weight:600; color:var(--text-main);">${esc(m.full_name)}</div>
                            <div style="font-size:12px; color:var(--text-secondary); margin-top:2px;">
                                ${esc(ROLE_LABEL[m.role] || m.role)}${m.birth_date ? ' · ' + esc(fmtDate(m.birth_date)) + ' г.р.' : ''}
                            </div>
                        </div>
                        <button class="icon-btn" data-family-edit="${m.id}" title="Редактировать">
                            <i class="fa-solid fa-pen"></i>
                        </button>
                        <button class="icon-btn" data-family-delete="${m.id}" title="Удалить"
                                style="color:var(--danger-color);">
                            <i class="fa-solid fa-trash"></i>
                        </button>
                    </div>
                `).join('')}
            </div>
        `;
    },

    openFamilyModal(member) {
        const d = this.dom;
        d.familyForm.reset();
        if (member) {
            d.familyModalTitle.textContent = 'Редактировать члена семьи';
            d.familyId.value = String(member.id);
            d.familyRole.value = member.role;
            d.familyFullName.value = member.full_name || '';
            d.familyBirthDate.value = member.birth_date || '';
            d.familyPassSeries.value = member.passport_series || '';
            d.familyPassNumber.value = member.passport_number || '';
            d.familyRegDate.value = member.registration_date || '';
        } else {
            d.familyModalTitle.textContent = 'Добавить члена семьи';
            d.familyId.value = '';
        }
        d.familyModal.classList.add('open');
    },

    async submitFamily(e) {
        e.preventDefault();
        const id = this.dom.familyId.value;
        const payload = {
            role: this.dom.familyRole.value,
            full_name: this.dom.familyFullName.value.trim(),
            birth_date: this.dom.familyBirthDate.value || null,
            passport_series: this.dom.familyPassSeries.value.trim() || null,
            passport_number: this.dom.familyPassNumber.value.trim() || null,
            registration_date: this.dom.familyRegDate.value || null,
        };
        const btn = this.dom.familyForm.querySelector('button[type="submit"]');
        const originalHtml = btn.innerHTML;
        btn.disabled = true;
        btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Сохранение…';
        try {
            if (id) {
                await api.put(`/me/family/${id}`, payload);
            } else {
                await api.post('/me/family', payload);
            }
            toast('Сохранено', 'success');
            this.dom.familyModal.classList.remove('open');
            this.loadFamily();
        } catch (err) {
            toast('Ошибка: ' + err.message, 'error');
        } finally {
            btn.disabled = false;
            btn.innerHTML = originalHtml;
        }
    },

    async deleteFamily(id) {
        if (!confirm('Удалить запись?')) return;
        try {
            await api.delete(`/me/family/${id}`);
            toast('Удалено', 'success');
            this.loadFamily();
        } catch (e) {
            toast('Ошибка: ' + e.message, 'error');
        }
    },

    // =====================================================
    // CERTIFICATES — заказ и история
    // =====================================================
    async loadCerts() {
        if (!this.dom.certsList) return;
        this.certsLoaded = true;
        this.dom.certsList.innerHTML = `
            <div style="padding:20px; text-align:center; color:var(--text-secondary);">
                <i class="fa-solid fa-spinner fa-spin"></i> Загрузка…
            </div>`;
        try {
            this.certs = await api.get('/me/certificates');
            this.renderCerts();
        } catch (e) {
            this.dom.certsList.innerHTML =
                `<div style="padding:16px; color:var(--danger-color);">Ошибка: ${esc(e.message)}</div>`;
        }
    },

    renderCerts() {
        if (!this.certs.length) {
            this.dom.certsList.innerHTML = `
                <div style="padding:30px; text-align:center; color:var(--text-secondary); font-size:13px;">
                    Пока нет заявок. Выберите справку выше и закажите — PDF будет готов за пару секунд.
                </div>`;
            return;
        }
        const TYPE_LABEL = { flc: 'Выписка из ФЛС' };
        this.dom.certsList.innerHTML = `
            <div style="display:flex; flex-direction:column; gap:8px;">
                ${this.certs.map(c => {
                    const meta = STATUS_META[c.status] || { color: '#6b7280', bg: '#f3f4f6', label: c.status };
                    return `
                        <div style="display:flex; align-items:center; gap:12px; padding:12px 14px;
                                    background:var(--bg-card); border-radius:8px; border:1px solid var(--border-color);">
                            <div style="width:40px; height:40px; border-radius:8px; background:${meta.bg};
                                        display:flex; align-items:center; justify-content:center; flex-shrink:0;">
                                <i class="fa-solid fa-file-pdf" style="color:${meta.color};"></i>
                            </div>
                            <div style="flex:1; min-width:0;">
                                <div style="font-weight:600;">${esc(TYPE_LABEL[c.type] || c.type)}</div>
                                <div style="font-size:12px; color:var(--text-secondary); margin-top:2px;">
                                    №${c.id} · ${esc(fmtDate(c.created_at))}
                                    ${c.data?.purpose ? ' · для: ' + esc(c.data.purpose) : ''}
                                </div>
                            </div>
                            <span style="padding:3px 10px; border-radius:12px; background:${meta.bg}; color:${meta.color}; font-size:11px; font-weight:600; white-space:nowrap;">
                                ${esc(meta.label)}
                            </span>
                            ${c.has_pdf ? `
                                <button class="action-btn primary-btn" data-cert-download="${c.id}"
                                        style="padding:6px 12px; font-size:12px; white-space:nowrap;">
                                    <i class="fa-solid fa-download"></i> Скачать
                                </button>` : ''}
                        </div>
                    `;
                }).join('')}
            </div>
        `;
    },

    async downloadCert(id) {
        try {
            await api.download(`/me/certificates/${id}/download`, `Zayavlenie_FLS_${id}.pdf`);
        } catch (e) {
            toast('Ошибка скачивания: ' + e.message, 'error');
        }
    },

    // =====================================================
    // МОДАЛКА ЗАКАЗА ФЛС
    // =====================================================
    async openFlcModal() {
        this.dom.flcForm.reset();
        this.dom.flcWarn.style.display = 'none';
        this.dom.flcContractBlock.style.display = 'none';

        // Pre-flight: проверяем профиль и подгружаем договор.
        // Если чего-то не хватает — показываем красный баннер СРАЗУ в модалке,
        // вместо того чтобы пустить формы и получить 400 от бекенда.
        if (!this.profile) {
            await this.loadProfile();
        }
        const p = this.profile || {};
        const missing = [];
        if (!p.full_name && !p.username) missing.push('ФИО');
        if (!p.passport_series || !p.passport_number) missing.push('паспорт (серия и номер)');
        if (!p.registration_date) missing.push('дата регистрации');

        if (missing.length) {
            this.dom.flcWarn.style.display = 'block';
            this.dom.flcWarn.innerHTML = `
                <b><i class="fa-solid fa-circle-exclamation"></i> Заполните профиль перед заказом</b>
                <div style="margin-top:6px;">
                    Недостающие данные: <b>${missing.map(esc).join(', ')}</b>.
                    Перейдите на вкладку «Профиль», заполните и вернитесь.
                </div>
            `;
        }

        // Показываем инфо о договоре найма, если есть.
        try {
            this.contracts = await api.get('/me/rental-contracts');
        } catch { this.contracts = []; }
        const active = this.contracts.find(c => c.is_active) || this.contracts[0];
        if (active) {
            this.dom.flcContractBlock.style.display = 'block';
            this.dom.flcContractInfo.innerHTML = `
                <b>№ ${esc(active.number || '—')}</b>
                · от ${esc(fmtDate(active.signed_date))}
                ${active.valid_until ? ' · действует до ' + esc(fmtDate(active.valid_until)) : ''}
            `;
        } else {
            this.dom.flcContractBlock.style.display = 'block';
            this.dom.flcContractInfo.innerHTML = `
                <span style="color:var(--warning-color);">⚠ Активный договор не найден.</span>
                Поля «дата/№ договора» в справке останутся пустыми — админ может дозаполнить.
            `;
        }

        this.dom.flcModal.classList.add('open');
    },

    async submitFlcOrder(e) {
        e.preventDefault();
        const payload = {
            type: 'flc',
            period_from: this.dom.flcFrom.value || null,
            period_to: this.dom.flcTo.value || null,
            purpose: this.dom.flcPurpose.value.trim(),
        };
        if (!payload.purpose) return toast('Укажите куда предоставить справку', 'error');

        const btn = this.dom.flcForm.querySelector('button[type="submit"]');
        const originalHtml = btn.innerHTML;
        btn.disabled = true;
        btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Генерируем PDF…';
        try {
            const cert = await api.post('/me/certificates', payload);
            this.dom.flcModal.classList.remove('open');
            toast('Заявка принята — PDF готов', 'success');
            await this.loadCerts();
            // Автоматически скачиваем — чтобы жилец сразу получил документ.
            if (cert.has_pdf) {
                this.downloadCert(cert.id);
            }
        } catch (err) {
            // Если backend вернул структурированный detail с missing_fields
            // (профиль не заполнен) — показываем в баннере модалки.
            const data = err.data;
            if (data?.detail?.missing_fields) {
                this.dom.flcWarn.style.display = 'block';
                this.dom.flcWarn.innerHTML = `
                    <b><i class="fa-solid fa-circle-exclamation"></i> ${esc(data.detail.message)}</b>
                    <div style="margin-top:6px;">
                        Не заполнено: <b>${data.detail.missing_fields.map(esc).join(', ')}</b>
                    </div>`;
            } else {
                toast('Ошибка: ' + err.message, 'error');
            }
        } finally {
            btn.disabled = false;
            btn.innerHTML = originalHtml;
        }
    },
};
