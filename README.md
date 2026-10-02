# 佳明国际版同步高驰（Garmin2Coros）

从 Garmin Connect 国际版读取最近 **7 天（含今天）**的已完成活动，涵盖所有运动类型（包含骑行），只将高驰缺少的记录通过原始 FIT/TCX 补传到 COROS Training Hub。提供命令行和 GitHub Actions。

所有运动类型都会进入处理流程，但最终能否导入受高驰支持范围和原始文件完整性限制。本次规则更新使用合成数据验证，真实运行结果以 Actions 日志及高驰记录为准。自动同步由 `AUTO_SYNC_ENABLED` 控制。

## 同步范围

- 跑步、骑行（含室内、山地、电助力等子类型）、步行、徒步、游泳、力量及其他运动均尝试同步。
- 混合运动即使含骑行段也会处理。原始文件保持完整，不拆分铁三等混合记录；高驰若拆分为多条而无法完整匹配，会保留待核实状态。
- 默认按北京时间筛选今天及之前 6 天。例如 2026-10-02 运行时处理 2026-09-26 至 2026-10-02，范围外的历史活动不会上传。
- 高驰已有匹配记录时跳过；本范围内曾同步、后来在高驰删除的记录也会补回。
- 只处理已经完成的活动，不同步步数、睡眠、日常心率或未来训练计划。
- 只上传源平台原始 FIT/TCX，不转 GPX、不伪造运动类型。文件已有的轨迹、心率、功率和分圈保持不变；高驰是否展示全部字段由其导入器决定。
- 没有原始文件、损坏文件、包含多个活动文件的 ZIP 会明确报未完成。未知类型不会伪装为跑步。

## 安装

支持 macOS / Linux，Python **3.12 以上**。本地交付已创建 `.venv`，可直接使用。新环境执行：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

`requirements.in` 记录直接依赖，`requirements.txt` 固定本次验证的完整版本。当前佳明库为 `garminconnect==0.3.15`，使用其现行认证实现。

## 账号与首次登录

从项目目录运行。凭据通过环境变量提供，避免把真实值写入脚本、Git 或聊天。以下均为占位符：

```bash
export GARMIN_USERNAME="佳明国际版账号"
export GARMIN_PASSWORD="佳明密码"
export COROS_USERNAME="高驰账号"
export COROS_PASSWORD="高驰密码"
export COROS_REGION="cn"
export COROS_ACCOUNT_TYPE="2"
```

`COROS_REGION` 支持 `cn/us/eu/sg`，默认 `cn`；登录后按账号返回区域选择接口。`COROS_ACCOUNT_TYPE` 默认 `2`，邮箱使用 `2`，手机号按账号实际登录方式使用 `1`。

佳明需要验证码时，先在本地交互终端执行：

```bash
python garmin_to_coros.py login
```

验证码输入不回显，会话保存在 `.auth/garmin_tokens.json`，目录权限 `0700`、文件权限 `0600`。以后运行会复用、刷新该会话。也支持通过 `GARMIN_TOKENS` 环境变量提供该文件的**完整 JSON 内容**，适合 GitHub Secrets；内联会话失效时需重新登录并更新 Secret。

未设置会话时也会尝试用户名和密码登录。非交互同步遇到验证码会失败退出，不等待输入，不将失败当作无活动。佳明可能限制云服务器 IP；如 Actions 认证失败，可在本地运行同一程序，不保证所有云端登录可用。

## 使用

先预览。预览会登录、读取活动、下载原始文件并核对高驰已有记录，不提交高驰导入，也不改写同步台账；佳明库仍可能刷新本地登录会话。

```bash
python garmin_to_coros.py --dry-run
python garmin_to_coros.py --dry-run --days 3 --limit 5
```

执行同步：

```bash
python garmin_to_coros.py --apply
python garmin_to_coros.py --apply --start 2026-09-01 --end 2026-09-15
```

日期范围包含首尾当天，默认以 `Asia/Shanghai` 划分，支持 `--timezone`。默认 `--days 7` 含今天共 7 天；手动传入 `--days` 或 `--start/--end` 可覆盖默认范围。`--limit` 限制本次检查的活动候选数，按时间从早到晚，包含已同步候选，不等于新增上传数。

## 去重、失败与恢复

1. 完整分页读取高驰活动列表。
2. 对开始时间接近的记录下载原始文件，核对运动类型、开始时间、运动时长和可用距离。不会仅凭日期或活动名称去重。
3. 上传文件至高驰使用的临时对象存储后，**先持久记录待核实状态，再提交一次导入请求**。提交超时不会自动重发。
4. 提交后轮询高驰列表，并核对导入活动，确认后才计为“新增并确认”。默认最多等待 60 秒，可调 `--poll-seconds`。
5. 高驰异步处理未完成、拒收或无法核对时保留“待核实”。下次运行先检查目标记录，找到匹配才确认；不会盲目重传。
6. 本次日期范围内曾经确认同步、后来在高驰消失的活动会重新补传；不修改高驰已有的记录。同 ID 的源文件摘要改变时仍暂停该活动，避免未经核对处理内容变化。

如果 Training Hub 显示失败或确认活动确实未导入，可显式重试：

```bash
python garmin_to_coros.py --apply --retry-pending --start 2026-09-15 --end 2026-09-15
```

该参数会重试所选日期范围内的待核实记录，应先核对平台状态并缩小范围。单条失败后继续处理后续活动；任何失败或待核实会以退出码 `2` 结束，全部完成或预览无异常返回 `0`。

台账存于 `.state/ledger.json`，记录账号组合哈希、活动 ID 哈希、原文件压缩包哈希、状态和更新时间，不含账号密码、token、坐标或原始 FIT。台账损坏时停止，不静默重置。同一状态目录带进程锁。**不要同时在不同电脑、状态目录和 Actions 上传同一账号**；跨机器没有分布式锁，远端列表也存在处理延迟。

高驰官方导入说明要求 FIT/TCX，并列出支持活动和大小限制；小文件或不支持的运动类型可能无法导入。本程序不会通过填充文件或改运动类型绕过限制。[高驰官方导入说明](https://support.coros.com/hc/en-us/articles/360040256971-How-to-Import-Activities-to-Your-COROS-Account)

## GitHub Actions

已提供 `.github/workflows/sync.yml`，在仓库默认分支运行：

- 手动运行默认仅预览，勾选 `apply` 才实际上传；可选择日期和重试待核实记录。
- 设置仓库变量 `AUTO_SYNC_ENABLED=true` 后，计划每天北京时间 **03:00** 补齐最近 **7 天**缺少的所有运动记录（包含骑行）；GitHub 调度可能延迟。
- 同步串行运行，不取消正在执行的同步。单次上限 45 分钟，大批量补录建议分日期运行。
- 每次先执行离线测试；官方 Actions 固定到具体 commit。

在 `Settings → Secrets and variables → Actions` 配置：

| Secret | 用途 |
|---|---|
| `GARMIN_USERNAME` / `GARMIN_PASSWORD` | 佳明国际版登录；使用有效会话时可不提供 |
| `GARMIN_TOKENS` | 推荐提供本地生成的完整会话 JSON；失效后手动更新 |
| `COROS_USERNAME` / `COROS_PASSWORD` | 高驰登录，必填 |
| `COROS_REGION` | 可选，默认 `cn` |
| `COROS_ACCOUNT_TYPE` | 可选，默认 `2` |

推荐先手动预览，核对后手动执行一次，建立台账，再启用 `AUTO_SYNC_ENABLED`。建议使用私有仓库，因为运行日志含活动 ID、运动类型及日期范围。

Actions 只缓存 `.state/ledger.json`，不缓存 `.auth`、运动文件或凭据。每次用唯一缓存键保存台账，并在任务失败后尝试保存。定时任务若未恢复到台账，会停止上传，要求先人工核实并手动初始化。

**缓存不是可靠数据库**：GitHub 可能回收缓存，强制取消或 runner 故障也可能丢失最后一次状态。即使恢复到较旧缓存，也可能遗漏仍在高驰处理的提交；无法保证跨 runner 故障的严格“恰好一次”。中断后先核对高驰导入列表。需要持久状态的长期运行可选择固定本机环境。

## 文件与验证

```text
garmin_to_coros.py       命令行入口
garmin2coros/domain.py   FIT/TCX 校验、活动匹配
garmin2coros/clients.py  两个平台的登录、读取和高驰上传
garmin2coros/sync.py     同步台账、进程锁及重试流程
garmin2coros/cli.py      参数、日期和执行模式
tests/test_sync.py      合成文件和模拟接口测试
REFERENCES.md           参考来源、协议依据和已知差异
VALIDATION.md           本次实际验证结果及缺口
```

```bash
python -m pip check
python -m unittest discover -s tests -v
python -m compileall -q garmin_to_coros.py garmin2coros tests
python garmin_to_coros.py --help
```

`.auth/`、`.state/`、`.venv/`、`tmp/` 和原始运动文件均被 Git 忽略。下载的运动文件只在内存处理；`tmp/reference/` 是开发时的公开接口取证，不纳入仓库。目录暂保留供复核，清理需另行授权。

停止使用时关闭自动同步变量即可；程序无删除源平台或高驰活动的功能。已导入的运动如需撤销，应先在高驰核对具体记录后另行处理。
