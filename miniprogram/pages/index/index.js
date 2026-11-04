const api = require('../../utils/api');
const fmt = require('../../utils/format');
const config = require('../../utils/config');

const DEMO_FILES = [
    { name: '会议纪要.docx', size: 24576, date: '20261020' },
    { name: '方案汇报 v3 final.pptx', size: 1887436, date: '20261021' },
    { name: '材料清单(1).xlsx', size: 47104, date: '20261019' },
    { name: 'IMG_0421.JPG', size: 3355443, date: '20261018' },
    { name: '微信图片_20261018.jpg', size: 2202010, date: '20261018' },
    { name: '扫描件_001.pdf', size: 901120, date: '20261017' },
];

function toItem(file) {
    return {
        key: file.name + '|' + (file.size || 0),
        name: file.name,
        size: file.size || 0,
        sizeText: fmt.sizeText(file.size),
        badge: fmt.badgeOf(file.name),
        type: fmt.typeOf(file.name),
        path: file.path || '',
        date: file.date || '',
    };
}

Page({
    data: {
        demo: config.DEMO,
        files: [],
        keyword: '',
        candidates: [],
        templates: fmt.TEMPLATES,
        templateKey: 'std',
        quotaText: '',
        member: false,
        plans: [],
        checkoutEnabled: false,
        scanning: false,
        busyText: '',
        canPreview: false,
        fileCountText: '',
    },

    onLoad() {
        this.workspace = '';
        this.refreshQuota();
        this.refreshPlans();
    },

    onShow() {
        this.syncPreviewState();
    },

    onUnload() {
        this.dropWorkspace();
    },

    /* ---------------------------------------------------------- 状态 */

    syncPreviewState() {
        const keyword = String(this.data.keyword || '').trim();
        const ready = this.data.files.length > 0 && keyword.length > 0;
        this.setData({
            canPreview: ready,
            fileCountText: this.data.files.length > 0 ? '共 ' + this.data.files.length + ' 个文件' : '',
        });
    },

    async refreshQuota() {
        try {
            const info = await api.billingStatus();
            let text;
            if (info.member) {
                text = '会员有效 · 不限次数';
                if (info.member_until) {
                    text += '（至 ' + fmt.dateText(info.member_until) + '）';
                }
            } else {
                text = '本月免费剩余 ' + info.free_remaining + '/' + info.free_limit + ' 个';
            }
            this.setData({ quotaText: text, member: !!info.member });
        } catch (e) {
            this.setData({ quotaText: '额度暂时无法查询' });
        }
    },

    async refreshPlans() {
        try {
            const info = await api.catalog();
            const raw = info.plans || [];
            const plans = [];
            for (let i = 0; i < raw.length; i += 1) {
                plans.push({
                    sku: raw[i].sku,
                    title: raw[i].title,
                    priceText: fmt.planPriceText(raw[i].price_cents, raw[i].days),
                });
            }
            this.setData({ plans: plans, checkoutEnabled: !!info.checkout_enabled });
        } catch (e) {
            this.setData({ plans: [] });
        }
    },

    /* ---------------------------------------------------------- 选文件 */

    async pickFromChat() {
        if (this.data.busyText) return;
        try {
            const res = await this.chooseMessageFile();
            await this.acceptFiles(res);
        } catch (e) {
            if (e && e.cancelled) return;
            wx.showToast({ title: e.message || '选择失败', icon: 'none' });
        }
    },

    chooseMessageFile() {
        return new Promise(function (resolve, reject) {
            wx.chooseMessageFile({
                count: 100,
                type: 'all',
                success: resolve,
                fail: function (err) {
                    const msg = (err && err.errMsg) || '';
                    if (msg.indexOf('cancel') >= 0) {
                        const cancelled = new Error('已取消');
                        cancelled.cancelled = true;
                        reject(cancelled);
                        return;
                    }
                    reject(new Error('无法读取聊天文件，请在真机上选择'));
                },
            });
        });
    },

    async pickFromAlbum() {
        if (this.data.busyText) return;
        try {
            const res = await this.chooseMedia();
            await this.acceptFiles(res, true);
        } catch (e) {
            if (e && e.cancelled) return;
            wx.showToast({ title: e.message || '选择失败', icon: 'none' });
        }
    },

    chooseMedia() {
        return new Promise(function (resolve, reject) {
            wx.chooseMedia({
                count: 20,
                mediaType: ['image'],
                sourceType: ['album', 'camera'],
                success: resolve,
                fail: function (err) {
                    const msg = (err && err.errMsg) || '';
                    if (msg.indexOf('cancel') >= 0) {
                        const cancelled = new Error('已取消');
                        cancelled.cancelled = true;
                        reject(cancelled);
                        return;
                    }
                    reject(new Error('无法读取相册，请检查权限'));
                },
            });
        });
    },

    /**
     * chooseMessageFile 返回带原始文件名；chooseMedia 只有临时路径，
     * 相册照片本来也没有有意义的文件名，这里按序号补一个。
     */
    normalizePicked(res, fromAlbum) {
        const list = (res && res.tempFiles) || [];
        const out = [];
        let photoSeq = this.data.files.length;
        for (let i = 0; i < list.length; i += 1) {
            const item = list[i];
            if (fromAlbum) {
                photoSeq += 1;
                const path = item.tempFilePath || '';
                const dot = path.lastIndexOf('.');
                const ext = dot > 0 ? path.slice(dot) : '.jpg';
                out.push({
                    name: '照片_' + (photoSeq < 10 ? '0' + photoSeq : photoSeq) + ext,
                    path: path,
                    size: item.size || 0,
                    date: '',
                });
            } else {
                out.push({
                    name: item.name || '未命名文件',
                    path: item.path || '',
                    size: item.size || 0,
                    date: '',
                });
            }
        }
        return out;
    },

    async acceptFiles(res, fromAlbum) {
        const picked = this.normalizePicked(res, !!fromAlbum);
        if (!picked.length) return;

        const exist = {};
        for (let i = 0; i < this.data.files.length; i += 1) {
            exist[this.data.files[i].name] = true;
        }
        const incoming = [];
        let skipped = 0;
        for (let i = 0; i < picked.length; i += 1) {
            if (exist[picked[i].name]) {
                skipped += 1;
                continue;
            }
            exist[picked[i].name] = true;
            incoming.push(picked[i]);
        }
        if (!incoming.length) {
            wx.showToast({ title: '这些文件已经在列表里了', icon: 'none' });
            return;
        }

        await this.ensureWorkspace();

        for (let i = 0; i < incoming.length; i += 1) {
            this.setData({ busyText: '正在上传 ' + (i + 1) + '/' + incoming.length });
            try {
                await api.uploadFile(this.workspace, incoming[i]);
                const files = this.data.files.concat([toItem(incoming[i])]);
                this.setData({ files: files });
            } catch (e) {
                wx.showToast({ title: e.message || '上传失败', icon: 'none' });
            }
        }
        this.setData({ busyText: '' });
        this.syncPreviewState();
        if (skipped > 0) {
            wx.showToast({ title: '已跳过 ' + skipped + ' 个同名文件', icon: 'none' });
        }
    },

    async loadDemoFiles() {
        if (this.data.busyText) return;
        await this.resetBatch();
        this.setData({ busyText: '正在载入示例' });
        try {
            // 必须走和真实文件相同的上传路径：演示模式的工作区状态由 api 层维护，
            // 只 setData 而不 uploadFile，预览时工作区里会是空的。
            await this.ensureWorkspace();
            const items = [];
            for (let i = 0; i < DEMO_FILES.length; i += 1) {
                await api.uploadFile(this.workspace, DEMO_FILES[i]);
                items.push(toItem(DEMO_FILES[i]));
            }
            this.setData({ files: items });
            this.syncPreviewState();
            wx.showToast({ title: '已载入示例文件', icon: 'none' });
        } catch (e) {
            wx.showToast({ title: e.message || '载入失败', icon: 'none' });
        } finally {
            this.setData({ busyText: '' });
        }
    },

    async removeFile(event) {
        const key = event.currentTarget.dataset.key;
        const next = [];
        for (let i = 0; i < this.data.files.length; i += 1) {
            if (this.data.files[i].key !== key) next.push(this.data.files[i]);
        }
        this.setData({ files: next });
        this.syncPreviewState();
        if (!next.length) {
            await this.dropWorkspace();
        }
    },

    async clearAll() {
        this.setData({ files: [] });
        this.syncPreviewState();
        await this.dropWorkspace();
    },

    async resetBatch() {
        this.setData({ files: [], candidates: [], keyword: '' });
        this.syncPreviewState();
        await this.dropWorkspace();
    },

    /* ---------------------------------------------------------- 工作区 */

    async ensureWorkspace() {
        if (this.workspace) return this.workspace;
        await api.ensureLogin();
        const res = await api.createWorkspace();
        this.workspace = res.workspace;
        getApp().globalData.workspace = res.workspace;
        return this.workspace;
    },

    async dropWorkspace() {
        const current = this.workspace;
        this.workspace = '';
        getApp().globalData.workspace = '';
        if (!current) return;
        try {
            await api.removeWorkspace(current);
        } catch (e) {
            // 清理失败不影响主流程，服务端有闲置过期机制兜底
        }
    },

    /* ---------------------------------------------------------- 关键词 */

    onKeywordInput(event) {
        this.setData({ keyword: event.detail.value });
        this.syncPreviewState();
    },

    pickCandidate(event) {
        this.setData({ keyword: event.currentTarget.dataset.kw });
        this.syncPreviewState();
    },

    async scanKeywords() {
        if (this.data.scanning) return;
        if (!this.data.files.length) {
            wx.showToast({ title: '请先选择文件', icon: 'none' });
            return;
        }
        this.setData({ scanning: true });
        try {
            await this.ensureWorkspace();
            const res = await api.extract(this.workspace);
            const list = [];
            const seen = {};
            const raw = res.candidates || [];
            for (let i = 0; i < raw.length; i += 1) {
                const item = raw[i];
                if (seen[item.kw]) continue;
                seen[item.kw] = true;
                list.push({
                    kw: item.kw,
                    why: item.why || '',
                    scoreText: String(item.score),
                });
            }
            this.setData({ candidates: list });
            if (!list.length) {
                wx.showToast({ title: '没读到正文，请手动输入关键词', icon: 'none' });
            } else if (!String(this.data.keyword || '').trim()) {
                this.setData({ keyword: list[0].kw });
                this.syncPreviewState();
            }
        } catch (e) {
            wx.showToast({ title: e.message || '识别失败', icon: 'none' });
        } finally {
            this.setData({ scanning: false });
        }
    },

    /* ---------------------------------------------------------- 模板 */

    pickTemplate(event) {
        this.setData({ templateKey: event.currentTarget.dataset.key });
    },

    openPrivacy() {
        wx.navigateTo({ url: '/pages/privacy/privacy' });
    },

    /* ---------------------------------------------------------- 下一步 */

    async goPreview() {
        if (!this.data.canPreview) {
            wx.showToast({
                title: this.data.files.length ? '请填写关键词' : '请先选择文件',
                icon: 'none',
            });
            return;
        }
        const template = fmt.templateByKey(this.data.templateKey).template;
        this.setData({ busyText: '正在生成预览' });
        try {
            await this.ensureWorkspace();
            const res = await api.preview(this.workspace, String(this.data.keyword).trim(), template);
            getApp().globalData.plan = res;
            getApp().globalData.workspace = this.workspace;
            getApp().globalData.keyword = this.data.keyword;
            getApp().globalData.template = template;
            this.setData({ busyText: '' });
            wx.navigateTo({ url: '/pages/preview/preview' });
        } catch (e) {
            this.setData({ busyText: '' });
            wx.showToast({ title: e.message || '预览失败', icon: 'none' });
        }
    },
});
