# 来源与实现边界

核查日期：2026-09-15。

## 参考项目

1. [guxxxx/Coros2Xingzhe](https://github.com/guxxxx/Coros2Xingzhe)
   - 对照本地 `个人项目/高驰同步行者/`。
   - 参考命令行、环境变量、bcrypt 高驰登录、区域选择和 FIT 转移思路。
   - 新项目独立存放，未修改原项目。
2. [cyberjunky/python-garminconnect](https://github.com/cyberjunky/python-garminconnect/tree/0.3.15)
   - 安装并固定 `0.3.15`，依赖许可 MIT。
   - 使用 `Garmin(is_cn=False)`、`get_activities_by_date`、`get_activity_types`、`download_activity(...ORIGINAL)`、当前 JSON 会话接口。
   - 包要求 Python ≥3.12，故不沿用旧项目的 Python 3.11 配置。
3. [lingdu1234/sports-sync-x](https://github.com/lingdu1234/sports-sync-x/tree/69e482bd8e7158209574c67713690a45f895be9d)
   - 阅读 `app/coros/coros_client.py` 和对象存储模块，参考“临时存储凭据 → 上传 → 提交导入”的协议顺序。
   - 未复制其业务代码；没有在读取的根目录树中发现 LICENSE，不将其整个项目合并或重新发布。
   - 参考代码的旧 `pwd` 登录、静态 STS app/sign 参数和“status == 2 即成功”判断均未照搬。

## 高驰当前网页协议依据

公开网页：[COROS Training Hub](https://t.coros.com/)。本次页面显示应用版本 2.12.0。

- [main-z_jG60fu.js](https://staticcn.coros.com/coros-traininghub-v2/assets/main-z_jG60fu.js)：活动查询、下载、导入接口定义。
- [index-xPJtRBxS.js](https://staticcn.coros.com/coros-traininghub-v2/assets/index-xPJtRBxS.js)：当前导入组件。
  - 从 `/api/proxy/oss/sts?bucket=...&service=...&v=2` 获取临时上传凭据。
  - 中国/新加坡使用阿里云 OSS，美国/欧洲使用 AWS S3；存储区域从响应获取。
  - 对象键形如 `fit_zip/{userId}/{md5}.zip`。
  - `POST /activity/fit/import` 以 multipart `jsonParameter` 提交 `source/timezone/bucket/md5/size/object/serviceName/oriFileName`。
  - 当前网页按返回导入 ID 表示提交成功；本项目进一步查询活动列表并核对文件，才确认同步完成。
- [index-NKo67Jto.js](https://staticcn.coros.com/coros-traininghub-v2/assets/index-NKo67Jto.js)：活动列表 `startTime` 为 Unix 秒，页面显示时乘 1000 并处理时区。

以上为网页内部协议，并非厂商承诺稳定的开放 API。已核实公开代码结构；尚未使用用户账号验证接口可用性、MFA、STS、上传、导入格式和真实去重效果。源文件在 `tmp/reference/` 保留，仅供本地复核。

## 平台限制

- [How to Import Activities to Your COROS Account](https://support.coros.com/hc/en-us/articles/360040256971-How-to-Import-Activities-to-Your-COROS-Account)：支持 FIT/TCX、部分运动模式和导入大小要求。用户要求的“除骑行外都同步”体现为全部非骑行活动尝试；高驰不接受的活动明确保留未完成状态。
- [Garth](https://github.com/matin/garth)：作者已声明弃用。本项目使用现行 garminconnect 认证，不直接依赖 Garth。

## 官方 Actions 版本

本次从官方仓库 tag 查询并固定：

- actions/checkout v6 → `d23441a48e516b6c34aea4fa41551a30e30af803`
- actions/setup-python v6 → `ece7cb06caefa5fff74198d8649806c4678c61a1`
- actions/cache v5 → `caa296126883cff596d87d8935842f9db880ef25`

后续更新接口或依赖时，应重新核查并运行测试。
