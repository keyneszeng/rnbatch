# rnbatch · Cloudflare Pages 免费演示版

目标地址：**https://rnbatch.aibuild.work**

这是一个 **100% 静态浏览器应用**，不需要 Cloudflare Workers Paid、
Containers、Tunnel、阿里云、D1、R2 或 Python 服务。

## 演示范围

| 功能 | 免费静态演示版 |
|---|---|
| 多文件上传/文件夹选择 | 浏览器本地读取，不上传服务器 |
| TXT / MD / CSV / JSON 文字提取 | 支持 |
| DOCX / PPTX / XLSX 正文初步提取 | 支持（小于 5 MB 的 Office 文档） |
| PDF、图片扫描件 OCR | 不支持，改用手动输入关键词 |
| 自定义关键词、4 种命名模板、排序、冲突预览 | 支持 |
| 保存文件名、下载 ZIP | 支持，在浏览器本地生成 |
| 云端登录 / 数据库存档 / 历史记录 | 不支持 |
| 原始文件是否被改名 | 否，不改变用户本机原始文件 |

演示防卡顿限制：最多 80 个文件，总量 40 MB，单文件 20 MB。
浏览器 RAM、移动设备性能可能限制可用容量。

## Cloudflare Pages 免费部署

使用 [Cloudflare Pages Git 集成](https://developers.cloudflare.com/pages/get-started/git-integration/)
连接仓库 `keyneszeng/rnbatch`，部署如下：

| Pages 设置 | 值 |
|---|---|
| 项目名称 | `rnbatch-demo`（示例，不要覆盖现有项目） |
| 生产分支 | `main` |
| Root directory（根目录） | `demo` |
| Framework preset | `None` |
| Build command | `npm install --no-audit --no-fund && npm run build` |
| Build output directory | `dist` |
| 环境变量 | 无 |

首次部署完成后，Cloudflare 会分配 `*.pages.dev` 预览域名。
在 **Workers & Pages → rnbatch-demo → Custom domains** 添加：

`rnbatch.aibuild.work`

确保 `aibuild.work` 站点位于当前 Cloudflare 账户中，
且 `rnbatch` 子域名没有正在使用的 DNS 记录/服务。
**不要修改 apex 的 aibuild.work 或其他业务子域名。**

Cloudflare 完成自定义域名验证后，打开 `https://rnbatch.aibuild.work` 测试。
不要凭空创建 CNAME 指向尚未产生的 pages.dev 站点；
优先通过 Cloudflare Pages Custom domains UI 来添加。

## 本地构建与验证

```bash
cd demo
npm install
npm run build
python3 -m http.server 4173 --directory dist
```

浏览器打开 `http://127.0.0.1:4173`。

仓库还添加了 Github Actions `cloudflare-pages-free-demo`：
在 CI 安装 Chromium，对真实静态产物运行浏览器端黑盒测试，包括上传、
TXT / DOCX 文字提取、命名预览、下载并核验 ZIP、同名冲突阻断、
清空文件以及无远程数据上传。

## 安全与真实能力边界

- 该版本没有后台服务。所有 File 对象和 ZIP 内容由浏览器的 JavaScript 操作。
- 页面设置 `Content-Security-Policy: connect-src 'none'`，禁止调用外部 API。
- 选择文件不会上传到 Cloudflare；Cloudflare 只提供 HTML、CSS、JS。
- 关闭/刷新页面后列表与预览清空，不能跨设备访问、恢复或协作。
- XML/Office 解析是演示性质的纯前端有限实现，不代表支持所有文件格式。
- 不应把这个演示版宣传为完整的 Python rnbatch 引擎或 SaaS。
- 如未来需要云端持久化、用户认证、多人共享和大型文件队列，需重新评估
  Workers Free、R2/D1 或其他云资源的限额与计费。
- 本方案与 `deploy/cloudflare` 下的 **Tunnel + Docker 方案** 独立，
  不需要运行后者；两种部署路线请勿混用。

## 状态

仓库代码可以准备好静态网页和自动化构建，
但创建 Cloudflare Pages 项目、设置 `aibuild.work` 的 DNS
需要对相应 Cloudflare 账户具有授权的操作连接。
在没有实际 Pages 部署结果之前，不声称网站已经上线。
