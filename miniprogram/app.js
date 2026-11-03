const config = require('./utils/config');

App({
    globalData: {
        // 当前这一批任务在页面之间共享的状态
        workspace: '',
        files: [],
        keyword: '',
        template: '{kw}-{n:02}-{type}',
        plan: null,
        result: null,
    },

    onLaunch() {
        if (config.DEMO) {
            console.info('[rnbatch] 演示模式：未配置后端地址，全部数据在本地模拟。');
        }
    },
});
