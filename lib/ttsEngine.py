import hashlib
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


def emit_log(message):
    """推送一条日志给界面显示。任意线程都可以安全调用。"""
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
# READ_TIMEOUT 是「相邻两次收到数据之间」的最长间隔，不是整个请求的总时长上限。
# 取 2 秒的依据：连接复用时实测 20 次「等待响应首字节」，中位数 417 ms、最大 1501 ms，
# 约留有 33% 余量。超时后界面会明确报错，再按一次回车即可重试。
CONNECT_TIMEOUT = 2.0
READ_TIMEOUT = 2.0

# 全局复用的 HTTP 会话。
# 复用 TCP/TLS 连接，避免每次请求都重新握手（实测每次新建连接需 2-4 秒）。
_http = requests.Session()

# 创建一个全局的播放队列
_playback_queue = queue.Queue()

# 播放线程的控制事件
_stop_playback = threading.Event()

# 写缓存的互斥锁。
# 同一句话如果在第一次合成完成前又被提交一次，两个线程会写同一个文件名，
# 而「写临时文件 + os.replace」是两步，不加锁时第二步会撞上 [WinError 5] 拒绝访问
# （实测可出现）。单次写入只有 1 ms 左右，串行化的代价可以忽略。
_save_lock = threading.Lock()


def _playback_worker():
    """播放队列中的音频文件的工作线程。"""
    while not _stop_playback.is_set():
        try:
            # 从队列中获取下一个任务：
            # (文件路径, 入队时刻, 请求发出时刻, 音频就绪耗时, 该耗时的名称)
            # block=True, timeout=1.0 避免无限阻塞，允许检查 _stop_playback
            (mp3_file_path, queued_at, requested_at,
             ready_ms, ready_label) = _playback_queue.get(timeout=1.0)

            # 处理获取到的路径
            file_path = Path(mp3_file_path)
            if not file_path.exists() or not file_path.is_file():
                print(f"Playback Error: File not found or invalid: {file_path}")
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

                print(f"[TTS]   入队 → 出声   {(t_playing - queued_at) * 1000:.0f} ms"
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

                # 三项耗时先各自取整再相加5作为总数，这样「总数 = 各项之和」
                # 在界面上永远成立（与真实耗时的差在 1 ms 以内）
                decode_ms = round((t_loaded - t_load) * 1000)
                queue_ms = round((t_playing - queued_at) * 1000) - decode_ms
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


def _post_ssml(config, headers, ssml):
    """发送 SSML 请求，只在「连接类失败」时重试一次。

    连接池里可能残留一条已被服务端关闭的连接，此时请求根本没送达，
    换一条新连接重发是安全的，而且几乎必然成功。

    读超时 / 连接超时都**不重试**：
      - 它们说明服务端慢，重试只会让等待时间翻倍；
      - 请求可能已经送达并被合成，重发等于重复消耗额度。
    """
    last_error = None
    for attempt in (1, 2):
        try:
            response = _http.post(
                url=config.FULL_API_URL,
                headers=headers,
                data=ssml.encode('utf-8'),  # 确保 SSML 字符串以 UTF-8 编码发送
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                stream=True,
            )
            if attempt == 2:
                print("Retried after a connection error and succeeded.")
                emit_log("[重试] 连接失败，重发成功")
            return response
        except requests.exceptions.ConnectTimeout as e:
            # 握手都没完成，属于「慢」而不是「连接失效」，重试不划算
            print(f"Connect timeout: {e}")
            emit_log("[失败] 连接超时")
            return None
        except requests.exceptions.ConnectionError as e:
            last_error = e
            print(f"Connection error (attempt {attempt}/2): {e}")
            continue
        except requests.exceptions.RequestException as e:
            print(f"Request failed: {e}")
            emit_log(f"[失败] 网络异常（{type(e).__name__}）")
            return None

    print(f"Connection failed twice, giving up: {last_error}")
    emit_log("[失败] 无法连接服务器")
    return None


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

    # 3. 发送 POST 请求（复用 _http 的持久连接，省去 TCP/TLS 握手）
    try:
        # stream=True：先只拿到响应头，便于把「等待服务端」与「接收数据」分开计时
        t_start = time.perf_counter()
        response = _post_ssml(config, headers, ssml)
        t_header = time.perf_counter()
        if response is None:
            return None

        # 4. 检查响应状态
        if response.status_code != 200:
            print(f"TTS API Error: {response.status_code} - {response.text}")
            emit_log(f"[失败] 服务端返回 {response.status_code}")
            return None

        # 5. 读取响应体
        #    必须读完整，否则连接不会被归还到连接池，下一句又要重新握手
        audio_data = response.content
        t_body = time.perf_counter()

        print(f"[TTS]   请求 → 响应头   {(t_header - t_start) * 1000:.0f} ms")
        print(f"[TTS]   接收数据        {(t_body - t_header) * 1000:.0f} ms")
        return audio_data

    except requests.exceptions.RequestException as e:
        # 走到这里说明是「读取正文」阶段失败的（发送阶段的失败已在 _post_ssml 里处理）
        print(f"Failed while reading response body: {e}")
        # 界面只报异常类型名，完整信息留给控制台，避免把日志行撑爆
        emit_log(f"[失败] 网络异常（{type(e).__name__}）")
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


def play_in_background_queued(mp3_path, requested_at=None, ready_ms=None, ready_label='合成'):
    """将播放请求添加到队列中。

    requested_at: 用户触发本次请求（按回车 / 按热键）的时刻，
                  用于统计「按键 → 出声」的端到端延迟。
    ready_ms:     从 requested_at 到音频就绪（已落盘 / 已命中缓存）的耗时。
    ready_label:  上述耗时在界面上的名称：「合成」或「命中」。
    """
    _playback_queue.put((mp3_path, time.perf_counter(), requested_at, ready_ms, ready_label))
    print(f"Queued for playback: {mp3_path}")


def cleanup_pygame():
    """清理 pygame 资源。"""
    if pygame.mixer.get_init():
        pygame.mixer.quit()


def text_to_speech(text, config):
    # 记录用户触发的时刻，用于统计「按键 → 出声」的端到端延迟
    requested_at = time.perf_counter()

    def target(text, config):
        text = search_fixed_collocation(text, config)
        text = search_word_replacement(text, config)
        hashed_text = hashlib.md5(
            f'{config.VOICE}{config.VOICE_STYLE}{config.SPEED}{config.PITCH}{text}'.encode('utf-8')).hexdigest()
        cache_dir = Path(config.STORED_FILEPATH)
        cache_file_path = cache_dir / f"{hashed_text}.mp3"

        # 如果缓存文件存在，将其加入播放队列
        if cache_file_path.exists():
            print("[TTS] 缓存命中")
            emit_log("[缓存] 命中，跳过合成")
            play_in_background_queued(cache_file_path, requested_at,
                                      (time.perf_counter() - requested_at) * 1000, '命中')
            return

        # 缓存不存在，生成音频
        print("[TTS] 缓存未命中，开始合成")
        emit_log(f"[合成] 开始：「{_brief(text)}」")
        audio_data = text_to_speech_web_api(text, config)
        if audio_data is None:
            print("[TTS] 合成失败：本次没有拿到音频")
            # 失败可能让复用的连接失效，后台补一条，免得下一句又花时间重新握手
            _rewarm_in_background(config)
            return

        t_save = time.perf_counter()
        saved = save_audio_to_file(audio_data, config, hashed_text)
        print(f"[TTS]   落盘            {(time.perf_counter() - t_save) * 1000:.0f} ms")
        if not saved:
            print("[TTS] 写盘失败：本次无音频可播放")
            return

        # 「合成」耗时从用户按键算起，到音频落盘完成为止。
        # 这样它加上「排队」「解码」正好等于端到端总耗时（见 _playback_worker）
        ready_ms = (time.perf_counter() - requested_at) * 1000
        emit_log(f"[合成] 完成  {ready_ms:.0f} ms")

        # 只有确实拿到音频文件才加入播放队列
        play_in_background_queued(cache_file_path, requested_at, ready_ms, '合成')

    thread = threading.Thread(target=target, args=(text, config), daemon=True)
    thread.start()


# --- 清理函数 ---
def cleanup_tts_engine():
    """清理 TTS 引擎资源。"""
    _stop_playback.set()  # 通知播放线程停止
    # 等播放线程自己退出后再关 mixer，否则它会访问已关闭的 mixer 而报错
    # （线程空闲时最多等 queue.get 的 1 秒超时，2 秒足够）
    _playback_thread.join(timeout=2.0)
    cleanup_pygame()  # 清理 pygame
