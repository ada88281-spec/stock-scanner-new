# 盘后选股 + 盘前确认 推送系统

A 股全市场扫描，结果通过 pushplus 推送到微信。
每天推送两批消息，构成完整的"选股 → 确认 → 执行"闭环。

## 推送节奏

| 时间（北京时间） | 内容 | 数据依据 | 用途 |
|-----------------|------|---------|------|
| 17:30 收盘后 | 盘后候选池 | 当日已收盘日线 | 真正的选股依据，含均线/ATR/MACD/六维打分 |
| 09:35 开盘后 | 盘前最终名单 | 当日盘中快照（量比/换手/成交额） | 过滤掉"昨天信号好、今天没资金跟进"的票 |

**为什么分两步**：盘前拿不到当日量比（成交量还是 0），所以盘前只能确认形态和位置；
开盘 5 分钟后第一波成交量形成，才能判断资金是否真跟进。只做第一步会买到"放量一日游"的票。

## 部署（GitHub Actions，零成本）

1. 建仓库 `stock-scanner`，上传本目录全部文件
2. `Settings → Secrets and variables → Actions → New repository secret`
   - Name: `PUSHPLUS_TOKEN`
   - Value: 你的 pushplus token
3. 仓库 → `Actions` → `Run workflow` → 选 `scan` 手动测一次
4. 之后每天自动跑

## 手动运行

```bash
python scan.py scan     # 盘后扫描
python scan.py confirm  # 盘前确认（需要已有缓存）
```

## 六维打分权重

| 维度 | 权重 |
|------|------|
| 信号强度 | 20 |
| 量能验证 | 18 |
| 均线位置 | 17 |
| 资金确认 | 17 |
| 波动适配 | 14 |
| 风险过滤 | 14 |

入选线：候选池 ≥55 分，最终名单 ≥65 分。

## 可调参数（环境变量覆盖）

CAPITAL / RISK_PCT / ATR_MULT / MAX_TOTAL_POS / MAX_SINGLE_POS /
MIN_SCORE / FINAL_MIN_SCORE / VOL_RATIO_MIN / TURN_MIN / TURN_MAX /
AMOUNT_MIN / ATR_PCT_MIN / ATR_PCT_MAX / TOP_N

## 已知限制

- 盘中快照用东方财富接口，9:30 前调用会返回空数据
- 交易日判断为简化版（只排除周末和大致节假日），每年需更新一次节假日列表
- GitHub Actions 定时可能延迟 3~15 分钟
