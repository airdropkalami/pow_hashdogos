#!/usr/bin/env python3
# ============================================================================
#  HashDogos 本地 GPU 挖矿器 (单文件, NVIDIA GPU) — Robinhood Chain
#  链上 keccak256 目标式工作量证明 -> mint (付费, 每个 mintPrice ETH)。
#
#  依赖:  pip install pyopencl eth-account numpy eth-hash[pycryptodome]
#  用法:  设置环境变量 PK=你的钱包私钥(0x...), 然后:
#           # 先干跑一遍(不发交易, 校验内核+模拟通过):
#           PK=0x... COUNT=1 python hashdogos.py
#           # 真发(每个约 mintPrice ETH, 会花真钱):
#           PK=0x... COUNT=10 PRICE_LOCK=480000000000000 CAP_ETH=0.006 DO=1 python hashdogos.py
#         COUNT=挖几个;  PRICE_LOCK=wei(价格必须等于它, 一变就停, 0=不锁);
#         CAP_ETH=总花费上限(超了就停);  DO=1 才真发, 不设=只模拟不花钱。停: Ctrl+C。
#
#  原理: hash = keccak256( abiEncode(
#              address contract, uint256 chainId, address miner,
#              uint256 nonce, bytes32 previousWork, bytes32 anchor) )   (6×32=192字节)
#        合法 <=> hash < currentTarget(miner)  (每个矿工独立难度)。
#        提交 mine(uint256 nonce, uint256 anchorBlock)  value=mintPrice。
#        previousWork 每次有人 mint 就变 -> 本器每轮重读; anchor 有新鲜窗口, 过期会 revert。
#  ⚠️ 付费: mintPrice 随全局供应上涨。用 PRICE_LOCK 锁价, 涨了自动停, 不会偷偷多花。
# ============================================================================
import os, sys, time, json, urllib.request
import numpy as np
import pyopencl as cl
from eth_account import Account
from eth_hash.auto import keccak

# ---- 配置 (换项目改这里; 合约/selector 从项目前端 /api/config + 成功tx 逆向, 别猜) ----
RPC      = "https://rpc.mainnet.chain.robinhood.com"
CONTRACT = "0x9464A2e848BCE4F16bD8589c426894535c33629A"
CHAIN_ID = 4663
SEL = {  # 函数选择器 = keccak(sig)[:4]
    "price":    "0x6817c76c",  # mintPrice() -> uint256
    "prevWork": "0x8b73c652",  # previousWork() -> bytes32
    "anchor":   "0xcd809b11",  # currentAnchor() -> (uint256 anchorBlock, bytes32 anchor)
    "target":   "0x8b08fe33",  # currentTarget(address miner) -> uint256
    "mine":     "0x071e9503",  # mine(uint256 nonce, uint256 anchorBlock) payable
}

PK = os.environ.get("PK", "").strip()
if not PK: sys.exit("请设置环境变量 PK=你的钱包私钥 (0x...)  (只用小号 burner!)")
acct = Account.from_key(PK if PK.startswith("0x") else "0x" + PK); ADDR = acct.address
COUNT      = int(os.environ.get("COUNT", "1"))
CAP        = float(os.environ.get("CAP_ETH", "0.005"))
PRICE_LOCK = int(os.environ.get("PRICE_LOCK", "0"))     # wei; 非0则价格必须等于它
DO         = os.environ.get("DO", "0") == "1"           # 只有 DO=1 才真发交易

def rpc(m, p):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": m, "params": p}).encode()
    # 注意: 很多 RPC 不带 User-Agent 会 403
    req = urllib.request.Request(RPC, data=body, headers={"content-type": "application/json", "user-agent": "Mozilla/5.0"})
    j = json.loads(urllib.request.urlopen(req, timeout=25).read())
    if "error" in j: raise RuntimeError(j["error"])
    return j["result"]
def call(data, to=CONTRACT): return rpc("eth_call", [{"to": to, "data": data}, "latest"])
def read_state():
    price      = int(call(SEL["price"]), 16)
    prevWork   = call(SEL["prevWork"])                                    # bytes32
    an         = call(SEL["anchor"]); anchorBlock = int(an[2:66], 16); anchor = "0x" + an[66:130]
    tgt        = int(call(SEL["target"] + ADDR[2:].lower().rjust(64, "0")), 16)
    return price, prevWork, anchorBlock, anchor, tgt

# ---- 预映像 (标准 abiEncode, 6×32=192字节) ----
def preimage(prevWork, anchor, nonce):
    def w_addr(a): return bytes(12) + bytes.fromhex(a[2:])
    def w_uint(n): return int(n).to_bytes(32, "big")
    def w_b32(h):  return bytes.fromhex(h[2:].rjust(64, "0"))
    buf = w_addr(CONTRACT) + w_uint(CHAIN_ID) + w_addr(ADDR) + w_uint(nonce) + w_b32(prevWork) + w_b32(anchor)
    assert len(buf) == 192, len(buf)
    return buf
def lanes_for(prevWork, anchor):
    buf = preimage(prevWork, anchor, 0)
    return [int.from_bytes(buf[i:i + 8], "little") for i in range(0, 192, 8)]  # 24 个小端 lane
def keccak_hi64(prevWork, anchor, nonce):
    h = keccak(preimage(prevWork, anchor, nonce)); return h, int.from_bytes(h[:8], "big")

# ---- OpenCL keccak256 (只 nonce 变: word3=lane15=bswap64(nonce)) ----
dev = None
for p in cl.get_platforms():
    if "NVIDIA" in p.name: dev = p.get_devices()[0]; break
if dev is None: dev = cl.get_platforms()[0].get_devices()[0]
ctx = cl.Context([dev]); q = cl.CommandQueue(ctx)
print(f"GPU: {dev.name} {dev.max_compute_units}CU  wallet {ADDR}", flush=True)

KSRC = r"""
__constant ulong RC[24]={0x0000000000000001UL,0x0000000000008082UL,0x800000000000808aUL,0x8000000080008000UL,
0x000000000000808bUL,0x0000000080000001UL,0x8000000080008081UL,0x8000000000008009UL,0x000000000000008aUL,
0x0000000000000088UL,0x0000000080008009UL,0x000000008000000aUL,0x000000008000808bUL,0x800000000000008bUL,
0x8000000000008089UL,0x8000000000008003UL,0x8000000000008002UL,0x8000000000000080UL,0x000000000000800aUL,
0x800000008000000aUL,0x8000000080008081UL,0x8000000000008080UL,0x0000000080000001UL,0x8000000080008008UL};
__constant int ROTC[24]={1,3,6,10,15,21,28,36,45,55,2,14,27,41,56,8,25,43,62,18,39,61,20,44};
__constant int PILN[24]={10,7,11,17,18,3,5,16,8,21,24,4,15,23,19,13,12,2,20,14,22,9,6,1};
#define R64(x,y) rotate((ulong)(x),(ulong)(y))
inline ulong bswap64(ulong x){
  return ((x&0xffUL)<<56)|((x&0xff00UL)<<40)|((x&0xff0000UL)<<24)|((x&0xff000000UL)<<8)
       |((x>>8)&0xff000000UL)|((x>>24)&0xff0000UL)|((x>>40)&0xff00UL)|((x>>56)&0xffUL);
}
inline void keccakf(ulong* s){
  ulong bc[5],t;
  for(int r=0;r<24;r++){
    for(int i=0;i<5;i++) bc[i]=s[i]^s[i+5]^s[i+10]^s[i+15]^s[i+20];
    for(int i=0;i<5;i++){ t=bc[(i+4)%5]^R64(bc[(i+1)%5],1); for(int j=0;j<25;j+=5) s[j+i]^=t; }
    t=s[1];
    for(int i=0;i<24;i++){ int j=PILN[i]; bc[0]=s[j]; s[j]=R64(t,ROTC[i]); t=bc[0]; }
    for(int j=0;j<25;j+=5){ for(int i=0;i<5;i++) bc[i]=s[j+i]; for(int i=0;i<5;i++) s[j+i]^=(~bc[(i+1)%5])&bc[(i+2)%5]; }
    s[0]^=RC[r];
  }
}
// L0..L23 烘进源码; block1=L0..16(lane15=nonce), block2=L17..23 + keccak pad(0x01/0x80)。
__kernel void mine(ulong base,__global volatile int* found,__global ulong* out,__global ulong* dbg){
  ulong nonce = base + (ulong)get_global_id(0)*__ITERS__;
  ulong Lc[24]={ __LANES__ };
  for(uint it=0; it<__ITERS__; it++){
    if(*found) return;
    ulong s[25]; for(int i=0;i<25;i++) s[i]=0;
    for(int i=0;i<17;i++) s[i]^= (i==15)? bswap64(nonce) : Lc[i];
    keccakf(s);
    for(int i=0;i<7;i++) s[i]^=Lc[17+i];
    s[7]^=0x0000000000000001UL; s[16]^=0x8000000000000000UL;
    keccakf(s);
    ulong hi=bswap64(s[0]);
    if(get_global_id(0)==0 && it==0) dbg[0]=s[0];        // 自检: 首nonce的lane0
    if(hi < __THI__){ if(atomic_cmpxchg(found,0,1)==0){ out[0]=nonce; } return; }
    nonce++;
  }
}
"""
ITERS = 1024
def mine(prevWork, anchor, tgt, stale, max_s=180):
    lanes = lanes_for(prevWork, anchor); THI = tgt >> 192   # 合法 <=> hash高64位 < target高64位 (target低位全f, 严格成立)
    src = (KSRC.replace("__LANES__", ",".join(f"{l}UL" for l in lanes))
               .replace("__ITERS__", str(ITERS)).replace("__THI__", f"{THI}UL"))
    prg = cl.Program(ctx, src).build()
    mf = cl.mem_flags; found = np.zeros(1, np.int32); out = np.zeros(1, np.uint64); dbg = np.zeros(1, np.uint64)
    fg = cl.Buffer(ctx, mf.READ_WRITE | mf.COPY_HOST_PTR, hostbuf=found)
    og = cl.Buffer(ctx, mf.READ_WRITE | mf.COPY_HOST_PTR, hostbuf=out)
    dg = cl.Buffer(ctx, mf.READ_WRITE | mf.COPY_HOST_PTR, hostbuf=dbg)
    ker = cl.Kernel(prg, "mine")
    GLOBAL = 1 << 20; per = GLOBAL * ITERS
    base = int.from_bytes(os.urandom(6), "big"); t0 = time.time(); tot = 0; last = t0; checked = False
    while True:
        ker(q, (GLOBAL,), None, np.uint64(base), fg, og, dg); q.finish()
        cl.enqueue_copy(q, found, fg); cl.enqueue_copy(q, dbg, dg); q.finish()
        if not checked:  # 自检: GPU keccak 必须逐位等于 eth_hash, 否则退出
            ref = keccak_hi64(prevWork, anchor, base)[0]; ref_l0 = int.from_bytes(ref[:8], "little")
            if int(dbg[0]) != ref_l0: raise SystemExit(f"❌ keccak 内核自检失败! {int(dbg[0]):#018x} != {ref_l0:#018x}")
            print(f"  ✓ keccak 内核自检通过 (lane0={ref_l0:#018x})", flush=True); checked = True
        tot += per; base = (base + per) & ((1 << 64) - 1); now = time.time()
        if found[0]:
            cl.enqueue_copy(q, out, og); q.finish(); return int(out[0])
        if now - last >= 5:
            print(f"  {tot/(now-t0)/1e9:.2f} GH/s  搜 {tot/1e9:.1f}e9 (期望 {(2**256)/tgt/1e9:.1f}e9)", flush=True); last = now
        if stale(): print("  challenge 变了 → 重挖", flush=True); return None
        if now - t0 > max_s: print("  超时, 重读状态重挖", flush=True); return None

def encode_mine(nonce, anchorBlock):
    return SEL["mine"] + int(nonce).to_bytes(32, "big").hex() + int(anchorBlock).to_bytes(32, "big").hex()

spent = 0.0; got = 0
try:
    while got < COUNT:
        price, prevWork, anchorBlock, anchor, tgt = read_state()
        if PRICE_LOCK and price != PRICE_LOCK:
            print(f"⛔ mintPrice 变为 {price/1e18} ETH (≠锁定 {PRICE_LOCK/1e18}), 按要求停止。已 mint {got} 个, 花 {spent:.5f} ETH。"); break
        if got == 0:
            print(f"price {price/1e18} ETH · target {tgt:#x} · 期望 {(2**256)/tgt:.2e} 次 · anchorBlock {anchorBlock}", flush=True)
        gp = int(rpc("eth_gasPrice", []), 16); proj = spent + price/1e18 + 600000*gp/1e18
        if proj > CAP:
            print(f"⛔ 预计花费 {proj:.5f} > 上限 {CAP} ETH, 停止。已 mint {got} 个, 花 {spent:.5f} ETH。"); break
        print(f"[{time.strftime('%H:%M:%S')}] mint #{got+1}/{COUNT} · prevWork {prevWork[:14]} · anchor {anchor[:14]}", flush=True)
        pw0 = prevWork
        def stale():
            try: return call(SEL["prevWork"]).lower() != pw0.lower()
            except Exception: return False   # RPC 抖动不算过期, 继续挖
        nonce = mine(prevWork, anchor, tgt, stale)
        if nonce is None: continue
        h, hi = keccak_hi64(prevWork, anchor, nonce)
        if hi >= (tgt >> 192): print(f"  本地复算不达标, 重挖"); continue
        data = encode_mine(nonce, anchorBlock)
        try:  # 提交前 eth_call 模拟 (权威闸: 错解/anchor过期直接 revert, 零浪费)
            rpc("eth_call", [{"from": ADDR, "to": CONTRACT, "data": data, "value": hex(price)}, "latest"])
        except Exception as e:
            print(f"  模拟 revert(anchor过期/被抢?), 重读重挖: {str(e)[:80]}"); continue
        print(f"  ✓ 解出 nonce {nonce} · keccak {h.hex()[:18]} · 模拟通过", flush=True)
        if not DO:
            print(f"  [DRY] 未发送(设 DO=1 真发)。value {price/1e18} ETH"); got += 1; continue
        tx = {"nonce": int(rpc("eth_getTransactionCount", [ADDR, "pending"]), 16), "to": CONTRACT, "value": price,
              "gas": 600000, "maxFeePerGas": gp*2, "maxPriorityFeePerGas": gp, "data": data, "chainId": CHAIN_ID}
        signed = acct.sign_transaction(tx); txh = rpc("eth_sendRawTransaction", ["0x" + signed.raw_transaction.hex()])
        rc = None
        for _ in range(50):
            rc = rpc("eth_getTransactionReceipt", [txh])
            if rc: break
            time.sleep(0.3)
        if rc and rc.get("status") == "0x1":
            gas_cost = int(rc["gasUsed"], 16) * int(rc.get("effectiveGasPrice", hex(gp)), 16) / 1e18
            spent += price/1e18 + gas_cost; got += 1
            print(f"  ✓✓ mint 成功! tx {txh} · gas {int(rc['gasUsed'],16)} · 累计花 {spent:.5f} ETH")
        else:
            print(f"  ✗ mint revert, 重挖 · {txh}")
    print(f"\n=== 结束 · mint {got}/{COUNT} · 共花 {spent:.5f} ETH ===")
except KeyboardInterrupt:
    print(f"\n已停止 · mint {got} · 花 {spent:.5f} ETH")
