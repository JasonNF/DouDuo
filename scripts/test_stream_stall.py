#!/usr/bin/env python3
"""边下边播断流看门狗（DYChunkStream）：真起一个本地 HTTP 服务，
发完响应头和一点数据就挂住——有看门狗的请求要在几秒内按 URLError.timedOut 失败（调用方据此换地址续传），
正常的请求、不设看门狗的请求照常收完；另外核对整条预下让路与续传的接线。"""
import http.server, pathlib, re, subprocess, tempfile, threading, time, unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
STREAM = (ROOT / "DouDuo" / "DYStreamCache.swift").read_text()
HOST = (ROOT / "DouDuo" / "DYRecommendFeedHost.swift").read_text()


def extract(src, start):
    i = src.index(start)
    depth, j, opened = 0, i, False
    while True:
        c = src[j]
        if c == "{": depth += 1; opened = True
        elif c == "}": depth -= 1
        j += 1
        if opened and depth == 0: return src[i:j]


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_GET(self):
        self.send_response(206)
        self.send_header("Content-Range", "bytes 0-99999/100000")
        self.send_header("Content-Length", "100000")
        self.end_headers()
        self.wfile.write(b"x" * 1000)
        self.wfile.flush()
        if self.path == "/stall":
            time.sleep(6)
            return
        if self.path == "/slow":
            time.sleep(1.5)
        self.wfile.write(b"x" * 99000)


MAIN = r'''
import Foundation
func check(_ c: Bool, _ m: String) { if !c { print("FAIL", m); exit(1) } }
let port = CommandLine.arguments[1]
let session = URLSession(configuration: .ephemeral)
func run(_ path: String, stall: TimeInterval?) async -> (bytes: Int, error: Error?, seconds: Double) {
    let t0 = Date()
    let s = DYChunkStream(session: session, request: URLRequest(url: URL(string: "http://127.0.0.1:\(port)\(path)")!),
                          priority: URLSessionTask.highPriority, stallAfter: stall, stallTimedFromStart: true)
    var n = 0
    do {
        _ = try await s.response()
        for try await d in s.data { n += d.count }
        s.cancel()
        return (n, nil, Date().timeIntervalSince(t0))
    } catch {
        s.cancel()
        return (n, error, Date().timeIntervalSince(t0))
    }
}
// 断流：1 秒没新数据 → timedOut（不是取消），且远早于服务器挂住的 6 秒
let a = await run("/stall", stall: 1)
check((a.error as? URLError)?.code == .timedOut, "stall → timedOut, got \(String(describing: a.error))")
check(a.seconds < 3, "stall detected quickly \(a.seconds)")
check(a.bytes == 1000, "bytes before stall kept \(a.bytes)")
// 正常：收完不误判
let b = await run("/ok", stall: 1)
check(b.error == nil && b.bytes == 100000, "ok stream \(b.bytes) \(String(describing: b.error))")
// 停顿短于阈值：不误判
let c = await run("/slow", stall: 3)
check(c.error == nil && c.bytes == 100000, "slow but alive \(c.bytes) \(String(describing: c.error))")
// 不设看门狗（最后一次尝试）：等着收完，跟以前一样
let d = await run("/slow", stall: nil)
check(d.error == nil && d.bytes == 100000, "no watchdog \(d.bytes)")
print("stream stall ok")
'''


class StreamStall(unittest.TestCase):
    def test_watchdog(self):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            chunk = extract(STREAM, "final class DYChunkStream")
            with tempfile.TemporaryDirectory() as d:
                p = pathlib.Path(d) / "main.swift"
                p.write_text(chunk + MAIN)
                r = subprocess.run(["swift", str(p), str(server.server_address[1])],
                                   capture_output=True, text=True, timeout=180)
        finally:
            server.shutdown()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("stream stall ok", r.stdout)

    def test_wiring(self):
        # 当前条与预加载分开连接池，当前条高优先级
        self.assertIn("currentSession", STREAM)
        self.assertIn("prefetchSession", STREAM)
        self.assertIn("URLSessionTask.highPriority", STREAM)
        # 换地址从断点续传，不从段首重拉（否则重复字节会被当成后续数据喂给播放器）
        self.assertIn('r.setValue("bytes=\\(resumeAt)-', STREAM)
        self.assertIn("if partial { resumeAt = offset }", STREAM)
        # 整条预下每块之前给正在卡的当前条让路
        whole = extract(STREAM, "    func fillWhole(")
        self.assertIn("DYVideoCache.isPlaybackWaiting", whole)
        # 正式版翻页不再在主线程把整个推荐列表编码落盘
        save = extract(HOST, "    func saveSession()")
        self.assertRegex(save, re.compile(r"#if DEBUG\s+DYFeedSession\(", re.S))


if __name__ == "__main__":
    unittest.main()
