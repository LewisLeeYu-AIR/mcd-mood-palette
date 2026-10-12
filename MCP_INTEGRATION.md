# MCP 集成说明（MCP_INTEGRATION.md）

## 1. MCP Server

| 项 | 值 |
|---|---|
| Server 名称 | `mcd-mcp` |
| 接入地址 | `https://mcp.mcd.cn`（唯一官方地址） |
| 传输协议 | Streamable HTTP |
| 认证 | 请求头 `Authorization: Bearer <MCP_TOKEN>`（Token 由申请者本人持有，严禁入库） |
| 限流 | 每 Token 每分钟 600 次；429 需退避；401 提示检查 Token |

## 2. Tool 清单（本项目实际调用 17 个）

| 工具 | 用途（本项目内） |
|---|---|
| `now-time-info` | 推断当前用餐时段（breakfast/lunch/afternoon/dinner），驱动推荐引擎 period 参数 |
| `list-nutrition-foods` | 拉取 160+ 餐品营养库（能量/蛋白/脂肪/碳水/钠/钙），解析为结构化缓存，24h 复用 |
| `query-nearby-stores` | 按定位取附近门店（含得来速 beCode） |
| `query-meals` | 按门店与场景（beType/orderType/beCode）拉真实菜单与商品 code，是名称桥接与下单的前置 |
| `query-meal-detail` | 餐品组成与特调选项（modification，selectedKey/unselectedKey 规则） |
| `calculate-price` | 组合算价（返回分，展示转元）；到店场景取 takeWayList.takeWayCode |
| `create-order` | 创建订单（五场景参数差异见调用流程） |
| `query-order` | 支付后状态跟踪（支付完成/失败确认） |
| `order-list` | 用户查历史订单 |
| `cancel-order` | 用户明确确认后的订单取消 |
| `campaign-calendar` | 「尝新」情绪联动当日营销活动（新品/联名），丰富推荐理由 |
| `query-meal-assistance` | 团餐场景取 gmServiceCode |
| `delivery-query-stores` | 外送/团餐可送门店 |
| `delivery-query-addresses` | 外送地址列表（取 addressId） |
| `delivery-create-address` | 用户无历史地址时新建（需用户确认） |
| `available-coupons` | 算价后只读提示订单可用券（门店+订单类型维度，不自动绑定） |
| `query-my-coupons` | 用户卡包券资产（order_playbook §8 优惠券环节，items 带 couponId/couponCode 的来源） |

## 3. 调用流程（三条主链路）

### 3.1 个人推荐主链（到店自取示例）

```
用户触发
→ now-time-info（时段）
→ [本地] profile_manager get / init（档案）
→ [本地] 营养缓存检查（>24h 则：list-nutrition-foods → parse_nutrition.py）
→ [本地] recommend_engine.py --mood ... （推荐引擎）
→ 用户选定组合【确认①】
→ query-nearby-stores → 用户选店
→ query-meals(storeCode, orderType=1, beType=1)
→ [本地] bridge_name.py match（营养名↔菜单 code 映射）
→ query-meal-detail(code)（如需特调）
→ calculate-price(items)（取 takeWayCode）
→ 订单复述 + 用户「确认下单」【确认②】
→ create-order(storeCode, orderType=1, beType=1, items, takeWayCode)
→ 支付引导 → query-order 状态闭环【确认③】
```

### 3.2 外送分支

主链同上，门店环节改：`delivery-query-stores → delivery-query-addresses`（无地址则 `delivery-create-address`，需用户确认）→ `query-meals(beType=2, beCode)` → `calculate-price` → `create-order(orderType=2, addressId, beCode)`。

### 3.3 团餐分支

```
触发（团队/聚餐）→ 采集目的/人数/人均预算
→ [本地] recommend_engine.py --mode party（官方 5 档规则）
→ query-nearby-stores / delivery-query-stores（确认支持团餐）
→ query-meal-assistance → gmServiceCode
→ query-meals(beType=6, beCode) → bridge_name → calculate-price
→ 复述（数量×人数）+ 确认下单【确认②】
→ create-order(beType=6, orderType=2, addressId, beCode, gmServiceCode)
→ 支付与状态闭环
```

## 4. 业务价值

- **个性化决策辅助**：把「翻 10 分钟菜单」压缩成「看 30 秒调色盘」——情绪体验匹配 + 营养区间过滤 + 过敏原/忌口黑名单，三个维度同时收敛选择空间
- **真实业务闭环**：不止于推荐，而是从营养数据到门店菜单、算价、下单、支付跟踪的全链路真实调用（非 mock 演示）
- **健身人群友好**：160+ 餐品营养数据按目标/训练日/时段动态计算区间，输出可解释的营养带
- **团队场景扩展**：团餐模式按官方预算档位规则组合，覆盖 B 端聚餐决策

## 5. 错误与限流策略

| 场景 | 处理 |
|---|---|
| 401 | 提示用户检查 WorkBuddy 连接器 Token |
| 429 | 等待 60s 重试 1 次，再失败告知稍后再试 |
| 常态限流 | 营养数据 24h 本地缓存；同会话不重复调用已成功工具；单次完整会话调用预算 ≤15 次（远低于 600/min） |
