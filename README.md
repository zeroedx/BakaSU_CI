# BakaSU_CI

独立的 CI 自动同步与发布仓库，用于自动获取 [BakaSU](https://github.com/Baka-SU/BakaSU) 官方
GitHub Actions 的成功构建产物，并发布到本仓库自己的 GitHub Releases。

本仓库**只发布一个文件**：最新成功构建中的 **ARM64 专用 Manager APK（`arm64-v8a`）**。
不发布内核模块（LKM）、`ksud`、`ksuinit` 或其他任何文件。

上游仓库**只读**，本仓库不会向其提交任何内容，也不会重新编译内核或安装包。

## 自动同步机制

`.github/workflows/sync.yml` **每小时运行一次**（`0 * * * *`），也可以在 Actions 页面手动
触发。工作流调用 `scripts/sync_release.py`：

1. 通过 GitHub API 拉取上游 `Build Manager` 工作流在 `main` 分支上**已成功完成**的运行记录
   （`status=success`、`conclusion=success`），并过滤掉 `pull_request` 事件。
2. 默认只检查**最近 1 条**成功运行（手动触发时可通过 `limit` 调整）。
3. 对每个运行，检查本仓库是否已经存在对应 Tag / Release；已存在则跳过，避免重复发布。
4. 下载 `Manager-release` 工件，从中**只挑选 ARM64 专用 APK**，创建 Release，Tag 形如
   `BakaSU_<run_id>`。
5. 每个 Release 记录上游提交 SHA、Actions Run ID、运行链接、分支与构建时间，便于追溯。

工作流使用最小权限的 `GITHUB_TOKEN`（仅 `contents: write`），不依赖任何自定义密钥，也不会
输出或提交密钥。

### 手动触发参数

- `limit`：检查最近多少条成功运行，**默认 1**。
- `dry_run`：只准备产物、不创建 Release，用于验证。

## ARM64 筛选规则

架构判定基于**实际构建产物**，不只看工件名称：

- 打开 `Manager-release` 压缩包，读取其中 APK 内的 `lib/<abi>/` 目录；
- 仅当 APK **只包含** `arm64-v8a` 时保留；
- 因此 `universal`（同时含 `arm64-v8a` 与 32 位 `armeabi-v7a`、`riscv64`、`x86_64`）、
  `armeabi-v7a`、`x86_64`、`riscv64` 这些 APK 均被排除；
- 一个 Release 中**最多只有一个 APK 文件**。

## 产物来源与命名

产物直接来自上游 `Build Manager` 运行，**保留原始文件名**：

| 上游产物 | 本仓库 Release 中的名称 | 说明 |
| --- | --- | --- |
| `Manager-release` | `BakaSU_<version>_<code>-arm64-v8a-release.apk` | 仅保留 `arm64-v8a` 这一个 APK |

### 不发布的产物

- **非 ARM64 的 APK**：`universal`、`armeabi-v7a`、`x86_64`、`riscv64`。
- **内核模块（LKM）**：`aarch64-<kmi>-lkm`、`x86_64-<kmi>-lkm` 等，全部不发布。
- **`ksud-*` / `ksuinit-*`**：全部不发布。
- `Spoofed-Manager-release`：其 APK 文件名与 `Manager-release` **完全相同**，同时发布会相互
  覆盖；如需伪装版请到上游运行页面下载。
- `Manager-debug`：调试版 APK。
- `Manager-mappings` / `Spoofed-Manager-mappings`：ProGuard 映射，属于调试信息。
- `telegram-bot-api-binary`：上游 CI 使用的 Telegram 机器人可执行文件。

## 异常处理

- **分页**：所有列表接口跟随 `Link` 头翻页，并设有页数上限防止失控。
- **API 限流**：遇到 403/429 时依据 `Retry-After` / `X-RateLimit-Reset` 退避重试。
- **下载失败 / 空产物**：记录日志并跳过该产物，不影响其余处理。
- **空运行 / 缺少必要产物**：若没有 `Manager-release`，则跳过该运行。
- **无 ARM64 APK**：若该运行中没有 ARM64 专用 APK，则跳过该运行，不创建 Release。
- **损坏的压缩包 / 非 ZIP**：捕获 `BadZipFile` 并跳过，不会中断程序。
- **重复 Tag**：创建 Release 前先检查，若上游 Tag 冲突（`422 already_exists`）则跳过。
- **无效 APK 文件名**：只接受扁平、安全的 `*.apk` 名称，忽略带路径或异常的条目。
- **过期产物**：已过期的 artifact 直接跳过。

## 使用注意事项

- Release 中的 APK 由上游 CI 使用其正式密钥签名；伪装版签名密钥不同，混装会导致签名冲突。
- 本仓库仅做搬运，不对构建结果做任何修改，也不保证可用性；遇到问题请向上游反馈。
- Tag 命名为 `BakaSU_<run_id>`，与上游正式发布 Tag（如 `v4.2.0`）无关，仅用于本仓库内部追踪。

## 致谢

- 上游项目：[Baka-SU/BakaSU](https://github.com/Baka-SU/BakaSU)
- 同步方案参考：[cctv18/ReSukiSU_CI](https://github.com/cctv18/ReSukiSU_CI)
