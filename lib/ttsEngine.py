import hashlib
import json
import requests
import pygame
import time
import threading
import queue
from pathlib import Path


# --- 配置 ---
class Config:
    def __init__(self, config_path):
        self.API_ENDPOINT = None
        self.BASE_URL = None
        self.FULL_API_URL = None
        self.load(config_path)

    def load(self, config_path):
        with open(config_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
            # 直接将字典的键值对作为实例属性
            self.__dict__.update(data)
        # 计算派生属性
        self.FULL_API_URL = self.BASE_URL + self.API_ENDPOINT


# --- 配置结束 ---

# 创建一个全局的播放队列
_playback_queue = queue.Queue()

# 播放线程的控制事件
_stop_playback = threading.Event()


def _playback_worker():
    """播放队列中的音频文件的工作线程。"""
    while not _stop_playback.is_set():
        try:
            # 从队列中获取下一个要播放的文件路径
            # block=True, timeout=1.0 避免无限阻塞，允许检查 _stop_playback
            mp3_file_path = _playback_queue.get(timeout=1.0)

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
                pygame.mixer.music.load(file_path)
                pygame.mixer.music.play()

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

    # 3. 发送 POST 请求
    try:
        response = requests.post(
            url=config.FULL_API_URL,
            headers=headers,
            data=ssml.encode('utf-8'),  # 确保 SSML 字符串以 UTF-8 编码发送
            timeout=30  # 设置超时，避免请求挂起
        )

        # 4. 检查响应状态
        if response.status_code == 200:
            # 成功，返回音频数据 (bytes)
            return response.content
        else:
            print(f"TTS API Error: {response.status_code} - {response.text}")
            return None

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


def play_in_background_queued(mp3_path):
    """将播放请求添加到队列中。"""
    _playback_queue.put(mp3_path)
    print(f"Queued for playback: {mp3_path}")


def cleanup_pygame():
    """清理 pygame 资源。"""
    if pygame.mixer.get_init():
        pygame.mixer.quit()


def text_to_speech(text, config):
    def target(text, config):
        hashed_text = hashlib.md5(
            f'{config.VOICE}{config.VOICE_STYLE}{config.SPEED}{config.PITCH}{text}'.encode('utf-8')).hexdigest()
        cache_dir = Path(config.STORED_FILEPATH)
        cache_file_path = cache_dir / f"{hashed_text}.mp3"

        # 如果缓存文件存在，将其加入播放队列
        if cache_file_path.exists():
            play_in_background_queued(cache_file_path)
            return

        # 缓存不存在，生成音频
        audio_data = text_to_speech_web_api(text, config)
        if audio_data:
            save_audio_to_file(audio_data, config, hashed_text)
        # 将（可能刚创建的）文件加入播放队列
        play_in_background_queued(cache_file_path)

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
