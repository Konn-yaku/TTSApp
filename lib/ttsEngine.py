import hashlib
import json
import re
import sys
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

# 全局复用的 HTTP 会话。
# 复用 TCP/TLS 连接，避免每次请求都重新握手（实测每次新建连接需 2-4 秒）。
_http = requests.Session()

# 创建一个全局的播放队列
_playback_queue = queue.Queue()

# 播放线程的控制事件
_stop_playback = threading.Event()


def _playback_worker():
    """播放队列中的音频文件的工作线程。"""
    while not _stop_playback.is_set():
        try:
            # 从队列中获取下一个任务：(文件路径, 入队时刻, 请求发出时刻)
            # block=True, timeout=1.0 避免无限阻塞，允许检查 _stop_playback
            mp3_file_path, queued_at, requested_at = _playback_queue.get(timeout=1.0)

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

                # 等待播放完成
                while pygame.mixer.music.get_busy() and not _stop_playback.is_set():
                    time.sleep(0.1)

                # 如果是因为停止信号中断的，可能需要停止音乐
                if _stop_playback.is_set():
                    pygame.mixer.music.stop()
                    print("Playback stopped by shutdown signal.")

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


def warm_up(config):
    """提前建立与 TTS 服务的 TCP/TLS 连接，供后续合成请求复用。

    只请求站点根路径（一个静态页面），不消耗任何语音合成配额。
    失败会被静默吞掉 —— 预热失败只会损失部分优化收益，不影响正常功能。
    """
    try:
        # 默认 stream=False，会完整读取响应并把连接归还到连接池
        _http.get(config.BASE_URL, timeout=10)
        print("TTS connection warmed up.")
    except Exception as e:
        print(f"Connection warm-up skipped: {e}")


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
        response = _http.post(
            url=config.FULL_API_URL,
            headers=headers,
            data=ssml.encode('utf-8'),  # 确保 SSML 字符串以 UTF-8 编码发送
            timeout=30,  # 设置超时，避免请求挂起
            stream=True,
        )
        t_header = time.perf_counter()

        # 4. 检查响应状态
        if response.status_code != 200:
            print(f"TTS API Error: {response.status_code} - {response.text}")
            return None

        # 5. 读取响应体
        #    必须读完整，否则连接不会被归还到连接池，下一句又要重新握手
        audio_data = response.content
        t_body = time.perf_counter()

        print(f"[TTS]   请求 → 响应头   {(t_header - t_start) * 1000:.0f} ms")
        print(f"[TTS]   接收数据        {(t_body - t_header) * 1000:.0f} ms")
        return audio_data

    except requests.exceptions.RequestException as e:
        print(f"Request failed: {e}")
        return None


def save_audio_to_file(audio_bytes, config, filename):
    """将音频字节数据保存为文件。

    Returns:
        bool: 写盘成功返回 True，否则 False。
    """
    if not audio_bytes:
        print("No audio data to save.")
        return False

    try:
        cache_dir = Path(config.STORED_FILEPATH)
        # 缓存目录可能被手动清空或删除，写盘前先补建，避免直接写入失败
        cache_dir.mkdir(parents=True, exist_ok=True)
        with open(cache_dir / f'{filename}.mp3', mode='wb') as f:
            f.write(audio_bytes)
        print(f"Audio saved to {filename}")
        return True
    except OSError as e:
        # 磁盘满 / 无写入权限等：只让本次失败，不能让异常冒泡把请求线程带走
        print(f"缓存写入失败：{e}")
        return False


def play_in_background_queued(mp3_path, requested_at=None):
    """将播放请求添加到队列中。

    requested_at: 用户触发本次请求（按回车 / 按热键）的时刻，
                  用于统计「按键 → 出声」的端到端延迟。
    """
    _playback_queue.put((mp3_path, time.perf_counter(), requested_at))
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
            play_in_background_queued(cache_file_path, requested_at)
            return

        # 缓存不存在，生成音频
        print("[TTS] 缓存未命中，开始合成")
        audio_data = text_to_speech_web_api(text, config)
        if audio_data is None:
            print("[TTS] 合成失败：本次没有拿到音频")
            return

        t_save = time.perf_counter()
        saved = save_audio_to_file(audio_data, config, hashed_text)
        print(f"[TTS]   落盘            {(time.perf_counter() - t_save) * 1000:.0f} ms")
        if not saved:
            print("[TTS] 写盘失败：本次无音频可播放")
            return

        # 只有确实拿到音频文件才加入播放队列
        play_in_background_queued(cache_file_path, requested_at)

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
