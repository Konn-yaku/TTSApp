import lib.mainWindow
import lib.ttsEngine
import lib.globalHotkeyManager

if __name__ == '__main__':
    config = lib.ttsEngine.Config("./config/sound_model.json",
                                  "./config/fixed_collocation.json",
                                  "./config/word_replacement.json")
    hotkey_manager = lib.globalHotkeyManager.GlobalHotkeyManager(config, './config/shortcut_key.json')
    hotkey_manager.start()
    root = lib.mainWindow.DraggableWindow()  # 使用可拖拽的窗口类
    app = lib.mainWindow.TTSApp(root, hotkey_manager, lib.ttsEngine.text_to_speech, config)  # 创建应用
    root.mainloop()
    hotkey_manager.stop()
    lib.ttsEngine.cleanup_pygame()
