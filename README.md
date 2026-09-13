# pow_hashdogos

本地 GPU 挖 **HashDogos**（[hashdogos.fun](https://hashdogos.fun)，Robinhood Chain）这类 **keccak256 目标式工作量证明**的 NFT。本机算出满足难度的 nonce → 提交 `mine(nonce, anchorBlock)`，**付费 mint**（每个 = 链上 `mintPrice`，价格随全局供应上涨）。

- `hashdogos/hashdogos.py` — 自包含挖矿器（OpenCL keccak256 + 首启自检 + 提交前 eth_call 模拟 + 锁价 + 花费上限 + 提交），单文件，改顶部配置即可换项目。
- `hashdogos/SKILL.md` — 一个 Claude Code skill，让你自己的 Claude 帮你从零建一个同类挖矿器。

## 原理

```
hash = keccak256( abiEncode(
         address contract, uint256 chainId, address miner,
         uint256 nonce, bytes32 previousWork, bytes32 anchor) )     # 6×32 = 192 字节
合法  <=>  hash < currentTarget(miner)        # 整数比较, 每个矿工独立难度
提交  mine(uint256 nonce, uint256 anchorBlock)  value = mintPrice
```

`previousWork` 每次有人 mint 就变（挖矿器每轮重读）；`anchor` 有新鲜窗口，过期提交会 revert。**这是抽奖，不是跑完所有**，运气好早出；想快只有加算力。

> 和 SHA-256 前导零式（如 HashBroker）不同：这里是 **keccak256** + **target 整数比较**（不是数前导零）+ **每矿工独立难度** + **标准 abiEncode**（补齐 32 字节）。别认错。

## 用法

```bash
pip install pyopencl eth-account numpy eth-hash[pycryptodome]

# 只用小号 burner 私钥!
export PK=0x你的burner私钥        # Windows PowerShell: $env:PK="0x..."

# 1) 先干跑(不发交易, 校验内核自检 + 模拟通过):
COUNT=1 python hashdogos/hashdogos.py

# 2) 真发(会花真钱, 每个约 mintPrice ETH):
COUNT=10 PRICE_LOCK=480000000000000 CAP_ETH=0.006 DO=1 python hashdogos/hashdogos.py
```

环境变量：
- `COUNT` — 挖几个
- `PRICE_LOCK` — 锁价（wei）。价格一变就停，绝不按新高价偷偷多花。`0`=不锁。
- `CAP_ETH` — 总花费上限，超了就停。
- `DO=1` — **只有设了才真发交易**；不设=只挖 + 模拟，不花一分钱。

实测 RTX 5090 ≈ 1.2 GH/s keccak256（比 SHA-256 慢，因每次 2 轮 keccak 置换）。

换别的同类项目：改 `hashdogos.py` 顶部 `CONTRACT / CHAIN_ID / SEL`（合约地址 + 各选择器，从项目前端 `/api/config` + 一笔成功 tx 逆向，别猜），并按前端核对 preimage 布局。

## ⚠️ 安全红线

- **付费项目**：mint 花真钱，价格随供应涨。务必 **锁价 + 设花费上限 + 先干跑**，别让价格/难度突变把钱花超。
- **只用 burner 小号私钥**，私钥只放本机环境变量，绝不硬编码/贴群/上传。脚本只做“读合约 + 算 keccak + 提交 mine”，不转账、不签别的。
- **拒绝变种骗局**：若某项目让你跑*它给的可执行文件*，或用*它给的公钥*去磨 vanity 地址（如 `profanity2 -z <公钥>`）——那是替骗子磨私钥 + 骗你充值，坚决不碰。只跑看得懂的开源脚本、只用自己的钱包。

## License

[MIT](LICENSE) — 按原样提供，作者不对任何损失负责，风险自负。
