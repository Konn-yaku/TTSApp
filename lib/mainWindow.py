import tkinter as tk
import win32gui
import win32con
from lib.globalHotkeyManager import GlobalHotkeyManager
from lib.ttsEngine import ensure_warm, poll_logs


class DraggableWindow(tk.Tk):
    """支持「按住指定把手拖动窗口」的窗口类。

    拖动绑定只挂在把手控件上，而不是整个窗口 —— 否则在日志区里
    拖动鼠标想选中文字时，会变成拖动整个窗口。
    """

    def __init__(self):
        super().__init__()
        self.old_x = None
        self.old_y = None

    def make_drag_handle(self, widget):
        """把 widget 注册为拖拽把手。"""
        widget.bind("<ButtonPress-1>", self.on_start)
        widget.bind("<B1-Motion>", self.on_drag)

    def on_start(self, event):
        """记录鼠标按下时，指针相对窗口左上角的偏移。

        用屏幕坐标（x_root / y_root）而不是控件内坐标：
        拖动过程中窗口会跟着指针移动，用控件内坐标会让基准自己漂移，导致窗口抽动。
        """
        self.old_x = event.x_root - self.winfo_x()
        self.old_y = event.y_root - self.winfo_y()

    def on_drag(self, event):
        """按按住时记录的偏移量重新计算窗口位置"""
        if self.old_x is not None and self.old_y is not None:
            self.geometry(f"+{event.x_root - self.old_x}+{event.y_root - self.old_y}")


class TTSApp:
    # 日志区最多保留的行数，超出后丢弃最旧的，避免长时间运行内存无限增长
    LOG_MAX_LINES = 300

    def __init__(self, root, hotkey_manager, func, *args):
        self.root = root
        # args 是传给 func 的额外参数；按 app.py 的调用方式，args == (config,)
        self.config = args[0] if args else None
        self.root.title("tts")
        self.root.geometry("560x420")  # 上半部分是输入区，下半部分是合成日志
        self.root.resizable(False, False)  # 通常这类小工具窗口不可调整大小
        self.hotkey_manager = hotkey_manager

        # --- 拖拽把手 ---
        # 只有这一条能拖动窗口，日志区与输入框保持正常的鼠标行为
        self.drag_handle = tk.Label(root, text="按住此处拖动窗口", cursor="fleur")
        self.drag_handle.pack(fill='x', ipady=4)
        self.root.make_drag_handle(self.drag_handle)

        # --- 输入区 ---
        input_frame = tk.Frame(root)
        input_frame.pack(fill='x', padx=10, pady=(0, 8))

        # --- 1. Label: 提示输入 ---
        self.prompt_label = tk.Label(input_frame, text="在此输入语音")
        self.prompt_label.pack(pady=(0, 5))

        # --- 2. Entry: 文本输入框 ---
        # 用 fill='x' 让它跟着窗口宽度伸展，比旧版固定 width=25 更好用。
        # 绑一个 StringVar 是为了监听「框里出现文字」：不管是手打还是粘贴，
        # 内容一变就能收到通知（只绑 <Key> 的话，右键粘贴那条路径会漏掉）。
        self.text_var = tk.StringVar()
        self.text_entry = tk.Entry(input_frame, font=("Microsoft YaHei UI", 10),
                                   textvariable=self.text_var)
        self.text_entry.pack(fill='x', pady=(0, 6))
        self.text_entry.focus()  # 获得焦点
        self.text_var.trace_add('write', self._on_entry_changed)

        checkbox_frame = tk.Frame(input_frame)
        checkbox_frame.pack()

        # --- 3. Checkbox: 窗口置顶 ---
        self.top_var = tk.IntVar(value=0)  # 0 表示不置顶，1 表示置顶
        self.top_checkbox = tk.Checkbutton(
            checkbox_frame,
            text="窗口置顶",
            variable=self.top_var,
            onvalue=1,  # 勾选时值为1
            offvalue=0,  # 取消勾选时值为0
            command=self.toggle_topmost  # 当状态改变时调用此函数
        )
        self.top_checkbox.pack(side='left', padx=(0, 20))

        # --- 4. Checkbox: 启动全局快捷键 ---
        self.shortcut_var = tk.IntVar(value=0)
        self.shortcut_checkbox = tk.Checkbutton(
            checkbox_frame,
            text="启用快捷键",
            variable=self.shortcut_var,
            onvalue=1,
            offvalue=0,
            command=self.update_shortcut_key
        )
        self.shortcut_checkbox.pack(side='left')

        # --- 5. 日志区 ---
        log_frame = tk.Frame(root)
        log_frame.pack(fill='both', expand=True, padx=10, pady=(0, 10))

        self.log_scrollbar = tk.Scrollbar(log_frame, orient='vertical')
        self.log_scrollbar.pack(side='right', fill='y')

        self.log_text = tk.Text(
            log_frame,
            width=1,   # 宽度完全交给窗口布局决定，避免请求宽度把窗口撑大
            height=12,
            wrap='word',  # 万一某行过长，宁可折行也不要被裁掉
            font=("Microsoft YaHei UI", 9),
            state='disabled',  # 只读：不能编辑，但可以选中复制
            yscrollcommand=self.log_scrollbar.set,
        )
        self.log_text.pack(side='left', fill='both', expand=True)
        self.log_scrollbar.config(command=self.log_text.yview)
        # 只读状态下 Ctrl+C 不会自动生效，这里手动接上
        self.log_text.bind('<Control-c>', self._copy_selection)

        # --- 初始化窗口置顶状态 ---
        # 根据初始值 (0) 设置窗口不置顶
        self.update_window_topmost()

        # --- 初始化快捷键状态 ---
        # 根据初始值 (0) 设置快捷键不开启
        self.update_shortcut_key()

        # --- 绑定回车键提交 ---
        self.text_entry.bind('<Return>', lambda event: self.submit_text(func, *args))

        # --- 输入框里一出现文字就补一次连接预热 ---
        # 监听挂在上面 text_var 的 trace 上（见 _on_entry_changed）：
        # 长停顿（实测 100-300 秒）之后服务端会把空闲连接关掉，那一句就得重新握手；
        # 内容一变就后台补一条，等你按回车时连接通常已经建好。

        # --- 启动日志轮询 ---
        # 合成跑在工作线程里，只能先把日志放进队列，再由主线程取出来写控件
        self.root.after(100, self._pump_logs)

    def _on_entry_changed(self, *_args):
        """输入框内容一变就调用：里面出现文字时，补一次连接预热。

        监听「内容变化」而不是「按键」，是为了覆盖粘贴这条路径。
        ensure_warm 内部自己节流（只有空闲超过阈值才真正动手），
        所以可以放心地在每次变化时调用，不会变成发请求的洪水。
        """
        if self.config is None:
            return
        if not self.text_var.get().strip():
            return
        ensure_warm(self.config)

    def _append_log(self, message):
        """把一行日志写入日志区（只能在主线程调用）。"""
        self.log_text.configure(state='normal')
        self.log_text.insert('end', message + '\n')
        # 超出上限就丢掉最旧的行
        line_count = int(self.log_text.index('end-1c').split('.')[0])
        if line_count > self.LOG_MAX_LINES:
            self.log_text.delete('1.0', f'{line_count - self.LOG_MAX_LINES}.0')
        self.log_text.see('end')  # 自动滚到最新一行
        self.log_text.configure(state='disabled')

    def _pump_logs(self):
        """定时把队列里的日志搬到界面上，然后安排下一次。"""
        for message in poll_logs():
            self._append_log(message)
        self.root.after(100, self._pump_logs)

    def _copy_selection(self, event=None):
        """复制日志区里选中的文本。"""
        try:
            selected = self.log_text.get('sel.first', 'sel.last')
        except tk.TclError:
            return 'break'  # 没有选中任何内容
        self.root.clipboard_clear()
        self.root.clipboard_append(selected)
        return 'break'

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
