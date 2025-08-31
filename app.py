import lib.mainWindow

if __name__ == '__main__':
    root = lib.mainWindow.DraggableWindow()  # 使用可拖拽的窗口类
    app = lib.mainWindow.TTSApp(root, None)  # 创建应用
    root.mainloop()
