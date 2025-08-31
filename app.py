import lib.mainWindow
import lib.ttsEngine

if __name__ == '__main__':
    config = lib.ttsEngine.Config("./config/sound_model.json")
    root = lib.mainWindow.DraggableWindow()  # 使用可拖拽的窗口类
    app = lib.mainWindow.TTSApp(root, lib.ttsEngine.text_to_speech, config)  # 创建应用
    root.mainloop()
