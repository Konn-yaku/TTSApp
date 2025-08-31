import requests
import pygame
import io

# --- 配置 ---
# 假设完整的API地址是 https://api.example.com/tts/v1
BASE_URL = "https://ttspro.xivcdn.com"  # 替换为实际的基地址
API_ENDPOINT = "/tts/v1"  # 与网页代码一致
FULL_API_URL = BASE_URL + API_ENDPOINT

# 从配置文件或变量中获取
API_KEY = "9cua5sn8cb385bza27gjgca5sjx8rwfn"  # 对应网页中的 apiKey.value
OUTPUT_FORMAT = "audio-24khz-48kbitrate-mono-mp3"  # 输出格式
# --- 配置结束 ---

def text_to_speech_web_api(text, voice, voice_style="general", speed="0", pitch="0"):
    """
    模拟网页行为，通过POST请求调用TTS API生成语音。

    Args:
        text (str): 要合成的文本。
        voice (str): 语音名称，如 "zh-CN-XiaoxiaoNeural"。
        voice_style (str): 语音风格，如 "cheerful", "sad", "general" 等。默认 "general"。
        speed (str): 语速百分比，如 "0", "10", "-5"。默认 "0"。
        pitch (str): 音调百分比，如 "0", "10", "-5"。默认 "0"。

    Returns:
        bytes: 返回的音频数据 (MP3 bytes)，如果成功。
               返回 None 如果请求失败。
    """
    # 1. 构造 SSML
    # 根据 voice_style 决定是否添加 <mstts:express-as> 标签
    vs_start = f'<mstts:express-as style="{voice_style}">' if voice_style.lower() != 'general' else ''
    vs_end = '</mstts:express-as>' if voice_style.lower() != 'general' else ''

    ssml = f'''<speak xmlns="http://www.w3.org/2001/10/synthesis" 
                  xmlns:mstts="http://www.w3.org/2001/mstts" 
                  xmlns:emo="http://www.w3.org/2009/10/emotionml" 
                  version="1.0" 
                  xml:lang="zh-CN">
                <voice name="{voice}">
                  {vs_start}
                  <prosody rate="{speed}%" pitch="{pitch}%">{text}</prosody>
                  {vs_end}
                </voice>
              </speak>'''

    # 2. 准备 Headers
    headers = {
        'Output-Format': OUTPUT_FORMAT,
        'Content-Type': 'application/ssml+xml',
        'FFCafe-Access-Token': API_KEY,  # 使用您的 API Key
        'Voice-Variant': voice.lower(),  # 语音变体，小写
    }

    # 3. 发送 POST 请求
    try:
        response = requests.post(
            url=FULL_API_URL,
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


def save_audio_to_file(audio_bytes, filename):
    """将音频字节数据保存为文件"""
    if audio_bytes:
        with open(filename, 'wb') as f:
            f.write(audio_bytes)
        print(f"Audio saved to {filename}")
    else:
        print("No audio data to save.")


# --- 使用示例 ---
if __name__ == "__main__":
    # 您的文本和参数
    TEXT_TO_SYNTHESIZE = "你好，这是一个通过模仿网页API调用生成的语音示例。"
    VOICE_NAME = "zh-CN-XiaoxiaoNeural"  # 需要确认API支持哪些语音
    VOICE_STYLE = "cheerful"  # 可选风格
    SPEED = "5"  # 语速 +5%
    PITCH = "-2"  # 音调 -2%

    # 调用函数
    audio_data = text_to_speech_web_api(
        text=TEXT_TO_SYNTHESIZE,
        voice=VOICE_NAME,
        voice_style=VOICE_STYLE,
        speed=SPEED,
        pitch=PITCH
    )

    # 保存到文件
    if audio_data:
        output_filename = "output_tts.mp3"
        save_audio_to_file(audio_data, output_filename)

        # 您也可以使用 pygame, playsound 等库直接播放 audio_data
        pygame.mixer.music.load(io.BytesIO(audio_data))
        pygame.mixer.music.play()
