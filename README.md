# BakaSU_CI

独立的 CI 自动同步与发布仓库，用于自动获取 [BakaSU](https://github.com/Baka-SU/BakaSU) 官方
GitHub Actions 的成功构建产物，并发布到本仓库自己的 GitHub Releases。

上游仓库**只读**，本仓库不会向其提交任何内容。

## 自动同步机制

`.github/workflows/sync.yml` 每个 8 小时运行一次（`0 */8 * * *`），也可以在 Actions 页面手动
触发。工作流调用 `scripts/sync_release.py`：

1. 通过 GitHub API 拉取上游 `Build Manager` 工作流在 `main` 分支上**已成功完成**的运行记录
   （`status=success`、`conclusion=success`），并过滤掉 `pull_request` 事件。
2. 对每个运行，检查本仓库是否已经存在对应 Tag / Release；已存在则跳过，避免重复发布。
3. 下载该运行的构建产物，按规则整理后创建 Release，Tag 形如 `BakaSU_<run_id>`。
4. 每个 Release 记录上游提交 SHA、Actions Run ID、运行链接、分支与构建时间，便于追溯。

工作流使用最小权限的 `GITHUB_TOKEN`（仅 `contents: write`），不依赖任何自定义密钥，也不会
输出或提交密钥。

### 手动触发参数

- `limit`：检查最近多少条成功运行，默认 50。
- `dry_run`：只准备产物、不发布，用于验证。

## 产物来源与命名

产物直接来自上游 `Build Manager` 运行。**APK 与工具类产物保留原始文件名**，仅在必要时处理：

| 上游产物 | 本仓库 Release 中的名称 | 说明 |
| --- | --- | --- |
| `Manager-release` | `BakaSU_<version>_<code>-<abi>-release.apk` | 按上游原名逐个发布（含 `universal`） |
| `Manager-debug` | `BakaSU_<version>_<code>-debug.apk` | 保留原始文件名 |
| `aarch64-<kmi>-lkm` | `lkm-aarch64-all.zip` | 合并该架构全部 `.ko` |
| `x86_64-<kmi>-lkm` | `lkm-x86_64-all.zip` | 合并该架构全部 `.ko` |
| `ksud-<target>` | `ksud-<target>.zip` | 原样发布上游压缩包 |
| `ksuinit-<arch>` | `ksuinit-<arch>.zip` | 原样发布上游压缩包 |

### 为什么不把所有 `.ko` 打成一个包

上游 `aarch64-<kmi>-lkm` 与 `x86_64-<kmi>-lkm` 中的模块**文件名完全相同**
（均为 `<kmi>_kernelsu.ko`）。如果像早期方案那样把全部 `.ko` 解包到同一目录再打成一个
`lkm-all.zip`，不同架构（以及不同 KMI）的文件会互相覆盖，导致发布出不兼容或被覆盖的模块。
因此本仓库按架构分别打包为 `lkm-aarch64-all.zip` 与 `lkm-x86_64-all.zip`，每个压缩包内保留
上游原始文件名。

### 不发布的产物

- `Spoofed-Manager-release`：其 APK 文件名与 `Manager-release` **完全相同**，同时发布会相互
  覆盖；如需伪装版请到上游运行页面下载。
- `Manager-mappings` / `Spoofed-Manager-mappings`：ProGuard 映射，属于调试信息。
- `telegram-bot-api-binary`：上游 CI 使用的 Telegram 机器人可执行文件。

## 异常处理

- **分页**：所有列表接口跟随 `Link` 头翻页。
- **API 限流**：遇到 403/429 时依据 `Retry-After` / `X-RateLimit-Reset` 退避重试。
- **下载失败 / 空产物 / 非 ZIP**：记录日志并跳过该产物，不影响其余产物。
- **空运行 / 缺少必要产物**：若没有 `Manager-release` 或没有有效 APK，则跳过该运行。
- **重复 Tag**：创建 Release 前先检查，若上游 Tag 冲突（`422 already_exists`）则跳过。
- **无效 APK 文件名**：只接受扁平、安全的 `*.apk` 名称，忽略带路径或异常的条目。
- **过期产物**：已过期的 artifact 直接跳过。

## 使用注意事项

- Release 中的 APK 由上游 CI 使用其正式密钥签名；伪装版签名密钥不同，混装会导致签名冲突。
- LKM 与内核版本（KMI）强相关，请选择与设备内核匹配的模块，不要跨 KMI/架构混用。
- 本仓库仅做搬运，不对构建结果做任何修改，也不保证可用性；遇到问题请向上游反馈。
- Tag 命名为 `BakaSU_<run_id>`，与上游正式发布 Tag（如 `v4.2.0`）无关，仅用于本仓库内部追踪。
