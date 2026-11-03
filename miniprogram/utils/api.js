/**
 * rnbatch 服务端接口封装。
 *
 * 真实模式走 Bearer token；演示模式不联网，用本地状态把整条流程跑通。
 * 两种模式对外暴露同一组方法，页面不需要知道自己跑在哪一种模式下。
 */
const config = require('./config');
const planUtil = require('./plan');
const fmt = require('./format');

const TOKEN_KEY = 'rnbatch_token';

/* ------------------------------------------------------------------ 通用 */

function getToken() {
    return wx.getStorageSync(TOKEN_KEY) || '';
}

function setToken(value) {
    wx.setStorageSync(TOKEN_KEY, value);
}

function clearToken() {
    wx.removeStorageSync(TOKEN_KEY);
}

function request(path, data, method) {
    return new Promise(function (resolve, reject) {
        wx.request({
            url: config.API_BASE + path,
            method: method || 'POST',
            data: data || {},
            timeout: config.REQUEST_TIMEOUT,
            header: {
                'content-type': 'application/json',
                authorization: 'Bearer ' + getToken(),
            },
            success: function (res) {
                const body = res.data || {};
                if (res.statusCode === 401) {
                    clearToken();
                    reject(new Error('登录已过期，请重新进入小程序'));
                    return;
                }
                if (res.statusCode >= 200 && res.statusCode < 300 && body.ok) {
                    resolve(body);
                    return;
                }
                reject(new Error(body.error || '请求失败（' + res.statusCode + '）'));
            },
            fail: function () {
                reject(new Error('网络连接失败，请检查网络后重试'));
            },
        });
    });
}

function ensureLogin() {
    if (config.DEMO) return Promise.resolve();
    if (getToken()) return Promise.resolve();
    return new Promise(function (resolve, reject) {
        wx.login({
            success: function (res) {
                if (!res.code) {
                    reject(new Error('微信登录失败'));
                    return;
                }
                request('/api/wx/login', { code: res.code })
                    .then(function (body) {
                        setToken(body.token);
                        resolve();
                    })
                    .catch(reject);
            },
            fail: function () {
                reject(new Error('微信登录失败'));
            },
        });
    });
}

/* ------------------------------------------------------------------ 演示 */

const DEMO_TEXT = [
    '会议主题：星海中心项目 一期交付启动会',
    '甲方：恒远置业集团；乙方：筑元建筑设计有限公司',
    '会议时间：2026年10月20日',
    '本次会议就星海中心项目一期交付节点进行了讨论，',
    '星海中心项目预计于12月完成结构封顶。',
].join('\n');

const DEMO_CANDIDATES = [
    { kw: '星海中心', score: 16, why: '出现 3 次、出现在标题区' },
    { kw: '恒远置业集团', score: 5.7, why: '机构名' },
    { kw: '筑元建筑设计有限公司', score: 5.5, why: '机构名' },
    { kw: '一期交付启动会', score: 2.1, why: '出现在标题区' },
];

let demoState = null;

function demo() {
    if (!demoState) {
        demoState = {
            workspace: '',
            files: [],
            applied: false,
            plan: null,
            lastResult: null,
        };
    }
    return demoState;
}

/* ------------------------------------------------------------------ 对外 */

function createWorkspace() {
    if (config.DEMO) {
        const state = demo();
        state.workspace = 'demo-' + Date.now();
        state.files = [];
        state.applied = false;
        state.plan = null;
        return Promise.resolve({ ok: true, workspace: state.workspace });
    }
    return request('/api/workspaces/create', {});
}

/**
 * 上传一个文件。
 * file: { name, path, size }
 */
function uploadFile(workspace, file) {
    if (config.DEMO) {
        const state = demo();
        for (let i = 0; i < state.files.length; i += 1) {
            if (state.files[i].name === file.name) {
                return Promise.reject(new Error('同名文件已上传：' + file.name));
            }
        }
        state.files.push({
            name: file.name,
            size: file.size || 0,
            date: file.date || '',
        });
        return Promise.resolve({ ok: true, count: state.files.length });
    }

    return new Promise(function (resolve, reject) {
        const query = '?workspace=' + encodeURIComponent(workspace) +
            '&name=' + encodeURIComponent(file.name);
        wx.uploadFile({
            url: config.API_BASE + '/api/workspaces/upload' + query,
            filePath: file.path,
            name: 'file',
            formData: { workspace: workspace, name: file.name },
            header: { authorization: 'Bearer ' + getToken() },
            timeout: config.REQUEST_TIMEOUT,
            success: function (res) {
                let body = {};
                try {
                    body = JSON.parse(res.data);
                } catch (e) {
                    body = {};
                }
                if (res.statusCode >= 200 && res.statusCode < 300 && body.ok) {
                    resolve(body);
                    return;
                }
                reject(new Error(body.error || '上传失败（' + res.statusCode + '）'));
            },
            fail: function () {
                reject(new Error('上传中断，请重试'));
            },
        });
    });
}

function extract(workspace) {
    if (config.DEMO) {
        return Promise.resolve({
            ok: true,
            sources: [{ name: '会议纪要.docx', text: DEMO_TEXT }],
            candidates: DEMO_CANDIDATES,
        });
    }
    return request('/api/workspaces/extract', { workspace: workspace });
}

function preview(workspace, keyword, template) {
    if (config.DEMO) {
        const state = demo();
        const result = planUtil.buildPlan(state.files, keyword, template);
        state.plan = { token: 'demo-plan-' + Date.now(), rows: result.rows };
        return Promise.resolve({
            ok: true,
            plan_token: state.plan.token,
            count: result.rows.length,
            changed: result.changed,
            conflicts: result.conflicts,
            rows: result.rows,
        });
    }
    return request('/api/workspaces/preview', {
        workspace: workspace,
        kw: keyword,
        template: template,
    });
}

function apply(workspace, planToken) {
    if (config.DEMO) {
        const state = demo();
        if (!state.plan || state.plan.token !== planToken) {
            return Promise.reject(new Error('缺少有效预览确认，请重新预览'));
        }
        let renamed = 0;
        for (let i = 0; i < state.plan.rows.length; i += 1) {
            if (state.plan.rows[i].changed) renamed += 1;
        }
        state.applied = true;
        state.plan = null;
        state.lastResult = { renamed: renamed };
        return Promise.resolve({
            ok: true,
            renamed: renamed,
            billing: { free_units: renamed, paid_units: 0 },
        });
    }
    return request('/api/workspaces/apply', {
        workspace: workspace,
        plan_token: planToken,
    });
}

function undo(workspace) {
    if (config.DEMO) {
        const state = demo();
        const renamed = state.lastResult ? state.lastResult.renamed : 0;
        state.applied = false;
        state.lastResult = null;
        return Promise.resolve({ ok: true, restored: renamed, skipped: 0 });
    }
    return request('/api/workspaces/undo', { workspace: workspace });
}

function removeWorkspace(workspace) {
    if (config.DEMO) {
        demoState = null;
        return Promise.resolve({ ok: true });
    }
    return request('/api/workspaces/delete', { workspace: workspace });
}

function billingStatus() {
    if (config.DEMO) {
        return Promise.resolve({
            ok: true,
            free_limit: 20,
            free_remaining: 20,
            free_used: 0,
            paid_balance: 0,
            available: 20,
        });
    }
    return request('/api/billing/status', {}, 'GET');
}

/**
 * 下载整理好的 ZIP 到本地临时目录，返回临时文件路径。
 * 演示模式没有真实文件，直接抛错让页面给出提示。
 */
function downloadArchive(workspace) {
    if (config.DEMO) {
        return Promise.reject(new Error('演示模式不产生真实文件，请部署后端后再试'));
    }
    return new Promise(function (resolve, reject) {
        wx.downloadFile({
            url: config.API_BASE + '/api/workspaces/download?workspace=' +
                encodeURIComponent(workspace),
            header: { authorization: 'Bearer ' + getToken() },
            timeout: 60000,
            success: function (res) {
                if (res.statusCode === 200 && res.tempFilePath) {
                    resolve(res.tempFilePath);
                    return;
                }
                reject(new Error('下载失败（' + res.statusCode + '）'));
            },
            fail: function () {
                reject(new Error('下载失败，请检查网络'));
            },
        });
    });
}

module.exports = {
    ensureLogin: ensureLogin,
    clearToken: clearToken,
    createWorkspace: createWorkspace,
    uploadFile: uploadFile,
    extract: extract,
    preview: preview,
    apply: apply,
    undo: undo,
    removeWorkspace: removeWorkspace,
    billingStatus: billingStatus,
    downloadArchive: downloadArchive,
};
