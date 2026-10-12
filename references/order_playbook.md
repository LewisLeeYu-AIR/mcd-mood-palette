# 下单操作手册（order_playbook.md）

> 本文件供 LLM 在进入下单阶段时加载。覆盖五场景参数、特调 key 规则、团餐与预约细节、订单生命周期。

## 1. 五场景参数总表

| 场景 | beType | orderType | beCode 来源 | addressId | 必传额外参数 |
|---|---|---|---|---|---|
| 到店自取 | 1 | 1 | **不传** | 不传 | takeWayCode |
| 得来速 | 5 | 1 | query-nearby-stores | 不传 | takeWayCode |
| 麦乐送 | 2 | 2 | delivery-query-stores | delivery-query-addresses（无则先 create） | remark 选填（≤50字） |
| 预约 | 同所属场景 | 同左 | 同左 | 同左 | reservationDate（yyyy-MM-dd HH:mm） |
| 企业团餐 | 6 | 2 | delivery-query-stores | 同麦乐送 | gmServiceCode（query-meal-assistance） |

**takeWayCode**：来自 `calculate-price` 返回的 `data.takeWayList[].code`，到店/得来速场景必传；外送/团餐不传。

## 2. 订单确认铁律（对齐 C4 质量标准）

1. **确认①**：用户从推荐卡片选定组合（明示字母编号或餐品名）
2. **确认②**：复述表格 + 用户消息中必须包含「确认」字样（如"确认下单"）才执行 create-order
3. **确认③**：create-order 后展示支付引导，跟踪支付状态（query-order）；取消订单（cancel-order）同样需要用户明确确认

## 3. 特调（modification）规则

来自 `query-meal-detail`：
- 商品或 choice 标注 `supportModify=true` → 名称后标【可特调】，但不主动展开选项
- 用户要求特调时，取 modification 选项构建 items[].modification.values：
  - 选中的项：`key = selectedKey`
  - 未选中的项：若该组 `unselectedKey` 非空则**必须一并传入**（key = unselectedKey）
  - 即：含 unselectedKey 的特调组，组内所有项都要传
- calculate-price 与 create-order 的 items 必须携带一致的 modification

## 4. 套餐子项（roundList）

套餐类商品若返回 roundList（套餐 round 分组），下单时需构建 `items[].roundList[]`：每个 round 内 comboItemList 填用户选择的子商品（code/quantity/modification）。默认选项取 isDefault=1 的 choices。

## 5. 团餐细节

- `query-meal-assistance` 返回助餐服务，取 gmServiceCode；调用失败或为空 → 该店不支持团餐，建议换店
- 预算档位（官方规则，引擎已内置）：人均 <20 小食 / 20-30 汉堡+小食或饮料 / 30-40 汉堡+薯条小食+饮料 / 40-50 汉堡+薯条+小食+饮料 / >50 丰富组合（小食不重复）
- 团餐订单必传：beType=6、orderType=2、beCode、addressId、gmServiceCode

## 6. 预约点餐细节

- reservationDate 格式：`yyyy-MM-dd HH:mm`（如 2026-10-11 12:30）
- query-meals / calculate-price / create-order 三个接口在预约场景都要传 reservationDate
- 引导话术：明天中午还是今天晚上？（日期芯片 + 时段芯片）

## 7. 订单生命周期

```
create-order 成功 → 展示支付链接/引导（扫码或麦当劳 APP）
  ├─ 用户："支付完成" → query-order 确认状态并恭喜
  ├─ 用户："支付失败" → query-order 查状态，引导重新支付
  ├─ 用户："订单状态" → query-order 反馈
  ├─ 用户："取消订单" → 复述订单号+内容，明确确认后 cancel-order
  └─ 用户："我的订单" → order-list 列出近期订单
```

## 8. 价格与单位

- `calculate-price` 返回金额单位为**分**，展示时 ÷100 转元
- 优惠券：items 内可带 couponId/couponCode（来自 query-my-coupons）；算价后可调 available-coupons 提示用户有可用券，但**不自动绑定**（auto-bind-coupons 属写操作，需确认）
- **算价与下单间价格漂移**：极小概率 calculate-price 与 create-order 返回价不一致（门店侧价格变动）。约定：以 **create-order 返回价**为准向用户复述差价并再次口头确认；差价 >10% 或用户不接受 → 立即 cancel-order（免费窗口内）并回到推荐

## 9. 平台错误码与桥接兜底（六情绪测试实测，2026-10-10）

### 9.1 平台错误码

| 错误码 | 消息 | 含义 | 处理 |
|---|---|---|---|
| 600039 | 今日下单数已经上限 | 账号级每日下单硬上限（实测当日累计创建第 5 笔后触发） | 停止创建订单，告知用户次日再试或改用麦当劳 APP；已创建订单正常跟踪/取消 |
| 508 | 太火爆了，请稍后再试 | 瞬时繁忙（并发突发时出现） | 等待 60 秒重试 1 次，仍失败则提示稍后再试 |
| 401 | — | Token 无效/过期 | 检查连接器 Token |
| 429 | — | 限流（600 次/分钟） | 退避重试 |

**未支付订单生命周期**：支付窗口为创建后 **15 分钟**（expirePayTime 字段），超时未支付的订单将自动取消（实测确认）。告知用户支付时效，超时后需重新下单。

### 9.2 桥接兜底规则（实测确认）

1. **bridge_name 的匹配结果为候选**，最终以 `query-meal-detail` 返回的 `data.name`（标准名）为准。实测：单串「玉米杯」被误配为「大杯玉米杯」，实际标准名为「小杯玉米杯」；单串「可乐」被误配为「无糖可口可乐中杯」，实际是可选杯型的容器商品
2. **容器商品**（supportModify 之外带必选 round 的商品）：必须用 roundList 传参，`round` 字段传 round 的 **id 字符串**（如 "1"），`comboItemList` 内传选中的 choice code（如可乐中杯=3050、脆汁鸡琵琶腿=505416）
3. **替换发生时**：重算替换后组合的营养估算并向用户提示差异（实测替换品与原品可差 ±100 kcal 以上）；差异导致超出营养带时，按「换小份/换饮品」话术建议调整
