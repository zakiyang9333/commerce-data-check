# Commerce Data Check

A small, read-only daily CSV validator for commerce dashboards. Python 3.9 or newer; no external dependencies.

早期项目：从实际电商看板的数据健康检查需求出发，独立编写通用实现。当前没有已验证的外部采用情况。

## Quick start / 快速开始

```sh
python3 check_data.py examples/healthy.csv --config examples/config.json --date 2026-01-01
python3 check_data.py examples/broken.csv --config examples/config.json --date 2026-01-01
python3 -m unittest discover -s tests -v
```

第一个示例正常，退出码 0；第二个示例预期失败，退出码 1，包含重复主键、日期不符、负数和无效数值。示例全部为虚构数据。

## What it checks / 检查内容

- Required columns and values; unique composite keys.
- Every row belongs to the requested date.
- Numeric values are finite and meet configurable bounds.
- Missing files, empty data, invalid headers and malformed row widths.
- Missing files before an explicit readiness deadline return `waiting`, rather than a false failure.

配置中的 `date_column` 指定日期列，日期使用 YYYY-MM-DD；`key_columns` 定义重复判断维度，必须符合实际数据粒度。`numeric_columns` 可逐列设置 min/max；默认示例限制销售和推广费不小于零，退款等允许负值的业务应修改配置。数值使用普通十进制，不接受货币符号、千位分隔符或百分号。

可选 `ready_after` 必须是包含时区的绝对时间，例如 `2026-01-02T10:00:00+08:00`。这是单次检查的截止时间，每日任务需传入对应日期的配置，不能永久复用旧截止时间。仅文件缺失时适用等待状态，已存在文件的内容错误仍报告失败。

`allow_empty: true` 表示业务明确允许只有合法表头、没有记录的文件；它不是实际零成交的证明。完全空白的文件仍失败。

## Report / 报告

JSON 输出只包含状态、目标日期、行数、问题类型、数据记录序号和字段名，不输出单元格内容。记录序号从 1 开始，不计表头和空白记录，不是原文件物理行号。字段名仍可能包含业务信息，分享报告前应检查。

Exit codes: `0` healthy/valid_empty; `1` failed; `2` waiting; `3` unreadable input or invalid configuration. 输入文件和配置只读；工具不连接网络、不启动浏览器、不修改数据库。

## Current limits / 当前边界

只支持 UTF-8 CSV，含 BOM 的 CSV 也可用；不支持 Excel、数据库核对、跨日覆盖检查、自动采集、自动回补或 Web 界面。整文件读入内存，暂不适合超大文件。配置由本地维护者提供，当前没有完整的配置结构校验器。

## Contributing

Please provide synthetic sample data, expected behavior and a reproducible command. Do not attach customer records, credentials or private exports. Improvements should include a test that demonstrates the behavior.

## Release status

Early-stage project. No external adoption, established community maintenance history, or OpenAI program selection is claimed. Developed with AI assistance. Licensed under the MIT License; see LICENSE.
