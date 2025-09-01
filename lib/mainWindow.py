import tkinter as tk
import win32gui
import win32con
from lib.globalHotkeyManager import GlobalHotkeyManager


class DraggableWindow(tk.Tk):
    """使窗口可以通过鼠标拖拽标题栏以外的区域移动"""

    def __init__(self):
        super().__init__()
        self.old_x = None
        self.old_y = None
        # 绑定鼠标左键按下和拖动事件
        self.bind("<ButtonPress-1>", self.on_start)
        self.bind("<B1-Motion>", self.on_drag)

    def on_start(self, event):
        """记录鼠标按下时的初始位置"""
        self.old_x = event.x
        self.old_y = event.y

    def on_drag(self, event):
        """根据鼠标移动距离计算窗口新位置并更新"""
        if self.old_x is not None and self.old_y is not None:
            deltax = event.x - self.old_x
            deltay = event.y - self.old_y
            x = self.winfo_x() + deltax
            y = self.winfo_y() + deltay
            self.geometry(f"+{x}+{y}")


class TTSApp:
    def __init__(self, root, hotkey_manager, func, *args):
        self.root = root
        self.root.title("tts")
        self.root.geometry("200x120")  # 与您代码中的尺寸一致
        self.root.resizable(False, False)  # 通常这类小工具窗口不可调整大小
        self.hotkey_manager = hotkey_manager

        # --- 创建主框架并居中所有内容 ---
        # 使用 place 配合 relx/rely/anchor 是实现居中的常用方法
        main_frame = tk.Frame(root)
        main_frame.place(relx=0.5, rely=0.5, anchor='center')

        # --- 1. Label: 提示输入 ---
        self.prompt_label = tk.Label(main_frame, text="在此输入语音")
        self.prompt_label.pack(pady=(0, 5))  # 添加一点下边距，与原代码视觉一致

        # --- 2. Entry: 文本输入框 ---
        self.text_entry = tk.Entry(main_frame, width=25)  # 设置宽度
        self.text_entry.pack(pady=(0, 5))
        self.text_entry.focus()  # 获得焦点

        # --- 3. Checkbox: 窗口置顶 ---
        self.top_var = tk.IntVar(value=0)  # 0 表示不置顶，1 表示置顶 (与您代码逻辑一致)
        self.top_checkbox = tk.Checkbutton(
            main_frame,
            text="窗口置顶",
            variable=self.top_var,
            onvalue=1,  # 勾选时值为1
            offvalue=0,  # 取消勾选时值为0
            command=self.toggle_topmost  # 当状态改变时调用此函数
        )
        self.top_checkbox.pack(pady=(5, 0))

        # --- 4. Checkbox: 启动全局快捷键 ---
        self.shortcut_var = tk.IntVar(value=0)
        self.shortcut_checkbox = tk.Checkbutton(
            main_frame,
            text="启用快捷键",
            variable=self.shortcut_var,
            onvalue=1,
            offvalue=0,
            command=self.update_shortcut_key
        )
        self.shortcut_checkbox.pack(pady=(5, 0))

        # --- 初始化窗口置顶状态 ---
        # 根据初始值 (0) 设置窗口不置顶
        self.update_window_topmost()

        # --- 初始化快捷键状态 ---
        # 根据初始值 (0) 设置快捷键不开启
        self.update_shortcut_key()

        # --- 绑定回车键提交 ---
        self.text_entry.bind('<Return>', lambda event: self.submit_text(func, *args))

    def update_shortcut_key(self):
        if self.shortcut_var.get() == 1:
            self.hotkey_manager.start()
        else:
            self.hotkey_manager.stop()

    def toggle_topmost(self):
        """复选框状态改变时调用，更新窗口置顶属性"""
        self.update_window_topmost()

    def update_window_topmost(self):
        """根据 top_var 的值设置窗口是否置顶"""
        hwnd = win32gui.FindWindow(None, self.root.title())
        if hwnd:
            if self.top_var.get() == 1:
                # 置顶
                win32gui.SetWindowPos(
                    hwnd,
                    win32con.HWND_TOPMOST,
                    0, 0, 0, 0,
                    win32con.SWP_NOMOVE | win32con.SWP_NOSIZE
                )
            else:
                # 取消置顶
                win32gui.SetWindowPos(
                    hwnd,
                    win32con.HWND_NOTOPMOST,
                    0, 0, 0, 0,
                    win32con.SWP_NOMOVE | win32con.SWP_NOSIZE
                )

    def submit_text(self, func, *args):
        """处理文本提交（示例函数，功能可扩展）"""
        user_input = self.text_entry.get().strip()
        if user_input:
            # 在这里可以添加调用 TTS 引擎、处理文本等逻辑
            if func is not None:
                func(user_input, *args)
            self.text_entry.delete(0, tk.END)  # 清空输入框
