let formInitialized = false;
let sipInitialized = false;
let fallbackInitialized = false;
let lastHistogram = null;
let lastSpectrum = null;
let streamConnected = false;

const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
}[c]));

const ROLE = (document.body.dataset.role || 'operator').trim();
const IS_ADMIN = ROLE === 'admin';

// ==================== Модалки ====================
function openModal(id) {
    const m = document.getElementById(id);
    if (!m) return;
    m.classList.add('open');
    document.body.style.overflow = 'hidden';
}
function closeModal(id) {
    const m = id ? document.getElementById(id) : null;
    if (m) m.classList.remove('open');
    document.body.style.overflow = '';
}
function closeAllModals() {
    document.querySelectorAll('.modal.open').forEach(m => m.classList.remove('open'));
    document.body.style.overflow = '';
}
document.querySelectorAll('[data-open]').forEach(btn => {
    btn.onclick = () => openModal(btn.dataset.open);
});
document.querySelectorAll('[data-close]').forEach(el => {
    el.onclick = () => {
        const modal = el.closest('.modal');
        if (modal) closeModal(modal.id);
        else closeAllModals();
    };
});
document.addEventListener('keydown', e => {
    if (e.key === 'Escape') closeAllModals();
});

// ==================== Стрелочные индикаторы ====================
function setMeter(id, value, min, max) {
    const el = $(id);
    if (!el) return;
    const clamp = Math.max(min, Math.min(max, value));
    const ratio = (clamp - min) / (max - min);
    const angle = -90 + ratio * 180;
    el.setAttribute('transform', `rotate(${angle.toFixed(1)} 50 52)`);
}

// ==================== Эквалайзер ====================
function drawEqualizer(spectrum) {
    const canvas = $('eq-canvas');
    if (!canvas) return;
    const dpr = window.devicePixelRatio || 1;
    const W = canvas.clientWidth;
    const H = canvas.clientHeight || 90;
    canvas.width = W * dpr;
    canvas.height = H * dpr;
    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);

    const bars = spectrum.length || 24;
    const barW = (W / bars) * 0.72;
    const gap = (W / bars) * 0.28;
    for (let i = 0; i < bars; i++) {
        const v = Math.max(0, Math.min(1, spectrum[i] || 0));
        const h = v * (H - 4);
        const x = i * (barW + gap) + gap / 2;
        const y = H - h;
        const grad = ctx.createLinearGradient(0, H, 0, 0);
        grad.addColorStop(0, '#3aa83a');
        grad.addColorStop(0.5, '#d4c44a');
        grad.addColorStop(0.85, '#e07a3a');
        grad.addColorStop(1, '#c83a3a');
        ctx.fillStyle = grad;
        ctx.fillRect(x, y, barW, h);
        if (v > 0.08) {
            ctx.fillStyle = 'rgba(255,230,150,0.7)';
            ctx.fillRect(x, y, barW, 2);
        }
    }
    ctx.strokeStyle = 'rgba(60,40,25,0.3)';
    for (let i = 1; i < 4; i++) {
        const y = (H / 4) * i;
        ctx.beginPath();
        ctx.moveTo(0, y);
        ctx.lineTo(W, y);
        ctx.stroke();
    }
}

// ==================== Гистограмма субтонов ====================
function drawToneHistogram(hist) {
    const canvas = $('tone-canvas');
    if (!canvas) return;
    const dpr = window.devicePixelRatio || 1;
    const W = canvas.clientWidth;
    const H = canvas.clientHeight || 90;
    canvas.width = W * dpr;
    canvas.height = H * dpr;
    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);

    const buckets = hist.buckets || [];
    if (!buckets.length) return;
    const maxCount = Math.max(1, ...buckets.map(b => b.ctcss + b.dcs));
    const bw = W / buckets.length;
    const padY = 6;
    const plotH = H - 2 * padY;

    ctx.strokeStyle = 'rgba(60,40,25,0.3)';
    for (let i = 1; i <= 3; i++) {
        const y = padY + plotH * (i / 4);
        ctx.beginPath();
        ctx.moveTo(0, y);
        ctx.lineTo(W, y);
        ctx.stroke();
    }
    for (let i = 0; i < buckets.length; i++) {
        const b = buckets[i];
        const total = b.ctcss + b.dcs;
        if (total === 0) continue;
        const x = i * bw;
        const barW = Math.max(2, bw - 1);
        const h = (total / maxCount) * plotH;
        const ctcssH = (b.ctcss / total) * h;
        const dcsH = h - ctcssH;
        ctx.fillStyle = '#3aa83a';
        ctx.fillRect(x, H - padY - ctcssH, barW, ctcssH);
        if (b.dcs > 0) {
            ctx.fillStyle = '#3a7ac8';
            ctx.fillRect(x, H - padY - ctcssH - dcsH, barW, dcsH);
        }
    }
    ctx.strokeStyle = 'rgba(200,60,60,0.6)';
    ctx.beginPath();
    ctx.moveTo(W - 0.5, padY);
    ctx.lineTo(W - 0.5, H - padY);
    ctx.stroke();
}

window.addEventListener('resize', () => {
    if (lastHistogram) drawToneHistogram(lastHistogram);
    if (lastSpectrum) drawEqualizer(lastSpectrum);
});

// ==================== Рендер статуса ====================
function renderIndicators(state) {
    $('ind-ctcss').classList.toggle('active', !!state.ctcss_detected);
    $('ind-dcs').classList.toggle('active', !!state.dcs_detected);
    $('ind-onair').classList.toggle('active', !!state.announcement_active || !!state.sip_active);
    $('ind-ctcss-p').textContent = (state.ctcss_power ?? 0).toFixed(3);
    $('ind-dcs-p').textContent = (state.dcs_power ?? 0).toFixed(3);
}

function renderMeters(state) {
    setMeter('needle-signal', state.signal_db ?? -90, -60, 0);
    $('signal-db').textContent = (state.signal_db ?? -90).toFixed(1);

    setMeter('needle-cpu', state.cpu_percent ?? 0, 0, 100);
    $('cpu-val').textContent = Math.round(state.cpu_percent ?? 0);

    const bps = state.network_bps || 0;
    const kb = bps / 1024;
    setMeter('needle-net', kb, 0, 64);
    $('net-val').textContent = bps < 1024 ? bps : kb.toFixed(1);
}

function renderStatus(state, settings) {
    $('running').textContent = state.running ? '✅ работает' : '⏹ остановлен';
    $('current-source').textContent = state.current_source || '—';
    $('count').textContent = state.announcements_played;
    $('event').textContent = state.last_event || '—';
    $('error').textContent = state.error || '—';
    $('seconds-since-tone').textContent = state.seconds_since_tone ?? 0;
    // DTMF-дисплей
    if ($('dtmf-display')) {
        const seq = state.dtmf_sequence || '';
        const last = state.last_dtmf_digit || '';
        const match = state.dtmf_last_match || '';
        let parts = [];
        if (seq) parts.push(`...${seq}`);
        else if (last) parts.push(`последняя: ${last}`);
        if (match) parts.push(`✓ ${match}`);
        $('dtmf-display').textContent = parts.length ? parts.join(' ') : '—';
    }
    const btnStart = $('btn-start');
    const btnStop = $('btn-stop');
    if (btnStart) btnStart.disabled = state.running;
    if (btnStop) btnStop.disabled = !state.running;

    let nowText;
    if (state.sip_active) nowText = '📞 SIP-звонок';
    else if (!state.running) nowText = '⏹ остановлен';
    else if (state.announcement_active && state.playing_name) nowText = `🔴 ${state.playing_name}`;
    else if (state.announcement_active) nowText = '🔴 объявление';
    else if (state.using_reserve) nowText = '🟡 резерв';
    else nowText = '🟢 трансляция';
    $('onair-text').textContent = nowText;

    renderMeters(state);
    renderIndicators(state);

    if (state.spectrum) {
        lastSpectrum = state.spectrum;
        drawEqualizer(state.spectrum);
    }
    if (state.tone_histogram) {
        lastHistogram = state.tone_histogram;
        drawToneHistogram(state.tone_histogram);
    }
    if (state.session_tones) {
        $('th-ctcss-total').textContent = state.session_tones.ctcss;
        $('th-dcs-total').textContent = state.session_tones.dcs;
    }
}

function fillForm(s) {
    for (const k of ['tone_freq', 'detection_threshold', 'dcs_freq', 'dcs_threshold',
                     'ducking_level', 'announcement_level', 'fade_out_ms', 'fade_in_ms',
                     'tone_timeout_seconds', 'fallback_timeout_seconds', 'fallback_retry_main_seconds']) {
        const el = $(k);
        if (el) el.value = s[k];
    }
    if ($('simulate')) $('simulate').checked = !!s.simulate_tone;
    if ($('tone_timeout_enabled')) $('tone_timeout_enabled').checked = !!s.tone_timeout_enabled;
    if ($('fallback_enabled')) $('fallback_enabled').checked = !!s.fallback_enabled;
}

function updateSourceUI(s) {
    if (!$('source-url')) return;
    const type = s.source_type || 'stream';
    const r = document.querySelector(`input[name="source_type"][value="${type}"]`);
    if (r) r.checked = true;
    $('source-stream').style.display = type === 'device' ? 'none' : '';
    $('source-device').style.display = type === 'device' ? '' : 'none';
    $('source-url').value = s.radio_url || '';
    const sel = $('source-device-select');
    if (sel) sel.value = s.input_device != null ? String(s.input_device) : '';
}

// ==================== Объявления ====================
function scheduleText(it) {
    const a = it.start_time, b = it.end_time, lim = it.repeat_limit || 0;
    const interval = it.min_interval_minutes || 0;
    const parts = [];
    if (a && b) parts.push(`${a}–${b}`);
    else if (a) parts.push(`с ${a}`);
    else parts.push('—');
    if (lim > 0) parts.push(`≤${lim}/день`);
    if (interval > 0) parts.push(`≥${interval}м`);
    return parts.join(' · ');
}

function renderAnnouncements(items, activeId) {
    const tb = $('ann-tbody');
    if (!tb) return;
    if (!items.length) {
        tb.innerHTML = '<tr><td colspan="7" class="empty">Загрузок пока нет</td></tr>';
        return;
    }
    tb.innerHTML = '';
    for (const it of items) {
        const tr = document.createElement('tr');
        if (it.id === activeId) tr.classList.add('active-row');
        const lim = it.repeat_limit > 0 ? `/${it.repeat_limit}` : '';
        const sizeKb = Math.round(it.size / 1024);
        const last = it.last_played ? it.last_played.replace('T', ' ').slice(0, 19) : '—';
        const isPriority = it.id === activeId;
        tr.innerHTML = `
            <td class="icon">${isPriority ? '🎯' : ''}</td>
            <td>
                <div class="fname">${esc(it.original_name)}</div>
                <div class="fdesc">${esc(it.description || '—')}</div>
                <div class="fsize">${sizeKb} КБ · ${it.duration ?? '?'} с</div>
            </td>
            <td class="small">${scheduleText(it)}</td>
            <td class="counter">${it.plays_today ?? 0}${lim}</td>
            <td class="counter">${it.plays_total ?? 0}</td>
            <td class="small">${last}</td>
            <td class="actions">
                <button class="btn-sm btn-activate" data-id="${it.id}"
                        title="${isPriority ? 'Снять приоритет' : 'Сделать приоритетным'}">
                    ${isPriority ? '🎯 Снять' : '🎯 Приоритет'}
                </button>
                <button class="btn-sm btn-edit" data-id="${it.id}">Расписание</button>
                <button class="btn-sm btn-delete" data-id="${it.id}">Удалить</button>
            </td>`;
        tb.appendChild(tr);
    }
    tb.querySelectorAll('.btn-activate').forEach(b => b.onclick = () => togglePriority(b.dataset.id, activeId));
    tb.querySelectorAll('.btn-delete').forEach(b => b.onclick = () => deleteAnn(b.dataset.id));
    tb.querySelectorAll('.btn-edit').forEach(b =>
        b.onclick = () => editSchedule(b.dataset.id, items.find(x => x.id === b.dataset.id))
    );
}

async function togglePriority(id, activeId) {
    const isActive = (id === activeId);
    await fetch('/api/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ active_announcement_id: isActive ? null : id }),
    });
    formInitialized = false;
    refreshAll();
}

async function deleteAnn(id) {
    if (!confirm('Удалить это объявление?')) return;
    await fetch(`/api/announcements/${id}`, { method: 'DELETE' });
    refreshAll();
}

async function editSchedule(id, item) {
    const start = prompt('Действует с (HH:MM):', item.start_time || '');
    if (start === null) return;
    const end = prompt('Действует до (HH:MM, пусто = без ограничения):', item.end_time || '');
    if (end === null) return;
    const lim = prompt('Повторов в день (0 = без лимита):', item.repeat_limit || 0);
    if (lim === null) return;
    const interval = prompt('Мин. интервал между показами, мин (0 = без паузы):', item.min_interval_minutes || 0);
    if (interval === null) return;
    const r = await fetch(`/api/announcements/${id}/schedule`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            start_time: start || null,
            end_time: end || null,
            repeat_limit: parseInt(lim) || 0,
            min_interval_minutes: parseInt(interval) || 0,
        }),
    });
    const data = await r.json();
    if (!data.ok) alert('❌ ' + (data.error || 'ошибка'));
    refreshAll();
}

// ==================== Очередь в эфир ====================
function renderQueue(items, playingId) {
    const box = $('queue-list');
    if (!box) return;
    if (!items.length) {
        box.innerHTML = '<div class="queue-empty">Нет объявлений — загрузите через «📢 Объявления»</div>';
        return;
    }
    const enriched = items.map((it, idx) => {
        let st = it.queue_state || 'blocked';
        if (playingId && it.id === playingId) st = 'playing';
        return { ...it, _idx: idx + 1, _state: st };
    });
    const order = { playing: 0, ready: 1, cooldown: 2, blocked: 3 };
    enriched.sort((a, b) => {
        const d = order[a._state] - order[b._state];
        if (d !== 0) return d;
        return (b.progress || 0) - (a.progress || 0);
    });

    box.innerHTML = '';
    for (const it of enriched) {
        const pct = Math.round((it.progress || 0) * 100);
        const div = document.createElement('div');
        div.className = `queue-item-vert state-${it._state}`;
        div.title = `${it.original_name} · ${it.queue_hint || ''}`;
        div.innerHTML = `
            <div class="queue-num-big">#${it._idx}</div>
            <div class="queue-led-vert">
                <div class="queue-led-fill" style="height:${pct}%"></div>
            </div>
            <div class="queue-hint-vert">${esc(it.queue_hint || '')}</div>
        `;
        box.appendChild(div);
    }
}

// ==================== Пользователи ====================
async function loadUsers() {
    if (!IS_ADMIN) return;
    try {
        const r = await fetch('/api/users');
        if (!r.ok) return;
        const data = await r.json();
        renderUsers(data.items || []);
    } catch (e) { console.error('loadUsers', e); }
}

function renderUsers(items) {
    const tb = $('users-tbody');
    if (!tb) return;
    if (!items.length) {
        tb.innerHTML = '<tr><td colspan="5" class="empty">Пользователей нет</td></tr>';
        return;
    }
    tb.innerHTML = '';
    for (const u of items) {
        const tr = document.createElement('tr');
        tr.innerHTML = `
            <td><b>${esc(u.username)}</b></td>
            <td>${u.role === 'admin' ? '🔑 админ' : '👤 оператор'}</td>
            <td class="small">${esc(u.created_at || '—')}</td>
            <td>${u.must_change_password ? '⚠️ да' : '—'}</td>
            <td class="actions">
                <button class="btn-sm btn-reset-pass" data-user="${esc(u.username)}">Сбросить пароль</button>
                <button class="btn-sm btn-delete" data-user="${esc(u.username)}">Удалить</button>
            </td>`;
        tb.appendChild(tr);
    }
    tb.querySelectorAll('.btn-reset-pass').forEach(b =>
        b.onclick = () => resetUserPassword(b.dataset.user)
    );
    tb.querySelectorAll('.btn-delete').forEach(b =>
        b.onclick = () => deleteUser(b.dataset.user)
    );
}

async function resetUserPassword(username) {
    const np = prompt(`Новый пароль для "${username}" (мин. 4):`);
    if (!np) return;
    const r = await fetch(`/api/users/${encodeURIComponent(username)}/reset-password`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ new_password: np }),
    });
    const data = await r.json();
    if (data.ok) alert('✅ Пароль сброшен. Пользователь сменит при входе.');
    else alert('❌ ' + (data.error || 'ошибка'));
    loadUsers();
}

async function deleteUser(username) {
    if (!confirm(`Удалить пользователя "${username}"?`)) return;
    const r = await fetch(`/api/users/${encodeURIComponent(username)}`, { method: 'DELETE' });
    const data = await r.json();
    if (!data.ok) alert('❌ ' + (data.error || 'ошибка'));
    loadUsers();
}

// ==================== Fallback ====================
async function loadFallbackSources() {
    if (!IS_ADMIN) return;
    try {
        const r = await fetch('/api/fallback/sources');
        if (!r.ok) return;
        const data = await r.json();
        renderFallbackList(data.items || []);
        fallbackInitialized = true;
    } catch (e) { console.error('loadFallbackSources', e); }
}

function renderFallbackList(items) {
    const box = $('fallback-list');
    if (!box) return;
    if (!items.length) {
        box.innerHTML = '<div class="fallback-empty">Резервных каналов нет</div>';
        return;
    }
    box.innerHTML = '';
    items.forEach((it, idx) => {
        const row = document.createElement('div');
        row.className = 'fallback-item';
        row.innerHTML = `
            <div class="fb-name">${esc(it.name || `Резерв ${idx + 1}`)}</div>
            <div class="fb-url" title="${esc(it.url)}">${esc(it.url)}</div>
            <button data-idx="${idx}">Удалить</button>`;
        row.querySelector('button').onclick = async () => {
            const newItems = items.filter((_, i) => i !== idx);
            await saveFallbackSources(newItems);
        };
        box.appendChild(row);
    });
}

async function saveFallbackSources(items) {
    const r = await fetch('/api/fallback/sources', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ items }),
    });
    const data = await r.json();
    if (data.ok) renderFallbackList(data.items || []);
}
// ==================== DTMF ====================
async function loadDtmfLabels() {
    if (!IS_ADMIN) return;
    try {
        const r = await fetch('/api/dtmf/labels');
        if (!r.ok) return;
        const data = await r.json();
        renderDtmfLabels(data.items || []);
    } catch (e) { console.error('loadDtmfLabels', e); }
}

function renderDtmfLabels(items) {
    const box = $('dtmf-labels-list');
    if (!box) return;
    if (!items.length) {
        box.innerHTML = '<div class="fallback-empty">Метки не заданы — детекция DTMF отключена</div>';
        return;
    }
    box.innerHTML = '';
    items.forEach((label, idx) => {
        const row = document.createElement('div');
        row.className = 'fallback-item';
        row.innerHTML = `
            <div class="fb-name" style="font-family:monospace;font-size:1.1rem;letter-spacing:0.15em;">📟 ${esc(label)}</div>
            <div class="fb-url">DTMF-метка, длина ${label.length}</div>
            <div style="display:flex;gap:4px;">
                <button class="btn-test-dtmf" data-label="${esc(label)}" title="Проверить — запустить объявление сейчас">▶</button>
                <button class="btn-del-dtmf" data-idx="${idx}" title="Удалить">✕</button>
            </div>`;
        row.querySelector('.btn-test-dtmf').onclick = async () => {
            const r = await fetch('/api/dtmf/test', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ label }),
            });
            const data = await r.json();
            if (!data.ok) alert('❌ Не удалось запустить (проверьте расписание объявлений)');
        };
        row.querySelector('.btn-del-dtmf').onclick = async () => {
            const newItems = items.filter((_, i) => i !== idx);
            await saveDtmfLabels(newItems);
        };
        box.appendChild(row);
    });
}

async function saveDtmfLabels(items) {
    const r = await fetch('/api/dtmf/labels', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ items }),
    });
    const data = await r.json();
    if (data.ok) renderDtmfLabels(data.items || []);
}
// ==================== Загрузчики устройств ====================
async function loadInputDevices() {
    if (!IS_ADMIN) return;
    try {
        const { items } = await (await fetch('/api/devices')).json();
        const sel = $('source-device-select');
        if (!sel) return;
        const cur = sel.value;
        sel.innerHTML = '<option value="">По умолчанию</option>';
        for (const d of items) {
            const o = document.createElement('option');
            o.value = String(d.index);
            o.textContent = `#${d.index} ${d.name} (${d.channels}ch)${d.is_default ? ' ★' : ''}`;
            sel.appendChild(o);
        }
        if (cur) sel.value = cur;
    } catch (e) { console.error(e); }
}

async function loadHwOutputs() {
    if (!IS_ADMIN) return;
    try {
        const { items } = await (await fetch('/api/outputs')).json();
        const sel = $('hw-output-select');
        if (!sel) return;
        const cur = sel.value;
        sel.innerHTML = '<option value="">Отключён</option>';
        for (const d of items) {
            const o = document.createElement('option');
            o.value = String(d.index);
            o.textContent = `#${d.index} ${d.name} (${d.channels}ch)${d.is_default ? ' ★' : ''}`;
            sel.appendChild(o);
        }
        if (cur) sel.value = cur;
    } catch (e) { console.error(e); }
}

async function loadSinkDevices() {
    try {
        if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) return;
        const devices = await navigator.mediaDevices.enumerateDevices();
        const sel = $('sink-select');
        if (!sel) return;
        const cur = sel.value;
        sel.innerHTML = '<option value="">По умолчанию</option>';
        for (const d of devices) {
            if (d.kind === 'audiooutput') {
                const o = document.createElement('option');
                o.value = d.deviceId;
                o.textContent = d.label || `Устройство ${d.deviceId.slice(0, 6)}`;
                sel.appendChild(o);
            }
        }
        if (cur) sel.value = cur;
    } catch (e) { console.error(e); }
}

// ==================== SIP ====================
async function refreshSipStatus() {
    try {
        const r = await fetch('/api/sip/status');
        if (!r.ok) return;
        const data = await r.json();

        // Лампочка SIP — для всех ролей
        const lamp = $('ind-sip');
        if (lamp) {
            lamp.classList.remove('active-idle', 'active-call');
            if (data.engine_sip_active) lamp.classList.add('active-call');
            else if (data.running) lamp.classList.add('active-idle');
        }

        if (!IS_ADMIN) return;
        if ($('sip-status')) $('sip-status').textContent = data.running ? '✅ подключён' : '⏹ отключён';
        if ($('sip-engine')) $('sip-engine').textContent = data.engine_sip_active ? '📞 активен' : '—';
        if ($('btn-sip-start')) $('btn-sip-start').disabled = data.running;
        if ($('btn-sip-stop')) $('btn-sip-stop').disabled = !data.running;

        if (!sipInitialized && $('sip_domain')) {
            $('sip_domain').value = data.sip_domain || '';
            $('sip_username').value = data.sip_username || '';
            $('sip_port').value = data.sip_port || 5060;
            $('sip_transport').value = data.sip_transport || 'udp';
            $('auto_start_sip').checked = !!data.auto_start_sip;
            if ($('auto_start_engine')) $('auto_start_engine').checked = !!data.auto_start_engine;
            $('sip_password').placeholder = data.sip_password_set
                ? '•••••••• (сохранён)' : 'Введите пароль';
            sipInitialized = true;
        }
    } catch (e) { console.error(e); }
}

function _sipPayload() {
    return {
        sip_domain: $('sip_domain').value.trim(),
        sip_username: $('sip_username').value.trim(),
        sip_password: $('sip_password').value,
        sip_port: parseInt($('sip_port').value) || 5060,
        sip_transport: $('sip_transport').value,
    };
}

// ==================== О программе ====================
async function loadAbout() {
    try {
        const r = await fetch('/api/about');
        if (!r.ok) return;
        const data = await r.json();
        if ($('about-version')) $('about-version').textContent = `v${data.version}`;
        if ($('about-author')) $('about-author').textContent = data.author;
        if ($('about-license')) $('about-license').textContent = data.license;
        if ($('about-github')) {
            $('about-github').href = data.github;
            $('about-github').textContent = data.github;
        }
        if ($('about-full-readme')) $('about-full-readme').href = data.github + '#readme';
        if ($('about-readme-text')) {
            const readme = (data.readme_md || '').trim();
            if (!readme) {
                $('about-readme-text').textContent = 'README.md не найден в папке приложения.';
            } else {
                $('about-readme-text').textContent =
                    readme.length > 8000 ? readme.slice(0, 8000) + '\n\n… (продолжение на GitHub)' : readme;
            }
        }
    } catch (e) { console.error('loadAbout', e); }
}

// ==================== Главный refresh ====================
let _refreshInFlight = false;
let _refreshFailCount = 0;
let _refreshTimer = null;

async function refreshAll() {
    if (_refreshInFlight) return;
    _refreshInFlight = true;
    try {
        const ctrl = new AbortController();
        const t = setTimeout(() => ctrl.abort(), 4000);
        let sr, ar;
        try {
            [sr, ar] = await Promise.all([
                fetch('/api/status', { signal: ctrl.signal }),
                fetch('/api/announcements', { signal: ctrl.signal }),
            ]);
        } finally {
            clearTimeout(t);
        }

        if (sr.status === 401) { location.href = '/login'; return; }
        if (sr.status === 403) { _refreshFailCount = 0; return; }
        if (!sr.ok || !ar.ok) throw new Error('HTTP ' + sr.status);

        const { state, settings } = await sr.json();
        const { items } = await ar.json();

        renderStatus(state, settings);

        if (!formInitialized) {
            fillForm(settings);
            updateSourceUI(settings);
            formInitialized = true;
        }
        if (IS_ADMIN && !fallbackInitialized) {
            loadFallbackSources();
        }
        renderAnnouncements(items, settings.active_announcement_id);
        renderQueue(items, state.playing_id);

        if (IS_ADMIN && $('hw-output-select') && settings.hw_output_device != null
            && $('hw-output-select').value === '') {
            $('hw-output-select').value = String(settings.hw_output_device);
        }
        refreshSipStatus();

        _refreshFailCount = 0;
    } catch (e) {
        _refreshFailCount++;
        if (_refreshFailCount === 3) {
            console.warn('Сервер не отвечает. Проверьте, что python main.py запущен.');
        }
        if (_refreshFailCount === 10) {
            console.warn('Сервер недоступен. Пауза 30 секунд, потом снова попытки.');
            if (_refreshTimer) clearInterval(_refreshTimer);
            _refreshTimer = null;
            setTimeout(() => {
                _refreshFailCount = 0;
                _refreshTimer = setInterval(refreshAll, 2000);
            }, 30000);
        }
    } finally {
        _refreshInFlight = false;
    }
}
// ==================== Веб-плеер (для всех ролей) ====================
if ($('btn-refresh-sinks')) {
    $('btn-refresh-sinks').onclick = loadSinkDevices;
}
if ($('sink-select')) {
    $('sink-select').onchange = async (e) => {
        const id = e.target.value;
        const player = $('player');
        if (!player.setSinkId) {
            $('output-status').textContent = '❌ браузер не поддерживает выбор устройства';
            return;
        }
        try { await player.setSinkId(id || ''); }
        catch (err) { $('output-status').textContent = '❌ ' + err.message + ' (нужен HTTPS)'; }
    };
}
if ($('btn-stream-connect')) {
    $('btn-stream-connect').onclick = async () => {
        const player = $('player');
        const btn = $('btn-stream-connect');
        if (streamConnected) {
            player.pause();
            player.removeAttribute('src');
            player.load();
            streamConnected = false;
            btn.textContent = '▶ Подключить';
        } else {
            player.src = '/stream';
            try {
                await player.play();
                streamConnected = true;
                btn.textContent = '⏸ Отключить';
            } catch (e) {
                $('output-status').textContent = '❌ ' + e.message;
            }
        }
    };
}

// ==================== Обработчики (только admin) ====================
if (IS_ADMIN) {
    document.querySelectorAll('input[name="source_type"]').forEach(r => {
        r.addEventListener('change', () => {
            const isDev = r.value === 'device' && r.checked;
            $('source-stream').style.display = isDev ? 'none' : '';
            $('source-device').style.display = isDev ? '' : 'none';
        });
    });
    $('btn-refresh-devices').onclick = loadInputDevices;

    $('btn-apply-source').onclick = async () => {
        const st = $('source-status');
        const type = document.querySelector('input[name="source_type"]:checked').value;
        const url = $('source-url').value.trim();
        const dv = $('source-device-select').value;
        if (type === 'stream' && !url) { st.textContent = '❌ Укажите URL'; return; }
        st.textContent = '⏳ Применение...';
        try {
            const r = await fetch('/api/settings', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    source_type: type,
                    radio_url: url,
                    input_device: dv === '' ? null : parseInt(dv)
                }),
            });
            const data = await r.json();
            st.textContent = data.restarting ? '✅ Применено, перезапуск...' : '✅ Применено';
            formInitialized = false;
            refreshAll();
        } catch (e) { st.textContent = '❌ ' + e.message; }
        setTimeout(() => st.textContent = '', 4000);
    };

    // Аппаратный выход (только admin)
    $('btn-refresh-hw-outputs').onclick = loadHwOutputs;
    $('btn-apply-hw-output').onclick = async () => {
        const status = $('output-status');
        const val = $('hw-output-select').value;
        status.textContent = '⏳ Применение...';
        try {
            const r = await fetch('/api/settings/output', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ hw_output_device: val === '' ? null : parseInt(val) }),
            });
            const data = await r.json();
            status.textContent = data.ok ? '✅ Применено' : '❌ ошибка';
        } catch (e) { status.textContent = '❌ ' + e.message; }
        setTimeout(() => status.textContent = '', 4000);
    };

    $('btn-start').onclick = async () => { await fetch('/api/start', { method: 'POST' }); refreshAll(); };
    $('btn-stop').onclick = async () => { await fetch('/api/stop', { method: 'POST' }); refreshAll(); };

    $('btn-scan-tone').onclick = async () => {
        const status = $('scan-status');
        const resultsBox = $('scan-results');
        status.textContent = '⏳ Сканирование...';
        resultsBox.innerHTML = '';
        try {
            const r = await fetch('/api/scan_tone', { method: 'POST' });
            const data = await r.json();
            if (!data.ok) { status.textContent = '❌ ' + (data.error || 'ошибка'); return; }
            status.textContent = `✅ Проанализировано ${data.buffer_seconds} с`;
            renderScanResults(data.candidates);
        } catch (err) { status.textContent = '❌ ' + err.message; }
    };

    // ==================== Fallback ====================
    $('btn-add-fallback').onclick = async () => {
        const name = $('new-fallback-name').value.trim();
        const url = $('new-fallback-url').value.trim();
        if (!url) { alert('Введите URL резервного потока'); return; }
        const r = await fetch('/api/fallback/sources');
        const data = await r.json();
        const items = data.items || [];
        items.push({ name: name || `Резерв ${items.length + 1}`, url });
        await saveFallbackSources(items);
        $('new-fallback-name').value = '';
        $('new-fallback-url').value = '';
    };

    // ==================== DTMF-метки ====================
    if ($('btn-add-dtmf-label')) {
        $('btn-add-dtmf-label').onclick = async () => {
            const inp = $('new-dtmf-label');
            const label = (inp.value || '').trim().toUpperCase();
            if (!label) { alert('Введите метку'); return; }
            if (label.length < 2 || label.length > 10) {
                alert('Метка должна быть 2–10 символов');
                return;
            }
            if (!/^[0-9*#ABCD]+$/.test(label)) {
                alert('Разрешены только цифры 0-9, а также *, #, A, B, C, D');
                return;
            }
            const r = await fetch('/api/dtmf/labels');
            const data = await r.json();
            const items = data.items || [];
            if (items.includes(label)) {
                alert(`Метка "${label}" уже есть`);
                return;
            }
            items.push(label);
            await saveDtmfLabels(items);
            inp.value = '';
        };
        // Enter в поле ввода — тоже добавить
        if ($('new-dtmf-label')) {
            $('new-dtmf-label').addEventListener('keydown', (e) => {
                if (e.key === 'Enter') {
                    e.preventDefault();
                    $('btn-add-dtmf-label').click();
                }
            });
        }
    }

    // ==================== SIP ====================
    $('btn-sip-save').onclick = async () => {
        const msg = $('sip-message');
        msg.textContent = '⏳ Сохранение...';
        try {
            const r = await fetch('/api/sip/save', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(_sipPayload()),
            });
            const data = await r.json();
            msg.textContent = data.ok ? '✅ Сохранено' : '❌ ' + (data.error || 'ошибка');
            if (data.ok) {
                $('sip_password').value = '';
                sipInitialized = false;
                refreshSipStatus();
            }
        } catch (e) { msg.textContent = '❌ ' + e.message; }
        setTimeout(() => msg.textContent = '', 4000);
    };

    $('btn-sip-start').onclick = async () => {
        const msg = $('sip-message');
        msg.textContent = '⏳ Подключение...';
        try {
            const r = await fetch('/api/sip/start', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(_sipPayload()),
            });
            const data = await r.json();
            msg.textContent = data.ok ? '✅ SIP подключён' : '❌ ' + (data.error || 'ошибка');
            $('sip_password').value = '';
            sipInitialized = false;
            refreshSipStatus();
        } catch (e) { msg.textContent = '❌ ' + e.message; }
        setTimeout(() => msg.textContent = '', 5000);
    };

    $('btn-sip-stop').onclick = async () => {
        await fetch('/api/sip/stop', { method: 'POST' });
        refreshSipStatus();
    };

    $('auto_start_sip').onchange = async e => {
        await fetch('/api/autostart', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                auto_start_sip: e.target.checked,
                auto_start_engine: $('auto_start_engine').checked,
            }),
        });
    };
    if ($('auto_start_engine')) {
        $('auto_start_engine').onchange = async e => {
            await fetch('/api/autostart', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    auto_start_engine: e.target.checked,
                    auto_start_sip: $('auto_start_sip').checked,
                }),
            });
        };
    }

    // ==================== Fallback enabled toggle ====================
    if ($('fallback_enabled')) {
        $('fallback_enabled').onchange = async e => {
            await fetch('/api/settings', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ fallback_enabled: e.target.checked }),
            });
            refreshAll();
        };
    }

    // ==================== Форма настроек ====================
    if ($('settings-form')) {
        $('settings-form').onsubmit = async e => {
            e.preventDefault();
            const data = {};
            for (const el of e.target.elements) {
                if (!el.name) continue;
                if (el.type === 'checkbox') data[el.name] = el.checked;
                else if (el.type === 'number') data[el.name] = parseFloat(el.value);
                else data[el.name] = el.value;
            }
            if ($('fallback_enabled')) data.fallback_enabled = $('fallback_enabled').checked;
            if ($('fallback_timeout_seconds')) data.fallback_timeout_seconds = parseFloat($('fallback_timeout_seconds').value) || 10;
            if ($('fallback_retry_main_seconds')) data.fallback_retry_main_seconds = parseFloat($('fallback_retry_main_seconds').value) || 60;

            await fetch('/api/settings', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(data),
            });
            formInitialized = false;
            refreshAll();
        };
    }

    if ($('simulate')) {
        $('simulate').onchange = async e => {
            await fetch('/api/settings', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ simulate_tone: e.target.checked }),
            });
            refreshAll();
        };
    }

    // ==================== Пользователи ====================
    if ($('btn-create-user')) {
        $('btn-create-user').onclick = async () => {
            const status = $('user-status');
            const username = $('new_user_name').value.trim();
            const password = $('new_user_pass').value;
            const role = $('new_user_role').value;
            const mustChange = $('new_user_must_change').checked;
            if (!username || !password) { status.textContent = '❌ Заполните логин и пароль'; return; }
            status.textContent = '⏳ Создание...';
            try {
                const r = await fetch('/api/users', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ username, password, role, must_change_password: mustChange }),
                });
                const data = await r.json();
                status.textContent = data.ok ? '✅ Создан' : '❌ ' + (data.error || 'ошибка');
                if (data.ok) {
                    $('new_user_name').value = '';
                    $('new_user_pass').value = '';
                    loadUsers();
                }
            } catch (e) { status.textContent = '❌ ' + e.message; }
            setTimeout(() => status.textContent = '', 4000);
        };
    }
}
// ==================== Сканер (общий для admin) ====================
function renderScanResults(candidates) {
    const box = $('scan-results');
    if (!box) return;
    if (!candidates || !candidates.length) {
        box.innerHTML = '<div class="empty">Нет кандидатов</div>';
        return;
    }
    let html = '<table class="scan-table"><thead><tr>' +
        '<th>Частота</th><th>Мощность</th><th>Оценка</th><th></th></tr></thead><tbody>';
    for (const c of candidates) {
        const power = c.power;
        let rating, cls;
        if (power >= 0.05) { rating = '🟢 уверенно'; cls = 'good'; }
        else if (power >= 0.01) { rating = '🟡 вероятно'; cls = 'mid'; }
        else { rating = '⚪ слабо'; cls = 'weak'; }
        html += `<tr class="${cls}">
            <td><b>${c.freq.toFixed(1)} Гц</b></td>
            <td class="mono">${power.toFixed(6)}</td>
            <td>${rating}</td>
            <td><button class="btn-sm btn-apply-tone" data-freq="${c.freq}">Применить</button></td>
        </tr>`;
    }
    html += '</tbody></table>';
    box.innerHTML = html;
    box.querySelectorAll('.btn-apply-tone').forEach(b => {
        b.onclick = async () => {
            const freq = parseFloat(b.dataset.freq);
            const r = await fetch('/api/apply_tone', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ freq }),
            });
            if (r.ok) {
                $('tone_freq').value = freq;
                $('scan-status').textContent = `✅ Применено: CTCSS ${freq} Гц`;
                formInitialized = false;
                refreshAll();
            }
        };
    });
}

// ==================== Загрузка объявления (для всех ролей) ====================
if ($('upload-form')) {
    $('upload-form').onsubmit = async e => {
        e.preventDefault();
        const status = $('upload-status');
        const fd = new FormData(e.target);
        status.textContent = '⏳ Загрузка...';
        try {
            const r = await fetch('/api/announcements', { method: 'POST', body: fd });
            const data = await r.json();
            status.textContent = data.ok ? '✅ Загружено' : '❌ ' + (data.error || 'ошибка');
            if (data.ok) {
                e.target.reset();
                refreshAll();
            }
        } catch (err) { status.textContent = '❌ ' + err.message; }
        setTimeout(() => status.textContent = '', 5000);
    };
}

// ==================== Старт ====================
loadSinkDevices();
if (IS_ADMIN) {
    loadInputDevices();
    loadHwOutputs();
    loadFallbackSources();
    loadUsers();
    loadDtmfLabels();
    refreshSipStatus();
}
loadAbout();
_refreshTimer = setInterval(refreshAll, 1000);
refreshAll();