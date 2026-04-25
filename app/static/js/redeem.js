// 用户兑换页面JavaScript

// HTML转义函数 - 防止XSS攻击
function escapeHtml(unsafe) {
    if (unsafe === null || unsafe === undefined) {
        return '';
    }
    return String(unsafe)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
}

// 全局变量
let currentEmail = '';
let currentCode = '';
let currentTopTab = 'redeem';
let isRedeeming = false;
let isCheckingWarranty = false;
let lastAnnouncementTrigger = null;
let lastRenewalReminderTrigger = null;
let pendingRenewalReminderResolver = null;

// Toast提示函数
function showToast(message, type = 'info') {
    const toast = document.getElementById('toast');
    if (!toast) return;

    let icon = 'info';
    if (type === 'success') icon = 'check-circle';
    if (type === 'error') icon = 'alert-circle';

    toast.innerHTML = `<i data-lucide="${icon}"></i><span>${escapeHtml(message)}</span>`;
    toast.className = `toast ${type} show`;

    if (window.lucide) {
        lucide.createIcons();
    }

    setTimeout(() => {
        toast.classList.remove('show');
    }, 3000);
}


function escapeForHtml(text) {
    return String(text || '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
}

function renderMarkdownSafe(markdownText) {
    const escaped = escapeForHtml(markdownText || '');
    const lines = escaped.split(/\r?\n/);
    let html = '';
    let inList = false;

    const applyInline = (line) => line
        .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
        .replace(/\*(.+?)\*/g, '<em>$1</em>')
        .replace(/`([^`]+)`/g, '<code>$1</code>')
        .replace(/\[(.+?)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');

    for (const rawLine of lines) {
        const line = rawLine.trim();

        if (!line) {
            if (inList) {
                html += '</ul>';
                inList = false;
            }
            continue;
        }

        const heading = line.match(/^(#{1,3})\s+(.*)$/);
        if (heading) {
            if (inList) {
                html += '</ul>';
                inList = false;
            }
            const level = heading[1].length;
            html += `<h${level}>${applyInline(heading[2])}</h${level}>`;
            continue;
        }

        const bullet = line.match(/^[-*]\s+(.*)$/);
        if (bullet) {
            if (!inList) {
                html += '<ul>';
                inList = true;
            }
            html += `<li>${applyInline(bullet[1])}</li>`;
            continue;
        }

        if (inList) {
            html += '</ul>';
            inList = false;
        }
        html += `<p>${applyInline(line)}</p>`;
    }

    if (inList) html += '</ul>';
    return html || '<p>暂无公告内容</p>';
}

function extractErrorText(payload) {
    if (payload === null || payload === undefined) return '';
    if (typeof payload === 'string') return payload;

    if (Array.isArray(payload)) {
        return payload
            .map(item => {
                if (!item) return '';
                if (typeof item === 'string') return item;
                if (item.msg) return String(item.msg);
                if (item.detail !== undefined) return extractErrorText(item.detail);
                try {
                    return JSON.stringify(item);
                } catch (_) {
                    return String(item);
                }
            })
            .filter(Boolean)
            .join('；');
    }

    if (typeof payload === 'object') {
        if (payload.detail !== undefined) return extractErrorText(payload.detail);
        if (payload.error !== undefined) return extractErrorText(payload.error);
        if (payload.message !== undefined) return extractErrorText(payload.message);
        if (payload.msg !== undefined) return extractErrorText(payload.msg);
        if (payload.reason !== undefined) return extractErrorText(payload.reason);
        try {
            return JSON.stringify(payload);
        } catch (_) {
            return String(payload);
        }
    }

    return String(payload);
}

function normalizeRawErrorMessage(rawMessage) {
    let message = extractErrorText(rawMessage).trim();
    if (!message) return '';

    for (let i = 0; i < 2; i++) {
        const trimmed = message.trim();
        if (!trimmed || (trimmed[0] !== '{' && trimmed[0] !== '[')) break;

        try {
            const parsed = JSON.parse(trimmed);
            const extracted = extractErrorText(parsed).trim();
            if (!extracted || extracted === trimmed) break;
            message = extracted;
        } catch (_) {
            break;
        }
    }

    return message.replace(/\s+/g, ' ').trim();
}

function isTechnicalLogMessage(message) {
    const normalized = String(message || '').trim();
    if (!normalized) return false;

    const lower = normalized.toLowerCase();
    if (normalized.length > 220) return true;

    const technicalKeywords = [
        'traceback',
        'exception',
        'stack',
        'sqlalchemy',
        'asyncsession',
        'httpx',
        'aiohttp',
        'error_code',
        'status_code',
        'file "',
        'line ',
        'detail:',
        'db_session',
        'invite_res',
        'redeem_flow'
    ];

    return technicalKeywords.some(keyword => lower.includes(keyword));
}

function getFriendlyRedeemErrorMessage(rawMessage, statusCode = 0) {
    const message = normalizeRawErrorMessage(rawMessage);
    const lower = message.toLowerCase();
    const includesAny = (...keywords) => keywords.some(keyword => lower.includes(String(keyword).toLowerCase()));

    if (includesAny('兑换失败次数过多')) {
        const marker = '最后报错:';
        const index = message.lastIndexOf(marker);
        if (index !== -1) {
            const lastError = message.slice(index + marker.length).trim();
            if (lastError && lastError !== message) {
                return getFriendlyRedeemErrorMessage(lastError, statusCode);
            }
        }
        return '请求重试次数过多，请稍后再试';
    }

    if (
        includesAny('value is not a valid email', 'invalid email', '邮箱格式', 'email address is not valid') ||
        (includesAny('field required', 'missing') && includesAny('email'))
    ) {
        return '邮箱格式不正确，请检查后重试';
    }

    if (includesAny('field required', 'missing') && includesAny('code', '兑换码')) {
        return '兑换码不能为空，请重新输入';
    }

    if (includesAny('兑换码不存在', 'invalid code', 'code not found', 'invalid redemption code')) {
        return '兑换码不存在或输入有误，请检查后重试';
    }

    if (includesAny('兑换码已被使用', '兑换码已使用', 'already used')) {
        return '该兑换码已使用，不能重复兑换';
    }

    if (includesAny('质保已过期')) {
        return '该兑换码质保已过期，无法再次兑换';
    }

    if (includesAny('兑换码已过期', '超过首次兑换截止时间', 'expired')) {
        return '兑换码已过期，请联系管理员更换新码';
    }

    if (includesAny('次数已用完', 'limit reached', 'no remaining')) {
        return '该兑换码可用次数已用完，请联系管理员';
    }

    if (includesAny('兑换码已失效', '最新福利通用兑换码')) {
        return '该兑换码已失效，请使用最新兑换码';
    }

    if (includesAny('已在 team', 'already in workspace', 'already in team', 'already a member')) {
        return '该邮箱已在所选 Team 中，当前兑换码不会被消耗，请改选其他 Team';
    }

    if (includesAny('已加入所有可用 team', '没有新的可用 team')) {
        return '您已加入当前所有可用 Team，当前兑换码不会被消耗';
    }

    if (includesAny('没有可用的 team')) {
        return '当前没有可用 Team 席位，请稍后重试';
    }

    if (includesAny('席位已满', 'maximum number of seats', 'no seats', 'team 已满', 'team已满', '请选择其他 team', ' full')) {
        return '所选 Team 席位已满，请选择其他 Team 重试';
    }

    if (includesAny('账号受限', '风控', '账单', 'billing', 'restricted', 'blocked')) {
        return '目标 Team 当前状态异常（可能账单或风控限制），请联系管理员处理';
    }

    if (
        includesAny('token', 'access token', 'session token') &&
        includesAny('过期', '失效', 'invalid', 'expired', 'invalidated')
    ) {
        return 'Team 登录状态已失效，请联系管理员刷新 Token 后重试';
    }

    if (includesAny('获取 team 访问权限失败')) {
        return '无法获取 Team 访问权限，请稍后重试或联系管理员';
    }

    if (includesAny('服务器响应格式错误', 'cannot parse', 'json')) {
        return '服务器返回异常，请稍后重试';
    }

    if (includesAny('proxy', 'connection', 'timeout', 'timed out', 'network', '连接', 'dns', 'ssl', 'socket')) {
        return '网络连接异常，请稍后重试';
    }

    if (statusCode === 409) {
        return 'Team 状态发生变化（如席位已满），请重试或选择其他 Team';
    }

    if (statusCode >= 500) {
        return '服务器繁忙，请稍后重试';
    }

    if (!message) {
        return statusCode >= 500 ? '服务器繁忙，请稍后重试' : '兑换失败，请稍后重试';
    }

    if (isTechnicalLogMessage(message)) {
        return statusCode >= 500 ? '服务器繁忙，请稍后重试' : '请求处理失败，请稍后重试或联系管理员';
    }

    return message;
}

function getFriendlyWarrantyErrorMessage(rawMessage, statusCode = 0) {
    const message = normalizeRawErrorMessage(rawMessage);
    const lower = message.toLowerCase();
    const includesAny = (...keywords) => keywords.some(keyword => lower.includes(String(keyword).toLowerCase()));

    if (includesAny('必须提供邮箱或兑换码')) {
        return '请输入兑换码或邮箱后再查询';
    }

    if (includesAny('查询太频繁')) {
        const waitMatch = message.match(/(\d+)\s*秒/);
        if (waitMatch) {
            return `查询过于频繁，请 ${waitMatch[1]} 秒后再试`;
        }
        return '查询过于频繁，请稍后再试';
    }

    if (includesAny('未登录', 'api key 无效', 'unauthorized', 'forbidden')) {
        return '登录状态已失效，请刷新页面后重试';
    }

    if (includesAny('兑换码不存在')) {
        return '未找到该兑换码，请检查输入是否正确';
    }

    if (includesAny('未找到兑换记录', '未找到相关记录')) {
        return '未找到相关记录，请确认邮箱或兑换码是否正确';
    }

    if (includesAny('质保已过期')) {
        return '该兑换码质保已过期';
    }

    if (includesAny('服务器响应格式错误', 'cannot parse', 'json')) {
        return '服务器返回异常，请稍后重试';
    }

    if (includesAny('proxy', 'connection', 'timeout', 'timed out', 'network', '连接', 'dns', 'ssl', 'socket')) {
        return '网络连接异常，请稍后重试';
    }

    if (statusCode === 429) {
        return '查询过于频繁，请稍后再试';
    }

    if (statusCode === 401 || statusCode === 403) {
        return '登录状态已失效，请刷新页面后重试';
    }

    if (statusCode >= 500) {
        return '系统繁忙，请稍后重试';
    }

    if (!message) {
        return '查询失败，请稍后重试';
    }

    if (isTechnicalLogMessage(message)) {
        return statusCode >= 500 ? '系统繁忙，请稍后重试' : '查询失败，请稍后重试';
    }

    return message;
}

function getFriendlyDeviceAuthErrorMessage(rawMessage, statusCode = 0) {
    const message = normalizeRawErrorMessage(rawMessage);
    const lower = message.toLowerCase();
    const includesAny = (...keywords) => keywords.some(keyword => lower.includes(String(keyword).toLowerCase()));

    if (includesAny('开启设备身份验证失败:')) {
        const marker = '开启设备身份验证失败:';
        const index = message.lastIndexOf(marker);
        if (index !== -1) {
            const innerMessage = message.slice(index + marker.length).trim();
            if (innerMessage && innerMessage !== message) {
                return getFriendlyDeviceAuthErrorMessage(innerMessage, statusCode);
            }
        }
    }

    if (includesAny('未登录', 'api key 无效', 'unauthorized', 'forbidden')) {
        return '登录状态已失效，请先登录管理员后重试';
    }

    if (includesAny('team id', 'team 不存在', '目标 team', 'not found')) {
        return '目标 Team 不存在或已失效，请刷新后重试';
    }

    if (
        includesAny('token', 'access token', 'session token') &&
        includesAny('过期', '失效', 'invalid', 'expired', 'invalidated')
    ) {
        return 'Team 登录状态已失效，请先在后台刷新 Token';
    }

    if (includesAny('服务器响应格式错误', 'cannot parse', 'json')) {
        return '服务器返回异常，请稍后重试';
    }

    if (includesAny('proxy', 'connection', 'timeout', 'timed out', 'network', '连接', 'dns', 'ssl', 'socket')) {
        return '网络连接异常，请稍后重试';
    }

    if (statusCode === 401 || statusCode === 403) {
        return '登录状态已失效，请先登录管理员后重试';
    }

    if (statusCode >= 500) {
        return '开启失败，请稍后重试';
    }

    if (!message) {
        return statusCode >= 500 ? '开启失败，请稍后重试' : '开启失败，请重试';
    }

    if (isTechnicalLogMessage(message)) {
        return statusCode >= 500 ? '开启失败，请稍后重试' : '开启失败，请重试';
    }

    return message;
}

function bindDialogFocusTrap(modal, dialog, onClose, getLastTrigger) {
    const focusableSelector = 'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])';

    const trapFocus = (event) => {
        if (event.key !== 'Tab') return;
        const focusableElements = Array.from(dialog.querySelectorAll(focusableSelector)).filter(el => !el.disabled && el.offsetParent !== null);
        if (focusableElements.length === 0) return;

        const first = focusableElements[0];
        const last = focusableElements[focusableElements.length - 1];

        if (event.shiftKey && document.activeElement === first) {
            event.preventDefault();
            last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
            event.preventDefault();
            first.focus();
        }
    };

    const handleKeydown = (event) => {
        if (event.key === 'Escape') {
            event.preventDefault();
            onClose();
            return;
        }
        trapFocus(event);
    };

    modal._focusTrapHandler = handleKeydown;
    document.addEventListener('keydown', handleKeydown);

    const firstFocusable = dialog.querySelector(focusableSelector);
    if (firstFocusable && typeof firstFocusable.focus === 'function') {
        firstFocusable.focus();
    }

    modal._restoreFocus = () => {
        document.removeEventListener('keydown', handleKeydown);
        const trigger = getLastTrigger();
        if (trigger && typeof trigger.focus === 'function') {
            trigger.focus();
        }
    };
}

function initAnnouncementModal() {
    const announcement = window.REDEEM_ANNOUNCEMENT || {};
    if (!announcement.enabled || !announcement.markdown || !String(announcement.markdown).trim()) {
        return;
    }

    const modal = document.getElementById('announcementModal');
    const content = document.getElementById('announcementContent');
    const closeBtn = document.getElementById('announcementCloseBtn');
    const confirmBtn = document.getElementById('announcementConfirmBtn');
    const backdrop = document.getElementById('announcementBackdrop');
    const dialog = modal?.querySelector('.announcement-dialog');

    if (!modal || !content || !dialog) return;

    const closeModal = () => {
        modal.classList.remove('show');
        modal.setAttribute('aria-hidden', 'true');
        document.body.classList.remove('modal-open');
        if (typeof modal._restoreFocus === 'function') {
            modal._restoreFocus();
        }
    };

    content.innerHTML = renderMarkdownSafe(String(announcement.markdown));
    lastAnnouncementTrigger = document.activeElement;
    modal.classList.add('show');
    modal.setAttribute('aria-hidden', 'false');
    document.body.classList.add('modal-open');
    bindDialogFocusTrap(modal, dialog, closeModal, () => lastAnnouncementTrigger);

    if (closeBtn) closeBtn.addEventListener('click', closeModal);
    if (confirmBtn) confirmBtn.addEventListener('click', closeModal);
    if (backdrop) backdrop.addEventListener('click', closeModal);
}

function initRenewalReminderModal() {
    const modal = document.getElementById('renewalReminderModal');
    const closeBtn = document.getElementById('renewalReminderCloseBtn');
    const cancelBtn = document.getElementById('renewalReminderCancelBtn');
    const skipBtn = document.getElementById('renewalReminderSkipBtn');
    const contactBtn = document.getElementById('renewalReminderContactBtn');
    const backdrop = document.getElementById('renewalReminderBackdrop');
    const dialog = modal?.querySelector('.announcement-dialog');
    if (!modal || !dialog) return;

    const resolveAndClose = (action) => {
        modal.classList.remove('show');
        modal.setAttribute('aria-hidden', 'true');
        document.body.classList.remove('modal-open');
        if (typeof modal._restoreFocus === 'function') {
            modal._restoreFocus();
        }
        if (pendingRenewalReminderResolver) {
            pendingRenewalReminderResolver(action);
            pendingRenewalReminderResolver = null;
        }
    };

    if (closeBtn) closeBtn.addEventListener('click', () => resolveAndClose('cancel'));
    if (cancelBtn) cancelBtn.addEventListener('click', () => resolveAndClose('cancel'));
    if (skipBtn) skipBtn.addEventListener('click', () => resolveAndClose('continue'));
    if (contactBtn) contactBtn.addEventListener('click', () => resolveAndClose('contact'));
    if (backdrop) backdrop.addEventListener('click', () => resolveAndClose('cancel'));

    modal._open = (contentHtml) => {
        const content = document.getElementById('renewalReminderContent');
        if (!content) return Promise.resolve('continue');
        content.innerHTML = contentHtml;
        lastRenewalReminderTrigger = document.activeElement;
        modal.classList.add('show');
        modal.setAttribute('aria-hidden', 'false');
        document.body.classList.add('modal-open');
        bindDialogFocusTrap(modal, dialog, () => resolveAndClose('cancel'), () => lastRenewalReminderTrigger);
        return new Promise((resolve) => {
            pendingRenewalReminderResolver = resolve;
        });
    };
}

// 切换步骤
function showStep(stepNumber) {
    document.querySelectorAll('.step').forEach(step => {
        step.classList.remove('active');
        step.style.display = ''; // 清除内联样式，交由CSS类控制显隐
    });
    const targetStep = document.getElementById(`step${stepNumber}`);
    if (targetStep) {
        targetStep.classList.add('active');
    }
}

function updateTabIndicator(activeTab) {
    const indicator = document.getElementById('tabIndicator');
    if (!indicator || !activeTab) return;

    indicator.style.left = `${activeTab.offsetLeft}px`;
    indicator.style.width = `${activeTab.offsetWidth}px`;
}

function switchTopTab(tabName) {
    currentTopTab = tabName;

    const redeemPanel = document.getElementById('redeemPanel');
    const warrantyPanel = document.getElementById('warrantyPanel');
    const tabRedeem = document.getElementById('tabRedeem');
    const tabWarranty = document.getElementById('tabWarranty');

    if (redeemPanel) redeemPanel.classList.toggle('active', tabName === 'redeem');
    if (warrantyPanel) warrantyPanel.classList.toggle('active', tabName === 'warranty');
    if (tabRedeem) {
        tabRedeem.classList.toggle('active', tabName === 'redeem');
        tabRedeem.setAttribute('aria-selected', tabName === 'redeem' ? 'true' : 'false');
        tabRedeem.setAttribute('tabindex', tabName === 'redeem' ? '0' : '-1');
    }
    if (tabWarranty) {
        tabWarranty.classList.toggle('active', tabName === 'warranty');
        tabWarranty.setAttribute('aria-selected', tabName === 'warranty' ? 'true' : 'false');
        tabWarranty.setAttribute('tabindex', tabName === 'warranty' ? '0' : '-1');
    }

    updateTabIndicator(tabName === 'redeem' ? tabRedeem : tabWarranty);
}

function resetRedeemResult() {
    const resultContent = document.getElementById('resultContent');
    if (resultContent) {
        resultContent.innerHTML = '';
    }
}

// 返回步骤1
function backToStep1(targetTab = 'redeem') {
    showStep(1);
    switchTopTab(targetTab);
}

function restartRedeemFlow() {
    currentEmail = '';
    currentCode = '';
    resetRedeemResult();
    backToStep1('redeem');
    const verifyForm = document.getElementById('verifyForm');
    if (verifyForm) verifyForm.reset();
    const emailInput = document.getElementById('email');
    if (emailInput) emailInput.focus();
}

function setWarrantyResultVisible(visible) {
    const warrantyResultContainer = document.getElementById('warrantyResultContainer');
    if (warrantyResultContainer) {
        warrantyResultContainer.hidden = !visible;
    }
}

function focusElementLater(element) {
    if (!element || typeof element.focus !== 'function') return;
    requestAnimationFrame(() => element.focus());
}

function encodeActionValue(value) {
    return encodeURIComponent(String(value ?? ''));
}

function decodeActionValue(value) {
    try {
        return decodeURIComponent(String(value ?? ''));
    } catch (_) {
        return String(value ?? '');
    }
}

async function parseJsonResponse(response) {
    const text = await response.text();
    if (!text) {
        return { text: '', data: null };
    }

    try {
        return { text, data: JSON.parse(text) };
    } catch (_) {
        return { text, data: null };
    }
}

function renderStepResult(html) {
    const resultContent = document.getElementById('resultContent');
    if (!resultContent) return;

    resultContent.innerHTML = html;
    if (window.lucide) lucide.createIcons();
    showStep(3);
    focusElementLater(resultContent);
}

function renderWarrantyPanel(html) {
    const warrantyContent = document.getElementById('warrantyContent');
    if (!warrantyContent) return;

    warrantyContent.innerHTML = html;
    if (window.lucide) lucide.createIcons();

    showStep(1);
    switchTopTab('warranty');
    setWarrantyResultVisible(true);
    focusElementLater(warrantyContent);
}

function renderResultDetails(items) {
    const rows = items
        .filter(item => item && item.value)
        .map(item => `
            <div class="result-detail-item">
                <span class="result-detail-label">${escapeHtml(item.label)}</span>
                <span class="result-detail-value">${escapeHtml(item.value)}</span>
            </div>
        `)
        .join('');

    if (!rows) return '';
    return `<div class="result-details">${rows}</div>`;
}

function getTeamStatusMeta(status) {
    switch (status) {
        case 'active':
            return { label: '正常', className: 'badge-success' };
        case 'full':
            return { label: '已满', className: 'badge-success' };
        case 'banned':
            return { label: '封号', className: 'badge-error' };
        case 'error':
            return { label: '异常', className: 'badge-warn' };
        case 'expired':
            return { label: '过期', className: 'badge-neutral' };
        case 'suspected_inconsistent':
            return { label: '同步异常', className: 'badge-warn' };
        default:
            return { label: status || '未知', className: 'badge-neutral' };
    }
}

function getWarrantyStatusBadge(valid) {
    return valid
        ? '<span class="badge badge-success">质保有效</span>'
        : '<span class="badge badge-error">质保已过期</span>';
}

async function handleDynamicActionClick(event) {
    const actionButton = event.target.closest('[data-action]');
    if (!actionButton) return;

    const action = actionButton.dataset.action;
    if (!action) return;

    switch (action) {
        case 'restart-redeem':
            restartRedeemFlow();
            break;
        case 'back-redeem':
            backToStep1('redeem');
            focusElementLater(document.getElementById('email'));
            break;
        case 'go-warranty':
            await goToWarrantyFromSuccess();
            break;
        case 'copy-warranty-code':
            await copyWarrantyCode(decodeActionValue(actionButton.dataset.code));
            break;
        case 'one-click-replace':
            await oneClickReplace(
                decodeActionValue(actionButton.dataset.code),
                decodeActionValue(actionButton.dataset.email),
                actionButton,
                {
                    teamId: Number(actionButton.dataset.teamId || 0) || null,
                    remainingWarrantyDays: actionButton.dataset.remainingWarrantyDays,
                    autoKickEnabled: actionButton.dataset.autoKickEnabled === 'true',
                    renewalReminderDays: actionButton.dataset.renewalReminderDays,
                    shouldShowRenewalReminder: actionButton.dataset.showRenewalReminder === 'true',
                }
            );
            break;
        case 'enable-device-auth':
            await enableUserDeviceAuth(
                Number(actionButton.dataset.teamId),
                decodeActionValue(actionButton.dataset.code),
                decodeActionValue(actionButton.dataset.email),
                actionButton
            );
            break;
        default:
            break;
    }
}

function handleTopTabKeydown(event) {
    const tabs = [
        document.getElementById('tabRedeem'),
        document.getElementById('tabWarranty')
    ].filter(Boolean);

    const currentIndex = tabs.indexOf(event.currentTarget);
    if (currentIndex === -1) return;

    let nextIndex = null;
    if (event.key === 'ArrowRight') nextIndex = (currentIndex + 1) % tabs.length;
    if (event.key === 'ArrowLeft') nextIndex = (currentIndex - 1 + tabs.length) % tabs.length;
    if (event.key === 'Home') nextIndex = 0;
    if (event.key === 'End') nextIndex = tabs.length - 1;

    if (nextIndex === null) return;

    event.preventDefault();
    const nextTab = tabs[nextIndex];
    switchTopTab(nextTab.dataset.tab);
    nextTab.focus();
}

document.addEventListener('DOMContentLoaded', () => {
    const tabRedeem = document.getElementById('tabRedeem');
    const tabWarranty = document.getElementById('tabWarranty');
    const warrantyForm = document.getElementById('warrantyForm');

    if (tabRedeem) {
        tabRedeem.addEventListener('click', () => switchTopTab('redeem'));
        tabRedeem.addEventListener('keydown', handleTopTabKeydown);
    }
    if (tabWarranty) {
        tabWarranty.addEventListener('click', () => switchTopTab('warranty'));
        tabWarranty.addEventListener('keydown', handleTopTabKeydown);
    }
    if (warrantyForm) {
        warrantyForm.addEventListener('submit', async (event) => {
            event.preventDefault();
            await checkWarranty();
        });
    }

    document.addEventListener('click', (event) => {
        void handleDynamicActionClick(event);
    });

    switchTopTab('redeem');
    setWarrantyResultVisible(false);
    initAnnouncementModal();
    initRenewalReminderModal();
    window.addEventListener('resize', () => {
        const activeTab = document.querySelector('.top-tab.active');
        updateTabIndicator(activeTab);
    });
});

// 步骤1: 验证兑换码并直接兑换
document.getElementById('verifyForm').addEventListener('submit', async (e) => {
    e.preventDefault();

    const email = document.getElementById('email').value.trim();
    const code = document.getElementById('code').value.trim();
    const verifyBtn = document.getElementById('verifyBtn');

    // 验证
    if (!email || !code) {
        showToast('请填写完整信息', 'error');
        return;
    }

    // 保存到全局变量
    currentEmail = email;
    currentCode = code;

    // 禁用按钮
    verifyBtn.disabled = true;
    verifyBtn.innerHTML = '<i data-lucide="loader-circle" class="spinning"></i> 正在兑换...';
    if (window.lucide) lucide.createIcons();

    // 直接调用兑换接口 (team_id = null 表示自动选择)
    await confirmRedeem(null);

    // 恢复按钮状态 (如果 confirmRedeem 失败并显示了错误也没关系，因为用户可以点返回重试)
    verifyBtn.disabled = false;
    verifyBtn.innerHTML = '<i data-lucide="shield-check"></i> 验证并激活兑换码';
    if (window.lucide) lucide.createIcons();
});

// 确认兑换
async function confirmRedeem(teamId) {
    if (isRedeeming) return;
    isRedeeming = true;

    try {
        const response = await fetch('/redeem/confirm', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                email: currentEmail,
                code: currentCode,
                team_id: teamId
            })
        });

        const { text, data } = await parseJsonResponse(response);
        if (!data) {
            throw new Error('服务器响应格式错误');
        }

        if (response.ok && data.success) {
            showSuccessResult(data);
            return;
        }

        const rawError = (data.detail ?? data.error ?? data.message ?? data.reason) || text;
        const errorMessage = getFriendlyRedeemErrorMessage(rawError, response.status);
        showErrorResult(errorMessage);
    } catch (error) {
        const errorMessage = getFriendlyRedeemErrorMessage(error?.message || '');
        showErrorResult(errorMessage || '网络错误，请稍后重试');
    } finally {
        isRedeeming = false;
    }
}

// 显示成功结果
function showSuccessResult(data) {
    const teamInfo = data.team_info || {};
    const detailsHtml = renderResultDetails([
        { label: 'Team 名称', value: teamInfo.team_name || '-' },
        { label: '邮箱地址', value: currentEmail || '-' },
        { label: '到期时间', value: teamInfo.expires_at ? formatDate(teamInfo.expires_at) : '' }
    ]);

    renderStepResult(`
        <div class="result-card result-success">
            <div class="result-card-header">
                <span class="result-icon success"><i data-lucide="check-circle"></i></span>
                <div class="result-title">兑换成功</div>
                <div class="result-message">${escapeHtml(data.message || '您已成功加入 Team')}</div>
            </div>
            ${detailsHtml}
            <div class="result-inline-note">
                <strong>邀请邮件已发送。</strong> 请前往邮箱查收，并按邮件指引接受邀请。如果 1-5 分钟后仍未收到，也可以前往质保查询进行自助修复。
            </div>
            <div class="result-card-actions">
                <button type="button" class="btn btn-secondary" data-action="go-warranty">
                    <i data-lucide="shield"></i> 前往质保查询 / 自助修复
                </button>
                <button type="button" class="btn btn-primary" data-action="restart-redeem">
                    <i data-lucide="refresh-cw"></i> 再次兑换
                </button>
            </div>
        </div>
    `);
}

// 显示错误结果
function showErrorResult(errorMessage) {
    renderStepResult(`
        <div class="result-card result-error">
            <div class="result-card-header">
                <span class="result-icon error"><i data-lucide="x-circle"></i></span>
                <div class="result-title">兑换失败</div>
                <div class="result-message">${escapeHtml(errorMessage)}</div>
            </div>
            <div class="result-card-actions">
                <button type="button" class="btn btn-secondary" data-action="back-redeem">
                    <i data-lucide="arrow-left"></i> 返回重试
                </button>
                <button type="button" class="btn btn-primary" data-action="restart-redeem">
                    <i data-lucide="rotate-ccw"></i> 重新开始
                </button>
            </div>
        </div>
    `);
}

// 格式化日期
function formatDate(dateString) {
    if (!dateString) return '-';

    try {
        const date = new Date(dateString);
        const year = date.getFullYear();
        const month = String(date.getMonth() + 1).padStart(2, '0');
        const day = String(date.getDate()).padStart(2, '0');
        return `${year}-${month}-${day}`;
    } catch (e) {
        return dateString;
    }
}

// ========== 质保查询功能 ==========

// 查询质保状态
async function checkWarranty() {
    if (isCheckingWarranty) return;

    const warrantyInput = document.getElementById('warrantyInput');
    const input = warrantyInput ? warrantyInput.value.trim() : '';

    if (!input) {
        showToast('请输入原兑换码或邮箱进行查询', 'error');
        if (warrantyInput) warrantyInput.focus();
        return;
    }

    let email = null;
    let code = null;
    if (input.includes('@')) {
        email = input;
    } else {
        code = input;
    }

    const checkBtn = document.getElementById('checkWarrantyBtn');
    isCheckingWarranty = true;
    setWarrantyResultVisible(false);

    if (checkBtn) {
        checkBtn.disabled = true;
        checkBtn.innerHTML = '<i data-lucide="loader" class="spinning"></i> 查询中...';
        if (window.lucide) lucide.createIcons();
    }

    try {
        const response = await fetch('/warranty/check', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                email: email || null,
                code: code || null
            })
        });

        const { text, data } = await parseJsonResponse(response);
        if (!data) {
            throw new Error('服务器响应格式错误');
        }

        if (response.ok && data.success) {
            showWarrantyResult(data);
            return;
        }

        const rawError = (data.error ?? data.detail ?? data.message ?? data.reason) || text;
        const errorMessage = getFriendlyWarrantyErrorMessage(rawError, response.status);
        showToast(errorMessage, 'error');
    } catch (error) {
        const errorMessage = getFriendlyWarrantyErrorMessage(error?.message || '');
        showToast(errorMessage || '网络错误，请稍后重试', 'error');
    } finally {
        isCheckingWarranty = false;
        if (checkBtn) {
            checkBtn.disabled = false;
            checkBtn.innerHTML = '<i data-lucide="search-check"></i> 查询质保状态';
            if (window.lucide) lucide.createIcons();
        }
    }
}

// 显示质保查询结果
function showWarrantyResult(data) {
    if ((!data.records || data.records.length === 0) && data.can_reuse) {
        renderWarrantyPanel(`
            <div class="result-card result-info">
                <div class="result-card-header">
                    <span class="result-icon success"><i data-lucide="check-circle"></i></span>
                    <div class="result-title">修复成功</div>
                    <div class="result-message">${escapeHtml(data.message || '系统检测到异常并已自动修复')}</div>
                </div>
                <div class="result-inline-note">
                    <strong>请复制兑换码返回主页重试。</strong>
                    系统已恢复您的可用状态，重新提交一次即可继续兑换。
                </div>
                <div class="inline-code-box">
                    <input type="text" class="inline-code-input" value="${escapeHtml(data.original_code || '')}" readonly>
                    <button type="button" class="btn btn-secondary" data-action="copy-warranty-code" data-code="${encodeActionValue(data.original_code || '')}">
                        <i data-lucide="copy"></i> 复制
                    </button>
                </div>
                <div class="result-card-actions">
                    <button type="button" class="btn btn-primary" data-action="back-redeem">
                        <i data-lucide="arrow-left"></i> 立即返回重兑
                    </button>
                </div>
            </div>
        `);
        return;
    }

    if (!data.records || data.records.length === 0) {
        renderWarrantyPanel(`
            <div class="empty-result">
                <span class="result-icon info"><i data-lucide="info"></i></span>
                <div class="result-title">未找到兑换记录</div>
                <div class="result-message">${escapeHtml(data.message || '未找到相关记录')}</div>
            </div>
        `);
        return;
    }

    const summaryHtml = data.has_warranty ? `
        <div class="warranty-summary">
            <div>
                <div class="summary-card-label">当前质保状态</div>
                <div class="summary-card-value">${getWarrantyStatusBadge(Boolean(data.warranty_valid))}</div>
            </div>
            <div>
                <div class="summary-card-label">质保到期时间</div>
                <div class="summary-card-value">${data.warranty_expires_at ? formatDate(data.warranty_expires_at) : '尚未开始计算'}</div>
            </div>
        </div>
    ` : '';

    const recordsHtml = data.records.map((record) => {
        const typeBadge = record.has_warranty
            ? '<span class="badge badge-success">质保码</span>'
            : '<span class="badge badge-neutral">常规码</span>';
        const teamStatus = getTeamStatusMeta(record.team_status);
        const canReplace = record.has_warranty && record.warranty_valid && record.team_status === 'banned';
        // /warranty/enable-device-auth 是 admin 专属接口（require_admin），
        // 用户即使点了也只会拿 401/403。这里直接不渲染按钮，让用户走联系管理员流程，
        // 避免出现"按钮明明亮着、点了却报错"的体验问题。
        const canEnableDeviceAuth = false;
        const warrantyExpiryText = record.warranty_expires_at
            ? `${formatDate(record.warranty_expires_at)}${record.warranty_valid ? '（有效）' : '（已过期）'}`
            : '尚未开始计算';

        return `
            <article class="record-card">
                <div class="record-card-header">
                    <div class="record-code">${escapeHtml(record.code || '-')}</div>
                    <div>${typeBadge}</div>
                </div>
                <div class="record-meta-grid">
                    <div class="record-meta-item">
                        <div class="record-meta-label">加入 Team</div>
                        <div class="record-meta-value inline">
                            <span>${escapeHtml(record.team_name || '未知 Team')}</span>
                            <span class="badge ${teamStatus.className}">${escapeHtml(teamStatus.label)}</span>
                        </div>
                    </div>
                    <div class="record-meta-item">
                        <div class="record-meta-label">兑换时间</div>
                        <div class="record-meta-value">${escapeHtml(formatDate(record.used_at))}</div>
                    </div>
                    <div class="record-meta-item">
                        <div class="record-meta-label">Team 到期</div>
                        <div class="record-meta-value">${escapeHtml(formatDate(record.team_expires_at))}</div>
                    </div>
                    <div class="record-meta-item">
                        <div class="record-meta-label">设备身份验证</div>
                        <div class="record-meta-value">${record.device_code_auth_enabled ? '已开启' : '未开启'}</div>
                    </div>
                    ${record.has_warranty ? `
                    <div class="record-meta-item">
                        <div class="record-meta-label">质保到期</div>
                        <div class="record-meta-value">${escapeHtml(warrantyExpiryText)}</div>
                    </div>
                    ` : ''}
                    ${record.email ? `
                    <div class="record-meta-item">
                        <div class="record-meta-label">兑换邮箱</div>
                        <div class="record-meta-value">${escapeHtml(record.email)}</div>
                    </div>
                    ` : ''}
                </div>
                <div class="record-footer">
                    <div class="record-footer-copy">
                        <div class="record-meta-label">当前处理建议</div>
                        <div class="record-meta-value">${canReplace ? '该记录可直接触发质保重兑。' : '如 Team 状态异常，可联系管理员进一步排查。'}</div>
                    </div>
                    <div class="record-footer-actions">
                        ${canReplace ? `
                        <button
                            type="button"
                            class="btn btn-primary"
                            data-action="one-click-replace"
                            data-code="${encodeActionValue(record.code || '')}"
                            data-email="${encodeActionValue(record.email || currentEmail || '')}"
                            data-team-id="${Number(record.team_id || 0)}"
                            data-remaining-warranty-days="${record.remaining_warranty_days ?? ''}"
                            data-auto-kick-enabled="${record.auto_kick_enabled ? 'true' : 'false'}"
                            data-renewal-reminder-days="${record.renewal_reminder_days ?? ''}"
                            data-show-renewal-reminder="${record.should_show_renewal_reminder ? 'true' : 'false'}"
                        >
                            <i data-lucide="rotate-cw"></i> 一键换车
                        </button>
                        ` : ''}
                        ${canEnableDeviceAuth ? `
                        <button
                            type="button"
                            class="btn btn-secondary"
                            data-action="enable-device-auth"
                            data-team-id="${Number(record.team_id)}"
                            data-code="${encodeActionValue(record.code || '')}"
                            data-email="${encodeActionValue(record.email || '')}"
                        >
                            <i data-lucide="shield-check"></i> 一键开启设备验证
                        </button>
                        ` : ''}
                    </div>
                </div>
            </article>
        `;
    }).join('');

    const canReuseHtml = data.can_reuse ? `
        <div class="result-inline-note">
            <strong>发现失效 Team，质保可触发。</strong>
            您当前可复制原兑换码重新提交，系统会为您自动匹配新的可用 Team。
            <div class="inline-code-box">
                <input type="text" class="inline-code-input" value="${escapeHtml(data.original_code || '')}" readonly>
                <button type="button" class="btn btn-secondary" data-action="copy-warranty-code" data-code="${encodeActionValue(data.original_code || '')}">
                    <i data-lucide="copy"></i> 复制
                </button>
            </div>
        </div>
    ` : '';

    renderWarrantyPanel(`
        <div class="warranty-view">
            ${summaryHtml}
            <section class="records-section">
                <h3 class="records-section-title">我的兑换记录</h3>
                <div class="records-list">${recordsHtml}</div>
            </section>
            ${canReuseHtml}
            <div class="result-card-actions">
                <button type="button" class="btn btn-secondary" data-action="back-redeem">
                    <i data-lucide="arrow-left"></i> 返回兑换
                </button>
            </div>
        </div>
    `);
}

// 复制质保兑换码
async function copyWarrantyCode(code) {
    try {
        if (navigator.clipboard && window.isSecureContext) {
            await navigator.clipboard.writeText(code);
        } else {
            const textArea = document.createElement('textarea');
            textArea.value = code;
            textArea.style.position = 'fixed';
            textArea.style.opacity = '0';
            document.body.appendChild(textArea);
            textArea.focus();
            textArea.select();
            const copied = document.execCommand('copy');
            document.body.removeChild(textArea);
            if (!copied) throw new Error('copy failed');
        }
        showToast('兑换码已复制到剪贴板', 'success');
    } catch (_) {
        showToast('复制失败，请手动复制', 'error');
    }
}

async function submitRenewalRequest(email, code, teamId = null) {
    const response = await fetch('/warranty/renewal-request', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json'
        },
        body: JSON.stringify({
            email,
            code,
            team_id: teamId,
            source: 'one_click_replace'
        })
    });

    const { text, data } = await parseJsonResponse(response);
    if (response.ok && data?.success) {
        return data;
    }

    const rawError = (data?.detail ?? data?.error ?? data?.message ?? data?.reason) || text;
    throw new Error(rawError || '提交续期请求失败');
}

async function maybeHandleRenewalReminder(code, email, btn, options = {}) {
    const shouldShow = Boolean(options.shouldShowRenewalReminder);
    if (!shouldShow || !options.autoKickEnabled) {
        return true;
    }

    const modal = document.getElementById('renewalReminderModal');
    if (!modal || typeof modal._open !== 'function') {
        return true;
    }

    const remainingDaysRaw = options.remainingWarrantyDays;
    const remainingDays = Number.isFinite(Number(remainingDaysRaw)) ? Number(remainingDaysRaw) : null;
    const reminderDaysRaw = options.renewalReminderDays;
    const reminderDays = Number.isFinite(Number(reminderDaysRaw)) ? Number(reminderDaysRaw) : null;

    const messageHtml = `
        <p>当前兑换码剩余 <strong>${escapeHtml(remainingDays ?? '-')}</strong> 天到期。</p>
        <p>如果现在加入新的 Team，质保到期后系统会自动将您移出。</p>
        <p>如需延长质保时间，可先联系管理员申请续期；也可以暂不续期，继续加入新的 Team。${reminderDays ? `（当前提醒阈值：${escapeHtml(reminderDays)} 天）` : ''}</p>
    `;

    const action = await modal._open(messageHtml);
    if (action === 'cancel') {
        return false;
    }

    if (action === 'contact') {
        try {
            const result = await submitRenewalRequest(email, code, options.teamId || null);
            showToast(result.message || '已通知管理员处理续期请求', 'success');
        } catch (error) {
            showToast(error?.message || '提交续期请求失败，请稍后重试', 'error');
            return false;
        }
    }

    return true;
}

// 一键换车
async function oneClickReplace(code, email, btn, options = {}) {
    if (!code || !email) {
        showToast('无法获取完整信息，请手动重试', 'error');
        return;
    }

    currentEmail = email;
    currentCode = code;

    const emailInput = document.getElementById('email');
    const codeInput = document.getElementById('code');
    if (emailInput) emailInput.value = email;
    if (codeInput) codeInput.value = code;

    const originalContent = btn ? btn.innerHTML : '';
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = '<i data-lucide="loader" class="spinning"></i> 处理中...';
        if (window.lucide) lucide.createIcons();
    }

    try {
        const canContinue = await maybeHandleRenewalReminder(code, email, btn, options);
        if (!canContinue) {
            return;
        }

        showToast('正在为您尝试自动兑换...', 'info');
        await confirmRedeem(null);
    } finally {
        if (btn && document.body.contains(btn)) {
            btn.disabled = false;
            btn.innerHTML = originalContent;
            if (window.lucide) lucide.createIcons();
        }
    }
}

// 用户一键开启设备身份验证
async function enableUserDeviceAuth(teamId, code, email, btn) {
    if (!window.confirm('确定要在该 Team 中开启设备代码身份验证吗？')) {
        return;
    }

    const originalContent = btn ? btn.innerHTML : '';
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = '<i data-lucide="loader" class="spinning"></i> 开启中...';
        if (window.lucide) lucide.createIcons();
    }

    try {
        const response = await fetch('/warranty/enable-device-auth', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                team_id: teamId,
                code,
                email
            })
        });

        const { text, data } = await parseJsonResponse(response);
        if (!data) {
            throw new Error('服务器响应格式错误');
        }

        if (response.ok && data.success) {
            showToast(data.message || '开启成功', 'success');
            await checkWarranty();
            return;
        }

        const rawError = (data.error ?? data.detail ?? data.message ?? data.reason) || text;
        const errorMessage = getFriendlyDeviceAuthErrorMessage(rawError, response.status);
        showToast(errorMessage, 'error');
    } catch (error) {
        const errorMessage = getFriendlyDeviceAuthErrorMessage(error?.message || '');
        showToast(errorMessage || '网络错误，请稍后重试', 'error');
    } finally {
        if (btn && document.body.contains(btn)) {
            btn.disabled = false;
            btn.innerHTML = originalContent;
            if (window.lucide) lucide.createIcons();
        }
    }
}

// 从成功页面跳转到质保查询
async function goToWarrantyFromSuccess() {
    const warrantyInput = document.getElementById('warrantyInput');
    if (warrantyInput) {
        warrantyInput.value = currentEmail || currentCode || '';
    }

    showStep(1);
    switchTopTab('warranty');
    await checkWarranty();
}
