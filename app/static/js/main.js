function getCurrentPoolType() {
    const pool = (document.body && document.body.dataset && document.body.dataset.poolType) || "normal";
    return pool === "welfare" ? "welfare" : "normal";
}

/**
 * GPT Team 管理系统 - 通用 JavaScript
 */

function cleanupLegacyThemeSettingsSection() {
    const legacyLinks = document.querySelectorAll('[data-target="settings-ui-theme"], a[href="#settings-ui-theme"]');
    legacyLinks.forEach((node) => node.remove());

    const legacyPanel = document.getElementById('settings-ui-theme');
    if (legacyPanel) {
        legacyPanel.remove();
    }

    if (window.location.hash === '#settings-ui-theme') {
        history.replaceState(null, '', '#settings-proxy');
    }
}


function applySystemTheme(themeName) {
    const body = document.body;
    if (!body) return;

    const normalized = String(themeName || '').toLowerCase() === 'warm' ? 'warm' : 'ocean';
    body.dataset.uiTheme = normalized;
    body.classList.remove('theme-ocean', 'theme-warm');
    body.classList.add(`theme-${normalized}`);
    document.documentElement.classList.remove('theme-ocean', 'theme-warm');
    document.documentElement.classList.add(`theme-${normalized}`);
}

function getCurrentSystemTheme() {
    const bodyTheme = document.body?.dataset?.uiTheme;
    if (bodyTheme === 'warm' || bodyTheme === 'ocean') return bodyTheme;
    try {
        const saved = localStorage.getItem('ui_theme');
        if (saved === 'warm' || saved === 'ocean') return saved;
    } catch (e) {}
    if (window.__EARLY_UI_THEME === 'warm' || window.__EARLY_UI_THEME === 'ocean') return window.__EARLY_UI_THEME;
    if (window.SYSTEM_UI_THEME === 'warm' || window.SYSTEM_UI_THEME === 'ocean') return window.SYSTEM_UI_THEME;
    return 'ocean';
}

async function saveSystemTheme(theme) {
    const response = await fetch('/admin/settings/ui-theme', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json'
        },
        body: JSON.stringify({ theme })
    });

    const data = await response.json();
    if (!response.ok || !data.success) {
        throw new Error(data.error || '保存失败');
    }

    return data.theme || theme;
}

function updateThemeToggleButton(theme) {
    const openBtn = document.getElementById('openThemeSwitcherBtn');
    if (!openBtn) return;
    const targetLabel = theme === 'warm' ? '暖色' : '深色';
    openBtn.innerHTML = `<i data-lucide="palette" style="width: 15px; height: 15px;"></i> ${targetLabel}`;
    if (window.lucide) {
        lucide.createIcons();
    }
}

async function initThemeSwitcher() {
    const isAdmin = !!document.body?.classList.contains('admin-theme');
    applySystemTheme(getCurrentSystemTheme());

    if (!isAdmin) return;

    try {
        const response = await fetch('/admin/settings/ui-theme');
        const data = await response.json();
        if (response.ok && data.success) {
            applySystemTheme(data.theme);
        }
    } catch (error) {
        console.error('load ui theme failed:', error);
    }

    updateThemeToggleButton(getCurrentSystemTheme());

    const openBtn = document.getElementById('openThemeSwitcherBtn');
    if (!openBtn) return;

    openBtn.addEventListener('click', async () => {
        const current = getCurrentSystemTheme();
        const nextTheme = current === 'warm' ? 'ocean' : 'warm';
        try {
            const savedTheme = await saveSystemTheme(nextTheme);
            applySystemTheme(savedTheme);
            try { localStorage.setItem('ui_theme', savedTheme); } catch (e) {}
            updateThemeToggleButton(savedTheme);
            showToast(`已切换为${savedTheme === 'warm' ? '暖色' : '深色'}主题`, 'success');
        } catch (error) {
            showToast(error.message || '保存失败', 'error');
        }
    });
}


// Toast 提示函数
function showToast(message, type = 'info') {
    const toast = document.getElementById('toast');
    if (!toast) return;

    let icon = 'info';
    if (type === 'success') icon = 'check-circle';
    if (type === 'error') icon = 'alert-circle';

    toast.innerHTML = `<i data-lucide="${icon}"></i><span>${message}</span>`;
    toast.className = `toast ${type} show`;

    if (window.lucide) {
        lucide.createIcons();
    }

    setTimeout(() => {
        toast.classList.remove('show');
    }, 3000);
}

// 日期格式化函数
function formatDateTime(dateString) {
    if (!dateString) return '-';

    const date = new Date(dateString);
    const year = date.getFullYear();
    const month = String(date.getMonth() + 1).padStart(2, '0');
    const day = String(date.getDate()).padStart(2, '0');
    const hours = String(date.getHours()).padStart(2, '0');
    const minutes = String(date.getMinutes()).padStart(2, '0');

    return `${year}-${month}-${day} ${hours}:${minutes}`;
}

// 登出函数
async function logout() {
    if (!confirm('确定要登出吗?')) {
        return;
    }

    try {
        const response = await fetch('/auth/logout', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            }
        });

        const data = await response.json();

        if (response.ok && data.success) {
            window.location.href = '/login';
        } else {
            showToast('登出失败', 'error');
        }
    } catch (error) {
        showToast('网络错误', 'error');
    }
}

// API 调用封装
async function apiCall(url, options = {}) {
    try {
        const response = await fetch(url, {
            ...options,
            headers: {
                'Content-Type': 'application/json',
                ...options.headers
            }
        });

        const data = await response.json();

        if (!response.ok) {
            throw new Error(data.error || data.detail || '请求失败');
        }

        return { success: true, data };
    } catch (error) {
        return { success: false, error: error.message };
    }
}

// 确认对话框
function confirmAction(message) {
    return confirm(message);
}


function setSingleImportMode(mode = 'quick') {
    const quickSection = document.getElementById('oauthQuickSection');
    const manualSection = document.getElementById('manualTokenSection');
    if (!quickSection || !manualSection) return;

    const isManual = mode === 'manual';
    quickSection.style.display = isManual ? 'none' : 'block';
    manualSection.style.display = isManual ? 'block' : 'none';
}

// 页面加载完成后执行
document.addEventListener('DOMContentLoaded', function () {
    // 检查认证状态
    checkAuthStatus();

    cleanupLegacyThemeSettingsSection();
    initThemeSwitcher();

    // OAuth 一键导入按钮绑定（避免仅依赖内联 onclick）
    const btnOneClickToken = document.getElementById('btnOneClickToken');
    if (btnOneClickToken) {
        btnOneClickToken.addEventListener('click', () => {
            generateOAuthAuthorizeLink();
        });
    }

    const btnParseOAuthCallback = document.getElementById('btnParseOAuthCallback');
    if (btnParseOAuthCallback) {
        btnParseOAuthCallback.addEventListener('click', () => {
            parseOAuthCallbackAndFill();
        });
    }

    const btnExportOAuthJson = document.getElementById('btnExportOAuthJson');
    if (btnExportOAuthJson) {
        btnExportOAuthJson.addEventListener('click', () => {
            exportOAuthJsonTemplateFile();
        });
    }

    const switchToManualFill = document.getElementById('switchToManualFill');
    if (switchToManualFill) {
        switchToManualFill.addEventListener('click', () => setSingleImportMode('manual'));
    }

    const switchToQuickToken = document.getElementById('switchToQuickToken');
    if (switchToQuickToken) {
        switchToQuickToken.addEventListener('click', () => setSingleImportMode('quick'));
    }

    const chooseJsonFileBtn = document.getElementById('chooseJsonFileBtn');
    const jsonImportFile = document.getElementById('jsonImportFile');
    if (chooseJsonFileBtn && jsonImportFile) {
        chooseJsonFileBtn.addEventListener('click', () => jsonImportFile.click());
        jsonImportFile.addEventListener('change', async () => {
            const fileNameNode = document.getElementById('jsonImportFileName');
            if (fileNameNode) {
                fileNameNode.textContent = jsonImportFile.files && jsonImportFile.files[0]
                    ? `已选择：${jsonImportFile.files[0].name}`
                    : '支持单对象、对象数组，或 {"teams": [...]} 格式';
            }
            if (jsonImportFile.files && jsonImportFile.files.length > 0) {
                await handleJsonFileImport();
            }
        });
    }

    setSingleImportMode('quick');
});

// 检查认证状态
async function checkAuthStatus() {
    // 如果在登录页面,跳过检查
    if (window.location.pathname === '/login') {
        return;
    }

    try {
        const response = await fetch('/auth/status');
        const data = await response.json();

        if (!data.authenticated && window.location.pathname.startsWith('/admin')) {
            // 未登录且在管理员页面,跳转到登录页
            window.location.href = '/login';
        }
    } catch (error) {
        console.error('检查认证状态失败:', error);
    }
}

// === 模态框控制逻辑 ===

function showModal(modalId) {
    const modal = document.getElementById(modalId);
    if (modal) {
        modal.classList.add('show');
        document.body.style.overflow = 'hidden'; // 防止背景滚动

        if (modalId === 'importTeamModal') {
            setSingleImportMode('quick');
        }
    }
}

function hideModal(modalId) {
    const modal = document.getElementById(modalId);
    if (modal) {
        modal.classList.remove('show');
        document.body.style.overflow = '';
    }
}

function switchModalTab(modalId, tabId) {
    const modal = document.getElementById(modalId);
    if (!modal) return;

    // 切换按钮状态
    const tabs = modal.querySelectorAll('.modal-tab-btn');
    tabs.forEach(tab => {
        if (tab.getAttribute('onclick').includes(`'${tabId}'`)) {
            tab.classList.add('active');
        } else {
            tab.classList.remove('active');
        }
    });

    // 切换面板显示
    const panels = modal.querySelectorAll('.import-panel, .card-body');
    panels.forEach(panel => {
        if (panel.id === tabId) {
            panel.style.display = 'block';
        } else {
            panel.style.display = 'none';
        }
    });
}

/**
 * 切换质保时长输入框的显示
 */
function toggleWarrantyDays(checkbox, targetId) {
    const target = document.getElementById(targetId);
    if (target) {
        target.style.display = checkbox.checked ? 'block' : 'none';
    }
}

// === Team 导入逻辑 ===



async function copyTextSilently(text) {
    if (!text) return false;
    try {
        if (navigator.clipboard && window.isSecureContext) {
            await navigator.clipboard.writeText(text);
            return true;
        }
    } catch (err) {
        console.error('silent copy failed:', err);
    }

    try {
        const textArea = document.createElement('textarea');
        textArea.value = text;
        textArea.style.position = 'fixed';
        textArea.style.left = '-9999px';
        textArea.style.top = '0';
        document.body.appendChild(textArea);
        textArea.focus();
        textArea.select();
        const ok = document.execCommand('copy');
        document.body.removeChild(textArea);
        return ok;
    } catch (err) {
        console.error('silent fallback copy failed:', err);
        return false;
    }
}

function unwrapApiPayload(result) {
    if (!result || !result.success) return null;
    const body = result.data || {};
    if (body && typeof body === 'object' && body.data && typeof body.data === 'object') {
        return body.data;
    }
    return body;
}

let oauthDraft = {
    codeVerifier: '',
    state: '',
    clientId: ''
};

let oauthParsedCache = null;
let oauthParsedCacheKey = '';

async function generateOAuthAuthorizeLink() {
    const form = document.getElementById('singleImportForm');
    if (!form) return;

    const formClientId = form.clientId ? form.clientId.value.trim() : '';
    const defaultClientId = 'app_EMoamEEZ73f0CkXaXp7hrann';
    const clientId = formClientId || defaultClientId;

    showToast('正在生成并复制授权链接...', 'info');

    try {
        const result = await apiCall('/admin/oauth/openai/authorize', {
            method: 'POST',
            body: JSON.stringify({
                client_id: clientId,
                redirect_uri: 'http://localhost:1455/auth/callback'
            })
        });

        if (!result.success) {
            showToast(result.error || '生成授权链接失败', 'error');
            return;
        }

        const data = unwrapApiPayload(result) || {};
        oauthDraft.codeVerifier = data.code_verifier || '';
        oauthDraft.state = data.state || '';
        oauthDraft.clientId = data.client_id || clientId;
        oauthParsedCache = null;
        oauthParsedCacheKey = '';

        document.getElementById('oauthAuthorizeUrlOutput').value = data.authorize_url || '';
        if (form.clientId) form.clientId.value = oauthDraft.clientId;

        const authUrl = (data.authorize_url || '').trim();
        if (!authUrl) {
            showToast('授权链接生成失败，请重试', 'error');
            return;
        }

        const copied = await copyTextSilently(authUrl);
        if (copied) {
            showToast('链接已复制，去浏览器登录后粘贴回调', 'success');
        } else {
            showToast('授权链接已生成，请手动复制', 'warning');
        }
    } catch (error) {
        showToast('生成授权链接失败', 'error');
    }
}

async function parseOAuthCallbackData(forceRefresh = false) {
    const callbackText = document.getElementById('oauthCallbackInput').value.trim();
    const form = document.getElementById('singleImportForm');

    if (!callbackText) {
        throw new Error('请先粘贴回调 URL');
    }

    if (!forceRefresh && oauthParsedCache && oauthParsedCacheKey === callbackText) {
        return oauthParsedCache;
    }

    const result = await apiCall('/admin/oauth/openai/parse-callback', {
        method: 'POST',
        body: JSON.stringify({
            callback_text: callbackText,
            code_verifier: oauthDraft.codeVerifier || null,
            expected_state: oauthDraft.state || null,
            client_id: ((form.clientId ? form.clientId.value.trim() : '') || oauthDraft.clientId || 'app_EMoamEEZ73f0CkXaXp7hrann'),
            redirect_uri: 'http://localhost:1455/auth/callback'
        })
    });

    if (!result.success) {
        throw new Error(result.error || '解析回调失败');
    }

    const parsed = unwrapApiPayload(result) || {};
    oauthParsedCache = parsed;
    oauthParsedCacheKey = callbackText;
    return parsed;
}

function decodeJwtPayload(token) {
    if (!token || typeof token !== 'string') return null;
    const parts = token.split('.');
    if (parts.length < 2) return null;

    try {
        const base64Url = parts[1];
        const base64 = base64Url.replace(/-/g, '+').replace(/_/g, '/');
        const padded = base64.padEnd(base64.length + (4 - base64.length % 4) % 4, '=');
        const jsonText = decodeURIComponent(escape(window.atob(padded)));
        return JSON.parse(jsonText);
    } catch (error) {
        return null;
    }
}

function toIsoStringWithOffset8(dateObj) {
    if (!(dateObj instanceof Date) || Number.isNaN(dateObj.getTime())) return '';
    const shifted = new Date(dateObj.getTime() + 8 * 60 * 60 * 1000);
    const iso = shifted.toISOString().replace('Z', '+08:00');
    return iso.slice(0, 19) + '+08:00';
}

function buildOAuthJsonTemplate(parsedData) {
    const accessToken = parsedData.access_token || '';
    const refreshToken = parsedData.refresh_token || '';
    const raw = parsedData.raw || {};
    const idToken = raw.id_token || parsedData.id_token || '';

    const accessPayload = decodeJwtPayload(accessToken) || {};
    const idPayload = decodeJwtPayload(idToken) || {};
    const accessAuth = accessPayload['https://api.openai.com/auth'] || {};
    const accessProfile = accessPayload['https://api.openai.com/profile'] || {};
    const idAuth = idPayload['https://api.openai.com/auth'] || {};

    const accountId = raw.account_id || parsedData.account_id || accessAuth.chatgpt_account_id || idAuth.chatgpt_account_id || '';
    const email = raw.email || parsedData.email || accessProfile.email || idPayload.email || '';
    const exp = accessPayload.exp ? new Date(accessPayload.exp * 1000) : null;
    const expired = raw.expired || parsedData.expired || (exp ? toIsoStringWithOffset8(exp) : '');

    return {
        access_token: accessToken,
        account_id: accountId,
        email,
        expired,
        id_token: idToken,
        last_refresh: raw.last_refresh || parsedData.last_refresh || toIsoStringWithOffset8(new Date()),
        refresh_token: refreshToken,
        type: raw.type || parsedData.type || 'codex'
    };
}

function downloadJsonFile(payload, filename) {
    const text = JSON.stringify(payload, null, 2);
    const blob = new Blob([text], { type: 'application/json;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
}

async function exportOAuthJsonTemplateFile() {
    try {
        const data = oauthParsedCache || await parseOAuthCallbackData();
        const payload = buildOAuthJsonTemplate(data);
        const filename = `team-oauth-${Date.now()}.json`;
        downloadJsonFile(payload, filename);
        showToast('JSON 文件已导出', 'success');
    } catch (error) {
        showToast(error.message || '导出 JSON 失败', 'error');
    }
}

async function parseOAuthCallbackAndFill() {
    const form = document.getElementById('singleImportForm');

    try {
        const data = await parseOAuthCallbackData(true);
        if (form.accessToken && data.access_token) form.accessToken.value = data.access_token;
        if (form.refreshToken && data.refresh_token) form.refreshToken.value = data.refresh_token;
        if (form.clientId && data.client_id) form.clientId.value = data.client_id;

        showToast('已自动填充 Token 信息，请确认后导入', 'success');
    } catch (error) {
        showToast(error.message || '解析回调失败', 'error');
    }
}


async function handleSingleImport(event) {
    event.preventDefault();
    const form = event.target;
    const accessToken = form.accessToken.value.trim();
    const refreshToken = form.refreshToken ? form.refreshToken.value.trim() : null;
    const sessionToken = form.sessionToken ? form.sessionToken.value.trim() : null;
    const clientId = form.clientId ? form.clientId.value.trim() : null;
    const email = form.email.value.trim();
    const accountId = form.accountId.value.trim();
    const submitButton = form.querySelector('button[type="submit"]');

    submitButton.disabled = true;
    submitButton.textContent = '导入中...';

    try {
        const result = await apiCall('/admin/teams/import', {
            method: 'POST',
            body: JSON.stringify({
                import_type: 'single',
                access_token: accessToken,
                refresh_token: refreshToken || null,
                session_token: sessionToken || null,
                client_id: clientId || null,
                email: email || null,
                account_id: accountId || null,
                pool_type: getCurrentPoolType()
            })
        });

        if (result.success) {
            showToast('Team 导入成功！', 'success');
            form.reset();
            setTimeout(() => location.reload(), 1500);
        } else {
            showToast(result.error || '导入失败', 'error');
        }
    } catch (error) {
        showToast('网络错误', 'error');
    } finally {
        submitButton.disabled = false;
        submitButton.textContent = '导入';
    }
}

async function handleBatchImport(event) {
    event.preventDefault();
    const form = event.target;
    const batchContent = (form.batchContent && form.batchContent.value ? form.batchContent.value.trim() : "");
    const submitButton = form.querySelector('button[type="submit"]');

    if (!batchContent) {
        showToast('请输入批量导入内容', 'error');
        return;
    }

    // UI 元素
    const progressContainer = document.getElementById('batchProgressContainer');
    const progressBar = document.getElementById('batchProgressBar');
    const progressStage = document.getElementById('batchProgressStage');
    const progressPercent = document.getElementById('batchProgressPercent');
    const successCountEl = document.getElementById('batchSuccessCount');
    const failedCountEl = document.getElementById('batchFailedCount');
    const resultsContainer = document.getElementById('batchResultsContainer');
    const resultsDiv = document.getElementById('batchResults');
    const finalSummaryEl = document.getElementById('batchFinalSummary');

    // 重置 UI
    progressContainer.style.display = 'block';
    resultsContainer.style.display = 'none';
    progressBar.style.width = '0%';
    progressStage.textContent = '准备导入...';
    progressPercent.textContent = '0%';
    successCountEl.textContent = '0';
    failedCountEl.textContent = '0';
    resultsDiv.innerHTML = '<table class="data-table"><thead><tr><th>邮箱</th><th>状态</th><th>消息</th></tr></thead><tbody id="batchResultsBody"></tbody></table>';
    const resultsBody = document.getElementById('batchResultsBody');

    submitButton.disabled = true;
    submitButton.textContent = '导入中...';

    try {
        const response = await fetch('/admin/teams/import', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                import_type: 'batch',
                content: batchContent,
                pool_type: getCurrentPoolType()
            })
        });

        if (!response.ok) {
            let msg = '请求失败';
            try {
                const errorData = await response.json();
                msg = errorData.error || errorData.detail || msg;
            } catch (_) {
                const errorText = await response.text();
                if (errorText) msg = errorText.slice(0, 200);
            }
            throw new Error(msg);
        }

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';

        const processStreamLine = (line) => {
            if (!line || !line.trim()) return;
            try {
                const trimmed = line.trim();
                if (!trimmed.startsWith('{')) return;
                const data = JSON.parse(trimmed);

                if (data.type === 'start') {
                    progressStage.textContent = `开始导入 (共 ${data.total} 条)...`;
                    // 给用户即时反馈，避免看起来一直卡在 0%
                    progressBar.style.width = '5%';
                    progressPercent.textContent = '5%';
                } else if (data.type === 'progress') {
                    const percent = Math.round((data.current / data.total) * 100);
                    progressBar.style.width = `${percent}%`;
                    progressPercent.textContent = `${percent}%`;
                    progressStage.textContent = `正在导入 ${data.current}/${data.total}...`;
                    successCountEl.textContent = data.success_count;
                    failedCountEl.textContent = data.failed_count;

                    if (data.last_result) {
                        resultsContainer.style.display = 'block';
                        const res = data.last_result;
                        const statusClass = res.success ? 'text-success' : 'text-danger';
                        const statusText = res.success ? '成功' : '失败';
                        const row = document.createElement('tr');
                        row.innerHTML = `
                            <td>${res.email}</td>
                            <td class="${statusClass}">${statusText}</td>
                            <td>${res.success ? (res.message || '导入成功') : res.error}</td>
                        `;
                        resultsBody.insertBefore(row, resultsBody.firstChild);
                    }
                } else if (data.type === 'finish') {
                    progressStage.textContent = '导入完成';
                    progressBar.style.width = '100%';
                    progressPercent.textContent = '100%';
                    finalSummaryEl.textContent = `总数: ${data.total} | 成功: ${data.success_count} | 失败: ${data.failed_count}`;

                    if (data.failed_count === 0) {
                        showToast('全部导入成功！', 'success');
                    } else {
                        showToast(`导入完成，成功 ${data.success_count} 条，失败 ${data.failed_count} 条`, 'warning');
                    }

                    if (data.success_count > 0) {
                        setTimeout(() => location.reload(), 3000);
                    }
                } else if (data.type === 'error') {
                    showToast(data.error, 'error');
                }
            } catch (e) {
                console.error('解析流数据失败:', e, line);
            }
        };

        while (true) {
            const { value, done } = await reader.read();
            if (done) {
                // 处理最后一段可能没有 \n 结尾的残余数据
                if (buffer && buffer.trim()) {
                    processStreamLine(buffer);
                }
                break;
            }

            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split('\n');
            buffer = lines.pop() || '';

            for (const line of lines) {
                processStreamLine(line);
            }
        }
    } catch (error) {
        showToast(error.message || '网络错误', 'error');
    } finally {
        submitButton.disabled = false;
        submitButton.textContent = '批量导入';
    }
}

async function handleJsonFileImport() {
    const fileInput = document.getElementById('jsonImportFile');
    const form = document.getElementById('batchImportForm');
    const submitButton = form ? form.querySelector('button[type="submit"]') : null;

    if (!fileInput || !fileInput.files || fileInput.files.length === 0) {
        showToast('请先选择 JSON 文件', 'error');
        return;
    }

    const file = fileInput.files[0];
    let content = '';
    try {
        content = await file.text();
        JSON.parse(content);
    } catch (error) {
        showToast('JSON 文件格式无效', 'error');
        return;
    }

    if (submitButton) {
        submitButton.disabled = true;
        submitButton.textContent = 'JSON 导入中...';
    }

    // UI 元素
    const progressContainer = document.getElementById('batchProgressContainer');
    const progressBar = document.getElementById('batchProgressBar');
    const progressStage = document.getElementById('batchProgressStage');
    const progressPercent = document.getElementById('batchProgressPercent');
    const successCountEl = document.getElementById('batchSuccessCount');
    const failedCountEl = document.getElementById('batchFailedCount');
    const resultsContainer = document.getElementById('batchResultsContainer');
    const resultsDiv = document.getElementById('batchResults');
    const finalSummaryEl = document.getElementById('batchFinalSummary');

    // 重置 UI
    progressContainer.style.display = 'block';
    resultsContainer.style.display = 'none';
    progressBar.style.width = '0%';
    progressStage.textContent = '准备 JSON 导入...';
    progressPercent.textContent = '0%';
    successCountEl.textContent = '0';
    failedCountEl.textContent = '0';
    resultsDiv.innerHTML = '<table class="data-table"><thead><tr><th>邮箱</th><th>状态</th><th>消息</th></tr></thead><tbody id="batchResultsBody"></tbody></table>';
    const resultsBody = document.getElementById('batchResultsBody');

    try {
        const response = await fetch('/admin/teams/import', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                import_type: 'json',
                content,
                pool_type: getCurrentPoolType()
            })
        });

        if (!response.ok) {
            let msg = '请求失败';
            try {
                const errorData = await response.json();
                msg = errorData.error || errorData.detail || msg;
            } catch (_) {
                const errorText = await response.text();
                if (errorText) msg = errorText.slice(0, 200);
            }
            throw new Error(msg);
        }

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';

        const processStreamLine = (line) => {
            if (!line || !line.trim()) return;
            try {
                const trimmed = line.trim();
                if (!trimmed.startsWith('{')) return;
                const data = JSON.parse(trimmed);

                if (data.type === 'start') {
                    progressStage.textContent = `开始导入 (共 ${data.total} 条)...`;
                    // 让用户看到实时变化，避免看起来一直 0%
                    progressBar.style.width = '5%';
                    progressPercent.textContent = '5%';
                } else if (data.type === 'progress') {
                    const percent = Math.round((data.current / data.total) * 100);
                    progressBar.style.width = `${percent}%`;
                    progressPercent.textContent = `${percent}%`;
                    progressStage.textContent = `正在导入 ${data.current}/${data.total}...`;
                    successCountEl.textContent = data.success_count;
                    failedCountEl.textContent = data.failed_count;

                    if (data.last_result) {
                        resultsContainer.style.display = 'block';
                        const res = data.last_result;
                        const statusClass = res.success ? 'text-success' : 'text-danger';
                        const statusText = res.success ? '成功' : '失败';
                        const row = document.createElement('tr');
                        row.innerHTML = `
                            <td>${res.email}</td>
                            <td class="${statusClass}">${statusText}</td>
                            <td>${res.success ? (res.message || '导入成功') : res.error}</td>
                        `;
                        resultsBody.insertBefore(row, resultsBody.firstChild);
                    }
                } else if (data.type === 'finish') {
                    progressStage.textContent = '导入完成';
                    progressBar.style.width = '100%';
                    progressPercent.textContent = '100%';
                    finalSummaryEl.textContent = `总数: ${data.total} | 成功: ${data.success_count} | 失败: ${data.failed_count}`;

                    if (data.failed_count === 0) {
                        showToast('全部导入成功！', 'success');
                    } else {
                        showToast(`导入完成，成功 ${data.success_count} 条，失败 ${data.failed_count} 条`, 'warning');
                    }

                    if (data.success_count > 0) {
                        setTimeout(() => location.reload(), 3000);
                    }
                } else if (data.type === 'error') {
                    showToast(data.error, 'error');
                }
            } catch (e) {
                console.error('解析流数据失败:', e, line);
            }
        };

        while (true) {
            const { value, done } = await reader.read();
            if (done) {
                if (buffer && buffer.trim()) {
                    processStreamLine(buffer);
                }
                break;
            }

            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split('\n');
            buffer = lines.pop() || '';

            for (const line of lines) {
                processStreamLine(line);
            }
        }
    } catch (error) {
        showToast(error.message || '网络错误', 'error');
    } finally {
        if (submitButton) {
            submitButton.disabled = false;
            submitButton.textContent = '批量导入';
        }
    }
}

// === 兑换码生成逻辑 ===

async function generateSingle(event) {
    event.preventDefault();
    const form = event.target;
    const customCode = form.customCode.value.trim();
    const expiresDays = form.expiresDays.value;
    const hasWarranty = form.hasWarranty.checked;
    const warrantyDays = form.warrantyDays ? form.warrantyDays.value : 30;

    const data = {
        type: 'single',
        has_warranty: hasWarranty,
        warranty_days: parseInt(warrantyDays || 30)
    };
    if (customCode) data.code = customCode;
    if (expiresDays) data.expires_days = parseInt(expiresDays);

    const result = await apiCall('/admin/codes/generate', {
        method: 'POST',
        body: JSON.stringify(data)
    });

    if (result.success) {
        document.getElementById('generatedCode').textContent = result.data.code;
        document.getElementById('singleResult').style.display = 'block';
        form.reset();
        showToast('兑换码生成成功', 'success');
        // 如果在列表中，延迟刷新
        if (window.location.pathname === '/admin/codes') {
            setTimeout(() => location.reload(), 2000);
        }
    } else {
        showToast(result.error || '生成失败', 'error');
    }
}

async function generateBatch(event) {
    event.preventDefault();
    const form = event.target;
    const count = parseInt(form.count.value);
    const expiresDays = form.expiresDays.value;
    const hasWarranty = form.hasWarranty.checked;
    const warrantyDays = form.warrantyDays ? form.warrantyDays.value : 30;

    if (count < 1 || count > 1000) {
        showToast('生成数量必须在1-1000之间', 'error');
        return;
    }

    const data = {
        type: 'batch',
        count: count,
        has_warranty: hasWarranty,
        warranty_days: parseInt(warrantyDays || 30)
    };
    if (expiresDays) data.expires_days = parseInt(expiresDays);

    const result = await apiCall('/admin/codes/generate', {
        method: 'POST',
        body: JSON.stringify(data)
    });

    if (result.success) {
        document.getElementById('batchTotal').textContent = result.data.total;
        document.getElementById('batchCodes').value = result.data.codes.join('\n');
        document.getElementById('batchResult').style.display = 'block';
        form.reset();
        showToast(`成功生成 ${result.data.total} 个兑换码`, 'success');
        if (window.location.pathname === '/admin/codes') {
            setTimeout(() => location.reload(), 3000);
        }
    } else {
        showToast(result.error || '生成失败', 'error');
    }
}

// 统一复制到剪贴板函数
async function copyToClipboard(text) {
    if (!text) return;

    try {
        // 尝试使用 Modern Clipboard API
        if (navigator.clipboard && window.isSecureContext) {
            await navigator.clipboard.writeText(text);
            showToast('已复制到剪贴板', 'success');
            return true;
        }
    } catch (err) {
        console.error('Modern copy failed:', err);
    }

    // Fallback: 使用 textarea 方式
    try {
        const textArea = document.createElement("textarea");
        textArea.value = text;

        // 确保 textarea 不可见且不影响布局
        textArea.style.position = "fixed";
        textArea.style.left = "-9999px";
        textArea.style.top = "0";
        textArea.style.opacity = "0";
        document.body.appendChild(textArea);

        textArea.focus();
        textArea.select();

        const successful = document.execCommand('copy');
        document.body.removeChild(textArea);

        if (successful) {
            showToast('已复制到剪贴板', 'success');
            return true;
        }
    } catch (err) {
        console.error('Fallback copy failed:', err);
    }

    showToast('复制失败', 'error');
    return false;
}

// === 辅助函数 ===

function copyCode(code) {
    // 如果没有传入 code，尝试从生成结果中获取
    if (!code) {
        const generatedCodeEl = document.getElementById('generatedCode');
        code = generatedCodeEl ? generatedCodeEl.textContent : '';
    }

    if (code) {
        copyToClipboard(code);
    } else {
        showToast('无内容可复制', 'error');
    }
}

function copyBatchCodes() {
    const codes = document.getElementById('batchCodes').value;
    copyToClipboard(codes);
}

function copyWelfareCode() {
    const el = document.getElementById('welfareCommonCodeValue');
    const code = el ? String(el.value || '').trim() : '';
    if (!code || code === '-') {
        showToast('暂无可复制的通用兑换码', 'warning');
        return;
    }
    copyToClipboard(code);
}

function downloadCodes() {
    const codes = document.getElementById('batchCodes').value;
    const blob = new Blob([codes], { type: 'text/plain' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `redemption_codes_${new Date().getTime()}.txt`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    showToast('下载成功', 'success');
}
// === 成员管理逻辑 ===

async function viewMembers(teamId, teamEmail = '') {
    window.currentTeamId = teamId;
    const modal = document.getElementById('manageMembersModal');
    if (!modal) return;

    // 设置基本信息
    document.getElementById('modalTeamEmail').textContent = teamEmail;

    // 打开模态框
    showModal('manageMembersModal');

    // 加载成员列表
    await loadModalMemberList(teamId);
}

async function loadModalMemberList(teamId) {
    const joinedTableBody = document.getElementById('modalJoinedMembersTableBody');
    const invitedTableBody = document.getElementById('modalInvitedMembersTableBody');

    if (joinedTableBody) joinedTableBody.innerHTML = '<tr><td colspan="4" style="text-align: center; padding: 2rem;">加载中...</td></tr>';
    if (invitedTableBody) invitedTableBody.innerHTML = '<tr><td colspan="4" style="text-align: center; padding: 2rem;">加载中...</td></tr>';

    try {
        const result = await apiCall(`/admin/teams/${teamId}/members/list`);
        if (result.success) {
            const allMembers = result.data.members || [];
            const joinedMembers = allMembers.filter(m => m.status === 'joined');
            const invitedMembers = allMembers.filter(m => m.status === 'invited');

            // 渲染已加入成员
            if (joinedTableBody) {
                if (joinedMembers.length === 0) {
                    joinedTableBody.innerHTML = '<tr><td colspan="4" style="text-align: center; padding: 1.5rem; color: var(--text-muted);">暂无已加入成员</td></tr>';
                } else {
                    joinedTableBody.innerHTML = joinedMembers.map(m => `
                        <tr>
                            <td>${m.email}</td>
                            <td>
                                <span class="role-badge role-${m.role}">
                                    ${m.role === 'account-owner' ? '所有者' : '成员'}
                                </span>
                            </td>
                            <td>${formatDateTime(m.added_at)}</td>
                            <td style="text-align: right;">
                                ${m.role !== 'account-owner' ? `
                                    <button onclick="deleteMember('${teamId}', '${m.user_id}', '${m.email}', true)" class="btn btn-sm btn-danger">
                                        <i data-lucide="trash-2"></i> 删除
                                    </button>
                                ` : '<span class="text-muted">不可删除</span>'}
                            </td>
                        </tr>
                    `).join('');
                }
            }

            // 渲染待加入成员
            if (invitedTableBody) {
                if (invitedMembers.length === 0) {
                    invitedTableBody.innerHTML = '<tr><td colspan="4" style="text-align: center; padding: 1.5rem; color: var(--text-muted);">暂无待加入成员</td></tr>';
                } else {
                    invitedTableBody.innerHTML = invitedMembers.map(m => `
                        <tr>
                            <td>${m.email}</td>
                            <td>
                                <span class="role-badge role-${m.role}">成员</span>
                            </td>
                            <td>${formatDateTime(m.added_at)}</td>
                            <td style="text-align: right;">
                                <button onclick="revokeInvite('${teamId}', '${m.email}', true)" class="btn btn-sm btn-warning">
                                    <i data-lucide="undo"></i> 撤回
                                </button>
                            </td>
                        </tr>
                    `).join('');
                }
            }

            if (window.lucide) lucide.createIcons();
        } else {
            const errorMsg = `<tr><td colspan="4" style="text-align: center; color: var(--danger);">${result.error}</td></tr>`;
            if (joinedTableBody) joinedTableBody.innerHTML = errorMsg;
            if (invitedTableBody) invitedTableBody.innerHTML = errorMsg;
        }
    } catch (error) {
        const errorMsg = '<tr><td colspan="4" style="text-align: center; color: var(--danger);">加载失败</td></tr>';
        if (joinedTableBody) joinedTableBody.innerHTML = errorMsg;
        if (invitedTableBody) invitedTableBody.innerHTML = errorMsg;
    }
}

async function revokeInvite(teamId, email, inModal = false) {
    if (!confirm(`确定要撤回对 "${email}" 的邀请吗？`)) {
        return;
    }

    try {
        showToast('正在撤回...', 'info');
        const result = await apiCall(`/admin/teams/${teamId}/invites/revoke`, {
            method: 'POST',
            body: JSON.stringify({ email: email })
        });

        if (result.success) {
            showToast('撤回成功', 'success');
            if (inModal) {
                await loadModalMemberList(teamId);
            } else {
                setTimeout(() => location.reload(), 1000);
            }
        } else {
            showToast(result.error || '撤回失败', 'error');
        }
    } catch (error) {
        showToast('网络错误', 'error');
    }
}

async function handleAddMember(event) {
    event.preventDefault();
    const form = event.target;
    const email = form.email.value.trim();
    const submitButton = document.getElementById('addMemberSubmitBtn');
    const teamId = window.currentTeamId;

    if (!teamId) {
        showToast('无法获取 Team ID', 'error');
        return;
    }

    submitButton.disabled = true;
    const originalText = submitButton.innerHTML;
    submitButton.textContent = '添加中...';

    try {
        const result = await apiCall(`/admin/teams/${teamId}/members/add`, {
            method: 'POST',
            body: JSON.stringify({ email })
        });

        if (result.success) {
            showToast('成员添加成功！正在刷新列表...', 'success');
            form.reset();

            if (document.getElementById('manageMembersModal').classList.contains('show')) {
                await loadModalMemberList(teamId);
            }

            setTimeout(() => {
                window.location.reload();
            }, 800);
        } else {
            showToast(result.error || '添加失败', 'error');
        }
    } catch (error) {
        showToast('网络错误', 'error');
    } finally {
        submitButton.disabled = false;
        submitButton.innerHTML = originalText;
    }
}

async function deleteMember(teamId, userId, email, inModal = false) {
    if (!confirm(`确定要删除成员 "${email}" 吗?\n\n此操作不可恢复!`)) {
        return;
    }

    try {
        showToast('正在删除...', 'info');
        const result = await apiCall(`/admin/teams/${teamId}/members/${userId}/delete`, {
            method: 'POST'
        });

        if (result.success) {
            showToast('删除成功', 'success');
            if (inModal) {
                await loadModalMemberList(teamId);
            } else {
                setTimeout(() => location.reload(), 1000);
            }
        } else {
            showToast(result.error || '删除失败', 'error');
        }
    } catch (error) {
        showToast('网络错误', 'error');
    }
}


// 确保内联 onclick 在任何加载模式下都可调用
if (typeof window !== 'undefined') {
    window.generateOAuthAuthorizeLink = generateOAuthAuthorizeLink;
    window.parseOAuthCallbackAndFill = parseOAuthCallbackAndFill;
    window.exportOAuthJsonTemplateFile = exportOAuthJsonTemplateFile;
    window.handleJsonFileImport = handleJsonFileImport;
    window.copyWelfareCode = copyWelfareCode;
}


async function generateWelfareCode() {
    try {
        const btn = document.getElementById('generateWelfareCodeBtn');
        if (btn) { btn.disabled = true; }
        const result = await apiCall('/admin/welfare/code/generate', { method: 'POST' });
        if (!result.success) throw new Error(result.error || '生成失败');

        const codeValueEl = document.getElementById('welfareCommonCodeValue');
        const newCode = result.code || '';
        if (codeValueEl) codeValueEl.value = newCode;

        const codeTextEl = document.getElementById('welfareCommonCodeText');
        if (codeTextEl) { codeTextEl.textContent = newCode || '-'; codeTextEl.title = newCode || ''; }

        const usageTextEl = document.getElementById('welfareCodeUsageText');
        if (usageTextEl) {
            const used = typeof result.used === 'number' ? result.used : 0;
            const limit = typeof result.limit === 'number' ? result.limit : 0;
            const remaining = typeof result.remaining === 'number' ? result.remaining : Math.max(limit - used, 0);
            usageTextEl.textContent = `剩余次数 ${remaining} / ${limit}`;
        }

        const copyBtn = document.getElementById('copyWelfareCodeBtn');
        if (copyBtn) copyBtn.disabled = !result.code;

        await copyToClipboard(result.code || '');
        showToast(`通用兑换码已更新并复制，剩余次数 ${result.remaining}/${result.limit}`, 'success');
    } catch (error) {
        showToast(error.message || '生成通用兑换码失败', 'error');
    } finally {
        const btn = document.getElementById('generateWelfareCodeBtn');
        if (btn) btn.disabled = false;
    }
}
