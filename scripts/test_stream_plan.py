#!/usr/bin/env python3
"""边下边播的纯计算（DYStreamPlan）：区间合并、允许下载范围、下一段请求、MP4 盒子定位。
MP4 头用真机抓到的抖音样本结构（ftyp 28 + moov 706578，moov 在前）。"""
import pathlib, subprocess, tempfile, unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
PLAN = (ROOT / "DouDuo" / "DYStreamPlan.swift").read_text()
STREAM = (ROOT / "DouDuo" / "DYStreamCache.swift").read_text()
CACHE = (ROOT / "DouDuo" / "DouyinVideoCache.swift").read_text()
PLAYER = (ROOT / "DouDuo" / "ShortVideoPlayer.swift").read_text()
FEED = (ROOT / "DouDuo" / "DouyinFeedView.swift").read_text()

MAIN = r'''
import Foundation
func check(_ c: Bool, _ m: String) { if !c { print("FAIL", m); exit(1) } }
typealias P = DYStreamPlan

// 区间合并
var r: [Range<Int64>] = []
r = P.insert(0..<100, into: r)
r = P.insert(200..<300, into: r)
r = P.insert(100..<150, into: r)     // 贴边合并
check(r == [0..<150, 200..<300], "merge adjacent \(r)")
r = P.insert(140..<210, into: r)     // 跨两段
check(r == [0..<300], "bridge \(r)")
check(P.contiguous(from: 50, in: r) == 250, "contiguous")
check(P.contiguous(from: 300, in: r) == 0, "contiguous at end")
check(P.isComplete([0..<1000], total: 1000) && !P.isComplete([0..<999], total: 1000), "complete")

// 形状：10 MB / 100 秒，moov 到 700 KB
let shape = P.Shape(total: 10_000_000, mediaStart: 700_000, duration: 100)
check(abs(shape.bytesPerSecond - 93_000) < 1, "bps \(shape.bytesPerSecond)")
// 预热条（非当前）：Wi-Fi 6 秒 → 700k + 558k
let headW = P.horizon(shape: shape, playhead: nil, current: false, cellular: false)
// 6 秒 = 558KB < 画面数据下限 768KB → 取下限（长视频 moov 大，6 秒不够首帧）
check(headW == 700_000 + P.minimumMediaBytes, "head wifi \(headW)")
let bigShape = P.Shape(total: 100_000_000, mediaStart: 700_000, duration: 100)  // 1MB/s
check(P.horizon(shape: bigShape, playhead: nil, current: false, cellular: false) == 700_000 + 6 * 993_000, "head by time when larger")
let headC = P.horizon(shape: shape, playhead: nil, current: false, cellular: true)
check(P.horizon(shape: bigShape, playhead: nil, current: false, cellular: true) < P.horizon(shape: bigShape, playhead: nil, current: false, cellular: false), "cellular head smaller")
// 当前条：播放到 20 秒 → 20 + 30 = 50 秒
let cur = P.horizon(shape: shape, playhead: 20, current: true, cellular: false)
check(cur == 700_000 + 93_000 * 50, "current \(cur)")
// 最小头部（码率估得太小也至少给 384 KB 画面数据）
let tiny = P.Shape(total: 50_000_000, mediaStart: 1000, duration: 100_000)
check(P.horizon(shape: tiny, playhead: nil, current: false, cellular: true) == 1000 + max(P.minimumHeadBytes, P.minimumMediaBytes), "min head")
// 不超文件尾
check(P.horizon(shape: shape, playhead: 99, current: true, cellular: false) == shape.total, "clamp total")

// 下一段请求：跳过本地已有、停在下一个已缓存区间前、不超过允许范围、单次 ≤ chunk
let have: [Range<Int64>] = [0..<1_000_000, 1_500_000..<2_000_000]
check(P.nextFetch(need: 0, ranges: have, total: 10_000_000, allowed: 5_000_000) == 1_000_000..<1_500_000, "fill gap")
check(P.nextFetch(need: 1_600_000, ranges: have, total: 10_000_000, allowed: 5_000_000) == 2_000_000..<(2_000_000 + P.chunkBytes), "skip local then chunk")
check(P.nextFetch(need: 2_000_000, ranges: have, total: 10_000_000, allowed: 2_000_000) == nil, "beyond allowed → park")
// 攒批：允许范围只多出一点点（播放推进 0.25 秒）→ 不发小请求，等攒够 minFetchBytes
check(P.nextFetch(need: 2_000_000, ranges: have, total: 10_000_000, allowed: 2_008_000) == nil, "tiny allowance waits")
check(P.nextFetch(need: 2_000_000, ranges: have, total: 10_000_000, allowed: 2_000_000 + P.minFetchBytes) == 2_000_000..<(2_000_000 + P.minFetchBytes), "batch once enough")
// 文件尾 / 碰到已缓存区间：小也照发（否则最后一点永远下不完）
check(P.nextFetch(need: 9_990_000, ranges: [], total: 10_000_000, allowed: 10_000_000) == 9_990_000..<10_000_000, "tail ok")
check(P.nextFetch(need: 1_400_000, ranges: have, total: 10_000_000, allowed: 5_000_000) == 1_400_000..<1_500_000, "gap before cached ok")
check(P.nextFetch(need: 0, ranges: [], total: nil, allowed: 0) == 0..<P.probeBytes, "probe when total unknown")
// 跳读窗口：拖到 80 秒 → 允许 8 秒
let w = P.jumpWindow(from: 8_000_000, shape: shape)
check(w.lowerBound == 8_000_000 && w.upperBound - w.lowerBound == max(93_000 * 8, 1024 * 1024), "jump window \(w)")
check(P.allowedEnd(for: 8_100_000, horizon: 2_000_000, windows: [w]) == w.upperBound, "window extends allowed")
check(P.allowedEnd(for: 3_000_000, horizon: 2_000_000, windows: [w]) == 2_000_000, "outside window")

// MP4：ftyp(28) + moov(706578) + mdat —— moov 在前，只要开头 16 字节就能算出画面起点
func be32(_ v: UInt32) -> [UInt8] { [UInt8(v >> 24 & 0xff), UInt8(v >> 16 & 0xff), UInt8(v >> 8 & 0xff), UInt8(v & 0xff)] }
var file = [UInt8](repeating: 0, count: 64)
file.replaceSubrange(0..<8, with: be32(28) + Array("ftyp".utf8))
file.replaceSubrange(28..<36, with: be32(706_578) + Array("moov".utf8))
let read: (Int64, Int) -> Data? = { o, n in o + 8 <= 64 ? Data(file[Int(o)..<min(64, Int(o) + n)]) : nil }
check(P.mediaStart(total: 3_000_000, read: read) == 28 + 706_578, "moov-first media start")
// mdat 在前（moov 在尾）
var file2 = [UInt8](repeating: 0, count: 64)
file2.replaceSubrange(0..<8, with: be32(24) + Array("ftyp".utf8))
file2.replaceSubrange(24..<32, with: be32(2_000_000) + Array("mdat".utf8))
let read2: (Int64, Int) -> Data? = { o, n in o + 8 <= 64 ? Data(file2[Int(o)..<min(64, Int(o) + n)]) : nil }
check(P.mediaStart(total: 3_000_000, read: read2) == 32, "mdat-first media start")
// 数据不够：返回 nil（等下次）
check(P.mediaStart(total: 3_000_000, read: { _, _ in nil }) == nil, "unknown yet")
print("stream plan ok")
'''


class StreamPlan(unittest.TestCase):
    def test_plan(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "main.swift"
            p.write_text(PLAN + MAIN)
            r = subprocess.run(["swift", str(p)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("stream plan ok", r.stdout)

    def test_wiring(self):
        # 播放器只认一个地址：ddstream:// 由 resourceLoader 供数据，不再有本地→在线切换
        self.assertIn('static let scheme = "ddstream"', STREAM)
        self.assertIn("resourceLoader.setDelegate(DYStreamLoader.shared", STREAM)
        self.assertIn("DYStream.asset(id: id, remote: url)", PLAYER)
        self.assertIn("DYStream.asset(for: a, remote: url)", FEED)
        # 播放位置驱动下载上限
        self.assertIn("DYStreamStore.shared.setPlayhead(id, t.seconds)", PLAYER)
        # 划到就算当前条（不等起播），否则长视频互相等永远起不来
        self.assertIn("DYStreamStore.shared.activate(id", PLAYER)
        # 用流量时不再整条预下载
        self.assertIn("if DYStream.enabled, DYNetPath.shared.cellular || DYNetPolicy.constrained, whole == 0 {", CACHE)
        # 看完拼齐 → 收进正式缓存
        self.assertIn("func adoptStreamed(", CACHE)
        # 不同清晰度不混字节
        self.assertIn("knownTotal != total", STREAM)
        # NSLock 不可重入：持有 e.lock 期间不能再调会拿同一把锁的方法（2026-11-20 真机卡死就是这个）
        import re
        held = False
        for n, line in enumerate(STREAM.splitlines(), 1):
            if "e.lock.lock()" in line and "e.lock.unlock()" not in line:
                held = True
            if held and re.search(r"\.horizon\(e\)|isStarving\(|finishIfComplete\(|logLeave\(", line):
                self.fail(f"line {n}: 持锁时调用会再拿锁的方法: {line.strip()}")
            if "e.lock.unlock()" in line:
                held = False

    def test_playing_cache_survives(self):
        """11-26 真机：播放中卡住几秒→黑屏→又播。正在播的这条的缓存被「已看过」淘汰删掉、ddstream 条目被 trim 收掉后，
        播放器缓冲播完拿不到数据，看门狗拆了重建再从网络拉"""
        evict = CACHE[CACHE.index("private func evict()"):CACHE.index("func purgeWatched()")]
        self.assertIn("focusedItems.map(\\.id)", evict)
        self.assertIn("DYStreamStore.shared.currentVideoID", evict)
        self.assertIn("Self.evictions(", evict)
        # 把正式的淘汰函数编出来真跑：真机场景——正在播的这条最早加入、刚被标已看，收进第 6 条缓存时不能删它
        a = CACHE.index("    nonisolated static func evictions(")
        b = CACHE.index("\n    }\n", a) + len("\n    }\n")
        harness = "import Foundation\nenum C {\n" + CACHE[a:b] + "}\n" + r'''
func check(_ c: Bool, _ m: String) { if !c { print("FAIL", m); exit(1) } }
let list: [(id: String, watched: Bool, bytes: Int)] = [("playing", true, 50), ("b", false, 10), ("c", false, 10), ("d", false, 10), ("e", false, 10), ("new", false, 10)]
check(C.evictions(list, protected: [], limit: 5, watchedKeep: 3, maxBytes: 1000) == ["playing"], "旧行为：不保护就删正在播的")
check(C.evictions(list, protected: ["playing"], limit: 5, watchedKeep: 3, maxBytes: 1000) == ["b"], "保护正在播的，改删最早未保护的")
// 已看过超 3 条：最早的已看先删，跳过受保护的
let w: [(id: String, watched: Bool, bytes: Int)] = [("p", true, 1), ("w1", true, 1), ("w2", true, 1), ("w3", true, 1), ("w4", true, 1)]
check(C.evictions(w, protected: ["p"], limit: 10, watchedKeep: 3, maxBytes: 1000) == ["w1", "w2"], "已看超额跳过受保护")
// 全都受保护：宁可暂时超上限，不死循环
check(C.evictions(list, protected: Set(list.map(\.id)), limit: 2, watchedKeep: 0, maxBytes: 1).isEmpty, "全保护不删")
// 体积超上限也按同样顺序删
check(C.evictions([("p", true, 900), ("x", false, 200)], protected: ["p"], limit: 10, watchedKeep: 3, maxBytes: 1000) == ["x"], "超体积跳过受保护")
print("PASS evictions protect playing")
'''
        with tempfile.TemporaryDirectory() as d:
            f = pathlib.Path(d) / "EV.swift"; f.write_text(harness)
            r = subprocess.run(["swift", str(f)], text=True, capture_output=True, timeout=180)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        print(r.stdout.strip())
        # 条目被收掉也能接着给数据：按原视频重建，正式缓存在就记下整条大小直接读本地
        self.assertIn("DYStreamStore.shared.entry(streamKey) ?? DYStreamStore.shared.revive(streamKey)", STREAM)
        revive = STREAM[STREAM.index("func revive("):STREAM.index("var currentVideoID")]
        self.assertIn("DYVideoCache.localURL(id)", revive)
        self.assertIn("e.total = Int64(whole)", revive)
        # 真实体积超上限不整条预下
        self.assertIn("if Double(size) > DYVideoCache.sizeCap", STREAM)
        # 重建优先本地整条；缓冲还够时就地续播不重建
        rebuild = PLAYER[PLAYER.index("private func rebuild()"):PLAYER.index("private func kickIfOverCautious")]
        self.assertIn("localWhole ?? altURL ?? url", rebuild)
        self.assertIn("DYPlaybackTelemetry.bufferedAhead(p) >= 2", PLAYER[PLAYER.index("private func watchdog("):PLAYER.index("private func retryPlayback()")])


if __name__ == "__main__":
    unittest.main()
