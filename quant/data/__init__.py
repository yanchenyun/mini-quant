"""数据层（Data Layer）—— 实现 ``core.abstractions`` 的两个接口契约。

本包是外部世界（行情源、数据库）与系统内部的翻译层。对上提供统一列
DataFrame（``code/dt/trade_date/open/high/low/close/pre_close/volume/
amount/trade_status/is_st``）；对下吸收各数据源 / 存储的脏格式（中文列名 /
毫秒级时间戳 / 复权标记差异 / 无 pre_close 等）。

模块划分
--------
- ``baostock_source``  Baostock 数据源适配器（免费、稳定）：归一 17 位毫秒时间戳、
                      支持 1d / 5/15/30/60min 分钟线（``_FREQ_MAP`` 一行加新频率）。
- ``akshare_source``   AKShare 数据源适配器（免费、聚合多源）：中文列名归一、
                      代码格式转换（``sh.600000`` ↔ ``600000``）、分钟数据按
                      30 天分段请求拼接、3 次重试应对网络抖动。
- ``wind_source``      Wind（万得）数据源适配器（需本机安装并登录 Wind 终端）：
                      WindPy 延迟导入（未装终端不影响其它源）、代码格式转换
                      （``sh.600519`` ↔ ``600519.SH``）、``wsd``/``wsi`` 频率
                      分发、字段降级容错、连接幂等复用。
- ``mysql_repo``       MySQL 行情仓储（实现 ``DataRepository`` 协议）：freq 分表
                      日线 ``ods_d_stock_quotation_i`` + 分钟 ``ods_mi_stock_quotation_i``，
                      upsert 幂等写入；分钟读取 LEFT JOIN 日线表回填
                      pre_close / trade_status / is_st（涨跌停基准 = 日线昨收）。
                      另含因子长表 ``dwd_factor_value_i`` 读写。
- ``timeutil``         时间戳归一工具 ``norm_dt``：识别 17 位毫秒串 / 14 位 /
                      8 位 / 无空格 等 5 种脏形态，统一归一为
                      ``YYYY-MM-DD HH:MM:SS``（毫秒丢弃）。

扩展方式
--------
- 新数据源（Tushare / Wind / ……）= 新增一个 ``MarketDataSource`` 实现 + 在
  ``app/service.get_source`` 注册一行。零改动其它代码（OCP）。
- 换存储（Parquet + DuckDB）= 重写 ``DataRepository`` 实现 + 在
  ``app/service.get_repo`` 装配处换一行。

参见
----
- 架构总览：``docs/ARCHITECTURE.md``
- 扩展指南：``docs/EXTENSION_GUIDE.md``
"""