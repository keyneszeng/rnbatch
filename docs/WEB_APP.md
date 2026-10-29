# Web App 多用户验证版（v0.4 开发中）

> **状态：代码试验版，不是安全审计完成的公网生产服务。** 该入口只处理浏览器上传后的临时副本；
> 不开放旧 `serve` 的任何本地路径 API。禁止将旧服务直接映射到公网。

## 启动与体验

要求 Python 3.9+，在仓库目录安装：

```bash
python3 -m pip install -e ".[docs]"
rnbatch user-add alice
rnbatch web
```

交互式输入至少 12 个字符的密码，在浏览器打开
`http://127.0.0.1:8766`，以刚创建的账号登录。

也可指定 SQLite 数据库和端口：

```bash
rnbatch user-add alice --db /srv/rnbatch/web-users.sqlite3
rnbatch web --db /srv/rnbatch/web-users.sqlite3 --port 8766
```

数据库保存账户和会话。建议把数据库放在仅服务账户可读写、具备安全备份的私有位置。
所有上传文件存放在系统临时目录；默认上传上限 200 文件、合计 100 MB、单文件 25 MB，
每个用户最多 5 个活跃工作区。闲置 1 小时后自动清除，进程重启后内存工作区索引不再有效。

## 仅供部署评估的 HTTPS 代理示例

服务端始终只监听 `127.0.0.1`。仅在完成安全审查、网络/配额限制及数据保留策略后，
才考虑通过受信任的 TLS 反向代理对外提供服务。

例如专用的 Caddy 主机：

```caddyfile
files.example.com {
    reverse_proxy 127.0.0.1:8766
}
```

启动时必须设置浏览器实际访问的 origin（严格比较 Host 和 Origin）：

```bash
rnbatch web --origin https://files.example.com --port 8766
```

此时会话 Cookie 启用 `Secure`、`HttpOnly` 和 `SameSite=Strict`。
HTTP 的 origin 只允许 localhost/127.0.0.1 供本机开发。
可信反向代理需要正确转发 Host，不能允许任意 Host 或转发公网到旧 `rnbatch serve` 服务。
浏览器登录和 API POST 必须带正确 Origin；已登录操作还需 CSRF 标头。

## 安全架构与现有边界

- 用户名由管理员手动创建；密码通过 scrypt 加盐哈希保存于 SQLite。
- 会话令牌是随机值，SQLite 中仅存 SHA-256 摘要；默认 12 小时有效。
- 访问工作区时，服务端从会话获取用户 ID 并验证工作区所有者，不信任客户端声明的 owner。
- 浏览器文件上传、关键词提取、预览、带令牌执行、撤销、ZIP 下载、删除均在相同账户的临时工作区内。
- 云端入口 `rnbatch web` 的路由表**没有**旧本地目录的 `/api/list`、`/api/apply` 等接口。
- 无公开注册、无长期存储、无网盘同步功能；关闭服务时工作区文件会被清理。
- 严格 Host / Origin 校验和一次性预览确认机制并不替代完整的安全测试。

## 公网生产化之前的必做项

1. 由独立安全专家审查登录限流、验证码/防滥用、账号生命周期、权限与会话撤销。
2. 引入进程/容器隔离的异步上传解析工作者，并设置 CPU、内存、磁盘、解压及 PDF/OCR 资源上限。
3. 引入持久化工作区状态（多实例或重启后仍可恢复）、对象存储及可验证的删除审计。
4. 配置代理层速率限制、WAF/恶意上传检测、TLS、标准安全响应头及监控报警。
5. 测试 Chrome/Safari/Edge、上传中断、并发和容量边界；完成法律与隐私审查。

## 验证

```bash
python -m unittest discover -s tests -v
```

测试包括两用户越权、CSRF/Origin、过期会话、密码哈希、登录/退出、
浏览器 API 上传 → 预览 → 执行 → ZIP 下载，以及旧服务端文件路径 API 不可调用。
