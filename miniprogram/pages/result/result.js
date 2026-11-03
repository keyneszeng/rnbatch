const api = require('../../utils/api');
const config = require('../../utils/config');

Page({
    data: {
        renamed: 0,
        demo: config.DEMO,
        busyText: '',
        canShare: true,
        canSaveDisk: true,
        undoable: true,
        freeUnits: 0,
        paidUnits: 0,
        quotaHint: '',
    },

    onLoad() {
        const app = getApp();
        const result = app.globalData.result || {};
        this.workspace = app.globalData.workspace || '';

        const billing = result.billing || {};
        const freeUnits = billing.free_units || 0;
        const paidUnits = billing.paid_units || 0;
        let quotaHint = '';
        if (freeUnits > 0) {
            quotaHint = '本次使用免费额度 ' + freeUnits + ' 个';
        }
        if (paidUnits > 0) {
            quotaHint += (quotaHint ? '，' : '') + '已购额度 ' + paidUnits + ' 个';
        }

        this.setData({
            renamed: result.renamed || 0,
            freeUnits: freeUnits,
            paidUnits: paidUnits,
            quotaHint: quotaHint,
            canShare: wx.canIUse('shareFileMessage'),
            canSaveDisk: wx.canIUse('saveFileToDisk'),
        });
    },

    /* ---------------------------------------------------------- 取回文件 */

    async fetchArchive() {
        if (config.DEMO) {
            throw new Error('演示模式不产生真实文件，部署后端后可下载');
        }
        if (!this.workspace) {
            throw new Error('工作区已失效，请重新整理');
        }
        return api.downloadArchive(this.workspace);
    },

    async shareToChat() {
        if (this.data.busyText) return;
        this.setData({ busyText: '正在打包' });
        try {
            const filePath = await this.fetchArchive();
            this.setData({ busyText: '' });
            wx.shareFileMessage({
                filePath: filePath,
                fileName: 'rnbatch-files.zip',
                fail: function (err) {
                    const msg = (err && err.errMsg) || '';
                    if (msg.indexOf('cancel') >= 0) return;
                    wx.showToast({ title: '转发失败，可先保存到电脑', icon: 'none' });
                },
            });
        } catch (e) {
            this.setData({ busyText: '' });
            wx.showModal({ title: '无法转发', content: e.message || '请稍后重试', showCancel: false });
        }
    },

    async saveToDisk() {
        if (this.data.busyText) return;
        if (!this.data.canSaveDisk) {
            wx.showToast({ title: '仅电脑版微信支持保存到本地', icon: 'none' });
            return;
        }
        this.setData({ busyText: '正在打包' });
        try {
            const filePath = await this.fetchArchive();
            this.setData({ busyText: '' });
            wx.saveFileToDisk({
                filePath: filePath,
                fail: function (err) {
                    const msg = (err && err.errMsg) || '';
                    if (msg.indexOf('cancel') >= 0) return;
                    wx.showToast({ title: '保存失败', icon: 'none' });
                },
            });
        } catch (e) {
            this.setData({ busyText: '' });
            wx.showModal({ title: '无法保存', content: e.message || '请稍后重试', showCancel: false });
        }
    },

    /* ---------------------------------------------------------- 撤销 */

    undoRename() {
        if (!this.data.undoable || this.data.busyText) return;
        const that = this;
        wx.showModal({
            title: '撤销这次改名',
            content: '文件名会还原成整理之前的样子。已扣的额度不会退回。',
            confirmText: '撤销',
            success: function (res) {
                if (res.confirm) that.runUndo();
            },
        });
    },

    async runUndo() {
        this.setData({ busyText: '正在还原' });
        try {
            const res = await api.undo(this.workspace);
            this.setData({ busyText: '', undoable: false });
            wx.showToast({ title: '已还原 ' + res.restored + ' 个文件', icon: 'none' });
            await this.releaseWorkspace();
            setTimeout(function () {
                wx.reLaunch({ url: '/pages/index/index' });
            }, 1200);
        } catch (e) {
            this.setData({ busyText: '' });
            wx.showModal({ title: '撤销失败', content: e.message || '请稍后重试', showCancel: false });
        }
    },

    async releaseWorkspace() {
        if (!this.workspace) return;
        try {
            await api.removeWorkspace(this.workspace);
        } catch (e) {
            // 清理失败不阻塞，服务端会按闲置时间回收
        }
        this.workspace = '';
        getApp().globalData.workspace = '';
    },

    /* ---------------------------------------------------------- 再来一次 */

    async restart() {
        await this.releaseWorkspace();
        getApp().globalData.plan = null;
        getApp().globalData.result = null;
        wx.reLaunch({ url: '/pages/index/index' });
    },
});
