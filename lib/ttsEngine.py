import hashlib
import itertools
import json
import os
import re
import sys
import tempfile
from xml.sax.saxutils import escape as xml_escape

import requests
import pygame
import time
import threading
import queue
from pathlib import Path


# --- 程序根目录 ---
# 打包成单文件 exe 后，__file__ 指向临时解包目录（%TEMP%\_MEIxxxxxx），
# 不能用它来定位配置与缓存。因此：
#   冻结运行时 -> 以 app.exe 所在目录为根
#   源码运行时 -> 以项目根目录（lib 的上一级）为根
# 配置与缓存必须和程序放在一起（README 要求不可移动文件位置），
# 不能落在临时目录，否则缓存会随程序退出一起丢失。
APP_ROOT = (Path(sys.executable).resolve().parent
            if getattr(sys, 'frozen', False)
            else Path(__file__).resolve().parent.parent)


def resolve_path(path):
    """将相对路径解析为基于 APP_ROOT 的绝对路径；传入绝对路径时原样返回。"""
    path = Path(path)
    return path if path.is_absolute() else APP_ROOT / path


# --- 界面日志 ---
# 合成跑在工作线程里，而 Tkinter 只能在主线程操作，直接写控件会崩。
# 所以工作线程只往队列里放文本，由界面主线程定时取出写进文本框。
_log_queue = queue.Queue()

# 当前线程正在处理哪一条请求。
# 合成线程与播放线程各自设置，设置后该线程发出的日志会自动带上 #N，
# 这样即使两条请求的日志交错在一起，也能一眼看出哪行属于谁。
_log_context = threading.local()


def set_request_index(index):
    """把本线程后续发出的日志标记为属于某一条请求。"""
    _log_context.index = index


def emit_log(message, with_index=True):
    """推送一条日志给界面显示。任意线程都可以安全调用。

    with_index=False 用于分隔标题本身，否则会变成「#1 ---- 第 1 条 ----」。
    """
    index = getattr(_log_context, 'index', None)
    if with_index and index is not None:
        # 编号插在最前面，但保留原有缩进，免得子项（延迟分解）失去对齐
        content = message.lstrip(' ')
        indent = message[:len(message) - len(content)]
        message = f"#{index}{indent} {content}"
    _log_queue.put(message)


def poll_logs(max_items=100):
    """取出待显示的日志（最多 max_items 条）。仅供界面主线程调用。"""
    messages = []
    for _ in range(max_items):
        try:
            messages.append(_log_queue.get_nowait())
        except queue.Empty:
            break
    return messages


def _brief(text, limit=20):
    """缩短文本用于日志显示，避免长句把日志行撑爆。"""
    text = text.replace('\n', ' ')
    return text if len(text) <= limit else text[:limit] + '…'


# --- 配置 ---
class Config:
    def __init__(self, *config_path):
        self.WORD_REPLACEMENT = None
        self.FIXED_COLLOCATION = None
        self.API_ENDPOINT = None
        self.BASE_URL = None
        self.FULL_API_URL = None
        self.load(*config_path)

    def load(self, *config_path):
        # 配置文件路径统一基于 APP_ROOT 解析，不再依赖「当前工作目录」
        sound_model_path = resolve_path(config_path[0])
        fixed_collocation_path = resolve_path(config_path[1])
        word_replacement_path = resolve_path(config_path[2])

        with open(sound_model_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
            # 直接将字典的键值对作为实例属性
            self.__dict__.update(data)
        # 计算派生属性
        self.FULL_API_URL = self.BASE_URL + self.API_ENDPOINT
        # 缓存目录同样基于 APP_ROOT 解析成绝对路径
        self.STORED_FILEPATH = str(resolve_path(self.STORED_FILEPATH))

        with open(fixed_collocation_path, 'r', encoding='utf-8') as f:
            self.FIXED_COLLOCATION = json.loads(f.read())

        with open(word_replacement_path, 'r', encoding='utf-8') as f:
            self.WORD_REPLACEMENT = json.loads(f.read())


def search_fixed_collocation(text, config):
    for collocation in config.FIXED_COLLOCATION:
        if collocation["before"] == text:
            return collocation["after"]
    return text


def re_search_word_replacement(text, replacement):
    return re.sub(replacement["before"], replacement["after"], text)


def search_word_replacement(text, config):
    for replacement in config.WORD_REPLACEMENT:
        if replacement["re"]:
            text = re_search_word_replacement(text, replacement)
        else:
            text = text.replace(replacement["before"], replacement["after"])
    return text


# --- 配置结束 ---

# --- 请求超时（秒）---
# 实测依据（2026-09-19，50 次真实合成，热连接，全部成功）：
#   等待首字节   p50 382 / p95 887  / max 1206 ms
#   接收正文     p50  22 / p95 217  / max  219 ms
#   单次尝试合计 p50 406 / p95 1105 / max 1425 ms
# 所以单次尝试给 3 秒约有 1 倍余量（50 次里触发 0 次），不会误杀正常请求。
# 之前取的 2 秒反而过紧：实测有一次 2003 ms 本可一次成功，却被 2 秒掐断后重发，
# 该请求总耗时反而涨到 3202 ms。
CONNECT_TIMEOUT = 2.0   # 建立连接（TCP/TLS 握手）最多等多久
ATTEMPT_TIMEOUT = 3.0   # 单次尝试的累计上限（从发出请求算起）；超过即中止并重发
REQUEST_BUDGET = 5.0    # 整个请求（含重发）的总预算，硬上限
ATTEMPT_LIMIT = 2       # 最多尝试几次

# 全局复用的 HTTP 会话。
# 复用 TCP/TLS 连接，避免每次请求都重新握手（实测每次新建连接需 2-4 秒）。
_http = requests.Session()

# 创建一个全局的播放队列。
# 队列里排的是「输入顺序」的占位，而不是「合成完成顺序」的成品 —— 见 _PendingSpeech。
_playback_queue = queue.Queue()


class _PendingSpeech:
    """一条已提交、但音频可能还没就绪的播放任务。

    为什么要在「提交的那一刻」就入队，而不是等合成完成才入队：
    合成是在各自的线程里并行跑的，谁先跑完谁先入队，于是短句会插到长句前面，
    播放顺序就和输入顺序不一致了（用户实测：先输 1234567 再输 1，结果是 1 先播）。
    现在队列里排的是「输入顺序」，音频就绪后再回填内容。
    """

    __slots__ = ('index', 'requested_at', 'ready', 'ready_at',
                 'path', 'ready_ms', 'ready_label', 'error')

    def __init__(self, index, requested_at):
        self.index = index
        self.requested_at = requested_at
        self.ready = threading.Event()  # 音频就绪（或确定没有音频）时被 set
        self.ready_at = None            # 音频就绪的时刻
        self.path = None                # 要播放的文件；None 表示这条没有音频
        self.ready_ms = None            # requested_at → ready_at 的耗时
        self.ready_label = '合成'
        self.error = None               # 没有音频的原因（只写控制台）

# 播放线程的控制事件
_stop_playback = threading.Event()

# 写缓存的互斥锁。
# 同一句话如果在第一次合成完成前又被提交一次，两个线程会写同一个文件名，
# 而「写临时文件 + os.replace」是两步，不加锁时第二步会撞上 [WinError 5] 拒绝访问
# （实测可出现）。单次写入只有 1 ms 左右，串行化的代价可以忽略。
_save_lock = threading.Lock()

# 请求序号。每条请求的日志都以一行分隔标题开头，方便在界面上区分彼此。
_request_seq = itertools.count(1)


def _playback_worker():
    """播放队列中的音频文件的工作线程。"""
    while not _stop_playback.is_set():
        try:
            # 从队列中获取下一个任务。block=True, timeout=1.0 避免无限阻塞，
            # 以便周期性检查 _stop_playback
            pending = _playback_queue.get(timeout=1.0)
            # 之后本线程发出的日志（播放开始/结束、延迟）都会带上这条请求的编号
            set_request_index(pending.index)

            # 队列里排的是「输入顺序」，但音频可能还没合成好，这里要等它就绪。
            # 用 0.2 秒轮询而不是无限等待，便于及时响应退出；
            # Event.wait 一旦被 set 会立刻返回，所以不会引入额外延迟。
            while not pending.ready.is_set():
                if _stop_playback.is_set():
                    break
                pending.ready.wait(0.2)

            if not pending.ready.is_set():
                print(f"Abandoned pending speech #{pending.index} (shutting down).")
                _playback_queue.task_done()
                continue

            # 没有音频（合成失败 / 写盘失败）就跳过这一条，继续处理下一条。
            # 失败原因已经在合成阶段报过了，这里不再重复刷界面。
            if pending.path is None:
                print(f"Request #{pending.index} has no audio to play: {pending.error}")
                _playback_queue.task_done()
                continue

            mp3_file_path = pending.path
            requested_at = pending.requested_at
            ready_ms = pending.ready_ms
            ready_label = pending.ready_label

            # 处理获取到的路径
            file_path = Path(mp3_file_path)
            if not file_path.exists() or not file_path.is_file():
                print(f"Playback Error: File not found or invalid: {file_path}")
                emit_log("[失败] 音频文件不存在")
                _playback_queue.task_done()  # 标记此任务完成
                continue

            # 初始化 mixer (如果需要)
            if not pygame.mixer.get_init():
                try:
                    pygame.mixer.init(frequency=22050, size=-16, channels=2, buffer=512)
                    print("Pygame mixer initialized by worker.")
                except pygame.error as e:
                    print(f"Failed to initialize pygame mixer: {e}")
                    _playback_queue.task_done()
                    continue

            try:
                print(f"Playing: {file_path}")
                t_load = time.perf_counter()
                pygame.mixer.music.load(file_path)
                t_loaded = time.perf_counter()
                pygame.mixer.music.play()
                t_playing = time.perf_counter()

                print(f"[TTS]   就绪 → 出声   {(t_playing - pending.ready_at) * 1000:.0f} ms"
                      f"（解码 {(t_loaded - t_load) * 1000:.0f} ms）")
                if requested_at is not None:
                    print(f"[TTS]   按键 → 出声   {(t_playing - requested_at) * 1000:.0f} ms")

                emit_log("[播放] 开始")

                # 等待播放完成
                while pygame.mixer.music.get_busy() and not _stop_playback.is_set():
                    time.sleep(0.1)

                # 如果是因为停止信号中断的，可能需要停止音乐
                if _stop_playback.is_set():
                    pygame.mixer.music.stop()
                    print("Playback stopped by shutdown signal.")

                emit_log("[播放] 结束")

                # 三项耗时先各自取整再相加作为总数，这样「总数 = 各项之和」
                # 在界面上永远成立（与真实耗时的差在 1 ms 以内）
                # 「排队」从音频就绪那一刻算起，因此它真实反映「在等前一句播完」的时间
                decode_ms = round((t_loaded - t_load) * 1000)
                queue_ms = round((t_playing - pending.ready_at) * 1000) - decode_ms
                if ready_ms is not None:
                    ready_ms = round(ready_ms)
                    emit_log(f"[延迟] 按键 → 出声  {ready_ms + queue_ms + decode_ms} ms")
                    emit_log(f"        （{ready_label} {ready_ms} ｜ 排队 {queue_ms} ｜ 解码 {decode_ms}）")
                else:
                    emit_log(f"[延迟] 入队 → 出声  {queue_ms + decode_ms} ms")

                print(f"Finished playing: {file_path}")

            except pygame.error as e:
                print(f"Pygame error playing {file_path}: {e}")
            except Exception as e:
                print(f"Unexpected error in playback worker: {e}")

            finally:
                # 标记队列中的这个任务已完成
                _playback_queue.task_done()

        except queue.Empty:
            # 队列为空，继续循环（等待新任务）
            continue
        except Exception as e:
            print(f"Unexpected error in playback worker loop: {e}")
            # 即使出错，也尽量继续循环
            time.sleep(0.1)


# 启动播放工作线程
_playback_thread = threading.Thread(target=_playback_worker, daemon=True)
_playback_thread.start()


def warm_up(config, tag='启动'):
    """提前建立与 TTS 服务的 TCP/TLS 连接，供后续合成请求复用。

    只请求站点根路径（一个静态页面），不消耗任何语音合成配额。
    失败会被吞掉 —— 预热失败只会损失部分优化收益，不影响正常功能。

    tag: 日志前缀。启动时是「启动」，请求失败后重连时是「重连」。
    """
    try:
        # 默认 stream=False，会完整读取响应并把连接归还到连接池
        _http.get(config.BASE_URL, timeout=10)
        print("TTS connection warmed up.")
        emit_log(f"[{tag}] TTS 连接已预热")
    except Exception as e:
        print(f"Connection warm-up skipped: {e}")
        emit_log(f"[{tag}] 连接预热失败，不影响使用")


def _rewarm_in_background(config):
    """请求失败后在后台重新预热连接。

    超时或连接中断可能让连接池里那条复用的连接失效，这里提前补一条，
    免得下一句又要付一次 TCP/TLS 握手成本。
    这个动作不依赖「连接到底丢没丢」的判断，无论丢没丢都是安全的。
    """
    threading.Thread(target=warm_up, args=(config, '重连'), daemon=True).start()


def text_to_speech_web_api(text, config):
    """
    模拟网页行为，通过POST请求调用TTS API生成语音。

    Args:
        text (str): 要合成的文本。
        config: 配置文件,包含以下内容：
            BASE_URL: API网址基址
            API_ENDPOINT: API位置
            API_KEY: 字面意思
            OUTPUT_FORMAT: "audio-24khz-48kbitrate-mono-mp3", 不建议更改
            VOICE: 微软的讲述人模型
            VOICE_STYLE: 讲述人的情感配置
            SPEED: 百分比语速
            PITCH: 百分比语调

    Returns:
        bytes: 返回的音频数据 (MP3 bytes)，如果成功。
               返回 None 如果请求失败。
               :param text:
               :param config:
    """
    # 1. 构造 SSML
    # 根据 voice_style 决定是否添加 <mstts:express-as> 标签
    vs_start = f'<mstts:express-as style="{config.VOICE_STYLE}">' if config.VOICE_STYLE.lower() != 'general' else ''
    vs_end = '</mstts:express-as>' if config.VOICE_STYLE.lower() != 'general' else ''

    # 文本必须先做 XML 转义：一旦出现 < 或 &，整段 SSML 就变成非法 XML，
    # 服务端会直接返回 400（实测可复现），这句话就永远发不出声音。
    text_escaped = xml_escape(text)

    ssml = f'''<speak xmlns="http://www.w3.org/2001/10/synthesis" 
                  xmlns:mstts="http://www.w3.org/2001/mstts" 
                  xmlns:emo="http://www.w3.org/2009/10/emotionml" 
                  version="1.0" 
                  xml:lang="zh-CN">
                <voice name="{config.VOICE}">
                  {vs_start}
                  <prosody rate="{config.SPEED}%" pitch="{config.PITCH}%">{text_escaped}</prosody>
                  {vs_end}
                </voice>
              </speak>'''

    # 2. 准备 Headers
    headers = {
        'Output-Format': config.OUTPUT_FORMAT,
        'Content-Type': 'application/ssml+xml',
        'FFCafe-Access-Token': config.API_KEY,  # 使用您的 API Key
        'Voice-Variant': config.VOICE.lower(),  # 语音变体，小写
    }

    # 3. 发送请求并读完整正文（复用 _http 的持久连接，省去 TCP/TLS 握手）
    #
    # 两层时间约束，缺一不可：
    #   ATTEMPT_TIMEOUT —— 单次尝试：从发出请求累计超过 3 秒就中止并重发
    #   REQUEST_BUDGET  —— 整个请求（含重发）总预算 5 秒，超了直接放弃
    # 为什么光靠读超时不够：读超时管的是「相邻两次收数据之间的间隔」。
    # 若服务端每隔 1.9 秒吐一点点数据，每次间隔都没超时，但永远也收不完。
    # 那种情况只有「累计计时」能兜住，所以正文必须逐块读（见下）。
    #
    # 重发策略：
    #   ReadTimeout / ConnectionError / 超过 ATTEMPT_TIMEOUT -> 重发
    #   ConnectTimeout                                      -> 重发（可能只是网络抖动）
    #   非 200                                              -> 不重发（请求本身有问题）
    # 重发不会造成「播放两遍」：只有完整拿到音频字节才会交给播放队列，
    # 被放弃的那次响应对象直接丢弃，永远进不了队列。
    request_started = time.perf_counter()
    budget_deadline = request_started + REQUEST_BUDGET
    last_failure = None

    for attempt in range(1, ATTEMPT_LIMIT + 1):
        # 总预算已经用完就不再重发
        if time.perf_counter() >= budget_deadline:
            last_failure = f'请求超时（超过 {REQUEST_BUDGET:.0f} 秒）'
            break
        try:
            t_start = time.perf_counter()
            # 单次尝试的上限，同时不能突破总预算
            remaining = budget_deadline - t_start
            attempt_deadline = min(t_start + ATTEMPT_TIMEOUT, budget_deadline)
            response = _http.post(
                url=config.FULL_API_URL,
                headers=headers,
                data=ssml.encode('utf-8'),  # 确保 SSML 字符串以 UTF-8 编码发送
                timeout=(min(CONNECT_TIMEOUT, remaining),
                         max(0.1, min(ATTEMPT_TIMEOUT, remaining))),
                stream=True,
            )
            t_header = time.perf_counter()

            # 4. 检查响应状态
            if response.status_code != 200:
                print(f"TTS API Error: {response.status_code} - {response.text}")
                emit_log(f"[失败] 服务端返回 {response.status_code}")
                return None

            # 5. 逐块读取正文。
            #    不能用 response.content：它是一次性阻塞读取，中途无法干预，
            #    遇到「慢慢吐数据」的服务端会一直读下去。逐块读才能在每块之间
            #    检查累计耗时。
            #    必须读完整，否则连接不会被归还到连接池，下一句又要重新握手。
            chunks = []
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    chunks.append(chunk)
                now = time.perf_counter()
                if now >= attempt_deadline:
                    raise requests.exceptions.ReadTimeout(
                        f'body not finished within {ATTEMPT_TIMEOUT:.0f}s')
                if now >= budget_deadline:
                    raise requests.exceptions.ReadTimeout(
                        f'body not finished within the {REQUEST_BUDGET:.0f}s budget')
            audio_data = b''.join(chunks)
            t_body = time.perf_counter()

            print(f"[TTS]   请求 → 响应头   {(t_header - t_start) * 1000:.0f} ms")
            print(f"[TTS]   接收数据        {(t_body - t_header) * 1000:.0f} ms")
            if attempt > 1:
                print("Succeeded on the automatic retry.")
                emit_log("[重试] 自动重发成功")
            return audio_data

        except requests.exceptions.ConnectTimeout as e:
            print(f"Connect timeout (attempt {attempt}/{ATTEMPT_LIMIT}): {e}")
            last_failure = '连接超时'
            if attempt >= ATTEMPT_LIMIT:
                break
            emit_log("[重试] 连接超时，自动重发一次")
        except requests.exceptions.ReadTimeout as e:
            print(f"Read timeout (attempt {attempt}/{ATTEMPT_LIMIT}): {e}")
            last_failure = '请求超时'
            if attempt >= ATTEMPT_LIMIT:
                break
            emit_log("[重试] 请求超时，自动重发一次")
        except requests.exceptions.ConnectionError as e:
            print(f"Connection error (attempt {attempt}/{ATTEMPT_LIMIT}): {e}")
            last_failure = '无法连接服务器'
            if attempt >= ATTEMPT_LIMIT:
                break
            emit_log("[重试] 连接失败，自动重发一次")
        except requests.exceptions.RequestException as e:
            print(f"Request failed: {e}")
            # 界面只报异常类型名，完整信息留给控制台，避免把日志行撑爆
            emit_log(f"[失败] 网络异常（{type(e).__name__}）")
            return None

    print(f"Gave up after at most {ATTEMPT_LIMIT} attempts ({last_failure}).")
    emit_log(f"[失败] {last_failure}")
    return None


def save_audio_to_file(audio_bytes, config, filename):
    """将音频字节数据保存为文件。

    Returns:
        bool: 写盘成功返回 True，否则 False。
    """
    if not audio_bytes:
        print("No audio data to save.")
        return False

    temp_path = None
    try:
        cache_dir = Path(config.STORED_FILEPATH)
        # 缓存目录可能被手动清空或删除，写盘前先补建，避免直接写入失败
        cache_dir.mkdir(parents=True, exist_ok=True)

        with _save_lock:
            # 先写临时文件再原子替换。
            # 同一句话如果在第一次合成完成前又被提交一次，两个线程会写同一个文件名，
            # 直接写目标文件可能让两次写入交错，生成损坏的 mp3。
            # 临时文件名由 mkstemp 保证唯一，os.replace 又是原子操作，
            # 所以最终文件必定是「某一次完整写入」的结果。
            fd, temp_path = tempfile.mkstemp(dir=cache_dir, suffix='.part')
            with os.fdopen(fd, 'wb') as f:
                f.write(audio_bytes)
            os.replace(temp_path, cache_dir / f'{filename}.mp3')
            temp_path = None

        print(f"Audio saved to {filename}")
        return True
    except OSError as e:
        # 磁盘满 / 无写入权限等：只让本次失败，不能让异常冒泡把请求线程带走
        print(f"缓存写入失败：{e}")
        emit_log("[失败] 缓存写入失败")
        return False
    finally:
        # 替换成功时 temp_path 已置空；中途失败则清掉残留的临时文件
        if temp_path is not None:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def _fill_pending(pending, path, label):
    """音频就绪：记录就绪时刻并回填播放内容。

    「放行」（pending.ready.set()）统一由 text_to_speech.target 的 finally 做，
    这样无论成功还是失败，播放线程都不会被卡住。
    """
    now = time.perf_counter()
    pending.ready_at = now
    pending.ready_ms = (now - pending.requested_at) * 1000
    pending.path = path
    pending.ready_label = label


def cleanup_pygame():
    """清理 pygame 资源。"""
    if pygame.mixer.get_init():
        pygame.mixer.quit()


def text_to_speech(text, config):
    # 记录用户触发的时刻，用于统计「按键 → 出声」的端到端延迟
    requested_at = time.perf_counter()

    # 关键：在「提交的这一刻」就分配编号并占住播放队列里的位置。
    # 合成是并行跑的，如果等合成完成才入队，短句会插到长句前面，
    # 播放顺序就和输入顺序不一致了。
    pending = _PendingSpeech(next(_request_seq), requested_at)
    _playback_queue.put(pending)

    def target(text, config):
        try:
            text = search_fixed_collocation(text, config)
            text = search_word_replacement(text, config)
            hashed_text = hashlib.md5(
                f'{config.VOICE}{config.VOICE_STYLE}{config.SPEED}{config.PITCH}{text}'.encode('utf-8')).hexdigest()
            cache_dir = Path(config.STORED_FILEPATH)
            cache_file_path = cache_dir / f"{hashed_text}.mp3"

            # 分隔标题：标题本身不带 #N，否则会变成「#1 ---- 第 1 条 ----」。
            emit_log(f"---- 第 {pending.index} 条：「{_brief(text)}」 ----", with_index=False)
            # 之后本线程发出的日志（含 text_to_speech_web_api、save_audio_to_file）
            # 都会自动带上 #N
            set_request_index(pending.index)

            # 缓存命中：直接回填占位，交给播放线程
            if cache_file_path.exists():
                print("[TTS] 缓存命中")
                emit_log("[缓存] 命中，跳过合成")
                _fill_pending(pending, cache_file_path, '命中')
                return

            # 缓存未命中，生成音频
            print("[TTS] 缓存未命中，开始合成")
            emit_log("[合成] 开始")
            audio_data = text_to_speech_web_api(text, config)
            if audio_data is None:
                print("[TTS] 合成失败：本次没有拿到音频")
                # 失败可能让复用的连接失效，后台补一条，免得下一句又花时间重新握手
                _rewarm_in_background(config)
                pending.error = '合成失败'
                return

            t_save = time.perf_counter()
            saved = save_audio_to_file(audio_data, config, hashed_text)
            print(f"[TTS]   落盘            {(time.perf_counter() - t_save) * 1000:.0f} ms")
            if not saved:
                print("[TTS] 写盘失败：本次无音频可播放")
                pending.error = '写盘失败'
                return

            # 「合成」耗时从用户按键算起，到音频落盘完成为止。
            # 这样它加上「排队」「解码」正好等于端到端总耗时（见 _playback_worker）
            _fill_pending(pending, cache_file_path, '合成')
            emit_log(f"[合成] 完成  {pending.ready_ms:.0f} ms")

        except Exception as e:
            print(f"Unexpected error while preparing speech #{pending.index}: {e}")
            emit_log("[失败] 准备音频时出错")
            pending.error = '内部错误'
        finally:
            # 关键：无论如何都必须放行。占位若不被放行，播放线程会永久卡在这一条上，
            # 之后所有句子都再也发不出声音（而且不会有任何提示）
            pending.ready.set()

    threading.Thread(target=target, args=(text, config), daemon=True).start()


# --- 清理函数 ---
def cleanup_tts_engine():
    """清理 TTS 引擎资源。"""
    _stop_playback.set()  # 通知播放线程停止
    # 等播放线程自己退出后再关 mixer，否则它会访问已关闭的 mixer 而报错
    # （线程空闲时最多等 queue.get 的 1 秒超时，2 秒足够）
    _playback_thread.join(timeout=2.0)
    cleanup_pygame()  # 清理 pygame
