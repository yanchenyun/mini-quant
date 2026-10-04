"""数据层（Data Layer）—— 实现 core.abstractions 的两个接口契约。

本包是外部世界（行情源、数据库）与系统内部的翻译层。对上提供统一列
DataFrame（code/dt/trade_date/open/high/low/close/pre_close/volume/
amount/trade_status/is_st/upper_limit/lower_limit）；对下吸收各数据源 /
存储的脏格式（中文列名 / 毫秒级时间戳 / 复权标记差异 / 无 pre_close 等）。

模块划分
--------
- wind_source      Wind（万得）数据源适配器（需本机安装并登录 Wind 终端）：
                  WindPy 延迟导入、代码格式转换（sh.600519 ↔ 600519.SH）、
                  wsd/wsi 频率分发、字段降级容错、连接幂等复用、
                  涨跌停价（maxup/maxdown 原始价口径，幅度法换算对齐
                  复权行情）等日线附加字段。
- mysql_repo       MySQL 行情仓储（实现 DataRepository 协议）：freq 分表
                  由注册表派生（日线 ods_d_stock_quotation_i + 5 分钟表，
                  15/30/60 分钟为派生频率不建表、读取时由 5 分钟聚合），
                  upsert 幂等写入；分钟读取 LEFT JOIN 日线表回填
                  pre_close / trade_status / is_st / 涨跌停价（日线口径）。
                  另含因子长表 dwd_factor_value_i 读写。
- registry         数据源与频率注册表（本层清单的单一事实来源）：数据源条目
                  存"模块名 + 类名"以便延迟导入（不把各家的第三方依赖绑到
                  本层），频率条目带行情表名与粒度。CLI 选项、服务层取源与
                  Web 校验全部由此派生，见模块内 docstring 的扩展说明。
- timeutil         时间戳归一工具 norm_dt：识别 17 位毫秒串 / 14 位 /
                  8 位 / 无空格 等 5 种脏形态，统一归一为
                  YYYY-MM-DD HH:MM:SS（毫秒丢弃）。

扩展方式
--------
新数据源 = 新增一个适配器文件（实现 MarketDataSource 契约），再到 registry
的 SOURCES 里加一行。CLI 的 --source 选项、服务层取源与 Web 侧校验全部由
该清单派生，入口代码零改动；适配器之间互不影响。

换存储 = 重写一个 DataRepository 实现（一个文件），再到 app/service.get_repo
装配处换一行。

新频率分两支。派生频率（源数据更细、目标可聚合而得，如 15/30/60 分钟
从 5 分钟聚合）= 在 registry 的 FREQS 里加一行并填 derived_from 指向源
频率，共用源频率表，读取时由仓储自动聚合，零建表零入库。独立表频率
（源数据直达该粒度）= 加一行并给独立表名，建表 DDL 自动派生（分钟频率
共用同一列口径），另需补仓储读写分派与适配器取数分支。

参见
----
- 开发文档（架构 / 接口契约 / 新数据源步骤）：docs/DEVELOPMENT.md §8
- 操作文档（数据源选择 / 网络排错）：根目录 ReadMe.md §6
"""