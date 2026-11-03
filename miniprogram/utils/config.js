/**
 * 后端地址配置。
 *
 * 留空 = 演示模式：所有请求在本地模拟，界面可完整点通，用于预览和产品评审。
 * 填上 = 真实模式：连你自己的 rnbatch 服务端。
 *
 * 填之前必须满足微信的两个硬性要求，否则请求会被平台拦掉：
 *   1. 必须是 HTTPS，且已在「小程序后台 → 开发管理 → 服务器域名」里登记；
 *   2. 域名不能带端口、不能用 IP。
 *
 * 例：API_BASE: 'https://rnbatch.example.com'
 */
const API_BASE = '';

module.exports = {
    API_BASE: API_BASE,
    DEMO: !API_BASE,
    REQUEST_TIMEOUT: 20000,
};
