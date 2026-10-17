# BakaSU_CI

独立的 CI 自动同步与发布仓库，用于自动获取 [BakaSU](https://github.com/Baka-SU/BakaSU) 官方
GitHub Actions 的成功构建产物，并发布到本仓库自己的 GitHub Releases。

本仓库**仅面向 ARM64（aarch64）设备**，只搬运 ARM64 相关的安装包与必要组件。

上游仓库**只读**，本仓库不会向其提交任何内容，也不会重新编译内核或安装包。

## 自动同步机制

`.github/workflows/sync.yml` **每小时运行一次**（`0 * * * *`），也可以在 Actions 页面手动
触发。工作流调用 `scripts/sync_release.py`：

1. 通过 GitHub API 拉取上游 `Build Manager` 工作流在 `main` 分支上**已成功完成**的运行记录
   （`status=success`、`conclusion=success`），并过滤掉 `pull_request` 事件。
2. 对每个运行，检查本仓库是否已经存在对应 Tag / Release；已存在则跳过，避免重复发布。
3. 下载该运行的构建产物，按架构筛选并整理后创建 Release，Tag 形如 `BakaSU_<run_id>`。
4. 每个 Release 记录上游提交 SHA、Actions Run ID、运行链接、分支与构建时间，便于追溯。

工作流使用最小权限的 `GITHUB_TOKEN`（仅 `contents: write`），不依赖任何自定义密钥，也不会
输出或提交密钥。

### 手动触发参数

- `limit`：检查最近多少条成功运行，默认 50。
- `dry_run`：只准备产物、不发布，用于验证。

## ARM64 筛选规则

架构判定基于**实际构建产物**，不只看工件名称：

- **管理器 APK**：读取 APK 内的 `lib/<abi>/` 目录。仅当 APK **只包含** `arm64-v8a` 时保留。
  因此 `universal`（同时含 `arm64-v8a` 与 32 位 `armeabi-v7a`、`riscv64`、`x86_64`）与
  `armeabi-v7a`、`x86_64`、`riscv64` 这些 APK 均被排除。
- **`ksud` / `ksuinit`**：打开压缩包，读取主可执行文件的 ELF 头 `e_machine` 字段，仅保留
  `AArch64`。例如 `ksud-armv7-linux-androideabi` 是 **32 位 ARM**（并非 arm64），会被排除。
- **内核模块（LKM）**：只保留 `aarch64-*` 工件，并再次校验模块本身是 AArch64 ELF；排除全部
  `x86_64-*`。

> 注意：Android 的 ABI 名称 `arm64-v8a` 与内核模块使用的 `aarch64` 是**两套不同的命名字段**，
> 因此 APK 与 LKM 分别按各自的实际产物识别，不能互相套用。

## 产物来源与命名

产物直接来自上游 `Build Manager` 运行。**APK 与工具类产物保留原始文件名**：

| 上游产物 | 本仓库 Release 中的名称 | 说明 |
| --- | --- | --- |
| `Manager-release` | `BakaSU_<version>_<code>-arm64-v8a-release.apk` | 仅保留 `arm64-v8a` 这一个 APK |
| `aarch64-<kmi>-lkm` | `<kmi>_kernelsu.ko` | 每个 KMI 一个独立模块文件 |
| `ksud-aarch64-linux-android` | `ksud-aarch64-linux-android.zip` | 原样发布上游压缩包 |
| `ksuinit-aarch64` | `ksuinit-aarch64.zip` | 原样发布上游压缩包 |

### 为什么不把所有 `.ko` 打成一个包

上游 `aarch64-<kmi>-lkm` 与 `x86_64-<kmi>-lkm` 中的模块**文件名完全相同**
（均为 `<kmi>_kernelsu.ko`），而且不同 KMI 之间并不兼容。如果把全部 `.ko` 解包到同一目录再打
成一个 `lkm-all.zip`，不同架构（以及不同 KMI）的文件会互相覆盖，发布出不兼容或被覆盖的模块。
因此本仓库**按 KMI 逐个发布独立模块文件**（文件名即 `<kmi>_kernelsu.ko`），不会把不同 KMI 的
模块合并为一个不加区分的安装包。

### 不发布的产物

- **非 ARM64 产物**：`x86_64-*` LKM、`ksud-x86_64/armv7/riscv64-*`、`ksuinit-x86_64/riscv64`，
  以及非 `arm64-v8a` 的 APK。
- `Spoofed-Manager-release`：其 APK 文件名与 `Manager-release` **完全相同**，同时发布会相互
  覆盖；如需伪装版请到上游运行页面下载。
- `Manager-debug`：调试版 APK。
- `Manager-mappings` / `Spoofed-Manager-mappings`：ProGuard 映射，属于调试信息。
- `telegram-bot-api-binary`：上游 CI 使用的 Telegram 机器人可执行文件。

## 异常处理

- **分页**：所有列表接口跟随 `Link` 头翻页，并设有页数上限防止失控。
- **API 限流**：遇到 403/429 时依据 `Retry-After` / `X-RateLimit-Reset` 退避重试。
- **下载失败 / 空产物 / 非 ZIP**：记录日志并跳过该产物，不影响其余产物。
- **空运行 / 缺少必要产物**：若没有 `Manager-release` 或没有有效 ARM64 APK，则跳过该运行。
- **重复 Tag**：创建 Release 前先检查，若上游 Tag 冲突（`422 already_exists`）则跳过。
- **无效 APK 文件名**：只接受扁平、安全的 `*.apk` 名称，忽略带路径或异常的条目。
- **过期产物**：已过期的 artifact 直接跳过。
- **架构无法判定**：无法确认为 ARM64 的产物一律不发布，并在日志中说明原因。

## 使用注意事项

- Release 中的 APK 由上游 CI 使用其正式密钥签名；伪装版签名密钥不同，混装会导致签名冲突。
- LKM 与内核版本（KMI）强相关，请选择与设备内核匹配的模块，不要跨 KMI 混用。
- 本仓库仅做搬运，不对构建结果做任何修改，也不保证可用性；遇到问题请向上游反馈。
- Tag 命名为 `BakaSU_<run_id>`，与上游正式发布 Tag（如 `v4.2.0`）无关，仅用于本仓库内部追踪。
