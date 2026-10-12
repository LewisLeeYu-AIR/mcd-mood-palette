---
name: mcd-mood-palette
description: 麦麦心情调色盘——根据用户今日心情、身体状况与健身安排，推荐匹配当日状态的麦当劳搭配，并协助完成到店自取、得来速、麦乐送外送、预约点餐、企业团餐五种场景的下单与订单跟踪。当用户说"今天吃什么麦麦""推荐个麦当劳套餐""麦麦心情调色盘""麦门今日推荐""健身完吃点什么麦当劳""团队聚餐点麦当劳""今天心情不错想吃点好的"等时使用。核心能力：情绪体验匹配（生成今日情绪色）、营养区间过滤（热量/蛋白质/过敏原/忌口）、真实门店菜单算价下单（三步确认）、订单状态跟踪。
---

# 麦麦心情调色盘

今天的心情，值得一份什么样的麦麦。按心情调出今日情绪色，按身体状况守住营养带，选中即可一路跟到下单。

**五步工作流**：采集信息 → 生成推荐 → 用户选定 → 复述确认 → 下单跟踪。

## 适用场景

- 用户想决定今天吃什么麦当劳（任何措辞均可能触发）
- 用户提到心情状态并想点麦当劳
- 健身人群纠结练后吃什么
- 团队要订麦当劳聚餐/团建

## 信息采集（两轮制，全部选择题）

**第 0 步**：调 MCP `now-time-info` 推断用餐时段（breakfast/lunch/afternoon/dinner），不问用户。

**首轮（仅首次，档案不存在时）**：运行 `python scripts/profile_manager.py get`，若 `exists: false` 则用选择题芯片采集三项档案后 `init` 写入：
- 过敏原（多选：无/麸质/乳制品/鸡蛋/鱼类/海鲜（虾蟹）/大豆/坚果花生）
- 忌口（自由短语 + 常用项芯片）
- 长期健身目标（减脂/增肌/保持/不设）

**次轮（每次）**：两道单选题：
- 今日心情（6 选 1，选项文案见 `references/emotion_palette.json` 的 chip_text）
- 今日训练（力量/有氧/休息日/今天没关注）

**纪律**：一次只问一题；用户拒绝回答就跳过并用默认值；预算默认不问（用户主动提预算时作为 --budget 传入）。

## 推荐生成

营养缓存检查：`data/nutrition_cache.json` 不存在或 `fetched_at` 超 24 小时 → 调 MCP `list-nutrition-foods`，把返回的 data 字段保存为 `data/nutrition_raw.txt`，运行 `python scripts/parse_nutrition.py --input data/nutrition_raw.txt`。

然后运行：
```
python scripts/recommend_engine.py --mood <mood_key> --training <力量|有氧|休息日> --goal <减脂|增肌|保持|无> --period <breakfast|lunch|afternoon|dinner> [--exclude "已展示候选名,逗号分隔"]
```

「换一批」用尽 9 个候选后：带 `--exclude`（已展示的套餐名或餐品名）重跑，保证不重复；返回空候选时引导用户改约束（换个心情/时段），不要原地空转。

**v2 套餐优先机制**：引擎同时生成「套餐池」（`references/set_meals.json`，含构成/参考价/可换子项/体验标签）与「单品组合池」，统一按 `0.40×营养 + 0.25×情绪 + 0.20×性价比 + 0.15×套餐加成` 打分排序——**套餐默认主推**（自带折扣结构，省钱率高），单品组合在套餐情绪/营养严重不匹配时自然进入前三。

引擎返回 JSON 关键字段：
- `emotion_palette`（今日情绪色与话术）/ `nutrition_band`（热量/蛋白区间）
- `set_meal_status`：套餐池状况；`degrade_note` 非空表示套餐候选不足已降级（下午茶/早餐等时段），渲染时如实告知用户「当前时段套餐少，为你推单品组合」
- `weights`：当前评分权重（可审计）
- `candidates`：两类候选统一按 score 排序，均含 `score_breakdown`（nutrition/emotion/value/set_bonus 分项，可解释）
  - `kind: "set_meal"` **套餐（默认主推）**：`composition`（构成）、`price_ref`（套餐价参考）、`original_price_ref`（单点合计）、`savings`（省额）、`swap_options`（换子项降档建议，如中薯条→小杯玉米杯 -236kcal）、`nutrition_estimated`（营养估算标记；为 true 时卡片加脚注）、`menu_code_hint`
  - `kind: "combo"` 单品组合：`items`、`over_budget`、`downsize_options`

**选店后套餐复核（必做）**：用 `query-meals` 在真实菜单中查找套餐名（`menu_code_hint` 兜底）——
- 存在且 `currentPrice` 与 `price_ref` 偏差 ≤15% → 正常推荐
- 缺失或偏差 >15% → 告知用户并换下一候选，**不得按过期价格承诺**

**LLM 职责**（引擎不管的部分）：
1. 从 candidates 取前 3-4 个（套餐优先呈现），可按对话上下文微调
2. 用 `emotion_palette.palette_copy` 写推荐理由；套餐理由须点出省额（如「套餐价直接省一半」）
3. 渲染推荐卡片（格式与文案规则见 `references/interaction_flows.md`）
4. `swap_options` 非空 → 卡片附「换子项后 XXkcal」提示；用户确认换子项时，下单阶段按 `references/order_playbook.md` 的 roundList 传参
5. `over_budget: true` 的组合：话术「体验不打折，帮你换成小份组合」+ downsize_options

**退出码 2**（无候选）：放宽处理——改用 `--goal 无` 重跑，并告知用户「今天先不管营养带，看看情绪色搭配」。

## 下单铁律（三步强制确认）

**确认①选定**：用户明确选定某个组合（"就要 A"/"B 和 C 换一下也行"→视为修改需求回到推荐）。

**确认②复述+指令**：获取门店与取餐方式后，`calculate-price` 算价（返回单位为分，展示时 ÷100 为元；同时记下 `takeWayList` 中的 takeWayCode），然后输出订单复述表格：

| 餐品 | 数量 | 门店 | 取餐方式 | 总价 | 预计热量 |
|---|---|---|---|---|---|

并明确说：**「回复『确认下单』我才会提交订单」**。用户未明确包含「确认」字样 → 绝不调用 create-order，回到澄清。

**确认③支付闭环**：`create-order` 成功后展示支付引导（扫码或麦当劳 APP 支付），告知用户「支付完成后请回复'支付完成'或'支付失败'」，随后用 `query-order` 查状态反馈。

**五场景参数速查**（完整规则见 `references/order_playbook.md`）：

| 场景 | beType | orderType | beCode | addressId | 其他 |
|---|---|---|---|---|---|
| 到店自取 | 1 | 1 | 不传 | 不传 | takeWayCode 必传 |
| 得来速 | 5 | 1 | 必传（query-nearby-stores） | 不传 | takeWayCode 必传 |
| 麦乐送 | 2 | 2 | 必传（delivery-query-stores） | 必传 | 备注最多50字 |
| 预约 | 同所属场景 | 同左 | 同左 | 同左 | reservationDate 必传（yyyy-MM-dd HH:mm） |
| 企业团餐 | 6 | 2 | 必传（delivery-query-stores） | 必传 | gmServiceCode 必传（query-meal-assistance） |

菜单与商品 code：`query-meals`（按场景传 storeCode/orderType/beType）取真实菜单；餐品名与营养库的对应关系用 `python scripts/bridge_name.py match --menu-item <名称>` 查询；特调规则（selectedKey/unselectedKey）见 order_playbook。

## 团餐模式

触发词：团队/聚餐/团建/组里/多人点麦当劳。采集 2 题：聚餐目的（项目庆功/部门团建/朋友聚会/家庭聚餐）+ 人数与人均预算（默认 30 元）。运行：
```
python scripts/recommend_engine.py --mode party --purpose <目的> --headcount <人数> --budget-per-head <人均>
```
输出按官方 5 档预算规则组合，**预算参与选品**：候选含 `price_est_per_head`（参考价估算的人均合计）与 `budget_status`——引擎自动在本档位换低价组合或降档使人均落在预算 15% 容差内；`budget_status` 为「超出预算…」时须与用户确认调预算或降档后才继续。之后走 `query-meal-assistance`（取 gmServiceCode）→ query-meals → calculate-price → 同样三步确认 → `create-order`（beType=6, gmServiceCode）。团餐模式**不套用个人档案过滤**，需口头确认团队忌口。

## 错误处理

- **401**：提示「mcd-mcp 连接器 Token 可能无效或过期，请在 WorkBuddy 连接器设置中检查后重试」
- **429**：等待 60 秒重试 1 次；再失败告知用户稍后再试，绝不连环重试
- **508（太火爆了）**：瞬时繁忙，等待 60 秒重试 1 次
- **600039（今日下单数已经上限）**：账号级每日下单数上限（实测当日累计创建第 5 笔后触发）。告知用户「今天的下单次数已达平台上限，建议明天再试或直接在麦当劳 APP 下单」，停止重试
- **60002（未查询到门店信息）**：storeCode 与 beCode 不配对或门店不存在——用 query-nearby-stores / delivery-query-stores 重新取门店，勿猜测门店码
- **600050（收藏餐厅列表为空）**：该场景无收藏门店（如得来速）——改用城市+关键词搜索，或建议换到店自取
- **60006（商品已售罄或不可售）**：该商品当前门店不可单点（部分 code 仅套餐内可选）——从推荐中移除该品并换下一候选
- **60001（未匹配到商品）**：早餐等时段商品在非供时段查详情不可见——带 reservationDate 重试或等时段
- **参数契约（实测）**：`calculate-price` 不接受 addressId（外送算价只用 beCode）；addressId 仅在 `create-order`（外送/团餐）使用
- **未支付订单 15 分钟时效**：创建订单后提醒用户支付窗口为 15 分钟，超时未支付将自动取消
- **限流纪律**：同一次会话中已成功返回的工具不重复调用；菜单按选定门店拉取一次即缓存复用
- 引擎/脚本异常：把 stderr 中的 JSON error 读出来，用自然语言告知用户，不暴露堆栈

## 合规红线

对外话术统一「体验匹配框架」：情绪是体验偏好信号，不是进食动机。总话术「今天的心情，值得一份什么样的麦麦」。
最高频禁用词（完整表见 `references/compliance_wording.md`）：安慰、解压、治愈、情绪化进食、放纵、罪恶感。
所有营养与过敏原输出必须带脚注「仅供参考，不构成营养或医疗建议」。不与其他品牌对比；不暗示官方背书。

## 资源索引

| 文件 | 用途 |
|---|---|
| `scripts/palette_common.py` | 共享常量（情绪/目标枚举、禁用词表、JSON 原子读写、品类兜底推断） |
| `scripts/parse_nutrition.py` | 营养接口返回 → 结构化缓存（data/nutrition_cache.json，24h） |
| `scripts/bridge_name.py` | 营养名 ↔ 门店菜单名匹配（match 单查 / report 批量匹配率） |
| `scripts/recommend_engine.py` | 推荐引擎：硬过滤+组合生成+打分（personal/party 双模式） |
| `scripts/profile_manager.py` | 用户档案 get/init/update（data/profile.json，不入库） |
| `scripts/self_check.py` | 一键质量自查（文件/Token/禁用词/JSON/语法/引擎冒烟） |
| `references/emotion_palette.json` | 6 情绪→体验标签→色彩→话术映射（芯片文案源） |
| `references/set_meals.json` | 套餐参考库（构成/营养/参考价/可换子项/体验标签/口碑）——套餐优先主推的数据底座 |
| `references/price_ref.json` | 单品参考价表（单品组合估价 + 套餐单点合计对比；下单前以门店实时价为准） |
| `references/nutrition_bands.json` | 健身目标×训练日→热量/蛋白区间（含时段缩放） |
| `references/allergen_map.json` | 过敏原→餐品关键词黑名单（含免责声明） |
| `references/name_alias.json` | 名称匹配兜底别名表（联调期扩充） |
| `references/food_experience_map.json` | 156 餐品品类+体验标签预标注（情绪打分底座） |
| `references/interaction_flows.md` | 完整对话流程、芯片文案库、推荐卡片模板（采集与渲染前加载） |
| `references/order_playbook.md` | 下单操作手册：五场景参数、特调 key 规则、团餐/预约细节（进入下单阶段加载） |
| `references/compliance_wording.md` | 禁用词全表+替代表达+健康声明模板（生成对外文案前加载） |
