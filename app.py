import threading

import lib.mainWindow
import lib.ttsEngine
import lib.globalHotkeyManager

if __name__ == '__main__':
    # 路径均相对「程序根目录」：源码运行时为项目根，打包后为 app.exe 所在目录，
    # 由 ttsEngine.resolve_path 解析，因此不再受启动时工作目录的影响
    config = lib.ttsEngine.Config("config/sound_model.json",
                                  "config/fixed_collocation.json",
                                  "config/word_replacement.json")
    # 按配置初始化音频输出（采样率 / 缓冲 / 播放方式）。
    # 放在这里而不是等第一次播放，这样设置不生效或设备打不开时，日志里立刻能看到。
    lib.ttsEngine.init_audio(config)
    # 后台预热与 TTS 服务的连接，不阻塞界面启动
    threading.Thread(target=lib.ttsEngine.warm_up, args=(config,), daemon=True).start()
    hotkey_manager = lib.globalHotkeyManager.GlobalHotkeyManager(config, 'config/shortcut_key.json')
    # 这里不启动快捷键监听：是否启用由界面上的「启用快捷键」勾选框决定
    # （TTSApp 初始化时该勾选框默认为未勾选，会主动 stop 监听器）
    root = lib.mainWindow.DraggableWindow()  # 使用可拖拽的窗口类
    app = lib.mainWindow.TTSApp(root, hotkey_manager, lib.ttsEngine.text_to_speech, config)  # 创建应用
    lib.ttsEngine.emit_log("[启动] 程序已就绪")
    root.mainloop()
    # 退出顺序：先停快捷键监听，再停播放线程与 mixer
    hotkey_manager.stop()
    lib.ttsEngine.cleanup_tts_engine()
