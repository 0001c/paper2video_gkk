# 公网HTTPS访问与登录保护

## 当前访问方式

- 公网：`https://47.109.152.106:23741/`，mefrp保持TCP转发，公网端口不变。
- 局域网：`https://192.168.31.128:8849/`。
- 本机：`https://127.0.0.1:8849/`。
- 原本机和局域网HTTP8848入口只对GET/HEAD跳转到HTTPS8849；不接受明文登录或业务写入。
- 公网23741现在直接承载TLS，必须显式使用`https://`；不提供公网HTTP到HTTPS自动升级。
- 开启公网模式后，包括本机和局域网在内的业务访问都需要登录。未启用公网模式时仍保留原本机/可信子网工作方式。

## 登录与证书

管理员账号为`admin`。随机密码只保存在本机受限文件：

`E:\Development\26年\paper2video\.local\public-access\credentials.txt`

不要把密码、config.json、server-key.pem或穿透认证token上传、提交或发送到通知平台。本目录已被Git忽略，Windows ACL仅允许当前用户、Administrators与SYSTEM访问。运行配置保存scrypt盐和摘要；credentials.txt是便利交付的明文文件，应妥善管理，保存密码后可自行移除它。

当前使用90天有效期的自签名证书，不是公共CA签发证书。浏览器首次会提示不受信任，部分内置浏览器可能完全阻止访问。程序不会自动导入证书、改变系统信任设置或跳过安全警告。用户需核对证书并在访问设备上手动建立信任，或提供可用于该地址的受信任证书替换它；不能只按“忽略证书错误”处理。

可分享的公钥证书文件（不是私钥）：

`E:\Development\26年\paper2video\.local\public-access\server-cert.cer`

本次证书SHA256指纹：

`3819408f491eb42baf73fd8f0f6893ef3917337381e1e34a702624ae7c78d1a7`

证书包含公网IP、127.0.0.1、192.168.31.128与localhost。访问设备的证书信任流程因系统和浏览器而异，未在其他设备上确认；不能假设手机导入成功或内置浏览器立即读取系统信任变化。正式使用建议改成可控域名和对应受信任证书；本项目不会替用户申请域名或取得DNS账户权限。

## 保护范围

- 业务页面、任务记录、素材和成片下载、视频Range请求、所有业务API及SSE进度均先验证会话。
- 仅登录页、编译后的静态assets及认证入口可匿名访问，不包含私有任务产物。
- 会话为服务端内存中的随机令牌摘要，最长8小时；Cookie使用`__Host-`前缀、Secure、HttpOnly、SameSite=Strict、Path=/，不设置Domain，不使用浏览器本地存储保存密码或会话。
- 退出、重启服务或过期均使旧会话失效；已建立SSE会在下一条消息发送前重新验证会话。
- 本机/LAN地址和明确公网来源白名单同时校验。即使穿透流量来自本机，伪造loopback Host也不能绕过登录；不信任X-Forwarded-*。
- 所有写入包含登录和退出都要求HTTPS及完全匹配的Origin，缺少Origin或跨站访问被拒绝。
- 登录体积最多4096字节；每分钟全局最多10次尝试，防止所有穿透连接来自同一本机IP时按IP限流失效；会话最多128个。达到限流或会话上限会明确拒绝。
- 单管理员拥有现有工作台全部权限，不是多用户隔离/RBAC服务。登录限流不代替网络层抗DDoS措施；不要把账号分享给不可信的人。

## 启动与配置

在项目根目录执行：

```powershell
& 'E:\Development\26年\paper2video\.venv\Scripts\python.exe' -u 'E:\Development\26年\paper2video\web\app.py' --host 0.0.0.0 --port 8848 --https-port 8849 --lan-subnet 192.168.31.0/24 --public-config 'E:\Development\26年\paper2video\.local\public-access\config.json'
```

这一个进程运行业务HTTPS服务和无业务权限的HTTP跳转服务，队列生命周期只启动一次。公网配置错误、摘要缺失、证书/私钥无效或HTTPS端口冲突会停止启动，不静默降级为匿名服务。公网模式不允许`--auto-port`，以免穿透目标与实际监听端口不一致。

当前后台启动没有注册开机自启或任务计划。关闭或重启电脑后需要重新启动服务及frpc；服务重启后模型连接密钥也要重新绑定。

mefrp配置仅改动本地目标端口：

```toml
localIP = "192.168.31.128"
localPort = 8849
remotePort = 23741
```

不要将TLS隧道指向只会跳转的HTTP8848端口。原配置备份位于受保护的`.local/public-access/frp-original.toml`，含原穿透凭据，不应共享或提交。

Windows入站规则`Paper2Video-LAN-8849-192-168-31`仅放行当前Python监听进程、以太网、本地192.168.31.128:8849及远端192.168.31.0/24。没有关闭防火墙或对任意公网来源放行本机端口。虚拟环境Python启动器与实际监听Python路径可能不同，防火墙规则要绑定后者。

### 新环境或轮换密码、证书

在新的空目录生成配置，生成器需要环境中已有的cryptography库：

```powershell
& 'E:\Development\26年\paper2video\.venv\Scripts\python.exe' 'E:\Development\26年\paper2video\scripts\configure_public_access.py' --origin https://你的域名:23741 --local-ip 192.168.31.128 --directory 'E:\Development\26年\paper2video\.local\public-access\replacement' --generate-password
```

不加`--generate-password`时从终端安全输入密码，不通过命令参数传入明文。生成器拒绝覆盖非空目录；随后检查新配置并以新`--public-config`重启，旧会话随进程退出失效。Windows生成前先限定目录ACL，其他平台目录权限700、敏感文件600。

使用公共CA证书时，将私有config.json的tls_cert/tls_key指向正确证书链和私钥（可为配置目录相对路径或绝对路径），public_origins保持实际完整HTTPS来源。证书名称必须覆盖访问域名/IP；不支持通配符来源或任意反向代理头自动推断。

证书及账户变更后，应更新指纹/访问说明并重新验证。保持原私有配置的ACL和Git忽略规则，不能把配置移入可被前端静态目录或任务文件下载接口读取的位置。

## 验证与待办边界

- 自动化：公网专项22项、字幕相邻11项、文字/视觉相邻13项、前端16项；TypeScript/Vite构建和Python编译验证。
- 实际公网TLS探测使用生成证书作为信任锚，验证证书链及主机名，没有使用verify=False。确认登录、匿名拒绝、Host/Origin拒绝、成片206（只读64字节）、SSE与退出后旧Cookie失效。
- 本机HTTP跳转、HTTPS登录页和LAN地址未登录拒绝已验证；未从另一台LAN设备测试路由/AP隔离。
- 内置浏览器被自签证书阻止，完整浏览器登录交互和桌面/手机视觉验收仍待用户建立证书信任后确认。构建成功不代表这项已完成。
- 本次没有创建付费生成任务或调用模型API。验收证据在受限目录`.local/public-access/live-check.json`，不保存密码或Cookie。
