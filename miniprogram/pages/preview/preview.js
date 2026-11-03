const api = require('../../utils/api');

Page({
    data: {
        rows: [],
        count: 0,
        changed: 0,
        conflicts: 0,
        keyword: '',
        template: '',
        ready: false,
        applying: false,
        canApply: false,
    },

    onLoad() {
        const app = getApp();
        const plan = app.globalData.plan;
        if (!plan || !plan.rows) {
            this.setData({ ready: false });
            wx.showToast({ title: '预览已失效，请返回重试', icon: 'none' });
            return;
        }

        const rows = [];
        for (let i = 0; i < plan.rows.length; i += 1) {
            const item = plan.rows[i];
            rows.push({
                index: i + 1,
                from: item.from,
                to: item.to,
                changed: item.changed,
                conflict: item.conflict,
                same: !item.changed,
            });
        }

        this.planToken = plan.plan_token;
        this.workspace = app.globalData.workspace;

        this.setData({
            rows: rows,
            count: plan.count,
            changed: plan.changed,
            conflicts: plan.conflicts,
            keyword: app.globalData.keyword,
            template: app.globalData.template,
            ready: true,
            canApply: plan.conflicts === 0 && plan.changed > 0,
        });
    },

    goBack() {
        wx.navigateBack({ delta: 1 });
    },

    async applyPlan() {
        if (this.data.applying) return;
        if (!this.data.canApply) {
            wx.showToast({
                title: this.data.conflicts > 0 ? '存在冲突，请返回修改模板' : '没有需要修改的文件',
                icon: 'none',
            });
            return;
        }

        this.setData({ applying: true });
        try {
            const res = await api.apply(this.workspace, this.planToken);
            const app = getApp();
            app.globalData.result = res;
            app.globalData.plan = null;
            this.setData({ applying: false });
            wx.redirectTo({ url: '/pages/result/result' });
        } catch (e) {
            this.setData({ applying: false });
            wx.showModal({
                title: '执行失败',
                content: e.message || '请返回重新预览',
                showCancel: false,
            });
        }
    },
});
