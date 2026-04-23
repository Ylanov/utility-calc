// static/js/modules/client-certificates.js
//
// Вкладка «Справки» клиентского портала. Персональные данные (паспорт,
// ФИО, регистрация) и семья теперь живут ВНУТРИ модалки заказа справки
// как сворачиваемые секции — жилец заполняет их один раз при первом
// заказе, после чего просто вводит «период + куда» и получает PDF.
//
// Секции в модалке заказа:
//   1. Предупреждение «нужно заполнить» / баннер «всё готово»
//   2. <details> «Мои данные» — паспорт, должность, регистрация
//   3. <details> «Состав семьи» — необязательно
//   4. Форма заказа — период + куда, кнопка активна только если профиль ОК

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
        if (!this.dom.certTab) {
            // Старая версия HTML без вкладки «Справки» — молчим.
            return;
        }
        this.bindEvents();
        this.isInitialized = true;

        // Профиль грузим ЛЕНИВО — только при открытии модалки заказа
        // или при первом клике на вкладку «Справки». Раньше он тянулся
        // сразу на старте и давал 500 если таблица users ещё без новых
        // колонок.
        if (document.getElementById('tab-certificates')?.classList.contains('active')) {
            this.loadCerts();
        }
    },

    cacheDOM() {
        this.dom = {
            certTab: document.getElementById('tab-certificates'),
            certTabBtn: document.querySelector('.tab-btn[data-tab="tab-certificates"]'),
            catalog: document.getElementById('certCatalog'),
            certsList: document.getElementById('certsList'),
            btnRefreshCerts: document.getElementById('btnRefreshCerts'),

            // Модалка ФЛС
            flcModal: document.getElementById('certFlcModal'),
            flcPurpose: document.getElementById('certPurpose'),
            flcFrom: document.getElementById('certPeriodFrom'),
            flcTo: document.getElementById('certPeriodTo'),
            flcContractBlock: document.getElementById('certContractBlock'),
            flcContractInfo: document.getElementById('certContractInfo'),
            flcWarn: document.getElementById('certProfileWarn'),
            flcOk: document.getElementById('certProfileOk'),
            flcSubmit: document.getElementById('btnSubmitCertOrder'),

            // Секция «Мои данные» — inline-форма профиля
            profileSection: document.getElementById('certProfileSection'),
            profileHint: document.getElementById('certProfileHint'),
            profileForm: document.getElementById('certProfileForm'),
            profFullName: document.getElementById('profFullName'),
            profPosition: document.getElementById('profPosition'),
            profPassSeries: document.getElementById('profPassSeries'),
            profPassNumber: document.getElementById('profPassNumber'),
            profPassIssuedBy: document.getElementById('profPassIssuedBy'),
            profPassIssuedAt: document.getElementById('profPassIssuedAt'),
            profRegDate: document.getElementById('profRegDate'),

            // Секция «Семья»
            familyList: document.getElementById('familyList'),
            familyCount: document.getElementById('certFamilyCount'),
            btnAddFamily: document.getElementById('btnAddFamilyMember'),
            familyModal: document.getElementById('familyMemberModal'),
            familyForm: document.getElementById('familyMemberForm'),
            familyModalTitle: document.getElementById('familyModalTitle'),
            familyId: document.getElementById('familyMemberId'),
            familyRole: document.getElementById('familyRole'),
            familyFullName: document.getElementById('familyFullName'),
            familyBirthDate: document.getElementById('familyBirthDate'),
        };
    },

    bindEvents() {
        // Ленивая загрузка списка при переключении на вкладку
        this.dom.certTabBtn?.addEventListener('click', () => {
            if (!this.certsLoaded) this.loadCerts();
        });

        // Открытие заказа из каталога
        this.dom.catalog?.addEventListener('click', (e) => {
            const card = e.target.closest('.cert-card[data-cert-type]');
            if (!card) return;
            if (card.dataset.certType === 'flc') this.openFlcModal();
        });

        // Закрытие модалки ФЛС
        this.dom.flcModal?.addEventListener('click', (e) => {
            if (e.target.closest('[data-cert-close]') || e.target === this.dom.flcModal) {
                this.dom.flcModal.classList.remove('open');
            }
        });

        // Кнопки в модалке
        this.dom.flcSubmit?.addEventListener('click', () => this.submitFlcOrder());
        this.dom.profileForm?.addEventListener('submit', (e) => this.saveProfileInline(e));
        this.dom.btnRefreshCerts?.addEventListener('click', () => this.loadCerts());
        this.dom.certsList?.addEventListener('click', (e) => {
            const btn = e.target.closest('[data-cert-download]');
            if (btn) this.downloadCert(Number(btn.dataset.certDownload));
        });

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
    // PROFILE (ленивая загрузка)
    // =====================================================
    async loadProfile() {
        try {
            this.profile = await api.get('/me/profile');
        } catch (e) {
            // Нет профиля / 500 — не блокируем вкладку, просто покажем форму.
            this.profile = null;
            console.warn('Profile load failed:', e.message);
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

    async saveProfileInline(e) {
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
        const orig = btn.innerHTML;
        btn.disabled = true;
        btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Сохранение…';
        try {
            this.profile = await api.put('/me/profile', payload);
            toast('Данные сохранены', 'success');
            // После сохранения обновляем баннер + скрываем секцию
            this.updateModalState();
        } catch (err) {
            toast('Ошибка сохранения: ' + err.message, 'error');
        } finally {
            btn.disabled = false;
            btn.innerHTML = orig;
        }
    },

    // Проверка «всё ли готово для заказа»
    _missingFields() {
        const p = this.profile || {};
        const missing = [];
        if (!(p.full_name || p.username)) missing.push('ФИО');
        if (!p.passport_series || !p.passport_number) missing.push('паспорт (серия и номер)');
        if (!p.registration_date) missing.push('дата регистрации');
        return missing;
    },

    updateModalState() {
        const missing = this._missingFields();
        const ready = missing.length === 0;

        // Баннеры
        if (this.dom.flcWarn) {
            if (ready) {
                this.dom.flcWarn.style.display = 'none';
            } else {
                this.dom.flcWarn.style.display = 'block';
                this.dom.flcWarn.innerHTML = `
                    <b><i class="fa-solid fa-circle-exclamation"></i> Заполните данные для заказа</b>
                    <div style="margin-top:6px;">
                        Недостающие поля: <b>${missing.map(esc).join(', ')}</b>.
                        Раскройте секцию «Мои данные» ниже и заполните — потом сможете заказать справку.
                    </div>
                `;
            }
        }
        if (this.dom.flcOk) {
            this.dom.flcOk.style.display = ready ? 'block' : 'none';
        }

        // Секция профиля: если не готов — открыта; если готов — свёрнута
        if (this.dom.profileSection) {
            this.dom.profileSection.open = !ready;
        }
        if (this.dom.profileHint) {
            this.dom.profileHint.textContent = ready
                ? '— сохранено, можно менять'
                : '— заполняется один раз';
        }

        // Кнопка заказа
        if (this.dom.flcSubmit) {
            this.dom.flcSubmit.disabled = !ready;
            this.dom.flcSubmit.style.opacity = ready ? '1' : '0.5';
            this.dom.flcSubmit.style.cursor = ready ? 'pointer' : 'not-allowed';
        }
    },

    // =====================================================
    // FAMILY (ленивая, внутри модалки)
    // =====================================================
    async loadFamily() {
        if (!this.dom.familyList) return;
        try {
            this.family = await api.get('/me/family');
            this.renderFamily();
        } catch (e) {
            this.dom.familyList.innerHTML =
                `<div style="padding:10px; color:var(--danger-color); font-size:12px;">Ошибка: ${esc(e.message)}</div>`;
        }
    },

    renderFamily() {
        if (this.dom.familyCount) {
            this.dom.familyCount.textContent = `— (${this.family.length})`;
        }
        if (!this.family.length) {
            this.dom.familyList.innerHTML = `
                <div style="padding:10px; color:var(--text-secondary); font-size:12px; font-style:italic;">
                    Пока никого не добавлено.
                </div>`;
            return;
        }
        this.dom.familyList.innerHTML = `
            <div style="display:flex; flex-direction:column; gap:6px;">
                ${this.family.map(m => `
                    <div style="display:flex; align-items:center; gap:10px; padding:8px 10px;
                                background:var(--bg-page); border-radius:6px; border:1px solid var(--border-color);">
                        <i class="fa-solid fa-user" style="color:var(--primary-color); font-size:14px;"></i>
                        <div style="flex:1; min-width:0;">
                            <div style="font-weight:600; font-size:13px;">${esc(m.full_name)}</div>
                            <div style="font-size:11px; color:var(--text-secondary);">
                                ${esc(ROLE_LABEL[m.role] || m.role)}${m.birth_date ? ' · ' + esc(fmtDate(m.birth_date)) + ' г.р.' : ''}
                            </div>
                        </div>
                        <button class="icon-btn" data-family-edit="${m.id}" title="Редактировать" style="padding:4px;">
                            <i class="fa-solid fa-pen"></i>
                        </button>
                        <button class="icon-btn" data-family-delete="${m.id}" title="Удалить"
                                style="padding:4px; color:var(--danger-color);">
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
            d.familyModalTitle.textContent = 'Редактировать';
            d.familyId.value = String(member.id);
            d.familyRole.value = member.role;
            d.familyFullName.value = member.full_name || '';
            d.familyBirthDate.value = member.birth_date || '';
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
        };
        const btn = this.dom.familyForm.querySelector('button[type="submit"]');
        const orig = btn.innerHTML;
        btn.disabled = true;
        btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i>';
        try {
            if (id) await api.put(`/me/family/${id}`, payload);
            else await api.post('/me/family', payload);
            toast('Сохранено', 'success');
            this.dom.familyModal.classList.remove('open');
            this.loadFamily();
        } catch (err) {
            toast('Ошибка: ' + err.message, 'error');
        } finally {
            btn.disabled = false;
            btn.innerHTML = orig;
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
    // CERTIFICATES
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
    // МОДАЛКА ЗАКАЗА — теперь включает inline-профиль/семью
    // =====================================================
    async openFlcModal() {
        // Открываем модалку сразу, грузим данные параллельно
        this.dom.flcModal.classList.add('open');
        this.dom.flcPurpose.value = '';
        this.dom.flcFrom.value = '';
        this.dom.flcTo.value = '';

        // Грузим профиль + семью + договор параллельно
        const tasks = [this.loadProfile(), this.loadFamily()];
        try {
            tasks.push(api.get('/me/rental-contracts').then(r => { this.contracts = r; }).catch(() => {}));
        } catch {}
        await Promise.all(tasks);

        this.populateProfileForm();
        this.updateModalState();

        // Договор найма — если есть активный, показываем инфо-блок
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
    },

    async submitFlcOrder() {
        // Финальная проверка
        if (this._missingFields().length > 0) {
            toast('Сначала заполните «Мои данные»', 'warning');
            this.updateModalState();
            this.dom.profileSection.open = true;
            return;
        }

        const purpose = this.dom.flcPurpose.value.trim();
        if (!purpose) return toast('Укажите куда предоставить справку', 'error');

        const payload = {
            type: 'flc',
            period_from: this.dom.flcFrom.value || null,
            period_to: this.dom.flcTo.value || null,
            purpose,
        };
        const btn = this.dom.flcSubmit;
        const orig = btn.innerHTML;
        btn.disabled = true;
        btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Генерация PDF…';
        try {
            const cert = await api.post('/me/certificates', payload);
            this.dom.flcModal.classList.remove('open');
            toast('Заявка принята — PDF готов', 'success');
            await this.loadCerts();
            if (cert.has_pdf) this.downloadCert(cert.id);
        } catch (err) {
            const data = err.data;
            if (data?.detail?.missing_fields) {
                toast('Не заполнено: ' + data.detail.missing_fields.join(', '), 'error');
                this.updateModalState();
                this.dom.profileSection.open = true;
            } else {
                toast('Ошибка: ' + err.message, 'error');
            }
        } finally {
            btn.disabled = false;
            btn.innerHTML = orig;
        }
    },
};
