import hashlib
import json
import re

import requests
import pygame
import time
import threading
import queue
from pathlib import Path


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
        with open(config_path[0], 'r', encoding='utf-8') as f:
            data = json.load(f)
            # 直接将字典的键值对作为实例属性
            self.__dict__.update(data)
        # 计算派生属性
        self.FULL_API_URL = self.BASE_URL + self.API_ENDPOINT

        with open(config_path[1], 'r', encoding='utf-8') as f:
            self.FIXED_COLLOCATION = json.loads(f.read())

        with open(config_path[2], 'r', encoding='utf-8') as f:
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

    ssml = f'''<speak xmlns="http://www.w3.org/2001/10/synthesis" 
                  xmlns:mstts="http://www.w3.org/2001/mstts" 
                  xmlns:emo="http://www.w3.org/2009/10/emotionml" 
                  version="1.0" 
                  xml:lang="zh-CN">
                <voice name="{config.VOICE}">
                  {vs_start}
                  <prosody rate="{config.SPEED}%" pitch="{config.PITCH}%">{text}</prosody>
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
    """将音频字节数据保存为文件"""
    if audio_bytes:
        with open(file=f'{config.STORED_FILEPATH}/{filename}.mp3', mode='wb') as f:
            f.write(audio_bytes)
        print(f"Audio saved to {filename}")
    else:
        print("No audio data to save.")


def play_mp3_file(mp3_file_path):
    """
    播放指定的 MP3 文件。

    Args:
        mp3_file_path (str or Path): MP3 文件的路径。

    Returns:
        bool: 播放成功返回 True，失败返回 False。
    """
    # 确保路径是 Path 对象以便检查
    file_path = Path(mp3_file_path)

    # 1. 等待文件出现
    while not file_path.exists():
        pass

    if not file_path.is_file():
        print(f"Error: Path is not a file: {file_path}")
        return False

    # 2. 初始化 pygame mixer (如果尚未初始化)
    # 这通常只需要在整个程序启动时做一次
    # 如果 mixer 已经初始化，再次调用 init() 通常无害，但最好检查一下
    if not pygame.mixer.get_init():
        try:
            # 根据您的音频文件调整参数 (可选，通常不指定也能工作)
            # frequency: 音频采样率, size: 位深度, channels: 声道数
            pygame.mixer.init(frequency=22050, size=-16, channels=2, buffer=512)
            print("Pygame mixer initialized.")
        except pygame.error as e:
            print(f"Failed to initialize pygame mixer: {e}")
            return False

    try:
        # 3. 加载 MP3 文件
        pygame.mixer.music.load(file_path)

        # 4. 播放音频
        pygame.mixer.music.play()

        # 5. 等待播放完成 (阻塞当前线程)
        # 这个循环检查 mixer 是否还在忙于播放
        while pygame.mixer.music.get_busy():
            time.sleep(0.1)  # 小休一下，避免占用过多CPU

        print(f"Playback finished: {file_path}")
        return True

    except pygame.error as e:
        print(f"Pygame error playing {file_path}: {e}")
        return False
    except Exception as e:
        print(f"Unexpected error playing {file_path}: {e}")
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
        if audio_data:
            t_save = time.perf_counter()
            save_audio_to_file(audio_data, config, hashed_text)
            print(f"[TTS]   落盘            {(time.perf_counter() - t_save) * 1000:.0f} ms")
        else:
            print("[TTS] 合成失败：本次没有拿到音频")
        # 将（可能刚创建的）文件加入播放队列
        play_in_background_queued(cache_file_path, requested_at)

    thread = threading.Thread(target=target, args=(text, config), daemon=True)
    thread.start()


# --- 清理函数 ---
def cleanup_tts_engine():
    """清理 TTS 引擎资源。"""
    global _stop_playback
    _stop_playback.set()  # 通知播放线程停止
    # 等待播放线程结束 (可选，daemon=True 线程会随主程序结束)
    # _playback_thread.join(timeout=2.0)
    cleanup_pygame()  # 清理 pygame
