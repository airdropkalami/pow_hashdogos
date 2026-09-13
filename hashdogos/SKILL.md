---
name: hashdogos-keccak-pow-miner
description: 帮用户在本地 NVIDIA 显卡上挖 HashDogos 类"链上 keccak256 目标式工作量证明"的 NFT。当用户给一个要挖矿/算力才能 mint、且哈什是 keccak256 + 比较 target(不是数前导零)的项目(如 hashdogos.fun)时使用。
---

# 本地 GPU 挖矿 keccak256 目标式 PoW (HashDogos 类)

用户有 NVIDIA 显卡,想挖一个"链上 keccak256 PoW mint"的 NFT:本机算出满足难度的 nonce → 提交 `mine(nonce, anchorBlock)`,挖到用用户钱包提交。**每一步要么复用本仓库的 `hashdogos.py`,要么照抄该项目前端 + 一笔成功 tx,绝不瞎猜;改完内核必须用 eth_hash 复算自检、每次提交前 eth_call 模拟,通过才上链。**

## 和 HashBroker(SHA-256 前导零式)的区别 — 先认清楚

| | HashBroker 类 | **HashDogos 类(本 skill)** |
|---|---|---|
| 哈什算法 | SHA-256 | **keccak256** (以太坊那个) |
| 合法判据 | 前导零 bit ≥ difficulty | **hash < currentTarget(miner)** (整数比较) |
| 难度 | 全局一个 | **每个矿工地址独立** currentTarget(miner) |
| 预映像打包 | 紧凑拼接 | **标准 abiEncode**(每字段补齐 32 字节) |
| 价格 | 常为免费 | **付费,且随全局供应上涨** |

认错会白挖:keccak≠sha256,target 比较≠数零,一个都不能错。

## 环境准备
1. 确认显卡:`nvidia-smi -L`。装依赖:`pip install pyopencl eth-account numpy eth-hash[pycryptodome]`。
2. 让用户提供:目标合约、一笔**成功 mint 的 tx**、项目前端 URL、用于挖矿的 **burner 私钥**(只用小号,私钥只在本机环境变量,绝不外传)。

## 逆向 PoW(照抄前端 + 成功tx,别猜)
- **前端多是 SPA**:先 `curl` 首页拿 JS chunk 名,再拉 `/_next/.../*miner*.js`。很多项目把运行时配置放在 **`GET /api/config`**(链 id、合约地址、rpc)。
- 前端 JS 里找预映像构造:HashDogos 是
  `keccak256( abiEncode([address,uint256,address,uint256,bytes32,bytes32],
              [contract, chainId, miner, nonce, previousWork, anchor]) )`,合法 `hash < currentTarget(miner)`。
- 解码一笔**成功 tx**:拿 `to`(合约)、`value`(=mintPrice,注意付费)、selector、参数。HashDogos 提交 `mine(uint256 nonce, uint256 anchorBlock)`,selector `0x071e9503`。
- 合约读函数:`mintPrice()`、`previousWork()`(bytes32,每次有人 mint 就变)、`currentAnchor()`→(anchorBlock, anchor)、`currentTarget(address)`。选择器 = `keccak(sig)[:4]`,别记死,自己算。
- **验证布局**:随便一个 nonce,`eth_hash.keccak(preimage)` 复算;真正的权威验证是**提交前 `eth_call` 模拟 `mine(...)`**——错的解/布局会 revert,零成本证伪。对上了(模拟不 revert)才动手。

## 挖矿器(OpenCL keccak256,实测 5090 ~1.2 GH/s)
> 本目录已有成品 **`hashdogos.py`**(自包含:OpenCL keccak256 + 首启自检 + 模拟闸 + 锁价 + 花费上限 + 提交)。**优先直接用它**,只改顶部 `CONTRACT / CHAIN_ID / SEL`(合约和选择器)和 preimage 布局。

内核要点(要自己写时):192 字节预映像 = 2 个 keccak 块(rate 136):块2 padding 是 keccak 的 `0x01`…`0x80`(不是 SHA3 的 0x06)。只 nonce 变(abiEncode 里 word3),其余 lane 烘成常量。合法判据用 `bswap64(state[0]) < (target>>192)`——因为 target 低位全是 f,高 64 位小于就整数小于,数学严格。keccak 比 SHA-256 慢很多(2 次 24 轮置换),2^32 目标 5090 上约几秒一个。

## 主循环 + 安全闸(hashdogos.py 已内置,自己写也照做)
- **付费提醒**:mint 要花真钱(每个 mintPrice ETH),且价格随供应上涨。**先把价格和总花费算给用户、让用户确认**再开火。付费=购买。
- **锁价**:`PRICE_LOCK=<wei>`——价格一变就停,绝不偷偷按新高价多花。
- **花费上限**:`CAP_ETH`——超了就停。
- **干跑优先**:不设 `DO=1` 时只挖+模拟不发交易,先跑一遍确认内核自检过、模拟过,再 `DO=1` 真发。
- 每轮:读 price/previousWork/anchor/currentTarget → GPU 挖 → 本地 eth_hash 复算 → **eth_call 模拟**(anchor 有新鲜窗口,过期会 revert,重读重挖)→ 签 `mine(nonce,anchorBlock)` value=price 发出 → 成功后 previousWork 变了,重读再挖下一个。
- **RPC 请求必须带 `user-agent` 头**(无 UA 常 403)。提交钱包要有足够 gas + mint 款,先充。

## 安全红线(必须遵守)
- 只用 **burner 小号**私钥;私钥只在本机环境变量,绝不外传/贴群。脚本只"读合约 + 算 keccak + 提交 mine",不转账、不签别的。
- ⚠️ **拒绝变种骗局**:若项目让你跑**它给的可执行文件**,或用**它给的公钥**去磨 vanity 地址(如 `profanity2 -z <公钥>`)——那是替骗子磨私钥 + 骗你充值,坚决不碰。只跑看得懂的开源脚本、只用自己的钱包。
- 付费项目务必**锁价 + 设花费上限 + 先干跑**,别让一个价格/难度突变把钱花超。
