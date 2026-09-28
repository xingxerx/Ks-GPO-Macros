const invoke = window.__TAURI__?.core?.invoke ?? (async (cmd, args) => {
    console.log('[invoke]', cmd, args);
    if (cmd === 'launch_macro') return { port: window.__BACKEND_PORT__ || 8765 };
    return null;
});

const MACROS = [
    {
        id: 'fishing',
        name: 'Fishing Macro',
        desc: 'Auto-fishes with detection, cast timing, and item collection.',
        tag: 'FISHING',
        free: true,
        visible: true,
        color: '#4f8ef7',
        icon: `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M18 16.5a2 2 0 1 1-4 0c0-1.1 2-4 2-4s2 2.9 2 4z"/><path d="M4 18h2M6 14c0 0 2-2 4-2s4 2 4 2"/><line x1="6" y1="18" x2="6" y2="10"/><line x1="6" y1="10" x2="18" y2="4"/></svg>`,
    },
    {
        id: 'mihawk',
        name: 'Mihawk Macro',
        comingSoon: true,
        desc: 'Precision Mihawk boss automation with dodge and DPS logic.',
        tag: 'MIHAWK',
        free: false,
        visible: true,
        color: '#e0b854',
        icon: `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><polyline points="8 6 18 6 18 16"/></svg>`,
    },
    {
        id: 'roger',
        name: 'Roger Macro',
        comingSoon: true,
        desc: 'Full Roger raid loop with phase detection and auto-heal.',
        tag: 'ROGER',
        free: false,
        visible: true,
        color: '#e05555',
        icon: `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 8v4l3 3"/></svg>`,
    },
];

function hexToRgb(hex) {
    const r = parseInt(hex.slice(1, 3), 16);
    const g = parseInt(hex.slice(3, 5), 16);
    const b = parseInt(hex.slice(5, 7), 16);
    return `${r},${g},${b}`;
}

function buildGrid() {
    const grid = document.getElementById('macroGrid');
    grid.innerHTML = '';

    const visible = MACROS.filter(m => m.visible);

    visible.forEach((m, i) => {
        const rgb = hexToRgb(m.color);
        const row = document.createElement('div');
        row.className = 'macro-row' + (m.comingSoon ? ' coming-soon' : '');
        row.dataset.macro = m.id;
        row.style.setProperty('--row-color', m.color);
        row.style.setProperty('--row-color-dim', `rgba(${rgb},0.35)`);
        row.style.setProperty('--row-color-bg', `rgba(${rgb},0.1)`);
        row.style.animationDelay = `${i * 0.04}s`;

        if (!m.comingSoon) {
            row.onclick = () => handleCardClick(row, m.id);
        }

        const lockIcon = `<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>`;

        row.innerHTML = `
            <div class="macro-pip"></div>
            <div class="macro-icon">${m.comingSoon ? lockIcon : m.icon}</div>
            <div class="macro-info">
                <div class="macro-name">
                    ${m.name}
                    <span class="macro-tag ${m.comingSoon ? 'tag-unavailable' : ''}">${m.comingSoon ? 'UNAVAILABLE' : m.tag}${m.free && !m.comingSoon ? ' <span style="font-size:8px;opacity:0.65">FREE</span>' : ''}</span>
                </div>
                <div class="macro-desc">${m.comingSoon ? 'Not yet available — currently in development' : m.desc}</div>
            </div>
            <div class="macro-right" id="right-${m.id}">
                ${m.comingSoon
                ? `<span class="macro-soon-label">in development</span>`
                : `<svg class="macro-chevron" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"/></svg>`
            }
            </div>`;

        grid.appendChild(row);
    });
}

function setToast(text, visible) {
    const toast = document.getElementById('backendToast');
    const toastText = document.getElementById('backendToastText');
    toastText.textContent = text;
    toast.classList.toggle('show', visible);
}

function setRowLoading(id, loading) {
    const row = document.querySelector(`[data-macro="${id}"]`);
    const right = document.getElementById(`right-${id}`);
    if (!row || !right) return;
    row.classList.toggle('card-loading', loading);
    if (loading) {
        right.innerHTML = `<div class="macro-spinner"></div>`;
    } else {
        right.innerHTML = `<svg class="macro-chevron" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"/></svg>`;
    }
}

async function handleCardClick(rowEl, macro_id) {
    if (rowEl.classList.contains('card-loading')) return;

    setRowLoading(macro_id, true);
    setToast('Starting backend…', true);

    try {
        await invoke('launch_macro', { macroName: macro_id });
        document.getElementById('rippleRing').classList.add('go');
        await new Promise(r => setTimeout(r, 400));
        setToast('', false);
    } catch (e) {
        console.error('launch_macro failed:', e);
        setToast(`Error: ${e?.toString().replace('Error: ', '') || 'Failed to start'}`, true);
        setTimeout(() => setToast('', false), 3000);
    } finally {
        setRowLoading(macro_id, false);
    }
}

window.addEventListener('DOMContentLoaded', () => {
    buildGrid();
    fetch('./version.json')
        .then(r => r.json())
        .then(d => { document.getElementById('hubVer').textContent = `v${d.version}`; })
        .catch(() => { });
});