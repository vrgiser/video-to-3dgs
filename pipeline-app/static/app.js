// Pipeline SPA — vanilla ES module, hash router, SSE-driven progress.
console.log('[pipeline] app.js loaded — build 20260915b (track editor enabled)');

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

// --- Routing ---------------------------------------------------------------

function currentRoute() {
    const hash = location.hash || '#/';
    // Optional ?tab= query to deep-link a specific top-level tab
    const tabParam = new URLSearchParams(location.search).get('tab');
    // Try the more specific routes first so '/jobs/{id}/track' doesn't get
    // swallowed by the bare '/jobs/{id}' pattern.
    let m = hash.match(/^#\/track\/([0-9a-f]+)/i);
    if (m) return { view: 'track', jobId: m[1] };
    m = hash.match(/^#\/viewer\/([0-9a-f]+)/i);
    if (m) return { view: 'viewer', jobId: m[1] };
    m = hash.match(/^#\/jobs\/([0-9a-f]+)/i);
    if (m) return { view: 'detail', jobId: m[1] };
    return { view: 'home', tab: tabParam || 'upload' };
}

function navigate() {
    const route = currentRoute();
    // Top-level views: only one of these is shown at a time
    const isHome = route.view === 'home';
    $('#view-upload').hidden = !isHome || route.tab !== 'upload';
    $('#view-list').hidden = !isHome || route.tab !== 'list';
    $('#view-detail').hidden = route.view !== 'detail';
    $('#view-viewer').hidden = route.view !== 'viewer';
    $('#view-track').hidden  = route.view !== 'track';

    // Tab link active state
    $$('.tab-link').forEach(el => {
        el.classList.toggle(
            'active',
            isHome && el.dataset.tab === route.tab
        );
    });

    if (route.view === 'detail') {
        openJobDetail(route.jobId);
    } else if (route.view === 'viewer') {
        openViewer(route.jobId);
    } else if (route.view === 'track') {
        openTrackEditor(route.jobId);
    } else {
        closeJobDetail();
        closeViewer();
        closeTrackEditor();
        closeHome();
        if (route.tab === 'list') loadJobsList();
    }
}

// Top-level tab clicks: stay on home, just swap the tab param
document.addEventListener('click', (e) => {
    const tabLink = e.target.closest('.tab-link');
    if (!tabLink) return;
    e.preventDefault();
    const tab = tabLink.dataset.tab;
    location.hash = '#/';
    history.replaceState(null, '', `?tab=${tab}#`);
    navigate();
});

function closeHome() {
    // Tear down any list-tab subscriptions when navigating away from home.
    for (const es of activeSubscriptions.values()) es.close();
    activeSubscriptions.clear();
}

window.addEventListener('hashchange', navigate);

// --- Jobs list -------------------------------------------------------------

// Map of jobId -> EventSource so we can close them when leaving the list tab.
const activeSubscriptions = new Map();

function thumbnailHtml(j) {
    if (['pending', 'ffmpeg'].includes(j.stage)) {
        return `<div class="thumb thumb-placeholder">⋯</div>`;
    }
    return `<img class="thumb" src="/jobs/${j.id}/frames/0001.jpg" loading="lazy" alt="" onerror="this.outerHTML='<div class=&quot;thumb thumb-placeholder&quot;>×</div>'" />`;
}

function paramsHtml(j) {
    const fps = j.fps ?? '-';
    const scale = j.scale_factor != null ? `${j.scale_factor}×` : '-';
    // Prefer trained_iters (cumulative actual training) over the originally
    // requested opensplat_iters, since resumes and stage-3 re-runs add to it.
    const trained = (j.trained_iters && j.trained_iters > 0) ? j.trained_iters : j.opensplat_iters;
    const iters = trained ?? '-';
    return `<span class="params">` +
        `<span title="每秒抽帧数">${fps} fps</span>` +
        `<span title="缩放倍数">${scale}</span>` +
        `<span title="累计 OpenSplat 训练步数">${iters} iter</span>` +
        `</span>`;
}

function jobRowHtml(j) {
    // Resume works for any job with a final .ply — done or failed.
    // Run-stage-3 works for jobs that have COLMAP output but no splat (sparse exists).
    // Requeue works for pending/cancelled/failed jobs (idempotent retry).
    const canResume = j.splat_exists;
    const canRunStage3 = !j.splat_exists && j.has_sparse;
    const canRequeue = ['pending', 'cancelled', 'failed'].includes(j.stage);
    const requeueLabel = j.stage === 'pending' ? '重新入队' : '重试';
    const erpBadge = j.is_erp
        ? '<span class="badge erp" title="360° equirectangular — each frame → 6 cubemap faces">360°</span> '
        : '';
    return `
        <tr data-job-id="${j.id}">
            <td class="thumb-cell">${thumbnailHtml(j)}</td>
            <td><a href="#/jobs/${j.id}">${j.id.slice(0, 8)}…</a></td>
            <td>${erpBadge}<span class="badge ${j.stage}" id="badge-${j.id}">${j.stage}</span></td>
            <td id="pct-${j.id}">${(j.percent ?? 0).toFixed(0)}%</td>
            <td>${paramsHtml(j)}</td>
            <td class="muted" id="msg-${j.id}">${escapeHtml(j.message || '')}</td>
            <td id="actions-${j.id}">
                ${canRequeue ? `<button class="secondary" data-action="requeue" data-id="${j.id}">${requeueLabel}</button> ` : ''}
                ${canResume ? `<button class="secondary" data-action="resume" data-id="${j.id}" style="color: var(--success); border-color: var(--success);">+续训</button> ` : ''}
                ${canRunStage3 ? `<button class="secondary" data-action="run-stage3" data-id="${j.id}" style="color: var(--success); border-color: var(--success);">跑 OpenSplat</button> ` : ''}
                ${j.stage === 'done' ? `<a class="secondary" style="padding: 4px 10px; border-radius: 4px; border: 1px solid var(--border); text-decoration: none; font-size: 13px;" href="#/viewer/${j.id}">查看 3D</a> ` : ''}
                ${j.sog_exists ? `<a class="secondary" style="padding: 4px 10px; border-radius: 4px; border: 1px solid var(--border); text-decoration: none; font-size: 13px;" href="/jobs/${j.id}/export.html" download="splat-${j.id.slice(0,8)}.html" title="下载独立 HTML（含内嵌模型）">导出 HTML</a> ` : ''}
                ${j.sog_exists ? `<button class="secondary" data-action="edit-track" data-id="${j.id}" title="${j.track_exists ? '已记录相机轨迹，点击编辑' : '记录播放视点轨迹'}" style="${j.track_exists ? 'color: var(--accent); border-color: var(--accent);' : ''}">${j.track_exists ? '✏ 轨迹' : '+ 轨迹'}</button> ` : ''}
                <button class="secondary" data-action="cancel" data-id="${j.id}" ${j.stage === 'done' || j.stage === 'failed' || j.stage === 'cancelled' ? 'disabled' : ''}>取消</button>
                <button class="secondary" data-action="delete" data-id="${j.id}" style="color: var(--error);">删除</button>
            </td>
        </tr>
    `;
}

function subscribeRow(jobId) {
    if (activeSubscriptions.has(jobId)) return;
    const es = new EventSource(`/api/jobs/${jobId}/events`);
    es.onmessage = (e) => {
        try {
            const ev = JSON.parse(e.data);
            updateRow(jobId, ev);
        } catch {}
    };
    activeSubscriptions.set(jobId, es);
}

function updateRow(jobId, ev) {
    const stage = ev.stage || 'pending';
    const badge = document.getElementById(`badge-${jobId}`);
    if (badge) {
        badge.textContent = stage;
        badge.className = `badge ${stage}`;
    }
    const pct = document.getElementById(`pct-${jobId}`);
    if (pct && typeof ev.percent === 'number') {
        pct.textContent = `${ev.percent.toFixed(0)}%`;
    }
    const msg = document.getElementById(`msg-${jobId}`);
    if (msg && ev.message) msg.textContent = ev.message;

    if (['done', 'failed', 'cancelled'].includes(stage)) {
        const es = activeSubscriptions.get(jobId);
        if (es) { es.close(); activeSubscriptions.delete(jobId); }
        fetch(`/api/jobs/${jobId}`).then(r => r.json()).then(j => {
            const tr = document.querySelector(`tr[data-job-id="${jobId}"]`);
            if (tr) tr.outerHTML = jobRowHtml(j);
        });
    }
}

async function loadJobsList() {
    // Close any existing subscriptions from a previous list render.
    for (const es of activeSubscriptions.values()) es.close();
    activeSubscriptions.clear();

    const tbody = $('#jobs-tbody');
    try {
        const jobs = await fetch('/api/jobs').then(r => r.json());
        if (!jobs.length) {
            tbody.innerHTML = '<tr><td colspan="7" class="empty">暂无作业</td></tr>';
            return;
        }
        tbody.innerHTML = jobs.map(jobRowHtml).join('');
        // Subscribe to live SSE for any job that may still be progressing.
        for (const j of jobs) {
            if (['pending', 'ffmpeg', 'colmap', 'opensplat'].includes(j.stage)) {
                subscribeRow(j.id);
            }
        }
    } catch (e) {
        tbody.innerHTML = `<tr><td colspan="7" class="empty">加载失败: ${e.message}</td></tr>`;
    }
}

$('#refresh-list').addEventListener('click', loadJobsList);

$('#cleanup-btn').addEventListener('click', async () => {
    if (!confirm('删除所有已完成/失败/已取消的作业及其所有产物？此操作不可撤销。')) return;
    const btn = $('#cleanup-btn');
    btn.disabled = true;
    btn.textContent = '清理中…';
    try {
        const r = await fetch('/api/jobs', { method: 'DELETE' });
        const data = await r.json();
        const freed = (data.freed_bytes / 1024 / 1024).toFixed(1);
        alert(`已清理 ${data.deleted} 个作业，释放 ${freed}MB${data.skipped_active ? `（跳过 ${data.skipped_active} 个正在运行的）` : ''}。`);
        await loadJobsList();
    } catch (e) {
        alert('清理失败：' + e.message);
    } finally {
        btn.disabled = false;
        btn.textContent = '清除全部已完成';
    }
});

$('#jobs-tbody').addEventListener('click', async (e) => {
    const cancelBtn = e.target.closest('[data-action="cancel"]');
    if (cancelBtn) {
        const id = cancelBtn.dataset.id;
        cancelBtn.disabled = true;
        await fetch(`/api/jobs/${id}/cancel`, { method: 'POST' });
        return;
    }
    const resumeBtn = e.target.closest('[data-action="resume"]');
    if (resumeBtn) {
        const id = resumeBtn.dataset.id;
        const extra = prompt('续训多少步？', '10000');
        if (!extra) return;
        const n = parseInt(extra, 10);
        if (!Number.isFinite(n) || n <= 0) {
            alert('请输入正整数');
            return;
        }
        resumeBtn.disabled = true;
        resumeBtn.textContent = '排队中…';
        try {
            const r = await fetch(`/api/jobs/${id}/resume?extra_iters=${n}`, { method: 'POST' });
            if (!r.ok) throw new Error(await r.text());
            location.hash = `#/jobs/${id}`;
        } catch (err) {
            alert('续训失败：' + err.message);
            resumeBtn.disabled = false;
            resumeBtn.textContent = '+续训';
        }
        return;
    }
    const stage3Btn = e.target.closest('[data-action="run-stage3"]');
    if (stage3Btn) {
        const id = stage3Btn.dataset.id;
        const extra = prompt('OpenSplat 跑多少步？', '10000');
        if (!extra) return;
        const n = parseInt(extra, 10);
        if (!Number.isFinite(n) || n <= 0) {
            alert('请输入正整数');
            return;
        }
        stage3Btn.disabled = true;
        stage3Btn.textContent = '排队中…';
        try {
            const r = await fetch(`/api/jobs/${id}/stage/opensplat?extra_iters=${n}`, { method: 'POST' });
            if (!r.ok) throw new Error(await r.text());
            location.hash = `#/jobs/${id}`;
        } catch (err) {
            alert('启动失败：' + err.message);
            stage3Btn.disabled = false;
            stage3Btn.textContent = '跑 OpenSplat';
        }
        return;
    }
    const requeueBtn = e.target.closest('[data-action="requeue"]');
    if (requeueBtn) {
        const id = requeueBtn.dataset.id;
        const originalLabel = requeueBtn.textContent;
        requeueBtn.disabled = true;
        requeueBtn.textContent = '排队中…';
        try {
            const r = await fetch(`/api/jobs/${id}/requeue`, { method: 'POST' });
            if (!r.ok) throw new Error(await r.text());
            location.hash = `#/jobs/${id}`;
        } catch (err) {
            alert('重新入队失败：' + err.message);
            requeueBtn.disabled = false;
            requeueBtn.textContent = originalLabel;
        }
        return;
    }
    const delBtn = e.target.closest('[data-action="delete"]');
    if (delBtn) {
        const id = delBtn.dataset.id;
        if (!confirm(`确定删除作业 ${id.slice(0, 8)}… 及其所有产物？此操作不可撤销。`)) return;
        delBtn.disabled = true;
        await fetch(`/api/jobs/${id}`, { method: 'DELETE' });
        await loadJobsList();
    }
    const trackBtn = e.target.closest('[data-action="edit-track"]');
    if (trackBtn) {
        const id = trackBtn.dataset.id;
        console.log('[track] click → navigate to track view', id);
        location.hash = `#/track/${id}`;
    }
});

function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c =>
        ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])
    );
}

// --- Upload ----------------------------------------------------------------

const fileInput = $('#file-input');
const dropzone = $('#dropzone');
const submitBtn = $('#submit-btn');
const uploadError = $('#upload-error');
let chosenFile = null;

dropzone.addEventListener('click', () => fileInput.click());
dropzone.addEventListener('dragover', (e) => { e.preventDefault(); dropzone.classList.add('dragover'); });
dropzone.addEventListener('dragleave', () => dropzone.classList.remove('dragover'));
dropzone.addEventListener('drop', (e) => {
    e.preventDefault();
    dropzone.classList.remove('dragover');
    if (e.dataTransfer.files.length) {
        setFile(e.dataTransfer.files[0]);
    }
});
fileInput.addEventListener('change', () => {
    if (fileInput.files.length) setFile(fileInput.files[0]);
});

function setFile(file) {
    chosenFile = file;
    $('#dropzone-text').textContent = `${file.name} · ${(file.size / 1024 / 1024).toFixed(1)} MB`;
    submitBtn.disabled = false;

    // Probe video dimensions for the "原尺寸" preview; computing actual output
    // dims also happens client-side so we can show "原尺寸 1920×1080 → 输出 960×540".
    probeAspectRatio(file).then((dim) => {
        if (!dim) {
            $('#dims-info').hidden = true;
            return;
        }
        const f = parseFloat($('#scale-factor').value) || 1.0;
        const outW = Math.max(2, Math.round(dim.srcW * f / 2) * 2);
        const outH = Math.max(2, Math.round(dim.srcH * f / 2) * 2);
        showDimsInfo(dim.srcW, dim.srcH, outW, outH);
    });
}

// Live recompute output dims when the user edits the scale factor.
$('#scale-factor').addEventListener('input', () => {
    if (!chosenFile) return;
    probeAspectRatio(chosenFile).then((dim) => {
        if (!dim) return;
        const f = parseFloat($('#scale-factor').value) || 1.0;
        const outW = Math.max(2, Math.round(dim.srcW * f / 2) * 2);
        const outH = Math.max(2, Math.round(dim.srcH * f / 2) * 2);
        showDimsInfo(dim.srcW, dim.srcH, outW, outH);
    });
});

function showDimsInfo(srcW, srcH, outW, outH) {
    const el = $('#dims-info');
    if (outW === srcW && outH === srcH) {
        el.textContent = `原尺寸 ${srcW}×${srcH}（无缩放）`;
    } else {
        el.textContent = `原尺寸 ${srcW}×${srcH} → 输出 ${outW}×${outH}`;
    }
    el.hidden = false;
}

function probeAspectRatio(file) {
    return new Promise((resolve) => {
        const url = URL.createObjectURL(file);
        const v = document.createElement('video');
        v.preload = 'metadata';
        const cleanup = () => URL.revokeObjectURL(url);
        v.onloadedmetadata = () => {
            const w = v.videoWidth, h = v.videoHeight;
            cleanup();
            if (!w || !h) return resolve(null);
            resolve({ srcW: w, srcH: h });
        };
        v.onerror = () => { cleanup(); resolve(null); };
        v.src = url;
    });
}

$('#upload-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    if (!chosenFile) return;
    uploadError.hidden = true;
    submitBtn.disabled = true;
    submitBtn.textContent = '上传中…';

    const form = new FormData();
    form.append('file', chosenFile);
    form.append('fps', $('#fps').value);
    form.append('scale_factor', $('#scale-factor').value);
    form.append('opensplat_iters', $('#iters').value);

    try {
        const r = await fetch('/api/jobs', { method: 'POST', body: form });
        if (!r.ok) {
            const err = await r.text();
            throw new Error(err);
        }
        const job = await r.json();
        location.hash = `#/jobs/${job.id}`;
        // Reset form
        chosenFile = null;
        fileInput.value = '';
        $('#dropzone-text').textContent = '点击或拖放视频到此处 (.mp4 / .mov / .webm)';
        submitBtn.textContent = '上传并开始';
        $('#scale-factor').value = '1';
        $('#dims-info').hidden = true;
    } catch (err) {
        uploadError.textContent = `上传失败: ${err.message}`;
        uploadError.hidden = false;
        submitBtn.disabled = false;
        submitBtn.textContent = '上传并开始';
    }
});

// --- Job detail ------------------------------------------------------------

let eventSource = null;
let logPollTimer = null;
let currentLogStage = null;   // which stage's log we're tailing
let currentJobId = null;

const LOG_FILE_FOR_STAGE = {
    ffmpeg:    'ffmpeg.log',
    cubemap:   'cubemap.log',
    colmap:    'colmap.log',
    opensplat: 'opensplat.log',
};

function setLogStage(jobId, stage) {
    if (!LOG_FILE_FOR_STAGE[stage]) return;  // not a runnable stage
    if (currentJobId === jobId && currentLogStage === stage) return;  // already tailing
    currentJobId = jobId;
    currentLogStage = stage;
    $('#log-tail').textContent = '(loading ' + stage + ' log…)';
    scheduleLogPoll(true);
}

function scheduleLogPoll(immediate = false) {
    if (logPollTimer) {
        clearTimeout(logPollTimer);
        logPollTimer = null;
    }
    if (!currentJobId || !currentLogStage) return;
    const poll = async () => {
        if (!currentJobId || !currentLogStage) return;
        const file = LOG_FILE_FOR_STAGE[currentLogStage];
        try {
            const log = await fetch(`/jobs/${currentJobId}/logs/${file}`).then(r => r.ok ? r.text() : '');
            const pre = $('#log-tail');
            const newText = log ? log.split('\n').slice(-300).join('\n') : '(no log yet)';
            if (pre.textContent !== newText) {
                const atBottom = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 20;
                pre.textContent = newText;
                if (atBottom) pre.scrollTop = pre.scrollHeight;
            }
        } catch (e) { /* ignore */ }
        logPollTimer = setTimeout(poll, 2000);
    };
    if (immediate) poll();
    else logPollTimer = setTimeout(poll, 50);
}

function openJobDetail(jobId) {
    $('#detail-id').textContent = jobId.slice(0, 12) + '…';
    $('#detail-stage').textContent = 'pending';
    $('#detail-stage').className = 'badge pending';
    $('#detail-message').textContent = '';
    $('#detail-progress').style.width = '0%';
    $('#log-tail').textContent = '';
    $('#frames-card').hidden = true;
    $('#splat-card').hidden = true;

    if (eventSource) {
        eventSource.close();
        eventSource = null;
    }
    if (logPollTimer) {
        clearTimeout(logPollTimer);
        logPollTimer = null;
    }
    currentJobId = jobId;
    currentLogStage = null;

    // Initial fetch for current status
    fetch(`/api/jobs/${jobId}`).then(r => r.json()).then(j => {
        applyStatus(j);
        if (j.stage && LOG_FILE_FOR_STAGE[j.stage]) {
            setLogStage(jobId, j.stage);
        }
    }).catch(() => {});

    eventSource = new EventSource(`/api/jobs/${jobId}/events`);
    eventSource.onmessage = (e) => {
        try {
            const ev = JSON.parse(e.data);
            handleEvent(ev);
            // Auto-switch the tailed log when the active stage changes.
            if (ev.stage && LOG_FILE_FOR_STAGE[ev.stage]) {
                setLogStage(jobId, ev.stage);
            } else if (ev.stage === 'done' || ev.stage === 'failed' || ev.stage === 'cancelled') {
                // Terminal: stop polling but do a final fetch.
                if (logPollTimer) { clearTimeout(logPollTimer); logPollTimer = null; }
                if (currentLogStage) {
                    const file = LOG_FILE_FOR_STAGE[currentLogStage];
                    fetch(`/jobs/${jobId}/logs/${file}`).then(r => r.ok ? r.text() : '').then(t => {
                        if (t) $('#log-tail').textContent = t.split('\n').slice(-300).join('\n');
                    });
                }
            }
        } catch {}
    };
    eventSource.onerror = () => {
        // Browser will auto-reconnect; nothing to do
    };

    $('#cancel-btn').onclick = async () => {
        $('#cancel-btn').disabled = true;
        await fetch(`/api/jobs/${jobId}/cancel`, { method: 'POST' });
    };
}

function closeJobDetail() {
    if (eventSource) {
        eventSource.close();
        eventSource = null;
    }
    if (logPollTimer) {
        clearTimeout(logPollTimer);
        logPollTimer = null;
    }
    currentJobId = null;
    currentLogStage = null;
}

function openViewer(jobId) {
    $('#viewer-job-id').textContent = jobId;
    $('#viewer-iframe').src = `/viewer/${jobId}`;
    $('#viewer-newtab').href = `/viewer/${jobId}`;
}

function closeViewer() {
    $('#viewer-iframe').src = 'about:blank';
}

function applyStatus(j) {
    $('#detail-stage').textContent = j.stage;
    $('#detail-stage').className = `badge ${j.stage}`;
    $('#detail-message').textContent = j.message || '';
    $('#detail-progress').style.width = `${(j.percent ?? 0).toFixed(1)}%`;
}

function handleEvent(ev) {
    if (ev.type === 'progress' || ev.type === 'stage') {
        $('#detail-stage').textContent = ev.stage;
        $('#detail-stage').className = `badge ${ev.stage}`;
        $('#detail-message').textContent = ev.message || '';
        $('#detail-progress').style.width = `${(ev.percent ?? 0).toFixed(1)}%`;
    }
    if (ev.stage === 'done') {
        // After ffmpeg completes, load the frames gallery
        loadFramesGallery();
        $('#splat-card').hidden = false;
        $('#open-viewer-link').href = `#/viewer/${currentJobId}`;
        $('#open-viewer-link').onclick = (e) => {
            e.preventDefault();
            location.hash = `#/viewer/${currentJobId}`;
        };
    }
    // (Log file auto-switching happens in openJobDetail's SSE handler)
}

async function refreshLog(jobId) {
    try {
        const log = await fetch(`/jobs/${jobId}/logs/ffmpeg.log`).then(r => r.ok ? r.text() : '');
        $('#log-tail').textContent = log ? log.split('\n').slice(-200).join('\n') : '(no log yet)';
        const pre = $('#log-tail');
        pre.scrollTop = pre.scrollHeight;
    } catch {}
}


async function loadFramesGallery() {
    const jobId = currentRoute().jobId;
    if (!jobId) return;
    try {
        const idx = await fetch(`/jobs/${jobId}/`).then(r => r.json());
        const frames = idx.frames || [];
        $('#frames-count').textContent = `共 ${frames.length} 帧`;
        $('#frames-gallery').innerHTML = frames.slice(0, 60).map(f =>
            `<img src="/jobs/${jobId}/frames/${f}" loading="lazy" alt="${f}" />`
        ).join('');
        if (frames.length > 60) {
            $('#frames-gallery').insertAdjacentHTML('beforeend',
                `<div class="muted" style="padding: 12px;">… 还有 ${frames.length - 60} 帧未显示</div>`);
        }
        $('#frames-card').hidden = frames.length === 0;
    } catch (e) {
        console.warn('frames gallery load failed', e);
    }
}

// --- Camera-track editor ---------------------------------------------------

let trackJobId = null;
let trackFrames = [];          // [{position, target, fov}, ...]
let trackViewerReady = false;
const trackPendingCmds = [];   // queued postMessage requests before viewer hello

function trackRenderTable() {
    const n = trackFrames.length;
    $('#track-count-pill').textContent = `${n} 个关键帧`;
    $('#track-save-btn').disabled = n === 0;
    if (n === 0) {
        $('#track-tbody').innerHTML = `<tr><td colspan="5" class="muted" style="text-align:center;">（暂无）</td></tr>`;
        return;
    }
    $('#track-tbody').innerHTML = trackFrames.map((k, i) => `
        <tr>
            <td>#${i+1}</td>
            <td>[${k.position.map(v=>v.toFixed(2)).join(', ')}]</td>
            <td>[${k.target.map(v=>v.toFixed(2)).join(', ')}]</td>
            <td>${k.fov.toFixed(1)}</td>
            <td><span class="kf-del" data-i="${i}" style="color:var(--error);cursor:pointer;">删</span></td>
        </tr>
    `).join('');
    $$('#track-tbody .kf-del').forEach(el => {
        el.onclick = () => {
            trackFrames.splice(+el.dataset.i, 1);
            trackRenderTable();
        };
    });
}

async function trackCallViewer(type, payload) {
    return new Promise((resolve, reject) => {
        if (!trackViewerReady) {
            trackPendingCmds.push({ type, payload, resolve, reject });
            return;
        }
        const w = $('#track-viewer').contentWindow;
        trackPendingCmds.push({ type, payload, resolve, reject });
        w.postMessage({ type, ...(payload||{}) }, '*');
        setTimeout(() => {
            const idx = trackPendingCmds.findIndex(c => c.resolve === resolve);
            if (idx >= 0) {
                trackPendingCmds.splice(idx, 1);
                reject(new Error('viewer 1秒内未响应: ' + type));
            }
        }, 1000);
    });
}

async function openTrackEditor(jobId) {
    trackJobId = jobId;
    $('#track-job-id').textContent = jobId.slice(0, 8) + '…';
    $('#track-status').textContent = '加载 viewer…';
    trackFrames = [];
    trackRenderTable();
    trackViewerReady = false;
    $('#track-capture-btn').disabled = true;
    $('#track-viewer-status').innerHTML = 'viewer 状态：<span class="err">加载中…</span>';
    $('#track-delete-btn').hidden = true;
    $('#track-viewer').src = `/viewer/${jobId}?debug=1`;

    // Hydrate from saved track (if any)
    try {
        const r = await fetch(`/api/jobs/${jobId}/camera-track`);
        if (r.ok) {
            const data = await r.json();
            trackFrames = data.keyframes || [];
            trackRenderTable();
            $('#track-status').textContent = `已载入 ${trackFrames.length} 个关键帧（自动播放）`;
            $('#track-delete-btn').hidden = false;
        } else {
            $('#track-status').textContent = '尚未记录关键帧';
        }
    } catch (e) {
        $('#track-status').textContent = '载入失败：' + e.message;
    }
}

function closeTrackEditor() {
    trackJobId = null;
    trackFrames = [];
    const v = $('#track-viewer');
    if (v) v.src = 'about:blank';
    trackViewerReady = false;
}

function openShowcase() {}
function closeShowcase() {}

// Bridge events from viewer iframe
window.addEventListener('message', (ev) => {
    if (!$('#track-viewer') || ev.source !== $('#track-viewer').contentWindow) return;
    const d = ev.data;
    if (!d || typeof d !== 'object') return;
    if (d.type === 'pipeline:hello') {
        trackViewerReady = !!d.getCameraStateAvailable;
        $('#track-viewer-status').innerHTML = trackViewerReady
            ? 'viewer 状态：<span class="ok">就绪</span>'
            : 'viewer 状态：<span class="err">无 getCameraState</span>';
        $('#track-capture-btn').disabled = !trackViewerReady;
        // flush queued
        const w = ev.source;
        for (const c of trackPendingCmds.splice(0)) {
            w.postMessage({ type: c.type, ...(c.payload||{}) }, '*');
        }
    } else if (d.type === 'pipeline:cameraState') {
        const cb = trackPendingCmds.shift();
        if (cb && cb.resolve) cb.resolve(d.snapshot);
    } else if (d.type === 'pipeline:error') {
        const cb = trackPendingCmds.shift();
        if (cb && cb.reject) cb.reject(new Error(d.message));
    }
});

$('#track-capture-btn').onclick = async () => {
    try {
        const s = await trackCallViewer('pipeline:getCameraState');
        if (!s || !s.target) { alert('无法读取当前视角（target 缺失）'); return; }
        trackFrames.push({
            position: s.position,
            target:   s.target,
            fov:      s.fov,
        });
        trackRenderTable();
    } catch (e) { alert('读取失败：' + e.message); }
};

$('#track-clear-btn').onclick = () => {
    if (!trackFrames.length) return;
    if (!confirm('清空当前所有关键帧？（不影响已保存的轨迹）')) return;
    trackFrames = [];
    trackRenderTable();
};

$('#track-save-btn').onclick = async () => {
    if (!trackJobId || !trackFrames.length) return;
    const btn = $('#track-save-btn');
    btn.disabled = true;
    btn.textContent = '保存中…';
    try {
        const r = await fetch(`/api/jobs/${trackJobId}/camera-track`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ keyframes: trackFrames }),
        });
        if (!r.ok) throw new Error(await r.text());
        $('#track-status').textContent = `已保存 ${trackFrames.length} 个关键帧`;
        $('#track-delete-btn').hidden = false;
    } catch (e) {
        alert('保存失败：' + e.message);
    } finally {
        btn.disabled = false;
        btn.textContent = '💾 保存轨迹';
    }
};

$('#track-delete-btn').onclick = async () => {
    if (!trackJobId) return;
    if (!confirm('删除已保存的相机轨迹？')) return;
    try {
        const r = await fetch(`/api/jobs/${trackJobId}/camera-track`, { method: 'DELETE' });
        if (!r.ok) throw new Error(await r.text());
        trackFrames = [];
        trackRenderTable();
        $('#track-status').textContent = '已删除';
        $('#track-delete-btn').hidden = true;
    } catch (e) { alert('删除失败：' + e.message); }
};

$('#track-play-btn').onclick = async () => {
    if (!trackFrames.length) return;
    // Save first, then reload the viewer iframe so it picks up the saved track.
    await $('#track-save-btn').click();
    $('#track-viewer').src = $('#track-viewer').src;
};

// --- Boot ------------------------------------------------------------------

navigate();