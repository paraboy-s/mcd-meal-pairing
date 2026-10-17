---
name: mcd-meal-pairing
description: 麦当劳配餐决策 Skill（McPair / 麦麦配）。当用户问「吃什么」「帮我配一份」「预算多少怎么点」「控卡怎么点」「忌口怎么选」「这券怎么用最划算」「我们几个人一起点」时使用。本 Skill 不靠模型猜测商品与价格：先调麦当劳 MCP 取门店真实在售菜单、营养表、券与点餐史，再由确定性求解器枚举组合、算到手价与热量，最后给出三档方案并解释取舍。适用于单人配餐、多人拼单、优惠券诊断。
---

# McPair · 麦麦配

**不推荐套餐，只做最优配对。** 你说一句「随便」，它算完上千种搭配，给你三档方案，并告诉你为什么不是另外那些。

## 铁律

1. **不猜价格，不猜热量。** 所有金额与营养数值只能来自 MCP 工具返回或 `scripts/pair.py` 的计算结果。接口没给的，就说「这个我查不到」，不要估。
2. **不虚构 SKU。** 只能推荐本次 `query-meals` 返回过的商品。名字必须逐字照抄，不能写成你以为的简称。
3. **不下单。** 本 Skill 只做决策。把商品清单和到手价交给用户，让用户自己在官方渠道确认下单。
4. **不隐瞒不确定性。** `dataStatus.issues` 里每一条都要在回复里说清楚（用了离线数据、券没取到、套餐构成是推断的），不能当作没看见。
5. **脚本失败就停下。** 如果 `pair.py` 返回 `ok: false`，把 `error` 原样告诉用户。**绝对不要因为脚本挂了就自己心算一个方案**——那正是本项目要消灭的行为。

---

## 执行流程

### 第 0 步：判断场景

| 用户说的话 | 模式 |
|---|---|
| 「我想吃」「帮我配一份」「今天中午」 | `solo` |
| 「我们」「同事」「一起点」「AA」「三个人」「带娃」 | `squad` |

只有一个人吃，就 `solo`。提到两个人以上共同吃一单，才切 `squad`。
**「我和朋友各点各的」仍是 solo，不要自作主张合并。**

### 第 1 步：取数

先 `now-time-info` 拿准当前时间（券的可用性完全依赖它），再并行取以下数据。

| 工具 | 参数 | 用途 | 缺失后果 |
|---|---|---|---|
| `query-meals` | `storeCode`、`orderType`、`beType` | 门店在售商品与真实价格 | 无法计算，必须拿到 |
| `list-nutrition-foods` | 无 | 营养表（热量/蛋白/脂肪/钠/钙） | 热量与控卡失效 |
| `query-my-coupons` | 无 | 用户持有的券 | 算不出券抵扣 |
| `order-list` | 无 | 历史订单 → 口味画像 | 画像为空，只能按中性口味配 |

门店未知时先 `query-nearby-stores`（`beType=1` 到店自取 / `5` 得来速；`searchType=1` 收藏门店，`2` 按城市+关键词搜索）。

- 到店自取：`orderType=1, beType=1`，不传 `beCode`
- 麦乐送：`orderType=2, beType=2`，必传 `beCode`
- 得来速：`orderType=1, beType=5`，必传 `beCode`

**`order-list` 拿不到不要报错**，画像为空是正常状态，等于「第一次来」。此时不要编造口味，直接按中性口味配。

`query-meals` 的返回是**三层结构**，注意 `meals` 是 map 不是数组：

```
data.categories[]        → 分类名（饮品 / 鸡肉汉堡 / 小食甜品 …）
data.categories[].meals[] → { code, tags }   ← tags 里有「套餐」「单品」
data.meals.{code}        → { name, currentPrice, originalPrice }  ← 价格是字符串
data.frequent            → 我的常点
```

**把 `query-meals` 的原始 `data` 整块塞进 payload 的 `menu` 字段即可**，解析交给脚本。不要自己拍平、不要自己改字段名、不要四舍五入价格——脚本已经处理了 map 结构、字符串价格、套餐标签和周边过滤。

### 另外两个接口的返回格式不一样，别搞混

| 工具 | 返回格式 | 怎么放进 payload |
|---|---|---|
| `query-meals` | JSON（`data.categories` + `data.meals`） | `"menu": <整个 data 对象>` |
| `list-nutrition-foods` | JSON 数组 | `"nutrition": <数组>` |
| `query-my-coupons` | **Markdown 文本**（给人读的，不是 JSON） | `"coupons": "<整段文本>"` |
| `order-list` | JSON（`data.list[]`，明细叫 `orderProductList`） | `"orders": <整个 data 对象>` |

`query-my-coupons` 返回长这样，**原样当字符串传**，脚本用正则解析：

```
## 11.9元麦辣鸡腿堡
- **优惠**: ¥11.9 (用券价格)
- **有效期**: 2026-10-19 10:30-2026-10-23 23:59 周一、二、三、四、五 10:30-23:59
- **标签**: 到店专用、外送专用
```

**不要自己从这段文本里抄价格**——抄错一位，整单就错了。交给脚本解析，解析不出来它会记进 `issues`。

### 第 2 步：组 payload

构造一个 JSON 对象。除 `menus`/`constraints` 外都可以省略。

```json
{
  "mode": "solo",
  "now": "2026-10-19 12:30",
  "channel": "到店",
  "constraints": {
    "budget": 40,
    "kcalCap": 600,
    "proteinMin": 0,
    "avoid": ["spicy", "beef"],
    "needDrink": false
  },
  "store": {"storeCode": "1950064", "storeName": "麦当劳北京和平街北口餐厅"},
  "menu": { "categories": [...], "meals": {...} },
  "nutrition": [ ... list-nutrition-foods 的返回 ... ],
  "coupons": "## 11.9元麦辣鸡腿堡\n- **优惠**: ¥11.9 (用券价格)\n- **有效期**: ...",
  "orders": { "list": [ ... order-list 的 data.list ... ] }
}
```

`squad` 模式把 `constraints` 换成 `budget` + `members`：

```json
{
  "mode": "squad",
  "budget": 100,
  "now": "2026-10-19 12:30",
  "members": [
    {"name": "小明", "kcalCap": 600, "avoid": ["spicy"], "budget": 34,
     "taste": "chicken", "spiceTolerance": 0.15},
    {"name": "小美", "taste": "beef", "budget": 40},
    {"name": "老王", "taste": "chicken", "spiceTolerance": 0.9, "budget": 40}
  ],
  "store": {...}, "menu": {...}, "nutrition": [...], "coupons": [...]
}
```

成员字段全部来自**用户亲口说的内容**，没说就留空：

| 用户说 | 字段 |
|---|---|
| 「我控制在 600 大卡」 | `kcalCap: 600` |
| 「我不吃辣」 | `avoid: ["spicy"]` |
| 「我最多花 34」 | `budget: 34` |
| 「我想吃牛肉的」 | `taste: "beef"` |
| 「我能吃辣」 | `spiceTolerance: 0.9` |

**没有提到的不要编。** 编了就等于替用户决定，而他并没有说过。

`avoid` 可选值：`beef` `chicken` `fish` `pork` `spicy` `fried` `dessert`。
用户说「不吃牛肉」写 `beef`，说「不吃辣」写 `spicy`——**不要说「不吃辣」就顺手把 `beef` 也加上**。

### 第 3 步：计算

```bash
cd <本 Skill 根目录>
echo '<payload JSON>' | python scripts/pair.py
```

只想验证安装是否成功、或没有 Token 时：

```bash
python scripts/pair.py --demo
```

脚本输出一个 JSON 对象，`ok` 为 `true` 才算成功。**所有数字以它为准。**
如果 `dataStatus.issues` 非空，说明部分数据缺失或为推断值，渲染时必须转述。

### 第 4 步：渲染

**按下面的结构输出，不要一次性倒出全部字段。**

#### solo 模板

```
从 {stats.enumerated} 种搭配里筛出 {stats.passed} 组可行。（可选，一句话）

{profile.labels 用「 / 」连接}（置信度 {profile.confidence}）
{profile.surprise —— 有就放在这里，这是最容易被记住的一句话}

**方案一 · {tiers[0].label}** ¥{tiers[0].netPrice}
  {items 名称，用「 + 」连接}
  {tiers[0].kcal} kcal · 蛋白 {tiers[0].protein} g
  {rationale 第一条，或券的信息}

**方案二 · {tiers[1].label}** …（同上）

**方案三 · {tiers[2].label}** …（同上）

{如果有 couponNotes，用两三行转述最要紧的那条}
```

三条硬要求：

- 每套方案必须写清 **几件商品 + 到手价 + 热量**
- 券必须写清 **能不能用、为什么**
- `tierSpread` 小于 5 时，把 `spreadNote` 说出来：「三档只差 ¥x，不是没算开，是约束把空间压窄了」

#### squad 模板

```
{party} 人拼单 · 预算池 ¥{budget}
最公平的一单 ¥{options[0].netPrice}

{购物车：名称 ×份数，用「、」连接}

  {member.name}  ¥{member.pay}  满意度 {member.score}（{member.kcal} kcal）
    {member.notes 逐条列出，没有就省略这一行}

最不满意的是 {min score 的成员}（{score}）——这是 max-min 解：它不追求总分最高，
只保证最不满意的人不被牺牲。
```

**拼单绝对不要说「已为你们优化到最优」。** 目标是 max-min，不是总和最优。正确说法是「最不满意的是小美（0.77），这套方案保证这个数被抬到最高」。

成员问「凭什么我少吃了」时，直接引用该成员的 `notes`，不要重新组织语言——那是引擎算出来的让步，不是话术。

### 第 5 步：接住追问

| 用户说 | 动作 |
|---|---|
| 「还有更便宜的吗」 | `budget` 乘以 0.8 重跑，或直接讲省钱档 |
| 「热量再低点」 | `kcalCap` 设为当前值的 0.85 重跑 |
| 「这个券怎么用」 | 只讲 `couponNotes`，说清生效日、时段、渠道 |
| 「就它了」 | 输出商品清单与到手价，提示到官方渠道下单 |
| 「为什么不是 X」 | 引用 `eliminated` 里 X 的 `reasons` |
| 「加钱能吃到什么」 | 引用 `counterfactuals` |

追问时**沿用上一轮的约束**，只改用户指定的那一项。

---

## 数据陷阱清单

这些都是真实踩过的，别重复踩：

| 现象 | 原因 | 怎么处理 |
|---|---|---|
| 推荐了帽子、徽章、毛绒玩具 | 菜单接口混入周边商品 | 脚本已过滤，若仍出现就报告 |
| 三件套热量只有 300 多 | 套餐名被模糊匹配到单品营养 | 已修复：套餐禁止借用单品热量，算不出就标 `missing` 并排除 |
| 薯条被当成饮料 | 分类名把薯条挂在「饮品」下 | 判定顺序已改为「标签 > 商品名 > 分类」 |
| 「脆汁鸡」被当成饮品 | 名字含「汁」 | 已修复 |
| 券永远匹配不上 | 券面写「麦辣鸡腿堡」，菜单叫「麦辣鸡腿汉堡」 | 有别名归一 + 内置对照表 |
| `meals` 遍历出的是编码不是商品 | 它是 map 不是数组 | 整块传进 payload，别自己遍历 |

---

## 边界

- **拼单 2–5 人**。6 人以上枚举空间过宽，建议拆单。
- **满意度是显式加权模型**（口味 45% / 热量贴合 30% / 配餐完整度 25%），不是学习出来的。**不要包装成「AI 懂你」**。
- 营养表覆盖不到的新品会被排除，不会用估算值顶上。`dataStatus.issues` 会说明。
- 套餐构成若由名称推断，热量是累加估算值，`nutrientSource` 会是 `composed`，需要说明。
- 不计算配送费与满减，外送只影响券的可用性判断。
- 券的时段限制以 `now` 为准。**默认用真实当前时间**，不要自己假设「现在是中午」。

## 输出格式约定

- 价格保留 1 位小数，前缀 `¥`
- 热量取整，单位 `kcal`；蛋白取整，单位 `g`
- 商品名逐字照抄接口返回，**包括括号和引号**（如 `"堡" 你欢乐套餐`、`【美汁源】"多汁柠柠"`）
- 不写「大约」「左右」——数值是算出来的，不是估的；如果不确定，就说不确定在哪
