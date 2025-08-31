import json
import threading
from pynput import keyboard
from pathlib import Path
from lib.ttsEngine import text_to_speech


class GlobalHotkeyManager:
    def __init__(self, config, shortcut_file='shortcut_key.json'):
        """
        初始化全局热键管理器。

        Args:
            config: TTS 配置对象。
            shortcut_file (str): 快捷键配置文件的路径。
        """
        self.config = config
        self.shortcut_file = Path(shortcut_file)
        self.hotkeys = {}  # 存储 {快捷键字符串: 文本} 的字典
        self.listener = None
        self._load_shortcuts()

    def _load_shortcuts(self):
        """从 JSON 文件加载快捷键配置。"""
        try:
            if not self.shortcut_file.exists():
                print(f"Shortcut file not found: {self.shortcut_file}")
                # 可以选择创建一个示例文件或使用默认值
                self._create_example_file()
                return

            with open(self.shortcut_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                # 验证数据结构
                if not isinstance(data, list):
                    raise ValueError("Shortcut file must contain a JSON array of objects.")
                self.hotkeys = {}
                for item in data:
                    if not isinstance(item, dict) or 'key' not in item or 'text' not in item:
                        print(f"Invalid shortcut item: {item}. Skipping.")
                        continue
                    key_str = item['key']
                    text = item['text']
                    self.hotkeys[key_str] = text
                    print(f"Loaded shortcut: {key_str} -> '{text}'")
        except Exception as e:
            print(f"Error loading shortcut file {self.shortcut_file}: {e}")

    def _create_example_file(self):
        """创建一个示例快捷键文件。"""
        example_data = [
            {"key": "<ctrl>+a", "text": "你好，世界！"},
            {"key": "<ctrl>+b", "text": "这是一条测试语音。"},
            {"key": "<ctrl>+c", "text": "快捷键功能正常。"}
        ]
        try:
            with open(self.shortcut_file, 'w', encoding='utf-8') as f:
                json.dump(example_data, f, ensure_ascii=False, indent=2)
            print(f"Example shortcut file created at: {self.shortcut_file}")
        except Exception as e:
            print(f"Failed to create example file: {e}")

    def _on_hotkey_triggered(self, key_str):
        """
        当某个全局快捷键被触发时调用的内部函数。

        Args:
            key_str (str): 被按下的快捷键字符串（如 "<ctrl>+a"）。
        """
        if key_str in self.hotkeys:
            text_to_speak = self.hotkeys[key_str]
            print(f"Global Hotkey Triggered: {key_str} -> '{text_to_speak}'")
            # 调用 TTS 引擎播放语音
            # 注意: ttsEngine.text_to_speech 是异步的（使用了线程），所以这里不会阻塞
            text_to_speech(text_to_speak, self.config)
        else:
            print(f"Unexpected hotkey triggered: {key_str}")

    def start(self):
        """启动全局热键监听器。"""
        if self.listener is not None:
            print("Hotkey listener is already running.")
            return

        # 构建 GlobalHotKeys 需要的字典
        # 格式: {"<key_combination>": callback_function}
        # 我们需要为每个快捷键创建一个绑定到 _on_hotkey_triggered 的函数
        # 使用 lambda 时需要注意闭包问题，用默认参数解决
        hotkey_callbacks = {
            key_str: lambda k=key_str: self._on_hotkey_triggered(k)
            for key_str in self.hotkeys.keys()
        }

        # 如果没有有效的快捷键，不启动监听器
        if not hotkey_callbacks:
            print("No valid hotkeys loaded. Listener not started.")
            return

        try:
            # 创建并启动监听器
            # 注意: GlobalHotKeys 在内部使用了线程
            self.listener = keyboard.GlobalHotKeys(hotkey_callbacks)
            self.listener.start()
            print("Global hotkey listener started.")
            # 通常不需要 join()，除非你想阻塞主线程
            # self.listener.join()
        except Exception as e:
            print(f"Failed to start global hotkey listener: {e}")
            self.listener = None

    def stop(self):
        """停止全局热键监听器。"""
        if self.listener:
            self.listener.stop()
            self.listener = None
            print("Global hotkey listener stopped.")

    def reload_shortcuts(self):
        """重新加载快捷键配置文件。"""
        old_hotkeys = self.hotkeys.copy()
        self._load_shortcuts()
        # 如果快捷键有变化，需要重启监听器
        if self.listener and old_hotkeys != self.hotkeys:
            print("Shortcut configuration changed. Restarting listener...")
            self.stop()
            self.start()
