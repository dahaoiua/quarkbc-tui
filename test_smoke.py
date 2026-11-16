# -*- coding: utf-8 -*-
"""
无头冒烟测试：不联网、不开真终端，用 textual 的 run_test 驱动完整交互链路
  启动 → 勾选文件 → 生成 BC 链接 → 结果屏内容可回解 → 唤起比特彗星收到 bc:// 链接
  → 手机下载中继起服务并用真 HTTP 请求验字节
网络层不在本测试覆盖范围内（selftest 只测编码，真联网行为要实机跑）。
"""
import asyncio
import base64
import sys
import tempfile
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import quarkbc
from quarkbc import QuarkClient, build_app


class FakeUpstream:
    """假夸克 CDN：中继替手机补头之后打的就是它。吐固定字节，好逐字节核对。"""

    def __init__(self):
        self.blob = bytes(range(256)) * 2048      # 512 KiB
        blob = self.blob

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(blob)))
                self.end_headers()
                self.wfile.write(blob)

            def log_message(self, *a):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def port(self) -> int:
        return self.httpd.server_address[1]

    def url(self, name: str = "movie.mkv") -> str:
        return f"http://127.0.0.1:{self.port}/{name}"

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class FakeClient:
    """替身：只实现 TUI 用到的方法"""
    cookie = "fake_cookie=1"

    def __init__(self):
        self.transferred = []   # 转存调用记录：(pwd_id, passcode, to_pdir_fid, batch)

    def list_dir(self, fid="0"):
        return [
            {"fid": "dir1", "file_name": "文件夹A", "file": False,
             "obj_category": "folder", "size": 0},
            {"fid": "f1", "file_name": "movie.mkv", "file": True,
             "obj_category": "", "size": 1234},
            {"fid": "f2", "file_name": "song.mp3", "file": True,
             "obj_category": "", "size": 456},
        ]

    def get_download_links(self, fids, on_progress=None):
        urls = {"f1": "https://dl.quark.cn/a?x=1", "f2": "https://dl.quark.cn/b?y=2"}
        names = {"f1": "movie.mkv", "f2": "song.mp3"}
        return [{"fid": f, "name": names[f], "size": 1, "url": urls[f]} for f in fids]

    def ensure_dir(self, name, pdir_fid="0"):
        self.transferred.append(("dir", name))
        return "dirfid"

    def transfer_share(self, pwd_id, passcode, to_pdir_fid="0", batch_size=100, on_log=None):
        self.transferred.append(("share", pwd_id, passcode, to_pdir_fid, batch_size))
        if on_log:
            on_log(f"分享《{pwd_id}》共 2 个顶层条目")
        return {"title": pwd_id, "total": 2, "ok": 2, "failed": []}


async def wait_for(pilot, cond, timeout_s=5.0):
    waited = 0.0
    while waited < timeout_s:
        await pilot.pause(0.05)
        waited += 0.05
        if cond():
            return True
    return False


async def main() -> int:
    failures = []

    def check(name, cond, detail=""):
        print(("PASS " if cond else "FAIL ") + name + (f"  {detail}" if detail and not cond else ""))
        if not cond:
            failures.append(name)

    config = {"cookie": "fake_cookie=1"}
    # 真实配置必须一个字节都不动：上一版测试传 config_path=None，扫码链路落盘时
    # 直接写进了用户的 quarkbc.json，把真 Cookie 覆盖成假值（线上事故）
    real_cfg = quarkbc.CONFIG_PATH
    real_before = real_cfg.read_bytes() if real_cfg.exists() else None

    tmp_cfg = Path(tempfile.mkdtemp(prefix="quarkbc_test_")) / "quarkbc.json"
    app = build_app()(client=FakeClient(), config=config, config_path=tmp_cfg)

    async with app.run_test(size=(110, 40)) as pilot:
        # 1. 目录加载完成（假客户端给了 3 行）
        check("boot.list_loaded", await wait_for(pilot, lambda: len(app.order) == 3))

        dt = app.query_one("#files")
        # 2. 第一行是目录，勾选应被拒绝
        await pilot.press("space")
        check("toggle.folder_refused", not app.selected and "dir1" not in app.selected)

        # 3. 下移到文件行，空格勾选
        await pilot.press("down")
        await pilot.press("space")
        check("toggle.file_selected", "f1" in app.selected)

        # 4. b 生成 BC 链接 → 结果屏出现
        await pilot.press("b")
        ok = await wait_for(pilot, lambda: _screen_of(app, "ResultScreen") is not None)
        check("bc.result_screen_open", ok)
        screen = _screen_of(app, "ResultScreen")
        if screen:
            text = str(screen.query_one("#r-links").text)
            check("bc.prefix", text.startswith("bc://http/"), text[:60])
            payload = base64.b64decode(text.removeprefix("bc://http/") + "=" * (-len(text) % 4)).decode()
            check("bc.payload_frame", payload.startswith("AA/") and payload.endswith("ZZ"))
            check("bc.payload_url", "url=https%3A%2F%2Fdl.quark.cn%2Fa" in payload)
            check("bc.payload_ua", "QuarkPC%2F6.9.7.761" in payload)
            check("bc.payload_cookie", "fake_cookie%3D1" in payload)
            # 5. esc 关闭结果屏
            await pilot.press("escape")
            await pilot.pause(0.1)
            check("bc.result_closed", _screen_of(app, "ResultScreen") is None)
        else:
            check("bc.prefix", False, "结果屏未出现，后续校验跳过")

        # 6. enter 进入目录 + u 返回上级（enter 走 DataTable 行选中消息）
        await pilot.press("up")  # 回到第一行（目录）
        await pilot.press("enter")
        ok = await wait_for(pilot, lambda: app.stack[-1][0] == "dir1")
        check("nav.enter_dir", ok)
        await pilot.press("u")
        ok = await wait_for(pilot, lambda: app.stack[-1][0] == "0")
        check("nav.up", ok)

        # 7b. 鼠标操作：单击文件行勾选/取消、点工具栏按钮生成
        await pilot.click("#files", offset=(30, 2))  # 第2个数据行 = movie.mkv
        ok = await wait_for(pilot, lambda: "f1" in app.selected)
        check("mouse.click_toggle", ok)
        await pilot.click("#files", offset=(30, 2))
        ok = await wait_for(pilot, lambda: "f1" not in app.selected)
        check("mouse.click_untoggle", ok)
        await pilot.press("space")  # 光标已被点击带到第2行，直接空格重新勾选
        check("mouse.select_for_toolbar", "f1" in app.selected)
        await pilot.click("#tb-gen")
        ok = await wait_for(pilot, lambda: _screen_of(app, "ResultScreen") is not None)
        check("mouse.toolbar_gen", ok)

        # 7c. 结果屏「唤起比特彗星」：os.startfile 收到 bc:// 链接
        # 7d. 主屏一键唤起：勾选 → w/tb-wake 自动生成链接并直接唤起（不进结果页）
        wake_log = []
        _orig_startfile = getattr(quarkbc.os, "startfile", None)

        def fake_startfile(path):
            wake_log.append(path)

        quarkbc.os.startfile = fake_startfile
        try:
            await pilot.click("#r-wake")
            await pilot.pause(0.6)
            check("wake.startfile_called",
                  len(wake_log) >= 1 and str(wake_log[0]).startswith("bc://"),
                  f"wake_log={wake_log[:1]}")
            ok = await wait_for(pilot, lambda: _screen_of(app, "ResultScreen") is None)
            check("wake.screen_closed", ok)

            # 主屏一键唤起（f1 仍处于勾选状态，无需再进结果页）
            wake_log.clear()
            await pilot.click("#tb-wake")
            ok = await wait_for(pilot, lambda: len(wake_log) >= 1)
            check("wake.main_toolbar", ok and str(wake_log[0]).startswith("bc://"),
                  f"wake_log={wake_log[:1]}")
            check("wake.no_result_screen", _screen_of(app, "ResultScreen") is None)
        finally:
            if _orig_startfile is not None:
                quarkbc.os.startfile = _orig_startfile

        # 7e. 批量转存：多行链接 → 解析出 pwd_id/提取码 → 先解析目标目录再逐条转存
        await pilot.press("t")
        ok = await wait_for(pilot, lambda: _screen_of(app, "TransferScreen") is not None)
        check("transfer.screen_open", ok)
        tscreen = _screen_of(app, "TransferScreen")
        if tscreen:
            tscreen.query_one("#t-links").text = (
                "https://pan.quark.cn/s/aaa111?pwd=k9x2\n"
                "https://pan.quark.cn/s/bbb222 提取码：z3z3\n"
                "这一行不是链接\n")
            tscreen.query_one("#t-dir").value = "转存/测试"
            tscreen.action_start()
            ok = await wait_for(pilot, lambda: len(app.client.transferred) >= 3)
            check("transfer.calls", ok, f"transferred={app.client.transferred}")
            recs = app.client.transferred
            check("transfer.dir_first", recs and recs[0] == ("dir", "转存/测试"))
            shares = [r for r in recs if r[0] == "share"]
            check("transfer.link_count", len(shares) == 2, f"shares={shares}")  # 第 3 行应被跳过
            check("transfer.parse_pwd_query", shares and shares[0][1:] == ("aaa111", "k9x2", "dirfid", 100))
            check("transfer.parse_pwd_word", len(shares) > 1 and shares[1][1:] == ("bbb222", "z3z3", "dirfid", 100))
            ok = await wait_for(pilot, lambda: not tscreen._busy)
            check("transfer.finished", ok)
            await pilot.press("escape")
            await wait_for(pilot, lambda: _screen_of(app, "TransferScreen") is None)
            check("transfer.screen_closed", _screen_of(app, "TransferScreen") is None)
        else:
            check("transfer.calls", False, "转存屏未出现")

        # 7f. 手机下载：m → 起局域网中继 → 用真 HTTP 请求当作「手机」验字节，
        #     再关掉弹窗确认服务真的停了（端口不释放会占住下次启动）
        up = FakeUpstream()
        app.client.get_download_links = lambda fids, on_progress=None: [
            {"fid": f, "name": "movie.mkv", "size": len(up.blob), "url": up.url()}
            for f in fids]
        quarkbc.RELAY_BIND = "127.0.0.1"      # 测试只走环回，别触发系统防火墙
        await pilot.press("m")
        ok = await wait_for(pilot, lambda: _screen_of(app, "MobileScreen") is not None)
        check("mobile.screen_open", ok)
        relay = app._relay
        check("mobile.relay_running", relay is not None)
        if relay is not None:
            base = f"http://127.0.0.1:{relay.port}{relay.prefix}/"
            with urllib.request.urlopen(base, timeout=15) as r:
                page = r.read().decode()
            check("mobile.index_page", "movie.mkv" in page and relay.prefix + "/d/" in page)
            tok = relay.registry.ordered()[0][0]
            with urllib.request.urlopen(base + "d/" + tok, timeout=15) as r:
                body = r.read()
            check("mobile.phone_can_download", body == up.blob,
                  f"{len(body)} vs {len(up.blob)}")
            await pilot.press("escape")
            ok = await wait_for(pilot, lambda: _screen_of(app, "MobileScreen") is None)
            check("mobile.screen_closed", ok)
            try:
                urllib.request.urlopen(base + "d/" + tok, timeout=3).read()
                check("mobile.relay_stopped", False, "关闭弹窗后端口仍在服务")
            except Exception:
                check("mobile.relay_stopped", True)
        up.stop()

        # 8. 扫码登录链路（网络层已替换为假实现，验证 CAS 会话 Cookie 贯通）
        poll_state = {"n": 0}

        def fake_qr_get_token(collector, request_id=None):
            collector["CAS_SESSION"] = "abc"   # 模拟 uop 在取 token 时下发的会话 Cookie
            return "TOK123"

        def fake_qr_poll(token, collector, request_id=None):
            poll_state["n"] += 1
            if poll_state["n"] >= 2:
                return {"st": "ST_TICKET"}
            return {"waiting": True, "message": ""}

        def fake_qr_exchange(st, collector):
            check("qr.exchange_got_ticket", st == "ST_TICKET")
            # 会话 Cookie 必须从取 token 一路带到换 ticket（上一版漏带导致 401）
            check("qr.exchange_carries_session", collector.get("CAS_SESSION") == "abc")
            collector.update({"__pus": "a", "__puus": "b", "__kps": "c"})
            return collector, {"data": {"nickname": "测试号"}}, False

        quarkbc.qr_get_token = fake_qr_get_token
        quarkbc.qr_poll = fake_qr_poll
        quarkbc.qr_exchange = fake_qr_exchange

        old_cookie = app.client.cookie
        await pilot.press("l")
        ok = await wait_for(pilot, lambda: _screen_of(app, "QrLoginScreen") is not None)
        check("qr.screen_open", ok)
        ok = await wait_for(pilot, lambda: "__pus=a" in app.config["cookie"])
        check("qr.cookie_applied", ok, f"cookie={app.config['cookie']!r}")
        check("qr.client_rebuilt", app.client is not None and app.client.cookie != old_cookie)
        # 最终 Cookie = CAS 会话键 + bootstrap 得到的鉴权键全量合并（LitePan 同款）
        check("qr.saved_payload",
              all(k in app.config["cookie"] for k in ("CAS_SESSION", "__pus", "__puus", "__kps")))
        ok = await wait_for(pilot, lambda: _screen_of(app, "QrLoginScreen") is None)
        check("qr.screen_closed", ok)

        # 9. 回归守卫：测试全程不许改写真实配置文件
        real_after = real_cfg.read_bytes() if real_cfg.exists() else None
        check("config.real_untouched", real_after == real_before,
              f"{real_cfg} 被测试改写了")
        check("config.tmp_written", tmp_cfg.exists())

    print(f"\n{len(failures)} failed" if failures else "\nall passed")
    return 1 if failures else 0


def _screen_of(app, type_name):
    for s in app.screen_stack:
        if type(s).__name__ == type_name:
            return s
    return None


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
