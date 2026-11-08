# -*- coding: utf-8 -*-
"""
QuarkBC TUI —— 夸克网盘 → 比特彗星 bc:// 链接生成器

先说清楚「转换」的本质：bc://http/<base64> 是比特彗星自有的「命名链接」协议，
内容是一个 HTTP 下载任务描述，跟 BitTorrent 协议没有任何关系。本工具做三件事：

  1) 用 Cookie 向夸克换会话令牌（acquire_dl_token），批量取 download_url
  2) 把「直链 + UA + Cookie」拼进 AA/…ZZ 帧，Base64 后输出 bc:// 链接
  3) 唤起比特彗星：os.startfile(bc://…) 走协议处理器拉起 BC，无需提前开着

编码格式逐字节对齐油小猴 LinkSwift 改版脚本（E:\\AI\\chat\\QuarkBC 下的 user.js），
对齐与否用 `python quarkbc.py --selftest` 里的 golden 用例保证——那是从原脚本
跑出来的真实输出，改坏编码会立刻挂。

接口来源：
  - acquire_dl_token / file/download：直接抄自上述脚本（已在页面环境验证）
  - file/sort（列目录）：脚本里是读 DOM，这里改走社区通用端点（greasyfork
    503595、quark-save 同款参数）。此端点未在原脚本中出现，如夸克改版需自查。
"""
from __future__ import annotations

import base64
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

# ---------------------------------------------------------------- 常量

# 桌面客户端 UA。夸克的下载接口按 UA 发直链，浏览器 UA 会拿到受限链接
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 "
      "QuarkPC/6.9.7.761 QuarkCloudDrivePC/6.9.7.761 quark-cloud-drive/2.5.40")

CONFIG_PATH = Path(__file__).with_name("quarkbc.json")

DEFAULT_CONFIG = {
    "cookie": "",
}

# ---------------------------------------------------------------- 纯逻辑（可单测）

def fix_filename(name: str) -> str:
    # 与原脚本一致：非法字符统一换下划线
    return re.sub(r'[!?&|`"\'*/:<>\\]', "_", name)


# encodeURIComponent 不编码 A-Z a-z 0-9 和 -_.!~*'()
_URI_SAFE = "-_.!~*'()"


def uri_component(s) -> str:
    return urllib.parse.quote(str(s), safe=_URI_SAFE)


def form_encode(params: dict) -> str:
    # 对齐 URLSearchParams：空格变 '+'，同名参数这里不会出现，不再处理
    return "&".join(
        f"{urllib.parse.quote_plus(k)}={urllib.parse.quote_plus(v)}"
        for k, v in params.items()
    )


def encode_base(s: str) -> str:
    # 原脚本的 encodeURIComponent→unescape→btoa 三段式，等价于 UTF-8 直接 b64
    return base64.b64encode(s.encode("utf-8")).decode("ascii")


def bc_link(filename: str, url: str, *, user_agent: str = "", cookie: str = "",
            referrer: str = "", extra: dict | None = None) -> str:
    """生成 bc://http/<base64> 链接，AA/…ZZ 帧格式见模块 docstring"""
    params: dict[str, str] = {}
    if referrer:
        params["referrer"] = referrer
    if user_agent:
        params["user_agent"] = user_agent
    if cookie:
        params["cookie"] = cookie
    if extra:
        params.update(extra)
    qs = form_encode(params)
    payload = (f"AA/{uri_component(fix_filename(filename))}/?url={uri_component(url)}"
               + (f"&{qs}" if qs else "") + "ZZ")
    return f"bc://http/{encode_base(payload)}"


def size_format(n) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "-"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024.0
    return "-"


# ---------------------------------------------------------------- 分享转存（纯逻辑）

# 夸克分享链接：https://pan.quark.cn/s/<pwd_id>，可能带 ?pwd=xxxx / 提取码：xxxx
_SHARE_ID_RE = re.compile(r"/s/([0-9A-Za-z_-]{4,})")
_PWD_QUERY_RE = re.compile(r"[?&#]pwd=([0-9A-Za-z]{1,10})")
_PWD_WORD_RE = re.compile(r"(?:提取码|密码|访问码)\s*[:：=]?\s*([0-9A-Za-z]{1,10})")
_PWD_TAIL_RE = re.compile(r"\s([0-9A-Za-z]{4})\s*$")


def parse_share_link(text: str) -> tuple[str, str]:
    """从一行分享文本里抽 (pwd_id, passcode)。

    支持的写法（都是夸克 APP「复制链接」实际给出的形态）：
        https://pan.quark.cn/s/abc123
        https://pan.quark.cn/s/abc123?pwd=k9x2
        https://pan.quark.cn/s/abc123 提取码：k9x2
        https://pan.quark.cn/s/abc123 k9x2
    解析不出来返回 ("", "")，由调用方报错，不做猜测。
    """
    s = (text or "").strip()
    m = _SHARE_ID_RE.search(s)
    if not m:
        return "", ""
    pwd_id = m.group(1)
    rest = s[m.end():]
    for rx in (_PWD_QUERY_RE, _PWD_WORD_RE):
        pm = rx.search(rest) or rx.search(s)
        if pm:
            return pwd_id, pm.group(1)
    pm = _PWD_TAIL_RE.search(s)
    return pwd_id, (pm.group(1) if pm else "")


def chunk_list(items: list, size: int) -> list[list]:
    """按 size 切片。转存接口单次可提交的条目数有上限，超了会整批失败。"""
    size = max(1, int(size or 1))
    return [items[i:i + size] for i in range(0, len(items), size)]


# ---------------------------------------------------------------- 配置

def load_config(path: Path = CONFIG_PATH) -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # 深拷贝默认值
    if path.exists():
        try:
            saved = json.loads(path.read_text("utf-8"))
            if isinstance(saved, dict):
                cfg.update({k: saved[k] for k in ("cookie",) if k in saved})
        except (json.JSONDecodeError, OSError):
            pass  # 配置损坏就用默认值，别挡住启动
    return cfg


def save_config(cfg: dict, path: Path = CONFIG_PATH) -> None:
    try:
        # 先留一份上一版：Cookie 一旦被坏数据覆盖（比如测试/异常流程）就没有退路，
        # 而 .bak 里至少还有上一次能用的值
        if path.exists():
            try:
                path.with_name(path.name + ".bak").write_bytes(path.read_bytes())
            except OSError:
                pass
        path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")
    except OSError as e:
        raise RuntimeError(f"配置写入失败：{e}")


# ---------------------------------------------------------------- 夸克 API

class QuarkError(Exception):
    pass


class QuarkClient:
    GET_LINK = ("https://drive-pc.quark.cn/1/clouddrive/file/download"
                "?pr=ucpro&fr=pc&sys=win32&ve=6.9.7.761")
    GET_TOKEN = ("https://drive-social-api.quark.cn/1/clouddrive/chat/conv/file/acquire_dl_token"
                 "?pr=ucpro&fr=pc&sys=win32&ve=6.9.7.761&fr=win&la=zh-CN&ch=pckk%40product_guanwan")
    LIST_DIR = "https://drive-pc.quark.cn/1/clouddrive/file/sort"
    PAGE_SIZE = 200
    BATCH = 15       # 与原脚本一致：每批 15 个 fid
    THROTTLE = 1.0   # 批间隔 1s，防 429

    def __init__(self, cookie: str, timeout: int = 30, on_cookie_update=None):
        self.cookie = (cookie or "").strip()
        self.timeout = timeout
        self._token: tuple[str, float] | None = None  # (token, 过期时间戳)
        # 夸克的 __puus 是轮换 Cookie：每个响应都会发新的，旧的宽限期过后作废。
        # 不吸收并持久化的话，存下的 Cookie 十几分钟就死（CatVodSpider 专门
        # 写了每次响应后更新 __puus 的逻辑，即为此故）
        self._on_cookie_update = on_cookie_update
        self._save_host: str | None = None   # 转存 save 能用的域名，首次成功后记住
        self._task_host: str | None = None   # 任务查询能用的域名
        self._mkdir_host: str | None = None  # 建目录能用的域名

    def _headers(self, json_ct: bool = False, web: bool = False) -> dict:
        h = {"User-Agent": UA, "Cookie": self.cookie}
        if json_ct:
            h["Content-Type"] = "application/json"
        if web:
            # 写接口（转存/任务/建目录）实测必须像浏览器一样带上 Origin+Referer，
            # 只给 UA/Cookie 会被网关判成匿名（code=31001 require login [guest]）
            h.update({
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Origin": "https://pan.quark.cn",
                "Referer": "https://pan.quark.cn/",
            })
        return h

    @staticmethod
    def _ac() -> str:
        """__dt/__t 是夸克的反爬时间戳参数，写接口缺了也容易被判匿名"""
        return f"&__dt={random.randint(200, 9999)}&__t={int(time.time() * 1000)}"

    def _req_json(self, url: str, *, data: bytes | None = None,
                  headers: dict | None = None) -> dict:
        req = urllib.request.Request(
            url, data=data, headers=headers or self._headers(),
            method="POST" if data is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                self._absorb_cookie_rotation(resp.headers)  # 先记账再取数据
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            # 夸克的业务错误常挂在非 2xx 状态码上（实测：无效分享 404/41006、
            # 参数错 400/14001、未登录 401/31001）。body 里的业务码比状态码有用得多，
            # 直接扔了用户只会看到「HTTP 404：请求被拒绝」，等于没说。
            raw = b""
            try:
                raw = e.read()
            except Exception:
                pass
            bcode = bmsg = None
            try:
                j = json.loads(raw.decode("utf-8"))
                bcode, bmsg = j.get("code"), j.get("message")
            except Exception:
                pass
            if bcode == 31001:
                raise QuarkError("Cookie 失效或未登录（31001），按 l 重新扫码") from e
            if bcode is not None:
                raise QuarkError(f"{bmsg or '请求失败'}（code={bcode}）") from e
            hint = "Cookie 失效或未登录" if e.code in (401, 403, 412) else "请求被拒绝"
            raise QuarkError(f"HTTP {e.code}：{hint}") from e
        except urllib.error.URLError as e:
            raise QuarkError(f"网络错误：{e.reason}") from e
        except json.JSONDecodeError as e:
            raise QuarkError(f"响应不是合法 JSON（可能被风控页替换）") from e

    def _absorb_cookie_rotation(self, resp_headers) -> None:
        """吸收响应轮换下来的 Cookie（__puus 等），变更时回调持久化。
        没有这一步，存下的 Cookie 十几分钟就因轮换作废。"""
        try:
            setc = resp_headers.get_all("Set-Cookie") or []
        except Exception:
            return
        new = merge_set_cookies(setc)
        if not new:
            return
        pairs: dict = {}
        for part in self.cookie.split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                if k.strip():
                    pairs[k.strip()] = v
        changed = any(pairs.get(k) != v for k, v in new.items())
        pairs.update(new)
        self.cookie = cookie_header(pairs)
        if changed and self._on_cookie_update:
            try:
                self._on_cookie_update(self.cookie)
            except Exception:
                pass  # 持久化失败不打断下载

    @staticmethod
    def _check(res, where: str):
        if not isinstance(res, dict) or res.get("code") != 0:
            code = res.get("code") if isinstance(res, dict) else "?"
            msg = res.get("message", "") if isinstance(res, dict) else str(res)[:200]
            if code == 31001:
                raise QuarkError(f"{where}：Cookie 无效或已过期（31001）")
            if code == 32003:
                raise QuarkError(f"{where}：网盘容量不足（32003）")
            raise QuarkError(f"{where}失败：code={code} {msg}")
        return res.get("data")

    def get_dl_token(self) -> str:
        now = time.time()
        if self._token and self._token[1] > now + 60:  # 预留 60s 缓冲，同原脚本
            return self._token[0]
        t = int(now)
        body = json.dumps({"conversation_id": f"300000{t}",
                           "conversation_type": 3, "msg_id": f"{t}000"}).encode()
        data = self._check(self._req_json(self.GET_TOKEN, data=body,
                                          headers=self._headers(True)), "获取下载令牌")
        token = (data or {}).get("token") or ""
        if not token:
            raise QuarkError("获取下载令牌失败：响应里没有 token")
        self._token = (token, float((data or {}).get("expired_timestamp") or now + 300))
        return token

    def list_dir(self, fid: str = "0") -> list:
        items: list = []
        page = 1
        while True:
            url = (f"{self.LIST_DIR}?pr=ucpro&fr=pc&uc_param_str=&pdir_fid={fid}"
                   f"&_page={page}&_size={self.PAGE_SIZE}&_fetch_total=1"
                   f"&_fetch_sub_dirs=0&_sort=file_type:asc,file_name:asc")
            data = self._check(self._req_json(url), "列目录") or {}
            batch = data.get("list") or []
            items.extend(batch)
            if len(batch) < self.PAGE_SIZE:
                break
            page += 1
            time.sleep(0.3)  # 翻页温和一点，降低风控概率
        return items

    def get_download_links(self, fids: list, on_progress=None) -> list:
        token = self.get_dl_token()
        out: list[dict] = []
        for i in range(0, len(fids), self.BATCH):
            batch = fids[i:i + self.BATCH]
            body = json.dumps({"fids": batch, "speedup_session": "", "token": token}).encode()
            data = self._check(self._req_json(self.GET_LINK, data=body,
                                              headers=self._headers(True)), "获取直链")
            for it in (data if isinstance(data, list) else []):
                out.append({
                    "fid": it.get("fid") or "",
                    "name": it.get("file_name") or "",
                    "size": int(it.get("size") or 0),
                    "url": it.get("download_url") or "",
                })
            if on_progress:
                on_progress(len(out), len(fids))
            if i + self.BATCH < len(fids):
                time.sleep(self.THROTTLE)
        return out

    # ---------- 分享转存
    # 端点来自 quark-auto-save（Cp0202）等社区实现的 sharepage 一族：
    #   token（换 stoken）→ detail（读分享目录）→ save（提交，返回 task_id）→ task 轮询
    # 注意：save 是异步任务，只看接口返回 0 不代表存好了，必须等 task.status==2。
    SHARE_TOKEN = "https://drive-pc.quark.cn/1/clouddrive/share/sharepage/token"
    SHARE_DETAIL = "https://drive-pc.quark.cn/1/clouddrive/share/sharepage/detail"
    # save 用 drive.quark.cn：两个独立项目（OpenMinis/quark-hub、ihmily/QuarkPanTool）
    # 都这么写，而 drive-pc 的 save 会被网关判匿名（31001 require login [guest]）。
    # 保留 drive-pc 作兜底，成功后记住能用的域名，避免每批都白试一次。
    SAVE_HOSTS = ("https://drive.quark.cn", "https://drive-pc.quark.cn")

    @classmethod
    def _host_order(cls, cached: str | None) -> list[str]:
        """把「记住能用的域名」排最前，其余按默认顺序补后面。
        只试缓存那一个是不够的：域名策略会变，缓存失效时仍然要能兜底。"""
        rest = [h for h in cls.SAVE_HOSTS if h != cached]
        return ([cached] + rest) if cached else rest

    def share_stoken(self, pwd_id: str, passcode: str = "") -> str:
        body = json.dumps({"pwd_id": pwd_id, "passcode": passcode or ""}).encode()
        url = f"{self.SHARE_TOKEN}?pr=ucpro&fr=pc&uc_param_str={self._ac()}"
        data = self._check(self._req_json(url, data=body,
                                          headers=self._headers(True, True)),
                           "获取分享令牌") or {}
        stoken = data.get("stoken") or ""
        if not stoken:
            raise QuarkError("获取分享令牌失败：链接可能已失效，或需要正确的提取码")
        return stoken

    def share_list(self, pwd_id: str, stoken: str,
                   pdir_fid: str = "0") -> tuple[list, str]:
        """返回 (条目列表, 分享标题)。只取顶层，顶层文件夹本身算一个条目。"""
        items: list = []
        title = ""
        page = 1
        while True:
            url = (f"{self.SHARE_DETAIL}?pr=ucpro&fr=pc&uc_param_str=&pwd_id={pwd_id}"
                   f"&stoken={urllib.parse.quote(stoken)}&pdir_fid={pdir_fid}&force=0"
                   f"&_page={page}&_size=100&_fetch_banner=0&_fetch_share=1"
                   f"&_fetch_total=1&_sort=file_type:asc,file_name:asc{self._ac()}")
            data = self._check(self._req_json(url, headers=self._headers(
                web=True)), "读取分享目录") or {}
            if not title:
                title = ((data.get("share") or {}).get("title") or "")
            batch = data.get("list") or []
            items.extend(batch)
            total = int((data.get("metadata") or {}).get("_total") or 0)
            if len(batch) < 100 or (total and len(items) >= total):
                break
            page += 1
            time.sleep(0.3)
        return items, title

    def share_save(self, pwd_id: str, stoken: str, fid_list: list,
                   fid_token_list: list, to_pdir_fid: str = "0",
                   passcode: str = "", on_log=None) -> str:
        # 夸克的转存接口对域名敏感：drive.quark.cn 认登录态，drive-pc 会回 guest。
        # 先在首选域名上试，被当匿名就换下一个；两个都不行才把错抛出去。
        payload = {
            "fid_list": fid_list,
            "fid_token_list": fid_token_list,
            "to_pdir_fid": to_pdir_fid,
            "pwd_id": pwd_id,
            "stoken": stoken,
            "pdir_fid": "0",
            "scene": "link",
        }
        if passcode:
            payload["pwd_id_extra"] = passcode   # 有提取码的分享必须带上
        body = json.dumps(payload).encode()
        hosts = self._host_order(self._save_host)
        last: Exception | None = None
        for host in hosts:
            url = (f"{host}/1/clouddrive/share/sharepage/save"
                   f"?pr=ucpro&fr=pc&uc_param_str=&app=clouddrive{self._ac()}")
            try:
                data = self._check(self._req_json(url, data=body,
                                                  headers=self._headers(True, True)),
                                   "提交转存") or {}
            except QuarkError as e:
                last = e
                if "31001" in str(e) or "未登录" in str(e):
                    if on_log:
                        on_log(f"[yellow]{host} 判为未登录，换域名重试…[/]")
                    continue      # 这个域名不认我们的登录态，换下一个
                raise             # 容量不足等业务错误换域名也没用，直接上抛
            self._save_host = host
            task_id = data.get("task_id") or ""
            if not task_id:
                raise QuarkError("提交转存失败：响应里没有 task_id")
            return task_id
        raise QuarkError(
            "转存被判定为未登录（31001）：两个域名都试过了。"
            "先按 l 重新扫码；仍然不行说明这份 Cookie 没有写权限"
            f"（原始错误：{last}）")

    def wait_task(self, task_id: str, timeout: float = 300.0, on_log=None) -> dict:
        """轮询异步任务。夸克 task.status：2=完成，3/4=失败，其余为进行中。

        域名同样兜底：这份 Cookie 在 drive-pc 的写接口上被当过 guest（31001），
        task 查询属于同一类写权限，所以按「记住能用的 → 另一个」顺序试。
        """
        deadline = time.time() + timeout
        retry = 0
        while time.time() < deadline:
            hosts = self._host_order(self._task_host)
            last: Exception | None = None
            data = None
            for host in hosts:
                url = (f"{host}/1/clouddrive/task?pr=ucpro&fr=pc&uc_param_str="
                       f"&task_id={task_id}&retry_index={retry}{self._ac()}")
                try:
                    data = self._check(self._req_json(url,
                                                      headers=self._headers(web=True)),
                                       "查询转存进度") or {}
                except QuarkError as e:
                    last = e
                    if "31001" in str(e) or "未登录" in str(e):
                        if on_log:
                            on_log(f"[yellow]{host} 判为未登录，换域名查询…[/]")
                        continue
                    raise
                self._task_host = host
                break
            if data is None:
                raise last or QuarkError("查询转存进度失败")
            status = data.get("status")
            if status == 2:
                return data
            if status in (3, 4):
                raise QuarkError(f"转存任务失败：{data.get('message') or f'status={status}'}")
            # 深递归时每个小批都要轮询，逐秒打日志会把界面刷爆，三秒一报就够
            if on_log and retry % 3 == 0:
                on_log(f"任务进行中… status={status}（已等 {retry}s）")
            retry += 1
            time.sleep(1.0)
        raise QuarkError(f"转存任务超时（已等 {int(timeout)}s，可能仍在后台执行）")

    def ensure_dir(self, name: str, pdir_fid: str = "0") -> str:
        """按名字找目录，不存在就建；支持 "a/b" 多级。返回最终目录 fid。"""
        cur = pdir_fid
        for part in [p for p in (name or "").replace("\\", "/").split("/") if p.strip()]:
            part = part.strip()
            found = ""
            for it in self.list_dir(cur):
                if is_folder(it) and it.get("file_name") == part:
                    found = it.get("fid") or ""
                    break
            if not found:
                body = json.dumps({"pdir_fid": cur, "file_name": part,
                                   "dir_path": "", "dir_path_file_name": ""}).encode()
                hosts = self._host_order(self._mkdir_host)
                last: Exception | None = None
                for host in hosts:
                    url = (f"{host}/1/clouddrive/file"
                           f"?pr=ucpro&fr=pc&uc_param_str=&app=clouddrive{self._ac()}")
                    try:
                        data = self._check(self._req_json(
                            url, data=body, headers=self._headers(True, True)),
                            f"创建目录 {part}") or {}
                    except QuarkError as e:
                        last = e
                        if "31001" in str(e) or "未登录" in str(e):
                            continue    # 建目录也属写权限，换域名再来
                        raise
                    found = data.get("fid") or ""
                    if not found:
                        raise QuarkError(f"创建目录失败：{part}")
                    self._mkdir_host = host
                    time.sleep(0.3)
                    break
                if not found:
                    raise last or QuarkError(f"创建目录失败：{part}")
            cur = found
        return cur

    def transfer_share(self, pwd_id: str, passcode: str, to_pdir_fid: str = "0",
                       batch_size: int = 100, on_log=None) -> dict:
        """把一个分享转存到 to_pdir_fid，遇到数量限制自动拆分。

        关键实测结论（2026-11-08，真机）：夸克的「单次转存文件个数超出用户等级限制」
        （code=41035）是在**任务执行阶段**报的，不是提交阶段——一个顶层文件夹只算 1 个
        条目，能提交成功，但里面的文件总数超限，任务才会失败。所以按顶层条目切批根本
        治不了这个限制，必须「能整包就整包，超限就拆」：
          整批提交 → 命中 41035 → 对半拆 → 拆到单个文件夹还超限 →
          在目标里建同名目录，下钻到它的子项继续递归（结构保住了）
        """
        log = on_log or (lambda m: None)
        stoken = self.share_stoken(pwd_id, passcode)
        entries, title = self.share_list(pwd_id, stoken)
        if not entries:
            raise QuarkError("分享目录是空的，或没有读取权限")
        log(f"分享《{title or pwd_id}》共 {len(entries)} 个顶层条目")
        self._tstats = {"ok": 0, "fail": 0}
        self._tfailed: list[tuple] = []
        self._save_entries(pwd_id, stoken, passcode, entries, to_pdir_fid,
                           max(1, int(batch_size or 1)), log, 0)
        return {"title": title, "total": self._tstats["ok"] + self._tstats["fail"],
                "ok": self._tstats["ok"], "failed": self._tfailed}

    def _save_entries(self, pwd_id: str, stoken: str, passcode: str, entries: list,
                      to_fid: str, batch_size: int, log, depth: int) -> None:
        """把一组条目存进 to_fid；41035（超量）时对半拆，单文件夹再下钻。"""
        if not entries:
            return
        if len(entries) > batch_size:
            for chunk in chunk_list(entries, batch_size):
                self._save_entries(pwd_id, stoken, passcode, chunk, to_fid,
                                   batch_size, log, depth)
                time.sleep(1.2)
            return
        names = "、".join(str(e.get("file_name") or "")[:24] for e in entries[:3])
        if len(entries) > 3:
            names += f" 等 {len(entries)} 项"
        fids = [e.get("fid") or "" for e in entries]
        tokens = [e.get("share_fid_token") or "" for e in entries]
        try:
            task_id = self.share_save(pwd_id, stoken, fids, tokens, to_fid,
                                      passcode=passcode, on_log=log)
            self.wait_task(task_id, on_log=log)
        except QuarkError as e:
            if "41035" not in str(e) and "超出用户等级限制" not in str(e):
                self._tstats["fail"] += len(entries)
                self._tfailed.append((names, len(entries), str(e)))
                log(f"[red]失败[/]：{names}　→　{e}")
                return
            if len(entries) > 1:
                mid = len(entries) // 2
                log(f"[yellow]这 {len(entries)} 项超出转存数量限制，对半拆开重试[/]")
                self._save_entries(pwd_id, stoken, passcode, entries[:mid], to_fid,
                                   batch_size, log, depth)
                time.sleep(1.0)
                self._save_entries(pwd_id, stoken, passcode, entries[mid:], to_fid,
                                   batch_size, log, depth)
                return
            only = entries[0]
            if not is_folder(only):
                self._tstats["fail"] += 1
                self._tfailed.append((names, 1, "单个文件也超出该账号的转存限制"))
                log(f"[red]失败[/]：{names}（单个文件超限）")
                return
            if depth >= 20:
                self._tstats["fail"] += 1
                self._tfailed.append((names, 1, "目录层级过深，已停止下钻"))
                log(f"[red]失败[/]：{names}（目录层级过深）")
                return
            # 单个文件夹也超限 → 在目标建同名目录，把它的子项存进去
            name = str(only.get("file_name") or "未命名")
            try:
                sub_fid = self.ensure_dir(name, to_fid)
                children, _ = self.share_list(pwd_id, stoken, pdir_fid=only.get("fid") or "")
            except QuarkError as e:
                self._tstats["fail"] += 1
                self._tfailed.append((names, 1, str(e)))
                log(f"[red]拆目录失败[/]：{name}　→　{e}")
                return
            log(f"[yellow]《{name}》整包超限，改为建同名目录后拆成 {len(children)} 项[/]")
            self._save_entries(pwd_id, stoken, passcode, children, sub_fid,
                               batch_size, log, depth + 1)
            return
        self._tstats["ok"] += len(entries)
        log(f"[green]完成[/]：{names}　（累计 {self._tstats['ok']} 项）")


# ---------------------------------------------------------------- 扫码登录
# 流程对齐 QuarkDownloaderPro（OP404OP）的 qrlogin 实现（2026-11 扒取）：
#   1) uop 换扫码 token → 2) su.quark.cn 二维码 → 3) 轮询拿 service_ticket
#   → 4) account/info?st= 换 Cookie → 5) 缺 __puus 时逐站补全。
# 这些端点原 userscript 里没有，未实机验证；跑不通优先怀疑 4/5 步的 Set-Cookie 行为。

class QrError(Exception):
    pass


UOP_QR_TOKEN_URL = "https://uop.quark.cn/cas/ajax/getTokenForQrcodeLogin"
UOP_QR_POLL_URL = "https://uop.quark.cn/cas/ajax/getServiceTicketByQrcodeToken"
# 二维码内容与 xiaoya-alist（大量用户验证过的实现）逐字符一致：
# token 原样嵌入不编码，uc_param_str/uc_biz_str 两个参数是实测能扫的关键
QR_URL_TEMPLATE = ("https://su.quark.cn/4_eMHBJ?token={token}&client_id=532&ssb=weblogin"
                   "&uc_param_str=&uc_biz_str=S%3Acustom%7COPT%3ASAREA%400%7COPT%3AIMMERSIVE"
                   "%401%7COPT%3ABACK_BTN_STYLE%400")
QR_POLL_INTERVAL = 2.0
QR_EXPIRE_SECONDS = 120
QR_PNG_PATH = Path(__file__).with_name("qrcode_login.png")

# 补 __puus 的兜底请求（带已有 Cookie 逐个打，直到拿到 __puus 为止）
PUUS_STEP_URLS = [
    "https://pan.quark.cn/list",
    ("https://drive-pc.quark.cn/1/clouddrive/file/sort?pr=ucpro&fr=pc&uc_param_str="
     "&pdir_fid=0&_page=1&_size=50&_fetch_total=1&_sort=file_type:asc,updated_at:desc"),
    ("https://drive.quark.cn/1/clouddrive/member?pr=ucpro&fr=pc&uc_param_str="
     "&fetch_subscribe=true"),
]


def qr_url(token: str) -> str:
    return QR_URL_TEMPLATE.format(token=urllib.parse.quote(token))


def parse_poll_response(res: dict) -> dict:
    """uop 轮询响应 → {'st': ticket} / {'expired': True} / {'waiting': True, ...}"""
    if not isinstance(res, dict):
        return {"waiting": True, "message": "响应异常"}
    # 状态码口径来自 xiaoya-alist：50004001=等待扫码，50004002=码无效或已过期
    if res.get("status") == 50004002:
        return {"expired": True, "message": "二维码无效或已过期"}
    members = ((res.get("data") or {}).get("members")) or {}
    st = members.get("service_ticket") or members.get("ticket")
    if res.get("status") == 2000000 and st:
        return {"st": str(st)}
    msg = str(res.get("message") or "")
    if res.get("status"):
        msg = f"{msg}({res['status']})".strip()
    return {"waiting": True, "message": msg or "等待扫码"}


def merge_set_cookies(raw_list) -> dict:
    """Set-Cookie 列表 → {name: value}，同名保留最后一次（后写的覆盖先写的）"""
    pairs: dict = {}
    for raw in raw_list or []:
        pair = (raw or "").split(";", 1)[0].strip()
        if "=" in pair:
            k, v = pair.split("=", 1)
            if k.strip():
                pairs[k.strip()] = v
    return pairs


def cookie_header(pairs: dict) -> str:
    return "; ".join(f"{k}={v}" for k, v in pairs.items())


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # 换 Cookie 的请求要逐跳收集 Set-Cookie，urllib 自动重定向会丢中间跳的
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def fetch_no_redirect(url: str, cookie_pairs: dict | None = None,
                      timeout: int = 15, headers: dict | None = None) -> tuple:
    """返回 (status, body, set_cookie列表, location)；3xx 不自动跳"""
    if headers is None:
        headers = {"User-Agent": UA,
                   "Referer": "https://pan.quark.cn/",
                   "Origin": "https://pan.quark.cn",
                   "Accept": "application/json, text/plain, */*"}
    if cookie_pairs:
        headers = {**headers, "Cookie": cookie_header(cookie_pairs)}
    req = urllib.request.Request(url, headers=headers)
    try:
        resp = _OPENER.open(req, timeout=timeout)
        return resp.status, resp.read(), list(resp.headers.get_all("Set-Cookie") or []), None
    except urllib.error.HTTPError as e:
        return e.code, e.read(), list(e.headers.get_all("Set-Cookie") or []), e.headers.get("Location")
    except urllib.error.URLError as e:
        raise QrError(f"网络错误：{e.reason}")


def fetch_collect(url: str, pairs: dict, timeout: int = 15,
                  headers: dict | None = None) -> tuple:
    """请求并把沿途（含重定向各跳）的 Set-Cookie 并入 pairs；返回 (status, body)"""
    body = b""
    for _ in range(6):
        status, body, setc, loc = fetch_no_redirect(url, pairs, timeout, headers)
        pairs.update(merge_set_cookies(setc))
        if 300 <= status < 400 and loc:
            url = urllib.parse.urljoin(url, loc)
            continue
        break
    return status, body


# uop 认证接口用极简头：xiaoya 的实现连 UA 都不定制照样工作，
# 说明这组接口对请求头不敏感——少带 Referer/Origin 反而少一个变量
_PLAIN_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")


def qr_get_token(collector: dict, request_id: str | None = None) -> str:
    """取二维码 token。CAS 是会话制：本请求下发的会话 Cookie 必须吸收进
    collector 并在后续轮询/换 ticket 时回带（LitePan 注释原文「取 token
    阶段吸收的 CAS Cookie，轮询时回带」），否则 ticket 换不出登录态。"""
    rid = request_id or str(uuid.uuid4())  # 标准带横杠 UUID（与 xiaoya 一致）
    status, body, setc, _ = fetch_no_redirect(
        f"{UOP_QR_TOKEN_URL}?client_id=532&v=1.2&request_id={rid}",
        cookie_pairs=collector or None,
        headers={"User-Agent": _PLAIN_UA, "Accept-Language": "zh-CN,zh;q=0.9"})
    collector.update(merge_set_cookies(setc))
    try:
        res = json.loads(body.decode("utf-8"))
    except Exception as e:
        raise QrError("获取二维码 token 失败：响应不是 JSON") from e
    if res.get("status") == 2000000:
        token = ((res.get("data") or {}).get("members") or {}).get("token")
        if token:
            return token
    raise QrError(f"获取二维码 token 失败：{res.get('message') or res.get('status')}")


def qr_poll(token: str, collector: dict, request_id: str | None = None) -> dict:
    rid = request_id or str(uuid.uuid4())
    url = (f"{UOP_QR_POLL_URL}?client_id=532&v=1.2"
           f"&token={urllib.parse.quote(token)}&request_id={rid}")
    status, body, setc, _ = fetch_no_redirect(
        url, cookie_pairs=collector or None,
        headers={"User-Agent": _PLAIN_UA, "Accept-Language": "zh-CN,zh;q=0.9"})
    collector.update(merge_set_cookies(setc))
    try:
        res = json.loads(body.decode("utf-8"))
    except Exception:
        return {"waiting": True, "message": "响应异常"}
    return parse_poll_response(res)


# ticket 换到登录态后的 bootstrap：访问首页 + 列目录，补全 drive-pc/.quark.cn
# 域 Cookie（LitePan finalizeQRLogin 同款）；member 兜底补 __puus
QR_BOOTSTRAP_URLS = [
    "https://pan.quark.cn/",
    ("https://drive-pc.quark.cn/1/clouddrive/file/sort?pr=ucpro&fr=pc&uc_param_str="
     "&pdir_fid=0&_page=1&_size=1&_fetch_total=1"),
    ("https://drive.quark.cn/1/clouddrive/member?pr=ucpro&fr=pc&uc_param_str="
     "&fetch_subscribe=true"),
]


def qr_exchange(st: str, collector: dict) -> tuple:
    """service_ticket → (cookie键值对, 用户信息, 是否缺__puus)。
    collector 必须已带取 token/轮询阶段吸收的 CAS 会话 Cookie。"""
    status, body = fetch_collect(
        f"https://pan.quark.cn/account/info?st={urllib.parse.quote(st)}&lw=scan",
        collector,
        headers={"User-Agent": _PLAIN_UA,
                 "Referer": "https://pan.quark.cn/",
                 "Accept": "application/json, text/plain, */*"})
    if status >= 400:
        raise QrError(f"换取 Cookie 失败：HTTP {status}")
    user_info = {}
    try:
        user_info = json.loads(body.decode("utf-8"))
    except Exception:
        pass
    for step_url in QR_BOOTSTRAP_URLS:
        if "__puus" in collector and "__pus" in collector:
            break
        try:
            fetch_collect(step_url, collector)
        except QrError:
            continue  # bootstrap 是尽力而为，失败不阻塞登录
    if not collector:
        raise QrError("未收到任何 Cookie")
    return collector, user_info, "__puus" not in collector


def qr_text(data: str, border: int = 4) -> str:
    """二维码 → 半块字符画（█▀▄空格）。渲染时整体套 black-on-white 样式，
    黑块 = 前景色，白底 = 背景色，在浅色/深色终端下都成立"""
    import qrcode
    qr = qrcode.QRCode(border=border, error_correction=qrcode.constants.ERROR_CORRECT_H)
    qr.add_data(data)
    qr.make(fit=True)
    matrix = qr.get_matrix()  # 含边框，1=黑
    lines = []
    for y in range(0, len(matrix), 2):
        top = matrix[y]
        bot = matrix[y + 1] if y + 1 < len(matrix) else [0] * len(matrix)
        lines.append("".join(
            "█" if a and b else "▀" if a else "▄" if b else " "
            for a, b in zip(top, bot)))
    return "\n".join(lines)


def qr_png(data: str, path=QR_PNG_PATH) -> Path:
    """同内容生成 PNG 落盘——终端字符画扫不出来时的保底（图片查看器渲染无歧义）"""
    import qrcode
    img = qrcode.make(data, error_correction=qrcode.constants.ERROR_CORRECT_H, border=4)
    img.save(str(path))
    return path


# ---------------------------------------------------------------- 自测

_GOLDEN_B64 = ("QUEvJUU2JUI1JThCJUU4JUFGJTk1JTIwJUU2JTk2JTg3JUU0JUJCJUI2JTVCMDElNUQubWt2"
               "Lz91cmw9aHR0cHMlM0ElMkYlMkZkb3dubG9hZC5xdWFyay5jbiUyRnh4eHh4JTNGYXV0aF9rZXk"
               "lM0QxNzAwMDAwMDAwLTAtMC1hYmNkZWYlMjZidHlwZSUzRDEmdXNlcl9hZ2VudD1Nb3ppbGxhJT"
               "JGNS4wKyUyOFdpbmRvd3MrTlQrMTAuMCUzQitXaW42NCUzQit4NjQlMjkrQXBwbGVXZWJLaXQlM"
               "kY1MzcuMzYrJTI4S0hUTUwlMkMrbGlrZStHZWNrbyUyOStDaHJvbWUlMkYxMzAuMC4wLjArU2Fm"
               "YXJpJTJGNTM3LjM2K1F1YXJrUEMlMkY2LjkuNy43NjEmY29va2llPV9fcHVzJTNEYWJjMTIzJTN"
               "CK19fcHV1cyUzRGRlZjQ1Nlpa")
_GOLDEN_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 QuarkPC/6.9.7.761")
_GOLDEN_COOKIE = "__pus=abc123; __puus=def456"


def selftest() -> int:
    """纯逻辑自测：golden 是从原 userscript 的 JS 实现跑出来的真实输出"""
    failures = []

    def check(name, cond, detail=""):
        print(("PASS " if cond else "FAIL ") + name + (f"  {detail}" if detail and not cond else ""))
        if not cond:
            failures.append(name)

    # 1. 文件名清洗：13 个非法字符 → 13 个下划线
    check("fix_filename", fix_filename('a!?&|`"\'*/:<>\\b') == "a" + "_" * 13 + "b")

    # 2. uri_component 与 encodeURIComponent 逐字符对齐
    check("uri_component.cjk",
          uri_component("测试 文件[01].mkv") == "%E6%B5%8B%E8%AF%95%20%E6%96%87%E4%BB%B6%5B01%5D.mkv")
    check("uri_component.reserved",
          uri_component("a b!~*'()-._") == "a%20b!~*'()-._")

    # 3. 端到端 golden：与 JS 输出逐字节一致
    got = bc_link("测试 文件[01].mkv",
                  "https://download.quark.cn/xxxxx?auth_key=1700000000-0-0-abcdef&btype=1",
                  user_agent=_GOLDEN_UA, cookie=_GOLDEN_COOKIE)
    check("golden.bc_link", got == f"bc://http/{_GOLDEN_B64}")

    # 4. 载荷结构：AA 开头、ZZ 结尾、utf-8 可回解
    payload = base64.b64decode(got.removeprefix("bc://http/")).decode("utf-8")
    check("payload.frame", payload.startswith("AA/") and payload.endswith("ZZ"))
    check("payload.url", "%3A%2F%2F" in payload and "user_agent=" in payload and "cookie=" in payload)

    # 5. 轮询响应解析
    check("qr.poll_success",
          parse_poll_response({"status": 2000000,
                               "data": {"members": {"service_ticket": "ST1"}}}) == {"st": "ST1"})
    check("qr.poll_waiting",
          parse_poll_response({"status": 50004001, "message": "Query result is empty"})["waiting"] is True)
    check("qr.poll_expired",
          parse_poll_response({"status": 50004002, "message": "expired"}).get("expired") is True)
    check("qr.poll_garbage", parse_poll_response(None)["waiting"] is True)

    # 6. Set-Cookie 合并：同名取最新、保序
    pairs = merge_set_cookies(["a=1; Path=/; HttpOnly", "b=2", "a=3"])
    check("qr.merge_cookies", pairs == {"a": "3", "b": "2"} and cookie_header(pairs) == "a=3; b=2")

    # 7. 二维码渲染：字符画回解后必须与原始矩阵逐模块一致
    import qrcode
    test_url = qr_url("TOK123")
    check("qr.url_template",
          test_url == ("https://su.quark.cn/4_eMHBJ?token=TOK123&client_id=532&ssb=weblogin"
                       "&uc_param_str=&uc_biz_str=S%3Acustom%7COPT%3ASAREA%400%7COPT%3AIMMERSIVE"
                       "%401%7COPT%3ABACK_BTN_STYLE%400"))
    qr = qrcode.QRCode(border=4, error_correction=qrcode.constants.ERROR_CORRECT_H)
    qr.add_data(test_url)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    rows = []
    for line in qr_text(test_url).splitlines():
        rows.append([1 if ch in "█▀" else 0 for ch in line])   # 上半块
        rows.append([1 if ch in "█▄" else 0 for ch in line])   # 下半块
    rows = rows[:len(matrix)]
    check("qr.render_roundtrip", rows == matrix and len(rows) == len(matrix))

    # 8. PNG 保底：能落盘且尺寸为正
    png = qr_png(test_url, path=Path(__file__).with_name("_selftest_qr.png"))
    from PIL import Image
    with Image.open(png) as im:
        check("qr.png_valid", im.size[0] > 0 and im.size[1] > 0)
    png.unlink(missing_ok=True)

    # 9. Cookie 轮换吸收：响应下发新 __puus 必须替换并回调；无变化不回调
    class _FakeHeaders:
        def __init__(self, cookies):
            self._c = cookies

        def get_all(self, name):
            return self._c if name == "Set-Cookie" else None

    fired = []
    c = QuarkClient("a=1; __puus=old", on_cookie_update=lambda ck: fired.append(ck))
    c._absorb_cookie_rotation(_FakeHeaders(["__puus=new; Path=/; HttpOnly", "x=2; Domain=.quark.cn"]))
    check("cookie.rotate_value", c.cookie == "a=1; __puus=new; x=2", c.cookie)
    check("cookie.rotate_cb", fired == [c.cookie])
    c._absorb_cookie_rotation(_FakeHeaders([]))
    c._absorb_cookie_rotation(_FakeHeaders(["__puus=new"]))
    check("cookie.rotate_noop", fired == [c.cookie] and c.cookie == "a=1; __puus=new; x=2")

    # 10. 分享链接解析 + 分批（转存单次有条目上限，切批错了会整批失败）
    check("share.parse_plain", parse_share_link("https://pan.quark.cn/s/abc123") == ("abc123", ""))
    check("share.parse_pwd_query",
          parse_share_link("https://pan.quark.cn/s/abc123?pwd=k9x2") == ("abc123", "k9x2"))
    check("share.parse_pwd_word",
          parse_share_link("https://pan.quark.cn/s/abc123 提取码：k9x2") == ("abc123", "k9x2"))
    check("share.parse_pwd_tail",
          parse_share_link("https://pan.quark.cn/s/abc123 k9x2") == ("abc123", "k9x2"))
    check("share.parse_share_hash",
          parse_share_link("https://pan.quark.cn/s/9f8e7d#/list/share")[0] == "9f8e7d")
    check("share.parse_garbage", parse_share_link("https://example.com/x") == ("", ""))
    check("share.chunk_exact", [len(b) for b in chunk_list(list(range(200)), 100)] == [100, 100])
    check("share.chunk_rest", [len(b) for b in chunk_list(list(range(250)), 100)] == [100, 100, 50])
    check("share.chunk_guard", [len(b) for b in chunk_list([1, 2, 3], 0)] == [1, 1, 1])

    # 11. 非 2xx 里的业务错误码要透出来（无效分享/未登录全是这条路，实测过）
    def _fake_urlopen(req, timeout=0):
        import io
        body = b'{"code":41006,"message":"\xe5\x88\x86\xe4\xba\xab\xe4\xb8\x8d\xe5\xad\x98\xe5\x9c\xa8"}'
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, io.BytesIO(body))

    _orig_urlopen = urllib.request.urlopen
    urllib.request.urlopen = _fake_urlopen
    try:
        msg = ""
        try:
            QuarkClient("a=1").share_stoken("bogus", "")
        except QuarkError as e:
            msg = str(e)
    finally:
        urllib.request.urlopen = _orig_urlopen
    check("api.error_body_code", "41006" in msg and "分享不存在" in msg, msg)

    # 12. 转存 save 域名兜底：首选域名被判 guest 时要换域名重试
    #     （实测 drive-pc 的 save 回 31001 require login [guest]）
    tried: list = []
    state = {"fail_once": True}

    class _HostFallback(QuarkClient):
        def _req_json(self, url, *, data=None, headers=None):
            host = url.split("/1/clouddrive")[0]
            tried.append(host)
            if state["fail_once"]:      # 只让第一次请求失败，模拟首选域名不认登录态
                state["fail_once"] = False
                return {"code": 31001, "message": "require login [guest]"}
            return {"code": 0, "data": {"task_id": "T1"}}

    fc = _HostFallback("a=1")
    tid = fc.share_save("pid", "st", ["f1"], ["tok1"])
    check("share.save_fallback",
          tid == "T1" and tried[:2] == ["https://drive.quark.cn",
                                        "https://drive-pc.quark.cn"], str(tried))
    tried.clear()
    fc.share_save("pid", "st", ["f2"], ["tok2"])
    check("share.save_host_cached", tried == ["https://drive-pc.quark.cn"], str(tried))

    # 13. 写接口必须带 Origin/Referer + __dt/__t（少了被判匿名）
    seen: dict = {}

    class _Hdr(QuarkClient):
        def _req_json(self, url, *, data=None, headers=None):
            seen["url"] = url
            seen["h"] = headers or {}
            return {"code": 0, "data": {"task_id": "T2"}}

    _Hdr("a=1").share_save("pid", "st", ["f"], ["t"])
    check("share.save_anticrawl", "__dt=" in seen["url"] and "__t=" in seen["url"],
          seen["url"][:100])
    check("share.save_web_headers",
          seen["h"].get("Referer") == "https://pan.quark.cn/"
          and seen["h"].get("Origin") == "https://pan.quark.cn")

    # 14. 有提取码的分享，save 要带 pwd_id_extra
    body_seen: dict = {}

    class _Body(QuarkClient):
        def _req_json(self, url, *, data=None, headers=None):
            body_seen.update(json.loads(data))
            return {"code": 0, "data": {"task_id": "T3"}}

    _Body("a=1").share_save("pid", "st", ["f"], ["t"], passcode="k9x2")
    check("share.save_pwd_extra", body_seen.get("pwd_id_extra") == "k9x2", str(body_seen))

    # 15. task 轮询也要域名兜底（同一份 Cookie 在 drive-pc 的写接口上被判过 guest）
    tq: list = []

    class _TaskHost(QuarkClient):
        def _req_json(self, url, *, data=None, headers=None):
            host = url.split("/1/clouddrive")[0]
            tq.append(host)
            if host == "https://drive.quark.cn":
                return {"code": 0, "data": {"status": 2}}
            return {"code": 31001, "message": "require login [guest]"}

    th = _TaskHost("a=1")
    th._task_host = "https://drive-pc.quark.cn"    # 假设之前记住了坏域名
    res = th.wait_task("T")
    check("share.task_fallback",
          res.get("status") == 2
          and tq[:2] == ["https://drive-pc.quark.cn", "https://drive.quark.cn"], str(tq))
    tq.clear()
    th.wait_task("T")
    check("share.task_host_cached", tq == ["https://drive.quark.cn"], str(tq))

    # 16. 41035 超量拆分：整包被拒 → 单个文件夹建同名目录下钻 → 子项按批存
    #     （真机实测：文件夹只算 1 个条目，能提交，但任务执行时才报超限）
    class _SplitFake(QuarkClient):
        tree = {"0": [{"fid": "fld", "file_name": "大合集", "file": False}],
                "fld": [{"fid": "a", "file_name": "a.mkv", "file": True},
                        {"fid": "b", "file_name": "b.mkv", "file": True},
                        {"fid": "c", "file_name": "c.mkv", "file": True}]}
        saved: list = []
        mkdirs: list = []

        def share_stoken(self, pwd_id, passcode=""):
            return "st"

        def share_list(self, pwd_id, stoken, pdir_fid="0"):
            return list(self.tree.get(pdir_fid, [])), "T"

        def ensure_dir(self, name, pdir_fid="0"):
            self.mkdirs.append((name, pdir_fid))
            return "newdir"

        def share_save(self, pwd_id, stoken, fid_list, fid_token_list,
                       to_pdir_fid="0", passcode="", on_log=None):
            if len(fid_list) > 2 or fid_list == ["fld"]:
                raise QuarkError("提交转存失败：code=41035 单次转存文件个数超出用户等级限制")
            self.saved.append((list(fid_list), to_pdir_fid))
            return "task"

        def wait_task(self, task_id, timeout=1.0, on_log=None):
            return {"status": 2}

    sf = _SplitFake("a=1")
    res = sf.transfer_share("pid", "", to_pdir_fid="dir0", batch_size=2)
    check("share.split_mkdir", sf.mkdirs == [("大合集", "dir0")], str(sf.mkdirs))
    check("share.split_batches",
          [s[0] for s in sf.saved] == [["a", "b"], ["c"]], str(sf.saved))
    check("share.split_to_subdir", all(s[1] == "newdir" for s in sf.saved), str(sf.saved))
    check("share.split_ok", res["ok"] == 3 and not res["failed"], str(res))

    print(f"\n{len(failures)} failed" if failures else "\nall passed")
    return 1 if failures else 0


# ---------------------------------------------------------------- TUI

def is_folder(item: dict) -> bool:
    # 与原脚本口径一致：file === false 即目录；再兜底 obj_category
    if item.get("obj_category") == "folder":
        return True
    if item.get("file") is False:
        return True
    return bool(item.get("dir"))


def _lazy_import_textual():
    try:
        import textual  # noqa: F401
    except ImportError:
        sys.exit("缺少依赖：请先执行  pip install textual pyperclip")


if _TUI := True:  # noqa: E999 —— 下面整段 TUI 依赖 textual，延迟到真正运行时才 import
    pass


class QuarkBCApp:  # 占位避免前向引用；真正实现见下方 build_app()
    pass


def build_app():
    from rich.text import Text
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal
    from textual.screen import ModalScreen
    from textual.widgets import (Button, DataTable, Footer, Header, Input,
                                 Label, RichLog, Static, TextArea)

    class QrLoginScreen(ModalScreen):
        """扫码登录：uop 换 token → 终端渲染二维码 → 轮询 → 换 Cookie"""
        BINDINGS = [("escape", "cancel", "取消")]

        def __init__(self):
            super().__init__()
            self._cancelled = False
            self._png: Path | None = None

        def compose(self) -> ComposeResult:
            yield Label("[b]夸克 APP 扫码登录[/]　扫描下方二维码并在手机上确认", id="q-title")
            yield Static("", id="q-qr")
            yield Label("正在获取二维码…", id="q-status")
            yield Button("终端码扫不出来？打开图片扫", id="q-open-img", variant="warning")

        def on_mount(self) -> None:
            self.run_worker(self._worker, thread=True, exclusive=True, group="qr")

        def on_button_pressed(self, event: Button.Pressed) -> None:
            if event.button.id == "q-open-img" and self._png:
                # Windows 下用系统默认图片查看器打开，避开终端字符画的渲染歧义
                if sys.platform.startswith("win"):
                    os.startfile(str(self._png))  # noqa: S606
                elif sys.platform == "darwin":
                    import subprocess
                    subprocess.Popen(["open", str(self._png)])
                else:
                    import subprocess
                    subprocess.Popen(["xdg-open", str(self._png)])
                self.notify("已打开二维码图片，请用夸克 APP 扫描", severity="information")

        def _worker(self) -> None:
            # 注意：call_from_thread 挂在 App 上而不是 Screen 上，线程 worker
            # 里碰 UI 必须走 self.app.call_from_thread
            app = self.app
            session: dict = {}   # CAS 会话 Cookie，token→轮询→换 ticket 全程贯通
            try:
                token = qr_get_token(session)
                url = qr_url(token)
                self._png = qr_png(url)   # 同步生成 PNG 保底
            except Exception as e:
                app.call_from_thread(self._set_status, f"[red]获取二维码失败：{e}[/]")
                return
            app.call_from_thread(self._show, qr_text(url))
            app.call_from_thread(self._set_status,
                                 "等待扫码…（终端码扫不出来就点下方按钮打开图片扫）")
            deadline = time.time() + QR_EXPIRE_SECONDS
            while time.time() < deadline and not self._cancelled:
                try:
                    res = qr_poll(token, session)
                except Exception as e:
                    app.call_from_thread(self._set_status, f"[red]轮询失败：{e}[/]")
                    time.sleep(QR_POLL_INTERVAL)
                    continue
                if "st" in res:
                    app.call_from_thread(self._set_status, "扫码成功，正在换取 Cookie…")
                    try:
                        pairs, info, missing = qr_exchange(res["st"], session)
                    except Exception as e:
                        app.call_from_thread(self._set_status, f"[red]换取 Cookie 失败：{e}[/]")
                        return
                    app.call_from_thread(self._done, cookie_header(pairs), missing, info)
                    return
                if res.get("expired"):
                    app.call_from_thread(self._set_status,
                                         "[yellow]二维码无效或已过期，esc 关闭后按 l 重试[/]")
                    return
                left = int(deadline - time.time())
                app.call_from_thread(self._set_status,
                                     f"等待扫码… {res.get('message', '')}（剩余 {left}s）")
                time.sleep(QR_POLL_INTERVAL)
            if not self._cancelled:
                app.call_from_thread(self._set_status, "[yellow]二维码已过期，esc 关闭后重试[/]")

        def _show(self, art: str) -> None:
            # 整体 black-on-white：█/▀/▄/空格 分别表示双黑/上黑/下黑/双白，
            # 配合半块字符一个终端格竖向压两个模块，浅色深色终端都能扫
            self.query_one("#q-qr", Static).update(Text(art, style="black on white"))

        def _set_status(self, msg: str) -> None:
            self.query_one("#q-status", Label).update(msg)

        def _done(self, cookie: str, missing_puus: bool, info: dict) -> None:
            nickname = ""
            if isinstance(info, dict):
                nickname = (info.get("data") or {}).get("nickname") or ""
            self.app.apply_qr_cookie(cookie)
            suffix = ("　[n yellow]缺 __puus，下载可能受限[/]"
                      if missing_puus else "") 
            self.notify(f"扫码登录成功{'：' + nickname if nickname else ''}{suffix}",
                        severity="warning" if missing_puus else "information")
            self.dismiss()

        def action_cancel(self) -> None:
            self._cancelled = True
            self.dismiss()

    class SettingsScreen(ModalScreen):
        BINDINGS = [("escape", "cancel", "取消(不保存)")]

        def __init__(self, config: dict):
            super().__init__()
            self._cfg = config

        def compose(self) -> ComposeResult:
            yield Label("[b]夸克 Cookie[/]（通常用不到：按 l 扫码即可自动获取）",
                        id="s-label-cookie")
            yield TextArea(self._cfg.get("cookie", ""), id="s-cookie")
            yield Button("保存", id="s-save", variant="primary")

        def on_button_pressed(self, event: Button.Pressed) -> None:
            if event.button.id == "s-save":
                cfg = {"cookie": self.query_one("#s-cookie", TextArea).text.strip()}
                self.dismiss(cfg)

        def action_cancel(self) -> None:
            self.dismiss(None)

    class ResultScreen(ModalScreen):
        BINDINGS = [Binding("escape", "close", "关闭"),
                    Binding("w", "wake", "唤起下载"),
                    Binding("c", "copy_all", "复制全部")]

        def __init__(self, links: list, title: str):
            super().__init__()
            self._links = links
            self._title = title

        def compose(self) -> ComposeResult:
            yield Label(f"[b]{self._title}[/]　共 {len(self._links)} 条　"
                        f"[dim]w=唤起比特彗星 / c=复制全部 / esc=关闭[/]")
            yield TextArea("\r\n".join(l["bc"] for l in self._links),
                           read_only=True, id="r-links")
            with Horizontal(id="r-btns"):
                yield Button("唤起比特彗星下载", id="r-wake", variant="success")
                yield Button("复制全部", id="r-copy", variant="primary")

        @property
        def links(self):
            return self._links

        def on_button_pressed(self, event: Button.Pressed) -> None:
            if event.button.id == "r-wake":
                self.action_wake()
            elif event.button.id == "r-copy":
                self.action_copy_all()

        def action_copy_all(self) -> None:
            import pyperclip
            pyperclip.copy("\r\n".join(l["bc"] for l in self._links))
            self.notify("已复制全部 BC 链接", severity="information")

        def action_wake(self) -> None:
            self.dismiss()
            self.app.wake_bitcomet(self._links)

        def action_close(self) -> None:
            self.dismiss()

    class TransferScreen(ModalScreen):
        """分享链接批量转存。

        转存接口单次能提交的条目数有上限，所以按「每批条目数」切批提交；
        顶层是文件夹的条目只算一个（服务端递归转存整个目录），不会拆碎结构。
        """
        BINDINGS = [("escape", "cancel", "关闭")]

        def __init__(self):
            super().__init__()
            # 注意别用 _running：MessagePump（Screen 的父类）自己就有 _running，
            # 一进来就被自己的守卫判成「上一轮还在跑」，worker 永远不启动
            self._busy = False

        def compose(self) -> ComposeResult:
            yield Label("[b]分享链接批量转存[/]　一行一条，形如 "
                        "[dim]https://pan.quark.cn/s/xxxx?pwd=abcd[/]"
                        "（提取码也可写成「提取码：abcd」）　"
                        "[yellow]单次转存文件数受账号等级限制，超出会自动拆批[/]", id="t-title")
            yield TextArea("", id="t-links")
            with Horizontal(id="t-opts"):
                yield Input(placeholder="目标文件夹（留空=根目录，不存在会自动创建）", id="t-dir")
                yield Input(value="100", placeholder="每批条目数（超限会自动再拆）", id="t-batch")
            with Horizontal(id="t-btns"):
                yield Button("开始转存", id="t-start", variant="primary")
                yield Button("关闭", id="t-close")
            yield RichLog(id="t-log", markup=True, wrap=True, max_lines=400)

        def on_button_pressed(self, event: Button.Pressed) -> None:
            if event.button.id == "t-start":
                self.action_start()
            elif event.button.id == "t-close":
                self.dismiss()

        def _log(self, msg: str) -> None:
            self.query_one("#t-log", RichLog).write(msg)

        def action_start(self) -> None:
            if self._busy:
                self.notify("上一轮还在跑，等它结束", severity="warning")
                return
            raw = self.query_one("#t-links", TextArea).text
            links: list[tuple[str, str, str]] = []
            for line in raw.splitlines():
                line = line.strip()
                if not line:
                    continue
                pid, pwd = parse_share_link(line)
                if pid:
                    links.append((pid, pwd, line))
                else:
                    self._log(f"[yellow]跳过无法识别的行[/]：{line[:70]}")
            if not links:
                self.notify("没有可用的分享链接", severity="warning")
                return
            dir_name = self.query_one("#t-dir", Input).value.strip()
            try:
                batch = int(self.query_one("#t-batch", Input).value.strip() or "100")
            except ValueError:
                batch = 100
            batch = max(1, batch)
            self._busy = True
            self._log(f"[b]开始：{len(links)} 条链接，每批 {batch} 个条目，"
                      f"目标 {'根目录' if not dir_name else dir_name}[/]")
            self.run_worker(lambda: self._worker(links, dir_name, batch),
                            thread=True, exclusive=True, group="transfer")

        def action_cancel(self) -> None:
            self.dismiss()

        def _worker(self, links: list, dir_name: str, batch: int) -> None:
            app = self.app
            client = app.client
            if client is None:
                app.call_from_thread(self._log, "[red]未登录，先按 l 扫码[/]")
                app.call_from_thread(setattr, self, "_busy", False)
                return
            ok_total = 0
            try:
                to_fid = client.ensure_dir(dir_name) if dir_name else "0"
                if dir_name:
                    app.call_from_thread(self._log, f"目标目录：{dir_name}（fid={to_fid}）")
                for i, (pid, pwd, raw) in enumerate(links, 1):
                    app.call_from_thread(self._log,
                                         f"[b]（{i}/{len(links)}）{raw[:70]}[/]")
                    try:
                        res = client.transfer_share(
                            pid, pwd, to_pdir_fid=to_fid, batch_size=batch,
                            on_log=lambda m: app.call_from_thread(self._log, m))
                        ok_total += res["ok"]
                        tail = (f"　[red]失败 {len(res['failed'])} 批[/]"
                                if res["failed"] else "")
                        app.call_from_thread(
                            self._log,
                            f"[green]这条完成[/]：{res['ok']}/{res['total']} 个条目{tail}")
                    except Exception as e:
                        app.call_from_thread(self._log, f"[red]这条失败[/]：{e}")
            except Exception as e:
                app.call_from_thread(self._log, f"[red]准备目标目录失败[/]：{e}")
            finally:
                app.call_from_thread(setattr, self, "_busy", False)
                app.call_from_thread(self.notify,
                                     f"转存结束：共提交成功 {ok_total} 个条目")
                app.call_from_thread(app.action_refresh)

    class QuarkBCAppImpl(App):
        TITLE = "QuarkBC · 夸克网盘 → 比特彗星"
        CSS = """
        #pathbar { height: 1; padding: 0 1; color: $text-muted; }
        #toolbar { height: 3; }
        #toolbar Button { margin-right: 1; min-width: 10; }
        #files { height: 1fr; }
        #log { height: 7; border-top: solid $primary; padding: 0 1; }
        SettingsScreen { align: center middle; }
        #s-cookie { height: 6; width: 90%; }
        SettingsScreen Input { width: 90%; }
        ResultScreen { align: center middle; }
        #r-links { height: 1fr; width: 95%; }
        #r-btns { height: 3; }
        #r-btns Button { margin-right: 1; }
        QrLoginScreen { align: center middle; }
        #q-title { margin-bottom: 1; }
        #q-status { margin-top: 1; }
        TransferScreen { align: center middle; }
        #t-title { margin-bottom: 1; }
        #t-links { height: 8; width: 95%; }
        #t-opts { height: 3; width: 95%; }
        #t-opts Input { width: 1fr; margin-right: 1; }
        #t-btns { height: 3; }
        #t-btns Button { margin-right: 1; }
        #t-log { height: 12; width: 95%; border: solid $primary; }
        """
        BINDINGS = [
            Binding("space", "toggle", "勾选"),
            Binding("o", "open", "进入/生成"),
            Binding("b", "gen_bc", "生成BC链接"),
            Binding("w", "wake", "唤起下载"),
            Binding("u,backspace", "up", "上级"),
            Binding("r", "refresh", "刷新"),
            Binding("t", "transfer", "批量转存"),
            Binding("l", "qrlogin", "扫码登录"),
            Binding("s", "settings", "设置"),
            Binding("q", "quit", "退出"),
        ]

        def __init__(self, client=None, config: dict | None = None,
                     config_path: Path | None = None):
            super().__init__()
            self.config_path = config_path
            self.config = config or load_config(config_path or CONFIG_PATH)
            self.client = client or (QuarkClient(self.config["cookie"],
                                     on_cookie_update=self._persist_cookie)
                                     if self.config.get("cookie") else None)
            self.stack: list[tuple[str, str]] = [("0", "根目录")]
            self.selected: dict[str, dict] = {}   # fid -> 列表项
            self.items: dict[str, dict] = {}      # fid -> 当前列表项（勾选判断要用原始字段）
            self.order: list[str] = []            # fid，与表格行序一致

        # ---------- UI 骨架

        def compose(self) -> ComposeResult:
            yield Header()
            yield Static("", id="pathbar")
            with Horizontal(id="toolbar"):
                yield Button("生成链接(b)", id="tb-gen", variant="primary")
                yield Button("唤起下载(w)", id="tb-wake", variant="success")
                yield Button("上级(u)", id="tb-up")
                yield Button("刷新(r)", id="tb-refresh")
                yield Button("批量转存(t)", id="tb-transfer")
                yield Button("扫码登录(l)", id="tb-qr")
                yield Button("设置(s)", id="tb-set")
            yield DataTable(id="files")
            yield RichLog(id="log", markup=True, wrap=True, max_lines=300)
            yield Footer()

        def on_mount(self) -> None:
            dt = self.query_one("#files", DataTable)
            dt.cursor_type = "row"
            dt.add_column("√", key="sel", width=4)
            dt.add_column("类型", key="kind", width=6)
            dt.add_column("名称", key="name")
            dt.add_column("大小", key="size", width=14)
            if self.client is None:
                self.action_settings()
            else:
                self.reload()

        def on_button_pressed(self, event: Button.Pressed) -> None:
            # 主屏工具栏：所有高频操作都可以纯鼠标完成
            actions = {
                "tb-gen": self.action_gen_bc,
                "tb-wake": self.action_wake,
                "tb-up": self.action_up,
                "tb-refresh": self.action_refresh,
                "tb-transfer": self.action_transfer,
                "tb-qr": self.action_qrlogin,
                "tb-set": self.action_settings,
            }
            handler = actions.get(event.button.id)
            if handler:
                handler()

        def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
            # 鼠标单击行 / 回车 = 目录进入、文件勾选切换
            # （DataTable 默认把 enter 绑定为行选中，点击行同样触发本消息）
            fid = event.row_key.value
            if not fid:
                return
            item = self.items.get(fid)
            if item and is_folder(item):
                self.stack.append((fid, item.get("file_name") or fid))
                self.reload()
            else:
                self._toggle_fid(fid)

        # ---------- 唤起比特彗星（bc:// 协议处理器自动拉起程序，无需提前开着）

        def wake_bitcomet(self, links: list) -> None:
            if not sys.platform.startswith("win"):
                self.notify("bc:// 协议唤起仅支持 Windows", severity="warning")
                return
            ok = 0
            for l in links:
                try:
                    os.startfile(l["bc"])  # noqa: S606 ShellExecute 处理注册协议
                    ok += 1
                except Exception as e:
                    self.notify(f"唤起失败：{e}", severity="error")
                time.sleep(0.25)  # 给 BC 协议处理器留出排队时间
            if ok:
                self.notify(f"已向比特彗星发起 {ok} 个下载任务（若未弹窗请检查 BC 是否安装）")

        # ---------- 工具

        def _log(self, line: str) -> None:
            self.query_one("#log", RichLog).write(line)

        def _pathbar(self) -> None:
            path = " / ".join(n for _, n in self.stack)
            self.query_one("#pathbar", Static).update(f"{path}　　已选 {len(self.selected)} 个文件")

        def _current_fid(self) -> str | None:
            dt = self.query_one("#files", DataTable)
            if 0 <= dt.cursor_row < len(self.order):
                return self.order[dt.cursor_row]
            return None

        # ---------- 目录加载

        def reload(self) -> None:
            if self.client is None:
                return
            dt = self.query_one("#files", DataTable)
            dt.loading = True
            self.run_worker(self._worker_dir, thread=True, exclusive=True, group="dir")

        def _worker_dir(self) -> None:
            fid, _ = self.stack[-1]
            try:
                items = self.client.list_dir(fid)
            except Exception as e:
                self.call_from_thread(self._dir_failed, str(e))
                return
            self.call_from_thread(self._populate, items)

        def _dir_failed(self, msg: str) -> None:
            self.query_one("#files", DataTable).loading = False
            self._log(f"[red]列目录失败[/] {msg}")
            hint = "——按 l 重新扫码即可恢复" if "401" in msg else ""
            self.notify(f"列目录失败：{msg}{hint}", severity="error")

        def _populate(self, items: list) -> None:
            dt = self.query_one("#files", DataTable)
            dt.loading = False
            dt.clear()
            self.order = []
            self.items = {}
            for it in items:
                fid = it.get("fid") or ""
                name = it.get("file_name") or "(未命名)"
                folder = is_folder(it)
                self.items[fid] = it
                self.order.append(fid)
                dt.add_row("√" if fid in self.selected else "",
                           "目录" if folder else "文件",
                           name, "-" if folder else size_format(it.get("size")),
                           key=fid)
            self._pathbar()
            self._log(f"[dim]已加载 {len(items)} 项（{' / '.join(n for _, n in self.stack)}）[/]")

        # ---------- 动作

        def _toggle_fid(self, fid: str) -> None:
            item = self._item_by_fid(fid)
            if item is None or is_folder(item):
                self.notify("目录不能勾选，只能勾选文件", severity="warning")
                return
            if fid in self.selected:
                del self.selected[fid]
            else:
                self.selected[fid] = item
            self.query_one("#files", DataTable).update_cell(fid, "sel",
                                                            "√" if fid in self.selected else "")
            self._pathbar()

        def action_toggle(self) -> None:
            fid = self._current_fid()
            if fid is None:
                return
            self._toggle_fid(fid)

        def _item_by_fid(self, fid: str) -> dict | None:
            # 优先用列表接口的原始数据（勾选/目录判断需要 file、obj_category 字段）
            item = self.items.get(fid)
            if item:
                return item
            dt = self.query_one("#files", DataTable)
            try:
                name = str(dt.get_row(fid)[2])
            except Exception:
                name = fid
            return {"fid": fid, "file_name": name, "size": 0}

        def action_open(self) -> None:
            fid = self._current_fid()
            if fid is None:
                return
            item = self._item_by_fid(fid)
            if item is None:
                return
            # 目录行判断：kind 列显示"目录"
            kind = str(self.query_one("#files", DataTable).get_row(fid)[1])
            if kind == "目录":
                self.stack.append((fid, str(self.query_one("#files", DataTable).get_row(fid)[2])))
                self.reload()
            else:
                self._gen_links([fid], f"生成 {item['file_name']} 的 BC 链接")

        def action_up(self) -> None:
            if len(self.stack) > 1:
                self.stack.pop()
                self.reload()

        def action_refresh(self) -> None:
            self.reload()

        def action_settings(self) -> None:
            self.push_screen(SettingsScreen(self.config), self._settings_saved)

        def action_qrlogin(self) -> None:
            self.push_screen(QrLoginScreen())

        def action_transfer(self) -> None:
            self.push_screen(TransferScreen())

        def _persist_cookie(self, cookie: str) -> None:
            """轮换 Cookie 的持久化回调：__puus 每次响应都在换，不落盘就死"""
            if cookie == self.config.get("cookie"):
                return
            self.config["cookie"] = cookie
            try:
                save_config(self.config, self.config_path or CONFIG_PATH)
            except RuntimeError:
                pass  # 落盘失败不打断下载，下次扫码可恢复

        def apply_qr_cookie(self, cookie: str) -> None:
            """扫码成功后的收尾：落盘 → 重建客户端 → 回根目录刷新"""
            self.config["cookie"] = cookie
            try:
                save_config(self.config, self.config_path or CONFIG_PATH)
            except RuntimeError as e:
                self.notify(str(e), severity="error")
            self.client = QuarkClient(cookie, on_cookie_update=self._persist_cookie)
            self.stack = [("0", "根目录")]
            self.selected.clear()
            self._log("[green]Cookie 已更新（扫码登录）[/]")
            self.reload()

        def _settings_saved(self, cfg) -> None:
            if cfg is None:
                return
            self.config = cfg
            try:
                save_config(cfg, self.config_path or CONFIG_PATH)
            except RuntimeError as e:
                self.notify(str(e), severity="error")
            self.client = (QuarkClient(cfg["cookie"], on_cookie_update=self._persist_cookie)
                           if cfg.get("cookie") else None)
            if self.client is None:
                self.notify("Cookie 为空，无法访问网盘", severity="warning")
                return
            self.stack = [("0", "根目录")]
            self.selected.clear()
            self.reload()

        def action_gen_bc(self) -> None:
            if not self._guard():
                return
            fids = list(self.selected.keys())
            if not fids:
                self.notify("先用空格勾选文件，再按 b 生成", severity="warning")
                return
            self._gen_links(fids, f"生成 {len(fids)} 个文件的 BC 链接")

        def _guard(self) -> bool:
            if self.client is None:
                self.notify("请先按 s 配置 Cookie", severity="warning")
                return False
            return True

        def action_wake(self) -> None:
            # 一步到位：勾选 → 取直链 → 直接唤起，不进结果页
            if not self._guard():
                return
            fids = list(self.selected.keys())
            if not fids:
                self.notify("先用空格勾选文件，再按 w 唤起下载", severity="warning")
                return
            self.run_worker(lambda: self._worker_links(fids, then="wake"),
                            thread=True, exclusive=True, group="links")
            self._log("[dim]获取直链中…（完成后自动唤起比特彗星）[/]")

        def _gen_links(self, fids: list, title: str) -> None:
            if not self._guard():
                return
            self.run_worker(lambda: self._worker_links(fids, title),
                            thread=True, exclusive=True, group="links")
            self._log(f"[dim]获取直链中…（{title}）[/]")

        # ---------- 直链 / 结果

        def _progress(self, done: int, total: int) -> None:
            self._log(f"[dim]已获取 {done}/{total} 个直链[/]")

        def _worker_links(self, fids: list, title: str = "BC 链接",
                          then: str = "screen") -> None:
            try:
                got = self.client.get_download_links(
                    fids, on_progress=lambda d, t: self.call_from_thread(self._progress, d, t))
            except Exception as e:
                self.call_from_thread(self.notify, f"获取直链失败：{e}", severity="error")
                self.call_from_thread(self._log, f"[red]获取直链失败[/] {e}")
                return
            links = [{
                "name": g["name"], "size": g["size"], "url": g["url"],
                "bc": bc_link(g["name"], g["url"], user_agent=UA,
                              cookie=self.client.cookie),
            } for g in got]
            if then == "wake":
                self.call_from_thread(self._do_wake, links)
            else:
                self.call_from_thread(self._show_results, links, title)

        def _do_wake(self, links: list) -> None:
            if not links:
                self.notify("没有拿到任何直链", severity="warning")
                return
            self.wake_bitcomet(links)

        def _show_results(self, links: list, title: str) -> None:
            if not links:
                self.notify("没有拿到任何直链", severity="warning")
                return
            self.push_screen(ResultScreen(links, title))

    return QuarkBCAppImpl


def probe_transfer(share_text: str) -> int:
    """转存接口探针：用本机 Cookie 把几种请求变体各打一次，看哪种能过 31001。

    为什么要这个：转存 save 的鉴权行为在「域名 / 反爬参数 / 浏览器头」三个维度上
    都随版本变化，没有真实 Cookie 无法离线验证。这里是唯一可靠的判定方式——
    拿你手上这份 Cookie 挨个试，成功的那一条就是答案。
    副作用：成功的那次会真的转存第 1 个条目（1 项），用完自己删掉即可。
    """
    cfg = load_config(CONFIG_PATH)
    cookie = (cfg.get("cookie") or "").strip()
    if not cookie:
        print("本机没有 Cookie：先运行程序，按 l 扫码登录一次")
        return 2
    pwd_id, passcode = parse_share_link(share_text)
    if not pwd_id:
        print(f"链接解析失败：{share_text}")
        return 2
    c = QuarkClient(cookie)
    keys = []
    for part in cookie.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            keys.append(f"{k.strip()}({len(v)})")
    print("Cookie 键：" + ", ".join(keys))
    # 先分清失败层次：读都不过 = Cookie 死了；读得过写不过 = 权限/请求形态问题
    try:
        n = len(c.list_dir("0"))
        print(f"读接口正常（根目录 {n} 项）")
    except Exception as e:
        print(f"读接口也失败：{e}")
        print("\n→ Cookie 本身无效（可能已轮换过期，或被测试假数据覆盖）。"
              "先运行程序按 l 重新扫码，再跑本探针。")
        return 1
    try:
        stoken = c.share_stoken(pwd_id, passcode)
        entries, title = c.share_list(pwd_id, stoken)
    except Exception as e:
        print(f"读取分享失败：{e}")
        return 1
    print(f"分享《{title or pwd_id}》共 {len(entries)} 项")
    if not entries:
        return 1
    first = entries[0]
    print(f"探针条目：{first.get('file_name')}")
    fid, ftok = first.get("fid") or "", first.get("share_fid_token") or ""
    payload = {
        "fid_list": [fid], "fid_token_list": [ftok], "to_pdir_fid": "0",
        "pwd_id": pwd_id, "stoken": stoken, "pdir_fid": "0", "scene": "link",
    }
    if passcode:
        payload["pwd_id_extra"] = passcode
    body = json.dumps(payload).encode()

    web_h = c._headers(True, True)          # 带 Origin/Referer/Accept
    bare_h = c._headers(True)               # 只有 UA/Cookie/CT
    ac = c._ac()
    variants = [
        ("drive.quark.cn + app + 反爬 + 浏览器头",
         f"https://drive.quark.cn/1/clouddrive/share/sharepage/save"
         f"?pr=ucpro&fr=pc&uc_param_str=&app=clouddrive{ac}", web_h),
        ("drive-pc.quark.cn + app + 反爬 + 浏览器头",
         f"https://drive-pc.quark.cn/1/clouddrive/share/sharepage/save"
         f"?pr=ucpro&fr=pc&uc_param_str=&app=clouddrive{ac}", web_h),
        ("drive.quark.cn + 反爬 + 浏览器头（不带 app）",
         f"https://drive.quark.cn/1/clouddrive/share/sharepage/save"
         f"?pr=ucpro&fr=pc&uc_param_str={ac}", web_h),
        ("drive-pc.quark.cn + 仅基础参数 + 最简头（旧版行为）",
         "https://drive-pc.quark.cn/1/clouddrive/share/sharepage/save"
         "?pr=ucpro&fr=pc&uc_param_str=", bare_h),
        ("drive.quark.cn + 仅基础参数 + 最简头",
         "https://drive.quark.cn/1/clouddrive/share/sharepage/save"
         "?pr=ucpro&fr=pc&uc_param_str=", bare_h),
    ]
    ok = False
    for name, url, headers in variants:
        try:
            data = c._check(c._req_json(url, data=body, headers=headers), "转存") or {}
            tid = data.get("task_id") or ""
            print(f"✅ {name}　→ task_id={tid}")
            ok = True
            break          # 第一个成功的就够定位问题，别把条目重复转存
        except Exception as e:
            print(f"❌ {name}　→ {e}")
        time.sleep(0.6)
    if not ok:
        print("\n所有变体都被判未登录。此时问题在 Cookie 本身，不在请求形态：")
        print("  1) 按 l 重新扫码（__puus 轮换，超过十几分钟会失效）")
        print("  2) 仍不行就说明这份 Cookie 缺写权限，需要在网页端登录后重新取 Cookie")
    return 0 if ok else 1


def main() -> None:
    # selftest / probe 只走 HTTP 层，不依赖 textual，先处理掉再要求装 TUI 依赖
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    if "--probe" in sys.argv:
        i = sys.argv.index("--probe")
        if i + 1 >= len(sys.argv):
            print("用法：python quarkbc.py --probe \"https://pan.quark.cn/s/xxxx?pwd=abcd\"")
            sys.exit(2)
        sys.exit(probe_transfer(sys.argv[i + 1]))
    _lazy_import_textual()
    app_cls = build_app()
    app_cls().run()


if __name__ == "__main__":
    main()
