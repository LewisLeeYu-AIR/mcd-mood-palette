# 端到端逻辑审查报告（E2E_LOGIC_AUDIT.md）

> 审查日期：2026-10-11 ｜ 方法：静态代码走读 + 数据交叉核对 + 引擎实跑复现（审计脚本 `.workbuddy/tmp/audit_logic.py`，可重跑复现）
> **修复状态（2026-10-11 16:00–17:00）：15 项全部修复并回归通过**——self_check 10/10 全绿（新增 no_dup_keys；冒烟 6→9 组）；修复后验证脚本 9/9 PASS（`.workbuddy/tmp/audit_verify.py`）。**残余二轮清理（16:45）**：price_ref 经 choice code 直算探价再回填 11 价（71→79）+ `blocked` 表把无 SKU/不可售品挡在池外 + 随心配 1+1 建模为下午茶套餐——下午茶前 3 套餐 0→1（首推随心配换笋卷 ¥13.9/278kcal）。剩余 ~75 缺价品为麦咖啡杯型矩阵变体（同系列仅杯型差），由 warnings 估价标注兜底；afternoon 套餐缺口已用随心配补齐。
> 结论速览：**3 项 P1 / 5 项 P2 / 7 项 P3**；下单主链路（算价→确认→创建→取消）逻辑健壮已实测，问题集中在**推荐层数据失真**与**文档-代码契约不一致**。

---

## 0. 检查范围

### 0.1 模块清单（全部走读）

| 层 | 模块 | 文件 |
|---|---|---|
| 采集 | 用户档案 CRUD | `scripts/profile_manager.py` |
| 数据管道 | 营养接口解析 | `scripts/parse_nutrition.py` → `data/nutrition_cache.json` |
| 推荐 | 引擎（过滤/候选/打分） | `scripts/recommend_engine.py`（personal + party 双模式） |
| 桥接 | 营养名↔菜单名匹配 | `scripts/bridge_name.py` + `references/name_alias.json` |
| 数据底座 | 6 个 references JSON | set_meals / price_ref / emotion_palette / nutrition_bands / allergen_map / food_experience_map |
| 交互 | 对话流与卡片渲染规则 | `references/interaction_flows.md` |
| 下单 | 五场景参数/特调/生命周期 | `references/order_playbook.md` + SKILL.md 下单铁律 |
| 门禁 | 发布自查 | `scripts/self_check.py` |

### 0.2 上下游依赖链（数据流向）

```
list-nutrition-foods → parse_nutrition → nutrition_cache(158品)
                                              ↓ enrich(叠加 food_experience_map 品类/标签)
profile.json(过敏/忌口/目标) ──→ recommend_engine ← emotion_palette + nutrition_bands
                                              ↑ set_meals(7套餐) + price_ref(56价)
                                              ↓
用户选定候选 ──→ query-meals(真实菜单) ──→ 套餐复核(价差≤15%) / bridge_name(单品映射)
                                              ↓
query-meal-detail(roundList/特调key) → calculate-price(分→元, takeWayCode) → 复述确认②
                                              ↓
create-order → [15min支付窗口] → query-order / cancel-order（闭环③）
```

## 1. 逻辑正确性判断依据与标准

1. **数据一致性**：同一数据点（价格/热量/蛋白/名称）在 references、引擎输出、卡片展示、下单参数四层间数值可追溯且一致；派生值（省钱率、营养单价、换子项差值）有明确计算口径。
2. **状态机完整性**：订单状态（未创建→待支付→已支付/已取消/已过期）每条迁移都有触发条件、处理动作与话术；无死状态、无不可达分支。
3. **异常路径闭合**：每个外部调用（MCP 16 工具、脚本退出码 0/1/2）的失败分支都有定义的处理动作，不允许静默吞错或无限重试。
4. **契约一致性**：文档（SKILL.md/playbook/interaction_flows）声明的行为与代码实际行为一致；声明给用户的安全语义（如过敏原"保守剔除"）必须真实成立。
5. **用户输入有效性**：采集的用户约束（预算/过敏原/忌口/目标）必须真实参与推荐决策，不得采集后被静默忽略。

## 2. 逐环节结论（数据流转 / 状态传递 / 异常处理）

| 环节 | 数据流转 | 状态传递 | 异常处理 | 结论 |
|---|---|---|---|---|
| 档案采集 | get/init/update 原子写 ✓ | exists:false→init ✓ | SystemExit 走 stderr，与 SKILL.md 契约一致 ✓ | **通过** |
| 营养管道 | 160→158 去重 ✓ fetched_at 24h ✓ | — | 脏行跳过不中断 ✓ | **通过**（名称含英文逗号会错位列，属边角） |
| 推荐引擎·过滤 | 过敏/忌口双层过滤 ✓ | — | 退出码 1/2 语义清晰 ✓ | 见 P1-3/P2-1 |
| 推荐引擎·套餐池 | 预筛/换子项/打分 ✓（D1/D2 已修复） | degrade_note 降级 ✓ | — | 见 P1-1/P2-3/P2-4 |
| 推荐引擎·组合池 | 骨架×槽位×去重 ✓ | over_budget+downsize ✓ | — | 见 P1-1/P2-2 |
| 推荐引擎·团餐 | 档位规则 ✓ | — | 品类池空 exit 2 ✓ | 见 P1-2 |
| 名称桥接 | 四层+别名兜底 ✓（D3/D4 已修复） | — | 无匹配返回 none ✓ | 见 P2-1/P3-7 |
| MCP 只读链路 | 菜单→code→算价(分→元) ✓ | takeWayCode 记取 ✓ | 60002/600050/508 话术齐 ✓ | **通过**（addressId 契约差异已固化入文档） |
| 写操作闭环 | 复述→确认→创建→核对→取消 ✓ | 待支付/已取消实测 ✓ | 600039 停止重试 ✓ | **通过**（实测 7 笔全取消零支付） |
| 支付状态机 | 支付引导→用户回报→query-order | **待支付→配餐中 未验证** | 支付失败话术 ✓ | 见 §4 未确认项 |
| 15min 过期 | expirePayTime ✓ | 超时自动取消实测 ✓ | 过期后"重新扫码"话术未区分（P3-3） | 小瑕 |
| 门禁自查 | 9 项检查 ✓ | — | — | 见 P3-6（冒烟覆盖） |

## 3. 问题清单（按严重程度）

### P1（核心输出失真或安全语义不成立，建议 M5 前修复）

**P1-1 price_ref 覆盖率仅 36%，性价比排序与卡片价格双重失真**
- 位置：`references/price_ref.json`（56 价）vs `data/nutrition_cache.json`（158 品）；消费方 `recommend_engine.py` `combo_price()` L242-250、两段式打分 L527-538
- 成因：价格表为门店单日快照，未覆盖全部可进组合池的单品；缺价品按 `category_default` 兜底估价（main=¥23/drink=¥10），与实价偏差可达 ±50%
- 实证：cozy/减脂/下午茶 实跑 top4 中 3/4 用默认估价（儿童鱼排堡按 ¥23 估、冰牛奶按 ¥10 估）；100 个可进池单品缺价
- 影响：① `value_fit`（权重 0.20）中营养单价归一化基于错误价格 → 相邻候选排序可被翻转；② 卡片展示价格失真，用户确认后算价跳变，损伤信任
- 修复建议：从 query-meals 菜单快照一次性回填可进池单品的 currentPrice（160 品全量更好）；`combo_price` 缺价时在 warnings 中显式标注"估价"；中期把 price_ref 改为"选店后用真实菜单刷新"而非静态快照

**P1-2 团餐预算不参与选品，候选不含任何价格字段**
- 位置：`recommend_engine.py` `gen_party()` L395-441；输出候选字段仅 kcal/protein/category（审计实跑证实无 price 字段）
- 成因：档位（PARTY_TIERS）按预算选**品类结构**，但选品排序只看情绪标签权重（PURPOSE_WEIGHTS），全程无价格校验
- 实证：人均 ¥25/8 人/部门团建 → 推「果然多肉+麦辣鸡翅-2块」；按引擎自身估价逻辑（果然多肉缺价→main 默认 ¥23 + 鸡翅 ¥14.5）人均 ≈ ¥37.5，超预算 50%
- 影响：用户显式输入的预算约束被静默忽略；候选无价格 → 复述前 LLM 无法预警；算价后才暴露超支，流程折返
- 修复建议：`gen_party` 内用 price_ref 对 per_head_items 合计并做预算校验（超 15% 时换下一候选/降档），候选输出带 `price_est_per_head`；缺价品跳过或标注

**P1-3 过敏原「宁漏勿错/保守剔除」声明与实现方向相反**
- 位置：`references/allergen_map.json` note 声明 vs `recommend_engine.py` `filter_allergen()` L104-112 实现
- 成因：声明称"无法判断是否含某过敏原的餐品将被保守剔除"；实现是**关键词命中才剔除**——无法判断的餐品全部保留。且关键词表本身有盲区：大豆组仅 ["豆浆","燕麦奶","蔬菜","雪菜"]，只命中饮料，而汉堡胚/酱料普遍含大豆蛋白（培根蔬萃双层牛堡、板烧鸡腿堡等全部漏网）；鸡蛋组 misses 蛋黄酱类
- 影响：声明了"保守剔除"的安全语义实际不成立；对大豆/鸡蛋过敏用户，含过敏原餐品会进入推荐（有顶部免责声明缓解，但契约矛盾本身是缺陷）
- 修复建议（二选一，使声明与实现一致）：A. 按声明实现—— allergen 声明后，未命中任何"确认不含"白名单的餐品保守剔除（需建白名单）；B. 改声明——明示"仅按名称关键词剔除已知含过敏原餐品，无法覆盖隐性过敏原（酱料/面包胚），过敏体质务必人工核实"，并把大豆组关键词扩充（+堡/面包/酱）。推荐 B（改动小、诚实）

### P2（特定场景输出失真/体验受损）

**P2-1 name_alias.json 重复键「麦辣鸡腿汉堡」，前值被 JSON 静默丢弃**
- 位置：`references/name_alias.json` L27 与 L29（同名键两次出现）
- 成因：JSON 标准允许重复键、解析器取最后值；前一行的别名列表 `["麦辣鸡腿堡", "麦辣鸡腿堡套餐"]` 被静默丢弃，仅存 `["麦辣鸡腿堡"]`
- 影响：菜单出现「麦辣鸡腿堡套餐」时别名兜底失效，落到子串/相似度层（可能误配）；数据完整性隐患——编辑者以为两条都在
- 修复：合并为一条 `"麦辣鸡腿汉堡": ["麦辣鸡腿堡", "麦辣鸡腿堡套餐"]`；self_check 增加"references JSON 重复键检测"（正则级即可，防复发）

**P2-2 build_downsize「大→中」通用替换生成不存在的 SKU**
- 位置：`recommend_engine.py` `build_downsize()` L386-387
- 成因：`startswith("大")` 的 side 一律替换首字为"中"——大薯条→中薯条 ✓，但大杯玉米杯→「中杯玉米杯」✗（营养库/菜单仅有大/小两档）
- 影响：降档建议推荐不可点商品，用户确认后桥接失败，需人工折返
- 修复：替换目标先在营养库+price_ref 中校验存在性，不存在则跳过该条或映射到「小」档；或改用显式映射表（复用 palette_common 中闲置的 DOWNSIZE_RULES，见 P3-4）

**P2-3 套餐换子项后 protein 未同步修正**
- 位置：`recommend_engine.py` `gen_set_meal_candidates()` L229：`"totals": {"kcal": eff_kcal, "protein_g": protein}`——kcal 用换后值，protein 用默认值
- 实证：中薯条(4g)→小杯玉米杯(2g)，双换后 totals.protein_g 仍为默认，偏差 -2g/单（卡片蛋白、prot_fit 蛋白达标度均用错值）
- 影响：蛋白达标判定（0.3 权重子项）虚高；减脂·力量日（蛋白门槛 35g）场景下边缘套餐可能被误判达标
- 修复：swappable 增加 `protein_delta`（数据侧 3 处改），或运行时从 nutrition_cache 差值计算

**P2-4 套餐池时段覆盖：breakfast/afternoon 结构性无套餐，且库未跟进新套餐**
- 位置：`references/set_meals.json`——7 套餐 periods 并集仅 {lunch, dinner}
- 成因：建库时只收录正餐套餐；10-11 实测菜单已出现早餐套餐（枫糖厚松饼三/四件套）与周末双人餐，库未更新
- 影响：「套餐优先」在早餐/下午茶两个时段名存实亡（degrade_note 有兜底话术，但用户拿不到真实存在的套餐优惠）；测试阶段 3 的"新增 4 套餐"证明静态库与每日菜单存在漂移
- 修复：补录早餐套餐（periods 含 breakfast）；把"选店后发现的缺失套餐回写 set_meals.json"写入运营手册（自助生长）

**P2-5 「换一批」用尽后重跑引擎输出完全相同（确定性死胡同）**
- 位置：`references/interaction_flows.md` §2.1「用完再重跑引擎」vs 引擎确定性（同参数同输出）
- 影响：用户连续换 3 批（9 候选）后再换一批 → 返回同样 9 个，话术承诺落空
- 修复：引擎加 `--exclude "名称1,名称2"` 参数（LLM 把已展示候选传入），或重跑时强制换 mood 相邻情绪/放宽 band；短期可在 interaction_flows 改话术引导用户改约束（换个心情/时段）而非"重跑"

### P3（文档漂移/死配置/边角，可攒批处理）

| # | 问题 | 位置 | 说明与修复 |
|---|---|---|---|
| P3-1 | MCP_INTEGRATION 工具表漏 `query-my-coupons` | MCP_INTEGRATION.md §2（16 个）vs order_playbook §8 引用 | 实际使用 17 个，补表 |
| P3-2 | `combo_hints.prefer / avoid / use_campaign_calendar` 未被任何代码消费 | emotion_palette.json vs 引擎（仅用 category_weights.main） | cozy 的 avoid:["大份"] 等形同虚设；要么实现（rank_pool 惩罚项）要么从数据中删除，避免"看起来生效" |
| P3-3 | 「支付失败→可重新扫码」话术未区分 15 分钟已过期 | interaction_flows §4 vs order_playbook §9.1 | 过期订单不能再支付；话术加分支：先 query-order，已取消则引导重新下单 |
| P3-4 | palette_common 的 `DOWNSIZE_RULES` / `ZERO_DRINK_HINT` / `clamp` 为死代码 | palette_common.py L38-42, L96-97 | 与 build_downsize 硬编码规则并存漂移；统一为一处（见 P2-2） |
| P3-5 | calculate-price 与 create-order 间价格漂移无处理约定 | order_playbook §8 | 极小概率但存在；约定：以 create-order 返回价复述差价并再次确认 |
| P3-6 | self_check 冒烟仅覆盖 personal×lunch | self_check.py run_smoke | 未覆盖 afternoon（历史缺陷高发区）、party 模式；各加 1 组 |
| P3-7 | downsize 建议文案「无糖可口可乐（约0kcal）」无法通过桥接 | recommend_engine.py L385 | 该字符串非营养库标准名，四层匹配+别名均不命中；改为输出标准名「无糖可口可乐中杯」+备注 0kcal |

## 4. 无法确认的环节（需补充信息/验证方式）

| # | 环节 | 缺口 | 所需验证 |
|---|---|---|---|
| 1 | 支付后状态流转 | 待支付→已支付→配餐中的状态值与时机 | 真实支付 1 笔（用户在场），query-order 轮询记录状态序列（已知唯一未验证链路） |
| 2 | 外送/团餐 create-order 写路径 | addressId/gmServiceCode 组合的真实下单 | 各下 1 笔并立即取消（额度允许时） |
| 3 | 401/429 实际返回结构 | 话术基于推测 | 文档审查已覆盖；如需实证需等 Token 自然过期（不主动破坏配置） |
| 4 | 团餐大数量上限 | headcount>5 时 items quantity 是否有平台 cap | 团餐链路实测时用 8 人单验证 |
| 5 | price_ref 整体偏差率 | 快照价与现价的漂移幅度未知 | 抽 20 品跑 calculate-price 比对（只读零成本），为 P1-1 修复提供基线 |
| 6 | 「中杯玉米杯」缺档是否跨门店一致 | 仅验证营养库缺 | 换 1 家门店 query-meals 抽查玉米杯 SKU 档位 |

## 5. 总体结论

- **下单主链路（确认②→算价→创建→取消）逻辑健壮**：三步确认、错误码处理、15 分钟窗口、幂等取消均有代码+文档+实测三重支撑，无需改动。
- **推荐层是当前质量洼地**：3 项 P1 全部指向"数据快照不完整 + 用户约束未闭环"——价格覆盖、团餐预算、过敏原语义。共同根因是 references 静态快照与真实菜单/价格的漂移没有被系统性校准。
- **修复优先级建议**：P1-3（改声明，10 分钟）→ P1-1（回填价格+标注，1 小时）→ P2-1/P2-2/P2-3（半小时内）→ P1-2（引擎改造，2 小时）→ P2-4/P2-5 → P3 批量。均不阻塞 M5 建仓，但建议报名 Issue 前完成 P1 三项（演示场景可能踩中）。
