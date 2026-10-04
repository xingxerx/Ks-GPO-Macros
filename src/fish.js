let CURRENT_VERSION = '0.0.0';
const GITHUB_REPO = 'K3nD4rk-Code-Developer/Grand-Piece-Online-Fishing';

let BackendPort = 0;
let pollTimer = null;
let pollingStarted = false;
let lastFullSync = 0;
let activeCategoryIndex = 0;
let activeSlideIndex = 0;
let activeElement = null;
let skipNextUpdate = new Set();

let SLIDESHOW_DATA = [];

const invoke = window.__TAURI__?.core?.invoke ?? (async (cmd, args) => {
    if (cmd === 'get_backend_port') return window.__BACKEND_PORT__ || 0;
    return null;
});

function extractVersion(v) {
    const m = v.match(/(\d+)\.(\d+)\.(\d+)/);
    return m ? m[0] : v;
}

function compareVersions(v1, v2) {
    const a = extractVersion(v1).split('.').map(Number);
    const b = extractVersion(v2).split('.').map(Number);
    for (let i = 0; i < Math.max(a.length, b.length); i++) {
        if ((a[i] || 0) > (b[i] || 0)) return 1;
        if ((a[i] || 0) < (b[i] || 0)) return -1;
    }
    return 0;
}

const tauriInvoke = window.__TAURI__?.core?.invoke;

function flog(msg) {
    if (tauriInvoke) {
        tauriInvoke('frontend_log', { message: `[${new Date().toISOString()}] ${msg}` });
    }
    console.log(msg);
}

function BackendUrl(path) {
    return `http://127.0.0.1:${BackendPort}${path}`;
}

function ApplyBackendPort(port) {
    if (!port || port <= 0) return;
    if (BackendPort === port) return;
    BackendPort = port;
    flog(`BackendPort updated to ${BackendPort}`);
    lastFullSync = 0;
}

if (window.__TAURI__?.event?.listen) {
    window.__TAURI__.event.listen('backend-port-ready', (event) => {
        const port = event?.payload?.port;
        flog(`backend-port-ready event received: port=${port}`);
        ApplyBackendPort(port);
    });
}

async function InitBackendPort() {
    try {
        const port = await invoke('get_backend_port');
        flog(`get_backend_port returned: ${port}`);
        if (port && port > 0) {
            ApplyBackendPort(port);
            return;
        }
    } catch (e) {
        flog(`get_backend_port failed: ${e}`);
    }

    if (window.__BACKEND_PORT__ && window.__BACKEND_PORT__ > 0) {
        flog(`BackendPort from __BACKEND_PORT__ global: ${window.__BACKEND_PORT__}`);
        ApplyBackendPort(window.__BACKEND_PORT__);
        return;
    }

    flog('WARNING: Could not resolve backend port at startup — waiting for event');
}

async function sendToPython(action, payload) {
    try {
        const res = await fetch(BackendUrl('/command'), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ action, payload })
        });
        const result = await res.json();
        if (result.status === 'error') showErrorNotification(`Error: ${result.message}`);
        // Pull settings on the next tick so the change (and anything it affected) shows right away
        lastFullSync = 0;
        return result;
    } catch (e) {
        console.error('Failed to send to Python:', e);
    }
}

function showToast(msg, type = 'warn') {
    const el = document.createElement('div');
    el.className = `notif-toast ${type}`;
    el.textContent = msg;
    document.body.appendChild(el);
    setTimeout(() => el.remove(), 5000);
}

function showWarningNotification(msg) { showToast(msg, 'warn'); }
function showErrorNotification(msg) { showToast(msg, 'err'); }

function setInputValue(id, value) {
    const el = document.getElementById(id);
    if (!el || document.activeElement === el) return;
    el.value = value;
}

function setToggleState(id, isActive) {
    const el = document.getElementById(id);
    if (!el || activeElement === el || skipNextUpdate.has(id)) return;
    el.classList.toggle('active', isActive);
}

function setExpandableSection(toggleId, expandId, isActive) {
    const toggle = document.getElementById(toggleId);
    const section = document.getElementById(expandId);
    if (!toggle || !section) return;
    if (activeElement === toggle || skipNextUpdate.has(toggleId)) return;
    toggle.classList.toggle('active', isActive);
    section.classList.toggle('expanded', isActive);
}

function updateStatus(isRunning) {
    document.getElementById('statusDot').classList.toggle('active', isRunning);
    document.getElementById('statusText').textContent = isRunning ? 'Active' : 'Inactive';
    const btn = document.getElementById('macroToggleBtn');
    if (btn) {
        btn.textContent = isRunning ? 'Stop' : 'Start';
        btn.classList.toggle('running', isRunning);
    }
}

async function toggleMacro() {
    await sendToPython('toggle_macro', '');
}

async function resetStats() {
    if (!confirm('Reset fish, devil fruit and time counters for this session?')) return;
    await sendToPython('reset_stats', '');
    lastRenderedFruitKey = null;
}

const RARITY_LABELS = { Common: 'Common', Rare: 'Rare', Epic: 'Epic', Legendary: 'Legendary', Mythical: 'Mythical', Unknown: 'Other' };
let lastRenderedFruitKey = null;

function escapeHtml(text) {
    return String(text).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// Session numbers on the Dashboard: counts, the per-rarity split and the latest fruit drops
function updateDashboard(state) {
    const set = (id, value) => { const el = document.getElementById(id); if (el) el.textContent = value; };
    set('dashFish', state.fishCaught || 0);
    set('dashFruits', state.devilFruitsCaught || 0);
    set('dashRate', (state.fishPerHour || 0).toFixed(1));
    set('dashTime', state.timeElapsed || '0:00:00');

    const history = state.fruitHistory || [];
    const key = JSON.stringify([state.devilFruitsByRarity, history]);
    if (key === lastRenderedFruitKey) return;
    lastRenderedFruitKey = key;

    const rarityEl = document.getElementById('dashRarity');
    if (rarityEl) {
        const counts = state.devilFruitsByRarity || {};
        rarityEl.innerHTML = Object.keys(RARITY_LABELS)
            .filter(r => counts[r] || ['Common', 'Rare', 'Epic', 'Legendary', 'Mythical'].includes(r))
            .map(r => `<span class="rarity-chip rarity-${r}">${RARITY_LABELS[r]}<b>${counts[r] || 0}</b></span>`)
            .join('');
    }

    const historyEl = document.getElementById('dashFruitHistory');
    if (historyEl) {
        if (!history.length) {
            historyEl.innerHTML = '<div class="empty-msg">No devil fruits yet this session</div>';
            return;
        }
        historyEl.innerHTML = history.slice().reverse().map(f => {
            const name = f.name === 'Unknown' ? 'Devil fruit' : escapeHtml(f.name);
            const pity = f.pity != null ? ` · pity ${f.pity}/40` : '';
            return `<div class="fruit-row">
              <span class="fruit-time">${escapeHtml(f.time)}</span>
              <span class="rarity-${escapeHtml(f.rarity)}">${name} <span class="fruit-meta">(${RARITY_LABELS[f.rarity] || escapeHtml(f.rarity)})</span></span>
              <span class="fruit-meta">catch #${f.catch}${pity}</span>
            </div>`;
        }).join('');
    }
}

// Live view of the macro loop, so the window shows the step it is on rather than just on/off
function updateMacroActivity(state) {
    const statusEl = document.getElementById('macroStatusText');
    if (statusEl) {
        const status = state.currentStatus || 'Idle';
        statusEl.textContent = status;
        statusEl.title = status;
    }
    const fishEl = document.getElementById('macroFishText');
    if (fishEl) {
        const rate = state.isRunning && state.fishPerHour ? ` · ${state.fishPerHour}/hr` : '';
        fishEl.textContent = `${state.fishCaught || 0} fish · ${state.devilFruitsCaught || 0} fruit${rate}`;
    }
}

function updateFruitDetectStatus(state) {
    const el = document.getElementById('fruitDetectStatus');
    if (!el || state.ocrStatus === undefined) return;
    const labels = { ready: 'Ready', loading: 'Loading OCR…', off: 'Off (fast mode or OCR failed)' };
    let text = labels[state.ocrStatus] || state.ocrStatus;
    if (state.ocrStatus === 'ready' && state.fruitPendingStore) text = 'Fruit caught — storing next cast';
    el.textContent = text;
    el.className = 'point-badge ' + (state.ocrStatus === 'ready' ? 'set' : 'unset');
}

function updateHotkey(key, value) {
    const el = document.getElementById(`hotkey-${key}`);
    if (el) el.textContent = value.toUpperCase();
}

function updatePointStatus(name, x, y) {
    const el = document.getElementById(`${name}Status`);
    if (!el) return;
    if (x != null && y != null) {
        el.textContent = `${x}, ${y}`;
        el.className = 'point-badge set';
    } else {
        el.textContent = 'Not Set';
        el.className = 'point-badge unset';
    }
}

function navigateToLocations() {
    document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
    document.querySelector('[data-view="locations"]').classList.add('active');
    document.getElementById('locations').classList.add('active');
}

function checkRequirements(name) {
    const warn = document.getElementById(`${name}Warning`);
    const toggle = document.getElementById(`${name}Toggle`);
    if (!warn) return;
    const on = toggle && toggle.classList.contains('active');
    let missing = false;
    if (name === 'autoStoreFruit') {
        if (document.getElementById('storeFruitPointStatus')?.classList.contains('unset')) missing = true;
    }
    if (name === 'autoBuyBait') {
        ['leftPointStatus', 'middlePointStatus', 'rightPointStatus'].forEach(id => {
            if (document.getElementById(id)?.classList.contains('unset')) missing = true;
        });
    }
    if (name === 'autoSelectBait') {
        // Smart mode finds bait by OCR, so the Top Bait Point is only an optional fallback
        const smart = document.getElementById('smartBaitSelectToggle')?.classList.contains('active');
        if (!smart && document.getElementById('baitPointStatus')?.classList.contains('unset')) missing = true;
    }
    warn.classList.toggle('hidden', !(on && missing));
}

function changeTheme(name) {
    document.documentElement.setAttribute('data-theme', name === 'default' ? '' : name);
    document.querySelectorAll('.theme-opt').forEach(o => o.classList.toggle('active', o.dataset.theme === name));
    localStorage.setItem('selectedTheme', name);
}

function loadSavedTheme() {
    changeTheme(localStorage.getItem('selectedTheme') || 'default');
}

async function toggleFastMode(enabled) {
    document.documentElement.setAttribute('data-perf', enabled ? 'fast' : 'normal');
    localStorage.setItem('fastMode', enabled ? 'true' : 'false');
    try {
        await fetch(BackendUrl('/set_fast_mode'), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enabled })
        });
    } catch (e) { }
}

function loadFastMode() {
    const saved = localStorage.getItem('fastMode') === 'true';
    const toggle = document.getElementById('fastModeToggle');
    if (toggle) toggle.classList.toggle('active', saved);
    if (saved) document.documentElement.setAttribute('data-perf', 'fast');
}

function checkDisclaimer() {
    if (localStorage.getItem('hideDisclaimer') === 'true') {
        document.getElementById('disclaimerModal').classList.add('hidden');
    }
}

function closeDisclaimer() {
    if (document.getElementById('dontShowAgain').checked) {
        localStorage.setItem('hideDisclaimer', 'true');
    }
    document.getElementById('disclaimerModal').classList.add('hidden');
}

let lastRenderedBackpackKey = null;

function renderBackpackLocationRows(slots, locations) {
    const container = document.getElementById('backpackLocationsContainer');
    const hint = document.getElementById('backpackLocationsHint');
    if (!container) return;

    const storeToBackpackEnabled = document.getElementById('storeToBackpackToggle')?.classList.contains('active');

    const key = JSON.stringify([storeToBackpackEnabled, slots, locations]);
    if (key === lastRenderedBackpackKey) return;
    lastRenderedBackpackKey = key;

    container.innerHTML = '';

    if (!storeToBackpackEnabled) {
        if (hint) hint.style.display = 'none';
        return;
    }

    if (hint) hint.style.display = '';

    if (!slots || !slots.length) return;
    slots.forEach((slot, i) => {
        const loc = locations && locations[i];
        const row = document.createElement('div');
        row.className = 'row-item sub';
        row.innerHTML = `
          <span class="row-label">Slot ${slot} Backpack Location</span>
          <div class="row-ctrl">
            <span class="point-badge ${loc ? 'set' : 'unset'}" id="backpackLoc${i}Status">
              ${loc ? `${loc.x}, ${loc.y}` : 'Not Set'}
            </span>
            <button class="btn-sm" onclick="setBackpackLocationPoint(${i})">Set</button>
          </div>`;
        container.appendChild(row);
    });
}

async function setBackpackLocationPoint(slotIndex) {
    await sendToPython('set_backpack_location_point', JSON.stringify({ slotIndex }));
}

async function checkForUpdates() {
    try {
        const res = await fetch(`https://api.github.com/repos/${GITHUB_REPO}/releases/latest`);
        if (!res.ok) return;
        const data = await res.json();
        const latest = extractVersion(data.tag_name);
        if (compareVersions(latest, CURRENT_VERSION) > 0) {
            document.getElementById('newVersionText').textContent = `v${latest}`;
            document.getElementById('updateBanner').classList.remove('hidden');
            window.latestReleaseUrl = data.html_url;
        }
    } catch (e) { console.error('Update check failed:', e); }
}

async function downloadUpdate() {
    const url = window.latestReleaseUrl || `https://github.com/${GITHUB_REPO}/releases/latest`;
    try {
        await fetch(BackendUrl('/command'), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ action: 'open_browser', payload: url })
        });
    } catch (e) { window.location.href = url; }
}

function dismissBanner() {
    document.getElementById('updateBanner').classList.add('hidden');
}

async function loadAndBuildSlideshow() {
    const url = `https://raw.githubusercontent.com/${GITHUB_REPO}/main/src-tauri/visual-guide.json`;
    const res = await fetch(url, { cache: 'no-cache' });
    SLIDESHOW_DATA = await res.json();
    buildSlideshow();
}

function buildSlideshow() {
    const tabsEl = document.getElementById('slideshowTabs');
    const viewEl = document.getElementById('slideshowViewport');

    tabsEl.innerHTML = '';
    SLIDESHOW_DATA.forEach((cat, ci) => {
        const btn = document.createElement('button');
        btn.className = 'ss-tab' + (ci === 0 ? ' active' : '');
        btn.textContent = cat.label;
        btn.onclick = () => switchCategory(ci);
        tabsEl.appendChild(btn);
    });

    const prev = viewEl.querySelector('.slide-arrow.prev');
    const next = viewEl.querySelector('.slide-arrow.next');
    viewEl.querySelectorAll('.slide').forEach(s => s.remove());

    SLIDESHOW_DATA.forEach((cat, ci) => {
        cat.slides.forEach((slide, si) => {
            const div = document.createElement('div');
            div.className = 'slide';
            div.id = `slide-${ci}-${si}`;
            if (ci === 0 && si === 0) div.classList.add('visible');
            div.innerHTML = `<img src="${slide.url}" alt="${slide.title || ''}">
            <div class="slide-overlay">
              ${slide.title ? `<div class="slide-overlay-title">${slide.title}</div>` : ''}
              ${slide.desc ? `<div class="slide-overlay-desc">${slide.desc}</div>` : ''}
            </div>`;
            viewEl.insertBefore(div, prev);
        });
    });

    rebuildDots();
    prev.onclick = () => goToSlide(activeSlideIndex - 1);
    next.onclick = () => goToSlide(activeSlideIndex + 1);
}

function rebuildDots() {
    const dotsEl = document.getElementById('slideDots');
    dotsEl.innerHTML = '';
    const count = SLIDESHOW_DATA[activeCategoryIndex].slides.length;
    for (let i = 0; i < count; i++) {
        const dot = document.createElement('div');
        dot.className = 'slide-dot' + (i === activeSlideIndex ? ' active' : '');
        dot.onclick = () => goToSlide(i);
        dotsEl.appendChild(dot);
    }
}

function switchCategory(ci) {
    document.querySelectorAll('.ss-tab').forEach((t, i) => t.classList.toggle('active', i === ci));
    activeCategoryIndex = ci;
    activeSlideIndex = 0;
    showCurrentSlide();
    rebuildDots();
}

function goToSlide(idx) {
    const len = SLIDESHOW_DATA[activeCategoryIndex].slides.length;
    if (idx < 0) idx = len - 1;
    if (idx >= len) idx = 0;
    activeSlideIndex = idx;
    showCurrentSlide();
    rebuildDots();
}

function showCurrentSlide() {
    SLIDESHOW_DATA.forEach((cat, ci) =>
        cat.slides.forEach((_, si) => {
            const el = document.getElementById(`slide-${ci}-${si}`);
            if (el) el.classList.remove('visible');
        })
    );
    const target = document.getElementById(`slide-${activeCategoryIndex}-${activeSlideIndex}`);
    if (target) target.classList.add('visible');
}

let lastRenderedSlotsKey = null;

function renderDevilFruitSlotSelector(selectedSlots) {
    const container = document.getElementById('devilFruitSlotSelector');
    if (!container) { setTimeout(() => renderDevilFruitSlotSelector(selectedSlots), 100); return; }
    if (!Array.isArray(selectedSlots)) selectedSlots = ['3'];
    // Polling calls this every tick; rebuilding unchanged buttons could swallow a click mid-rebuild
    const key = selectedSlots.join(',');
    if (key === lastRenderedSlotsKey && container.children.length) return;
    lastRenderedSlotsKey = key;
    container.innerHTML = '';
    for (let i = 0; i <= 9; i++) {
        const btn = document.createElement('button');
        btn.className = 'slot-btn' + (selectedSlots.includes(i.toString()) ? ' slot-selected' : '');
        btn.textContent = i;
        btn.onclick = () => toggleDevilFruitSlot(i.toString());
        container.appendChild(btn);
    }
}

function toggleDevilFruitSlot(slot) {
    const rodSlot = document.getElementById('rodHotkey').value;
    const secSlot = document.getElementById('anythingElseHotkey').value;
    if (slot === rodSlot) { showToast(`Slot ${slot} is used by the Fishing Rod!`, 'warn'); return; }
    if (slot === secSlot) { showToast(`Slot ${slot} is used by the Secondary Item!`, 'warn'); return; }
    let slots = window.currentDevilFruitSlots || ['3'];
    slots = slots.includes(slot) ? slots.filter(s => s !== slot) : [...slots, slot];
    if (slots.length === 0) slots = ['3'];
    window.currentDevilFruitSlots = slots;
    renderDevilFruitSlotSelector(slots);
    sendToPython('set_devil_fruit_hotkeys', slots.join(','));
    renderBackpackLocationRows(slots, window.currentBackpackLocations || []);
}

function validateAndSetRodSlot(value) {
    const sec = document.getElementById('anythingElseHotkey').value;
    const df = window.currentDevilFruitSlots || ['3'];
    if (value === sec || df.includes(value)) {
        showToast(`Slot ${value} is already in use!`, 'warn');
        document.getElementById('rodHotkey').value = window.lastValidRodSlot || '1';
        return;
    }
    window.lastValidRodSlot = value;
    sendToPython('set_rod_hotkey', value);
}

function validateAndSetSecondarySlot(value) {
    const rod = document.getElementById('rodHotkey').value;
    const df = window.currentDevilFruitSlots || ['3'];
    if (value === rod || df.includes(value)) {
        showToast(`Slot ${value} is already in use!`, 'warn');
        document.getElementById('anythingElseHotkey').value = window.lastValidSecondarySlot || '2';
        return;
    }
    window.lastValidSecondarySlot = value;
    sendToPython('set_anything_else_hotkey', value);
}

function toggleSetting(settingName) {
    const toggle = document.getElementById(`${settingName}Toggle`);
    activeElement = toggle;
    skipNextUpdate.add(`${settingName}Toggle`);
    toggle.classList.toggle('active');
    const isActive = toggle.classList.contains('active');
    sendToPython(`toggle_${settingName.replace(/([A-Z])/g, '_$1').toLowerCase()}`, isActive.toString());

    if (settingName === 'storeToBackpack') {
        renderBackpackLocationRows(window.currentDevilFruitSlots || [], window.currentBackpackLocations || []);
    }

    setTimeout(() => { activeElement = null; skipNextUpdate.delete(`${settingName}Toggle`); }, 1000);
}

function toggleExpandable(settingName, expandId) {
    const toggle = document.getElementById(`${settingName}Toggle`);
    const section = document.getElementById(expandId);
    activeElement = toggle;
    skipNextUpdate.add(`${settingName}Toggle`);
    toggle.classList.toggle('active');
    const isActive = toggle.classList.contains('active');
    section.classList.toggle('expanded', isActive);
    sendToPython(`toggle_${settingName.replace(/([A-Z])/g, '_$1').toLowerCase()}`, isActive.toString());
    setTimeout(() => { activeElement = null; skipNextUpdate.delete(`${settingName}Toggle`); }, 1000);
}

function toggleLoggingExpandable(toggleId, expandId) {
    const toggle = document.getElementById(toggleId);
    const section = document.getElementById(expandId);
    activeElement = toggle;
    skipNextUpdate.add(toggleId);
    toggle.classList.toggle('active');
    const isActive = toggle.classList.contains('active');
    section.classList.toggle('expanded', isActive);
    const nameMap = {
        'logDevilFruitToggle': 'log_devil_fruit',
        'logRecastTimeoutsToggle': 'log_recast_timeouts',
        'logPeriodicStatsToggle': 'log_periodic_stats',
        'logGeneralUpdatesToggle': 'log_general_updates',
        'logMacroStateToggle': 'log_macro_state',
        'logErrorsToggle': 'log_errors'
    };
    const name = nameMap[toggleId];
    if (name) sendToPython(`toggle_${name}`, isActive.toString());
    setTimeout(() => { activeElement = null; skipNextUpdate.delete(toggleId); }, 1000);
}

async function checkAndToggleMegalodon() {
    const toggle = document.getElementById('megalodonSoundToggle');
    const warning = document.getElementById('megalodonAudioWarning');
    if (toggle.classList.contains('active')) {
        activeElement = toggle;
        skipNextUpdate.add('megalodonSoundToggle');
        toggle.classList.remove('active');
        warning.classList.add('hidden');
        sendToPython('toggle_megalodon_sound', 'false');
        setTimeout(() => { activeElement = null; skipNextUpdate.delete('megalodonSoundToggle'); }, 1000);
        return;
    }
    try {
        const res = await fetch(BackendUrl('/check_audio_device'));
        const result = await res.json();
        if (!result.found) { warning.classList.remove('hidden'); return; }
        warning.classList.add('hidden');
        activeElement = toggle;
        skipNextUpdate.add('megalodonSoundToggle');
        toggle.classList.add('active');
        sendToPython('toggle_megalodon_sound', 'true');
        setTimeout(() => { activeElement = null; skipNextUpdate.delete('megalodonSoundToggle'); }, 1000);
    } catch (e) {
        warning.classList.remove('hidden');
        showErrorNotification('Could not check audio device: ' + e.message);
    }
}

async function loadAudioDevices() {
    try {
        const res = await fetch(BackendUrl('/get_audio_devices'));
        const data = await res.json();
        const sel = document.getElementById('audioDeviceSelect');
        if (!sel) return;
        while (sel.options.length > 2) sel.remove(2);
        if (data.devices?.length) {
            data.devices.forEach(d => {
                const opt = document.createElement('option');
                opt.value = d.index;
                opt.textContent = `${d.name} (${d.sampleRate}Hz)`;
                opt.dataset.deviceName = d.name;
                sel.appendChild(opt);
            });
            document.getElementById('megalodonAudioWarning')?.classList.add('hidden');
        } else {
            document.getElementById('megalodonAudioWarning')?.classList.remove('hidden');
        }
    } catch (e) { console.error('Failed to load audio devices:', e); }
}

async function refreshAudioDevices() {
    await loadAudioDevices();
}

async function selectAudioDevice(value) {
    const sel = document.getElementById('audioDeviceSelect');
    const opt = sel.options[sel.selectedIndex];
    try {
        await fetch(BackendUrl('/command'), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                action: 'set_audio_device',
                payload: JSON.stringify({ index: value !== 'auto' ? parseInt(value) : null, name: opt.dataset.deviceName || '' })
            })
        });
    } catch (e) { showErrorNotification('Failed to set audio device'); }
}

async function testWebhook() {
    const url = document.getElementById('webhookUrl').value;
    if (!url?.trim()) { showErrorNotification('Enter a Webhook URL first.'); return; }
    try {
        const res = await fetch(BackendUrl('/command'), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ action: 'test_webhook', payload: '' })
        });
        const result = await res.json();
        if (result.status !== 'success') showErrorNotification(`Webhook test failed: ${result.message}`);
    } catch (e) { showErrorNotification('Failed to send test webhook.'); }
}

function updateSmartBait(state) {
    if (state.smartBaitSelect !== undefined) setToggleState('smartBaitSelectToggle', state.smartBaitSelect);
    if (state.baitTierOrder) setInputValue('baitTierOrder', state.baitTierOrder.join(', '));
    const el = document.getElementById('selectedBaitStatus');
    if (!el) return;
    if (state.selectedBait) {
        el.textContent = state.baitRemaining != null ? `${state.selectedBait} (${Math.max(state.baitRemaining, 0)} left)` : state.selectedBait;
        el.className = 'point-badge set';
    } else {
        el.textContent = 'None';
        el.className = 'point-badge unset';
    }
}

async function testBaitScan() {
    showToast('Scanning bait list — equip your rod so the list is visible', 'warn');
    const result = await sendToPython('test_bait_scan', '');
    if (result?.status !== 'success') return;
    if (!result.baits.length) {
        showErrorNotification('No bait found. Check the Bait List Region in Locations → Bait.');
        return;
    }
    const found = result.baits.map(b => b.count != null ? `${b.name} x${b.count}` : b.name).join(', ');
    showToast(`Found (best first): ${found}`, 'warn');
}

function confirmResetSettings() {
    if (confirm('This will reset ALL settings to defaults. This cannot be undone. Continue?')) {
        if (confirm('Really reset everything?')) {
            sendToPython('reset_settings', 'confirm').then(async () => {
                try {
                    const res = await fetch(BackendUrl('/state'));
                    const state = await res.json();
                    loadAllSettings(state);
                    window.currentDevilFruitSlots = state.devilFruitHotkeys || ['3'];
                    renderDevilFruitSlotSelector(window.currentDevilFruitSlots);
                    window.lastValidRodSlot = state.rodHotkey || '1';
                    window.lastValidSecondarySlot = state.anythingElseHotkey || '2';
                } catch (e) {
                    console.error('Failed to reload state after reset:', e);
                }
            });
        }
    }
}

function loadAllSettings(state) {
    if (state.hotkeys) {
        updateHotkey('start', state.hotkeys.StartStop || state.hotkeys.start_stop || 'f1');
        updateHotkey('exit', state.hotkeys.Exit || state.hotkeys.exit || 'f3');
    }
    setInputValue('rodHotkey', state.rodHotkey || '1');
    setInputValue('anythingElseHotkey', state.anythingElseHotkey || '2');

    let dfSlots = state.devilFruitHotkeys || state.devilFruitHotkey || ['3'];
    if (!Array.isArray(dfSlots)) dfSlots = [dfSlots];
    window.currentDevilFruitSlots = dfSlots;
    window.lastValidRodSlot = state.rodHotkey || '1';
    window.lastValidSecondarySlot = state.anythingElseHotkey || '2';
    renderDevilFruitSlotSelector(dfSlots);
    
    setToggleState('alwaysOnTopToggle', state.alwaysOnTop);
    setToggleState('debugOverlayToggle', state.showDebugOverlay);
    setToggleState('megalodonSoundToggle', state.megalodonSoundEnabled);
    
    setExpandableSection('autoBuyBaitToggle', 'autoBuyExpand', state.autoBuyCommonBait);
    setExpandableSection('autoStoreFruitToggle', 'autoStoreExpand', state.autoStoreDevilFruit);
    setExpandableSection('autoSelectBaitToggle', 'autoSelectBaitExpand', state.autoSelectTopBait);
    updateSmartBait(state);
    setToggleState('storeToBackpackToggle', state.storeToBackpack);
    setToggleState('storeOnlyWhenDetectedToggle', state.storeOnlyWhenDetected !== false);
    setToggleState('dropUnstorableFruitToggle', state.dropUnstorableFruit || false);
    setInputValue('fruitSweepLoops', state.fruitSweepLoops != null ? state.fruitSweepLoops : 25);
    updateFruitDetectStatus(state);

    setInputValue('loopsPerTopBait', state.loopsPerTopBait != null ? state.loopsPerTopBait : 1);

    setInputValue('kp', state.kp);
    setInputValue('kd', state.kd);
    setInputValue('pdClamp', state.pdClamp);
    setInputValue('pdApproaching', state.pdApproachingDamping);
    setInputValue('pdChasing', state.pdChasingDamping);
    setInputValue('gapTolerance', state.gapToleranceMultiplier);
    setInputValue('castHold', state.castHoldDuration);
    setInputValue('recastTimeout', state.recastTimeout);
    setInputValue('fishEndDelay', state.fishEndDelay);
    setInputValue('catchEndGrace', state.catchEndGrace);
    setInputValue('stateResend', state.stateResendInterval);
    setInputValue('loopsPerPurchase', state.loopsPerPurchase);
    setInputValue('loopsPerStore', state.loopsPerStore);
    setInputValue('focusDelay', state.robloxFocusDelay);
    setInputValue('postFocusDelay', state.robloxPostFocusDelay);
    setInputValue('preCastE', state.preCastEDelay);
    setInputValue('preCastClick', state.preCastClickDelay);
    setInputValue('preCastType', state.preCastTypeDelay);
    setInputValue('antiDetect', state.preCastAntiDetectDelay);
    setInputValue('fruitHotkey', state.storeFruitHotkeyDelay);
    setInputValue('fruitClick', state.storeFruitClickDelay);
    setInputValue('fruitShift', state.storeFruitShiftDelay);
    setInputValue('fruitBackspace', state.storeFruitBackspaceDelay);
    setInputValue('rodDelay', state.rodSelectDelay);
    setInputValue('baitDelay', state.autoSelectBaitDelay);
    setInputValue('cursorDelay', state.cursorAntiDetectDelay);
    setInputValue('scanDelay', state.scanLoopDelay);
    setInputValue('blackThreshold', state.blackScreenThreshold);
    setInputValue('spamDelay', state.antiMacroSpamDelay);
    setInputValue('webhookUrl', state.webhookUrl || '');
    setInputValue('discordUserId', state.discordUserId || '');
    setInputValue('soundSensitivity', state.soundSensitivity || 0.1);
    const audioSel = document.getElementById('audioDeviceSelect');
    if (audioSel && document.activeElement !== audioSel) {
        const wanted = state.audioDeviceIndex != null ? String(state.audioDeviceIndex) : 'auto';
        if ([...audioSel.options].some(o => o.value === wanted)) audioSel.value = wanted;
    }

    setExpandableSection('logDevilFruitToggle', 'logDevilFruitExpand', state.logDevilFruit || false);
    setToggleState('pingDevilFruitToggle', state.pingDevilFruit || false);
    setExpandableSection('logRecastTimeoutsToggle', 'logRecastTimeoutsExpand', state.logRecastTimeouts || false);
    setToggleState('pingRecastTimeoutsToggle', state.pingRecastTimeouts || false);
    setExpandableSection('logPeriodicStatsToggle', 'logPeriodicStatsExpand', state.logPeriodicStats || false);
    setToggleState('pingPeriodicStatsToggle', state.pingPeriodicStats || false);
    setExpandableSection('logGeneralUpdatesToggle', 'logGeneralUpdatesExpand', state.logGeneralUpdates || false);
    setToggleState('pingGeneralUpdatesToggle', state.pingGeneralUpdates || false);
    setExpandableSection('logMacroStateToggle', 'logMacroStateExpand', state.logMacroState || false);
    setToggleState('pingMacroStateToggle', state.pingMacroState || false);
    setExpandableSection('logErrorsToggle', 'logErrorsExpand', state.logErrors !== false);
    setToggleState('pingErrorsToggle', state.pingErrors || false);
    setInputValue('periodicStatsInterval', state.periodicStatsInterval || 5);

    updatePointStatus('waterPoint', state.waterPoint?.x, state.waterPoint?.y);
    updatePointStatus('leftPoint', state.leftPoint?.x, state.leftPoint?.y);
    updatePointStatus('middlePoint', state.middlePoint?.x, state.middlePoint?.y);
    updatePointStatus('rightPoint', state.rightPoint?.x, state.rightPoint?.y);
    updatePointStatus('storeFruitPoint', state.storeFruitPoint?.x, state.storeFruitPoint?.y);
    updatePointStatus('baitPoint', state.baitPoint?.x, state.baitPoint?.y);
    window.currentBackpackLocations = state.backpackLocations || [];
    if (state.devilFruitHotkeys && state.backpackLocations !== undefined) {
        renderBackpackLocationRows(state.devilFruitHotkeys, state.backpackLocations);
    }
}

async function loadInitialSettings() {
    const MAX_ATTEMPTS = 30;
    const RETRY_DELAY = 2000;
    for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt++) {
        try {
            const res = await fetch(BackendUrl('/state'));
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const state = await res.json();
            loadAllSettings(state);
            console.log(`Settings loaded on attempt ${attempt}`);
            return;
        } catch (e) {
            flog(`loadInitialSettings attempt ${attempt}/${MAX_ATTEMPTS} failed: ${e?.stack || e}`);
            if (attempt < MAX_ATTEMPTS) await new Promise(r => setTimeout(r, RETRY_DELAY));
        }
    }
    console.error('Failed to load settings after max attempts — backend may be down.');
}

function applyLiveState(state) {
    updateStatus(state.isRunning);
    updateMacroActivity(state);
    updateSmartBait(state);
    updateFruitDetectStatus(state);
    updateDashboard(state);
}

function applyFullState(state) {
    applyLiveState(state);
    // Toggles and values changed by hotkeys, reset/import or the macro itself show up here.
    // Focused inputs and just-clicked toggles are left alone
    loadAllSettings(state);

    if (state.is_admin !== undefined) {
        document.getElementById('adminIndicator').classList.toggle('active', state.is_admin);
        // On macOS the flag is Accessibility plus Input Monitoring, which input control and the hotkeys need there
        const isMac = state.platform === 'darwin';
        document.getElementById('adminText').textContent = isMac
            ? (state.is_admin ? 'Input Access Granted' : 'Needs Accessibility + Input Monitoring')
            : (state.is_admin ? 'Running as Admin' : 'Not Admin');
    }

    const portEl = document.getElementById('backendPortDisplay');
    if (portEl) portEl.textContent = `:${BackendPort}`;

    checkRequirements('autoStoreFruit');
    checkRequirements('autoBuyBait');
    checkRequirements('autoSelectBait');
}

// The live state (status, counters) is small and polled twice a second; the full settings only every few
// seconds or right after a change. Each request takes time from the macro's minigame loop, so keep them light
async function pollPythonState() {
    if (!BackendPort) return;
    const full = Date.now() - lastFullSync > 3000;
    try {
        const res = await fetch(BackendUrl(full ? '/state' : '/state?live=1'));
        const state = await res.json();
        if (full) {
            lastFullSync = Date.now();
            applyFullState(state);
        } else {
            applyLiveState(state);
        }
    } catch (e) {
        // A failing full sync leaves every setting at its page default ("Not Set" points), so say why, once
        const msg = `pollPythonState (${full ? 'full' : 'live'}) failed: ${e?.stack || e}`;
        if (msg !== window.lastPollError) flog(msg);
        window.lastPollError = msg;
    }
}

function startPolling() {
    if (pollingStarted) return;
    pollingStarted = true;
    // A chained timeout never overlaps a slow request the way setInterval does; a hidden window polls slowly
    const tick = async () => {
        await pollPythonState();
        pollTimer = setTimeout(tick, document.hidden ? 2000 : 500);
    };
    tick();
}

window.addEventListener('DOMContentLoaded', async () => {
    CURRENT_VERSION = (await fetch('./version.json').then(r => r.json())).version;
    document.getElementById('brandVer').textContent = `v${CURRENT_VERSION}`;
    document.getElementById('appVer').textContent = CURRENT_VERSION;

    await InitBackendPort();
    flog(`Backend port resolved to ${BackendPort} at DOMContentLoaded`);
    loadFastMode();
    loadSavedTheme();
    checkForUpdates();
    checkDisclaimer();
    loadAndBuildSlideshow();
    await loadAudioDevices();
    loadInitialSettings();

    setTimeout(() => {
        const c = document.getElementById('devilFruitSlotSelector');
        if (c && !c.children.length) renderDevilFruitSlotSelector(window.currentDevilFruitSlots || ['3']);
    }, 500);

    startPolling();

    document.querySelectorAll('.nav-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
            document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
            btn.classList.add('active');
            document.getElementById(btn.dataset.view).classList.add('active');
        });
    });
});

window.updateStatus = updateStatus;
window.updateHotkey = updateHotkey;
window.updatePointStatus = updatePointStatus;