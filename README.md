# TTSAPP

一款借助xivCDN TTS PRO服务的tts生成软件。

---

### 简介

该软件旨在帮助想在固定队或Fps游戏中进行交流，但是又因为个人原因不方便开麦的人群方便快捷地进行语音沟通。

本软件使用的TTS技术基于游戏最终幻想14的FoxTTS插件中的肥肥咖啡（FFCafe）TTS Pro引擎，原网址为：[xivCDN TTS PRO](https://ttspro.xivcdn.com/)

---

### 快速开始

1. 下载最新的Release版本并解压。此时应当能看到以下几个内容：
   - cache（用于存放语音缓存文件）
   - config（用于存放配置文件）
   - app.exe（程序本体）
   
    **请不要随意修改文件位置**，否则可能会产生意想不到的bug。
2. 打开app.exe程序，输入你想要发音的文本，按下回车，即可听到合成的TTS语音！
3. 程序中置顶勾选框可以设置窗口置顶，启用快捷键勾选框可以设置快捷键功能是否启用。

---

### 自定义配置

本程序支持进行自定义配置，如更换语音引擎，设定语速、语调等功能。

打开config文件夹，应当可以看到以下几项文件：
- fixed_collocation.json（用于配置缩写词）
- shortcut_key.json（用于配置全局快捷键）
- sound_model.json（用于配置语音引擎）
- word_replacement.json（用于配置文本替换）

接下来会详细介绍各项文件该如何进行配置。

#### fixed_collocation.json

本文件用于配置缩写词。例如，当配置了`{"before": "0.o", "after": "尊嘟假嘟"}`一项后，在输入"0.o"时，会自动生成"尊嘟假嘟"语音。

配置方法如下：该json文件的格式类似：
`[{"before": 缩写词1, "after": 替换后的词1}, {"before": 缩写词2, "after": 替换后的词2}]`需要添加新的缩写词时，在json数组中添加一项即可。

#### shortcut_key.json

本文件用于配置全局快捷键。例如，当配置了`{"key": "<ctrl>+a", "text": "你好，世界！"}`一项后，当软件打开，且**启用快捷键选项被勾选**的情况下，按下ctrl+a组合键，即可自动生成"你好，世界！"语音。

配置方法如下：该json文件的格式类似：`[{"key": 快捷键1, "text": 语音1}, {"key": 快捷键2, "text": 语音2}]`需要添加新的快捷键时，在json数组中添加一项即可。

表示按键的方法为：<ctrl>, <shift>, <alt>等功能键需要使用<>进行包裹，abc、123、f1f2f3等按键直接写即可。

#### sound_model.json

本文件用于配置语音模型。其中，"STORED_FILEPATH"及以上的项目不建议进行修改。VOICE及以下的项目用于配置语音的各种属性，如语音模型，语调，语速，音高等。

VOICE和VOICE_STYLE词条使用的是微软讲述人的词条，可以自行进行查询。SPEED和PITCH两项是百分比进行调整，如SPEED=5，就是语速为105%。

#### word_replacement.json

本文件用于配置文本替换。可以选择是否使用正则表达式进行匹配。

配置方法如下：该json文件的格式类似：
`[{"before": 替换前的词1, "after": 替换后的词1, "re": true}, {"before": 替换前的词2, "after": 替换后的词2, "re": false}]`需要添加新的缩写词时，在json数组中添加一项即可。

如果re一项为true，将会进行正则表达式匹配并进行替换。如果re一项为false，则只进行简单的文本替换。

---

### 如何将我生成的语音导入到语音软件中

在此建议使用[voicemeeter](https://voicemeeter.com/)软件进行自定义语音输出位置，如将该软件输出的语音导入到语音软件中。当然也可以将其他软件，如bilibili正在播放的视频的声音导入到语音软件中。

在安装voicemeeter后，可以在音量合成器中自行配置应用的输入和输出设备。只需要将tts应用的输出设置为VoiceMeeter Input，将语音软件的输入设置为VoiceMeeter Output，即可实现语音转发。具体配置可以参考[这篇攻略](https://vb-audio.cn/post/140.html)中输入输出配置的部分。